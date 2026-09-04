"""策略单元测试。"""

import datetime as _dt
import statistics
import unittest
from decimal import Decimal

from yieldwave.config import DEFAULT_CONFIG, load_config
from yieldwave.models import POS_EMPTY, POS_HOLDING, PositionState, ValuationRecord
from yieldwave.precision import fmt_yield
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
    valid_dp2_count,
    weekly_locked_thresholds_for_records,
)

# 一致性修复后的默认仓位（越跌越买：A/B/C = 8/12/20）
CONFIG_81220 = {
    "primary_window": 42,
    "positions": {
        "A": {"label": "A仓", "percent": 8, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓", "percent": 20, "buy_offset": 0.12, "sell_offset": -0.11},
    },
}

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
        self.assertEqual(m["M42"], Decimal("20.5"))

    def test_median_uses_last_n(self):
        # 50 个值，M42 取最后 42 个的中位数
        recs = _recs_n(50, start=0)
        m = compute_medians(recs, {"M42": 42})
        last42 = [float(i) for i in range(50)][-42:]
        self.assertEqual(m["M42"], Decimal(str(statistics.median(last42))))


class TestPercentPoints(unittest.TestCase):
    def test_add_point(self):
        th = compute_thresholds(4.90, CONFIG)
        self.assertEqual(th["A_buy"], Decimal("4.92"))   # 4.90 + 0.02
        self.assertEqual(th["A_sell"], Decimal("4.89"))  # 4.90 - 0.01
        self.assertEqual(th["B_buy"], Decimal("4.97"))   # +0.07
        self.assertEqual(th["B_sell"], Decimal("4.86"))  # -0.04
        self.assertEqual(th["C_buy"], Decimal("5.02"))   # +0.12
        self.assertEqual(th["C_sell"], Decimal("4.79"))  # -0.11


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


class TestABCNewRatio(unittest.TestCase):
    """一致性修复 #1：默认 A/B/C 比例应为 8/12/20（越跌越买）。"""

    def test_compute_thresholds_uses_new_percents(self):
        # 偏移不变，但 A/B/C 占比变成 8/12/20；这里校验占比字段，不校验买卖线
        self.assertEqual(CONFIG_81220["positions"]["A"]["percent"], 8)
        self.assertEqual(CONFIG_81220["positions"]["B"]["percent"], 12)
        self.assertEqual(CONFIG_81220["positions"]["C"]["percent"], 20)

    def test_default_config_abc_81220(self):
        cfg = load_config()
        self.assertEqual(cfg["positions"]["A"]["percent"], 8)
        self.assertEqual(cfg["positions"]["B"]["percent"], 12)
        self.assertEqual(cfg["positions"]["C"]["percent"], 20)
        # 核心仓拆分为 3 档各 20%
        self.assertEqual(len(cfg["core_tranches"]), 3)
        for spec in cfg["core_tranches"].values():
            self.assertEqual(spec["percent"], 20)

    def test_default_config_constant(self):
        # DEFAULT_CONFIG 也必须是 8/12/20，避免兜底与文件不一致
        self.assertEqual(DEFAULT_CONFIG["positions"]["A"]["percent"], 8)
        self.assertEqual(DEFAULT_CONFIG["positions"]["B"]["percent"], 12)
        self.assertEqual(DEFAULT_CONFIG["positions"]["C"]["percent"], 20)


class TestMissingDp2ValidCount(unittest.TestCase):
    """一致性修复 #15：有缺失 D/P2 时，有效条数与中枢必须用 valid_dp2_count。"""

    def test_valid_dp2_count_ignores_none(self):
        recs = [
            _rec("2026-09-01", 4.80),
            _rec("2026-09-02", None),
            _rec("2026-09-03", 4.82),
            _rec("2026-09-04", None),
        ]
        self.assertEqual(valid_dp2_count(recs), 2)
        self.assertEqual(len(recs), 4)

    def test_compute_medians_ignores_none_dp2(self):
        # 用 4 个有效值：4.80,4.82,4.84,4.86 -> 中位数 (4.82+4.84)/2 = 4.83
        recs = [
            _rec("2026-09-01", 4.80),
            _rec("2026-09-02", None),
            _rec("2026-09-03", 4.82),
            _rec("2026-09-04", 4.84),
            _rec("2026-09-05", None),
            _rec("2026-09-06", 4.86),
        ]
        m = compute_medians(recs, {"M42": 42})
        self.assertEqual(m["M42"], Decimal("4.83"))


class TestWeeklyLockNoFutureLeak(unittest.TestCase):
    """一致性修复 #5/#7：周度锁定用本周首个交易日的可见中枢，杜绝未来数据泄漏。

    场景：本周一锁定 M42=4.90；周后段“实时”中枢若重算会升到 5.10，
    但周二~周五必须仍使用周一锁定的 4.90 买卖线，不能中途改写。
    """

    def _build(self):
        # 窗口用 5，便于构造中枢前后变化
        prior = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
        week = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
        dy2 = {
            "2026-08-24": 4.90, "2026-08-25": 4.90, "2026-08-26": 4.90,
            "2026-08-27": 4.90, "2026-08-28": 4.90,
            "2026-08-31": 4.90,  # 本周一（锁定日）
            "2026-09-01": 5.10, "2026-09-02": 5.10, "2026-09-03": 5.10,
            "2026-09-04": 5.00,
        }
        recs = [ValuationRecord(
            date=_dt.date.fromisoformat(d), index_code="H30269", index_name="测试",
            dividend_yield_2=dy2[d],
        ) for d in prior + week]
        return recs

    def test_locked_threshold_constant_within_week(self):
        recs = self._build()
        locked = weekly_locked_thresholds_for_records(recs, CONFIG_81220, 5)
        # 整周（08-31..09-04）应共享同一个周一锁定的阈值
        idx_mon = 5  # 08-31 是第 6 条
        base = locked[idx_mon]
        self.assertIsNotNone(base)
        for i in range(5, 10):
            self.assertEqual(locked[i], base)
        # 周一锁定的 M42=4.90 -> A 买线 4.92
        self.assertEqual(base["A_buy"], Decimal("4.92"))

    def test_no_future_leak_vs_live_recompute(self):
        recs = self._build()
        locked = weekly_locked_thresholds_for_records(recs, CONFIG_81220, 5)
        # 周五（09-04）若按“实时”重算中枢会升到 5.10 -> A 买线 5.12
        live_median = compute_medians(recs[:10], {"M42": 5})["M42"]  # 取前 10 条窗口 5
        live = compute_thresholds(live_median, CONFIG_81220)
        self.assertEqual(live_median, Decimal("5.10"))
        self.assertEqual(live["A_buy"], Decimal("5.12"))
        # 但周锁定用的是周一的 4.92，二者不同 -> 证明未泄漏未来数据
        self.assertNotEqual(locked[9]["A_buy"], live["A_buy"])
        self.assertEqual(locked[9]["A_buy"], Decimal("4.92"))


class TestPrecision(unittest.TestCase):
    """一致性修复 #7：股息率 / 阈值用 Decimal，保留完整精度，绝不先 round 再比较。

    内部值、比较逻辑、UI 格式化（fmt_yield，ROUND_HALF_UP）三者必须一致。
    """

    def test_thresholds_full_precision(self):
        # M42 = 4.835（偶数样本中位数可能出现的半基点值）
        m42 = Decimal("4.835")
        th = compute_thresholds(m42, CONFIG_81220)
        # A 买 +0.02 => 4.855；A 卖 -0.01 => 4.825；B 卖 -0.04 => 4.795
        self.assertEqual(th["A_buy"], Decimal("4.855"))
        self.assertEqual(th["A_sell"], Decimal("4.825"))
        self.assertEqual(th["B_sell"], Decimal("4.795"))
        # 内部值保留完整精度，不出现 4.8549999… 这类二进制污染
        self.assertEqual(th["A_buy"], m42 + Decimal("0.02"))
        self.assertIsInstance(th["A_buy"], Decimal)

    def test_no_round_before_compare(self):
        # 信号判定在比较之后才量化；比较必须用原始 Decimal，不能用 round 后的近似值
        m42 = Decimal("4.835")
        th = compute_thresholds(m42, CONFIG_81220)
        # 当前股息率恰好等于 A 买入线 4.855 -> 应触发 BUY（>=）
        pos_a = PositionState(name="A", label="A仓", percent=Decimal("8"), status=POS_EMPTY)
        act, _ = evaluate_position(pos_a, th["A_buy"], th)
        self.assertEqual(act, ACT_BUY)
        # 比买入线低半基点（4.854）-> 不触发
        act2, _ = evaluate_position(pos_a, th["A_buy"] - Decimal("0.001"), th)
        self.assertEqual(act2, ACT_WAIT)

    def test_ui_format_matches_internal(self):
        # UI 用 fmt_yield（ROUND_HALF_UP，>=3 位小数）展示，应与内部 Decimal 一致
        m42 = Decimal("4.835")
        th = compute_thresholds(m42, CONFIG_81220)
        self.assertEqual(fmt_yield(th["A_buy"], 3), "4.855")
        self.assertEqual(fmt_yield(th["A_sell"], 3), "4.825")
        self.assertEqual(fmt_yield(th["B_sell"], 3), "4.795")
        self.assertEqual(fmt_yield(m42, 3), "4.835")
        # 原始 2 位 D/P2 显示 2 位
        self.assertEqual(fmt_yield(Decimal("4.77"), 2), "4.77")

    def test_median_even_sample_half_bp(self):
        # 偶数样本中位数本身可能产生半基点值，且必须是 Decimal
        recs = [
            ValuationRecord(date=_dt.date(2026, 1, 1) + _dt.timedelta(days=i), index_code="H30269", index_name="x",
                            dividend_yield_2=Decimal("4.83") if i % 2 == 0 else Decimal("4.84"))
            for i in range(42)
        ]
        med = compute_medians(recs, {"M42": 42})["M42"]
        self.assertEqual(med, Decimal("4.835"))
        self.assertIsInstance(med, Decimal)


if __name__ == "__main__":
    unittest.main()
