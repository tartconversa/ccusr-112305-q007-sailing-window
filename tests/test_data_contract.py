"""校验可供分析的资料关系。"""

import unittest

from scripts.inspect_sources import read_table


class SourceContractTest(unittest.TestCase):
    def test_source_observations_and_references(self):
        history = read_table("history")
        self.assertGreaterEqual(len({row["week"] for row in history}), 10)
        self.assertTrue(any(row["actual"] is None for row in history))
        lots = {row["lot"] for row in read_table("orders")}
        events = read_table("carrier_events")
        self.assertTrue(all(set(row["lots"]) <= lots for row in events))
        self.assertEqual([row["state"] for row in events if row["booking"] == "B3"], ["待答复","拒绝"])
        original = next(row for row in events if row["booking"] == "B1")
        sailing = next(row for row in read_table("sailings") if row["sailing"] == original["sailing"])
        update = next(row for row in read_table("new_evidence") if row["lot"] == "C12")
        self.assertIn(update["lot"], original["lots"])
        self.assertLess(update["hour"], sailing["cancel_h"])
        self.assertGreater(update["new_ready"], sailing["cut_h"])
