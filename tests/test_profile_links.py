"""profile_links: row 4 (links between steps) of the profile exporter.

Known answers are hand-built inputs ported from the analysis pack's test of a4_edges (read as a source, never
imported). The demo tests run the kit's synthetic oracle run once per module (zero model calls).
"""
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from second_scribe import match, metrics, provenance
from second_scribe import profile_base as pb
from second_scribe import profile_links as pl
from second_scribe.cli import DEMO_CONFIG
from second_scribe.match import MatchSetting

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "tests" / "data" / "profile_v2_skeleton.json"
F = frozenset
Z = dict.fromkeys(("n_universe", "endpoint_lost", "collapsed", "eligible", "direct", "undirected", "path_le2"), 0)


def skeleton_row4():
    sk = json.loads(SKELETON.read_text(encoding="utf-8"))
    return [row for row in sk["rows"] if row["row"] == 4][0]["cells"]


class TestPure(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(pl.classify(F(), F({"a"})), "endpoint_lost")
        self.assertEqual(pl.classify(F({"a"}), F()), "endpoint_lost")
        self.assertEqual(pl.classify(F({"a"}), F({"a"})), "collapsed")
        self.assertEqual(pl.classify(F({"a"}), F({"b"})), "eligible")
        self.assertEqual(pl.classify(F({"a", "b"}), F({"a"})), "eligible")      # a shared node, but b != a exists

    def test_recovery_kinds(self):
        succ = pl.successors([("a", "b"), ("b", "c"), ("x", "y"), ("y", "x")])
        self.assertTrue(pl.rec_direct(F({"a"}), F({"b"}), succ))
        self.assertFalse(pl.rec_direct(F({"b"}), F({"a"}), succ))              # direction kept
        self.assertTrue(pl.rec_undirected(F({"b"}), F({"a"}), succ))
        self.assertFalse(pl.rec_path_le2(F({"b"}), F({"a"}), succ))
        self.assertFalse(pl.rec_direct(F({"a"}), F({"c"}), succ))
        self.assertTrue(pl.rec_path_le2(F({"a"}), F({"c"}), succ))             # a->b->c
        self.assertFalse(pl.rec_undirected(F({"a"}), F({"c"}), succ))
        self.assertFalse(pl.rec_path_le2(F({"c"}), F({"a"}), succ))
        # a 2-cycle back to the start is not a path between two AI nodes (b != a)
        self.assertFalse(pl.rec_path_le2(F({"x"}), F({"x", "q"}), succ))
        self.assertTrue(pl.rec_path_le2(F({"x"}), F({"y", "q"}), succ))

    def test_map_counts(self):
        succ = pl.successors([("a", "b"), ("b", "c")])
        edges = [(F({"a"}), F({"b"})),           # eligible: direct, undirected, path
                 (F({"b"}), F({"a"})),           # eligible: undirected only
                 (F({"a"}), F({"c"})),           # eligible: path only
                 (F({"c"}), F({"d"})),           # eligible: none
                 (F({"a"}), F({"a"})),           # collapsed
                 (F(), F({"a"}))]                # endpoint lost
        self.assertEqual(pl.map_counts(edges, succ), {"n_universe": 6, "endpoint_lost": 1, "collapsed": 1,
                                                      "eligible": 4, "direct": 1, "undirected": 2, "path_le2": 2})

    def test_null_two_nodes(self):
        units = [([(F({"a"}), F({"b"}))], pl.successors([("a", "b")]))]
        ex = pl.null_expected(units, n_perm=4000, seed=1)
        self.assertAlmostEqual(ex["direct"], 0.5, delta=0.03)
        self.assertAlmostEqual(ex["path_le2"], 0.5, delta=0.03)
        self.assertEqual(ex["undirected"], 1.0)
        self.assertEqual(pl.null_expected(units, n_perm=50, seed=7), pl.null_expected(units, n_perm=50, seed=7))

    def test_null_keeps_classes_and_ignores_unmatched_ai_nodes(self):
        units = [([(F({"a"}), F({"b"})), (F({"c"}), F({"c"})), (F(), F({"a"}))],
                  pl.successors([("a", "b"), ("z", "a")]))]
        ex = pl.null_expected(units, n_perm=6000, seed=3)
        self.assertAlmostEqual(ex["direct"], 1 / 6, delta=0.02)        # 6 bijections of {a,b,c}: a->b kept in 1
        self.assertTrue(math.isnan(pl.null_expected([([], {})], n_perm=5)["direct"]))

    def test_null_defaults(self):
        self.assertEqual(pl.N_NULL, 1000)
        self.assertEqual(inspect.signature(pl.null_expected).parameters["n_perm"].default, 1000)


class TestSettings(unittest.TestCase):
    def test_lenient_is_lex06_same_or_close(self):
        self.assertEqual(pl.ROW4_LENIENT, MatchSetting(0.6, "same_or_close"))
        self.assertEqual((pl.ROW4_LENIENT.lex_threshold, pl.ROW4_LENIENT.judge_accept), (0.6, "same_or_close"))
        # distinct from the profile's lenient (lex .4) used by rows 1-3 and 7
        self.assertNotEqual(pl.ROW4_LENIENT, pb.SETTINGS["lenient"])
        self.assertEqual(pb.SETTINGS["lenient"], MatchSetting(0.4, "same_or_close"))      # profile_base untouched
        self.assertEqual(pl.ROW4_SETTINGS["primary"], pb.SETTINGS["primary"])
        self.assertEqual(pl.ROW4_SETTINGS["lenient"], pl.ROW4_LENIENT)

    def test_row_meta(self):
        self.assertEqual(pl.ROW4_RESTS_ON, ["R", "IT", "LX", "AL", "Q", "MO"])
        self.assertEqual(pl.ROW4_QUALIFIERS, ["no_structural_claim", "seam_withheld_k"])


class TestCellsFromCounts(unittest.TestCase):
    """The cell builder on hand-built per-map counts: pooled ratio of sums, Amendment 4 (no CI when a replicate was dropped)."""

    def res(self, per_map, null):
        return {"per_map": per_map, "null": null}

    def by(self, cells):
        return {(c["name"], c.get("x")): c for c in cells}

    def test_pooled_ratio_of_sums(self):
        m1 = {**Z, "n_universe": 4, "endpoint_lost": 1, "collapsed": 1, "eligible": 2, "direct": 1, "undirected": 2,
              "path_le2": 1}
        m2 = {**Z, "n_universe": 3, "eligible": 3, "direct": 3, "undirected": 3, "path_le2": 3}
        cells = pl.setting_cells(self.res([m1, m2], {"direct": 0.4, "undirected": 0.5, "path_le2": 0.0}),
                                 "primary", B=200, seed=1)
        g = self.by(cells)
        self.assertEqual(g[("eligible_edges", "primary")]["value"], 5)
        self.assertEqual(g[("eligible_edges", "primary")]["n"], 7)
        self.assertEqual(g[("endpoint_lost", "primary")]["value"], round(1 / 7, 3))
        self.assertEqual(g[("direct", "primary")]["value"], 0.8)                 # 4/5, not the mean of 1/2 and 1
        self.assertEqual(g[("direct", "primary")]["n"], 5)
        self.assertEqual(g[("undirected", "primary")]["value"], 1.0)
        self.assertEqual(g[("direct_null_expected", "primary")]["value"], 0.4)
        self.assertEqual(g[("direct_over_null", "primary")]["value"], 2.0)
        self.assertEqual(g[("collapsed_share_of_matched", "primary")]["value"], round(1 / 6, 3))
        self.assertEqual(g[("collapsed_share_of_matched", "primary")]["n"], 6)
        lo, hi = g[("direct", "primary")]["ci_lo"], g[("direct", "primary")]["ci_hi"]
        self.assertTrue(0.5 <= lo <= 0.8 <= hi <= 1.0)
        self.assertNotIn("note", g[("direct", "primary")])
        self.assertIsNone(g[("eligible_edges", "primary")]["ci_level"])

    def test_over_null_uses_the_unrounded_rate(self):
        # direct = 1/3 (printed .333); (1/3) / .5 = .667, whereas .333 / .5 would print .666
        m = {**Z, "n_universe": 3, "eligible": 3, "direct": 1, "undirected": 1, "path_le2": 1}
        g = self.by(pl.setting_cells(self.res([m, dict(m)], dict.fromkeys(pl.KINDS, 0.5)), "primary", B=50, seed=1))
        self.assertEqual(g[("direct", "primary")]["value"], 0.333)
        self.assertEqual(g[("direct_over_null", "primary")]["value"], 0.667)

    def test_ci_undefined_when_replicates_dropped(self):
        one = {**Z, "n_universe": 3, "endpoint_lost": 1, "eligible": 2, "direct": 1, "undirected": 1, "path_le2": 2}
        lost = {**Z, "n_universe": 2, "endpoint_lost": 2}
        g = self.by(pl.setting_cells(self.res([one] + [lost] * 11, dict.fromkeys(pl.KINDS, 0.25)), "primary",
                                     B=300, seed=5))
        for name in ("direct", "undirected", "path_le2", "collapsed_share_of_matched"):
            c = g[(name, "primary")]
            self.assertNotIn("ci_lo", c)
            self.assertIsNone(c["ci_level"])
            self.assertEqual(c["note"], "ci_undefined")
        self.assertEqual(g[("direct", "primary")]["value"], 0.5)
        self.assertIn("ci_lo", g[("endpoint_lost", "primary")])               # universe > 0 in every map
        self.assertNotIn("note", g[("endpoint_lost", "primary")])

    def test_no_ratio_when_expected_is_zero(self):
        m = {**Z, "n_universe": 2, "eligible": 2, "direct": 1, "undirected": 1, "path_le2": 1}
        g = self.by(pl.setting_cells(self.res([m, dict(m)], {"direct": 0.0, "undirected": 0.0, "path_le2": 0.0}),
                                     "primary", B=100, seed=2))
        c = g[("direct_over_null", "primary")]
        self.assertEqual(c["state"], "not_run")
        self.assertIsNone(c["value"])

    def test_no_eligible_edges_is_not_run_never_zero(self):
        lost = {**Z, "n_universe": 2, "endpoint_lost": 2}
        cells = pl.setting_cells(self.res([lost, dict(lost)], dict.fromkeys(pl.KINDS, math.nan)), "primary",
                                 B=100, seed=2)
        g = self.by(cells)
        self.assertEqual(g[("eligible_edges", "primary")]["value"], 0)
        for name in ("direct", "undirected", "path_le2", "direct_null_expected", "direct_over_null"):
            self.assertEqual(g[(name, "primary")]["state"], "not_run", name)
            self.assertIsNone(g[(name, "primary")]["value"])
        self.assertEqual(g[("collapsed_share_of_matched", "primary")]["state"], "not_run")

    def test_partial_when_lenient_direct_undefined(self):
        """Primary direct computed, lenient undefined: the primary direct is partial_not_run, missing the band."""
        from unittest import mock
        m = {**Z, "n_universe": 2, "eligible": 2, "direct": 1, "undirected": 1, "path_le2": 1}
        lost = {**Z, "n_universe": 2, "endpoint_lost": 2}
        results = iter([{"per_map": [m, dict(m)], "null": dict.fromkeys(pl.KINDS, 0.5)},
                        {"per_map": [lost, dict(lost)], "null": dict.fromkeys(pl.KINDS, math.nan)}])
        with mock.patch.object(pl, "analyse", lambda ctx, setting: next(results)):
            cells = pl.row4_cells(mock.Mock(B=100, seed=1))
        d = {(c["name"], c["x"]): c for c in cells}
        c = d[("direct", "primary")]
        self.assertEqual((c["state"], c["missing"], c["value"]), ("partial_not_run", ["band"], 0.5))
        self.assertNotIn("band", c)
        self.assertEqual(c["source"], "kit:a4_edges")
        self.assertEqual(d[("direct", "lenient")]["state"], "not_run")
        self.assertEqual(len(cells), 14)

    def test_lenient_cells_are_the_four(self):
        m = {**Z, "n_universe": 2, "eligible": 2, "direct": 1, "undirected": 1, "path_le2": 1}
        cells = pl.setting_cells(self.res([m, dict(m)], dict.fromkeys(pl.KINDS, 0.5)), "lenient", B=100, seed=2)
        self.assertEqual([(c["name"], c["x"]) for c in cells],
                         [("endpoint_lost", "lenient"), ("eligible_share", "lenient"),
                          ("eligible_edges", "lenient"), ("direct", "lenient")])


class TestKitSignaturePins(unittest.TestCase):
    """a4 calls private kit functions. A rename or a changed signature must fail HERE, not deep inside an export."""

    def params(self, fn):
        return [(n, p.kind.name, p.default is inspect.Parameter.empty)
                for n, p in inspect.signature(fn).parameters.items()]

    def test_attested_matching(self):
        self.assertEqual([n for n, _, _ in self.params(metrics._attested_matching)],
                         ["p", "spec", "arm", "setting", "min_words"])

    def test_nj(self):
        self.assertEqual([n for n, _, _ in self.params(metrics._nj)], ["p", "spec"])

    def test_edge_recovered(self):
        self.assertEqual([n for n, _, _ in self.params(match.edge_recovered)], ["ref_edge", "ai", "m"])

    def test_provenance_eligible(self):
        self.assertEqual([n for n, _, _ in self.params(provenance.eligible)], ["p", "arm", "min_words"])

    def test_buckets_and_spec_arm(self):
        self.assertEqual([n for n, _, _ in self.params(metrics.buckets)],
                         ["p", "spec_key", "arm", "setting", "min_words"])
        self.assertEqual(self.params(metrics.buckets)[-1][1], "KEYWORD_ONLY")
        self.assertEqual([n for n, _, _ in self.params(metrics.spec_arm)], ["spec_key"])


class TestOnDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-pl-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.demo = Path(cls._tmp.name) / "demo"
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(cls.demo),
                            "--client", "oracle"], cwd=REPO, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.demo)
        cls.cells = pl.row4_cells(cls.ctx)

    def by(self):
        return {(c["name"], c.get("x")): c for c in self.cells}

    def test_row4_names_match_skeleton(self):
        want = [(c["name"], c["x"]) for c in skeleton_row4()]
        self.assertEqual([(c["name"], c["x"]) for c in self.cells], want)
        for c, s in zip(self.cells, skeleton_row4()):
            self.assertEqual((c.get("unit"), c.get("kind")), (s["unit"], s["kind"]), c["name"])

    def test_direct_denominator_is_eligible(self):
        g = self.by()
        for x in ("primary", "lenient"):
            self.assertEqual(g[("direct", x)]["n"], g[("eligible_edges", x)]["value"])
            self.assertGreater(g[("eligible_edges", x)]["value"], 0)
        for name in ("undirected", "path_le2", "direct_null_expected", "direct_over_null"):
            self.assertEqual(g[(name, "primary")]["n"], g[("eligible_edges", "primary")]["value"])
        for name in ("endpoint_lost", "collapsed", "eligible_share", "eligible_edges"):
            self.assertEqual(g[(name, "primary")]["n"], g[("eligible_edges", "primary")]["n"])

    def test_universe_is_the_kits_edge_denominator(self):
        """n_universe at each setting equals the sum over analysed maps of the kit's buckets n_edges_eligible."""
        arm = metrics.spec_arm(self.ctx.spec)
        mw = int((self.ctx.cfg.stats or {}).get("min_words", 8))
        g = self.by()
        for x, setting in pl.ROW4_SETTINGS.items():
            tot = sum(metrics.buckets(p, self.ctx.spec, arm, setting, min_words=mw)["n_edges_eligible"]
                      for p in self.ctx.analysed)
            self.assertEqual(g[("eligible_edges", x)]["n"], tot, x)

    def test_shares_close_the_universe(self):
        g = self.by()
        s = sum(g[(n, "primary")]["value"] for n in ("endpoint_lost", "collapsed", "eligible_share"))
        self.assertAlmostEqual(s, 1.0, delta=0.0015)

    def test_lenient_cells_use_lex06_close(self):
        """The lenient cells are computed at MatchSetting(0.6, same_or_close), not at the profile's lex .4."""
        arm = metrics.spec_arm(self.ctx.spec)
        mw = int((self.ctx.cfg.stats or {}).get("min_words", 8))
        g = self.by()
        tot = sum(metrics.buckets(p, self.ctx.spec, arm, MatchSetting(0.6, "same_or_close"),
                                  min_words=mw)["n_edges_eligible"] for p in self.ctx.analysed)
        self.assertEqual(g[("eligible_edges", "lenient")]["n"], tot)

    def test_row4_analyses_exactly_primary_and_lex06_close(self):
        """The demo cannot tell lex .4 from lex .6 apart (same edges), so check which settings row4_cells asks for."""
        from unittest import mock
        seen = []
        real = pl.analyse

        def spy(ctx, setting):
            seen.append(setting)
            return real(ctx, setting)
        with mock.patch.object(pl, "analyse", spy):
            pl.row4_cells(self.ctx)
        self.assertEqual(seen, [MatchSetting(0.6, "same"), MatchSetting(0.6, "same_or_close")])
        self.assertNotIn(pb.SETTINGS["lenient"], seen)

    def test_rates_in_range_and_band(self):
        g = self.by()
        for n in ("direct", "undirected", "path_le2"):
            c = g[(n, "primary")]
            self.assertTrue(0.0 <= c["ci_lo"] <= c["value"] <= c["ci_hi"] <= 1.0, n)
            self.assertEqual(c["ci_level"], 0.95)
        self.assertGreaterEqual(g[("undirected", "primary")]["value"], g[("direct", "primary")]["value"])
        self.assertGreaterEqual(g[("path_le2", "primary")]["value"], g[("direct", "primary")]["value"])
        b = g[("direct", "primary")]["band"]
        self.assertEqual(b["over"], "lenient")
        vals = [g[("direct", "primary")]["value"], g[("direct", "lenient")]["value"]]
        self.assertEqual((b["lo"], b["hi"]), (min(vals), max(vals)))
        self.assertTrue(all("band" not in c for c in self.cells if (c["name"], c["x"]) != ("direct", "primary")))

    def test_row_meta(self):
        meta = pl.row4_meta(self.cells)
        self.assertEqual(meta["rests_on"], pl.ROW4_RESTS_ON)
        self.assertEqual(meta["qualifiers"], pl.ROW4_QUALIFIERS)
        self.assertEqual(meta["band"], self.by()[("direct", "primary")]["band"])
        self.assertEqual(meta["source"], ["kit:a4_edges"])

    def test_every_cell_source_is_the_kit_analysis(self):
        """R11: a lab's cells never name a cleared/ id."""
        self.assertEqual(pl.SOURCE, "kit:a4_edges")
        self.assertEqual({c["source"] for c in self.cells}, {"kit:a4_edges"})
        self.assertEqual(len(self.cells), 14)
        self.assertNotIn("cleared/", json.dumps(self.cells))

    def test_no_pid_or_hash_in_cells(self):
        text = json.dumps(self.cells)
        for p in self.ctx.p_list:
            self.assertNotIn(p.pid, text)
        self.assertIsNone(re.search(r"[0-9a-fA-F]{64}", text))

    def test_deterministic(self):
        self.assertEqual(json.dumps(pl.row4_cells(self.ctx), sort_keys=True),
                         json.dumps(self.cells, sort_keys=True))

    def test_kit_return_shapes(self):
        """The shapes map_units relies on (beyond the parameter names pinned above)."""
        p = self.ctx.analysed[0]
        spec = self.ctx.spec
        arm = metrics.spec_arm(spec)
        m, m_att, att = metrics._attested_matching(p, spec, arm, pb.SETTINGS["primary"], 8)
        self.assertTrue(hasattr(m, "pairs") and hasattr(m_att, "pairs") and hasattr(m_att, "right_matched"))
        self.assertIsInstance(metrics._nj(p, spec), set)
        some = next(iter(p.prov[spec].values()))
        ok, why = provenance.eligible(some, arm, 8)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(why, str)
        ed = p.endorsed.edges[0]
        self.assertIsInstance(match.edge_recovered(ed, p.ai[spec], m_att), bool)
        for attr in ("glued", "source", "target"):
            self.assertTrue(hasattr(ed, attr))
        self.assertTrue(hasattr(p, "in_transcript"))


@unittest.skipUnless(os.environ.get("SS_PARITY_DIR"), "SS_PARITY_DIR not set")
class TestParity(unittest.TestCase):
    """Row 4 cells against the analysis pack's values on the same synthetic demo (3 decimals, matched on (row, name, x))."""

    def test_row4_matches_profile_synth(self):
        pdir = Path(os.environ["SS_PARITY_DIR"])
        ref = json.loads((pdir / "profile-synth.json").read_text(encoding="utf-8"))
        want = {(c["name"], c.get("x")): c for r in ref["rows"] if r["row"] == 4 for c in r["cells"]}
        with tempfile.TemporaryDirectory(prefix="ss-pl-par-") as td:
            demo = Path(td) / "demo"
            r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(demo),
                                "--client", "oracle"], cwd=REPO, capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            cells = pl.row4_cells(pb.open_context(Path(DEMO_CONFIG), demo))
        got = {(c["name"], c.get("x")): c for c in cells}
        self.assertEqual(set(got), set(want))
        bad = []
        for key, w in want.items():
            g = got[key]
            for f in ("value", "n", "ci_lo", "ci_hi"):
                if w.get(f) is None and g.get(f) is None:
                    continue
                if w.get(f) is None or g.get(f) is None or abs(w[f] - g[f]) > 0.0005:
                    bad.append((key, f, w.get(f), g.get(f)))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
