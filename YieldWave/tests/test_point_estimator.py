"""point_estimator 纯计算函数单元测试。

覆盖文档要求的 1-6 项：
1. estimate_current_dp2 基本公式正确（4.770 * 11083.48 / 11161.72 ≈ 4.737）。
2. target_dp2 越高 -> target_point 越低（方向相反）。
3. target_dp2 = 0 -> 不能除零，返回 None。
4. 缺 anchor -> 不可估算，UI 不崩。
5. 未锁定周 -> 不生成正式机械点位，只生成预览（由 main_window 控制路径，
   这里覆盖底层 can_estimate 返回 False）。
6. 锁定周 -> 所有 A/B/C 阈值换算成点位。
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from yieldwave.services import market_quote, point_estimator
from yieldwave.services.point_estimator import (
    Anchor,
    can_estimate,
    distance_to_target,
    estimate_current_dp2,
    yield_to_target_point,
)


class TestEstimateCurrentDp2(unittest.TestCase):
    def test_basic_formula(self):
        # 文档示例：anchor_dp2=4.770, anchor_close=11083.48, current=11161.72
        # 期望 estimated_current_dp2 ≈ 4.737（保留完整精度，不 round）
        v = estimate_current_dp2(Decimal("4.770"), Decimal("11083.48"), Decimal("11161.72"))
        self.assertIsNotNone(v)
        # 等价于 4.770 * 11083.48 / 11161.72
        expected = Decimal("4.770") * Decimal("11083.48") / Decimal("11161.72")
        self.assertEqual(v, expected)
        # ROUND_HALF_UP 到 3 位应得到 4.737
        q = Decimal(1).scaleb(-3)
        self.assertEqual(v.quantize(q, rounding="ROUND_HALF_UP"), Decimal("4.737"))

    def test_missing_any_param_returns_none(self):
        self.assertIsNone(estimate_current_dp2(None, 11083.48, 11161.72))
        self.assertIsNone(estimate_current_dp2(4.770, None, 11161.72))
        self.assertIsNone(estimate_current_dp2(4.770, 11083.48, None))

    def test_zero_or_negative_returns_none(self):
        # 不能除零
        self.assertIsNone(estimate_current_dp2(4.770, 11083.48, 0))
        self.assertIsNone(estimate_current_dp2(4.770, 0, 11161.72))
        self.assertIsNone(estimate_current_dp2(0, 11083.48, 11161.72))
        self.assertIsNone(estimate_current_dp2(4.770, 11083.48, -1))


class TestYieldToTargetPoint(unittest.TestCase):
    def test_target_dp2_higher_point_lower(self):
        # 方向相反：target_dp2 越高 -> target_point 越低（越接近买入）
        anchor_dp2 = Decimal("4.770")
        anchor_close = Decimal("11083.48")
        p_high_yield = yield_to_target_point(anchor_dp2, anchor_close, Decimal("4.955"))   # C买
        p_low_yield = yield_to_target_point(anchor_dp2, anchor_close, Decimal("4.725"))   # C卖
        self.assertIsNotNone(p_high_yield)
        self.assertIsNotNone(p_low_yield)
        self.assertLess(p_high_yield, p_low_yield)  # 高股息率 -> 低点位

    def test_known_values(self):
        # 文档示例：A_buy=4.855% -> A_buy_point = 11083.48 * 4.770 / 4.855
        v = yield_to_target_point(Decimal("4.770"), Decimal("11083.48"), Decimal("4.855"))
        expected = Decimal("11083.48") * Decimal("4.770") / Decimal("4.855")
        self.assertEqual(v, expected)
        # 实际计算结果约 10889.43（用户文档里 10889.32 仅为示例取整数字）
        q = Decimal(1).scaleb(-2)
        self.assertEqual(v.quantize(q, rounding="ROUND_HALF_UP"), expected.quantize(q, rounding="ROUND_HALF_UP"))

    def test_zero_target_returns_none(self):
        # 不能除零
        self.assertIsNone(yield_to_target_point(4.770, 11083.48, 0))
        self.assertIsNone(yield_to_target_point(4.770, 11083.48, None))
        self.assertIsNone(yield_to_target_point(4.770, 11083.48, -0.1))
        self.assertIsNone(yield_to_target_point(4.770, 0, 4.855))
        self.assertIsNone(yield_to_target_point(None, 11083.48, 4.855))


class TestDistanceToTarget(unittest.TestCase):
    def test_distance_signed(self):
        # current=11161.72, target=11189（C卖），距 +27.28 点 / +0.24%
        diff, pct = distance_to_target(Decimal("11161.72"), Decimal("11189"))
        self.assertEqual(diff, Decimal("11161.72") - Decimal("11189"))
        # current(11161.72) < target(11189)，所以 diff = -27.28（距离卖线还差 27.28 点）
        q2 = Decimal(1).scaleb(-2)
        self.assertEqual(diff.quantize(q2, rounding="ROUND_HALF_UP"), Decimal("-27.28"))
        # 百分比 ≈ -0.24%（用 2 位小数断言，避免精度噪声）
        q2p = Decimal(1).scaleb(-2)
        self.assertEqual(pct.quantize(q2p, rounding="ROUND_HALF_UP"), Decimal("-0.24"))

    def test_zero_target_returns_none(self):
        self.assertIsNone(distance_to_target(11161.72, 0))
        self.assertIsNone(distance_to_target(11161.72, None))
        self.assertIsNone(distance_to_target(None, 11189))


class TestCanEstimate(unittest.TestCase):
    def test_no_anchor(self):
        ok, reason = can_estimate(None, 11161.72)
        self.assertFalse(ok)
        self.assertIn("锚点", reason)

    def test_invalid_anchor(self):
        # close 缺失
        a = Anchor(date="2026-09-02", dp2=Decimal("4.770"), close=None)
        ok, reason = can_estimate(a, 11161.72)
        self.assertFalse(ok)
        # dp2 缺失
        a = Anchor(date="2026-09-02", dp2=None, close=Decimal("11083.48"))
        ok, _ = can_estimate(a, 11161.72)
        self.assertFalse(ok)
        # close <= 0
        a = Anchor(date="2026-09-02", dp2=Decimal("4.770"), close=Decimal(0))
        ok, _ = can_estimate(a, 11161.72)
        self.assertFalse(ok)

    def test_no_current_point(self):
        a = Anchor(date="2026-09-02", dp2=Decimal("4.770"), close=Decimal("11083.48"))
        ok, reason = can_estimate(a, None)
        self.assertFalse(ok)
        self.assertIn("当前指数点位", reason)

    def test_zero_current_point(self):
        a = Anchor(date="2026-09-02", dp2=Decimal("4.770"), close=Decimal("11083.48"))
        ok, reason = can_estimate(a, 0)
        self.assertFalse(ok)
        self.assertIn("无效", reason)

    def test_ok(self):
        a = Anchor(date="2026-09-02", dp2=Decimal("4.770"), close=Decimal("11083.48"))
        ok, _ = can_estimate(a, 11161.72)
        self.assertTrue(ok)


class TestWeeklyThresholdsToPoints(unittest.TestCase):
    """锁定周 -> A/B/C 六条线全部换算成点位（验证不崩 + 方向正确）。"""

    def _all_six_points(self):
        anchor_dp2 = Decimal("4.770")
        anchor_close = Decimal("11083.48")
        thresholds = {
            "A_buy": Decimal("4.855"), "A_sell": Decimal("4.825"),
            "B_buy": Decimal("4.905"), "B_sell": Decimal("4.795"),
            "C_buy": Decimal("4.955"), "C_sell": Decimal("4.725"),
        }
        return {k: yield_to_target_point(anchor_dp2, anchor_close, v) for k, v in thresholds.items()}

    def test_all_six_returned(self):
        pts = self._all_six_points()
        for k in ("A_buy", "A_sell", "B_buy", "B_sell", "C_buy", "C_sell"):
            self.assertIsNotNone(pts[k], f"{k} should not be None")
            self.assertGreater(pts[k], 0)

    def test_buy_higher_than_sell_for_same_tranche(self):
        # 同一档：buy 线 > sell 线（股息率），所以 buy_point < sell_point（点位方向相反）
        pts = self._all_six_points()
        self.assertLess(pts["A_buy"], pts["A_sell"])
        self.assertLess(pts["B_buy"], pts["B_sell"])
        self.assertLess(pts["C_buy"], pts["C_sell"])

    def test_ordering_across_tranches(self):
        # 越跌越买：A_buy < B_buy < C_buy（股息率），点位方向相反 -> A_buy_point > B_buy_point > C_buy_point
        pts = self._all_six_points()
        self.assertGreater(pts["A_buy"], pts["B_buy"])
        self.assertGreater(pts["B_buy"], pts["C_buy"])
        # 卖出线：A_sell > B_sell > C_sell（股息率，越涨越卖） -> 点位方向相反
        # A_sell_point < B_sell_point < C_sell_point（点位升序）
        self.assertLess(pts["A_sell"], pts["B_sell"])
        self.assertLess(pts["B_sell"], pts["C_sell"])


class TestMarketQuoteDateHelpers(unittest.TestCase):
    """market_quote 日期辅助函数（不依赖网络）。"""

    def test_to_dashless_and_dashed(self):
        from datetime import date
        self.assertEqual(market_quote._to_dashless(date(2026, 9, 2)), "20260902")
        self.assertEqual(market_quote._to_dashless("2026-09-02"), "20260902")
        self.assertEqual(market_quote._to_dashless("20260902"), "20260902")
        self.assertEqual(market_quote._to_dashed("20260902"), "2026-09-02")
        self.assertEqual(market_quote._to_dashed("2026-09-02"), "2026-09-02")

    def test_closest_close_before(self):
        m = {"2026-09-01": 11154.63, "2026-09-02": 11083.48, "2026-09-03": 11072.90}
        # target=09-02 -> 命中 09-02
        r = market_quote.closest_close_before(m, "2026-09-02")
        self.assertEqual(r[0], "2026-09-02")
        self.assertEqual(r[1], 11083.48)
        # target=09-04 -> 取 09-03（最接近且 <=）
        r = market_quote.closest_close_before(m, "2026-09-04")
        self.assertEqual(r[0], "2026-09-03")
        # target=08-31（早于所有） -> None
        self.assertIsNone(market_quote.closest_close_before(m, "2026-08-31"))


if __name__ == "__main__":
    unittest.main()
