"""Run-2 quote rule (council round 8, item 1): ``textnorm.locate_quote_tiered`` / ``locate_quote_in_turns``.

exact       = ``contains_quote`` (unchanged), approximate = >= 0.85 in-order word match inside ONE same-speaker turn
(consecutive lines with the same [I]/[P] marker merged, never across a marker change), matched span <= 1.5 x the
quote, every "..." part inside that turn, guard words (negations, modals, wish) equal in the quote and the span.
"""
import unittest

from second_scribe import textnorm
from second_scribe.textnorm import (APPROX_MAX_SPAN, APPROX_MIN_RATIO, GUARD_WORDS, contains_quote,
                                    locate_quote_in_turns, locate_quote_tiered, tokens)

# 20 neutral content words (no guard word, no stopword trickery needed); numbered variants are built from them
BASE = ("we waited six weeks for the first appointment at the clinic downtown and then a coordinator phoned "
        "about the referral paperwork").split()


def words(n, start=0, tag="x"):
    """n distinct neutral words; no guard word can appear."""
    return [f"{tag}{start + i}w" for i in range(n)]


def line(marker, ws):
    return f"[{marker}] " + " ".join(ws)


def tier(text, quote):
    return locate_quote_tiered(text, quote)


class TestConstants(unittest.TestCase):
    def test_constants_are_module_level(self):
        self.assertEqual(APPROX_MIN_RATIO, 0.85)                 # L16, council round 10 (was 0.95)
        self.assertEqual(APPROX_MAX_SPAN, 1.5)
        for w in ("not", "no", "never", "would", "wish", "wished", "could", "should", "cannot", "can't", "won't",
                  "didn't", "don't", "wasn't", "isn't", "nothing", "nobody"):
            self.assertIn(w, GUARD_WORDS)
        for w in ("want", "wanted", "wants", "need", "needed", "needs", "hope", "hoped", "hopes", "if", "unless",
                  "wishes", "wishing", "supposed"):             # L16: wish / need / condition words
            self.assertIn(w, GUARD_WORDS)


class TestExactUnchanged(unittest.TestCase):
    def test_exact_iff_contains_quote(self):
        text = "\n".join([line("I", words(8, tag="q")), line("P", BASE), line("P", words(10, tag="r"))])
        quotes = [" ".join(BASE[:12]), "We WAITED six weeks, for the first appointment", "xx yy",
                  " ".join(BASE[:5]) + " ... " + " ".join(BASE[12:]), " ".join(BASE[:5]) + " ... zz", "", "...",
                  " ".join(words(3, tag="r")) + " ... " + " ".join(BASE[:4])]
        for q in quotes:
            t, _info = tier(text, q)
            if contains_quote(text, q):
                self.assertEqual(t, "exact", q)
            else:
                self.assertNotEqual(t, "exact", q)

    def test_exact_with_ellipsis_parts_in_different_turns_is_no_longer_exact(self):
        # run-2 review B1: contains_quote on the whole text would accept it; the tier rule wants ONE turn.
        text = "\n".join([line("P", BASE), line("I", words(5, tag="q")), line("P", words(20, tag="r"))])
        q = " ".join(BASE[:6]) + " ... " + " ".join(words(20, tag="r")[:6])
        self.assertTrue(contains_quote(text, q))
        self.assertEqual(tier(text, q)[0], "unfound")

    def test_exact_works_without_markers(self):
        t, _ = tier(" ".join(BASE), " ".join(BASE[2:10]))
        self.assertEqual(t, "exact")


class TestApproximate(unittest.TestCase):
    def setUp(self):
        self.turn = words(30, tag="a")

    def test_one_substituted_word_in_a_long_quote_is_approximate(self):
        quote = list(self.turn)
        quote[10] = "zzz"
        t, info = tier(line("P", self.turn), " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertAlmostEqual(info["ratio"], 29 / 30)

    def test_omitted_middle_word_is_approximate_when_the_rest_is_in_order(self):
        quote = self.turn[:12] + self.turn[13:]            # a word of the turn left out of the quote
        t, info = tier(line("P", self.turn), " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertEqual(info["span_tokens"], 30)
        self.assertEqual(info["quote_tokens"], 29)

    def test_interviewer_turn_can_be_approximate_too(self):
        quote = list(self.turn)
        quote[3] = "zzz"
        self.assertEqual(tier(line("I", self.turn), " ".join(quote))[0], "approximate")


class TestThresholdEdges(unittest.TestCase):
    def test_exactly_085_passes(self):
        turn = words(20, tag="a")
        quote = list(turn)
        quote[3] = quote[9] = quote[15] = "zzz"                # 17 / 20 = 0.85
        t, info = tier(line("P", turn), " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertAlmostEqual(info["ratio"], 0.85)

    def test_just_below_085_fails(self):
        turn = words(19, tag="a")
        quote = list(turn)
        quote[3] = quote[9] = quote[15] = "zzz"                # 16 / 19 = 0.842
        t, info = tier(line("P", turn), " ".join(quote))
        self.assertEqual(t, "unfound")
        self.assertEqual(info["reason"], "below_threshold")

    def test_substitutions_allowed_by_length(self):
        # council round 10: quotes < 7 words differ by 0, 7-13 words by 1, 14-19 by 2, 20 by 3
        for n, allowed in ((6, 0), (7, 1), (13, 1), (14, 2), (19, 2), (20, 3)):
            for subs in (allowed, allowed + 1):
                with self.subTest(words=n, substituted=subs):
                    turn = words(n, tag="a")
                    quote = list(turn)
                    for k in range(subs):
                        quote[1 + 2 * k] = "zzz"
                    want = ("exact" if subs == 0 else "approximate") if subs <= allowed else "unfound"
                    self.assertEqual(tier(line("P", turn), " ".join(quote))[0], want)

    def test_ten_word_quote_one_substituted_is_approximate_two_is_unfound(self):
        turn = words(10, tag="a")
        one = list(turn)
        one[4] = "zzz"
        two = list(one)
        two[7] = "yyy"
        self.assertEqual(tier(line("P", turn), " ".join(one))[0], "approximate")     # 9 / 10 = 0.90
        t, info = tier(line("P", turn), " ".join(two))                                # 8 / 10 = 0.80
        self.assertEqual((t, info["reason"]), ("unfound", "below_threshold"))

    def test_twenty_word_quote_three_substituted_is_approximate(self):
        turn = words(20, tag="a")
        quote = list(turn)
        quote[2] = quote[8] = quote[14] = "zzz"
        self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "approximate")


class TestSpanLimit(unittest.TestCase):
    def split_turn(self, gap):
        a, b, filler = words(10, tag="a"), words(10, 10, tag="a"), words(gap, tag="f")
        return line("P", a + filler + b), " ".join(a + b)

    def test_span_at_the_limit_passes(self):
        text, quote = self.split_turn(10)                  # span 30 == 1.5 x 20
        t, info = tier(text, quote)
        self.assertEqual(t, "approximate")
        self.assertEqual(info["span_tokens"], 30)

    def test_span_over_the_limit_fails(self):
        text, quote = self.split_turn(11)                  # span 31 > 30
        t, info = tier(text, quote)
        self.assertEqual(t, "unfound")
        self.assertEqual(info["reason"], "span")

    def test_a_stray_early_match_does_not_inflate_the_span(self):
        # one quote word (also) occurs far earlier in the turn; the alignment must use the dense cluster
        turn = words(30, tag="a")
        quote = list(turn)
        quote[4] = "stray"
        text = line("P", ["stray"] + words(40, tag="f") + turn)
        t, info = tier(text, " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertLessEqual(info["span_tokens"], 45)


class TestSameSpeakerWindow(unittest.TestCase):
    def test_never_crosses_an_i_p_change(self):
        q_end, a_start = words(12, tag="a"), words(12, 12, tag="a")
        text = "\n".join([line("I", words(5, tag="f") + q_end), line("P", a_start + words(5, tag="g"))])
        quote = " ".join(q_end + a_start)
        self.assertEqual(tier(text, quote)[0], "unfound")

    def test_same_text_in_one_speaker_is_found(self):
        q_end, a_start = words(12, tag="a"), words(12, 12, tag="a")
        text = "\n".join([line("P", words(5, tag="f") + q_end), line("P", a_start + words(5, tag="g"))])
        quote = " ".join(q_end + a_start)
        t, info = tier(text, quote)
        self.assertEqual(t, "approximate")            # the "[P]" tag between the lines breaks contains_quote
        self.assertEqual((info["unit_start"], info["unit_end"]), (0, 1))

    def test_multi_line_turn_with_one_wrong_word(self):
        l1, l2, l3 = words(15, tag="a"), words(15, 15, tag="a"), words(15, 30, tag="a")
        text = "\n".join([line("I", words(6, tag="q")), line("P", l1), line("P", l2), line("P", l3), line("I", words(4, tag="q2"))])
        quote = l1[8:] + l2 + l3[:8]
        quote[20] = "zzz"
        t, info = tier(text, " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertEqual((info["unit_start"], info["unit_end"]), (1, 3))

    def test_continuation_lines_belong_to_the_preceding_marker(self):
        l1, cont = words(15, tag="a"), words(15, 15, tag="a")
        text = line("P", l1) + "\n" + " ".join(cont) + "\n" + line("I", words(5, tag="q"))
        quote = l1[5:] + cont
        quote[3] = "zzz"
        t, info = tier(text, " ".join(quote))
        self.assertEqual(t, "approximate")
        self.assertEqual(info["unit_start"], 0)

    def test_continuation_does_not_leak_into_the_next_speaker(self):
        l1, cont = words(15, tag="a"), words(15, 15, tag="a")
        text = line("P", l1) + "\n" + line("I", words(3, tag="q") + cont)
        quote = l1[5:] + cont
        quote[3] = "zzz"
        self.assertEqual(tier(text, " ".join(quote))[0], "unfound")

    def test_unknown_speaker_lines_are_not_merged(self):
        l1, l2 = words(20, tag="a"), words(20, 20, tag="a")
        text = "[?] " + " ".join(l1) + "\n[?] " + " ".join(l2)
        quote = l1[5:] + l2[:10]
        quote[3] = "zzz"
        self.assertEqual(tier(text, " ".join(quote))[0], "unfound")
        q1 = list(l1)
        q1[3] = "zzz"
        self.assertEqual(tier(text, " ".join(q1))[0], "approximate")     # inside one [?] line it works

    def test_without_markers_approximate_is_unavailable(self):
        turn = words(30, tag="a")
        quote = list(turn)
        quote[3] = "zzz"
        t, info = tier(" ".join(turn), " ".join(quote))
        self.assertEqual(t, "unfound")
        self.assertEqual(info["reason"], "no_markers")


class TestEllipsisParts(unittest.TestCase):
    def parts(self):
        a, b = words(20, tag="a"), words(20, 20, tag="a")
        return a, b

    def test_parts_in_one_turn_in_order(self):
        a, b = self.parts()
        qa = list(a)
        qa[5] = "zzz"                                       # part A 19/20, part B exact: not exact overall
        text = line("P", a + words(6, tag="f") + b)
        t, info = tier(text, " ".join(qa) + " ... " + " ".join(b))
        self.assertEqual(t, "approximate")
        self.assertEqual(info["span_tokens"], 40)           # the 6 words left out between the parts do not count

    def test_parts_in_different_turns_of_the_same_speaker_fail(self):
        a, b = self.parts()
        qa = list(a)
        qa[5] = "zzz"
        text = "\n".join([line("P", a), line("I", words(4, tag="q")), line("P", b)])
        self.assertEqual(tier(text, " ".join(qa) + " ... " + " ".join(b))[0], "unfound")

    def test_parts_out_of_order_fail(self):
        a, b = self.parts()
        qa = list(a)
        qa[5] = "zzz"
        text = line("P", b + a)
        self.assertEqual(tier(text, " ".join(qa) + " ... " + " ".join(b))[0], "unfound")

    def test_a_part_missing_from_the_turn_fails_even_if_the_global_ratio_is_high(self):
        a = words(60, tag="a")
        short = words(2, tag="s")                           # 2 of 62 words: 0.968 overall without it
        qa = list(a)
        qa[5] = "zzz"
        text = line("P", a)
        t, info = tier(text, " ".join(qa) + " ... " + " ".join(short))
        self.assertEqual(t, "unfound")


class TestGuardWords(unittest.TestCase):
    def test_dropped_not_is_rejected(self):
        turn = words(12, tag="a") + ["not"] + words(12, 12, tag="a")
        quote = words(12, tag="a") + words(12, 12, tag="a")        # the quote forgets the "not"
        t, info = tier(line("P", turn), " ".join(quote))
        self.assertEqual(t, "unfound")
        self.assertEqual(info["reason"], "guard")

    def test_added_not_is_rejected(self):
        turn = words(25, tag="a")
        quote = turn[:12] + ["not"] + turn[12:]                    # 25 / 26 = 0.96 matched, but "not" is invented
        t, info = tier(line("P", turn), " ".join(quote))
        self.assertEqual(t, "unfound")
        self.assertEqual(info["reason"], "guard")

    def test_equal_guard_words_pass(self):
        turn = words(12, tag="a") + ["not"] + words(12, 12, tag="a")
        quote = list(turn)
        quote[3] = "zzz"                                            # 24 / 25 = 0.96, the "not" is in both
        self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "approximate")

    def test_every_guard_word_counts_in_both_directions(self):
        for g in sorted(GUARD_WORDS):
            with self.subTest(guard=g, direction="dropped"):
                turn = words(12, tag="a") + [g] + words(12, 12, tag="a")
                quote = words(12, tag="a") + words(12, 12, tag="a")
                self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "unfound")
            with self.subTest(guard=g, direction="added"):
                turn = words(25, tag="a")
                quote = turn[:12] + [g] + turn[12:]
                self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "unfound")

    def test_n_t_forms_are_guard_words(self):
        for g in ("haven't", "couldn't", "wouldn't", "aren't", "hasn't", "doesn't"):
            with self.subTest(g=g):
                turn = words(12, tag="a") + [g] + words(12, 12, tag="a")
                quote = words(12, tag="a") + words(12, 12, tag="a")
                self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "unfound")

    def test_curly_apostrophe_is_the_same_guard_word(self):
        turn = words(12, tag="a") + ["didn’t"] + words(12, 12, tag="a")
        quote = list(turn)
        quote[2] = "zzz"
        self.assertEqual(tier(line("P", turn), " ".join(quote))[0], "approximate")
        self.assertIn("didn't", tokens(" ".join(quote)))

    def test_guard_word_in_an_unmatched_part_of_the_turn_is_irrelevant(self):
        turn = words(30, tag="a")
        text = line("P", ["never"] + words(5, tag="f") + turn + words(5, tag="g") + ["not"])
        quote = list(turn)
        quote[10] = "zzz"
        self.assertEqual(tier(text, " ".join(quote))[0], "approximate")


class TestWrongTranscript(unittest.TestCase):
    def interview(self, tag):
        return "\n".join([line("I", words(10, tag=tag + "q")), line("P", words(40, tag=tag + "p")),
                          line("I", words(8, 10, tag=tag + "q")), line("P", words(35, 40, tag=tag + "p"))])

    def test_quote_scored_against_another_transcript_is_unfound(self):
        mine, other = self.interview("m"), self.interview("o")
        quote = " ".join(words(30, tag="mp"))
        self.assertEqual(tier(mine, quote)[0], "exact")
        t, _ = tier(other, quote)
        self.assertEqual(t, "unfound")
        # a near-miss copy of the quote is approximate on its own transcript and unfound on the other one
        near = words(30, tag="mp")
        near[4] = "zzz"
        self.assertEqual(tier(mine, " ".join(near))[0], "approximate")
        self.assertEqual(tier(other, " ".join(near))[0], "unfound")

    def test_sibling_transcript_with_a_fifth_of_the_words_changed_is_unfound(self):
        turn = words(40, tag="a")
        sibling = list(turn)
        for i in range(0, 40, 5):
            sibling[i] = f"other{i}"
        self.assertEqual(tier(line("P", sibling), " ".join(turn))[0], "unfound")


class TestDegenerateQuotes(unittest.TestCase):
    def test_empty_and_ellipsis_only(self):
        text = line("P", words(30, tag="a"))
        for q in ("", "   ", "...", " ... ... ", "!?"):
            self.assertEqual(tier(text, q)[0], "unfound", q)
            self.assertEqual(locate_quote_in_turns([("P", " ".join(words(30, tag="a")))], q)[0], "unfound", q)

    def test_empty_transcript(self):
        self.assertEqual(tier("", "some words here")[0], "unfound")


class TestInTurns(unittest.TestCase):
    """The per-paragraph entry point used by Task-6 bucketing and the in-transcript check."""

    def turns(self):
        return [("I", " ".join(words(8, tag="q"))), ("P", " ".join(words(15, tag="a"))),
                ("P", " ".join(words(15, 15, tag="a"))), ("I", " ".join(words(6, tag="r")))]

    def test_exact_lists_every_matching_paragraph(self):
        turns = self.turns() + [("P", " ".join(words(15, tag="a")))]
        t, info = locate_quote_in_turns(turns, " ".join(words(12, tag="a")))
        self.assertEqual((t, info["turn_indices"]), ("exact", [1, 4]))

    def test_exact_needs_one_paragraph_so_cross_paragraph_ellipsis_is_not_found(self):
        a, b = " ".join(words(8, tag="q")), " ".join(words(6, tag="r"))
        self.assertEqual(locate_quote_in_turns(self.turns(), a + " ... " + b)[0], "unfound")

    def test_approximate_merges_consecutive_same_speaker_paragraphs(self):
        quote = words(15, tag="a")[8:] + words(15, 15, tag="a")
        quote[2] = "zzz"
        t, info = locate_quote_in_turns(self.turns(), " ".join(quote))
        self.assertEqual((t, info["unit_start"], info["unit_end"]), ("approximate", 1, 2))

    def test_agrees_with_the_text_entry_point(self):
        turns = self.turns()
        text = "\n".join(f"[{m}] {t}" for m, t in turns)
        quote = words(15, tag="a")[8:] + words(15, 15, tag="a")
        quote[2] = "zzz"
        for q in (" ".join(quote), " ".join(words(12, tag="a")), " ".join(words(12, tag="a")) + " extra bits"):
            self.assertEqual(locate_quote_tiered(text, q)[0], locate_quote_in_turns(turns, q)[0], q)


if __name__ == "__main__":
    unittest.main()
