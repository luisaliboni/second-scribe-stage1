"""Guard: the suite must run inside one per-run temp dir (tests/__init__.py), removed at exit.

Without it every leaked test temp dir lands in the shared $TMPDIR and piles up (F7: ~24,000 dirs, ~10 GB).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TmpdirIsolation(unittest.TestCase):
    def test_tempfile_tempdir_is_the_per_run_dir(self):
        d = Path(tempfile.tempdir).resolve()
        self.assertTrue(d.name.startswith("ss-tests-"), f"tempfile.tempdir not isolated: {d}")
        self.assertTrue(d.is_dir())
        self.assertTrue(Path(tempfile.gettempdir()).resolve() == d)

    def test_env_vars_point_at_it_so_subprocesses_follow(self):
        for var in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(Path(os.environ[var]).resolve(), Path(tempfile.tempdir).resolve(), var)
        out = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(Path(out).resolve(), Path(tempfile.tempdir).resolve())

    def test_new_temp_dirs_land_inside_it(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(Path(t).resolve().parent, Path(tempfile.tempdir).resolve())

    def test_dir_is_removed_at_exit(self):
        code = ("import tests, tempfile; print(tempfile.tempdir)")
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root).stdout.strip()
        self.assertTrue(Path(out).name.startswith("ss-tests-"))
        self.assertFalse(Path(out).exists(), "per-run dir survived interpreter exit")


class DemoLeavesNothing(unittest.TestCase):
    def test_demo_leaves_nothing_in_tmpdir(self):
        """`make demo` runs `cli demo`; point its TMPDIR at an empty dir and require it empty afterwards."""
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as probe, tempfile.TemporaryDirectory() as out:
            env = dict(os.environ, TMPDIR=probe, TMP=probe, TEMP=probe)
            r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", out, "--client", "replay"],
                               capture_output=True, text=True, cwd=root, env=env)
            self.assertEqual(r.returncode, 0, r.stderr[-500:])
            self.assertEqual(sorted(os.listdir(probe)), [], "demo left files in $TMPDIR")


if __name__ == "__main__":
    unittest.main()
