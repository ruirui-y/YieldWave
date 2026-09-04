"""SQLite 数据库封装。

表：
- h30269_valuation : 历史估值（按 date 主键去重，UPSERT，永不删除更早历史）
- weekly_strategy  : 周度锁定策略（按 week_id 主键）
- positions        : 各波段仓状态（按 name 主键）
- trades           : 交易确认记录（自增 id）

设计原则：
- 更新只做 UPSERT，绝不清空历史。
- 即使抓取失败，历史数据也保留在库里。
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
import sqlite3
from decimal import Decimal
from typing import Dict, List, Optional

from .precision import D

# Decimal -> float：SQLite REAL 列无法直接绑定 Decimal，统一在写入边界转 float（保留 ~15 位有效数字）。
# 读回时由 models.from_row / 下方 D() 统一还原为 Decimal，保证显示与比较精度。
sqlite3.register_adapter(Decimal, float)

from .config import DB_PATH, ensure_dirs
from .models import (
    POS_EMPTY,
    PositionState,
    Trade,
    ValuationRecord,
    WeeklyStrategy,
    _to_date,
)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _to_dash(value) -> str:
    """日期 -> "YYYY-MM-DD"（统一缓存键格式）。"""
    if isinstance(value, _dt.date):
        return value.isoformat()
    s = str(value).strip()
    if "-" in s:
        return s[:10]
    if "/" in s:
        return s.replace("/", "-")[:10]
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _f(v) -> Optional[float]:
    """把 CSV/字符串安全地转成 float 或 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


class Database:
    def __init__(self, db_path: str = DB_PATH):
        ensure_dirs()
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.init_schema()

    # ---------- schema ----------
    def init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS h30269_valuation (
                date              TEXT PRIMARY KEY,
                index_code       TEXT,
                index_name       TEXT,
                dividend_yield_1 REAL,
                dividend_yield_2 REAL,
                pe_1             REAL,
                pe_2             REAL,
                close            REAL,
                source           TEXT,
                fetched_at       TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_strategy (
                week_id    TEXT PRIMARY KEY,
                start_date TEXT,
                M42        REAL,
                A_buy      REAL,
                A_sell     REAL,
                B_buy      REAL,
                B_sell     REAL,
                C_buy      REAL,
                C_sell     REAL,
                created_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                name       TEXT PRIMARY KEY,
                label      TEXT,
                percent    REAL,
                status     TEXT,
                buy_date   TEXT,
                buy_yield  REAL,
                buy_close  REAL,
                buy_price  REAL,
                sell_date  TEXT,
                sell_yield REAL,
                sell_price REAL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                position_name  TEXT,
                action         TEXT,
                signal_date    TEXT,
                execution_date TEXT,
                dividend_yield REAL,
                M42            REAL,
                threshold      REAL,
                percentage     REAL,
                etf_price      REAL,
                shares         REAL,
                amount         REAL,
                note           TEXT,
                created_at     TEXT
            )
            """
        )
        # 安全迁移：为已存在的库补充新列（不影响已有数据）
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN kind TEXT DEFAULT 'swing'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN amount REAL")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN note TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN signal_data_date TEXT")
        except sqlite3.OperationalError:
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fetch_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source         TEXT,
                fetch_time     TEXT,
                success        INTEGER,
                latest_data_date TEXT,
                records_count  INTEGER
            )
            """
        )
        # ---- 行情点位缓存（CSI 公开接口） ----
        # index_close：每日 H30269 收盘点位缓存，避免重复请求
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS index_close (
                date        TEXT PRIMARY KEY,
                index_code  TEXT,
                close       REAL,
                source      TEXT,
                fetched_at  TEXT
            )
            """
        )
        # last_quote：最后一次成功的“当前盘中点位”快照（网络失败时兜底使用）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS last_quote (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                index_code   TEXT,
                trade_date   TEXT,
                trade_time   TEXT,
                current      REAL,
                pre_close    REAL,
                source       TEXT,
                fetched_at   TEXT
            )
            """
        )
        # 每日估算 D/P2 历史：独立于官方 h30269_valuation，只保存“估算”口径。
        # trade_date 唯一键：同一天再次运行时 UPDATE，每天只保留最新一条。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS estimated_dp2_daily (
                trade_date     TEXT PRIMARY KEY,
                estimated_dp2  REAL,
                index_point    REAL,
                anchor_date    TEXT,
                anchor_dp2     REAL,
                anchor_close   REAL,
                saved_at       TEXT
            )
            """
        )
        self.conn.commit()

    # ---------- index close cache ----------
    def upsert_index_close(self, date: str, close: float, index_code: str = "H30269", source: str = "csindex") -> None:
        """按 date UPSERT 一条收盘点位。"""
        if close is None:
            return
        self.conn.execute(
            """
            INSERT INTO index_close (date, index_code, close, source, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                index_code=excluded.index_code,
                close=excluded.close,
                source=excluded.source,
                fetched_at=excluded.fetched_at
            """,
            (_to_dash(date), index_code, float(close), source, _now()),
        )
        self.conn.commit()

    def upsert_index_close_many(self, mapping: dict, index_code: str = "H30269", source: str = "csindex") -> int:
        """批量 UPSERT 收盘点位。单事务，失败回滚。"""
        if not mapping:
            return 0
        self.conn.execute("BEGIN")
        try:
            for d, c in mapping.items():
                if c is None:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO index_close (date, index_code, close, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                        index_code=excluded.index_code,
                        close=excluded.close,
                        source=excluded.source,
                        fetched_at=excluded.fetched_at
                    """,
                    (_to_dash(d), index_code, float(c), source, _now()),
                )
            self.conn.commit()
            return sum(1 for v in mapping.values() if v is not None)
        except Exception:
            self.conn.rollback()
            raise

    def get_index_close(self, date: str) -> Optional[float]:
        """按日期取 close（无则 None）。"""
        row = self.conn.execute(
            "SELECT close FROM index_close WHERE date=?", (_to_dash(date),)
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def get_index_close_range(self, start_date: str, end_date: str) -> "Dict[str, float]":
        """取 [start, end] 闭区间内缓存的 close 字典（{YYYY-MM-DD: float}）。"""
        rows = self.conn.execute(
            "SELECT date, close FROM index_close WHERE date BETWEEN ? AND ? ORDER BY date ASC",
            (_to_dash(start_date), _to_dash(end_date)),
        ).fetchall()
        return {r["date"]: float(r["close"]) for r in rows if r["close"] is not None}

    def latest_cached_close_date(self) -> Optional[str]:
        """缓存中最新 close 的日期（无则 None）。"""
        row = self.conn.execute(
            "SELECT MAX(date) FROM index_close"
        ).fetchone()
        return row[0] if row and row[0] else None

    # ---------- last quote (intraday current point) ----------
    def save_last_quote(self, quote) -> None:
        """保存最后一次成功的盘中点位快照（id=1 单行）。"""
        from .services.market_quote import CurrentQuote
        if quote is None:
            return
        self.conn.execute(
            """
            INSERT INTO last_quote (id, index_code, trade_date, trade_time, current, pre_close, source, fetched_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                index_code=excluded.index_code,
                trade_date=excluded.trade_date,
                trade_time=excluded.trade_time,
                current=excluded.current,
                pre_close=excluded.pre_close,
                source=excluded.source,
                fetched_at=excluded.fetched_at
            """,
            (
                getattr(quote, "index_code", "H30269") if hasattr(quote, "index_code") else "H30269",
                quote.trade_date, quote.trade_time,
                quote.current, quote.pre_close,
                getattr(quote, "source", "csindex"),
                _now(),
            ),
        )
        self.conn.commit()

    def get_last_quote(self):
        """读最后一次成功的盘中点位快照（无则 None）。"""
        from .services.market_quote import CurrentQuote
        row = self.conn.execute(
            "SELECT * FROM last_quote WHERE id=1"
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return CurrentQuote(
            trade_date=r.get("trade_date", ""),
            trade_time=r.get("trade_time", ""),
            current=r.get("current"),
            pre_close=r.get("pre_close"),
            source=r.get("source", "csindex"),
        )

    # ---------- estimated dp2 daily ----------
    def upsert_estimated_dp2(
        self,
        trade_date: str,
        estimated_dp2,
        index_point,
        anchor_date: str,
        anchor_dp2,
        anchor_close,
        saved_at: Optional[str] = None,
    ) -> None:
        """按 trade_date UPSERT 每日估算 D/P2。

        - 当天第一次运行 -> INSERT
        - 当天再次运行 -> UPDATE，只保留最新一条
        - 与官方 h30269_valuation 完全独立，绝不混表
        """
        if estimated_dp2 is None:
            return
        self.conn.execute(
            """
            INSERT INTO estimated_dp2_daily
                (trade_date, estimated_dp2, index_point, anchor_date,
                 anchor_dp2, anchor_close, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                estimated_dp2=excluded.estimated_dp2,
                index_point=excluded.index_point,
                anchor_date=excluded.anchor_date,
                anchor_dp2=excluded.anchor_dp2,
                anchor_close=excluded.anchor_close,
                saved_at=excluded.saved_at
            """,
            (
                _to_dash(trade_date),
                float(D(estimated_dp2)),
                float(D(index_point)) if index_point is not None else None,
                _to_dash(anchor_date) if anchor_date else None,
                float(D(anchor_dp2)) if anchor_dp2 is not None else None,
                float(D(anchor_close)) if anchor_close is not None else None,
                saved_at or _now(),
            ),
        )
        self.conn.commit()

    def get_estimated_dp2(self, trade_date: str) -> Optional[dict]:
        """按交易日读取一条估算 D/P2，Decimal 字段还原为 Decimal。"""
        row = self.conn.execute(
            "SELECT * FROM estimated_dp2_daily WHERE trade_date=?",
            (_to_dash(trade_date),),
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return {
            "trade_date": r["trade_date"],
            "estimated_dp2": D(r["estimated_dp2"]),
            "index_point": D(r["index_point"]) if r["index_point"] is not None else None,
            "anchor_date": r["anchor_date"],
            "anchor_dp2": D(r["anchor_dp2"]) if r["anchor_dp2"] is not None else None,
            "anchor_close": D(r["anchor_close"]) if r["anchor_close"] is not None else None,
            "saved_at": r["saved_at"],
        }

    def get_all_estimated_dp2(self, ascending: bool = True) -> List[dict]:
        """读取全部每日估算历史（按 trade_date 排序）。"""
        order = "ASC" if ascending else "DESC"
        rows = self.conn.execute(
            f"SELECT * FROM estimated_dp2_daily ORDER BY trade_date {order}"
        ).fetchall()
        out = []
        for row in rows:
            r = dict(row)
            out.append({
                "trade_date": r["trade_date"],
                "estimated_dp2": D(r["estimated_dp2"]),
                "index_point": D(r["index_point"]) if r["index_point"] is not None else None,
                "anchor_date": r["anchor_date"],
                "anchor_dp2": D(r["anchor_dp2"]) if r["anchor_dp2"] is not None else None,
                "anchor_close": D(r["anchor_close"]) if r["anchor_close"] is not None else None,
                "saved_at": r["saved_at"],
            })
        return out

    def count_estimated_dp2(self) -> int:
        """统计已持久化的每日估算条数。"""
        row = self.conn.execute("SELECT COUNT(*) FROM estimated_dp2_daily").fetchone()
        return int(row[0]) if row else 0

    # ---------- valuation ----------
    def upsert_valuation(self, rec: ValuationRecord, commit: bool = True) -> None:
        """按 date UPSERT：已存在的日期更新，新的日期插入。"""
        row = rec.to_row()
        row["fetched_at"] = rec.fetched_at or _now()
        self.conn.execute(
            """
            INSERT INTO h30269_valuation
                (date, index_code, index_name, dividend_yield_1, dividend_yield_2,
                 pe_1, pe_2, close, source, fetched_at)
            VALUES (:date, :index_code, :index_name, :dividend_yield_1, :dividend_yield_2,
                    :pe_1, :pe_2, :close, :source, :fetched_at)
            ON CONFLICT(date) DO UPDATE SET
                index_code=excluded.index_code,
                index_name=excluded.index_name,
                dividend_yield_1=excluded.dividend_yield_1,
                dividend_yield_2=excluded.dividend_yield_2,
                pe_1=excluded.pe_1,
                pe_2=excluded.pe_2,
                close=excluded.close,
                source=excluded.source,
                fetched_at=excluded.fetched_at
            """,
            row,
        )
        if commit:
            self.conn.commit()

    def upsert_many(self, recs: List[ValuationRecord]) -> int:
        """批量 UPSERT：单事务 + 一次提交。任一条失败整体回滚，绝不出现半批更新。"""
        self.conn.execute("BEGIN")
        try:
            for r in recs:
                self.upsert_valuation(r, commit=False)
            self.conn.commit()
            return len(recs)
        except Exception:
            self.conn.rollback()
            raise

    def get_all_valuations(self, ascending: bool = True) -> List[ValuationRecord]:
        order = "ASC" if ascending else "DESC"
        rows = self.conn.execute(
            f"SELECT * FROM h30269_valuation ORDER BY date {order}"
        ).fetchall()
        return [ValuationRecord.from_row(dict(r)) for r in rows]

    def get_latest(self) -> Optional[ValuationRecord]:
        row = self.conn.execute(
            "SELECT * FROM h30269_valuation ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return ValuationRecord.from_row(dict(row)) if row else None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM h30269_valuation").fetchone()[0]

    def earliest_date(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MIN(date) FROM h30269_valuation"
        ).fetchone()
        return row[0] if row and row[0] else None

    def latest_date(self) -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date) FROM h30269_valuation"
        ).fetchone()
        return row[0] if row and row[0] else None

    def get_by_date(self, date: str) -> Optional[ValuationRecord]:
        row = self.conn.execute(
            "SELECT * FROM h30269_valuation WHERE date=?", (date,)
        ).fetchone()
        return ValuationRecord.from_row(dict(row)) if row else None

    def delete_by_date(self, date: str) -> None:
        """仅用于“手工修改/删除一条数据”功能，需要二次确认后才调用。"""
        self.conn.execute("DELETE FROM h30269_valuation WHERE date=?", (date,))
        self.conn.commit()

    def dedupe(self) -> int:
        """去除重复 date（保留最新 fetched_at 的一条）。返回删除条数。"""
        rows = self.conn.execute(
            """
            SELECT date, COUNT(*) c FROM h30269_valuation GROUP BY date HAVING c > 1
            """
        ).fetchall()
        removed = 0
        for r in rows:
            d = r["date"]
            # 保留 fetched_at 最大的一条
            keep = self.conn.execute(
                "SELECT fetched_at FROM h30269_valuation WHERE date=? ORDER BY fetched_at DESC LIMIT 1",
                (d,),
            ).fetchone()[0]
            self.conn.execute(
                "DELETE FROM h30269_valuation WHERE date=? AND fetched_at<>?", (d, keep)
            )
            removed += 1
        self.conn.commit()
        return removed

    def missing_trading_days_check(self, expected_gap_days: int = 4) -> List[str]:
        """粗略检查缺失：相邻记录日期差 > expected_gap_days 视为可能有缺失交易日。"""
        rows = self.conn.execute(
            "SELECT date FROM h30269_valuation ORDER BY date ASC"
        ).fetchall()
        gaps: List[str] = []
        prev = None
        for r in rows:
            d = _dt.date.fromisoformat(r["date"])
            if prev is not None:
                gap = (d - prev).days
                if gap > expected_gap_days:
                    gaps.append(f"{prev.isoformat()} -> {d.isoformat()} 间隔 {gap} 天")
            prev = d
        return gaps

    # ---------- weekly strategy ----------
    def save_weekly_strategy(self, ws: WeeklyStrategy, force: bool = False) -> bool:
        """保存周度锁定策略。

        - force=False（默认，普通 UI）：若 week_id 已存在则**禁止覆盖**（INSERT OR IGNORE），
          返回 False 表示本周已锁定、未改动。
        - force=True（仅开发/维护模式）：允许覆盖原策略。
        返回 True 表示本次写入生效（新增或强制覆盖）。
        """
        params = {
            "week_id": ws.week_id,
            "start_date": ws.start_date,
            "M42": ws.m42,
            "A_buy": ws.a_buy, "A_sell": ws.a_sell,
            "B_buy": ws.b_buy, "B_sell": ws.b_sell,
            "C_buy": ws.c_buy, "C_sell": ws.c_sell,
            "created_at": ws.created_at or _now(),
        }
        if not force:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO weekly_strategy
                    (week_id, start_date, M42, A_buy, A_sell, B_buy, B_sell, C_buy, C_sell, created_at)
                VALUES (:week_id, :start_date, :M42, :A_buy, :A_sell, :B_buy, :B_sell, :C_buy, :C_sell, :created_at)
                """,
                params,
            )
            self.conn.commit()
            return cur.rowcount > 0
        self.conn.execute(
            """
            INSERT INTO weekly_strategy
                (week_id, start_date, M42, A_buy, A_sell, B_buy, B_sell, C_buy, C_sell, created_at)
            VALUES (:week_id, :start_date, :M42, :A_buy, :A_sell, :B_buy, :B_sell, :C_buy, :C_sell, :created_at)
            ON CONFLICT(week_id) DO UPDATE SET
                start_date=excluded.start_date,
                M42=excluded.M42,
                A_buy=excluded.A_buy, A_sell=excluded.A_sell,
                B_buy=excluded.B_buy, B_sell=excluded.B_sell,
                C_buy=excluded.C_buy, C_sell=excluded.C_sell,
                created_at=excluded.created_at
            """,
            params,
        )
        self.conn.commit()
        return True

    def get_weekly_strategy(self, week_id: str) -> Optional[WeeklyStrategy]:
        row = self.conn.execute(
            "SELECT * FROM weekly_strategy WHERE week_id=?", (week_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return WeeklyStrategy(
            week_id=r["week_id"], start_date=r["start_date"], m42=D(r["M42"]),
            a_buy=D(r["A_buy"]), a_sell=D(r["A_sell"]), b_buy=D(r["B_buy"]), b_sell=D(r["B_sell"]),
            c_buy=D(r["C_buy"]), c_sell=D(r["C_sell"]), created_at=r.get("created_at"),
        )

    # ---------- positions ----------
    @staticmethod
    def _all_position_specs(config: dict) -> "dict":
        """把 config 中的 swing(positions) 与 core(core_tranches) 合并为统一的仓位规格。"""
        specs: dict = {}
        for name, p in config.get("positions", {}).items():
            specs[name] = {
                "label": p.get("label", name),
                "percent": float(p["percent"]),
                "kind": "swing",
            }
        for name, p in config.get("core_tranches", {}).items():
            specs[name] = {
                "label": p.get("label", name),
                "percent": float(p["percent"]),
                "kind": "core",
            }
        return specs

    def ensure_positions(self, config: dict) -> None:
        """安全初始化/迁移 positions 表。

        - 已有 name：仅 UPDATE label / percent / kind（保留 status、买卖日期、成交价、历史）。
        - 新 name（如 CORE1/CORE2/CORE3）：INSERT EMPTY。
        全程单事务，绝不破坏已有 EMPTY/HOLDING 状态与历史成交记录。

        入参兼容两种写法：
        - 完整配置 {"positions": {...}, "core_tranches": {...}}（UI / 迁移用）
        - 仅 positions 映射 {"A": {...}, "B": {...}}（旧测试 / 旧调用兼容）
        """
        if "positions" not in config and "core_tranches" not in config:
            # 兼容旧调用：直接把整个 dict 当作 positions 映射
            config = {"positions": config, "core_tranches": {}}
        specs = self._all_position_specs(config)
        self.conn.execute("BEGIN")
        try:
            for name, s in specs.items():
                row = self.conn.execute(
                    "SELECT name FROM positions WHERE name=?", (name,)
                ).fetchone()
                if row:
                    self.conn.execute(
                        "UPDATE positions SET label=?, percent=?, kind=? WHERE name=?",
                        (s["label"], s["percent"], s["kind"], name),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT INTO positions
                            (name, label, percent, kind, status, buy_date, buy_yield,
                             buy_close, buy_price, amount, note, sell_date, sell_yield, sell_price)
                        VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                        """,
                        (name, s["label"], s["percent"], s["kind"], POS_EMPTY),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def get_positions(self) -> List[PositionState]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY name").fetchall()
        return [PositionState.from_row(dict(r)) for r in rows]

    def get_position(self, name: str) -> Optional[PositionState]:
        row = self.conn.execute("SELECT * FROM positions WHERE name=?", (name,)).fetchone()
        return PositionState.from_row(dict(row)) if row else None

    def save_position(self, ps: PositionState) -> None:
        self.conn.execute(
            """
            INSERT INTO positions
                (name, label, percent, kind, status, buy_date, buy_yield, buy_close,
                 buy_price, amount, note, sell_date, sell_yield, sell_price)
            VALUES (:name, :label, :percent, :kind, :status, :buy_date, :buy_yield, :buy_close,
                    :buy_price, :amount, :note, :sell_date, :sell_yield, :sell_price)
            ON CONFLICT(name) DO UPDATE SET
                label=excluded.label, percent=excluded.percent, kind=excluded.kind,
                status=excluded.status,
                buy_date=excluded.buy_date, buy_yield=excluded.buy_yield,
                buy_close=excluded.buy_close, buy_price=excluded.buy_price,
                amount=excluded.amount, note=excluded.note,
                sell_date=excluded.sell_date, sell_yield=excluded.sell_yield,
                sell_price=excluded.sell_price
            """,
            {
                "name": ps.name, "label": ps.label, "percent": ps.percent, "kind": ps.kind,
                "status": ps.status, "buy_date": ps.buy_date, "buy_yield": ps.buy_yield,
                "buy_close": ps.buy_close, "buy_price": ps.buy_price, "amount": ps.amount,
                "note": ps.note, "sell_date": ps.sell_date, "sell_yield": ps.sell_yield,
                "sell_price": ps.sell_price,
            },
        )
        self.conn.commit()

    # ---------- trades ----------
    def add_trade(self, t: Trade) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO trades
                (position_name, action, signal_date, execution_date, signal_data_date,
                 dividend_yield, M42, threshold, percentage, etf_price, shares, amount, note, created_at)
            VALUES (:position_name, :action, :signal_date, :execution_date, :signal_data_date,
                    :dividend_yield, :M42, :threshold, :percentage, :etf_price, :shares, :amount, :note, :created_at)
            """,
            {
                "position_name": t.position_name, "action": t.action,
                "signal_date": t.signal_date, "execution_date": t.execution_date,
                "signal_data_date": t.signal_data_date,
                "dividend_yield": t.dividend_yield, "M42": t.m42, "threshold": t.threshold,
                "percentage": t.percentage, "etf_price": t.etf_price, "shares": t.shares,
                "amount": t.amount, "note": t.note, "created_at": t.created_at or _now(),
            },
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_trades(self, position_name: Optional[str] = None) -> List[Trade]:
        if position_name:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE position_name=? ORDER BY id DESC",
                (position_name,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM trades ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append(Trade(
                id=d["id"], position_name=d["position_name"], action=d["action"],
                signal_date=d["signal_date"], execution_date=d["execution_date"],
                signal_data_date=d.get("signal_data_date"),
                dividend_yield=D(d.get("dividend_yield")), m42=D(d.get("M42")),
                threshold=D(d.get("threshold")), percentage=D(d.get("percentage")),
                etf_price=d.get("etf_price"), shares=d.get("shares"), amount=d.get("amount"),
                note=d.get("note"), created_at=d.get("created_at"),
            ))
        return out

    # ---------- CSV 导入导出 ----------
    def export_csv(self, path: str) -> int:
        """导出全部历史为 CSV。返回导出条数。"""
        rows = self.conn.execute(
            "SELECT date, index_code, index_name, dividend_yield_1, dividend_yield_2, "
            "pe_1, pe_2, close, source, fetched_at FROM h30269_valuation ORDER BY date ASC"
        ).fetchall()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "date", "index_code", "index_name", "dividend_yield_1",
                "dividend_yield_2", "pe_1", "pe_2", "close", "source", "fetched_at",
            ])
            for r in rows:
                w.writerow(list(r))
        return len(rows)

    def import_csv(self, path: str, replace: bool = False) -> int:
        """从 CSV 导入（按 date UPSERT）。replace=True 时先清空表再导入。"""
        if replace:
            # 明确的“整表替换”属于危险操作，调用方必须二次确认后显式传入 replace=True
            self.conn.execute("DELETE FROM h30269_valuation")
            self.conn.commit()
        n = 0
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rec = ValuationRecord(
                    date=_to_date(row["date"]),
                    index_code=row.get("index_code", ""),
                    index_name=row.get("index_name", ""),
                    dividend_yield_1=D(row.get("dividend_yield_1")),
                    dividend_yield_2=D(row.get("dividend_yield_2")),
                    pe_1=D(row.get("pe_1")),
                    pe_2=D(row.get("pe_2")),
                    close=D(row.get("close")),
                    source=row.get("source", "import"),
                    fetched_at=row.get("fetched_at"),
                )
                self.upsert_valuation(rec)
                n += 1
        return n

    def close(self) -> None:
        self.conn.close()

    def backup(self, dest: Optional[str] = None) -> str:
        """生成一致性备份。使用 sqlite3.Connection.backup() 把整个数据库（含 WAL 中
        尚未 checkpoint 的事务）原子地拷贝到目标文件，避免 shutil.copyfile 漏掉 WAL 数据。

        返回备份路径。
        """
        if dest is None:
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = self.db_path + f".bak_{ts}"
        # 先把 WAL 合并进主库，确保备份起点一致
        self.conn.execute("PRAGMA wal_checkpoint(FULL);")
        src = sqlite3.connect(self.db_path)
        try:
            src.backup(sqlite3.connect(dest))
        finally:
            src.close()
        return dest

    def restore(self, path: str) -> None:
        """从备份文件恢复：用备份覆盖当前库（含结构）。调用方需自行确认。"""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.conn.execute("PRAGMA wal_checkpoint(FULL);")
        src = sqlite3.connect(path)
        try:
            src.backup(self.conn)
        finally:
            src.close()
        self.conn.commit()

    # ---------- fetch_log（每日抓取次数控制） ----------
    def log_fetch(
        self, source: str, success: bool, latest_data_date: Optional[str], records_count: int
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fetch_log (source, fetch_time, success, latest_data_date, records_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, _now(), 1 if success else 0, latest_data_date, records_count),
        )
        self.conn.commit()

    def fetch_count_today(self, source: str) -> int:
        today = _dt.date.today().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_log WHERE source=? AND fetch_time LIKE ?",
            (source, today + "%"),
        ).fetchone()
        return int(row[0]) if row else 0

    def last_fetch_success(self, source: str) -> Optional[bool]:
        row = self.conn.execute(
            "SELECT success FROM fetch_log WHERE source=? ORDER BY id DESC LIMIT 1", (source,)
        ).fetchone()
        return bool(row[0]) if row else None
