"""profile_base: the settings, the pooled statistics, the cell constructors and the run context of the profile exporter.

The expected numbers of the statistics tests were produced ONCE by executing the source of the analysis pack's
common.py by hand (never imported here); they are literals so that a change to the port shows up as a failing test.
"""
import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from second_scribe import profile_base as pb
from second_scribe.cli import DEMO_CONFIG
from second_scribe.match import MatchSetting

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "tests" / "data" / "profile_v2_skeleton.json"

NUMS = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8]          # a fixed 12-cluster toy (one (num, den) pair per map)
DENS = [10, 8, 12, 7, 15, 20, 9, 14, 11, 10, 13, 18]


class TestSettings(unittest.TestCase):
    def test_settings_match_a1(self):
        # a1_recall_matchers.MATCHERS (word, lexical threshold, judge_accept), copied as literals
        a1 = (("lexical_only_04", 0.4, "none"), ("lexical_only_05", 0.5, "none"),
              ("lexical_only_06", 0.6, "none"), ("lexical_only_07", 0.7, "none"),
              ("judge_only_same", 1.01, "same"), ("judge_only_same_or_close", 1.01, "same_or_close"),
              ("primary", 0.6, "same"), ("lenient", 0.4, "same_or_close"))
        self.assertEqual(list(pb.SETTINGS), [w for w, _, _ in a1])
        for word, lex, acc in a1:
            with self.subTest(word):
                self.assertIsInstance(pb.SETTINGS[word], MatchSetting)
                self.assertEqual((pb.SETTINGS[word].lex_threshold, pb.SETTINGS[word].judge_accept), (lex, acc))

    def test_judge_using_is_the_four_judge_settings(self):
        self.assertEqual(pb.JUDGE_USING, {"judge_only_same", "judge_only_same_or_close", "primary", "lenient"})
        self.assertTrue(pb.JUDGE_USING <= set(pb.SETTINGS))
        for key in pb.JUDGE_USING:
            self.assertNotEqual(pb.SETTINGS[key].judge_accept, "none")
        for key in set(pb.SETTINGS) - pb.JUDGE_USING:
            self.assertEqual(pb.SETTINGS[key].judge_accept, "none")

    def test_setting_labels_of_the_skeleton_are_settings(self):
        """The x labels of the profile files for the 8 settings (primary, lenient, lexical_only_04) are SETTINGS keys."""
        sk = json.loads(SKELETON.read_text(encoding="utf-8"))
        xs = {c["x"] for r in sk["rows"] for c in r["cells"] if "x" in c}
        self.assertTrue({"primary", "lenient", "lexical_only_04"} <= xs)
        self.assertTrue({"primary", "lenient", "lexical_only_04"} <= set(pb.SETTINGS))

    def test_defaults(self):
        self.assertEqual((pb.B, pb.SEED, pb.N_PERM), (2000, 20261017, 10000))


class TestSkeleton(unittest.TestCase):
    def test_skeleton_shape(self):
        sk = json.loads(SKELETON.read_text(encoding="utf-8"))
        self.assertEqual([r["row"] for r in sk["rows"]], ["lead", "ledger", *range(1, 10)])
        self.assertEqual(sum(len(r["cells"]) for r in sk["rows"]), 147)
        by_row = {r["row"]: len(r["cells"]) for r in sk["rows"]}
        self.assertEqual((by_row["lead"], by_row[2]), (25, 9))
        for r in sk["rows"]:
            self.assertEqual(set(r), {"row", "label_key", "cells"})
            for c in r["cells"]:
                self.assertTrue(set(c) <= {"name", "x", "unit", "kind"})
                self.assertIn("name", c)

    def test_skeleton_holds_no_numeric_values(self):
        sk = json.loads(SKELETON.read_text(encoding="utf-8"))
        self.assertEqual(set(sk), {"rows"})

        def leaves(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k, v
                    yield from leaves(v)
            elif isinstance(o, list):
                for v in o:
                    yield from leaves(v)
        for r in sk["rows"]:
            for c in r["cells"]:
                for k, v in leaves(c):
                    self.assertIsInstance(v, str, (c["name"], k))
                self.assertFalse({"value", "n", "ci_lo", "ci_hi", "ci_level", "band", "p", "p_lt", "state"} & set(c))
            self.assertIsInstance(r["label_key"], str)
        self.assertNotIn("run", sk)


class TestStats(unittest.TestCase):
    def test_pooled_ratio(self):
        self.assertEqual(pb.pooled_ratio(NUMS, DENS), 0.35374149659863946)
        self.assertTrue(math.isnan(pb.pooled_ratio([1, 2], [0, 0])))

    def test_cluster_bootstrap_ratio_matches_common(self):
        got = pb.cluster_bootstrap_ratio(NUMS, DENS)
        self.assertEqual(got[3], 2000)
        self.assertEqual(got, (0.35374149659863946, 0.29133858267716534, 0.4012345679012346, 2000))
        got = pb.cluster_bootstrap_ratio(NUMS, DENS, B=200, seed=7, alpha=0.10)
        self.assertEqual(got, (0.35374149659863946, 0.29770992366412213, 0.39156626506024095, 200))

    def test_cluster_bootstrap_ratio_drops_zero_denominator_replicates(self):
        got = pb.cluster_bootstrap_ratio([1, 0, 2, 0], [0, 0, 3, 0], B=50, seed=3)
        self.assertEqual(got, (1.0, 0.6666666666666666, 1.3333333333333333, 33))

    def test_cluster_bootstrap_ratio_edges(self):
        self.assertEqual(pb.cluster_bootstrap_ratio([], [])[3], 0)
        with self.assertRaises(ValueError):
            pb.cluster_bootstrap_ratio([1, 2], [1])

    def test_cluster_bootstrap_is_a_fresh_stream_per_call(self):
        a = pb.cluster_bootstrap_ratio(NUMS, DENS, B=100)
        pb.cluster_bootstrap_ratio(NUMS, DENS, B=100, seed=1)
        self.assertEqual(pb.cluster_bootstrap_ratio(NUMS, DENS, B=100), a)

    def test_cluster_bootstrap_general_statistic(self):
        got = pb.cluster_bootstrap(NUMS, lambda m: sum(m) / len(m), B=300, seed=11)
        self.assertEqual(got, (4.333333333333333, 2.9166666666666665, 5.75, 300))

    def test_clopper_pearson_known(self):
        lo, hi = pb.clopper_pearson(0, 90, 0.90)
        self.assertEqual((lo, hi), (0.0, 0.03273803383353463))
        self.assertAlmostEqual(hi, 1 - 0.05 ** (1 / 90), places=12)        # the closed form of the one-sided bound
        lo, hi = pb.clopper_pearson(4, 90)
        self.assertEqual((lo, hi), (0.01224052785001134, 0.10990082687801367))
        self.assertEqual(pb.clopper_pearson(90, 90, 0.90), (0.9672619661664654, 1.0))
        self.assertTrue(all(math.isnan(v) for v in pb.clopper_pearson(0, 0)))
        with self.assertRaises(ValueError):
            pb.clopper_pearson(5, 4)

    def test_wilson_known(self):
        self.assertEqual(pb.wilson(4, 90), (0.01741724185270873, 0.1087685503281223))
        self.assertTrue(all(math.isnan(v) for v in pb.wilson(0, 0)))

    def test_holm_order(self):
        # adjusted p-values come back in INPUT order, monotone in the sorted order, capped at 1
        self.assertEqual(pb.holm([0.01, 0.04, 0.03, 0.005]), [0.03, 0.06, 0.06, 0.02])
        self.assertEqual(pb.holm([0.5, 0.6]), [1.0, 1.0])
        self.assertEqual(pb.holm([]), [])

    def test_permutation_within_map_matches_common(self):
        groups = [(["a", "a", "b", "b"], [1, 1, 0, 0]), (["a", "b", "a", "b"], [1, 0, 0, 1]),
                  (["a", "a", "b", "b", "b"], [1, 1, 1, 0, 0])]

        def diff(g):
            a = [o for labs, outs in g for lab, o in zip(labs, outs) if lab == "a"]
            b = [o for labs, outs in g for lab, o in zip(labs, outs) if lab == "b"]
            return sum(a) / len(a) - sum(b) / len(b)
        self.assertEqual(pb.permutation_within_map(groups, diff, n_perm=500, seed=5, alternative="greater"),
                         (0.5476190476190477, 0.12974051896207583))
        with self.assertRaises(ValueError):
            pb.permutation_within_map(groups, diff, n_perm=5, alternative="sideways")

    def test_r3(self):
        self.assertEqual(pb.r3(0.12345), 0.123)
        with self.assertRaises(ValueError):
            pb.r3(math.nan)


class TestCell(unittest.TestCase):
    def test_cell_minimal_keys(self):
        c = pb.cell("kept", 0.90912, x="primary", n=121, unit="node", kind="share", ci=(0.8617, 0.9553))
        self.assertEqual(c, {"name": "kept", "x": "primary", "n": 121, "unit": "node", "kind": "share",
                             "value": 0.909, "ci_lo": 0.862, "ci_hi": 0.955, "ci_level": 0.95})
        bare = pb.cell("ai_boxes", 110, unit="ai_box", kind="count")
        self.assertEqual(bare, {"name": "ai_boxes", "unit": "ai_box", "kind": "count", "value": 110,
                                "ci_level": None})
        self.assertIsInstance(bare["value"], int)

    def test_cell_ci_level(self):
        c = pb.cell("u", 0.03, ci=(0.0, 0.033), ci_level=0.9, kind="upper_bound")
        self.assertEqual((c["ci_lo"], c["ci_hi"], c["ci_level"]), (0.0, 0.033, 0.9))
        self.assertEqual(pb.cell("u", 0.03, ci_level=0.9)["ci_level"], 0.9)

    def test_cell_p_zero_becomes_p_lt(self):
        c = pb.cell("own_map_mean_rank", 1.0, p=0.0)
        self.assertEqual(c["p_lt"], 0.001)
        self.assertNotIn("p", c)
        # a p that the kit would print as 0.000 is written the same way
        c = pb.cell("x", 1.0, p=0.0001)
        self.assertEqual((c.get("p_lt"), "p" in c), (0.001, False))
        c = pb.cell("x", 1.0, p=0.1297405)
        self.assertEqual((c.get("p"), "p_lt" in c), (0.13, False))
        self.assertEqual(pb.cell("x", 1.0, p=1.0)["p"], 1.0)

    def test_cell_p_holm(self):
        c = pb.cell("d", 0.0, p=1.0, p_holm=0.99951)
        self.assertEqual((c["p"], c["p_holm"]), (1.0, 1.0))
        self.assertEqual(pb.cell("d", 0.0, p=0.01, p_holm=0.0301)["p_holm"], 0.03)
        self.assertNotIn("p_holm", pb.cell("d", 0.0, p=0.01))

    def test_cell_p_holm_zero_becomes_p_holm_lt(self):
        c = pb.cell("d", 0.5, p=0.0, p_holm=0.0004)
        self.assertEqual((c["p_lt"], c["p_holm_lt"]), (0.001, 0.001))
        self.assertNotIn("p_holm", c)
        self.assertNotIn("p_holm_lt", pb.cell("d", 0.5, p_holm=0.002))

    def test_cell_source_formula_note(self):
        c = pb.cell("yield_per_box", 0.614, source="cleared/003", formula_code="yield_per_box:ai", note="pooled_only",
                    derived=True)
        self.assertEqual((c["source"], c["formula_code"], c["note"], c["derived"]),
                         ("cleared/003", "yield_per_box:ai", "pooled_only", True))
        bare = pb.cell("a", 1)
        self.assertFalse({"source", "formula_code", "formula", "note"} & set(bare))
        p = pb.partial_not_run("a", 1, missing=["band"], source="cleared/013", formula_code="f", note="n")
        self.assertEqual((p["source"], p["formula_code"], p["note"], p["state"]), ("cleared/013", "f", "n", "partial_not_run"))

    def test_cell_optional_fields_copied(self):
        b = pb.band(0.0, 0.909, "8_settings")
        c = pb.cell("kept", 0.5, band=b, rests_on=["R", "LX"], qualifiers=["one_draw"], derived="per_map_mean")
        self.assertEqual((c["band"], c["rests_on"], c["qualifiers"], c["derived"]),
                         (b, ["R", "LX"], ["one_draw"], "per_map_mean"))
        self.assertEqual(pb.cell("y", 0.5, derived=True)["derived"], True)

    def test_cell_refuses_non_finite_and_booleans(self):
        for bad in (math.nan, math.inf, None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pb.cell("z", bad)
        with self.assertRaises(ValueError):
            pb.cell("z", 0.5, ci=(math.nan, 0.6))

    def test_cell_is_deterministic_json(self):
        a = json.dumps(pb.cell("k", 1 / 3, ci=(1 / 7, 2 / 3), p=0.0123456), sort_keys=True)
        self.assertEqual(a, json.dumps(pb.cell("k", 1 / 3, ci=(1 / 7, 2 / 3), p=0.0123456), sort_keys=True))
        self.assertIn('"value": 0.333', a)


class TestNotRun(unittest.TestCase):
    def test_not_run_shape(self):
        c = pb.not_run("attested_absent_from_map", ["J2", "sidejudge"], x="labellers", unit="ai_box", kind="count")
        self.assertEqual(c, {"name": "attested_absent_from_map", "x": "labellers", "unit": "ai_box", "kind": "count",
                             "value": None, "state": "not_run", "needs": ["J2", "sidejudge"]})
        self.assertIsNone(c["value"])
        self.assertEqual(c["state"], "not_run")
        self.assertEqual(pb.not_run("a", ["ladder"])["needs"], ["ladder"])

    def test_not_run_needs_use_the_four_codes(self):
        for code in ("J2", "sidejudge", "ladder", "judge_script"):
            self.assertEqual(pb.not_run("a", [code])["needs"], [code])
        with self.assertRaises(ValueError):
            pb.not_run("a", ["gpu"])
        with self.assertRaises(ValueError):
            pb.not_run("a", "ladder")           # a list, not a string

    def test_not_run_reason_code_and_source(self):
        c = pb.not_run("a", ["J2"], reason_code="needs_J2", source="cleared/018")
        self.assertEqual((c["reason_code"], c["source"], c["state"], c["value"]),
                         ("needs_J2", "cleared/018", "not_run", None))
        bare = pb.not_run("a", ["J2"])
        self.assertFalse({"reason", "reason_code", "source", "note"} & set(bare))

    def test_not_run_without_needs_requires_a_reason_code(self):
        for bad in ([], ()):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pb.not_run("a", bad)
        with self.assertRaises(ValueError):
            pb.not_run("a", [], source="no reason code")      # nothing but a source does not make a reason
        self.assertEqual(pb.not_run("a", ["ladder"])["needs"], ["ladder"])

    def test_not_run_reason_code(self):
        c = pb.not_run("f", [], reason_code="grid_spec_missing")
        self.assertEqual((c["needs"], c["reason_code"]), ([], "grid_spec_missing"))
        self.assertNotIn("note", c)


class TestPartialNotRun(unittest.TestCase):
    def test_partial_not_run_shape(self):
        c = pb.partial_not_run("wrong_transcript_difference", 0.0261, missing=["band"], x="primary", n=121,
                               unit="node", kind="difference", ci=(0.01, 0.045))
        self.assertEqual(c, {"name": "wrong_transcript_difference", "x": "primary", "n": 121, "unit": "node",
                             "kind": "difference", "value": 0.026, "ci_lo": 0.01, "ci_hi": 0.045, "ci_level": 0.95,
                             "state": "partial_not_run", "missing": ["band"]})

    def test_partial_not_run_keeps_a_value_and_names_what_is_missing(self):
        self.assertEqual(pb.partial_not_run("u", 2, missing=["n", "band"])["missing"], ["n", "band"])
        for bad in ([], "band", [""], [None]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pb.partial_not_run("u", 2, missing=bad)
        with self.assertRaises(ValueError):                       # the value must exist and be finite
            pb.partial_not_run("u", None, missing=["band"])


class TestBands(unittest.TestCase):
    def test_band_plain(self):
        self.assertEqual(pb.band(0.587, 0.587, "lenient"), {"lo": 0.587, "hi": 0.587, "over": "lenient"})
        self.assertEqual(pb.band(0, 108, "8_settings")["lo"], 0)

    def test_band_with_members_and_subbands(self):
        members = [{"x": "primary", "value": 0.909}, {"x": "lexical_only_07", "value": 0.0}]
        sub = [{"name": "judge_using", "lo": 0.909, "hi": 0.909}]
        b = pb.band(0.0, 0.909, "8_settings", narrow=True, members=members, subbands=sub)
        self.assertEqual(b, {"lo": 0.0, "hi": 0.909, "over": "8_settings", "narrow": True,
                             "members": members, "subbands": sub})
        members.append({"x": "extra", "value": 1})
        self.assertEqual(len(b["members"]), 2)                 # a copy, not the caller's list

    def test_band_over_is_one_of_the_five(self):
        for over in ("8_settings", "lenient", "labellers", "ci95", "variants"):
            self.assertEqual(pb.band(0.1, 0.2, over)["over"], over)
        with self.assertRaises(ValueError):
            pb.band(0.1, 0.2, "everything")
        with self.assertRaises(ValueError):
            pb.band(0.3, 0.2, "lenient")

    def test_labeller_band_names_each_labeller(self):
        members = [{"x": "judge", "value": 0.9}, {"x": "judge2", "value": 0.7}, {"x": "keyword", "value": 0.4}]
        b = pb.labeller_band(members)
        self.assertEqual((b["over"], b["lo"], b["hi"]), ("labellers", 0.4, 0.9))
        self.assertEqual([m["x"] for m in b["members"]], ["judge", "judge2", "keyword"])
        self.assertEqual(pb.labeller_band(members, narrow=False)["narrow"], False)
        self.assertNotIn("narrow", b)

    def test_labeller_band_refuses_empty_or_repeated(self):
        with self.assertRaises(ValueError):
            pb.labeller_band([])
        with self.assertRaises(ValueError):
            pb.labeller_band([{"x": "judge", "value": 1}, {"x": "judge", "value": 2}])
        with self.assertRaises(ValueError):
            pb.labeller_band([{"x": "judge"}])


class TestOpenContext(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-pb-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.demo = Path(cls._tmp.name) / "demo"
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(cls.demo),
                            "--client", "oracle"], cwd=REPO, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.demo)

    def test_open_context_demo(self):
        ctx = self.ctx
        self.assertEqual(len(ctx.p_list), 12)
        self.assertEqual(len(ctx.analysed), 12)
        self.assertEqual((ctx.B, ctx.seed), (2000, 20261017))
        self.assertEqual(ctx.out_dir, self.demo.resolve())
        self.assertEqual(ctx.spec, ctx.cfg.recon_specs()[0].key())
        self.assertTrue(all(p.ai.get(ctx.spec) is not None for p in ctx.analysed))

    def test_context_keeps_exclusions_and_judging(self):
        self.assertIsInstance(self.ctx.exclusions, dict)
        self.assertIsInstance(self.ctx.judging, dict)
        self.assertIn(self.ctx.spec, self.ctx.judging)
        self.assertEqual(self.ctx.judging[self.ctx.spec]["n_items"] > 0, True)

    def test_context_is_frozen_and_overridable(self):
        with self.assertRaises(FrozenInstanceError):
            self.ctx.B = 1
        small = pb.open_context(Path(DEMO_CONFIG), self.demo, B=50, seed=1)
        self.assertEqual((small.B, small.seed), (50, 1))

    def test_analysed_excludes_maps_without_the_primary_ai_map(self):
        ps = self.ctx.p_list
        spec = self.ctx.spec
        self.assertEqual([p.pid for p in self.ctx.analysed], [p.pid for p in ps if p.ai.get(spec) is not None])

    def test_open_context_reads_only(self):
        before = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*") if p.is_file())
        pb.open_context(Path(DEMO_CONFIG), self.demo)
        after = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*") if p.is_file())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
