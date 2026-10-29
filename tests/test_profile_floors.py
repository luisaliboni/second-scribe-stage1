"""profile_floors: the lead, rows 5 and 7 and the f1 / r12 ported functions (Task 4; the labeller numbers and row 6 are
tested in test_profile_labellers and test_profile_slant). Two synthetic runs, zero model calls: see tests/profile_fixture.py.
"""
import dataclasses
import json
import math
import os
import unittest
from pathlib import Path

from second_scribe import profile_base as pb
from second_scribe import profile_floors as pf
from second_scribe import profile_labellers as lb
from second_scribe import profile_slant as sl
from tests.profile_fixture import SKELETON, by_name, contexts, names, skeleton_names
from tests.profile_fixture import REPO

DEMO = GRID = None          # see tests/profile_fixture.py


def setUpModule():
    global DEMO, GRID
    DEMO, GRID = contexts()


class TestNames(unittest.TestCase):
    def test_lead_names_match_skeleton(self):
        self.assertEqual(names(pf.lead_cells(GRID)), skeleton_names("lead"))
        self.assertEqual(len(pf.lead_cells(GRID)), 25)

    def test_row5_names_match_skeleton(self):
        self.assertEqual(names(pf.row5_cells(DEMO)), skeleton_names(5))

    def test_row7_names_match_skeleton(self):
        self.assertEqual(names(pf.row7_cells(GRID)), skeleton_names(7))

    def test_names_also_match_without_the_grid(self):
        self.assertEqual(names(pf.lead_cells(DEMO)), skeleton_names("lead"))
        self.assertEqual(names(pf.row7_cells(DEMO)), skeleton_names(7))
        self.assertEqual(names(pf.row5_cells(GRID)), skeleton_names(5))

    def test_unit_and_kind_match_skeleton(self):
        for row, cells in (("lead", pf.lead_cells(GRID)), (5, pf.row5_cells(DEMO)), (7, pf.row7_cells(GRID))):
            rows = {r["row"]: r for r in SKELETON["rows"]}
            for sk, c in zip(rows[row]["cells"], cells):
                with self.subTest(row=row, name=sk["name"], x=sk.get("x")):
                    self.assertEqual((c.get("unit"), c.get("kind")), (sk["unit"], sk["kind"]))


class TestLead(unittest.TestCase):
    def test_listening_added_equals_ai_minus_prior(self):
        cells = pf.lead_cells(GRID)
        for x in ("primary", "lenient"):
            ai, pr = by_name(cells, "ai_recall", x), by_name(cells, "no_interview_recall", x)
            la = by_name(cells, "listening_added", x)
            self.assertNotIn("state", la)
            self.assertAlmostEqual(la["value"], ai["value"] - pr["value"], delta=0.0011)

    def test_interval_brackets_value(self):
        for c in pf.lead_cells(GRID):
            if "ci_lo" in c:
                self.assertLessEqual(c["ci_lo"], c["value"] + 1e-9, c["name"])
                self.assertGreaterEqual(c["ci_hi"], c["value"] - 1e-9, c["name"])
                self.assertEqual(c["ci_level"], 0.95)

    def test_f1_cells_are_computed_without_the_grid_spec(self):
        # Ruling R15: no gate. The shipped demo has no stability-grid spec and still gets every f1 cell
        for cells in (pf.lead_cells(DEMO), pf.row7_cells(DEMO)):
            for c in cells[:13]:
                self.assertNotEqual(c.get("reason_code"), "grid_spec_missing", c["name"])
                self.assertNotEqual(c.get("state"), "not_run", (c["name"], c.get("x")))
        self.assertFalse(hasattr(pf, "grid_missing"))
        for fn in (pf.lead_cells, pf.row7_cells):
            with self.assertRaises(TypeError):
                fn(DEMO, require_grid=False)

    def test_the_values_do_not_depend_on_the_grid_spec(self):
        a = pf.lead_cells(DEMO)
        b = pf.lead_cells(GRID)
        self.assertEqual(names(a), names(b))
        for ca, cb in zip(a, b):
            for key in ("value", "ci_lo", "ci_hi", "n"):
                self.assertEqual(ca.get(key), cb.get(key), (ca["name"], ca.get("x"), key))

    def test_no_interview_size_cells_have_no_ci(self):
        for ctx in (DEMO, GRID):
            cells = pf.lead_cells(ctx)
            for nm in ("no_interview_nodes_per_map", "no_interview_edges_per_map", "no_interview_over_ai_nodes"):
                c = by_name(cells, nm)
                self.assertNotIn("ci_lo", c)
                self.assertNotIn("ci_hi", c)
                self.assertIsNone(c["ci_level"])
                self.assertEqual(c["note"], "pooled_only")
                self.assertIn("pooled_only", c["qualifiers"])
                self.assertIn("size_beside_lead", c["qualifiers"])
            for nm in ("ai_nodes_per_map", "ai_edges_per_map", "endorsed_nodes_per_map",
                       "ai_minus_no_interview_nodes_per_map", "ai_minus_no_interview_edges_per_map"):
                self.assertIn("ci_lo", by_name(cells, nm), nm)

    def test_derived_cells(self):
        cells = pf.lead_cells(GRID)
        for nm, x in (("yield_per_box", "ai"), ("yield_per_box", "no_interview"), ("equal_size_bound", "no_interview"),
                      ("listening_edge_equal_size", "no_interview")):
            c = by_name(cells, nm, x)
            self.assertIs(c["derived"], True, nm)
            self.assertIn("not_a_test", c["qualifiers"])
            self.assertIn("size_beside_lead", c["qualifiers"])
            self.assertEqual(c["formula_code"], {"yield_per_box": f"yield_per_box:{x}"}.get(nm, nm))
            self.assertNotIn("formula", c)
            self.assertNotIn("ci_lo", c)
        ai = by_name(cells, "ai_recall", "primary")["value"]
        bound = by_name(cells, "equal_size_bound", "no_interview")["value"]
        edge = by_name(cells, "listening_edge_equal_size", "no_interview")["value"]
        self.assertAlmostEqual(edge, ai - bound, delta=0.0011)
        for nm in ("equal_size_bound", "listening_edge_equal_size"):
            self.assertIn("floor_not_estimate", by_name(cells, nm, "no_interview")["qualifiers"])

    def test_wrong_transcript_band_names_its_labellers(self):
        c = by_name(pf.row7_cells(GRID), "wrong_transcript_difference", "primary")
        self.assertEqual(c["band"]["over"], "labellers")
        self.assertEqual([m["x"] for m in c["band"]["members"]], ["judge", "keyword"])
        judge = c["band"]["members"][0]["value"]
        self.assertAlmostEqual(judge, c["value"], delta=1e-9)

    def test_size_claim_is_reported_never_raised(self):
        claims = pf.size_claims(DEMO)
        self.assertIs(claims["prior_map_smaller"], False)       # the demo's CI of AI minus no-interview nodes covers 0
        self.assertIn("covers_all_maps", claims)

    def test_same_cells_in_lead_and_row7(self):
        lead = {(c["name"], c.get("x")): c for c in pf.lead_cells(GRID)}
        for c in pf.row7_cells(GRID):
            self.assertEqual(lead[(c["name"], c.get("x"))], c)


class TestRow5(unittest.TestCase):
    def test_absence_counts_close(self):
        cells = pf.row5_cells(DEMO)
        n = by_name(cells, "n_ideals", "primary")["value"]
        self.assertGreater(n, 0)
        total = sum(by_name(cells, nm, "primary")["value"]
                    for nm in ("recovered_as_wished", "flattened", "counterfactual", "missed", "unanchored"))
        self.assertEqual(total, n)
        self.assertEqual(by_name(cells, "n_matched", "primary")["value"],
                         by_name(cells, "recovered_as_wished", "primary")["value"] + 0
                         + pf.absence_counts(DEMO, "primary")["leaked_as_existing"])

    def test_unanchored_derived_names_the_cleared_counts(self):
        """L-1: the schema, the pack and profile_ladder call this derivation sum_of_cleared_counts."""
        c = by_name(pf.row5_cells(DEMO), "unanchored", "primary")
        self.assertEqual(c["derived"], "sum_of_cleared_counts")

    def test_counts_equal_the_kit(self):
        from second_scribe import metrics
        cells = pf.row5_cells(DEMO)
        for x in ("primary", "lenient"):
            kit = metrics._pooled_absences(DEMO.analysed, DEMO.spec, pb.SETTINGS[x], 8)
            self.assertEqual(by_name(cells, "recovered_as_wished", x)["value"], kit["recovered_as_wished"])
            self.assertEqual(by_name(cells, "flattened", x)["value"], kit["adequacy_flattening"])
            self.assertEqual(by_name(cells, "counterfactual", x)["value"], kit["counterfactual_leakage"])
            self.assertEqual(by_name(cells, "missed", x)["value"], kit["missed"])

    def test_counterfactual_upper_bound_zero_count(self):
        cells = pf.row5_cells(DEMO)
        self.assertEqual(by_name(cells, "counterfactual", "primary")["value"], 0)     # the demo has none
        for x in ("all_ideals", "matched", "matched_lenient"):
            c = by_name(cells, "counterfactual_upper_bound", x)
            self.assertIn("zero_count", c["qualifiers"], x)
            self.assertGreater(c["value"], 0, x)
            self.assertEqual(c["ci_level"], 0.95)
            self.assertNotIn("ci_lo", c)
        # exact one-sided 95% bound of 0 of n is 1 - .05 ** (1 / n)
        n = by_name(cells, "n_ideals", "primary")["value"]
        self.assertAlmostEqual(by_name(cells, "counterfactual_upper_bound", "all_ideals")["value"],
                               1 - 0.05 ** (1 / n), places=3)

    def test_flattening_rate_has_an_exact_interval(self):
        c = by_name(pf.row5_cells(DEMO), "flattening_rate", "primary")
        self.assertEqual(c["value"], 0.0)
        self.assertEqual(c["ci_lo"], 0.0)
        self.assertGreater(c["ci_hi"], 0.0)

    def test_matcher_free_rate(self):
        cells = pf.row5_cells(DEMO)
        j, k, j2 = (by_name(cells, "matcher_free_rate", x) for x in ("judge", "keyword", "judge2"))
        self.assertNotIn("state", j)
        self.assertNotIn("state", k)
        self.assertEqual((j2["state"], j2["needs"]), ("not_run", ["J2"]))
        self.assertEqual(j["band"]["over"], "labellers")
        self.assertEqual([m["x"] for m in j["band"]["members"]], ["judge", "keyword"])

    def test_recovery_rate_matches_pooled(self):
        cells = pf.row5_cells(DEMO)
        n = by_name(cells, "n_ideals", "primary")["value"]
        rec = by_name(cells, "recovered_as_wished", "primary")["value"]
        self.assertAlmostEqual(by_name(cells, "recovery_rate", "primary")["value"], rec / n, places=3)


class TestPrivacyAndDeterminism(unittest.TestCase):
    def test_no_pid_or_map_id_in_any_cell(self):
        text = json.dumps([pf.lead_cells(GRID), pf.row5_cells(GRID), sl.row6_cells(GRID), pf.row7_cells(GRID),
                           lb.labeller_numbers(GRID)])
        for p in GRID.p_list:
            self.assertNotIn(p.pid, text)
            self.assertNotIn(p.role, text.replace("rank", ""))
        for bad in ("map_id", "interview"):
            self.assertNotIn(f'"{bad}"', text)

    def test_two_calls_give_the_same_bytes(self):
        a = json.dumps([pf.lead_cells(GRID), pf.row5_cells(GRID), sl.row6_cells(GRID)], sort_keys=True)
        b = json.dumps([pf.lead_cells(GRID), pf.row5_cells(GRID), sl.row6_cells(GRID)], sort_keys=True)
        self.assertEqual(a, b)

    def test_source_is_a_kit_script_name_never_a_cleared_id(self):
        # Ruling R11: "kit:<analysis script>", derived cells join the names with "+"
        import re
        pat = re.compile(r"^kit:[a-z0-9_]+(\+kit:[a-z0-9_]+)*$")
        seen = set()
        for ctx in (DEMO, GRID):
            for cells in (pf.lead_cells(ctx), pf.row5_cells(ctx), sl.row6_cells(ctx), pf.row7_cells(ctx)):
                for c in cells:
                    self.assertRegex(c["source"], pat, (c["name"], c.get("x")))
                    seen.add(c["source"])
        self.assertIn("kit:f1_floors_checks", seen)
        self.assertIn("kit:r12_prior_size", seen)
        self.assertIn("kit:f1_floors_checks+kit:r12_prior_size", seen)
        self.assertFalse([s for s in seen if "cleared" in s])

    def test_every_not_run_cell_has_needs_or_a_reason_code(self):
        for cells in (pf.lead_cells(DEMO), pf.row5_cells(DEMO), sl.row6_cells(DEMO), pf.row7_cells(DEMO)):
            for c in cells:
                if c.get("state") == "not_run":
                    self.assertTrue(c["needs"] or c.get("reason_code"), c)
                    self.assertFalse({"reason", "note"} & set(c), c)


class TestPortedFunctions(unittest.TestCase):
    """The ported pure functions against the kit's own aggregate functions (the analysis scripts' built-in checks)."""

    def test_prior_route_equals_the_kit(self):
        from second_scribe import metrics
        mw = 8
        for sk in ("primary", "lenient"):
            for attested in (False, True):
                rows, _units = pf.prior_units(GRID.analysed, GRID.spec, pb.SETTINGS[sk], mw, attested)
                ka, kb, _ex = metrics._prior_rows(GRID.analysed, GRID.spec, pb.SETTINGS[sk], mw, attested)
                used = [r for r in rows if r is not None]
                self.assertEqual(len(used), len(ka))
                for r, a, b in zip(used, ka, kb):
                    self.assertAlmostEqual(r[0] / r[1], a, places=12)
                    self.assertAlmostEqual(r[2] / r[3], b, places=12)

    def test_shuffled_route_equals_the_kit(self):
        from second_scribe import metrics
        hk = pf._hk(GRID)
        dseed = pf.derange_seed(hk)
        _rows, (real, shuf), _dropped = pf.shuffled_parts(GRID.analysed, GRID.spec, pb.SETTINGS["primary"], 8,
                                                          hk["n_derangements"], dseed)
        kreal, kshuf = metrics._shuffled_pairs_rows(GRID.analysed, GRID.spec, pb.SETTINGS["primary"], dseed,
                                                    hk["n_derangements"], 8, {})[:2]
        self.assertEqual(len(real), len(kreal))
        for a, b in zip(real + shuf, kreal + kshuf):
            self.assertAlmostEqual(a, b, places=12)

    def test_permutation_rank_is_the_kits(self):
        from second_scribe import metrics
        hk = pf._hk(GRID)
        ctl = metrics.controls(GRID.analysed, pb.SETTINGS["primary"], spec_key=GRID.spec,
                               seed=metrics._seed(hk["seed"], "controls"), n_derangements=hk["n_derangements"],
                               min_words=8, B=50)["permutation_rank"]
        c = by_name(pf.row7_cells(GRID), "own_map_mean_rank", "primary")
        self.assertAlmostEqual(c["value"], ctl["mean_rank"], places=3)
        self.assertAlmostEqual(by_name(pf.row7_cells(GRID), "rank_null_expectation", "primary")["value"],
                               ctl["expected_rank_null"], places=3)

    def test_one_sided_upper_bound(self):
        self.assertAlmostEqual(pf.one_sided_upper_bound(0, 90), 1 - 0.05 ** (1 / 90), places=12)
        self.assertAlmostEqual(pf.one_sided_upper_bound(0, 32), 0.0894, places=4)
        self.assertTrue(math.isnan(pf.one_sided_upper_bound(0, 0)) or pf.one_sided_upper_bound(0, 0) != pf.one_sided_upper_bound(0, 0))

    def test_part_status(self):
        self.assertEqual(pf.part_status(0, 43), "full")
        self.assertEqual(pf.part_status(5, 43), "suppressed")
        self.assertEqual(pf.part_status(10, 43), "partial")
        self.assertEqual(pf.part_status(33, 43), "partial")
        self.assertEqual(pf.part_status(34, 43), "suppressed")


class TestSmallPopulation(unittest.TestCase):
    def test_ten_maps_run_without_error(self):
        ctx = dataclasses.replace(GRID, analysed=GRID.analysed[:10])
        self.assertEqual(names(pf.lead_cells(ctx)), skeleton_names("lead"))
        self.assertEqual(names(pf.row5_cells(ctx)), skeleton_names(5))

    def test_two_maps_do_not_crash(self):
        ctx = dataclasses.replace(GRID, analysed=GRID.analysed[:2])
        self.assertEqual(names(pf.lead_cells(ctx)), skeleton_names("lead"))


class TestSplitModules(unittest.TestCase):
    """Ruling R17 + R19: the file is split into profile_common (leaf) / profile_f1 / profile_labellers / profile_slant /
    profile_floors without an import cycle and without lazy imports; profile_floors still exports the moved names."""

    def test_labeller_functions_are_reexported(self):
        self.assertIs(pf.labeller_numbers, lb.labeller_numbers)
        self.assertIs(pf.keyword_labeller, lb.keyword_labeller)
        from second_scribe.profile_floors import keyword_labeller, labeller_numbers      # noqa: F401
        with self.assertRaises(AttributeError):
            pf.no_such_function

    def test_moved_names_are_reexported_from_floors(self):
        from second_scribe import profile_common as pc
        from second_scribe import profile_f1 as f1
        for name in ("K_CELL", "EPS", "LABS", "SRC", "_finite", "_mean", "_ci_defined", "part_status", "_nr", "_est", "_hk"):
            self.assertIs(getattr(pf, name), getattr(pc, name), name)
        for name in ("derange_seed", "shuffled_parts", "stat_real", "stat_shuf", "stat_diff", "_in_ids", "ai_counts",
                     "prior_counts", "prior_units", "n_left_out_part1", "stat_ai", "stat_prior", "stat_ai_minus_prior",
                     "floor1_numbers", "_floor1", "_floor2", "_permutation", "one_sided_upper_bound"):
            self.assertIs(getattr(pf, name), getattr(f1, name), name)

    def test_no_lazy_imports_and_no_module_getattr(self):
        import ast
        for mod in ("profile_common", "profile_f1", "profile_floors", "profile_labellers", "profile_slant"):
            tree = ast.parse((REPO / "second_scribe" / f"{mod}.py").read_text(encoding="utf-8"))
            nested = [n for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for n in ast.walk(f) if isinstance(n, (ast.Import, ast.ImportFrom))]
            self.assertEqual(nested, [], mod)
            self.assertNotIn("__getattr__", [n.name for n in tree.body if isinstance(n, ast.FunctionDef)], mod)

    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        import subprocess
        import sys
        for mod in ("profile_common", "profile_f1", "profile_floors", "profile_labellers", "profile_slant"):
            r = subprocess.run([sys.executable, "-c", f"import second_scribe.{mod} as m; print(m.__name__)"],
                               cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (mod, r.stderr))

@unittest.skipUnless(os.environ.get("SS_PARITY_DIR"), "SS_PARITY_DIR not set")
class TestParity(unittest.TestCase):
    """Cells against the analysis pack's values on the same synthetic demo (parity-synth/profile-synth.json), matched on
    (row, name, x) to 3 decimals. Rows 5 and 6 come from the shipped demo; the lead and row 7 are checked on the shipped
    demo (no stability-grid spec: Ruling R15) and on the grid run."""

    def test_parity_on_the_shipped_demo(self):
        self._parity(DEMO)

    def test_parity_on_the_grid_run(self):
        self._parity(GRID)

    def _parity(self, lead_ctx):
        par = json.loads((Path(os.environ["SS_PARITY_DIR"]) / "profile-synth.json").read_text(encoding="utf-8"))
        ref = {(str(r["row"]), c["name"], c.get("x")): c for r in par["rows"] for c in r["cells"]}
        mine = {}
        for tag, cells in (("lead", pf.lead_cells(lead_ctx)), ("5", pf.row5_cells(DEMO)),
                           ("6", sl.row6_cells(DEMO)), ("7", pf.row7_cells(lead_ctx))):
            mine.update({(tag, c["name"], c.get("x")): c for c in cells})
        compared = 0
        for key, c in mine.items():
            pc = ref.get(key)
            with self.subTest(key=key):
                self.assertIsNotNone(pc, "cell not in the parity file")
                if pc.get("state") == "not_run" or c.get("state") == "not_run":
                    continue
                for f in ("value", "ci_lo", "ci_hi", "ci_level", "n", "p", "p_lt"):
                    a, b = c.get(f), pc.get(f)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        self.assertAlmostEqual(a, b, delta=0.0005, msg=f)
                    else:
                        self.assertEqual(a, b, f)
                for f in ("unit", "kind"):
                    self.assertEqual(c.get(f), pc.get(f), f)
                compared += 1
        self.assertGreater(compared, 55)


if __name__ == "__main__":
    unittest.main()
