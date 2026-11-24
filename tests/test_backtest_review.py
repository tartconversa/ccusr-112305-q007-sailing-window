"""往季重跑与业务复盘。"""

import unittest

from shipping.backtest import backtest
from shipping.review import review
from shipping.sources import HistoryRow, load_dataset


class BacktestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()

    def test_reruns_each_week_with_prior_data_only(self):
        report = backtest(self.ds.history)
        weeks = {r.week for r in report.results}
        self.assertNotIn("W01", weeks)  # 第一周无更早历史,不出预测
        self.assertEqual(len(report.results), 10)
        for r in report.results:
            self.assertLessEqual(r.lo, r.mu)
            self.assertLessEqual(r.mu, r.hi)

    def test_coverage_and_misjudgments_reported(self):
        report = backtest(self.ds.history, level=0.8)
        self.assertGreaterEqual(report.coverage, 0.0)
        self.assertLessEqual(report.coverage, 1.0)
        known = [r for r in report.results if r.actual is not None]
        self.assertEqual(len(known), 10)  # 本资料没有末道工序删失的票
        self.assertEqual(report.censored, 0)
        # H07/H10/H11 实际 8-9 天超过生产周:宽松舱位下也有误判
        self.assertGreaterEqual(report.misjudgments_loose, 1)

    def test_tight_capacity_bumps_tickets(self):
        loose = backtest(self.ds.history)
        tight = backtest(self.ds.history, capacity_per_group=1)
        bumped = [r for r in tight.results if r.bumped]
        self.assertTrue(bumped)
        # 每运输批次限 1 票:同批 mu 大的票被挤到下一周
        self.assertGreaterEqual(tight.misjudgments_tight, loose.misjudgments_loose)

    def test_censored_ticket_counted(self):
        rows = [
            HistoryRow("L1", "T1", "W01", "缝制", "D0", 2, 2, "不适用", 0, "W02", "G"),
            HistoryRow("L2", "T1", "W01", "装箱", "D0", 4, 4, "不适用", 0, "W02", "G"),
            HistoryRow("L3", "T2", "W02", "缝制", "D0", 2, 2, "不适用", 0, "W03", "G"),
            HistoryRow("L4", "T2", "W02", "装箱", "D0", 4, None, "不适用", 0, "W03", "G"),
        ]
        report = backtest(rows)
        self.assertEqual(report.censored, 1)
        censored = next(r for r in report.results if r.actual is None)
        self.assertIsNone(censored.covered)
        self.assertIsNone(censored.misjudgment_loose)


class ReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.text = review(cls.ds, 79)

    def test_why_previous_sailing_not_chosen(self):
        self.assertIn("为什么上一班没选", self.text)
        self.assertIn("当时 S2", self.text)

    def test_actual_sets_received(self):
        self.assertIn("17/30 套", self.text)
        self.assertIn("半套余量 11 件", self.text)

    def test_risk_materialized(self):
        self.assertIn("延误风险变现", self.text)
        self.assertIn("误班", self.text)  # C12 实际就绪 35h 误 S1 截 22h
        self.assertIn("赶及", self.text)  # C22 赶及 S2

    def test_rejected_modify_visible(self):
        self.assertIn("拒绝", self.text)


if __name__ == "__main__":
    unittest.main()
