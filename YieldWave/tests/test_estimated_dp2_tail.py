"""估算 D/P2 尾巴（官方数据滞后期间）单元测试。

覆盖 Task B 的判定：
1. 公式精度：anchor_dp2 * anchor_close / point ≈ 4.7745576678…
2. 无递归：每个尾巴点都【直接相对最后一个官方锚点】计算（dp2*point == anchor_dp2*anchor_close）。
3. estimated_dp2_tail 绝不进入官方 records / _visible_dp2（运行时数据，不进库、不进策略）。
4. M42 / Mean42 计算结果不随 estimated_dp2_tail 改变（尾巴只观察，不参与中枢与买卖线）。
5. Hover tooltip 文案正确：估算点显示「估算 D/P2 / 指数点位 / 性质」，绝不含「官方 D/P2」。

纯函数（point_estimator）无需 Qt；ChartWidget 集成测试走 headless Qt（skip 兜底）。
"""

from __future__ import annotations

import datetime as _dt
import unittest
from decimal import Decimal

from yieldwave.models import ValuationRecord
from yieldwave.precision import D
from yieldwave.services.point_estimator import (
    assemble_estimated_tail,
    estimate_current_dp2,
)
from yieldwave.strategy import rolling_mean, rolling_median


# ---- 当前真实锚点（与文档一致）----
ANCHOR_DATE = "2026-09-02"
ANCHOR_DP2 = Decimal("4.77")
ANCHOR_CLOSE = Decimal("11083.48")


def _rec(date_str: str, dy2):
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str), index_code="H30269", index_name="t",
        dividend_yield_2=dy2,
    )


class TestEstimateFormulaPrecision(unittest.TestCase):
    """公式精度：anchor_dp2 * anchor_close / point。

    以文档给定样例：anchor_dp2=4.77, anchor_close=11083.48, point=11072.90
    -> 约 4.7745576678…（中证红利低波 H30269 股息率2 的收盘反推估算）。
    """

    def test_0903_close_reverse(self):
        result = estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, Decimal("11072.90"))
        self.assertIsNotNone(result)
        # 量化到 10 位小数比对精度（避免直接硬写全部尾数位）
        self.assertEqual(
            D(result).quantize(Decimal("0.0000000001")),
            Decimal("4.7745576678"),
        )

    def test_0904_intraday_reverse(self):
        # 09-04 用实时指数点位反推（与 09-03 同一 anchor，无递归）
        result = estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, Decimal("11050.00"))
        self.assertIsNotNone(result)
        expected = ANCHOR_DP2 * ANCHOR_CLOSE / Decimal("11050.00")
        self.assertEqual(result, expected)

    def test_invalid_point_returns_none(self):
        # 点位缺失 / <=0 -> None（UI 显示 '--'，不崩）
        self.assertIsNone(estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, None))
        self.assertIsNone(estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, Decimal("0")))
        self.assertIsNone(estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, Decimal("-1")))


class TestEstimatedTailNoRecursion(unittest.TestCase):
    """无递归：每个尾巴点都直接相对最后一个官方锚点计算。

    关键不变量：对任意尾巴点都有 dp2 * index_point == anchor_dp2 * anchor_close。
    若实现递归（拿前一天估算值当新锚点），该乘积会逐日漂移、不再等于锚点乘积。
    """

    def _build_tail(self) -> list:
        daily_closes = {
            "2026-09-03": Decimal("11072.90"),
            "2026-09-04": Decimal("11055.00"),
            "2026-09-07": Decimal("11060.10"),
        }
        return assemble_estimated_tail(
            anchor_date=ANCHOR_DATE,
            anchor_dp2=ANCHOR_DP2,
            anchor_close=ANCHOR_CLOSE,
            daily_closes=daily_closes,
            current_date="2026-09-07",
            current_point=Decimal("11060.10"),
        )

    def test_all_points_direct_from_anchor(self):
        tail = self._build_tail()
        self.assertGreater(len(tail), 0)
        anchor_product = ANCHOR_DP2 * ANCHOR_CLOSE
        for pt in tail:
            dp2 = D(pt["dp2"])
            pt_val = D(pt["index_point"])
            # dp2 = anchor_dp2 * anchor_close / point  => dp2 * point == anchor 乘积
            self.assertEqual(
                (dp2 * pt_val).quantize(Decimal("0.0001")),
                anchor_product.quantize(Decimal("0.0001")),
                msg=f"点 {pt['date']} 未直接相对锚点计算（疑似递归）",
            )

    def test_0903_uses_anchor_not_recursive(self):
        tail = self._build_tail()
        by_date = {pt["date"]: pt for pt in tail}
        self.assertIn("2026-09-03", by_date)
        direct = estimate_current_dp2(ANCHOR_DP2, ANCHOR_CLOSE, Decimal("11072.90"))
        self.assertEqual(D(by_date["2026-09-03"]["dp2"]), direct)
        # 即便「假想递归」用 09-03 估算值当伪锚点，单步数学上等价；
        # 但真实实现必须始终以官方 anchor_dp2/anchor_close 为源（见 test_all_points_direct_from_anchor）。
        self.assertEqual(by_date["2026-09-03"]["kind"], "close")

    def test_0904_or_later_uses_intraday_kind(self):
        tail = self._build_tail()
        by_date = {pt["date"]: pt for pt in tail}
        # 当前交易日 09-07 走盘中分支（kind="intraday"）
        self.assertIn("2026-09-07", by_date)
        self.assertEqual(by_date["2026-09-07"]["kind"], "intraday")

    def test_anchor_date_excluded_from_tail(self):
        tail = self._build_tail()
        dates = [pt["date"] for pt in tail]
        self.assertNotIn(ANCHOR_DATE, dates)
        # 尾巴按日期升序
        self.assertEqual(dates, sorted(dates))

    def test_dates_sorted_ascending(self):
        tail = self._build_tail()
        dates = [pt["date"] for pt in tail]
        self.assertEqual(dates, sorted(dates))


class _QtTailTestCase(unittest.TestCase):
    """headless ChartWidget 基底：只测尾部的「数据隔离」与「不污染中枢」。"""

    def setUp(self) -> None:
        try:
            import sys

            from PyQt6.QtWidgets import QApplication

            from yieldwave.ui.chart_widget import ChartWidget

            self._app = QApplication.instance() or QApplication(sys.argv)
            self.ChartWidget = ChartWidget
        except Exception as exc:
            self.skipTest(f"无法初始化 Qt：{exc}")
        # 21 条官方记录（09-01..09-21），最后一条即锚点之后、尾巴之前
        self.recs = [
            _rec(f"2026-09-{i:02d}", 4.00 + i * 0.01)
            for i in range(1, 22)
        ]

    def _make_tail(self) -> list:
        # 锚点之后两条估算尾巴（09-22 收盘反推、09-23 盘中）
        return [
            {"date": "2026-09-22", "dp2": Decimal("4.83"), "index_point": Decimal("11072.90"), "kind": "close"},
            {"date": "2026-09-23", "dp2": Decimal("4.79"), "index_point": Decimal("11050.00"), "kind": "intraday"},
        ]


class TestTailNotInRecords(_QtTailTestCase):
    """estimated_dp2_tail 与官方 records / 可见缓存严格分离。"""

    def test_tail_stored_separately(self):
        tail = self._make_tail()
        cw = self.ChartWidget()
        cw.set_data(self.recs, {}, estimated_dp2_tail=tail)
        # 尾巴存在专属缓存
        self.assertEqual(cw._estimated_tail, tail)
        # 官方记录数量不变（尾巴不合并进 records）
        self.assertEqual(len(cw._records), len(self.recs))
        self.assertEqual(cw._records, self.recs)
        # 可见 D/P2 / 索引长度 == 官方记录长度（尾巴未追加到主线序列）
        self.assertEqual(len(cw._visible_dp2), len(self.recs))
        self.assertEqual(len(cw._visible_idx), len(self.recs))
        # 最后一个可见点仍是官方最后一条，不是尾巴
        self.assertEqual(cw._visible_dp2[-1], 4.00 + 21 * 0.01)
        # 尾部 X 坐标从最后一个官方点之后开始（tail_x[0] 复用官方末点做视觉连接）
        self.assertEqual(len(cw._tail_x), len(tail) + 1)
        self.assertAlmostEqual(float(cw._tail_x[0]), float(len(self.recs) - 1), places=6)

    def test_clearing_tail_does_not_touch_records(self):
        cw = self.ChartWidget()
        cw.set_data(self.recs, {}, estimated_dp2_tail=self._make_tail())
        # 再次 set_data 不带尾巴 -> 清空尾巴缓存，records 仍完整
        cw.set_data(self.recs, {})
        self.assertEqual(cw._estimated_tail, [])
        self.assertEqual(len(cw._records), len(self.recs))
        self.assertEqual(len(cw._visible_dp2), len(self.recs))


class TestTailDoesNotChangeM42Mean42(_QtTailTestCase):
    """M42 / Mean42 计算结果不随 estimated_dp2_tail 改变。"""

    def test_m42_mean42_identical_with_and_without_tail(self):
        cw = self.ChartWidget()
        # 带尾巴
        cw.set_data(self.recs, {}, estimated_dp2_tail=self._make_tail())
        m42_with = list(cw._visible_m42)
        mean42_with = list(cw._visible_mean42)
        # 不带尾巴
        cw.set_data(self.recs, {})
        m42_without = list(cw._visible_m42)
        mean42_without = list(cw._visible_mean42)
        self.assertEqual(m42_with, m42_without)
        self.assertEqual(mean42_with, mean42_without)

    def test_m42_still_from_official_records(self):
        # M42 必须等于基于官方 records 的滚动中位数（与尾巴无关）
        cw = self.ChartWidget()
        cw.set_data(self.recs, {}, estimated_dp2_tail=self._make_tail())
        official_dy2 = [r.dividend_yield_2 for r in self.recs if r.dividend_yield_2 is not None]
        # 只取最后一个可见点对比（最后一条记录已满足 42 窗口）
        last_m42 = rolling_median(official_dy2, 42)
        self.assertEqual(D(cw._visible_m42[-1]), last_m42)
        last_mean42 = rolling_mean(official_dy2, 42)
        self.assertEqual(D(cw._visible_mean42[-1]), last_mean42)


class TestEstimatedTooltipText(_QtTailTestCase):
    """估算点 hover tooltip 文案：估算 D/P2 / 指数点位 / 性质，绝不含「官方 D/P2」。"""

    def test_close_tooltip_text(self):
        cw = self.ChartWidget()
        text = cw._build_estimated_tooltip_text(
            _dt.date(2026, 9, 22), 4.7745576678, Decimal("11072.90"), "close",
        )
        self.assertIn("估算 D/P2", text)
        self.assertIn("指数点位", text)
        self.assertIn("收盘反推估算", text)
        self.assertIn("11072.90", text)
        self.assertNotIn("官方 D/P2", text)

    def test_intraday_tooltip_text(self):
        cw = self.ChartWidget()
        text = cw._build_estimated_tooltip_text(
            _dt.date(2026, 9, 23), 4.7844, Decimal("11050.00"), "intraday",
        )
        self.assertIn("盘中估算", text)
        self.assertIn("11050.00", text)
        self.assertNotIn("官方 D/P2", text)

    def test_motion_routes_to_tail_tooltip(self):
        # x 超过最后一个官方点 -> 走尾巴 hover，annotation 文案为估算点
        cw = self.ChartWidget()
        cw.set_data(self.recs, {}, estimated_dp2_tail=self._make_tail())
        last_x = float(len(self.recs) - 1)
        event = type("E", (), {"inaxes": cw._ax, "xdata": last_x + 0.5})()
        cw._on_motion(event)
        self.assertTrue(cw._annotation.get_visible())
        ann_text = cw._annotation.get_text()
        self.assertIn("估算 D/P2", ann_text)
        self.assertNotIn("官方 D/P2", ann_text)


if __name__ == "__main__":
    unittest.main()
