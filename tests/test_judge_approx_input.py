"""R68 (FABLE review 3.6): for an APPROXIMATE-tier quote the modality judge sees the TRANSCRIPT's own words for the
matched span (not the reconstructor's wording), the speaker tag is the matched turn's, and the participant's reply
follows the LAST matched line. Exact-tier quotes keep the old input. ``textnorm._approximate`` reports the matched
span as extra fields only; tier decisions do not change."""
import unittest
from pathlib import Path

from second_scribe import textnorm
from second_scribe.judge import JudgeCache, _render_item, modality_items, prompt_fingerprint
from second_scribe.provenance import locate_quote, provenance_for
from second_scribe.schemas import MapGraph, Node, Edge, Transcript, Turn

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# transcript wording vs the reconstructor's near-copy (one word differs: 1 of 16 >= 0.85)
TRANSCRIPT_WORDS = "i really wish the clinic had called us back in the first week after the referral"
RECON_WORDS = "i really wish the clinic had phoned us back in the first week after the referral"


def tr():
    return Transcript("S", [
        Turn(0, "participant", "Okay."),
        Turn(1, "interviewer", "So the first office sent you away, and then what happened with the second office?"),
        Turn(2, "interviewer", "Take your time on the next part."),
        Turn(3, "participant", "Sure. Well, " + TRANSCRIPT_WORDS.capitalize() + ", and they never did."),
        Turn(4, "participant", "That was the worst of it."),
    ], "synthetic")


class TestMatchedSpanInInfo(unittest.TestCase):
    def test_info_carries_the_matched_tokens(self):
        t, info = textnorm.locate_quote_in_turns(tr().units("full"), RECON_WORDS)
        self.assertEqual(t, "approximate")
        self.assertEqual(info["span_parts"], [TRANSCRIPT_WORDS.split()])
        self.assertEqual(info["span_end_token"] - info["span_start_token"], info["span_tokens"])
        self.assertEqual(sum(len(p) for p in info["span_parts"]), info["span_tokens"])

    def test_ellipsis_parts_are_kept_apart(self):
        text = "we waited six weeks for the first visit and then a nurse phoned about the form we had sent"
        units = [("P", text)]
        t, info = textnorm.locate_quote_in_turns(units, "we waited six weeks for the first visit ... a nurse called about the form we had sent")
        self.assertEqual(t, "approximate")
        self.assertEqual(len(info["span_parts"]), 2)
        self.assertEqual(info["span_parts"][0], "we waited six weeks for the first visit".split())

    def test_exact_tier_has_no_span_fields_and_unfound_neither(self):
        t, info = textnorm.locate_quote_in_turns([("P", "alpha beta gamma")], "alpha beta")
        self.assertEqual((t, sorted(info)), ("exact", ["turn_indices"]))
        t, info = textnorm.locate_quote_in_turns([("P", "alpha beta gamma")], "zzz yyy")
        self.assertEqual((t, info), ("unfound", {"reason": "below_threshold"}))

    def test_tier_decision_fields_unchanged(self):
        t, info = textnorm.locate_quote_in_turns(tr().units("full"), RECON_WORDS)
        self.assertEqual((info["unit_start"], info["unit_end"], info["candidates"]), (3, 3, 1))
        self.assertEqual((info["matched_tokens"], info["quote_tokens"], info["span_tokens"]), (15, 16, 16))
        self.assertAlmostEqual(info["ratio"], 15 / 16)


class TestTiersUnchanged(unittest.TestCase):
    OLD_KEYS = {"ratio", "matched_tokens", "quote_tokens", "span_tokens", "unit_start", "unit_end", "candidates"}
    NEW_KEYS = {"span_start_token", "span_end_token", "span_parts"}

    def test_approximate_info_is_the_old_info_plus_the_new_fields_only(self):
        t, info = textnorm.locate_quote_in_turns(tr().units("full"), RECON_WORDS)
        self.assertEqual(set(info), self.OLD_KEYS | self.NEW_KEYS)

    def test_tier_outcomes_on_the_existing_fixtures(self):
        # a one-word-off quote stays approximate, a dropped negation stays unfound (guard), a far-off quote stays unfound,
        # an exact quote stays exact: the span fields are read by nothing that decides a tier
        units = [("P", "we never got a call back from the clinic about the referral we sent in the spring")]
        base = "we never got a call back from the clinic about the referral we sent in the spring"
        self.assertEqual(textnorm.locate_quote_in_turns(units, base)[0], "exact")
        self.assertEqual(textnorm.locate_quote_in_turns(units, base.replace("clinic", "office"))[0], "approximate")
        self.assertEqual(textnorm.locate_quote_in_turns(units, base.replace("never ", ""))[0], "unfound")
        self.assertEqual(textnorm.locate_quote_in_turns(units, "completely different words about nothing here at all")[0],
                         "unfound")


class TestProvenance(unittest.TestCase):
    def test_approximate_provenance_carries_matched_text_and_last_turn(self):
        p = locate_quote(RECON_WORDS, tr(), "n1", "node")
        self.assertEqual(p.tier, "approximate")
        self.assertEqual(p.matched_text, TRANSCRIPT_WORDS)
        self.assertEqual((p.turn_idx, p.last_turn_idx), (3, 3))

    def test_multi_line_span_last_turn_is_the_last_matched_line(self):
        t = Transcript("S", [
            Turn(0, "interviewer", "Tell me about the referral."),
            Turn(1, "participant", "We waited six weeks for the first appointment at the downtown clinic."),
            Turn(2, "participant", "Then a coordinator phoned about the referral paperwork we had filed."),
            Turn(3, "interviewer", "And after that?"),
            Turn(4, "participant", "Nothing."),
        ], "synthetic")
        q = ("waited six weeks for the first appointment at the downtown clinic then a coordinator called "
             "about the referral paperwork we had filed")
        p = locate_quote(q, t, "n1", "node")
        self.assertEqual((p.tier, p.turn_idx, p.last_turn_idx), ("approximate", 1, 2))

    def test_exact_provenance_has_no_matched_text(self):
        p = locate_quote("Take your time on the next part", tr(), "n1", "node")
        self.assertEqual((p.tier, p.matched_text, p.last_turn_idx), ("exact", None, None))


class TestJudgeInput(unittest.TestCase):
    def items(self, quote, speaker_turns=None):
        T = speaker_turns or tr()
        ai = MapGraph("S", "ai", nodes=[Node("n1", "Clinic call back", quote=quote)])
        return modality_items(ai, transcript=T)[0]

    def test_approximate_quote_is_shown_as_the_transcripts_words(self):
        it = self.items(RECON_WORDS)
        self.assertEqual(it["quote"], TRANSCRIPT_WORDS)            # "called", not the reconstructor's "phoned"
        self.assertEqual(it["reconstructor_quote"], RECON_WORDS)
        self.assertIn("called us back", _render_item(1, it))
        self.assertNotIn("phoned", _render_item(1, it))
        self.assertEqual(it["speaker"], "participant")

    def test_two_reconstructor_wordings_of_one_span_give_one_cache_key(self):
        fp = prompt_fingerprint(PROMPTS)
        a, b = self.items(RECON_WORDS), self.items(RECON_WORDS.replace("phoned", "rang"))
        key = lambda it: JudgeCache.key(it["element"], it["quote"], fp, it.get("speaker"), it.get("reply"))
        self.assertEqual(key(a), key(b))
        # and the key is the hash of what is rendered: the transcript words, not the reconstructor's
        exact_wording = JudgeCache.key("Clinic call back", TRANSCRIPT_WORDS, fp, "participant", None)
        self.assertEqual(key(a), exact_wording)
        self.assertNotEqual(key(a), JudgeCache.key("Clinic call back", RECON_WORDS, fp, "participant", None))

    def test_ellipsis_quote_renders_parts_joined_by_ellipsis(self):
        T = Transcript("S", [Turn(0, "participant",
                                  "We waited six weeks for the first visit and then a nurse phoned about the form we had sent.")],
                       "synthetic")
        it = self.items("we waited six weeks for the first visit ... a nurse called about the form we had sent", T)
        self.assertEqual(it["quote"], "we waited six weeks for the first visit ... a nurse phoned about the form we had sent")

    def test_interviewer_approximate_reply_anchors_on_the_last_matched_line(self):
        T = Transcript("S", [
            Turn(0, "participant", "Okay."),
            Turn(1, "interviewer", "So the first office sent you away at the front desk, and then you went on to the second office."),
            Turn(2, "interviewer", "Did the second office then agree to look at your file after all of that waiting?"),
            Turn(3, "participant", "They did, in the end."),
            Turn(4, "interviewer", "Good."),
            Turn(5, "participant", "Yes."),
        ], "synthetic")
        # matched span starts on line 1 and ends on line 2; the reply to the LAST line is turn 3
        q = ("the first office sent you away at the front desk and then you went on to the second office "
             "did the second office then agree to look at your file after all that waiting")
        it = self.items(q, T)
        self.assertEqual(it["speaker"], "interviewer")
        self.assertEqual(it["reply"], "They did, in the end.")
        # anchored on line 1 (the old behaviour) there would be NO reply: another interviewer turn comes first
        from second_scribe.judge import _next_participant_reply
        self.assertIsNone(_next_participant_reply(T, 1))

    def test_exact_quote_keeps_the_old_item(self):
        it = self.items("Take your time on the next part.")
        self.assertEqual(it, {"element_id": "n1", "element": "Clinic call back",
                              "quote": "Take your time on the next part.", "speaker": "interviewer",
                              "reply": tr().turns[3].text})

    def test_provenance_without_matched_text_falls_back_to_the_reconstructor_quote(self):
        # an old Provenance (no matched_text) or no transcript: nothing to render but the quote
        ai = MapGraph("S", "ai", nodes=[Node("n1", "X", quote=RECON_WORDS)])
        p = provenance_for(ai, tr())
        p["n1"].matched_text = None
        self.assertEqual(modality_items(ai, provenance=p)[0]["quote"], RECON_WORDS)

    def test_edge_quotes_get_the_same_treatment(self):
        ai = MapGraph("S", "ai", nodes=[Node("n1", "A"), Node("n2", "B")], edges=[Edge("n1", "n2", quote=RECON_WORDS)])
        it = modality_items(ai, transcript=tr())[0]
        self.assertEqual((it["element_id"], it["quote"]), ("e:n1->n2", TRANSCRIPT_WORDS))


if __name__ == "__main__":
    unittest.main()
