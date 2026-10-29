"""F2 item 5: homogenization and linearization rows are marked ``symptom_of_script`` only when the AI-minus-endorsed
difference and the prior-only-minus-endorsed difference have the same sign and neither is zero."""
import unittest
from dataclasses import replace

from second_scribe.match import PRIMARY_SETTING, SETTINGS_GRID
from second_scribe.metrics import PairInputs, headline, same_sign
from second_scribe.provenance import Provenance
from second_scribe.schemas import Edge, MapGraph, Node

FULL = "full__p0__t0__r0"
VAR = "full__p1__t0__r0"
PRIOR = "prior_only__p0__t0.7__r"


def prov():
    return Provenance("x", "node", True, "participant", 1, 1, 12, False, False)


def chain(map_id, origin, labels, tag):
    nodes = [Node(f"{tag}{k}", t) for k, t in enumerate(labels)]
    return MapGraph(map_id, origin, nodes, [Edge(a.node_id, b.node_id) for a, b in zip(nodes, nodes[1:])])


def uniq(i, n, tag):
    return [f"zq{tag}{i:03d}x{k:02d} zr{tag}{i:03d}x{k:02d} zs{tag}{i:03d}x{k:02d}" for k in range(n)]


def person(i, n_end, n_ai, prior_sizes, *, ai_labels=None, end_labels=None, prior_labels=None):
    """Endorsed map with ``n_end`` nodes, AI map with ``n_ai`` nodes (both specs), and one prior-only sample per entry
    of ``prior_sizes`` (the whole role's samples sit on every participant, as in the pipeline)."""
    e = chain(f"E{i}", "endorsed", end_labels(i) if end_labels else uniq(i, n_end, "e"), "e")
    a = chain(f"A{i}", "ai", ai_labels(i) if ai_labels else uniq(i, n_ai, "a"), "a")
    ai = {FULL: a, VAR: a}
    for s, n in enumerate(prior_sizes):
        ai[f"{PRIOR}{s}"] = chain(f"PR{s}", "prior_only", prior_labels(s) if prior_labels else uniq(100 + s, n, "p"), "p")
    pv = {sp: {n.node_id: prov() for n in a.nodes} for sp in (FULL, VAR)}
    md = {sp: {n.node_id: "actual" for n in a.nodes} for sp in (FULL, VAR)}
    return PairInputs(
        pid=f"P{i}", role="r1", interviewer="I1" if i % 2 else "I2", endorsed=e, ai=ai, prov=pv, modality=md,
        in_transcript={n.node_id: True for n in e.nodes}, judged_ai_vs_endorsed={FULL: {}, VAR: {}},
        judged_prior_vs_endorsed={}, judged_ai_vs_script={FULL: {}, VAR: {}}, judged_endorsed_vs_script={})


def run(ps):
    return headline(ps, FULL, [VAR], list(SETTINGS_GRID), B=40, n_perm=10, n_perm_script=20)


class TestSameSign(unittest.TestCase):
    def test_rule(self):
        self.assertTrue(same_sign(2.0, 1.0))
        self.assertTrue(same_sign(-0.5, -3.0))
        self.assertFalse(same_sign(2.0, -1.0))
        self.assertFalse(same_sign(-2.0, 1.0))
        self.assertFalse(same_sign(0.0, 1.0))                      # a zero is no sign
        self.assertFalse(same_sign(1.0, 0.0))
        self.assertFalse(same_sign(0.0, 0.0))
        self.assertFalse(same_sign(float("nan"), 1.0))
        self.assertFalse(same_sign(1.0, float("nan")))
        self.assertFalse(same_sign(None, 1.0))


class TestLinearizationSymptom(unittest.TestCase):
    def measure(self, n_end, n_ai, n_prior, key="n_nodes"):
        ps = [person(i, n_end, n_ai, [n_prior]) for i in range(8)]
        return run(ps)["linearization"]["glued"]["measures"][key]

    def test_same_sign_both_ways_is_a_symptom(self):
        up = self.measure(4, 6, 6)                 # AI and prior both longer than the endorsed map
        self.assertEqual((up["mean_diff"], up["prior_mean_diff"], up["symptom_of_script"]), (2.0, 2.0, True))
        down = self.measure(4, 3, 2)               # both shorter
        self.assertEqual((down["mean_diff"], down["prior_mean_diff"], down["symptom_of_script"]), (-1.0, -2.0, True))

    def test_opposite_signs_are_not_a_symptom(self):
        m = self.measure(4, 6, 3)                  # AI longer, prior shorter: the AI's own doing, not the script's
        self.assertEqual((m["mean_diff"], m["prior_mean_diff"], m["symptom_of_script"]), (2.0, -1.0, False))
        m = self.measure(4, 3, 6)
        self.assertEqual((m["mean_diff"], m["prior_mean_diff"], m["symptom_of_script"]), (-1.0, 2.0, False))

    def test_a_zero_difference_on_either_side_is_not_a_symptom(self):
        self.assertFalse(self.measure(4, 6, 4)["symptom_of_script"])       # prior equals the endorsed size
        self.assertFalse(self.measure(4, 4, 6)["symptom_of_script"])       # AI equals the endorsed size
        self.assertFalse(self.measure(4, 4, 4)["symptom_of_script"])

    def test_no_prior_only_map_gives_no_symptom_not_an_error(self):
        ps = [person(i, 4, 6, []) for i in range(6)]
        lin = run(ps)["linearization"]["glued"]
        m = lin["measures"]["n_nodes"]
        self.assertFalse(m["symptom_of_script"])
        self.assertIsNone(m["prior_mean_diff"])
        self.assertEqual(lin["prior_only_contrast"]["excluded_reasons"], {"prior_or_ai_missing": 6})

    def test_every_measure_row_carries_the_flag_and_the_all_edges_block_too(self):
        lin = run([person(i, 4, 6, [6]) for i in range(6)])["linearization"]
        for block in ("glued", "all"):
            for k, row in lin[block]["measures"].items():
                self.assertIn("symptom_of_script", row, (block, k))
                self.assertIn("prior_mean_diff", row, (block, k))


class TestHomogenizationSymptom(unittest.TestCase):
    """Endorsed maps share one concept and each add a private one (pairwise Jaccard 1/3). AI maps all alike
    (Jaccard 1): the AI is MORE homogeneous. The prior-only samples decide whether the script does the same."""
    COMMON = "zzcommon zzconcept zzlabel"

    def people(self, prior_labels, n_samples=4, n=8):
        return [person(i, 2, 2, [2] * n_samples, ai_labels=lambda i: [self.COMMON + " aaa", "zzother zzsame zzthing"],
                       end_labels=lambda i: ["zzshared zzendorsed zzconcept", uniq(i, 1, "u")[0]],
                       prior_labels=prior_labels) for i in range(n)]

    def test_script_as_homogeneous_as_the_ai_is_a_symptom(self):
        ps = self.people(lambda s: ["zzprior zzsame zzone", "zzprior zzsame zztwo"])      # samples alike
        c = run(ps)["homogenization"]["concept"]
        self.assertGreater(c["diff"], 0)
        self.assertGreater(c["prior_minus_endorsed"], 0)
        self.assertTrue(c["symptom_of_script"])

    def test_script_less_homogeneous_than_the_endorsed_maps_is_not_a_symptom(self):
        ps = self.people(lambda s: uniq(500 + s, 2, "p"))                                  # every sample its own words
        c = run(ps)["homogenization"]["concept"]
        self.assertGreater(c["diff"], 0)
        self.assertLess(c["prior_minus_endorsed"], 0)
        self.assertFalse(c["symptom_of_script"])

    def test_participants_sharing_a_prior_sample_are_not_paired_with_each_other(self):
        # one sample only: every pair shares it, so no prior pair survives and no symptom can be claimed
        ps = self.people(lambda s: ["zzprior zzsame zzone", "zzprior zzsame zztwo"], n_samples=1)
        h = run(ps)["homogenization"]
        self.assertEqual(h["prior_only_contrast"]["concept"]["n_pairs"], 0)
        self.assertFalse(h["concept"]["symptom_of_script"])
        self.assertIsNone(h["concept"]["prior_minus_endorsed"])

    def test_homogenization_names_its_label_source(self):
        h = run(self.people(lambda s: ["zzprior zzsame zzone", "zzprior zzsame zztwo"]))["homogenization"]
        self.assertEqual(h["label_source"]["label_source"], "raw_fallback")       # no canonical labels were supplied
        self.assertEqual(h["label_source"]["n_raw_ai"], 8)
        canon = [replace(p, canon_ai={FULL: ["c1", "c2"], VAR: ["c1", "c2"]}, canon_endorsed=["c3", "c4"],
                         canon_prior={k: ["c5", "c6"] for k in p.ai if k.startswith("prior_only")})
                 for p in self.people(lambda s: ["zzprior zzsame zzone", "zzprior zzsame zztwo"])]
        self.assertEqual(run(canon)["homogenization"]["label_source"]["label_source"], "canonical")
        mixed = [canon[0]] + [replace(p, canon_ai=None) for p in canon[1:]]
        h = run(mixed)["homogenization"]["label_source"]
        self.assertEqual((h["label_source"], h["n_raw_ai"]), ("mixed", 7))


if __name__ == "__main__":
    unittest.main()
