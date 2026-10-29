"""Task 30 (plan V4, site contract): the study page and the Install page, Python side.

The page logic is pinned in tests/js/study.test.js. Here: the fragments' header comments, the shipped files they
list, nothing synthetic on the study page, the exact Install commands, the as-run reading-3 prompt quoted from the
strings file, and (when T28's build_site is on the branch) the built study page and its data file.

Dev harness: SS_WRITE_STUDY_DEV_PAGE=1 python3 -m unittest tests.test_study_page writes
static/dev/study.html, static/dev/install.html, static/dev/data/study.js (the public profile v3 through profile_to_js's
escaping) and static/dev/data/strings.study.js. Dev files are never shipped (build_site skips static/dev/).
"""
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
PAGES = APP / "pages"
STATIC = APP / "static"
EN = APP / "strings" / "en.json"
FIXTURE = ROOT / "tests" / "fixtures" / "profiles" / "profile-v3.json"
SYNTH_PROFILE = ROOT / "tests" / "fixtures" / "profiles" / "profile-synth.json"
_PUBLIC = Path(os.environ.get("SS_ANALYSIS_PUBLIC", "/nonexistent"))
# the analysis copy, when set: the study's public profile (v4, reading 3 at temperature 0), shipped as profile.json
REAL = _PUBLIC / "profile.json"
# the declared sha256 of that file (the one constant for it)
V4_SHA = "40b1976a62f6f40c218701bb2ac629de4a0176cbc7087e7a5f3c158c3bfb9c25"
# the test fixture: the frozen v3 profile with its reading-3 cells replaced by the v4 cells (temperature 0). Not the
# real v3 file (20ec3796...), which carries the temperature-0.7 reading-3 point (council 23 fix 4).
PROFILE_SHA = "618341e4973da4014a9b20bd1fe7cbc5a69221709eb31408a1b05592060f54dd"
PROMPT = ROOT / "prompts" / "prior_only.p0.v2.txt"
DATA_FILES = {"data/strings.js", "data/study.js"}
HEADER_RE = re.compile(r"^<!-- (title_key|scripts|styles): ([^<>]*?) -->$")
HAVE_BUILD = importlib.util.find_spec("second_scribe.app.build_site") is not None


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header(name: str) -> tuple[dict, str]:
    lines = (PAGES / name).read_text(encoding="utf-8").splitlines()
    head = {}
    i = 0
    while i < len(lines) and lines[i].startswith("<!--"):
        m = HEADER_RE.match(lines[i])
        if m is None:
            raise AssertionError(f"{name}: bad header line {lines[i]!r}")
        head[m.group(1)] = m.group(2).split()
        i += 1
    return head, "\n".join(lines[i:])


def unescape_braces(text: str) -> str:
    return text.replace("{{", "{").replace("}}", "}")


class TestFragments(unittest.TestCase):
    def test_header_comments_and_listed_files(self):
        for name, own_js in (("study.html", "js/site-study.js"), ("install.html", "js/site-install.js")):
            with self.subTest(page=name):
                head, body = header(name)
                self.assertEqual(sorted(head), ["scripts", "styles", "title_key"])
                self.assertEqual(len(head["title_key"]), 1)
                self.assertEqual(head["scripts"][0], "data/strings.js", "strings load before any script")
                self.assertEqual(head["scripts"][-1], own_js)
                for f in head["scripts"] + head["styles"]:
                    if f in DATA_FILES:
                        continue
                    self.assertTrue((STATIC / f).is_file(), f)
                    self.assertFalse(f.startswith("dev/"), f)
                    self.assertNotRegex(f, r"^js/(app-|api\.js)")
                self.assertNotIn("css/site.css", head["styles"], "the shell adds site.css")

    def test_fragments_are_body_only_with_no_inline_code_and_no_text(self):
        for name in ("study.html", "install.html"):
            with self.subTest(page=name):
                _, body = header(name)
                low = body.lower()
                for bad in ("<html", "<head", "<body", "<script", "<style", " style=", "<meta", "<link"):
                    self.assertNotIn(bad, low)
                self.assertNotRegex(low, r"\son[a-z]+\s*=", "no inline handlers (CSP)")
                self.assertNotRegex(low, r"https?://")
                # every visible word comes from en.json through JS: the fragment holds containers only
                text = re.sub(r"<[^>]*>", " ", body)
                self.assertEqual(text.split(), [], f"text in {name}: {text.split()[:8]}")

    def test_study_page_no_synthetic(self):
        head, body = header("study.html")
        files = head["scripts"] + head["styles"]
        self.assertFalse([f for f in files if "tryit" in f or "synth" in f or "testpattern" in f], files)
        self.assertNotIn("synthetic", body.lower())
        for f in (STATIC / "js" / "site-study.js", STATIC / "css" / "site-study.css"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("--synthetic", text, f.name)
            self.assertNotIn("band.synthetic", text, f.name)
            self.assertNotIn("tryit", text, f.name)
            self.assertNotRegex(text, r"\.synthetic\b|class[^\n]*['\"]synthetic['\"]", f.name)


class TestStrings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.en = json.loads(EN.read_text(encoding="utf-8"))

    def test_install_commands_exact(self):
        en = self.en
        self.assertEqual(en["install.cmd.start"], "python3 -m second_scribe.app")
        self.assertEqual(en["install.cmd.start_windows"], "py -m second_scribe.app")
        self.assertEqual(en["install.cmd.python_version"], "python3 --version")
        self.assertEqual(en["install.cmd.python_version_windows"], "py --version")
        self.assertEqual(en["install.cmd.no_cloud_serve"], "OLLAMA_NO_CLOUD=1 ollama serve")
        self.assertEqual(en["install.cmd.no_cloud_macos_app"], "launchctl setenv OLLAMA_NO_CLOUD 1")
        self.assertEqual(en["install.cmd.no_cloud_windows"], "setx OLLAMA_NO_CLOUD 1")
        self.assertEqual(en["install.cmd.pull_gemma"], "ollama pull gemma3:12b")
        self.assertEqual(en["install.cmd.pull_qwen"], "ollama pull qwen3:8b")
        for k, v in en.items():
            if k.startswith("install.cmd."):
                self.assertNotRegex(v, r"https?://", k)
                self.assertEqual(v, v.strip(), k)

    def test_reading3_prompt_is_the_as_run_file(self):
        quoted = unescape_braces(self.en["study.reading3.prompt"])
        self.assertEqual(quoted, PROMPT.read_text(encoding="utf-8").rstrip("\n"))
        self.assertTrue(sha(PROMPT).startswith("9b48f863"))
        doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(doc["run"]["reading3_prompt"]["sha256"], sha(PROMPT))
        # every brace is doubled, so SS.t(key, {}) prints the file and fills no slot
        self.assertNotRegex(re.sub(r"\{\{|\}\}", "", self.en["study.reading3.prompt"]), r"[{}]")

    def test_own_keys_only_study_and_install(self):
        own = [k for k in self.en if k.split(".")[0] in ("study", "install")]
        self.assertGreater(len(own), 40)


class TestFixture(unittest.TestCase):
    def test_fixture_is_v3_with_temperature_0_reading3_values(self):
        self.assertEqual(sha(FIXTURE), PROFILE_SHA)
        self.assertEqual(json.loads(FIXTURE.read_text(encoding="utf-8"))["run"]["settings"]["reading3_temperature"], 0.0)

    @unittest.skipUnless(REAL.is_file(), "set SS_ANALYSIS_PUBLIC to the analysis public folder")
    def test_real_file_unchanged(self):
        self.assertEqual(sha(REAL), V4_SHA)


def _tryit_input(tmp: Path) -> Path:
    from second_scribe import export_view
    from second_scribe.app import testpattern
    out = tmp / "tryit.js"
    out.write_text(export_view.view_to_js(testpattern.make_test_pattern(drift=False), "tryit"), encoding="utf-8")
    return out


def _data_doc(js: str):
    m = re.match(r'^window\.SS_DATA = window\.SS_DATA \|\| \{\}; window\.SS_DATA\["study"\] = (.*);\s*$', js, re.S)
    if m is None:
        raise AssertionError('study.js must set window.SS_DATA["study"] (site contract 3-4)')
    return json.loads(m.group(1))


@unittest.skipUnless(HAVE_BUILD, "build_site (T28) not on this branch yet")
class TestBuiltStudyPage(unittest.TestCase):
    def build(self, study):
        from second_scribe.app.build_site import build_site
        d = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(d.cleanup)
        tmp = Path(d.name)
        out = tmp / "site"
        build_site(out, tryit=_tryit_input(tmp), tryit_profile=SYNTH_PROFILE, study=study)
        return out

    def test_built_with_study(self):
        out = self.build(FIXTURE)
        page = (out / "study.html").read_text(encoding="utf-8")
        self.assertIn('src="data/study.js"', page)
        self.assertIn('src="js/site-study.js"', page)
        self.assertNotIn("tryit", page)
        self.assertNotRegex(page, r"class=\"[^\"]*synthetic")
        doc = _data_doc((out / "data" / "study.js").read_text(encoding="utf-8"))
        self.assertEqual(doc, json.loads(FIXTURE.read_text(encoding="utf-8")))
        sources = json.loads((out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
        self.assertEqual(sources["study"]["sha256"], PROFILE_SHA)
        self.assertTrue((out / "install.html").is_file())
        self.assertIn('src="js/site-install.js"', (out / "install.html").read_text(encoding="utf-8"))
        for f in ("js/site-study.js", "js/site-install.js", "css/site-study.css", "css/site-install.css"):
            self.assertTrue((out / f).is_file(), f)
        self.assertFalse((out / "dev").exists())

    def test_built_stub_when_no_study(self):
        out = self.build(None)
        self.assertEqual(_data_doc((out / "data" / "study.js").read_text(encoding="utf-8")), {"state": "not_yet_run"})


# ------------------------------------------------------------------------------------------- dev harness
DEV_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<!-- Generated by tests/test_study_page.py (SS_WRITE_STUDY_DEV_PAGE=1). Dev only, never shipped. -->
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/fonts.css">
<link rel="stylesheet" href="../css/base.css">
{styles}
<style>
  /* a stand-in for T28's shell header (site.css is T28's) */
  .dev-shell {{ display: flex; gap: 18px; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--rule);
    font-family: var(--sans); font-size: 14px; color: var(--muted); flex-wrap: wrap; }}
  .dev-shell strong {{ color: var(--navy); font-family: var(--serif); font-size: 17px; }}
  .dev-main {{ max-width: 1180px; margin: 0 auto; padding: 24px 16px 40px; }}
</style>
<script>
  // #theme=dark|light and #state=<file state> for screenshots
  (function () {{
    var h = String(location.hash || '').replace(/^#/, '').split('&');
    window.SS_DEV = {{}};
    h.forEach(function (kv) {{ var p = kv.split('='); if (p[0]) window.SS_DEV[p[0]] = p[1] || ''; }});
    if (window.SS_DEV.theme) document.documentElement.setAttribute('data-theme', window.SS_DEV.theme);
  }})();
</script>
</head>
<body>
<header class="dev-shell"><strong>What did listening add?</strong><span>Explain</span><span>The study</span><span>Try it</span><span>Install</span><span>(dev shell)</span></header>
<main class="dev-main">
{fragment}
</main>
{scripts}
</body>
</html>
"""

DEV_STATE = """<script>
  // dev only: swap the data file for a state the build can produce, without any other data
  (function () {
    var s = window.SS_DEV && window.SS_DEV.state;
    if (!s) return;
    var real = window.SS_DATA.study;
    if (s === 'not_yet_run') window.SS_DATA.study = { state: 'not_yet_run' };
    else if (s === 'none') delete window.SS_DATA.study;
    else if (s === 'unreadable') window.SS_DATA.study = { schema: 'orgsci-profile/v9', state: 'reported', rows: [] };
    else window.SS_DATA.study = Object.assign({}, real, { state: s });
  })();
</script>"""


DEV_SCROLL = """<script>
  // dev only: #at=<element id> hides what comes before that panel (headless screenshots ignore scrolling), so a
  // screenshot starts at the panel. Runs on DOMContentLoaded, after the page script's own listener has drawn.
  document.addEventListener('DOMContentLoaded', function () {
    var at = window.SS_DEV && window.SS_DEV.at;
    var t = at && document.getElementById(at);
    if (!t) return;
    var hide = function (n) { if (n) n.style.display = 'none'; };
    hide(document.querySelector('.study-head'));
    hide(document.querySelector('.study-nav'));
    for (var c = t.previousElementSibling; c; c = c.previousElementSibling) hide(c);
  });
</script>"""


def write_dev_pages() -> list[Path]:
    from second_scribe.app.profile_view import load_profile, profile_to_js
    from second_scribe.app.strings import load_strings

    dev = STATIC / "dev"
    (dev / "data").mkdir(parents=True, exist_ok=True)
    body = profile_to_js(load_profile(FIXTURE), "SS_DEV_STUDY").split(" = ", 1)[1]
    study_js = dev / "data" / "study.js"
    study_js.write_text('window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["study"] = ' + body + "\n",
                        encoding="utf-8")
    strings_js = dev / "data" / "strings.study.js"
    strings_js.write_text("window.SS_STRINGS = " + json.dumps(load_strings("en"), ensure_ascii=False, sort_keys=True)
                          + ";\n", encoding="utf-8")
    written = [study_js, strings_js]
    for name, title in (("study.html", "Study page dev harness"), ("install.html", "Install page dev harness")):
        head, fragment = header(name)
        styles = "\n".join(f'<link rel="stylesheet" href="../{s}">' for s in head["styles"])
        tags = []
        for s in head["scripts"]:
            if s == "data/strings.js":
                tags.append('<script src="data/strings.study.js"></script>')
            elif s == "data/study.js":
                tags.append('<script src="data/study.js"></script>')
                tags.append(DEV_STATE)
            else:
                tags.append(f'<script src="../{s}"></script>')
        tags.append(DEV_SCROLL)
        page =DEV_SHELL.format(title=title, styles=styles, fragment=fragment.strip(), scripts="\n".join(tags))
        out = dev / name
        out.write_text(page, encoding="utf-8")
        written.append(out)
    return written


@unittest.skipUnless(os.environ.get("SS_WRITE_STUDY_DEV_PAGE") == "1", "set SS_WRITE_STUDY_DEV_PAGE=1 to write")
class TestWriteDevPages(unittest.TestCase):
    def test_write(self):
        for p in write_dev_pages():
            self.assertTrue(p.is_file(), p)


if __name__ == "__main__":
    unittest.main()
