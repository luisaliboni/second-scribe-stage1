"""profile_coverage: the ledger, rows 1-3 and row 9's two floors, on the oracle demo (zero model calls).

The labeller numbers (Task 4) and the rung-1 size (Task 5) are taken as parameters; these tests pass stub dicts in the
shapes stated in the Task 2 brief. Parity against the analysis pack's synthetic profile runs when SS_PARITY_DIR is set.
"""
import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe import metrics
from second_scribe import profile_base as pb
from second_scribe import profile_coverage as pc
from second_scribe.cli import DEMO_CONFIG

REPO = Path(__file__).resolve().parent.parent
SKELETON = json.loads((REPO / "tests" / "data" / "profile_v2_skeleton.json").read_text(encoding="utf-8"))
SK = {r["row"]: [(c["name"], c.get("x")) for c in r["cells"]] for r in SKELETON["rows"]}

LAB_METRICS = ("node_recall", "attested_absent_from_map", "unanchored", "matcher_free_rate",
               "contradiction_actual_as_wished", "contaminated_share", "wrong_transcript_difference")


def stub_labellers(*names):
    """Task 4's labeller_numbers shape: {labeller: {metric: number}}, arbitrary numbers."""
    base = {"judge": (0.30, 12, 5), "keyword": (0.28, 14, 7), "judge2": (0.26, 9, 8)}
    out = {}
    for lab in names:
        r, a, u = base[lab]
        out[lab] = {m: 0.1 for m in LAB_METRICS}
        out[lab].update(node_recall=r, attested_absent_from_map=a, unanchored=u)
    return out


def names(cells):
    return [(c["name"], c.get("x")) for c in cells]


class TestToyFunctions(unittest.TestCase):
    def test_recall_cell(self):
        pairs = {("a1", "e1"), ("a2", "e1"), ("a3", "e2"), ("a9", "e3")}
        self.assertEqual(pc.recall_cell({"e1", "e2", "e3", "e4"}, pairs, {"a1", "a3"}), (2, 4))
        self.assertEqual(pc.recall_cell({"e1", "e2", "e3", "e4"}, pairs, {"a1", "a3"}, drop={"e1"}), (1, 3))
        self.assertEqual(pc.recall_cell(set(), pairs, {"a1"}), (0, 0))

    def test_precision_cell(self):
        pairs = {("a1", "e1"), ("a1", "e2"), ("a2", "e1"), ("a5", "e9")}
        # a5 is not judged, a3 has no partner, a1 counts once
        self.assertEqual(pc.precision_cell(pairs, {"a1", "a2", "a3", "a5"}, {"a1", "a2", "a3"}), (2, 3))

    def test_three_way(self):
        e_in = {"e1", "e2", "e3", "e4"}
        primary = {("a1", "e1"), ("a2", "e9")}
        lenient = {("a1", "e1"), ("a3", "e2"), ("a4", "e3")}
        self.assertEqual(pc.three_way(e_in, primary, lenient), (1, 2, 1))


class TestNarrowByHand(unittest.TestCase):
    """The ``narrow`` flag, with small fabricated numbers whose answer is worked out by hand."""

    def test_half_width_scales(self):
        # ratio: CI 0.40..0.60 -> half-width (0.60 - 0.40) / 2 = 0.10 (scale 1)
        self.assertAlmostEqual(pc._half_width((0.5, 0.40, 0.60, True, 1)), 0.10)
        # count: CI on the ratio 0.20..0.40 over 50 judged boxes -> (0.40 - 0.20) * 50 / 2 = 5 boxes
        self.assertAlmostEqual(pc._half_width((10, 0.20, 0.40, True, 50)), 5.0)
        self.assertIsNone(pc._half_width((10, 0.20, 0.40, False, 50)))          # CI undefined

    def test_narrow_flag(self):
        # Counts for judge / judge2 / keyword: 12, 15, 14 -> band 12..15, width 3.
        # The judge's CI half-width is 5 (see above): 3 <= 5, so narrow is True.
        hw = pc._half_width((12, 0.20, 0.40, True, 50))
        mem = [{"x": "judge", "value": 12}, {"x": "judge2", "value": 15}, {"x": "keyword", "value": 14}]
        self.assertTrue(pc._labeller_band(mem, hw)["narrow"])
        # Counts 12, 18, 14 -> width 6 > 5: narrow is False.
        mem = [{"x": "judge", "value": 12}, {"x": "judge2", "value": 18}, {"x": "keyword", "value": 14}]
        self.assertFalse(pc._labeller_band(mem, hw)["narrow"])
        # A width exactly equal to the half-width counts as narrow (<=): 0.75 - 0.5 = 0.25 (binary-exact values).
        mem = [{"x": "judge", "value": 0.5}, {"x": "keyword", "value": 0.75}]
        self.assertTrue(pc._labeller_band(mem, 0.25)["narrow"])
        # An undefined CI is never narrow.
        self.assertFalse(pc._labeller_band(mem, None)["narrow"])

    def test_judge_recall_uses_a1_denominator(self):
        """M-2: the judge's recall for the band is a1's: matched / ALL in-transcript endorsed nodes, ``nj_end`` kept in.
        Map A: 4 in-transcript endorsed nodes, 2 matched, 1 only reachable by an unjudged AI node -> 2 / 4.
        Map B: 2 in-transcript, 1 matched -> 1 / 2. Pooled: 3 / 6 = 0.5 (the kit's buckets would give 3 / 5 = 0.6)."""
        a = {"e_in": {"e1", "e2", "e3", "e4"}, "nj_end": {"e4"}, "pairs": {"primary": {("x1", "e1"), ("x2", "e2")}},
             "att": {"x1", "x2"}, "judged": {"x1", "x2", "x3"}}
        b = {"e_in": {"e5", "e6"}, "nj_end": set(), "pairs": {"primary": {("y1", "e5")}},
             "att": {"y1"}, "judged": {"y1", "y2"}}
        ctx = types.SimpleNamespace(B=50, seed=7)
        point = pc._judge_numbers(ctx, [a, b])["node_recall"][0]
        self.assertAlmostEqual(point, 0.5)
        self.assertNotAlmostEqual(point, 0.6)

    def test_setting_band_with_a_nan_value_is_none(self):
        per = {s: 0.5 for s in pb.SETTINGS}
        self.assertIsNotNone(pc._setting_band(per))
        self.assertIsNone(pc._setting_band({**per, "lenient": math.nan}))

    def test_missing_band_becomes_partial_not_run(self):
        res = (0.5, 0.4, 0.6, True, 10)
        c = pc._share_cell("kept", res, band_=None, want_band=True, rests_on=["R"], source="kit:a1_recall_matchers")
        self.assertEqual((c["state"], c["missing"]), ("partial_not_run", ["band"]))
        self.assertEqual(c["value"], 0.5)
        self.assertNotIn("band", c)
        band = pb.band(0.4, 0.6, "8_settings")
        self.assertNotIn("state", pc._share_cell("kept", res, band_=band, want_band=True))
        # a cell that is not meant to carry a band is unchanged
        self.assertNotIn("state", pc._share_cell("coarsened", res))

    def test_empty_share_cell_keeps_rests_on_and_source(self):
        c = pc._share_cell("kept", (math.nan, math.nan, math.nan, False, 0), x="primary", rests_on=["R", "IT"],
                           source="kit:a1_recall_matchers")
        self.assertEqual(c["state"], "not_run")
        self.assertEqual(c["reason_code"], "empty_denominator")
        self.assertEqual(c["rests_on"], ["R", "IT"])
        self.assertEqual(c["source"], "kit:a1_recall_matchers")
        self.assertEqual(c["x"], "primary")


class TestCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t2-")
        cls.demo = Path(cls._tmp.name) / "demo"
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(cls.demo),
                            "--client", "oracle"], capture_output=True, text=True, cwd=REPO)
        assert r.returncode == 0, r.stderr[-2000:]
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.demo)
        cls.agg = json.loads((cls.demo / "aggregates" / "aggregates.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ---- the ledger
    def test_extract_nj_end_matches_kit_denominator(self):
        """len(e_in) - len(nj_end) is the kit's matched + missed, per map (also with unjudged nodes)."""
        spec = self.ctx.spec
        p0 = self.ctx.analysed[0]
        ids = [n.node_id for n in p0.ai[spec].nodes][:5]
        p1 = dataclasses.replace(p0, not_judged={**(p0.not_judged or {}), spec: set(ids)})
        ctx = dataclasses.replace(self.ctx, analysed=[p1, *self.ctx.analysed[1:]])
        arm = metrics.spec_arm(spec)
        seen = []
        for p in ctx.analysed:
            d = pc.extract(ctx, p)
            b = metrics.buckets(p, spec, arm, pb.SETTINGS["primary"])
            with self.subTest(pid=p.pid):
                self.assertEqual(len(d["e_in"]) - len(d["nj_end"] & d["e_in"]), b["matched"] + b["missed"])
                self.assertEqual(len(d["nj_end"] & d["e_in"]), b["n_endorsed_not_judged"])
                seen.append(b["n_endorsed_not_judged"])
        self.assertGreater(sum(seen), 0)                  # not vacuous: unjudging 5 nodes of map 0 does hide endorsed nodes

    def test_ledger_closes(self):
        counts = pc.ledger_counts(self.ctx)
        self.assertEqual(list(counts), list(pb.SETTINGS))
        self.assertEqual(len(counts), 8)
        for s, c in counts.items():
            with self.subTest(setting=s):
                self.assertEqual(c["on_map"] + c["attested_absent_from_map"] + c["unanchored"], c["ai_boxes_total"])
                self.assertGreaterEqual(c["attested_absent_from_map"], 0)

    def test_ledger_closes_with_unjudged_nodes(self):
        """AI nodes the judge never answered for are left out of every count, so the ledger still closes."""
        spec = self.ctx.spec
        p0 = self.ctx.analysed[0]
        ids = [n.node_id for n in p0.ai[spec].nodes][:3]
        p1 = dataclasses.replace(p0, not_judged={**(p0.not_judged or {}), spec: set(ids)})
        ctx = dataclasses.replace(self.ctx, analysed=[p1, *self.ctx.analysed[1:]])
        base = pc.ledger_counts(self.ctx)["primary"]["ai_boxes_total"]
        counts = pc.ledger_counts(ctx)
        self.assertEqual(counts["primary"]["ai_boxes_total"], base - 3)
        for s, c in counts.items():
            with self.subTest(setting=s):
                self.assertEqual(c["on_map"] + c["attested_absent_from_map"] + c["unanchored"], c["ai_boxes_total"])

    def test_ledger_primary_matches_kit_buckets(self):
        """The kit's ``metrics.buckets`` takes any match setting, so the ledger is checked at primary AND at every
        other setting (lenient, the two judge-only ones and the four lexical-only thresholds)."""
        arm = metrics.spec_arm(self.ctx.spec)
        counts = pc.ledger_counts(self.ctx)
        for setting in pb.SETTINGS:
            with self.subTest(setting=setting):
                bs = [metrics.buckets(p, self.ctx.spec, arm, pb.SETTINGS[setting]) for p in self.ctx.analysed]
                c = counts[setting]
                self.assertEqual(c["on_map"], sum(b["ai_attested_matched"] for b in bs))
                self.assertEqual(c["attested_absent_from_map"], sum(b["attested_absent_from_map"] for b in bs))
                self.assertEqual(c["unanchored"], sum(b["unanchored"] for b in bs))
                self.assertEqual(c["ai_boxes_total"], sum(b["n_ai_nodes"] - b["n_not_judged"] for b in bs))
        self.assertIn("lenient", pb.SETTINGS)
        # the settings really differ somewhere, so the loop above is not checking one number eight times
        self.assertGreater(len({c["on_map"] for c in counts.values()}), 1)

    def test_ledger_cells_values(self):
        cells = pc.ledger_cells(self.ctx)
        by = {(c["name"], c.get("x")): c for c in cells}
        counts = pc.ledger_counts(self.ctx)
        self.assertEqual(by[("ai_boxes_total", None)]["value"], counts["primary"]["ai_boxes_total"])
        self.assertEqual(by[("ai_boxes_attested_on_map", "primary")]["value"], counts["primary"]["on_map"])
        absent = by[("attested_absent_from_map", "primary")]
        self.assertEqual(absent["value"], counts["primary"]["attested_absent_from_map"])
        self.assertEqual(absent["derived"], "ledger_band")
        band = absent["band"]
        self.assertEqual(band["over"], "8_settings")
        self.assertEqual([m["x"] for m in band["members"]], list(pb.SETTINGS))
        for m in band["members"]:
            self.assertNotIn("value_hi", m)               # exact counts: members carry value only
            self.assertEqual(m["value"], counts[m["x"]]["attested_absent_from_map"])
        self.assertEqual((band["lo"], band["hi"]),
                         (min(m["value"] for m in band["members"]), max(m["value"] for m in band["members"])))
        self.assertEqual([s["name"] for s in band["subbands"]], ["judge_using", "lexical_only"])
        endorsed = sum(len(p.endorsed.nodes) for p in self.ctx.analysed)
        off = by[("off_record_endorsed", "primary")]
        self.assertEqual(off["n"], endorsed)

    def test_ledger_labeller_cells(self):
        cells = pc.ledger_cells(self.ctx, stub_labellers("judge", "keyword", "judge2"))
        by = {(c["name"], c.get("x")): c for c in cells}
        un = by[("unanchored", "primary")]
        self.assertNotIn("state", un)
        self.assertEqual(un["band"]["over"], "labellers")
        self.assertEqual([m["x"] for m in un["band"]["members"]], ["judge", "judge2", "keyword"])
        self.assertEqual((un["band"]["lo"], un["band"]["hi"]), (5, 8))
        self.assertIsInstance(un["band"]["narrow"], bool)
        ab = by[("attested_absent_from_map", "labellers")]
        self.assertEqual((ab["band"]["lo"], ab["band"]["hi"]), (9, 14))
        self.assertEqual(ab["value"], pc.ledger_counts(self.ctx)["primary"]["attested_absent_from_map"])

    def test_two_labellers_still_a_band(self):
        cells = pc.row3_cells(self.ctx, stub_labellers("judge", "keyword"))
        by = {(c["name"], c.get("x")): c for c in cells}
        b = by[("attested_recall", "primary")]["band"]
        self.assertEqual(b["over"], "labellers")
        self.assertEqual([m["x"] for m in b["members"]], ["judge", "keyword"])
        self.assertEqual((b["lo"], b["hi"]), (0.28, 0.3))

    def test_no_labellers_gives_partial_not_run(self):
        for fn, key in ((pc.ledger_cells, ("unanchored", "primary")), (pc.ledger_cells, ("attested_absent_from_map", "labellers")),
                        (pc.row3_cells, ("attested_recall", "primary")), (pc.row3_cells, ("unanchored", "primary")),
                        (pc.row3_cells, ("attested_absent_from_map", "primary"))):
            with self.subTest(fn=fn.__name__, cell=key):
                c = {(c["name"], c.get("x")): c for c in fn(self.ctx, None)}[key]
                self.assertEqual(c["state"], "partial_not_run")
                self.assertEqual(c["missing"], ["band"])
                self.assertIsNotNone(c["value"])
                self.assertNotIn("band", c)

    # ---- names and order
    def test_coverage_names_match_skeleton(self):
        labs = stub_labellers("judge", "keyword", "judge2")
        for label, cells in (("ledger", pc.ledger_cells(self.ctx)), ("ledger", pc.ledger_cells(self.ctx, labs)),
                             (1, pc.row1_cells(self.ctx)),
                             (2, pc.row2_cells(self.ctx, None)), (2, pc.row2_cells(self.ctx, {"n_ai_nodes": 40})),
                             (3, pc.row3_cells(self.ctx)), (3, pc.row3_cells(self.ctx, labs))):
            with self.subTest(row=label):
                self.assertEqual(names(cells), SK[label])

    def test_row9_floor_names_match_skeleton(self):
        self.assertEqual(names(pc.row9_floor_cells(self.ctx)), SK[9][:2])

    def test_units_and_kinds_match_skeleton(self):
        sk = {r["row"]: r["cells"] for r in SKELETON["rows"]}
        for label, cells in (("ledger", pc.ledger_cells(self.ctx)), (1, pc.row1_cells(self.ctx)),
                             (2, pc.row2_cells(self.ctx, {"n_ai_nodes": 40})), (3, pc.row3_cells(self.ctx)),
                             (9, pc.row9_floor_cells(self.ctx))):
            for got, want in zip(cells, sk[label]):
                with self.subTest(row=label, cell=want["name"], x=want.get("x")):
                    self.assertEqual(got.get("unit"), want.get("unit"))
                    self.assertEqual(got.get("kind"), want.get("kind"))

    # ---- row 1
    def test_primary_recall_point_equals_kit(self):
        prim = self.agg["buckets"]["primary"]
        self.assertEqual(prim["n_endorsed_not_judged"] if "n_endorsed_not_judged" in prim else 0, 0)
        c = {(c["name"], c.get("x")): c for c in pc.row1_cells(self.ctx)}[("attested_recall", "primary")]
        self.assertEqual(c["value"], round(prim["node_recall"], 3))
        self.assertEqual(c["ci_level"], 0.95)
        self.assertLessEqual(c["ci_lo"], c["value"])
        self.assertGreaterEqual(c["ci_hi"], c["value"])

    def test_row3_attested_recall_sits_inside_its_band_with_unjudged_nodes(self):
        """M-2 (ruling: a1's denominator). With AI nodes the judge never answered for, the labeller members and the
        ``narrow`` flag use the same in-transcript endorsed denominator as the cell's value and CI, so the cell is
        inside its own band. The labeller numbers are the real ones (profile_labellers), not a stub."""
        from second_scribe import profile_labellers as lb
        spec = self.ctx.spec
        an = list(self.ctx.analysed)
        for i in range(3):                       # 10 AI nodes unjudged in each of 3 maps (value 0.694, old band 0.742+)
            ids = [n.node_id for n in an[i].ai[spec].nodes][:10]
            an[i] = dataclasses.replace(an[i], not_judged={**(an[i].not_judged or {}), spec: set(ids)})
        ctx = dataclasses.replace(self.ctx, analysed=an)
        d = [pc.extract(ctx, p) for p in ctx.analysed]
        self.assertGreater(sum(len(x["nj_end"] & x["e_in"]) for x in d), 0)          # not vacuous
        labs = lb.labeller_numbers(ctx)
        cell = {(c["name"], c.get("x")): c for c in pc.row3_cells(ctx, labs)}[("attested_recall", "primary")]
        self.assertNotIn("state", cell)
        band = cell["band"]
        self.assertLessEqual(band["lo"], cell["value"] + 0.0005)
        self.assertGreaterEqual(band["hi"], cell["value"] - 0.0005)
        # the judge member is the cell's own number (same numerator and denominator, unrounded)
        judge = [m["value"] for m in band["members"] if m["x"] == "judge"][0]
        self.assertAlmostEqual(judge, cell["value"], delta=0.0005)
        # the half-width behind ``narrow`` is the cell's own CI half-width
        jn = pc._judge_numbers(ctx, pc._data(ctx))["node_recall"]
        self.assertAlmostEqual(pc._half_width(jn), (cell["ci_hi"] - cell["ci_lo"]) / 2, delta=0.0006)

    def test_row2_with_no_analysed_maps_is_not_run_not_a_crash(self):
        """L-9: zero analysed maps -> the per-map cells are not_run (empty_denominator), the counts are 0."""
        ctx = dataclasses.replace(self.ctx, analysed=[])
        cells = pc.row2_cells(ctx, {"n_ai_nodes": 3})
        self.assertEqual(names(cells), [(c["name"], c.get("x")) for c in pc.row2_cells(self.ctx, {"n_ai_nodes": 3})])
        got = {(c["name"], c.get("x")): c for c in cells}
        for key in (("ai_per_map", None), ("endorsed_per_map", None), ("ai_nodes_per_map", None),
                    ("ai_edges_per_map", None), ("endorsed_nodes_per_map", None)):
            with self.subTest(cell=key):
                self.assertEqual((got[key]["state"], got[key]["reason_code"]), ("not_run", "empty_denominator"))
        self.assertEqual((got[("ai_boxes", None)]["value"], got[("endorsed_boxes", None)]["value"]), (0, 0))

    def test_three_way_shares_sum_to_one(self):
        r1 = {c["name"]: c for c in pc.row1_cells(self.ctx) if c["name"] in ("kept", "coarsened", "omitted")}
        self.assertAlmostEqual(sum(c["value"] for c in r1.values()), 1.0, delta=0.0015)
        self.assertEqual(len({c["n"] for c in r1.values()}), 1)

    def test_row1_bands_cover_the_8_settings(self):
        by = {(c["name"], c.get("x")): c for c in pc.row1_cells(self.ctx)}
        for key in (("kept", None), ("attested_recall", "primary")):
            b = by[key]["band"]
            self.assertEqual(b["over"], "8_settings")
            self.assertEqual([m["x"] for m in b["members"]], list(pb.SETTINGS))
            # the cell's own value is the primary member of its band, so it lies inside [lo, hi]
            self.assertLessEqual(b["lo"], by[key]["value"])
            self.assertLessEqual(by[key]["value"], b["hi"])
        # the primary member of the attested band is the cell's own value
        prim = [m for m in by[("attested_recall", "primary")]["band"]["members"] if m["x"] == "primary"][0]
        self.assertEqual(prim["value"], by[("attested_recall", "primary")]["value"])
        self.assertNotIn("band", by[("attested_recall", "lenient")])

    def test_r_star_not_run_without_sidejudge(self):
        c = {(c["name"], c.get("x")): c for c in pc.row1_cells(self.ctx)}[("r_star", None)]
        self.assertEqual(c["state"], "not_run")
        self.assertEqual(c["needs"], ["sidejudge"])
        self.assertIsNone(c["value"])

    # ---- row 2
    def test_row2_endorsed_boxes_and_derived_cells(self):
        by = {(c["name"], c.get("x")): c for c in pc.row2_cells(self.ctx, None)}
        counts = pc.ledger_counts(self.ctx)["primary"]
        end = sum(len(p.endorsed.nodes) for p in self.ctx.analysed)
        arm = metrics.spec_arm(self.ctx.spec)
        bs = [metrics.buckets(p, self.ctx.spec, arm, pb.SETTINGS["primary"]) for p in self.ctx.analysed]
        in_t = sum(b["matched"] + b["missed"] + b["n_endorsed_not_judged"] for b in bs)
        off = sum(b["off_record"] for b in bs)
        self.assertEqual(end, in_t + off)
        self.assertEqual(by[("endorsed_boxes", None)]["value"], end)
        self.assertEqual(by[("ai_boxes", None)]["value"], counts["ai_boxes_total"])
        self.assertEqual(by[("ai_per_endorsed", None)]["value"], round(counts["ai_boxes_total"] / end, 3))
        self.assertEqual(by[("ai_per_endorsed", None)]["derived"], "ratio_ai_per_endorsed")
        n = len(self.ctx.analysed)
        self.assertEqual(by[("ai_per_map", None)]["value"], round(counts["ai_boxes_total"] / n, 3))
        self.assertEqual(by[("endorsed_per_map", None)]["value"], round(end / n, 3))
        self.assertEqual(by[("endorsed_per_map", None)]["derived"], "per_map_mean")
        self.assertEqual(by[("endorsed_nodes_per_map", None)]["value"], round(end / n, 3))
        self.assertEqual(by[("endorsed_nodes_per_map", None)]["n"], n)

    def test_ai_per_endorsed_rung1(self):
        end = sum(len(p.endorsed.nodes) for p in self.ctx.analysed)
        none = {(c["name"], c.get("x")): c for c in pc.row2_cells(self.ctx, None)}[("ai_per_endorsed", "rung1")]
        self.assertEqual((none["state"], none["needs"], none["value"]), ("not_run", ["ladder"], None))
        got = {(c["name"], c.get("x")): c for c in pc.row2_cells(self.ctx, {"n_ai_nodes": 40})}[("ai_per_endorsed", "rung1")]
        self.assertNotIn("state", got)
        self.assertEqual(got["value"], round(40 / end, 3))
        self.assertEqual(got["qualifiers"], ["one_draw", "no_interval"])
        self.assertEqual(got["derived"], "ratio_ai_per_endorsed")

    # ---- row 9 floors
    def test_row9_floors(self):
        floors = {(c["name"], c.get("x")): c for c in pc.row9_floor_cells(self.ctx)}
        r1 = {(c["name"], c.get("x")): c for c in pc.row1_cells(self.ctx)}
        any_primary = floors[("any_node_recall", "primary")]
        self.assertEqual(any_primary["value"], r1[("kept", None)]["value"])   # kept = partner under primary, any node
        lex = floors[("lexical_floor", "lexical_only_04")]
        kept_band = {m["x"]: m["value"] for m in r1[("kept", None)]["band"]["members"]}
        self.assertEqual(lex["value"], kept_band["lexical_only_04"])
        self.assertEqual(lex["rests_on"], ["R", "IT", "LX"])

    # ---- hygiene
    def test_deterministic(self):
        a = json.dumps([pc.ledger_cells(self.ctx), pc.row1_cells(self.ctx), pc.row2_cells(self.ctx, None),
                        pc.row3_cells(self.ctx), pc.row9_floor_cells(self.ctx)], sort_keys=True)
        b = json.dumps([pc.ledger_cells(self.ctx), pc.row1_cells(self.ctx), pc.row2_cells(self.ctx, None),
                        pc.row3_cells(self.ctx), pc.row9_floor_cells(self.ctx)], sort_keys=True)
        self.assertEqual(a, b)

    def test_no_forbidden_wording_or_ids(self):
        text = json.dumps([pc.ledger_cells(self.ctx), pc.row1_cells(self.ctx), pc.row2_cells(self.ctx, None),
                           pc.row3_cells(self.ctx), pc.row9_floor_cells(self.ctx)]).lower()
        for w in ("validated", "ground truth", "replicable", "first"):
            self.assertNotIn(w, text)
        for p in self.ctx.p_list:
            self.assertNotIn(p.pid.lower(), text)

    def test_sources_are_kit_script_names(self):
        """Ruling R11: a lab has no gate; every cell's source is kit:<script>, derived ones joined with '+'."""
        labs = stub_labellers("judge", "keyword")
        for c in [*pc.ledger_cells(self.ctx, labs), *pc.row1_cells(self.ctx), *pc.row2_cells(self.ctx, None),
                  *pc.row2_cells(self.ctx, {"n_ai_nodes": 3}), *pc.row3_cells(self.ctx, labs),
                  *pc.row9_floor_cells(self.ctx)]:
            with self.subTest(cell=(c["name"], c.get("x"))):
                self.assertTrue(c["source"])
                for part in c["source"].split("+"):
                    self.assertRegex(part, r"^kit:[a-z0-9_]+$")
        self.assertNotIn("cleared", json.dumps(pc.row1_cells(self.ctx)))

    def test_does_not_write_into_the_run(self):
        before = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*") if p.is_file())
        pc.ledger_cells(self.ctx)
        pc.row1_cells(self.ctx)
        after = sorted(str(p.relative_to(self.demo)) for p in self.demo.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    # ---- parity with the analysis pack's synthetic profile (optional)
    @unittest.skipUnless(os.environ.get("SS_PARITY_DIR"), "SS_PARITY_DIR not set")
    def test_parity_with_analysis_pack(self):
        ref = json.loads((Path(os.environ["SS_PARITY_DIR"]) / "profile-synth.json").read_text(encoding="utf-8"))
        want = {(r["row"], c["name"], c.get("x")): c for r in ref["rows"] for c in r["cells"]}
        mine = {}
        for row, cells in (("ledger", pc.ledger_cells(self.ctx)), (1, pc.row1_cells(self.ctx)),
                           (2, pc.row2_cells(self.ctx, None)), (3, pc.row3_cells(self.ctx)),
                           (9, pc.row9_floor_cells(self.ctx))):
            for c in cells:
                mine[(row, c["name"], c.get("x"))] = c
        mismatches, compared, extra = [], 0, []
        for key, c in mine.items():
            w = want.get(key)
            if w is None or w.get("state") == "not_run" or c.get("state") == "not_run" or w.get("value") is None:
                extra.append((key, "not in parity / not_run there"))
                continue
            compared += 1
            for f in ("value", "ci_lo", "ci_hi", "n"):
                a, b = c.get(f), w.get(f)
                if f == "n" and (a is None or b is None):
                    continue                      # parity leaves n out where its 009 denominator was not_run
                if (a is None) != (b is None) or (a is not None and abs(a - b) > 0.0005 + 1e-9):
                    mismatches.append((key, f, a, b))
            wb, mb = w.get("band"), c.get("band")
            if wb and mb and wb["over"] == mb["over"] == "8_settings":
                if (wb["lo"], wb["hi"]) != (mb["lo"], mb["hi"]):
                    mismatches.append((key, "band", (mb["lo"], mb["hi"]), (wb["lo"], wb["hi"])))
        print("\nparity: compared %d cells; %d mismatches; not compared: %s" % (compared, len(mismatches), extra),
              file=sys.stderr)
        self.assertGreater(compared, 20)
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
