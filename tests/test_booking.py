"""预订确认的合同约束与台账超卖提示。"""

import unittest

from shipping.ledger import Ledger
from shipping.plan import check_booking_constraints
from shipping.sources import CarrierEvent, load_dataset


class BookingConstraintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()

    def ledger(self, now):
        return Ledger.build(self.ds.sailings, self.ds.orders,
                            self.ds.carrier_events, until_h=now)

    def test_whole_set_must_cover_all_lots(self):
        problems = check_booking_constraints(
            self.ds, self.ledger(10), "O1", ("C11",), "S3"
        )
        self.assertTrue(any("整套" in p for p in problems))
        self.assertEqual(
            check_booking_constraints(self.ds, self.ledger(10), "O1",
                                      ("C11", "C12"), "S3"),
            [],
        )

    def test_split_batch_gap_against_existing_booking(self):
        # C22 已订 S2(发 56h),C21 再订 S3(发 76h)间隔 20h < 24h
        problems = check_booking_constraints(
            self.ds, self.ledger(12), "O2", ("C21",), "S3"
        )
        self.assertTrue(any("间隔" in p for p in problems))
        # S1(发 32h)间隔 24h 满足
        self.assertEqual(
            check_booking_constraints(self.ds, self.ledger(12), "O2", ("C21",), "S1"),
            [],
        )

    def test_same_sailing_violates_separate_sail(self):
        problems = check_booking_constraints(
            self.ds, self.ledger(12), "O2", ("C21",), "S2"
        )
        self.assertTrue(any("非同船" in p for p in problems))

    def test_destination_mismatch(self):
        problems = check_booking_constraints(
            self.ds, self.ledger(10), "O3", ("C31",), "S1"
        )
        self.assertTrue(any("目的港" in p for p in problems))

    def test_oversell_is_logged(self):
        events = [
            CarrierEvent("预订", "B1", "O1", ("C11", "C12"), "S1", 1, "接受"),
            CarrierEvent("预订", "B2", "O2", ("C21", "C22"), "S1", 2, "接受"),
        ]
        ledger = Ledger.build(self.ds.sailings, self.ds.orders, events)
        self.assertLess(ledger.remaining("S1"), 0)
        self.assertTrue(any("超卖" in line for line in ledger.log))


if __name__ == "__main__":
    unittest.main()
