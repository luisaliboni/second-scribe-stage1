"""Task 3 (plan amendment V9): the publish gate for lab profiles, schema ``orgsci-profile/v2``.

Every ``test_refuses_<code>`` builds a minimal lab profile that breaks exactly one rule and asserts the code and
that the offending text is not in ``str(exception)`` (the gate names a JSON path, never the text).
"""
import ast
import copy
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from second_scribe import publish
from second_scribe.publish import PublishRefused, assert_publishable

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "tests" / "fixtures" / "profiles"   # shipped copies of the analysis profiles (SOURCES.md)
PARITY = PROFILES / "profile-synth.json"
STUDY = PROFILES / "profile-v2.json"
SCHEMA = "orgsci-profile/v2"
HEX = "6d32b6d6a8ed64d1cc0cb87cb1d58ebf8f90ebd9414aed662426641a7c7c347a"   # holds 8 consecutive digits


def lab_doc() -> dict:
    """A small lab profile that passes the gate at k = 10."""
    return {
        "schema": SCHEMA,
        "state": "pending",
        "population": {"label": "12 of 12", "n_people": 12, "n_total": 12},
        "reference": {"type": "participant-endorsed",
                      "map_size": {"ai_boxes": 120, "endorsed_boxes": 200, "ai_per_endorsed": 0.6,
                                   "source": "kit:a1_coverage",
                                   "reading3_map_size": {"nodes_per_map": 10.9, "note": "pooled_only",
                                                         "source": "kit:r12_prior_size"}}},
        "pooling": {"rule": "side_by_side", "within": ["reference_type", "prompt_set"], "meta_analysis": False},
        "rows": [
            {"row": "lead", "label_key": "profile.lead", "population": "12 of 12", "n_people": 12,
             "rests_on": ["R", "P"], "source": ["kit:a1_coverage"], "qualifiers": ["size_beside_lead"],
             "band": {"lo": 0.2, "hi": 0.3, "over": "lenient"},
             "cells": [
                 {"name": "ai_recall", "x": "primary", "n": 300, "unit": "node", "kind": "share", "value": 0.394,
                  "ci_lo": 0.35, "ci_hi": 0.44, "ci_level": 0.95, "p_lt": 0.001, "source": "kit:a1_coverage",
                  "band": {"lo": 0.3, "hi": 0.5, "over": "8_settings", "members": [{"x": "primary", "value": 0.394}],
                           "subbands": [{"name": "judge_using", "lo": 0.3, "hi": 0.4}]}},
                 {"name": "ai_nodes_per_map", "n": 12, "unit": "map", "kind": "count", "value": 12, "ci_level": None},
                 {"name": "yield_per_box", "x": "ai", "kind": "ratio", "value": 0.614, "ci_level": None,
                  "derived": True, "formula_code": "yield_per_box:ai",
                  "qualifiers": ["not_a_test"], "note": "pooled_only"},
                 {"name": "no_interview_over_ai_nodes", "value": None, "state": "not_run", "needs": ["J2"],
                  "ci_level": None},
             ]},
            {"row": 1, "label_key": "profile.row1.coverage", "population": "12 of 12", "n_people": 12,
             "cells": [{"name": "attested_recall", "x": "lenient", "n": 40, "unit": "ideal", "value": 0.5}]},
        ],
        "run": {
            "gate_chain": None,
            "declaration_log": None,
            "run_chain_heads": {"j": HEX, "m": None, "source": "logs/run.manifest.json"},
            "kit": {"release_commit": "fb26351369341381828831e0e58f727db46487f5",
                    "release_branch": "release-stage1"},
            "prompt_set": {"q_phase_sha256": HEX, "j_m_phase_sha256": HEX},
            "reading3_prompt": {"sha256": HEX, "agree": None},
            "models": [{"role": "judge", "family": "Claude", "model_id": "claude-sonnet-4-5-20250929"}],
            "settings": {"bootstrap_resamples": 2000, "bootstrap_seed": 20261017, "gate_k_people": 10,
                         "matcher_primary": {"lexical_threshold": 0.6, "judge_label": "same"}},
            "verify": {"j": "2026-10-17"},
        },
        "detection_limits": {"schema": "orgsci-detection-limits/v1",
                             "flatten": {"smallest_detected": 0.1, "lowest_tried": 0.05, "at_or_below": False},
                             "pull_planted": 0.2, "pull_detected": True, "kit_commit": "fb26351",
                             "limit": "tests the gauge's arithmetic on planted doses, not real-data matching"},
    }


def cell(doc, i=0):
    return doc["rows"][0]["cells"][i]


class GateCase(unittest.TestCase):
    def refuse(self, doc, code, secret=None, k=10, forbidden=frozenset()):
        with self.assertRaises(PublishRefused) as cm:
            assert_publishable(doc, k, SCHEMA, forbidden=forbidden)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        self.assertTrue(cm.exception.where.startswith("$"), cm.exception.where)
        if secret is not None:
            self.assertNotIn(str(secret), str(cm.exception))
            self.assertNotIn(str(secret), cm.exception.where)
        return cm.exception

    def passes(self, doc, k=10, forbidden=frozenset()):
        assert_publishable(doc, k, SCHEMA, forbidden=forbidden)


class TestBaseline(GateCase):
    def test_lab_doc_passes(self):
        self.passes(lab_doc())

    def test_schemas(self):
        self.assertEqual(publish.SCHEMAS, ("orgsci-profile/v2",))

    def test_refused_is_value_error_with_code_and_where(self):
        e = PublishRefused("not_allowed", "$.x")
        self.assertIsInstance(e, ValueError)
        self.assertEqual((e.code, e.where), ("not_allowed", "$.x"))

    def test_synthetic_state_passes(self):
        d = lab_doc()
        d["state"] = "synthetic"
        self.passes(d)

    def test_nulls_are_never_refused(self):
        d = lab_doc()
        d["run"]["kit"] = None
        cell(d)["band"] = None
        self.passes(d)


class TestRefusals(GateCase):
    def test_refuses_unknown_schema(self):
        d = lab_doc()
        d["schema"] = "orgsci-profile/v1"
        self.refuse(d, "unknown_schema", "orgsci-profile/v1")
        with self.assertRaises(PublishRefused) as cm:
            assert_publishable(lab_doc(), 10, "numbers-public/1")
        self.assertEqual(cm.exception.code, "unknown_schema")
        self.assertNotIn("numbers-public", str(cm.exception))
        self.refuse(["not", "a", "dict"], "unknown_schema")

    def test_refuses_not_allowed(self):
        d = lab_doc()
        d["extra_secret_block"] = {"a": 1}
        self.refuse(d, "not_allowed", "extra_secret_block")
        d = lab_doc()
        cell(d)["reasonx"] = "free text"
        self.refuse(d, "not_allowed", "reasonx")
        d = lab_doc()
        d["run"]["pack_scripts"] = {"001": {"status": "ran"}}
        self.refuse(d, "not_allowed", "pack_scripts")

    def test_refuses_not_allowed_nested_in_a_leaf(self):
        d = lab_doc()
        cell(d)["value"] = {"hidden": 1}
        self.refuse(d, "not_allowed", "hidden")
        d = lab_doc()
        cell(d)["qualifiers"] = [{"hidden": 1}]
        self.refuse(d, "not_allowed", "hidden")
        d = lab_doc()
        d["rows"] = {"0": d["rows"][0]}
        self.refuse(d, "not_allowed")
        d = lab_doc()
        cell(d)["value"] = [0.1, 0.2, 0.3]           # no per-map vector in a value
        self.refuse(d, "not_allowed")
        d = lab_doc()
        cell(d)["qualifiers"] = ["ceiling", 3]       # codes are strings
        self.refuse(d, "not_allowed")
        d = lab_doc()
        d["rows"][0]["source"] = "kit:a1_coverage"   # the row's source is an array (PROFILE-SCHEMA §2)
        self.refuse(d, "not_allowed")
        d = lab_doc()
        d["detection_limits"]["dose0_bands"] = {"flatten": {"lo": 0.0, "hi": 0.1}, "pull": [0.0, 0.1]}
        self.passes(d)
        d["detection_limits"]["dose0_bands"]["pull"] = [{"x": 1}]
        self.refuse(d, "not_allowed")

    def test_state_key_allowed_only_in_map_size(self):
        d = lab_doc()
        d["reference"]["map_size"]["endorsed_boxes_state"] = "not_run (input absent)"
        self.passes(d)
        d = lab_doc()
        d["rows"][0]["endorsed_boxes_state"] = "not_run"
        self.refuse(d, "not_allowed", "endorsed_boxes_state")

    def test_refuses_state_not_lab(self):
        for state in ("reported", "interpreted", None):
            d = lab_doc()
            d["state"] = state
            self.refuse(d, "state_not_lab", state)

    def test_refuses_lab_lock_field(self):
        for where in ("gate_chain", "declaration_log"):
            d = lab_doc()
            d["run"][where] = {"head": HEX, "entries": 20}
            self.refuse(d, "lab_lock_field", HEX)
        d = lab_doc()
        d["run"]["reading3_prompt"]["agree"] = False
        self.refuse(d, "lab_lock_field")

    def test_refuses_per_map_key(self):
        for key in ("per_map", "by_map", "maps", "items", "evidence", "#3", "#3#12"):
            d = lab_doc()
            cell(d)[key] = [1, 2]
            e = self.refuse(d, "per_map_key")
            self.assertNotIn(key, e.where)
        d = lab_doc()
        d["run"]["settings"]["per_map"] = 1        # inside run too
        self.assertNotIn("per_map", self.refuse(d, "per_map_key").where)

    def test_refuses_identity_key(self):
        for key in ("interviewer", "participant_ids", "role", "record_hash", "entry", "map_id", "pair_id",
                    "Interviewer_A"):
            d = lab_doc()
            d["rows"][0][key] = "x"
            self.refuse(d, "identity_key", key)

    def test_identity_words_allowed_inside_run(self):
        d = lab_doc()
        self.assertIn("role", d["run"]["models"][0])
        self.passes(d)

    def test_refuses_hex_outside_run(self):
        d = lab_doc()
        cell(d)["source"] = HEX
        self.refuse(d, "hex_outside_run", HEX)
        d = lab_doc()
        d["rows"][0]["source"] = ["kit:a1", HEX.upper()]
        self.refuse(d, "hex_outside_run", HEX.upper())

    def test_refuses_record_hash(self):
        for mutate in (lambda d: d["run"]["settings"].__setitem__("interval_method", HEX),
                       lambda d: d["run"]["kit"].__setitem__("source", HEX),               # source allows 200
                       lambda d: d["run"]["kit"].__setitem__("source", "chain " + HEX + " ok"),
                       lambda d: d["run"]["reading3_prompt"].__setitem__("sha256", "x" + HEX)):
            d = lab_doc()
            mutate(d)
            self.refuse(d, "record_hash", HEX)

    def test_refuses_hex_embedded_outside_run(self):
        d = lab_doc()
        cell(d)["source"] = "kit:a1 " + HEX
        self.refuse(d, "hex_outside_run", HEX)

    def test_refuses_cell_below_k(self):
        d = lab_doc()
        d["population"]["n_people"] = 9
        self.refuse(d, "cell_below_k")
        d = lab_doc()
        d["rows"][1]["n_people"] = 3
        self.refuse(d, "cell_below_k", "3")
        d = lab_doc()
        cell(d, 1)["n"] = 7                       # unit "map": one map per person
        self.refuse(d, "cell_below_k", "7")
        d = lab_doc()
        cell(d, 1)["n"], cell(d, 1)["unit"] = 7, " Participant"
        self.refuse(d, "cell_below_k", "7")
        d = lab_doc()
        d["rows"][1]["cells"][0]["n"] = 7         # unit "ideal" counts elements, not people
        self.passes(d)

    def test_refuses_no_people_count(self):
        for bad in (None, "12", 12.0, True):
            d = lab_doc()
            d["rows"][1]["n_people"] = bad
            self.refuse(d, "no_people_count")
        d = lab_doc()
        del d["rows"][1]["n_people"]
        self.refuse(d, "no_people_count")
        d = lab_doc()
        del d["population"]["n_people"]
        self.refuse(d, "no_people_count")
        d = lab_doc()
        cell(d, 1)["n"] = "12"
        self.refuse(d, "no_people_count")

    def test_refuses_long_string(self):
        secret = "a sentence that is far too long to be a code here"
        d = lab_doc()
        cell(d)["note"] = secret
        self.refuse(d, "long_string", secret)

    def test_long_string_exemptions(self):
        d = lab_doc()
        d["detection_limits"]["limit"] = "l" * 400              # label_key is no longer exempt (audit K2)
        cell(d)["source"] = "kit:" + "s" * 196
        d["rows"][0]["source"] = ["kit:" + "s" * 196]
        d["reference"]["map_size"]["endorsed_boxes_state"] = "t" * 200
        d["run"]["models"][0]["model_id"] = "m" * 80
        self.passes(d)
        for mutate in (lambda d: d["rows"][0].__setitem__("label_key", "profile." + "k" * 33),
                       lambda d: cell(d, 2).__setitem__("formula_code", "z" * 41),
                       lambda d: d["detection_limits"].__setitem__("limit", "l" * 401),
                       lambda d: cell(d).__setitem__("source", "kit:" + "s" * 197),
                       lambda d: d["reference"]["map_size"].__setitem__("endorsed_boxes_state", "t" * 201),
                       lambda d: d["run"]["models"][0].__setitem__("model_id", "m" * 81),
                       lambda d: cell(d).__setitem__("note", "l" * 41)):
            d = lab_doc()
            mutate(d)
            self.refuse(d, "long_string")

    def test_limit_exempt_only_under_detection_limits(self):
        d = lab_doc()
        cell(d)["note"] = "l" * 100
        self.refuse(d, "long_string")

    def test_refuses_label_or_quote(self):
        d = lab_doc()
        cell(d)["note"] = "went to  the CLINIC"
        self.refuse(d, "label_or_quote", "CLINIC", forbidden=frozenset({"Went to the clinic"}))
        d = lab_doc()
        cell(d)["note"] = "pooled_only"                     # a note is a code (audit K1/K2), not free text
        self.passes(d, forbidden=frozenset({"Went to the clinic"}))
        self.passes(lab_doc(), forbidden=frozenset({"Source", "value"}))   # schema keys are not labels
        d = lab_doc()
        d["Went to the clinic"] = 1
        self.refuse(d, "label_or_quote", "clinic", forbidden=frozenset({"Went to the clinic"}))

    def test_refuses_endpoint_host(self):
        # IPv4 literals are built at run time, so the kit's leak scan of the public tree stays clean
        for host in ("api.example.com", "My-Server.lab.ca:8080", "http://x", "see https://a.b/c", ".".join(("10", "0", "0", "1")),
                     "at " + ".".join(("192", "168", "1", "20")) + ":11434", "::1", "fe80::1", "localhost:11434"):
            d = lab_doc()
            cell(d)["note"] = host
            self.refuse(d, "endpoint_host", host)

    def test_model_id_exempt_from_host_and_date(self):
        d = lab_doc()
        d["run"]["models"][0]["model_id"] = "claude-sonnet-4-5-20250929"
        self.passes(d)
        d["run"]["models"][0]["model_id"] = "qwen3.5:27b"
        self.passes(d)

    def test_refuses_dated_run_id(self):
        for key, value in (("run_id", "run-2026-10-18"), ("judge_run_id", "j20261018T1200"),
                           ("run_ids", ["ok-run", "r-20261018"])):
            d = lab_doc()
            d["run"][key] = value
            self.refuse(d, "dated_run_id", "2026")
        d = lab_doc()
        d["run"]["run_id"] = "claude-sonnet-4-5-20250929"    # a model id is exempt only under run.models
        self.refuse(d, "dated_run_id", "20250929")

    def test_hash_with_digit_run_passes_under_hash_keys(self):
        d = lab_doc()
        self.assertEqual(d["run"]["reading3_prompt"]["sha256"], HEX)
        self.passes(d)


class TestDiffers(unittest.TestCase):
    def test_differs_by_fewer_than_k(self):
        a, b = {"population": {"n_people": 12}}, {"population": {"n_people": 15}}
        self.assertTrue(publish.differs_by_fewer_than_k(a, b, 10))
        self.assertFalse(publish.differs_by_fewer_than_k(a, a, 10))
        self.assertFalse(publish.differs_by_fewer_than_k(a, {"population": {"n_people": 22}}, 10))
        self.assertTrue(publish.differs_by_fewer_than_k(a, {"population": {"n_people": 21}}, 10))
        self.assertTrue(publish.differs_by_fewer_than_k(a, {"population": {}}, 10))   # fail closed: warn


class TestFiles(GateCase):
    def test_schema_13_cell_fields_allowed(self):
        doc = lab_doc()
        cell = doc["rows"][0]["cells"][0]
        cell.update({"p_holm_lt": 0.001, "reason_code": "not_computable", "missing": ["band", "ci"]})   # codes resolve (K2)
        self.passes(doc, k=10)

    def test_free_formula_refused_in_lab_export(self):
        doc = lab_doc()
        doc["rows"][0]["cells"][2]["formula"] = "x"
        self.refuse(doc, "not_allowed")

    def test_free_reason_refused_in_lab_export(self):
        doc = lab_doc()
        doc["rows"][0]["cells"][0]["reason"] = "x"
        self.refuse(doc, "not_allowed")

    # Coordinator ruling (18 Oct): parity-synth is the analysis pack's diagnostic file, not a lab export. Its
    # extra blocks may only trip key-level codes; with them stripped, the content rules must pass it. Since audit K2
    # the gate also runs §13 (lab_string_problems): a pack cell that said why in a free `reason` (or `formula`) and
    # loses it in the strip has no reason_code / formula_code, so exactly those cells trip `lab_string`. The
    # exporter's own output is checked once E2 milestone M1 lands (test_exporter_output_passes, added then).
    PARITY_DIAGNOSTICS = {
        "top": ("not_run_cells", "not_run_statements", "lookups_not_run", "partial_checks", "substitutions"),
        "run": ("kind", "pack_scripts", "not_run_ids", "synthetic"),
        "cell": ("reason", "partial_not_run", "formula"),
        "band": ("reason", "state"),
    }

    def parity_stripped(self):
        doc = json.loads(PARITY.read_text(encoding="utf-8"))
        d = self.PARITY_DIAGNOSTICS
        for key in d["top"]:
            doc.pop(key, None)
        for key in d["run"]:
            doc["run"].pop(key, None)
        doc["run"].get("kit", {}).pop("pack_kit_tree", None)
        doc["run"].get("run_chain_heads", {}).pop("demo_single_log", None)
        for m in doc["run"].get("models", []) if isinstance(doc["run"].get("models"), list) else []:
            m.pop("label", None)
        for row in doc["rows"]:
            band = row.get("band")
            if isinstance(band, dict) and band.get("state") == "not_run":
                row.pop("band")
            elif isinstance(band, dict):
                for key in d["band"]:
                    band.pop(key, None)
            for cell in row["cells"]:
                for key in d["cell"]:
                    cell.pop(key, None)
        return doc

    def parity_stripped_sentence_cells(self) -> set:
        """Paths of the cells whose free reason/formula the strip removes (the pack's §13 sentence fields)."""
        doc = json.loads(PARITY.read_text(encoding="utf-8"))
        return {f"$.rows[{i}].cells[{j}]" for i, row in enumerate(doc["rows"]) for j, c in enumerate(row["cells"])
                if "reason" in c or "formula" in c}

    def test_parity_synth_raw_trips_only_key_codes(self):
        doc = json.loads(PARITY.read_text(encoding="utf-8"))
        codes = {code for code, _ in publish.violations(doc, 10, "orgsci-profile/v2")}
        self.assertTrue(codes)
        self.assertLessEqual(codes, {"not_allowed", "record_hash", "lab_string"})

    def test_parity_synth_stripped_passes(self):
        found = publish.violations(self.parity_stripped(), 10, "orgsci-profile/v2")
        self.assertEqual({code for code, _ in found} - {"lab_string"}, set(), found)
        self.assertLessEqual({w for _, w in found}, self.parity_stripped_sentence_cells())

    def test_study_profile_refused_as_lab_export(self):
        doc = json.loads(STUDY.read_text(encoding="utf-8"))
        self.assertEqual(doc["state"], "reported")
        self.refuse(doc, "state_not_lab")

    def test_write_public_roundtrip(self):
        doc = lab_doc()
        with tempfile.TemporaryDirectory() as tmp:
            jp, cp = Path(tmp) / "out" / "profile.json", Path(tmp) / "out" / "profile.csv"
            sj, sc = publish.write_public(doc, jp, cp)
            self.assertEqual(sj, hashlib.sha256(jp.read_bytes()).hexdigest())
            self.assertEqual(sc, hashlib.sha256(cp.read_bytes()).hexdigest())
            self.assertEqual(json.loads(jp.read_text(encoding="utf-8")), doc)
            self.assertEqual(jp.read_text(encoding="utf-8"),
                             json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n")
            rows = list(csv.reader(io.StringIO(cp.read_text(encoding="utf-8"))))
            self.assertEqual(rows[0], ["row", "name", "x", "value", "ci_lo", "ci_hi", "ci_level", "n", "unit",
                                       "state"])
            cells = [(r["row"], c) for r in doc["rows"] for c in r["cells"]]
            self.assertEqual(len(rows) - 1, len(cells))
            for line, (row, c) in zip(rows[1:], cells):
                self.assertEqual(line[0], str(row))
                self.assertEqual(line[1], c["name"])
                for col, key in zip(line[2:], ("x", "value", "ci_lo", "ci_hi", "ci_level", "n", "unit", "state")):
                    v = c.get(key)
                    self.assertEqual(col, "" if v is None else str(v), (c["name"], key))
            self.assertEqual(rows[4][3], "")            # the not_run cell: empty, never 0
            self.assertEqual(rows[4][9], "not_run")
            self.assertEqual(publish.write_public(doc, jp, cp), (sj, sc))   # same bytes again
            self.assertEqual(sorted(p.name for p in jp.parent.iterdir()), ["profile.csv", "profile.json"])

    def test_cli_exit_codes(self):
        def run(*args):
            return subprocess.run([sys.executable, "-m", "second_scribe.publish", *args], cwd=ROOT,
                                  capture_output=True, text=True, timeout=60)
        with tempfile.TemporaryDirectory() as tmp:
            good, bad, junk = Path(tmp) / "good.json", Path(tmp) / "bad.json", Path(tmp) / "junk.json"
            good.write_text(json.dumps(lab_doc()), encoding="utf-8")
            d = lab_doc()
            cell(d)["note"] = "api.secret-host.org"
            bad.write_text(json.dumps(d), encoding="utf-8")
            junk.write_text("{not json", encoding="utf-8")
            r = run("check", "--profile", str(good))
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run("check", "--profile", str(good), "--k", "13")
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.splitlines()[0].split(), ["cell_below_k", "$.population.n_people"])
            r = run("check", "--profile", str(bad))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.splitlines()[0].split()[0], "endpoint_host")
            self.assertNotIn("secret-host", r.stderr + r.stdout)
            r = run("check", "--profile", str(junk))
            self.assertEqual(r.returncode, 2)
            self.assertNotIn("not json", r.stderr)
            r = run("check", "--profile", str(Path(tmp) / "missing.json"))
            self.assertEqual(r.returncode, 2)
            r = run("check", "--profile", str(STUDY))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stderr.split()[:2], ["state_not_lab", "$.state"])


COMMIT = "6b8c3986d46cb6190ff2e9a7fa0888f8dc339911"
SAMPLE = ROOT / "tests" / "fixtures" / "profiles" / "e2-m1-sample-demo.json"
MATCHERS = {name: {"lexical_threshold": 0.6, "judge_label": "same"} for name in
            ("primary", "lenient", "lexical_only_04", "lexical_only_05", "lexical_only_06", "lexical_only_07",
             "judge_only_same", "judge_only_same_or_close")}


def kit_doc() -> dict:
    """lab_doc with the kit exporter's run block and pooling (PROFILE-SCHEMA §14, E2 M1 6b8c398)."""
    d = lab_doc()
    d["pooling"]["within"] = ["reference_type", "prompt_set_pair", "state"]
    run = d["run"]
    run["run_chain_heads"] = {"head": HEX, "records": 2190, "manifest_hash": HEX,
                              "source": "logs/run.manifest.json, checked by audit.verify.verify_run"}
    run["kit"] = {"release_commit": COMMIT, "run_commits": [COMMIT, COMMIT],
                  "source": "release_commit = HEAD of the kit now"}
    run["settings"]["match_settings"] = copy.deepcopy(MATCHERS)
    run["settings"]["matcher_row4_lenient"] = {"lexical_threshold": 0.6, "judge_label": "same_or_close"}
    return d


class TestKitRunBlock(GateCase):
    """The three run-block / pooling shapes of the kit exporter pass the gate; a malformed variant of each refuses."""

    def test_kit_doc_passes(self):
        self.passes(kit_doc())

    def test_v2_shapes_still_pass(self):
        d = kit_doc()
        d["run"]["run_chain_heads"] = {"q": HEX, "j": "[SOURCE]", "m": None, "source": "x"}
        d["run"]["kit"]["run_commits"] = {"q": COMMIT, "j": COMMIT, "m": COMMIT}
        d["pooling"]["within"] = ["reference_type", "prompt_set"]
        self.passes(d)

    # ---- run.remote_endpoints (§14.8): [{role, provider, declaration_sha256}], the hash allowed at that key only
    def test_remote_endpoints_pass(self):
        d = kit_doc()
        d["run"]["remote_endpoints"] = [{"role": "judge", "provider": "anthropic", "declaration_sha256": HEX},
                                        {"role": "judge2", "provider": "openai_compat", "declaration_sha256": HEX}]
        self.passes(d)

    def test_remote_endpoints_refuse_a_host_and_a_hash_elsewhere(self):
        d = kit_doc()
        d["run"]["remote_endpoints"] = [{"role": "judge", "provider": "anthropic", "declaration_sha256": HEX,
                                         "host": "api.example.com"}]
        self.refuse(d, "endpoint_host")                    # the address check fires before the allow-list
        d["run"]["remote_endpoints"][0]["host"] = "cloud"
        self.refuse(d, "not_allowed")                      # and a host key is not in the entry's schema
        d = kit_doc()
        d["run"]["remote_endpoints"] = [{"role": "judge", "provider": HEX, "declaration_sha256": HEX}]
        e = self.refuse(d, "record_hash")
        self.assertEqual(e.where, "$.run.remote_endpoints[0].provider")
        d = kit_doc()
        d["run"]["kit"]["source"] = HEX
        self.refuse(d, "record_hash")
    def test_kit_code_sha256_passes_as_hash_or_null(self):          # village E6 (vil-pool ee452b2)
        for v in (HEX, None):
            d = kit_doc()
            d["run"]["kit"]["code_sha256"] = v
            self.passes(d)

    # ---- run_chain_heads {head, records, manifest_hash, source}
    def test_refuses_mixed_run_chain_heads(self):
        d = kit_doc()
        d["run"]["run_chain_heads"]["j"] = HEX
        e = self.refuse(d, "not_allowed")
        self.assertEqual(e.where, "$.run.run_chain_heads")

    def test_refuses_unknown_key_in_kit_heads(self):
        d = kit_doc()
        d["run"]["run_chain_heads"]["log_path"] = "/private/run/logs"
        e = self.refuse(d, "not_allowed", secret="private")
        self.assertEqual(e.where, "$.run.run_chain_heads")

    def test_refuses_hash_in_records(self):
        d = kit_doc()
        d["run"]["run_chain_heads"]["records"] = HEX
        e = self.refuse(d, "record_hash", secret=HEX)
        self.assertEqual(e.where, "$.run.run_chain_heads.records")

    def test_refuses_object_as_head(self):
        d = kit_doc()
        d["run"]["run_chain_heads"]["head"] = {"records": 3}
        e = self.refuse(d, "not_allowed")
        self.assertEqual(e.where, "$.run.run_chain_heads.head")
        d["run"]["run_chain_heads"]["head"] = {"j": HEX}         # a hash nested under head: refused by pass 1
        self.refuse(d, "record_hash", secret=HEX)

    # ---- kit.run_commits: a list
    def test_refuses_run_commits_list_of_objects(self):
        d = kit_doc()
        d["run"]["kit"]["run_commits"] = [{"q": COMMIT}]
        e = self.refuse(d, "not_allowed")
        self.assertEqual(e.where, "$.run.kit.run_commits[0]")

    def test_refuses_hash_in_run_commits(self):
        d = kit_doc()
        d["run"]["kit"]["run_commits"] = [COMMIT, HEX]
        e = self.refuse(d, "record_hash", secret=HEX)
        self.assertEqual(e.where, "$.run.kit.run_commits[1]")

    def test_refuses_unknown_phase_in_run_commits(self):
        d = kit_doc()
        d["run"]["kit"]["run_commits"] = {"q": COMMIT, "participant_7": COMMIT}
        e = self.refuse(d, "not_allowed", secret="participant_7")
        self.assertEqual(e.where, "$.run.kit.run_commits")

    # ---- pooling.within: a list of codes
    def test_refuses_within_as_object(self):
        d = kit_doc()
        d["pooling"]["within"] = {"reference_type": "A"}
        e = self.refuse(d, "not_allowed")
        self.assertEqual(e.where, "$.pooling.within")

    def test_refuses_within_nested_list(self):
        d = kit_doc()
        d["pooling"]["within"] = ["reference_type", ["prompt_set_pair", "state"]]
        e = self.refuse(d, "not_allowed")
        self.assertEqual(e.where, "$.pooling.within[1]")

    # ---- settings the kit adds (match_settings, matcher_row4_lenient)
    def test_refuses_unknown_match_setting(self):
        d = kit_doc()
        d["run"]["settings"]["match_settings"]["lexical_only_03"] = {"lexical_threshold": 0.3, "judge_label": "none"}
        self.refuse(d, "not_allowed")

    def test_refuses_extra_key_in_row4_matcher(self):
        d = kit_doc()
        d["run"]["settings"]["matcher_row4_lenient"]["pairs"] = [1, 2]
        self.refuse(d, "not_allowed")

    # ---- section 13 still holds on a kit-shaped file
    def test_section_13_kept_on_kit_doc(self):
        for key, value in (("reason", "x"), ("formula", "x"), ("reason_code", "x" * 41)):
            with self.subTest(key):
                d = kit_doc()
                cell(d, 0)[key] = value
                self.refuse(d, "long_string" if key == "reason_code" else "not_allowed")

    def test_sample_fixture_passes(self):
        self.passes(json.loads(SAMPLE.read_text(encoding="utf-8")))


class TestExporterOutput(GateCase):
    """V6: the real exporter's output on the recorded demo replay (E2 M1) passes the gate as a lab export. The exporter
    itself writes run.gate_chain, run.declaration_log and run.reading3_prompt.agree as null (PROFILE-SCHEMA §12), so
    the test asserts the file as written; the app does not patch them."""

    @classmethod
    def setUpClass(cls):
        from tests.e2_demo import demo_export
        _run, cls.path, cls.sha = demo_export()

    def doc(self):
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), self.sha)
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_exporter_writes_the_lab_lock_nulls(self):
        run = self.doc()["run"]
        for key in ("gate_chain", "declaration_log"):
            self.assertIn(key, run)
            self.assertIsNone(run[key], key)
        self.assertIn("agree", run["reading3_prompt"])
        self.assertIsNone(run["reading3_prompt"]["agree"])

    def test_exporter_output_passes(self):
        doc = self.doc()
        self.assertEqual(doc["state"], "synthetic")
        self.passes(doc, k=10)
        self.assertEqual(publish.violations(doc, 10, SCHEMA), [])

    def test_exporter_output_passes_as_pending(self):
        doc = self.doc()
        doc["state"] = "pending"                     # the app's own-data export (V6)
        self.passes(doc, k=10)

    def test_exporter_output_refused_when_lock_field_set(self):
        doc = self.doc()
        doc["run"]["reading3_prompt"]["agree"] = False
        self.refuse(doc, "lab_lock_field")

    def test_exporter_output_below_k(self):
        self.refuse(self.doc(), "cell_below_k", k=13)       # the demo has 12 people

    def test_cli_check_on_exporter_output(self):
        r = subprocess.run([sys.executable, "-m", "second_scribe.publish", "check", "--profile", str(self.path)],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestStandalone(unittest.TestCase):
    def test_publish_imports_only_stdlib_and_report(self):
        tree = ast.parse((ROOT / "second_scribe" / "publish.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertIn(a.name.split(".")[0], sys.stdlib_module_names, a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:     # report; profile_base for the §13 check (audit K2: one rule, not two)
                    self.assertIn((node.level, node.module), ((1, "report"), (1, "profile_base")))
                elif node.module != "__future__":
                    self.assertTrue(node.module == "second_scribe.report"
                                    or node.module.split(".")[0] in sys.stdlib_module_names, node.module)

    def test_gate_does_not_mutate_input(self):
        d = lab_doc()
        before = copy.deepcopy(d)
        assert_publishable(d, 10, SCHEMA)
        self.assertEqual(d, before)


if __name__ == "__main__":
    unittest.main()
