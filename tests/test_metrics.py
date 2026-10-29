import json
import math
import random
import unittest
from dataclasses import replace

from second_scribe.match import PRIMARY_SETTING, SETTINGS_GRID, MatchSetting
from second_scribe.metrics import (PairInputs, scripts_by_role, absences, claim_stable, contradiction_rate, status_permutation_null, build_script, buckets, controls, headline,
                                   homogenization, leakage, linearization, script_share, sign_stability,
                                   with_keyword_modality)
from second_scribe.provenance import Provenance
from second_scribe.schemas import BUCKETS, Edge, MapGraph, Node

FULL = "full__p0__t0__r0"
REMOVED = "removed__p0__t0__r0"
VAR = "full__p1__t0__r0"
PRIOR = "prior_only__p0__t0__r0"
S = PRIMARY_SETTING


def prov(speaker="participant", words=12, whole=False, mapping=False, found=True):
    return Provenance("x", "node", found, speaker, 1, 1, words, whole, mapping)


def word(k):
    return f"zq{k:03d}"


def label(i, k):
    """A 3-content-word label that shares no word with any other (i, k)."""
    return f"{word(i * 10 + k)} {word(i * 10 + k + 100)} {word(i * 10 + k + 200)}"


def endorsed_map(i=0):
    nodes = [Node("e0", label(i, 0)), Node("e1", label(i, 1), status="wished"), Node("e2", label(i, 2))]
    return MapGraph(f"E{i}", "endorsed", nodes, [Edge("e0", "e1"), Edge("e1", "e2")], interviewer="I1")


IN_T = {"e0": True, "e1": True, "e2": False}


def ai_node(nid, text, status="experienced", modality=None, p=None, quote=None):
    modality = modality or {"experienced": "actual", "wished": "wished"}[status]
    return Node(nid, text, status=status, quote=quote), modality, p or prov()


def pair(ai_nodes, *, spec=FULL, i=0, edges=None, in_t=None, endorsed=None):
    nodes = [n for n, _, _ in ai_nodes]
    g = MapGraph(f"A{i}", "ai", nodes, edges if edges is not None else
                 [Edge(a.node_id, b.node_id) for a, b in zip(nodes, nodes[1:])])
    return PairInputs(
        pid=f"P{i}", role="r1", interviewer="I1", endorsed=endorsed or endorsed_map(i),
        ai={spec: g}, prov={spec: {n.node_id: pr for n, _, pr in ai_nodes}},
        modality={spec: {n.node_id: m for n, m, _ in ai_nodes}},
        in_transcript=in_t or dict(IN_T),
        judged_ai_vs_endorsed={spec: {}}, judged_prior_vs_endorsed={},
        judged_ai_vs_script={spec: {}}, judged_endorsed_vs_script={})


def perfect(i=0, spec=FULL):
    e = endorsed_map(i)
    return pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished")], spec=spec, i=i)


class TestBuckets(unittest.TestCase):
    def test_perfect_ai(self):
        b = buckets(perfect(), FULL, "full", S)
        self.assertEqual(b["node_recall"], 1.0)
        self.assertEqual(b["matched"], 2)
        self.assertEqual(b["missed"], 0)
        self.assertEqual(b["off_record"], 1)             # e2 is off the record: not "missed"
        self.assertEqual((b["unanchored"], b["attested_absent_from_map"]), (0, 0))
        self.assertEqual(b["edge_recall"], 1.0)          # e0->e1 only; e1->e2 has an off-record end
        self.assertEqual(b["n_edges_eligible"], 1)
        for k in BUCKETS:
            self.assertIn(k, b)
        a = absences(perfect(), FULL, "full", S)
        self.assertEqual((a["recovered_as_wished"], a["leaked_as_existing"], a["missed"]), (1, 0, 0))
        self.assertEqual((a["n_wished"], a["leak_rate"], a["recovery_rate"]), (1, 0.0, 1.0))
        self.assertEqual((a["n_control"], a["flipped"], a["flip_rate"]), (1, 0, 0.0))

    def test_leaky_ai(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "experienced")])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["leaked_as_existing"], a["recovered_as_wished"], a["missed"]), (1, 0, 0))
        self.assertEqual(a["leak_rate"], 1.0)
        self.assertEqual((a["leaked_quote_actual"], a["leaked_quote_wished"], a["leaked_quote_other"]), (1, 0, 0))
        self.assertEqual(buckets(p, FULL, "full", S)["node_recall"], 1.0)   # recall ignores status

    def leak_case(self, modality):
        e = endorsed_map()
        return absences(pair([ai_node("a0", e.nodes[0].text),
                              ai_node("a1", e.nodes[1].text, "experienced", modality=modality)]),
                        FULL, "full", S)

    def test_leak_split_by_quote_modality(self):
        # an experienced AI node whose OWN quote the judge reads as wished: still located, still a leak,
        # and the strongest form of one
        a = self.leak_case("wished")
        self.assertEqual((a["leaked_as_existing"], a["leaked_quote_wished"]), (1, 1))
        self.assertEqual((a["leaked_quote_actual"], a["leaked_quote_other"], a["missed"], a["recovered_as_wished"]),
                         (0, 0, 0, 0))
        self.assertEqual((a["leak_rate"], a["leak_quote_wished_rate"]), (1.0, 1.0))
        for mod in ("negated", "hypothetical", "other_person", "unsupported"):
            a = self.leak_case(mod)
            self.assertEqual((a["leaked_as_existing"], a["leaked_quote_other"], a["leaked_quote_wished"],
                              a["leaked_quote_actual"]), (1, 1, 0, 0), mod)
        a = self.leak_case("actual")
        self.assertEqual((a["leaked_as_existing"], a["leaked_quote_actual"], a["leaked_quote_other"]), (1, 1, 0))
        self.assertEqual(a["leak_quote_wished_rate"], 0.0)

    def test_leak_split_strongest_modality_wins(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text),
                  ai_node("b1", e.nodes[1].text, "experienced", modality="negated"),
                  ai_node("c1", e.nodes[1].text, "experienced", modality="actual"),
                  ai_node("d1", e.nodes[1].text, "experienced", modality="wished")])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["leaked_as_existing"], a["leaked_quote_wished"], a["leaked_quote_actual"]), (1, 1, 0))
        p2 = pair([ai_node("a0", e.nodes[0].text),
                   ai_node("b1", e.nodes[1].text, "experienced", modality="negated"),
                   ai_node("c1", e.nodes[1].text, "experienced", modality="actual")])
        a = absences(p2, FULL, "full", S)
        self.assertEqual((a["leaked_quote_actual"], a["leaked_quote_other"]), (1, 0))

    def test_unlocated_quote_is_leaked_unanchored_not_leaked(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text),
                  ai_node("a1", e.nodes[1].text, "experienced", p=prov(found=False))])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["leaked_unanchored"], a["leaked_as_existing"], a["missed"]), (1, 0, 0))
        self.assertEqual(a["leaked_unanchored_rate"], 1.0)
        self.assertEqual(a["leak_rate"], 0.0)
        # a located experienced node alongside the unlocated one: the located one decides
        p2 = pair([ai_node("a1", e.nodes[1].text, "experienced", p=prov(found=False)),
                   ai_node("b1", e.nodes[1].text, "experienced")])
        a2 = absences(p2, FULL, "full", S)
        self.assertEqual((a2["leaked_as_existing"], a2["leaked_unanchored"]), (1, 0))

    def test_unlocated_wished_node_is_neither_leak_nor_recovery(self):
        e = endorsed_map()
        p = pair([ai_node("a1", e.nodes[1].text, "wished", p=prov(found=False))])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["unanchored_wished"], a["recovered_as_wished"], a["leaked_unanchored"], a["missed"]),
                         (1, 0, 0, 0))

    def test_counts_partition_the_wished_nodes(self):
        e = endorsed_map()
        for nodes in ([], [ai_node("a1", e.nodes[1].text, "wished")],
                      [ai_node("a1", e.nodes[1].text, "experienced")],
                      [ai_node("a1", e.nodes[1].text, "experienced", p=prov(found=False))],
                      [ai_node("a1", e.nodes[1].text, "wished", p=prov(found=False))]):
            a = absences(pair(nodes), FULL, "full", S)
            self.assertEqual(a["recovered_as_wished"] + a["leaked_as_existing"] + a["leaked_unanchored"]
                             + a["unanchored_wished"] + a["missed"], a["n_wished"])
            self.assertEqual(a["leaked_quote_wished"] + a["leaked_quote_actual"] + a["leaked_quote_other"],
                             a["leaked_as_existing"])

    def test_located_experienced_beats_unlocated_wished(self):
        # pinned ruling: only LOCATED nodes decide; an unlocated wished match next to a located experienced one
        # is ignored, so the wished endorsed node counts as a leak (and the mirror case as a flip)
        e = endorsed_map()
        p = pair([ai_node("a1", e.nodes[1].text, "experienced"),
                  ai_node("b1", e.nodes[1].text, "wished", p=prov(found=False))])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["leaked_as_existing"], a["recovered_as_wished"], a["unanchored_wished"]), (1, 0, 0))
        q = pair([ai_node("a0", e.nodes[0].text, "wished"),
                  ai_node("b0", e.nodes[0].text, "experienced", p=prov(found=False))])
        self.assertEqual(absences(q, FULL, "full", S)["flipped"], 1)

    def test_modality_does_not_decide_recovery(self):
        e = endorsed_map()
        # wished-drawn, located, quote judged "actual": recovered all the same
        a = absences(pair([ai_node("a1", e.nodes[1].text, "wished", modality="actual")]), FULL, "full", S)
        self.assertEqual((a["recovered_as_wished"], a["missed"]), (1, 0))

    def test_control_flip_needs_located_wished(self):
        e = endorsed_map()
        located = absences(pair([ai_node("a0", e.nodes[0].text, "wished", modality="actual")]), FULL, "full", S)
        self.assertEqual(located["flipped"], 1)
        unloc = absences(pair([ai_node("a0", e.nodes[0].text, "wished", p=prov(found=False))]), FULL, "full", S)
        self.assertEqual(unloc["flipped"], 0)
        two = absences(pair([ai_node("a0", e.nodes[0].text, "wished"), ai_node("b0", e.nodes[0].text, "wished")]),
                       FULL, "full", S)
        self.assertEqual(two["flipped"], 1)                                  # several wished nodes, no experienced one

    def test_negated_node_is_unanchored_never_absent(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished"),
                  ai_node("a2", "parking permit fee", modality="negated")])
        b = buckets(p, FULL, "full", S)
        self.assertEqual(b["unanchored"], 1)
        self.assertEqual(b["attested_absent_from_map"], 0)

    def test_edge_recall_needs_attested_ends(self):
        e = endorsed_map()
        ok = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished")])
        self.assertEqual(buckets(ok, FULL, "full", S)["edge_recall"], 1.0)
        bad = pair([ai_node("a0", e.nodes[0].text, modality="negated"), ai_node("a1", e.nodes[1].text, "wished")])
        self.assertEqual(buckets(bad, FULL, "full", S)["edge_recall"], 0.0)       # a0 is not attested
        flipped = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished")],
                       edges=[Edge("a1", "a0")])
        self.assertEqual(buckets(flipped, FULL, "full", S)["edge_recall"], 0.0)   # direction respected

    def test_attested_unmatched_is_absent_from_map(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished"),
                  ai_node("a2", "parking permit fee")])
        b = buckets(p, FULL, "full", S)
        self.assertEqual(b["attested_absent_from_map"], 1)
        self.assertEqual(b["unanchored"], 0)

    def test_missed_when_only_unattested_ai_matches(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text, modality="hypothetical")])
        b = buckets(p, FULL, "full", S)
        self.assertEqual((b["matched"], b["missed"], b["unanchored"]), (0, 2, 1))
        self.assertEqual(b["node_recall"], 0.0)

    def test_attested_ai_matching_off_record_node_is_not_absent(self):
        e = endorsed_map()
        p = pair([ai_node("a2", e.nodes[2].text)])
        b = buckets(p, FULL, "full", S)
        self.assertEqual((b["off_record"], b["attested_absent_from_map"]), (1, 0))

    def test_removed_arm_interviewer_voiced_is_unanchored(self):
        e = endorsed_map()
        nodes = [ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished", p=prov("interviewer"))]
        full = pair(nodes, spec=FULL)
        removed = pair(nodes, spec=REMOVED)
        self.assertEqual(buckets(full, FULL, "full", S)["unanchored"], 0)       # full arm accepts it
        b = buckets(removed, REMOVED, "removed", S)
        self.assertEqual(b["unanchored"], 1)
        self.assertEqual((b["matched"], b["missed"]), (1, 1))
        ra = absences(removed, REMOVED, "removed", S)       # e1 is matched, but only by a quote that is not located
        self.assertEqual((ra["unanchored_wished"], ra["missed"], ra["recovered_as_wished"]), (1, 0, 0))
        self.assertEqual(absences(full, FULL, "full", S)["recovered_as_wished"], 1)

    def test_removed_arm_mapping_talk_turn_is_unanchored(self):
        e = endorsed_map()
        p = pair([ai_node("a1", e.nodes[1].text, "wished", p=prov(mapping=True))], spec=REMOVED)
        self.assertEqual(buckets(p, REMOVED, "removed", S)["unanchored"], 1)

    def test_min_words_cutoff(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text, p=prov(words=6)), ai_node("a1", e.nodes[1].text, "wished")])
        self.assertEqual(buckets(p, FULL, "full", S)["unanchored"], 1)                 # default 8
        self.assertEqual(buckets(p, FULL, "full", S, min_words=5)["unanchored"], 0)
        self.assertEqual(buckets(p, FULL, "full", S, min_words=12)["unanchored"], 1)   # 6 < 12, 12 >= 12
        self.assertEqual(buckets(p, FULL, "full", S, min_words=13)["unanchored"], 2)
        self.assertEqual(buckets(p, FULL, "full", S, min_words=5)["matched"], 2)
        self.assertEqual(buckets(p, FULL, "full", S)["matched"], 1)

    def test_whole_turn_short_quote_is_eligible(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text, p=prov(words=2, whole=True)),
                  ai_node("a1", e.nodes[1].text, "wished")])
        self.assertEqual(buckets(p, FULL, "full", S)["unanchored"], 0)

    def test_control_flip(self):
        e = endorsed_map()
        # the endorsed experienced node e0 is drawn as wished by the AI
        p = pair([ai_node("a0", e.nodes[0].text, "wished"), ai_node("a1", e.nodes[1].text, "wished")])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["flipped"], a["n_control"], a["flip_rate"]), (1, 1, 1.0))

    def test_mixed_statuses_on_one_endorsed_node(self):
        e = endorsed_map()
        # e1 (wished) is matched by an experienced AND a wished AI node: it is recovered, not leaked.
        # e0 (experienced) is matched by an experienced AND a wished AI node: not a flip.
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("b0", e.nodes[0].text, "wished"),
                  ai_node("a1", e.nodes[1].text), ai_node("b1", e.nodes[1].text, "wished")])
        a = absences(p, FULL, "full", S)
        self.assertEqual((a["recovered_as_wished"], a["leaked_as_existing"], a["flipped"]), (1, 0, 0))

    def test_judge_alignment_is_used_and_missing_inputs_raise(self):
        e = endorsed_map()
        p = pair([ai_node("a0", "something unrelated entirely")])
        self.assertEqual(buckets(p, FULL, "full", S)["matched"], 0)
        p2 = replace(p, judged_ai_vs_endorsed={FULL: {(0, 0): "same"}})
        self.assertEqual(buckets(p2, FULL, "full", S)["matched"], 1)
        p3 = replace(p2, judged_ai_vs_endorsed={FULL: {(0, 0): "close"}})
        self.assertEqual(buckets(p3, FULL, "full", S)["matched"], 0)               # strict setting
        self.assertEqual(buckets(p3, FULL, "full", MatchSetting(0.6, "same_or_close"))["matched"], 1)
        with self.assertRaises(KeyError):
            buckets(replace(p, in_transcript={"e0": True}), FULL, "full", S)
        with self.assertRaises(ValueError):
            buckets(replace(p, ai={FULL: None}), FULL, "full", S)

    def test_node_recall_nan_without_in_transcript_nodes(self):
        p = replace(perfect(), in_transcript={"e0": False, "e1": False, "e2": False})
        self.assertTrue(math.isnan(buckets(p, FULL, "full", S)["node_recall"]))
        self.assertTrue(math.isnan(absences(p, FULL, "full", S)["leak_rate"]))


class TestCachedMatching(unittest.TestCase):
    def test_same_as_match_module(self):
        import second_scribe.match as mm
        from second_scribe.metrics import match_nodes as cached
        rng = random.Random(5)
        vocab = [word(k) for k in range(12)]
        for _ in range(40):
            def g(n):
                return MapGraph("g", "ai", [Node(f"n{k}", " ".join(rng.sample(vocab, rng.randint(0, 4))))
                                            for k in range(n)])
            left, right = g(rng.randint(0, 6)), g(rng.randint(0, 6))
            judged = {(i, j): rng.choice(["same", "close"]) for i in range(len(left.nodes))
                      for j in range(len(right.nodes)) if rng.random() < 0.2}
            for setting in list(SETTINGS_GRID) + [MatchSetting(0.5, "none")]:
                a, b = cached(left, right, setting, judged), mm.match_nodes(left, right, setting, judged)
                self.assertEqual((a.pairs, a.left_matched, a.right_matched), (b.pairs, b.left_matched, b.right_matched))


class TestCachedClustering(unittest.TestCase):
    def test_same_as_match_module(self):
        import second_scribe.match as mm
        from second_scribe.metrics import cluster_labels as cached
        rng = random.Random(8)
        vocab = [word(k) for k in range(10)]
        for _ in range(40):
            labels = [" ".join(rng.sample(vocab, rng.randint(0, 4))) for _ in range(rng.randint(0, 25))]
            for thr in (0.3, 0.5, 0.8):
                self.assertEqual(cached(labels, thr), mm.cluster_labels(labels, thr))


class TestScript(unittest.TestCase):
    def test_script_share_identical_and_disjoint(self):
        e = endorsed_map()
        script = MapGraph("script", "prior_only", [Node(f"s{k}", n.text) for k, n in enumerate(e.nodes)])
        self.assertEqual(script_share(e, {}, script, S), 1.0)
        other = endorsed_map(5)
        self.assertEqual(script_share(other, {}, script, S), 0.0)

    def test_script_share_judged_and_only(self):
        e = endorsed_map()
        script = MapGraph("script", "prior_only", [Node("s0", "unrelated words here")])
        self.assertEqual(script_share(e, {(0, 0): "same"}, script, S), 1 / 3)
        self.assertEqual(script_share(e, {(0, 0): "same"}, script, S, only={"e0", "e1"}), 1 / 2)
        self.assertTrue(math.isnan(script_share(MapGraph("x", "ai"), {}, script, S)))

    def test_build_script_support_label_and_ties(self):
        def m(*labels):
            return MapGraph("p", "prior_only", [Node(f"n{k}", l) for k, l in enumerate(labels)])
        maps = [m("waiting list queue", "rare unique thing"),
                m("Waiting list queue", "office counter desk"),
                m("waiting list queue"),
                m("office counter desk")]
        s = build_script(maps)
        texts = sorted(n.text for n in s.nodes)
        self.assertEqual(texts, ["office counter desk", "waiting list queue"])   # "rare unique thing" has support 1
        # most frequent member label: "waiting list queue" x2 beats "Waiting list queue" x1
        # tie ("a b c" x1 vs "A b c" x1): alphabetical, "A b c" < "a b c" in codepoint order
        t = build_script([m("a b c"), m("A b c")])
        self.assertEqual([n.text for n in t.nodes], ["A b c"])
        self.assertEqual(build_script(maps, support=3).nodes[0].text, "waiting list queue")
        self.assertEqual(len(build_script(maps, support=5).nodes), 0)

    def test_scripts_by_role_counts_a_shared_prior_sample_once(self):
        # prior-only maps are drawn per role, so the pipeline gives every participant of the role the same
        # samples; a concept in ONE sample must not reach support 2 because three participants carry it
        k0, k1 = "prior_only__p0__t0.7__r0", "prior_only__p0__t0.7__r1"
        s0 = MapGraph("r1__" + k0, "prior_only", [Node("n0", "waiting list queue"), Node("n1", "rare unique thing")])
        s1 = MapGraph("r1__" + k1, "prior_only", [Node("n0", "waiting list queue")])
        ps = [replace(perfect(i), ai={**perfect(i).ai, k0: s0, k1: s1}) for i in range(3)]
        script = scripts_by_role(ps)["r1"]
        self.assertEqual([n.text for n in script.nodes], ["waiting list queue"])
        self.assertEqual(script.meta["n_maps"], 2)

    def test_build_script_support_counts_maps_not_labels(self):
        one = MapGraph("p", "prior_only", [Node("n0", "waiting list queue"), Node("n1", "waiting list queue")])
        self.assertEqual(len(build_script([one]).nodes), 0)


class TestHomogenization(unittest.TestCase):
    def test_identical_ai_vs_different_endorsed(self):
        n = 4
        ai_maps = [MapGraph(f"a{i}", "ai", [Node("x0", "alpha beta gamma"), Node("x1", "delta eps zeta")],
                            [Edge("x0", "x1")]) for i in range(n)]
        end_maps = [endorsed_map(i) for i in range(n)]
        for i, m in enumerate(end_maps):                       # endorsed maps differ in size too
            m.nodes += [Node(f"x{k}", label(i, 5 + k)) for k in range(i)]
        canon_a = [[x.text for x in m.nodes] for m in ai_maps]
        canon_b = [[x.text for x in m.nodes] for m in end_maps]
        r = homogenization(ai_maps, end_maps, canon_a, canon_b, ["I1", "I1", "I2", "I2"], seed=1)
        self.assertEqual(r["concept"]["mean_jaccard_a"], 1.0)
        self.assertLess(r["concept"]["mean_jaccard_b"], 1.0)
        self.assertGreater(r["concept"]["diff"], 0)
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["n_excluded"], 0)
        # identical structure vectors -> zero distance within AI
        self.assertEqual(r["structure"]["mean_distance_a"], 0.0)
        self.assertGreater(r["structure"]["mean_distance_b"], 0.0)
        self.assertEqual(r["by_interviewer"]["same"]["n_pairs"], 2)
        self.assertEqual(r["by_interviewer"]["different"]["n_pairs"], 4)
        self.assertEqual(r["by_interviewer"]["same"]["concept"]["mean_jaccard_a"], 1.0)

    def test_exclusions_and_unknown_interviewer(self):
        ai_maps = ([MapGraph(f"a{i}", "ai", [Node("x0", "alpha beta gamma")]) for i in range(3)] + [None]
                   + [MapGraph("a4", "ai", [Node("x0", "alpha beta gamma")]), MapGraph("a5", "ai")])
        end_maps = [endorsed_map(i) for i in range(6)]
        canon_a = [["alpha beta gamma"]] * 3 + [[]] + [["alpha beta gamma"], []]
        canon_b = [[x.text for x in m.nodes] for m in end_maps]
        r = homogenization(ai_maps, end_maps, canon_a, canon_b, ["I1", None, "I1", "I1", None, "I2"])
        self.assertEqual((r["n"], r["n_excluded"], r["excluded_reasons"]), (4, 2, {"ai_failed": 1, "empty_map": 1}))
        # kept participants 0, 1, 2, 4 (interviewers I1, None, I1, None): only (0, 2) share one; two
        # unknown interviewers do not count as "the same", and unknown pairs are in neither group
        self.assertEqual(r["by_interviewer"]["same"]["n_pairs"], 1)
        self.assertEqual(r["by_interviewer"]["different"]["n_pairs"], 0)
        self.assertEqual(r["by_interviewer"]["n_pairs_unknown_excluded"], 5)       # 6 pairs, 1 in "same"

    def test_deterministic(self):
        ai_maps = [MapGraph(f"a{i}", "ai", [Node("x0", f"alpha{i} beta gamma")]) for i in range(5)]
        end_maps = [endorsed_map(i) for i in range(5)]
        canon_a = [[x.text for x in m.nodes] for m in ai_maps]
        canon_b = [[x.text for x in m.nodes] for m in end_maps]
        args = (ai_maps, end_maps, canon_a, canon_b, ["I1"] * 5)
        self.assertEqual(homogenization(*args, seed=3), homogenization(*args, seed=3))


class TestLinearization(unittest.TestCase):
    def test_paired_diffs(self):
        def chain(i, n):
            return MapGraph(f"c{i}", "ai", [Node(f"n{k}", f"s{k}") for k in range(n)],
                            [Edge(f"n{k}", f"n{k+1}") for k in range(n - 1)])

        def branchy(i):
            g = chain(i, 4)
            g.edges += [Edge("n0", "n2"), Edge("n3", "n1")]
            return g
        ai = [chain(i, 4) for i in range(6)]
        end = [branchy(i) for i in range(6)]
        r = linearization(ai, end, glued_only=True, seed=1)
        self.assertEqual((r["n"], r["n_excluded"]), (6, 0))
        m = r["measures"]["branching_share"]
        self.assertAlmostEqual(m["mean_diff"], 0.0 - 0.25)
        self.assertLess(m["ci_hi"], 0)
        self.assertEqual(m["sign"]["n_neg"], 6)
        self.assertEqual(r["measures"]["n_nontrivial_scc"]["mean_diff"], -1.0)
        self.assertIn("type_share:service", r["measures"])
        self.assertEqual(r["alternative"], "two-sided")

    def test_exclusions(self):
        a = MapGraph("a", "ai", [Node("n0", "x")])
        r = linearization([a, None, MapGraph("e", "ai")], [endorsed_map(), endorsed_map(1), endorsed_map(2)], True)
        self.assertEqual((r["n"], r["n_excluded"]), (1, 2))
        self.assertEqual(r["excluded_reasons"], {"ai_failed": 1, "empty_map": 1})


class TestLeakageControls(unittest.TestCase):
    def two_arm(self, i=0):
        e = endorsed_map(i)
        full = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "experienced")],
                    spec=FULL, i=i)
        rem = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished", p=prov("interviewer"))],
                   spec=REMOVED, i=i)
        return replace(full, ai={**full.ai, **rem.ai}, prov={**full.prov, **rem.prov},
                       modality={**full.modality, **rem.modality},
                       judged_ai_vs_endorsed={FULL: {}, REMOVED: {}})

    def test_leakage_arms(self):
        ps = [self.two_arm(i) for i in range(5)]
        r = leakage(ps, S, full_spec=FULL, removed_spec=REMOVED, seed=1)
        # full recall 1.0; removed: e1 unanchored (interviewer) -> recall 0.5
        self.assertAlmostEqual(r["node_recall_diff"]["mean"], 0.5)
        self.assertEqual(r["node_recall_diff"]["n"], 5)
        self.assertEqual(r["node_recall_diff"]["n_excluded"], 0)
        # leak: full leaks (1.0), removed misses (0.0)
        self.assertAlmostEqual(r["leak_rate_diff"]["mean"], 1.0)
        self.assertIn("contaminated_share", r)       # edges here have no provenance entry: not elements

    def test_contaminated_share(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text, p=prov("interviewer")),
                  ai_node("a1", e.nodes[1].text, "wished", p=prov(mapping=True)),
                  ai_node("a2", "parking permit fee")], edges=[])
        r = leakage([p], S, full_spec=FULL, removed_spec=FULL)
        c = r["contaminated_share"]
        self.assertEqual((c["n_attested_elements"], c["n_contaminated"]), (3, 2))
        self.assertAlmostEqual(c["pooled_share"], 2 / 3)
        self.assertAlmostEqual(c["mean_map_share"], 2 / 3)

    def test_contaminated_share_counts_edges_that_have_provenance(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "wished")],
                 edges=[Edge("a0", "a1", quote="q"), Edge("a1", "a0", quote="q")])
        p.prov[FULL]["e:a0->a1"] = prov("interviewer")
        p.modality[FULL]["e:a0->a1"] = "actual"
        c = leakage([p], S, full_spec=FULL, removed_spec=FULL)["contaminated_share"]
        self.assertEqual((c["n_attested_elements"], c["n_contaminated"]), (3, 1))   # 2 nodes + the edge with data

    def test_leakage_exclusions(self):
        ps = [self.two_arm(0), replace(self.two_arm(1), ai={FULL: None, REMOVED: None})]
        r = leakage(ps, S, full_spec=FULL, removed_spec=REMOVED)
        self.assertEqual((r["node_recall_diff"]["n"], r["node_recall_diff"]["n_excluded"]), (1, 1))
        self.assertEqual(r["node_recall_diff"]["excluded_reasons"], {"ai_failed": 1})

    def controls_inputs(self, n=5):
        return [perfect(i) for i in range(n)]

    def test_shuffled_pairs_and_rank(self):
        ps = self.controls_inputs()
        r = controls(ps, S, spec_key=FULL, n_derangements=6, seed=2)
        sp = r["shuffled_pairs"]
        self.assertEqual(sp["real_recall_mean"], 1.0)
        self.assertEqual(sp["shuffled_recall_mean"], 0.0)       # disjoint vocabularies
        self.assertEqual((sp["n"], sp["n_excluded"], sp["n_derangements"]), (5, 0, 6))
        self.assertEqual(r["permutation_rank"]["mean_rank"], 1.0)
        self.assertEqual(r["permutation_rank"]["n"], 5)
        self.assertEqual(r["permutation_rank"]["share_rank1"], 1.0)
        self.assertEqual(r["shuffled_quotes"]["n"], 0)          # no shuffled re-judge supplied
        self.assertEqual(r["shuffled_quotes"]["excluded_reasons"], {"no_shuffled_judge": 5})
        self.assertEqual(r, controls(ps, S, spec_key=FULL, n_derangements=6, seed=2))

    def canon_inputs(self, n=4):
        out = []
        for i in range(n):
            p = perfect(i)
            out.append(replace(p, canon_endorsed=[x.text for x in p.endorsed.nodes],
                               canon_ai={FULL: [x.text for x in p.ai[FULL].nodes]}))
        return out

    def test_rank_canonical_flag_uses_the_claims_audit(self):
        ps = self.canon_inputs()
        r = controls(ps, S, spec_key=FULL, n_derangements=3)["permutation_rank"]
        self.assertTrue(r["canonical_used"])
        self.assertEqual(r["canonical_fallback_reasons"], [])
        # a participant whose AI map failed needs no canonical AI labels (as in the claim's audit)
        failed = list(ps)
        failed[3] = replace(ps[3], ai={FULL: None}, canon_ai=None)
        self.assertTrue(controls(failed, S, spec_key=FULL, n_derangements=3)["permutation_rank"]["canonical_used"])
        # a missing endorsed list, or a missing AI list for an existing map, clears the flag
        no_end = [replace(ps[0], canon_endorsed=None)] + ps[1:]
        r2 = controls(no_end, S, spec_key=FULL, n_derangements=3)["permutation_rank"]
        self.assertFalse(r2["canonical_used"])
        self.assertEqual(r2["canonical_fallback_reasons"], ["canon_endorsed_missing"])
        no_ai = [replace(ps[0], canon_ai=None)] + ps[1:]
        r3 = controls(no_ai, S, spec_key=FULL, n_derangements=3)["permutation_rank"]
        self.assertEqual(r3["canonical_fallback_reasons"], [f"canon_ai_missing:{FULL}"])
        # ... and so does a prior-only sample without canonical labels, as in the claim
        prior = MapGraph("pr", "prior_only", [Node("p0", "something")])
        with_prior = [replace(p, ai={**p.ai, PRIOR: prior}) for p in ps]
        r4 = controls(with_prior, S, spec_key=FULL, n_derangements=3)["permutation_rank"]
        self.assertFalse(r4["canonical_used"])
        self.assertEqual(r4["canonical_fallback_reasons"], ["canon_prior_missing"])

    def test_shuffled_pairs_counts_attested_ai_nodes_only(self):
        e = endorsed_map(0)
        p = pair([ai_node("a0", e.nodes[0].text, modality="negated"), ai_node("a1", e.nodes[1].text, "wished")])
        others = [perfect(i) for i in range(1, 4)]
        sp = controls([p] + others, S, spec_key=FULL, n_derangements=3)["shuffled_pairs"]
        self.assertAlmostEqual(sp["real_recall_mean"], (0.5 + 3 * 1.0) / 4)

    def test_shuffled_pairs_uses_judge_free_matching(self):
        # alignment would make everything "same", but the control must ignore it
        ps = [replace(p, judged_ai_vs_endorsed={FULL: {(a, b): "same" for a in range(2) for b in range(3)}})
              for p in self.controls_inputs()]
        r = controls(ps, S, spec_key=FULL, n_derangements=3)
        self.assertEqual(r["shuffled_pairs"]["shuffled_recall_mean"], 0.0)

    def test_shuffled_quotes(self):
        ps = [replace(p, modality_shuffled={FULL: {"a0": "negated", "a1": "actual"}}) for p in self.controls_inputs(3)]
        r = controls(ps, S, spec_key=FULL, n_derangements=3)["shuffled_quotes"]
        self.assertEqual(r["real_share_mean"], 1.0)
        self.assertEqual(r["shuffled_share_mean"], 0.0)        # a0 actual!=negated; a1 wished != actual
        self.assertEqual(r["n"], 3)
        half = [replace(p, modality_shuffled={FULL: {"a0": "actual", "a1": "actual"}}) for p in self.controls_inputs(3)]
        self.assertEqual(controls(half, S, spec_key=FULL, n_derangements=3)["shuffled_quotes"]["shuffled_share_mean"], 0.5)

    def test_controls_too_few_participants(self):
        r = controls([perfect()], S, spec_key=FULL, n_derangements=3)
        self.assertEqual(r["shuffled_pairs"]["n"], 0)
        self.assertIn("too_few_participants", r["shuffled_pairs"]["excluded_reasons"])


class TestNotJudged(unittest.TestCase):
    """Elements the judge never answered for (failed or unparsed batch): neither attested nor unanchored."""

    def nj(self, p, ids, spec=FULL):
        return replace(p, not_judged={spec: set(ids)})

    def test_a_not_judged_node_moves_out_of_unanchored_into_n_not_judged(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", "zq900 zq901 zq902", modality="unsupported")])
        before = buckets(p, FULL, "full", S)
        self.assertEqual((before["unanchored"], before["n_not_judged"]), (1, 0))
        after = buckets(self.nj(p, ["a1"]), FULL, "full", S)
        self.assertEqual((after["unanchored"], after["attested_absent_from_map"], after["n_not_judged"]), (0, 0, 1))

    def test_a_not_judged_node_is_never_attested(self):
        e = endorsed_map()
        p = self.nj(pair([ai_node("a0", e.nodes[0].text)]), ["a0"])       # placeholder label says "actual"
        b = buckets(p, FULL, "full", S)
        self.assertEqual((b["matched"], b["ai_attested_matched"], b["n_not_judged"]), (0, 0, 1))

    def test_a_leak_whose_quote_is_not_judged_lands_in_leaked_quote_unjudged(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "experienced", modality="unsupported")])
        self.assertEqual(absences(p, FULL, "full", S)["leaked_quote_other"], 1)
        a = absences(self.nj(p, ["a1"]), FULL, "full", S)
        self.assertEqual((a["leaked_as_existing"], a["leaked_quote_unjudged"], a["leaked_quote_other"]), (1, 1, 0))

    def test_contradiction_rate_leaves_not_judged_nodes_out(self):
        e = endorsed_map()
        p = pair([ai_node("a0", e.nodes[0].text), ai_node("a1", e.nodes[1].text, "experienced", modality="wished"),
                  ai_node("a2", "second idea", "wished")])
        w = contradiction_rate([p], FULL, B=50)["quote_wished_marked_experienced"]
        self.assertEqual((w["n_nodes"], w["n_contradicting"]), (2, 1))
        w = contradiction_rate([self.nj(p, ["a1"])], FULL, B=50)["quote_wished_marked_experienced"]
        self.assertEqual((w["n_nodes"], w["n_contradicting"]), (1, 0))

    def test_shuffled_control_leaves_out_nodes_either_judge_pass_did_not_answer(self):
        ps = [perfect(i) for i in range(3)]
        sh = {FULL: {"a0": "actual", "a1": "negated"}}              # a0 would count as attested under shuffling
        base = [replace(p, modality_shuffled=sh) for p in ps]
        r = controls(base, S, spec_key=FULL, n_derangements=3)["shuffled_quotes"]
        self.assertEqual((r["real_share_mean"], r["shuffled_share_mean"]), (1.0, 0.5))
        out = [replace(p, not_judged_shuffled={FULL: {"a0"}}) for p in base]
        r = controls(out, S, spec_key=FULL, n_derangements=3)["shuffled_quotes"]
        self.assertEqual((r["real_share_mean"], r["shuffled_share_mean"]), (1.0, 0.0))   # a1 only, on both sides
        none_left = [replace(p, not_judged={FULL: {"a1"}}, not_judged_shuffled={FULL: {"a0"}}) for p in base]
        r = controls(none_left, S, spec_key=FULL, n_derangements=3)["shuffled_quotes"]
        self.assertEqual(r["excluded_reasons"], {"no_judged_nodes": 3})

    def test_keyword_modality_is_total(self):
        e = endorsed_map()
        p = self.nj(pair([ai_node("a0", e.nodes[0].text, quote="we went to the counter desk office")]), ["a0"])
        self.assertIsNone(with_keyword_modality(p).not_judged)


class TestMissingAlignments(unittest.TestCase):
    """A raw alignment that was never obtained (None, or no entry for the spec) leaves the participant out of
    that comparison, counted; it never reads as "nothing matched"."""

    def ps(self):
        out = []
        for i in range(4):
            p = perfect(i)
            prior = MapGraph(f"PR{i}", "prior_only", [Node("p0", p.endorsed.nodes[0].text),
                                                      Node("p1", "shared office counter desk")])
            out.append(replace(p, ai={**p.ai, PRIOR: prior}))
        return out

    def test_missing_prior_alignment_excludes_from_the_prior_baseline(self):
        ps = self.ps()
        ps[0] = replace(ps[0], judged_prior_vs_endorsed=None)
        r = run_headline(ps)["prior_only_baseline"]
        self.assertEqual(r["excluded_reasons"].get("alignment_missing"), 1)
        self.assertEqual(r["n"], 3)

    def test_missing_script_alignments_exclude_from_the_raw_pull(self):
        ps = self.ps()
        ps[0] = replace(ps[0], judged_endorsed_vs_script=None)
        ps[1] = replace(ps[1], judged_ai_vs_script={})
        raw = run_headline(ps)["pull_toward_script"]["robustness"]["raw_labels"]
        self.assertEqual(raw["excluded_reasons"], {"alignment_missing": 2})
        self.assertEqual(raw["n"], 2)


class TestContradictionRate(unittest.TestCase):
    def case(self, nodes, **kw):
        e = endorsed_map()
        return contradiction_rate([pair(nodes)], FULL, B=50, **kw)

    def test_quote_wished_marked_experienced(self):
        e = endorsed_map()
        nodes = [ai_node("a0", e.nodes[0].text),                                           # actual quote, experienced
                 ai_node("a1", e.nodes[1].text, "experienced", modality="wished"),         # contradicts its quote
                 ai_node("a2", "second idea", "wished"),                                   # wished quote, wished
                 ai_node("a3", "third idea", "experienced", modality="wished"),            # contradicts
                 ai_node("a4", "fourth idea", "wished", modality="actual")]                # mirror contradiction
        r = self.case(nodes)
        w, a = r["quote_wished_marked_experienced"], r["quote_actual_marked_wished"]
        self.assertEqual(r["n_same_set"], 1)
        self.assertEqual((w["n_nodes"], w["n_contradicting"]), (3, 2))
        self.assertAlmostEqual(w["pooled_share"], 2 / 3)
        self.assertAlmostEqual(w["mean"], 2 / 3)
        self.assertEqual((a["n_nodes"], a["n_contradicting"]), (2, 1))                     # a0 and a4 are actual-quoted
        self.assertAlmostEqual(a["pooled_share"], 0.5)
        self.assertEqual((w["n"], w["n_excluded"]), (1, 0))

    def test_only_located_nodes_count(self):
        e = endorsed_map()
        nodes = [ai_node("a1", e.nodes[1].text, "experienced", modality="wished", p=prov(found=False)),
                 ai_node("a2", "second idea", "wished")]
        w = self.case(nodes)["separate_samples"]["quote_wished_marked_experienced"]
        self.assertEqual((w["n_nodes"], w["n_contradicting"], w["pooled_share"]), (1, 0, 0.0))
        short = [ai_node("a1", e.nodes[1].text, "experienced", modality="wished", p=prov(words=6)),
                 ai_node("a0", e.nodes[0].text)]
        self.assertEqual(self.case(short)["quote_wished_marked_experienced"]["n"], 0)       # neither located at 8
        self.assertEqual(self.case(short, min_words=5)["quote_wished_marked_experienced"]["n_nodes"], 1)
        removed = contradiction_rate([pair([ai_node("a1", "x y", "experienced", modality="wished",
                                                    p=prov("interviewer")),
                                            ai_node("a0", "plain words")], spec=REMOVED)], REMOVED, B=10)
        self.assertEqual(removed["separate_samples"]["quote_wished_marked_experienced"]["n_nodes"], 0)   # not located
        self.assertEqual(removed["n_same_set"], 0)

    def test_both_directions_use_the_same_participants(self):
        both = pair([ai_node("a1", "idea one", "experienced", modality="wished"),
                     ai_node("a0", "idea zero", "wished", modality="actual")], i=0)
        wished_only = pair([ai_node("a1", "idea two", "experienced", modality="wished")], i=1)
        actual_only = pair([ai_node("a0", "idea three", "wished", modality="actual"),
                            ai_node("a2", "idea four", "experienced", modality="actual")], i=2)
        r = contradiction_rate([both, wished_only, actual_only], FULL, B=100)
        self.assertEqual(r["n_same_set"], 1)
        w, a = r["quote_wished_marked_experienced"], r["quote_actual_marked_wished"]
        self.assertEqual((w["n"], a["n"]), (1, 1))
        self.assertEqual(w["excluded_reasons"], {"no_located_wished_quotes": 1, "no_located_actual_quotes": 1})
        self.assertEqual(a["excluded_reasons"], {"no_located_wished_quotes": 1, "no_located_actual_quotes": 1})
        self.assertAlmostEqual(w["mean"], 1.0)
        self.assertAlmostEqual(a["mean"], 1.0)
        # secondary: every participant that has nodes of that kind
        sw = r["separate_samples"]["quote_wished_marked_experienced"]
        sa = r["separate_samples"]["quote_actual_marked_wished"]
        self.assertEqual((sw["n"], sa["n"]), (2, 2))
        self.assertAlmostEqual(sa["mean"], (1.0 + 0.5) / 2)
        self.assertIn("base rate", r["note"])
        self.assertIn("wished_rate_q", r["note"])

    def test_exclusions_and_participant_level_ci(self):
        mk = lambda i, status: pair([ai_node("a1", f"idea {i}", status, modality="wished")], i=i)
        ps = [mk(0, "experienced"), mk(1, "wished"), mk(2, "experienced"), mk(3, "wished")]
        ps.append(pair([ai_node("a0", "plain")], i=4))                   # no wished-quoted node at all
        ps.append(replace(ps[0], ai={FULL: None}))                       # failed map
        r = contradiction_rate(ps, FULL, B=300, seed=2)
        self.assertEqual(r["quote_wished_marked_experienced"]["n"], 0)       # nobody has both kinds of quote
        r = r["separate_samples"]                                           # the secondary, per-direction samples
        w = r["quote_wished_marked_experienced"]
        self.assertEqual((w["n"], w["n_excluded"]), (4, 2))
        self.assertEqual(w["excluded_reasons"], {"ai_failed": 1, "no_located_wished_quotes": 1})
        self.assertAlmostEqual(w["mean"], 0.5)
        self.assertLessEqual(w["ci_lo"], 0.5)
        self.assertGreaterEqual(w["ci_hi"], 0.5)
        self.assertLess(w["ci_lo"], w["ci_hi"])                          # participants differ (1, 0, 1, 0)
        self.assertEqual(r, contradiction_rate(ps, FULL, B=300, seed=2)["separate_samples"])
        self.assertEqual(r["quote_actual_marked_wished"]["n"], 1)        # only the plain participant has an actual quote

    def test_in_headline(self):
        h = run_headline([participant(i, leaky=True, leak_wish_quote=True, a1_modality="wished") for i in range(6)])
        c = h["absences"]["contradiction_rate"]
        self.assertEqual(c["quote_wished_marked_experienced"]["n"], 6)
        self.assertAlmostEqual(c["quote_wished_marked_experienced"]["mean"], 1.0)         # one wished-quoted node, drawn experienced
        self.assertEqual(c["quote_wished_marked_experienced"]["n_nodes"], 6)
        self.assertAlmostEqual(c["quote_actual_marked_wished"]["mean"], 0.0)


class TestStatusPermutationNull(unittest.TestCase):
    def maps(self, n=6, **kw):
        return [participant(i, **kw) for i in range(n)]

    def test_never_wished_ai_equals_the_null(self):
        r = status_permutation_null(self.maps(leaky=True), FULL, S, n_perm=200, seed=1)
        self.assertEqual((r["n"], r["n_excluded"], r["n_perm"]), (6, 0, 200))
        self.assertAlmostEqual(r["observed_mean"], r["null_mean"])
        self.assertAlmostEqual(r["null_p95"], r["observed_mean"])
        self.assertEqual(r["p"], 1.0)
        self.assertFalse(r["observed_exceeds_null_p95"])

    def test_leaks_beyond_the_marginal_wished_rate(self):
        r = status_permutation_null(self.maps(leaky=True, extra_wished=3), FULL, S, n_perm=300, seed=1)
        self.assertAlmostEqual(r["observed_mean"], 1.0)
        self.assertLess(r["null_mean"], 0.5)
        self.assertLess(r["null_p95"], r["observed_mean"])
        self.assertLess(r["p"], 0.01)
        self.assertGreaterEqual(r["p"], 1 / 301)               # smoothed: never exactly 0
        self.assertTrue(r["observed_exceeds_null_p95"])

    def test_aligned_ai_is_below_the_null(self):
        # the AI draws the wished node as wished (and nothing else): observed 0, the null leaks at random
        r = status_permutation_null(self.maps(extra_wished=0), FULL, S, n_perm=300, seed=1)
        self.assertAlmostEqual(r["observed_mean"], 0.0)
        self.assertFalse(r["observed_exceeds_null_p95"])
        self.assertGreater(r["p"], 0.9)

    def test_deterministic_seeded_and_untouched_global_random(self):
        ps = self.maps(leaky=True, extra_wished=3)
        self.assertEqual(status_permutation_null(ps, FULL, S, n_perm=50, seed=3),
                         status_permutation_null(ps, FULL, S, n_perm=50, seed=3))
        random.seed(5)
        expected = random.random()
        random.seed(5)
        status_permutation_null(ps, FULL, S, n_perm=20, seed=3)
        self.assertEqual(random.random(), expected)

    def test_matches_absences_and_excludes_like_the_paired_summary(self):
        ps = self.maps(4, leaky=True, extra_wished=3)
        ps[1] = replace(ps[1], ai={**ps[1].ai, FULL: None})
        ps[2].endorsed.nodes[1].status = "experienced"             # no wished node left
        r = status_permutation_null(ps, FULL, S, n_perm=20, seed=1)
        self.assertEqual((r["n"], r["n_excluded"], r["excluded_reasons"]),
                         (2, 2, {"ai_failed": 1, "no_wished_in_transcript": 1}))
        ps2 = self.maps(3, leaky=True)
        ps2[0].in_transcript.update({"e0": False, "e2": False})      # wished node left, no control node in the transcript
        r2 = status_permutation_null(ps2, FULL, S, n_perm=10)
        self.assertEqual(r2["excluded_reasons"], {"no_control_nodes": 1})
        empty = status_permutation_null([], FULL, S, n_perm=5)
        self.assertTrue(math.isnan(empty["observed_mean"]))
        self.assertFalse(empty["observed_exceeds_null_p95"])


class TestSignStability(unittest.TestCase):
    def test_cells_need_enough_maps(self):
        vals = {"p": 0.2, "a": 0.1, "b": 0.3}
        r = sign_stability(vals, {"p": 10, "a": 4, "b": 6}, "p")          # needs max(3, 5) = 5 maps
        self.assertEqual((r["n_low_n"], r["n_nan"], r["min_cell_n"], r["min_n_required"]), (1, 0, 4, 5))
        self.assertAlmostEqual(r["coverage"], 2 / 3)
        self.assertTrue(r["stable"])                                       # the valid cells agree ...
        self.assertFalse(claim_stable(r))                                  # ... but a claim needs every cell
        floor = sign_stability(vals, {"p": 4, "a": 3, "b": 2}, "p")        # max(3, 2) = 3: the floor
        self.assertEqual((floor["n_low_n"], floor["min_n_required"]), (1, 3))
        ok = sign_stability(vals, {"p": 4, "a": 3, "b": 3}, "p")
        self.assertTrue(claim_stable(ok))
        self.assertEqual(ok["min_cell_n"], 3)
        self.assertIsNone(sign_stability(vals)["min_cell_n"])              # without n: no threshold
        # a cell with no value is nan, not low-n
        nan = sign_stability({"p": 0.2, "a": math.nan}, {"p": 10, "a": 0}, "p")
        self.assertEqual((nan["n_nan"], nan["n_low_n"]), (1, 0))

    def test_sign_stability(self):
        r = sign_stability({"a": 0.1, "b": 0.2})
        self.assertIs(r["stable"], True)
        self.assertEqual((r["n_pos"], r["n_neg"], r["min"], r["max"]), (2, 0, 0.1, 0.2))
        self.assertIs(sign_stability({"a": 0.1, "b": -0.2})["stable"], False)
        self.assertIs(sign_stability({"a": -0.1, "b": -0.2})["stable"], True)
        self.assertIs(sign_stability({"a": 0.1, "b": 0.0})["stable"], False)      # zero is not a sign
        self.assertIs(sign_stability({})["stable"], False)
        r = sign_stability({"a": 0.1, "b": math.nan})
        self.assertEqual((r["n_nan"], r["stable"], r["coverage"], r["n_cells"]), (1, True, 0.5, 2))
        self.assertFalse(claim_stable(r))                              # a claim needs every cell
        self.assertTrue(claim_stable(sign_stability({"a": 0.1, "b": 0.3})))
        self.assertFalse(claim_stable(sign_stability({"a": -0.1, "b": -0.3})))   # "above zero" claims only
        self.assertFalse(claim_stable(sign_stability({})))


# ----------------------------------------------------------------------------- headline

QUOTE = "I went to the office and filed the form last week"
WISH_QUOTE = "I wish there was a single contact person for all of this"
CAPTION = "generic waiting list queue"


def participant(i, *, leaky=False, a1_words=12, judge_wrong=False, with_quotes=True, var_unlike=False,
                ai_caption=True, endorsed_caption=False, caption_off_record=False,
                leak_wish_quote=False, extra_wished=0, a1_modality=None):
    """Endorsed map: e0, e1 (wished), e2 in transcript, e3 off the record. Specs: full (+ a script-ish
    node), removed (no wished node), variation, and a prior-only map with script labels."""
    e = MapGraph(f"E{i}", "endorsed",
                 [Node("e0", label(i, 0)), Node("e1", label(i, 1), status="wished"), Node("e2", label(i, 2)),
                  Node("e3", label(i, 3))],
                 [Edge("e0", "e1"), Edge("e1", "e2"), Edge("e2", "e3")], interviewer="I1" if i % 2 else "I2")
    in_t = {"e0": True, "e1": True, "e2": True, "e3": False}
    if endorsed_caption:                                   # the participant themself drew a script-like node
        e.nodes.append(Node("e4", CAPTION))
        in_t["e4"] = not caption_off_record

    def full_nodes(leaky=leaky, caption=True):
        a1_status = "experienced" if leaky else "wished"
        a1_mod = a1_modality or ("actual" if (leaky or judge_wrong) else "wished")
        q1 = QUOTE if (leaky and not leak_wish_quote) else WISH_QUOTE
        q = (lambda s: s) if with_quotes else (lambda s: None)
        extras = [(Node(f"x{k}", f"zx{i:02d}{k:02d} zy{i:02d}{k:02d} zz{i:02d}{k:02d}", status="wished",
                        quote=q(WISH_QUOTE)), "wished", prov()) for k in range(extra_wished)]
        return extras + [(Node("a0", e.nodes[0].text, quote=q(QUOTE)), "actual", prov()),
                (Node("a1", e.nodes[1].text, status=a1_status, quote=q(q1)), a1_mod, prov(words=a1_words)),
                (Node("a2", e.nodes[2].text, quote=q(QUOTE)), "actual", prov()),
                (Node("a3", CAPTION, quote=q(QUOTE)), "actual", prov())][:4 if caption else 3]

    def build(nodes, spec):
        g = MapGraph(f"A{i}", "ai", [n for n, _, _ in nodes],
                     [Edge(a.node_id, b.node_id) for (a, _, _), (b, _, _) in zip(nodes, nodes[1:])])
        return g, {n.node_id: pr for n, _, pr in nodes}, {n.node_id: m for n, m, _ in nodes}

    ai, pv, md = {}, {}, {}
    ai[FULL], pv[FULL], md[FULL] = build(full_nodes(caption=ai_caption), FULL)
    # var_unlike: the variation spec neither leaks nor sits on the script, so the effects have no stable sign
    ai[VAR], pv[VAR], md[VAR] = build(full_nodes(False, False) if var_unlike else full_nodes(caption=ai_caption), VAR)
    removed = [t for t in full_nodes(caption=ai_caption) if t[0].node_id != "a1"]
    ai[REMOVED], pv[REMOVED], md[REMOVED] = build(removed, REMOVED)
    ai[PRIOR] = MapGraph(f"PR{i}", "prior_only", [Node("p0", CAPTION), Node("p1", "second generic counter desk")])
    return PairInputs(
        pid=f"P{i}", role="r1", interviewer=e.interviewer, endorsed=e, ai=ai, prov=pv, modality=md,
        in_transcript=in_t,
        judged_ai_vs_endorsed={FULL: {}, VAR: {}, REMOVED: {}}, judged_prior_vs_endorsed={},
        judged_ai_vs_script={FULL: {}, VAR: {}, REMOVED: {}}, judged_endorsed_vs_script={})


def run_headline(ps, **kw):
    kw.setdefault("B", 200)
    return headline(ps, FULL, [VAR], list(SETTINGS_GRID), **kw)


class TestHeadline(unittest.TestCase):
    def test_structure_and_json_safe(self):
        ps = [participant(i) for i in range(6)]
        h = run_headline(ps)
        for key in ("absences", "pull_toward_script", "homogenization", "linearization", "leakage",
                    "controls", "prior_only_baseline", "quote_cutoff_sensitivity"):
            self.assertIn(key, h)
        self.assertEqual(h["n_participants"], 6)
        json.dumps(h, allow_nan=False)                              # no nan / inf survives
        self.assertEqual(h["modality_source"], "judge")
        self.assertEqual(h, run_headline(ps))                       # deterministic
        self.assertEqual(set(h["quote_cutoff_sensitivity"]), {"5", "8", "12"})

    def test_pull_toward_script(self):
        h = run_headline([self.full_canon(participant(i)) for i in range(6)])
        p = h["pull_toward_script"]
        # AI has 1 of 4 nodes on the script, endorsed 0 of 4: d = .25 for everyone
        self.assertAlmostEqual(p["mean"], 0.25)
        self.assertGreater(p["ci_lo"], 0)
        self.assertTrue(p["sign_stability"]["stable"])
        self.assertEqual(p["sign_stability"]["n_pos"], 8 * 2)       # 8 settings x 2 specs
        self.assertTrue(p["claim_pulled_toward_script"])
        self.assertTrue(p["in_transcript_only"]["claim_pulled_toward_script"])
        self.assertEqual((p["label_source"], p["canonical_used"], p["judge_channel_canonical"]),
                         ("canonical", True, True))
        self.assertEqual((p["fallback_reasons"], p["missing_prior_samples"]), ([], []))
        self.assertEqual((p["n"], p["n_excluded"]), (6, 0))
        self.assertAlmostEqual(p["in_transcript_only"]["mean"], 0.25)   # endorsed in-transcript: 0 of 3

    def test_no_canonical_inputs_means_raw_fallback_and_no_claim(self):
        p = run_headline([participant(i) for i in range(6)])["pull_toward_script"]
        self.assertEqual(p["label_source"], "raw_fallback")
        self.assertFalse((p["canonical_used"], p["judge_channel_canonical"]) == (True, True))
        self.assertGreater(p["ci_lo"], 0)                                  # the numbers would have said "pulled" ...
        self.assertTrue(p["sign_stability"]["stable"])
        self.assertFalse(p["claim_pulled_toward_script"])                 # ... but the path is not canonical
        self.assertFalse(p["in_transcript_only"]["claim_pulled_toward_script"])
        self.assertIn("canon_endorsed_missing", p["fallback_reasons"])
        self.assertIn(f"canon_ai_missing:{FULL}", p["fallback_reasons"])
        self.assertIn("canon_prior_missing", p["fallback_reasons"])
        self.assertIn("judged_endorsed_vs_script_canon_missing", p["fallback_reasons"])
        self.assertEqual(p["fallback_counts"]["canon_endorsed_missing"], 6)
        self.assertEqual(len(p["missing_prior_samples"]), 6)

    def test_canonical_ai_side_with_endorsed_side_missing_is_mixed(self):
        ps = [replace(self.full_canon(participant(i)), canon_endorsed=None) for i in range(6)]
        p = run_headline(ps)["pull_toward_script"]
        self.assertEqual(p["label_source"], "mixed")
        self.assertFalse(p["canonical_used"])
        self.assertTrue(p["judge_channel_canonical"])
        self.assertEqual(p["fallback_reasons"], ["canon_endorsed_missing"])
        self.assertFalse(p["claim_pulled_toward_script"])
        self.assertGreater(p["ci_lo"], 0)

    def test_none_alignment_is_missing_but_empty_alignment_is_supplied(self):
        none_end = [replace(self.full_canon(participant(i)), judged_endorsed_vs_script_canon=None) for i in range(6)]
        p = run_headline(none_end)["pull_toward_script"]
        self.assertFalse(p["judge_channel_canonical"])
        self.assertTrue(p["canonical_used"])
        self.assertEqual((p["label_source"], p["claim_pulled_toward_script"]), ("mixed", False))
        self.assertEqual(p["fallback_reasons"], ["judged_endorsed_vs_script_canon_missing"])
        empty = run_headline([self.full_canon(participant(i)) for i in range(6)])["pull_toward_script"]
        self.assertTrue(empty["judge_channel_canonical"])                  # {} = supplied, nothing matched
        self.assertTrue(empty["claim_pulled_toward_script"])

    def test_missing_canonical_labels_for_a_variation_spec_block_the_claim(self):
        ps = []
        for i in range(6):
            p = self.full_canon(participant(i))
            ps.append(replace(p, canon_ai={FULL: p.canon_ai[FULL], REMOVED: p.canon_ai[REMOVED]}))   # no VAR
        r = run_headline(ps)["pull_toward_script"]
        self.assertFalse(r["canonical_used"])
        self.assertIn(f"canon_ai_missing:{VAR}", r["fallback_reasons"])
        self.assertNotIn(f"canon_ai_missing:{FULL}", r["fallback_reasons"])
        self.assertFalse(r["claim_pulled_toward_script"])
        self.assertEqual(r["label_source"], "mixed")
        ps2 = [self.full_canon(participant(i)) for i in range(6)]
        ps2 = [replace(p, judged_ai_vs_script_canon={FULL: {}}) for p in ps2]      # alignments: no VAR either
        r2 = run_headline(ps2)["pull_toward_script"]
        self.assertFalse(r2["judge_channel_canonical"])
        self.assertIn(f"judged_ai_vs_script_canon_missing:{VAR}", r2["fallback_reasons"])
        self.assertFalse(r2["claim_pulled_toward_script"])

    def test_failed_ai_map_needs_no_canonical_labels(self):
        ps = [self.full_canon(participant(i)) for i in range(7)]
        p = ps[6]
        ps[6] = replace(p, ai={**p.ai, VAR: None}, canon_ai={FULL: p.canon_ai[FULL], REMOVED: p.canon_ai[REMOVED]},
                        judged_ai_vs_script_canon={FULL: {}, REMOVED: {}})
        r = run_headline(ps)["pull_toward_script"]
        self.assertEqual((r["canonical_used"], r["judge_channel_canonical"]), (True, True))
        self.assertTrue(r["claim_pulled_toward_script"])

    def test_one_prior_sample_without_canonical_labels_clears_the_flag(self):
        ps = [self.full_canon(participant(i)) for i in range(6)]
        ps[3] = replace(ps[3], canon_prior=None)
        r = run_headline(ps)["pull_toward_script"]
        self.assertFalse(r["canonical_used"])
        self.assertEqual(r["missing_prior_samples"], [f"P{3}:{PRIOR}"])
        self.assertEqual(r["fallback_counts"], {"canon_prior_missing": 1})
        self.assertFalse(r["claim_pulled_toward_script"])
        self.assertEqual(r["label_source"], "mixed")

    def test_scripts_by_role_reports_samples_without_canonical_labels(self):
        ps = [self.full_canon(participant(i)) for i in range(3)]
        ps[1] = replace(ps[1], canon_prior=None)
        missing = []
        canon = scripts_by_role(ps, canonical=True, missing=missing)
        self.assertEqual(missing, [f"P{1}:{PRIOR}"])
        self.assertEqual(len(canon["r1"].nodes), 2)           # the script is still built (raw text for that sample)
        none = []
        scripts_by_role(ps, canonical=False, missing=none)
        self.assertEqual(none, [])                             # raw scripts report nothing

    def test_absences_perfect_and_leaky(self):
        h = run_headline([participant(i) for i in range(6)])
        a = h["absences"]
        self.assertEqual(a["pooled"]["recovered_as_wished"], 6)
        self.assertEqual(a["pooled"]["leaked_as_existing"], 0)
        self.assertNotIn("claim_counterfactual_leakage", a)          # F2: that name is reserved for the leak split
        hl = run_headline([participant(i, leaky=True, extra_wished=3) for i in range(6)])
        a = hl["absences"]
        self.assertEqual(a["pooled"]["leaked_as_existing"], 6)
        self.assertEqual(a["pooled"]["leak_rate"], 1.0)
        self.assertEqual((a["pooled"]["leaked_quote_actual"], a["pooled"]["leaked_quote_wished"]), (6, 0))
        self.assertEqual(a["pooled"]["leak_quote_wished_rate"], 0.0)
        self.assertAlmostEqual(a["paired_diff_quote_wished"]["mean"], 0.0)
        self.assertAlmostEqual(a["paired_diff"]["mean"], 1.0)
        self.assertGreater(a["paired_diff"]["ci_lo"], 0)
        self.assertTrue(a["sign_stability"]["stable"])
        self.assertTrue(claim_stable(a["sign_stability"]))

    def test_claims_need_sign_stability(self):
        ps = [participant(i, leaky=True, var_unlike=True) for i in range(6)]
        h = run_headline(ps)
        self.assertGreater(h["absences"]["paired_diff"]["ci_lo"], 0)         # the primary spec alone leaks
        self.assertFalse(h["absences"]["sign_stability"]["stable"])
        self.assertGreater(h["pull_toward_script"]["ci_lo"], 0)
        self.assertFalse(h["pull_toward_script"]["sign_stability"]["stable"])
        self.assertFalse(h["pull_toward_script"]["claim_pulled_toward_script"])

    def test_pull_in_transcript_only_drops_off_record_endorsed_nodes(self):
        # the endorsed map holds a script-like node that is off the record: counted in the main d (5 nodes,
        # .25 - .2), left out of the robustness d (4 in-transcript nodes, none on the script)
        ps = [participant(i, endorsed_caption=True, caption_off_record=True) for i in range(6)]
        p = run_headline(ps)["pull_toward_script"]
        self.assertAlmostEqual(p["mean"], 0.25 - 0.2)
        self.assertAlmostEqual(p["in_transcript_only"]["mean"], 0.25)

    PRIOR_LABELS = [CAPTION, "second generic counter desk"]

    @staticmethod
    def full_canon(p):
        """Every canonical input supplied (labels = raw text, alignments = none matched)."""
        return replace(p, canon_endorsed=[n.text for n in p.endorsed.nodes],
                       canon_ai={s: [n.text for n in g.nodes] for s, g in p.ai.items()
                                 if g is not None and not s.startswith("prior_only")},
                       canon_prior={s: [n.text for n in g.nodes] for s, g in p.ai.items()
                                    if g is not None and s.startswith("prior_only")},
                       judged_ai_vs_script_canon={FULL: {}, VAR: {}, REMOVED: {}},
                       judged_endorsed_vs_script_canon={})

    @staticmethod
    def canon(p, ai=None, end=None, prior=None):
        return replace(p, canon_ai={FULL: list(ai), VAR: list(ai)} if ai else None, canon_endorsed=end,
                       canon_prior={PRIOR: list(prior)} if prior else None)

    def test_primary_pull_uses_canonical_labels(self):
        # raw wording of the AI map sits on the script (same model wording as the prior-only runs), but the
        # canonical labels of all three sources do not: the primary estimate has no pull, the raw line does
        ps = []
        for i in range(6):
            p = participant(i)
            ai = [n.text for n in p.ai[FULL].nodes]
            ai[3] = "unrelated canonical concept"
            ps.append(self.canon(p, ai, [n.text for n in p.endorsed.nodes], self.PRIOR_LABELS))
        pull = run_headline(ps)["pull_toward_script"]
        self.assertEqual(pull["label_source"], "mixed")                 # labels canonical, alignments not supplied
        self.assertTrue(pull["canonical_used"])
        self.assertFalse(pull["judge_channel_canonical"])
        self.assertAlmostEqual(pull["mean"], 0.0)
        self.assertFalse(pull["claim_pulled_toward_script"])
        self.assertAlmostEqual(pull["robustness"]["raw_labels"]["mean"], 0.25)
        self.assertAlmostEqual(pull["robustness"]["judge_only"]["mean"], 0.0)

    def test_primary_pull_canonicalizes_the_endorsed_map_too(self):
        # the participant's own wording becomes the script concept once canonicalized: the AI's lead disappears
        ps = []
        for i in range(6):
            p = participant(i)
            end = [n.text for n in p.endorsed.nodes]
            end[0] = CAPTION
            ps.append(self.canon(p, [n.text for n in p.ai[FULL].nodes], end, self.PRIOR_LABELS))
        pull = run_headline(ps)["pull_toward_script"]
        self.assertAlmostEqual(pull["mean"], 0.25 - 0.25)
        self.assertAlmostEqual(pull["robustness"]["raw_labels"]["mean"], 0.25)

    def test_primary_pull_script_is_built_from_canonical_prior_labels(self):
        ps = []
        for i in range(6):
            p = participant(i, ai_caption=False)
            ai = ["canon thing alpha"] + [n.text for n in p.ai[FULL].nodes][1:]
            p = self.canon(p, ai, [n.text for n in p.endorsed.nodes], ["canon thing alpha", "canon thing beta"])
            ps.append(replace(p, judged_ai_vs_script_canon={FULL: {}, VAR: {}, REMOVED: {}},
                              judged_endorsed_vs_script_canon={}))
        pull = run_headline(ps)["pull_toward_script"]
        self.assertAlmostEqual(pull["mean"], 1 / 3)
        self.assertTrue(pull["claim_pulled_toward_script"])
        self.assertEqual((pull["label_source"], pull["fallback_reasons"]), ("canonical", []))
        self.assertAlmostEqual(pull["robustness"]["raw_labels"]["mean"], 0.0)

    def test_primary_pull_uses_canonical_alignments(self):
        ps = []
        for i in range(6):
            p = participant(i)
            ai = [f"unrelated concept {k}" for k in range(4)]
            p = self.canon(p, ai, [n.text for n in p.endorsed.nodes], self.PRIOR_LABELS)
            ps.append(replace(p, judged_ai_vs_script_canon={FULL: {(0, 0): "same"}, VAR: {(0, 0): "same"}},
                              judged_endorsed_vs_script_canon={}))
        pull = run_headline(ps)["pull_toward_script"]
        self.assertTrue(pull["judge_channel_canonical"])
        self.assertAlmostEqual(pull["mean"], 0.25)
        self.assertTrue(pull["claim_pulled_toward_script"])
        # the same alignment for the endorsed map's first node cancels the lead
        both = [replace(p, judged_endorsed_vs_script_canon={(0, 0): "same"}) for p in ps]
        self.assertAlmostEqual(run_headline(both)["pull_toward_script"]["mean"], 0.0)

    def test_judge_only_line_switches_the_lexical_channel_off(self):
        ps = []
        for i in range(6):
            p = participant(i)
            for spec in (FULL, VAR):
                p.ai[spec].nodes[3].text = "unrelated raw words"
            ps.append(replace(p, judged_ai_vs_script={FULL: {(3, 0): "same"}, VAR: {(3, 0): "same"}, REMOVED: {}}))
        pull = run_headline(ps)["pull_toward_script"]
        self.assertAlmostEqual(pull["robustness"]["judge_only"]["mean"], 0.25)     # only the judge matches it
        self.assertAlmostEqual(pull["robustness"]["raw_labels"]["mean"], 0.25)
        self.assertAlmostEqual(pull["mean"], 0.0)                                  # canonical, no canonical judge
        self.assertFalse(pull["canonical_used"])                                   # raw text stood in
        plain = run_headline([participant(i) for i in range(6)])["pull_toward_script"]
        self.assertAlmostEqual(plain["robustness"]["judge_only"]["mean"], 0.0)     # lexical-only match is switched off
        self.assertAlmostEqual(plain["robustness"]["raw_labels"]["mean"], 0.25)

    def test_pull_claim_needs_ci_above_zero(self):
        # d = +.25 for three participants, -.2 for two (the endorsed map holds a script-like node), 0 for one:
        # the mean is positive in every setting, but the bootstrap CI reaches 0
        ps = ([participant(i) for i in range(3)]
              + [participant(i, ai_caption=False, endorsed_caption=True) for i in range(3, 5)]
              + [participant(5, ai_caption=False)])
        p = run_headline(ps)["pull_toward_script"]
        self.assertGreater(p["mean"], 0)
        self.assertTrue(p["sign_stability"]["stable"])
        self.assertLessEqual(p["ci_lo"], 0)
        self.assertFalse(p["claim_pulled_toward_script"])

    def test_never_wished_ai_equals_the_old_null(self):
        # an AI that never draws anything as wished renders the ideals as existing; the old status-permutation null
        # (now only the "worse_than_chance_check") cannot tell that from any permutation of its statuses
        h = run_headline([participant(i, leaky=True) for i in range(6)])
        a = h["absences"]
        self.assertGreater(a["paired_diff"]["ci_lo"], 0)
        self.assertTrue(claim_stable(a["sign_stability"]))
        m = a["worse_than_chance_check"]
        self.assertAlmostEqual(m["observed_mean_diff"], 1.0)
        self.assertAlmostEqual(m["null_mean_diff"], 1.0)
        self.assertAlmostEqual(m["null_p95"], 1.0)
        self.assertEqual(m["p"], 1.0)
        self.assertFalse(m["observed_exceeds_null_p95"])
        self.assertEqual(m["wished_rate_q"], {"mean": 0.0, "min": 0.0, "max": 0.0})
        self.assertEqual((m["n"], m["n_excluded"]), (6, 0))

    def test_misplaced_wished_statuses_exceed_the_old_null(self):
        # the AI draws wished statuses, but on nodes that are not the endorsed wished one: worse than chance
        h = run_headline([participant(i, leaky=True, extra_wished=3) for i in range(6)])
        m = h["absences"]["worse_than_chance_check"]
        self.assertTrue(m["observed_exceeds_null_p95"])
        self.assertAlmostEqual(m["observed_mean_diff"], 1.0)
        self.assertLess(m["null_p95"], 1.0)
        self.assertLess(m["p"], 0.01)
        self.assertAlmostEqual(m["wished_rate_q"]["mean"], 3 / 7)
        self.assertEqual((m["wished_rate_q"]["min"], m["wished_rate_q"]["max"]), (3 / 7, 3 / 7))

    def test_perfect_ai_leaks_nothing(self):
        a = run_headline([participant(i) for i in range(6)])["absences"]
        self.assertAlmostEqual(a["worse_than_chance_check"]["observed_mean_diff"], 0.0)
        self.assertEqual(a["pooled"]["leaked_as_existing"], 0)

    def test_old_null_reports_variable_wished_rates_as_a_range(self):
        ps = [participant(i, leaky=True, extra_wished=(0 if i < 3 else 2)) for i in range(6)]
        q = run_headline(ps)["absences"]["worse_than_chance_check"]["wished_rate_q"]
        self.assertEqual((q["min"], q["max"]), (0.0, 2 / 6))
        self.assertAlmostEqual(q["mean"], (3 * 0.0 + 3 * 2 / 6) / 6)

    def test_failed_variation_spec_blocks_the_claims(self):
        ps = []
        for i in range(6):
            p = participant(i, leaky=True, endorsed_caption=False)
            ps.append(replace(p, ai={**p.ai, VAR: None}))
        h = run_headline(ps)
        a, pull = h["absences"], h["pull_toward_script"]
        self.assertGreater(a["paired_diff"]["ci_lo"], 0)
        self.assertEqual(a["sign_stability"]["coverage"], 0.5)           # the VAR cells are all nan
        self.assertEqual((a["sign_stability"]["n_cells"], a["sign_stability"]["n_nan"]), (16, 8))
        self.assertTrue(a["sign_stability"]["stable"])                   # nan cells are ignored by "stable" ...
        self.assertFalse(claim_stable(a["sign_stability"]))              # ... but a claim needs full coverage
        self.assertGreater(pull["ci_lo"], 0)
        self.assertEqual(pull["sign_stability"]["coverage"], 0.5)
        self.assertFalse(pull["claim_pulled_toward_script"])
        self.assertFalse(pull["in_transcript_only"]["claim_pulled_toward_script"])

    def test_thin_variation_spec_blocks_the_claims(self):
        ps = []
        for i in range(6):
            p = participant(i, leaky=True, extra_wished=3)
            ps.append(p if i < 2 else replace(p, ai={**p.ai, VAR: None}))     # VAR cells rest on 2 maps only
        h = run_headline(ps)
        a = h["absences"]
        st = a["sign_stability"]
        self.assertEqual((st["n_nan"], st["n_low_n"], st["min_cell_n"], st["min_n_required"]), (0, 8, 2, 3))
        self.assertEqual(st["coverage"], 0.5)
        self.assertGreater(a["paired_diff"]["ci_lo"], 0)
        self.assertFalse(claim_stable(st))
        self.assertEqual(h["pull_toward_script"]["sign_stability"]["min_cell_n"], 2)
        self.assertFalse(h["pull_toward_script"]["claim_pulled_toward_script"])
        self.assertEqual(h["homogenization"]["sign_stability"]["concept_diff_A_minus_B"]["min_cell_n"], 2)
        self.assertEqual(h["linearization"]["sign_stability"]["n_nodes"]["min_cell_n"], 2)

    def test_primary_setting_must_be_in_settings(self):
        ps = [participant(i) for i in range(4)]
        with self.assertRaises(ValueError):
            headline(ps, FULL, [VAR], [MatchSetting(0.4, "same")], B=50)

    def test_stability_grids_for_leak_rate_diff_and_quote_wished(self):
        h = run_headline([participant(i, leaky=True, leak_wish_quote=True, a1_modality="wished") for i in range(6)])
        st = h["leakage"]["leak_rate_diff"]["sign_stability"]
        self.assertEqual((st["n_cells"], st["n_pos"]), (16, 8))          # 8 settings x [FULL, VAR]; VAR has no removed twin
        self.assertEqual(st["n_nan"], 8)
        qs = h["absences"]["paired_diff_quote_wished"]["sign_stability"]
        self.assertEqual((qs["n_cells"], qs["n_pos"], qs["n_nan"]), (16, 16, 0))
        self.assertAlmostEqual(qs["min"], 1.0)

    def test_linearization_stability_for_all_edges(self):
        ps = [participant(i) for i in range(6)]
        for p in ps:
            for spec in (FULL, VAR):
                p.ai[spec].edges[0].glued = False                # AI: 2 glued edges of 3; endorsed: 3 of 3
        lin = run_headline(ps)["linearization"]
        self.assertEqual(lin["sign_stability"]["n_edges"]["max"], -1.0)             # glued edges only
        self.assertEqual(lin["sign_stability_all_edges"]["n_edges"]["max"], 0.0)    # all edges
        self.assertEqual(set(lin["sign_stability"]), set(lin["sign_stability_all_edges"]))

    def test_shuffled_pairs_stability_cells_match_the_primary_block(self):
        shared = "shared common alpha"
        ps = [participant(i) for i in range(12)]
        for p in ps[:5]:                                         # only some endorsed maps can be hit by chance
            p.endorsed.nodes[0].text = shared
            p.ai[FULL].nodes[0].text = shared
        for nd in (2, 7, 20):
            h = headline(ps, FULL, [], [PRIMARY_SETTING], B=50, n_derangements=nd)
            sp = h["controls"]["shuffled_pairs"]
            self.assertEqual(sp["n_derangements"], nd)
            self.assertLess(sp["shuffled_recall_mean"], sp["real_recall_mean"])
            self.assertGreater(sp["shuffled_recall_mean"], 0)
            st = sp["sign_stability"]
            self.assertEqual(st["n_cells"], 1)
            self.assertAlmostEqual(st["min"], sp["mean"])

    def test_exclusions_carry_reasons(self):
        ps = [participant(i) for i in range(6)]
        ps[2] = replace(ps[2], ai={**ps[2].ai, FULL: None})
        h = run_headline(ps)
        for block in (h["absences"]["paired_diff"], h["pull_toward_script"], h["linearization"]["glued"]):
            self.assertEqual((block["n"], block["n_excluded"]), (5, 1))
            self.assertEqual(block["excluded_reasons"], {"ai_failed": 1})
        self.assertEqual(h["n_participants"], 6)

    def test_no_wished_nodes_excluded_from_absences(self):
        ps = [participant(i) for i in range(4)]
        e = ps[0].endorsed
        e.nodes[1].status = "experienced"
        h = run_headline(ps)
        self.assertEqual(h["absences"]["paired_diff"]["n_excluded"], 1)
        self.assertEqual(h["absences"]["paired_diff"]["excluded_reasons"], {"no_wished_in_transcript": 1})

    def test_quote_cutoff_sensitivity_on_leaks(self):
        ps = [participant(i, leaky=True, a1_words=6) for i in range(6)]
        s = run_headline(ps)["quote_cutoff_sensitivity"]
        self.assertEqual(s["5"]["absences"]["leaked_as_existing"], 6)
        self.assertEqual(s["8"]["absences"]["leaked_as_existing"], 0)
        self.assertEqual(s["8"]["absences"]["leaked_unanchored"], 6)
        self.assertEqual(s["12"]["absences"]["leaked_unanchored"], 6)

    def test_quote_cutoff_sensitivity(self):
        ps = [participant(i, a1_words=6) for i in range(6)]
        s = run_headline(ps)["quote_cutoff_sensitivity"]
        self.assertEqual(s["5"]["absences"]["recovered_as_wished"], 6)
        self.assertEqual(s["8"]["absences"]["recovered_as_wished"], 0)
        self.assertEqual(s["12"]["absences"]["recovered_as_wished"], 0)
        self.assertEqual(s["8"]["absences"]["unanchored_wished"], 6)      # not located at 8: neither leak nor recovery
        self.assertEqual(s["8"]["absences"]["missed"], 0)
        self.assertEqual(s["5"]["absences"]["unanchored_wished"], 0)
        self.assertEqual(s["5"]["buckets"]["unanchored"], 0)
        self.assertEqual(s["8"]["buckets"]["unanchored"], 6)
        self.assertEqual(s["8"]["buckets"]["n"], 6)
        self.assertEqual(s["8"]["absences"]["n"], 6)
        self.assertEqual(s["8"]["absences"]["n_excluded"], 0)

    def test_keyword_arm_is_no_ai(self):
        # the AI draws the wished node as experienced and quotes "I wish ..." for it; the judge labels that
        # quote "actual" (wrong), the keyword cue "I wish" says wished
        ps = [participant(i, leaky=True, judge_wrong=True, leak_wish_quote=True) for i in range(6)]
        judge = run_headline(ps)["absences"]["pooled"]
        kw_h = run_headline(ps, modality_source="keyword")
        kw = kw_h["absences"]["pooled"]
        self.assertEqual((judge["leaked_as_existing"], judge["leaked_quote_actual"], judge["leaked_quote_wished"]),
                         (6, 6, 0))
        self.assertEqual((kw["leaked_as_existing"], kw["leaked_quote_actual"], kw["leaked_quote_wished"]), (6, 0, 6))
        self.assertEqual(kw["leak_quote_wished_rate"], 1.0)
        self.assertEqual(kw_h["modality_source"], "keyword")
        # swap helper rebuilt modality from the quotes alone
        kp = with_keyword_modality(ps[0])
        self.assertEqual(kp.modality[FULL]["a1"], "wished")
        self.assertEqual(kp.modality[FULL]["a0"], "actual")
        self.assertEqual(ps[0].modality[FULL]["a1"], "actual")          # original untouched

    def test_keyword_arm_needs_quotes(self):
        kp = with_keyword_modality(participant(0, with_quotes=False))
        self.assertEqual(set(kp.modality[FULL].values()), {"unsupported"})

    def test_second_judge_kappa_and_band(self):
        ps = [participant(i, leaky=True) for i in range(6)]
        j2 = []
        for p in ps:
            m = {spec: dict(mm) for spec, mm in p.modality.items()}
            m[FULL]["a1"] = "wished"                  # judge 2 reads the leaked node's quote as wished
            j2.append(replace(p, modality_judge2=m))
        h = run_headline(j2)
        sj = h["second_judge"]
        self.assertLess(sj["cohen_kappa"], 1.0)
        self.assertEqual(sj["n_participants"], 6)
        # per participant: 2 specs (full, variation) x 4 nodes + removed spec skipped = 8 labels, the 6 flips
        # are the full spec's a1 only -> 1 of 8 labels per participant differs
        self.assertEqual(sj["n_labels"], 6 * 8)
        self.assertAlmostEqual(sj["agreement"], 7 / 8)
        self.assertEqual(h["absences"]["pooled"]["leaked_quote_actual"], 6)
        self.assertEqual(h["absences"]["pooled"]["leaked_quote_wished"], 0)
        j2h = sj["headline_judge2"]["absences"]["pooled"]
        self.assertEqual((j2h["leaked_quote_wished"], j2h["leaked_quote_actual"], j2h["leaked_as_existing"]), (6, 0, 6))
        self.assertEqual(sj["headline_judge1_same_participants"]["absences"]["pooled"]["leaked_quote_actual"], 6)
        self.assertNotIn("second_judge", run_headline(ps))

    def test_second_judge_partial_coverage(self):
        ps = [participant(i) for i in range(6)]
        ps[0] = replace(ps[0], modality_judge2={s: dict(m) for s, m in ps[0].modality.items()})
        h = run_headline(ps)
        self.assertEqual(h["second_judge"]["n_participants"], 1)
        self.assertEqual(h["second_judge"]["n_participants_without_second_judge"], 5)

    def test_judge2_source_leaves_out_uncovered_participants(self):
        ps = [participant(i, leaky=True) for i in range(4)]
        m = {spec: dict(mm) for spec, mm in ps[0].modality.items()}
        m[FULL]["a1"] = "wished"
        ps[0] = replace(ps[0], modality_judge2=m)
        h = run_headline(ps, modality_source="judge2")
        self.assertEqual((h["n_participants"], h["n_participants_without_second_judge"]), (1, 3))
        self.assertEqual(h["absences"]["pooled"]["leaked_quote_wished"], 1)
        self.assertEqual(h["modality_source"], "judge2")

    def test_stability_cells_use_each_spec(self):
        h = run_headline([participant(i) for i in range(6)])
        hom = h["homogenization"]
        st = hom["sign_stability"]["concept_diff_A_minus_B"]
        self.assertAlmostEqual(st["min"], hom["concept"]["diff"])      # VAR is a copy of FULL
        self.assertAlmostEqual(st["max"], hom["concept"]["diff"])
        lin = h["linearization"]
        self.assertAlmostEqual(lin["sign_stability"]["n_nodes"]["min"], lin["glued"]["measures"]["n_nodes"]["mean_diff"])
        sh = h["controls"]["shuffled_pairs"]["sign_stability"]
        self.assertEqual((sh["n_pos"], sh["n_neg"]), (4 * 2, 0))       # 4 lexical thresholds x 2 specs

    def test_prior_only_baseline(self):
        h = run_headline([participant(i) for i in range(6)])
        b = h["prior_only_baseline"]
        # prior-only matches none of the endorsed in-transcript nodes; AI recovers all three
        self.assertAlmostEqual(b["mean_prior_recall"], 0.0)
        self.assertAlmostEqual(b["mean_ai_recall"], 1.0)
        self.assertAlmostEqual(b["paired_diff"]["mean"], 1.0)
        self.assertTrue(b["sign_stability"]["stable"])

    def test_prior_only_baseline_is_like_for_like(self):
        # the wished node's quote is too short to anchor: the attested AI recall drops, the like-for-like one does not
        b = run_headline([participant(i, a1_words=6) for i in range(6)])["prior_only_baseline"]
        self.assertAlmostEqual(b["mean_ai_recall"], 1.0)                  # any AI node vs any prior node
        self.assertAlmostEqual(b["mean_prior_recall"], 0.0)
        self.assertAlmostEqual(b["attested_line"]["mean_ai_recall"], 2 / 3)
        self.assertAlmostEqual(b["attested_line"]["paired_diff"]["mean"], 2 / 3)
        self.assertTrue(b["attested_line"]["sign_stability"]["stable"])

    def test_prior_only_baseline_counts_unattested_ai_nodes(self):
        ps = [participant(i, leaky=False) for i in range(4)]
        for p in ps:                                       # the judge calls every quote hypothetical: nothing is attested
            for spec in (FULL, VAR):
                p.modality[spec] = {k: "hypothetical" for k in p.modality[spec]}
        b = run_headline(ps)["prior_only_baseline"]
        self.assertAlmostEqual(b["mean_ai_recall"], 1.0)
        self.assertAlmostEqual(b["attested_line"]["mean_ai_recall"], 0.0)

    def test_leakage_block(self):
        h = run_headline([participant(i) for i in range(6)])
        lk = h["leakage"]
        self.assertAlmostEqual(lk["node_recall_diff"]["mean"], 1 / 3)       # full 3/3, removed 2/3
        self.assertTrue(lk["sign_stability"]["stable"])

    def test_headline_leaves_inputs_untouched(self):
        ps = [participant(i) for i in range(4)]
        before = [json.dumps({s: g.to_json() if g else None for s, g in p.ai.items()}) for p in ps]
        run_headline(ps, modality_source="keyword")
        self.assertEqual(before, [json.dumps({s: g.to_json() if g else None for s, g in p.ai.items()}) for p in ps])

    def test_global_random_untouched(self):
        random.seed(99)
        expected = random.random()
        random.seed(99)
        run_headline([participant(i) for i in range(4)])
        self.assertEqual(random.random(), expected)


if __name__ == "__main__":
    unittest.main()
