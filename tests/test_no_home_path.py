"""No tracked file names the builder's home folder or an internal worktree (both would leak into the public repo)."""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestNoHomePath(unittest.TestCase):
    def test_no_tracked_file_holds_the_home_folder(self):
        home = os.path.expanduser("~").rstrip("/")
        if home.count("/") < 2:          # "/root" or unknown: nothing user-specific to look for
            self.skipTest("no user home folder")
        files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        needle = (home + "/").encode()
        hits = [f for f in files.decode().split("\0") if f and (ROOT / f).is_file()
                and needle in (ROOT / f).read_bytes()]
        self.assertEqual(hits, [])

    def test_no_tracked_file_names_an_internal_worktree(self):
        files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        needle = ("second-scribe" + "-wt/").encode()
        hits = [f for f in files.decode().split("\0") if f and (ROOT / f).is_file()
                and needle in (ROOT / f).read_bytes()]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
