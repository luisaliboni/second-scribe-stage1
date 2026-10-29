"""Task 19b: profile loader, exporter wrapper and profile_to_js (plan amendment V2).

Fixtures are byte copies of the two public reference profiles and of the kit exporter's sample export on the demo
(tests/fixtures/profiles/SOURCES.md). run_exporter is tested with a fake interpreter script (every exit path) and
once against the real kit exporter (E2 milestone M1) on the recorded demo replay (tests/e2_demo.py).
"""
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from second_scribe.app import profile_view as pv

FIX = Path(__file__).resolve().parent / "fixtures" / "profiles"
STUDY = FIX / "profile-v2.json"
STUDY3 = FIX / "profile-v3.json"                   # the study's public file: run block in the §14 form
SYNTH = FIX / "profile-synth.json"
# STUDY_SHA, STUDY3_SHA: the v2 and v3 fixtures with their reading-3 cells at temperature 0 (council 23 fix 4); the real
# v2 and v3 files (e5ef8840..., 20ec3796...) carry the temperature-0.7 point and no longer ship.
STUDY_SHA = "1f755871604b5c734039312c8effa219fa14a758a99a7fb27e92e879f511457b"
STUDY3_SHA = "618341e4973da4014a9b20bd1fe7cbc5a69221709eb31408a1b05592060f54dd"
SYNTH_SHA = "5bc4716a994d732dd8858d9f2d1f379f22ea1b87d8700b21b80789714fa1feb1"
SAMPLE = FIX / "e2-m1-sample-demo.json"
SAMPLE_SHA = "33e41001d5e6dd871fc4fa142d4575372c0f60226b8d41d892689ba28bb3e661"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="ss-tests-profile-")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def write(self, doc, name="p.json") -> Path:
        p = self.tmp / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p


class TestFixtures(unittest.TestCase):
    def test_fixture_hashes(self):
        self.assertEqual(sha(STUDY), STUDY_SHA)
        self.assertEqual(sha(STUDY3), STUDY3_SHA)
        self.assertEqual(sha(SYNTH), SYNTH_SHA)

    def test_sources_md_records_both(self):
        text = (FIX / "SOURCES.md").read_text(encoding="utf-8")
        self.assertIn(STUDY_SHA, text)
        self.assertIn(STUDY3_SHA, text)
        self.assertIn(SYNTH_SHA, text)

    def test_both_load(self):
        for p, state in ((STUDY, "reported"), (STUDY3, "reported"), (SYNTH, "synthetic")):
            with self.subTest(p.name):
                doc = pv.load_profile(p)
                self.assertEqual(doc["schema"], "orgsci-profile/v2")
                self.assertEqual(doc["state"], state)
                self.assertEqual([r["row"] for r in doc["rows"]],
                                 ["lead", "ledger", 1, 2, 3, 4, 5, 6, 7, 8, 9])

    def test_load_keeps_file_order_and_values(self):
        for p in (STUDY, STUDY3):
            with self.subTest(p.name):
                self.assertEqual(pv.load_profile(p), json.loads(p.read_text(encoding="utf-8")))

    def test_v3_has_v2s_cells_and_values_and_the_public_run_block(self):
        v2, v3 = pv.load_profile(STUDY), pv.load_profile(STUDY3)
        self.assertEqual(pv.cells(v2), pv.cells(v3))

        keep = ("name", "x", "n", "unit", "kind", "value", "ci_lo", "ci_hi", "ci_level", "p", "p_holm", "state")

        def values(doc):        # the numbers; v3 words the "source" notes as "gate chain entry NNN" (or "entries NNN+NNN") and adds formula codes
            return [{k: c[k] for k in keep if k in c} for r in doc["rows"] for c in r["cells"]]
        self.assertEqual(values(v2), values(v3))
        self.assertTrue(all(re.fullmatch(r"gate chain entr(?:y|ies) \d{3}(?:\+\d{3})*", c["source"]) for r in v3["rows"] for c in r["cells"]
                            if isinstance(c.get("source"), str)))
        run = v3["run"]
        self.assertIsNone(run["run_chain_heads"])
        self.assertEqual(run["run_chain_heads_source"], "available_from_authors")
        self.assertIsInstance(run["kit"]["run_commits"], list)
        self.assertIsNone(run["kit"]["release_commit"])
        self.assertNotIn("release_branch", run["kit"])
        self.assertEqual([m["source"] for m in run["models"] if None in m.values()], ["not_recorded_public"] * 3)


class TestInvalid(TmpCase):
    def load_mut(self, mutate, base=STUDY, name="p.json"):
        doc = json.loads(base.read_text(encoding="utf-8"))
        mutate(doc)
        return pv.load_profile(self.write(doc, name))

    def bad(self, mutate, path_part, reason_part="", base=STUDY):
        with self.assertRaises(pv.ProfileInvalid) as cm:
            self.load_mut(mutate, base)
        e = cm.exception
        self.assertIn(path_part, e.path)
        self.assertIn(reason_part, e.reason)
        self.assertIn(e.path, str(e))
        return e

    # ---- file level
    def test_not_json(self):
        p = self.tmp / "x.json"
        p.write_text("{nope", encoding="utf-8")
        with self.assertRaises(pv.ProfileInvalid) as cm:
            pv.load_profile(p)
        self.assertEqual(cm.exception.path, "$")

    def test_nan_refused(self):
        p = self.tmp / "x.json"
        p.write_text(STUDY.read_text(encoding="utf-8").replace('"value": 0.614', '"value": NaN', 1),
                     encoding="utf-8")
        with self.assertRaises(pv.ProfileInvalid):
            pv.load_profile(p)

    def test_duplicate_key_refused(self):
        p = self.tmp / "x.json"
        p.write_text('{"schema": "orgsci-profile/v2", "schema": "orgsci-profile/v2"}', encoding="utf-8")
        with self.assertRaises(pv.ProfileInvalid):
            pv.load_profile(p)

    def test_not_an_object(self):
        p = self.tmp / "x.json"
        p.write_text("[1]", encoding="utf-8")
        with self.assertRaises(pv.ProfileInvalid):
            pv.load_profile(p)

    # ---- schema / top level
    def test_unknown_major(self):
        for s in ("orgsci-profile/v3", "orgsci-profile/v1", "orgsci-profile/v10", "other/v2", 2):
            with self.subTest(s):
                self.bad(lambda d, s=s: d.__setitem__("schema", s), "schema")

    def test_minor_of_v2_accepted(self):
        doc = self.load_mut(lambda d: d.__setitem__("schema", "orgsci-profile/v2.1"))
        self.assertEqual(doc["schema"], "orgsci-profile/v2.1")

    def test_missing_top_level_keys(self):
        for k in ("schema", "state", "population", "reference", "pooling", "rows", "run"):
            with self.subTest(k):
                self.bad(lambda d, k=k: d.pop(k), k if k != "schema" else "schema")

    def test_bad_file_state(self):
        for s in ("final", None, "Reported"):
            with self.subTest(s):
                self.bad(lambda d, s=s: d.__setitem__("state", s), "state")

    def test_file_states_accepted(self):
        for s in ("reported", "synthetic", "pending"):
            doc = self.load_mut(lambda d, s=s: d.__setitem__("state", s))
            self.assertEqual(doc["state"], s)

    def test_population_shape(self):
        self.bad(lambda d: d.__setitem__("population", []), "population")
        self.bad(lambda d: d["population"].pop("n_people"), "population.n_people")
        self.bad(lambda d: d["population"].__setitem__("n_people", "43"), "population.n_people")
        self.bad(lambda d: d["population"].__setitem__("n_people", True), "population.n_people")

    def test_meta_analysis_must_be_false(self):
        self.bad(lambda d: d["pooling"].__setitem__("meta_analysis", True), "pooling.meta_analysis")

    def test_reference_pooling_run_must_be_objects(self):
        for k in ("reference", "pooling", "run"):
            self.bad(lambda d, k=k: d.__setitem__(k, "x"), k)

    def test_detection_limits_optional_but_object(self):
        doc = self.load_mut(lambda d: d.__setitem__("detection_limits", {"schema": "orgsci-detection-limits/v1"}))
        self.assertIn("detection_limits", doc)
        self.bad(lambda d: d.__setitem__("detection_limits", 3), "detection_limits")

    # ---- rows
    def test_rows_not_a_list(self):
        self.bad(lambda d: d.__setitem__("rows", {}), "rows")

    def test_rows_wrong_order(self):
        def swap(d):
            d["rows"][0], d["rows"][1] = d["rows"][1], d["rows"][0]
        e = self.bad(swap, "rows[0].row")
        self.assertIn("lead", e.reason)

    def test_rows_missing_one(self):
        self.bad(lambda d: d["rows"].pop(5), "rows")

    def test_rows_extra(self):
        self.bad(lambda d: d["rows"].append(copy.deepcopy(d["rows"][-1])), "rows")

    def test_row_not_object(self):
        self.bad(lambda d: d["rows"].__setitem__(3, 5), "rows[3]")

    def test_row_id_is_int_not_string(self):
        self.bad(lambda d: d["rows"][2].__setitem__("row", "1"), "rows[2].row")

    def test_row_required_fields(self):
        for k in ("label_key", "population", "n_people", "cells", "rests_on", "source", "qualifiers"):
            with self.subTest(k):
                self.bad(lambda d, k=k: d["rows"][4].pop(k), f"rows[4].{k}")

    def test_row_field_types(self):
        self.bad(lambda d: d["rows"][4].__setitem__("label_key", ""), "rows[4].label_key")
        self.bad(lambda d: d["rows"][4].__setitem__("population", 43), "rows[4].population")
        self.bad(lambda d: d["rows"][4].__setitem__("n_people", 4.5), "rows[4].n_people")
        self.bad(lambda d: d["rows"][4].__setitem__("source", "cleared/003"), "rows[4].source")
        self.bad(lambda d: d["rows"][4].__setitem__("rests_on", "R"), "rows[4].rests_on")
        self.bad(lambda d: d["rows"][4].__setitem__("qualifiers", [1]), "rows[4].qualifiers")
        self.bad(lambda d: d["rows"][4].__setitem__("cells", None), "rows[4].cells")

    # ---- cells
    def cell0(self, d, row=2):
        return d["rows"][row]["cells"][0]

    def test_cell_not_object(self):
        self.bad(lambda d: d["rows"][2]["cells"].__setitem__(0, []), "rows[2].cells[0]")

    def test_cell_required_fields(self):
        for k in ("name", "source", "value"):
            with self.subTest(k):
                self.bad(lambda d, k=k: self.cell0(d).pop(k), f"rows[2].cells[0].{k}")

    def test_cell_field_types(self):
        c = "rows[2].cells[0]."
        self.bad(lambda d: self.cell0(d).__setitem__("name", ""), c + "name")
        self.bad(lambda d: self.cell0(d).__setitem__("x", 3), c + "x")
        self.bad(lambda d: self.cell0(d).__setitem__("value", "12"), c + "value")
        self.bad(lambda d: self.cell0(d).__setitem__("value", True), c + "value")
        self.bad(lambda d: self.cell0(d).__setitem__("value", None), c + "value")
        self.bad(lambda d: self.cell0(d).__setitem__("n", 1.5), c + "n")
        self.bad(lambda d: self.cell0(d).__setitem__("ci_lo", "a"), c + "ci_lo")
        self.bad(lambda d: self.cell0(d).__setitem__("ci_level", "0.95"), c + "ci_level")
        self.bad(lambda d: self.cell0(d).__setitem__("p", "x"), c + "p")
        self.bad(lambda d: self.cell0(d).__setitem__("source", 3), c + "source")
        self.bad(lambda d: self.cell0(d).__setitem__("unit", 3), c + "unit")
        self.bad(lambda d: self.cell0(d).__setitem__("qualifiers", "x"), c + "qualifiers")
        self.bad(lambda d: self.cell0(d).__setitem__("rests_on", [None]), c + "rests_on")
        self.bad(lambda d: self.cell0(d).__setitem__("note", 3), c + "note")

    def test_duplicate_cell_in_row(self):
        def dup(d):
            cs = d["rows"][2]["cells"]
            cs.append(copy.deepcopy(cs[0]))
        e = self.bad(dup, "rows[2].cells[")
        self.assertIn("duplicate", e.reason)

    def test_same_name_different_x_is_fine(self):
        def addx(d):
            cs = d["rows"][2]["cells"]
            c = copy.deepcopy(cs[0])
            c["x"] = "zz_other"
            cs.append(c)
        doc = self.load_mut(addx)
        self.assertEqual(len(doc["rows"][2]["cells"]), len(json.loads(STUDY.read_text())["rows"][2]["cells"]) + 1)

    def test_source_placeholder_allowed_in_cell_value(self):
        doc = self.load_mut(lambda d: self.cell0(d).__setitem__("value", "[SOURCE]"))
        self.assertEqual(doc["rows"][2]["cells"][0]["value"], "[SOURCE]")

    def test_source_placeholder_allowed_in_run(self):
        doc = self.load_mut(lambda d: d["run"].__setitem__("run_chain_heads", {"j": "[SOURCE]"}))
        self.assertEqual(doc["run"]["run_chain_heads"]["j"], "[SOURCE]")

    # ---- the public form of the run block (PROFILE-SCHEMA section 4 / 14; profile-v3)
    def test_public_run_block_accepted(self):
        doc = self.load_mut(lambda d: None, STUDY3)
        self.assertIsNone(doc["run"]["run_chain_heads"])

    def test_chain_heads_source_is_a_code(self):
        for ok in ("available_from_authors", "withheld", "a.b-c_9", "x" * 40, None):
            with self.subTest(ok=ok):
                self.load_mut(lambda d: d["run"].__setitem__("run_chain_heads_source", ok), STUDY3)
        for bad in ("x" * 41, "Heads are held by the authors", "Upper", "a b", "", 7, True, ["a"], {"a": 1}):
            with self.subTest(bad=bad):
                self.bad(lambda d: d["run"].__setitem__("run_chain_heads_source", bad), "run_chain_heads_source",
                         base=STUDY3)

    def test_chain_heads_source_not_beside_heads(self):
        self.bad(lambda d: d["run"].__setitem__("run_chain_heads", {"q": "a" * 64}), "run_chain_heads_source",
                 "only where run_chain_heads is null", base=STUDY3)
        doc = self.load_mut(lambda d: d["run"].pop("run_chain_heads_source"), STUDY3)   # null heads, no source: older files
        self.assertIsNone(doc["run"]["run_chain_heads"])

    def test_run_commits_list_and_null_release_commit(self):
        self.load_mut(lambda d: d["run"]["kit"].__setitem__("run_commits", []), STUDY3)
        self.bad(lambda d: d["run"]["kit"].__setitem__("run_commits", ["abc"]), "run_commits[0]", base=STUDY3)
        self.bad(lambda d: d["run"]["kit"].__setitem__("run_commits", {"x": "a" * 40}), "run_commits", base=STUDY3)
        self.bad(lambda d: d["run"]["kit"].__setitem__("release_commit", "main"), "release_commit", base=STUDY3)

    def test_null_model_fields_with_source(self):
        doc = self.load_mut(lambda d: d["run"]["models"][3].__setitem__("model_id", None), STUDY3)
        self.assertIsNone(doc["run"]["models"][3]["model_id"])
        self.bad(lambda d: d["run"]["models"][0].__setitem__("quantisation", 8), "models[0].quantisation", base=STUDY3)
        self.bad(lambda d: d["run"].__setitem__("models", {"role": "judge"}), "run.models", base=STUDY3)
        self.bad(lambda d: d["run"]["models"].__setitem__(1, "judge"), "models[1]", base=STUDY3)

    def test_other_strings_not_allowed_as_value(self):
        self.bad(lambda d: self.cell0(d).__setitem__("value", "[source]"), "value")

    # ---- cell state
    def test_cell_state_values(self):
        c = "rows[2].cells[0].state"
        self.bad(lambda d: self.cell0(d).__setitem__("state", "done"), c)
        self.bad(lambda d: self.cell0(d).__setitem__("state", 1), c)

    def test_state_absent_literal_accepted_as_computed(self):
        doc = self.load_mut(lambda d: self.cell0(d).__setitem__("state", "absent"))
        self.assertEqual(doc["rows"][2]["cells"][0]["state"], "absent")

    def test_partial_not_run_keeps_value(self):
        def mut(d):
            self.cell0(d)["state"] = "partial_not_run"
        doc = self.load_mut(mut)
        self.assertEqual(doc["rows"][2]["cells"][0]["state"], "partial_not_run")
        self.bad(lambda d: (self.cell0(d).__setitem__("state", "partial_not_run"),
                            self.cell0(d).__setitem__("value", None)), "rows[2].cells[0].value")

    def not_run(self, d, **kw):
        c = self.cell0(d)
        for k in ("value", "ci_lo", "ci_hi", "p"):
            c.pop(k, None)
        c.update(state="not_run", value=None, needs=["J2"])
        c.update(kw)
        for k, v in list(c.items()):
            if v is ...:
                del c[k]
        return c

    def test_not_run_ok(self):
        doc = self.load_mut(lambda d: self.not_run(d))
        self.assertIsNone(doc["rows"][2]["cells"][0]["value"])
        doc = self.load_mut(lambda d: self.not_run(d, needs=["J2", "sidejudge", "ladder", "judge_script"]))
        self.assertEqual(len(doc["rows"][2]["cells"][0]["needs"]), 4)

    def test_not_run_value_must_be_none(self):
        c = "rows[2].cells[0].value"
        self.bad(lambda d: self.not_run(d, value=3), c)
        self.bad(lambda d: self.not_run(d, value="[SOURCE]"), c)

    def test_not_run_value_key_may_be_missing(self):
        # the parity file writes not_run cells with no value key at all
        doc = self.load_mut(lambda d: self.not_run(d, value=...))
        self.assertNotIn("value", doc["rows"][2]["cells"][0])

    def test_not_run_needs_rules(self):
        c = "rows[2].cells[0].needs"
        self.bad(lambda d: self.not_run(d, needs=["J3"]), c)
        self.bad(lambda d: self.not_run(d, needs="J2"), c)
        self.bad(lambda d: self.not_run(d, needs=[1]), c)
        # empty or absent needs: only with a stated reason or note (exporter note, legacy parity reason)
        self.bad(lambda d: self.not_run(d, needs=[]), c)
        self.bad(lambda d: self.not_run(d, needs=...), c)
        doc = self.load_mut(lambda d: self.not_run(d, needs=..., reason="needs the second judge"))
        self.assertNotIn("needs", doc["rows"][2]["cells"][0])
        doc = self.load_mut(lambda d: self.not_run(d, needs=[], note="grid_spec_missing"))
        self.assertEqual(doc["rows"][2]["cells"][0]["needs"], [])

    def test_needs_only_with_not_run(self):
        self.bad(lambda d: self.cell0(d).__setitem__("needs", ["J2"]), "rows[2].cells[0].needs")

    # ---- derived
    def test_derived_true_needs_formula(self):
        c = "rows[0].cells"

        def find(d):
            return next(x for x in d["rows"][0]["cells"] if x.get("derived") is True)

        def drop(d):
            find(d).pop("formula")
        e = self.bad(drop, c)
        self.assertIn("formula", e.reason)
        self.bad(lambda d: find(d).__setitem__("formula", ""), c)
        self.bad(lambda d: find(d).__setitem__("formula", 3), c)

    def test_derived_types(self):
        c = "rows[2].cells[0].derived"
        self.bad(lambda d: self.cell0(d).__setitem__("derived", False), c)
        self.bad(lambda d: self.cell0(d).__setitem__("derived", 3), c)
        self.bad(lambda d: self.cell0(d).__setitem__("derived", ""), c)
        doc = self.load_mut(lambda d: self.cell0(d).__setitem__("derived", "net"))
        self.assertEqual(doc["rows"][2]["cells"][0]["derived"], "net")

    # ---- bands
    def find_band_cell(self, d):
        for ri, r in enumerate(d["rows"]):
            for ci, c in enumerate(r["cells"]):
                if "band" in c and "lo" in c["band"] and c["band"].get("members"):
                    return ri, ci, c
        raise AssertionError("no band cell")

    def test_band_rules(self):
        base = json.loads(STUDY.read_text(encoding="utf-8"))
        ri, ci, _ = self.find_band_cell(base)
        p = f"rows[{ri}].cells[{ci}].band"
        self.bad(lambda d: d["rows"][ri]["cells"][ci].__setitem__("band", []), p)
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"].pop("lo"), p + ".lo")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"].__setitem__("hi", "9"), p + ".hi")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"].__setitem__("over", "everything"), p + ".over")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"].pop("over"), p + ".over")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"].__setitem__("members", {}), p + ".members")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"]["members"].__setitem__(0, {"value": 1}),
                 p + ".members[0].x")
        self.bad(lambda d: d["rows"][ri]["cells"][ci]["band"]["members"][0].__setitem__("value", "a"),
                 p + ".members[0].value")

    def test_row_level_band_checked(self):
        self.bad(lambda d: d["rows"][2].__setitem__("band", {"lo": 1}), "rows[2].band")

    def test_band_not_run_form_accepted(self):
        band = {"state": "not_run", "over": "labellers", "reason": "cleared/018 not_run"}
        doc = self.load_mut(lambda d: d["rows"][5].__setitem__("band", band))
        self.assertEqual(doc["rows"][5]["band"], band)
        self.bad(lambda d: d["rows"][5].__setitem__("band", {"state": "weird", "over": "labellers"}),
                 "rows[5].band.state")

    # ---- the synthetic fixture trips the same rules
    def test_synth_not_run_rules_apply(self):
        def mut(d):
            c = next(c for r in d["rows"] for c in r["cells"] if c.get("state") == "not_run")
            c["value"] = 0.5
        with self.assertRaises(pv.ProfileInvalid):
            self.load_mut(mut, base=SYNTH)


class TestProfileToJs(unittest.TestCase):
    def test_shape_and_roundtrip(self):
        doc = pv.load_profile(STUDY)
        js = pv.profile_to_js(doc, "SS_STUDY")
        self.assertTrue(js.startswith("window.SS_STUDY = "))
        self.assertTrue(js.rstrip().endswith(";"))
        body = js[len("window.SS_STUDY = "):].rstrip()[:-1]
        self.assertEqual(json.loads(body), doc)
        self.assertEqual(list(json.loads(body)["rows"][0]), list(doc["rows"][0]))   # order kept

    def test_escapes(self):
        doc = {"a": "</script><script>x</script>", "b": "line sep par", "c": "<!-- <script>",
               "d": "é", "e": "</"}
        js = pv.profile_to_js(doc, "X")
        self.assertNotIn("</", js)
        self.assertNotIn("<!--", js)
        self.assertNotIn(" ", js)
        self.assertNotIn(" ", js)
        self.assertIn("<\\/script>", js)
        body = js[len("window.X = "):].rstrip()[:-1]
        self.assertEqual(json.loads(body), doc)

    def test_single_line(self):
        js = pv.profile_to_js({"a": "x\ny"}, "X")
        self.assertEqual(js.count("\n"), 0)

    def test_var_names(self):
        for ok in ("X", "_x1", "SS_PROFILE"):
            pv.profile_to_js({}, ok)
        for bad in ("", "1x", "a-b", "a.b", "a b", "x;alert(1)", "a\n", "é"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    pv.profile_to_js({}, bad)

    def test_nan_refused(self):
        with self.assertRaises(ValueError):
            pv.profile_to_js({"a": float("nan")}, "X")


class TestCells(unittest.TestCase):
    def test_unique_on_both_fixtures(self):
        for p in (STUDY, SYNTH):
            with self.subTest(p.name):
                doc = pv.load_profile(p)
                cs = pv.cells(doc)
                self.assertEqual(len(cs), len(set(cs)))
                self.assertEqual(len(cs), sum(len(r["cells"]) for r in doc["rows"]))

    def test_counts_and_order(self):
        cs = pv.cells(pv.load_profile(STUDY))
        self.assertEqual(len(cs), 147)
        self.assertEqual(cs[0][0], "lead")
        self.assertEqual(cs[0], ("lead", "ai_recall", "primary"))
        self.assertEqual(len(pv.cells(pv.load_profile(SYNTH))), 144)

    def test_x_missing_is_none(self):
        doc = {"rows": [{"row": 1, "cells": [{"name": "a"}, {"name": "b", "x": "z"}]}]}
        self.assertEqual(pv.cells(doc), [(1, "a", None), (1, "b", "z")])

    def test_same_name_in_other_row_is_fine(self):
        doc = {"rows": [{"row": 1, "cells": [{"name": "a"}]}, {"row": 2, "cells": [{"name": "a"}]}]}
        self.assertEqual(len(pv.cells(doc)), 2)

    def test_duplicate_raises(self):
        doc = {"rows": [{"row": 1, "cells": [{"name": "a", "x": "p"}, {"name": "a", "x": "p"}]}]}
        with self.assertRaises(pv.ProfileInvalid):
            pv.cells(doc)
        doc = {"rows": [{"row": 1, "cells": [{"name": "a"}, {"name": "a"}]}]}
        with self.assertRaises(pv.ProfileInvalid):
            pv.cells(doc)


FAKE = r'''#!{py}
import hashlib, json, sys
a = sys.argv[1:]
log = {log!r}
open(log, "w").write(json.dumps(a))
mode = {mode!r}
if a[:3] != ["-m", "second_scribe.cli", "export-profile"]:
    sys.stderr.write("unexpected argv\n"); sys.exit(9)
prof = a[a.index("--profile") + 1]
if mode in ("ok", "badsha", "nofile_ok", "bare", "twosha"):
    if mode != "nofile_ok":
        open(prof, "w").write('{{"hello": 1}}')
    h = hashlib.sha256(open(prof, "rb").read()).hexdigest() if mode in ("ok", "bare", "twosha") else "0" * 64
    if mode == "nofile_ok":
        h = "1" * 64
    if mode == "bare":
        print(h)
    else:
        print("wrote " + prof)
        print("sha256 " + h)
        if mode == "twosha":
            print("sha256 " + "2" * 64)
    sys.exit(0)
if mode == "refuse":
    sys.stderr.write("refused: n_analysed 7 < 10\nrefused: privacy scan: key 'role' outside run\n\n")
    sys.exit(2)
if mode == "usage":
    sys.stderr.write("usage: second_scribe.cli export-profile [-h] --config CONFIG\nerror: unrecognized arguments\n")
    sys.exit(2)
if mode == "junk":
    print("not a hash")
    sys.exit(0)
if mode == "crash":
    sys.stderr.write("Traceback: boom\n")
    sys.exit(1)
if mode == "exit9":
    sys.exit(9)
'''


class TestRunExporter(TmpCase):
    def fake(self, mode):
        log = self.tmp / f"argv-{mode}.json"
        exe = self.tmp / f"fakepy-{mode}"
        exe.write_text(FAKE.format(py=sys.executable, log=str(log), mode=mode), encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return str(exe), log

    def test_exit0_returns_sha_of_written_file(self):
        py, log = self.fake("ok")
        cfg, out, prof = self.tmp / "run.json", self.tmp / "out", self.tmp / "profile.json"
        h = pv.run_exporter(cfg, out, prof, python=py)
        self.assertEqual(h, hashlib.sha256(prof.read_bytes()).hexdigest())
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(json.loads(log.read_text()),
                         ["-m", "second_scribe.cli", "export-profile", "--config", str(cfg),
                          "--out-dir", str(out), "--profile", str(prof), "--B", "2000", "--seed", "20261017"])

    def test_options_passed_in_cli_order(self):
        py, log = self.fake("ok")
        cfg, prof, info = self.tmp / "run.json", self.tmp / "profile.json", self.tmp / "models.json"
        pv.run_exporter(cfg, None, prof, B=50, seed=7, model_info=info, reference_type="B", python=py)
        self.assertEqual(json.loads(log.read_text())[3:],
                         ["--config", str(cfg), "--profile", str(prof), "--B", "50", "--seed", "7",
                          "--model-info", str(info), "--reference-type", "B"])

    def test_defaults_are_b2000_seed20261017(self):
        import inspect
        params = inspect.signature(pv.run_exporter).parameters
        self.assertEqual((params["B"].default, params["seed"].default), (2000, 20261017))
        self.assertEqual((pv.DEFAULT_B, pv.DEFAULT_SEED), (2000, 20261017))

    def test_bad_b_or_seed_refused_before_running(self):
        py, log = self.fake("ok")
        for kw in ({"B": 0}, {"B": True}, {"B": "2000"}, {"seed": -1}, {"seed": 1.5}):
            with self.subTest(kw):
                with self.assertRaises(ValueError):
                    pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py, **kw)
        self.assertFalse(log.exists())

    def test_relative_paths_made_absolute(self):
        # the exporter runs with the kit root as its working directory, so every path it is given is absolute
        py, log = self.fake("ok")
        here = os.getcwd()
        os.chdir(self.tmp)
        try:
            pv.run_exporter(Path("c.json"), Path("o"), Path("p.json"), python=py)
        finally:
            os.chdir(here)
        argv = json.loads(log.read_text())
        for flag in ("--config", "--out-dir", "--profile"):
            self.assertTrue(os.path.isabs(argv[argv.index(flag) + 1]), flag)

    def test_out_dir_none_omits_flag(self):
        py, log = self.fake("ok")
        pv.run_exporter(self.tmp / "run.json", None, self.tmp / "profile.json", python=py)
        argv = json.loads(log.read_text())
        self.assertNotIn("--out-dir", argv)
        self.assertEqual(argv[3:5], ["--config", str(self.tmp / "run.json")])

    def test_no_shell_for_odd_paths(self):
        py, log = self.fake("ok")
        odd = self.tmp / "a b;touch pwned $(x)"
        odd.mkdir()
        pv.run_exporter(odd / "c.json", odd / "o", odd / "p.json", python=py)
        self.assertEqual(json.loads(log.read_text())[4], str(odd / "c.json"))
        self.assertFalse((self.tmp / "pwned").exists())

    def test_exit2_is_refusal_with_reasons(self):
        py, _ = self.fake("refuse")
        with self.assertRaises(pv.ProfileRefused) as cm:
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)
        self.assertEqual(cm.exception.reasons, ["n_analysed 7 < 10", "privacy scan: key 'role' outside run"])
        self.assertIn("n_analysed 7 < 10", str(cm.exception))

    def test_exit2_without_refused_lines_is_not_a_refusal(self):
        # argparse also exits 2 (a bad command line): that is a bug in the call, not the exporter's refusal
        py, _ = self.fake("usage")
        with self.assertRaises(RuntimeError) as cm:
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)
        self.assertNotIsInstance(cm.exception, pv.ProfileRefused)

    def test_stdout_must_carry_one_sha256_line(self):
        for mode in ("bare", "twosha"):
            py, _ = self.fake(mode)
            with self.subTest(mode):
                with self.assertRaises(RuntimeError) as cm:
                    pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)
                self.assertIn("sha256", str(cm.exception))

    def test_refused_is_not_runtime_error(self):
        self.assertFalse(issubclass(pv.ProfileRefused, RuntimeError))
        self.assertEqual(pv.ProfileRefused(["a"]).reasons, ["a"])

    def test_other_exit_is_runtime_error(self):
        for mode, code in (("crash", "1"), ("exit9", "9")):
            py, _ = self.fake(mode)
            with self.subTest(mode):
                with self.assertRaises(RuntimeError) as cm:
                    pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)
                self.assertNotIsInstance(cm.exception, pv.ProfileRefused)
                self.assertIn(code, str(cm.exception))

    def test_sha_mismatch(self):
        py, _ = self.fake("badsha")
        with self.assertRaises(RuntimeError) as cm:
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)
        self.assertIn("sha256", str(cm.exception))

    def test_stdout_not_a_hash(self):
        py, _ = self.fake("junk")
        with self.assertRaises(RuntimeError):
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=py)

    def test_file_missing_after_exit0(self):
        py, _ = self.fake("nofile_ok")
        with self.assertRaises(RuntimeError):
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "nope.json", python=py)

    def test_interpreter_missing(self):
        with self.assertRaises(RuntimeError):
            pv.run_exporter(self.tmp / "c.json", None, self.tmp / "p.json", python=str(self.tmp / "no-such-python"))

    def test_default_python_is_sys_executable(self):
        import inspect
        self.assertEqual(inspect.signature(pv.run_exporter).parameters["python"].default, sys.executable)


if __name__ == "__main__":
    unittest.main()


class TestSchema13(unittest.TestCase):
    """PROFILE-SCHEMA §13 (18 Oct): reason_code satisfies a not_run cell; missing is drawn from band/n/ci."""

    def _doc(self):
        import json as _json
        return _json.loads((Path(__file__).parent / "fixtures" / "profiles" / "profile-synth.json").read_text())

    def _first_cell(self, doc):
        return doc["rows"][0]["cells"][0]

    def test_reason_code_satisfies_not_run(self):
        import json as _json, tempfile as _tf
        doc = self._doc()
        c = self._first_cell(doc)
        for k in ("value", "needs", "reason", "note"):
            c.pop(k, None)
        c.update({"state": "not_run", "value": None, "reason_code": "needs_J2"})
        with _tf.TemporaryDirectory(prefix="ss-tests-") as d:
            f = Path(d) / "p.json"; f.write_text(_json.dumps(doc))
            pv.load_profile(f)

    def test_missing_bad_code_refused(self):
        import json as _json, tempfile as _tf
        doc = self._doc()
        self._first_cell(doc)["missing"] = ["band", "everything"]
        with _tf.TemporaryDirectory(prefix="ss-tests-") as d:
            f = Path(d) / "p.json"; f.write_text(_json.dumps(doc))
            with self.assertRaises(pv.ProfileInvalid):
                pv.load_profile(f)

    def test_formula_code_satisfies_derived(self):
        import json as _json, tempfile as _tf
        doc = self._doc()
        cells = [c for r in doc["rows"] for c in r["cells"] if c.get("derived") is True]
        self.assertTrue(cells)
        for c in cells:
            c["formula_code"] = "a1_coverage"
            c.pop("formula", None)
        with _tf.TemporaryDirectory(prefix="ss-tests-") as d:
            f = Path(d) / "p.json"; f.write_text(_json.dumps(doc))
            pv.load_profile(f)


HEX_A = "ab" * 32
COMMIT = "6b8c3986d46cb6190ff2e9a7fa0888f8dc339911"


class TestRunBlockShapes(TmpCase):
    """PROFILE-SCHEMA §4 (the study's v2 run block) and §14 (the kit exporter's run block, E2 M1 6b8c398): load_profile
    accepts both forms of run_chain_heads, kit.run_commits and pooling.within, and refuses anything in between."""

    def load_mut(self, mutate, base=SAMPLE):
        doc = json.loads(base.read_text(encoding="utf-8"))
        mutate(doc)
        return pv.load_profile(self.write(doc))

    def bad(self, mutate, path_part, base=SAMPLE):
        with self.assertRaises(pv.ProfileInvalid) as cm:
            self.load_mut(mutate, base)
        self.assertIn(path_part, cm.exception.path)
        return cm.exception

    def heads(self, d, value):
        d["run"]["run_chain_heads"] = value

    def test_sample_fixture_hash(self):
        self.assertEqual(sha(SAMPLE), SAMPLE_SHA)
        self.assertIn(SAMPLE_SHA, (FIX / "SOURCES.md").read_text(encoding="utf-8"))

    def test_sample_export_loads_with_kit_shapes(self):
        doc = pv.load_profile(SAMPLE)
        self.assertEqual(doc["state"], "synthetic")
        self.assertEqual(sorted(doc["run"]["run_chain_heads"]), ["head", "manifest_hash", "records", "source"])
        self.assertIsInstance(doc["run"]["kit"]["run_commits"], list)
        self.assertEqual(doc["pooling"]["within"], ["reference_type", "prompt_set_pair", "state"])
        self.assertEqual(len(pv.cells(doc)), 147)
        self.assertEqual(sum(1 for r in doc["rows"] for c in r["cells"] if c.get("state") == "not_run"), 14)

    def test_study_v2_shapes_still_load(self):
        doc = pv.load_profile(STUDY)
        self.assertEqual(sorted(doc["run"]["run_chain_heads"]), ["j", "m", "q", "source"])
        self.assertIsInstance(doc["run"]["kit"]["run_commits"], dict)
        self.assertEqual(doc["pooling"]["within"], ["reference_type", "prompt_set"])

    # ---- run_chain_heads
    def test_kit_heads_variants_accepted(self):
        for value in (None,
                      {"head": HEX_A, "records": 0, "manifest_hash": HEX_A, "source": "x"},
                      {"head": HEX_A, "records": 5, "manifest_hash": None},
                      {"head": None, "records": None, "manifest_hash": None, "source": "available from the authors"},
                      {"head": "[SOURCE]", "records": "[SOURCE]", "manifest_hash": "[SOURCE]"}):
            with self.subTest(value):
                self.load_mut(lambda d, v=value: self.heads(d, v))

    def test_phase_heads_accepted(self):
        self.load_mut(lambda d: self.heads(d, {"j": HEX_A, "m": None, "source": "x"}))
        self.load_mut(lambda d: self.heads(d, {"q": "[SOURCE]"}))

    def test_heads_mixed_shape_refused(self):
        e = self.bad(lambda d: self.heads(d, {"head": HEX_A, "records": 1, "manifest_hash": HEX_A, "j": HEX_A}),
                     "run.run_chain_heads")
        self.assertIn("shape", e.reason)
        self.bad(lambda d: self.heads(d, {"j": HEX_A, "records": 3}), "run.run_chain_heads")

    def test_heads_kit_shape_needs_all_three(self):
        for k in ("head", "records", "manifest_hash"):
            with self.subTest(k):
                def mut(d, k=k):
                    h = {"head": HEX_A, "records": 1, "manifest_hash": HEX_A}
                    h.pop(k)
                    self.heads(d, h)
                self.bad(mut, "run.run_chain_heads")

    def test_heads_value_types(self):
        p = "run.run_chain_heads."
        kit = {"head": HEX_A, "records": 1, "manifest_hash": HEX_A, "source": "x"}
        self.bad(lambda d: self.heads(d, {**kit, "head": "abc"}), p + "head")
        self.bad(lambda d: self.heads(d, {**kit, "head": HEX_A.upper()}), p + "head")
        self.bad(lambda d: self.heads(d, {**kit, "manifest_hash": 3}), p + "manifest_hash")
        self.bad(lambda d: self.heads(d, {**kit, "records": -1}), p + "records")
        self.bad(lambda d: self.heads(d, {**kit, "records": True}), p + "records")
        self.bad(lambda d: self.heads(d, {**kit, "records": 2.0}), p + "records")
        self.bad(lambda d: self.heads(d, {**kit, "source": 4}), p + "source")
        self.bad(lambda d: self.heads(d, {"j": "nothex"}), p + "j")
        self.bad(lambda d: self.heads(d, {"source": "only a source"}), "run.run_chain_heads")
        self.bad(lambda d: self.heads(d, [HEX_A]), "run.run_chain_heads")

    # ---- kit.run_commits
    def commits(self, d, value):
        d["run"]["kit"]["run_commits"] = value

    def test_run_commits_forms_accepted(self):
        for value in ([COMMIT], [], [COMMIT, COMMIT], None, {"q": COMMIT, "j": COMMIT, "m": COMMIT}, {"j": "[SOURCE]"}):
            with self.subTest(value):
                self.load_mut(lambda d, v=value: self.commits(d, v))

    def test_run_commits_malformed_refused(self):
        p = "run.kit.run_commits"
        self.bad(lambda d: self.commits(d, [{"q": COMMIT}]), p + "[0]")
        self.bad(lambda d: self.commits(d, ["not a commit"]), p + "[0]")
        self.bad(lambda d: self.commits(d, [COMMIT[:7]]), p + "[0]")
        self.bad(lambda d: self.commits(d, [HEX_A]), p + "[0]")
        self.bad(lambda d: self.commits(d, {"x": COMMIT}), p)
        self.bad(lambda d: self.commits(d, {"q": [COMMIT]}), p + ".q")
        self.bad(lambda d: self.commits(d, COMMIT), p)

    def test_kit_block_types(self):
        self.bad(lambda d: d["run"].__setitem__("kit", [COMMIT]), "run.kit")
        self.bad(lambda d: d["run"]["kit"].__setitem__("release_commit", 7), "run.kit.release_commit")
        self.bad(lambda d: d["run"]["kit"].__setitem__("release_commit", "HEAD"), "run.kit.release_commit")
        self.load_mut(lambda d: d["run"]["kit"].__setitem__("release_commit", "[SOURCE]"))
        self.load_mut(lambda d: d["run"].__setitem__("kit", None))

    # ---- pooling.within
    def within(self, d, value):
        d["pooling"]["within"] = value

    def test_within_both_known_forms(self):
        self.load_mut(lambda d: self.within(d, ["reference_type", "prompt_set"]))
        self.load_mut(lambda d: self.within(d, ["reference_type", "prompt_set_pair", "state"]))

    def test_within_malformed_refused(self):
        for value in (["reference_type"], ["reference_type", "prompt_set_pair"], ["state", "reference_type",
                      "prompt_set_pair"], ["reference_type", "prompt_set", "state"], "reference_type",
                      ["reference_type", "prompt_set_pair", "state", "state"], [], None, [["reference_type"]]):
            with self.subTest(value):
                self.bad(lambda d, v=value: self.within(d, v), "pooling.within")

    def test_pooling_rule_is_side_by_side(self):
        self.bad(lambda d: d["pooling"].__setitem__("rule", "meta"), "pooling.rule")

    # ---- the parity pack's diagnostic form stays readable, only in a synthetic file
    def test_pack_heads_form_only_in_synthetic_file(self):
        self.assertEqual(pv.load_profile(SYNTH)["state"], "synthetic")
        self.bad(lambda d: d.__setitem__("state", "pending"), "run.run_chain_heads", base=SYNTH)

        def broken(d):
            d["run"]["run_chain_heads"]["demo_single_log"]["replay_run"]["record_count"] = "many"
        self.bad(broken, "run.run_chain_heads.demo_single_log", base=SYNTH)


class TestRealExporter(unittest.TestCase):
    """E2 milestone M1: run_exporter drives the real ``export-profile`` on the recorded demo replay (no model calls)."""

    @classmethod
    def setUpClass(cls):
        from tests.e2_demo import demo_export
        cls.run_dir, cls.profile, cls.sha = demo_export()

    def test_sha_matches_file(self):
        self.assertRegex(self.sha, r"^[0-9a-f]{64}$")
        self.assertEqual(self.sha, sha(self.profile))

    def test_load_profile_accepts_output(self):
        doc = pv.load_profile(self.profile)
        self.assertEqual(doc["schema"], "orgsci-profile/v2")
        self.assertEqual(doc["state"], "synthetic")
        self.assertEqual(doc["population"]["n_people"], 12)
        self.assertEqual(doc["run"]["settings"]["bootstrap_resamples"], 2000)
        self.assertEqual(doc["run"]["settings"]["bootstrap_seed"], 20261017)
        self.assertEqual(sorted(doc["run"]["run_chain_heads"]), ["head", "manifest_hash", "records", "source"])

    def test_cells_match_the_m1_sample(self):
        # the sample export was made on the same demo corpus and kit commit: the same (row, name, x) triples
        out = pv.load_profile(self.profile)
        ref = pv.load_profile(SAMPLE)
        self.assertEqual(pv.cells(out), pv.cells(ref))

    def test_refusal_from_the_real_cli(self):
        # an out_dir with no run log: the exporter refuses (exit 2) and its reasons come back verbatim
        with tempfile.TemporaryDirectory(prefix="ss-tests-e2-wire-empty-") as d:
            from second_scribe.cli import DEMO_CONFIG
            with self.assertRaises(pv.ProfileRefused) as cm:
                pv.run_exporter(Path(DEMO_CONFIG), Path(d) / "nothing", Path(d) / "p.json")
            self.assertTrue(cm.exception.reasons)
            self.assertFalse(any(r.startswith("refused:") for r in cm.exception.reasons))
            self.assertFalse((Path(d) / "p.json").exists())


class TestTryItProfile(TmpCase):
    """The Try-it synthetic profile (plan V2): the exporter on the recorded demo replay, for the site build (T28)."""

    def test_reuses_a_finished_replay_run(self):
        from tests.e2_demo import demo_export
        run, ref_profile, _ = demo_export()
        out = self.tmp / "site" / "tryit-profile.json"
        h = pv.build_tryit_profile(run, None, out)
        self.assertEqual(h, sha(out))
        doc = pv.load_profile(out)
        self.assertEqual(doc["state"], "synthetic")
        self.assertEqual(pv.cells(doc), pv.cells(pv.load_profile(ref_profile)))
        self.assertEqual(out.read_bytes(), ref_profile.read_bytes())       # same run, same bytes

    def test_replays_into_an_empty_dir(self):
        run = self.tmp / "fresh-run"
        out = self.tmp / "p.json"
        h = pv.build_tryit_profile(run, None, out)
        self.assertEqual(h, sha(out))
        self.assertTrue((run / "logs" / "run.manifest.json").is_file())
        self.assertEqual(pv.load_profile(out)["state"], "synthetic")

    def test_refuses_a_dir_that_is_not_a_finished_run(self):
        run = self.tmp / "half"
        run.mkdir()
        (run / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            pv.build_tryit_profile(run, None, self.tmp / "p.json")
        self.assertFalse((self.tmp / "p.json").exists())

    def test_refuses_a_non_synthetic_config(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "configs" / "demo.json").read_text())
        cfg["corpus"]["kind"] = "real"
        p = self.write(cfg, "real.json")
        with self.assertRaises(RuntimeError):
            pv.build_tryit_profile(self.tmp / "r", p, self.tmp / "p.json")
        self.assertFalse((self.tmp / "r").exists())


def _code_keys(doc) -> set:
    """The strings-file keys a profile's codes are read through (E2-PROFILE-CODES.md: reason and note codes by the
    part before any ':', several notes joined with ';'; formula codes whole; missing codes; needs codes)."""
    keys = set()
    for r in doc["rows"]:
        for c in r["cells"]:
            if c.get("reason_code"):
                keys.add("profile.reason." + c["reason_code"].split(":")[0])
            for n in (c.get("note") or "").split(";"):
                if n and n != "pooled_only":
                    keys.add("profile.reason." + n.split(":")[0])
            if c.get("formula_code"):
                keys.add("profile.formula." + c["formula_code"])
            for m in c.get("missing") or []:
                keys.add("profile.missing." + m)
            for n in c.get("needs") or []:
                keys.add("profile.needs." + re.sub(r"[^a-z0-9_]", "_", n.lower()))
    return keys


class TestCodesHaveStrings(unittest.TestCase):
    """Every code a kit export carries has a sentence in the merged strings (en.json + the kit's profile_strings.json,
    PROFILE-SCHEMA §13), so the UI never shows a raw code."""

    def test_kit_strings_merged(self):
        from second_scribe.app.strings import load_strings
        kit = json.loads((Path(pv.KIT_ROOT) / "second_scribe" / "profile_strings.json").read_text(encoding="utf-8"))
        merged = load_strings("en")
        self.assertTrue(kit)
        for k, v in kit.items():
            self.assertEqual(merged.get(k), v, k)

    def test_sample_codes_have_strings(self):
        from second_scribe.app.strings import load_strings
        keys = _code_keys(pv.load_profile(SAMPLE))
        self.assertIn("profile.formula.yield_per_box:ai", keys)
        self.assertEqual(sorted(keys - set(load_strings("en"))), [])

    def test_exporter_output_codes_have_strings(self):
        from second_scribe.app.strings import load_strings
        from tests.e2_demo import demo_export
        _run, prof, _sha = demo_export()
        self.assertEqual(sorted(_code_keys(pv.load_profile(prof)) - set(load_strings("en"))), [])
