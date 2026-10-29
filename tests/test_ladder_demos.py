"""Ladder item 1: the held-out demo set (remedy vs placebo). Demos are never scored."""
import json
import re
import unittest

from second_scribe.reconstruct import validate_recon
from second_scribe.synth import bank
from second_scribe.synth.demos import (DEMO_NGRAM_N, INTRO, PLACEBO_DEMOS, REMEDY_DEMOS, demo_ngrams, demo_texts,
                                       render_demos)
from second_scribe.textnorm import contains_quote, tokens, word_count

_PLACEHOLDER = re.compile(r"\{(role|domain|phases|schema|transcript|definitions)\}")
NODE_KEYS = {"id", "label", "type", "phase", "status", "quote"}
EDGE_KEYS = {"source", "target", "status", "quote"}


def _wished(d):
    f = d.fragment
    return sum(1 for x in f["nodes"] + f["edges"] if x["status"] == "wished")


def _items(demos):
    return sum(len(d.fragment["nodes"]) + len(d.fragment["edges"]) for d in demos)


def _bank_strings():
    out, seen = [], set()

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k); walk(v)
        elif isinstance(x, (list, tuple, set, frozenset)):
            for v in x:
                walk(v)
        elif hasattr(x, "__dataclass_fields__") and not isinstance(x, type):
            for f in x.__dataclass_fields__:
                walk(getattr(x, f))
    for name in dir(bank):
        if not name.startswith("__"):
            v = getattr(bank, name)
            if id(v) not in seen:
                seen.add(id(v)); walk(v)
    return out


class TestDemoShape(unittest.TestCase):
    def test_two_demos_per_arm(self):
        self.assertEqual(len(REMEDY_DEMOS), 2)
        self.assertEqual(len(PLACEBO_DEMOS), 2)

    def test_fragments_are_exactly_the_v2_schema_and_verbatim(self):
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            f = d.fragment
            self.assertEqual(set(f), {"nodes", "edges", "emotion_word"}, d.demo_id)
            for n in f["nodes"]:
                self.assertEqual(set(n), NODE_KEYS, d.demo_id)
                self.assertLessEqual(len(n["label"].split()), 8, d.demo_id)
            for e in f["edges"]:
                self.assertEqual(set(e), EDGE_KEYS, d.demo_id)
            # the repo's own validator accepts it against the excerpt, with no unfound quote at all
            self.assertIsNone(validate_recon(json.dumps(f), arm="full", transcript_text=d.excerpt,
                                             max_unfound_share=0.0), d.demo_id)
            for x in f["nodes"] + f["edges"]:
                self.assertGreaterEqual(word_count(x["quote"]), 8, (d.demo_id, x["quote"]))
                turns = [t for t in d.excerpt.split("\n") if t.startswith("[P] ")]
                self.assertTrue(any(contains_quote(t[4:], x["quote"]) for t in turns),
                                f"{d.demo_id}: quote not inside ONE participant turn: {x['quote']}")
            self.assertTrue(d.demo_id.startswith(("REM", "PLA")), d.demo_id)

    def test_rendered_block_has_no_template_placeholder(self):
        for arm in ("remedy", "placebo"):
            self.assertIsNone(_PLACEHOLDER.search(render_demos(arm)), arm)

    def test_each_demo_appears_in_its_rendered_block(self):
        for arm, demos in (("remedy", REMEDY_DEMOS), ("placebo", PLACEBO_DEMOS)):
            block = render_demos(arm)
            for d in demos:
                self.assertIn(d.excerpt, block)
        with self.assertRaises(ValueError):
            render_demos("other")


class TestDemoMatching(unittest.TestCase):
    def test_length_within_two_percent(self):
        a, b = len(render_demos("remedy")), len(render_demos("placebo"))
        self.assertLessEqual(abs(a - b) / a, 0.02, (a, b))

    def test_same_number_of_items_and_same_wished_share(self):
        self.assertEqual(_items(REMEDY_DEMOS), _items(PLACEBO_DEMOS))
        self.assertEqual(sum(map(_wished, REMEDY_DEMOS)), sum(map(_wished, PLACEBO_DEMOS)))
        self.assertGreaterEqual(sum(map(_wished, REMEDY_DEMOS)), 2)   # at least one per demo
        for r, p in zip(REMEDY_DEMOS, PLACEBO_DEMOS):
            self.assertEqual(_wished(r), _wished(p), (r.demo_id, p.demo_id))

    def test_remedy_wished_labels_use_the_third_person_narrator_frame(self):
        for d in REMEDY_DEMOS:
            self.assertTrue(d.note.startswith("The participant says"), d.demo_id)
            self.assertGreaterEqual(_wished(d), 1)

    def test_placebo_has_no_narrator_frame(self):
        block = render_demos("placebo").lower()
        self.assertNotIn("the participant", block)
        self.assertNotIn("according to", block)
        self.assertNotIn("participant says", block)

    def test_placebo_wishes_are_not_a_missing_official_step(self):
        # placebo: every step the interviewer asks about happened; the only wish is a side matter
        for d in PLACEBO_DEMOS:
            wished = [n for n in d.fragment["nodes"] if n["status"] == "wished"]
            self.assertTrue(wished, d.demo_id)
            self.assertTrue(d.unrelated_wish, d.demo_id)
        for d in REMEDY_DEMOS:
            self.assertFalse(d.unrelated_wish, d.demo_id)


class TestDemoNeverScored(unittest.TestCase):
    def test_other_domain_and_no_shared_concept_with_the_synthetic_bank(self):
        strings = _bank_strings()
        self.assertTrue(strings)
        bank_text = " ".join(strings)
        bank_tokens = set(tokens(bank_text))
        forms = {" ".join(tokens(f)) for c in bank.CONCEPTS for f in c.forms}
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            for n in d.fragment["nodes"]:
                lab = " ".join(tokens(n["label"]))
                self.assertNotIn(lab, forms, (d.demo_id, lab))
                shared = set(tokens(n["label"])) & bank_tokens
                # a shared word is only tolerated when it is a function word
                from second_scribe.textnorm import STOPWORDS
                self.assertFalse(shared - STOPWORDS, (d.demo_id, n["label"], shared))
        # no 4-gram of any demo text occurs in the bank
        bank_grams = set()
        for s in strings:
            t = tokens(s)
            bank_grams.update(tuple(t[i:i + 4]) for i in range(len(t) - 3))
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            for text in demo_texts(d):
                t = tokens(text)
                for i in range(len(t) - 3):
                    self.assertNotIn(tuple(t[i:i + 4]), bank_grams, (d.demo_id, text))


# Review C1: the demos must sit far from the real study's domain (health, disability, children, school, care,
# government services, benefits). Word stems matched at a word start; the list is deliberately broad.
FORBIDDEN = re.compile(
    r"\b(autis\w*|diagnos\w*|therap\w*|school\w*|child\w*|kid\w*|daughters?|sons?|baby|babies|hospital\w*|clinic\w*|"
    r"doctors?|nurs\w*|pharmac\w*|medic\w*|health\w*|patients?|interpret\w*|translat\w*|fund\w*|wait ?lists?|"
    r"government\w*|ministr\w*|care\w*|carer\w*|disab\w*|parent\w*|mother\w*|father\w*|famil\w*|brother\w*|"
    r"sister\w*|wife|husband|teacher\w*|principal\w*|student\w*|social|workers?|benefits?|welfare|support\w*|"
    r"assess\w*|referral\w*|special\w*|therapist\w*|counsel\w*|caseworker\w*|housing|province|provincial|"
    r"ontario|canad\w*|disorder\w*|symptom\w*|treatment\w*|surgery|operation|discharge\w*|enrol\w*|"
    r"registration|portuguese|language|immigra\w*|refugee\w*|income|subsid\w*|grant)\b", re.I)


class TestFarDomain(unittest.TestCase):
    def test_no_forbidden_vocabulary_anywhere_in_the_demo_text(self):
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            for text in demo_texts(d) + [d.setting, d.note]:
                m = FORBIDDEN.search(text)
                self.assertIsNone(m, (d.demo_id, m and m.group(0), text))
        self.assertIsNone(FORBIDDEN.search(INTRO))

    def test_the_forbidden_list_actually_bites(self):
        for bad in ("a hospital discharge", "my daughter", "the school office", "an interpreter", "the waitlist",
                    "a social worker", "his disability", "Autism clinic", "the ministry", "child care"):
            self.assertIsNotNone(FORBIDDEN.search(bad), bad)

    def test_the_two_domains_are_not_the_same_and_the_twins_share_a_domain(self):
        self.assertNotEqual(REMEDY_DEMOS[0].setting, REMEDY_DEMOS[1].setting)
        for r, p in zip(REMEDY_DEMOS, PLACEBO_DEMOS):
            self.assertEqual(r.setting, p.setting)


class TestDemoTextsAndGramBoundaries(unittest.TestCase):
    """Review M1: pin what demo_texts holds and that n-grams never span two pieces of text."""

    def test_demo_texts_hold_turns_labels_quotes_and_the_note(self):
        for d in REMEDY_DEMOS + PLACEBO_DEMOS:
            texts = demo_texts(d)
            for turn in d.excerpt.split("\n"):
                self.assertIn(turn[4:], texts)
            for n in d.fragment["nodes"]:
                self.assertIn(n["label"], texts)
            for x in d.fragment["nodes"] + d.fragment["edges"]:
                self.assertIn(x["quote"], texts)
            self.assertIn(d.note, texts)

    def test_note_and_label_wording_reaches_the_ngram_set(self):
        grams = demo_ngrams(n=2)
        note_tokens = tokens(REMEDY_DEMOS[0].note)
        self.assertIn(tuple(note_tokens[:2]), grams)
        label_tokens = tokens(REMEDY_DEMOS[0].fragment["nodes"][2]["label"])
        self.assertIn(tuple(label_tokens[:2]), demo_ngrams(n=2, arms=("remedy",)))
        # a label only exists as a label (3 words) so a 3-gram of it must be present
        self.assertIn(tuple(tokens("Lost bag report")), demo_ngrams(n=3))

    def test_no_ngram_spans_two_pieces_of_text(self):
        for n in (2, 3, 6):
            expected = set()
            for arm in ("remedy", "placebo"):
                for d in (REMEDY_DEMOS if arm == "remedy" else PLACEBO_DEMOS):
                    for text in demo_texts(d):
                        t = tokens(text)
                        expected.update(tuple(t[i:i + n]) for i in range(len(t) - n + 1))
            self.assertEqual(set(demo_ngrams(n=n)), expected, n)
        d = REMEDY_DEMOS[0]
        a, b = tokens(d.excerpt.split("\n")[0][4:]), tokens(d.excerpt.split("\n")[1][4:])
        bridge = tuple(a[-3:] + b[:3])
        self.assertNotIn(bridge, demo_ngrams())

    def test_a_text_of_exactly_n_tokens_gives_one_gram(self):
        # the first n tokens of a quote and its last n tokens are both in the set (no off-by-one at either end)
        words = tokens(REMEDY_DEMOS[0].fragment["nodes"][1]["quote"])
        grams = demo_ngrams()
        self.assertIn(tuple(words[:6]), grams)
        self.assertIn(tuple(words[-6:]), grams)


class TestDemoNgrams(unittest.TestCase):
    def test_ngrams_are_six_token_tuples_over_demo_text_not_schema_words(self):
        grams = demo_ngrams()
        self.assertEqual(DEMO_NGRAM_N, 6)
        self.assertTrue(grams)
        self.assertTrue(all(isinstance(g, tuple) and len(g) == 6 for g in grams))
        # a known stretch of the first remedy excerpt is in the set, normalised
        t = tokens(REMEDY_DEMOS[0].fragment["nodes"][0]["quote"])
        self.assertIn(tuple(t[:6]), grams)
        # JSON keys / schema boilerplate never produce a gram
        self.assertNotIn(("id", "n1", "label"), {g[:3] for g in grams})
        flat = " ".join(" ".join(g) for g in grams)
        self.assertNotIn("emotion_word", flat)

    def test_ngrams_cover_both_arms_and_n_is_adjustable(self):
        both = demo_ngrams()
        self.assertTrue(demo_ngrams(arms=("remedy",)) < both)
        self.assertTrue(demo_ngrams(arms=("placebo",)) < both)
        self.assertTrue(all(len(g) == 3 for g in demo_ngrams(n=3)))


if __name__ == "__main__":
    unittest.main()
