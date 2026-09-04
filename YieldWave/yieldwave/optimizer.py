"""参数优化（搜索）。

目标不是追求历史收益最大，而是寻找满足实际需求的参数：
第一优先：A 仓年均交易次数落入 6~9 次。
第二优先：A 仓中位持有时间落入 5~20 个交易日。
第三优先：若有可靠价格数据，再看收益与最大回撤（在此环境下通常不参与排序）。

B/C 跟随 A 设置“相对更深的档位”（固定深度差），不在搜索里单独展开。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from typing import Dict, List, Optional, Tuple

from .backtest import _rounds_from_records
from .models import ValuationRecord

# B/C 相对 A 的固定更深档位（百分点）
B_DELTA_BUY = 0.05
B_DELTA_SELL = -0.03
C_DELTA_BUY = 0.10
C_DELTA_SELL = -0.10


def search_parameters(
    records: List[ValuationRecord],
    windows: Optional[List[int]] = None,
    a_buy_range: Tuple[float, float] = (0.01, 0.10),
    a_sell_range: Tuple[float, float] = (-0.10, -0.01),
    step: float = 0.01,
) -> List[Dict]:
    """网格搜索，返回按优先级排序的候选参数列表。"""
    if not records:
        return []
    if windows is None:
        windows = list(range(30, 61))

    dates = [r.date for r in records if r.date]
    span_days = max((max(dates) - min(dates)).days, 1)
    years_span = span_days / 365.0

    # 生成步进序列（保留两位小数的百分点）
    def seq(lo: float, hi: float):
        vals = []
        v = round(lo, 2)
        while v <= hi + 1e-9:
            vals.append(round(v, 2))
            v = round(v + step, 2)
        return vals

    buy_vals = seq(a_buy_range[0], a_buy_range[1])
    sell_vals = seq(a_sell_range[0], a_sell_range[1])

    results: List[Dict] = []
    for w in windows:
        for ab in buy_vals:
            for as_ in sell_vals:
                if as_ >= ab:
                    continue  # 卖出线必须低于买入线
                offsets = {
                    "A": (ab, as_),
                    "B": (round(ab + B_DELTA_BUY, 2), round(as_ + B_DELTA_SELL, 2)),
                    "C": (round(ab + C_DELTA_BUY, 2), round(as_ + C_DELTA_SELL, 2)),
                }
                rounds = _rounds_from_records(records, w, offsets)
                a_rounds = rounds["A"]
                a_count = len(a_rounds)
                annual_a = a_count / years_span if years_span > 0 else 0
                a_holding = [int(r["holding_days"]) for r in a_rounds]
                median_hold = statistics.median(a_holding) if a_holding else None
                meets_freq = 6 <= annual_a <= 9
                meets_hold = (median_hold is not None) and (5 <= median_hold <= 20)
                results.append(
                    {
                        "window": w,
                        "A_buy": ab,
                        "A_sell": as_,
                        "B_buy": offsets["B"][0],
                        "B_sell": offsets["B"][1],
                        "C_buy": offsets["C"][0],
                        "C_sell": offsets["C"][1],
                        "A_rounds": a_count,
                        "annual_A": round(annual_a, 2),
                        "median_hold_A": median_hold,
                        "meets_freq": meets_freq,
                        "meets_hold": meets_hold,
                    }
                )

    # 排序：满足频率优先 -> 满足持有时间 -> 年化越接近 7.5 越好 -> 中位持有越接近 12.5 越好
    def sort_key(d: Dict):
        freq = 0 if d["meets_freq"] else 1
        hold = 0 if d["meets_hold"] else 1
        adj_freq = abs(d["annual_A"] - 7.5)
        adj_hold = 0 if d["median_hold_A"] is None else abs(d["median_hold_A"] - 12.5)
        return (freq, hold, adj_freq, adj_hold)

    results.sort(key=sort_key)
    return results
