"""走势图 hover 与周锁定阶梯线单元测试（不启动 Qt GUI）。

只测 ChartWidget 的纯逻辑函数与 strategy.weekly_locked_thresholds_for_records：
1. hover 最近日期吸附逻辑正确（_snap_to_index 用 searchsorted）。
2. 切换时间范围后索引缓存正确更新（_visible_idx 等被 redraw 重置）。
3. 历史周锁定阈值不能被今天阈值覆盖（阶梯线：整周共享周一锁定值）。
4. 锁定周里每条 A/B/C 阈值都换算成点位（在 test_point_estimator 里已覆盖；
   这里再覆盖一次"历史阶梯线" vs "今日阈值"不同）。
"""

from __future__ import annotations

import datetime as _dt
import unittest
from decimal import Decimal
from types import SimpleNamespace
from typing import List, Optional

import numpy as np

from yieldwave.models import ValuationRecord
from yieldwave.precision import D
from yieldwave.strategy import (
    compute_thresholds,
    current_week_id,
    weekly_locked_thresholds_for_records,
)


CFG = {
    "primary_window": 5,
    "positions": {
        "A": {"label": "A仓", "percent": 8, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓", "percent": 20, "buy_offset": 0.12, "sell_offset": -0.11},
    },
}


def _rec(date_str, dy2):
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str), index_code="H30269", index_name="t",
        dividend_yield_2=dy2,
    )


class TestSnapToIndex(unittest.TestCase):
    """hover 索引吸附：模拟 chart_widget._on_motion 里的 searchsorted 逻辑。"""

    def _snap(self, x: float, n: int) -> int:
        """复刻 chart_widget._on_motion 里的吸附逻辑。"""
        idx_arr = np.arange(n, dtype=float)
        idx = int(np.searchsorted(idx_arr, x, side="left"))
        if idx >= n:
            idx = n - 1
        elif idx > 0:
            left = idx_arr[idx - 1]
            right = idx_arr[idx]
            if abs(x - left) < abs(right - x):
                idx = idx - 1
        return int(idx)

    def test_left_of_first_snaps_to_zero(self):
        # x < 0 -> 第一个
        self.assertEqual(self._snap(-1.5, 10), 0)

    def test_right_of_last_snaps_to_last(self):
        # x > n-1 -> 最后一个
        self.assertEqual(self._snap(20.0, 10), 9)
        self.assertEqual(self._snap(9.6, 10), 9)

    def test_exact_integer_x(self):
        # x = i 命中 i（searchsorted side=left 把 i 放在 i 位置）
        self.assertEqual(self._snap(3.0, 10), 3)
        self.assertEqual(self._snap(0.0, 10), 0)
        self.assertEqual(self._snap(9.0, 10), 9)

    def test_between_two_integers_picks_nearest(self):
        # x = 2.4 -> 命中 2；x = 2.6 -> 命中 3
        self.assertEqual(self._snap(2.4, 10), 2)
        self.assertEqual(self._snap(2.6, 10), 3)

    def test_just_after_zero(self):
        # x = 0.1 -> 距离 0 更近
        self.assertEqual(self._snap(0.1, 10), 0)

    def test_just_before_last(self):
        # x = 8.9 -> 距离 9 更近
        self.assertEqual(self._snap(8.9, 10), 9)


class TestWeeklyLockStaircase(unittest.TestCase):
    """历史周锁定阈值必须是阶梯线：同一周内 A/B/C 阈值保持不变，跨周才跳变。"""

    def _build(self) -> List[ValuationRecord]:
        # 窗口=5；构造两周
        # W35 (08-31..09-04)，每周一锁定，W36 的周一 09-07 把中枢锁到不同值
        dates = [
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",  # W35
            "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",  # W36 (周一 08-31)
            "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",  # W37 (周一 09-07)
        ]
        # W35 全 4.90；W36 周一 4.90，其余 5.10；W37 周一 5.20（中枢跳变）
        yields = [
            4.90, 4.90, 4.90, 4.90, 4.90,
            4.90, 5.10, 5.10, 5.10, 5.10,
            5.20, 5.20, 5.20, 5.20, 5.20,
        ]
        return [_rec(d, y) for d, y in zip(dates, yields)]

    def test_locked_constant_within_week(self):
        recs = self._build()
        locks = weekly_locked_thresholds_for_records(recs, CFG, 5)
        # W36 整周（索引 5..9）应共享周一锁定的阈值
        for i in range(5, 10):
            self.assertIsNotNone(locks[i])
            self.assertEqual(locks[i]["A_buy"], locks[5]["A_buy"])
            self.assertEqual(locks[i]["C_sell"], locks[5]["C_sell"])
            self.assertEqual(locks[i]["M42"], locks[5]["M42"])

    def test_locked_changes_across_weeks(self):
        recs = self._build()
        locks = weekly_locked_thresholds_for_records(recs, CFG, 5)
        # W36 周一（08-31）锁定的中枢 = 截至当日（含）可见数据的中位数
        # 截至索引 5：[4.90]*5 + [4.90] = 6 个值，窗口 5 -> 取最后 5 个：[4.90]*5，中位数 4.90
        self.assertEqual(locks[5]["M42"], Decimal("4.90"))
        # W37 周一（09-07）锁定的中枢 = 截至当日（含）的窗口 5 中位数
        # 截至索引 10：[4.90]*5 + [4.90] + [5.10]*4 + [5.20] = 11 个值，窗口 5 -> 最后 5 个：
        # [5.10, 5.10, 5.10, 5.10, 5.20]，中位数 5.10
        self.assertEqual(locks[10]["M42"], Decimal("5.10"))
        # 跨周中枢不同 -> 阈值不同（阶梯线跳变）
        self.assertNotEqual(locks[5]["A_buy"], locks[10]["A_buy"])

    def test_today_thresholds_do_not_overwrite_history(self):
        """今天的阈值（live）不能覆盖历史周锁定值。

        模拟 chart_widget.redraw 拿"今天这一周"的阈值去画历史 → 错误。
        正确实现：用 weekly_locked_thresholds_for_records 逐日生成阶梯线。
        """
        recs = self._build()
        locks = weekly_locked_thresholds_for_records(recs, CFG, 5)
        today_live = compute_thresholds(Decimal("5.20"), CFG)  # 假设今天中枢 5.20
        # 历史每周锁定值与今天 live 不同
        self.assertNotEqual(locks[5]["A_buy"], today_live["A_buy"])
        self.assertNotEqual(locks[10]["A_buy"], today_live["A_buy"])


class TestChartVisibleIndexCache(unittest.TestCase):
    """切换时间范围后 _visible_idx 等缓存必须随 redraw 重置（不引用旧数组）。

    由于这里不启动 Qt，我们直接调用 redraw 验证缓存被刷新（无 GUI 也能跑 redraw，
    因为 FigureCanvasQTAgg 在 headless 也允许创建；如果失败会跳过）。
    """

    def setUp(self):
        try:
            from yieldwave.ui.chart_widget import ChartWidget
            from PyQt6.QtWidgets import QApplication
            import sys
            self._app = QApplication.instance() or QApplication(sys.argv)
            self.ChartWidget = ChartWidget
        except Exception as exc:
            self.skipTest(f"无法初始化 Qt：{exc}")

    def test_redraw_resets_visible_arrays(self):
        from yieldwave.ui.chart_widget import _RANGES
        cw = self.ChartWidget()
        recs = [
            _rec(f"2026-09-{i:02d}", 4.80 + i * 0.01)
            for i in range(1, 21)
        ]
        # 给一个 thresholds（空也行）
        cw.set_data(recs, {})
        # 默认 6 个月（126 天），但只有 20 条 -> visible 长度 = 20
        n1 = len(cw._visible_idx)
        self.assertEqual(n1, 20)
        # 切换到 1 个月（21 天），仍然 20 条
        cw.range_combo.setCurrentText("1个月")
        cw.redraw()
        n2 = len(cw._visible_idx)
        self.assertEqual(n2, 20)
        # 切换到 3 个月（63 天），仍然 20 条
        cw.range_combo.setCurrentText("3个月")
        cw.redraw()
        n3 = len(cw._visible_idx)
        self.assertEqual(n3, 20)
        # 锁数组长度也对齐
        self.assertEqual(len(cw._visible_locks), 20)
        self.assertEqual(len(cw._visible_dates), 20)
        self.assertEqual(len(cw._visible_dp2), 20)
        self.assertEqual(len(cw._visible_m42), 20)


class TestWeeklyLockIncludesM42(unittest.TestCase):
    """新增 M42 字段后，原有行为不破坏（A/B/C 阈值不变），M42 可读。"""

    def test_lock_dict_has_m42_key(self):
        recs = [
            _rec("2026-08-24", 4.90),
            _rec("2026-08-25", 4.90),
            _rec("2026-08-26", 4.90),
            _rec("2026-08-27", 4.90),
            _rec("2026-08-28", 4.90),
            _rec("2026-08-31", 4.90),  # 本周一（锁定日）
        ]
        locks = weekly_locked_thresholds_for_records(recs, CFG, 5)
        self.assertIsNotNone(locks[5])
        self.assertIn("M42", locks[5])
        self.assertEqual(locks[5]["M42"], Decimal("4.90"))
        # A/B/C 阈值未变（向后兼容）
        self.assertEqual(locks[5]["A_buy"], Decimal("4.92"))


class _QtTooltipTestCase(unittest.TestCase):
    """headless ChartWidget 测试基类：QApplication + 21 条记录 + 固定坐标轴。

    set_data 会创建 annotation；固定 xlim/ylim 让 transData 映射可预测。
    不 show() 任何窗口；draw 一次让真实文本排版可被 get_window_extent 测量。
    """

    def setUp(self) -> None:
        try:
            import sys

            from PyQt6.QtWidgets import QApplication

            from yieldwave.ui.chart_widget import ChartWidget

            self._app = QApplication.instance() or QApplication(sys.argv)
            self.ChartWidget = ChartWidget
        except Exception as exc:
            self.skipTest(f"无法初始化 Qt：{exc}")
        recs = [
            _rec(f"2026-09-{i:02d}", 4.00 + i * 0.01)
            for i in range(1, 22)
        ]
        self.cw = self.ChartWidget()
        self.cw.set_data(recs, {})
        self.ax = self.cw._ax
        self.ann = self.cw._annotation
        self.ax.set_xlim(0.0, 20.0)
        self.ax.set_ylim(0.0, 10.0)
        # 真实文本 + 可见状态：空文本/不可见文本不会参与排版，bbox 测量不可靠，
        # 可能把第二级 clamp 误触发，污染四象限/锚点的纯逻辑断言。
        self.ann.set_text("日期：2026-09-01\n股息率2 D/P2：4.10%")
        self.ann.set_visible(True)
        self.ann.set_alpha(1.0)
        self.cw.canvas.draw()


class TestTooltipPlacementQuadrants(_QtTooltipTestCase):
    """_place_tooltip 四象限避让：只依赖 axes 几何，headless 稳定可跑。

    期望映射（锚点在 axes 中的位置 -> tooltip 方位）：
    - 左下点 -> tooltip 右上（ha=left,  va=bottom, offset=(+20, +20)）
    - 左上点 -> tooltip 右下（ha=left,  va=top,    offset=(+20, -20)）
    - 右下点 -> tooltip 左上（ha=right, va=bottom, offset=(-20, +20)）
    - 右上点 -> tooltip 左下（ha=right, va=top,    offset=(-20, -20)）
    """

    def test_bottom_left_anchor(self):
        # 左下点（axes 左半边 + 下半边）-> tooltip 右上
        self.cw._place_tooltip(1.0, 1.0)
        self.assertEqual(self.ann.get_ha(), "left")
        self.assertEqual(self.ann.get_va(), "bottom")
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0, places=6)
        self.assertAlmostEqual(oy, 20.0, places=6)
        self.assertEqual(tuple(self.ann.xy), (1.0, 1.0))

    def test_top_left_anchor(self):
        # 左上点（axes 左半边 + 上半边）-> tooltip 右下
        self.cw._place_tooltip(1.0, 9.0)
        self.assertEqual(self.ann.get_ha(), "left")
        self.assertEqual(self.ann.get_va(), "top")
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0, places=6)
        self.assertAlmostEqual(oy, -20.0, places=6)
        self.assertEqual(tuple(self.ann.xy), (1.0, 9.0))

    def test_bottom_right_anchor(self):
        # 右下点（axes 右半边 + 下半边）-> tooltip 左上
        self.cw._place_tooltip(19.0, 1.0)
        self.assertEqual(self.ann.get_ha(), "right")
        self.assertEqual(self.ann.get_va(), "bottom")
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, -20.0, places=6)
        self.assertAlmostEqual(oy, 20.0, places=6)
        self.assertEqual(tuple(self.ann.xy), (19.0, 1.0))

    def test_top_right_anchor(self):
        # 右上点（axes 右半边 + 上半边）-> tooltip 左下
        self.cw._place_tooltip(19.0, 9.0)
        self.assertEqual(self.ann.get_ha(), "right")
        self.assertEqual(self.ann.get_va(), "top")
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, -20.0, places=6)
        self.assertAlmostEqual(oy, -20.0, places=6)
        self.assertEqual(tuple(self.ann.xy), (19.0, 9.0))


class TestTooltipFigureClamp(_QtTooltipTestCase):
    """_place_tooltip 第二级保护：最终 bbox 必须留在 figure 8px 安全边距内。

    两种验证：
    1. 确定性 clamp 数学：monkeypatch annotation.get_window_extent 返回固定越界
       bbox，断言 offset 补偿量 = 越界 px * (72/dpi)（不依赖字体排版，必稳定）。
    2. 真实 bbox 安全网：用真实多行中文文本 + 真实 draw，遍历代表性锚点，断言
       测量到的最终 bbox 不越出 figure.bbox 的 8px 边距。
    """

    def _patch_window_extent(self, bounds: tuple[float, float, float, float]) -> None:
        from matplotlib.transforms import Bbox

        self.ann.get_window_extent = lambda renderer=None: Bbox.from_extents(*bounds)

    def _px_to_pt(self) -> float:
        return 72.0 / self.cw.figure.dpi

    def test_clamp_pushes_left_when_overflowing_right(self):
        # tooltip 越过 figure 右边界 58px -> offset_x 减小 58px 折算的 pt
        self._patch_window_extent((600.0, 200.0, 850.0, 300.0))
        self.cw._place_tooltip(1.0, 1.0)
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0 - 58.0 * self._px_to_pt(), places=6)
        self.assertAlmostEqual(oy, 20.0, places=6)

    def test_clamp_pushes_down_when_overflowing_top(self):
        # tooltip 越过 figure 上边界 38px -> offset_y 减小 38px 折算的 pt
        self._patch_window_extent((300.0, 300.0, 500.0, 450.0))
        self.cw._place_tooltip(1.0, 1.0)
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0, places=6)
        self.assertAlmostEqual(oy, 20.0 - 38.0 * self._px_to_pt(), places=6)

    def test_clamp_pushes_right_and_up_when_overflowing_left_bottom(self):
        # 同时越左边界 48px、下边界 38px -> offset_x/offset_y 都相应增大
        self._patch_window_extent((-40.0, -30.0, 100.0, 50.0))
        self.cw._place_tooltip(1.0, 1.0)
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0 + 48.0 * self._px_to_pt(), places=6)
        self.assertAlmostEqual(oy, 20.0 + 38.0 * self._px_to_pt(), places=6)

    def test_no_change_when_already_inside(self):
        # bbox 完全在边距内 -> offset 不动
        self._patch_window_extent((100.0, 100.0, 300.0, 300.0))
        self.cw._place_tooltip(1.0, 1.0)
        ox, oy = self.ann.get_position()
        self.assertAlmostEqual(ox, 20.0, places=6)
        self.assertAlmostEqual(oy, 20.0, places=6)

    def _full_tooltip_text(self) -> str:
        locks = {
            "M42": D("4.50"),
            "A_buy": D("4.52"), "A_sell": D("4.47"),
            "B_buy": D("4.57"), "B_sell": D("4.42"),
            "C_buy": D("4.62"), "C_sell": D("4.37"),
        }
        return self.cw._build_tooltip_text(_dt.date(2026, 9, 5), 4.2, 4.5, locks)

    def test_real_bbox_never_crosses_figure_margin(self):
        # 用真实多行 tooltip 文本遍历代表性锚点：最终测量 bbox 不越 figure 8px 边距
        w = float(self.cw.figure.bbox.width)
        h = float(self.cw.figure.bbox.height)
        margin = 8.0
        tol = 1.0
        self.ann.set_text(self._full_tooltip_text())
        self.ann.set_visible(True)
        self.ann.set_alpha(1.0)
        self.cw.canvas.draw()
        renderer = self.cw.canvas.get_renderer()
        anchors = [
            (1.0, 1.0), (1.0, 9.0), (19.0, 1.0), (19.0, 9.0),  # 四角
            (10.0, 4.0), (10.0, 6.0),  # 中部两侧（真实文本够高，会触发 clamp）
        ]
        for xi, ay in anchors:
            self.cw._place_tooltip(xi, ay)
            bb = self.ann.get_window_extent(renderer=renderer)
            msg = f"anchor=({xi},{ay}) bbox={bb.bounds} fig=({w},{h})"
            self.assertGreaterEqual(bb.x0, margin - tol, msg=f"左越界 {msg}")
            self.assertGreaterEqual(bb.y0, margin - tol, msg=f"下越界 {msg}")
            self.assertLessEqual(bb.x1, w - margin + tol, msg=f"右越界 {msg}")
            self.assertLessEqual(bb.y1, h - margin + tol, msg=f"上越界 {msg}")


class TestHoverAnchorSelection(_QtTooltipTestCase):
    """_on_motion 锚点优先级：D/P2 -> M42 -> y 轴中点（NaN 兜底）。"""

    def _event(self, xdata: float) -> SimpleNamespace:
        return SimpleNamespace(inaxes=self.ax, xdata=xdata)

    def test_valid_dp2_uses_dp2_anchor(self):
        # xdata=5.4 吸附到 idx=5：dp2 有效 -> 锚点 = (xi, dp2) = (5, 4.06)
        self.cw._on_motion(self._event(5.4))
        self.assertTrue(self.ann.get_visible())
        self.assertEqual(tuple(self.ann.xy), (5.0, 4.00 + 6 * 0.01))

    def test_nan_dp2_falls_back_to_m42_anchor(self):
        # dp2=NaN 但 m42 有效 -> 锚点取 m42（此处 idx=5 -> 4.50）
        self.cw._visible_dp2[5] = float("nan")
        self.cw._visible_m42[5] = 4.50
        self.cw._on_motion(self._event(5.4))
        self.assertEqual(tuple(self.ann.xy), (5.0, 4.50))

    def test_all_nan_uses_ylim_midpoint_anchor(self):
        # dp2 与 m42 都 NaN -> 锚点 = y 轴中点（ylim(0,10) -> 5.0）
        self.cw._visible_dp2[5] = float("nan")
        self.cw._visible_m42[5] = float("nan")
        self.cw._on_motion(self._event(5.4))
        self.assertEqual(tuple(self.ann.xy), (5.0, 5.0))


if __name__ == "__main__":
    unittest.main()
