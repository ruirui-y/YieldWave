"""数据模型定义。

约定：
- 日期统一为 date 对象（或 ISO 字符串 "YYYY-MM-DD"）。
- 股息率 / PE 一律以“百分数”存储（4.77 表示 4.77%，而不是 0.0477）。
- 偏移量 buy_offset / sell_offset 单位为“百分点”（0.02 表示 +0.02 个百分点）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .precision import D


def _to_date(value) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        return _dt.date.fromisoformat(value[:10])
    if isinstance(value, (int, float)):
        # 可能是类似 20260902 的整数
        s = str(int(value))
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"无法解析日期: {value!r}")


@dataclass
class ValuationRecord:
    date: _dt.date
    index_code: str
    index_name: str
    dividend_yield_1: Optional[Decimal] = None
    dividend_yield_2: Optional[Decimal] = None
    pe_1: Optional[Decimal] = None
    pe_2: Optional[Decimal] = None
    close: Optional[Decimal] = None
    source: str = "honglicha"
    fetched_at: Optional[str] = None

    @staticmethod
    def from_row(row: dict) -> "ValuationRecord":
        return ValuationRecord(
            date=_to_date(row["date"]),
            index_code=row["index_code"],
            index_name=row["index_name"],
            dividend_yield_1=D(row.get("dividend_yield_1")),
            dividend_yield_2=D(row.get("dividend_yield_2")),
            pe_1=D(row.get("pe_1")),
            pe_2=D(row.get("pe_2")),
            close=D(row.get("close")),
            source=row.get("source", "honglicha"),
            fetched_at=row.get("fetched_at"),
        )

    def to_row(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "index_code": self.index_code,
            "index_name": self.index_name,
            "dividend_yield_1": self.dividend_yield_1,
            "dividend_yield_2": self.dividend_yield_2,
            "pe_1": self.pe_1,
            "pe_2": self.pe_2,
            "close": self.close,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


# 仓位状态常量
POS_EMPTY = "EMPTY"
POS_HOLDING = "HOLDING"


@dataclass
class PositionState:
    name: str  # "A" / "B" / "C" / "CORE1" / "CORE2" / "CORE3"
    label: str  # "A仓" / "核心1"
    percent: Decimal  # 占总资金百分比，例如 20（Decimal，保留完整精度）
    status: str = POS_EMPTY
    kind: str = "swing"  # "swing" 波段仓（有自动信号） / "core" 核心仓（仅手动建仓）
    buy_date: Optional[str] = None
    buy_yield: Optional[Decimal] = None
    buy_close: Optional[float] = None
    buy_price: Optional[float] = None  # ETF 实际成交价（手工录入）
    amount: Optional[float] = None  # 实际投入金额（手工录入）
    note: Optional[str] = None
    sell_date: Optional[str] = None
    sell_yield: Optional[Decimal] = None
    sell_price: Optional[float] = None

    @staticmethod
    def from_row(row: dict) -> "PositionState":
        return PositionState(
            name=row["name"],
            label=row.get("label", row["name"]),
            percent=D(row.get("percent", 0)),
            status=row.get("status", POS_EMPTY),
            kind=row.get("kind", "core" if str(row["name"]).startswith("CORE") else "swing"),
            buy_date=row.get("buy_date"),
            buy_yield=D(row.get("buy_yield")),
            buy_close=row.get("buy_close"),
            buy_price=row.get("buy_price"),
            amount=row.get("amount"),
            note=row.get("note"),
            sell_date=row.get("sell_date"),
            sell_yield=D(row.get("sell_yield")),
            sell_price=row.get("sell_price"),
        )


@dataclass
class WeeklyStrategy:
    week_id: str
    start_date: str
    m42: Decimal
    a_buy: Decimal
    a_sell: Decimal
    b_buy: Decimal
    b_sell: Decimal
    c_buy: Decimal
    c_sell: Decimal
    created_at: Optional[str] = None


@dataclass
class Trade:
    id: Optional[int]
    position_name: str
    action: str  # "BUY" / "SELL"
    signal_date: str  # 信号生成/记录日期（通常 = 执行日期，用于审计）
    execution_date: str  # 实际成交日期（用户确认成交当天）
    signal_data_date: Optional[str] = None  # 产生信号所依据的官方估值日期（dividend_yield_2 数据日期）
    dividend_yield: Optional[Decimal] = None
    m42: Optional[Decimal] = None  # 产生信号时的“本周锁定 M42”
    threshold: Optional[Decimal] = None  # 产生信号时的“本周锁定”买卖线
    percentage: Optional[Decimal] = None
    etf_price: Optional[float] = None
    shares: Optional[float] = None
    amount: Optional[float] = None
    note: Optional[str] = None
    created_at: Optional[str] = None
