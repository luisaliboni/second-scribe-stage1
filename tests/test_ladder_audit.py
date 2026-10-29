"""Ladder item 4: the copy auditor (6-gram overlap between demo wording and a model's map)."""
import unittest

from second_scribe.ladder import CopyFlag, audit_map, audit_outputs, audit_rungs
from second_scribe.schemas import Edge, MapGraph, Node
from second_scribe.synth.demos import PLACEBO_DEMOS, REMEDY_DEMOS, demo_label_tuples, demo_ngrams

GRAMS = demo_ngrams()
# a stretch of demo wording (the first remedy wished quote) and a plainly different sentence
DEMO_QUOTE = REMEDY_DEMOS[0].fragment["nodes"][1]["quote"]
CLEAN_QUOTE = "We waited six weeks for the licence and nobody told us why it took so long."


def graph(map_id, *, label="Business licence", quote=CLEAN_QUOTE, edge_quote=CLEAN_QUOTE):
    return MapGraph(map_id=map_id, origin="ai",
                    nodes=[Node("n1", label, "service", None, "experienced", quote),
                           Node("n2", "City hall counter", "actor", None, "experienced", CLEAN_QUOTE)],
                    edges=[Edge("n1", "n2", "experienced", True, edge_quote)])


class TestAuditMap(unittest.TestCase):
    def test_clean_map_has_no_flags(self):
        self.assertEqual(audit_map(graph("M1"), GRAMS), [])

    def test_a_copied_quote_is_flagged_with_item_and_field(self):
        flags = audit_map(graph("M1", quote=DEMO_QUOTE), GRAMS)
        self.assertTrue(flags)
        self.assertTrue(all(isinstance(f, CopyFlag) for f in flags))
        self.assertEqual({(f.map_id, f.item_id, f.field) for f in flags}, {("M1", "n1", "quote")})
        self.assertTrue(all(len(f.ngram) == 6 for f in flags))
        self.assertTrue(all(f.ngram in GRAMS for f in flags))

    def test_edge_quote_and_node_label_are_checked_too(self):
        flags = audit_map(graph("M1", edge_quote=DEMO_QUOTE), GRAMS)
        self.assertEqual({(f.item_id, f.field) for f in flags}, {("n1->n2", "quote")})
        # a label that holds six demo tokens in a row
        label = " ".join(DEMO_QUOTE.split()[:8])      # demo wording, 8 tokens
        flags = audit_map(graph("M1", label=label), GRAMS)
        self.assertEqual({(f.item_id, f.field) for f in flags}, {("n1", "label")})

    def test_matching_is_normalised_and_five_tokens_are_not_enough(self):
        words = DEMO_QUOTE.split()
        shouty = " ".join(words[:3]).upper() + " — " + ", ".join(words[3:8]) + "!"
        self.assertTrue(audit_map(graph("M1", quote=shouty), GRAMS))
        five = " ".join(words[:5])
        self.assertEqual(audit_map(graph("M1", quote=five), GRAMS), [])
        self.assertEqual(audit_map(graph("M1", quote=None), GRAMS), [])    # no quote, nothing to scan

    def test_gram_size_must_match_the_set(self):
        with self.assertRaises(ValueError):
            audit_map(graph("M1"), demo_ngrams(n=4))
        self.assertTrue(audit_map(graph("M1", quote=DEMO_QUOTE), demo_ngrams(n=4), n=4))


class TestScanBoundaries(unittest.TestCase):
    """Review M1: the last 6-gram of a text and a text of exactly six tokens must be checked."""

    def test_the_last_six_tokens_of_a_quote_are_checked(self):
        words = DEMO_QUOTE.split()
        tail = " ".join(words[-6:])
        self.assertTrue(audit_map(graph("M1", quote="Nothing like it before " + tail), GRAMS))
        self.assertTrue(audit_map(graph("M1", quote=tail), GRAMS))               # exactly six tokens

    def test_punctuation_inside_the_gram_does_not_hide_it(self):
        words = DEMO_QUOTE.split()
        punctuated = ", ".join(words[2:8]) + "."
        self.assertTrue(audit_map(graph("M1", quote="Well: " + punctuated), GRAMS))
        curly = "“" + " ".join(words[2:8]) + "”"
        self.assertTrue(audit_map(graph("M1", quote=curly), GRAMS))

    def test_a_gram_split_across_label_and_quote_is_not_a_copy(self):
        words = DEMO_QUOTE.split()
        g = graph("M1", label=" ".join(words[:3]), quote=" ".join(words[3:6]))
        self.assertEqual(audit_map(g, GRAMS, labels=frozenset()), [])


class TestAuditOutputs(unittest.TestCase):
    def test_summary_counts_outputs_flagged_and_ids(self):
        outs = {"M1": graph("M1"), "M2": graph("M2", quote=DEMO_QUOTE), "M3": graph("M3"), "M4": None,
                "M5": graph("M5", edge_quote=DEMO_QUOTE)}
        s = audit_outputs(outs, GRAMS)
        self.assertEqual(s["n_outputs"], 4)            # M4 failed to parse: counted apart, not as clean
        self.assertEqual(s["n_missing"], 1)
        self.assertEqual(s["n_flagged"], 2)
        self.assertEqual(s["flagged_ids"], ["M2", "M5"])
        self.assertEqual(s["n"], 6)
        self.assertTrue(all(f["map_id"] in ("M2", "M5") for f in s["flags"]))

    def test_empty_input(self):
        s = audit_outputs({}, GRAMS)
        self.assertEqual((s["n_outputs"], s["n_flagged"], s["flagged_ids"], s["flags"]), (0, 0, [], []))

    def test_per_rung_summary_keeps_rungs_apart_and_default_grams_are_the_demo_set(self):
        by_rung = {"rung2": {"M1": graph("M1")}, "rung3": {"M1": graph("M1", quote=DEMO_QUOTE), "M2": graph("M2")}}
        s = audit_rungs(by_rung)
        self.assertEqual(set(s), {"rung2", "rung3"})
        self.assertEqual((s["rung2"]["n_outputs"], s["rung2"]["n_flagged"], s["rung2"]["flagged_ids"]), (1, 0, []))
        self.assertEqual((s["rung3"]["n_outputs"], s["rung3"]["n_flagged"], s["rung3"]["flagged_ids"]),
                         (2, 1, ["M1"]))

    def test_summary_is_json_serialisable(self):
        import json
        json.dumps(audit_rungs({"r": {"M1": graph("M1", quote=DEMO_QUOTE)}}))


class TestSourceFilter(unittest.TestCase):
    """Review I1: a demo 6-gram that is also in the output's own transcript is not a copy."""

    def test_gram_also_in_the_source_is_not_flagged_and_is_counted_apart(self):
        interviewer = REMEDY_DEMOS[0].excerpt.split("\n")[2][4:]       # "Did they give you anything to drive ..."
        g = graph("M1", quote=interviewer)
        self.assertTrue(audit_map(g, GRAMS))                            # flagged with no source
        self.assertEqual(audit_map(g, GRAMS, source_text=f"[I] {interviewer}\n[P] Yes."), [])
        s = audit_outputs({"M1": (g, f"[I] {interviewer}\n[P] Yes.")}, GRAMS)
        self.assertEqual((s["n_flagged"], s["n_outputs"]), (0, 1))
        self.assertGreater(s["n_in_source"], 0)

    def test_a_gram_that_only_spans_two_source_turns_is_still_flagged(self):
        words = DEMO_QUOTE.split()
        half = len(words) // 2
        source = " ".join(words[:half]) + "\n" + " ".join(words[half:])
        self.assertTrue(audit_map(graph("M1", quote=DEMO_QUOTE), GRAMS, source_text=source))

    def test_unrelated_source_changes_nothing(self):
        self.assertTrue(audit_map(graph("M1", quote=DEMO_QUOTE), GRAMS, source_text=CLEAN_QUOTE))
        s = audit_outputs({"M1": (graph("M1", quote=DEMO_QUOTE), CLEAN_QUOTE)}, GRAMS)
        self.assertEqual((s["n_flagged"], s["n_in_source"]), (1, 0))


class TestLabelEquality(unittest.TestCase):
    """Review M2: demo labels are 2-5 tokens, so the 6-gram scan can never see them; compare whole labels."""

    def test_a_node_label_equal_to_a_demo_label_is_flagged(self):
        demo_label = REMEDY_DEMOS[0].fragment["nodes"][1]["label"]
        flags = audit_map(graph("M1", label=demo_label.upper() + "!"), GRAMS)
        self.assertEqual([(f.item_id, f.field, f.kind) for f in flags], [("n1", "label", "label_equal")])
        self.assertEqual(flags[0].ngram, tuple(demo_label.lower().split()))

    def test_both_arms_labels_are_covered_and_other_labels_are_not(self):
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            for n in d.fragment["nodes"]:
                self.assertTrue(audit_map(graph("M1", label=n["label"]), GRAMS), n["label"])
        self.assertEqual(audit_map(graph("M1", label="Business licence"), GRAMS), [])
        self.assertEqual(audit_map(graph("M1", label="Loaner car"), GRAMS), [])          # a part is not equal

    def test_label_check_can_be_switched_off_and_is_in_the_summary(self):
        demo_label = REMEDY_DEMOS[0].fragment["nodes"][1]["label"]
        g = graph("M1", label=demo_label)
        self.assertEqual(audit_map(g, GRAMS, labels=frozenset()), [])
        s = audit_outputs({"M1": g}, GRAMS)
        self.assertEqual((s["n_flagged"], s["flags"][0]["kind"]), (1, "label_equal"))
        from second_scribe.textnorm import tokens
        every = {tuple(tokens(n["label"])) for d in REMEDY_DEMOS + PLACEBO_DEMOS for n in d.fragment["nodes"]}
        self.assertEqual(demo_label_tuples(), frozenset(every))
        self.assertEqual(len(demo_label_tuples(arms=("remedy",))), 6)
        self.assertTrue(all(isinstance(t, tuple) and 1 <= len(t) <= 8 for t in every))
        self.assertGreaterEqual(sum(len(t) < 6 for t in every), 8)      # why the 6-gram scan mostly misses them


if __name__ == "__main__":
    unittest.main()
