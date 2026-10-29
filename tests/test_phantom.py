"""Tests of second_scribe/phantom.py (Task 8 Part A; stdlib unittest; synthetic data only; zero model calls).

A port of analysis/tests/test_kd.py (22 tests) retargeted to the kit: known answers for every dose builder on tiny
hand-built maps (scored by the kit's own metrics.buckets / absences), the pass-rule logic, checks against the kit on the
shipped demo run (the oracle replay: the pooling equals pipeline._pooled, the carried-over provenance equals
provenance.provenance_for), and an end-to-end run of the `phantom` command at 2 replicates. Then the Task 8 additions:
the LIMIT in every output, no forbidden word, the detection_limits block and the pinned private signatures.
"""
import copy
import dataclasses
import inspect
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import metrics, phantom as KD, pipeline
from second_scribe import profile_base as pb
from second_scribe import profile_export as pe
from second_scribe import provenance
from second_scribe.cli import DEMO_CONFIG
from second_scribe.match import MatchSetting

REPO = Path(__file__).resolve().parent.parent
S = KD.SPEC
K = KD.K
PS = pb.SETTINGS["primary"]
Node, Edge, MapGraph = metrics.Node, K.schemas.Edge, metrics.MapGraph
Provenance = provenance.Provenance

WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
OTHER = ("kilo", "lima", "mike", "november", "oscar", "papa")

_CACHE = {}


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ss-tests-phantom-"))


def demo_ctx():
    """The shipped demo replayed by the oracle (built once per process), as a ProfileContext."""
    if "ctx" not in _CACHE:
        out = tmpdir() / "demo"
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(out), "--client", "oracle"],
                           cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stdout + r.stderr)
        _CACHE["ctx"] = pb.open_context(Path(DEMO_CONFIG), out)
    return _CACHE["ctx"]


def cli_outputs():
    """The `phantom` command at 2 replicates and B = 200 (run once per process): (out dir, completed process)."""
    if "cli" not in _CACHE:
        out = tmpdir() / "ph"
        p = subprocess.run([sys.executable, "-m", "second_scribe.cli", "phantom", "--reps", "2", "--B", "200",
                            "--out", str(out)], cwd=REPO, capture_output=True, text=True, timeout=1800)
        _CACHE["cli"] = (out, p)
    return _CACHE["cli"]


LAB_B, LAB_SEED, LAB_COMMIT = 200, 12345, "labcommit0123"


def lab_profile():
    """A lab export of the shipped demo run (B 200, seed 12345): (lab context, profile dict, path of the written file).
    Built once per process; tests copy the file before calibrating it."""
    if "lab" not in _CACHE:
        lab = pb.open_context(Path(DEMO_CONFIG), demo_ctx().out_dir, B=LAB_B, seed=LAB_SEED)
        path = tmpdir() / "lab-profile.json"
        pe.write_profile(pe.build_profile(lab), path, forbidden=pe.forbidden_tokens(lab))
        _CACHE["lab"] = (lab, json.loads(path.read_text(encoding="utf-8")), path)
    return _CACHE["lab"]


def copy_of_lab_profile(edit=None) -> Path:
    """A fresh copy of the lab profile file (``edit(dict)`` may change it first); returns the path."""
    lab, prof, _path = lab_profile()
    d = copy.deepcopy(prof)
    if edit is not None:
        edit(d)
    out = tmpdir() / "profile.json"
    out.write_text(json.dumps(d, indent=1, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return out


def run_calibrate_cli(profile_path, *extra):
    lab, _prof, _p = lab_profile()
    return subprocess.run([sys.executable, "-m", "second_scribe.cli", "calibrate", "--config", str(DEMO_CONFIG),
                           "--out-dir", str(lab.out_dir), "--profile", str(profile_path), "--reps", "2", *extra],
                          cwd=REPO, capture_output=True, text=True, timeout=1800)


def calibrated():
    """The calibrate command at 2 replicates on a copy of the lab profile (once per process): (before, after, path)."""
    if "cal" not in _CACHE:
        path = copy_of_lab_profile()
        before = json.loads(path.read_text(encoding="utf-8"))
        p = run_calibrate_cli(path)
        assert p.returncode == 0, p.stdout + p.stderr
        _CACHE["cal"] = (before, json.loads(path.read_text(encoding="utf-8")), path)
    return _CACHE["cal"]


def located(eid: str, kind: str = "node") -> Provenance:
    return Provenance(eid, kind, True, "participant", 1, 1, 20, True, False, "exact")


def tiny(n: int = 4, wished=(), same_labels: bool = False, prior=None):
    """One hand-built participant: a chain of n endorsed nodes e1..en and an AI chain g1..gn, AI node i paired with
    endorsed node i by the judge ("same"); labels share no content word unless ``same_labels``. Nodes in ``wished``
    (1-based) are wished on both sides, quoted as wished. Every quote is located."""
    st = lambda i: "wished" if i in wished else "experienced"
    en = [Node(f"e{i}", f"{WORDS[i - 1]} step", status=st(i)) for i in range(1, n + 1)]
    an = [Node(f"g{i}", f"{(WORDS if same_labels else OTHER)[i - 1]} step", status=st(i), quote=f"quote {i}")
          for i in range(1, n + 1)]
    ee = [Edge(f"e{i}", f"e{i + 1}") for i in range(1, n)]
    ae = [Edge(f"g{i}", f"g{i + 1}", quote=f"edge {i}") for i in range(1, n)]
    g = MapGraph("T1", "ai", an, ae)
    prov = {x.node_id: located(x.node_id) for x in an} | {KD._eid(e): located(KD._eid(e), "edge") for e in ae}
    mod = {x.node_id: ("wished" if x.status == "wished" else "actual") for x in an} | {KD._eid(e): "actual" for e in ae}
    ai = {S: g}
    canon_prior = None
    if prior is not None:
        ai["prior_only__p0__t0.7__r0"] = MapGraph("P", "prior_only", [Node(f"n{j}", t) for j, t in enumerate(prior, 1)])
        canon_prior = {"prior_only__p0__t0.7__r0": [f"C{j}" for j in range(1, len(prior) + 1)]}
    return metrics.PairInputs(
        pid="T1", role="r", interviewer=None, endorsed=MapGraph("T1", "endorsed", en, ee), ai=ai,
        prov={S: prov}, modality={S: mod}, in_transcript={x.node_id: True for x in en},
        judged_ai_vs_endorsed={S: {(i, i): "same" for i in range(n)}}, judged_prior_vs_endorsed=None,
        judged_ai_vs_script={S: {(0, 0): "same"}}, judged_endorsed_vs_script={},
        canon_ai={S: [f"K{i}" for i in range(1, n + 1)]}, canon_endorsed=[f"K{i}" for i in range(1, n + 1)],
        canon_prior=canon_prior, judged_ai_vs_script_canon={S: {(i, 0): "same" for i in range(n)}},
        not_judged={S: set()})


def b(p, setting=PS):
    return metrics.buckets(p, S, KD.ARM, setting, min_words=KD.MW)


def a(p, setting=PS):
    return metrics.absences(p, S, KD.ARM, setting, min_words=KD.MW)


class TinyBaseline(unittest.TestCase):
    def test_oracle_tiny_is_zero_distance(self):
        p = tiny(4, wished=(2,))
        self.assertEqual((b(p)["matched"], b(p)["missed"], b(p)["n_edges_recovered"], b(p)["n_edges_eligible"]),
                         (4, 0, 3, 3))
        self.assertEqual(a(p)["recovered_as_wished"], 1)
        # the labels share no content word: the lexical half alone sees nothing
        self.assertEqual(b(p, pb.SETTINGS["lexical_only_06"])["matched"], 0)


class DoseBuilders(unittest.TestCase):
    def test_d1_drop_half_of_four_leaves_two(self):
        p = tiny(4)
        q = KD.drop_nodes(p, {"g1", "g3"})
        g = q.ai[S]
        self.assertEqual([x.node_id for x in g.nodes], ["g2", "g4"])
        self.assertEqual(g.edges, [])                                  # every edge touched g1 or g3
        self.assertEqual(q.judged_ai_vs_endorsed[S], {(0, 1): "same", (1, 3): "same"})   # re-indexed, not shifted
        self.assertEqual(q.canon_ai[S], ["K2", "K4"])
        self.assertEqual(set(q.prov[S]), {"g2", "g4"})
        self.assertEqual((b(q)["matched"], b(q)["missed"]), (2, 2))
        self.assertEqual(len(p.ai[S].nodes), 4)                         # the original is untouched

    def test_d1_through_apply_dose(self):
        q, info = KD.apply_dose("D1", 0.5, [tiny(4)], random.Random(1), K)
        self.assertEqual(len(q[0].ai[S].nodes), 2)
        self.assertEqual((info["n_pool"], info["n_chosen"], info["realised_share"]), (4, 2, 0.5))

    def test_d2_drop_one_of_three_links(self):
        q = KD.drop_edges(tiny(4), {1})
        self.assertEqual([(e.source, e.target) for e in q.ai[S].edges], [("g1", "g2"), ("g3", "g4")])
        self.assertNotIn("e:g2->g3", q.prov[S])
        self.assertEqual((b(q)["n_edges_recovered"], b(q)["matched"]), (2, 4))

    def test_d3_merge_one_edge(self):
        q, absorbed = KD.merge_edges(tiny(4), {0})
        g = q.ai[S]
        self.assertEqual(absorbed, 1)
        self.assertEqual([x.text for x in g.nodes], ["kilo step and lima step", "mike step", "november step"])
        self.assertEqual([(e.source, e.target) for e in g.edges], [("g1", "g3"), ("g3", "g4")])
        self.assertEqual(q.judged_ai_vs_endorsed[S], {(1, 2): "same", (2, 3): "same"})   # merged box: no pair
        self.assertIn("g1", q.not_judged[S])
        self.assertIn("e:g1->g3", q.not_judged[S])
        self.assertEqual(q.modality[S]["g1"], "unsupported")
        self.assertEqual(q.prov[S]["e:g1->g3"].element_id, "e:g1->g3")
        self.assertEqual(q.canon_ai[S], ["kilo step and lima step", "K3", "K4"])
        bq = b(q)
        self.assertEqual((bq["matched"], bq["missed"], bq["n_ai_nodes"]), (2, 2, 3))   # the merged box matches neither
        self.assertEqual(bq["n_edges_recovered"], 1)                                     # only g3->g4 survives

    def test_d3_merge_chain_and_cycle(self):
        q, absorbed = KD.merge_edges(tiny(4), {0, 1, 2})
        self.assertEqual((absorbed, len(q.ai[S].nodes), q.ai[S].edges), (3, 1, []))
        p = tiny(3)
        p.ai[S].edges.append(Edge("g2", "g1", quote="back"))
        p.prov[S]["e:g2->g1"] = located("e:g2->g1", "edge")
        p.modality[S]["e:g2->g1"] = "actual"
        q, absorbed = KD.merge_edges(p, {0, 2})                         # g1->g2 and g2->g1: one merge, no self-loop
        self.assertEqual(absorbed, 1)
        self.assertEqual([(e.source, e.target) for e in q.ai[S].edges], [("g1", "g3")])

    def test_d3_merge_with_lexical_match(self):
        # with shared labels the merged box keeps a lexical match only if Jaccard >= .6; here 2/4 words: none
        q, _ = KD.merge_edges(tiny(4, same_labels=True), {0})
        self.assertEqual(b(q)["matched"], 2)

    def test_d4_flip_turns_kept_into_counterfactual(self):
        p = tiny(4, wished=(2,))
        self.assertEqual(KD.ideal_partners(p, K, PS), ["g2"])
        q = KD.flip_nodes(p, {"g2"})
        self.assertEqual(q.ai[S].nodes[1].status, "experienced")
        self.assertEqual(q.ai[S].nodes[1].quote, "quote 2")             # the quote is kept
        self.assertEqual(q.modality[S]["g2"], "wished")                 # and so is its modality label
        aq = a(q)
        self.assertEqual((aq["recovered_as_wished"], aq["leaked_as_existing"], aq["counterfactual_leakage"],
                          aq["adequacy_flattening"]), (0, 1, 1, 0))
        self.assertEqual(b(q)["matched"], 3)                            # no longer attested: recall falls (coupled)

    def test_d5_insert_no_transcript_node(self):
        p = tiny(4, prior=["generic step", "other generic"])
        self.assertEqual(KD.prior_pool(p), [("prior_only__p0__t0.7__r0", 0), ("prior_only__p0__t0.7__r0", 1)])
        q = KD.insert_prior(p, {"g3": ("prior_only__p0__t0.7__r0", 1)}, K)
        n = q.ai[S].nodes[2]
        self.assertEqual((n.node_id, n.text, n.quote), ("g3", "other generic", None))
        self.assertFalse(q.prov[S]["g3"].found)
        self.assertEqual(q.modality[S]["g3"], "unsupported")
        self.assertNotIn("g3", q.not_judged[S])                         # unsupported by rule, not "not judged"
        self.assertEqual(q.canon_ai[S], ["K1", "K2", "C2", "K4"])
        self.assertNotIn((2, 2), q.judged_ai_vs_endorsed[S])
        self.assertNotIn((2, 0), q.judged_ai_vs_script_canon[S])
        self.assertEqual([e.target for e in q.ai[S].edges], ["g2", "g3", "g4"])     # edges stay
        self.assertEqual(b(q)["matched"], 3)

    def test_pick_rounds_and_is_seeded(self):
        pool = list(range(10))
        self.assertEqual(len(KD._pick(random.Random(0), pool, 0.25)), 3)          # 2.5 -> 3
        self.assertEqual(KD._pick(random.Random("x"), pool, 0.5), KD._pick(random.Random("x"), pool, 0.5))


class Fixes18Oct(unittest.TestCase):
    def test_d3_survivor_canon_variant(self):
        q, _ = KD.merge_edges(tiny(4), {0}, "survivor")
        self.assertEqual(q.canon_ai[S], ["K1", "K3", "K4"])
        self.assertEqual(q.ai[S].nodes[0].text, "kilo step and lima step")      # the raw label itself is unchanged
        with self.assertRaises(ValueError):
            KD.merge_edges(tiny(4), {0}, "other")

    def test_stale_fields_cleared(self):
        p = tiny(4)
        p.modality_judge2 = {S: {"g1": "actual"}}
        p.not_judged_shuffled = {S: {"g1"}}
        q = KD.drop_nodes(p, {"g1"})
        self.assertEqual((q.modality_judge2, q.modality_shuffled, q.not_judged_judge2, q.not_judged_shuffled),
                         (None, None, None, None))

    def test_non_synthetic_config_refused_before_any_work(self):
        # retargeted: the kit's context is already open, so the refusal is on the context's corpus kind, before any
        # dose is drawn or any file is written
        ctx = demo_ctx()
        real = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, corpus={**ctx.cfg.corpus, "kind": "real"}))
        out = tmpdir() / "refused"
        with self.assertRaises(RuntimeError):
            KD.run_phantom(real, reps=1, out_dir=out)
        self.assertFalse(out.exists())


class PassRules(unittest.TestCase):
    def test_monotone(self):
        self.assertTrue(KD.monotone([1.0, 0.8, 0.8, 0.5], -1))
        self.assertFalse(KD.monotone([1.0, 0.8, 0.9], -1))
        self.assertFalse(KD.monotone([0.5, 0.5, 0.5], -1))             # never moved
        self.assertTrue(KD.monotone([0.0, 0.3, 1.0], +1))
        self.assertFalse(KD.monotone([0.0, math.nan], +1))

    def test_within_and_smallest_detected(self):
        self.assertTrue(KD.within(0.5, [0.4, 0.6]))
        self.assertFalse(KD.within(0.61, [0.4, 0.6]))
        self.assertEqual(KD.smallest_detected([0.1, 0.2, 0.4], 0.9, [(0.85, 0.95), (0.7, 0.89), (0.5, 0.6)]), 0.2)
        self.assertIsNone(KD.smallest_detected([0.1], 0.9, [(0.8, 0.95)]))

    def test_smallest_detected_with_an_undefined_base_is_none(self):
        """Final review B L-2: a dose-0 value that is undefined (NaN) detects nothing, never the lowest dose."""
        self.assertIsNone(KD.smallest_detected([0.1, 0.2], math.nan, [(0.0, 0.1), (0.0, 0.2)]))
        self.assertIsNone(KD.smallest_detected([0.1], math.nan, [(math.nan, math.nan)]))
        self.assertEqual(KD.smallest_detected([0.1, 0.2], 0.5, [(0.4, 0.6), (0.0, 0.2)]), 0.2)   # finite base unchanged

    def test_pull_detection(self):
        r = KD.pull_detection([0.05, 0.10, 0.20], 0.01, [0.05, 0.085, 0.16], [True, True, True])
        self.assertEqual((r["interpolated"], r["dose"], r["detected"]), (False, 0.10, True))   # .075 within .01
        r = KD.pull_detection([0.05, 0.10, 0.20], 0.0, [0.04, 0.10, 0.2], [False, True, True])
        self.assertTrue(r["interpolated"])
        self.assertAlmostEqual(r["dose"], 0.05 + 0.03 / 0.06 * 0.05)
        self.assertIsNone(r["detected"])                               # only the upper bracket is detected
        r = KD.pull_detection([0.05], 0.0, [0.02], [True])
        self.assertIsNone(r["dose"])

    def test_rules_and_floor(self):
        base = {x: 0.5 for x in KD.READINGS} | {"counterfactual": 0.0, "kept_as_wished": 0.0}
        band = {x: [0.4, 0.6] for x in KD.READINGS}
        summ = [{x: {"median": 0.5, "rep_low": 0.45, "rep_high": 0.55} for x in KD.READINGS} for _ in range(3)]
        ru = KD.rules("D4", [0.25, 0.5, 1.0], base, band, summ, None)
        self.assertFalse(ru["pass"])
        self.assertEqual(sorted(ru["at_floor"]), ["counterfactual", "kept_as_wished"])
        self.assertTrue(all(v["pass"] for v in ru["specificity"].values()))
        summ[2]["pull"]["median"] = 0.7
        self.assertFalse(KD.rules("D4", [0.25, 0.5, 1.0], base, band, summ, None)["specificity"]["pull"]["pass"])


class AgainstTheKit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = demo_ctx()
        cls.pop = cls.ctx.analysed
        cls.scripts = metrics.scripts_by_role(cls.ctx.p_list, canonical=True)

    def _pooled_equal(self, pop):
        mine = KD.readings(pop, PS, self.scripts, K)["values"]
        kb, ka = pipeline._pooled(pop, S, PS, KD.MW)
        self.assertAlmostEqual(mine["node_recall"], kb["node_recall"], places=12)
        self.assertAlmostEqual(mine["edge_recall"], kb["edge_recall"], places=12)
        nw = ka["n_wished"]
        for name, key in (("kept_as_wished", "recovered_as_wished"), ("missed", "missed"),
                          ("counterfactual", "counterfactual_leakage"), ("flattened", "adequacy_flattening")):
            self.assertAlmostEqual(mine[name], ka[key] / nw, places=12)

    def test_pooling_equals_pipeline_pooled(self):
        self._pooled_equal(self.pop)
        for dose in KD.DOSES:
            self._pooled_equal(KD.apply_dose(dose, KD.DOSES[dose][-1], self.pop, random.Random(7), K)[0])

    def test_dose0_equals_the_kit_aggregates(self):
        agg = json.loads((self.ctx.out_dir / "aggregates" / "aggregates.json").read_text())["buckets"]["primary"]
        v = KD.readings(self.pop, PS, self.scripts, K)["values"]
        self.assertAlmostEqual(v["node_recall"], agg["node_recall"], places=12)
        self.assertAlmostEqual(v["edge_recall"], agg["edge_recall"], places=12)

    def test_carried_provenance_equals_the_kit(self):
        people = {x.pid: x for x in self.ctx.run.people}
        for dose in ("D1", "D2", "D3", "D4"):
            pop, _ = KD.apply_dose(dose, KD.DOSES[dose][-1], self.pop, random.Random(3), K)
            for p in pop:
                fresh = provenance.provenance_for(p.ai[S], self.ctx.run.transcript(people[p.pid]))
                self.assertEqual(p.prov[S], fresh, (dose, p.pid))

    def test_apply_dose_is_deterministic_and_leaves_the_oracle(self):
        before = KD.readings(self.pop, PS, self.scripts, K)["values"]
        x1, _ = KD.apply_dose("D3", 0.25, self.pop, random.Random("s"), K)
        x2, _ = KD.apply_dose("D3", 0.25, self.pop, random.Random("s"), K)
        self.assertEqual([q.ai[S] for q in x1], [q.ai[S] for q in x2])
        self.assertEqual(KD.readings(self.pop, PS, self.scripts, K)["values"], before)


class EndToEnd(unittest.TestCase):
    def test_cli_two_replicates(self):
        out, p = cli_outputs()
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        d = json.loads((out / "phantom.json").read_text())
        self.assertEqual(d["meta"]["replicates"], 2)
        for s in ("primary", "lexical_only"):
            res = d["results"][s]
            self.assertEqual(set(res["doses"]), set(KD.DOSES))
            for dose, levels in KD.DOSES.items():
                blk = res["doses"][dose]
                self.assertEqual(blk["levels"], list(levels))
                self.assertEqual(len(blk["summary"]), len(levels))
                for cell in blk["summary"]:
                    for name in KD.READINGS:
                        self.assertEqual(set(cell[name]), {"median", "rep_low", "rep_high", "n"})
        prim = d["results"]["primary"]["doses"]
        self.assertTrue(prim["D2"]["rules"]["monotone"]["edge_recall"])
        self.assertEqual(prim["D4"]["summary"][-1]["counterfactual"]["median"], 1.0)   # every partner flipped
        md = (out / "phantom.md").read_text()
        self.assertIn(KD.LIMIT, md)
        self.assertIn("## Limits", md)
        self.assertIn("upper bounds", md)
        self.assertIn("Best case", md)
        self.assertNotIn("min and max", md)
        self.assertEqual(len(d["results"]["primary"]["d3_survivor_canon"]["summary"]), len(KD.DOSES["D3"]))
        self.assertEqual(md.count("| reading | role |"), 5)


FORBIDDEN = re.compile(r"validated|\bfirst\b|ground truth|replicable", re.IGNORECASE)
SCRIPT_WORD = re.compile(r"(?i)\bscript\w*")


class Task8Additions(unittest.TestCase):
    def test_limit_text_is_exact(self):
        self.assertEqual(KD.LIMIT, "Tests the gauge's arithmetic on planted doses in synthetic text; it does not "
                                   "show that real-data matching is right.")

    def test_limit_in_every_output(self):
        # Part A: phantom.json and phantom.md (the profile's detection_limits block is Part B)
        out, p = cli_outputs()
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        d = json.loads((out / "phantom.json").read_text())
        self.assertEqual(d["meta"]["limit"], KD.LIMIT)
        self.assertEqual(d["detection_limits"]["limit"], KD.LIMIT)
        self.assertIn(KD.LIMIT, (out / "phantom.md").read_text())
        self.assertEqual(KD.detection_limits(d)["limit"], KD.LIMIT)
        # Part B: and the profile's own detection_limits block, after calibrate
        _before, after, _path = calibrated()
        self.assertEqual(after["detection_limits"]["limit"], KD.LIMIT)

    def test_no_validated_word(self):
        out, p = cli_outputs()
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        for path in (out / "phantom.json", out / "phantom.md", Path(KD.__file__)):
            self.assertIsNone(FORBIDDEN.search(path.read_text()), path.name)
        self.assertIsNone(FORBIDDEN.search(KD.LIMIT))
        self.assertIsNone(FORBIDDEN.search(p.stdout + p.stderr))

    def test_no_script_word_in_written_text(self):
        # app rule (SPEC v5 V1): no user-visible word beginning "script"; the written files, not code identifiers
        out, p = cli_outputs()
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        for path in (out / "phantom.json", out / "phantom.md"):
            self.assertNotRegex(path.read_text(encoding="utf-8"), SCRIPT_WORD, path.name)
        self.assertNotRegex(p.stdout + p.stderr, SCRIPT_WORD)
        _before, after, _path = calibrated()
        self.assertNotRegex(json.dumps(after["detection_limits"]), SCRIPT_WORD)

    def test_detection_limits_block(self):
        out, _p = cli_outputs()
        d = json.loads((out / "phantom.json").read_text())
        dl = KD.detection_limits(d)
        self.assertEqual(dl, d["detection_limits"])                 # the block written to phantom.json
        self.assertEqual(dl["schema"], "orgsci-detection-limits/v1")
        self.assertEqual(set(dl["per_dose"]), set(KD.DOSES))
        for dose, levels in KD.DOSES.items():
            blk = dl["per_dose"][dose]
            self.assertEqual(set(blk), {"smallest_detected", "lowest_tried", "at_or_below", "rule"})
            self.assertEqual(blk["rule"], "rep_interval+kit_ci" if dose == "D5" else "rep_interval")
            self.assertEqual(blk["lowest_tried"], levels[0])
            self.assertIsInstance(blk["at_or_below"], bool)
            self.assertEqual(blk["at_or_below"], blk["smallest_detected"] == levels[0])
        prim = d["results"]["primary"]
        self.assertEqual(dl["per_dose"]["D5"]["smallest_detected"],       # D5 is led by the safe dose when there is one
                         KD._d5_safe(prim))
        self.assertEqual(dl["pull_rule"], "rep_interval")
        self.assertIs(dl["low_reps"], True)                         # 2 replicates: fewer than 5
        self.assertEqual(dl["pull_planted"], 0.07)
        self.assertIsInstance(dl["pull_detected"], bool)
        self.assertEqual(set(dl["dose0_bands"]), set(KD.READINGS))
        self.assertEqual(dl["settings"]["reps"], 2)
        self.assertEqual(dl["settings"]["B"], 200)
        self.assertEqual(dl["settings"]["seed"], pb.SEED)
        self.assertTrue(dl["kit_commit"])

    def test_detection_limits_from_a_hand_built_result(self):
        # smallest_detected None -> at_or_below False; pull not established -> pull_detected False
        lv = KD.DOSES
        res = {"primary": {"dose0_band": {x: [0.1, 0.2] for x in KD.READINGS}, "doses": {
            d: {"levels": list(lv[d]), "rules": {"smallest_detected_dose": None if d == "D1" else lv[d][0]}}
            for d in lv}}}
        res["primary"]["doses"]["D5"]["rules"]["pull_07"] = {"detected": None}
        r = {"meta": {"kit_commit": "abc", "replicates": 20, "bootstrap_B": 10, "seed_base": 1, "min_words": 8,
                      "spec": S},
             "results": res}
        dl = KD.detection_limits(r)
        self.assertEqual(dl["per_dose"]["D1"], {"smallest_detected": None, "lowest_tried": 0.10, "at_or_below": False,
                                                "rule": "rep_interval"})
        self.assertNotIn("low_reps", dl)                            # 20 replicates
        self.assertEqual(dl["pull_rule"], "rep_interval")
        self.assertTrue(dl["per_dose"]["D2"]["at_or_below"])
        self.assertIs(dl["pull_detected"], False)
        # D5, no dose safe: the plain rule says 5%, but the kit's CI is above 0 in only 60% of the replicates there
        # and in 90% at 10%: D5 is null (no fallback to the plain rule) and at_or_below is false
        res["primary"]["dose0"] = {"pull": 0.0}
        res["primary"]["doses"]["D5"]["summary"] = [{"pull": {"rep_low": 0.01, "rep_high": 0.02}},
                                                    {"pull": {"rep_low": 0.03, "rep_high": 0.05}},
                                                    {"pull": {"rep_low": 0.08, "rep_high": 0.1}}]
        res["primary"]["doses"]["D5"]["rules"]["smallest_detected_dose"] = 0.05
        res["primary"]["doses"]["D5"]["rules"]["pull_07"] = {"detected": True,
                                                             "kit_ci_lo_above_0_share": [0.6, 0.9, 0.95]}
        dl = KD.detection_limits(r)
        self.assertEqual(dl["per_dose"]["D5"], {"smallest_detected": None, "lowest_tried": 0.05, "at_or_below": False,
                                                "rule": "rep_interval+kit_ci"})
        self.assertIs(dl["pull_detected"], True)
        # D5, a safe dose at 10% (CI above 0 in every replicate) while the plain rule says 5%
        res["primary"]["doses"]["D5"]["rules"]["pull_07"]["kit_ci_lo_above_0_share"] = [0.6, 1.0, 1.0]
        dl = KD.detection_limits(r)
        self.assertEqual(dl["per_dose"]["D5"], {"smallest_detected": 0.10, "lowest_tried": 0.05, "at_or_below": False,
                                                "rule": "rep_interval+kit_ci"})
        # a replicate interval that does not exclude dose 0 is not safe even when the kit's CI always is
        res["primary"]["doses"]["D5"]["summary"][1]["pull"]["rep_low"] = -0.01
        res["primary"]["doses"]["D5"]["summary"][2]["pull"]["rep_low"] = -0.01
        self.assertIsNone(KD.detection_limits(r)["per_dose"]["D5"]["smallest_detected"])
        # fewer than 5 replicates: low_reps
        r["meta"]["replicates"] = 4
        self.assertIs(KD.detection_limits(r)["low_reps"], True)

    @staticmethod
    def _raw_result():
        if "raw" not in _CACHE:
            _CACHE["raw"] = KD.run_phantom(dataclasses.replace(demo_ctx(), B=50), reps=2)
        return copy.deepcopy(_CACHE["raw"])

    def _md_with(self, safe_at, plain_at_lowest_for_others, interp=None):
        """phantom.md text of the 2-replicate result with D5 forged: ``safe_at`` the index of the first safe D5 dose
        (None: no safe dose); the plain rule says the lowest D5 dose; D1-D4 at their lowest dose when asked."""
        r = self._raw_result()
        prim = r["results"]["primary"]
        for d in ("D1", "D2", "D3", "D4"):
            lv = prim["doses"][d]["levels"]
            prim["doses"][d]["rules"]["smallest_detected_dose"] = lv[0] if plain_at_lowest_for_others else lv[1]
        d5 = prim["doses"]["D5"]
        base = prim["dose0"]["pull"]
        d5["rules"]["smallest_detected_dose"] = d5["levels"][0]
        n = len(d5["levels"])
        d5["rules"]["pull_07"]["kit_ci_lo_above_0_share"] = [1.0 if safe_at is not None and i >= safe_at else 0.5
                                                           for i in range(n)]
        for i, sm in enumerate(d5["summary"]):
            sm["pull"]["rep_low"] = base + 0.05
            sm["pull"]["rep_high"] = base + 0.06
        if interp is not None:
            d5["rules"]["pull_07"].update(interp)
        return KD.markdown(r["results"], r["meta"]), prim, d5

    def test_md_agrees_with_the_block_for_d5(self):
        # plain rule: D5 at its lowest dose (5%); the safe dose is the second (10%): no "every dose" claim
        md, prim, d5 = self._md_with(safe_at=1, plain_at_lowest_for_others=True)
        self.assertNotIn("Every dose type detects", md)
        self.assertIn("Smallest detected dose per dose type", md)
        self.assertIn("D5 10% (kit CI above 0 in 100% of replicates)", md)
        # no safe dose: no "every dose" claim either, and the lead says so
        md, prim, d5 = self._md_with(safe_at=None, plain_at_lowest_for_others=True)
        self.assertNotIn("Every dose type detects", md)
        self.assertIn("none safe", md)
        # every dose at its lowest, D5 safe at 5%: the claim is allowed
        md, prim, d5 = self._md_with(safe_at=0, plain_at_lowest_for_others=True)
        self.assertIn("Every dose type detects at the lowest dose tried", md)
        self.assertIn("D5 5% (kit CI above 0", md)
        # the block agrees with the text in each case
        r = self._raw_result()
        self.assertEqual(KD.detection_limits(r)["per_dose"]["D5"]["smallest_detected"], KD._d5_safe(r["results"]["primary"]))

    def test_bracketing_sentence_only_when_interpolated_and_detected(self):
        interp = {"interpolated": True, "bracket": [0.05, 0.10], "dose": 0.07, "planted": 0.07}
        for det, interpolated, expect in ((True, True, True), (None, True, False), (False, True, False),
                                          (True, False, False)):
            fix = dict(interp, detected=det, interpolated=interpolated)
            if not interpolated:
                fix.update(dose=0.10, planted=0.075)
            md, _prim, _d5 = self._md_with(safe_at=1, plain_at_lowest_for_others=True, interp=fix)
            self.assertEqual("the lower bracketing dose is detected" in md, expect, (det, interpolated))

    def test_low_reps_says_so(self):
        out, _p = cli_outputs()
        md = (out / "phantom.md").read_text()
        self.assertIn("Low replicate count", md)                    # the end-to-end run has 2 replicates
        self.assertIn("median flattened share is 0 at every dose", md)
        r = self._raw_result()
        r["meta"]["replicates"] = 20
        self.assertNotIn("Low replicate count", KD.markdown(r["results"], r["meta"]))

    def test_private_signatures_pinned(self):
        # the private kit functions phantom calls: a change of their signature fails here, not in a run
        def params(fn):
            return list(inspect.signature(fn).parameters)
        self.assertEqual(params(metrics._pull_rows),
                         ["p_list", "spec", "setting", "scripts", "in_transcript_only", "variant"])
        self.assertEqual(params(metrics._mean_diff), ["a", "b"])
        self.assertEqual(params(metrics._absence_structure), ["p", "spec_key", "arm", "setting", "min_words"])
        self.assertEqual(params(metrics.absence_category), ["st", "status", "nid"])
        self.assertEqual(params(metrics.buckets), ["p", "spec_key", "arm", "setting", "min_words"])
        self.assertEqual(params(metrics.absences), ["p", "spec_key", "arm", "setting", "min_words"])
        self.assertEqual(params(K.stats.paired_bootstrap_ci), ["a", "b", "B", "alpha", "seed"])
        self.assertEqual(params(provenance.locate_quote)[:4], ["quote", "transcript", "element_id", "kind"])
        fields = {f.name for f in dataclasses.fields(metrics.PairInputs)}
        self.assertTrue({"ai", "prov", "modality", "not_judged", "canon_ai", "canon_prior", "judged_ai_vs_endorsed",
                         "judged_ai_vs_script", "judged_ai_vs_script_canon", "modality_judge2", "modality_shuffled",
                         "not_judged_judge2", "not_judged_shuffled"} <= fields)

    def test_run_phantom_records_a_given_commit(self):
        ctx = dataclasses.replace(demo_ctx(), B=50)
        r = KD.run_phantom(ctx, reps=1, commit=LAB_COMMIT)
        self.assertEqual(r["meta"]["kit_commit"], LAB_COMMIT)
        self.assertEqual(r["detection_limits"]["kit_commit"], LAB_COMMIT)
        self.assertEqual(KD.run_phantom(ctx, reps=1)["meta"]["kit_commit"], KD.kit_commit())    # default: local HEAD

    def test_spec_mismatch_refused(self):
        ctx = demo_ctx()
        other = dataclasses.replace(ctx, spec="removed__p1__t0__r0")
        with self.assertRaises(ValueError):
            KD.run_phantom(other, reps=1)

    def test_same_inputs_same_bytes(self):
        ctx = dataclasses.replace(demo_ctx(), B=50)
        a_dir, b_dir = tmpdir() / "a", tmpdir() / "b"
        KD.run_phantom(ctx, reps=1, out_dir=a_dir)
        KD.run_phantom(ctx, reps=1, out_dir=b_dir)
        for name in ("phantom.json", "phantom.md"):
            self.assertEqual((a_dir / name).read_bytes(), (b_dir / name).read_bytes(), name)


class Calibrate(unittest.TestCase):
    """Part B: ``calibrate --config C --profile P.json`` (Ruling R27). Its numbers come from the public synthetic demo, never
    from the lab's data; only B, seed and the kit commit are taken from the lab's profile."""

    def test_calibrate_attaches_block_and_rescans(self):
        lab, original, _p = lab_profile()
        before, after, path = calibrated()
        after = copy.deepcopy(after)                           # the cached copy is shared with other tests
        self.assertNotIn("detection_limits", before)
        dl = after.pop("detection_limits")
        self.assertEqual(after, original)                      # nothing else in the profile changed
        self.assertEqual(dl["schema"], "orgsci-detection-limits/v1")
        self.assertEqual(dl["limit"], KD.LIMIT)
        self.assertEqual((dl["settings"]["B"], dl["settings"]["seed"], dl["settings"]["reps"]), (LAB_B, LAB_SEED, 2))
        self.assertEqual(dl["kit_commit"], original["run"]["kit"]["release_commit"])
        self.assertIs(dl["low_reps"], True)
        self.assertEqual(set(dl["per_dose"]), set(KD.DOSES))
        # the file passes the lab-string rules and the full privacy scan, with the lab's own people as forbidden tokens
        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(pb.lab_string_problems(full), [])
        self.assertEqual(pe.privacy_scan(full, forbidden=pe.forbidden_tokens(lab)), [])
        self.assertIsNone(FORBIDDEN.search(path.read_text(encoding="utf-8")))
        # the deterministic writer: the file is sorted-key JSON, and a second calibrate of the same profile gives the same bytes
        again = copy_of_lab_profile()
        self.assertEqual(run_calibrate_cli(again).returncode, 0)
        self.assertEqual(again.read_bytes(), path.read_bytes())
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=1, sort_keys=True, ensure_ascii=True) + "\n")

    def test_calibrate_reruns_the_privacy_scan_on_the_whole_file(self):
        lab, _prof, _p = lab_profile()
        path = copy_of_lab_profile()
        with mock.patch.object(pe, "privacy_scan", wraps=pe.privacy_scan) as spy:
            KD.calibrate(DEMO_CONFIG, path, reps=1, lab_out_dir=lab.out_dir)
        self.assertTrue(spy.called)
        scanned = spy.call_args.args[0]
        self.assertIn("detection_limits", scanned)             # the scan sees the new block
        self.assertTrue(spy.call_args.kwargs["forbidden"]["pids"])   # and the lab's people

    def test_calibrate_with_uppercase_initials_is_not_refused(self):
        """Ruling R31 (final review A1): a lab whose interviewers are coded OF, BY, IS or IN calibrates (the limit
        sentence and the population label hold those words in lower case); the initials planted in a scanned field of
        the calibrated file are still refused."""
        lab, _prof, _p = lab_profile()
        real = pe.forbidden_tokens(lab)
        initials = {"pids": real["pids"], "codes": real["codes"] | {"OF", "BY", "IS", "IN"}}
        path = copy_of_lab_profile()
        with mock.patch.object(pe, "forbidden_tokens", side_effect=lambda ctx: initials):
            KD.calibrate(DEMO_CONFIG, path, reps=1, lab_out_dir=lab.out_dir)
        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("detection_limits", full)
        self.assertEqual(pe.privacy_scan(full, forbidden=initials), [])
        full["detection_limits"]["pull_rule"] = "rep_interval IS"
        self.assertTrue(pe.privacy_scan(full, forbidden=initials))

    def test_refused_calibrate_leaves_no_phantom_files(self):
        """Final review A7: the profile is scanned before the demo is built; a refusal writes no phantom.json/md, and a
        success writes both (atomically) after the profile."""
        lab, _prof, _p = lab_profile()
        out = tmpdir() / "ph"
        path = copy_of_lab_profile(lambda d: d["rows"][3]["cells"][0].__setitem__("participant_note", "x"))
        raw = path.read_bytes()
        with mock.patch.object(KD, "demo_context", side_effect=AssertionError("the demo was built")):
            with self.assertRaises(pe.ProfileRefused):
                KD.calibrate(DEMO_CONFIG, path, reps=1, out_dir=out, lab_out_dir=lab.out_dir)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(out.exists() and any(out.iterdir()))
        # a refusal that only the final write can see (the new block is scanned too) leaves nothing behind either
        path = copy_of_lab_profile()
        raw = path.read_bytes()
        real_write = pe.write_profile
        def refuse_after_block(profile, *a, **k):
            if "detection_limits" in profile:
                raise pe.ProfileRefused("planted refusal")
            return real_write(profile, *a, **k)
        with mock.patch.object(pe, "write_profile", side_effect=refuse_after_block):
            with self.assertRaises(pe.ProfileRefused):
                KD.calibrate(DEMO_CONFIG, path, reps=1, out_dir=out, lab_out_dir=lab.out_dir)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(out.exists() and any(out.iterdir()))
        KD.calibrate(DEMO_CONFIG, path, reps=1, out_dir=out, lab_out_dir=lab.out_dir)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["phantom.json", "phantom.md"])
        # the write order (temps, then the profile, then the renames) is test_calibrate_writes_the_profile_last

    def test_calibrate_input_errors_exit_2_without_traceback(self):
        """Final review A8: a malformed profile is a refusal (exit 2); a demo that does not finish is a refusal too (exit 2,
        fix round 2 N4: anything before the profile write), without a traceback."""
        import contextlib
        import io
        from second_scribe import cli
        lab, _prof, _p = lab_profile()
        for tag, edit in (("population text", lambda d: d.__setitem__("population", "12 of 12")),
                          ("run text", lambda d: d.__setitem__("run", "x")),
                          ("settings text", lambda d: d["run"].__setitem__("settings", "x"))):
            with self.subTest(tag):
                path = copy_of_lab_profile(edit)
                raw = path.read_bytes()
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["calibrate", "--config", str(DEMO_CONFIG), "--out-dir", str(lab.out_dir),
                                   "--profile", str(path), "--reps", "1"])
                self.assertEqual(rc, 2, err.getvalue())
                self.assertIn("refused", err.getvalue())
                self.assertEqual(path.read_bytes(), raw)
        listed = tmpdir() / "list.json"
        listed.write_text("[1, 2]", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["calibrate", "--config", str(DEMO_CONFIG), "--out-dir", str(lab.out_dir),
                                       "--profile", str(listed), "--reps", "1"]), 2)
        path = copy_of_lab_profile()
        err = io.StringIO()
        with mock.patch.object(KD, "demo_context", side_effect=RuntimeError("phantom: the oracle demo did not finish clean")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["calibrate", "--config", str(DEMO_CONFIG), "--out-dir", str(lab.out_dir),
                           "--profile", str(path), "--reps", "1"])
        self.assertEqual(rc, 2)                                # fix round 2, N4: before the profile write = refused
        self.assertIn("refused", err.getvalue())
        self.assertIn("did not finish", err.getvalue())

    def test_calibrate_writes_the_profile_last(self):
        """Fix round 2, N4: the phantom files are staged under temp names, the profile is written, then the temps are
        renamed into place. A failure before the profile write removes the temps and is a refusal (exit 2); a failure
        after it is never called a refusal."""
        import contextlib
        import io
        from second_scribe import cli
        lab, _prof, _p = lab_profile()

        def run(path, out):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["calibrate", "--config", str(DEMO_CONFIG), "--out-dir", str(lab.out_dir), "--profile",
                               str(path), "--reps", "1", "--out", str(out)])
            return rc, err.getvalue()
        # 1. order: while the profile is written, only temp files sit in the out dir
        out, path = tmpdir() / "ph-order", copy_of_lab_profile()
        seen = []
        real_write = pe.write_profile

        def spy(profile, *a, **k):
            seen.append(sorted(p.name for p in out.iterdir()) if out.exists() else [])
            return real_write(profile, *a, **k)
        with mock.patch.object(pe, "write_profile", side_effect=spy):
            rc, err = run(path, out)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 2)
        self.assertTrue(all(n.endswith(".tmp") for n in seen[0]), seen)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["phantom.json", "phantom.md"])
        # 2. a failure before the profile write (staging, or the write itself): refused, exit 2, no temp, file untouched
        for tag, patch in (("write refused", mock.patch.object(pe, "write_profile",
                                                              side_effect=pe.ProfileRefused("planted"))),
                           ("staging fails", mock.patch.object(KD, "_stage_text", side_effect=OSError("disk full")))):
            with self.subTest(tag):
                out, path = tmpdir() / "ph-before", copy_of_lab_profile()
                raw = path.read_bytes()
                with patch:
                    rc, err = run(path, out)
                self.assertEqual(rc, 2, err)
                self.assertIn("refused", err)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(sorted(p.name for p in out.iterdir()) if out.exists() else [], [])
        # 3. a failure after the profile write (the rename): not "refused"; the profile is the calibrated one, no temp left
        out, path = tmpdir() / "ph-after", copy_of_lab_profile()
        real_replace = os.replace

        def fail_phantom_renames(src, dst):
            if Path(dst).name.startswith("phantom."):
                raise OSError("rename failed")
            return real_replace(src, dst)
        with mock.patch.object(KD.os, "replace", side_effect=fail_phantom_renames):
            rc, err = run(path, out)
        self.assertEqual(rc, 1, err)
        self.assertNotIn("refused", err)
        self.assertIn("profile", err)
        self.assertIn("detection_limits", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(sorted(p.name for p in out.iterdir()), [])

    def test_calibrate_refuses_a_planted_pid_or_hex_and_leaves_the_file(self):
        lab, _prof, _p = lab_profile()
        pid = sorted(pe.forbidden_tokens(lab)["pids"])[0]
        plants = {"pid in the population label": lambda d: d["population"].__setitem__("label", f"{pid} of 12"),
                  "64-hex outside run": lambda d: d.__setitem__("note_hex", "0123456789abcdef" * 4),
                  "forbidden key": lambda d: d.__setitem__("map_id", "x")}
        for tag, edit in plants.items():
            with self.subTest(tag):
                path = copy_of_lab_profile(edit)
                raw = path.read_bytes()
                p = run_calibrate_cli(path)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("refused", p.stderr)
                self.assertEqual(path.read_bytes(), raw)       # the file is untouched
                self.assertNotIn(pid, p.stderr + p.stdout)     # and the message names no person

    def test_calibrate_refuses_a_profile_of_another_run_or_no_settings(self):
        lab, _prof, _p = lab_profile()
        for tag, edit in (("other population", lambda d: d["population"].__setitem__("n_people", 11)),
                          ("no settings", lambda d: d["run"].pop("settings")),
                          ("no commit", lambda d: d["run"]["kit"].pop("release_commit"))):
            with self.subTest(tag):
                path = copy_of_lab_profile(edit)
                raw = path.read_bytes()
                with self.assertRaises(pe.ProfileRefused):
                    KD.calibrate(DEMO_CONFIG, path, reps=1, lab_out_dir=lab.out_dir)
                self.assertEqual(path.read_bytes(), raw)

    def test_calibrate_takes_the_labs_commit_B_and_seed_not_the_local_ones(self):
        lab, _prof, _p = lab_profile()
        path = copy_of_lab_profile(lambda d: (d["run"]["kit"].__setitem__("release_commit", LAB_COMMIT),
                                              d["run"]["settings"].update(bootstrap_resamples=77, bootstrap_seed=5)))
        KD.calibrate(DEMO_CONFIG, path, reps=1, lab_out_dir=lab.out_dir)
        dl = json.loads(path.read_text(encoding="utf-8"))["detection_limits"]
        self.assertEqual(dl["kit_commit"], LAB_COMMIT)
        self.assertNotEqual(dl["kit_commit"], KD.kit_commit())
        self.assertEqual((dl["settings"]["B"], dl["settings"]["seed"]), (77, 5))
        self.assertEqual(dl["settings"]["spec"], S)            # always the demo's primary spec, never the lab's own

    def test_calibrate_bad_reps_config_and_label_are_refused_and_leave_the_file(self):
        lab, _prof, _p = lab_profile()
        cases = {
            "reps 0": (lambda d: None, ["--reps", "0"], str(DEMO_CONFIG), "reps must be at least 1"),
            "missing config": (lambda d: None, [], str(tmpdir() / "nope.json"), "run config cannot be opened"),
            "bad config": (lambda d: None, [], None, "run config cannot be opened"),
            "people differ": (lambda d: d["population"].__setitem__("n_people", 11), [], str(DEMO_CONFIG),
                              "people analysed: profile 11, run 12"),                    # fix round 1, S3
            "label differs": (lambda d: d["population"].__setitem__("label", "12 of 99"), [], str(DEMO_CONFIG),
                              "population label: profile '12 of 99', run "),            # village E4: both values
        }
        for tag, (edit, extra, config, expect) in cases.items():
            with self.subTest(tag):
                path = copy_of_lab_profile(edit)
                if config is None:
                    bad = tmpdir() / "bad.json"
                    bad.write_text("{not json", encoding="utf-8")
                    config = str(bad)
                raw = path.read_bytes()
                args = [sys.executable, "-m", "second_scribe.cli", "calibrate", "--config", config, "--out-dir",
                        str(lab.out_dir), "--profile", str(path), "--reps", "2", *extra]   # a later --reps wins
                p = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=600)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn("refused", p.stderr)
                self.assertIn(expect, p.stderr)
                self.assertEqual(path.read_bytes(), raw)       # byte-identical

    def test_calibrate_refuses_before_opening_the_lab_context_when_reps_is_zero(self):
        path = copy_of_lab_profile()
        with mock.patch.object(pb, "open_context", side_effect=AssertionError("opened")):
            with self.assertRaises(pe.ProfileRefused):
                KD.calibrate(DEMO_CONFIG, path, reps=0)

    def test_calibrate_notes_a_different_local_commit_on_stderr_and_keeps_the_stored_one(self):
        lab, _prof, _p = lab_profile()
        path = copy_of_lab_profile(lambda d: d["run"]["kit"].__setitem__("release_commit", LAB_COMMIT))
        p = run_calibrate_cli(path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("note:", p.stderr)
        self.assertIn(LAB_COMMIT[:12], p.stderr)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["detection_limits"]["kit_commit"], LAB_COMMIT)
        same = copy_of_lab_profile()                           # the lab's commit is this kit's HEAD: no note
        self.assertNotIn("note:", run_calibrate_cli(same).stderr)

    def test_make_calibrate_target(self):
        lab, _prof, _p = lab_profile()
        path = copy_of_lab_profile()
        r = subprocess.run(["make", "calibrate", f"CONFIG={DEMO_CONFIG}", f"PROFILE={path}", f"OUTDIR={lab.out_dir}"],
                           cwd=REPO, capture_output=True, text=True, timeout=1800)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["detection_limits"]["settings"]["reps"], 20)
        r = subprocess.run(["make", "calibrate"], cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                   # CONFIG and PROFILE are required
        self.assertIn("usage", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
