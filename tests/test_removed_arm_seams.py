"""Run-2 review S4: in the removed arm a deleted mapping-talk turn leaves a SEAM; the validator must not join the
participant paragraphs on both sides of it into one turn (Task 6 searches the full transcript, where they are apart)."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.audits import audit_recon_outputs
from second_scribe.provenance import locate_quote
from second_scribe.reconstruct import parse_recon, validate_recon
from second_scribe.schemas import Transcript, Turn
from second_scribe.textnorm import locate_quote_in_turns
from tests.test_audits import ai_map, put

A = "we waited for the letter from the clinic and then there was a long pause"
B = "months went by with silence at every single step along the winding path"
T = Transcript("SYN-SEAM", [
    Turn(0, "participant", A),
    Turn(1, "interviewer", "show me on the map where that sits", mapping_talk=True),
    Turn(2, "participant", B),
], "synthetic")
STITCHED_APPROX = A + " " + B.replace("winding", "zzz")        # 25 words, one slip: approximate if the turns are joined


class TestUnits(unittest.TestCase):
    def test_removed_arm_units_keep_a_break_where_a_turn_was_removed(self):
        u = T.validation_units("removed")
        self.assertEqual([m for m, _t in u], ["P", None, "P"])
        self.assertEqual(T.validation_units("full"), T.units("full"))

    def test_consecutive_removals_make_one_break_and_edges_none(self):
        t = Transcript("t", [Turn(0, "interviewer", "x", mapping_talk=True), Turn(1, "participant", "a b c"),
                             Turn(2, "interviewer", "y", mapping_talk=True), Turn(3, "interviewer", "z", mapping_talk=True),
                             Turn(4, "participant", "d e f")], "synthetic")
        self.assertEqual([m for m, _t in t.validation_units("removed")], [None, "P", None, "P"])

    def test_the_old_units_are_untouched(self):
        self.assertEqual([m for m, _t in T.units("removed")], ["P", "P"])


class TestSeamConsistency(unittest.TestCase):
    def test_validator_and_provenance_agree_across_the_seam(self):
        want = locate_quote(STITCHED_APPROX, T, "n1", "node").tier
        self.assertEqual(want, "unfound")
        self.assertEqual(locate_quote_in_turns(T.validation_units("removed"), STITCHED_APPROX)[0], want)

    def test_validate_recon_rejects_a_quote_across_the_seam_in_the_removed_arm(self):
        nodes = [{"id": "n1", "label": "S", "type": "service", "phase": None, "status": "experienced",
                  "quote": STITCHED_APPROX}]
        reply = json.dumps({"nodes": nodes, "edges": [], "emotion_word": None})
        err = validate_recon(reply, arm="removed", transcript_text=T.text("removed"), phases=None,
                             turn_units=T.validation_units("removed"))
        self.assertIn("not verbatim", err)
        g = parse_recon(reply, "m", "ai", transcript_text=T.text("removed"), turn_units=T.validation_units("removed"))
        self.assertEqual(g.meta["quote_tiers"]["n1"], "unfound")

    def test_a_quote_inside_one_turn_is_fine(self):
        nodes = [{"id": "n1", "label": "S", "type": "service", "phase": None, "status": "experienced", "quote": A}]
        reply = json.dumps({"nodes": nodes, "edges": [], "emotion_word": None})
        self.assertIsNone(validate_recon(reply, arm="removed", transcript_text=T.text("removed"), phases=None,
                                         turn_units=T.validation_units("removed")))

    def test_audit_uses_the_same_units(self):
        work = Path(tempfile.mkdtemp())
        put(work, "recon", "W1__removed__p0__t0__r0", {"status": "ok", "map": ai_map(
            [("n0", "A", "experienced", STITCHED_APPROX), ("n1", "B", "experienced", A),
             ("n2", "C", "experienced", STITCHED_APPROX)])})
        r = audit_recon_outputs(work, {"W1": T})
        self.assertEqual(r.counts["quotes_approximate"], 0)
        self.assertFalse(r.passed)          # 2 of 3 unfound > 50% (L16)


if __name__ == "__main__":
    unittest.main()
