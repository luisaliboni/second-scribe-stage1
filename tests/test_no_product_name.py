"""Item 34 (design feedback, 23 Oct): no product name on any surface a reader sees. The words "second scribe" (any case, with a
space) never appear in the app's strings, in any JavaScript, CSS or HTML file of the app and the site, or in a freshly
built site. Code names with an underscore or a hyphen (the ``second_scribe`` package, ``second-scribe-stage1``) are
not reader-facing text and are not matched.

Item 41 (Part 5c): the outputs a reader gets carry no product name either. The generated ``report.md`` of a run, the
methods paragraph that ``export.export_methods`` writes, and the README's title are matched with a wider pattern that
also catches the hyphenated name used as a title or in prose ("# second-scribe report", "made with second-scribe").
Technical ids stay allowed: ``second_scribe`` in commands, ``second-scribe-stage1`` as a folder name, and file paths
("second-scribe/...")."""
from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
NAME = re.compile(r"second\s+scribe", re.IGNORECASE)
SUFFIXES = (".js", ".css", ".html", ".json", ".svg", ".md", ".txt")
# item 41: the name with a space or a hyphen, unless it is part of a folder name or a path
PROSE_NAME = re.compile(r"(?<![\w/.-])second[\s-]+scribe(?![\w/-])", re.IGNORECASE)


def hits(paths) -> list[str]:
    out = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in NAME.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{p}:{line}: {text[max(0, m.start() - 30):m.end() + 30]!r}")
    return out


def surface_files() -> list[Path]:
    files = [APP / "strings" / "en.json"]
    for folder in (APP / "static", APP / "pages"):
        files += [p for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix in SUFFIXES]
    return files


class TestNoProductName(unittest.TestCase):
    def test_pattern_matches_the_name_and_not_code_names(self):
        self.assertTrue(NAME.search("Welcome to Second Scribe"))
        self.assertTrue(NAME.search("SECOND  SCRIBE"))
        self.assertFalse(NAME.search("second_scribe.app"))
        self.assertFalse(NAME.search("second-scribe-stage1"))

    def test_strings_and_app_files(self):
        files = surface_files()
        self.assertGreater(len([p for p in files if p.suffix == ".js"]), 20)
        self.assertGreater(len([p for p in files if p.suffix == ".css"]), 5)
        self.assertGreater(len([p for p in files if p.suffix == ".html"]), 3)
        self.assertEqual(hits(files), [])

    def test_freshly_built_site(self):
        from tests import test_build_site as tbs
        out = tbs.build(study=tbs.STUDY_PROFILE)
        try:
            built = [p for p in sorted(out.rglob("*")) if p.is_file() and p.suffix in SUFFIXES]
            self.assertTrue(any(p.name == "index.html" for p in built))
            self.assertEqual(hits(built), [])
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


class TestNoProductNameInOutputs(unittest.TestCase):
    """Item 41: a run's report.md, the exported methods text and the README title."""

    def test_prose_pattern_catches_titles_and_prose_and_spares_technical_ids(self):
        for bad in ("# second-scribe report", "made with second-scribe.", "Second Scribe", "SECOND-SCRIBE run"):
            self.assertTrue(PROSE_NAME.search(bad), bad)
        for ok in ("python3 -m second_scribe.cli demo", "cd second-scribe-stage1", "second-scribe/.superpowers/sdd",
                   "repository/second-scribe-stage1/README.md"):
            self.assertFalse(PROSE_NAME.search(ok), ok)

    def test_generated_report_and_methods_on_a_temp_run(self):
        from second_scribe.app import export
        from tests.test_export import RUN_FOLDER, RUN_ID, fresh_project
        p = fresh_project()
        try:
            reports = sorted(p.path(RUN_FOLDER).rglob("report.md"))
            self.assertTrue(reports, "the demo run wrote a report.md")
            for path in reports:
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[0], "# Report: what did listening add?")
                self.assertEqual(PROSE_NAME.findall(text), [], str(path))
            methods = export.export_methods(p, RUN_ID)
            self.assertTrue(methods.strip())
            self.assertEqual(PROSE_NAME.findall(methods), [])
        finally:
            shutil.rmtree(p.root.parent, ignore_errors=True)

    def test_readme_title_and_prose(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0], "# What did listening add? Kit for Stage 1")
        self.assertEqual(PROSE_NAME.findall(text), [])


if __name__ == "__main__":
    unittest.main()
