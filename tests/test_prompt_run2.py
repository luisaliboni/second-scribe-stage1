"""Run-2 prompt changes (council round 8, items 2-3). Every v2-family prompt that renders a transcript says a turn
may run over several lines, each starting with [I] or [P] (the old "each line is one turn" was false: lines are
paragraphs); every v2-family prompt that asks for node types says the type is never a phase name. Rung 1 (the
authors' v1 first draft) stays byte-identical, so the ladder anchor holds."""
import hashlib
import unittest
from pathlib import Path

from second_scribe import ladder
from second_scribe.ladder import LENGTH_TOLERANCE, get_rung, validate_rungs
from second_scribe.prompt_manifest import verify_manifest

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"

TURN_SENTENCE = "a speaker's turn may run over several lines, each starting with [I] (interviewer) or [P] (participant)."
TYPE_SENTENCE = "A node's type must be one of the listed types and is never a phase name."
RECON_V2 = ("reconstruct.p0.v2.txt", "reconstruct.p1.v2.txt", "reconstruct.p2.v2.txt",
            "reconstruct.p0.remedy.v1.txt", "reconstruct.p0.placebo.v1.txt")
PRIOR_V2 = ("prior_only.p0.v2.txt", "prior_only.p1.v2.txt", "prior_only.p2.v2.txt")
TRANSCRIPT_V2 = RECON_V2 + ("in_transcript.v2.txt",)

# sha256 of the v1 files at main b3e5f53 (before run 2): rung 1 is reconstruct.p0.v1.txt
V1_SHA256 = {
    "reconstruct.p0.v1.txt": "4b17dd7827b2de459229e931a4195efc5cb2f256d1cd8120235a46035c43584a",
}


def read(name):
    return (PROMPTS / name).read_text(encoding="utf-8")


class TestTurnSentence(unittest.TestCase):
    def test_every_transcript_prompt_has_the_new_sentence_and_not_the_false_one(self):
        for name in TRANSCRIPT_V2:
            with self.subTest(name):
                text = read(name)
                self.assertIn(TURN_SENTENCE.lower(), text.lower())
                self.assertNotIn("is one turn", text)
                self.assertNotIn("each line is one turn", text.lower())

    def test_the_sentence_sits_before_the_transcript(self):
        for name in TRANSCRIPT_V2:
            with self.subTest(name):
                text = read(name)
                self.assertLess(text.lower().index("may run over several lines"), text.index("{transcript}"))


class TestTypeSentence(unittest.TestCase):
    def test_every_v2_prompt_that_asks_for_types_has_it_once(self):
        for name in RECON_V2 + PRIOR_V2:
            with self.subTest(name):
                self.assertEqual(read(name).count(TYPE_SENTENCE), 1)

    def test_it_sits_after_the_schema(self):
        for name in RECON_V2 + PRIOR_V2:
            with self.subTest(name):
                text = read(name)
                self.assertGreater(text.index(TYPE_SENTENCE), text.index("{schema}"))

    def test_helpers_that_do_not_ask_for_types_do_not_get_it(self):
        self.assertNotIn(TYPE_SENTENCE, read("in_transcript.v2.txt"))


class TestRungOneByteIdentical(unittest.TestCase):
    def test_rung1_prompt_bytes(self):
        data = (PROMPTS / "reconstruct.p0.v1.txt").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), V1_SHA256["reconstruct.p0.v1.txt"])
        self.assertEqual(ladder.RUNG1_SHA256, V1_SHA256["reconstruct.p0.v1.txt"])
        self.assertEqual(ladder.PINNED_SHA256["reconstruct.p0.v1.txt"], ladder.RUNG1_SHA256)
        self.assertEqual(get_rung("rung1").prompt_file, "reconstruct.p0.v1.txt")

    def test_rung1_carries_neither_new_sentence(self):
        text = read("reconstruct.p0.v1.txt")
        self.assertNotIn("may run over several lines", text)
        self.assertNotIn("never a phase name", text)
        for name in ("reconstruct.p1.v1.txt", "reconstruct.p2.v1.txt", "prior_only.p0.v1.txt"):
            self.assertNotIn("never a phase name", read(name))

    def test_the_gate_refuses_a_changed_rung1_even_if_the_pin_is_edited(self):
        import shutil
        import tempfile
        from second_scribe.prompt_manifest import write_manifest
        d = tempfile.mkdtemp()
        for p in PROMPTS.iterdir():
            if p.is_file():
                shutil.copyfile(p, Path(d) / p.name)
        f = Path(d) / "reconstruct.p0.v1.txt"
        f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
        write_manifest(d)
        self.assertTrue(any("rung 1" in p and "reconstruct.p0.v1.txt" in p for p in validate_rungs(d)), validate_rungs(d))


class TestLadderStillConsistent(unittest.TestCase):
    def test_validate_rungs_clean_and_manifest_matches(self):
        self.assertEqual(validate_rungs(PROMPTS), [])
        self.assertEqual(verify_manifest(PROMPTS), [])

    def test_pins_match_the_files_on_disk(self):
        for name, sha in ladder.PINNED_SHA256.items():
            self.assertEqual(hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest(), sha, name)

    def test_remedy_and_placebo_still_within_the_length_rule(self):
        a, b = len(read("reconstruct.p0.remedy.v1.txt")), len(read("reconstruct.p0.placebo.v1.txt"))
        self.assertLessEqual(abs(a - b) / max(a, b), LENGTH_TOLERANCE)

    def test_remedy_and_placebo_got_the_same_two_sentences(self):
        for name in ("reconstruct.p0.remedy.v1.txt", "reconstruct.p0.placebo.v1.txt", "reconstruct.p0.v2.txt"):
            text = read(name)
            self.assertEqual(text.count(TYPE_SENTENCE), 1, name)
            self.assertEqual(text.lower().count(TURN_SENTENCE.lower()), 1, name)


if __name__ == "__main__":
    unittest.main()
