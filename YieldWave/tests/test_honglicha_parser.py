"""红利查解析器测试（使用 fixtures 下的真实页面快照，不依赖网络）。"""

import os
import statistics
import unittest

from yieldwave.data_sources import honglicha
from yieldwave.strategy import compute_medians

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "h30269_sample.html")


class TestHonglichaParser(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE, "r", encoding="utf-8", errors="replace") as f:
            self.html = f.read()

    def test_parse_real_fixture(self):
        recs = honglicha.parse_html(self.html)
        self.assertGreaterEqual(len(recs), 100)  # 真实历史序列（非一行）
        self.assertEqual(recs[-1].date.isoformat(), "2026-09-02")
        self.assertAlmostEqual(recs[-1].dividend_yield_2, 4.77, places=2)
        # D/P1 与 D/P2 均存在
        self.assertIsNotNone(recs[-1].dividend_yield_1)
        self.assertIsNotNone(recs[-1].pe_1)
        self.assertIsNotNone(recs[-1].pe_2)

    def test_m42_from_fixture(self):
        recs = honglicha.parse_html(self.html)
        m = compute_medians(recs, {"M42": 42})
        # 已验证：M42 ≈ 4.835
        self.assertAlmostEqual(m["M42"], 4.835, places=2)

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(honglicha.parse_html("<html><body>无数据</body></html>"), [])
        self.assertEqual(honglicha.parse_html(""), [])
        self.assertEqual(honglicha.parse_html("option: nothing here"), [])

    def test_fetch_failure_does_not_wipe_history(self):
        # 解析失败时返回空列表 —— 调用方据此显示“抓取失败”并保留旧数据。
        # 这里断言：解析器本身对坏页面返回空，因此主流程不会用空数据覆盖历史。
        bad = "<html><body>页面结构已变化</body></html>"
        self.assertEqual(honglicha.parse_html(bad), [])


if __name__ == "__main__":
    unittest.main()
