"""Makefile targets (dry runs with ``make -n``, nothing is executed)."""
import shutil
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def dry_run(target):
    r = subprocess.run(["make", "-n", "-s", "-C", str(REPO), target], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    return r.returncode, [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class TestDemoTargetsStartClean(unittest.TestCase):
    """A4 + E15: a fresh demo must not erase a folder owned by another process."""

    def test_demo_targets_preserve_an_active_runs_files(self):
        from second_scribe.run_safety import run_lock
        with tempfile.TemporaryDirectory(prefix="ss-tests-make-lock-") as tmp:
            root = Path(tmp)
            shutil.copyfile(REPO / "Makefile", root / "Makefile")
            env = dict(os.environ, PYTHONPATH=str(REPO))
            for target in ("demo", "demo-oracle"):
                with self.subTest(target=target):
                    out = root / "out" / target
                    with run_lock(out):
                        report = out / "report.md"
                        report.write_text("active run's report")
                        result = subprocess.run(["make", "-s", target, f"PY={sys.executable}"],
                                                cwd=root, env=env, capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("another run is using", result.stderr)
                        self.assertEqual(report.read_text(), "active run's report")


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class TestTestTargets(unittest.TestCase):
    """E9: ``make test`` is quiet (no -v); ``make test-fast`` runs the same discovery minus the slow modules."""

    def test_test_target_is_not_verbose(self):
        rc, lines = dry_run("test")
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("unittest discover -s tests -t .", lines[0])
        self.assertNotIn(" -v", lines[0])

    def test_test_fast_target_runs_the_fast_runner(self):
        rc, lines = dry_run("test-fast")
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["python3 -m tests.fast"])


class TestFastSuite(unittest.TestCase):
    def test_slow_list_names_real_modules(self):
        from tests import fast
        for name in fast.SLOW_MODULES:
            self.assertTrue((REPO / "tests" / f"{name}.py").is_file(), name)

    def test_fast_suite_is_the_discovery_minus_the_slow_modules(self):
        from tests import fast
        all_mods = {type(t).__module__.rpartition(".")[2] for t in fast._flatten(
            unittest.defaultTestLoader.discover(str(REPO / "tests"), top_level_dir=str(REPO)))}
        fast_mods = {type(t).__module__.rpartition(".")[2] for t in fast._flatten(fast.fast_suite())}
        self.assertEqual(all_mods - fast.SLOW_MODULES, fast_mods)
        self.assertIn("test_makefile", fast_mods)          # a new test file is in the fast run without being listed
        self.assertTrue(fast.SLOW_MODULES <= all_mods)
        self.assertFalse(fast_mods & fast.SLOW_MODULES)

    def test_an_empty_slow_list_keeps_everything(self):
        from tests import fast
        n_all = sum(1 for _ in fast._flatten(unittest.defaultTestLoader.discover(
            str(REPO / "tests"), top_level_dir=str(REPO))))
        self.assertEqual(sum(1 for _ in fast._flatten(fast.fast_suite(slow=frozenset()))), n_all)


if __name__ == "__main__":
    unittest.main()
