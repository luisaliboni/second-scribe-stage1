"""Task 28: ``make app`` builds the offline site (pages, shell, CSP meta, Try it) from the static files.

Contract: ``app/build/site-contract.md`` (T28 / T29 / T30). Page fragments live in ``second_scribe/app/pages``; the
build wraps them in ``pages/shell.html``. Inputs here are the shippable test pattern (an oracle run, no model), the
dev-only drifting file (which must be refused), the recorded Try-it run replayed from ``demo/recorded`` (replay
client, no model) and byte copies of the two public profiles (``tests/fixtures/profiles``).
"""
import hashlib
import html as _html
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from second_scribe import cli, export_view
from second_scribe.app import build_site as bs
from second_scribe.app import strings, testpattern
from second_scribe.pipeline import RunConfig

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
PAGES = APP / "pages"
STATIC = APP / "static"
DEV_TRYIT = STATIC / "dev" / "data" / "tryit.js"
PROFILES = ROOT / "tests" / "fixtures" / "profiles"
SYNTH_PROFILE = PROFILES / "profile-synth.json"
STUDY_PROFILE = PROFILES / "profile-v3.json"
RECORDED = ROOT / "demo" / "recorded"
DEMO_CONFIG = ROOT / "configs" / "demo.json"
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"

CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; connect-src \'none\'; '
       'img-src \'self\' data:; font-src \'self\'; style-src \'self\'; script-src \'self\'">')
PAGE_FILES = ("index.html", "study.html", "try.html", "install.html")

_PATTERN_JS: Path | None = None


def html_escape(text: str) -> str:
    return _html.escape(text, quote=True)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pattern_js() -> Path:
    """The shippable test pattern (oracle demo run, no drift) as a view_to_js file, built once per test run."""
    global _PATTERN_JS
    if _PATTERN_JS is None:
        d = Path(tempfile.mkdtemp())
        with redirect_stdout(io.StringIO()):
            view = testpattern.make_test_pattern(drift=False)
        _PATTERN_JS = d / "tryit.js"
        _PATTERN_JS.write_text(export_view.view_to_js(view, "tryit"), encoding="utf-8")
    return _PATTERN_JS


def build(out: Path | None = None, **kw) -> Path:
    out = out or Path(tempfile.mkdtemp()) / "site"
    kw.setdefault("tryit", pattern_js())
    kw.setdefault("tryit_profile", SYNTH_PROFILE)
    kw.setdefault("study", None)
    bs.build_site(out, **kw)
    return out


def data_payload(path: Path) -> tuple[str, object]:
    """(var, payload) of a ``window.SS_DATA["var"] = ...;`` file."""
    m = re.fullmatch(r'window\.SS_DATA = window\.SS_DATA \|\| \{\}; window\.SS_DATA\["([A-Za-z_][A-Za-z0-9_]*)"\] = '
                     r"(.*);", path.read_text(encoding="utf-8").strip(), re.S)
    if not m:
        raise AssertionError(f"{path.name} is not an SS_DATA file")
    return m.group(1), json.loads(m.group(2))


def tree(out: Path) -> dict[str, bytes]:
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def pages_without(*names: str) -> Path:
    """A copy of the real pages folder with the named fragments removed (T29/T30 may or may not be merged)."""
    d = Path(tempfile.mkdtemp()) / "pages"
    shutil.copytree(PAGES, d)
    for n in names:
        (d / f"{n}.html").unlink(missing_ok=True)
    return d


class TestBaseline(unittest.TestCase):
    def test_demo_hash_unchanged(self):
        self.assertEqual(RunConfig.load(str(DEMO_CONFIG)).sha256(), DEMO_HASH)


class TestRefusals(unittest.TestCase):
    def test_build_refuses_dev_only_view(self):
        out = Path(tempfile.mkdtemp()) / "site"
        with self.assertRaises(ValueError) as cm:
            bs.build_site(out, tryit=DEV_TRYIT, tryit_profile=SYNTH_PROFILE, study=None)
        self.assertEqual(str(cm.exception), "dev_only view data cannot be shipped")
        self.assertFalse(out.exists() and any(out.iterdir()), "nothing may be written on a refusal")

    def test_normal_build_has_no_dev_folder(self):
        out = build()
        self.assertFalse((out / "dev").exists())
        self.assertEqual([p for p in out.rglob("*") if "dev" in p.relative_to(out).parts], [])

    def test_refuses_a_view_that_is_not_synthetic(self):
        d = Path(tempfile.mkdtemp())
        _var, view = data_payload_view(pattern_js())
        view["source"] = "own"
        p = d / "tryit.js"
        p.write_text(export_view.view_to_js(view, "tryit"), encoding="utf-8")
        with self.assertRaises(ValueError):
            build(d / "site", tryit=p)
        self.assertFalse((d / "site").exists())

    def test_refuses_a_file_that_is_not_a_view_js(self):
        d = Path(tempfile.mkdtemp())
        p = d / "tryit.js"
        p.write_text('{"schema": "view-data/1"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            build(d / "site", tryit=p)

    def test_refuses_a_non_synthetic_tryit_profile(self):
        with self.assertRaises(ValueError):
            build(tryit_profile=STUDY_PROFILE)      # state reported: real and synthetic never share a page

    def test_refuses_a_synthetic_study_profile(self):
        with self.assertRaises(ValueError):
            build(study=SYNTH_PROFILE)

    def test_refuses_a_non_empty_out_that_is_not_a_site_build(self):
        d = Path(tempfile.mkdtemp())
        (d / "keep.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(ValueError):
            build(d)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["keep.txt"])

    def test_rebuild_prunes_stale_files(self):
        out = build()
        (out / "js" / "stale.js").write_text("x", encoding="utf-8")
        build(out)
        self.assertFalse((out / "js" / "stale.js").exists())


def data_payload_view(path: Path):
    from second_scribe.replay_check import parse_view_js
    return parse_view_js(path.read_text(encoding="utf-8"))


class TestPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build()

    def page(self, name: str) -> str:
        return (self.out / name).read_text(encoding="utf-8")

    def test_four_pages_written(self):
        for name in PAGE_FILES:
            self.assertTrue((self.out / name).is_file(), name)

    def test_pages_have_exact_csp_meta(self):
        for name in PAGE_FILES:
            html = self.page(name)
            self.assertEqual(html.count(CSP), 1, name)
            self.assertEqual(len(re.findall(r"http-equiv", html, re.I)), 1, name)
            self.assertLess(html.index(CSP), html.index("</head>"), name)
            self.assertTrue(html.startswith("<!doctype html>\n"), name)

    def test_no_inline_script_or_style(self):
        for name in PAGE_FILES:
            html = self.page(name)
            for tag in re.findall(r"<script\b[^>]*>", html, re.I):
                self.assertRegex(tag, r'\bsrc="[^"]+"', f"{name}: inline script {tag}")
            self.assertEqual(re.findall(r"<script\b[^>]*>([^<]+)</script>", html, re.I), [], name)
            self.assertNotRegex(html, r"(?i)<style\b", name)
            self.assertNotRegex(html, r"(?i)\sstyle\s*=", name)
            self.assertNotRegex(html, r"(?i)\son[a-z]+\s*=", name)
            self.assertNotRegex(html, r"(?i)javascript:", name)

    def test_every_local_reference_exists_and_nothing_is_remote(self):
        for name in PAGE_FILES:
            html = self.page(name)
            self.assertNotRegex(html, r"(?i)https?://", name)
            for ref in re.findall(r'(?:src|href)="([^"#]+)', html):
                self.assertTrue((self.out / ref).is_file(), f"{name}: {ref}")

    def test_nav_marks_the_current_page_only(self):
        for name in PAGE_FILES:
            html = self.page(name)
            current = re.findall(r'<a href="([^"]+)" aria-current="page"', html)
            self.assertEqual(current, [name], name)
            for target in PAGE_FILES:
                self.assertIn(f'href="{target}"', html, f"{name} links {target}")
            for key in ("nav.explain", "nav.study", "nav.try", "nav.install"):
                self.assertIn(strings.t(key), html)

    def test_theme_toggle_and_shell_script_in_head(self):
        for name in PAGE_FILES:
            html = self.page(name)
            head = html[:html.index("</head>")]
            self.assertIn('<script src="js/site-shell.js"></script>', head, name)
            self.assertRegex(html, r'<button type="button" id="site-theme"[^>]*aria-pressed="false"', name)

    def test_shell_styles_in_order(self):
        html = self.page("try.html")
        links = re.findall(r'<link rel="stylesheet" href="([^"]+)">', html)
        self.assertEqual(links[:4], ["css/fonts.css", "css/tokens.css", "css/base.css", "css/site.css"])

    def test_try_page_loads_data_then_viewer_in_order(self):
        srcs = re.findall(r'<script src="([^"]+)"></script>', self.page("try.html"))
        want = ["js/site-shell.js", "data/strings.js", "data/tryit.js", "data/tryit-profile.js", "js/core.js",
                "js/layout.js", "js/encoding.js", "js/a11y.js", "js/lanes.js", "js/views.js", "js/state.js",
                "js/ledger.js", "js/strip.js", "js/profile.js", "js/drawer.js", "js/viewer.js",
                "js/glossary.js", "js/site-try.js"]  # item 32: the info marker, before the page's own script
        self.assertEqual(srcs, want)
        self.assertIn('id="tryit-viewer"', self.page("try.html"))

    def test_titles_from_title_key(self):
        self.assertIn(f"<title>{strings.t('nav.try')} · {strings.t('app.name')}</title>", self.page("try.html"))

    def test_static_files_copied_from_the_allow_list(self):
        for rel in ("fonts/SchibstedGrotesk-Variable-latin.woff2", "fonts/OFL-SchibstedGrotesk.txt", "css/tokens.css", "css/site.css",
                    "js/viewer.js", "js/site-try.js", "js/site-shell.js"):
            self.assertEqual((self.out / rel).read_bytes(), (STATIC / rel).read_bytes(), rel)
        self.assertFalse((self.out / "app.html").exists())
        self.assertFalse((self.out / "specimen.html").exists())

    def test_tryit_bytes_copied(self):
        self.assertEqual((self.out / "data" / "tryit.js").read_bytes(), pattern_js().read_bytes())

    def test_strings_file(self):
        text = (self.out / "data" / "strings.js").read_text(encoding="utf-8")
        m = re.fullmatch(r"window\.SS_STRINGS = (.*);", text.strip(), re.S)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1)), strings.load_strings("en"))

    def test_tryit_profile_file(self):
        var, doc = data_payload(self.out / "data" / "tryit-profile.js")
        self.assertEqual(var, "tryit_profile")
        self.assertEqual(doc, json.loads(SYNTH_PROFILE.read_text(encoding="utf-8")))

    def test_tryit_oracle_state_and_test_pattern_label_both_shown(self):
        html = self.page("try.html")
        self.assertIn(strings.t("try.oracle_state"), html)
        _var, view = data_payload_view(self.out / "data" / "tryit.js")
        self.assertIs(view["test_pattern"], True)          # the viewer prints band.test_pattern from this flag
        self.assertEqual(view["run"]["models"]["recon"], "oracle")
        self.assertIn(strings.t("try.runs.none"), html)  # the test pattern has no recorded run

    def test_study_stub_when_absent(self):
        var, doc = data_payload(self.out / "data" / "study.js")
        self.assertEqual((var, doc), ("study", {"state": "not_yet_run"}))
        src = json.loads((self.out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
        self.assertIsNone(src["study"])
        self.assertEqual(src["other"]["study"]["kind"], "stub")

    def test_sources_json_hashes_match_inputs(self):
        src = json.loads((self.out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
        self.assertEqual(src["tryit"]["sha256"], sha(pattern_js()))
        self.assertIs(src["tryit"]["recorded"], False)
        self.assertEqual(src["tryit"]["runs"], [])
        self.assertIsNone(src["tryit"]["recorded_log_head"])
        self.assertEqual(src["other"]["tryit_profile"], {"sha256": sha(SYNTH_PROFILE), "kind": "profile_synthetic"})
        accounted = {"tryit.js": src["tryit"]["sha256"]}
        names = {"strings": "strings.js", "study": "study.js", "tryit_profile": "tryit-profile.js",
                 "explain": "explain.js"}
        for key, entry in src["other"].items():
            accounted[names[key]] = entry["sha256"]
        on_disk = {p.name: sha(p) for p in (self.out / "data").iterdir() if p.name != "SOURCES.json"}
        self.assertEqual(set(on_disk), set(accounted))
        for name, digest in on_disk.items():
            if name == "tryit-profile.js":
                continue                     # other.tryit_profile is the input profile's hash
            self.assertEqual(accounted[name], digest, name)
        self.assertNotIn("test_pattern", json.dumps(src["other"]))

    def test_placeholders_name_the_page(self):
        for name, key in (("study.html", "nav.study"), ("install.html", "nav.install"), ("index.html", "nav.explain")):
            if (PAGES / {"index.html": "explain.html"}.get(name, name)).exists():
                continue                     # the real fragment is merged; T29/T30 test it
            html = self.page(name)
            self.assertIn(strings.t("site.placeholder"), html)
            self.assertIn(f"<h1>{strings.t(key)}</h1>", html)


class TestFrontPage(unittest.TestCase):
    """A7: a standard build's front page is the Explain page, never the placeholder (a tester once got a build made
    from a branch without the Explain fragment and saw only "This page is not built yet.")."""
    def test_standard_build_front_page_is_explain_not_placeholder(self):
        out = build(study=STUDY_PROFILE)
        index = (out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(strings.t("site.placeholder"), index)
        self.assertNotIn("not built yet", index)
        self.assertEqual(len(re.findall(r"<main\b", index)), 1)
        self.assertEqual(index.count("</main>"), 1)
        fragment = (PAGES / "explain.html").read_text(encoding="utf-8")
        title_key = re.search(r"title_key:\s*(\S+)", fragment).group(1)
        self.assertIn(f"<title>{html_escape(strings.t(title_key))} ", index)
        self.assertIn('<script src="data/explain.js"></script>', index)
        self.assertTrue((out / "data" / "explain.js").is_file())
        for name in PAGE_FILES:
            self.assertNotIn(strings.t("site.placeholder"), (out / name).read_text(encoding="utf-8"), name)

    def test_placeholder_is_not_a_dead_end(self):
        out = build(pages_dir=pages_without("explain"))
        index = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn(strings.t("site.placeholder"), index)
        self.assertIn(f"<h1>{strings.t('nav.explain')}</h1>", index)         # names the page
        self.assertIn("Run `make app` in the kit folder to build this page.", strings.t("site.placeholder"))
        for target, key in (("study.html", "nav.study"), ("try.html", "nav.try"), ("install.html", "nav.install")):
            self.assertIn(f'<a href="{target}">{strings.t(key)}</a>', index.split("site-placeholder-pages", 1)[1], target)
        self.assertNotIn('<a href="index.html">', index.split("site-placeholder-pages", 1)[1])   # not a link to itself

    def test_placeholder_links_only_to_pages_that_exist(self):
        out = build(pages_dir=pages_without("explain", "study"))
        tail = (out / "index.html").read_text(encoding="utf-8").split("site-placeholder-pages", 1)[1]
        self.assertNotIn('href="study.html"', tail)
        self.assertIn('href="try.html"', tail)


class TestFragments(unittest.TestCase):
    def test_placeholder_for_each_missing_fragment(self):
        out = build(pages_dir=pages_without("explain", "study", "install"))
        for name in ("index.html", "study.html", "install.html"):
            html = (out / name).read_text(encoding="utf-8")
            self.assertIn(strings.t("site.placeholder"), html)
            self.assertEqual(html.count(CSP), 1)

    def test_fragment_scripts_and_styles_follow_the_header(self):
        d = pages_without("explain", "study", "install")
        (d / "install.html").write_text("<!-- title_key: nav.install -->\n<!-- scripts: data/strings.js js/core.js -->\n"
                                        "<!-- styles: css/viewer.css -->\n<h1>{{t:nav.install}}</h1>\n",
                                        encoding="utf-8")
        out = build(pages_dir=d)
        html = (out / "install.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<script src="([^"]+)"></script>', html),
                         ["js/site-shell.js", "data/strings.js", "js/core.js"])
        self.assertEqual(re.findall(r'<link rel="stylesheet" href="([^"]+)">', html)[-1], "css/viewer.css")
        self.assertIn(f"<h1>{strings.t('nav.install')}</h1>", html)

    def test_fragment_with_an_unshipped_script_is_refused(self):
        d = pages_without("explain", "study", "install")
        (d / "install.html").write_text("<!-- title_key: nav.install -->\n<!-- scripts: js/app-shell.js -->\n<p>x</p>\n",
                                        encoding="utf-8")
        out = Path(tempfile.mkdtemp()) / "site"
        with self.assertRaises(ValueError):
            build(out, pages_dir=d)
        self.assertFalse(out.exists())

    def test_fragment_with_inline_script_or_style_is_refused(self):
        for body in ("<script>var a = 1;</script>", "<style>p{}</style>", '<p style="color:red">x</p>',
                     '<button onclick="x()">x</button>'):
            d = pages_without("explain", "study", "install")
            (d / "install.html").write_text(f"<!-- title_key: nav.install -->\n{body}\n", encoding="utf-8")
            with self.assertRaises(ValueError, msg=body):
                build(pages_dir=d)

    def test_fragment_without_title_key_is_refused(self):
        d = pages_without("explain", "study", "install")
        (d / "install.html").write_text("<p>x</p>\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build(pages_dir=d)

    def test_unknown_string_key_is_refused(self):
        d = pages_without("explain", "study", "install")
        (d / "install.html").write_text("<!-- title_key: nav.install -->\n<p>{{t:no.such_key}}</p>\n", encoding="utf-8")
        with self.assertRaises(KeyError):
            build(pages_dir=d)


class TestStaticAllowList(unittest.TestCase):
    def test_allow_list(self):
        d = Path(tempfile.mkdtemp())
        for rel in ("fonts/a.woff2", "fonts/OFL-x.txt", "css/site.css", "css/app.css", "js/viewer.js",
                    "js/app-shell.js", "js/api.js", "app.html", "specimen.html", "dev/viewer.html",
                    "dev/data/tryit.js", "js/notes.txt", "templates/map-template.csv"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("x", encoding="utf-8")
        self.assertEqual(bs.static_files(d), ["css/app.css", "css/site.css", "fonts/OFL-x.txt", "fonts/a.woff2",
                                              "js/viewer.js"])


class TestStudy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = build(study=STUDY_PROFILE)
        cls.src = json.loads((cls.out / "data" / "SOURCES.json").read_text(encoding="utf-8"))

    def test_study_sha_equals_input_file(self):
        self.assertEqual(self.src["study"], {"sha256": sha(STUDY_PROFILE)})
        # the fixture: the frozen v3 profile with its reading-3 cells at temperature 0 (not the real v3 file, 20ec3796...)
        self.assertEqual(sha(STUDY_PROFILE), "618341e4973da4014a9b20bd1fe7cbc5a69221709eb31408a1b05592060f54dd")
        self.assertEqual(self.src["other"]["study"]["kind"], "study_public")
        self.assertEqual(self.src["other"]["study"]["sha256"], sha(self.out / "data" / "study.js"))

    def test_every_number_on_study_is_a_key(self):
        """Every number in study.js sits at a key path of the input file, with the same value (Task 30 extends
        this to the rendered page)."""
        var, doc = data_payload(self.out / "data" / "study.js")
        self.assertEqual(var, "study")
        src = json.loads(STUDY_PROFILE.read_text(encoding="utf-8"))

        def numbers(x, path=()):
            if isinstance(x, bool) or x is None or isinstance(x, str):
                return
            if isinstance(x, (int, float)):
                yield path, x
            elif isinstance(x, dict):
                for k, v in x.items():
                    yield from numbers(v, path + (k,))
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    yield from numbers(v, path + (i,))

        found = list(numbers(doc))
        self.assertTrue(found)
        for path, value in found:
            node = src
            for k in path:
                node = node[k]
            self.assertEqual(node, value, path)
        self.assertEqual(len(found), len(list(numbers(src))))

    def test_study_page_carries_no_synthetic_data(self):
        html = (self.out / "study.html").read_text(encoding="utf-8")
        self.assertNotIn("tryit", html)


class TestDeterminism(unittest.TestCase):
    def test_build_is_deterministic(self):
        a = build(study=STUDY_PROFILE)
        b = build(study=STUDY_PROFILE)
        self.assertEqual(tree(a), tree(b))


class TestExplainHook(unittest.TestCase):
    def test_explain_skipped_when_module_absent(self):
        try:
            import second_scribe.app.explain_data  # noqa: F401
        except ImportError:
            pass
        else:
            self.skipTest("explain_data is merged; T29's test covers explain.js")
        out = build()
        self.assertFalse((out / "data" / "explain.js").exists())
        src = json.loads((out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
        self.assertNotIn("explain", src["other"])


class TestCli(unittest.TestCase):
    def test_cli_builds_and_refuses(self):
        d = Path(tempfile.mkdtemp())
        with redirect_stdout(io.StringIO()):
            code = bs.main(["--out", str(d / "site"), "--tryit", str(pattern_js()),
                            "--tryit-profile", str(SYNTH_PROFILE)])
        self.assertEqual(code, 0)
        self.assertTrue((d / "site" / "try.html").is_file())
        err = io.StringIO()
        with redirect_stderr(err):
            code = bs.main(["--out", str(d / "other"), "--tryit", str(DEV_TRYIT), "--tryit-profile", str(SYNTH_PROFILE)])
        self.assertEqual(code, 2)
        self.assertIn("dev_only view data cannot be shipped", err.getvalue())

    def test_tryit_js_subcommand_writes_the_exporter_bytes(self):
        d = Path(tempfile.mkdtemp())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["demo", "--out", str(d / "run"), "--client", "oracle"]), 0)
        self.assertEqual(bs.main(["tryit-js", "--run", str(d / "run"), "--out", str(d / "tryit.js")]), 0)
        cfg = RunConfig.load(str(DEMO_CONFIG), out_dir=str(d / "run"))
        view = export_view.build_view(cfg, source="synthetic", reference_type=None)
        _var, shipped = data_payload_view(d / "tryit.js")
        self.assertEqual(shipped["run"]["chain_head"], view["run"]["chain_head"])
        self.assertEqual(export_view.view_to_js(shipped, "tryit"), (d / "tryit.js").read_text(encoding="utf-8"))
        # a relative --run (as the Makefile passes it) names the same run, not one next to the config
        rel = os.path.relpath(d / "run", Path.cwd())
        self.assertEqual(bs.main(["tryit-js", "--run", rel, "--out", str(d / "tryit-rel.js")]), 0)
        _var, again = data_payload_view(d / "tryit-rel.js")
        self.assertEqual(again["run"]["chain_head"], view["run"]["chain_head"])
        self.assertEqual(again["run"]["verify"]["status"], "passed")
        # an empty folder is refused, not exported as an empty view
        (d / "empty").mkdir()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(bs.main(["tryit-js", "--run", str(d / "empty"), "--out", str(d / "e.js")]), 2)
        self.assertFalse((d / "e.js").exists())


class TestMakeApp(unittest.TestCase):
    """``make -n app`` prints the recipe without running it. The recipe is one shell ``if``: the recorded branch, then
    ``else``, then the test-pattern branch."""
    @classmethod
    def setUpClass(cls):
        import subprocess
        out = subprocess.run(["make", "-n", "app"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        head, sep, tail = out.partition("else \\\n")
        assert sep, out
        cls.recorded, cls.pattern = head.splitlines(), tail.splitlines()

    def test_recorded_path_exports_the_profile_from_the_replay_and_ships_it(self):
        lines = self.recorded
        replay = next(i for i, ln in enumerate(lines) if "second_scribe.cli demo" in ln)
        prof = next(i for i, ln in enumerate(lines) if "build_site tryit-profile" in ln)
        site = next(i for i, ln in enumerate(lines) if "build_site --out site" in ln)
        self.assertLess(replay, prof)
        self.assertLess(prof, site)
        self.assertIn("--run build/tryit-run --out build/tryit-profile.json", lines[prof])
        self.assertIn("--tryit-profile build/tryit-profile.json", lines[site])
        self.assertNotIn("profile-synth.json", "\n".join(lines))

    def test_test_pattern_path_keeps_the_parity_copy(self):
        site = next(ln for ln in self.pattern if "build_site --out site" in ln)
        self.assertIn("--tryit-profile tests/fixtures/profiles/profile-synth.json", site)
        self.assertNotIn("tryit-profile --run", "\n".join(self.pattern))


@unittest.skipUnless((RECORDED / "demo-live-fixtures.v2.json").is_file(), "no recorded Try-it run")
class TestRecorded(unittest.TestCase):
    """The recorded Try-it run (demo/recorded), replayed with the replay client: no model is called."""
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        with redirect_stdout(io.StringIO()):
            code = cli.main(["demo", "--out", str(cls.d / "run"), "--client", "replay",
                             "--fixtures", str(RECORDED / "demo-live-fixtures.v2.json")])
        assert code == 0, code
        cls.tryit = bs.write_tryit_js(cls.d / "run", cls.d / "tryit.js")
        cls.profile = cls.d / "tryit-profile.json"
        with redirect_stdout(io.StringIO()):
            code = bs.main(["tryit-profile", "--run", str(cls.d / "run"), "--out", str(cls.profile)])
        assert code == 0, code
        cls.out = build(cls.d / "site", tryit=cls.tryit, tryit_profile=cls.profile, recorded=RECORDED)
        cls.src = json.loads((cls.out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads((RECORDED / "run.manifest.json").read_text(encoding="utf-8"))

    def test_sources_mark_recorded_and_list_the_shipped_replay(self):
        t = self.src["tryit"]
        _var, view = data_payload_view(self.out / "data" / "tryit.js")
        self.assertIs(t["recorded"], True)
        # runs: the replay whose data is shipped (equal to tryit.js); recorded_*: the recording it was checked against
        self.assertEqual(t["runs"], [{"run_id": view["run"]["run_id"], "chain_head": view["run"]["chain_head"],
                                      "records": view["run"]["verify"]["records"]}])
        self.assertEqual(t["recorded_log_head"], self.manifest["head_hash"])
        self.assertEqual(t["recorded_records"], self.manifest["record_count"])
        self.assertNotEqual(t["runs"][0]["chain_head"], t["recorded_log_head"])
        self.assertEqual(t["sha256"], sha(self.tryit))

    def test_footer_lists_the_shipped_replay_and_names_the_recording(self):
        html = (self.out / "try.html").read_text(encoding="utf-8")
        _var, view = data_payload_view(self.out / "data" / "tryit.js")
        _pvar, prof = data_payload(self.out / "data" / "tryit-profile.js")
        head = view["run"]["chain_head"]
        footer = html[html.index('class="try-runs"'):]
        # the footer's run head is the profile's run_chain_heads.head and tryit.js's chain head
        self.assertEqual(re.findall(r'<code class="site-hash">([0-9a-f]{64})</code>', footer), [head])
        self.assertEqual(head, prof["run"]["run_chain_heads"]["head"])
        n_replay = view["run"]["verify"]["records"]
        # the page's own run is named as the replay, with its record count (from the data) and its full head
        self.assertIn(f"This page&#x27;s run (the replay): {n_replay:,} log records, chain head", footer)
        self.assertEqual(prof["run"]["run_chain_heads"]["records"], n_replay)
        # the recording is a second, named object, with its own count and head, and the match is stated
        rec = self.src["tryit"]["recorded_log_head"]
        self.assertNotIn(rec, footer)
        want = strings.t("try.runs.checked").format(records=f"{self.src['tryit']['recorded_records']:,}",
                                                    head=rec[:8] + "\u2026")
        self.assertEqual(footer.count(want), 1)
        self.assertEqual(want, f"The original recording has {self.manifest['record_count']:,} log records and the chain head "
                               f"{self.manifest['head_hash'][:8]}\u2026; the replay matched it request by request, with no model call.")
        # the reason for the difference is given, and it is true of the data: three sittings, one start, summary and end each
        why = strings.t("try.runs.recorded").format(run_id=view["run"]["run_id"])
        self.assertEqual(footer.count(html_escape(why)), 1)
        self.assertIn("three sittings, one per phase", why)
        self.assertIn(view["run"]["run_id"], why)
        kinds = [json.loads(l)["record_type"] for l in (RECORDED / "run.jsonl").read_text(encoding="utf-8").splitlines()]
        replay = [json.loads(l)["record_type"] for l in
                  (self.d / "run" / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines()]
        for k in ("run_start", "audit_summary", "run_end"):
            self.assertEqual((kinds.count(k), replay.count(k)), (3, 1), k)
        self.assertEqual(len(kinds) - len(replay), 6)
        self.assertEqual(self.manifest["record_count"] - n_replay, 6)
        # every sentence on the three lines starts with a capital
        for key in ("try.runs.row", "try.runs.row_replay", "try.runs.checked", "try.runs.recorded"):
            self.assertTrue(strings.t(key)[0].isupper(), key)
        self.assertNotIn(strings.t("try.oracle_state"), html)
        self.assertNotIn(strings.t("try.runs.none"), html)

    def test_recorded_flag_refused_when_replay_check_fails(self):
        out = self.d / "bad-site"
        with self.assertRaises(ValueError) as cm:
            build(out, tryit=pattern_js(), recorded=RECORDED)       # the oracle pattern is not the recorded run
        self.assertIn("replay-check failed", str(cm.exception))
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
