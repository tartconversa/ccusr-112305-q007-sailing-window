"""接货核实:码头实装为事实,重复抄送作废,半套余量不可转单。"""

import unittest

from shipping.ledger import Ledger
from shipping.reconcile import reconcile
from shipping.sources import LoadEvent, load_dataset


class ReconcileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.ledger = Ledger.build(cls.ds.sailings, cls.ds.orders, cls.ds.carrier_events)
        cls.recon = reconcile(
            cls.ds.load_events, cls.ledger, cls.ds.orders, cls.ds.materials
        )

    def test_terminal_load_is_the_fact(self):
        bx2 = next(b for b in self.recon.boxes if b.box == "BX-2")
        self.assertTrue(bx2.departed)
        self.assertEqual(bx2.sailing, "S1")  # 认 S1,不认已失效 B3 抄送的 S2
        self.assertEqual(bx2.departed_pieces, 17)
        self.assertEqual(bx2.short_load, 23 - 17)

    def test_resent_copy_of_dead_booking_is_void(self):
        bx2 = next(b for b in self.recon.boxes if b.box == "BX-2")
        self.assertEqual(bx2.void_reports, ("K3",))
        self.assertTrue(any("作废" in w for w in self.recon.warnings))

    def test_whole_set_accounting(self):
        o1 = self.recon.order("O1")
        self.assertEqual(o1.granularity, "整套")
        self.assertEqual(o1.sets_target, 30)
        self.assertEqual(o1.departed_sets, 17)  # 已离港可成套
        lots = {lot.lot: lot for lot in o1.lots}
        self.assertEqual(lots["C11"].short, 2)  # 仍可补发的欠件
        self.assertEqual(lots["C12"].short, 13)
        # 多余半套不能当成可满足另一个客户的整套
        self.assertEqual(lots["C11"].surplus, 11)
        self.assertEqual(lots["C12"].surplus, 0)

    def test_splittable_order_measured_in_pieces(self):
        o2 = self.recon.order("O2")
        self.assertEqual(o2.granularity, "可拆")
        self.assertIsNone(o2.departed_sets)
        lots = {lot.lot: lot for lot in o2.lots}
        self.assertEqual(lots["C22"].shipped, 18)
        self.assertEqual(lots["C21"].short, 24)

    def test_stuffed_without_terminal_load_is_not_departed(self):
        events = [
            LoadEvent("K9", "BX-9", "C31", "B4", "S4", "入柜", 30, 20, None),
        ]
        recon = reconcile(events, self.ledger, self.ds.orders)
        bx9 = recon.boxes[0]
        self.assertFalse(bx9.departed)
        self.assertEqual(recon.order("O3").departed_sets, 0)

    def test_conflicting_terminal_records_flagged(self):
        events = [
            LoadEvent("K10", "BX-8", "C31", "B4", "S4", "码头实装", 40, 20, None),
            LoadEvent("K11", "BX-8", "C31", "B4", "S3", "码头实装", 60, 20, None),
        ]
        recon = reconcile(events, self.ledger, self.ds.orders)
        self.assertTrue(recon.boxes[0].conflicts)

    def test_factory_complete_but_terminal_short_warns(self):
        self.assertTrue(any("工厂报完工" in w for w in self.recon.warnings))


if __name__ == "__main__":
    unittest.main()
