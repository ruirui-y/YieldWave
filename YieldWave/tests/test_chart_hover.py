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


if __name__ == "__main__":
    unittest.main()
