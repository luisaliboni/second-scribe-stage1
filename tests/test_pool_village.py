"""Village wave 1, cluster W2: pool / export-profile / calibrate refusals and checks (TRIAGE A1, A8, A9, A12, E1-E6).

Zero model calls and zero network calls. Pool tests use the profile dicts of ``test_pool.profile``.
"""
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import cli
from second_scribe import pool as poolmod
from tests.test_pool import profile


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def write_labs(t: Path, **profiles) -> list[str]:
    paths = []
    for name, p in profiles.items():
        (t / f"{name}.json").write_text(json.dumps(p), encoding="utf-8")
        paths.append(str(t / f"{name}.json"))
    return paths


class PoolOutWriteTests(unittest.TestCase):
    """A1: both pool writes are inside the refusal path; an unwritable --out is one `refused:` line, exit 2."""

    def test_out_under_a_file_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile(), labB=profile())
            (t / "afile").write_text("x", encoding="utf-8")
            rc, _, err = run_cli(["pool", "--profiles", *paths, "--out", str(t / "afile" / "p.json")])
            self.assertEqual(rc, 2)
            self.assertTrue(err.startswith("refused: cannot write "), err)
            self.assertIn(str(t / "afile" / "p.json"), err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("[Errno", err)
            self.assertEqual(len(err.strip().splitlines()), 1)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "needs a POSIX permission bit that binds")
    def test_unwritable_out_folder_is_refused_and_nothing_written(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile(), labB=profile())
            ro = t / "ro"
            ro.mkdir()
            ro.chmod(0o555)
            try:
                rc, _, err = run_cli(["pool", "--profiles", *paths, "--out", str(ro / "p.json")])
            finally:
                ro.chmod(0o755)
            self.assertEqual(rc, 2)
            self.assertIn("refused: cannot write", err)
            self.assertIn("Permission denied", err)
            self.assertEqual(list(ro.iterdir()), [])

    def test_md_write_failure_refuses_and_leaves_no_json(self):
        """Fix round 1, N4: the .md target cannot be written (it is a folder): refused, and the .json just written is
        removed. Accepted leftover: an older pool.md from a previous run would stay beside no pool.json."""
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile(), labB=profile(recall=0.6))
            (t / "p.md").mkdir()
            rc, _, err = run_cli(["pool", "--profiles", *paths, "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertTrue(err.startswith(f"refused: cannot write {t / 'p.md'}: "), err)
            self.assertFalse((t / "p.json").exists())

    def test_missing_out_folder_is_created(self):
        """Ruling: a missing --out folder that can be created is created (as before); only one that cannot is refused."""
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile(), labB=profile())
            rc, _, _ = run_cli(["pool", "--profiles", *paths, "--out", str(t / "new" / "p.json")])
            self.assertEqual(rc, 0)
            self.assertTrue((t / "new" / "p.md").exists())

    def test_md_failure_preserves_previous_json(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-pool-review-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile())
            previous = b'{"previous": true}\n'
            (t / "p.json").write_bytes(previous)
            (t / "p.md").mkdir()
            rc, _, _ = run_cli(["pool", "--profiles", *paths, "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual((t / "p.json").read_bytes(), previous)

    def test_failed_overwrite_restores_both_existing_outputs(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-pool-review-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile())
            target = t / "p.json"
            markdown = t / "p.md"
            target.write_bytes(b'{"previous":true}\r\n')
            markdown.write_text("Previous report", encoding="utf-8")
            original_write = cli.report.write_text_atomic
            def fail_markdown(path, text):
                if path == markdown:
                    raise OSError("synthetic write failure")
                return original_write(path, text)
            with mock.patch.object(cli.report, "write_text_atomic", side_effect=fail_markdown):
                rc, _, _ = run_cli(["pool", "--profiles", *paths, "--out", str(target)])
            self.assertEqual(rc, 2)
            self.assertEqual(target.read_bytes(), b'{"previous":true}\r\n')
            self.assertEqual(markdown.read_text(), "Previous report")
            self.assertFalse(list(t.glob("*.bak")))


class PoolSchemaTests(unittest.TestCase):
    """A8: pool reads only orgsci-profile/v2 and v2.<minor>."""

    def _pool_with(self, schema):
        b = profile()
        if schema is None:
            del b["schema"]
        else:
            b["schema"] = schema
        return poolmod.pool([profile(), b], labels=["labA", "labB"])

    def test_v2_and_minor_are_read(self):
        for schema in ("orgsci-profile/v2", "orgsci-profile/v2.1", "orgsci-profile/v2.13"):
            self.assertEqual(self._pool_with(schema)["groups"][0]["labs"], ["labA", "labB"], schema)

    def test_other_schemas_are_refused(self):
        for schema in ("orgsci-profile/v1", "orgsci-profile/v3", "banana", "orgsci-profile/v2.x", "orgsci-profile/v21"):
            with self.assertRaises(ValueError) as cm:
                self._pool_with(schema)
            self.assertEqual(str(cm.exception),
                             f"labB: schema '{schema}' is not orgsci-profile/v2; this kit reads v2 only")

    def test_missing_schema_says_has_no_schema(self):
        with self.assertRaises(ValueError) as cm:
            self._pool_with(None)
        self.assertEqual(str(cm.exception), "labB has no schema; this kit reads orgsci-profile/v2 only")

    def test_free_text_schema_is_not_echoed(self):
        for schema in ("Maria Silva v2", 3, ["orgsci-profile/v2"]):
            with self.assertRaises(ValueError) as cm:
                self._pool_with(schema)
            self.assertIn("<not a schema id>", str(cm.exception))
            self.assertNotIn("Maria", str(cm.exception))

    def test_cli_refuses_v3_exit_2_nothing_written(self):
        """The TRIAGE repro: labA plus the same file with schema v3 -> exit 2, not one table."""
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            v3 = profile()
            v3["schema"] = "orgsci-profile/v3"
            paths = write_labs(t, labA=profile(), labV3=v3)
            rc, _, err = run_cli(["pool", "--profiles", *paths, "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), "refused: labV3: schema 'orgsci-profile/v3' is not orgsci-profile/v2; "
                                          "this kit reads v2 only")
            self.assertFalse((t / "p.json").exists())


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "profiles"


class PoolStudyProfileTests(unittest.TestCase):
    """A9 (Ruling R1, option 2): the study's published profile is refused early with a plain reason; problem lists in
    refusals show at most 3 problems and a count."""

    STUDY_TAIL = ("is the study's published profile (the app's reading copy); it cannot be pooled. "
                  "Pool the file written by export-profile from a finished run instead.")

    def test_published_profile_fixture_is_recognised(self):
        study = json.loads((FIXTURES / "profile-v2.json").read_text(encoding="utf-8"))   # copy of the study's file
        self.assertTrue(poolmod.is_study_profile(study))
        kit = json.loads((FIXTURES / "e2-m1-sample-demo.json").read_text(encoding="utf-8"))  # the kit exporter's output
        self.assertFalse(poolmod.is_study_profile(kit))
        self.assertFalse(poolmod.is_study_profile(profile()))
        self.assertFalse(poolmod.is_study_profile(profile(state="reported")))

    def test_study_built_synthetic_profile_is_refused_by_its_sources(self):
        """Fix round 1, S1: the study-built profile-synth.json has a null gate_chain and "cleared/NNN" sources."""
        synth = json.loads((FIXTURES / "profile-synth.json").read_text(encoding="utf-8"))
        self.assertIsNone(synth["run"].get("gate_chain"))
        self.assertTrue(poolmod.is_study_profile(synth))
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile())
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(FIXTURES / "profile-synth.json"),
                                  "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: {FIXTURES / 'profile-synth.json'} {self.STUDY_TAIL}")
            self.assertNotIn("privacy scan", err)
            self.assertFalse((t / "p.json").exists())
        v2 = json.loads((FIXTURES / "profile-v2.json").read_text(encoding="utf-8"))
        v2["run"]["gate_chain"] = None                    # source detection alone, on the real study file
        self.assertTrue(poolmod.is_study_profile(v2))

    def test_gate_chain_sources_and_block_are_recognised(self):
        for source in ("gate chain entry 013", "gate chain entries 001+018", "gate chain entries 003+013+020",
                       "cleared/013", "cleared/003+cleared/013+cleared/020"):
            p = profile()
            p["rows"][0]["cells"][0]["source"] = source
            self.assertTrue(poolmod.is_study_profile(p), source)
        p = profile()
        p["run"]["gate_chain"] = {"entries": 20}
        self.assertTrue(poolmod.is_study_profile(p))
        p["run"]["gate_chain"] = None                    # the kit exporter's value
        self.assertFalse(poolmod.is_study_profile(p))

    def test_cli_refuses_the_study_file_early_by_its_path(self):
        """The TRIAGE repro: labA plus the study's profile -> one plain refusal, not 304 privacy problems."""
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile())
            study = t / "profile.json"
            study.write_bytes((FIXTURES / "profile-v2.json").read_bytes())
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(study), "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: {study} {self.STUDY_TAIL}")
            self.assertNotIn("privacy scan", err)
            self.assertFalse((t / "p.json").exists())

    def test_pool_api_names_the_label(self):
        study = json.loads((FIXTURES / "profile-v2.json").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError) as cm:
            poolmod.pool([profile(), study], labels=["labA", "study"])
        self.assertEqual(str(cm.exception), f"study {self.STUDY_TAIL}")

    def test_privacy_problem_list_is_capped_at_3(self):
        bad = profile()
        for c in bad["rows"][0]["cells"] + bad["rows"][1]["cells"]:
            c["unit"] = "Not A Code"                   # one closed-form problem per cell (3 cells) ...
        bad["rows"][0]["cells"].append({"name": "extra", "unit": "Bad Unit", "value": 1})
        bad["rows"][1]["cells"].append({"name": "extra", "unit": "Bad Unit", "value": 1})
        with self.assertRaises(ValueError) as cm:
            poolmod.pool([profile(), bad], labels=["labA", "labB"])
        msg = str(cm.exception)
        n = int(msg.split("(")[1].split(" problem")[0])
        self.assertGreater(n, 3)
        shown = msg.split("problem(s): ")[1].split("; and ")[0]
        self.assertEqual(len(shown.split("; ")), 3)
        self.assertIn(f"; and {n - 3} more", msg)

    def test_shown_problems_helper(self):
        self.assertEqual(poolmod.shown_problems(["a", "b"]), "a; b")
        self.assertEqual(poolmod.shown_problems(["a", "b", "c"]), "a; b; c")
        self.assertEqual(poolmod.shown_problems(["a", "b", "c", "d", "e"]), "a; b; c; and 2 more")


class RefusalLinesTests(unittest.TestCase):
    """A9: export-profile and calibrate print at most 3 reasons, then one "and N more" line."""

    def test_refusal_lines(self):
        from second_scribe.profile_export import refusal_lines
        self.assertEqual(refusal_lines(["a"]), ["refused: a"])
        self.assertEqual(refusal_lines(["a", "b", "c", "d", "e"]),
                         ["refused: a", "refused: b", "refused: c", "refused: and 2 more problem(s)"])


REPO = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO / "configs" / "demo.json"


class ExportNoFinishedRunTests(unittest.TestCase):
    """A12: export-profile on a folder with no finished run says so, before the k floor."""

    def test_empty_out_dir_is_refused_with_the_folder(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            empty = t / "elsewhere"
            empty.mkdir()
            rc, _, err = run_cli(["export-profile", "--config", str(DEMO_CONFIG), "--out-dir", str(empty),
                                  "--profile", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: no finished run found at {empty.resolve()}; "
                                          "pass --out-dir <the run's folder>")
            self.assertNotIn("k floor", err)
            self.assertFalse((t / "p.json").exists())

    def test_aggregates_without_log_or_log_without_aggregates_is_refused(self):
        for present in ("aggregates/aggregates.json", "logs/run.jsonl"):
            with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
                t = Path(t)
                f = t / "run" / present
                f.parent.mkdir(parents=True)
                f.write_text("{}", encoding="utf-8")
                rc, _, err = run_cli(["export-profile", "--config", str(DEMO_CONFIG), "--out-dir", str(t / "run"),
                                      "--profile", str(t / "p.json")])
                self.assertEqual(rc, 2, present)
                self.assertIn("no finished run found at", err, present)


class RefusalWordingTests(unittest.TestCase):
    """E4: the k floor in plain words, a missing file named as missing, no raw [Errno N], both values in a mismatch."""

    def test_k_floor_reason_wording(self):
        from second_scribe.profile_export import K_FLOOR, k_floor_reason
        self.assertEqual(K_FLOOR, 10)
        self.assertEqual(k_floor_reason(9), "this run analysed 9 people; a profile needs at least 10 (the k floor) so "
                                            "no number can point to one person. Nothing was written.")
        self.assertTrue(k_floor_reason(1).startswith("this run analysed 1 person;"))
        self.assertIn("(the k floor)", k_floor_reason(None))
        self.assertIn("not a whole number", k_floor_reason("12"))
        self.assertIsNone(k_floor_reason(10))

    def test_pool_k_floor_uses_the_same_words(self):
        with self.assertRaises(ValueError) as cm:
            poolmod.pool([profile(), profile(9)], labels=["labA", "lab9"])
        self.assertTrue(str(cm.exception).startswith("lab9: this run analysed 9 people; a profile needs at least 10 "
                                                     "(the k floor)"), str(cm.exception))

    def test_pool_missing_file_and_bad_json_are_two_messages(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=profile())
            (t / "junk.json").write_text("{not json", encoding="utf-8")
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(t / "nope.json"), "--out", str(t / "o.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: file not found: {t / 'nope.json'} (was this lab's export refused?)")
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(t / "junk.json"), "--out", str(t / "o.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: {t / 'junk.json'} cannot be read as JSON (line 1, column 2)")
            (t / "folder.json").mkdir()                     # exists but cannot be read as a file
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(t / "folder.json"), "--out", str(t / "o.json")])
            self.assertEqual(rc, 2)
            self.assertTrue(err.startswith(f"refused: cannot read {t / 'folder.json'}: "), err)
            self.assertNotIn("[Errno", err)
            self.assertFalse((t / "o.json").exists())

    def test_calibrate_missing_profile_is_file_not_found(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            rc, _, err = run_cli(["calibrate", "--config", str(DEMO_CONFIG), "--profile", str(t / "lab.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), f"refused: file not found: {t / 'lab.json'} (was this lab's export refused?)")

    def test_label_count_mismatch_gives_both_values(self):
        with self.assertRaises(ValueError) as cm:
            poolmod.pool([profile(), profile()], labels=["labA"])
        self.assertIn("1 label(s), 2 profile(s)", str(cm.exception))


class CodeHashTests(unittest.TestCase):
    """E6: run.kit.code_sha256 in the profile; pool lists differing code hashes as settings_differ."""

    H1, H2 = "1" * 64, "2" * 64

    def test_run_code_sha256_rule(self):
        from second_scribe.profile_export import run_code_sha256

        def starts(*codes):
            return [{"record_type": "run_start", "payload": {} if c is None else {"code_sha256": c}} for c in codes]
        self.assertEqual(run_code_sha256(starts(self.H1, self.H1)), self.H1)
        self.assertIsNone(run_code_sha256(starts(self.H1, self.H2)))            # sessions ran different code
        self.assertIsNone(run_code_sha256(starts(self.H1, None)))               # one session logged none
        self.assertIsNone(run_code_sha256(starts("not a hash")))
        self.assertIsNone(run_code_sha256([]))
        self.assertEqual(run_code_sha256(starts(self.H1) + [{"record_type": "run_fork", "payload": {}}]), self.H1)

    def _with_code(self, code):
        p = profile()
        if code is not None:
            p["run"]["kit"] = {"code_sha256": code}
        return p

    def test_same_or_absent_code_is_not_flagged(self):
        for a, b in ((self.H1, self.H1), (None, None)):
            g = poolmod.pool([self._with_code(a), self._with_code(b)], labels=["labA", "labB"])["groups"][0]
            self.assertIs(g["settings_differ"], False)
            self.assertIs(g["code_differs"], False)
            self.assertNotIn("kit code", poolmod.pool_markdown({"groups": [g], "separate": []}))

    def test_different_or_missing_code_is_settings_differ_side_by_side(self):
        for a, b in ((self.H1, self.H2), (self.H1, None)):
            out = poolmod.pool([self._with_code(a), self._with_code(b)], labels=["labA", "labB"])
            g = out["groups"][0]
            self.assertEqual(g["labs"], ["labA", "labB"])                       # still one table
            self.assertIs(g["settings_differ"], True)
            self.assertIs(g["code_differs"], True)
            self.assertIs(g["bootstrap_differs"], False)
            md = poolmod.pool_markdown(out)
            self.assertIn("ran different kit code (run.kit.code_sha256 differs", md)
            self.assertNotIn("bootstrap settings", md)
            self.assertNotIn(self.H1, json.dumps(out))                          # the hash is compared, never copied

    def test_the_study_profile_without_the_key_is_tolerated_by_the_reader(self):
        study = json.loads((FIXTURES / "profile-v2.json").read_text(encoding="utf-8"))
        self.assertNotIn("code_sha256", study["run"]["kit"])
        self.assertIsNone(poolmod._code_sha256(study))
        self.assertIsNone(poolmod._code_sha256({"run": None}))


class PoolChecksTests(unittest.TestCase):
    """E3: cheap checks, as warnings on the console and a Checks section in pool.md, only when something is found."""

    def _distinct(self):
        a, b = profile(recall=0.9), profile(recall=0.6)
        a["run"]["run_chain_heads"] = {"head": "c" * 64}
        b["run"]["run_chain_heads"] = {"head": "d" * 64}
        return a, b

    def test_clean_pair_has_no_checks_key_and_no_section(self):
        out = poolmod.pool(list(self._distinct()), labels=["labA", "labB"])
        self.assertNotIn("checks", out)
        self.assertNotIn("## Checks", poolmod.pool_markdown(out))

    def test_byte_identical_copy_is_flagged_cli(self):
        """The TRIAGE repro: cp labA.json labA-copy.json; pool both -> exit 0 (side by side) with a warning."""
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            a, _ = self._distinct()
            paths = write_labs(t, labA=a)
            (t / "labA-copy.json").write_bytes((t / "labA.json").read_bytes())
            rc, _, err = run_cli(["pool", "--profiles", paths[0], str(t / "labA-copy.json"), "--out", str(t / "p.json")])
            self.assertEqual(rc, 0)
            msg = "labA and labA-copy are identical files; labA-copy may be a copy, not a second lab"
            self.assertIn(f"warning: {msg}", err)
            md = (t / "p.md").read_text(encoding="utf-8")
            self.assertIn("## Checks", md)
            self.assertIn(f"- {msg}", md)
            self.assertEqual(json.loads((t / "p.json").read_text(encoding="utf-8"))["checks"], [msg])

    def test_same_chain_head_is_flagged(self):
        a, b = self._distinct()
        b["run"]["run_chain_heads"]["head"] = "c" * 64
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(out["checks"], ["labA and labB have the same run chain head; they come from one run, not two labs"])

    def test_value_outside_its_own_interval_is_flagged(self):
        a, b = self._distinct()
        b["rows"][0]["cells"][0]["value"] = 0.95          # interval [0.5, 0.7]: a hand edit
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(out["checks"], ["labB: row 1 attested_recall (primary): the value is outside its own "
                                         "interval [ci_lo, ci_hi]"])
        self.assertNotIn("0.95", json.dumps(out["checks"]))

    def test_lead_and_row7_disagreement_is_flagged(self):
        a, b = self._distinct()
        lead = {"name": "listening_added", "x": "primary", "n": 50, "unit": "node", "kind": "difference",
                "value": 0.3, "ci_lo": 0.2, "ci_hi": 0.4, "ci_level": 0.95}
        for p in (a, b):
            p["rows"].insert(0, {"row": "lead", "label_key": "profile.lead", "n_people": 12, "cells": [dict(lead)]})
            p["rows"].append({"row": 7, "label_key": "profile.row7.floors", "n_people": 12, "cells": [dict(lead)]})
        self.assertNotIn("checks", poolmod.pool([a, b], labels=["labA", "labB"]))
        b["rows"][-1]["cells"][0]["value"] = 0.35
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(out["checks"], ["labB: row 7 listening_added (primary) disagrees with the same cell in the "
                                         "lead block"])

    def test_no_value_and_not_not_run_is_malformed(self):
        a, b = self._distinct()
        del b["rows"][0]["cells"][1]["state"]             # r_star: value None, no state
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(out["checks"], ["labB: row 1 r_star has no value and is not marked not_run (malformed)"])

    def test_label_clash_names_the_label(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            (t / "a").mkdir()
            (t / "b").mkdir()
            for d in ("a", "b"):
                (t / d / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
            rc, _, err = run_cli(["pool", "--profiles", str(t / "a" / "labA.json"), str(t / "b" / "labA.json"),
                                  "--out", str(t / "p.json")])
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip(), "refused: two files are called labA; rename one")


def detection_block(sd1=0.1):
    per = {"D1": {"smallest_detected": sd1, "lowest_tried": 0.1, "at_or_below": sd1 == 0.1, "rule": "rep_interval"},
           "D2": {"smallest_detected": 0.5, "lowest_tried": 0.25, "at_or_below": False, "rule": "rep_interval"},
           "D3": {"smallest_detected": None, "lowest_tried": 0.1, "at_or_below": False, "rule": "rep_interval"},
           "D4": {"smallest_detected": 0.25, "lowest_tried": 0.25, "at_or_below": True, "rule": "rep_interval"},
           "D5": {"smallest_detected": 0.05, "lowest_tried": 0.05, "at_or_below": True, "rule": "rep_interval+kit_ci"}}
    return {"schema": "orgsci-detection-limits/v1", "per_dose": per, "pull_planted": 0.07, "pull_detected": True}


class CalibrateOutputTests(unittest.TestCase):
    """E5: calibrate prints one line per dose type and says it rewrites the profile in place; pool.md shows each lab's
    detection limits when present."""

    def test_calibrate_prints_one_line_per_dose_type(self):
        from second_scribe import phantom
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            path = t / "lab.json"
            path.write_text(json.dumps(profile()), encoding="utf-8")

            def fake(config, profile_path, **kw):
                d = json.loads(Path(profile_path).read_text(encoding="utf-8"))
                d["detection_limits"] = detection_block()
                Path(profile_path).write_text(json.dumps(d), encoding="utf-8")
                return "e" * 64
            with mock.patch.object(phantom, "calibrate", side_effect=fake):
                rc, out, err = run_cli(["calibrate", "--config", str(DEMO_CONFIG), "--profile", str(path)])
            self.assertEqual(rc, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[0], f"note: when it succeeds, calibrate rewrites {path} in place; keep a copy")
            self.assertIn(f"wrote {path}", lines)
            self.assertIn("sha256 " + "e" * 64, lines)
            doses = [ln for ln in lines if ln[:2] in ("D1", "D2", "D3", "D4", "D5")]
            self.assertEqual(len(doses), 5)
            self.assertEqual(doses[0], "D1 drop steps (share of AI nodes, with their edges): smallest dose detected "
                                       "10%, lowest dose tried 10% (read as: at or below 10%)")
            self.assertEqual(doses[1], "D2 drop links (share of AI edges): smallest dose detected 50%, lowest dose "
                                       "tried 25%")
            self.assertIn("D3 merge steps", doses[2])
            self.assertTrue(doses[2].endswith("not detected at any dose tried (lowest dose tried 10%)"))

    def test_help_says_in_place(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["calibrate", "--help"])
        self.assertIn("keep a copy", " ".join(out.getvalue().split()))

    def test_pool_shows_detection_limits_per_lab_only_when_present(self):
        a, b = profile(recall=0.9), profile(recall=0.6)
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertNotIn("detection_limits", out)
        self.assertNotIn("Detection limits", poolmod.pool_markdown(out))
        a["detection_limits"] = detection_block()
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(list(out["detection_limits"]), ["labA"])
        self.assertEqual(out["detection_limits"]["labA"]["D2"],
                         {"smallest_detected": 0.5, "lowest_tried": 0.25, "at_or_below": False})
        md = poolmod.pool_markdown(out)
        self.assertIn("## Detection limits", md)
        self.assertIn("| Dose type | labA |", md)
        self.assertIn("| D4 flip wished to received (share of AI partners of matched ideals) | smallest dose detected "
                      "25%, lowest dose tried 25% (read as: at or below 25%) |", md)

    def test_malformed_block_is_not_shown(self):
        a = profile()
        a["detection_limits"] = detection_block()
        a["detection_limits"]["per_dose"]["D1"]["smallest_detected"] = "ten percent"
        self.assertIsNone(poolmod.detection_limits_of(a))


def kit_export(head="c"):
    """The kit exporter's real output on the demo (fixture), with its own chain head so two copies are two runs."""
    p = json.loads((FIXTURES / "e2-m1-sample-demo.json").read_text(encoding="utf-8"))
    p["run"]["run_chain_heads"]["head"] = head * 64
    for row in p["rows"]:                     # the fixture predates the exporter's current derivation codes
        for c in row["cells"]:
            if c.get("derived") == "sum_of_counts":
                c["derived"] = "sum_of_cleared_counts"
    return p


class PoolMarkdownReadableTests(unittest.TestCase):
    """E1 (how to read, headline table in plain words, then all cells) and E2 (a lone profile, labs on their own)."""

    def test_how_to_read_then_headline_then_all_cells(self):
        out = poolmod.pool([kit_export("c"), kit_export("d")], labels=["labA", "labB"])
        md = poolmod.pool_markdown(out)
        lines = md.splitlines()
        h = lines.index("## How to read this")
        self.assertEqual([ln[:2] for ln in lines[h + 2:h + 7]], ["- "] * 5)
        self.assertEqual(lines[h + 7], "")
        self.assertIn("PROFILE-SCHEMA.md", lines[h + 6])
        self.assertLess(md.index("## How to read this"), md.index("|"))
        self.assertLess(md.index("### Headline"), md.index("### All cells"))
        self.assertIn("| People analysed | 12 | 12 |", md)
        self.assertIn("| Listening added (primary): AI recall minus no-interview recall | 0.587 [", md)
        self.assertIn("| AI recall (primary) | 0.909 [", md)
        self.assertIn("| No-interview recall (primary) | 0.322 [", md)
        self.assertIn("| AI map size: boxes per map | 9.167 [8.167, 10.333] | 9.167 [8.167, 10.333] |", md)
        self.assertIn("| No-interview map size: boxes per map | 10 | 10 |", md)
        self.assertIn("| Endorsed map size: boxes per map | 10.583 [9.583, 11.75] |", md)
        self.assertNotIn("## Checks", md)

    def test_single_profile_gets_its_own_one_column_tables(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=kit_export())
            rc, out, _ = run_cli(["pool", "--profiles", *paths, "--out", str(t / "p.json")])
            self.assertEqual(rc, 0)
            self.assertTrue(out.startswith("1 table, 1 lab listed on its own -> "), out)
            md = (t / "p.md").read_text(encoding="utf-8")
            self.assertIn("## Only one profile was given", md)
            self.assertIn("| Headline | labA |", md)
            self.assertIn("| Row | Cell | Setting | labA |", md)
            self.assertIn("| Listening added (primary): AI recall minus no-interview recall | 0.587 [", md)
            self.assertNotIn("every other lab", md)
            self.assertNotIn("## Listed separately", md)

    def test_labs_listed_on_their_own_each_get_a_headline_table(self):
        a, b, c = kit_export("c"), kit_export("d"), kit_export("e")
        c["run"]["prompt_set"]["q_phase_sha256"] = "f" * 64          # labC: another prompt set, so on its own
        with tempfile.TemporaryDirectory(prefix="ss-tests-w2-") as t:
            t = Path(t)
            paths = write_labs(t, labA=a, labB=b, labC=c)
            rc, out, _ = run_cli(["pool", "--profiles", *paths, "--out", str(t / "p.json")])
            self.assertEqual(rc, 0)
            self.assertTrue(out.startswith("2 tables, 1 lab listed on its own -> "), out)
            md = (t / "p.md").read_text(encoding="utf-8")
            sep = md[md.index("## Listed separately"):]
            self.assertIn("### labC", sep)
            self.assertIn("| Headline | labC |", sep)
            self.assertIn("| AI recall (primary) | 0.909 [", sep)
            self.assertIn("people analysed 12", sep)

    def test_two_labs_both_on_their_own(self):
        a, b = kit_export("c"), kit_export("d")
        b["run"]["prompt_set"]["q_phase_sha256"] = "f" * 64
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(poolmod.table_counts(out), (2, 2))
        md = poolmod.pool_markdown(out)
        self.assertIn("### labA", md)
        self.assertIn("### labB", md)
        self.assertIn("| Headline | labB |", md)
        self.assertNotIn("Only one profile", md)


if __name__ == "__main__":
    unittest.main()
