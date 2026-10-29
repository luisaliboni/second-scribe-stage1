"""Browser-pass review fixes R4, R6/R17, R8, R13 (round 2). Each test pins the decided fix."""
import json
import os
import re
import unittest
from pathlib import Path

from tests import results_fixture as rf
from tests.test_results_api import Case

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
STRINGS = json.loads((APP / "strings" / "en.json").read_text(encoding="utf-8"))


class R4StepNamedByTitle(unittest.TestCase):
    def test_check_notes_name_the_step_not_a_direction(self):
        for key in ("setup.check.not_ready", "setup.check.failed_note"):
            text = STRINGS[key]
            self.assertIsNone(re.search(r"\b(above|below)\b", text, re.I), key)
            self.assertIn("{step}", text, key)

    def test_the_step_title_the_notes_use_exists(self):
        self.assertEqual(STRINGS["setup.models.title"], "Models")


# R6 / R17. The bundled fonts DO carry U+2713 (checked by reading the woff2 cmap), but Plex draws it so that it reads
# as a square-root sign. The pass mark is therefore drawn in CSS, and no shipped file may emit the font glyph.
CHECK_FORMS = re.compile(r"\\0*27(13|14)\b|\\u27(13|14)|&#x0*27(13|14);|&#0*1(0003|0004);|&check;|&checkmark;|\u2713|\u2714",
                         re.I)


def _shipped_text_files():
    skip = {".woff2", ".png", ".ico", ".jpg", ".gif"}
    for base in (APP, ROOT / "second_scribe" / "site"):
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() not in skip:
                yield p


class R6R17DrawnCheckMark(unittest.TestCase):
    def test_no_shipped_file_emits_the_check_mark_glyph(self):
        bad = []
        for p in _shipped_text_files():
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if CHECK_FORMS.search(text):
                bad.append(str(p.relative_to(ROOT)))
        self.assertEqual(bad, [], "U+2713/U+2714 must be drawn in CSS, not set as a font glyph")

    def test_the_three_marks_are_drawn_with_a_rotated_border_box(self):
        css = APP / "static" / "css"
        for name, selector in (("app.css", ".ss-check-passed::before"),
                               ("strip.css", '.sd-mode[aria-pressed="true"]::before'),
                               ("site-explain.css", ".ss-x-stamp::before")):
            text = (css / name).read_text(encoding="utf-8")
            at = text.index(selector)
            block = text[at: text.index("}", at)]
            self.assertIn("rotate(45deg)", block, f"{name} {selector}")
            self.assertIn("border-width", block, f"{name} {selector}")
            self.assertIn("currentColor", block, f"{name} {selector}")


# R8. The Compare instructions screen tells the researcher what the last run of each group was.
def setUpModule():
    rf.demo_run()


class R8LastRunPerGroup(Case):
    def _age(self, run_id, seconds_ago):
        cfg = self.p.path(f"runs/{run_id}/config.json")
        t = cfg.stat().st_mtime - seconds_ago
        os.utime(cfg, (t, t))

    def test_no_runs_answers_an_empty_list(self):
        self.assertEqual(self.ok("GET", "/api/runs/last"), {"last": []})

    def test_latest_run_of_a_group_with_its_state(self):
        rf.place_run(self.p, "A-0a1b2c3d", view=False)
        rf.place_run(self.p, "A-1b2c3d4e", view=False)
        self._age("A-1b2c3d4e", 3600)                      # so A-0a1b2c3d is the later start
        (self.p.path("runs/A-0a1b2c3d/logs/run.manifest.json")).unlink()   # ... and it never finished
        doc = self.ok("GET", "/api/runs/last")
        self.assertEqual(doc, {"last": [{"group": "A", "run_id": "A-0a1b2c3d", "finished": False}]})

    def test_finished_state_follows_the_manifest(self):
        rf.place_run(self.p, "A-0a1b2c3d", view=False)
        doc = self.ok("GET", "/api/runs/last")
        self.assertEqual(doc["last"], [{"group": "A", "run_id": "A-0a1b2c3d", "finished": True}])

    def test_nothing_ai_derived_and_no_reveal(self):
        rf.place_run(self.p, "A-0a1b2c3d", view=False)
        self.open_b_coding()
        doc = self.ok("GET", "/api/runs/last")
        self.assertEqual(set(doc["last"][0]), {"group", "run_id", "finished"})
        self.assertEqual(self.reveals(), 0)


if __name__ == "__main__":
    unittest.main()
