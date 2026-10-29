"""Lint: every generated corpus item's gold agrees with ``prompts/definitions.v2.txt`` (F6, gold correctness).

The definitions are the authority; the judge's precedence is actual > wished > negated > other_person > hypothetical.
Each check below states the definitions clause it relies on, and ``test_definitions_still_say_what_the_lint_assumes``
fails if the file stops saying it (so a changed definition forces a re-read of this lint). The lint reads the
generated files (transcripts, traps, gold, concepts), never the templates alone.
"""
import json
import re
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from second_scribe.judge_bench import FILL_CONCEPTS
from second_scribe.synth.bank import (ACKS, CONCEPTS, FILL_KINDS, SAFE_FILL_CIDS, TRAP_TEMPLATES,
                                      says_it_happened)
from second_scribe.synth.build import build_corpus
from second_scribe.transcripts import MAPPING_PATTERNS, flag_mapping_talk, parse_paragraphs, read_docx_text

_ROOT = Path(__file__).resolve().parent.parent
_BY_CID = {c.cid: c for c in CONCEPTS}
_FIRST = re.compile(r"\b(we|us|our|i|i'm|i'd)\b", re.I)
_WANT = re.compile(r"\b(wish|needed|wanted|hoped|hoping|should have|loved|longed)\b", re.I)
_DENY = re.compile(r"\b(never|nobody|didn't|did not|without|skipped|wasn't)\b", re.I)
_OTHER = re.compile(r"\b(my (cousin|friend|neighbou?r|brother|sister|mother|father|uncle)|a friend of mine|"
                    r"another owner|someone else)\b", re.I)
_POSSIBLE = re.compile(r"\b(might|may|could|maybe|perhaps|possibly|if|should|what if)\b", re.I)
_PAST_ASSUMED = re.compile(r"\b(had|was|were|would have|could have|did)\b", re.I)
_GOT_THROUGH = re.compile(r"\b(took us through|walked us through|got through|went through|finished)\b", re.I)
_NOT_AFFIRMING_ATTEMPT = re.compile(r"\b(applied|application|grant|request|around|never heard)\b", re.I)
_AFFIRM_EVENT = re.compile(r"\b(happened|went through|we did)\b", re.I)
_MAPPING = [re.compile(p, re.I) for p in MAPPING_PATTERNS]


def _frame(tr):
    """The planted sentence with the concept wording blanked out: the rules read the template, not the filler
    (a wording such as "the garage test to see if the truck was fit" has its own "if" and "was")."""
    return re.sub(re.escape(_BY_CID[tr["cid"]].forms[1]), "X", tr["sentence"], flags=re.I)


def _read(d, sub, pid, suffix=".json"):
    return json.loads(Path(d, sub, pid + suffix).read_text(encoding="utf-8"))


class TestGoldLint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpora = {}
        for seed in (0, 1, 2, 3, 4):
            d = tempfile.mkdtemp(prefix="ss-fix-f6-")
            cls.addClassCleanup(shutil.rmtree, d, True)
            cls.corpora[seed] = (d, build_corpus(d, seed=seed))

    def items(self):
        """(seed, pid, trap, turns, gold nodes by cid) for every trap of every pooled corpus."""
        for seed, (d, meta) in self.corpora.items():
            for p in meta["participants"]:
                pid = p["pid"]
                t = flag_mapping_talk(parse_paragraphs(read_docx_text(Path(d, "transcripts", pid + ".docx")),
                                                       {"pt"}, pid))
                gold = _read(d, "gold", pid)
                cid_of = _read(d, "gold", pid, ".concepts.json")["gold"]
                nodes = {cid_of[n["node_id"]]: n for n in gold["nodes"]}
                for tr in _read(d, "traps", pid):
                    yield seed, pid, tr, t.turns, nodes

    # ---- the authority ---------------------------------------------------------------------------
    def test_definitions_still_say_what_the_lint_assumes(self):
        text = (_ROOT / "prompts" / "definitions.v2.txt").read_text(encoding="utf-8")
        for clause in ("experienced: the participant says this happened to them, or existed for them",
                       "An attempt the participant made",
                       "wished: the participant says they wanted or needed this",
                       "Not part of it: something the participant says did not happen to them, happened only to "
                       "someone else, or only might have happened"):
            self.assertIn(clause, text)

    # ---- the "it happened to the speaker" rule ---------------------------------------------------
    def test_own_rule_is_at_least_as_strict_as_the_bench_rule(self):
        self.assertTrue(SAFE_FILL_CIDS)
        self.assertLessEqual(SAFE_FILL_CIDS, {c.cid for c in FILL_CONCEPTS})
        for c in CONCEPTS:
            if c.cid in SAFE_FILL_CIDS:
                self.assertFalse(says_it_happened(c.forms[1]), c.cid)
        self.assertTrue(says_it_happened("the money we borrowed from the credit union"))
        self.assertTrue(says_it_happened("the visit when the health unit checked the truck over"))
        self.assertTrue(says_it_happened("the health official who came out to the truck"))
        self.assertFalse(says_it_happened("the letter saying the land could be used for vending"))

    def test_enough_safe_concepts_for_every_participant_role(self):
        for role in ("owner", "officer"):
            self.assertGreaterEqual(len([c for c in CONCEPTS if role in c.roles and c.cid in SAFE_FILL_CIDS]),
                                    len(FILL_KINDS) + 4, role)

    def test_fill_kinds_use_only_wordings_that_do_not_say_it_happened(self):
        seen = Counter()
        for seed, pid, tr, _turns, _nodes in self.items():
            if tr["kind"] in FILL_KINDS:
                seen[tr["kind"]] += 1
                self.assertNotIn(tr["cid"], {c.cid for c in CONCEPTS if says_it_happened(c.forms[1])},
                                 (seed, pid, tr["kind"], tr["sentence"]))
        for k in FILL_KINDS:
            self.assertGreaterEqual(seen[k], 15, k)

    # ---- per kind: sentence and gold against the definitions ----------------------------------------
    def test_wished_says_wanted_and_gold_is_wished(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "wished":
                continue
            n += 1
            self.assertRegex(_frame(tr), _WANT, (seed, pid))
            self.assertEqual((tr["expected_modality"], nodes[tr["cid"]]["status"]), ("wished", "wished"))
        self.assertGreaterEqual(n, 15)

    def test_negated_denies_without_wanting_and_is_not_in_gold(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "negated":
                continue
            n += 1
            self.assertRegex(_frame(tr), _DENY, (seed, pid))
            self.assertNotRegex(_frame(tr), _WANT, (seed, pid))
            self.assertNotRegex(_frame(tr), _OTHER, (seed, pid))
            self.assertEqual(tr["expected_modality"], "negated")
            self.assertNotIn(tr["cid"], nodes, (seed, pid))      # "did not happen to them" is not part of the journey
        self.assertGreaterEqual(n, 15)

    def test_other_person_is_about_someone_else_and_never_denies_the_speaker(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "other_person":
                continue
            n += 1
            self.assertRegex(_frame(tr), _OTHER, (seed, pid))
            # a denial of the speaker ("..., but we didn't", "not us") would make the label negated (negated outranks
            # other_person): no first-person word and no denial word at all
            self.assertNotRegex(_frame(tr), _FIRST, (seed, pid, tr["sentence"]))
            self.assertNotRegex(_frame(tr), _DENY, (seed, pid, tr["sentence"]))
            self.assertNotRegex(_frame(tr), _WANT, (seed, pid))
            self.assertNotIn(tr["cid"], nodes, (seed, pid))
        self.assertGreaterEqual(n, 15)

    def test_hypothetical_is_a_possibility_that_assumes_nothing_happened(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "hypothetical":
                continue
            n += 1
            self.assertRegex(_frame(tr), _POSSIBLE, (seed, pid))
            self.assertNotRegex(_frame(tr), _PAST_ASSUMED, (seed, pid, tr["sentence"]))
            self.assertNotRegex(_frame(tr), _WANT, (seed, pid))
            self.assertNotRegex(_frame(tr), _DENY, (seed, pid))
            self.assertNotIn(tr["cid"], nodes, (seed, pid))
        self.assertGreaterEqual(n, 15)

    def test_workaround_says_they_got_through_it_so_gold_is_experienced(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "workaround":
                continue
            n += 1
            self.assertRegex(_frame(tr), _GOT_THROUGH, (seed, pid, tr["sentence"]))
            self.assertNotRegex(_frame(tr), _NOT_AFFIRMING_ATTEMPT, (seed, pid, tr["sentence"]))
            self.assertEqual((tr["expected_modality"], nodes[tr["cid"]]["status"]), ("actual", "experienced"))
        self.assertGreaterEqual(n, 15)

    def test_dead_end_says_the_step_happened_and_what_followed_went_nowhere(self):
        n = 0
        for seed, pid, tr, _turns, nodes in self.items():
            if tr["kind"] != "dead_end":
                continue
            n += 1
            self.assertRegex(_frame(tr), _GOT_THROUGH, (seed, pid, tr["sentence"]))
            self.assertNotRegex(_frame(tr), _NOT_AFFIRMING_ATTEMPT, (seed, pid, tr["sentence"]))
            self.assertEqual((tr["expected_modality"], nodes[tr["cid"]]["status"]), ("actual", "experienced"))
        self.assertGreaterEqual(n, 15)

    def test_interviewer_voiced_is_affirmed_by_the_participant(self):
        n = 0
        for seed, pid, tr, turns, nodes in self.items():
            if tr["kind"] != "interviewer_voiced":
                continue
            n += 1
            i = next(k for k, x in enumerate(turns) if x.text == tr["sentence"])
            self.assertEqual(turns[i + 1].speaker, "participant")
            self.assertRegex(turns[i + 1].text, r"(?i)\byes\b|\bright\b", (seed, pid))
            self.assertEqual((tr["expected_modality"], nodes[tr["cid"]]["status"]), ("actual", "experienced"))
        self.assertGreaterEqual(n, 15)

    def test_mapping_talk_gold_rests_on_a_reply_that_says_it_happened(self):
        self.assertTrue(ACKS["mapping_talk"])
        for a in ACKS["mapping_talk"]:
            self.assertRegex(a, _AFFIRM_EVENT, a)
            self.assertFalse(any(r.search(a) for r in _MAPPING), a)       # the reply is not itself mapping talk
        n = 0
        for seed, pid, tr, turns, nodes in self.items():
            if tr["kind"] != "mapping_talk":
                continue
            n += 1
            i = next(k for k, x in enumerate(turns) if x.text == tr["sentence"])
            self.assertTrue(turns[i].mapping_talk and turns[i].speaker == "interviewer")
            self.assertEqual(turns[i + 1].speaker, "participant")
            self.assertIn(turns[i + 1].text, ACKS["mapping_talk"])
            self.assertEqual((tr["expected_modality"], nodes[tr["cid"]]["status"]), ("actual", "experienced"))
        self.assertGreaterEqual(n, 15)

    def test_every_template_of_the_lint_kinds_passes_the_sentence_rules(self):
        """Static check of the templates themselves, so a bad template fails even if no seed picks it."""
        x = "the form"
        for t in TRAP_TEMPLATES["hypothetical"]:
            s = t.format(x=x)
            self.assertRegex(s, _POSSIBLE, t)
            self.assertNotRegex(s, _PAST_ASSUMED, t)
        for t in TRAP_TEMPLATES["other_person"]:
            s = t.format(x=x)
            self.assertRegex(s, _OTHER, t)
            self.assertNotRegex(s, _FIRST, t)
            self.assertNotRegex(s, _DENY, t)
        for k in ("workaround", "dead_end"):
            for t in TRAP_TEMPLATES[k]:
                s = t.format(x=x)
                self.assertRegex(s, _GOT_THROUGH, t)
                self.assertNotRegex(s, _NOT_AFFIRMING_ATTEMPT, t)

    # ---- lint self-test: it must be able to fail -------------------------------------------------------
    def test_lint_rejects_the_old_templates(self):
        self.assertRegex("If we had applied in winter, maybe the x would have been faster.", _PAST_ASSUMED)
        self.assertRegex("My cousin got the x, but we didn't.", _FIRST)
        self.assertRegex("A friend of mine went through the x, not us.", _FIRST)
        self.assertRegex("We got around the x through a friend with a licence.", _NOT_AFFIRMING_ATTEMPT)
        self.assertRegex("The grant application for the x just went nowhere.", _NOT_AFFIRMING_ATTEMPT)
        self.assertIsNone(_AFFIRM_EVENT.search("Okay."))
        self.assertIsNone(_AFFIRM_EVENT.search("Sure, go ahead."))


if __name__ == "__main__":
    unittest.main()
