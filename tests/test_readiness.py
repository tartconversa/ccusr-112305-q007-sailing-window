"""就绪预估:返检中区别于完工未知,同工位/同批次关联不当作独立。"""

import unittest

from shipping.readiness import (
    STATE_DONE,
    STATE_REWORK,
    STATE_UNKNOWN,
    DeskModel,
    LotForecast,
    forecast_lots,
    independent_prob_ready,
    joint_prob_ready,
)
from shipping.sources import HistoryRow, load_dataset


def _row(order, week, desk, planned, actual, process="装箱", inspection="不适用",
         group="G", available="W99"):
    return HistoryRow(
        lot=f"{order}{process}", order=order, week=week, process=process, desk=desk,
        planned=planned, actual=actual, inspection_result=inspection,
        wait_hours=0, available=available, dispatch_group=group,
    )


class DeskModelTest(unittest.TestCase):
    def test_censored_ticket_not_in_mean(self):
        # 末道工序 actual 为空 = 完工时间未知的旧单,只作删失证据
        history = [
            _row("T1", "W01", "D0", 4, 4),
            _row("T2", "W01", "D0", 4, None),  # 完工未知
            _row("T3", "W01", "D0", 4, 8),
        ]
        model = DeskModel.fit(history)
        stats = model.stats["D0"]
        self.assertEqual(stats.tickets, 2)
        self.assertEqual(stats.censored, 1)
        self.assertAlmostEqual(stats.ratio_mean, ((0 / 4) + (4 / 4)) / 2)

    def test_mid_process_missing_actual_is_not_censored(self):
        # 中间工序缺失但已装箱:按已知完工处理
        history = [
            _row("T1", "W01", "D0", 2, 2, process="缝制"),
            _row("T1", "W01", "D0", 3, None, process="返检", inspection="通过"),
            _row("T1", "W01", "D0", 4, 7, process="装箱"),
        ]
        model = DeskModel.fit(history)
        self.assertEqual(model.stats["D0"].censored, 0)
        self.assertAlmostEqual(model.stats["D0"].ratio_mean, (7 - 4) / 4)

    def test_rework_subsample_separate(self):
        history = [
            _row("T1", "W01", "D0", 4, 5, inspection="需返检"),
            _row("T2", "W01", "D0", 4, 8, inspection="通过"),
        ]
        model = DeskModel.fit(history)
        stats = model.stats["D0"]
        self.assertAlmostEqual(stats.rework_mean, 0.25)
        self.assertAlmostEqual(stats.ratio_mean, (0.25 + 1.0) / 2)

    def test_small_sample_falls_back_to_global(self):
        history = [_row("T1", "W01", "D0", 4, 8)]
        model = DeskModel.fit(history)
        self.assertIs(model.desk("D9"), model.global_stats)


class ForecastTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.model = DeskModel.fit(cls.ds.history)

    def test_done_lot_is_deterministic(self):
        fc = forecast_lots(self.ds.materials, [], self.model, 0)
        self.assertEqual(fc["C11"].state, STATE_DONE)
        self.assertEqual(fc["C11"].sigma, 0.0)
        self.assertEqual(fc["C11"].prob_ready_by(8), 1.0)
        self.assertEqual(fc["C11"].prob_ready_by(7.9), 0.0)

    def test_rework_uses_live_estimate_not_unknown(self):
        fc = forecast_lots(self.ds.materials, [], self.model, 0)
        c12 = fc["C12"]
        self.assertEqual(c12.state, STATE_REWORK)
        # 返工未结束:实时估计 ×(1+返工超出),不是完工未知
        self.assertGreater(c12.mu, 21)
        self.assertLess(c12.mu, 72)

    def test_unknown_lot_wide_distribution(self):
        fc = forecast_lots(self.ds.materials, [], self.model, 0, lots=["C99"])
        self.assertEqual(fc["C99"].state, STATE_UNKNOWN)
        self.assertGreater(fc["C99"].sigma, 10)

    def test_passed_evidence_narrows_uncertainty(self):
        evidence = [e for e in self.ds.evidence if e.lot == "C21"]
        fc = forecast_lots(self.ds.materials, evidence, self.model, 18)
        self.assertAlmostEqual(fc["C21"].mu, 21.0)
        self.assertLess(fc["C21"].sigma, 2.0)

    def test_rework_evidence_keeps_rework_state(self):
        evidence = [e for e in self.ds.evidence if e.lot == "C12" and e.hour <= 18]
        fc = forecast_lots(self.ds.materials, evidence, self.model, 18)
        self.assertEqual(fc["C12"].state, STATE_REWORK)
        self.assertGreater(fc["C12"].mu, 32)  # 证据 32h 再按返工放大


class JointProbabilityTest(unittest.TestCase):
    def _forecast(self, lot, desk, mu=10.0, sigma=2.0):
        return LotForecast(lot, desk, "返检中", mu, sigma, "测试")

    def test_same_desk_joint_exceeds_independent_product(self):
        forecasts = {"A": self._forecast("A", "D0"), "B": self._forecast("B", "D0")}
        joint = joint_prob_ready(forecasts, ["A", "B"], 10.0)
        indep = independent_prob_ready(forecasts, ["A", "B"], 10.0)
        self.assertGreater(joint, indep)
        self.assertAlmostEqual(indep, 0.25, places=3)
        # 二元正态 rho=0.5 的理论值 1/3
        self.assertAlmostEqual(joint, 1 / 3, places=2)

    def test_same_group_correlates_across_desks(self):
        forecasts = {"A": self._forecast("A", "D0"), "B": self._forecast("B", "D1")}
        groups = {"A": "S1", "B": "S1"}
        joint = joint_prob_ready(forecasts, ["A", "B"], 10.0, groups=groups)
        indep = independent_prob_ready(forecasts, ["A", "B"], 10.0)
        self.assertGreater(joint, indep)

    def test_unrelated_lots_stay_independent(self):
        forecasts = {"A": self._forecast("A", "D0"), "B": self._forecast("B", "D1")}
        joint = joint_prob_ready(forecasts, ["A", "B"], 10.0)
        self.assertAlmostEqual(
            joint, independent_prob_ready(forecasts, ["A", "B"], 10.0), places=6
        )

    def test_deterministic_lot_is_constant_factor(self):
        forecasts = {
            "A": self._forecast("A", "D0", mu=5.0, sigma=0.0),
            "B": self._forecast("B", "D0"),
        }
        joint = joint_prob_ready(forecasts, ["A", "B"], 10.0)
        self.assertAlmostEqual(joint, forecasts["B"].prob_ready_by(10.0), places=6)
        # 确定性分包来不及 → 联合为零
        late = {
            "A": self._forecast("A", "D0", mu=50.0, sigma=0.0),
            "B": self._forecast("B", "D0"),
        }
        self.assertEqual(joint_prob_ready(late, ["A", "B"], 10.0), 0.0)


if __name__ == "__main__":
    unittest.main()
