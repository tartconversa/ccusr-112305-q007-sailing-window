"""重排约束:不放掉最后一个可行班次,取消截止,已装车可撤回性。"""

import unittest

from shipping.ledger import Ledger
from shipping.readiness import DeskModel, forecast_lots
from shipping.replan import evaluate_evidence, evaluate_replan
from shipping.sources import load_dataset


class ReplanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.model = DeskModel.fit(cls.ds.history)

    def verdict(self, order, target, now):
        ledger = Ledger.build(self.ds.sailings, self.ds.orders,
                              self.ds.carrier_events, until_h=now)
        forecasts = forecast_lots(self.ds.materials, self.ds.evidence, self.model, now)
        return evaluate_replan(self.ds, ledger, forecasts, order, target, now)

    def test_never_release_before_acceptance(self):
        verdict = self.verdict("O1", "S3", 20)
        self.assertEqual(verdict.action, "持证申请撤改")
        self.assertTrue(verdict.keep_original)
        self.assertTrue(any("保留" in n for n in verdict.notes))

    def test_cancel_deadline_fee_after_cancel_h(self):
        late = self.verdict("O1", "S3", 20)  # S1 cancel_h = 19
        self.assertEqual(late.fees.get("改舱费"), 85)
        self.assertTrue(late.manual_confirm)
        early = self.verdict("O1", "S3", 18)
        self.assertNotIn("改舱费", early.fees)

    def test_capacity_block_keeps_original(self):
        verdict = self.verdict("O1", "S2", 20)  # S2 余 50 < 60
        self.assertEqual(verdict.action, "保持原状")
        self.assertTrue(verdict.keep_original)
        self.assertTrue(any("余舱" in b for b in verdict.blocks))

    def test_loaded_lot_cannot_be_withdrawn(self):
        # C22 已装车,没有货代解除或拒绝回执 → 阻断
        verdict = self.verdict("O2", "S1", 20)
        self.assertEqual(verdict.action, "保持原状")
        self.assertTrue(any("已装车" in b for b in verdict.blocks))

    def test_wrong_destination_blocked(self):
        verdict = self.verdict("O3", "S1", 32)  # O3 去南港,S1 去北港
        self.assertEqual(verdict.action, "保持原状")
        self.assertTrue(any("目的港" in b for b in verdict.blocks))

    def test_last_feasible_sailing_noted(self):
        # O3 只有 S4 一个南港班次 → 原舱是最后一个可行班次
        ledger = Ledger.build(self.ds.sailings, self.ds.orders,
                              self.ds.carrier_events, until_h=32)
        forecasts = forecast_lots(self.ds.materials, self.ds.evidence, self.model, 32)
        verdict = evaluate_replan(self.ds, ledger, forecasts, "O3", None, 32)
        self.assertTrue(verdict.keep_original)

    def test_evidence_triggers_evaluation(self):
        ledger = Ledger.build(self.ds.sailings, self.ds.orders,
                              self.ds.carrier_events, until_h=29)
        forecasts = forecast_lots(self.ds.materials, self.ds.evidence, self.model, 29)
        verdicts = evaluate_evidence(self.ds, ledger, forecasts, "C12", 29)
        self.assertTrue(verdicts)
        # 新证据下 S1 已赶不上,且过截装后原舱不可调整 → 全部保持原状并说明
        for verdict in verdicts:
            self.assertTrue(verdict.keep_original)

    def test_unbooked_order_direct_booking(self):
        ledger = Ledger.build(self.ds.sailings, self.ds.orders,
                              self.ds.carrier_events, until_h=10)
        forecasts = forecast_lots(self.ds.materials, self.ds.evidence, self.model, 10)
        verdict = evaluate_replan(self.ds, ledger, forecasts, "O3", "S4", 10)
        self.assertEqual(verdict.action, "直接预订")
        self.assertFalse(verdict.keep_original)


if __name__ == "__main__":
    unittest.main()
