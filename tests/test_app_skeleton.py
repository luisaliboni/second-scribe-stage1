"""Task 1: app package skeleton, strings file, Python 3.10 syntax guard."""
import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from second_scribe.app import strings

ROOT = Path(__file__).resolve().parent.parent
EN = ROOT / "second_scribe" / "app" / "strings" / "en.json"


def _run(*args):
    return subprocess.run([sys.executable, "-m", "second_scribe.app", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


class TestEntry(unittest.TestCase):
    def test_help_exits_zero(self):
        r = _run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("what listening added", r.stdout)   # no product name on screen (author, 23 Oct)

    def test_version_prints_kit_version(self):
        r = _run("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0.1.0")


class TestStrings(unittest.TestCase):
    def test_strings_flat_and_valid(self):
        data = json.loads(EN.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertTrue(data)
        for key, value in data.items():
            self.assertRegex(key, r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
            self.assertIsInstance(value, str, key)
            self.assertTrue(value.strip(), key)
        # load_strings merges the kit's profile_strings.json (PROFILE-SCHEMA §13) over en.json, never overriding it
        kit_file = ROOT / "second_scribe" / "profile_strings.json"
        kit = json.loads(kit_file.read_text(encoding="utf-8")) if kit_file.is_file() else {}
        self.assertFalse(set(kit) & set(data))
        self.assertEqual(strings.load_strings("en"), {**data, **kit})

    def test_t_formats_slots(self):
        self.assertEqual(strings.t("band.own", reference="A"), "Your data · reference: A")
        self.assertEqual(strings.t("app.name"), "What did listening add?")   # no product name (author, 23 Oct)

    def test_t_formats_slot_pattern(self):
        # the documented contract: "x of {n}" style values fill from kwargs
        saved = dict(strings._CACHE)
        try:
            strings._CACHE["en"] = {"demo.count": "x of {n}"}
            self.assertEqual(strings.t("demo.count", n=3), "x of 3")
        finally:
            strings._CACHE.clear()
            strings._CACHE.update(saved)

    def test_t_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            strings.t("nope.nope")


class TestPy310(unittest.TestCase):
    def test_sources_parse_as_py310(self):
        files = [p for d in ("second_scribe", "tests") for p in (ROOT / d).rglob("*.py")]
        self.assertTrue(files)
        for p in files:
            with self.subTest(file=str(p.relative_to(ROOT))):
                ast.parse(p.read_text(encoding="utf-8"), filename=str(p), feature_version=(3, 10))


if __name__ == "__main__":
    unittest.main()
