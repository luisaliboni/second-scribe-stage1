"""F2 item 6 (A1 minors): not-judged counts on the endorsed side, the not-judged flag over every spec of the
headline, and the stale primary constants."""
import math
import unittest
from dataclasses import replace

from second_scribe import metrics
from second_scribe.match import SETTINGS_GRID
from second_scribe.metrics import buckets, headline
from tests.test_f2_script_conditional import participant
from tests.test_metrics import FULL, S, ai_node, endorsed_map, pair, prov

VAR = "full__p1__t0__r0"


class TestEndorsedSideNotJudged(unittest.TestCase):
    def setup(self, *, extra_judged_match=False, located=True):
        e = endorsed_map()
        nodes = [ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished", p=prov(found=located))]
        if extra_judged_match:
            nodes.append(ai_node("a1b", e.nodes[1].text, "wished"))
        return replace(pair(nodes), not_judged={FULL: {"a1"}})

    def test_a_node_matched_only_by_a_not_judged_node_is_not_a_miss(self):
        b = buckets(self.setup(), FULL, "full", S)
        self.assertEqual((b["matched"], b["missed"], b["n_endorsed_not_judged"]), (1, 0, 1))
        self.assertEqual(b["node_recall"], 1.0)                      # the not-judged node leaves the denominator
        self.assertEqual(b["n_not_judged"], 1)                       # the AI-side count is unchanged

    def test_an_attested_match_still_wins(self):
        b = buckets(self.setup(extra_judged_match=True), FULL, "full", S)
        self.assertEqual((b["matched"], b["missed"], b["n_endorsed_not_judged"]), (2, 0, 0))

    def test_a_not_judged_node_that_could_never_be_attested_leaves_a_real_miss(self):
        # its quote is not located in the transcript: judged or not, it would not have counted
        b = buckets(self.setup(located=False), FULL, "full", S)
        self.assertEqual((b["matched"], b["missed"], b["n_endorsed_not_judged"]), (1, 1, 0))

    def test_no_not_judged_nodes_changes_nothing(self):
        e = endorsed_map()
        b = buckets(pair([ai_node("a0", e.nodes[0].text)]), FULL, "full", S)
        self.assertEqual((b["matched"], b["missed"], b["n_endorsed_not_judged"]), (1, 1, 0))

    def test_edges_with_a_not_judged_end_are_left_out_of_edge_recall(self):
        b = buckets(self.setup(), FULL, "full", S)
        self.assertEqual((b["n_edges_eligible"], b["edge_recall"] != b["edge_recall"]), (0, True))


class TestNotJudgedFlagOverAllSpecs(unittest.TestCase):
    def two_spec(self, ps, nj_var):
        out = []
        for p in ps:
            g = p.ai[FULL]
            out.append(replace(p, ai={**p.ai, VAR: g}, prov={**p.prov, VAR: p.prov[FULL]},
                               modality={**p.modality, VAR: p.modality[FULL]},
                               judged_ai_vs_endorsed={FULL: {}, VAR: {}}, judged_ai_vs_script={FULL: {}, VAR: {}},
                               not_judged={FULL: set(), VAR: set(nj_var)}))
        return out

    def run_h(self, ps):
        return headline(ps, FULL, [VAR], list(SETTINGS_GRID), B=30, n_perm=10, n_perm_script=10)

    def test_a_variation_spec_over_the_limit_blocks_the_interpretation(self):
        ps = self.two_spec([participant(i, 5, 5, leaks=(0, 1)) for i in range(6)], {"a0", "a1", "a2"})   # 3 of 10 nodes
        nj = self.run_h(ps)["not_judged"]
        self.assertEqual(nj["limit"], 0.10)
        self.assertEqual(nj["by_spec"][FULL]["share"], 0.0)
        self.assertAlmostEqual(nj["by_spec"][VAR]["share"], 0.3)
        self.assertEqual((nj["by_spec"][VAR]["n_elements"], nj["by_spec"][VAR]["n_not_judged"]), (60, 18))
        self.assertEqual(nj["interpretation"], "reported_not_interpreted")      # the primary spec alone is clean
        self.assertEqual(nj["by_spec"][FULL]["n_endorsed_not_judged"], 0)

    def test_all_specs_within_the_limit_is_interpreted(self):
        ps = self.two_spec([participant(i, 5, 5, leaks=(0, 1)) for i in range(6)], set())
        self.assertEqual(self.run_h(ps)["not_judged"]["interpretation"], "interpreted")

    def test_exactly_at_the_limit_is_interpreted(self):
        ps = self.two_spec([participant(i, 5, 5, leaks=(0, 1)) for i in range(6)], {"a0"})     # 1 of 10
        nj = self.run_h(ps)["not_judged"]
        self.assertAlmostEqual(nj["by_spec"][VAR]["share"], 0.10)
        self.assertEqual(nj["interpretation"], "interpreted")


class TestStaleConstants(unittest.TestCase):
    def test_primary_spec_is_the_removed_arm_and_full_spec_is_the_full_one(self):
        self.assertEqual(metrics.PRIMARY_SPEC, "removed__p0__t0__r0")
        self.assertEqual(metrics.FULL_SPEC, "full__p0__t0__r0")

    def test_defaults_follow_the_constants(self):
        import inspect
        sig = inspect.signature(metrics.leakage)
        self.assertEqual(sig.parameters["full_spec"].default, metrics.FULL_SPEC)
        self.assertEqual(inspect.signature(metrics.controls).parameters["spec_key"].default, metrics.PRIMARY_SPEC)


if __name__ == "__main__":
    unittest.main()
