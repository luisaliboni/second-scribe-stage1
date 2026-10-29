"""Ladder item 1: the three-way outcome (kept / leaked / missed) over ALL endorsed ideals, no conditioning on a match."""
import math
import unittest
from dataclasses import replace

from second_scribe.ladder_metrics import Item, MapDraw, draw_from_pair_inputs, three_way
from second_scribe.match import PRIMARY_SETTING
from second_scribe.schemas import MapGraph
from tests.test_f2_script_conditional import FULL, participant

S = PRIMARY_SETTING


def md(map_id, draw, states, *, retries=0, failed=False):
    """A MapDraw whose ideals have the given states (a string over k=kept, l=leaked, m=missed)."""
    name = {"k": "wished", "l": "experienced", "m": "missed", "u": "unattested"}
    items = tuple(Item(f"{map_id}-i{j}", "wished", name[c]) for j, c in enumerate(states))
    return MapDraw(map_id, draw, items, n_ai_nodes=len(states), retries=retries, parse_failed=failed)


class TestThreeWay(unittest.TestCase):
    def test_counts_add_up_and_rates_are_over_all_ideals(self):
        r = three_way([md("A", 0, "kkll"), md("B", 0, "klmm")])
        p = r["pooled"]
        self.assertEqual((p["n_ideals"], p["kept"], p["leaked"], p["missed"]), (8, 3, 3, 2))
        self.assertEqual(p["kept"] + p["leaked"] + p["missed"], p["n_ideals"])
        self.assertAlmostEqual(p["kept_rate"], 3 / 8)
        self.assertAlmostEqual(p["leaked_rate"], 3 / 8)
        self.assertAlmostEqual(p["missed_rate"], 2 / 8)
        by = {m["map_id"]: m for m in r["per_map"]}
        self.assertEqual((by["A"]["kept"], by["A"]["leaked"], by["A"]["missed"]), (2, 2, 0))
        self.assertAlmostEqual(by["B"]["missed_rate"], 0.5)

    def test_leak_is_not_conditioned_on_a_match(self):
        # dropping the leaky ideals moves them to "missed": the leak rate falls but kept does not rise
        before = three_way([md("A", 0, "kkllllll")])["pooled"]
        after = three_way([md("A", 0, "kkmmmmmm")])["pooled"]
        self.assertLess(after["leaked_rate"], before["leaked_rate"])
        self.assertEqual(after["kept"], before["kept"])
        self.assertAlmostEqual(after["missed_rate"], 6 / 8)
        # a leak rate conditional on a match would read 0/2 -> "perfect"; the three-way does not
        self.assertAlmostEqual(after["leaked_rate"], 0.0)

    def test_unattested_matches_are_neither_kept_nor_leaked_nor_missed(self):
        # an ideal matched only by AI nodes whose quote is not found in the transcript: reported as unattested
        r = three_way([md("A", 0, "kluum")])["pooled"]
        self.assertEqual((r["n_ideals"], r["kept"], r["leaked"], r["missed"], r["unattested"]), (5, 1, 1, 1, 2))
        self.assertAlmostEqual(r["unattested_rate"], 0.4)
        self.assertAlmostEqual(r["kept_rate"] + r["leaked_rate"] + r["missed_rate"] + r["unattested_rate"], 1.0)

    def test_only_wished_ideals_count_experienced_items_are_not_ideals(self):
        items = (Item("w", "wished", "wished"), Item("x1", "experienced", "wished"), Item("x2", "experienced", "missed"))
        r = three_way([MapDraw("A", 0, items)])["pooled"]
        self.assertEqual((r["n_ideals"], r["kept"], r["leaked"], r["missed"]), (1, 1, 0, 0))

    def test_map_without_ideals_has_nan_rates_and_is_listed(self):
        r = three_way([MapDraw("A", 0, (Item("x", "experienced", "experienced"),)), md("B", 0, "kl")])
        by = {m["map_id"]: m for m in r["per_map"]}
        self.assertTrue(math.isnan(by["A"]["leaked_rate"]))
        self.assertEqual(r["n_maps"], 2)
        self.assertEqual(r["n_maps_with_ideals"], 1)

    def test_draws_pool_within_map(self):
        r = three_way([md("A", 0, "kl"), md("A", 1, "ll")])
        a = r["per_map"][0]
        self.assertEqual((a["n_draws"], a["n_ideals"], a["leaked"]), (2, 4, 3))
        self.assertAlmostEqual(a["leaked_rate"], 0.75)
        self.assertEqual(r["n_cells"], 2)

    def test_parse_failure_counts_as_missed_and_is_reported_separately(self):
        ok = md("A", 0, "kk")
        bad = MapDraw.failed("A", 1, ok.items, retries=2)
        r = three_way([ok, bad])
        self.assertEqual((r["pooled"]["kept"], r["pooled"]["missed"], r["pooled"]["n_ideals"]), (2, 2, 4))
        self.assertEqual(r["parse_failures"], 1)
        self.assertEqual(r["retries"], 2)
        self.assertEqual(r["n_cells"], 2)                      # fixed denominator: the failed cell stays in
        self.assertTrue(all(i.state == "missed" for i in bad.items))

    def test_duplicate_map_draw_is_an_error(self):
        with self.assertRaises(ValueError):
            three_way([md("A", 0, "kk"), md("A", 0, "kk")])

    def test_bad_state_is_an_error(self):
        with self.assertRaises(ValueError):
            Item("i", "wished", "leaked")
        with self.assertRaises(ValueError):
            Item("i", "actual", "wished")

    def test_empty_input_is_an_error(self):
        with self.assertRaises(ValueError):
            three_way([])


class TestAdapter(unittest.TestCase):
    """draw_from_pair_inputs reads the three-way from the existing PairInputs / metrics.absence_category."""

    def test_kept_leaked_missed_from_pair_inputs(self):
        # 6 wished ideals: 0,1 leak (drawn experienced), 2 is missing from the AI map, 3-5 kept; 2 controls
        p = participant(1, 6, 2, leaks=(0, 1))
        g = p.ai[FULL]
        g2 = MapGraph(g.map_id, g.origin, [n for n in g.nodes if n.node_id != "a2"], [])
        p = replace(p, ai={FULL: g2})
        d = draw_from_pair_inputs(p, FULL, 0, S)
        by = {i.item_id: i for i in d.items}
        self.assertEqual([by[f"e{k}"].state for k in range(6)],
                         ["experienced", "experienced", "missed", "wished", "wished", "wished"])
        t = three_way([d])["pooled"]
        self.assertEqual((t["n_ideals"], t["kept"], t["leaked"], t["missed"]), (6, 3, 2, 1))
        self.assertEqual(len(d.items), 8)                      # the two controls are items too
        self.assertEqual(d.n_ai_nodes, 7)
        self.assertFalse(d.parse_failed)

    def test_unattested_wished_is_not_kept_and_not_missed_from_pair_inputs(self):
        from second_scribe.metrics import absences
        from second_scribe.provenance import Provenance
        p = participant(1, 5, 2, leaks=(0,))                   # ideal 0 leaks, ideals 1-4 are drawn wished
        pv = dict(p.prov[FULL])
        pv["a1"] = Provenance("a1", "node", False, None, None, 0, 0, False, False)      # quote not found: unlocated
        pv["a0"] = Provenance("a0", "node", False, None, None, 0, 0, False, False)
        p = replace(p, prov={FULL: pv})
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["unanchored_wished"], a["leaked_unanchored"]), (1, 1))
        t = three_way([draw_from_pair_inputs(p, FULL, 0, S)])["pooled"]
        self.assertEqual((t["kept"], t["leaked"], t["missed"], t["unattested"]),
                         (a["recovered_as_wished"], a["leaked_as_existing"], a["missed"], 2))
        self.assertEqual(t["kept"], 3)

    def test_none_ai_map_is_a_parse_failure_all_missed(self):
        p = participant(1, 4, 1)
        p = replace(p, ai={FULL: None})
        d = draw_from_pair_inputs(p, FULL, 0, S, retries=3)
        self.assertTrue(d.parse_failed)
        self.assertEqual(d.retries, 3)
        self.assertEqual(three_way([d])["pooled"]["missed"], 4)
        self.assertEqual(d.n_ai_nodes, 0)

    def test_agrees_with_metrics_absences(self):
        from second_scribe.metrics import absences
        p = participant(2, 6, 3, leaks=(1, 4))
        a = absences(p, FULL, "full", S)
        t = three_way([draw_from_pair_inputs(p, FULL, 0, S)])["pooled"]
        self.assertEqual(t["kept"], a["recovered_as_wished"])
        self.assertEqual(t["leaked"], a["leaked_as_existing"])
        self.assertEqual(t["missed"], a["missed"])


if __name__ == "__main__":
    unittest.main()
