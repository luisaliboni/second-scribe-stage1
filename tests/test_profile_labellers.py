"""profile_labellers: keyword_labeller and labeller_numbers (Task 4). Two synthetic runs, zero model calls: see
tests/profile_fixture.py.
"""
import dataclasses
import math
import unittest

from second_scribe import profile_base as pb
from second_scribe import profile_floors as pf
from second_scribe import profile_labellers as lb
from tests.profile_fixture import by_name, contexts

DEMO = GRID = None          # see tests/profile_fixture.py


def setUpModule():
    global DEMO, GRID
    DEMO, GRID = contexts()


class TestLabellers(unittest.TestCase):
    METRICS = {"node_recall", "attested_absent_from_map", "unanchored", "matcher_free_rate",
               "contradiction_actual_as_wished", "contaminated_share", "wrong_transcript_difference"}

    def test_shape(self):
        out = lb.labeller_numbers(DEMO)
        self.assertEqual(set(out), {"judge", "keyword"})
        for lab, d in out.items():
            self.assertEqual(set(d), self.METRICS, lab)          # Ruling R16: all 7 keys, always
            for k, v in d.items():
                if v is not None:
                    self.assertIsInstance(v, (int, float), (lab, k))
                    self.assertTrue(math.isfinite(v), (lab, k))
        self.assertIsNotNone(out["judge"]["node_recall"])
        self.assertIsNotNone(out["judge"]["wrong_transcript_difference"])

    def test_a_metric_that_cannot_be_computed_is_none_not_absent(self):
        out = lb.labeller_numbers(dataclasses.replace(GRID, analysed=GRID.analysed[:1]))   # one map: no bootstrap/shuffle
        for lab, d in out.items():
            self.assertEqual(set(d), self.METRICS, lab)
        self.assertIsNone(out["judge"]["wrong_transcript_difference"])

    def test_labeller_band_needs_two_finite_members_and_narrow_is_a_bool(self):
        def nums(judge, kw, ci_ok=True):
            by = {"judge": {"point": judge, "ci_ok": ci_ok, "ci_lo": 0.1, "ci_hi": 0.5},
                  "keyword": {"point": kw, "ci_ok": True, "ci_lo": 0.0, "ci_hi": 1.0}}
            return {"k": {"by": by}}
        labs = ("judge", "keyword")
        self.assertIsNone(lb._labeller_band(nums(0.3, math.nan), "k", labs=labs))
        self.assertIsNone(lb._labeller_band(nums(math.nan, math.nan), "k", labs=labs))
        b = lb._labeller_band(nums(0.3, 0.32), "k", labs=labs)
        self.assertEqual([m["x"] for m in b["members"]], ["judge", "keyword"])
        self.assertIs(b["narrow"], True)
        self.assertIs(lb._labeller_band(nums(0.1, 0.9), "k", labs=labs)["narrow"], False)
        self.assertIs(lb._labeller_band(nums(0.3, 0.32, ci_ok=False), "k", labs=labs)["narrow"], False)

    def test_a_band_below_two_members_makes_the_cell_partial_not_run(self):
        from unittest import mock
        with mock.patch.object(pf, "_labeller_band", return_value=None):
            wt = by_name(pf.row7_cells(GRID), "wrong_transcript_difference", "primary")
            mf = by_name(pf.row5_cells(GRID), "matcher_free_rate", "judge")
        for c in (wt, mf):
            self.assertEqual((c["state"], c["missing"]), ("partial_not_run", ["band"]))
            self.assertNotIn("band", c)
            self.assertIsNotNone(c["value"])

    def test_judge_numbers_equal_the_kit(self):
        from second_scribe import metrics, pipeline
        kb, _ = pipeline._pooled(DEMO.analysed, DEMO.spec, pb.SETTINGS["primary"], 8)
        j = lb.labeller_numbers(DEMO)["judge"]
        # the demo has no unjudged node, so a1's denominator (M-2) equals the kit's matched + missed
        self.assertEqual(kb.get("n_endorsed_not_judged", 0), 0)
        self.assertAlmostEqual(j["node_recall"], kb["matched"] / (kb["matched"] + kb["missed"]), places=12)
        self.assertEqual(j["unanchored"], kb["unanchored"])
        self.assertEqual(j["attested_absent_from_map"], kb["attested_absent_from_map"])

    def test_keyword_labeller_returns_the_a9_numbers(self):
        out = lb.keyword_labeller(DEMO)
        for key in ("node_recall", "attested_absent", "unanchored", "contradiction_wm", "contradiction_am",
                    "contaminated", "shuf_real", "shuf_shuffled", "shuf_diff"):
            self.assertIn(key, out, key)
            self.assertEqual(set(out[key]["by"]), {"judge", "keyword"})
        self.assertGreater(out["label_diffs"]["primary"], 0)       # keyword labels differ from the judge's somewhere

    def test_judge2_appears_only_with_a_store(self):
        self.assertNotIn("judge2", lb.labeller_numbers(GRID))
        # a second judge that agrees with the first everywhere: its numbers equal the judge's
        maps = []
        for p in GRID.analysed:
            maps.append(dataclasses.replace(
                p, modality_judge2={s: dict(m) for s, m in p.modality.items()},
                not_judged_judge2={s: set(v) for s, v in (p.not_judged or {}).items()} or None))
        ctx2 = dataclasses.replace(GRID, analysed=maps)
        out = lb.labeller_numbers(ctx2)
        self.assertEqual(set(out), {"judge", "judge2", "keyword"})
        self.assertEqual(out["judge2"], out["judge"])
        cell = by_name(pf.row5_cells(ctx2), "matcher_free_rate", "judge2")
        self.assertNotIn("state", cell)
        self.assertEqual(cell["source"], "kit:a10_band")                  # computed form matches its not_run form
        self.assertEqual(by_name(pf.row5_cells(GRID), "matcher_free_rate", "judge2")["source"], "kit:a10_band")
        self.assertEqual([m["x"] for m in by_name(pf.row5_cells(ctx2), "matcher_free_rate", "judge")["band"]["members"]],
                         ["judge", "judge2", "keyword"])


class TestSmallPopulation(unittest.TestCase):
    def test_ten_maps_run_without_error(self):
        ctx = dataclasses.replace(GRID, analysed=GRID.analysed[:10])
        out = lb.labeller_numbers(ctx)
        self.assertIn("judge", out)
        self.assertEqual(len(out["judge"]), 7)


if __name__ == "__main__":
    unittest.main()
