"""A11: ``--help`` of synth.labs and prompt_manifest prints usage and writes nothing; a path starting with ``-`` is refused."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(module, *args, cwd):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", module, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


class _InTempCwd(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="ss-help-")
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)

    def listing(self):
        return sorted(os.listdir(self.cwd))


class TestLabsCli(_InTempCwd):
    M = "second_scribe.synth.labs"

    def test_help_prints_usage_and_creates_nothing(self):
        for flag in ("--help", "-h"):
            r = _run(self.M, flag, cwd=self.cwd)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("usage:", r.stdout)
            self.assertIn("DEST", r.stdout)
            self.assertEqual(self.listing(), [])

    def test_dest_starting_with_a_dash_is_refused(self):
        for bad in ("-x", "--out"):
            r = _run(self.M, bad, cwd=self.cwd)
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("usage:", r.stderr)
            self.assertEqual(self.listing(), [])

    def test_dash_dash_does_not_smuggle_a_dash_dest(self):
        r = _run(self.M, "--", "-x", cwd=self.cwd)
        self.assertEqual(r.returncode, 2)
        self.assertIn("must not start with '-'", r.stderr)
        self.assertEqual(self.listing(), [])

    def test_missing_dest_is_refused(self):
        r = _run(self.M, cwd=self.cwd)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.listing(), [])

    def test_normal_call_unchanged(self):
        r = _run(self.M, "labs", cwd=self.cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        base = Path(self.cwd, "labs").resolve()
        self.assertEqual(r.stdout.splitlines(),
                         [f"{lab}: {base / lab / 'config.json'}" for lab in ("labA", "labB", "lab9")])
        for lab in ("labA", "labB", "lab9"):
            self.assertTrue((base / lab / "config.json").is_file())


class TestPromptManifestCli(_InTempCwd):
    M = "second_scribe.prompt_manifest"

    def test_help_prints_usage_and_writes_nothing(self):
        before = (REPO / "prompts" / "MANIFEST.json").read_bytes()
        for flag in ("--help", "-h"):
            r = _run(self.M, flag, cwd=self.cwd)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("usage:", r.stdout)
            self.assertEqual(self.listing(), [])
        self.assertEqual((REPO / "prompts" / "MANIFEST.json").read_bytes(), before)

    def test_folder_starting_with_a_dash_is_refused(self):
        r = _run(self.M, "-x", cwd=self.cwd)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        r = _run(self.M, "--", "-x", cwd=self.cwd)
        self.assertEqual(r.returncode, 2)
        self.assertIn("must not start with '-'", r.stderr)
        self.assertEqual(self.listing(), [])

    def test_missing_folder_is_refused_not_a_traceback(self):
        r = _run(self.M, "nope", cwd=self.cwd)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.listing(), [])

    def test_normal_call_unchanged(self):
        d = Path(self.cwd, "p")
        d.mkdir()
        (d / "a.txt").write_text("hello\n", encoding="utf-8")
        r = _run(self.M, str(d), cwd=self.cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(r.stdout.strip(), json.dumps(m["set_sha256"]))
        self.assertEqual(list(m["files"]), ["a.txt"])


if __name__ == "__main__":
    unittest.main()
