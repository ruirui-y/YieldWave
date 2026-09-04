"""策略核心：动态中枢、周度锁定、机械信号。

所有阈值数字都来自 config.json，这里只做计算与状态机判定。

重要约定（百分点，不是比例）：
- 数值一律以百分数存储（4.90 表示 4.90%）。
- buy_line = M42 + buy_offset，其中 buy_offset 单位为“百分点”（0.02 -> 4.90+0.02 = 4.92）。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from typing import Dict, List, Optional, Tuple

from .models import POS_EMPTY, POS_HOLDING, PositionState, ValuationRecord, WeeklyStrategy

# 动作常量
ACT_BUY = "BUY"
ACT_SELL = "SELL"
ACT_HOLD = "HOLD"
ACT_WAIT = "WAIT"


def rolling_median(values: List[float], window: int) -> Optional[float]:
    """最近 window 个有效值的中位数（不是自然日，是有效交易日条数）。"""
    if not values or len(values) < 1:
        return None
    n = min(window, len(values))
    window_vals = values[-n:]
    return statistics.median(window_vals)


def compute_medians(
    records: List[ValuationRecord], windows: Dict[str, int]
) -> Dict[str, Optional[float]]:
    """对 dividend_yield_2 计算各窗口中位数。records 必须按日期升序。"""
    dy2 = [r.dividend_yield_2 for r in records if r.dividend_yield_2 is not None]
    out: Dict[str, Optional[float]] = {}
    for key, w in windows.items():
        out[key] = rolling_median(dy2, w)
    return out


def current_week_id(d: Optional[_dt.date] = None) -> str:
    """ISO 年份-周，例如 2026-W36。同一自然周共享一个 id。"""
    d = d or _dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_start_date(d: Optional[_dt.date] = None) -> str:
    """返回该日期所在自然周的周一（作为本周 start_date）。"""
    d = d or _dt.date.today()
    monday = d - _dt.timedelta(days=d.weekday())
    return monday.isoformat()


def compute_thresholds(m42: float, config: dict) -> Dict[str, float]:
    """根据 M42 与各仓偏移量计算买卖线。偏移量为“百分点”。"""
    positions = config["positions"]
    out: Dict[str, float] = {}
    for name, p in positions.items():
        buy_off = float(p["buy_offset"])
        sell_off = float(p["sell_offset"])
        out[f"{name}_buy"] = round(m42 + buy_off, 4)
        out[f"{name}_sell"] = round(m42 + sell_off, 4)
    return out


def generate_weekly_strategy(m42: float, config: dict, today: Optional[_dt.date] = None) -> WeeklyStrategy:
    """生成本周锁定策略。"""
    th = compute_thresholds(m42, config)
    return WeeklyStrategy(
        week_id=current_week_id(today),
        start_date=week_start_date(today),
        m42=round(m42, 4),
        a_buy=th["A_buy"], a_sell=th["A_sell"],
        b_buy=th["B_buy"], b_sell=th["B_sell"],
        c_buy=th["C_buy"], c_sell=th["C_sell"],
    )


def evaluate_position(
    position: PositionState,
    current_yield: float,
    thresholds: Dict[str, float],
) -> Tuple[str, str]:
    """返回 (动作, 原因)。

    规则（机械、单向）：
    - EMPTY 且 current >= buy  -> BUY（只触发一次，买入后状态变 HOLDING）
    - EMPTY 且 current <  buy  -> WAIT（空仓等待）
    - HOLDING 且 current <= sell -> SELL（卖出全部）
    - HOLDING 且 current >  sell -> HOLD（继续持有）
    绝不会出现：持仓又提示买 / 空仓却提示卖。
    """
    name = position.name
    buy = thresholds[f"{name}_buy"]
    sell = thresholds[f"{name}_sell"]
    if position.status == POS_EMPTY:
        if current_yield >= buy:
            return ACT_BUY, f"空仓且股息率 {current_yield:.2f}% >= 买入线 {buy:.2f}%"
        return ACT_WAIT, f"空仓，股息率 {current_yield:.2f}% < 买入线 {buy:.2f}%"
    else:  # HOLDING
        if current_yield <= sell:
            return ACT_SELL, f"持仓且股息率 {current_yield:.2f}% <= 卖出线 {sell:.2f}%"
        return ACT_HOLD, f"持仓，股息率 {current_yield:.2f}% > 卖出线 {sell:.2f}%"


def apply_action_to_position(
    position: PositionState,
    action: str,
    current_yield: float,
    m42: float,
    signal_date: str,
    today: Optional[_dt.date] = None,
) -> PositionState:
    """就地更新仓位状态（仅在用户“确认”后调用，这里只是状态转移）。"""
    today_str = (today or _dt.date.today()).isoformat()
    if action == ACT_BUY:
        position.status = POS_HOLDING
        position.buy_date = signal_date
        position.buy_yield = current_yield
        position.sell_date = None
        position.sell_yield = None
        position.sell_price = None
    elif action == ACT_SELL:
        position.status = POS_EMPTY
        position.sell_date = today_str
        position.sell_yield = current_yield
    return position


def total_suggested_percent(
    positions: List[PositionState], core_percent: float
) -> float:
    """机械信号给出的“总建议仓位” = 核心仓 + 当前处于 HOLDING 的波段仓百分比之和。"""
    s = core_percent
    for p in positions:
        if p.status == POS_HOLDING:
            s += p.percent
    return s
