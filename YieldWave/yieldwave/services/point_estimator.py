"""盘中估算点位计算器（纯函数，无任何 IO）。

公式（与文档完全一致，单位统一为百分数，例如 4.770 表示 4.770%）：

    estimated_current_dp2 = anchor_dp2 * anchor_close / current_index_point
    target_index_point   = anchor_close * anchor_dp2 / target_dp2
    distance              = current_index_point - target_point

注意方向相反：
- 股息率越高 → 指数越低 → 越接近买入
- 股息率越低 → 指数越高 → 越接近卖出

全程用 Decimal，避免 float 二进制污染。任何无法估算的情形都返回 None，
调用方据此显示 '--'，绝不让 UI 崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Dict, List, Optional, Tuple

from ..precision import D


class EstimateError(Exception):
    """估算前置条件不满足（缺 anchor / current_point / target_dp2）。"""


@dataclass(frozen=True)
class Anchor:
    """官方锚点：最近一个已公布 D/P2 的日期、股息率、当日指数收盘点位。"""

    date: str               # "2026-09-02"
    dp2: Decimal            # 4.770
    close: Decimal          # 11083.48

    def valid(self) -> bool:
        return bool(self.date) and self.dp2 is not None and self.close is not None and self.close > 0


def estimate_current_dp2(
    anchor_dp2,
    anchor_close,
    current_index_point,
) -> Optional[Decimal]:
    """估算当前 D/P2。

    公式：anchor_dp2 * anchor_close / current_index_point
    任何参数缺失、为 0、非法 -> 返回 None（UI 显示 '--'）。
    """
    a_dp2 = D(anchor_dp2)
    a_close = D(anchor_close)
    cur_pt = D(current_index_point)
    if a_dp2 is None or a_close is None or cur_pt is None:
        return None
    if a_close <= 0 or cur_pt <= 0 or a_dp2 <= 0:
        return None
    try:
        return a_dp2 * a_close / cur_pt
    except (DivisionByZero, InvalidOperation):
        return None


def yield_to_target_point(
    anchor_dp2,
    anchor_close,
    target_dp2,
) -> Optional[Decimal]:
    """股息率阈值 -> 对应指数点位。

    公式：anchor_close * anchor_dp2 / target_dp2
    target_dp2 越高 -> 点位越低（更接近买入）。
    target_dp2 = 0 或缺失 -> 返回 None（不崩，UI 显示 '--'）。
    """
    a_dp2 = D(anchor_dp2)
    a_close = D(anchor_close)
    tgt = D(target_dp2)
    if a_dp2 is None or a_close is None or tgt is None:
        return None
    if a_close <= 0 or a_dp2 <= 0 or tgt <= 0:
        return None
    try:
        return a_close * a_dp2 / tgt
    except (DivisionByZero, InvalidOperation):
        return None


def assemble_estimated_tail(
    anchor_date: str,
    anchor_dp2: Decimal,
    anchor_close: Decimal,
    daily_closes: Dict[str, Decimal],
    current_date: Optional[str],
    current_point: Optional[Decimal],
) -> List[Dict[str, object]]:
    """组装 anchor 之后的估算 D/P2 尾巴（纯函数，无 IO，不进库、不进策略）。

    规则（与文档完全一致）：
    - 每个历史已结束交易日 -> estimate_current_dp2(anchor_dp2, anchor_close, 当日close)，
      kind="close"（收盘反推）；
    - 当前交易日 -> estimate_current_dp2(anchor_dp2, anchor_close, 实时current)，
      kind="intraday"（盘中估算）；
    - 所有点都【直接相对最后一个官方锚点】计算，绝不拿前一天估算值递归累乘
      （公式固定为 anchor_dp2 * anchor_close / 当日点位，无链式误差）；
    - 不人为生成周末/非交易日数据点：daily_closes 由调用方来自 fetch_close_range
      （本身只含真实交易日），current_date 为当前真实交易日。

    返回按日期升序的列表：[{date, dp2, index_point, kind}, ...]。
    锚点日本身不进入尾巴（尾巴从 anchor 之后开始）。
    """
    tail: List[Dict[str, object]] = []
    # 历史已结束交易日：用官方收盘反推（排除锚点日与当天）
    for d in sorted(daily_closes.keys()):
        if d == anchor_date:
            continue
        if current_date is not None and d == current_date:
            continue  # 当天统一走盘中分支
        close = D(daily_closes[d])
        if close is None or close <= 0:
            continue
        dp2 = estimate_current_dp2(anchor_dp2, anchor_close, close)
        if dp2 is None:
            continue
        tail.append({
            "date": d,
            "dp2": dp2,
            "index_point": close,
            "kind": "close",
        })
    # 当前交易日：用实时 current 反推（盘中估算）
    if (
        current_date is not None
        and current_date != anchor_date
        and current_point is not None
        and D(current_point) is not None
        and D(current_point) > 0
    ):
        cp = D(current_point)
        dp2 = estimate_current_dp2(anchor_dp2, anchor_close, cp)
        if dp2 is not None:
            tail.append({
                "date": current_date,
                "dp2": dp2,
                "index_point": cp,
                "kind": "intraday",
            })
    tail.sort(key=lambda x: x["date"])
    return tail


def distance_to_target(current_point, target_point) -> Optional[Tuple[Decimal, Decimal]]:
    """距离目标点位的点数与百分比。

    返回 (point_distance, percent_distance)：
    - point_distance = current_point - target_point（带符号）
    - percent_distance = point_distance / target_point * 100（百分数）

    target_point 为 0 或缺失 -> None。current 缺失 -> None。
    """
    cur = D(current_point)
    tgt = D(target_point)
    if cur is None or tgt is None or tgt == 0:
        return None
    diff = cur - tgt
    try:
        pct = diff / tgt * Decimal(100)
    except (DivisionByZero, InvalidOperation):
        return None
    return diff, pct


def can_estimate(anchor: Optional[Anchor], current_point) -> Tuple[bool, str]:
    """前置条件检查，返回 (是否可估算, 不可估算时的简短原因)。"""
    if anchor is None:
        return False, "缺少官方锚点"
    if not anchor.valid():
        return False, "锚点数据不完整（缺 anchor_dp2 / anchor_close）"
    cp = D(current_point)
    if cp is None:
        return False, "缺少当前指数点位"
    if cp <= 0:
        return False, "当前指数点位无效（<=0）"
    return True, ""
