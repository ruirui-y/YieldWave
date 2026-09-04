"""策略核心：动态中枢、周度锁定、机械信号。

所有阈值数字都来自 config.json，这里只做计算与状态机判定。

重要约定（百分点，不是比例）：
- 数值一律以百分数存储（4.90 表示 4.90%）。
- buy_line = M42 + buy_offset，其中 buy_offset 单位为“百分点”（0.02 -> 4.90+0.02 = 4.92）。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .models import POS_EMPTY, POS_HOLDING, PositionState, ValuationRecord, WeeklyStrategy
from .precision import D, fmt_yield

# 动作常量
ACT_BUY = "BUY"
ACT_SELL = "SELL"
ACT_HOLD = "HOLD"
ACT_WAIT = "WAIT"

# D/P2 拐点阈值（单位：百分点，例如 5.19 -> 5.13 的回落 = 0.06）
TURN_THRESHOLD = Decimal("0.06")


def rolling_median(values: List[Decimal], window: int) -> Optional[Decimal]:
    """最近 window 个有效值的中位数（不是自然日，是有效交易日条数）。

    保留完整精度：输入为 Decimal（D/P2），中位数也返回 Decimal，绝不先 round。
    偶数样本时取中间两值的平均（仍为 Decimal，例如 4.835）。

    用 D() 收口（而非 Decimal(float)）：当输入为 float 来源的 Decimal 时，
    D() 走最短十进制字符串，避免 4.835 -> 4.8349999… 这类二进制污染。
    """
    if not values or len(values) < 1:
        return None
    n = min(window, len(values))
    window_vals = values[-n:]
    return D(statistics.median(window_vals))


def rolling_mean(values: List[Decimal], window: int) -> Optional[Decimal]:
    """最近 window 个有效值的标准算术平均数（不是自然日，是有效交易日条数）。

    与 rolling_median 规则一致：
    - 不足 window 个时使用当前已有全部有效值；
    - 空列表返回 None；
    - 全程 Decimal，不提前 round，返回值经 D() 收口。

    含义即：最近 N 个有效 D/P2 数值之和 / N（Decimal 整数除法）。
    """
    if not values:
        return None
    n = min(window, len(values))
    window_vals = values[-n:]
    if not window_vals:
        return None
    # 逐值经 D() 收口为 Decimal（兼容 float 来源的 D/P2，避免 float 求和再转 Decimal 的二进制污染），
    # 再求和做标准算术平均；结果仍走 D() 收口。
    s = sum((D(v) for v in window_vals), Decimal(0))
    return D(s / Decimal(n))


def _turning_item_date_dp2(item) -> Tuple[str, Decimal]:
    """从纯计算输入中取出 (date, dp2)。

    兼容两种常用形态：
    - dict：{"date": "...", "dp2": ...}
    - 记录对象：.date / .dp2（或策略模块的 .dividend_yield_2）

    返回值 date 统一为 ISO 字符串，dp2 统一收口为 Decimal。
    """
    if isinstance(item, dict):
        date = item.get("date")
        dp2 = item.get("dp2")
        if dp2 is None:
            dp2 = item.get("dividend_yield_2")
    elif isinstance(item, (tuple, list)) and len(item) >= 2:
        date, dp2 = item[0], item[1]
    else:
        date = getattr(item, "date", None)
        dp2 = getattr(item, "dp2", None)
        if dp2 is None:
            dp2 = getattr(item, "dividend_yield_2", None)
    if dp2 is None:
        raise ValueError("turning point input item must contain a dp2 value")
    if isinstance(date, _dt.datetime):
        date_str = date.date().isoformat()
    elif isinstance(date, _dt.date):
        date_str = date.isoformat()
    else:
        date_str = str(date)
    return date_str, D(dp2)


def _raw_directional_change_turns(
    values,
    threshold: Decimal,
) -> List[Dict[str, object]]:
    """纯 directional-change 扫描（不访问数据库 / Qt / Matplotlib）。

    状态机从等待顶拐点开始：
    - peak 状态跟踪最高 D/P2，直到从最高值回落 >= threshold，确认 peak；
    - 随后切换 trough 状态跟踪最低 D/P2，直到从最低值反弹 >= threshold，确认 trough；
    - 之后再次切回 peak，如此严格交替。

    返回严格交替的已确认拐点列表。
    """
    out: List[Dict[str, object]] = []
    state = "peak"
    pivot_date: Optional[str] = None
    pivot_dp2: Optional[Decimal] = None
    for item in values:
        date_str, dp2 = _turning_item_date_dp2(item)
        if dp2 is None:
            continue
        if pivot_date is None:
            pivot_date, pivot_dp2 = date_str, dp2
            continue
        if state == "peak":
            if dp2 > pivot_dp2:
                pivot_date, pivot_dp2 = date_str, dp2
            elif pivot_dp2 - dp2 >= threshold:
                out.append({
                    "kind": "peak",
                    "pivot_date": pivot_date,
                    "pivot_dp2": pivot_dp2,
                    "confirm_date": date_str,
                    "confirm_dp2": dp2,
                    "reversal": pivot_dp2 - dp2,
                })
                state = "trough"
                pivot_date, pivot_dp2 = date_str, dp2
        else:  # trough
            if dp2 < pivot_dp2:
                pivot_date, pivot_dp2 = date_str, dp2
            elif dp2 - pivot_dp2 >= threshold:
                out.append({
                    "kind": "trough",
                    "pivot_date": pivot_date,
                    "pivot_dp2": pivot_dp2,
                    "confirm_date": date_str,
                    "confirm_dp2": dp2,
                    "reversal": dp2 - pivot_dp2,
                })
                state = "peak"
                pivot_date, pivot_dp2 = date_str, dp2
    return out


def detect_turning_points(
    values,
    threshold=Decimal("0.06"),
) -> List[Dict[str, object]]:
    """D/P2 拐点检测（纯计算，不访问数据库 / Qt / Matplotlib / 行情接口）。

    parameters
    ----------
    values : 按日期升序的序列
        每个元素可以是 dict {"date", "dp2"}，或带 .date/.dp2/.dividend_yield_2 的对象。
    threshold : Decimal, default Decimal("0.06")
        反向变化阈值，单位百分点（0.06 表示 D/P2 数值相差 0.06）。

    返回已确认拐点，元素字段：
    {
        "kind": "peak" | "trough",
        "pivot_date": "...",
        "pivot_dp2": Decimal(...),
        "confirm_date": "...",
        "confirm_dp2": Decimal(...),
        "reversal": Decimal(...)
    }
    """
    th = D(threshold)
    if th is None or th <= 0:
        raise ValueError("threshold must be a positive Decimal")
    return _raw_directional_change_turns(values, th)


def compute_medians(
    records: List[ValuationRecord], windows: Dict[str, int]
) -> Dict[str, Optional[Decimal]]:
    """对 dividend_yield_2 计算各窗口中位数。records 必须按日期升序。"""
    dy2 = [r.dividend_yield_2 for r in records if r.dividend_yield_2 is not None]
    out: Dict[str, Optional[Decimal]] = {}
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


def compute_thresholds(m42: object, config: dict) -> Dict[str, Decimal]:
    """根据 M42 与各仓偏移量计算买卖线。偏移量为“百分点”。

    全程 Decimal，保留完整精度；绝对不“先 round 再比较信号”——round 只发生在显示层。
    """
    m = D(m42)
    if m is None:
        return {}
    positions = config["positions"]
    out: Dict[str, Decimal] = {}
    for name, p in positions.items():
        buy_off = D(p["buy_offset"])
        sell_off = D(p["sell_offset"])
        out[f"{name}_buy"] = m + buy_off
        out[f"{name}_sell"] = m + sell_off
    return out


def generate_weekly_strategy(m42: object, config: dict, today: Optional[_dt.date] = None) -> WeeklyStrategy:
    """生成本周锁定策略。m42 与阈值均保留完整 Decimal 精度（不 round）。"""
    m = D(m42)
    th = compute_thresholds(m, config)
    return WeeklyStrategy(
        week_id=current_week_id(today),
        start_date=week_start_date(today),
        m42=m,
        a_buy=th["A_buy"], a_sell=th["A_sell"],
        b_buy=th["B_buy"], b_sell=th["B_sell"],
        c_buy=th["C_buy"], c_sell=th["C_sell"],
    )


def thresholds_from_weekly(ws: WeeklyStrategy) -> Dict[str, Decimal]:
    """把 weekly_strategy 转成 {A_buy, A_sell, ...} 阈值字典（Decimal，锁定值）。"""
    return {
        "A_buy": ws.a_buy, "A_sell": ws.a_sell,
        "B_buy": ws.b_buy, "B_sell": ws.b_sell,
        "C_buy": ws.c_buy, "C_sell": ws.c_sell,
    }


def valid_dp2_count(records: List[ValuationRecord]) -> int:
    """有效 dividend_yield_2 条数（用于热身计数，忽略 D/P2 为空的记录）。"""
    return sum(1 for r in records if r.dividend_yield_2 is not None)


def weekly_locked_thresholds_for_records(
    records: List[ValuationRecord],
    config: dict,
    window: Optional[int] = None,
) -> List[Optional[Dict[str, Decimal]]]:
    """实盘与回测共用的“周度锁定”函数：杜绝未来数据泄漏。

    规则（与实盘完全一致）：
    - 按 ISO 自然周分组，每周取**第一个有效交易日**作为锁定日。
    - 锁定日的中枢 = 截至该日（含）当时可见的 dividend_yield_2 的滚动中位数
      （窗口默认为 primary_window=42，即 M42）。
    - 该周全部交易日统一使用锁定日算出的 A/B/C 阈值，周中即便实时 M42 变化也不改。
    - 下周第一个交易日重新计算并锁定。

    返回与 records 等长的列表：每个元素是该记录对应的锁定阈值字典，或 None（数据不足）。
    每个字典同时包含 "M42" 键（该周锁定中枢，便于走势图 tooltip 直接读取，避免反向推断偏移）。
    """
    if not records:
        return []
    win = window if window else config.get("primary_window", 42)
    # 按 ISO 周分组，记录每个周的第一个交易日索引
    weeks: Dict[str, List[int]] = {}
    for idx, r in enumerate(records):
        wid = current_week_id(r.date)
        weeks.setdefault(wid, []).append(idx)
    out: List[Optional[Dict[str, Decimal]]] = [None] * len(records)
    for idxs in weeks.values():
        first_idx = idxs[0]
        # 仅使用截至锁定日（含）当时可见的 D/P2，严格无未来泄漏
        past = [
            records[k].dividend_yield_2
            for k in range(0, first_idx + 1)
            if records[k].dividend_yield_2 is not None
        ]
        m = rolling_median(past, win) if past else None
        thr = compute_thresholds(m, config) if m is not None else None
        if thr is not None and m is not None:
            thr["M42"] = m  # 同步锁定中枢，便于走势图 tooltip 直接读
        for i in idxs:
            out[i] = thr
    return out


def evaluate_position(
    position: PositionState,
    current_yield: object,
    thresholds: Dict[str, object],
) -> Tuple[str, str]:
    """返回 (动作, 原因)。

    规则（机械、单向）：
    - EMPTY 且 current >= buy  -> BUY（只触发一次，买入后状态变 HOLDING）
    - EMPTY 且 current <  buy  -> WAIT（空仓等待）
    - HOLDING 且 current <= sell -> SELL（卖出全部）
    - HOLDING 且 current >  sell -> HOLD（继续持有）
    绝不会出现：持仓又提示买 / 空仓却提示卖。

    比较一律用 Decimal（current_yield / 阈值均转 Decimal），且先做比较、后做显示量化。
    """
    cy = D(current_yield)
    name = position.name
    buy = D(thresholds[f"{name}_buy"])
    sell = D(thresholds[f"{name}_sell"])
    if position.status == POS_EMPTY:
        if cy >= buy:
            return ACT_BUY, f"空仓且股息率 {fmt_yield(cy, 2)}% >= 买入线 {fmt_yield(buy, 2)}%"
        return ACT_WAIT, f"空仓，股息率 {fmt_yield(cy, 2)}% < 买入线 {fmt_yield(buy, 2)}%"
    else:  # HOLDING
        if cy <= sell:
            return ACT_SELL, f"持仓且股息率 {fmt_yield(cy, 2)}% <= 卖出线 {fmt_yield(sell, 2)}%"
        return ACT_HOLD, f"持仓，股息率 {fmt_yield(cy, 2)}% > 卖出线 {fmt_yield(sell, 2)}%"


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


def split_positions(positions: List[PositionState]):
    """把仓位拆成 (核心仓列表, 波段仓列表)。"""
    core = [p for p in positions if p.kind == "core"]
    swing = [p for p in positions if p.kind != "core"]
    return core, swing


def current_core_percent(positions: List[PositionState]) -> Decimal:
    """当前核心仓“实际已建仓”比例（只统计 HOLDING 的核心档）。返回 Decimal（不 round）。"""
    return sum((p.percent for p in positions if p.kind == "core" and p.status == POS_HOLDING), Decimal(0))


def current_swing_percent(positions: List[PositionState]) -> Decimal:
    """当前波段仓“实际已持有”比例（只统计 HOLDING 的 A/B/C）。返回 Decimal（不 round）。"""
    return sum((p.percent for p in positions if p.kind != "core" and p.status == POS_HOLDING), Decimal(0))


def current_equity_percent(positions: List[PositionState]) -> Decimal:
    """当前实际权益仓位 = 所有 HOLDING 仓位百分比之和（核心 + 波段）。返回 Decimal（不 round）。"""
    return sum((p.percent for p in positions if p.status == POS_HOLDING), Decimal(0))


def total_suggested_percent(positions: List[PositionState]) -> float:
    """机械信号给出的“总建议仓位” = 当前实际处于 HOLDING 的所有仓位百分比之和。

    不再默认把核心 60% 当作已持有：核心仓只有用户“确认已买入”后才计入实际仓位。
    """
    return current_equity_percent(positions)
