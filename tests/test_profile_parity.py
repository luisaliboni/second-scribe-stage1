"""Whole-profile parity (Task 9): ``build_profile`` on the synthetic demo against the analysis pack's
``parity-synth/profile-synth.json``, matched on (row, name, x), value / ci_lo / ci_hi to 3 decimals.

The parity test runs only when SS_PARITY_DIR points at ``analysis/public/parity-synth/`` (read only here: nothing in it is
ever written). The pack's reference run is the replay run (``run/``); the lead and row 7 f1 cells come from the oracle
grid run (``run-grid/``). Replay and oracle runs have the same aggregates (the pack's README), so this test exports the
shipped demo and the grid demo with the oracle client (zero model calls, zero network) and checks both: the grid run
stands for ``run-grid/``, the shipped demo for ``run/``. A cell that is not_run in either file is compared on state only.

Known differences, pinned in ``KNOWN_PACK_GAPS``: the pack could not run a7 (no AI box attested and absent from the map in
the demo), a10_band, a10_flips and a2 (they need a second judge family, which needs a model), so its builder stopped or
marked those cells not_run. The kit exporter computes them from what the run has (the judge and the keyword labeller, the
kit's own counts). They are listed here, exactly, so any other difference fails the test.
"""
import json
import os
import sys
import unittest
from pathlib import Path

from second_scribe import profile_export as pe
from tests.profile_fixture import contexts

FIELDS = ("value", "ci_lo", "ci_hi")
TOL = 0.0005

# (row, name, x): cells the exporter computes (or adds) where the pack's cell is not_run or absent, for the reasons above.
KNOWN_PACK_GAPS = {
    ("ledger", "attested_absent_from_map", "labellers"): "pack: not_run (cleared/018, second judge family)",
    ("2", "endorsed_boxes", None): "pack: not_run (cleared/009, a7 stops: empty T group)",
    ("2", "ai_per_endorsed", None): "pack: cell absent (build stopped on cleared/009)",
    ("2", "ai_per_endorsed", "rung1"): "pack: cell absent (build stopped on cleared/009)",
    ("2", "endorsed_per_map", None): "pack: cell absent (build stopped on cleared/009)",
    ("9", "attested_recall", "judge"): "pack: not_run (cleared/018)",
    ("9", "attested_absent_from_map", "judge"): "pack: not_run (cleared/018)",
    ("9", "unanchored", "judge"): "pack: not_run (cleared/018)",
    ("9", "matcher_free_rate", "judge"): "pack: not_run (cleared/018)",
    ("9", "quote_actual_drawn_as_wished", "judge"): "pack: not_run (cleared/018)",
    ("9", "contaminated_share", "judge"): "pack: not_run (cleared/018)",
    ("9", "identical_requests", "run2_determinism_audit"): "pack: not_run (cleared/019)",
}


def index(profile: dict) -> dict:
    """{(row, name, x): cell} over every row of a profile; a repeated key is an error."""
    out = {}
    for r in profile["rows"]:
        for c in r["cells"]:
            key = (str(r["row"]), c["name"], c.get("x"))
            if key in out:
                raise ValueError(f"cell key repeated: {key}")
            out[key] = c
    return out


def compare(ours: dict, theirs: dict) -> dict:
    """Compare two indexed profiles. Returns {"compared", "missing", "extra", "state", "numbers"}: ``missing`` are keys only
    in ``theirs``, ``extra`` only in ``ours``; ``state`` lists keys where exactly one side is not_run; ``numbers`` lists
    (key, field, ours, theirs) beyond 3 decimals. A cell not_run on either side is compared on state only."""
    res = {"compared": 0, "missing": sorted(set(theirs) - set(ours), key=str), "extra": sorted(set(ours) - set(theirs), key=str),
           "state": [], "numbers": []}
    for key in sorted(set(ours) & set(theirs), key=str):
        a, b = ours[key], theirs[key]
        res["compared"] += 1
        na, nb = a.get("state") == "not_run", b.get("state") == "not_run"
        if na or nb:
            if na != nb:
                res["state"].append(key)
            continue
        for f in FIELDS:
            x, y = a.get(f), b.get(f)
            if x is None and y is None:
                continue
            if x is None or y is None or abs(x - y) > TOL:
                res["numbers"].append((key, f, x, y))
    return res


def unexplained(res: dict) -> dict:
    """What is left of a comparison once the pinned differences are taken out (an empty result passes)."""
    gaps = set(KNOWN_PACK_GAPS)
    return {"missing": res["missing"], "extra": [k for k in res["extra"] if k not in gaps],
            "state": [k for k in res["state"] if k not in gaps], "numbers": res["numbers"],
            "pinned_not_seen": sorted(gaps - set(res["extra"]) - set(res["state"]), key=str)}


class TestCompare(unittest.TestCase):
    """The comparison itself, with planted differences (always runs)."""

    @staticmethod
    def prof(cells, row=1):
        return {"rows": [{"row": row, "cells": cells}]}

    def test_equal_within_three_decimals(self):
        a = index(self.prof([{"name": "k", "x": "p", "value": 0.5, "ci_lo": 0.4, "ci_hi": 0.6}]))
        b = index(self.prof([{"name": "k", "x": "p", "value": 0.5004, "ci_lo": 0.4, "ci_hi": 0.6}]))
        res = compare(a, b)
        self.assertEqual((res["compared"], res["numbers"], res["state"], res["missing"], res["extra"]), (1, [], [], [], []))

    def test_a_planted_number_a_state_and_a_missing_cell_are_all_listed(self):
        a = index(self.prof([{"name": "k", "x": "p", "value": 0.5, "ci_lo": 0.4, "ci_hi": 0.6},
                             {"name": "s", "value": None, "state": "not_run"},
                             {"name": "only_ours", "value": 1}]))
        b = index(self.prof([{"name": "k", "x": "p", "value": 0.5, "ci_lo": 0.4, "ci_hi": 0.7},
                             {"name": "s", "value": 3},
                             {"name": "only_theirs", "value": 1}]))
        res = compare(a, b)
        self.assertEqual(res["numbers"], [(("1", "k", "p"), "ci_hi", 0.6, 0.7)])
        self.assertEqual(res["state"], [("1", "s", None)])
        self.assertEqual(res["missing"], [("1", "only_theirs", None)])
        self.assertEqual(res["extra"], [("1", "only_ours", None)])
        self.assertTrue(any(unexplained(res).values()))

    def test_both_not_run_compares_state_only(self):
        a = index(self.prof([{"name": "s", "state": "not_run", "value": None, "needs": ["J2"]}]))
        b = index(self.prof([{"name": "s", "state": "not_run", "reason": "x"}]))
        res = compare(a, b)
        self.assertEqual((res["compared"], res["state"], res["numbers"]), (1, [], []))

    def test_a_repeated_key_is_refused(self):
        with self.assertRaises(ValueError):
            index(self.prof([{"name": "k"}, {"name": "k"}]))


@unittest.skipUnless(os.environ.get("SS_PARITY_DIR"), "SS_PARITY_DIR not set")
class TestParity(unittest.TestCase):
    """build_profile (every row) against profile-synth.json, on the shipped demo and on the grid demo."""

    @classmethod
    def setUpClass(cls):
        pdir = Path(os.environ["SS_PARITY_DIR"])
        cls.ref = index(json.loads((pdir / "profile-synth.json").read_text(encoding="utf-8")))
        demo, grid = contexts()
        cls.results = {tag: compare(index(pe.build_profile(ctx)), cls.ref) for tag, ctx in (("demo", demo), ("grid", grid))}
        for tag, res in cls.results.items():
            print(f"\nparity[{tag}]: {res['compared']} cells compared, {len(res['numbers'])} number mismatches, "
                  f"{len(res['state'])} state mismatches, {len(res['missing'])} missing, {len(res['extra'])} extra "
                  f"(pinned pack gaps: {len(KNOWN_PACK_GAPS)})", file=sys.stderr)

    def _check(self, tag):
        res = self.results[tag]
        self.assertGreater(res["compared"], 100)
        left = unexplained(res)
        self.assertEqual({k: v for k, v in left.items() if v}, {}, f"mismatches against the parity pack ({tag})")

    def test_shipped_demo_matches_the_replay_reference(self):
        self._check("demo")

    def test_grid_demo_matches_the_reference_including_lead_and_row7(self):
        self._check("grid")

    def test_every_row_is_compared(self):
        rows = {k[0] for k in self.ref}
        self.assertEqual(rows, {"lead", "ledger", "1", "2", "3", "4", "5", "6", "7", "8", "9"})


if __name__ == "__main__":
    unittest.main()
