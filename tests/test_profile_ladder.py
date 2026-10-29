"""profile_ladder: row 8 (instruction ladder), the rejudge cells of row 9 and the rung-1 size of row 2.

The pure functions are ported from the analysis pack's a12_ladder.py and a8_nulls.py; their expected numbers below are
hand-computed literals. The cell tests run on the oracle demo (zero model calls), which has a ladder block; the
no-ladder case writes the same config without its ``ladder`` key to a tmp file.
"""
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from second_scribe import profile_base as pb
from second_scribe import profile_ladder as pl
from second_scribe.cli import DEMO_CONFIG

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "tests" / "data" / "profile_v2_skeleton.json"


def skeleton_row(row):
    sk = json.loads(SKELETON.read_text(encoding="utf-8"))
    return next(r for r in sk["rows"] if r["row"] == row)["cells"]


def names(cells):
    return [(c["name"], c.get("x")) for c in cells]


def demo_run(tmp, config_edit=None):
    """Run the oracle demo into ``tmp`` (optionally with an edited config written beside it) and open its context."""
    import subprocess
    import sys
    cfg_path = Path(DEMO_CONFIG)
    if config_edit is not None:
        d = json.loads(cfg_path.read_text(encoding="utf-8"))
        d["base_dir"] = str(cfg_path.parent)             # keep the corpus globs relative to configs/
        config_edit(d)
        cfg_path = Path(tmp) / "config.json"
        cfg_path.write_text(json.dumps(d), encoding="utf-8")
    out = Path(tmp) / "demo"
    r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--config", str(cfg_path), "--out", str(out),
                        "--client", "oracle"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return pb.open_context(cfg_path, out)


class TestWithLadder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = demo_run(cls._tmp.name)

    def test_has_ladder(self):
        self.assertTrue(pl.has_ladder(self.ctx))

    def test_row8_names_match_skeleton(self):
        cells = pl.row8_cells(self.ctx)
        self.assertEqual(names(cells), names(skeleton_row(8)))
        self.assertEqual(len(cells), 18)
        for c, s in zip(cells, skeleton_row(8)):
            self.assertEqual((c.get("unit"), c.get("kind")), (s["unit"], s["kind"]), c["name"])
            self.assertNotEqual(c.get("state"), "not_run", c["name"])

    def test_rejudge_names_match_skeleton(self):
        want = [(c["name"], c.get("x")) for c in skeleton_row(9) if c["name"].startswith("rejudge")]
        self.assertEqual(len(want), 5)
        self.assertEqual(names(pl.row9_rejudge_cells(self.ctx)), want)


class TestNoLadder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = demo_run(cls._tmp.name, lambda d: d.pop("ladder"))

    def test_no_ladder_cells_not_run(self):
        self.assertFalse(pl.has_ladder(self.ctx))
        cells = pl.row8_cells(self.ctx) + pl.row9_rejudge_cells(self.ctx)
        self.assertEqual(len(cells), 23)
        for c in cells:
            with self.subTest(c["name"], x=c.get("x")):
                self.assertEqual((c["state"], c["value"], c["needs"]), ("not_run", None, ["ladder"]))
        self.assertEqual(names(pl.row8_cells(self.ctx)), names(skeleton_row(8)))
        self.assertIsNone(pl.rung1_size(self.ctx))


class TestHolm(unittest.TestCase):
    def test_holm_applied_to_contrasts(self):
        """Rung 2 minus rung 1: p_holm is Holm over (size_adjusted_recall, differential_recall), never below p."""
        rows_a = [{"size_adjusted_recall": (3, 4), "ideal": (2, 2), "exp": (1, 2)} for _ in range(12)]
        rows_b = [{"size_adjusted_recall": (1, 4), "ideal": (0, 2), "exp": (1, 2)} for _ in range(12)]
        got = pl.c21_contrasts(rows_a, rows_b, B=200, seed=7, n_perm=200)
        p1, p2 = got["size_adjusted_recall"]["p"], got["differential_recall"]["p"]
        self.assertEqual(got["size_adjusted_recall"]["p_holm"], pb.holm([p1, p2])[0])
        self.assertEqual(got["differential_recall"]["p_holm"], pb.holm([p1, p2])[1])
        self.assertGreaterEqual(got["size_adjusted_recall"]["p_holm"], p1)
        self.assertAlmostEqual(got["size_adjusted_recall"]["p_holm"], min(1, 2 * p1))     # p1 is the smaller p of the pair
        self.assertGreater(got["size_adjusted_recall"]["p_holm"], p1 - 1e-12)
        self.assertLessEqual(max(got["size_adjusted_recall"]["p_holm"], got["differential_recall"]["p_holm"]), 1.0)
        # literal family size of 2: the smaller p is multiplied by 2
        self.assertAlmostEqual(min(got["size_adjusted_recall"]["p_holm"], got["differential_recall"]["p_holm"]),
                               min(1, 2 * min(p1, p2)))
        self.assertFalse(got["size_adjusted_recall"]["holm_family_lt_2"])

    def test_holm_family_shrinks_when_a_p_is_nan(self):
        """No ideals anywhere: differential recall is nan, its p is nan, so the family has one member: no Holm."""
        rows_a = [R(sar=(3, 4), ideal=(0, 0), exp=(1, 2)) for _ in range(12)]
        rows_b = [R(sar=(1, 4), ideal=(0, 0), exp=(1, 2)) for _ in range(12)]
        got = pl.c21_contrasts(rows_a, rows_b, B=200, seed=7, n_perm=200)
        self.assertTrue(math.isnan(got["differential_recall"]["p"]))
        self.assertTrue(got["size_adjusted_recall"]["holm_family_lt_2"])
        self.assertTrue(math.isnan(got["size_adjusted_recall"]["p_holm"]))




def R(sar=(1, 1), ideal=(0, 0), exp=(0, 0), failed=False, ai=(1, 1), fw=(0, 0), flip=(0, 0), actual=None):
    """A fabricated per-map row of a12's map_parts."""
    return {"size_adjusted_recall": sar, "ideal": ideal, "exp": exp, "failed": failed, "ai_node_count": ai,
            "false_wished_rate": fw, "flip_rate": flip, "actual_recall": actual or exp}


class TestPorts(unittest.TestCase):
    def test_max_matching(self):
        self.assertEqual(pl.max_matching({"a": ["x", "y"], "b": ["x"], "c": []}), 2)
        self.assertEqual(pl.max_matching({"a": ["x"], "b": ["x"], "c": ["x"]}), 1)
        self.assertEqual(pl.max_matching({"a": ["x", "y"], "b": ["x", "y"], "c": ["y", "z"]}), 3)
        self.assertEqual(pl.max_matching({}), 0)

    def test_located_changes(self):
        main = {"ends": [("n1", "wished"), ("n2", "experienced"), ("n3", "experienced"), ("n4", "wished")],
                "loc": {"n1": ["a"], "n3": ["b"]}}
        rung = {"ends": main["ends"], "loc": {"n2": ["a"], "n3": ["c"], "n4": ["d"]}}
        got = pl.located_changes([(main, rung)])
        self.assertEqual(got, {("ideal", "gained"): 1, ("ideal", "lost"): 1,
                               ("experienced", "gained"): 1, ("experienced", "lost"): 0})
        with self.assertRaises(ValueError):
            pl.located_changes([(main, {"ends": [], "loc": {}})])

    def test_filled_rows(self):
        self.assertEqual(pl.filled_rows(["r1", "r2", "r3"], ["m1", "m2", "m3"], [True, False, True]), ["r1", "m2", "r3"])

    def test_ratio_measure(self):
        rows = [R(sar=(1, 2), ideal=(1, 2), exp=(1, 4)), R(sar=(2, 2), ideal=(0, 0), exp=(3, 4))]
        self.assertAlmostEqual(pl.ratio(rows, "size_adjusted_recall"), 3 / 4)
        self.assertAlmostEqual(pl.measure(rows, "differential_recall"), 1 / 2 - 4 / 8)
        self.assertTrue(math.isnan(pl.measure([R()], "differential_recall")))      # no ideals: nan, not 0

    def test_holm_finite_keeps_nan(self):
        got = pl.holm_finite([0.01, math.nan, 0.04])
        self.assertAlmostEqual(got[0], 0.02)
        self.assertAlmostEqual(got[2], 0.04)
        self.assertTrue(math.isnan(got[1]))

    def test_mde80_and_testable(self):
        self.assertAlmostEqual(pl.mde80(-0.05, 0.05), 2.8 * 0.1 / 3.92)
        self.assertTrue(pl.testable_at_floor(0.10, -0.05, 0.05))
        self.assertFalse(pl.testable_at_floor(0.05, -0.05, 0.05))                   # below the 0.0714 floor
        self.assertFalse(pl.testable_at_floor(0.0, 0.0, 0.0))                       # zero-width interval, zero rate
        self.assertFalse(pl.testable_at_floor(0.5, math.nan, math.nan))             # no interval, no claim

    def test_leak_ratio_and_contrast(self):
        a = [{"leaked": (1, 2), "failed": False}, {"leaked": (0, 0), "failed": False}, {"leaked": (1, 2), "failed": False}]
        b = [{"leaked": (0, 2), "failed": False}, {"leaked": (0, 0), "failed": False}, {"leaked": (1, 2), "failed": False}]
        self.assertAlmostEqual(pl.leak_ratio(a), 0.5)
        c = pl.leak_contrast(a, b, B=200, seed=3, n_perm=200)
        self.assertAlmostEqual(c["value"], 0.5 - 0.25)
        self.assertTrue(0 < c["p"] <= 1)

    def test_contrast_is_deterministic_and_pooled(self):
        rows_a = [R(sar=(3, 4)), R(sar=(2, 4)), R(sar=(4, 4))]
        rows_b = [R(sar=(1, 4)), R(sar=(2, 4)), R(sar=(1, 4))]
        c1 = pl.contrast(rows_a, rows_b, "size_adjusted_recall", B=100, seed=5, n_perm=100)
        c2 = pl.contrast(rows_a, rows_b, "size_adjusted_recall", B=100, seed=5, n_perm=100)
        self.assertEqual(c1, c2)
        self.assertAlmostEqual(c1["value"], 9 / 12 - 4 / 12)
        self.assertIsNone(pl.contrast(rows_a, rows_b, "size_adjusted_recall", B=100, with_p=False)["p"])


def fake_ladder(rows_by_arm, leak, filled=(), changes=None):
    return {"configured": list(pl.RUNG_KEYS), "rows": rows_by_arm, "leak": leak, "filled": list(filled), "changes": changes}


class TestCellsFromFabricatedRows(unittest.TestCase):
    """The cell layer, with `_ladder_rows` patched to hand-built rows (the oracle demo gives identical rungs)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = demo_run(cls._tmp.name)

    def run_with(self, fake, fn):
        from unittest import mock
        with mock.patch.object(pl, "_ladder_rows", return_value=fake):
            return fn(self.ctx)

    def strong(self, n=24):
        a = [R(sar=(3, 4), ideal=(2, 2), exp=(1, 2), ai=(8, 1)) for _ in range(n)]
        b = [R(sar=(1, 4), ideal=(0, 2), exp=(1, 2), ai=(4, 1)) for _ in range(n)]
        lk = [{"leaked": (1, 2), "failed": False} for _ in range(n)]
        lp = [{"leaked": (0, 2), "failed": False} for _ in range(n)]
        return fake_ladder({"rung1": b, "rung2": a, "remedy": a, "placebo": b}, {"remedy": lk, "placebo": lp})

    def test_tiny_p_and_p_holm_are_lt(self):
        cells = {(c["name"], c.get("x")): c for c in self.run_with(self.strong(), pl.row8_cells)}
        c = cells[("size_adjusted_recall_2_minus_1", "rung2-rung1")]
        self.assertEqual(c["p_lt"], 0.001)
        self.assertNotIn("p", c)
        self.assertEqual(c["p_holm_lt"], 0.001)
        self.assertNotIn("p_holm", c)
        leak = cells[("leak_rate_remedy_minus_placebo", "remedy-placebo")]
        self.assertEqual(leak["p_lt"], 0.001)
        self.assertEqual(leak["n"], 48)
        self.assertAlmostEqual(leak["value"], 0.5)

    def test_holm_in_cell_matches_two_measure_family(self):
        fake = self.strong(8)
        a, b = fake["rows"]["rung2"], fake["rows"]["rung1"]
        got = pl.c21_contrasts(a, b, B=self.ctx.B, seed=self.ctx.seed)
        c = {(c["name"], c.get("x")): c for c in self.run_with(fake, pl.row8_cells)}[("size_adjusted_recall_2_minus_1", "rung2-rung1")]
        self.assertIn("p_holm", c)
        self.assertAlmostEqual(c["p_holm"], round(got["size_adjusted_recall"]["p_holm"], 3), places=3)

    def test_side_effect_contrasts_carry_no_p(self):
        cells = self.run_with(self.strong(), pl.row8_cells)
        for c in cells:
            if c["name"].endswith("remedy_minus_placebo") and c["name"] != "leak_rate_remedy_minus_placebo":
                self.assertNotIn("p", c)
                self.assertNotIn("p_lt", c)

    def test_mde80_cell_and_90_level(self):
        cells = {(c["name"], c.get("x")): c for c in self.run_with(self.strong(), pl.row8_cells)}
        c95 = cells[("leak_rate_remedy_minus_placebo", "remedy-placebo")]
        c90 = cells[("leak_rate_remedy_minus_placebo_90", "remedy-placebo")]
        self.assertEqual((c95["ci_level"], c90["ci_level"]), (0.95, 0.9))
        self.assertLessEqual(c95["ci_hi"] - c95["ci_lo"], 1.0)
        self.assertLessEqual(c90["ci_hi"] - c90["ci_lo"], c95["ci_hi"] - c95["ci_lo"] + 1e-9)
        m = cells[("mde80_leak_contrast", "remedy-placebo")]
        self.assertAlmostEqual(m["value"], round(2.8 * (c95["ci_hi"] - c95["ci_lo"]) / 3.92, 3), places=2)
        self.assertIsNone(m["ci_level"])

    def test_undefined_leak_is_not_run_with_a_note(self):
        fake = self.strong()
        fake["leak"] = {a: [{"leaked": (0, 0), "failed": False} for _ in range(24)] for a in ("remedy", "placebo")}
        cells = {(c["name"], c.get("x")): c for c in self.run_with(fake, pl.row8_cells)}
        for key in (("leak_rate", "remedy"), ("leak_rate_remedy_minus_placebo", "remedy-placebo"),
                    ("mde80_leak_contrast", "remedy-placebo")):
            self.assertEqual(cells[key]["state"], "not_run")
            self.assertEqual(cells[key]["needs"], [])
            self.assertTrue(cells[key]["reason_code"])
            self.assertNotIn("note", cells[key])

    def test_holm_family_note_on_the_cell(self):
        fake = self.strong()
        for arm in ("rung1", "rung2"):                              # no ideals: differential recall (and its p) is nan
            fake["rows"][arm] = [dict(r, ideal=(0, 0)) for r in fake["rows"][arm]]
        c = {(c["name"], c.get("x")): c for c in self.run_with(fake, pl.row8_cells)}[("size_adjusted_recall_2_minus_1", "rung2-rung1")]
        self.assertEqual(c["note"], "holm_family_lt_2")
        self.assertNotIn("p_holm", c)
        self.assertNotIn("p_holm_lt", c)
        self.assertTrue("p" in c or "p_lt" in c)

    def test_undefined_interval_is_noted(self):
        fake = self.strong()
        one = [{"leaked": (1, 1) if i == 0 else (0, 0), "failed": False} for i in range(24)]
        fake["leak"] = {"remedy": one, "placebo": one}
        cells = {(c["name"], c.get("x")): c for c in self.run_with(fake, pl.row8_cells)}
        c = cells[("leak_rate", "remedy")]
        self.assertEqual(c["value"], 1.0)
        self.assertEqual(c["note"], "ci_undefined")
        self.assertNotIn("ci_lo", c)
        self.assertEqual(cells[("mde80_leak_contrast", "remedy-placebo")]["state"], "not_run")

    def test_rejudge_counts(self):
        rows = [R(ideal=(1, 3), exp=(2, 10)) for _ in range(4)]            # 12 ideals, 40 lived steps
        ch = {("experienced", "gained"): 3, ("experienced", "lost"): 1, ("ideal", "gained"): 0, ("ideal", "lost"): 2}
        fake = fake_ladder({"rung2": rows}, {}, filled=[False] * 4, changes=ch)
        cells = {(c["name"], c["x"]): c for c in self.run_with(fake, pl.row9_rejudge_cells)}
        self.assertEqual(cells[("rejudge_changed", "lived_steps")]["value"], 4)
        self.assertEqual(cells[("rejudge_net", "lived_steps")]["value"], 2)
        self.assertEqual(cells[("rejudge_changed_share", "lived_steps")]["value"], 0.1)
        self.assertEqual(cells[("rejudge_changed_share", "lived_steps")]["n"], 40)
        self.assertEqual(cells[("rejudge_changed", "ideals")]["value"], 2)
        self.assertEqual(cells[("rejudge_net", "ideals")]["value"], -2)
        self.assertEqual(cells[("rejudge_net", "ideals")]["n"], 12)

    def test_rejudge_not_released_when_a_map_was_filled(self):
        rows = [R(ideal=(1, 3), exp=(2, 10)) for _ in range(4)]
        ch = {(g, d): 0 for g in ("ideal", "experienced") for d in ("gained", "lost")}
        fake = fake_ladder({"rung2": rows}, {}, filled=[False, True, False, False], changes=ch)
        for c in self.run_with(fake, pl.row9_rejudge_cells):
            self.assertEqual(c["source"], "kit:a12_ladder")
            self.assertEqual((c["state"], c["value"], c["needs"], c["reason_code"]),
                             ("not_run", None, ["ladder"], "rung2_filled_from_main"))


class TestRung1Size(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = demo_run(cls._tmp.name)

    def test_rung1_size_keys_and_values(self):
        got = pl.rung1_size(self.ctx)
        self.assertEqual(set(got), {"n_ai_nodes", "n_maps", "n_failed", "ai_per_map", "source"})
        self.assertEqual((got["n_maps"], got["n_failed"]), (len(self.ctx.analysed), 0))
        self.assertGreater(got["n_ai_nodes"], 0)
        self.assertAlmostEqual(got["ai_per_map"], got["n_ai_nodes"] / got["n_maps"])
        # the same number the ai_per_map cell of rung 1 carries
        c = next(c for c in pl.row8_cells(self.ctx) if (c["name"], c.get("x")) == ("ai_per_map", "rung1"))
        self.assertAlmostEqual(c["value"], got["ai_per_map"], places=3)

    def test_cells_are_read_only_and_deterministic(self):
        before = sorted(str(p.relative_to(self.ctx.out_dir)) for p in self.ctx.out_dir.rglob("*") if p.is_file())
        a, b = pl.row8_cells(self.ctx), pl.row8_cells(self.ctx)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        after = sorted(str(p.relative_to(self.ctx.out_dir)) for p in self.ctx.out_dir.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_source_is_kit_script_name(self):
        """Ruling R11: a lab cell's source is kit:<analysis script>, never cleared/NNN."""
        cells = pl.row8_cells(self.ctx) + pl.row9_rejudge_cells(self.ctx)
        self.assertEqual(pl.rung1_size(self.ctx)["source"], "kit:a12_ladder")
        for c in cells:
            want = "kit:a8_nulls" if c["name"].startswith(("leak", "mde80")) else "kit:a12_ladder"
            self.assertEqual(c["source"], want, c["name"])
        self.assertNotIn("cleared/", json.dumps(cells))

    def test_wording(self):
        text = json.dumps(pl.row8_cells(self.ctx) + pl.row9_rejudge_cells(self.ctx)).lower()
        for w in ("validated", "ground truth", "replicable", "first"):
            self.assertNotIn(w, text)


class TestFillAndFailedDraws(unittest.TestCase):
    """Amendment 7 and failed draws, on a copy of the demo run whose ladder stores are damaged."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        ctx = demo_run(cls._tmp.name)
        cls.base = ctx
        cls.copy = Path(cls._tmp.name) / "damaged"
        import shutil
        shutil.copytree(ctx.out_dir, cls.copy)
        cls.pid = sorted(p.pid for p in ctx.analysed)[0]
        for f in (cls.copy / "work" / "recon:rung2").glob(cls.pid + "__*.json"):
            f.unlink()                                         # rung 2's copy absent: filled from the main run
        for f in (cls.copy / "work" / "recon:rung1").glob(cls.pid + "__*.json"):
            f.write_text(json.dumps({"status": "failed", "map": None}), encoding="utf-8")     # attempted and failed
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.copy)

    def test_missing_rung2_copy_is_filled_not_failed(self):
        d = pl._ladder_rows(self.ctx)
        self.assertEqual(sum(d["filled"]), 1)
        self.assertFalse(any(r["failed"] for r in d["rows"]["rung2"]))
        want = pl._ladder_rows(self.base)["rows"]["rung2"]
        self.assertEqual(d["rows"]["rung2"], want)                 # the fill is the main run's row (oracle: same map)

    def test_rejudge_cells_not_released_after_a_fill(self):
        for c in pl.row9_rejudge_cells(self.ctx):
            self.assertEqual((c["state"], c["needs"], c["reason_code"]), ("not_run", ["ladder"], "rung2_filled_from_main"))

    def test_failed_rung1_map_is_a_failed_draw(self):
        d = pl._ladder_rows(self.ctx)
        self.assertEqual(sum(r["failed"] for r in d["rows"]["rung1"]), 1)
        got, base = pl.rung1_size(self.ctx), pl.rung1_size(self.base)
        self.assertEqual((got["n_failed"], base["n_failed"]), (1, 0))
        self.assertLess(got["n_ai_nodes"], base["n_ai_nodes"])
        cells = {(c["name"], c.get("x")): c for c in pl.row8_cells(self.ctx)}
        self.assertLess(cells[("ai_per_map", "rung1")]["value"], cells[("ai_per_map", "rung2")]["value"])


class TestRejudgeDifference(unittest.TestCase):
    """A rung-2 alignment that finds fewer partners than the main judging: the endorsed steps it loses are counted."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        base = demo_run(cls._tmp.name)
        cls.copy = Path(cls._tmp.name) / "tampered"
        import shutil
        shutil.copytree(base.out_dir, cls.copy)
        pid = sorted(p.pid for p in base.analysed)[0]
        f = cls.copy / "work" / "align:rung2" / f"{pid}__ai_endorsed__removed__p0__t0__r0.json"
        item = json.loads(f.read_text(encoding="utf-8"))
        cls.n_pairs = len(item["pairs"])
        item["pairs"] = []
        f.write_text(json.dumps(item), encoding="utf-8")
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.copy)

    def test_lost_partners_are_counted(self):
        d = pl._ladder_rows(self.ctx)
        self.assertFalse(any(d["filled"]))
        lost = sum(v for (g, k), v in d["changes"].items() if k == "lost")
        gained = sum(v for (g, k), v in d["changes"].items() if k == "gained")
        self.assertGreater(lost, 0)
        self.assertEqual(gained, 0)
        cells = {(c["name"], c["x"]): c for c in pl.row9_rejudge_cells(self.ctx)}
        self.assertEqual(cells[("rejudge_changed", "lived_steps")]["value"] + cells[("rejudge_changed", "ideals")]["value"], lost)
        self.assertEqual(cells[("rejudge_net", "lived_steps")]["value"] + cells[("rejudge_net", "ideals")]["value"], -lost)
        self.assertGreater(cells[("rejudge_changed_share", "lived_steps")]["value"] + cells[("rejudge_changed", "ideals")]["value"], 0)


def stripped_copy(tmp, base_ctx, stages):
    """A copy of the demo run with the named work stage dirs removed; returns its context."""
    import shutil
    copy = Path(tmp) / ("stripped-" + "-".join(sorted(s.replace(":", "_") for s in stages)))
    shutil.copytree(base_ctx.out_dir, copy)
    for st in stages:
        shutil.rmtree(copy / "work" / st)
    return pb.open_context(Path(DEMO_CONFIG), copy)


RUNG_STORES = [f"{st}:{r}" for r in ("rung1", "rung2", "rung3", "placebo") for st in ("recon", "align", "judge_modality")]


class TestUnfinishedLadder(unittest.TestCase):
    """Ruling R14: a configured ladder whose stores are missing fails closed (not_run, ladder_unfinished), no zeros."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.base = demo_run(cls._tmp.name)

    def assert_unfinished(self, c):
        self.assertEqual((c["state"], c["value"], c["needs"], c["reason_code"]), ("not_run", None, ["ladder"], "ladder_unfinished"),
                         (c["name"], c.get("x")))

    def test_all_rung_stores_removed(self):
        ctx = stripped_copy(self._tmp.name, self.base, RUNG_STORES)
        self.assertTrue(pl.has_ladder(ctx))                          # still configured
        cells = pl.row8_cells(ctx) + pl.row9_rejudge_cells(ctx)
        self.assertEqual(len(cells), 23)
        for c in cells:
            self.assert_unfinished(c)
        self.assertEqual(names(pl.row8_cells(ctx)), names(skeleton_row(8)))
        self.assertIsNone(pl.rung1_size(ctx))

    def test_only_rung2_stores_removed(self):
        ctx = stripped_copy(self._tmp.name, self.base, [f"{st}:rung2" for st in ("recon", "align", "judge_modality")])
        cells = {(c["name"], c.get("x")): c for c in pl.row8_cells(ctx)}
        self.assertEqual(list(cells), names(skeleton_row(8)))
        dead = {("ai_per_map", "rung2"), ("size_adjusted_recall", "rung2"), ("size_adjusted_recall_2_minus_1", "rung2-rung1")}
        for key, c in cells.items():
            if key in dead:
                self.assert_unfinished(c)
            else:
                self.assertNotEqual(c.get("state"), "not_run", key)
                self.assertNotIn("note", c, key)
        for c in pl.row9_rejudge_cells(ctx):
            self.assert_unfinished(c)
        self.assertEqual(pl.rung1_size(ctx)["n_failed"], 0)

    def test_only_remedy_stores_removed(self):
        ctx = stripped_copy(self._tmp.name, self.base, [f"{st}:rung3" for st in ("recon", "align", "judge_modality")])
        cells = {(c["name"], c.get("x")): c for c in pl.row8_cells(ctx)}
        dead = {("ai_per_map", "remedy"), ("size_adjusted_recall", "remedy"), ("leak_rate", "remedy"),
                ("lived_recall_remedy_minus_placebo", "remedy-placebo"), ("ai_nodes_remedy_minus_placebo", "remedy-placebo"),
                ("false_wished_remedy_minus_placebo", "remedy-placebo"), ("flip_remedy_minus_placebo", "remedy-placebo"),
                ("leak_rate_remedy_minus_placebo", "remedy-placebo"), ("leak_rate_remedy_minus_placebo_90", "remedy-placebo"),
                ("mde80_leak_contrast", "remedy-placebo")}
        for key, c in cells.items():
            if key in dead:
                self.assert_unfinished(c)
            else:
                self.assertNotEqual(c.get("state"), "not_run", key)

    def test_all_rung2_copies_different_is_unfinished(self):
        """Rung 2 copies present but none is the main map: 100% filled, nothing re-judged (R14)."""
        import shutil
        copy = Path(self._tmp.name) / "all-filled"
        shutil.copytree(self.base.out_dir, copy)
        for f in (copy / "work" / "recon:rung2").glob("*.json"):
            item = json.loads(f.read_text(encoding="utf-8"))
            item["map"]["nodes"][0]["text"] = item["map"]["nodes"][0]["text"] + " changed"
            f.write_text(json.dumps(item), encoding="utf-8")
        ctx = pb.open_context(Path(DEMO_CONFIG), copy)
        d = pl._ladder_rows(ctx)
        self.assertIn("rung2", d["unfinished"])
        for c in pl.row9_rejudge_cells(ctx):
            self.assert_unfinished(c)


class TestRung1Missing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.ctx = demo_run(cls._tmp.name, lambda d: d["ladder"].update(rungs=["rung2", "rung3", "placebo"]))

    def test_cells_that_read_rung1_are_not_run(self):
        self.assertTrue(pl.has_ladder(self.ctx))
        self.assertIsNone(pl.rung1_size(self.ctx))
        cells = {(c["name"], c.get("x")): c for c in pl.row8_cells(self.ctx)}
        self.assertEqual(list(cells), names(skeleton_row(8)))
        for key in (("ai_per_map", "rung1"), ("size_adjusted_recall", "rung1"), ("size_adjusted_recall_2_minus_1", "rung2-rung1")):
            self.assertEqual((cells[key]["state"], cells[key]["needs"]), ("not_run", ["ladder"]), key)
        for key in (("ai_per_map", "rung2"), ("leak_rate", "remedy"), ("mde80_leak_contrast", "remedy-placebo")):
            self.assertNotEqual(cells[key].get("state"), "not_run", key)


@unittest.skipUnless(os.environ.get("SS_PARITY_DIR"), "SS_PARITY_DIR not set")
class TestParity(unittest.TestCase):
    """Row 8 and the rejudge cells against the analysis pack's values on the same synthetic demo."""

    def test_matches_profile_synth(self):
        pdir = Path(os.environ["SS_PARITY_DIR"])
        ref = json.loads((pdir / "profile-synth.json").read_text(encoding="utf-8"))
        want = {(r["row"], c["name"], c.get("x")): c for r in ref["rows"] if r["row"] in (8, 9) for c in r["cells"]
                if r["row"] == 8 or c["name"].startswith("rejudge")}
        with tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5-par-") as td:
            ctx = demo_run(td)
            got = {(8, c["name"], c.get("x")): c for c in pl.row8_cells(ctx)}
            got.update({(9, c["name"], c.get("x")): c for c in pl.row9_rejudge_cells(ctx)})
        self.assertEqual(set(got), set(want))
        bad = []
        for key, w in want.items():
            g = got[key]
            for f in ("value", "n", "ci_lo", "ci_hi", "p", "p_holm", "p_lt", "p_holm_lt"):
                if w.get(f) is None and g.get(f) is None:
                    continue
                if w.get(f) is None or g.get(f) is None or abs(w[f] - g[f]) > 0.0005:
                    bad.append((key, f, w.get(f), g.get(f)))
        print("\nparity: %d cells compared, %d mismatches %s" % (len(want), len(bad), bad), file=__import__("sys").stderr)
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
