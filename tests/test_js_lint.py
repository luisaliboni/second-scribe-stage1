"""JS safety lint (plan Task 27 and Global Constraints): the viewer's scripts are classic scripts that put data
into the page through textContent and attributes only.

Rules, applied to every file under second_scribe/app/static/js/ (comments included, so a rule cannot be
dodged by hiding in one):
  unsafe DOM and code sinks   innerHTML, outerHTML, insertAdjacentHTML, document.write(ln), createContextualFragment,
                              parseFromString, srcdoc, eval(, new Function, Function(, string timers
  no module syntax            import, export (classic scripts attach to window.SS)
  CSP                         no style attribute (positions go through element.style.*), no on* handler attributes,
                              no javascript: URLs
  no external URLs            no http:// or https:// anywhere under static/ (fonts/SOURCES.md and OFL-*.txt excepted)
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "second_scribe" / "app" / "static"
JS_DIR = STATIC / "js"

# (rule name, pattern). A hit anywhere in a file is a failure.
JS_RULES = (
    ("innerHTML", re.compile(r"innerHTML")),
    ("outerHTML", re.compile(r"outerHTML")),
    ("insertAdjacentHTML", re.compile(r"insertAdjacentHTML")),
    ("document.write", re.compile(r"document\s*\.\s*write(ln)?\b")),
    ("createContextualFragment", re.compile(r"createContextualFragment")),
    ("parseFromString", re.compile(r"parseFromString")),
    ("srcdoc", re.compile(r"srcdoc", re.I)),
    ("eval", re.compile(r"\beval\s*\(")),
    ("new Function", re.compile(r"\bnew\s+Function\b")),
    ("Function(", re.compile(r"(?<![\w.$])Function\s*\(")),
    ("string timer", re.compile(r"\bset(Timeout|Interval)\s*\(\s*[\"'`]")),
    ("import", re.compile(r"(^|[^\w.$])import(\s|\()")),
    ("export", re.compile(r"(^|[^\w.$])export\s")),
    ("style attribute", re.compile(r"setAttribute\s*\(\s*[\"']style[\"']", re.I)),
    ("on* handler attribute", re.compile(r"setAttribute\s*\(\s*[\"']on[a-z]+[\"']", re.I)),
    ("javascript: url", re.compile(r"javascript\s*:", re.I)),
)

EXTERNAL_URL = re.compile(r"https?://", re.I)
TEXT_SUFFIXES = {".js", ".css", ".html", ".json", ".md", ".txt", ".svg"}


def js_hits(text: str) -> list[str]:
    return [name for name, pat in JS_RULES if pat.search(text)]


def external_url_files() -> list[str]:
    bad = []
    for f in sorted(STATIC.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = f.relative_to(STATIC).as_posix()
        if rel == "fonts/SOURCES.md" or (rel.startswith("fonts/OFL-") and rel.endswith(".txt")):
            continue
        if EXTERNAL_URL.search(f.read_text(encoding="utf-8")):
            bad.append(rel)
    return bad


class TestJsSafetyLint(unittest.TestCase):
    def test_scans_every_js_file(self):
        files = sorted(JS_DIR.rglob("*.js"))
        self.assertGreaterEqual(len(files), 12, "the viewer has at least twelve scripts")
        names = {f.name for f in files}
        for need in ("core.js", "layout.js", "encoding.js", "a11y.js", "lanes.js", "state.js", "views.js",
                     "ledger.js", "profile.js", "strip.js", "drawer.js", "viewer.js"):
            self.assertIn(need, names)
        # nothing hides in a subfolder or behind another suffix
        listed = sorted(p.name for p in JS_DIR.iterdir() if p.is_file())
        self.assertEqual(sorted(f.name for f in files), listed, "only .js files live under static/js/")

    def test_no_unsafe_dom_apis(self):
        bad = {}
        for f in sorted(JS_DIR.rglob("*.js")):
            hits = js_hits(f.read_text(encoding="utf-8"))
            if hits:
                bad[f.relative_to(STATIC).as_posix()] = hits
        self.assertEqual(bad, {}, f"unsafe APIs in the viewer scripts: {bad}")

    def test_no_external_urls(self):
        self.assertEqual(external_url_files(), [], "http:// or https:// under static/")

    # The detector itself: each rule fires on a line that breaks it, and spares the look-alikes.
    def test_detector_catches_each_rule(self):
        broken = {
            "innerHTML": "el.innerHTML = x;",
            "outerHTML": "n.outerHTML = x;",
            "insertAdjacentHTML": "n.insertAdjacentHTML('beforeend', x);",
            "document.write": "document.write(x);",
            "createContextualFragment": "r.createContextualFragment(x);",
            "parseFromString": "new DOMParser().parseFromString(x, 'text/html');",
            "srcdoc": "frame.srcdoc = x;",
            "eval": "var a = eval(x);",
            "new Function": "var f = new Function('return 1');",
            "Function(": "var f = Function('return 1');",
            "string timer": "setTimeout('go()', 10);",
            "import": "import { a } from './a.js';",
            "export": "export default a;",
            "style attribute": "n.setAttribute('style', 'left: 3px');",
            "on* handler attribute": "n.setAttribute('onclick', 'go()');",
            "javascript: url": "a.href = 'javascript:alert(1)';",
        }
        self.assertEqual(set(broken), {name for name, _ in JS_RULES}, "a rule without a probe")
        for name, line in broken.items():
            with self.subTest(rule=name):
                self.assertIn(name, js_hits(line))

    def test_detector_spares_look_alikes(self):
        fine = (
            "n.textContent = x;",
            "n.setAttribute('class', 'a');",
            "n.style.left = '3px';",
            "setTimeout(function () { go(); }, 10);",
            "var reimport = 1; var exported = 2;",
            "SS.export_names = [];",
            "var typeFunction = 1; if (isFunction(x)) { go(); }",
            "window.SS.importers = {};",
        )
        for line in fine:
            with self.subTest(line=line):
                self.assertEqual(js_hits(line), [])

    def test_external_url_detector(self):
        self.assertTrue(EXTERNAL_URL.search("src='https://cdn.example.com/x.js'"))
        self.assertTrue(EXTERNAL_URL.search("url(http://fonts.example/x.woff2)"))
        self.assertFalse(EXTERNAL_URL.search("href='../css/base.css'"))


if __name__ == "__main__":
    unittest.main()
