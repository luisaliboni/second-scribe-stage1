"""Wording gate (plan V1 and V14, SPEC v5 section 6): no user-visible text may carry a forbidden term.

Task 1 started it over strings/en.json. Task 43 extends it to the full SPEC v5 section 6 never-list and to every
other user-visible surface: INSTALL.md, DATAFLOW.md, the README's app section, pages/*.html and the string literals
in the static JS. Reviewed hits live in tests/wording_allow.txt, one reason each.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EN = ROOT / "second_scribe" / "app" / "strings" / "en.json"
APP = ROOT / "second_scribe" / "app"
ALLOW = ROOT / "tests" / "wording_allow.txt"
DOCS = ("INSTALL.md", "DATAFLOW.md")
README_START, README_END = "<!-- app-section:start -->", "<!-- app-section:end -->"

# Whole-word matching for words that occur inside harmless longer words ("first" in "firstly").
WORD_TERMS = ("first", "radical", "wild")
# Any word that BEGINS with the term (regex \bscript\w*, case-insensitive; coordinator 18 Oct): "scripted",
# "scripts" and "scripting" are caught, while "description", "descriptive" and "transcript" are not.
PREFIX_TERMS = ("script",)
# One narrow, named allowance to the rule above: DECISIONS L67 (22 Oct, council 23 D8) adopted "the model's
# no-interview script" and "the other family's no-interview script" for the kit and the paper, and the later decision
# wins. Only these exact phrases are stripped before the prefix scan; "script" alone, "scripted", "scripts" and every
# other "script..." word stay refused, as do "official script" and "pull toward the script".
ALLOWED_SCRIPT_PHRASES = re.compile(r"no-interview scripts?\b", re.IGNORECASE)
SUBSTRING_TERMS = (
    # SPEC v5 section 6, the "Never" list, in its order
    "validated",
    "ground truth",
    "replicable",
    "erase",
    "fabrication",
    "the ai audits",
    "found what the interviewers missed",
    "the interviewer erred",
    "neither map is right",
    "interviews are redundant",
    "the model already knew",
    "adds little",
    "federated network",
    "federated science",
    "a standard every paper should report",
    "certificate",
    "our simulator tests autism policy",
    "synthetic families can stand in",
    "ai loses the connections",
    "monoculture",
    "leaderboard",
    "low recall survives calibration",
    "covers most",
    "the official script",
    "preserves absences",
    # plan V1 and the spec's wording rules
    "official script",
    "pull toward the script",
    "second-order invisibility",
    "entry test",
    "correct the ai",
    "fix the ai",
    "the first tool",
    "first to",
    # The export is labelled "Profile for comparison" (wording rule V14).
    "profile for pooling",
    # plan V1: the 365 reads "not attested by its own quote"
    "no support in the transcript",
)
# DECISIONS L36: the ladder compares readings; it does not rank them. "better" is refused on the ladder and the
# instructions screen only (setup.models.model_hint_api says a pinned id is "better than an alias", about a model
# id, and stays). Scope: en.json keys under "instructions." or with "ladder" or "rung" in the key, and the string
# literals of the instructions screen's script.
LADDER_ONLY_TERMS = (r"\bbetter\b",)
LADDER_PREFIXES = ("instructions.",)
LADDER_KEY = re.compile(r"ladder|rung")
INSTRUCTIONS_JS = APP / "static" / "js" / "app-instructions.js"
BY_DEFAULT_CLAIM = "nothing leaves your machine"
# In the documents, "certificate" is allowed only as the name of the document ("run certificate"); any claim of
# validity ("certified", "certifies", "certification") stays banned.
DOC_CERTIFICATE_OK = "run certificate"
CERTIFY = re.compile(r"\bcertif(?:y|ies|ied|ying|ication)\b", re.IGNORECASE)


def find_hits(text: str, *, doc: bool = False) -> list[str]:
    """The forbidden terms in ``text``. ``doc=True`` is for the documents: the name "run certificate" is allowed,
    and "nothing leaves your machine" must be qualified by "by default" in the same sentence."""
    low = re.sub(r"\s+", " ", text.lower())      # a line break may split a phrase
    hits = [t for t in SUBSTRING_TERMS if t in low and not (doc and t == "certificate")]
    if doc:
        if re.search(r"(?<!run )certificate", low):
            hits.append("certificate")
        hits += ["certify" for _ in CERTIFY.findall(text)][:1]
        for m in re.finditer(re.escape(BY_DEFAULT_CLAIM), low):
            sentence = re.split(r"[.!?\n]", low[: m.start()])[-1]
            if "by default" not in sentence:
                hits.append("nothing leaves your machine (without 'by default')")
    hits += [t for t in WORD_TERMS if re.search(rf"\b{re.escape(t)}\b", low)]
    scanned = ALLOWED_SCRIPT_PHRASES.sub(" ", text)   # DECISIONS L67 (council 23 D8): the one allowed phrase
    hits += [t for t in PREFIX_TERMS if re.search(rf"\b{re.escape(t)}\w*", scanned, re.IGNORECASE)]
    return hits


def all_js_literals(source: str) -> list[str]:
    """Every string literal of a script, comments dropped, none skipped (``js_user_strings`` skips a run of
    lower-case words as a class list, which would let a lower-case "better" through)."""
    src = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    src = re.sub(r"(?m)^\s*//.*$|(?<=[;{}, \t])//[^\n'\"]*$", "", src)
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _LITERAL.finditer(src)]


def ladder_hits(text: str) -> list[str]:
    """The ladder-only terms in ``text`` (case-insensitive, whole words)."""
    return [t for t in LADDER_ONLY_TERMS if re.search(t, text, re.IGNORECASE)]


class TestWording(unittest.TestCase):
    def test_no_forbidden_terms_in_strings(self):
        from second_scribe.app.strings import load_strings
        data = load_strings("en")   # en.json merged with the kit's profile_strings.json when present
        bad = {k: find_hits(v) for k, v in data.items() if find_hits(v)}
        self.assertEqual(bad, {}, f"forbidden wording in en.json: {bad}")

    def test_ladder_and_instructions_strings_never_rank(self):
        """V6 (DECISIONS L36): no "better" in the ladder or instructions strings, nor in the instructions screen JS."""
        from second_scribe.app.strings import load_strings
        data = load_strings("en")
        scoped = {k: v for k, v in data.items() if k.startswith(LADDER_PREFIXES) or LADDER_KEY.search(k)}
        self.assertGreater(len(scoped), 60, "the scope matched too few strings")
        self.assertTrue(any(k.startswith("instructions.rung3") for k in scoped))
        self.assertTrue(any("rung" in k and not k.startswith("instructions.") for k in scoped))
        bad = {k: ladder_hits(v) for k, v in scoped.items() if ladder_hits(v)}
        self.assertEqual(bad, {}, f'"better" in the ladder or instructions strings: {bad}')
        js = [x for x in all_js_literals(INSTRUCTIONS_JS.read_text(encoding="utf-8")) if ladder_hits(x)]
        self.assertEqual(js, [], 'a "better" literal in app-instructions.js')

    def test_ladder_term_is_scoped_and_caught(self):
        for bad in ("A better rung", "BETTER", "which is better, the AI or the interviewer"):
            with self.subTest(text=bad):
                self.assertTrue(ladder_hits(bad))
        for ok in ("betterment", "Compare the readings", "a bettor"):
            with self.subTest(text=ok):
                self.assertEqual(ladder_hits(ok), [])
        # outside the scope the word is not a finding
        from second_scribe.app.strings import load_strings
        data = load_strings("en")
        self.assertIn("better", data["setup.models.model_hint_api"])
        self.assertEqual(find_hits(data["setup.models.model_hint_api"]), [])

    def test_detector_catches_each_term(self):
        for t in SUBSTRING_TERMS + WORD_TERMS + PREFIX_TERMS:
            with self.subTest(term=t):
                self.assertTrue(find_hits(f"x {t.upper()} y"), t)

    def test_word_boundary_spares_longer_words(self):
        for ok in ("description", "descriptive", "Descriptive only", "firstly", "transcript", "transcripts",
                   "manuscript", "postscript"):
            with self.subTest(text=ok):
                self.assertEqual(find_hits(ok), [])

    def test_any_word_beginning_script_is_caught(self):
        # coordinator, 18 Oct: \bscript\w*, case-insensitive (scripted, scripts, Scripting, SCRIPT...)
        for bad in ("script", "scripted run", "its scripted share", "Scripts", "SCRIPTING", "the script's",
                    "(scripted)"):
            with self.subTest(text=bad):
                self.assertIn("script", find_hits(bad))

    def test_no_interview_script_is_the_one_allowed_phrase(self):
        # DECISIONS L67 (council 23 D8): "no-interview script" and "no-interview scripts" pass, in any case,
        # including inside the possessive forms the kit uses.
        for ok in ("no-interview script", "no-interview scripts", "the model's no-interview script",
                   "the other family's no-interview script", "Pull toward the model's No-Interview Script",
                   "match a no-interview script."):
            with self.subTest(text=ok):
                self.assertEqual(find_hits(ok), [])

    def test_allowance_does_not_widen_the_script_ban(self):
        for bad in ("the script", "scripted", "the no-interview scripted share", "no-interview scripting",
                    "no interview script", "an interview script", "the no-interview script and the script",
                    "the no-interview script is scripted", "official script", "pull toward the script"):
            with self.subTest(text=bad):
                self.assertTrue(find_hits(bad), bad)


if __name__ == "__main__":
    unittest.main()


class TestKitProfileStrings(unittest.TestCase):
    """PROFILE-SCHEMA §13: the kit's profile_strings.json is merged into the app strings, never copied."""

    def _with(self, data):
        import tempfile
        from unittest import mock
        from second_scribe.app import strings
        d = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(d.cleanup)
        f = Path(d.name) / "profile_strings.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        patch = mock.patch.object(strings, "KIT_PROFILE_STRINGS", f)
        patch.start()
        self.addCleanup(patch.stop)
        return strings

    def test_merged(self):
        s = self._with({"profile.reason.needs_j2": "Needs a second judge family."})
        self.assertEqual(s.load_strings("en")["profile.reason.needs_j2"], "Needs a second judge family.")

    def test_collision_refused(self):
        s = self._with({"profile.reason.x": "a"})
        own = json.loads(EN.read_text(encoding="utf-8"))
        with mock_own(s, {**own, "profile.reason.x": "b"}):
            with self.assertRaises(ValueError):
                s.load_strings("en")

    def test_wrong_prefix_refused(self):
        s = self._with({"app.name": "x"})
        with self.assertRaises(ValueError):
            s.load_strings("en")


def mock_own(strings_mod, data):
    """Serve ``data`` as en.json for the duration of the block."""
    import contextlib
    import tempfile
    from unittest import mock

    @contextlib.contextmanager
    def cm():
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d:
            (Path(d) / "en.json").write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(strings_mod, "_DIR", Path(d)):
                yield
    return cm()


# ---------------------------------------------------------------------------------------------------------------
# Task 43: every other user-visible surface, the documents, and the agreement between INSTALL.md and the site.

_LITERAL = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\"""")
_CLASS_TOKEN = re.compile(r"[a-z0-9_-]+$")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def read_allow(path: Path = ALLOW) -> list[tuple[str, str, str]]:
    """Reviewed hits: ``path<TAB>snippet<TAB>reason`` per line; ``#`` starts a comment line."""
    rows = []
    if not path.is_file():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValueError(f"{path.name} line {n}: expected path<TAB>snippet<TAB>reason")
        rows.append((parts[0].strip(), parts[1], parts[2].strip()))
    return rows


def without_allowed(rel: str, text: str) -> str:
    for path, snippet, _reason in read_allow():
        if path == rel:
            text = text.replace(snippet, " ")
    return text


def js_user_strings(source: str) -> list[str]:
    """The string literals of a classic script that can reach a reader: comments dropped; a one-word literal is an
    identifier or a string-table key (the table is scanned as en.json), and a list of lower-case hyphenated words is
    a class list; everything else counts."""
    src = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    src = re.sub(r"(?m)^\s*//.*$|(?<=[;{}, \t])//[^\n'\"]*$", "", src)
    out = []
    for m in _LITERAL.finditer(src):
        s = m.group(1) if m.group(1) is not None else m.group(2)
        words = s.split()
        if len(words) < 2 or all(_CLASS_TOKEN.match(w) for w in words) or s == "use strict":
            continue
        out.append(s)
    return out


def html_user_text(source: str) -> str:
    """The text a reader can see in an HTML page or fragment: text nodes plus alt, title, aria-label, placeholder
    and meta content; comments, tags and {{slots}} are left out."""
    from html.parser import HTMLParser

    bits: list[str] = []

    class P(HTMLParser):
        def handle_data(self, data):
            bits.append(data)

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k in ("alt", "title", "aria-label", "placeholder") or (tag == "meta" and k == "content"):
                    bits.append(v or "")

    P().feed(re.sub(r"\{\{[^}]*\}\}", " ", source))
    return "\n".join(bits)


def readme_app_section() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    if text.count(README_START) != 1 or text.count(README_END) != 1:
        raise AssertionError(f"README.md must hold {README_START} and {README_END} exactly once")
    a, b = text.index(README_START), text.index(README_END)
    if a > b:
        raise AssertionError("README app section markers are in the wrong order")
    return text[a + len(README_START):b]


def readme_prose() -> str:
    """The whole README as prose (audit fix C, V5): fenced and indented command blocks and `code spans` are
    dropped, because code identifiers may keep the kit's `script` names (plan V1)."""
    text = HTML_COMMENT.sub("", (ROOT / "README.md").read_text(encoding="utf-8"))
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    return "\n".join(line for line in text.splitlines() if not line.startswith("       "))


def doc_surfaces() -> dict[str, str]:
    """{label: text} for every document surface the lint covers; comments (the DATAFLOW citations) are not text."""
    out = {name: HTML_COMMENT.sub("", (ROOT / name).read_text(encoding="utf-8")) for name in DOCS}
    out["README.md (app section)"] = HTML_COMMENT.sub("", readme_app_section())
    return out


def static_surfaces() -> dict[str, str]:
    out = {}
    for f in sorted((APP / "pages").glob("*.html")) + [APP / "static" / "app.html"]:
        rel = f.relative_to(ROOT).as_posix()
        out[rel] = without_allowed(rel, html_user_text(f.read_text(encoding="utf-8")))
    for f in sorted((APP / "static" / "js").glob("*.js")):
        rel = f.relative_to(ROOT).as_posix()
        out[rel] = without_allowed(rel, "\n".join(js_user_strings(f.read_text(encoding="utf-8"))))
    for f in sorted((APP / "static" / "templates").glob("*.csv")):
        rel = f.relative_to(ROOT).as_posix()
        out[rel] = without_allowed(rel, f.read_text(encoding="utf-8"))
    return out


class TestWordingSurfaces(unittest.TestCase):
    def test_pages_and_static_js_are_clean(self):
        bad = {k: find_hits(v) for k, v in static_surfaces().items() if find_hits(v)}
        self.assertEqual(bad, {}, f"forbidden wording on the site or in the static scripts: {bad}")

    def test_documents_are_clean(self):
        surfaces = {}
        for name, text in doc_surfaces().items():
            rel = "README.md" if name.startswith("README") else name
            surfaces[name] = without_allowed(rel, text)
        bad = {k: find_hits(v, doc=True) for k, v in surfaces.items() if find_hits(v, doc=True)}
        self.assertEqual(bad, {}, f"forbidden wording in the documents: {bad}")

    def test_whole_readme_prose_is_clean(self):
        # V5: not only the app section; the opening lines said "erases" and "official script"
        text = without_allowed("README.md", readme_prose())
        # The ordinal "first" of the kit's technical sections ("the first request", "the first spec") is not a priority
        # claim; the app section above is still scanned for it.
        hits = [h for h in find_hits(text, doc=True) if h != "first"]
        self.assertEqual(hits, [], "forbidden wording in README.md prose")
        self.assertIn("the no-interview map", text)
        self.assertIn("a map of the official pathway (prompt quoted)", re.sub(r"\s+", " ", text))
        self.assertNotRegex(text.lower(), r"\berase")

    def test_every_allowed_hit_has_a_reason_and_still_occurs(self):
        rows = read_allow()
        for rel, snippet, reason in rows:
            with self.subTest(path=rel, snippet=snippet):
                self.assertTrue(reason)
                f = ROOT / rel
                if rel == "README.md":
                    text = readme_app_section()
                elif f.suffix == ".js":
                    text = "\n".join(js_user_strings(f.read_text(encoding="utf-8")))
                elif f.suffix == ".html":
                    text = html_user_text(f.read_text(encoding="utf-8"))
                else:
                    text = f.read_text(encoding="utf-8")
                self.assertIn(snippet, HTML_COMMENT.sub("", text) if f.suffix == ".md" else text,
                              "a stale entry in tests/wording_allow.txt")

    def test_profile_for_pooling_is_refused_on_every_surface(self):
        # plan V14 (DECISIONS L36): the export is "Profile for comparison". Case and spacing do not matter.
        for phrase in ("Profile for pooling", "PROFILE FOR POOLING", "profile for\npooling"):
            with self.subTest(phrase=phrase):
                self.assertIn("profile for pooling", find_hits(phrase, doc=True))
        self.assertIn("profile for pooling", find_hits("Profile for pooling"))
        # the neighbours the coordinator allows stay allowed
        for ok in ("Labs pool profiles, never interviews", "a pooled point"):
            with self.subTest(phrase=ok):
                self.assertEqual(find_hits(ok, doc=True), [])

    def test_the_surfaces_are_really_scanned(self):
        # a scan that finds no files proves nothing
        s = static_surfaces()
        self.assertTrue(any(k.endswith("site-install.js") for k in s))
        self.assertTrue(any(k.endswith("install.html") for k in s))
        self.assertEqual(sorted(doc_surfaces()), ["DATAFLOW.md", "INSTALL.md", "README.md (app section)"])

    def test_js_extractor_keeps_sentences_and_drops_identifiers(self):
        src = ("var a = 'script'; var b = 'ss-item ss-lane-'; // 'a script comment'\n"
               "var c = \"Pull toward the script\"; /* 'the official script' */ var d = 'use strict';")
        self.assertEqual(js_user_strings(src), ["Pull toward the script"])

    def test_html_extractor_reads_text_and_attributes_not_tags(self):
        src = '<!-- title_key: x --><script src="js/a.js"></script><p title="A scripted tip">Hello {{t:k}}</p>'
        text = html_user_text(src)
        self.assertIn("A scripted tip", text)
        self.assertNotIn("a.js", text)
        self.assertEqual(find_hits(html_user_text('<script src="js/a.js"></script><p>Plain</p>')), [])


class TestDocTerms(unittest.TestCase):
    def test_certificate_is_allowed_only_as_the_name_of_the_document(self):
        self.assertEqual(find_hits("The run certificate lists the hashes.", doc=True), [])
        self.assertIn("certificate", find_hits("The certificate shows the study is sound.", doc=True))
        self.assertIn("certificate", find_hits("run certificate", doc=False))      # strings: banned outright
        for bad in ("This certifies the result.", "The run is certified.", "A certification of validity."):
            with self.subTest(text=bad):
                self.assertTrue(find_hits(bad, doc=True), bad)

    def test_nothing_leaves_needs_by_default(self):
        self.assertEqual(find_hits("By default nothing leaves your machine.", doc=True), [])
        self.assertEqual(find_hits("Runs on your computer. By default nothing leaves your machine.", doc=True), [])
        self.assertTrue(find_hits("Nothing leaves your machine.", doc=True))
        self.assertTrue(find_hits("By default it is local. Nothing leaves your machine.", doc=True))

    def test_the_rest_of_the_never_list_is_caught(self):
        for phrase in ("the AI audits the interviewers", "the interviewer erred", "Neither map is right", "adds little",
                       "a leaderboard", "monoculture", "a radical idea", "wild results", "covers most cases",
                       "the model already knew it", "preserves absences", "a standard every paper should report",
                       "federated science", "the first tool", "validated", "ground truth", "replicable",
                       "an erase", "fabrication", "correct the AI", "fix the AI"):
            with self.subTest(phrase=phrase):
                self.assertTrue(find_hits(phrase, doc=True), phrase)


def fenced_lines(text: str) -> list[str]:
    """Every line inside a ``` fenced block, stripped; empty lines left out."""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside and line.strip():
            out.append(line.strip())
    return out


class TestInstallAgreesWithTheSite(unittest.TestCase):
    """INSTALL.md and the site's Install page (static/js/site-install.js, strings install.cmd.*) give the same commands,
    byte for byte. The strings are the site's source of truth."""

    @classmethod
    def setUpClass(cls):
        from second_scribe.app.strings import load_strings
        cls.strings = load_strings("en")
        cls.cmds = {k: v for k, v in cls.strings.items() if k.startswith("install.cmd.")}
        cls.install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
        cls.docs = {**{n: (ROOT / n).read_text(encoding="utf-8") for n in DOCS},
                    "README.md (app section)": readme_app_section()}

    def test_every_site_command_is_a_line_of_a_code_block_in_install(self):
        lines = set(fenced_lines(self.install))
        for key, cmd in self.cmds.items():
            with self.subTest(key=key):
                self.assertIn(cmd, lines, f"INSTALL.md must show {cmd!r} on a line of its own in a code block")

    def test_the_commands_the_site_names_are_the_ones_the_docs_name(self):
        start, win = self.cmds["install.cmd.start"], self.cmds["install.cmd.start_windows"]
        self.assertEqual((start, win), ("python3 -m second_scribe.app", "py -m second_scribe.app"))
        launch = re.compile(r"(?:python3?|py)\s+-m\s+second_scribe[. ]app\b")
        for name, text in self.docs.items():
            for found in launch.findall(text):
                with self.subTest(doc=name, command=found):
                    self.assertIn(found, (start, win))
        self.assertIn(start, self.docs["INSTALL.md"])
        self.assertIn(win, self.docs["INSTALL.md"])
        self.assertIn(start, self.docs["README.md (app section)"])

    def test_the_start_step_says_where_to_run_it(self):
        # item 21 (23 Oct): run outside the kit folder, the start command says "No module named second_scribe"; the
        # site and INSTALL.md both say where to run it, show a cd line per OS and say what that message means
        cds = (self.cmds["install.cmd.cd"], self.cmds["install.cmd.cd_windows"])
        for cd in cds:
            self.assertTrue(cd.startswith("cd ") and cd.endswith("second-scribe-stage1"), cd)
        lines = fenced_lines(self.install)
        for cd, start in zip(cds, ("install.cmd.start", "install.cmd.start_windows")):
            i = lines.index(cd)
            self.assertEqual(lines[i + 1], self.cmds[start], "the cd line comes right before the start command")
        for key in ("install.step.start.where", "install.step.start.where.windows"):
            self.assertIn("README.md", self.strings[key])
            self.assertIn("kit folder", self.strings[key])
        self.assertIn('"No module named second_scribe"', self.strings["install.step.start.no_module"])
        self.assertIn('"No module named second_scribe"', self.install)
        self.assertIn("kit folder, the one that holds `README.md`", self.install)

    def test_models_are_the_two_examples_and_are_called_examples(self):
        pulls = {self.cmds["install.cmd.pull_gemma"], self.cmds["install.cmd.pull_qwen"]}
        self.assertEqual(pulls, {"ollama pull gemma3:12b", "ollama pull qwen3:8b"})
        for name, text in self.docs.items():
            for found in re.findall(r"ollama pull \S+", text):
                with self.subTest(doc=name, command=found):
                    self.assertIn(found, pulls)
        self.assertRegex(self.install, r"(?i)examples and carry no endorsement")   # council 30: no "X, not Y"
        self.assertIn("LM Studio", self.install)

    def test_no_cloud_value_is_one_everywhere(self):
        for name, text in self.docs.items():
            for m in re.finditer(r"OLLAMA_NO_CLOUD(?:=|\s+)\"?([^\s\"`]+)", text):
                with self.subTest(doc=name, at=m.group(0)):
                    self.assertEqual(m.group(1), "1")
        self.assertIn("OLLAMA_NO_CLOUD=1", self.install)
        for key in ("install.cmd.no_cloud_macos_app", "install.cmd.no_cloud_serve", "install.cmd.no_cloud_windows"):
            self.assertRegex(self.cmds[key], r"OLLAMA_NO_CLOUD(?:=|\s+)1(?:\s|$)")

    def test_python_minimum_matches_the_site_and_the_package(self):
        self.assertIn("3.10", self.strings["install.intro"])
        self.assertIn("3.10", self.install)
        self.assertRegex((ROOT / "pyproject.toml").read_text(encoding="utf-8"), r'requires-python\s*=\s*">=\s*3\.10"')

    def test_the_site_links_to_files_that_exist(self):
        js = (APP / "static" / "js" / "site-install.js").read_text(encoding="utf-8")
        links = re.findall(r"'\.\./([A-Z]+\.md)'", js)
        self.assertEqual(sorted(links), ["DATAFLOW.md", "INSTALL.md"])
        for name in links:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_the_start_flags_install_names_exist(self):
        from second_scribe.app.__main__ import build_parser
        help_text = build_parser().format_help()
        for flag in re.findall(r"`(--[a-z-]+)`", self.install):
            with self.subTest(flag=flag):
                self.assertIn(flag, help_text)


# --- DATAFLOW.md: every claim cites code, and the figures it quotes are the code's own -------------------------

CITE = re.compile(r"<!--\s*cite:\s*(.*?)\s*-->", re.DOTALL)


def cite_problem(ref: str) -> str | None:
    """None when ``path:Name`` (or ``path:Class.method``) names a file of the kit and a def, class or assignment in it."""
    path, sep, name = ref.strip().partition(":")
    if not sep or not name.strip():
        return f"{ref!r}: write path:name"
    f = ROOT / path
    if not f.is_file():
        return f"{ref!r}: no such file"
    text = f.read_text(encoding="utf-8")
    for part in name.strip().split("."):
        if not re.search(rf"(?:^|\s)(?:def|class)\s+{re.escape(part)}\b|^\s*{re.escape(part)}\s*[:=]", text, re.MULTILINE):
            return f"{ref!r}: {part} is not defined in {path}"
    return None


class TestDataflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (ROOT / "DATAFLOW.md").read_text(encoding="utf-8")
        cls.text = re.sub(r"\s+", " ", HTML_COMMENT.sub("", cls.raw))
        cls.cites = [r.strip() for m in CITE.finditer(cls.raw) for r in m.group(1).split(",") if r.strip()]

    def test_every_citation_names_a_real_file_and_definition(self):
        self.assertGreaterEqual(len(self.cites), 20)
        bad = [p for p in map(cite_problem, self.cites) if p]
        self.assertEqual(bad, [])

    def test_the_four_sources_the_task_names_are_cited(self):
        joined = " ".join(self.cites)
        for needle in ("llm.py:guard_real_clients", "anthropic_client.py:", "app/server.py:", "app/api.py:Secrets",
                       "certificate.py:"):
            with self.subTest(needle=needle):
                self.assertIn(needle, joined)

    def test_it_says_what_leaves_by_default(self):
        self.assertIn("By default nothing leaves your machine", self.text)

    def test_the_csp_lines_are_the_servers_own(self):
        from second_scribe.app import build_site, server
        self.assertIn(server.CSP, self.text)
        self.assertIn(build_site.CSP, self.text)

    def test_remote_section_matches_the_guard(self):
        from second_scribe import llm, toolcfg
        self.assertEqual(llm.REMOTE_SCHEMES, ("https",))
        self.assertEqual(sorted(toolcfg.PROVIDERS), ["anthropic", "openai_compat"])
        low = self.text.lower()
        for needle in ("https", "allow-list", "declaration", "memory", "role", "host", "provider"):
            with self.subTest(needle=needle):
                self.assertIn(needle, low)
        for p in toolcfg.PROVIDERS:
            self.assertIn(p, self.text)

    def test_the_certificate_section_quotes_what_it_attests(self):
        from second_scribe import certificate
        self.assertIn(re.sub(r"\s+", " ", certificate.ATTESTS), self.text)
        self.assertIn(re.sub(r"\s+", " ", certificate.REMOTE_LINE), self.text)
        self.assertIn("integrity", self.text.lower())

    def test_the_export_is_called_profile_for_comparison_and_the_floor_is_the_codes(self):
        from second_scribe import profile_export
        self.assertIn("Profile for comparison", self.text)
        self.assertIn(f"fewer than {profile_export.K_FLOOR} people", self.text)

    def test_the_steward_check_has_its_commands(self):
        for needle in ("lsof -iTCP -sTCP:LISTEN", 'grep -rE "https?://" --include',
                       "shasum -a 256", "Get-FileHash", "-m second_scribe.cli certificate",
                       "Enhanced spell check", "extension"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)


def steward_grep_argv() -> list[str]:
    """The steward's "no outside address" command, read out of DATAFLOW.md (the fenced line that starts with grep -rE
    "https?://"), split as a shell would, so the test runs the document's own words."""
    import shlex
    raw = (ROOT / "DATAFLOW.md").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith('grep -rE "https?://"')]
    if len(lines) != 1:
        raise AssertionError(f"DATAFLOW.md must hold exactly one 'grep -rE \"https?://\"' line, found {len(lines)}")
    return shlex.split(lines[0])


class TestStewardGrep(unittest.TestCase):
    """Audit fix C, S7: the one-line check comes back empty on a clean tree, and says why licence and source files
    that name their origin are outside its scope."""

    def run_grep(self, argv: list[str], cwd: Path):
        import shutil
        import subprocess
        if shutil.which("grep") is None:
            self.skipTest("grep is not installed")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    def test_the_document_scopes_the_grep_to_scripts_styles_and_pages(self):
        argv = steward_grep_argv()
        self.assertIn("--include=*.js", argv)
        self.assertIn("--include=*.css", argv)
        self.assertIn("--include=*.html", argv)
        self.assertNotIn("--include=*.md", argv)
        self.assertTrue(any(a.startswith("second_scribe/app/") for a in argv))

    def test_it_finds_nothing_over_the_tree(self):
        r = self.run_grep(steward_grep_argv(), ROOT)
        self.assertEqual((r.returncode, r.stdout), (1, ""), f"an outside address in the app's files:\n{r.stdout}")

    def test_it_would_find_an_address_in_a_script_a_style_or_a_page(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d:
            base = Path(d)
            (base / "js").mkdir()
            for name, body in (("js/a.js", 'fetch("https://example.org/x")'), ("b.css", "@import url(http://example.org/c.css);"),
                               ("c.html", '<script src="https://example.org/d.js"></script>')):
                (base / name).write_text(body, encoding="utf-8")
            # the files the document says are out of scope hold addresses too, and must not show up
            (base / "SOURCES.md").write_text("source: https://example.org/fonts\n", encoding="utf-8")
            (base / "OFL-X.txt").write_text("http://scripts.sil.org/OFL\n", encoding="utf-8")
            argv = [a for a in steward_grep_argv() if not a.startswith("second_scribe/")] + ["."]
            r = self.run_grep(argv, base)
        hit_files = sorted({line.split(":", 1)[0] for line in r.stdout.splitlines()})
        self.assertEqual(hit_files, ["./b.css", "./c.html", "./js/a.js"])

    def test_the_document_says_why_licence_and_source_files_name_their_origin(self):
        text = re.sub(r"\s+", " ", HTML_COMMENT.sub("", (ROOT / "DATAFLOW.md").read_text(encoding="utf-8")))
        self.assertRegex(text, r"licence and source files name where they come from")
        self.assertIn("grep -rE \"https?://\" second_scribe/app/static", text)   # the unscoped command, for the curious


class TestReadmeAppSection(unittest.TestCase):
    def test_section_exists_and_points_at_the_documents(self):
        s = readme_app_section()
        for needle in ("python3 -m second_scribe.app", "INSTALL.md", "DATAFLOW.md", "By default nothing leaves your machine",
                       "Profile for comparison"):
            with self.subTest(needle=needle):
                self.assertIn(needle, s)


class TestNoSessionMessage(unittest.TestCase):
    """Item 23 (23 Oct): a tab without a session says what to do in plain words, with no product name."""

    def test_the_message_says_copy_the_newest_link_into_this_tab(self):
        en = json.loads((ROOT / "second_scribe" / "app" / "strings" / "en.json").read_text(encoding="utf-8"))
        for key in ("server.no_session", "server.open_link"):
            with self.subTest(key=key):
                text = en[key]
                self.assertIn("This tab is not connected to the app.", text)
                self.assertIn("copy the newest link and open it in this tab", text)
                self.assertIn("prints a new link each time one is used", text)
                self.assertNotRegex(text, r"(?i)second scribe|no session|—")
