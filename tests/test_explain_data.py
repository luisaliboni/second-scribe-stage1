"""Task 29 (plan V5): the Explain page's case data, built from the shipped synthetic SYN-OW03 files only.

The animation is an illustration, never a recorded result: reading 1 is the shipped endorsed map (the .vsdx),
the AI's map is the shipped gold map with the kit oracle's quotes, and the no-interview map is the kit oracle's
stand-in for the role. These tests pin where each piece comes from, that the page fragment follows the shared
site contract (app/build/site-contract.md), and that the dev harness and its data stay in step with the code.
"""
import inspect
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "second_scribe" / "app"
CORPUS = ROOT / "demo" / "corpus"
PAGE = APP / "pages" / "explain.html"
JS = APP / "static" / "js" / "site-explain.js"
CSS = APP / "static" / "css" / "site-explain.css"
DEV = APP / "static" / "dev"
EN = APP / "strings" / "en.json"
MAP_ID = "SYN-OW03"
SHIPPED = ("meta.json", f"transcripts/{MAP_ID}.docx", f"maps/{MAP_ID}.vsdx", f"gold/{MAP_ID}.json",
           f"gold/{MAP_ID}.concepts.json")


def _case():
    from second_scribe.app.explain_data import build_case
    return build_case(ROOT)


class TestBuildCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = _case()

    def test_shape(self):
        c = self.case
        self.assertEqual(c["schema"], "explain-case/1")
        self.assertEqual(c["map_id"], MAP_ID)
        self.assertIs(c["synthetic"], True)
        for k in ("domain", "role", "phases", "transcript", "reference", "ai", "mapping_talk", "no_interview", "sources"):
            self.assertIn(k, c)

    def test_phases_domain_role_from_meta(self):
        meta = json.loads((CORPUS / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(self.case["phases"], meta["phases"])
        self.assertEqual(self.case["domain"], meta["domain"])
        self.assertEqual(self.case["role"], meta["roles"]["owner"])

    def test_transcript_from_shipped_docx(self):
        from second_scribe.transcripts import parse_paragraphs, read_docx_text
        t = parse_paragraphs(read_docx_text(CORPUS / "transcripts" / f"{MAP_ID}.docx"), {"pt"}, MAP_ID,
                             interviewer_labels={"iv"})
        want = [{"speaker": x.speaker, "text": x.text} for x in t.turns]
        self.assertEqual(self.case["transcript"], want)
        self.assertEqual(len(want), 21)
        self.assertEqual({x["speaker"] for x in want}, {"participant", "interviewer"})
        self.assertFalse(any(x["text"].startswith("[") for x in want), "speaker labels are stripped")

    def test_reference_from_shipped_vsdx(self):
        from second_scribe.maps.readers import read_map
        from second_scribe.maps.vsdx import ExtractConfig
        g = read_map(CORPUS / "maps" / f"{MAP_ID}.vsdx", MAP_ID,
                     extract=ExtractConfig(phase_names=tuple(self.case["phases"])))
        boxes = [n for n in g.nodes if n.phase is not None]
        self.assertEqual([r["label"] for r in self.case["reference"]], [n.text for n in boxes])
        self.assertEqual([r["phase"] for r in self.case["reference"]], [n.phase for n in boxes])
        missing = [r["label"] for r in self.case["reference"] if r["missing"]]
        self.assertEqual(missing, [n.text for n in boxes if n.status == "wished"])
        self.assertEqual(missing, ["Bylaw officer"])

    def test_ai_from_shipped_gold(self):
        gold = json.loads((CORPUS / "gold" / f"{MAP_ID}.json").read_text(encoding="utf-8"))
        talk_only = set(gold["meta"]["mapping_talk_only"])
        kept = [n for n in gold["nodes"] if n["node_id"] not in talk_only]
        self.assertEqual([a["label"] for a in self.case["ai"]], [n["text"] for n in kept])
        self.assertEqual([a["phase"] for a in self.case["ai"]], [n["phase"] for n in kept])
        missing = [a["label"] for a in self.case["ai"] if a["missing"]]
        self.assertEqual(missing, [n["text"] for n in gold["nodes"] if n["status"] == "wished"])
        self.assertEqual(missing, ["enforcement officer"])

    def test_mapping_talk_is_removed_before_the_ai_reads(self):
        """Council 30 (MUST 1): the planted mapping_talk_only trap (g4, the interviewer's turn 6) is not on the AI's map,
        and its turn is listed as removed, as glossary.ai_map says; every AI box cites a participant turn."""
        gold = json.loads((CORPUS / "gold" / f"{MAP_ID}.json").read_text(encoding="utf-8"))
        self.assertEqual(gold["meta"]["mapping_talk_only"], ["g4"])
        self.assertNotIn("g4", [a["id"] for a in self.case["ai"]])
        self.assertNotIn("front desk staff member", [a["label"] for a in self.case["ai"]])
        self.assertEqual(self.case["mapping_talk"], [6])
        turn = self.case["transcript"][6]
        self.assertEqual(turn["speaker"], "interviewer")
        self.assertIn("Let me draw an arrow", turn["text"])
        self.assertEqual([a["label"] for a in self.case["ai"] if self.case["transcript"][a["turn"]]["speaker"] != "participant"], [])
        # the endorsed map keeps its Licensing clerk box (drawn by the interviewer, reviewed by the author)
        self.assertIn("Licensing clerk", [r["label"] for r in self.case["reference"]])

    def test_concepts_from_shipped_concepts_file(self):
        con = json.loads((CORPUS / "gold" / f"{MAP_ID}.concepts.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(r["concept"] for r in self.case["reference"]), sorted(con["endorsed"].values()))
        gold = json.loads((CORPUS / "gold" / f"{MAP_ID}.json").read_text(encoding="utf-8"))
        talk_only = set(gold["meta"]["mapping_talk_only"])
        self.assertEqual(sorted(a["concept"] for a in self.case["ai"]),
                         sorted(v for k, v in con["gold"].items() if k not in talk_only))

    def test_turn_links_name_the_concept(self):
        from second_scribe.synth.bank import CONCEPTS
        from second_scribe.textnorm import token_seq
        phrase = {c.cid: c.forms[1] for c in CONCEPTS}
        turns = self.case["transcript"]
        for lane in ("reference", "ai", "no_interview"):
            for box in self.case[lane]:
                with self.subTest(lane=lane, box=box["label"]):
                    hit = [i for i, t in enumerate(turns) if token_seq(phrase[box["concept"]]) in token_seq(t["text"])]
                    if box["turn"] is None:
                        self.assertEqual(hit, [])
                    else:
                        self.assertIn(box["turn"], hit)
        # every AI box is attested by a turn; the endorsed water-tank box is on the map, not in the transcript
        self.assertTrue(all(a["turn"] is not None for a in self.case["ai"]))
        self.assertEqual([r["label"] for r in self.case["reference"] if r["turn"] is None], ["Water tank approval"])

    def test_no_interview_is_the_kit_oracle_stand_in(self):
        from second_scribe.demo_oracle import OracleClient
        oracle = OracleClient(CORPUS, {"owner": self.case["role"]})
        msg = [{"role": "user", "content": f"Describe the typical pathway of a {self.case['role']}."}]
        ans = json.loads(oracle.complete(msg, temperature=0.0, seed=0, max_tokens=10).text)
        self.assertEqual([n["label"] for n in self.case["no_interview"]], [n["label"] for n in ans["nodes"]])
        self.assertEqual([n["phase"] for n in self.case["no_interview"]], [n["phase"] for n in ans["nodes"]])
        self.assertFalse(any(n.get("missing") for n in self.case["no_interview"]))

    def test_listening_added_is_not_empty(self):
        ni = {n["concept"] for n in self.case["no_interview"]}
        added = [a["label"] for a in self.case["ai"] if a["concept"] not in ni]
        self.assertTrue(added)

    def test_no_digit_in_any_shown_text(self):
        c = self.case
        texts = [c["domain"], c["role"], *c["phases"], *(t["text"] for t in c["transcript"])]
        for lane in ("reference", "ai", "no_interview"):
            texts += [b["label"] for b in c[lane]] + [b["phase"] for b in c[lane]]
        self.assertEqual([t for t in texts if re.search(r"\d", t)], [])

    def test_deterministic(self):
        from second_scribe.app.explain_data import case_to_js
        self.assertEqual(case_to_js(_case()), case_to_js(_case()))

    def test_case_to_js_format(self):
        from second_scribe.app.explain_data import case_to_js
        js = case_to_js(self.case)
        head = 'window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["explain"] = '
        self.assertTrue(js.startswith(head), js[:80])
        body = js[len(head):].rstrip()
        self.assertTrue(body.endswith(";"))
        self.assertEqual(json.loads(body[:-1]), self.case)
        self.assertNotIn("</", js, "no closing tag can end a script element early")

    def test_reads_only_the_shipped_files(self):
        from second_scribe.app.explain_data import build_case
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d:
            for rel in SHIPPED:
                dst = Path(d) / "demo" / "corpus" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(CORPUS / rel, dst)
            self.assertEqual(build_case(Path(d)), self.case)

    def test_sources_name_the_files(self):
        src = self.case["sources"]
        for rel in SHIPPED:
            self.assertIn(f"demo/corpus/{rel}", src.values())


class TestPageFragment(unittest.TestCase):
    def test_header_comments(self):
        lines = PAGE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "<!-- title_key: nav.explain -->")
        self.assertEqual(lines[1], "<!-- scripts: data/strings.js data/explain.js js/core.js js/glossary.js js/site-explain.js -->")
        self.assertEqual(lines[2], "<!-- styles: css/site-explain.css -->")
        for rel in ("js/core.js", "js/site-explain.js", "css/site-explain.css"):
            self.assertTrue((APP / "static" / rel).is_file(), rel)

    def test_is_a_body_fragment_without_inline_code(self):
        html = PAGE.read_text(encoding="utf-8").lower()
        for bad in ("<html", "<head", "<body", "<script", "<style", " style=", "javascript:"):
            self.assertNotIn(bad, html)
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), "no on* handler attributes")

    def test_every_text_comes_from_a_key(self):
        """No English in the fragment: every element with text is filled from en.json through data-ss-t."""
        html = PAGE.read_text(encoding="utf-8")
        body = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", body)
        self.assertEqual(text.split(), [], "visible text in the fragment")

    def test_keys_exist(self):
        en = json.loads(EN.read_text(encoding="utf-8"))
        used = set(re.findall(r'data-ss-t="([^"]+)"', PAGE.read_text(encoding="utf-8")))
        used |= set(re.findall(r"'((?:explain|lane|band|common|app)\.[a-z0-9_.]+)'", JS.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertEqual(sorted(k for k in used if k not in en), [])

    def test_explain_strings_have_no_digit(self):
        en = json.loads(EN.read_text(encoding="utf-8"))
        bad = {k: v for k, v in en.items() if k.startswith("explain.") and re.search(r"\d", v)
               and k != "explain.install_command"}
        self.assertEqual(bad, {})
        self.assertEqual(en["explain.install_command"], "python3 -m second_scribe.app")

    def test_css_uses_tokens_only(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css), "colours come from tokens.css")
        self.assertNotIn("Fraunces", css)
        self.assertNotIn("http", css)


class TestDevHarness(unittest.TestCase):
    def test_dev_data_is_fresh(self):
        from second_scribe.app.explain_data import case_to_js
        self.assertEqual((DEV / "data" / "explain.js").read_text(encoding="utf-8"), case_to_js(_case()))

    def test_harness_carries_the_fragment(self):
        body = "\n".join(PAGE.read_text(encoding="utf-8").splitlines()[3:]).strip()
        self.assertIn(body, (DEV / "explain.html").read_text(encoding="utf-8"))

    def test_dev_strings_hold_every_key_the_page_uses(self):
        dev = (DEV / "data" / "explain.strings.js").read_text(encoding="utf-8")
        head = "window.SS_STRINGS = "
        self.assertTrue(dev.startswith(head))
        table = json.loads(dev[len(head):].rstrip().rstrip(";"))
        en = json.loads(EN.read_text(encoding="utf-8"))
        used = set(re.findall(r'data-ss-t="([^"]+)"', PAGE.read_text(encoding="utf-8")))
        used |= set(re.findall(r"'((?:explain|lane|band|common|app)\.[a-z0-9_.]+)'", JS.read_text(encoding="utf-8")))
        self.assertEqual(sorted(k for k in used if table.get(k) != en.get(k)), [])


def _build_site_importable() -> bool:
    try:
        from second_scribe.app import build_site  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_build_site_importable(), "build_site (T28) is not on this branch yet")
class TestBuildSiteWritesExplain(unittest.TestCase):
    def test_build_site_writes_explain_js(self):
        from second_scribe.app.build_site import build_site
        from second_scribe.app.explain_data import case_to_js
        from second_scribe.app.testpattern import make_test_pattern
        from second_scribe.export_view import view_to_js
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d:
            tryit = Path(d) / "tryit.js"
            tryit.write_text(view_to_js(make_test_pattern(), "tryit"), encoding="utf-8")
            out = Path(d) / "site"
            kw = {"tryit": tryit, "study": None}
            if "tryit_profile" in inspect.signature(build_site).parameters:   # T28 (65a67d4) takes the synthetic profile
                kw["tryit_profile"] = ROOT / "tests" / "fixtures" / "profiles" / "profile-synth.json"
            build_site(out, **kw)
            got = (out / "data" / "explain.js").read_text(encoding="utf-8")
            self.assertEqual(got, case_to_js(_case()))
            index = (out / "index.html").read_text(encoding="utf-8")
            for ref in ("data/explain.js", "js/site-explain.js", "css/site-explain.css"):
                self.assertIn(ref, index)
            self.assertTrue((out / "js" / "site-explain.js").is_file())
            sources = json.loads((out / "data" / "SOURCES.json").read_text(encoding="utf-8"))
            self.assertEqual(sources["other"]["explain"]["kind"], "explain")


if __name__ == "__main__":
    unittest.main()
