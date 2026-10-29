"""F2 item 3: the leak split by the AI's own quote. Only a leaked ideal whose quote contradicts the drawn
status is "counterfactual_leakage"; one whose quote says it happened is "adequacy_flattening"."""
import unittest

from second_scribe.metrics import ABSENCE_COUNTS, absences
from tests.test_metrics import FULL, S, ai_node, endorsed_map, pair


def leak_of(*modalities, nj=()):
    """One endorsed wished ideal (e1) matched only by experienced-drawn located AI nodes with these quote modalities."""
    e = endorsed_map()
    nodes = [ai_node("a0", e.nodes[0].text)]
    nodes += [ai_node(f"b{k}", e.nodes[1].text, "experienced", modality=m) for k, m in enumerate(modalities)]
    p = pair(nodes)
    if nj:
        from dataclasses import replace
        p = replace(p, not_judged={FULL: set(nj)})
    return absences(p, FULL, "full", S)


class TestLeakSplit(unittest.TestCase):
    def test_quote_contradicting_the_drawn_status_is_counterfactual_leakage(self):
        for mod in ("wished", "negated", "hypothetical", "other_person"):
            a = leak_of(mod)
            self.assertEqual((a["counterfactual_leakage"], a["adequacy_flattening"], a["leak_split_indeterminate"]),
                             (1, 0, 0), mod)
            self.assertEqual(a["counterfactual_leakage_rate"], 1.0, mod)
            self.assertEqual(a["adequacy_flattening_rate"], 0.0, mod)

    def test_quote_saying_it_happened_is_adequacy_flattening(self):
        a = leak_of("actual")
        self.assertEqual((a["counterfactual_leakage"], a["adequacy_flattening"], a["leak_split_indeterminate"]),
                         (0, 1, 0))
        self.assertEqual((a["adequacy_flattening_rate"], a["counterfactual_leakage_rate"]), (1.0, 0.0))

    def test_unsupported_or_unjudged_quote_names_neither(self):
        self.assertEqual(leak_of("unsupported")["leak_split_indeterminate"], 1)
        a = leak_of("actual", nj=("b0",))             # the only matching quote was never judged
        self.assertEqual((a["counterfactual_leakage"], a["adequacy_flattening"], a["leak_split_indeterminate"]),
                         (0, 0, 1))

    def test_a_quote_saying_it_happened_keeps_the_strong_name_out(self):
        # the ideal is matched by a wished-quoted node and by an actual-quoted node: the transcript says it
        # happened somewhere, so the strong name is not claimed (conservative; documented in absences())
        a = leak_of("wished", "actual")
        self.assertEqual((a["counterfactual_leakage"], a["adequacy_flattening"]), (0, 1))

    def test_split_adds_up_to_the_leaks_and_the_old_sub_split_is_kept(self):
        for mods in (("wished",), ("actual",), ("unsupported",), ("negated", "actual")):
            a = leak_of(*mods)
            self.assertEqual(a["counterfactual_leakage"] + a["adequacy_flattening"] + a["leak_split_indeterminate"],
                             a["leaked_as_existing"], mods)
        a = leak_of("negated")
        self.assertEqual(a["leaked_quote_other"], 1)

    def test_not_a_leak_is_not_split(self):
        e = endorsed_map()
        a = absences(pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished")]), FULL, "full", S)
        self.assertEqual((a["counterfactual_leakage"], a["adequacy_flattening"], a["leak_split_indeterminate"]),
                         (0, 0, 0))

    def test_counts_are_listed_for_the_pooled_sums(self):
        for k in ("counterfactual_leakage", "adequacy_flattening", "leak_split_indeterminate"):
            self.assertIn(k, ABSENCE_COUNTS)


if __name__ == "__main__":
    unittest.main()
