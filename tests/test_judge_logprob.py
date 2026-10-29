import json
import math
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.judge import JudgeCache, judge_modality_logprob, letter_distribution, letter_prompt_fingerprint
from second_scribe.llm import ModelResponse
from second_scribe.prompts import load_definitions, load_system
from second_scribe.schemas import MODALITIES

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
LETTERS = "ABCDEF"


class LetterClient:
    """Answers one item per call: ``fn(user_text) -> list of [token, logprob]`` (or None for a server that
    returns no logprobs). Records every call's arguments."""

    def __init__(self, fn, finish="length"):
        self.fn, self.finish, self.calls = fn, finish, []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                           "params": params})
        top = self.fn(messages[-1]["content"])
        return ModelResponse(top[0][0].strip() if top else "A", self.finish, {}, 7, "h", logprobs=top)


def sure(letter, lp=-0.05):
    """Top-logprobs that put (almost) all mass on ``letter``."""
    other = LETTERS[(LETTERS.index(letter) + 1) % 6]
    return [[letter, lp], [other, -4.0], ["The", -6.0]]


def one(element="Fire permit", quote="we waited for the fire permit", **kw):
    return {"element_id": "n0", "element": element, "quote": quote, **kw}


class TestLetterDistribution(unittest.TestCase):
    def test_renormalised_over_the_letters_found_and_other_tokens_ignored(self):
        d = letter_distribution([["A", -0.2], [" B", -1.8], ["\nB", -5.0], ["The", -0.1], ["AB", -0.3], ["G", -0.5]])
        self.assertEqual(set(d), set(MODALITIES))
        pa, pb = math.exp(-0.2), math.exp(-1.8) + math.exp(-5.0)      # " B" and "\nB" both count for B
        self.assertAlmostEqual(d["actual"], pa / (pa + pb))
        self.assertAlmostEqual(d["wished"], pb / (pa + pb))
        self.assertAlmostEqual(sum(d.values()), 1.0)
        self.assertEqual(d["unsupported"], 0.0)

    def test_lower_case_letters_are_not_answers(self):
        d = letter_distribution([["A", -0.2], ["a", -0.1], [" b", -0.1]])    # "a" is the article
        self.assertEqual((d["actual"], d["wished"]), (1.0, 0.0))
        self.assertIsNone(letter_distribution([["a", -0.1], ["b", -0.2]]))

    def test_none_when_no_letter_or_no_logprobs(self):
        self.assertIsNone(letter_distribution(None))
        self.assertIsNone(letter_distribution([]))
        self.assertIsNone(letter_distribution([["The", -0.1], ["I", -2.0], ["G", -3.0]]))


class TestJudgeModalityLogprob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def records(self, record_type):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r["payload"] for r in recs if r["record_type"] == record_type]

    def test_letter_mapping_and_the_request_that_is_sent(self):
        client = LetterClient(lambda u: sure("E"))
        labels, dist, nj = judge_modality_logprob(self.log, client, [one()], PROMPTS, item_id="S")
        self.assertEqual(labels, {"n0": "other_person"})
        self.assertEqual(nj, set())
        self.assertEqual(max(dist["n0"], key=dist["n0"].get), "other_person")
        self.assertEqual(len(client.calls), 1)
        c = client.calls[0]
        self.assertEqual((c["temperature"], c["max_tokens"]), (0.0, 1))
        self.assertEqual(c["params"], {"logprobs": True, "top_logprobs": 10})
        system, user = c["messages"]
        self.assertEqual(system, {"role": "system", "content": load_system(PROMPTS, "judge", "v2")})
        self.assertIn(load_definitions(PROMPTS, "v2"), user["content"])
        self.assertIn("ELEMENT: Fire permit", user["content"])
        self.assertIn('QUOTE: "we waited for the fire permit"', user["content"])
        self.assertIn("Answer with one letter only", user["content"])
        self.assertNotIn("{definitions}", user["content"])
        self.assertNotIn("{item}", user["content"])
        # the six letters are the six labels in MODALITIES order
        for letter, label in zip(LETTERS, MODALITIES):
            self.assertIn(f"{letter}. {label}:", user["content"])

    def test_the_attempt_line_is_in_the_shared_definitions_and_every_prompt_that_uses_them(self):
        line = ("- An attempt the participant made (applying, asking, a visit they went through) counts as experienced "
                "even if it failed or led nowhere; the thing they were trying to get counts as experienced only if "
                "they say they got it; if they wanted or needed it and did not get it, it is wished.")
        self.assertIn(line, load_definitions(PROMPTS, "v2"))
        self.assertEqual(load_definitions(PROMPTS, "v2").count("\n"), 3)          # one line added: 4 bullets
        client = LetterClient(lambda u: sure("A"))
        judge_modality_logprob(self.log, client, [one()], PROMPTS, item_id="S")
        self.assertIn(line, client.calls[0]["messages"][-1]["content"])

    def test_each_letter_gives_its_label(self):
        for letter, label in zip(LETTERS, MODALITIES):
            labels, _, _ = judge_modality_logprob(self.log, LetterClient(lambda u, l=letter: sure(l)), [one()],
                                                  PROMPTS, item_id=f"S{letter}")
            self.assertEqual(labels["n0"], label)

    def test_interviewer_quote_gets_its_reply_cut_to_60_words_and_participant_quote_does_not(self):
        seen = []
        client = LetterClient(lambda u: seen.append(u) or sure("A"))
        reply = " ".join(f"w{k}" for k in range(80))
        judge_modality_logprob(self.log, client, [one(speaker="interviewer", reply=reply),
                                                  {**one(speaker="participant", reply=reply), "element_id": "n1",
                                                   "quote": "other quote"}], PROMPTS, item_id="S")
        self.assertIn("QUOTE (interviewer)", seen[0])
        self.assertIn('REPLY (participant): "' + " ".join(f"w{k}" for k in range(60)) + '"', seen[0])
        self.assertNotIn("w60", seen[0])
        self.assertNotIn("REPLY (participant)", seen[1])

    def test_braces_in_the_item_survive(self):
        seen = []
        judge_modality_logprob(self.log, LetterClient(lambda u: seen.append(u) or sure("A")),
                               [one(element="X {item} {definitions}", quote="I said {item} and {")], PROMPTS,
                               item_id="S")
        self.assertIn('ELEMENT: X {item} {definitions}', seen[0])
        self.assertIn('QUOTE: "I said {item} and {"', seen[0])

    def test_server_without_logprobs_judges_nothing_and_never_guesses_from_the_text(self):
        client = LetterClient(lambda u: None)          # text is "A", but there are no logprobs
        items = [one(), {**one(), "element_id": "n1", "quote": "another quote"}]
        stats = {}
        labels, dist, nj = judge_modality_logprob(self.log, client, items, PROMPTS, item_id="S", stats=stats)
        self.assertEqual(nj, {"n0", "n1"})
        self.assertEqual(dist, {})
        self.assertEqual(stats["no_logprobs"], 2)
        failed = self.records("judge_logprob_failed")
        self.assertEqual([(f["item_ids"], f["reason"]) for f in failed],
                         [(["n0"], "no_logprobs"), (["n1"], "no_logprobs")])      # one record per call

    def test_no_letter_among_the_top_logprobs_is_not_judged(self):
        labels, dist, nj = judge_modality_logprob(self.log, LetterClient(lambda u: [["The", -0.1], ["I", -1.0]]),
                                                  [one()], PROMPTS, item_id="S")
        self.assertEqual((nj, dist), ({"n0"}, {}))

    def test_a_whitespace_first_token_is_no_letter_token_not_a_failed_call(self):
        stats = {}
        client = LetterClient(lambda u: [["\n", -0.1], [" ", -2.0]])        # text "" after strip
        labels, dist, nj = judge_modality_logprob(self.log, client, [one()], PROMPTS, item_id="S", stats=stats)
        self.assertEqual((nj, dist), ({"n0"}, {}))
        self.assertEqual((stats["no_letter_token"], stats["failed_calls"], stats["no_logprobs"]), (1, 0, 0))
        self.assertEqual(self.records("judge_logprob_failed")[0]["reason"], "no_letter_token")
        self.assertEqual(self.records("call_response")[0]["logprobs"][0], ["\n", -0.1])    # the logprobs were read
        # a letter among the alternatives of a whitespace-first reply is still read (when it holds at least half the mass)
        _, dist, nj = judge_modality_logprob(self.log, LetterClient(lambda u: [["\n", -2.0], ["B", -0.3]]), [one()],
                                             PROMPTS, item_id="S2")
        self.assertEqual((nj, max(dist["n0"], key=dist["n0"].get)), (set(), "wished"))

    def test_a_failing_transport_is_not_judged_not_a_crash(self):
        def boom(u):
            raise ConnectionError("down")
        _, _, nj = judge_modality_logprob(self.log, LetterClient(boom), [one()], PROMPTS, item_id="S")
        self.assertEqual(nj, {"n0"})
        self.assertEqual(len(self.records("call_error")), 1)

    def test_empty_quote_is_unsupported_without_a_call(self):
        client = LetterClient(lambda u: sure("A"))
        labels, dist, nj = judge_modality_logprob(self.log, client, [one(quote="  ")], PROMPTS, item_id="S")
        self.assertEqual((labels, nj, len(client.calls)), ({"n0": "unsupported"}, set(), 0))

    def test_duplicates_are_sent_once_and_each_id_gets_the_answer(self):
        client = LetterClient(lambda u: sure("C"))
        items = [one(), {**one(), "element_id": "n1"}, {**one(), "element_id": "n2", "quote": "different"}]
        labels, dist, _ = judge_modality_logprob(self.log, client, items, PROMPTS, item_id="S")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(labels, {"n0": "negated", "n1": "negated", "n2": "negated"})
        self.assertEqual(set(dist), {"n0", "n1", "n2"})

    def test_cache_hits_are_not_sent_and_carry_no_distribution(self):
        cache = JudgeCache(Path(self.tmp) / "c.json")
        judge_modality_logprob(self.log, LetterClient(lambda u: sure("B")), [one()], PROMPTS, item_id="S",
                               cache=cache)
        client = LetterClient(lambda u: sure("A"))
        labels, dist, nj = judge_modality_logprob(self.log, client, [one()], PROMPTS, item_id="S2", cache=cache)
        self.assertEqual((labels, dist, nj, len(client.calls)), ({"n0": "wished"}, {}, set(), 0))
        # the letter prompt has its own fingerprint: the generative cache entries do not collide with it
        from second_scribe.judge import prompt_fingerprint
        self.assertNotEqual(letter_prompt_fingerprint(PROMPTS), prompt_fingerprint(PROMPTS))

    def test_logprobs_are_recorded_in_the_audit_log(self):
        judge_modality_logprob(self.log, LetterClient(lambda u: sure("A", -0.1)), [one()], PROMPTS, item_id="S")
        resp = self.records("call_response")[0]
        self.assertEqual(resp["logprobs"][0], ["A", -0.1])
        req = self.records("call_request")[0]
        self.assertEqual((req["max_tokens"], req["params"]["logprobs"], req["params"]["top_logprobs"]), (1, True, 10))


if __name__ == "__main__":
    unittest.main()
