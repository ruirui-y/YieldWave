"""回测引擎。

两种回测：
A. 股息率信号回测：只统计触发次数、持有时间、股息率口径信号表现（永远可算）。
B. 收益回测：需要指数点位 / 全收益指数（close）。若数据库没有可靠价格数据，
   胜率(资金)/收益率/最大回撤/年化 等字段统一显示“缺少价格数据，无法计算”，不乱算。

注意：本程序不伪造任何真实资金收益。没有 ETF 真实成交价时，绝不以假价格推算资金盈亏。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from typing import Dict, List, Optional, Tuple

from .models import POS_EMPTY, POS_HOLDING, ValuationRecord
from .strategy import rolling_median

NO_PRICE = "缺少价格数据，无法计算"

# 每个完成轮的记录
Round = Dict[str, object]


def _rounds_from_records(
    records: List[ValuationRecord],
    window: int,
    offsets: Dict[str, Tuple[float, float]],
) -> Dict[str, List[Round]]:
    """按机械规则前向模拟，返回各仓完成的“轮次”列表。

    offsets: {"A": (buy_off, sell_off), "B": (...), "C": (...)}，单位为百分点。
    每天 i 使用 [i-window+1 .. i] 的 dividend_yield_2 中位数作为中枢 M（含当日，与线上规则一致）。
    """
    dy2 = [r.dividend_yield_2 for r in records]
    close = [r.close for r in records]
    rounds: Dict[str, List[Round]] = {k: [] for k in offsets}
    state = {k: POS_EMPTY for k in offsets}
    buy_info: Dict[str, Dict[str, object]] = {k: {} for k in offsets}

    for i in range(len(records)):
        if i < window - 1:
            continue
        if dy2[i] is None:
            continue
        m = rolling_median(dy2[: i + 1], window)
        if m is None:
            continue
        cur = dy2[i]
        for name, (buy_off, sell_off) in offsets.items():
            buy_line = m + buy_off
            sell_line = m + sell_off
            if state[name] == POS_EMPTY:
                if cur >= buy_line:
                    state[name] = POS_HOLDING
                    buy_info[name] = {
                        "buy_idx": i,
                        "buy_date": records[i].date.isoformat(),
                        "buy_yield": cur,
                        "buy_close": close[i],
                    }
            else:  # HOLDING
                if cur <= sell_line:
                    bi = buy_info[name]["buy_idx"]
                    bdate = buy_info[name]["buy_date"]
                    byield = buy_info[name]["buy_yield"]
                    bclose = buy_info[name]["buy_close"]
                    sclose = close[i]
                    holding_days = i - bi  # 交易日数
                    rnd: Round = {
                        "position": name,
                        "buy_date": bdate,
                        "sell_date": records[i].date.isoformat(),
                        "buy_yield": byield,
                        "sell_yield": cur,
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
            "win_rate_yield": NO_PRICE,
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
    price_rets = [float(r["price_return"]) for r in all_rounds if r.get("price_return") is not None]  # type: ignore[arg-type]

    has_price = len(price_rets) == len(all_rounds) and len(all_rounds) > 0

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
        # 股息率口径（信号表现，非真实资金）：买入时股息率高于卖出时 = 价格口径盈利
        "win_rate_yield": round(
            sum(1 for g in yield_gain if g > 0) / len(yield_gain), 4
        ),
        "avg_yield_gain": round(statistics.mean(yield_gain), 4),
        "median_yield_gain": round(statistics.median(yield_gain), 4),
        "max_yield_loss": round(min(yield_gain), 4),
        "max_yield_gain": round(max(yield_gain), 4),
        "per_year_rounds": per_year,
    }
    if has_price:
        out["win_rate_price"] = round(
            sum(1 for x in price_rets if x > 0) / len(price_rets), 4
        )
        out["total_return_price"] = round(sum(price_rets), 4)
        # 年化：用首尾日期跨度粗略估算（仅作参考）
        try:
            first = _dt.date.fromisoformat(all_rounds[0]["buy_date"])  # type: ignore[arg-type]
            last = _dt.date.fromisoformat(all_rounds[-1]["sell_date"])  # type: ignore[arg-type]
            days = max((last - first).days, 1)
            out["annual_return_price"] = round(
                ((1 + out["total_return_price"]) ** (365.0 / days) - 1), 4
            )
        except Exception:
            out["annual_return_price"] = NO_PRICE
        # 最大回撤（基于逐轮收益累积极曲线）
        eq = 1.0
        peak = 1.0
        mdd = 0.0
        for x in price_rets:
            eq *= (1 + x)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        out["max_drawdown_price"] = round(mdd, 4)
    else:
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
    """对给定窗口与偏移组合跑回测，返回统计字典。"""
    rounds = _rounds_from_records(records, window, offsets)
    summary = _summarize_rounds(rounds)
    summary["window"] = window
    return summary


def default_offsets_from_config(config: dict) -> Dict[str, Tuple[float, float]]:
    out: Dict[str, Tuple[float, float]] = {}
    for name, p in config["positions"].items():
        out[name] = (float(p["buy_offset"]), float(p["sell_offset"]))
    return out
