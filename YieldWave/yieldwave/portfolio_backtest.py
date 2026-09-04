"""组合（真实资金）回测：完整账户 NAV 模型。

与信号回测（backtest.py）的关系：
- 信号生成（买/卖触发）复用同一套周度锁定规则（strategy.weekly_locked_thresholds_for_records），
  保证“实时策略 / 周锁定 / 回测 / 成交记录”四者逻辑一致。
- 本模块在信号之上叠加**真实资金账户**模拟：初始资金 1,000,000，
  每日净值 = 现金 + 核心1/2/3 + A/B/C 持仓市值（+ 未来的债券/现金准备金）。
- 核心仓按“一年 D/P2 分位”建仓（build_percentile），只有买入、暂不自动卖出。
- 没有指数点位 / 全收益数据（close）时，所有资金指标统一返回“缺少价格数据，无法计算”，
  绝不以假价格或简单逐轮收益相加冒充真实收益。

指标（有价格数据时）：CAGR、最大回撤、年度收益、Calmar、TWR。XIRR 在存在真实资金流时再扩展。
"""

from __future__ import annotations

import datetime as _dt
import statistics
from typing import Dict, List, Optional

from .models import POS_EMPTY, ValuationRecord
from .strategy import current_week_id, weekly_locked_thresholds_for_records

NO_PRICE = "缺少价格数据，无法计算"
INITIAL_CAPITAL = 1_000_000.0


def _percentile_rank(history: List[float], value: float) -> float:
    """value 在 history 中的分位（0~100）。history 为空返回 50。"""
    if not history:
        return 50.0
    below = sum(1 for x in history if x <= value)
    return below / len(history) * 100.0


def run_portfolio_backtest(
    records: List[ValuationRecord],
    config: dict,
    window: Optional[int] = None,
) -> Dict[str, object]:
    """完整账户回测。返回信号统计 + 资金统计（无价格数据时资金统计为 NO_PRICE）。"""
    if not records:
        return {"error": "无历史数据"}

    win = window or config.get("primary_window", 42)
    pseudo_cfg = {
        "positions": {
            n: {"buy_offset": float(p["buy_offset"]), "sell_offset": float(p["sell_offset"])}
            for n, p in config["positions"].items()
        },
        "primary_window": win,
    }
    locked = weekly_locked_thresholds_for_records(records, pseudo_cfg, win)

    core_specs = config.get("core_tranches", {})

    # 状态
    cash = INITIAL_CAPITAL
    holdings: Dict[str, Dict[str, float]] = {}  # name -> {shares, notional}
    core_state = {name: POS_EMPTY for name in core_specs}
    swing_state = {name: POS_EMPTY for name in config["positions"]}

    nav_series: List[Dict[str, object]] = []
    has_price = all(r.close is not None for r in records)

    dy2_hist: List[float] = []

    for i, r in enumerate(records):
        cur = r.dividend_yield_2
        close = r.close
        dy2_hist.append(cur) if cur is not None else None

        # ---- 核心仓：按分位建仓（仅买入） ----
        for name, spec in core_specs.items():
            if core_state[name] == POS_EMPTY and cur is not None and close is not None:
                rank = _percentile_rank([x for x in dy2_hist if x is not None], cur)
                if rank <= float(spec.get("build_percentile", 50)):
                    notional = INITIAL_CAPITAL * float(spec["percent"]) / 100.0
                    shares = notional / close
                    cash -= notional
                    holdings[name] = {"shares": shares, "notional": notional}
                    core_state[name] = "HOLDING"

        # ---- 波段仓：周锁定信号 ----
        thr = locked[i]
        if thr is not None and cur is not None and close is not None:
            for name in config["positions"]:
                buy_line = thr[f"{name}_buy"]
                sell_line = thr[f"{name}_sell"]
                if swing_state[name] == POS_EMPTY:
                    if cur >= buy_line:
                        notional = INITIAL_CAPITAL * float(config["positions"][name]["percent"]) / 100.0
                        shares = notional / close
                        cash -= notional
                        holdings[name] = {"shares": shares, "notional": notional}
                        swing_state[name] = "HOLDING"
                else:
                    if cur <= sell_line and name in holdings:
                        shares = holdings[name]["shares"]
                        cash += shares * close
                        del holdings[name]
                        swing_state[name] = POS_EMPTY

        # ---- 每日净值 ----
        if close is not None:
            nav = cash + sum(h["shares"] * close for h in holdings.values())
        else:
            nav = None
        nav_series.append({"date": r.date.isoformat(), "nav": nav})

    # 信号统计（近似）：用 swing 状态变化计数
    result: Dict[str, object] = {
        "initial_capital": INITIAL_CAPITAL,
        "window": win,
        "core_tranches": {n: core_specs[n]["percent"] for n in core_specs},
        "swing_positions": {n: config["positions"][n]["percent"] for n in config["positions"]},
    }

    if not has_price or any(d["nav"] is None for d in nav_series):
        result["money_metrics"] = NO_PRICE
        result["reason"] = "数据库缺少指数点位 / 全收益数据（close），无法计算真实资金净值与收益。"
        return result

    navs = [d["nav"] for d in nav_series if d["nav"] is not None]
    dates = [d["date"] for d in nav_series if d["nav"] is not None]
    final_nav = navs[-1]
    first_nav = navs[0]
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    days = max((_dt.date.fromisoformat(dates[-1]) - _dt.date.fromisoformat(dates[0])).days, 1)
    cagr = (final_nav / first_nav) ** (365.0 / days) - 1.0 if first_nav > 0 else 0.0
    calmar = (cagr / abs(mdd)) if mdd < 0 else NO_PRICE

    # 年度收益
    annual: Dict[str, float] = {}
    by_year: Dict[str, List[float]] = {}
    for d, v in zip(dates, navs):
        by_year.setdefault(d[:4], []).append(v)
    for y, vs in by_year.items():
        if len(vs) >= 2 and vs[0] > 0:
            annual[y] = vs[-1] / vs[0] - 1.0

    # TWR（时间加权，按净值比值）
    twr = 1.0
    for k in range(1, len(navs)):
        if navs[k - 1] > 0:
            twr *= navs[k] / navs[k - 1]

    result["money_metrics"] = {
        "final_nav": round(final_nav, 2),
        "total_return": round(final_nav / first_nav - 1.0, 4),
        "cagr": round(cagr, 4),
        "max_drawdown": round(mdd, 4),
        "calmar": round(calmar, 4) if isinstance(calmar, float) else calmar,
        "twr": round(twr - 1.0, 4),
        "annual_returns": {y: round(x, 4) for y, x in annual.items()},
    }
    return result
