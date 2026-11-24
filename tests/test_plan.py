"""周会方案:装哪一班、等哪个分包、拆哪一票,四种费用情景。"""

import unittest

from shipping.ledger import Ledger
from shipping.plan import weekly_options
from shipping.readiness import DeskModel, forecast_lots
from shipping.sources import load_dataset


class WeeklyOptionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.model = DeskModel.fit(cls.ds.history)

    def options(self, now):
        ledger = Ledger.build(self.ds.sailings, self.ds.orders,
                              self.ds.carrier_events, until_h=now)
        forecasts = forecast_lots(self.ds.materials, self.ds.evidence, self.model, now)
        return weekly_options(self.ds, ledger, forecasts, now)

    def test_stay_option_credits_own_booking(self):
        options = self.options(10)
        stay = next(o for o in options if o.order == "O1" and o.label.startswith("留 S1"))
        self.assertTrue(stay.feasible)
        self.assertNotIn("候补", stay.label)
        # 同一工位的分包联合概率不高于独立假设的对照
        self.assertLessEqual(stay.ready_prob, stay.ready_prob_independent + 1e-9)

    def test_binding_lot_identified(self):
        options = self.options(10)
        for opt in [o for o in options if o.order == "O1"]:
            self.assertEqual(opt.binding_lot, "C12")  # 等哪个分包

    def test_waitlist_when_capacity_short(self):
        # 20h 时 B2 已占 S2 18 件,O1 需要 60 > 余 50 → 候补情景
        options = self.options(20)
        wait = next(o for o in options if o.order == "O1" and "候补" in o.label)
        self.assertIn("S2", wait.label)
        self.assertGreater(wait.cost.waitlist, 0)
        self.assertTrue(any("需人工确认" in r for r in wait.reasons))

    def test_rebook_fee_after_cancel_deadline(self):
        options = self.options(20)  # S1 cancel_h = 19
        move = next(o for o in options if o.order == "O1" and o.label == "装 S3")
        self.assertEqual(move.cost.rebook, 85)
        early = self.options(10)
        move_early = next(o for o in early if o.order == "O1" and o.label == "装 S3")
        self.assertEqual(move_early.cost.rebook, 0)

    def test_split_respects_gap_and_separate_sail(self):
        options = self.options(0)
        splits = [o for o in options if o.order == "O2" and o.label.startswith("拆:")]
        self.assertTrue(splits)
        by_label = {o.label: o for o in splits}
        # S2/S3 间隔 20h < 约定 24h
        gap = by_label["拆:C21→S2,C22→S3"]
        self.assertFalse(gap.feasible)
        self.assertTrue(any("间隔" in r for r in gap.reasons))
        # 同船违反非同船限制
        same = by_label["拆:C21→S2,C22→S2"]
        self.assertFalse(same.feasible)
        self.assertTrue(any("非同船" in r for r in same.reasons))
        # C21→S1, C22→S2 间隔 24h,0h 时舱位够 → 可行
        good = by_label["拆:C21→S1,C22→S2"]
        self.assertTrue(good.feasible)

    def test_whole_set_order_has_no_split(self):
        options = self.options(10)
        self.assertFalse(
            any(o.order == "O1" and o.label.startswith("拆:") for o in options)
        )

    def test_penalty_uses_miss_chain_with_fallback(self):
        # S3 几乎肯定赶上 → 留 S1 的赔付期望因后续 fallback 而接近 0
        options = self.options(10)
        stay = next(o for o in options if o.order == "O1" and o.label.startswith("留 S1"))
        self.assertLess(stay.ready_prob, 0.2)
        self.assertLess(stay.cost.penalty, 50)


if __name__ == "__main__":
    unittest.main()
