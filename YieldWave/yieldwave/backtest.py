"""回测引擎。

两种回测：
A. 股息率信号回测：只统计触发次数、持有时间、股息率口径信号表现（永远可算）。
B. 真实资金回测：见 portfolio_backtest.py（完整账户 NAV 模型）。本模块不做“逐轮收益相加”
   当作正式资金收益；没有指数点位/全收益数据时，资金相关字段统一显示
   “缺少价格数据，无法计算”，不乱算。

重要一致性约束：
- 信号回测与实盘、成交记录使用**同一套周度锁定规则**（strategy.weekly_locked_thresholds_for_records）：
  每周第一个交易日用当时可见历史锁定全周阈值，杜绝未来数据泄漏。
- 本程序不伪造任何真实资金收益。没有 ETF 真实成交价时，绝不以假价格推算资金盈亏。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from typing import Dict, List, Optional, Tuple

from .models import POS_EMPTY, POS_HOLDING, ValuationRecord
from .precision import D
from .strategy import weekly_locked_thresholds_for_records

NO_PRICE = "缺少价格数据，无法计算"

# 每个完成轮的记录
Round = Dict[str, object]


def _rounds_from_records(
    records: List[ValuationRecord],
    offsets: Dict[str, Tuple[float, float]],
    window: Optional[int] = None,
) -> Dict[str, List[Round]]:
    """按机械规则前向模拟，返回各仓完成的“轮次”列表。

    offsets: {"A": (buy_off, sell_off), ...}，单位为百分点。
    使用与实盘完全一致的周度锁定：每周首个交易日用当时可见数据算中枢并锁定全周阈值，
    本周其余交易日沿用锁定值（无未来泄漏）。

    注意：signal_date 与 execution_date 在回测中统一取该交易日日期；m42 取该周锁定中枢。
    """
    # 构造仅含 positions 偏移的伪 config 供共享周锁定函数计算阈值
    pseudo_cfg = {
        "positions": {
            n: {"buy_offset": o[0], "sell_offset": o[1]} for n, o in offsets.items()
        },
        "primary_window": window or 42,
    }
    locked = weekly_locked_thresholds_for_records(records, pseudo_cfg, window)

    close = [r.close for r in records]
    rounds: Dict[str, List[Round]] = {k: [] for k in offsets}
    state = {k: POS_EMPTY for k in offsets}
    buy_info: Dict[str, Dict[str, object]] = {k: {} for k in offsets}

    for i, r in enumerate(records):
        thr = locked[i]
        if thr is None:
            continue  # 该周数据不足，无法产生信号
        cur = r.dividend_yield_2
        if cur is None:
            continue
        for name in offsets:
            buy_line = thr[f"{name}_buy"]
            sell_line = thr[f"{name}_sell"]
            if state[name] == POS_EMPTY:
                if cur >= buy_line:
                    state[name] = POS_HOLDING
                    buy_info[name] = {
                        "buy_idx": i,
                        "buy_date": r.date.isoformat(),
                        "buy_yield": D(cur),
                        "buy_close": close[i],
                        "lock_m42": thr.get("A_buy"),  # 仅占位，真正中枢见 weekly
                    }
            else:  # HOLDING
                if cur <= sell_line:
                    bi = buy_info[name]["buy_idx"]  # type: ignore[assignment]
                    bdate = buy_info[name]["buy_date"]  # type: ignore[assignment]
                    byield = buy_info[name]["buy_yield"]  # type: ignore[assignment]
                    bclose = buy_info[name]["buy_close"]  # type: ignore[assignment]
                    sclose = close[i]
                    holding_days = i - bi  # 交易日数
                    rnd: Round = {
                        "position": name,
                        "buy_date": bdate,
                        "sell_date": r.date.isoformat(),
                        "buy_yield": byield,
                        "sell_yield": D(cur),
                        "holding_days": holding_days,
                        "buy_close": bclose,
                        "sell_close": sclose,
                    }
                    if bclose is not None and sclose is not None and bclose > 0:
                        rnd["price_return"] = sclose / bclose - 1.0
                    else:
                        rnd["price_return"] = None
                    rounds[name].append(rnd)
                    state[name] = POS_EMPTY
                    buy_info[name] = {}
    return rounds


def _summarize_rounds(rounds: Dict[str, List[Round]]) -> Dict[str, object]:
    all_rounds: List[Round] = []
    for lst in rounds.values():
        all_rounds.extend(lst)

    per_position_counts = {k: len(v) for k, v in rounds.items()}

    if not all_rounds:
        return {
            "total_rounds": 0,
            "per_position_counts": per_position_counts,
            "avg_holding_days": NO_PRICE,
            "median_holding_days": NO_PRICE,
            "min_holding_days": NO_PRICE,
            "max_holding_days": NO_PRICE,
            "yield_completion_rate": NO_PRICE,
            "avg_yield_gain": NO_PRICE,
            "median_yield_gain": NO_PRICE,
            "max_yield_loss": NO_PRICE,
            "max_yield_gain": NO_PRICE,
            "win_rate_price": NO_PRICE,
            "total_return_price": NO_PRICE,
            "annual_return_price": NO_PRICE,
            "max_drawdown_price": NO_PRICE,
            "per_year_rounds": {},
        }

    holding = [int(r["holding_days"]) for r in all_rounds]  # type: ignore[arg-type]
    yield_gain = [float(r["buy_yield"]) - float(r["sell_yield"]) for r in all_rounds]  # type: ignore[operator]

    per_year: Dict[str, int] = {}
    for r in all_rounds:
        y = str(r["buy_date"])[:4]  # type: ignore[index]
        per_year[y] = per_year.get(y, 0) + 1

    out: Dict[str, object] = {
        "total_rounds": len(all_rounds),
        "per_position_counts": per_position_counts,
        "avg_holding_days": round(statistics.mean(holding), 2),
        "median_holding_days": statistics.median(holding),
        "min_holding_days": min(holding),
        "max_holding_days": max(holding),
        # 股息率信号完成率（非真实资金盈利胜率）：买入时股息率高于卖出时 = 均值回归完成
        "yield_completion_rate": round(
            sum(1 for g in yield_gain if g > 0) / len(yield_gain), 4
        ),
        "avg_yield_gain": round(statistics.mean(yield_gain), 4),
        "median_yield_gain": round(statistics.median(yield_gain), 4),
        "max_yield_loss": round(min(yield_gain), 4),
        "max_yield_gain": round(max(yield_gain), 4),
        "per_year_rounds": per_year,
    }
    # 真实资金收益由 portfolio_backtest.py 统一计算；此处不把逐轮收益相加当作正式资金收益
    out["win_rate_price"] = NO_PRICE
    out["total_return_price"] = NO_PRICE
    out["annual_return_price"] = NO_PRICE
    out["max_drawdown_price"] = NO_PRICE
    return out


def run_backtest(
    records: List[ValuationRecord],
    window: int,
    offsets: Dict[str, Tuple[float, float]],
) -> Dict[str, object]:
    """对给定窗口与偏移组合跑信号回测，返回统计字典。"""
    rounds = _rounds_from_records(records, offsets, window)
    summary = _summarize_rounds(rounds)
    summary["window"] = window
    return summary


def default_offsets_from_config(config: dict) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for name, p in config["positions"].items():
        out[name] = (float(p["buy_offset"]), float(p["sell_offset"]))
    return out
