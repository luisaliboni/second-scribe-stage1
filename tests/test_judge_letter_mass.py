"""Letter mode: an item is judged only when the model put real probability on a letter (A-F)."""
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.judge import LETTER_MASS_MIN, judge_modality_logprob, letter_distribution, letter_mass
from second_scribe.llm import ModelResponse

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class Client:
    def __init__(self, top):
        self.top = top

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        return ModelResponse("x", "length", {}, 1, "h", logprobs=self.top)


def one(**kw):
    return {"element_id": "n0", "element": "Fire permit", "quote": "we waited for the fire permit", **kw}


class TestLetterMass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def records(self, record_type):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r["payload"] for r in recs if r["record_type"] == record_type]

    def test_the_floor_is_one_half(self):
        self.assertEqual(LETTER_MASS_MIN, 0.5)

    def test_letter_mass_is_the_raw_probability_of_the_letters_before_renormalising(self):
        self.assertAlmostEqual(letter_mass([["A", -0.2], [" B", -1.8], ["The", -0.1], ["a", -0.3]]),
                               math.exp(-0.2) + math.exp(-1.8))
        self.assertEqual(letter_mass(None), 0.0)
        self.assertEqual(letter_mass([["The", -0.1]]), 0.0)

    def test_markdown_opener_with_a_sliver_on_B_is_not_a_confident_wished(self):
        # the audit probe: first token "**" (p=0.999), "B" at p=0.00012. The old code gave wished with confidence 1.0.
        top = [["**", -0.001], ["The", -7.5], ["B", -9.0]]
        self.assertEqual(letter_distribution(top), {"actual": 0.0, "wished": 1.0, "negated": 0.0,
                                                    "hypothetical": 0.0, "other_person": 0.0, "unsupported": 0.0})
        stats = {}
        labels, dist, nj = judge_modality_logprob(self.log, Client(top), [one()], PROMPTS, item_id="S", stats=stats)
        self.assertEqual((nj, dist), ({"n0"}, {}))
        self.assertEqual(stats["low_letter_mass"], 1)
        self.assertEqual((stats["no_letter_token"], stats["no_logprobs"], stats["failed_calls"]), (0, 0, 0))
        failed = self.records("judge_logprob_failed")
        self.assertEqual([(f["item_ids"], f["reason"]) for f in failed], [(["n0"], "low_letter_mass")])
        self.assertAlmostEqual(failed[0]["letter_mass"], math.exp(-9.0))

    def test_mass_just_below_the_floor_is_not_judged_and_at_the_floor_is(self):
        below = [["A", math.log(0.49)], ["The", math.log(0.5)]]
        at = [["A", math.log(0.5)], ["The", math.log(0.4)]]
        _, _, nj = judge_modality_logprob(self.log, Client(below), [one()], PROMPTS, item_id="S1")
        self.assertEqual(nj, {"n0"})
        labels, dist, nj = judge_modality_logprob(self.log, Client(at), [one()], PROMPTS, item_id="S2")
        self.assertEqual((nj, labels["n0"]), (set(), "actual"))

    def test_the_mass_of_every_call_is_logged_and_summarised_in_stats(self):
        stats = {}
        top = [["B", math.log(0.9)], ["C", math.log(0.05)]]
        judge_modality_logprob(self.log, Client(top), [one(), {**one(), "element_id": "n1", "quote": "another"}],
                               PROMPTS, item_id="S", stats=stats)
        masses = [r["letter_mass"] for r in self.records("judge_letter_mass")]
        self.assertEqual(len(masses), 2)
        self.assertAlmostEqual(masses[0], 0.95)
        self.assertEqual(stats["letter_mass_n"], 2)
        self.assertAlmostEqual(stats["letter_mass_sum"], 1.9)
        self.assertEqual(stats["low_letter_mass"], 0)

    def test_low_mass_items_still_count_in_the_not_judged_gate_of_the_bench(self):
        from second_scribe.judge_bench import PASS_NOT_JUDGED, score_modality
        items = [{"element_id": "n0", "expected": "actual", "kind": "plain", "style": "prose"}]
        s = score_modality(items, {"n0": "unsupported"}, {"n0"})
        self.assertEqual(s["n_not_judged"], 1)
        self.assertEqual(PASS_NOT_JUDGED, 0.02)


if __name__ == "__main__":
    unittest.main()
