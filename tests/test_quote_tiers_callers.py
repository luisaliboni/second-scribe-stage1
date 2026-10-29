"""Run-2 quote rule, item 3: every consumer of "is this quote in the transcript" uses the same tiered function --
Task-6 bucketing (provenance.locate_quote), the in-transcript check (match._quote_supports), the audits' cross-check
and the trap carrier test of report.py."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.match import _quote_supports
from second_scribe.provenance import eligible, locate_quote, provenance_for
from second_scribe.schemas import Edge, MapGraph, Node, Transcript, Turn
from second_scribe.textnorm import locate_quote_tiered
from second_scribe.audits import audit_recon_outputs
from tests.test_audits import ai_map, put, transcript


def words(n, start=0, tag="a"):
    return [f"{tag}{start + i}w" for i in range(n)]


def near(ws, at=4, sub="zzz"):
    out = list(ws)
    out[at] = sub
    return " ".join(out)


TXA, TXB, TXC = words(30, tag="p"), words(30, tag="q"), words(30, tag="r")
T = Transcript("SYN-OW09", [
    Turn(0, "interviewer", " ".join(words(10, tag="i"))),
    Turn(1, "participant", " ".join(TXA)),
    Turn(2, "participant", " ".join(TXB)),
    Turn(3, "interviewer", " ".join(words(25, tag="j")), mapping_talk=True),
    Turn(4, "participant", " ".join(TXC)),
], "synthetic")


class TestProvenanceTiers(unittest.TestCase):
    def test_exact_is_unchanged_and_tiered_exact(self):
        p = locate_quote(" ".join(TXA[:12]), T, "n1", "node")
        self.assertEqual((p.found, p.tier, p.speaker, p.turn_idx, p.occurrences, p.whole_turn),
                         (True, "exact", "participant", 1, 1, False))

    def test_approximate_quote_is_found_and_tiered(self):
        p = locate_quote(near(TXA), T, "n1", "node")
        self.assertEqual((p.found, p.tier, p.speaker, p.turn_idx, p.occurrences, p.span_words, p.whole_turn),
                         (True, "approximate", "participant", 1, 1, 30, False))
        self.assertEqual(eligible(p, "removed"), (True, "ok"))
        self.assertEqual(eligible(p, "full"), (True, "ok"))

    def test_quote_across_two_paragraphs_of_one_speaker(self):
        q = TXA[20:] + TXB[:15]
        q[3] = "zzz"
        p = locate_quote(" ".join(q), T, "n1", "node")
        self.assertEqual((p.found, p.tier, p.turn_idx), (True, "approximate", 1))

    def test_never_across_a_speaker_change(self):
        q = words(10, tag="i")[4:] + TXA[:14]
        p = locate_quote(" ".join(q), T, "n1", "node")
        self.assertEqual((p.found, p.tier), (False, "unfound"))

    def test_interviewer_approximate_and_mapping_talk_flag(self):
        p = locate_quote(near(words(25, tag="j"), 2), T, "n1", "node")
        self.assertEqual((p.found, p.tier, p.speaker, p.mapping_talk_turn), (True, "approximate", "interviewer", True))
        self.assertEqual(eligible(p, "removed"), (False, "not_participant"))

    def test_guard_mismatch_is_not_found(self):
        t = Transcript("t", [Turn(0, "participant", " ".join(TXA[:12] + ["not"] + TXA[12:]))], "synthetic")
        self.assertFalse(locate_quote(" ".join(TXA), t, "n1", "node").found)

    def test_wrong_transcript_is_not_found(self):
        other = Transcript("o", [Turn(0, "participant", " ".join(words(60, tag="y")))], "synthetic")
        for q in (" ".join(TXA[:20]), near(TXA)):
            self.assertFalse(locate_quote(q, other, "n1", "node").found)

    def test_not_found_has_tier_unfound(self):
        for q in (None, "", "..."):
            self.assertEqual(locate_quote(q, T, "n1", "node").tier, "unfound")

    def test_same_verdict_as_the_text_entry_point_used_by_the_validator(self):
        for q in (" ".join(TXA[:12]), near(TXA), " ".join(words(10, tag="i")[4:] + TXA[:14]), "nothing like it here ok"):
            want = locate_quote_tiered(T.text("full"), q)[0]
            self.assertEqual(locate_quote(q, T, "n1", "node").tier, want, q)

    def test_provenance_for_carries_the_tier(self):
        g = MapGraph("m", "ai", [Node("n1", "A", "service", None, "experienced", near(TXA)),
                                 Node("n2", "B", "service", None, "experienced", " ".join(TXB[:12]))],
                     [Edge("n1", "n2", "experienced", True, near(TXC))])
        pv = provenance_for(g, T)
        self.assertEqual({k: v.tier for k, v in pv.items()},
                         {"n1": "approximate", "n2": "exact", "e:n1->n2": "approximate"})


class TestQuoteSupports(unittest.TestCase):
    def test_approximate_quote_supports_a_label(self):
        self.assertTrue(_quote_supports(near(TXA), "p0w p1w p2w", T))

    def test_exact_still_works_and_cross_speaker_does_not(self):
        self.assertTrue(_quote_supports(" ".join(TXA[:12]), "p0w p1w p2w", T))
        self.assertFalse(_quote_supports(" ".join(words(10, tag="i")[4:] + TXA[:14]), "x", T))
        self.assertFalse(_quote_supports(" ".join(words(30, tag="y")), "y0w y1w", T))

    def test_triviality_rule_still_applies_to_approximate_quotes(self):
        # an approximate quote is long, so the content-token rule is met; a guard mismatch is still rejected
        t = Transcript("t", [Turn(0, "participant", " ".join(TXA[:12] + ["never"] + TXA[12:]))], "synthetic")
        self.assertFalse(_quote_supports(" ".join(TXA), "p0w p1w", t))


class TestAuditTiers(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.tr = {"W1": transcript(" ".join(TXA), " ".join(TXB))}

    def put_map(self, quotes, edge_quote=None):
        nodes = [(f"n{i}", f"Step {i}", "experienced", q) for i, q in enumerate(quotes)]
        edges = [("n0", "n1", edge_quote)] if edge_quote else []
        put(self.work, "recon", "W1__full__p0__t0__r0", {"status": "ok", "map": ai_map(nodes, edges)})

    def test_approximate_quotes_pass_and_are_counted_apart(self):
        self.put_map([" ".join(TXA[:12]), near(TXA), near(TXB, 7)], edge_quote=" ".join(TXB[:10]))
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["quotes_checked"], 4)
        self.assertEqual((r.counts["quotes_exact"], r.counts["quotes_approximate"]), (2, 2))
        self.assertEqual(r.counts["disagree_textnorm_yes_regex_no"], 0)

    def test_unfound_quotes_block_above_the_validator_share(self):
        # L16: the audit's blocking share mirrors the validator's max_unfound_share, 0.50 (was 0.10)
        self.put_map([" ".join(TXA[:12])] * 5 + ["words that nobody ever said"] * 5)         # 5 of 10: tolerated
        self.assertTrue(audit_recon_outputs(self.work, self.tr).passed)
        self.put_map([" ".join(TXA[:12])] * 4 + ["words that nobody said"] * 5 + ["nor these either"])
        r = audit_recon_outputs(self.work, self.tr)
        self.assertFalse(r.passed)
        self.assertTrue(any(f.startswith("quote_share_regex") for f in r.failures), r.failures)

    def test_a_quote_from_another_transcript_blocks(self):
        self.put_map([" ".join(words(30, tag="y"))])
        self.assertFalse(audit_recon_outputs(self.work, self.tr).passed)


class TestTrapCarrier(unittest.TestCase):
    def test_exact_and_approximate_copies_of_a_trap_sentence_are_carriers(self):
        from second_scribe.report import quote_carries_sentence
        s = " ".join(TXA)
        self.assertTrue(quote_carries_sentence(" ".join(TXA[5:20]), s))      # fragment of the sentence (old rule)
        self.assertTrue(quote_carries_sentence(s + " " + " ".join(TXB), s))  # sentence inside a longer quote (old rule)
        self.assertTrue(quote_carries_sentence(near(TXA), s))                # run 2: a near copy still carries it
        self.assertFalse(quote_carries_sentence(" ".join(TXC), s))
        self.assertFalse(quote_carries_sentence(near(TXA, 3).replace("p1w", "not p1w"), s))   # a guard mismatch does not


if __name__ == "__main__":
    unittest.main()
