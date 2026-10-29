"""Task 5C: v2 for the four helper prompts (align, in_transcript, canonicalize, confirm_mapping_talk)."""
import glob
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe import prompts as prompts_mod
from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient
from second_scribe.match import align_labels, canonicalize, in_transcript
from second_scribe.prompts import load_helper_prompt, load_system
from second_scribe.schemas import Transcript, Turn
from second_scribe.transcripts import confirm_mapping_talk, flag_mapping_talk, parse_synthetic
from tests.test_judge import _demo_corpus_grams, _grams, _words

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
NAMES = ("align", "in_transcript", "canonicalize", "confirm_mapping_talk")
PINNED_V1 = {   # sha256 of the v1 helper files as committed before Task 5C
    "align.v1.txt": "1889dc67c00944b4105ba39e3091ca4c0843618f64207187fed61499afa9e7f6",
    "canonicalize.v1.txt": "7cebaa4c8a783562717d043f497983b9a0c9ea9290022d6cd195dad6ad258954",
    "confirm_mapping_talk.v1.txt": "d217cdd4072be4485e9bb71625ce495966c43bf3d80c6e346a5883ebafe21221",
    "in_transcript.v1.txt": "ab17f72cc478d75ed9befb246dc655a15c9aa748b7523854c3c11edf7ba217cc",
}
T = Transcript("t", [Turn(0, "interviewer", "Tell me about the clinic."),
                     Turn(1, "participant", "I waited months on the waiting list for the clinic.")], "synthetic")


def user_of(messages):
    return messages[-1]["content"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def capture(self, reply):
        seen = []
        return seen, MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or reply)


class TestFiles(unittest.TestCase):
    def test_v1_files_are_unchanged(self):
        for name, sha in PINNED_V1.items():
            self.assertEqual(hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest(), sha, name)

    def test_system_helper_is_short_one_paragraph(self):
        s = load_system(PROMPTS, "helper")
        self.assertEqual(s, (PROMPTS / "system.helper.v2.txt").read_text(encoding="utf-8").strip())
        self.assertTrue(s)
        self.assertLess(len(s.split()), 60)
        self.assertNotIn("\n", s)
        low = s.lower()
        for w in ("careful research assistant", "exactly", "json"):
            self.assertIn(w, low)

    def test_loader_default_v2_and_v1_reproducible(self):
        for n in NAMES:
            self.assertEqual(load_helper_prompt(PROMPTS, n), (PROMPTS / f"{n}.v2.txt").read_text(encoding="utf-8"))
            self.assertEqual(load_helper_prompt(PROMPTS, n, "v1"), (PROMPTS / f"{n}.v1.txt").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            load_helper_prompt(PROMPTS, "../align")
        with self.assertRaises(ValueError):
            load_helper_prompt(PROMPTS, "align", "../v2")

    def test_sandwich_contract_then_data_then_short_reminder_ending_with_brace(self):
        data_marker = {"align": "LIST A:\n{left}\nLIST B:\n{right}\n", "in_transcript": "TRANSCRIPT:\n{transcript}\nLABELS:\n{labels}\n",
                       "canonicalize": "LABELS:\n{labels}\n", "confirm_mapping_talk": "TURNS:\n{turns}\n"}
        for n in NAMES:
            p = load_helper_prompt(PROMPTS, n)
            head, sep, tail = p.partition(data_marker[n])
            self.assertTrue(sep, n)
            # rules and the output contract come first
            self.assertIn("Return only JSON", head, n)
            self.assertIn("Begin your answer with {", head, n)
            self.assertEqual(p.count("REMINDER:"), 1, n)
            self.assertTrue(tail.startswith("REMINDER:"), n)
            reminder = tail[len("REMINDER:"):].strip()
            self.assertLessEqual(len(reminder.split()), 60, n)
            self.assertTrue(reminder.endswith("Begin your answer with {"), n)
            self.assertIn("JSON", reminder, n)
            self.assertNotIn("{left}", head + tail)

    def test_new_prompts_have_no_trap_or_domain_vocabulary(self):
        banned = ("cousin", "food", "truck", "neighbour", "neighbor", "propane", "licen", "sales tax", "renewal",
                  "parking", "permit", "inspect", "commissary", "health", "doctor", "clinic", "hospital", "autis")
        for f in [PROMPTS / f"{n}.v2.txt" for n in NAMES] + [PROMPTS / "system.helper.v2.txt"]:
            low = f.read_text(encoding="utf-8").lower()
            for w in banned:
                self.assertNotIn(w, low, f"{f.name} mentions {w!r}")

    def test_no_five_word_run_of_a_new_prompt_is_in_the_demo_corpus(self):
        docs, corpus = _demo_corpus_grams()
        self.assertGreater(len(corpus), 500)
        for f in [PROMPTS / f"{n}.v2.txt" for n in NAMES] + [PROMPTS / "system.helper.v2.txt"]:
            shared = _grams(_words(f.read_text(encoding="utf-8"))) & corpus
            self.assertFalse(shared, f"{f.name} shares a 5-word run with the demo corpus: {sorted(shared)[:2]}")

    def test_neutral_wording_no_push_toward_matching(self):
        low = load_helper_prompt(PROMPTS, "align").lower()
        self.assertIn("most items match nothing; that is fine", low)
        for w in ("try to match", "be generous", "be strict", "when in doubt"):
            self.assertNotIn(w, low)

    def test_align_defines_same_close_and_no_pair(self):
        p = load_helper_prompt(PROMPTS, "align")
        self.assertIn("same: both items refer to the same thing", p)
        self.assertIn("however much detail each gives about it", p)
        self.assertIn("close: one item is broader and the other is one part or one kind of it, so they refer to "
                      "the same thing at a different level of detail", p)
        self.assertIn("Anything else is no pair", p)
        self.assertIn("steps before or after each other are not a pair", p)
        self.assertIn('"Booking a road test" and "Taking the road test": no pair.', p)
        self.assertIn("Different wording or detail does not prevent a pair", p)

    def test_in_transcript_quote_rule(self):
        p = load_helper_prompt(PROMPTS, "in_transcript")
        self.assertIn("word for word", p)
        self.assertIn("ONE turn", p)
        self.assertIn("null", p)
        self.assertIn("did not happen or happened to someone else", p)
        self.assertIn("the whole clause that mentions it (usually 5 to 15 words)", p)

    def test_canonicalize_rules_and_no_label_source(self):
        p = load_helper_prompt(PROMPTS, "canonicalize")
        self.assertIn("1 to 4 words", p)
        self.assertIn("Do not generalize", p)
        self.assertIn("distinct", p)
        self.assertIn("same things the same", p)
        self.assertIn("If a label names two things, keep both if they fit in 4 words; otherwise name the first.", p)
        self.assertIn("with the most specific kind of thing it is", p)
        low = p.lower()
        for w in ("ai ", "model", "interview", "endorse", "prior", "official", "participant", "map", "generated",
                  "human", "drawn"):
            self.assertNotRegex(low, r"\b" + w.strip() + r"\b", w)

    def test_confirm_mapping_talk_defines_and_gives_neutral_examples(self):
        p = load_helper_prompt(PROMPTS, "confirm_mapping_talk")
        self.assertIn("talking about the map itself", p)
        self.assertRegex(p, r"(?m)^- yes:")
        self.assertRegex(p, r"(?m)^- no:")
        self.assertIn("or showing it on screen. A turn that does this and also asks or says something about what "
                      "happened is yes.", p)
        self.assertIn("- no: the interviewer is talking only about something else", p)


class TestCallers(Base):
    def test_align_sends_system_then_sandwich_and_braces_stay_literal(self):
        seen, c = self.capture('{"pairs": []}')
        align_labels(self.log, c, ["{right} tricky {left}"], ["{transcript} {labels}"], PROMPTS, item_id="m")
        m = seen[0]
        self.assertEqual([x["role"] for x in m], ["system", "user"])
        self.assertEqual(m[0]["content"], load_system(PROMPTS, "helper"))
        u = user_of(m)
        self.assertIn("LIST A:\n1. {right} tricky {left}\nLIST B:\n1. {transcript} {labels}\nREMINDER:", u)
        self.assertLess(u.index("Return only JSON"), u.index("LIST A:"))
        self.assertTrue(u.rstrip().endswith("Begin your answer with {"))

    def test_in_transcript_sends_system_then_sandwich(self):
        seen, c = self.capture('{"answers": [{"n": 1, "quote": null}]}')
        in_transcript(self.log, c, ["{left} label"], T, PROMPTS, item_id="t")
        m = seen[0]
        self.assertEqual([x["role"] for x in m], ["system", "user"])
        u = user_of(m)
        self.assertIn("TRANSCRIPT:\n[I] Tell me about the clinic.\n[P] I waited months on the waiting list for the clinic.\nLABELS:\n1. {left} label\nREMINDER:", u)
        self.assertLess(u.index("Return only JSON"), u.index("TRANSCRIPT:"))

    def test_canonicalize_sends_system_then_sandwich(self):
        seen, c = self.capture('{"concepts": [{"n": 1, "concept": "x"}]}')
        canonicalize(self.log, c, ["{labels} one"], PROMPTS, item_id="c")
        m = seen[0]
        self.assertEqual([x["role"] for x in m], ["system", "user"])
        u = user_of(m)
        self.assertIn("LABELS:\n1. {labels} one\nREMINDER:", u)
        self.assertLess(u.index("Return only JSON"), u.index("LABELS:"))

    def test_canonicalize_takes_no_label_source_and_sends_the_same_text_every_time(self):
        # the pull-toward-script estimate relies on one prompt for AI, endorsed and prior-only labels:
        # the call takes no source argument (so the text can only depend on the labels)
        import inspect
        params = inspect.signature(canonicalize).parameters
        # ``stage`` only names the audit-log stage; the text sent never depends on it (checked below)
        self.assertEqual(sorted(params), ["batch_size", "client", "item_id", "labels", "log", "prompt_version",
                                          "prompts_dir", "stage"])
        outs = []
        for stage in ("canonicalize", "judge:canon"):
            seen, c = self.capture('{"concepts": [{"n": 1, "concept": "x"}, {"n": 2, "concept": "y"}]}')
            canonicalize(self.log, c, ["Booking the road test", "Instructor"], PROMPTS, item_id="c", stage=stage)
            outs.append(seen[0])
        self.assertEqual(outs[0], outs[1])

    def test_confirm_mapping_talk_sends_system_then_sandwich(self):
        t = flag_mapping_talk(parse_synthetic("[I] Let me add box {turns} here.\n[P] ok", "S"))
        seen, c = self.capture('{"answers": [{"n": 1, "map_talk": "yes"}]}')
        confirm_mapping_talk(t, self.log, c, item_id="S")
        m = seen[0]
        self.assertEqual([x["role"] for x in m], ["system", "user"])
        u = user_of(m)
        self.assertIn("TURNS:\n1. Let me add box {turns} here.\nREMINDER:", u)
        self.assertLess(u.index("Return only JSON"), u.index("TURNS:"))

    def test_v1_is_reproducible_without_a_system_message(self):
        t = flag_mapping_talk(parse_synthetic("[I] Let me add box 1 here.\n[P] ok", "S"))
        cases = [
            (lambda c: align_labels(self.log, c, ["x"], ["y"], PROMPTS, item_id="m", prompt_version="v1"),
             "align", '{"pairs": []}', {"left": "1. x", "right": "1. y"}),
            (lambda c: in_transcript(self.log, c, ["x"], T, PROMPTS, item_id="t", prompt_version="v1"),
             "in_transcript", '{"answers": [{"n": 1, "quote": null}]}', {"transcript": T.text("full"), "labels": "1. x"}),
            (lambda c: canonicalize(self.log, c, ["x"], PROMPTS, item_id="c", prompt_version="v1"),
             "canonicalize", '{"concepts": [{"n": 1, "concept": "x"}]}', {"labels": "1. x"}),
            (lambda c: confirm_mapping_talk(t, self.log, c, item_id="S", prompt_version="v1"),
             "confirm_mapping_talk", '{"answers": [{"n": 1, "map_talk": "yes"}]}', {"turns": "1. Let me add box 1 here."}),
        ]
        for run, name, reply, values in cases:
            seen, c = self.capture(reply)
            run(c)
            self.assertEqual([x["role"] for x in seen[0]], ["user"], name)
            expect = (PROMPTS / f"{name}.v1.txt").read_text(encoding="utf-8")
            for k, v in values.items():
                expect = expect.replace("{" + k + "}", v)
            self.assertEqual(user_of(seen[0]), expect, name)

    def test_bad_version_is_rejected(self):
        with self.assertRaises(ValueError):
            align_labels(self.log, MockClient(fn=lambda m: "{}"), ["x"], ["y"], PROMPTS, item_id="m",
                         prompt_version="../v1")


if __name__ == "__main__":
    unittest.main()
