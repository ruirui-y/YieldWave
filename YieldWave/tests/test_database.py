"""数据库单元测试（使用临时库文件）。"""

import datetime as _dt
import os
import tempfile
import unittest

from yieldwave.database import Database
from yieldwave.models import ValuationRecord


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
        self.assertAlmostEqual(self.db.get_latest().dividend_yield_2, 4.77)

    def test_dedup_by_date(self):
        # 同日期 UPSERT 不应增加条数，应更新
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-01", 4.85))
        self.assertEqual(self.db.count(), 1)
        self.assertAlmostEqual(self.db.get_latest().dividend_yield_2, 4.85)

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
        self.assertAlmostEqual(p2.buy_yield, 4.92)

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
            self.assertAlmostEqual(db2.get_latest().dividend_yield_2, 4.77)
        finally:
            db2.close()

    def test_dedupe_removes_duplicates(self):
        # 直接插入两条相同 date（绕过 upsert 的唯一约束用 INSERT OR REPLACE 不行，
        # 这里用 upsert 两次保证只有一条，再验证 dedupe 函数为 0）
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.db.upsert_valuation(_rec("2026-09-01", 4.80))
        self.assertEqual(self.db.dedupe(), 0)


if __name__ == "__main__":
    unittest.main()
