"""A6 minors on the bench's own sentences: the "No." items deny the EVENT (not the order), and the hypothetical
templates are about the speaker's possible future, so they cannot also read as unsupported."""
import re
import unittest
from pathlib import Path

from second_scribe.judge_bench import build_modality_set, template_label
from second_scribe.keywords import keyword_modality
from second_scribe.synth.bank import CONCEPTS
from second_scribe.synth.bench_bank import BENCH_TEMPLATES, IV_NO_QUESTIONS, render

CORPUS = Path(__file__).resolve().parent.parent / "demo" / "corpus"
ORDER_WORDS = re.compile(r"\b(next|followed|came|then|after|before|first|order|sequence)\b", re.I)


class TestIvNoQuestions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = [i for i in build_modality_set(CORPUS) if i["style"] == "prose"]

    def test_questions_ask_whether_it_happened_and_never_about_order(self):
        self.assertGreaterEqual(len(IV_NO_QUESTIONS), 3)
        for q in IV_NO_QUESTIONS:
            self.assertEqual(q.count("{x}"), 1, q)
            self.assertTrue(q.endswith("?"), q)
            self.assertIsNone(ORDER_WORDS.search(q), q)

    def test_the_no_items_use_those_questions_and_a_bare_no(self):
        no = [i for i in self.items if i["kind"] == "iv_no"]
        self.assertEqual(len(no), 6)
        allowed = {render(q, c.forms[1]) for q in IV_NO_QUESTIONS for c in CONCEPTS}
        for i in no:
            self.assertIn(i["quote"], allowed)
            self.assertEqual((i["reply"], i["speaker"], i["expected"]), ("No.", "interviewer", "negated"))
            self.assertIn(template_label(i["template"]), IV_NO_QUESTIONS)

    def test_a_no_item_whose_concept_wording_presupposes_the_event_is_excluded_not_scored(self):
        from second_scribe.judge_bench import FILL_CONCEPTS, FILL_REASON
        fill = {c.cid for c in FILL_CONCEPTS}
        for i in self.items:
            if i["kind"] == "iv_no":
                self.assertEqual(i.get("exclude"), None if i["cid"] in fill else FILL_REASON, i["cid"])

    def test_the_no_reply_items_keep_the_original_question_without_a_reply(self):
        orig = {(i["pid"], i["cid"]): i for i in self.items if i["kind"] == "interviewer_voiced"}
        for i in self.items:
            if i["kind"] == "iv_no_reply":
                o = orig[(i["pid"], i["cid"])]
                self.assertEqual((i["quote"], i["expected"]), (o["quote"], "unsupported"))
                self.assertNotIn("reply", i)

    def test_same_element_and_concept_as_the_original_question(self):
        orig = {(i["pid"], i["cid"]): i for i in self.items if i["kind"] == "interviewer_voiced"}
        for i in self.items:
            if i["kind"] == "iv_no":
                self.assertEqual(i["element"], orig[(i["pid"], i["cid"])]["element"])
                concept = next(c for c in CONCEPTS if c.cid == i["cid"])
                self.assertIn(concept.forms[1].lower(), i["quote"].lower())


class TestHypotheticalTemplatesAreAboutTheSpeaker(unittest.TestCase):
    SPEAKER = re.compile(r"\b(we|I|us|our|I'm|we're|we'd|I'd)\b")
    POSSIBLE = re.compile(r"\b(might|may|could|would|if|perhaps|maybe|possibly|guessing|assuming|should the|"
                          r"who knows|what if|depend)\w*", re.I)
    GENERIC_FROM_THE_AUDIT = ("Perhaps {x} will change next year.", "{x} could go either way, depending on who you get.",
                              "Depending on the officer, {x} may take a week or a month.",
                              "Who knows, {x} may well get simpler one day.",
                              "I'm guessing {x} depends on which person you draw.",
                              "Possibly {x} will get easier if the city changes the rules.",
                              "Assuming the paperwork is right, {x} should take about a week.",
                              "Should the rules change, {x} could become a quick formality.",
                              "Down the road, {x} might turn into something quicker.",
                              "What if {x} turned out to be a simple form?")

    def test_every_template_speaks_about_the_speaker_and_a_possibility(self):
        for t in BENCH_TEMPLATES["hypothetical"]:
            self.assertIsNotNone(self.SPEAKER.search(t), t)
            self.assertIsNotNone(self.POSSIBLE.search(t), t)

    def test_the_generic_process_statements_are_gone(self):
        self.assertFalse(set(self.GENERIC_FROM_THE_AUDIT) & set(BENCH_TEMPLATES["hypothetical"]))

    def test_no_template_says_it_happened_or_states_a_wish_or_a_denial(self):
        happened = re.compile(r"\b(we|I) (got|had|went through|dealt with|did|finished|applied)\b|took us|happened to us", re.I)
        wish = re.compile(r"\b(wish|hope|hoping|wanted|need|needed|longed|should have|would like|would be nice)\b", re.I)
        denial = re.compile(r"\b(never|didn't|did not|not|no one|nobody|nothing|without)\b", re.I)
        for t in BENCH_TEMPLATES["hypothetical"]:
            for rx in (happened, wish, denial):
                self.assertIsNone(rx.search(t), (rx.pattern, t))

    def test_still_a_dozen_and_more_than_four_cue_free_ones_the_keyword_rule_misses(self):
        ts = BENCH_TEMPLATES["hypothetical"]
        self.assertGreaterEqual(len(ts), 12)
        blind = [t for t in ts if all(keyword_modality(render(t, c.forms[1])) == "actual" for c in CONCEPTS if c.cid != "C19")]
        self.assertGreaterEqual(len(blind), 6)


if __name__ == "__main__":
    unittest.main()
