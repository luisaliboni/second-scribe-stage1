"""Run-2 review S2: polarity words missing from GUARD_WORDS let an approximate quote drop a negation."""
import unittest

from second_scribe.textnorm import GUARD_WORDS, locate_quote_tiered

REVIEW_LIST = ("dont didnt doesnt cant wont wouldnt couldnt shouldnt isnt wasnt arent werent havent hasnt hadnt aint "
               "nor neither none nowhere without hardly barely scarcely rarely seldom might may must").split()

L16_LIST = ("want wanted wants need needed needs hope hoped hopes if unless wishes wishing supposed").split()   # council round 10


def unfound_when_dropped(word, filler="x"):
    """Transcript says '<w1..w9> WORD <w10..w19>', quote drops WORD (20 words: ratio is 1.0 without it, 19/19)."""
    ws = [f"{filler}{i}w" for i in range(20)]
    quote = " ".join(ws)
    text = "[P] " + " ".join(ws[:10] + [word] + ws[10:])
    return locate_quote_tiered(text, quote)[0]


class TestGuardList(unittest.TestCase):
    def test_review_list_is_guarded(self):
        for w in REVIEW_LIST:
            self.assertIn(w, GUARD_WORDS, w)

    def test_apostrophe_less_contraction_is_not_dropped(self):
        self.assertEqual(locate_quote_tiered("[P] i didnt like the clinic", "i like the clinic")[0], "unfound")
        self.assertEqual(locate_quote_tiered("[P] i didn't like the clinic", "i like the clinic")[0], "unfound")

    def test_hardly_barely_nor_without_might_are_not_dropped(self):
        for w in ("hardly", "barely", "nor", "without", "might"):
            self.assertEqual(unfound_when_dropped(w), "unfound", w)

    def test_identical_guards_still_approximate(self):
        ws = [f"x{i}w" for i in range(20)]
        text = "[P] " + " ".join(ws[:10] + ["didnt"] + ws[10:])
        quote = " ".join(ws[:10] + ["didnt"] + ws[11:])        # one word slipped, guard kept
        self.assertEqual(locate_quote_tiered(text, quote)[0], "approximate")


if __name__ == "__main__":
    unittest.main()


class TestL16WantNeedGuards(unittest.TestCase):
    """Council round 10 (L16): "I need" can describe what happened, "I want" is a wish; a quote may not turn one into
    the other, drop either, or drop a condition ("if", "unless")."""

    def test_l16_list_is_guarded(self):
        for w in L16_LIST:
            self.assertIn(w, GUARD_WORDS, w)

    def test_dropping_any_l16_word_is_unfound(self):
        for w in L16_LIST:
            self.assertEqual(unfound_when_dropped(w), "unfound", w)

    def test_adding_any_l16_word_is_unfound(self):
        for w in L16_LIST:
            ws = [f"x{i}w" for i in range(20)]
            text = "[P] " + " ".join(ws)
            quote = " ".join(ws[:10] + [w] + ws[10:])
            self.assertEqual(locate_quote_tiered(text, quote)[0], "unfound", w)

    def test_a_quote_that_drops_wanted_is_unfound(self):
        text = "[P] i wanted somebody to explain the referral steps to me before the first appointment started"
        quote = "i somebody to explain the referral steps to me before the first appointment started"
        self.assertEqual(locate_quote_tiered(text, quote)[0], "unfound")

    def test_need_changed_to_want_is_unfound(self):
        text = "[P] i need somebody to explain the referral steps to me before the first appointment started"
        quote = "i want somebody to explain the referral steps to me before the first appointment started"
        self.assertEqual(locate_quote_tiered(text, quote)[0], "unfound")

    def test_one_ordinary_word_substituted_beside_the_same_guard_words_is_still_approximate(self):
        text = "[P] i needed somebody to explain the referral steps to me before the first appointment started"
        quote = "i needed someone to explain the referral steps to me before the first appointment started"
        self.assertEqual(locate_quote_tiered(text, quote)[0], "approximate")
