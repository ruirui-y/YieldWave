"""回测一致性测试：回测必须与实盘/周锁定共用同一套规则，且不泄漏未来数据。

关键约束：
- 信号生成复用 strategy.weekly_locked_thresholds_for_records（实盘同款）。
- 周内使用本周首个交易日的锁定中枢，周二~周五不得改用“实时重算”的更高中枢。
- "股息率胜率" 已重命名为 "股息率信号完成率"，且不冒充真实资金盈利胜率。
"""

import datetime as _dt
import unittest
from decimal import Decimal

from yieldwave.backtest import _rounds_from_records, run_backtest
from yieldwave.models import ValuationRecord

OFFSETS = {"A": (0.02, -0.01)}


def _rec(date_str, dy2):
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str), index_code="H30269", index_name="测试",
        dividend_yield_2=dy2,
    )


def _build_no_future_leak():
    """构造一个能区分“锁定(周一中枢)”与“实时重算(周后段中枢)”的数据集。

    窗口=5：
    - 前 5 天(2026-08-24~28, W35) 全部 4.90 -> 周一锁定时 M42=4.90 -> A买线 4.92。
    - 本周(2026-08-31~09-04, W36)：一 4.90，二/三/四 5.10，五 5.00。
    - 实时重算到周四时中枢升到 5.10 -> A买线 5.12（>=5.12 才买）。
    若回测错误地在周五用实时中枢，则周四(5.10)不会买、周五(5.00)也不会买；
    正确实现用周一锁定的 4.92，则在周二(5.10)即买入，并在下周一(4.80)卖出。
    """
    dy2 = {
        "2026-08-24": 4.90, "2026-08-25": 4.90, "2026-08-26": 4.90,
        "2026-08-27": 4.90, "2026-08-28": 4.90,
        "2026-08-31": 4.90,  # 本周一（锁定日）
        "2026-09-01": 5.10, "2026-09-02": 5.10, "2026-09-03": 5.10,
        "2026-09-04": 5.00,
        "2026-09-07": 4.80,  # 下周一：低于卖出线 4.89 -> 卖出
    }
    order = [
        "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
        "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        "2026-09-07",
    ]
    return [_rec(d, dy2[d]) for d in order]


class TestBacktestWeeklyLockNoFutureLeak(unittest.TestCase):
    def test_backtest_uses_locked_threshold_not_live(self):
        recs = _build_no_future_leak()
        rounds = _rounds_from_records(recs, OFFSETS, 5)
        self.assertEqual(len(rounds["A"]), 1)
        r = rounds["A"][0]
        # 买入发生在周二(5.10)，证明用的是周一锁定的 4.92 买线，而非实时 5.12
        self.assertEqual(r["buy_yield"], Decimal("5.10"))
        self.assertEqual(r["buy_date"], "2026-09-01")
        # 卖出发生在下周一(4.80)；若错误使用实时中枢会在周五(5.00)卖出
        self.assertEqual(r["sell_date"], "2026-09-07")
        self.assertEqual(r["sell_yield"], Decimal("4.80"))
        self.assertNotIn("2026-09-04", [x["sell_date"] for x in rounds["A"]])

    def test_backtest_summary_total_rounds(self):
        recs = _build_no_future_leak()
        summary = run_backtest(recs, 5, OFFSETS)
        self.assertEqual(summary["total_rounds"], 1)


class TestBacktestYieldCompletionRateRenamed(unittest.TestCase):
    """一致性修复 #9：'股息率胜率' 改名为 '股息率信号完成率'，且不冒充真实资金胜率。"""

    def test_renamed_key_present(self):
        recs = _build_no_future_leak()
        summary = run_backtest(recs, 5, OFFSETS)
        self.assertIn("yield_completion_rate", summary)
        self.assertNotIn("win_rate_yield", summary)

    def test_money_metrics_not_faked(self):
        recs = _build_no_future_leak()
        summary = run_backtest(recs, 5, OFFSETS)
        # 没有指数点位数据，真实资金收益一律为“缺少价格数据，无法计算”
        self.assertEqual(summary["win_rate_price"], "缺少价格数据，无法计算")
        self.assertEqual(summary["total_return_price"], "缺少价格数据，无法计算")


if __name__ == "__main__":
    unittest.main()
