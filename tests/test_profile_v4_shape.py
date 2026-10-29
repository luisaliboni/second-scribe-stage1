"""RERUN-PLAN-PROFILE-ZIP Task 4 (code only): the v4-shaped study file is readable by the app's loader.

The v4 document is built here, at test time, from the v3 fixture (reading 3 at temperature 0, changed reading-3
values, the two rarefied cells appended to the lead row) and written to a temporary folder. Nothing is added to
tests/fixtures/ (package.py ships that folder). The numbers are placeholders for shape only, not results.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.app import profile_view as pv

FIX = Path(__file__).resolve().parent / "fixtures" / "profiles"
V3_PATH = FIX / "profile-v3.json"
RARE = ("rarefied_ai_recall", "rarefied_ai_minus_prior_only_recall")


def make_v4(one_to_one=False):
    doc = json.loads(V3_PATH.read_text(encoding="utf-8"))
    doc["run"]["settings"]["reading3_temperature"] = 0
    lead = next(r for r in doc["rows"] if r["row"] == "lead")
    for c in lead["cells"]:
        if c["name"] == "no_interview_recall" and c.get("x") == "primary":
            c["value"] = 0.123
        if c["name"] == "listening_added" and c.get("x") == "primary":
            c["value"] = 0.271
    q = ["size_beside_lead"] + (["matching_one_to_one"] if one_to_one else [])
    for name, value, lo, hi, kind in ((RARE[0], 0.31, 0.27, 0.35, "share"), (RARE[1], 0.19, 0.15, 0.23, "difference")):
        lead["cells"].append({"name": name, "x": "primary", "n": 43, "unit": "node", "kind": kind, "value": value,
                              "ci_lo": lo, "ci_hi": hi, "ci_level": 0.95, "qualifiers": list(q),
                              "source": "gate chain entry 022"})
    return doc


class ProfileV4Shape(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="ss-tests-v4shape-")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def write(self, doc, name="profile-v4-shape.json"):
        p = self.tmp / name
        p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return p

    def test_v4_shape_loads(self):
        for one_to_one in (False, True):
            with self.subTest(one_to_one=one_to_one):
                doc = pv.load_profile(self.write(make_v4(one_to_one)))
                self.assertEqual(len(doc["rows"]), 11)
                self.assertEqual([r["row"] for r in doc["rows"]], list(pv.ROW_ORDER))
                self.assertEqual(doc["schema"], "orgsci-profile/v2")
                self.assertEqual(doc["run"]["settings"]["reading3_temperature"], 0)

    def test_rarefied_cells_are_in_the_lead_row_once_and_the_triples_are_unique(self):
        doc = pv.load_profile(self.write(make_v4()))
        triples = pv.cells(doc)                      # raises ProfileInvalid on a repeated (row, name, x)
        for name in RARE:
            self.assertEqual([t for t in triples if t[1] == name], [("lead", name, "primary")])
        self.assertEqual(len(triples), 147 + 2)      # the v3 study file has 147 cells; v4 adds exactly two

    def test_a_twelfth_row_is_still_refused(self):
        # the plan keeps Arm A inside existing rows: a new row would break the loader and the study page
        doc = make_v4()
        extra = copy.deepcopy(doc["rows"][-1])
        extra["row"] = 10
        doc["rows"].append(extra)
        with self.assertRaises(pv.ProfileInvalid):
            pv.load_profile(self.write(doc, "twelve.json"))

    def test_the_v3_fixture_is_not_touched_by_building_v4(self):
        before = V3_PATH.read_bytes()
        make_v4()
        self.assertEqual(V3_PATH.read_bytes(), before)
        # the fixture itself already holds the temperature-0 reading-3 values (council 23 fix 4)
        self.assertEqual(json.loads(before)["run"]["settings"]["reading3_temperature"], 0.0)
        self.assertFalse([p.name for p in FIX.iterdir() if "v4" in p.name.lower()],
                         "no v4 file belongs in tests/fixtures/profiles (package.py ships that folder)")


if __name__ == "__main__":
    unittest.main()
