"""Village run-path checks, using only synthetic files and offline clients."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import audits, cli, pipeline
from second_scribe.audit.manifest import compute_manifest_hash, read_manifest
from second_scribe.audit.registry import AuditLog
from second_scribe import run_safety
from tests.test_village_inputs import make_corpus


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(args))
    return rc, out.getvalue(), err.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-village-run-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class TestExternalAnchor(Tmp):
    def test_saved_hash_detects_a_consistent_replacement_log_and_manifest(self):
        path = self.root / "run.jsonl"
        log = AuditLog(path, "synthetic")
        log.append("example", {"value": 1})
        log.close()
        saved = compute_manifest_hash(read_manifest(self.root / "run.manifest.json"))
        rc, out, err = invoke("verify", str(path), "--expect-manifest-hash", saved)
        self.assertEqual(rc, 0, err)
        self.assertIn("PASS:", out)
        path.unlink()
        (self.root / "run.manifest.json").unlink()
        replacement = AuditLog(path, "synthetic")
        replacement.append("example", {"value": 2})
        replacement.close()
        self.assertEqual(invoke("verify", str(path))[0], 0)
        rc, out, err = invoke("verify", str(path), "--expect-manifest-hash", saved)
        self.assertEqual(rc, 1, err)
        self.assertIn("manifest_hash_mismatch", out)


class TestChangedResume(Tmp):
    def test_changed_synthetic_config_or_corpus_refuses_without_writing(self):
        config = make_corpus(self.root)
        args = ("run", "--config", str(config), "--only", "mapping_talk_confirm", "--client", "oracle")
        self.assertEqual(invoke(*args)[0], 0)
        folder = self.root / "run"
        before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        original = config.read_text()
        doc = json.loads(original)
        doc["context"]["domain"] += " changed"
        config.write_text(json.dumps(doc))
        for change in ("config", "corpus"):
            with self.subTest(change=change):
                if change == "corpus":
                    config.write_text(original)
                    meta = self.root / "corpus" / "meta.json"
                    meta.write_text(meta.read_text() + "\n")
                rc, out, err = invoke(*args)
                self.assertEqual(rc, 2, (out, err))
                self.assertIn("inputs changed since this folder was written; use a new --out", err)
                self.assertEqual(before, {str(p.relative_to(folder)): p.read_bytes()
                                          for p in folder.rglob("*") if p.is_file()})

    def test_blocked_run_marks_old_reports_and_names_its_evidence(self):
        config = make_corpus(self.root)
        folder = self.root / "run"
        reports = [folder / "report.md", folder / "aggregates" / "report.md"]
        for report in reports:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("previous successful report\n")
        failure = audits.AuditBlocked([audits.AuditResult("schema", True, False, {}, ["broken:1"])])
        with mock.patch.dict(pipeline.STAGES, {"mapping_talk_confirm": mock.Mock(side_effect=failure)}):
            rc, out, err = invoke("run", "--config", str(config), "--only", "mapping_talk_confirm", "--client", "oracle")
        self.assertEqual(rc, 4, (out, err))
        for report in reports:
            self.assertTrue(report.with_suffix(".STALE").is_file())
        evidence = folder / "audits" / "blocked.json"
        self.assertTrue(evidence.is_file())
        self.assertIn(f"details: {evidence}", err)


class TestOutputContainment(Tmp):
    def test_output_symlinks_cannot_write_outside_the_run_folder(self):
        config = make_corpus(self.root)
        folder = self.root / "run"
        folder.mkdir()
        outside = self.root / "elsewhere"
        outside.mkdir()
        for name in ("logs", "work", "aggregates", "audits", "work/nested", "report.md"):
            with self.subTest(path=name):
                link = folder / name
                link.parent.mkdir(parents=True, exist_ok=True)
                target = outside / "report.md" if name == "report.md" else outside
                link.symlink_to(target)
                rc, out, err = invoke("run", "--config", str(config), "--only", "mapping_talk_confirm", "--client", "oracle")
                self.assertEqual(rc, 2, (out, err))
                self.assertIn("outside the output folder", err)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((folder / "logs" / "run.jsonl").exists())
                link.unlink()


class TestRunLock(Tmp):
    def test_second_cli_run_is_refused_and_lock_releases_after_an_exception(self):
        config = make_corpus(self.root)
        folder = self.root / "run"
        with self.assertRaisesRegex(RuntimeError, "test stop"):
            with run_safety.run_lock(folder):
                meta = json.loads((folder / ".run.lock").read_text())
                self.assertEqual(meta["pid"], os.getpid())
                self.assertTrue(meta["started_at"])
                rc, out, err = invoke("run", "--config", str(config), "--only", "mapping_talk_confirm", "--client", "oracle")
                self.assertEqual(rc, 2, (out, err))
                self.assertIn("another run is using", err)
                self.assertFalse((folder / "logs").exists())
                raise RuntimeError("test stop")
        self.assertFalse((folder / ".run.lock").exists())
        with run_safety.run_lock(folder):
            self.assertTrue((folder / ".run.lock").exists())

    def test_crashed_owner_is_recovered_with_a_note(self):
        code = ("import os,sys; from second_scribe.run_safety import run_lock; "
                "lock=run_lock(sys.argv[1]); lock.__enter__(); os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(self.root)], check=True)
        notes = []
        with run_safety.run_lock(self.root, say=notes.append):
            self.assertEqual(json.loads((self.root / ".run.lock").read_text())["pid"], os.getpid())
        self.assertTrue(any("abandoned" in note for note in notes), notes)


class TestManifestTemp(Tmp):
    def test_manifest_does_not_overwrite_a_previous_writers_temp_file(self):
        old = self.root / "run.manifest.json.tmp"
        old.write_text("another writer's temporary bytes")
        log = AuditLog(self.root / "run.jsonl", "synthetic")
        log.append("example", {})
        log.close()
        self.assertEqual(old.read_text(), "another writer's temporary bytes")


class TestReplayResume(Tmp):
    def test_resumed_demo_keeps_the_recorded_determinism_result_and_report_bytes(self):
        args = ("demo", "--out", str(self.root / "demo"), "--client", "replay")
        rc, out, err = invoke(*args)
        self.assertEqual(rc, 0, (out[-1000:], err))
        report = self.root / "demo" / "report.md"
        before = report.read_bytes()
        rc, out, err = invoke(*args)
        self.assertEqual(rc, 0, (out[-1000:], err))
        self.assertEqual(report.read_bytes(), before)


class TestFreshDemo(Tmp):
    def test_fresh_demo_resets_generated_files_but_keeps_the_guard_and_other_files(self):
        folder = self.root / "demo"
        folder.mkdir()
        (folder / "notes.txt").write_text("keep my notes")
        args = ("demo", "--out", str(folder), "--client", "replay", "--fresh")
        rc, out, err = invoke(*args)
        self.assertEqual(rc, 0, (out[-1000:], err))
        guard = (folder / ".run.guard").stat().st_ino
        reports = {name: (folder / name).read_bytes() for name in ("report.md", "aggregates.json", "traps.md")}
        old = folder / "work" / "old-output.json"
        old.write_text("old output")
        rc, out, err = invoke(*args)
        self.assertEqual(rc, 0, (out[-1000:], err))
        self.assertFalse(old.exists())
        self.assertEqual((folder / ".run.guard").stat().st_ino, guard)
        self.assertEqual((folder / "notes.txt").read_text(), "keep my notes")
        self.assertEqual(reports, {name: (folder / name).read_bytes() for name in reports})

    def test_fresh_refuses_custom_configs_and_live_clients_without_writes(self):
        config = make_corpus(self.root)
        folder = self.root / "fresh"
        for extra in (("--config", str(config)), ("--client", "http")):
            rc, out, err = invoke("demo", "--out", str(folder), "--fresh", *extra)
            self.assertEqual(rc, 2, (out, err))
            self.assertIn("--fresh is only for the built-in offline demo", err)
            self.assertFalse(folder.exists())


class TestSummary(Tmp):
    def test_offline_summary_counts_calls_paths_manifest_and_stage_time(self):
        config = make_corpus(self.root)
        rc, out, err = invoke("run", "--config", str(config), "--only", "prior_only", "--client", "oracle")
        self.assertEqual(rc, 0, (out, err))
        self.assertIn("client: oracle", out)
        self.assertIn("0 model calls", out)
        self.assertIn("responses replayed: 0", out)
        for filename in ("aggregates.json", "report.md", "traps.md"):
            self.assertIn(str(self.root / "run" / "aggregates" / filename), out)
        manifest = read_manifest(self.root / "run" / "logs" / "run.manifest.json")
        self.assertIn(compute_manifest_hash(manifest), out)
        self.assertIn(f"audit log records: {manifest['record_count']}", out)
        self.assertIn("record this manifest hash outside this folder", out)
        self.assertRegex(out, r"stage prior_only: [0-9.]+ s")
        self.assertIn("PASS: {", out)
