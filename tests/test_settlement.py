"""短装结算与重新报价:是否补发、逾期责任、既成费用不相抵、提议可追溯。"""

import unittest

from shipping.ledger import Ledger
from shipping.readiness import DeskModel, forecast_lots
from shipping.reconcile import reconcile
from shipping.requote import proposals
from shipping.settlement import settle
from shipping.sources import load_dataset

NOW = 47  # COST4 补发预估已知、S3 截装未过的决策时点


class SettlementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.model = DeskModel.fit(cls.ds.history)
        cls.ledger = Ledger.build(cls.ds.sailings, cls.ds.orders,
                                  cls.ds.carrier_events, until_h=NOW)
        cls.recon = reconcile(cls.ds.load_events, cls.ledger, cls.ds.orders,
                              cls.ds.materials)
        cls.forecasts = forecast_lots(cls.ds.materials, cls.ds.evidence,
                                      cls.model, NOW)
        cls.settlements = {st.order: st for st in settle(
            cls.ds, cls.ledger, cls.recon, cls.forecasts, NOW
        )}

    def test_supplement_within_deadline_cures(self):
        o1 = self.settlements["O1"]
        self.assertEqual(o1.decision, "补发")
        s3 = next(r for r in o1.routes if r.sailing == "S3")
        self.assertTrue(s3.feasible)
        self.assertTrue(s3.cures_late)  # 76h 发 ≤ 期限 77h
        self.assertEqual(o1.owed_sets, 13)

    def test_sunk_and_predicted_costs_not_netted(self):
        o1 = self.settlements["O1"]
        sunk = dict((cid, amount) for cid, _, amount in o1.sunk_costs)
        self.assertEqual(sunk, {"COST1": 240, "COST2": 180, "COST3": 75})
        predicted = dict((cid, amount) for cid, _, amount in o1.predicted_costs)
        self.assertEqual(predicted, {"COST4": 190})
        # 净成本 = 既成 495 + 预估 190,两者不相抵
        self.assertEqual(o1.net_if_supplement, 495 + 190)
        self.assertEqual(o1.net_if_not, 495 + 297 + 760)

    def test_split_order_gap_constraint_blocks_supplement(self):
        o2 = self.settlements["O2"]
        s3 = next(r for r in o2.routes if r.sailing == "S3")
        self.assertFalse(s3.feasible)
        self.assertTrue(any("间隔" in reason for reason in s3.reasons))
        self.assertEqual(o2.decision, "需人工")

    def test_no_route_means_manual(self):
        o3 = self.settlements["O3"]  # 南港只有 S4,47h 已过截装
        self.assertEqual(o3.routes, ())
        self.assertEqual(o3.decision, "需人工")
        self.assertIsNone(o3.net_if_supplement)


class RequoteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.model = DeskModel.fit(cls.ds.history)
        cls.ledger = Ledger.build(cls.ds.sailings, cls.ds.orders,
                                  cls.ds.carrier_events, until_h=NOW)
        cls.recon = reconcile(cls.ds.load_events, cls.ledger, cls.ds.orders,
                              cls.ds.materials)
        cls.forecasts = forecast_lots(cls.ds.materials, cls.ds.evidence,
                                      cls.model, NOW)
        cls.settlements = settle(cls.ds, cls.ledger, cls.recon, cls.forecasts, NOW)
        cls.props = proposals(cls.ds, cls.ledger, cls.recon, cls.forecasts,
                              cls.settlements, NOW)

    def test_o1_proposals_traceable(self):
        o1 = [p for p in self.props if p.order == "O1"]
        names = {p.name for p in o1}
        self.assertEqual(names, {"普通补发 S3", "加急补发 S3", "商议赔付"})
        normal = next(p for p in o1 if p.name == "普通补发 S3")
        self.assertEqual(normal.sets_covered, 30)
        self.assertEqual(normal.remaining_sets, 0)
        self.assertEqual(normal.last_feasible_sailing, "S3")
        self.assertEqual(normal.capacity_used, (("S3", 15),))
        self.assertEqual(normal.net_cost, 495 + 190)
        # 每份提议都能追到既成运输和赔付的回单
        for receipt in ("COST1", "COST2", "COST3", "CSR1", "CSR2", "CSR4"):
            self.assertIn(receipt, normal.receipts)
        expedited = next(p for p in o1 if p.name == "加急补发 S3")
        self.assertEqual(expedited.net_cost, 495 + 190 + 170)
        negotiate = next(p for p in o1 if p.name == "商议赔付")
        self.assertEqual(negotiate.remaining_sets, 13)
        self.assertEqual(negotiate.capacity_used, ())
        self.assertEqual(negotiate.net_cost, 495 + 760)

    def test_split_order_measured_in_pieces(self):
        o2 = [p for p in self.props if p.order == "O2"]
        self.assertTrue(o2)
        for prop in o2:
            self.assertEqual(prop.unit, "件")
        negotiate = next(p for p in o2 if p.name == "商议赔付")
        self.assertEqual(negotiate.remaining_sets, 24)
        self.assertEqual(negotiate.sets_covered, 18)


if __name__ == "__main__":
    unittest.main()
