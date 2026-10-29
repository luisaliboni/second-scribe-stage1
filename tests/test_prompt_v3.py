"""judge-prompts-v3 (16 Oct 2026): the judge's rubric card and the off-record prompt move to v3; every other prompt,
the shared definitions and the system messages stay at v2, byte for byte."""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe import prompts as prompts_mod
from second_scribe.audit.registry import AuditLog
from second_scribe.judge import _render_template, judge_modality_detailed, reverse_label_order
from second_scribe.llm import MockClient
from second_scribe.match import _parse_quotes, in_transcript
from second_scribe.prompts import (DEFAULT_PROMPT_VERSION, IN_TRANSCRIPT_PROMPT_VERSION, JUDGE_PROMPT_VERSION,
                                   file_version, load_definitions, load_helper_prompt, load_system)
from second_scribe.schemas import MODALITIES, Transcript, Turn
from tests.test_judge import _demo_corpus_grams, _grams, _words

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
# sha256 of the files the v3 prompts must not touch, as committed at 088436c (run2-quotes)
PINNED = {
    "definitions.v2.txt": "306439dec9d09303ca3ccc877962868b60596741dac96d9b15a135821cdcbc34",
    "in_transcript.v1.txt": "ab17f72cc478d75ed9befb246dc655a15c9aa748b7523854c3c11edf7ba217cc",
    "in_transcript.v2.txt": "0d3e67d399713097ec57f175148d0531a3de8b056daf28f18579ff274c9b1420",
    "judge_modality.v1.txt": "3676ebfcee64b0a0c5af869436f9fcb5bb51e7f14f4864ca26e82a9bff546414",
    "judge_modality.v2.txt": "aa6be2ab4df358c44a596a35a625c57b890b2b37a08f9bee8858bc295f609776",
    "judge_modality_letter.v2.txt": "276879e4730415bdfe374ff1a8542df8b5c67744c70ee414ea657f7dbbd550a1",
    "system.helper.v2.txt": "80a7af6d84f9b5b2456b7a097e93a9a739d15e9edd0c5e10c3e06a932f25bb4f",
    "system.judge.v2.txt": "e91b001f6b4135a1614b65c1a9c07e2269573b2c4373b402a7a8bee78b3ca050",
}
V3_FILES = ("judge_modality.v3.txt", "in_transcript.v3.txt")
T = Transcript("t", [Turn(0, "interviewer", "Tell me about the clinic."),
                     Turn(1, "participant", "I waited months on the waiting list for the clinic.")], "synthetic")


def _examples(card):
    """[(letter, element, quote, label)] of the card's reference example lines (an optional reason in brackets)."""
    return re.findall(r'^Example ([A-F])\. ELEMENT: (.*?)\. QUOTE: "(.*)" Label: (\w+)(?: \(.*\))?\.$', card, re.M)


class TestVersions(unittest.TestCase):
    def test_earlier_files_are_byte_identical(self):
        for name, sha in PINNED.items():
            self.assertEqual(hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest(), sha, name)

    def test_only_the_card_and_the_off_record_prompt_have_a_v3(self):
        self.assertEqual(sorted(p.name for p in PROMPTS.glob("*.v3.txt")), sorted(V3_FILES))
        self.assertEqual((DEFAULT_PROMPT_VERSION, JUDGE_PROMPT_VERSION, IN_TRANSCRIPT_PROMPT_VERSION), ("v2", "v3", "v3"))

    def test_v3_reuses_the_v2_definitions_and_system_messages(self):
        self.assertEqual(load_definitions(PROMPTS, "v3"), load_definitions(PROMPTS, "v2"))
        for role in ("judge", "helper"):
            self.assertEqual(load_system(PROMPTS, role, "v3"), load_system(PROMPTS, role, "v2"))
        self.assertEqual(file_version("definitions", "v3"), "v2")
        self.assertEqual(file_version("judge_modality", "v3"), "v3")
        self.assertEqual(file_version("definitions", "v2"), "v2")
        with self.assertRaises(ValueError):
            file_version("definitions", "../v2")
        with self.assertRaises(FileNotFoundError):          # no reconstruction prompt or system message at v3
            load_system(PROMPTS, "reconstruct", "v3")


class TestJudgeCardV3(unittest.TestCase):
    def setUp(self):
        self.card, self.system = _render_template(PROMPTS, "v3")

    def test_default_judge_call_sends_the_v3_card_with_the_v2_system_and_definitions(self):
        sent = []

        def fn(messages):
            sent.append([dict(m) for m in messages])
            return json.dumps({"labels": [{"n": 1, "modality": "actual"}]})
        with tempfile.TemporaryDirectory() as d:
            judge_modality_detailed(AuditLog(Path(d) / "l.jsonl", "t"), MockClient(fn=fn),
                                    [{"element_id": "a", "element": "E", "quote": "we waited six weeks"}], PROMPTS,
                                    item_id="x")
        system, user = sent[0]
        self.assertEqual(system["content"], (PROMPTS / "system.judge.v2.txt").read_text(encoding="utf-8").strip())
        self.assertEqual(user["content"], self.card.replace("{items}", '1. ELEMENT: E\n   QUOTE: "we waited six weeks"'))
        self.assertIn((PROMPTS / "definitions.v2.txt").read_text(encoding="utf-8").strip(), user["content"])

    def test_labels_examples_contract_and_precedence_are_kept(self):
        labels = re.findall(r"^- (" + "|".join(MODALITIES) + r"):", self.card, re.M)[-6:]
        self.assertEqual(labels, list(MODALITIES))
        ex = _examples(self.card)
        self.assertEqual([e[0] for e in ex], list("ABCDEF"))
        self.assertEqual([e[3] for e in ex], list(MODALITIES))
        self.assertIn('Return only JSON, in exactly this form: {"labels": [{"n": <number>, "modality": "<label>"}]}',
                      self.card)
        self.assertIn("otherwise wished if it fits the wished definition above; otherwise negated, then other_person, "
                      "then hypothetical.", self.card)
        self.assertIn("If an interviewer quote has no REPLY, use unsupported.", self.card)
        self.assertEqual(self.card.count("ITEMS:\n"), 1)
        self.assertEqual(self.card.count("\nREMINDER:"), 1)
        self.assertTrue(self.card.rstrip("\n").endswith("Begin your answer with {"))

    def test_the_card_asks_first_whether_the_quote_is_about_the_element(self):
        self.assertLess(self.card.index("Step 1."), self.card.index("Step 2."))
        self.assertLess(self.card.index("Step 2."), self.card.index("Labels (use exactly one per item):"))
        reminder = self.card.split("\nREMINDER:", 1)[1]
        self.assertIn("about the element itself", reminder)
        # the unsupported example reuses the actual example's quote for another element
        ex = {e[3]: e for e in _examples(self.card)}
        self.assertEqual(ex["unsupported"][2], ex["actual"][2])
        self.assertNotEqual(ex["unsupported"][1], ex["actual"][1])

    def test_reversed_card_moves_only_the_label_and_example_lines(self):
        rev = reverse_label_order(self.card)
        self.assertEqual([e[3] for e in _examples(rev)], list(reversed(MODALITIES)))
        self.assertEqual(reverse_label_order(rev), self.card)


class TestInTranscriptV3(unittest.TestCase):
    def setUp(self):
        self.prompt = load_helper_prompt(PROMPTS, "in_transcript", "v3")

    def test_default_call_sends_the_v3_prompt_after_the_v2_helper_system_message(self):
        seen = []
        client = MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or json.dumps(
            {"answers": [{"n": 1, "quote": "I waited months on the waiting list"}]}))
        with tempfile.TemporaryDirectory() as d:
            out = in_transcript(AuditLog(Path(d) / "l.jsonl", "t"), client, ["waiting list"], T, PROMPTS, item_id="x")
        self.assertEqual(out, {0: True})
        system, user = seen[0]
        self.assertEqual(system["content"], (PROMPTS / "system.helper.v2.txt").read_text(encoding="utf-8").strip())
        self.assertTrue(user["content"].startswith(self.prompt.split("{transcript}")[0]))
        self.assertTrue(user["content"].endswith(self.prompt.split("{labels}")[1]))

    def test_the_null_example_is_valid_json_in_the_requested_form(self):
        m = re.search(r"the answer is (\{.*?\]\})\.", self.prompt)
        self.assertIsNotNone(m)
        self.assertEqual(_parse_quotes(m.group(1), 2), {1: "we signed the lease at the agent's office", 2: None})
        self.assertIn('"quote": null', self.prompt.split("\nREMINDER:", 1)[1])

    def test_sandwich_layout_and_markers_are_kept(self):
        p = self.prompt
        head, sep, tail = p.partition("TRANSCRIPT:\n{transcript}\nLABELS:\n{labels}\n")
        self.assertTrue(sep)
        self.assertIn("Return only JSON", head)
        self.assertIn('{"answers": [{"n": <label number>, "quote": "<words copied word for word from one turn>" | null}]}',
                      head)
        self.assertEqual(p.count("TRANSCRIPT:"), 1)
        self.assertEqual(p.count("LABELS:"), 1)
        self.assertTrue(tail.startswith("REMINDER:"))
        self.assertLessEqual(len(tail[len("REMINDER:"):].split()), 60)
        self.assertTrue(tail.rstrip("\n").endswith("Begin your answer with {"))
        self.assertFalse(re.search(r"(?m)^\d+\. ", tail))     # nothing after the labels reads as a numbered label


class TestNoTrapVocabulary(unittest.TestCase):
    def test_v3_prompts_have_no_trap_or_domain_vocabulary(self):
        banned = ("cousin", "food", "truck", "neighbour", "neighbor", "propane", "licen", "sales tax", "renewal",
                  "parking", "permit", "inspect", "commissary", "health", "doctor", "clinic", "hospital", "autis",
                  "vendor", "fire", "insurance", "loan")
        for name in V3_FILES:
            low = (PROMPTS / name).read_text(encoding="utf-8").lower()
            for w in banned:
                self.assertNotIn(w, low, f"{name} mentions {w!r}")

    def test_no_five_word_run_of_a_v3_prompt_is_in_the_demo_corpus(self):
        docs, corpus = _demo_corpus_grams()
        self.assertGreater(len(corpus), 500)
        for name in V3_FILES:
            shared = _grams(_words((PROMPTS / name).read_text(encoding="utf-8"))) & corpus
            self.assertFalse(shared, f"{name} shares a 5-word run with the demo corpus: {sorted(shared)[:2]}")


if __name__ == "__main__":
    unittest.main()
