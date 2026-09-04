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
from typing import List, Optional

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
        self.conn.commit()

    # ---------- valuation ----------
    def upsert_valuation(self, rec: ValuationRecord) -> None:
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
        self.conn.commit()

    def upsert_many(self, recs: List[ValuationRecord]) -> int:
        n = 0
        for r in recs:
            self.upsert_valuation(r)
            n += 1
        return n

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
    def save_weekly_strategy(self, ws: WeeklyStrategy) -> None:
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
            {
                "week_id": ws.week_id,
                "start_date": ws.start_date,
                "M42": ws.m42,
                "A_buy": ws.a_buy, "A_sell": ws.a_sell,
                "B_buy": ws.b_buy, "B_sell": ws.b_sell,
                "C_buy": ws.c_buy, "C_sell": ws.c_sell,
                "created_at": ws.created_at or _now(),
            },
        )
        self.conn.commit()

    def get_weekly_strategy(self, week_id: str) -> Optional[WeeklyStrategy]:
        row = self.conn.execute(
            "SELECT * FROM weekly_strategy WHERE week_id=?", (week_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        return WeeklyStrategy(
            week_id=r["week_id"], start_date=r["start_date"], m42=r["M42"],
            a_buy=r["A_buy"], a_sell=r["A_sell"], b_buy=r["B_buy"], b_sell=r["B_sell"],
            c_buy=r["C_buy"], c_sell=r["C_sell"], created_at=r.get("created_at"),
        )

    # ---------- positions ----------
    def ensure_positions(self, config_positions: dict) -> None:
        """初始化 positions 表（若没有该 name 则插入默认值）。"""
        for name, p in config_positions.items():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO positions
                    (name, label, percent, status, buy_date, buy_yield, buy_close,
                     buy_price, sell_date, sell_yield, sell_price)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                (name, p.get("label", name), float(p["percent"]), POS_EMPTY),
            )
        self.conn.commit()

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
                (name, label, percent, status, buy_date, buy_yield, buy_close,
                 buy_price, sell_date, sell_yield, sell_price)
            VALUES (:name, :label, :percent, :status, :buy_date, :buy_yield, :buy_close,
                    :buy_price, :sell_date, :sell_yield, :sell_price)
            ON CONFLICT(name) DO UPDATE SET
                label=excluded.label, percent=excluded.percent, status=excluded.status,
                buy_date=excluded.buy_date, buy_yield=excluded.buy_yield,
                buy_close=excluded.buy_close, buy_price=excluded.buy_price,
                sell_date=excluded.sell_date, sell_yield=excluded.sell_yield,
                sell_price=excluded.sell_price
            """,
            {
                "name": ps.name, "label": ps.label, "percent": ps.percent,
                "status": ps.status, "buy_date": ps.buy_date, "buy_yield": ps.buy_yield,
                "buy_close": ps.buy_close, "buy_price": ps.buy_price,
                "sell_date": ps.sell_date, "sell_yield": ps.sell_yield,
                "sell_price": ps.sell_price,
            },
        )
        self.conn.commit()

    # ---------- trades ----------
    def add_trade(self, t: Trade) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO trades
                (position_name, action, signal_date, execution_date, dividend_yield,
                 M42, threshold, percentage, etf_price, shares, amount, note, created_at)
            VALUES (:position_name, :action, :signal_date, :execution_date, :dividend_yield,
                    :M42, :threshold, :percentage, :etf_price, :shares, :amount, :note, :created_at)
            """,
            {
                "position_name": t.position_name, "action": t.action,
                "signal_date": t.signal_date, "execution_date": t.execution_date,
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
                dividend_yield=d.get("dividend_yield"), m42=d.get("M42"),
                threshold=d.get("threshold"), percentage=d.get("percentage"),
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
                    dividend_yield_1=_f(row.get("dividend_yield_1")),
                    dividend_yield_2=_f(row.get("dividend_yield_2")),
                    pe_1=_f(row.get("pe_1")),
                    pe_2=_f(row.get("pe_2")),
                    close=_f(row.get("close")),
                    source=row.get("source", "import"),
                    fetched_at=row.get("fetched_at"),
                )
                self.upsert_valuation(rec)
                n += 1
        return n

    def close(self) -> None:
        self.conn.close()

    def backup(self, dest: Optional[str] = None) -> str:
        """复制整个数据库文件作为备份。返回备份路径。"""
        import shutil

        if dest is None:
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = self.db_path + f".bak_{ts}"
        shutil.copyfile(self.db_path, dest)
        return dest
