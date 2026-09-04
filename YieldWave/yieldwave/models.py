"""数据模型定义。

约定：
- 日期统一为 date 对象（或 ISO 字符串 "YYYY-MM-DD"）。
- 股息率 / PE 一律以“百分数”存储（4.77 表示 4.77%，而不是 0.0477）。
- 偏移量 buy_offset / sell_offset 单位为“百分点”（0.02 表示 +0.02 个百分点）。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional


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
    dividend_yield_1: Optional[float] = None
    dividend_yield_2: Optional[float] = None
    pe_1: Optional[float] = None
    pe_2: Optional[float] = None
    close: Optional[float] = None
    source: str = "honglicha"
    fetched_at: Optional[str] = None

    @staticmethod
    def from_row(row: dict) -> "ValuationRecord":
        return ValuationRecord(
            date=_to_date(row["date"]),
            index_code=row["index_code"],
            index_name=row["index_name"],
            dividend_yield_1=row.get("dividend_yield_1"),
            dividend_yield_2=row.get("dividend_yield_2"),
            pe_1=row.get("pe_1"),
            pe_2=row.get("pe_2"),
            close=row.get("close"),
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
    name: str  # "A" / "B" / "C"
    label: str  # "A仓"
    percent: float  # 占总资金百分比，例如 20
    status: str = POS_EMPTY
    buy_date: Optional[str] = None
    buy_yield: Optional[float] = None
    buy_close: Optional[float] = None
    buy_price: Optional[float] = None  # ETF 实际成交价（手工录入）
    sell_date: Optional[str] = None
    sell_yield: Optional[float] = None
    sell_price: Optional[float] = None

    @staticmethod
    def from_row(row: dict) -> "PositionState":
        return PositionState(
            name=row["name"],
            label=row.get("label", row["name"]),
            percent=float(row.get("percent", 0)),
            status=row.get("status", POS_EMPTY),
            buy_date=row.get("buy_date"),
            buy_yield=row.get("buy_yield"),
            buy_close=row.get("buy_close"),
            buy_price=row.get("buy_price"),
            sell_date=row.get("sell_date"),
            sell_yield=row.get("sell_yield"),
            sell_price=row.get("sell_price"),
        )


@dataclass
class WeeklyStrategy:
    week_id: str
    start_date: str
    m42: float
    a_buy: float
    a_sell: float
    b_buy: float
    b_sell: float
    c_buy: float
    c_sell: float
    created_at: Optional[str] = None


@dataclass
class Trade:
    id: Optional[int]
    position_name: str
    action: str  # "BUY" / "SELL"
    signal_date: str
    execution_date: str
    dividend_yield: Optional[float]
    m42: Optional[float]
    threshold: Optional[float]
    percentage: Optional[float]
    etf_price: Optional[float]
    shares: Optional[float]
    amount: Optional[float]
    note: Optional[str]
    created_at: Optional[str] = None
