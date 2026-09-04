"""D/P2 directional-change 拐点纯算法单元测试。

覆盖：
1. 小于 0.06 的反向波动不能确认拐点。
2. 刚好 0.06 必须确认。
3. 顶底必须交替。
4. 连续创新高时必须更新 peak candidate，不能提前确认。
5. 连续创新低时不能提前确认 trough。
6. 当前 115 条官方历史 CSV 应得到纯 0.06 状态机输出的 15 个候选拐点，
   且必须包含 2026-06-26/5.19 peak 与 2026-07-30/4.59 trough。
   不包含任何二次合并/过滤逻辑。
"""

from __future__ import annotations

import csv
import datetime as _dt
import unittest
from decimal import Decimal
from pathlib import Path

from yieldwave.models import ValuationRecord
from yieldwave.strategy import detect_turning_points


def _d(date_str: str, dp2: str) -> dict:
    return {"date": date_str, "dp2": Decimal(dp2)}


def _dates(values: list[dict]):
    return [v["date"] for v in values]


def _rec(date_str: str, dy2: str) -> ValuationRecord:
    return ValuationRecord(
        date=_dt.date.fromisoformat(date_str),
        index_code="H30269",
        index_name="t",
        dividend_yield_2=Decimal(dy2),
    )


class TestTurningPointRules(unittest.TestCase):
    def test_less_than_0_06_cannot_confirm(self):
        values = [_d("2026-01-01", "5.00"), _d("2026-01-02", "5.04"), _d("2026-01-03", "5.01")]
        tps = detect_turning_points(values)
        self.assertEqual(tps, [])

    def test_exactly_0_06_confirms_peak(self):
        values = [_d("2026-01-01", "5.00"), _d("2026-01-02", "5.10"), _d("2026-01-03", "5.04")]
        tps = detect_turning_points(values)
        self.assertEqual(len(tps), 1)
        self.assertEqual(tps[0]["kind"], "peak")
        self.assertEqual(tps[0]["pivot_date"], "2026-01-02")
        self.assertEqual(tps[0]["pivot_dp2"], Decimal("5.10"))
        self.assertEqual(tps[0]["confirm_date"], "2026-01-03")
        self.assertEqual(tps[0]["confirm_dp2"], Decimal("5.04"))
        self.assertEqual(tps[0]["reversal"], Decimal("0.06"))

    def test_peak_trough_alternate(self):
        values = [
            _d("2026-01-01", "5.00"),
            _d("2026-01-02", "5.10"),
            _d("2026-01-03", "5.04"),
            _d("2026-01-04", "4.96"),
            _d("2026-01-05", "5.06"),
        ]
        tps = detect_turning_points(values)
        self.assertEqual([p["kind"] for p in tps], ["peak", "trough"])
        self.assertEqual(tps[0]["pivot_dp2"], Decimal("5.10"))
        self.assertEqual(tps[1]["pivot_dp2"], Decimal("4.96"))

    def test_continuous_new_high_updates_peak_candidate(self):
        values = [
            _d("2026-01-01", "5.00"),
            _d("2026-01-02", "5.01"),
            _d("2026-01-03", "5.05"),
            _d("2026-01-04", "5.09"),
            _d("2026-01-05", "5.10"),
            _d("2026-01-06", "5.04"),
        ]
        tps = detect_turning_points(values)
        self.assertEqual(len(tps), 1)
        self.assertEqual(tps[0]["kind"], "peak")
        # 不能把 5.01/5.05 之类中途高点误当成最终 peak
        self.assertEqual(tps[0]["pivot_date"], "2026-01-05")
        self.assertEqual(tps[0]["pivot_dp2"], Decimal("5.10"))

    def test_continuous_new_low_does_not_prematurely_confirm_trough(self):
        values = [
            _d("2026-01-01", "5.00"),
            _d("2026-01-02", "5.10"),
            _d("2026-01-03", "5.04"),   # peak 5.10 先确认
            _d("2026-01-04", "5.03"),
            _d("2026-01-05", "5.02"),
            _d("2026-01-06", "4.98"),
            _d("2026-01-07", "4.94"),
        ]
        tps = detect_turning_points(values)
        self.assertEqual(len(tps), 1)
        self.assertEqual(tps[0]["kind"], "peak")
        # 没有出现 0.06 反弹，不能确认底拐点
        self.assertNotIn("trough", [p["kind"] for p in tps])


class TestOfficialHistoryTurningPoints(unittest.TestCase):
    def setUp(self):
        csv_path = Path(__file__).resolve().parents[1] / "exports" / "H30269_dividend_yield_history.csv"
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.records = [
            _rec(r["date"], r["dividend_yield_2"])
            for r in rows
        ]

    def test_115_rows_pure_state_machine_15_turning_points(self):
        self.assertEqual(len(self.records), 115)
        tps = detect_turning_points(self.records)
        expected = [
            ("peak", "2026-03-27", Decimal("4.96")),
            ("trough", "2026-04-02", Decimal("4.88")),
            ("peak", "2026-04-13", Decimal("4.98")),
            ("trough", "2026-04-23", Decimal("4.87")),
            ("peak", "2026-04-27", Decimal("4.93")),
            ("trough", "2026-04-30", Decimal("4.67")),
            ("peak", "2026-05-28", Decimal("4.87")),
            ("trough", "2026-06-01", Decimal("4.67")),
            ("peak", "2026-06-26", Decimal("5.19")),
            ("trough", "2026-07-08", Decimal("4.91")),
            ("peak", "2026-07-09", Decimal("4.97")),
            ("trough", "2026-07-20", Decimal("4.70")),
            ("peak", "2026-07-21", Decimal("4.76")),
            ("trough", "2026-07-30", Decimal("4.59")),
            ("peak", "2026-08-14", Decimal("4.96")),
        ]
        actual = [
            (p["kind"], p["pivot_date"], p["pivot_dp2"])
            for p in tps
        ]
        self.assertEqual(actual, expected)

    def test_contains_2026_06_26_and_2026_07_30(self):
        tps = detect_turning_points(self.records)
        by_pivot = {p["pivot_date"]: p for p in tps}
        self.assertIn("2026-06-26", by_pivot)
        self.assertEqual(by_pivot["2026-06-26"]["kind"], "peak")
        self.assertEqual(by_pivot["2026-06-26"]["pivot_dp2"], Decimal("5.19"))
        self.assertIn("2026-07-30", by_pivot)
        self.assertEqual(by_pivot["2026-07-30"]["kind"], "trough")
        self.assertEqual(by_pivot["2026-07-30"]["pivot_dp2"], Decimal("4.59"))


if __name__ == "__main__":
    unittest.main()
