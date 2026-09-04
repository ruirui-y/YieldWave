"""数据库单元测试（使用临时库文件）。"""

import datetime as _dt
import os
import tempfile
import unittest
from decimal import Decimal

from yieldwave.database import Database
from yieldwave.models import POS_EMPTY, POS_HOLDING, Trade, ValuationRecord
from yieldwave.strategy import current_week_id, generate_weekly_strategy

# 含偏移量的配置（供周度锁定生成使用）
_WCFG = {
    "primary_window": 42,
    "positions": {
        "A": {"label": "A仓", "percent": 8, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓", "percent": 20, "buy_offset": 0.12, "sell_offset": -0.11},
    },
}

# 一致性修复后的新配置（A/B/C = 8/12/20 + 核心 3 档）
_NEW_CONFIG = {
    "positions": {
        "A": {"label": "A仓(小)", "percent": 8, "buy_offset": 0.02, "sell_offset": -0.01},
        "B": {"label": "B仓(中)", "percent": 12, "buy_offset": 0.07, "sell_offset": -0.04},
        "C": {"label": "C仓(大)", "percent": 20, "buy_offset": 0.12, "sell_offset": -0.11},
    },
    "core_tranches": {
        "CORE1": {"label": "核心1", "percent": 20, "build_percentile": 50},
        "CORE2": {"label": "核心2", "percent": 20, "build_percentile": 65},
        "CORE3": {"label": "核心3", "percent": 20, "build_percentile": 80},
    },
}


def _rec(date_str, dy2=4.80, source="honglicha"):
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str), index_code="H30269", index_name="测试",
        dividend_yield_1=4.20, dividend_yield_2=dy2, pe_1=8.0, pe_2=7.9,
        close=None, source=source,
    )


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))
        self.db.ensure_positions({
            "A": {"label": "A仓", "percent": 20},
            "B": {"label": "B仓", "percent": 12},
            "C": {"label": "C仓", "percent": 8},
        })

    def tearDown(self):
        self.db.close()

    def test_upsert_and_latest(self):
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-02", 4.77))
        self.assertEqual(self.db.count(), 2)
        self.assertEqual(self.db.latest_date(), "2026-09-02")
        self.assertEqual(self.db.earliest_date(), "2026-09-01")
        self.assertEqual(self.db.get_latest().dividend_yield_2, Decimal("4.77"))

    def test_dedup_by_date(self):
        # 同日期 UPSERT 不应增加条数，应更新
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-01", 4.85))
        self.assertEqual(self.db.count(), 1)
        self.assertEqual(self.db.get_latest().dividend_yield_2, Decimal("4.85"))

    def test_never_deletes_history(self):
        self.db.upsert_valuation(_rec("2026-08-30", 4.90))
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        # 新日期插入，旧日期仍在
        self.assertEqual(self.db.count(), 2)
        self.assertIsNotNone(self.db.get_by_date("2026-08-30"))

    def test_positions_persist(self):
        p = self.db.get_position("A")
        self.assertEqual(p.status, "EMPTY")
        p.status = "HOLDING"
        p.buy_yield = 4.92
        self.db.save_position(p)
        p2 = self.db.get_position("A")
        self.assertEqual(p2.status, "HOLDING")
        self.assertEqual(p2.buy_yield, Decimal("4.92"))

    def test_csv_roundtrip(self):
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-02", 4.77))
        path = os.path.join(self.tmp, "out.csv")
        n = self.db.export_csv(path)
        self.assertEqual(n, 2)
        # 导入到新库（UPSERT）
        db2 = Database(os.path.join(self.tmp, "test2.db"))
        try:
            m = db2.import_csv(path, replace=False)
            self.assertEqual(m, 2)
            self.assertEqual(db2.get_latest().dividend_yield_2, Decimal("4.77"))
        finally:
            db2.close()

    def test_dedupe_removes_duplicates(self):
        # 直接插入两条相同 date（绕过 upsert 的唯一约束用 INSERT OR REPLACE 不行，
        # 这里用 upsert 两次保证只有一条，再验证 dedupe 函数为 0）
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.assertEqual(self.db.dedupe(), 0)


class TestSafeMigration(unittest.TestCase):
    """一致性修复 #2：旧库 A/B/C=20/12/8 安全迁移到 8/12/20，不得破坏 HOLDING 状态与历史。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))
        # 旧配置：只有 A/B/C，且比例为 20/12/8，无核心仓
        self.db.ensure_positions({
            "A": {"label": "A仓", "percent": 20},
            "B": {"label": "B仓", "percent": 12},
            "C": {"label": "C仓", "percent": 8},
        })

    def tearDown(self):
        self.db.close()

    def test_migration_preserves_holding_and_history(self):
        # 先把 A 设为持仓并记录成交信息
        a = self.db.get_position("A")
        self.assertEqual(a.status, POS_EMPTY)
        a.status = POS_HOLDING
        a.buy_date = "2026-09-01"
        a.buy_yield = 4.92
        self.db.save_position(a)

        # 迁移到新配置
        self.db.ensure_positions(_NEW_CONFIG)

        a2 = self.db.get_position("A")
        self.assertEqual(a2.status, POS_HOLDING)          # 状态保留
        self.assertEqual(a2.buy_yield, Decimal("4.92"))        # 历史成交价保留
        self.assertEqual(a2.buy_date, "2026-09-01")       # 历史日期保留
        self.assertEqual(a2.percent, 8)                   # 占比更新为 8
        self.assertEqual(a2.label, "A仓(小)")             # label 更新

        # C 占比应为 20，且仍是空仓
        c2 = self.db.get_position("C")
        self.assertEqual(c2.status, POS_EMPTY)
        self.assertEqual(c2.percent, 20)

        # 核心仓被新增且为空仓
        for name in ("CORE1", "CORE2", "CORE3"):
            core = self.db.get_position(name)
            self.assertIsNotNone(core)
            self.assertEqual(core.status, POS_EMPTY)
            self.assertEqual(core.kind, "core")
            self.assertEqual(core.percent, 20)

        # B 不变
        b2 = self.db.get_position("B")
        self.assertEqual(b2.status, POS_EMPTY)
        self.assertEqual(b2.percent, 12)


class TestWeeklyLockNoOverwrite(unittest.TestCase):
    """一致性修复 #3/#4：本周已锁定禁止覆盖；跨周可重新锁定。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_no_repeat_weekly_lock(self):
        d = _dt.date(2026, 9, 4)  # 2026-W36
        ws1 = generate_weekly_strategy(4.90, _WCFG, today=d)
        self.assertTrue(self.db.save_weekly_strategy(ws1))          # 首次写入
        ws2 = generate_weekly_strategy(5.10, _WCFG, today=d)        # 同周再次写入
        self.assertFalse(self.db.save_weekly_strategy(ws2))         # 禁止覆盖，返回 False
        got = self.db.get_weekly_strategy(current_week_id(d))
        self.assertEqual(got.m42, Decimal("4.90"))                       # 原值未变
        self.assertNotEqual(got.m42, Decimal("5.10"))

    def test_cross_week_relock(self):
        d1 = _dt.date(2026, 9, 4)    # W36
        d2 = _dt.date(2026, 9, 14)   # W37
        self.assertTrue(self.db.save_weekly_strategy(generate_weekly_strategy(4.90, _WCFG, today=d1)))
        self.assertTrue(self.db.save_weekly_strategy(generate_weekly_strategy(5.10, _WCFG, today=d2)))
        g1 = self.db.get_weekly_strategy(current_week_id(d1))
        g2 = self.db.get_weekly_strategy(current_week_id(d2))
        self.assertEqual(g1.m42, Decimal("4.90"))
        self.assertEqual(g2.m42, Decimal("5.10"))


class TestNoFormalSignalWhenUnlocked(unittest.TestCase):
    """一致性修复 #5：未锁定的本周只允许 PREVIEW，不能发出正式 BUY/SELL。

    UI 用 get_weekly_strategy(本周) is None 来禁用“确认成交”按钮。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_unlocked_week_returns_none(self):
        wid = current_week_id(_dt.date(2026, 9, 4))
        self.assertIsNone(self.db.get_weekly_strategy(wid))  # 未锁定 -> PREVIEW

    def test_locked_week_returns_strategy(self):
        d = _dt.date(2026, 9, 4)
        self.db.save_weekly_strategy(generate_weekly_strategy(4.90, _WCFG, today=d))
        self.assertIsNotNone(self.db.get_weekly_strategy(current_week_id(d)))  # 已锁定


class TestTradeStoresLockedValues(unittest.TestCase):
    """一致性修复 #7/#8：成交记录必须存本周锁定的 M42/买卖线 与 信号数据日期。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_trade_stores_locked_m42_and_threshold(self):
        t = Trade(
            id=None, position_name="A", action="BUY",
            signal_date="2026-09-04", execution_date="2026-09-04",
            signal_data_date="2026-09-02",
            dividend_yield=4.92, m42=4.90, threshold=4.92,
            percentage=8, etf_price=None, shares=None, amount=None, note="lock",
        )
        self.db.add_trade(t)
        got = self.db.get_trades("A")[0]
        self.assertEqual(got.m42, Decimal("4.90"))
        self.assertEqual(got.threshold, Decimal("4.92"))

    def test_signal_data_date_distinct_from_execution_date(self):
        t = Trade(
            id=None, position_name="A", action="BUY",
            signal_date="2026-09-04", execution_date="2026-09-04",
            signal_data_date="2026-09-02",  # 官方估值数据日期，可能早于成交日
            dividend_yield=4.92, m42=4.90, threshold=4.92,
            percentage=8, etf_price=None, shares=None, amount=None, note="x",
        )
        self.db.add_trade(t)
        got = self.db.get_trades("A")[0]
        self.assertEqual(got.signal_data_date, "2026-09-02")
        self.assertEqual(got.execution_date, "2026-09-04")
        self.assertNotEqual(got.signal_data_date, got.execution_date)


class TestBatchUpsertTransaction(unittest.TestCase):
    """一致性修复 #10：批量 UPSERT 单事务提交；任一失败整体回滚，不出现半批。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_batch_commit_all(self):
        n = self.db.upsert_many([
            _rec("2026-09-10", 4.80),
            _rec("2026-09-11", 4.81),
        ])
        self.assertEqual(n, 2)
        self.assertEqual(self.db.count(), 2)

    def test_batch_rollback_on_failure(self):
        # 先成功提交 2 条
        self.db.upsert_many([_rec("2026-09-10", 4.80), _rec("2026-09-11", 4.81)])
        before = self.db.count()
        # 第二条 date=None 会在循环内 to_row() 抛错 -> 触发回滚
        bad = ValuationRecord(
            date=None, index_code="H30269", index_name="测试",
            dividend_yield_2=4.82,
        )
        with self.assertRaises(Exception):
            self.db.upsert_many([_rec("2026-09-12", 4.82), bad])
        # 失败不应产生半批提交
        self.assertEqual(self.db.count(), before)


class TestWalBackupConsistency(unittest.TestCase):
    """一致性修复 #13：备份必须用 sqlite3.backup() 包含 WAL 中未 checkpoint 的数据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_backup_includes_all_rows(self):
        for i in range(5):
            self.db.upsert_valuation(_rec(f"2026-10-{i+1:02d}", round(4.80 + i * 0.01, 2)))
        dest = os.path.join(self.tmp, "bak.db")
        path = self.db.backup(dest)
        self.assertTrue(os.path.exists(path))
        db2 = Database(path)
        try:
            self.assertEqual(db2.count(), self.db.count())
            self.assertEqual(db2.latest_date(), self.db.latest_date())
            self.assertEqual(db2.earliest_date(), self.db.earliest_date())
        finally:
            db2.close()


if __name__ == "__main__":
    unittest.main()
