"""Run-2 review B1/B2: ONE meaning of "exact" and one tie-break for every consumer of the quote rule.

The validator (reconstruct.validate_recon / parse_recon), the audits cross-check, Task-6 bucketing
(provenance.locate_quote) and the in-transcript check all call the same turn-aware function, so an exact quote lies
inside ONE paragraph by ONE speaker and a "..." part never sits in another turn. Synthetic text only."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.audits import audit_recon_outputs
from second_scribe.provenance import locate_quote
from second_scribe.reconstruct import parse_recon, validate_recon
from second_scribe.schemas import Transcript, Turn
from second_scribe.textnorm import locate_quote_tiered
from tests.test_audits import ai_map, put

CROSS = Transcript("SYN-CONS", [
    Turn(0, "participant", "i felt so tired after the visit"),
    Turn(1, "interviewer", "and then what happened next"),
    Turn(2, "participant", "totally alone in the waiting room"),
    Turn(3, "participant", "it was so"),
    Turn(4, "interviewer", "think about it"),
], "synthetic")

CASES = {
    "ellipsis across turns": "i felt ... alone",
    "ellipsis across speakers": "i felt ... totally alone",
    "marker token bridge": "it was so i think about it",
}


def tiers(quote):
    text = CROSS.text("full")
    return locate_quote_tiered(text, quote)[0], locate_quote(quote, CROSS, "n1", "node").tier


class TestOneMeaningOfExact(unittest.TestCase):
    def test_validator_tier_equals_provenance_tier_on_cross_turn_quotes(self):
        for name, q in CASES.items():
            validator, prov = tiers(q)
            self.assertEqual(validator, prov, name)
            self.assertEqual(validator, "unfound", name)

    def test_marker_tokens_never_bridge_a_speaker_change(self):
        self.assertEqual(tiers("it was so i think about it")[0], "unfound")
        self.assertEqual(tiers("it was so p think about it")[0], "unfound")

    def test_in_one_turn_exact_still_exact_with_ellipsis(self):
        self.assertEqual(tiers("i felt so ... after the visit")[0], "exact")
        self.assertEqual(tiers("i felt so ... after the visit")[1], "exact")

    def test_validate_recon_counts_cross_turn_quotes_as_unfound(self):
        nodes = [{"id": f"n{i}", "label": f"S{i}", "type": "service", "phase": None, "status": "experienced",
                  "quote": q} for i, q in enumerate(CASES.values())]
        err = validate_recon(json.dumps({"nodes": nodes, "edges": [], "emotion_word": None}), arm="full",
                             transcript_text=CROSS.text("full"), phases=None)
        self.assertIsNotNone(err)
        self.assertIn("not verbatim", err)

    def test_parse_recon_tiers_equal_provenance_tiers(self):
        nodes = [{"id": f"n{i}", "label": f"S{i}", "type": "service", "phase": None, "status": "experienced",
                  "quote": q} for i, q in enumerate(CASES.values())]
        g = parse_recon(json.dumps({"nodes": nodes, "edges": [], "emotion_word": None}), "m", "ai",
                        transcript_text=CROSS.text("full"))
        for n in g.nodes:
            self.assertEqual(g.meta["quote_tiers"][n.node_id], locate_quote(n.quote, CROSS, n.node_id, "node").tier)
        self.assertEqual(g.meta["quote_tier_counts"], {"exact": 0, "approximate": 0, "unfound": 3})

    def test_audit_counts_cross_turn_quote_as_blocking(self):
        work = Path(tempfile.mkdtemp())
        nodes = [("n0", "A", "experienced", "i felt ... alone"), ("n1", "B", "experienced", "i felt so tired"),
                 ("n2", "C", "experienced", "i felt ... alone")]
        put(work, "recon", "W1__full__p0__t0__r0", {"status": "ok", "map": ai_map(nodes)})
        r = audit_recon_outputs(work, {"W1": CROSS})
        self.assertFalse(r.passed)     # 2 of 3 unfound > 50% (L16, was 10%)
        self.assertEqual(r.counts["quotes_exact"], 1)
        self.assertEqual(r.counts["quotes_unfound_textnorm"], 2)


# ------------------------------------------------------------------ B2
ECHO = Transcript("SYN-ECHO", [
    Turn(0, "interviewer", "so you said the doctor told you to wait for the referral letter"),
    Turn(1, "participant", "yes the doctor told us we had to wait for the referral letter"),
], "synthetic")
ECHO_Q = "the doctor told to wait for the referral letter"


class TestParticipantPreferred(unittest.TestCase):
    def test_both_turns_qualify_and_participant_wins(self):
        p = locate_quote(ECHO_Q, ECHO, "n1", "node")
        self.assertEqual(p.tier, "approximate")
        self.assertEqual(p.occurrences, 2)
        self.assertEqual((p.speaker, p.turn_idx), ("participant", 1))

    def test_participant_still_wins_when_the_interviewer_comes_second(self):
        t = Transcript("SYN-ECHO2", [Turn(0, "participant", ECHO.turns[1].text),
                                     Turn(1, "interviewer", ECHO.turns[0].text)], "synthetic")
        p = locate_quote(ECHO_Q, t, "n1", "node")
        self.assertEqual((p.speaker, p.turn_idx), ("participant", 0))

    def test_interviewer_only_match_is_still_interviewer(self):
        t = Transcript("SYN-I", [ECHO.turns[0], Turn(1, "participant", "something else entirely different here")],
                       "synthetic")
        self.assertEqual(locate_quote(ECHO_Q, t, "n1", "node").speaker, "interviewer")


class TestTierCountsCountEveryElement(unittest.TestCase):
    """Review M4: a repeated edge (same source and target) keeps its own tier in the counts."""

    def test_repeated_edges_are_all_counted(self):
        nodes = [{"id": f"n{i}", "label": f"S{i}", "type": "service", "phase": None, "status": "experienced",
                  "quote": "i felt so tired after the visit"} for i in range(2)]
        edges = [{"source": "n0", "target": "n1", "status": "experienced", "quote": q}
                 for q in ("i felt so tired", "words nobody ever said here")]
        g = parse_recon(json.dumps({"nodes": nodes, "edges": edges, "emotion_word": None}), "m", "ai",
                        transcript_text=CROSS.text("full"))
        self.assertEqual(g.meta["quote_tier_counts"], {"exact": 3, "approximate": 0, "unfound": 1})


if __name__ == "__main__":
    unittest.main()
