"""舱位台账:预订接受才占舱,撤改未生效不放舱,已失效预订不能恢复。"""

import unittest

from shipping.ledger import Ledger
from shipping.sources import CarrierEvent, load_dataset


def _event(event, booking, order, lots, sailing, hour, state):
    return CarrierEvent(
        event=event, booking=booking, order=order, lots=tuple(lots),
        sailing=sailing, hour=hour, state=state,
    )


class LedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()

    def build(self, until=None):
        return Ledger.build(self.ds.sailings, self.ds.orders,
                            self.ds.carrier_events, until_h=until)

    def test_accepted_booking_occupies(self):
        ledger = self.build()
        self.assertEqual(ledger.remaining("S1"), 70 - 60)
        self.assertEqual(ledger.remaining("S2"), 68 - 18)
        self.assertEqual(ledger.remaining("S4"), 45 - 20)

    def test_rejected_modify_keeps_original(self):
        ledger = self.build()
        # B3 撤改被拒:B1 仍在 S1,S2 没被多占
        self.assertEqual(ledger.order_booking("O1").booking, "B1")
        self.assertEqual(ledger.order_booking("O1").sailing, "S1")
        self.assertIn("B3", ledger.dead)
        self.assertEqual(ledger.remaining("S2"), 68 - 18)

    def test_pending_does_not_occupy(self):
        ledger = self.build(until=20)  # B3 待答复(19h)已见,拒绝(21h)未见
        self.assertIn("B3", ledger.pending)
        self.assertEqual(ledger.remaining("S2"), 68 - 18)

    def test_dead_booking_cannot_be_restored(self):
        events = list(self.ds.carrier_events) + [
            _event("撤改", "B3", "O1", ["C11", "C12"], "S2", 25, "接受"),
        ]
        ledger = Ledger.build(self.ds.sailings, self.ds.orders, events)
        self.assertIn("B3", ledger.dead)
        self.assertNotIn("B3", ledger.active)
        self.assertEqual(ledger.remaining("S2"), 68 - 18)
        self.assertTrue(any("不能恢复" in line for line in ledger.log))

    def test_modify_accepted_swaps_atomically(self):
        events = [
            _event("预订", "X1", "O1", ["C11", "C12"], "S1", 5, "接受"),
            _event("撤改", "X2", "O1", ["C11", "C12"], "S3", 6, "接受"),
        ]
        ledger = Ledger.build(self.ds.sailings, self.ds.orders, events)
        self.assertNotIn("X1", ledger.active)
        self.assertEqual(ledger.order_booking("O1").sailing, "S3")
        self.assertEqual(ledger.remaining("S1"), 70)
        self.assertEqual(ledger.remaining("S3"), 66 - 60)

    def test_confirm_occupies_real_capacity(self):
        ledger = self.build()
        ledger.confirm("B9", "O2", ("C21",), "S2", 12)
        self.assertEqual(ledger.remaining("S2"), 68 - 18 - 24)

    def test_confirm_rejects_over_capacity_and_past_cut(self):
        ledger = self.build()
        with self.assertRaises(ValueError):
            ledger.confirm("B8", "O1", ("C11", "C12"), "S2", 12)  # 60 > 50
        with self.assertRaises(ValueError):
            ledger.confirm("B8", "O1", ("C11", "C12"), "S1", 99)  # 过了截装


if __name__ == "__main__":
    unittest.main()
