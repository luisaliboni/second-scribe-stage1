"""No ``[R3T0:`` placeholder ships in the kit's documents (WORDING-PLACES item 34).

The reading-3 numbers in the docs were written as ``[R3T0:<name>]`` placeholders until the cleared values existed.
This guard fails if one is left in README.md, any file under docs/, or analysis/public/PROFILE-SCHEMA.md.
"""
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MARK = "[R3T0:"


def surfaces(root: Path = REPO) -> list[Path]:
    """The files the guard reads: README.md, every file under docs/, and the profile schema."""
    files = [root / "README.md", root / "analysis" / "public" / "PROFILE-SCHEMA.md"]
    files += sorted(p for p in (root / "docs").rglob("*") if p.is_file())
    return files


def placeholder_hits(root: Path = REPO) -> list[str]:
    """``path:line`` for every line that holds the placeholder mark."""
    out = []
    for f in surfaces(root):
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if MARK in line:
                out.append(f"{f.relative_to(root).as_posix()}:{i}")
    return out


class TestNoR3T0Placeholders(unittest.TestCase):
    def test_the_surfaces_exist(self):
        files = surfaces()
        for f in files[:2]:
            self.assertTrue(f.is_file(), f)
        self.assertTrue((REPO / "docs" / "technical-appendix-full.md") in files)

    def test_no_placeholder_is_left(self):
        self.assertEqual(placeholder_hits(), [], "an [R3T0:...] placeholder is still in the documents")

    def test_a_planted_placeholder_is_caught(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-r3t0-guard-") as tmp:
            root = Path(tmp)
            (root / "analysis" / "public").mkdir(parents=True)
            (root / "docs" / "sub").mkdir(parents=True)
            (root / "README.md").write_text("clean\n", encoding="utf-8")
            (root / "analysis" / "public" / "PROFILE-SCHEMA.md").write_text("a\n[R3T0:nodes_per_map]\n",
                                                                           encoding="utf-8")
            (root / "docs" / "sub" / "x.md").write_text("value [R3T0:listening_added] here\n", encoding="utf-8")
            self.assertEqual(placeholder_hits(root), ["analysis/public/PROFILE-SCHEMA.md:2", "docs/sub/x.md:1"])


if __name__ == "__main__":
    unittest.main()
