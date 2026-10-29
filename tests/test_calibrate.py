import json
import random
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.calibrate import calibrate_in_transcript, calibrate_matcher, calibrate_synthetic, perturb
from second_scribe.llm import MockClient
from second_scribe.match import SETTINGS_GRID, MatchSetting, lexical_sim
from second_scribe.synth.bank import CONCEPTS
from second_scribe.synth.build import build_corpus

LABELS = ["City business licence", "Waiting for the family doctor", "Fire inspection by the city",
          "Commissary kitchen rental", "Bank account opening", "Health permit application",
          "Insurance quote from broker", "Signage permit", "Hiring first employee", "Food handler certificate"]


def make_case(seed=0):
    right, truth = perturb(LABELS, random.Random(seed))
    pairs = {(truth[j], j) for j in truth if truth[j] is not None}
    return LABELS, right, pairs, truth


class TestPerturb(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(perturb(LABELS, random.Random(0)), perturb(LABELS, random.Random(0)))
        self.assertNotEqual(perturb(LABELS, random.Random(0))[0], perturb(LABELS, random.Random(1))[0])

    def test_does_not_touch_global_random(self):
        random.seed(5)
        before = random.random()
        random.seed(5)
        perturb(LABELS, random.Random(0))
        self.assertEqual(random.random(), before)

    def test_truth_map(self):
        out, truth = perturb(LABELS, random.Random(0))
        self.assertEqual(set(truth), set(range(len(out))))
        real = [v for v in truth.values() if v is not None]
        self.assertEqual(len(real), len(set(real)))                       # each original at most once
        self.assertEqual(sum(v is None for v in truth.values()), 2)       # 20% of 10 dropped -> 2 decoys
        self.assertEqual(len(real), 8)
        for j, orig in truth.items():
            if orig is None:
                self.assertTrue(all(lexical_sim(out[j], l) == 0.0 for l in LABELS))   # decoy shares nothing

    def test_raises_when_decoys_run_out(self):
        many = [f"item{i} thing" for i in range(100)]    # 20 dropped, only 12 decoys exist
        with self.assertRaises(ValueError):
            perturb(many, random.Random(0))
        # a label that shares a token with a decoy removes that decoy from the pool
        labels = [f"dental task{i}" for i in range(60)]   # 12 dropped, "dental cleaning appointment" unusable
        with self.assertRaises(ValueError):
            perturb(labels, random.Random(0))

    def test_some_labels_unperturbed_and_some_perturbed(self):
        out, truth = perturb(LABELS, random.Random(0))
        same = [j for j, o in truth.items() if o is not None and out[j] == LABELS[o]]
        self.assertTrue(0 < len(same) < 8)


class TestCalibrateMatcher(unittest.TestCase):
    def test_perfect_judge_recall_one_for_same(self):
        left, right, pairs, _ = make_case()
        res = calibrate_matcher([(left, right, pairs)], [{p: "same" for p in pairs}])
        self.assertEqual(set(res), {s.key() for s in SETTINGS_GRID})
        for s in SETTINGS_GRID:
            if s.judge_accept == "same":
                self.assertEqual(res[s.key()]["recall"], 1.0)
        self.assertEqual(set(res[SETTINGS_GRID[0].key()]), {"precision", "recall", "f1", "n_pairs"})

    def test_empty_judge_recall_is_unperturbed_share(self):
        left, right, pairs, truth = make_case()
        # perturbed labels that survived unchanged; for this seed no stopword/reversal perturbation leaves
        # lexical_sim at 1.0, so these are exactly the pairs a 0.99 threshold can recover
        survivors = sum(1 for (i, j) in pairs if right[j] == left[i])
        self.assertTrue(0 < survivors < len(pairs))
        res = calibrate_matcher([(left, right, pairs)], [{}], settings=(MatchSetting(0.99, "same"),))
        self.assertAlmostEqual(res["lex0.99_same"]["recall"], survivors / len(pairs))
        self.assertEqual(res["lex0.99_same"]["precision"], 1.0)

    def test_precision_counts_false_pairs(self):
        left = ["alpha one", "beta two"]
        right = ["alpha one", "beta two"]
        res = calibrate_matcher([(left, right, {(0, 0)})], [{}], settings=(MatchSetting(0.9, "none"),))
        r = res["lex0.9_none"]
        self.assertEqual((r["precision"], r["recall"], r["n_pairs"]), (0.5, 1.0, 2))
        self.assertAlmostEqual(r["f1"], 2 / 3)

    def test_no_predictions_and_no_truth(self):
        res = calibrate_matcher([(["a b"], ["c d"], set())], [{}], settings=(MatchSetting(0.9, "none"),))
        self.assertEqual(res["lex0.9_none"], {"precision": 0.0, "recall": 0.0, "f1": 0.0, "n_pairs": 0})


class TestCalibrateInTranscript(unittest.TestCase):
    def test_rates(self):
        cases = [([True, True, False, False], {0: True, 1: False, 2: True, 3: False}),
                 ([True, False], {0: True})]   # missing prediction counts as "not mentioned"
        r = calibrate_in_transcript(cases)
        # tp=2 (c1:0, c2:0), fp=1 (c1:2), fn=1 (c1:1), tn=2 (c1:3, c2:1)
        self.assertAlmostEqual(r["precision"], 2 / 3)
        self.assertAlmostEqual(r["recall"], 2 / 3)
        self.assertAlmostEqual(r["false_mention_rate"], 1 / 3)

    def test_empty(self):
        self.assertEqual(calibrate_in_transcript([]), {"precision": 0.0, "recall": 0.0, "false_mention_rate": 0.0})


def _numbered(block: str) -> list[tuple[int, str]]:
    return [(int(m[1]), m[2]) for m in (re.match(r"(\d+)\. (.*)$", l) for l in block.splitlines()) if m]


_GOLD = {c.forms[2]: c.cid for c in CONCEPTS}
_ENDORSED = {c.forms[0]: c.cid for c in CONCEPTS}
_SAID = {c.cid: c.forms[1].lower() for c in CONCEPTS}


def oracle(msgs) -> str:
    """Answers from the concept bank: true pairs for the align prompt; for the in_transcript prompt a verbatim
    turn (the transcript line without its speaker tag) that contains the concept's spoken wording, else null."""
    p = msgs[-1]["content"]
    if "LIST A:" in p:
        a = _numbered(p.split("LIST A:\n")[1].split("LIST B:\n")[0])
        b = _numbered(p.split("LIST B:\n")[1])
        return json.dumps({"pairs": [{"a": i, "b": j, "rel": "same"} for i, x in a for j, y in b
                                     if _GOLD[x] == _ENDORSED[y]]})
    lines = p.split("TRANSCRIPT:\n")[1].split("\nLABELS:\n")[0].splitlines()
    answers = []
    for n, label in _numbered(p.split("LABELS:\n")[1]):
        quote = next((t.split("] ", 1)[1] for t in lines if _SAID[_ENDORSED[label]] in t.lower()), None)
        answers.append({"n": n, "quote": quote})
    return json.dumps({"answers": answers})


def empty(msgs) -> str:
    """Judge that never proposes anything: valid answers, no pairs, no quotes."""
    p = msgs[-1]["content"]
    if "LIST A:" in p:
        return '{"pairs": []}'
    return json.dumps({"answers": [{"n": n, "quote": None} for n, _ in _numbered(p.split("LABELS:\n")[1])]})


class TestCalibrateSynthetic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.d, True)
        build_corpus(cls.d, seed=0)

    def run_with(self, fn, name):
        calls = []
        log = AuditLog(Path(self.d) / f"{name}.jsonl", name, clock=lambda: "t")
        res = calibrate_synthetic(self.d, log, MockClient(fn=lambda m: calls.append(m[-1]["content"]) or fn(m)))
        recs = [json.loads(l) for l in (Path(self.d) / f"{name}.jsonl").read_text().splitlines()]
        return res, calls, [r for r in recs if r["record_type"] == "judge_fallback"]

    def test_oracle_gives_perfect_recall(self):
        res, calls, fallbacks = self.run_with(oracle, "oracle")
        self.assertEqual(res["n_participants"], 12)
        self.assertEqual(set(res["matcher"]), {s.key() for s in SETTINGS_GRID})
        for s in SETTINGS_GRID:
            self.assertEqual(res["matcher"][s.key()]["recall"], 1.0, s.key())
        self.assertEqual(res["matcher"]["lex0.6_same"]["precision"], 1.0)
        self.assertEqual(res["in_transcript"]["recall"], 1.0)
        self.assertEqual(res["in_transcript"]["false_mention_rate"], 0.0)
        self.assertEqual(fallbacks, [])
        self.assertEqual(sum("LIST A:" in c for c in calls), 12)   # one align call per participant, reused for the grid

    def test_judge_fallbacks_are_counted_and_zero_for_a_clean_judge(self):
        res, _, _ = self.run_with(oracle, "clean")
        self.assertEqual(res["judge_fallbacks"], {"align": 0, "in_transcript": 0, "total": 0})
        res, _, _ = self.run_with(empty, "clean-empty")                 # valid empty answers are not fallbacks
        self.assertEqual(res["judge_fallbacks"]["total"], 0)

    def test_a_judge_that_answers_garbage_shows_its_fallbacks(self):
        res, _, fb = self.run_with(lambda m: "not json at all", "garbage")
        n = res["n_participants"]
        self.assertEqual(res["judge_fallbacks"], {"align": n, "in_transcript": n, "total": 2 * n})
        self.assertEqual(len(fb), 2 * n)                                 # the same records the helpers log

    def test_only_the_failing_helper_is_counted(self):
        def align_broken(msgs):
            return "garbage" if "LIST A:" in msgs[-1]["content"] else oracle(msgs)
        res, _, _ = self.run_with(align_broken, "align-broken")
        self.assertEqual(res["judge_fallbacks"], {"align": res["n_participants"], "in_transcript": 0,
                                                  "total": res["n_participants"]})

    def test_settings_argument_and_in_transcript_counts(self):
        log = AuditLog(Path(self.d) / "set.jsonl", "set", clock=lambda: "t")
        only = (MatchSetting(2.0, "same"), MatchSetting(0.6, "none"))
        res = calibrate_synthetic(self.d, log, MockClient(fn=oracle), settings=only)
        self.assertEqual(set(res["matcher"]), {"lex2_same", "lex0.6_none"})
        self.assertEqual(res["matcher"]["lex2_same"]["recall"], 1.0)      # the judge alone (no lexical help)
        self.assertEqual(res["matcher"]["lex2_same"]["precision"], 1.0)
        lex = res["matcher"]["lex0.6_none"]                               # lexical alone: exact but thin
        self.assertEqual(lex["precision"], 1.0)
        self.assertTrue(0.0 < lex["recall"] < 0.2, lex)
        mc = res["matcher_counts"]["lex2_same"]               # counts behind the rates, so seeds can be pooled
        self.assertEqual(set(mc), {"tp", "n_pred", "n_truth"})
        self.assertEqual(mc["tp"], mc["n_pred"])
        self.assertEqual(mc["tp"], mc["n_truth"])             # the oracle judge alone: perfect
        self.assertAlmostEqual(res["matcher"]["lex0.6_none"]["recall"],
                               res["matcher_counts"]["lex0.6_none"]["tp"] / mc["n_truth"])
        c = res["in_transcript_counts"]
        self.assertEqual(set(c), {"tp", "fp", "fn", "tn"})
        self.assertEqual((c["fp"], c["fn"]), (0, 0))
        self.assertGreater(c["tn"], 0)                                    # the off-record labels
        self.assertAlmostEqual(c["tp"] / (c["tp"] + c["fn"]), res["in_transcript"]["recall"])

    def test_lexical_floor_when_judge_says_nothing(self):
        res, _, fallbacks = self.run_with(empty, "empty")
        strict = res["matcher"]["lex0.6_same"]
        self.assertEqual(strict["precision"], 1.0)
        self.assertTrue(0.0 < strict["recall"] < 0.2, strict)
        self.assertEqual(res["matcher"]["lex0.7_same"]["n_pairs"], 0)    # wordings share too little for 0.7
        self.assertTrue(0.0 < res["in_transcript"]["recall"] < 0.2, res["in_transcript"])
        self.assertEqual(res["in_transcript"]["false_mention_rate"], 0.0)
        self.assertEqual(fallbacks, [])   # valid empty answers are not failures

    def test_unrelated_verbatim_quote_shows_up_as_false_mentions(self):
        # the quote rule checks that a quote is real and non-trivial, not that it is about the label, so a judge
        # quoting an unrelated turn for every label inflates false mentions; calibration must expose that
        def always_first_turn(msgs):
            p = msgs[-1]["content"]
            if "LIST A:" in p:
                return '{"pairs": []}'
            lines = p.split("TRANSCRIPT:\n")[1].split("\nLABELS:\n")[0].splitlines()
            q = next(t.split("] ", 1)[1] for t in lines if t.startswith("[P]"))
            return json.dumps({"answers": [{"n": n, "quote": q} for n, _ in _numbered(p.split("LABELS:\n")[1])]})
        res, _, _ = self.run_with(always_first_turn, "first")
        self.assertGreater(res["in_transcript"]["false_mention_rate"], 0.0)
        self.assertLess(res["in_transcript"]["precision"], 1.0)


if __name__ == "__main__":
    unittest.main()
