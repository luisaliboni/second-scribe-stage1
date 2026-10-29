import unittest

from second_scribe.textnorm import (STOPWORDS, contains_quote, content_tokens, normalize, stem,
                                    token_seq, tokens, word_count)


class TestTextnorm(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("  It’s the  “GP” — ok… "),
                         "it's the \"gp\" - ok...")

    def test_normalize_other_spaces_dashes_and_nfkc(self):
        self.assertEqual(normalize("a b c–d"), "a b c-d")
        self.assertEqual(normalize("ﬁne\n\tthing"), "fine thing")   # NFKC ligature, whitespace collapse
        self.assertEqual(normalize("‘hi’"), "'hi'")

    def test_tokens_and_stem(self):
        self.assertEqual(tokens("We waited, six weeks!"), ["we", "waited", "six", "weeks"])
        self.assertEqual(stem("waiting"), "wait")
        self.assertEqual(stem("waited"), "wait")
        self.assertEqual(stem("lists"), "list")
        self.assertEqual(stem("licences"), stem("licence"))   # no "es" rule
        self.assertEqual(stem("is"), "is")                    # short words untouched
        self.assertEqual(stem("bed"), "bed")
        self.assertEqual(stem("sing"), "sing")                # len 4: not > 4

    def test_tokens_strip_edge_apostrophes_keep_internal(self):
        self.assertEqual(tokens("the 'one-stop' office"), ["the", "one", "stop", "office"])
        self.assertEqual(tokens("it's the clients' forms, 'don't'"),
                         ["it's", "the", "clients", "forms", "don't"])
        self.assertEqual(tokens("' '' ''' x"), ["x"])    # lone quote marks are not tokens
        self.assertTrue(contains_quote("he said 'one-stop' was gone", "one-stop was gone"))
        self.assertTrue(contains_quote("one-stop was gone", "'one-stop' was gone"))

    def test_content_tokens(self):
        self.assertEqual(content_tokens("The waiting lists"), {"wait", "list"})
        self.assertIn("the", STOPWORDS)

    def test_word_count(self):
        self.assertEqual(word_count("We waited, six weeks!"), 4)
        self.assertEqual(word_count(""), 0)

    def test_token_seq_is_padded(self):
        self.assertEqual(token_seq("Hi, there!"), " hi there ")

    def test_contains_quote(self):
        hay = "So, we waited six weeks; then they lost our forms twice."
        self.assertTrue(contains_quote(hay, "we waited six weeks ... they lost our forms"))
        self.assertFalse(contains_quote(hay, "they lost our forms ... we waited"))   # order matters
        self.assertFalse(contains_quote(hay, "waite"))                              # whole words only

    def test_contains_quote_punctuation_case_and_ellipsis_char(self):
        hay = "We’d WAITED — six weeks; then they lost it."
        self.assertTrue(contains_quote(hay, "we'd waited six weeks … they lost it"))
        self.assertTrue(contains_quote(hay, "We'd waited, six weeks."))

    def test_contains_quote_parts_do_not_overlap(self):
        self.assertFalse(contains_quote("six weeks", "six weeks ... weeks"))
        self.assertTrue(contains_quote("six weeks weeks", "six weeks ... weeks"))
        self.assertTrue(contains_quote("a b", "a ... b"))      # adjacent words are fine

    def test_contains_quote_empty_quote_is_not_support(self):
        self.assertFalse(contains_quote("anything", ""))
        self.assertFalse(contains_quote("anything", " ... "))


if __name__ == "__main__":
    unittest.main()
