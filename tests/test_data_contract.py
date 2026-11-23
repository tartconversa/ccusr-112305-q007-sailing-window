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

    def test_short_load_inputs(self):
        sources = [read_table(name) for name in ['load_events', 'customer_responses', 'carrier_events', 'dock_costs']]
        records=[x for x in sources[0] if x["box"]=="BX-2"]
        self.assertEqual([x["kind"] for x in records],["入柜","重发报送","码头实装"])
        self.assertEqual({x["state"] for x in sources[2] if x["booking"]=="B3"},{"待答复","拒绝"})
        self.assertTrue(any(x["kind"]=="要求补发" for x in sources[1]))
        self.assertEqual({x["actual"] for x in sources[3]},{False,True})

