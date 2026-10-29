"""Ladder item 2: mirror metrics per rung (false-wished rate, actual recall, AI node count, flip rate),
fixed denominator, parse failures and retries."""
import json
import math
import unittest
from dataclasses import replace

from second_scribe.ladder_metrics import (Item, MapDraw, draw_from_pair_inputs, metric_values, rung_summary)
from second_scribe.match import PRIMARY_SETTING
from second_scribe.metrics import contradiction_rate
from tests.test_f2_script_conditional import FULL, participant

S = PRIMARY_SETTING


def items(spec):
    """spec: list of (id, endorsed_status, state)."""
    return tuple(Item(i, e, s) for i, e, s in spec)


def draw(mid, k, spec, **kw):
    return MapDraw(mid, k, items(spec), **kw)


class TestPerDraw(unittest.TestCase):
    def test_false_wished_rate_is_marked_over_actual_quote_nodes(self):
        d = draw("A", 0, [("w", "wished", "wished")], n_actual_quote_nodes=4, n_actual_quote_marked_wished=1)
        self.assertAlmostEqual(metric_values(d)["false_wished_rate"], 0.25)

    def test_false_wished_is_nan_without_actual_quote_nodes(self):
        d = draw("A", 0, [("w", "wished", "wished")])
        self.assertTrue(math.isnan(metric_values(d)["false_wished_rate"]))

    def test_actual_recall_over_endorsed_experienced_items(self):
        d = draw("A", 0, [("a", "experienced", "experienced"), ("b", "experienced", "wished"),
                          ("c", "experienced", "missed"), ("w", "wished", "missed")])
        self.assertAlmostEqual(metric_values(d)["actual_recall"], 2 / 3)

    def test_unattested_experienced_item_is_not_recalled(self):
        d = draw("A", 0, [("a", "experienced", "experienced"), ("b", "experienced", "unattested"),
                          ("c", "experienced", "missed")])
        self.assertAlmostEqual(metric_values(d)["actual_recall"], 1 / 3)

    def test_unattested_rate_over_ideals(self):
        d = draw("A", 0, [("w", "wished", "unattested"), ("v", "wished", "wished")])
        self.assertAlmostEqual(metric_values(d)["unattested_rate"], 0.5)

    def test_actual_recall_nan_without_experienced_items(self):
        d = draw("A", 0, [("w", "wished", "wished")])
        self.assertTrue(math.isnan(metric_values(d)["actual_recall"]))

    def test_node_count(self):
        d = draw("A", 0, [("w", "wished", "wished")], n_ai_nodes=17)
        self.assertEqual(metric_values(d)["ai_node_count"], 17)

    def test_flip_rate_is_flipped_over_control_nodes(self):
        d = draw("A", 0, [("a", "experienced", "wished")], n_control=8, n_flipped=2)
        self.assertAlmostEqual(metric_values(d)["flip_rate"], 0.25)
        self.assertTrue(math.isnan(metric_values(draw("A", 0, [("a", "experienced", "wished")]))["flip_rate"]))

    def test_disagreement_is_share_of_items_that_differ_from_the_reference_draw(self):
        ref = draw("A", 0, [("a", "experienced", "experienced"), ("b", "experienced", "experienced"),
                            ("w", "wished", "wished"), ("v", "wished", "wished")])
        d = draw("A", 0, [("a", "experienced", "experienced"), ("b", "experienced", "wished"),
                          ("w", "wished", "experienced"), ("v", "wished", "wished")])
        v = metric_values(d, ref)
        self.assertAlmostEqual(v["disagreement_vs_rung2"], 0.5)
        self.assertAlmostEqual(v["disagreement_vs_rung2_experienced"], 0.5)        # b of {a, b}
        self.assertEqual(metric_values(ref, ref)["disagreement_vs_rung2"], 0.0)

    def test_disagreement_without_reference_is_nan(self):
        d = draw("A", 0, [("a", "experienced", "experienced")])
        self.assertTrue(math.isnan(metric_values(d)["disagreement_vs_rung2"]))

    def test_disagreement_with_different_item_sets_is_an_error(self):
        a = draw("A", 0, [("a", "experienced", "experienced")])
        b = draw("A", 0, [("z", "experienced", "experienced")])
        with self.assertRaises(ValueError):
            metric_values(a, b)

    def test_failed_draw_has_zero_recall_zero_nodes_and_no_false_wished(self):
        ok = draw("A", 0, [("a", "experienced", "experienced"), ("w", "wished", "wished")], n_ai_nodes=5)
        bad = MapDraw.failed("A", 1, ok.items, retries=2)
        v = metric_values(bad)
        self.assertEqual((v["actual_recall"], v["ai_node_count"], v["leaked_rate"], v["missed_rate"]), (0.0, 0, 0.0, 1.0))
        self.assertTrue(math.isnan(v["false_wished_rate"]))
        self.assertEqual(v["flip_rate"], 0.0)                      # nothing was drawn, nothing flipped; control stays in the denominator
        self.assertEqual(bad.n_control, 1)


class TestRungSummary(unittest.TestCase):
    def setUp(self):
        self.a0 = draw("A", 0, [("a", "experienced", "experienced"), ("b", "experienced", "wished"),
                                ("w", "wished", "wished"), ("v", "wished", "experienced")],
                       n_ai_nodes=6, n_actual_quote_nodes=4, n_actual_quote_marked_wished=1)
        self.a1 = draw("A", 1, [("a", "experienced", "experienced"), ("b", "experienced", "experienced"),
                                ("w", "wished", "wished"), ("v", "wished", "wished")],
                       n_ai_nodes=8, n_actual_quote_nodes=2, n_actual_quote_marked_wished=0)
        self.b0 = draw("B", 0, [("a", "experienced", "missed"), ("w", "wished", "missed")], n_ai_nodes=0)
        self.draws = [self.a0, self.a1, self.b0]

    def test_map_values_average_draws_within_map_first(self):
        s = rung_summary(self.draws)
        fw = s["mirror"]["false_wished_rate"]
        self.assertAlmostEqual(fw["per_map"]["A"], (0.25 + 0.0) / 2)
        self.assertIsNone(fw["per_map"]["B"])                      # no actual-quote node: excluded, not zero
        self.assertEqual(fw["n_maps_used"], 1)
        self.assertAlmostEqual(fw["pooled"], 1 / 6)                # pooled over nodes
        self.assertAlmostEqual(s["mirror"]["ai_node_count"]["per_map"]["A"], 7.0)
        self.assertAlmostEqual(s["mirror"]["actual_recall"]["per_map"]["A"], (1.0 + 1.0) / 2)
        self.assertAlmostEqual(s["mirror"]["actual_recall"]["per_map"]["B"], 0.0)

    def test_fixed_denominator_and_parse_failure_reporting(self):
        bad = MapDraw.failed("B", 1, self.b0.items, retries=3)
        s = rung_summary(self.draws + [bad])
        self.assertEqual((s["n_cells"], s["parse_failures"], s["retries"]), (4, 1, 3))
        self.assertAlmostEqual(s["parse_failure_rate"], 0.25)
        self.assertEqual(s["three_way"]["n_cells"], 4)
        self.assertEqual(s["mirror"]["ai_node_count"]["pooled"], (6 + 8 + 0 + 0) / 4)

    def test_flip_against_rung_two_reference(self):
        ref = [replace(d) for d in self.draws]
        s = rung_summary(self.draws, reference=ref)
        self.assertEqual(s["descriptive"]["disagreement_vs_rung2"]["pooled"], 0.0)
        changed = [replace(self.a0, items=tuple(replace(i, state="missed") if i.item_id == "w" else i for i in self.a0.items)),
                   self.a1, self.b0]
        s2 = rung_summary(changed, reference=ref)
        self.assertAlmostEqual(s2["descriptive"]["disagreement_vs_rung2"]["per_map"]["A"], (0.25 + 0.0) / 2)
        self.assertEqual(s2["disagreement_reference_draws_missing"], 0)

    def test_reference_draw_missing_counts_and_nan(self):
        ref = [self.a0, self.b0]                                   # a1 has no reference draw
        s = rung_summary(self.draws, reference=ref)
        self.assertEqual(s["disagreement_reference_draws_missing"], 1)
        self.assertEqual(s["descriptive"]["disagreement_vs_rung2"]["per_map"]["A"], 0.0)   # only draw 0 is comparable

    def test_summary_json_safe(self):
        s = rung_summary(self.draws, reference=self.draws)
        json.dumps(s, allow_nan=False)


class TestAdapterMirror(unittest.TestCase):
    def test_actual_quote_counts_reuse_contradiction_rate(self):
        p = participant(3, 4, 4, flips=(0, 1))                     # controls 0,1 drawn wished
        pv = p.modality[FULL]
        # the judge reads the quote of one flipped control as "actual" (a false-wished node), the other as "wished"
        md = dict(pv)
        md["a4"] = "actual"
        p = replace(p, modality={FULL: md})
        d = draw_from_pair_inputs(p, FULL, 0, S)
        direct = contradiction_rate([p], FULL, B=5)["separate_samples"]["quote_actual_marked_wished"]
        self.assertEqual((d.n_actual_quote_nodes, d.n_actual_quote_marked_wished),
                         (direct["n_nodes"], direct["n_contradicting"]))
        self.assertEqual((d.n_actual_quote_nodes, d.n_actual_quote_marked_wished), (3, 1))   # a5,a6,a7 + a4 -> actual
        self.assertAlmostEqual(metric_values(d)["false_wished_rate"], 1 / 3)

    def test_flip_rate_equals_metrics_absences_flip(self):
        from second_scribe.metrics import absences
        p = participant(4, 4, 6, flips=(0, 1))                     # controls 0,1 drawn wished
        a = absences(p, FULL, "full", S)
        d = draw_from_pair_inputs(p, FULL, 0, S)
        self.assertEqual((d.n_control, d.n_flipped), (a["n_control"], a["flipped"]))
        self.assertEqual((d.n_control, d.n_flipped), (6, 2))
        self.assertAlmostEqual(metric_values(d)["flip_rate"], a["flip_rate"])

    def test_failed_adapter_draw_has_no_quote_counts(self):
        p = replace(participant(3, 2, 2), ai={FULL: None})
        d = draw_from_pair_inputs(p, FULL, 0, S)
        self.assertEqual((d.n_actual_quote_nodes, d.n_actual_quote_marked_wished), (0, 0))


if __name__ == "__main__":
    unittest.main()
