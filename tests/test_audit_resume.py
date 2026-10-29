"""F1a items 3 and 4: a truncated log cannot be re-closed (resume checks the manifest prefix), manifests chain, and the
manifest is written atomically."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe.audit import manifest as manifest_mod
from second_scribe.audit.manifest import compute_manifest_hash, manifest_path_for, read_manifest
from second_scribe.audit.registry import AuditLog
from second_scribe.audit.verify import verify_manifest_chain, verify_run
from second_scribe.cli import DEMO_CONFIG, main


def clock():
    return "2026-01-01T00:00:00+00:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "run.jsonl")

    def make(self, n=10):
        log = AuditLog(self.path, "r", clock=clock)
        for i in range(n):
            log.append("x", {"i": i})
        return log.close()

    def lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.readlines()

    def write(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)


class TestResumeChecksThePrefix(Base):
    def test_a_truncated_log_cannot_be_resumed_and_the_manifest_is_left_alone(self):
        self.make(10)
        before = Path(manifest_path_for(self.path)).read_bytes()
        self.write(self.lines()[:-4])
        self.assertFalse(verify_run(self.path)[0])
        with self.assertRaises(RuntimeError) as cm:
            AuditLog(self.path, "r", clock=clock)
        self.assertTrue(str(cm.exception).startswith("refusing to resume"))
        self.assertIn("truncated", str(cm.exception))
        self.assertEqual(Path(manifest_path_for(self.path)).read_bytes(), before)
        self.assertEqual(len(self.lines()), 6)            # nothing was appended
        self.assertFalse(verify_run(self.path)[0])        # verify still fails

    def test_an_emptied_log_cannot_be_resumed(self):
        self.make(5)
        open(self.path, "w").close()
        with self.assertRaises(RuntimeError):
            AuditLog(self.path, "r", clock=clock)

    def test_a_rewritten_tail_with_a_valid_chain_cannot_be_resumed(self):
        self.make(5)
        keep = self.lines()[:3]
        self.write(keep)
        log = AuditLog.__new__(AuditLog)                  # build a different, internally valid tail by hand
        log.path, log.run_id, log._clock = self.path, "r", clock
        log.append("other", {"i": 99})
        log.append("other", {"i": 100})                   # 5 records again, but not the ones the manifest names
        self.assertEqual(len(self.lines()), 5)
        with self.assertRaises(RuntimeError) as cm:
            AuditLog(self.path, "r", clock=clock)
        self.assertTrue(str(cm.exception).startswith("refusing to resume"))

    def test_a_log_longer_than_its_manifest_resumes(self):
        # a session that crashed after the last close left more records than the manifest names: not tampering
        self.make(5)
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {"late": 1})                      # no close
        log2 = AuditLog(self.path, "r", clock=clock)      # resumes
        log2.append("x", {"later": 1})
        log2.close()
        self.assertTrue(verify_run(self.path)[0])

    def test_an_untampered_log_resumes_and_verifies_again(self):
        self.make(5)
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {"more": 1})
        log.close()
        ok, detail = verify_run(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 6)

    def test_a_log_with_no_manifest_still_resumes(self):
        # a first session that crashed before close
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {"i": 1})
        AuditLog(self.path, "r", clock=clock).append("x", {"i": 2})


class TestCliRefusesATruncatedLog(Base):
    def config(self):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=self.tmp, base_dir=str(Path(DEMO_CONFIG).parent))
        p = Path(self.tmp) / "config.json"
        p.write_text(json.dumps(d))
        return p

    def test_report_after_truncation_is_refused_and_verify_still_fails(self):
        cfg = self.config()
        log = Path(self.tmp) / "logs" / "run.jsonl"
        log.parent.mkdir()
        self.path = str(log)
        self.make(12)
        before = Path(manifest_path_for(self.path)).read_bytes()
        self.write(self.lines()[:-4])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["report", "--config", str(cfg)])
        self.assertEqual(rc, 2)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(Path(manifest_path_for(self.path)).read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify", self.path]), 1)


class TestManifestChain(Base):
    def test_each_session_links_to_the_manifest_before_it(self):
        _p, h1 = self.make(3)
        m1 = read_manifest(manifest_path_for(self.path))
        self.assertIsNone(m1["prev_manifest_hash"])
        copy1 = os.path.join(self.tmp, "m1.json")
        shutil.copy(manifest_path_for(self.path), copy1)
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {"more": 1})
        _p, h2 = log.close()
        m2 = read_manifest(manifest_path_for(self.path))
        self.assertEqual(m2["prev_manifest_hash"], h1)
        self.assertEqual(h1, compute_manifest_hash(m1))
        copy2 = os.path.join(self.tmp, "m2.json")
        shutil.copy(manifest_path_for(self.path), copy2)
        ok, info = verify_manifest_chain([copy1, copy2])
        self.assertTrue(ok, info)

    def test_an_explicit_previous_hash_is_kept(self):
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {})
        log.close(prev_manifest_hash="abc123")
        self.assertEqual(read_manifest(manifest_path_for(self.path))["prev_manifest_hash"], "abc123")


class TestManifestIsAtomic(Base):
    def test_a_failed_write_leaves_the_old_manifest_intact_and_no_temp_file(self):
        self.make(3)
        mp = manifest_path_for(self.path)
        before = Path(mp).read_bytes()
        log = AuditLog(self.path, "r", clock=clock)
        log.append("x", {"i": 1})
        with mock.patch.object(manifest_mod.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                log.close()
        self.assertEqual(Path(mp).read_bytes(), before)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_the_manifest_is_written_to_a_temp_file_fsynced_then_renamed(self):
        calls = []
        real_replace, real_fsync = os.replace, os.fsync
        with mock.patch.object(manifest_mod.os, "replace", side_effect=lambda a, b: (calls.append(("replace", a, b)),
                                                                                       real_replace(a, b))[1]), \
                mock.patch.object(manifest_mod.os, "fsync", side_effect=lambda fd: (calls.append(("fsync",)),
                                                                                       real_fsync(fd))[1]):
            self.make(2)
        kinds = [c[0] for c in calls]
        self.assertIn("fsync", kinds)
        rep = next(c for c in calls if c[0] == "replace")
        self.assertNotEqual(rep[1], rep[2])
        self.assertEqual(rep[2], manifest_path_for(self.path))
        self.assertLess(kinds.index("fsync"), kinds.index("replace"))


if __name__ == "__main__":
    unittest.main()
