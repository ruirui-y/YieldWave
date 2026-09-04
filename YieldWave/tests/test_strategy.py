"""策略单元测试。"""

import datetime as _dt
import statistics
import unittest

from yieldwave.models import POS_EMPTY, POS_HOLDING, PositionState, ValuationRecord
from yieldwave.strategy import (
    ACT_BUY,
    ACT_HOLD,
    ACT_SELL,
    ACT_WAIT,
    compute_medians,
    compute_thresholds,
    current_week_id,
    evaluate_position,
    generate_weekly_strategy,
)

CONFIG = {
    "core_percent": 60,
    "primary_window": 42,
    "windows": {"M20": 20, "M42": 42, "M60": 60},
    "positions": {
        "A": {"label": "A仓", "percent": 20, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓", "percent": 8, "buy_offset": 0.12, "sell_offset": -0.11},
    },
}


def _rec(date_str, dy2):
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str), index_code="H30269", index_name="测试",
        dividend_yield_2=dy2,
    )


_BASE = _dt.date(2026, 1, 1)


def _recs_n(n, start=0):
    """生成 n 条连续日期记录，Dy2 = start..start+n-1。"""
    return [
        ValuationRecord(
            date=_BASE + _dt.timedelta(days=i),
            index_code="H30269", index_name="测试",
            dividend_yield_2=float(start + i),
        )
        for i in range(n)
    ]


class TestMedians(unittest.TestCase):
    def test_42_median(self):
        # 42 个值：0..41，中位数应为 (20+21)/2 = 20.5
        recs = _recs_n(42, start=0)
        m = compute_medians(recs, {"M42": 42})
        self.assertAlmostEqual(m["M42"], 20.5, places=6)

    def test_median_uses_last_n(self):
        # 50 个值，M42 取最后 42 个的中位数
        recs = _recs_n(50, start=0)
        m = compute_medians(recs, {"M42": 42})
        last42 = [float(i) for i in range(50)][-42:]
        self.assertAlmostEqual(m["M42"], statistics.median(last42), places=6)


class TestPercentPoints(unittest.TestCase):
    def test_add_point(self):
        th = compute_thresholds(4.90, CONFIG)
        self.assertAlmostEqual(th["A_buy"], 4.92, places=4)   # 4.90 + 0.02
        self.assertAlmostEqual(th["A_sell"], 4.89, places=4)  # 4.90 - 0.01
        self.assertAlmostEqual(th["B_buy"], 4.97, places=4)   # +0.07
        self.assertAlmostEqual(th["B_sell"], 4.86, places=4)  # -0.04
        self.assertAlmostEqual(th["C_buy"], 5.02, places=4)   # +0.12
        self.assertAlmostEqual(th["C_sell"], 4.79, places=4)  # -0.11


class TestSignalStateMachine(unittest.TestCase):
    def setUp(self):
        self.th = compute_thresholds(4.90, CONFIG)

    def _pos(self, status=POS_EMPTY):
        return PositionState(name="A", label="A仓", percent=20, status=status)

    def test_empty_meets_buy_triggers_once(self):
        p = self._pos(POS_EMPTY)
        act, _ = evaluate_position(p, 4.92, self.th)  # 4.92 >= A_buy 4.92
        self.assertEqual(act, ACT_BUY)
        # 模拟已买入后，即便仍满足买线也不应再提示买入
        p.status = POS_HOLDING
        act2, _ = evaluate_position(p, 4.92, self.th)
        self.assertEqual(act2, ACT_HOLD)

    def test_holding_no_repeat_buy(self):
        p = self._pos(POS_HOLDING)
        act, _ = evaluate_position(p, 4.95, self.th)  # 高于买线
        self.assertNotEqual(act, ACT_BUY)
        self.assertEqual(act, ACT_HOLD)

    def test_holding_reaches_sell(self):
        p = self._pos(POS_HOLDING)
        act, _ = evaluate_position(p, 4.89, self.th)  # 4.89 <= A_sell 4.89
        self.assertEqual(act, ACT_SELL)

    def test_empty_never_sells(self):
        p = self._pos(POS_EMPTY)
        act, _ = evaluate_position(p, 4.80, self.th)  # 低于卖线但空仓
        self.assertNotEqual(act, ACT_SELL)
        self.assertEqual(act, ACT_WAIT)

    def test_empty_below_buy_waits(self):
        p = self._pos(POS_EMPTY)
        act, _ = evaluate_position(p, 4.90, self.th)  # 4.90 < A_buy 4.92
        self.assertEqual(act, ACT_WAIT)


class TestWeeklyLock(unittest.TestCase):
    def test_locked_thresholds_ignore_live_m42(self):
        # 锁定时的 M42
        ws = generate_weekly_strategy(4.90, CONFIG, today=_dt.date(2026, 9, 4))
        locked = {
            "A_buy": ws.a_buy, "A_sell": ws.a_sell,
            "B_buy": ws.b_buy, "B_sell": ws.b_sell,
            "C_buy": ws.c_buy, "C_sell": ws.c_sell,
        }
        # 当周即便“实时 M42”变化（这里用 live 重新算），信号也应基于锁定值
        live = compute_thresholds(5.20, CONFIG)  # 假设实时变了
        p = PositionState(name="A", label="A仓", percent=20, status=POS_EMPTY)
        # 使用锁定阈值：4.92 买线
        self.assertEqual(evaluate_position(p, 4.92, locked)[0], ACT_BUY)
        # 使用 live 阈值（5.22 买线）同样 4.92 不会触发——说明锁定与 live 不同
        self.assertEqual(evaluate_position(p, 4.92, live)[0], ACT_WAIT)
        self.assertNotEqual(locked["A_buy"], live["A_buy"])

    def test_new_week_can_relock(self):
        d1 = _dt.date(2026, 9, 4)   # 2026-W36
        d2 = _dt.date(2026, 9, 14)  # 2026-W37
        self.assertNotEqual(current_week_id(d1), current_week_id(d2))
        ws1 = generate_weekly_strategy(4.90, CONFIG, today=d1)
        ws2 = generate_weekly_strategy(4.95, CONFIG, today=d2)
        self.assertEqual(ws1.week_id, current_week_id(d1))
        self.assertEqual(ws2.week_id, current_week_id(d2))
        self.assertNotEqual(ws1.a_buy, ws2.a_buy)


if __name__ == "__main__":
    unittest.main()
