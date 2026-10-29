"""Off-record criterion made two-sided (A6 Important 3): recall of off-record labels AND recall of mentioned labels,
on a pooled off-record set of at least 100 labels (the bench adds corpora of further seeds until it has them)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe import judge_bench as jb
from second_scribe.audit.registry import AuditLog
from second_scribe.judge_bench import NullClient, OracleClient, bench, pass_rule
from second_scribe.llm import ModelResponse
from second_scribe.schemas import MODALITIES

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
CORPUS = ROOT / "demo" / "corpus"


def good():
    return {"modality": {"accuracy": 0.95, "recall": {c: 0.9 for c in MODALITIES},
                         "precision": {c: 0.9 for c in MODALITIES}, "style_gap": 0.01},
            "alignment": {"strict": {"f1": 0.9}},
            "off_record": {"recall": 0.9, "mention_recall": 0.9, "n_off_record": 120},
            "not_judged_rate": 0.0}


class TestConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(jb.PASS_OFF_RECORD_MENTION_RECALL, 0.85)
        self.assertEqual(jb.PASS_OFF_RECORD_MIN_N, 100)
        rule = jb.pass_rule_thresholds()
        self.assertEqual(rule["off_record_mention_recall_min"], jb.PASS_OFF_RECORD_MENTION_RECALL)
        self.assertEqual(rule["off_record_min_n"], jb.PASS_OFF_RECORD_MIN_N)


class TestPassRuleOffRecord(unittest.TestCase):
    def test_mention_recall_floor(self):
        r = good()
        r["off_record"]["mention_recall"] = 0.85
        self.assertEqual(pass_rule(r), (True, []))
        r["off_record"]["mention_recall"] = 0.849
        ok, reasons = pass_rule(r)
        self.assertFalse(ok)
        self.assertEqual(reasons, ["off-record mention recall 0.849 < 0.85"])

    def test_the_pool_must_have_the_minimum_size(self):
        r = good()
        r["off_record"]["n_off_record"] = 100
        self.assertEqual(pass_rule(r), (True, []))
        r["off_record"]["n_off_record"] = 99
        ok, reasons = pass_rule(r)
        self.assertFalse(ok)
        self.assertEqual(reasons, ["off-record set has 99 labels, fewer than 100"])
        r["off_record"]["min_n"] = 0         # data cannot lower the floor: it is PASS_OFF_RECORD_MIN_N, always
        self.assertFalse(pass_rule(r)[0])
        r["off_record"]["min_n"] = 5000      # nor raise it
        r["off_record"]["n_off_record"] = 100
        self.assertEqual(pass_rule(r), (True, []))

    def test_a_per_seed_row_is_not_held_to_the_pool_size(self):
        r = good()
        r["off_record"]["n_off_record"] = 7
        self.assertFalse(pass_rule(r)[0])
        self.assertEqual(pass_rule(r, check_pool_size=False), (True, []))

    def test_missing_values_fail_closed(self):
        for key in ("mention_recall", "n_off_record"):
            r = good()
            r["off_record"][key] = None
            self.assertFalse(pass_rule(r)[0], key)
            del r["off_record"][key]
            self.assertFalse(pass_rule(r)[0], key)


class NeverFinds(OracleClient):
    """The audit probe: right on everything, but answers "not in the transcript" (quote null) for every label."""

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        if "TRANSCRIPT:" in messages[-1]["content"]:
            return NullClient().complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens)
        return super().complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, params=params)


class TestBenchOffRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.oracle = bench(OracleClient(seeds=(0, 1, 2)), PROMPTS, None, AuditLog(Path(cls.tmp) / "a.jsonl", "a"),
                           mode="logprob", seeds=(0, 1, 2))
        cls.never = bench(NeverFinds(seeds=(0, 1, 2)), PROMPTS, None, AuditLog(Path(cls.tmp) / "b.jsonl", "b"),
                          mode="logprob", seeds=(0, 1, 2))

    def test_the_pool_is_grown_to_at_least_the_minimum_with_corpora_of_further_seeds(self):
        o = self.oracle["off_record"]
        self.assertGreaterEqual(o["n_off_record"], jb.PASS_OFF_RECORD_MIN_N)
        self.assertEqual(o["min_n"], jb.PASS_OFF_RECORD_MIN_N)
        extra = o["extra_seeds"]
        self.assertTrue(extra)
        self.assertEqual(extra[0], 3)                               # the next seed after the benched ones
        self.assertEqual(extra, list(range(3, 3 + len(extra))))
        per_seed = self.oracle["per_seed"]
        self.assertEqual(self.oracle["seeds"], ["0", "1", "2"])
        for k in ("tp", "fp", "fn", "tn"):                          # pooled = per-seed rows + the extra corpora
            self.assertGreaterEqual(o["counts"][k], sum(p["off_record"]["counts"][k] for p in per_seed.values()))
        self.assertEqual(o["extra_counts"]["tn"] + o["extra_counts"]["fp"] + sum(
            p["off_record"]["n_off_record"] for p in per_seed.values()), o["n_off_record"])
        self.assertEqual(self.oracle["pass"], {"passed": True, "reasons": []})

    def test_the_extra_corpora_add_off_record_only_not_modality_or_alignment_items(self):
        r = self.oracle
        self.assertEqual(r["n_items"], sum(p["modality"]["n_all"] for p in r["per_seed"].values()))
        self.assertEqual(r["alignment"]["strict"]["n_truth"],
                         sum(p["alignment"]["strict"]["n_truth"] for p in r["per_seed"].values()))

    def test_a_judge_that_never_finds_anything_in_the_transcript_now_fails(self):
        o = self.never["off_record"]
        self.assertEqual(o["recall"], 1.0)                          # it still "finds" every off-record label absent
        self.assertLess(o["mention_recall"], 0.1)
        self.assertFalse(self.never["pass"]["passed"])
        self.assertTrue(any("off-record mention recall" in x for x in self.never["pass"]["reasons"]))

    def test_mention_recall_has_an_interval_and_the_report_shows_it_with_the_pool_size(self):
        o = self.oracle["off_record"]
        self.assertEqual(len(o["mention_recall_ci"]), 2)
        self.assertLessEqual(o["mention_recall_ci"][0], o["mention_recall"])
        md = jb.render_markdown(self.oracle)
        self.assertIn("Off-record mention recall", md)
        self.assertIn(f">= {jb.PASS_OFF_RECORD_MENTION_RECALL}", md)
        self.assertIn(f"n={o['n_off_record']}", md)
        self.assertIn("extra corpora", md)
        json.dumps(self.oracle)

    def test_the_keyword_floor_row_is_not_grown_and_says_the_floor_is_uninformative(self):
        md = jb.render_markdown(self.oracle)
        self.assertIn("reference only", md.lower())


class TestSingleCorpus(unittest.TestCase):
    def test_a_single_corpus_is_not_grown_and_says_so(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = bench(OracleClient(CORPUS), PROMPTS, CORPUS, AuditLog(Path(tmp) / "l.jsonl", "l"), mode="logprob")
        o = r["off_record"]
        self.assertEqual((o["min_n"], o["extra_seeds"]), (0, []))
        self.assertLess(o["n_off_record"], jb.PASS_OFF_RECORD_MIN_N)
        self.assertFalse(r["pass"]["passed"])          # M-F5-1: a run that cannot select a judge never prints PASS
        self.assertTrue(any("fewer than 100" in x and "single-corpus" in x for x in r["pass"]["reasons"]),
                        r["pass"]["reasons"])
        self.assertEqual(r["pass"], dict(zip(("passed", "reasons"), pass_rule(r))))
        self.assertTrue(all(p["pass"]["passed"] for p in r["per_seed"].values()))   # the seed rows still judge the metrics
        self.assertIn("single corpus", jb.render_markdown(r).lower())


if __name__ == "__main__":
    unittest.main()
