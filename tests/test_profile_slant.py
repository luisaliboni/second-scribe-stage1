"""profile_slant: row 6 (pull cells and the a7 scripted share) and the ported a5 / a7 functions (Task 4). Two synthetic
runs, zero model calls: see tests/profile_fixture.py.
"""
import dataclasses
import math
import unittest
from unittest import mock

from second_scribe import profile_base as pb
from second_scribe import profile_slant as sl
from tests.profile_fixture import SKELETON, by_name, contexts, names, skeleton_names

DEMO = GRID = None          # see tests/profile_fixture.py


def setUpModule():
    global DEMO, GRID
    DEMO, GRID = contexts()


class TestNames(unittest.TestCase):
    def test_row6_names_match_skeleton(self):
        self.assertEqual(names(sl.row6_cells(DEMO)), skeleton_names(6))
        self.assertEqual(names(sl.row6_cells(GRID)), skeleton_names(6))

    def test_unit_and_kind_match_skeleton(self):
        rows = {r["row"]: r for r in SKELETON["rows"]}
        for sk, c in zip(rows[6]["cells"], sl.row6_cells(DEMO)):
            with self.subTest(name=sk["name"], x=sk.get("x")):
                self.assertEqual((c.get("unit"), c.get("kind")), (sk["unit"], sk["kind"]))


class TestRow6(unittest.TestCase):
    def test_judge_script_cells_not_run_without_a_judgescript_source(self):
        # the shipped demo declares a judgescript source now; this is the run that declares none
        no_source = dataclasses.replace(DEMO, cfg=dataclasses.replace(DEMO.cfg, script_sources=[]))
        cells = sl.row6_cells(no_source)
        for nm in ("scripted_share_difference", "largest_excess_ruled_out"):
            c = by_name(cells, nm, "other_family_script")
            self.assertEqual(c["state"], "not_run")
            self.assertEqual(c["needs"], ["judge_script"])
            self.assertIsNone(c["value"])
            self.assertNotIn("reason", c)                    # needs says it; no sentence in a lab export
            self.assertNotIn("reason_code", c)

    def test_judge_script_cells_on_the_demo_stop_for_an_empty_group(self):
        # the demo declares the source, so nothing is needed; a7 stops because no AI box is attested-and-absent
        cells = sl.row6_cells(DEMO)
        for nm in ("scripted_share_difference", "largest_excess_ruled_out"):
            c = by_name(cells, nm, "other_family_script")
            self.assertEqual(c["state"], "not_run")
            self.assertEqual(c["needs"], [])
            self.assertEqual(c["reason_code"], "group_empty")

    def test_own_script_scripted_share_cells_are_not_run_when_a_group_is_empty(self):
        # on the demo no AI box is attested-and-absent, so S(T) is undefined and a7 stops: never an invented value
        for ctx in (DEMO, GRID):
            cells = sl.row6_cells(ctx)
            for nm, x in (("scripted_share_unanchored", "own_script"), ("scripted_share_attested_absent", "own_script"),
                          ("scripted_share_difference", "own_script")):
                c = by_name(cells, nm, x)
                self.assertEqual((c["state"], c["value"]), ("not_run", None), nm)
                self.assertTrue(c["reason_code"])
                self.assertNotIn("note", c)
        c = by_name(sl.row6_cells(GRID), "scripted_share_difference", "other_family_script")
        self.assertEqual(c["state"], "not_run")
        self.assertNotIn("judge_script", c["needs"])        # the grid run declares a judgescript source

    def test_pull_cells(self):
        cells = sl.row6_cells(DEMO)
        pull = by_name(cells, "pull", "canonical")
        self.assertEqual(pull["ci_level"], 0.95)
        self.assertEqual(by_name(cells, "pull_90", "canonical")["ci_level"], 0.9)
        self.assertEqual(pull["band"]["over"], "variants")
        vals = [by_name(cells, "pull_variant", v)["value"] for v in ("raw_wording", "judge_only", "in_transcript_only")]
        self.assertAlmostEqual(pull["band"]["lo"], min(vals + [pull["value"]]), places=9)
        self.assertAlmostEqual(pull["band"]["hi"], max(vals + [pull["value"]]), places=9)
        for c in cells:
            if c["name"] in ("pull", "pull_90", "pull_variant"):
                self.assertEqual(c["n"], len(DEMO.analysed))


class TestPullBandNeedsTwoMembers(unittest.TestCase):
    """M-1 (Ruling R16): the ``pull`` band over variants needs at least 2 members, else the cell is partial_not_run."""

    @staticmethod
    def _cells(covering):
        real = sl.pull_variant

        def fake(maps, spec, ps, scripts, name, B=pb.B, seed=pb.SEED):
            out = real(maps, spec, ps, scripts, name, B=B, seed=seed)
            if name not in covering:                       # a variant that does not cover all the maps
                out = dict(out, n=len(maps) - 1)
            return out
        with mock.patch.object(sl, "pull_variant", side_effect=fake):
            return sl.row6_cells(DEMO)

    def test_one_covering_variant_gives_partial_not_run_missing_band(self):
        cells = self._cells({"canonical"})
        pull = by_name(cells, "pull", "canonical")
        self.assertEqual((pull["state"], pull["missing"]), ("partial_not_run", ["band"]))
        self.assertNotIn("band", pull)
        self.assertIsNotNone(pull["value"])                # the value and its interval are still computed
        self.assertIn("ci_lo", pull)
        for v in ("raw_wording", "judge_only", "in_transcript_only"):
            self.assertEqual(by_name(cells, "pull_variant", v)["reason_code"], "variant_does_not_cover_all_maps")

    def test_two_covering_variants_give_a_two_member_band(self):
        cells = self._cells({"canonical", "judge_only"})
        pull = by_name(cells, "pull", "canonical")
        self.assertNotIn("state", pull)
        self.assertEqual(pull["band"]["over"], "variants")
        self.assertLess(pull["band"]["lo"], pull["band"]["hi"] + 1e-12)


class TestPortedFunctions(unittest.TestCase):
    """The ported a7 functions."""

    def test_a7_analyse_by_hand(self):
        # 12 identical maps: U = {u1 scripted, u1 not}, T = {1 scripted of 3}, K = {1}: S(U) = 1/2, S(T) = 1/3
        u = [{"nodes": [("u1", 1), ("u1", 0), ("T", 1), ("T", 0), ("T", 0), ("K", 1)], "end": (1, 2)}] * 12
        r = sl.analyse(u, B=100, n_perm=200, seed=3)
        self.assertAlmostEqual(r["shares"]["U"][0], 0.5)
        self.assertAlmostEqual(r["shares"]["T"][0], 1 / 3)
        self.assertAlmostEqual(r["delta"][0], 1 / 6)
        self.assertAlmostEqual(r["shares"]["KT"][0], 2 / 4)
        self.assertAlmostEqual(r["delta_kt"][0], 0.0)
        self.assertEqual(r["counts"]["U"], (12, 24))
        self.assertEqual(r["counts"]["E"], (12, 24))
        self.assertTrue(r["enough_maps"])
        self.assertEqual(r["ci_undefined"], {"u2", "u3"})     # the empty groups have no interval
        self.assertAlmostEqual(r["shares"]["U"][1], 0.5)          # identical maps: a degenerate interval
        d = sl.decide(r, r)
        self.assertTrue(d["enough_maps_contribute"])
        self.assertFalse(d["no_sign_of_it"])                     # lo > 0 here (degenerate interval above 0)
        empty = sl.analyse([{"nodes": [("u1", 1)], "end": (0, 1)}] * 12, B=50, n_perm=50, seed=1)
        self.assertTrue(math.isnan(empty["shares"]["T"][0]))
        self.assertTrue(math.isnan(empty["delta"][0]))


class TestScriptedShareWhenGroupsExist(unittest.TestCase):
    """The shipped demo has no attested-and-absent AI box, so a7 stops there. Rebuild a run where half the maps carry
    another person's endorsed map (so their AI boxes match nothing) and check the cells are computed and consistent."""

    @classmethod
    def setUpClass(cls):
        ms = GRID.analysed
        new = []
        for i, p in enumerate(ms):
            q = ms[(i + 3) % len(ms)]
            if i % 2 == 0:
                p = dataclasses.replace(
                    p, endorsed=q.endorsed, in_transcript=q.in_transcript, canon_endorsed=q.canon_endorsed,
                    judged_endorsed_vs_script_canon=q.judged_endorsed_vs_script_canon,
                    judged_endorsed_vs_script=q.judged_endorsed_vs_script,
                    judged_ai_vs_endorsed={k: {} for k in p.judged_ai_vs_endorsed})
            new.append(p)
        cls.ctx = dataclasses.replace(GRID, analysed=new)
        cls.cells = sl.row6_cells(cls.ctx)

    def test_own_script_cells_are_computed(self):
        u = by_name(self.cells, "scripted_share_unanchored", "own_script")
        t = by_name(self.cells, "scripted_share_attested_absent", "own_script")
        d = by_name(self.cells, "scripted_share_difference", "own_script")
        for c in (u, t, d):
            self.assertNotIn("state", c)
            self.assertIsNotNone(c["value"])
        self.assertTrue(0 <= u["value"] <= 1 and 0 <= t["value"] <= 1)
        self.assertAlmostEqual(d["value"], u["value"] - t["value"], delta=0.0011)
        self.assertGreater(t["n"], 0)
        self.assertEqual(d["n"], u["n"] + t["n"])
        self.assertEqual(u["rests_on"], ["R", "Q", "MO", "P", "CN", "AS", "LX"])
        self.assertEqual(t["rests_on"], ["R", "Q", "MO", "P", "CN", "AS", "LX", "AL"])
        self.assertEqual(d["rests_on"], ["R", "Q", "MO", "AL", "P", "CN", "AS", "LX"])

    def test_descriptive_only_survives_an_undefined_interval(self):
        """L-12: fewer maps than the minimum AND no interval -> the note keeps both codes, ci_undefined first."""
        real = sl.analyse

        def fake(units, B=pb.B, seed=pb.SEED):
            res = real(units, B=B, seed=seed)
            return dict(res, enough_maps=False, ci_undefined=set(res["ci_undefined"]) | {"delta"})
        with mock.patch.object(sl, "analyse", side_effect=fake):
            cells = sl.row6_cells(self.ctx)
        d = by_name(cells, "scripted_share_difference", "own_script")
        self.assertEqual(d["note"], "ci_undefined;descriptive_only")
        self.assertNotIn("p", d)
        self.assertNotIn("ci_lo", d)

    def test_partly_lexical_follows_the_data(self):
        ps = pb.SETTINGS["primary"]
        own = sl.metrics.scripts_by_role(self.ctx.p_list, canonical=True)
        partly = False
        for p in self.ctx.analysed:
            _sa, _se, fb_ai, fb_end = sl.own_canonical_flags(p, self.ctx.spec, ps, own[p.role])
            partly = partly or fb_ai or fb_end
        d = by_name(self.cells, "scripted_share_difference", "own_script")
        self.assertEqual("partly_lexical" in (d.get("qualifiers") or []), bool(partly))
        for nm in ("scripted_share_unanchored", "scripted_share_attested_absent"):
            self.assertNotIn("partly_lexical", by_name(self.cells, nm, "own_script").get("qualifiers") or [])

    def test_partly_lexical_is_set_when_a_map_falls_back_to_the_lexical_reading(self):
        maps = list(self.ctx.analysed)
        maps[1] = dataclasses.replace(maps[1], judged_endorsed_vs_script_canon=None)
        cells = sl.row6_cells(dataclasses.replace(self.ctx, analysed=maps))
        d = by_name(cells, "scripted_share_difference", "own_script")
        self.assertNotIn("state", d)
        self.assertIn("partly_lexical", d["qualifiers"])

    def test_other_family_cells_never_claim_judge_script_when_declared(self):
        for nm in ("scripted_share_difference", "largest_excess_ruled_out"):
            c = by_name(self.cells, nm, "other_family_script")
            self.assertNotIn("judge_script", c.get("needs", []))
            self.assertTrue(c.get("state") != "not_run" or c["reason_code"])

    def test_without_a_judgescript_source_the_other_family_needs_it(self):
        no_source = dataclasses.replace(DEMO.cfg, script_sources=[])
        cells = sl.row6_cells(dataclasses.replace(DEMO, analysed=self.ctx.analysed, cfg=no_source))
        for nm in ("scripted_share_difference", "largest_excess_ruled_out"):
            c = by_name(cells, nm, "other_family_script")
            self.assertEqual((c["state"], c["needs"]), ("not_run", ["judge_script"]))
        self.assertNotIn("state", by_name(cells, "scripted_share_attested_absent", "own_script"))


class TestSmallPopulation(unittest.TestCase):
    def test_ten_maps_run_without_error(self):
        ctx = dataclasses.replace(GRID, analysed=GRID.analysed[:10])
        self.assertEqual(names(sl.row6_cells(ctx)), skeleton_names(6))


if __name__ == "__main__":
    unittest.main()
