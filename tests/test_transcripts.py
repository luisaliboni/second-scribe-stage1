import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient
from second_scribe.transcripts import (confirm_mapping_talk, flag_mapping_talk, parse_paragraphs,
                                       parse_synthetic, read_docx_text)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path, paragraphs):
    body = ""
    for p in paragraphs:
        runs = "".join(f"<w:r><w:t xml:space=\"preserve\">{r}</w:t></w:r>" for r in p)
        body += f"<w:p>{runs}</w:p>"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}'
           f'</w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


class TestTranscripts(unittest.TestCase):
    def test_bracket_format(self):
        paras = ["[AB] So tell me about the first doctor.", "[XY] We saw our family doctor in May.",
                 "and then nothing happened.", "[AB] Let me put that box here on the map."]
        t = parse_paragraphs(paras, {"xy"}, "SYN-OW01")
        self.assertEqual(t.speaker_format, "bracket")
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant", "interviewer"])
        self.assertIn("nothing happened", t.turns[1].text)

    def test_first_token_format(self):
        paras = ["AB 0:01 Hi there", "XY 0:05 Hi", "AB 0:09 Ready?", "XY 0:12 Yes we waited a year"]
        t = parse_paragraphs(paras, {"xy"}, "SYN-OW02")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(t.turns[3].speaker, "participant")

    def test_no_labels_marks_unknown(self):
        paras = ["We started in May.", "It was hard.", "Then the school called.", "Okay."]
        t = parse_paragraphs(paras, {"xy"}, "SYN-OW03")
        self.assertEqual(t.speaker_format, "none")
        self.assertTrue(all(x.speaker == "unknown" for x in t.turns))

    def test_mapping_talk_only_interviewer(self):
        t = parse_synthetic("[I] I'll add a box for the fire inspection here.\n"
                            "[P] The map of the city was confusing.\n[I] How did that feel?", "SYN-OW04")
        t = flag_mapping_talk(t)
        self.assertEqual([x.mapping_talk for x in t.turns], [True, False, False])
        self.assertNotIn("add a box", t.text("removed"))

    # --- format detection edge cases (controller ruling) ---
    def test_first_token_strips_timestamps_and_punctuation(self):
        paras = ["AB: 00:01:23 Hi there", "xy: 0:05 Hi", "AB: 0:09 Ready?", "xy: 1:12 Yes we waited"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual([x.text for x in t.turns], ["Hi there", "Hi", "Ready?", "Yes we waited"])
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "participant"])

    def test_first_token_continuation_paragraphs_join_previous_turn(self):
        paras = ["AB 0:01 Hi there", "XY 0:05 Hi", "we waited long", "AB 0:09 Ready?", "XY 0:12 Yes"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(len(t.turns), 4)
        self.assertIn("we waited long", t.turns[1].text)

    def test_common_word_first_tokens_are_not_labels(self):
        # two very frequent first tokens, but both ordinary words -> not a label format
        paras = ["We went in May.", "It was hard.", "We waited.", "It rained.", "We left.", "It ended."]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "none")

    def test_bracket_label_matching_is_punctuation_and_case_insensitive(self):
        paras = ["[Xy.] Hello", "[AB] Hi", "[Xy.] Again", "[AB] Bye"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual([x.speaker for x in t.turns],
                         ["participant", "interviewer", "participant", "interviewer"])

    def test_bracket_threshold_is_30_percent(self):
        plain = [f"plain {c}" for c in "abcdefg"]
        at_30 = ["[AB] one", "[XY] two", "[AB] three"] + plain          # 3/10
        below = ["[AB] one", "[XY] two"] + plain[:5]                    # 2/7 = 28.6%
        self.assertEqual(parse_paragraphs(at_30, {"xy"}, "S").speaker_format, "bracket")
        self.assertNotEqual(parse_paragraphs(below, {"xy"}, "S").speaker_format, "bracket")

    def test_unlabelled_opening_paragraph_is_unknown_turn(self):
        paras = ["intro text", "[AB] Hi", "[XY] Hello", "[AB] Bye", "[XY] Ok"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.turns[0].speaker, "unknown")
        self.assertEqual([x.idx for x in t.turns], [0, 1, 2, 3, 4])

    def test_empty_paragraphs_ignored(self):
        paras = ["[AB] Hi", "", "   ", "[XY] Hello"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(len(t.turns), 2)

    def test_interviewer_code_is_passed_through(self):
        t = parse_paragraphs(["[AB] Hi", "[XY] Hello"], {"xy"}, "S", interviewer="I1")
        self.assertEqual(t.interviewer, "I1")

    def test_flag_mapping_talk_does_not_mutate_input(self):
        t = parse_synthetic("[I] Let me draw a box.\n[P] ok", "S")
        out = flag_mapping_talk(t)
        self.assertFalse(t.turns[0].mapping_talk)
        self.assertTrue(out.turns[0].mapping_talk)

    def test_synthetic_continuation_and_format(self):
        t = parse_synthetic("[I] First line\ncontinued\n\n[P] Reply", "S", interviewer="I2")
        self.assertEqual(t.speaker_format, "synthetic")
        self.assertEqual(t.interviewer, "I2")
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"])
        self.assertIn("continued", t.turns[0].text)

    def test_read_docx_text_joins_runs_per_paragraph(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.docx"
            make_docx(p, [["[AB] He", "llo"], [], ["[XY] Bye"]])
            self.assertEqual(read_docx_text(p), ["[AB] Hello", "", "[XY] Bye"])


class TestSpeakerInference(unittest.TestCase):
    LONG = "we waited a long time for the school to call us back and nobody told us anything"

    def bracket(self, extra=()):
        return ["[AB] Tell me more.", f"[CD] {self.LONG}", "[AB] And then?", f"[CD] {self.LONG} again",
                "[AB] Okay.", f"[CD] {self.LONG} once more", *extra]

    def test_default_inference_is_label(self):
        t = parse_paragraphs(["[AB] Hi", "[XY] Hello", "[AB] Bye", "[XY] Ok"], {"xy"}, "S")
        self.assertEqual(t.speaker_inference, "label")
        self.assertEqual(parse_synthetic("[I] a\n[P] b", "S").speaker_inference, "label")

    def test_word_share_fallback_when_no_label_matches(self):
        t = parse_paragraphs(self.bracket(), {"zz"}, "S")
        self.assertEqual(t.speaker_format, "bracket")
        self.assertEqual(t.speaker_inference, "word_share")
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant"] * 3)

    def test_word_share_considers_only_top_two_labels_by_turn_count(self):
        # a third label with one huge turn must not win: it is not in the top 2 by turn count
        huge = "[EF] " + " ".join(["word"] * 500)
        t = parse_paragraphs(self.bracket([huge, huge]), {"zz"}, "S")
        self.assertEqual(t.speaker_inference, "word_share")
        self.assertEqual(t.turns[-1].speaker, "interviewer")
        self.assertEqual(t.turns[1].speaker, "participant")

    def test_word_share_works_in_first_token_format(self):
        paras = ["AB 0:01 Tell me more", f"CD 0:05 {self.LONG}", "AB 0:09 And then", f"CD 0:12 {self.LONG}"]
        t = parse_paragraphs(paras, {"zz"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(t.speaker_inference, "word_share")
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "participant"])

    def test_matching_label_keeps_label_inference(self):
        t = parse_paragraphs(self.bracket(), {"ab"}, "S")
        self.assertEqual(t.speaker_inference, "label")
        self.assertEqual(t.turns[0].speaker, "participant")

    def test_fallback_none_keeps_old_behaviour(self):
        t = parse_paragraphs(self.bracket(), {"zz"}, "S", fallback="none")
        self.assertEqual(t.speaker_inference, "label")
        self.assertTrue(all(x.speaker == "interviewer" for x in t.turns))

    def test_unknown_fallback_value_raises(self):
        with self.assertRaises(ValueError):
            parse_paragraphs(self.bracket(), {"zz"}, "S", fallback="guess")

    def test_single_label_covering_90_percent_is_ambiguous(self):
        paras = [f"[AB] turn {i}" for i in range(18)] + ["[CD] reply a", "[CD] reply b"]   # 18/20 = 90%
        t = parse_paragraphs(paras, {"ab"}, "S")
        self.assertEqual(t.speaker_inference, "ambiguous_labels")
        self.assertEqual(t.speaker_format, "none")
        self.assertTrue(all(x.speaker == "unknown" for x in t.turns))
        self.assertEqual(len(t.turns), 20)

    def test_single_label_ambiguity_wins_over_word_share_fallback(self):
        paras = [f"[AB] turn {i}" for i in range(10)]
        t = parse_paragraphs(paras, {"zz"}, "S")
        self.assertEqual((t.speaker_inference, t.speaker_format), ("ambiguous_labels", "none"))

    def test_just_under_90_percent_is_not_ambiguous(self):
        paras = [f"[AB] turn {i}" for i in range(8)] + ["[CD] reply a", "[CD] reply b"]   # 80%
        t = parse_paragraphs(paras, {"cd"}, "S")
        self.assertEqual(t.speaker_inference, "label")
        self.assertEqual(t.speaker_format, "bracket")

    def test_label_shared_with_interviewer_is_not_a_participant_label(self):
        # participant and interviewer have the same initials; CD is the wordier speaker
        paras = self.bracket()
        plain = parse_paragraphs(paras, {"ab"}, "S")
        self.assertEqual(plain.speaker_inference, "label")
        self.assertEqual(plain.turns[0].speaker, "participant")          # the old, wrong reading
        t = parse_paragraphs(paras, {"ab"}, "S", interviewer_labels={"AB."})
        self.assertEqual(t.speaker_inference, "word_share")
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"] * 3)

    def test_shared_label_removal_works_in_first_token_format(self):
        paras = ["AB 0:01 Tell me more", f"CD 0:05 {self.LONG}", "AB 0:09 And then", f"CD 0:12 {self.LONG}"]
        t = parse_paragraphs(paras, {"ab"}, "S", interviewer_labels={"ab"})
        self.assertEqual((t.speaker_format, t.speaker_inference), ("first_token", "word_share"))
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "participant"])

    def test_other_participant_labels_survive_shared_label_removal(self):
        t = parse_paragraphs(self.bracket(), {"ab", "cd"}, "S", interviewer_labels={"ab"})
        self.assertEqual(t.speaker_inference, "label")                    # cd still matches
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"] * 3)

    def test_interviewer_labels_without_overlap_change_nothing(self):
        t = parse_paragraphs(self.bracket(), {"cd"}, "S", interviewer_labels={"zz"})
        self.assertEqual(t.speaker_inference, "label")
        self.assertEqual(t.turns[1].speaker, "participant")

    def test_inference_survives_flagging_and_confirmation(self):
        paras = ["[AB] Let me add a box here.", f"[CD] {self.LONG}", "[AB] Fine.", f"[CD] {self.LONG}"]
        t = flag_mapping_talk(parse_paragraphs(paras, {"zz"}, "S"))
        self.assertEqual(t.speaker_inference, "word_share")
        log = AuditLog(Path(tempfile.mkdtemp()) / "r.jsonl", "run", clock=lambda: "t")
        out, _ = confirm_mapping_talk(t, log, MockClient(fn=lambda m: answer(numbered(m), {1})), item_id="S")
        self.assertEqual(out.speaker_inference, "word_share")


class TestFixRound1(unittest.TestCase):
    # 1. timestamp-only brackets
    def test_timestamp_only_brackets_do_not_crash_and_are_not_bracket_format(self):
        paras = ["[0:05] we went to the clinic", "[0:09] then the school called", "[0:12] nothing happened",
                 "plain closing line"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertNotEqual(t.speaker_format, "bracket")
        self.assertTrue(all(x.speaker == "unknown" for x in t.turns))

    def test_bracket_detected_but_no_speaker_label_survives_is_none_not_a_crash(self):
        paras = ["[Inaudible] a", "[Laughs] b", "[Crosstalk] c", "plain"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "none")
        self.assertTrue(all(x.speaker == "unknown" for x in t.turns))

    # 2. stray bracket annotations
    def test_stray_bracket_annotation_continues_the_previous_turn(self):
        paras = ["[AB] How did it go?", "[XY] We went.", "[Inaudible] and then the box on the map.",
                 "[AB] ok.", "[XY] yes."]
        t = flag_mapping_talk(parse_paragraphs(paras, {"xy"}, "S"))
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant", "interviewer", "participant"])
        self.assertIn("[Inaudible] and then the box on the map.", t.turns[1].text)
        self.assertFalse(any(x.mapping_talk for x in t.turns if x.speaker == "participant"))
        self.assertIn("box on the map", t.text("removed"))

    def test_annotation_words_are_never_speakers_even_when_repeated(self):
        paras = ["[AB] Q one", "[XY] a one", "[Inaudible] x", "[AB] Q two", "[XY] a two", "[Inaudible] y"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"] * 2)

    def test_label_opening_two_turns_is_a_speaker_single_use_label_is_not(self):
        paras = ["[AB] Q one", "[XY] a one", "[CD] Q two", "[AB] Q three", "[XY] a two", "[CD] Q four"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(len(t.turns), 6)                       # CD opens 2 turns: a speaker
        paras2 = ["[AB] Q one", "[XY] a one", "[EF] aside", "[AB] Q three", "[XY] a two"]
        t2 = parse_paragraphs(paras2, {"xy"}, "S")
        self.assertEqual(len(t2.turns), 4)                      # EF opens 1 turn: merged into XY's turn
        self.assertIn("[EF] aside", t2.turns[1].text)

    def test_single_use_label_in_participant_or_interviewer_labels_is_kept(self):
        paras = ["[AB] Q one", "[XY] a one", "[CD] aside", "[AB] Q two", "[XY] a two"]
        t = parse_paragraphs(paras, {"xy"}, "S", interviewer_labels={"cd"})
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "interviewer", "participant"])

    # 3. first-token stoplist
    def test_interview_guide_questions_are_not_first_token_format(self):
        qs = ["What was the first thing you did?", "How did that feel?", "Did you call the clinic?",
              "Tell me about the school.", "What happened next?", "How long did you wait?",
              "Did anyone explain it?", "Tell me about the waiting.", "What would you change?",
              "How did the family react?"]
        t = parse_paragraphs(qs, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "none")
        self.assertTrue(all(x.speaker == "unknown" for x in t.turns))

    def test_backchannel_paragraphs_are_not_first_token_format(self):
        bc = ["Mm-hmm.", "Right.", "Thanks.", "Right.", "Mm-hmm.", "Thanks.", "Right.", "Mm-hmm."]
        t = parse_paragraphs(bc, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "none")

    def test_label_shape_is_1_to_4_uppercase_letters(self):
        ok = ["ABC 0:01 Hi", "XYZW 0:05 Hello", "ABC 0:09 Ready?", "XYZW 0:12 Yes"]
        self.assertEqual(parse_paragraphs(ok, {"xyzw"}, "S").speaker_format, "first_token")
        for lab in ("Abc", "ABCDE", "AB1"):
            paras = [f"{lab} 0:01 Hi", "QR 0:05 Hello", f"{lab} 0:09 Ready?", "QR 0:12 Yes"]
            self.assertEqual(parse_paragraphs(paras, {"zz"}, "S").speaker_format, "none", lab)   # no anchor

    def test_participant_or_interviewer_label_overrides_label_shape(self):
        paras = ["Maria 0:01 Hi", "XY 0:05 Hello", "Maria 0:09 Ready?", "XY 0:12 Yes"]
        t = parse_paragraphs(paras, {"xy"}, "S", interviewer_labels={"maria"})
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "participant"])

    def test_question_word_continuation_is_not_split_into_a_turn(self):
        paras = ["AB 0:01 Hi", "XY 0:05 We waited", "What happened then was odd", "AB 0:09 Ready?", "XY 0:12 Yes"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(len(t.turns), 4)
        self.assertIn("What happened then", t.turns[1].text)


class TestAnchoredFirstToken(unittest.TestCase):
    """A known label that opens >=2 paragraphs anchors the file: its partner label may be a
    mixed-case word (a role word or first name), which the unanchored shape rule would refuse."""

    def test_mixed_case_partner_label_is_accepted_when_anchored_by_a_known_label(self):
        paras = ["Mara 0:01 Hi there", "XY 0:05 Hi", "Mara 0:09 Ready?", "XY 0:12 Yes we waited a year"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual([x.speaker for x in t.turns],
                         ["interviewer", "participant", "interviewer", "participant"])

    def test_no_anchor_means_mixed_case_pair_is_still_not_a_label_format(self):
        paras = ["Mara 0:01 Hi there", "Jon 0:05 Hi", "Mara 0:09 Ready?", "Jon 0:12 Yes we waited"]
        self.assertEqual(parse_paragraphs(paras, {"xy"}, "S").speaker_format, "none")

    def test_questions_stay_none_even_with_an_anchor_label_present_once(self):
        qs = ["What was the first thing you did?", "How did that feel?", "XY said so.", "What happened next?",
              "How long did you wait?", "What would you change?", "How did the family react?"]
        self.assertEqual(parse_paragraphs(qs, {"xy"}, "S").speaker_format, "none")


class TestFixRound2(unittest.TestCase):
    def probe(self):
        paras = []
        for i in range(8):
            paras += [f"Mara 0:{i:02d} question number {i}", f"XY 0:{i:02d} answer number {i}"]
        paras += ["Right and then the box on the map.", "Right so we waited.",
                  "[Inaudible] and then we went.", "[Inaudible] okay."]
        return paras

    def test_reviewer_probe_right_and_inaudible_continue_the_previous_turn(self):
        t = flag_mapping_talk(parse_paragraphs(self.probe(), {"xy"}, "S"))
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(len(t.turns), 16)                      # no turn opened by Right / [Inaudible]
        self.assertEqual(t.turns[-1].speaker, "participant")
        self.assertIn("[Inaudible] okay.", t.turns[-1].text)
        self.assertIn("Right so we waited.", t.turns[-1].text)
        self.assertFalse(any(x.mapping_talk for x in t.turns if x.speaker == "participant"))

    def test_only_one_partner_label_is_loosened(self):
        # Right is frequent enough to be in the top 4 but is not the single partner label
        paras = []
        for i in range(8):
            paras += [f"Mara 0:{i:02d} question {i}", f"XY 0:{i:02d} answer {i}"]
        paras += ["Right so a", "Right so b", "Right so c"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(len(t.turns), 16)
        self.assertEqual({x.speaker for x in t.turns}, {"interviewer", "participant"})

    def test_partner_failing_a_filter_is_not_loosened(self):
        # the most frequent non-known token is a stage direction / common word: no partner label
        paras = []
        for i in range(6):
            paras += [f"Right so {i}", f"XY 0:{i:02d} answer {i}"]
        self.assertEqual(parse_paragraphs(paras, {"xy"}, "S").speaker_format, "none")
        paras = []
        for i in range(6):
            paras += [f"Inaudible so {i}", f"XY 0:{i:02d} answer {i}"]
        self.assertEqual(parse_paragraphs(paras, {"xy"}, "S").speaker_format, "none")

    def test_label_AM_does_not_split_a_paragraph_starting_am_i(self):
        paras = ["Dana 0:01 Hi", "AM 0:05 Hello", "Dana 0:09 Ready?", "AM 0:12 Yes", "Am I allowed to say?"]
        t = parse_paragraphs(paras, {"am"}, "S")
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual(len(t.turns), 4)
        self.assertIn("Am I allowed to say?", t.turns[-1].text)
        self.assertEqual(t.turns[-1].speaker, "participant")

    def test_partner_label_must_match_its_observed_casing(self):
        paras = ["Dana 0:01 Hi", "XY 0:05 Hello", "Dana 0:09 Ready?", "XY 0:12 Yes", "DANA is the name here"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual(len(t.turns), 4)
        self.assertIn("DANA is the name", t.turns[-1].text)

    def test_unintelligible_twice_in_bracket_format_is_not_a_speaker(self):
        paras = ["[AB] Q one", "[XY] a one", "[Unintelligible] x", "[AB] Q two", "[XY] a two",
                 "[Unintelligible] y"]
        t = parse_paragraphs(paras, {"xy"}, "S")
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"] * 2)

    def test_annotations_are_applied_in_the_first_token_path(self):
        # an uppercase stage direction (4 letters or fewer after stripping) is never a label unless known
        paras = ["SIGH 0:01 a", "XY 0:05 b", "SIGH 0:09 c", "XY 0:12 d"]
        self.assertEqual(parse_paragraphs(paras, {"xy"}, "S").speaker_format, "none")
        self.assertEqual(parse_paragraphs(paras, {"xy"}, "S", interviewer_labels={"sigh"}).speaker_format,
                         "first_token")


def numbered(messages):
    """The n values of the numbered turns in the prompt sent to the model."""
    body = messages[-1]["content"].split("TURNS:", 1)[1]
    return [int(m) for m in re.findall(r"^(\d+)\. ", body, re.M)]


def answer(ns, yes):
    return json.dumps({"answers": [{"n": n, "map_talk": "yes" if n in yes else "no"} for n in ns]})


class TestConfirmMappingTalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def flagged(self, n):
        lines = "\n".join(f"[I] Let me add box number {k} here.\n[P] ok {k}" for k in range(n))
        t = flag_mapping_talk(parse_synthetic(lines, "S"))
        self.assertEqual(sum(x.mapping_talk for x in t.turns), n)
        return t

    def test_rule_and_model_must_agree(self):
        t = self.flagged(3)
        client = MockClient(fn=lambda m: answer(numbered(m), yes={1, 3}))
        out, stats = confirm_mapping_talk(t, self.log, client, item_id="S", screen_all=False)
        self.assertEqual(stats, {"rule_flagged": 3, "model_confirmed": 2, "disagreements": 1})
        self.assertEqual([x.mapping_talk for x in out.turns if x.speaker == "interviewer"],
                         [True, False, True])
        self.assertFalse(any(x.mapping_talk for x in out.turns if x.speaker == "participant"))
        self.assertTrue(all(x.mapping_talk for x in t.turns if x.speaker == "interviewer"))  # input untouched

    def test_batches_of_at_most_20_and_calls_are_logged(self):
        t = self.flagged(25)
        sizes = []

        def fn(m):
            ns = numbered(m)
            sizes.append(len(ns))
            return answer(ns, yes=set(ns))
        out, stats = confirm_mapping_talk(t, self.log, MockClient(fn=fn), item_id="S", screen_all=False)
        self.assertEqual(sizes, [20, 5])
        self.assertEqual(stats, {"rule_flagged": 25, "model_confirmed": 25, "disagreements": 0})
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        self.assertEqual(sum(1 for r in recs if r["record_type"] == "call_request"), 2)

    def test_prompt_comes_from_file_with_turns_filled(self):
        t = self.flagged(1)
        seen = []
        client = MockClient(fn=lambda m: seen.append(m[-1]["content"]) or answer(numbered(m), set()))
        confirm_mapping_talk(t, self.log, client, item_id="S", screen_all=False)
        self.assertIn("1. Let me add box number 0 here.", seen[0])
        self.assertNotIn("{turns}", seen[0])

    def test_turn_text_with_braces_is_not_treated_as_format_field(self):
        t = flag_mapping_talk(parse_synthetic("[I] Put {this} box here on the map", "S"))
        client = MockClient(fn=lambda m: answer(numbered(m), {1}))
        out, stats = confirm_mapping_talk(t, self.log, client, item_id="S", screen_all=False)
        self.assertEqual(stats["model_confirmed"], 1)

    def test_unusable_answers_leave_turns_unflagged_and_count_as_disagreements(self):
        t = self.flagged(2)
        out, stats = confirm_mapping_talk(t, self.log, MockClient(fn=lambda m: "garbage"), item_id="S", screen_all=False)
        self.assertEqual(stats, {"rule_flagged": 2, "model_confirmed": 0, "disagreements": 2})
        self.assertFalse(any(x.mapping_talk for x in out.turns))

    def test_answer_missing_a_turn_is_invalid_then_retried(self):
        t = self.flagged(2)
        replies = iter([json.dumps({"answers": [{"n": 1, "map_talk": "yes"}]}),
                        answer([1, 2], {1, 2})])
        out, stats = confirm_mapping_talk(t, self.log, MockClient(fn=lambda m: next(replies)), item_id="S", screen_all=False)
        self.assertEqual(stats["model_confirmed"], 2)

    def test_no_flagged_turns_makes_no_calls(self):
        t = parse_synthetic("[I] How did that feel?\n[P] Bad.", "S")
        def boom(m):
            raise AssertionError("no call expected")
        out, stats = confirm_mapping_talk(flag_mapping_talk(t), self.log, MockClient(fn=boom), item_id="S", screen_all=False)
        self.assertEqual(stats, {"rule_flagged": 0, "model_confirmed": 0, "disagreements": 0})


class TestScreenEveryInterviewerTurn(unittest.TestCase):
    """A3: the model screens EVERY interviewer turn, not only the regex-flagged ones; the final flag is the model's
    answer, and the stats report rule-only vs model counts and their agreement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")
        # interviewer turns: 1 rule-flagged ("add box"), 2 not flagged, 3 not flagged; participant turns in between
        self.t = flag_mapping_talk(parse_synthetic(
            "[I] Let me add box 1 here.\n[P] ok\n[I] I will jot that down on the board for you.\n[P] yes\n"
            "[I] How did that feel?\n[P] bad", "S"))
        self.assertEqual([x.mapping_talk for x in self.t.turns if x.speaker == "interviewer"], [True, False, False])

    def run_with(self, yes, client=None):
        seen = []

        def fn(m):
            ns = numbered(m)
            seen.append(m[-1]["content"])
            return answer(ns, yes=yes)
        out, stats = confirm_mapping_talk(self.t, self.log, client or MockClient(fn=fn), item_id="S")
        return out, stats, seen

    def test_every_interviewer_turn_is_sent_participant_turns_are_not(self):
        out, stats, seen = self.run_with(yes=set())
        self.assertEqual(len(seen), 1)
        self.assertIn("1. Let me add box 1 here.", seen[0])
        self.assertIn("2. I will jot that down on the board for you.", seen[0])
        self.assertIn("3. How did that feel?", seen[0])
        self.assertNotIn("ok\n", seen[0].split("TURNS:", 1)[1])
        self.assertEqual(stats["n_interviewer_turns"], 3)
        self.assertEqual(stats["n_screened"], 3)

    def test_the_model_can_flag_a_turn_the_rule_missed(self):
        out, stats, _ = self.run_with(yes={1, 2})
        self.assertEqual([x.mapping_talk for x in out.turns if x.speaker == "interviewer"], [True, True, False])
        self.assertFalse(any(x.mapping_talk for x in out.turns if x.speaker == "participant"))
        self.assertEqual((stats["rule_flagged"], stats["model_flagged"]), (1, 2))
        self.assertEqual((stats["model_confirmed"], stats["rule_only"], stats["model_only"]), (1, 0, 1))
        self.assertEqual(stats["disagreements"], 1)
        self.assertAlmostEqual(stats["agreement"], 2 / 3)

    def test_the_model_can_unflag_a_rule_hit(self):
        out, stats, _ = self.run_with(yes=set())
        self.assertFalse(any(x.mapping_talk for x in out.turns))
        self.assertEqual((stats["rule_only"], stats["model_only"], stats["model_flagged"]), (1, 0, 0))
        self.assertAlmostEqual(stats["agreement"], 2 / 3)

    def test_input_transcript_is_untouched(self):
        self.run_with(yes={1, 2, 3})
        self.assertEqual([x.mapping_talk for x in self.t.turns if x.speaker == "interviewer"], [True, False, False])

    def test_batches_of_20_cover_all_turns(self):
        lines = "\n".join(f"[I] Question number {k} about your day?\n[P] a{k}" for k in range(25))
        t = flag_mapping_talk(parse_synthetic(lines, "S"))
        self.assertFalse(any(x.mapping_talk for x in t.turns))
        sizes = []

        def fn(m):
            ns = numbered(m)
            sizes.append(len(ns))
            return answer(ns, yes={1})
        out, stats = confirm_mapping_talk(t, self.log, MockClient(fn=fn), item_id="S")
        self.assertEqual(sizes, [20, 5])
        self.assertEqual((stats["model_flagged"], stats["model_only"], stats["n_screened"]), (2, 2, 25))

    def test_a_batch_the_model_never_answers_keeps_the_rule_flags_and_is_counted(self):
        out, stats, _ = self.run_with(yes=set(), client=MockClient(fn=lambda m: "garbage"))
        self.assertEqual([x.mapping_talk for x in out.turns if x.speaker == "interviewer"], [True, False, False])
        self.assertEqual((stats["n_interviewer_turns"], stats["n_screened"], stats["unscreened_turns"]), (3, 0, 3))
        self.assertIsNone(stats["agreement"])

    def test_no_interviewer_turns_makes_no_calls(self):
        t = parse_synthetic("[P] Bad.", "S")

        def boom(m):
            raise AssertionError("no call expected")
        out, stats = confirm_mapping_talk(flag_mapping_talk(t), self.log, MockClient(fn=boom), item_id="S")
        self.assertEqual((stats["n_interviewer_turns"], stats["rule_flagged"], stats["model_flagged"]), (0, 0, 0))
