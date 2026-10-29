import unittest

from second_scribe.keywords import keyword_modality
from second_scribe.schemas import MODALITIES


class TestKeywordModality(unittest.TestCase):
    def check(self, quote, expected):
        self.assertEqual(keyword_modality(quote), expected, quote)

    def test_unsupported(self):
        for q in (None, "", "   \n"):
            self.check(q, "unsupported")

    def test_actual_default(self):
        self.check("I called the city office on Monday and waited two weeks.", "actual")

    def test_other_person(self):
        self.check("My cousin had to wait for months.", "other_person")
        self.check("my neighbour got a letter", "other_person")
        self.check("It happened to someone else entirely.", "other_person")
        self.check("Another family told us the same.", "other_person")
        self.check("Another owner gave up.", "other_person")
        self.check("My friends all went through it.", "other_person")

    def test_hypothetical(self):
        self.check("If they called me back I would go.", "hypothetical")
        self.check("It would have helped.", "hypothetical")
        self.check("I could have asked.", "hypothetical")
        self.check("They might send a form.", "hypothetical")
        self.check("Maybe a letter arrives.", "hypothetical")
        self.check("Suppose the form was ready.", "hypothetical")

    def test_negated(self):
        for q in ("They never called.", "They didn't call.", "They did not call.", "No one answered.",
                  "Nobody answered.", "It wasn't clear.", "They weren't helpful.", "It is not clear.",
                  "I couldn't reach them.", "We left without a form."):
            self.check(q, "negated")

    def test_more_negation_cues(self):
        for q in ("They don't call back.", "She doesn't answer.", "I can't reach them.", "We cannot apply online.",
                  "They won't help.", "It isn't clear.", "They aren't open.", "I haven't heard.",
                  "She hasn't replied.", "There was no answer.", "Nothing happened.", "None of them replied.",
                  "No reply came."):
            self.check(q, "negated")

    def test_no_is_a_whole_word(self):
        self.check("The notice came by post.", "actual")
        self.check("A casino near the nothingness", "actual")
        self.check("Piano lessons", "actual")

    def test_more_wished_cues(self):
        for q in ("I hope someone calls.", "We hoped for a letter.", "I was hoping for a clear answer.",
                  "It would be nice to have one number.", "It'd be nice to have one number."):
            self.check(q, "wished")

    def test_negated_wish_is_negated(self):
        for q in ("I didn't wish for anything.", "I never wanted anything special.", "I did not want a visit.",
                  "I don't want a callback.", "I did not hope for much.", "We never hoped for help."):
            self.check(q, "negated")

    def test_negated_wish_nouns_and_modals(self):
        for q in ("There was no hope of getting help.", "I had no hope.", "She had no wish to apply.",
                  "I am not hoping for much.", "I wouldn't want that.", "I would not want a visit.",
                  "We wouldn't wish that on anyone."):
            self.check(q, "negated")

    def test_negated_wish_plurals(self):
        self.check("We have no wishes.", "negated")
        self.check("I had no hopes for it.", "negated")
        self.check("She had no wish to apply.", "negated")

    def test_negated_wish_only_cancels_its_own_wish(self):
        self.check("Not hoping is hard, I hope they call.", "wished")           # one clause, two cues
        self.check("I didn't wish for anything. I hope they call.", "wished")   # a later sentence
        self.check("I never hoped for help but I wish there was one number.", "wished")   # across "but"
        self.check("I didn't wish for anything; they never called.", "negated")
        self.check("I had no hope, and I did not want a visit.", "negated")
        self.check("I did not hope for much, but nobody called.", "negated")
        self.check("I never wanted anything special. I would like a clear answer.", "wished")

    def test_hope_is_still_wished_when_negation_follows_it(self):
        self.check("I hope no one has to wait.", "wished")
        self.check("I have hope for a clear answer.", "wished")
        self.check("I would like a quick answer, not a form.", "wished")

    def test_wished_still_beats_other_negation(self):
        for q in ("I wish nobody had to wait.", "I hope no one else has to wait.",
                  "It would be nice to have no wait.",
                  "I hope they don't make me wait again."):
            self.check(q, "wished")

    def test_curly_apostrophe(self):
        self.check("They didn’t call.", "negated")

    def test_wished(self):
        for q in ("I wish someone had called.", "There should have been a number.",
                  "There should be one place to go.", "I would like a single contact.",
                  "We wanted there to be a clear step.", "I'd love a checklist.", "If only they had called."):
            self.check(q, "wished")

    def test_case_insensitive(self):
        self.check("NEVER again", "negated")
        self.check("MY COUSIN waited", "other_person")

    def test_whole_words_only(self):
        self.check("The notification arrived.", "actual")      # "not" only inside a longer word
        self.check("A gift card", "actual")                    # "if" inside "gift"
        self.check("The nobodyish clerk", "actual")
        self.check("We went to the wishing well", "actual")
        self.check("A knotted rope", "actual")

    def test_priority_order(self):
        # other_person beats everything
        self.check("My friend wished it never happened, if only.", "other_person")
        # hypothetical beats negated
        self.check("If they never call, I could have left.", "hypothetical")
        # hypothetical beats a lone wished cue
        self.check("I wish they might call.", "hypothetical")

    def test_wished_beats_negation(self):
        self.check("There should have been a person, but there wasn't.", "wished")
        self.check("I wish nobody had to wait.", "wished")
        self.check("I would like a clear step and not a maze.", "wished")

    def test_if_only_is_wished_not_hypothetical(self):
        self.check("If only they had called.", "wished")
        self.check("If only it were simpler, and it wasn't.", "wished")
        self.check("If they called me, I would go.", "hypothetical")

    def test_always_a_known_modality(self):
        for q in (None, "", "x", "never if wish my cousin"):
            self.assertIn(keyword_modality(q), MODALITIES)


if __name__ == "__main__":
    unittest.main()
