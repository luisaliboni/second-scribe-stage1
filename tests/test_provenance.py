import unittest
from second_scribe.schemas import Edge, MapGraph, Node, Transcript, Turn
from second_scribe.provenance import locate_quote, eligible, provenance_for

T = Transcript("SYN-OW02", [
    Turn(0, "interviewer", "So the fire inspection came next, right after the zoning review, is that right?"),
    Turn(1, "participant", "Yes."),
    Turn(2, "interviewer", "I'll put a box for the commissary kitchen here on the map.", mapping_talk=True),
    Turn(3, "participant", "We waited six weeks for the health unit and then they lost our forms twice."),
    Turn(4, "participant", "We waited six weeks for the health unit and then they lost our forms twice."),
], "synthetic")


class TestProvenance(unittest.TestCase):
    def test_interviewer_voiced(self):
        p = locate_quote("the fire inspection came next, right after the zoning review", T, "n1", "node")
        self.assertTrue(p.found); self.assertEqual(p.speaker, "interviewer")
        self.assertEqual(eligible(p, "full"), (True, "interviewer_voiced"))
        self.assertEqual(eligible(p, "removed"), (False, "not_participant"))

    def test_repeated_quote_counts_occurrences(self):
        p = locate_quote("we waited six weeks for the health unit", T, "n2", "node")
        self.assertEqual(p.occurrences, 2); self.assertEqual(p.turn_idx, 3); self.assertEqual(p.span_words, 8)

    def test_short_whole_turn_ok_short_fragment_not(self):
        self.assertTrue(locate_quote("Yes.", T, "n3", "node").whole_turn)
        self.assertEqual(eligible(locate_quote("lost our forms", T, "n4", "node"), "full"), (False, "too_short"))

    def test_ellipsis_parts_same_turn(self):
        p = locate_quote("We waited six weeks ... they lost our forms twice", T, "n5", "node")
        self.assertTrue(p.found)

    def test_mapping_talk_turn_rejected_in_removed_arm(self):
        p = locate_quote("I'll put a box for the commissary kitchen here on the map", T, "n6", "node")
        self.assertTrue(p.mapping_talk_turn); self.assertEqual(eligible(p, "removed")[0], False)

    def test_not_found(self):
        p = locate_quote("we loved every minute of it and would do it again", T, "n7", "node")
        self.assertEqual(eligible(p, "full"), (False, "not_found"))

    # --- beyond the brief ---
    def test_empty_and_none_quote_not_found(self):
        for q in (None, "", "   ", "..."):
            p = locate_quote(q, T, "n8", "node")
            self.assertFalse(p.found); self.assertEqual(p.span_words, 0)
            self.assertIsNone(p.speaker); self.assertIsNone(p.turn_idx)
            self.assertEqual(eligible(p, "full"), (False, "not_found"))
            self.assertEqual(eligible(p, "removed"), (False, "not_found"))

    def test_ellipsis_parts_must_be_in_one_turn_and_in_order(self):
        # "fire inspection" is in turn 0, "lost our forms" in turn 3: no single turn has both
        self.assertFalse(locate_quote("fire inspection ... lost our forms", T, "n9", "node").found)
        # right words, wrong order within one turn. (Turns 3 and 4 are two consecutive participant paragraphs, which
        # since run 2 form ONE speaker turn, where the two parts happen to be in order: so test a lone turn.)
        one = Transcript("SYN-OW02b", [T.turns[3]], "synthetic")
        self.assertFalse(locate_quote("lost our forms ... we waited six weeks", one, "n10", "node").found)

    def test_participant_turn_preferred_over_earlier_interviewer_turn(self):
        t = Transcript("SYN-X", [
            Turn(0, "interviewer", "so we waited six weeks for the health unit and then they lost forms",
                 mapping_talk=True),
            Turn(1, "participant", "yes we waited six weeks for the health unit and then they lost forms"),
        ], "synthetic")
        p = locate_quote("we waited six weeks for the health unit", t, "n", "node")
        self.assertEqual((p.speaker, p.turn_idx, p.occurrences), ("participant", 1, 2))
        self.assertFalse(p.mapping_talk_turn)
        self.assertEqual(eligible(p, "removed"), (True, "ok"))

    def test_removed_arm_rejects_unknown_speaker_and_participant_ok(self):
        t = Transcript("SYN-U", [Turn(0, "unknown", "we waited six weeks for the health unit")], "synthetic")
        p = locate_quote("we waited six weeks for the health unit", t, "n", "node")
        self.assertEqual(p.speaker, "unknown")
        self.assertEqual(eligible(p, "removed"), (False, "not_participant"))
        self.assertEqual(eligible(p, "full"), (True, "ok"))
        q = locate_quote("we waited six weeks for the health unit", T, "n", "node")
        self.assertEqual(eligible(q, "removed"), (True, "ok"))

    def test_mapping_talk_flag_beats_speaker_in_removed_arm(self):
        t = Transcript("SYN-M", [Turn(0, "participant", "I'd put the commissary kitchen box here on the map",
                                       mapping_talk=True)], "synthetic")
        p = locate_quote("I'd put the commissary kitchen box here on the map", t, "n", "node")
        self.assertEqual(eligible(p, "removed"), (False, "mapping_talk"))
        self.assertEqual(eligible(p, "full"), (True, "ok"))

    def test_min_words_parameter_and_boundary(self):
        p = locate_quote("we waited six weeks for the health unit", T, "n", "node")   # 8 words
        self.assertEqual(eligible(p, "full"), (True, "ok"))
        self.assertEqual(eligible(p, "full", min_words=9), (False, "too_short"))

    def test_unknown_arm_raises(self):
        p = locate_quote("Yes.", T, "n", "node")
        with self.assertRaises(ValueError):
            eligible(p, "both")

    def test_provenance_for_maps_nodes_and_edges(self):
        ai = MapGraph("SYN-OW02", "ai",
                      nodes=[Node("n1", "Health unit", quote="we waited six weeks for the health unit"),
                             Node("n2", "Ghost")],
                      edges=[Edge("n1", "n2", quote="Yes.")])
        out = provenance_for(ai, T)
        self.assertEqual(set(out), {"n1", "n2", "e:n1->n2"})
        self.assertEqual((out["n1"].kind, out["n1"].found), ("node", True))
        self.assertFalse(out["n2"].found)
        self.assertEqual((out["e:n1->n2"].kind, out["e:n1->n2"].whole_turn), ("edge", True))
        self.assertEqual(out["e:n1->n2"].element_id, "e:n1->n2")


if __name__ == "__main__":
    unittest.main()
