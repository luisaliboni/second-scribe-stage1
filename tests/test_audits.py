"""Deterministic auditors (Task 9A). Every "real" file here is a fixture the test creates in a temp dir."""
import contextlib
import copy
import io
import json
import math
import tempfile
import types
import unittest
from unittest import mock
from dataclasses import asdict
from pathlib import Path

from second_scribe import audits, prereg
from second_scribe.audits import (AuditResult, LeakIndex, audit_canary, audit_determinism, audit_judge_outputs,
                                  audit_leaks, audit_recon_outputs, audit_second_judge)
from second_scribe import pipeline
from second_scribe import cli as cli_module
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG
from second_scribe.cli import main as cli_main
from second_scribe.llm import MockClient
from second_scribe.pipeline import RunConfig
from second_scribe.schemas import Edge, MapGraph, Node, Transcript, Turn
from second_scribe.textnorm import contains_quote, tokens

SPEC = "full__p0__t0__r0"


def put(work: Path, stage: str, name: str, obj: dict) -> None:
    p = work / stage / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")


def transcript(*texts, speaker="participant") -> Transcript:
    return Transcript("T1", [Turn(i, speaker, t) for i, t in enumerate(texts)], "synthetic")


def ai_map(nodes, edges=(), origin="ai") -> dict:
    g = MapGraph("m", origin, [Node(i, t, "service", None, s, q) for i, t, s, q in nodes],
                 [Edge(a, b, "experienced", True, q) for a, b, q in edges])
    return asdict(g)


T_TEXT = "We waited a long time for the permit and then the officer called us back"


class TestReconAudit(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.tr = {"W1": transcript(T_TEXT, "It was a very hard summer for us all")}

    def put_map(self, d, status="ok", spec=SPEC, pid="W1"):
        put(self.work, "recon", f"{pid}__{spec}", {"status": status, "map": d})

    def good(self):
        return ai_map([("n1", "Permit wait", "experienced", "waited a long time for the permit"),
                       ("n2", "Call back", "experienced", "the officer called us back")],
                      [("n1", "n2", "then the officer called us back")])

    def test_a_clean_map_passes(self):
        self.put_map(self.good())
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue(r.passed, r.failures)
        self.assertTrue(r.blocking)
        self.assertEqual(r.counts["maps_audited"], 1)
        self.assertEqual(r.counts["quotes_checked"], 3)
        self.assertEqual(r.counts["disagree_textnorm_yes_regex_no"], 0)
        self.assertEqual(r.counts["disagree_textnorm_no_regex_yes"], 0)

    def test_an_edge_to_a_missing_node_blocks(self):
        d = self.good()
        d["edges"][0]["target"] = "n9"
        self.put_map(d)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertFalse(r.passed)
        self.assertTrue(r.blocking)
        self.assertTrue(any(f.startswith("edge_unknown_node") for f in r.failures), r.failures)

    def test_schema_violations_are_all_caught(self):
        cases = {
            "duplicate_node_id": lambda d: d["nodes"].append(dict(d["nodes"][0])),
            "bad_node_type": lambda d: d["nodes"][0].update(node_type="banana"),
            "bad_status": lambda d: d["nodes"][0].update(status="maybe"),
            "bad_phase": lambda d: d["nodes"][0].update(phase="Nowhere"),
            "bad_label": lambda d: d["nodes"][0].update(text=""),
            "self_loop": lambda d: d["edges"][0].update(target="n1"),
            "missing_quote": lambda d: d["nodes"][0].update(quote=""),
            "bad_origin": lambda d: d.update(origin="endorsed"),
            "no_nodes": lambda d: d.update(nodes=[], edges=[]),
        }
        for code, mutate in cases.items():
            with self.subTest(code):
                d = self.good()
                mutate(d)
                self.put_map(d)
                r = audit_recon_outputs(self.work, self.tr, phases=("Planning", "Permits"))
                self.assertFalse(r.passed)
                self.assertTrue(any(f.startswith(code) for f in r.failures), (code, r.failures))

    def test_ok_item_without_a_map_and_garbage_files_block(self):
        self.put_map(None)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertFalse(r.passed)
        (self.work / "recon" / "W1__x.json").write_text("{not json")
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue(any(f.startswith("unreadable_item") for f in r.failures))

    def test_a_failed_item_is_counted_not_audited(self):
        self.put_map(None, status="failed")
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue(r.passed)
        self.assertEqual(r.counts["maps_audited"], 0)
        self.assertEqual(r.counts["items_by_status"], {"failed": 1})

    def test_prior_only_maps_must_have_no_quotes_and_no_wished(self):
        g = ai_map([("n1", "Apply", "experienced", None)], origin="prior_only")
        put(self.work, "prior_only", "owner__prior_only__p0__t0.7__r0", {"status": "ok", "map": g})
        self.assertTrue(audit_recon_outputs(self.work, self.tr).passed)
        g["nodes"][0]["quote"] = "waited a long time"
        g["nodes"][0]["status"] = "wished"
        put(self.work, "prior_only", "owner__prior_only__p0__t0.7__r0", {"status": "ok", "map": g})
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue({"prior_quote", "prior_wished"} <= {f.split(":")[0] for f in r.failures}, r.failures)

    def test_script_prior_only_maps_are_audited_like_prior_only_maps(self):
        """Review M-4 (prereg/audits): the script sources' maps are the primary script behind the headline flag."""
        name = "judgeprior__owner__prior_only__p0__t0.7__r0"
        g = ai_map([("n1", "Apply", "experienced", None)], origin="prior_only")
        put(self.work, "script_prior_only", name, {"status": "ok", "map": g})
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["maps_audited"], 1)
        g["nodes"][0]["quote"] = "waited a long time"            # a script source has no transcript: no quote at all
        g["nodes"][0]["status"] = "wished"
        put(self.work, "script_prior_only", name, {"status": "ok", "map": g})
        r = audit_recon_outputs(self.work, self.tr)
        self.assertTrue({"prior_quote", "prior_wished"} <= {f.split(":")[0] for f in r.failures}, r.failures)

    def test_script_sources_can_be_left_out_of_the_recon_audit(self):
        """The Q group audits recon_outputs without them (they are drawn in J, so a resume must give the fresh bytes)."""
        self.put_map(self.good())
        g = ai_map([("n1", "Apply", "experienced", None)], origin="prior_only")
        put(self.work, "script_prior_only", "judgeprior__owner__prior_only__p0__t0.7__r0", {"status": "ok", "map": g})
        self.assertEqual(audit_recon_outputs(self.work, self.tr).counts["maps_audited"], 2)
        r = audit_recon_outputs(self.work, self.tr, script_sources=False)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["maps_audited"], 1)

    def test_disagreements_between_textnorm_and_the_regex_check_are_counted_both_ways(self):
        # textnorm cuts "café" to the token "caf", so the quote "caf" is found; a word-aware regex says no
        self.tr = {"W1": transcript("We met at the café on Friday and talked for an hour")}
        a = ai_map([("n1", "Met", "experienced", "met at the caf")])
        self.assertTrue(contains_quote(self.tr["W1"].text("full"), "met at the caf"))
        self.put_map(a)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertEqual(r.counts["disagree_textnorm_yes_regex_no"], 1)
        self.assertEqual(r.counts["disagree_textnorm_no_regex_yes"], 0)
        # a Cyrillic quote has no ASCII tokens, so textnorm refuses it; the regex finds it
        self.tr = {"W1": transcript("Мы ждали долго and then left")}
        b = ai_map([("n1", "Wait", "experienced", "Мы ждали долго")])
        self.assertFalse(contains_quote(self.tr["W1"].text("full"), "Мы ждали долго"))
        self.put_map(b)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertEqual(r.counts["disagree_textnorm_no_regex_yes"], 1)

    def test_the_regex_check_tolerates_case_punctuation_whitespace_and_ellipses(self):
        self.tr = {"W1": transcript("So, we WAITED... a  long time (really!) for the permit.\nThen it came, finally")}
        a = ai_map([("n1", "W", "experienced", "we waited ... for the permit"),
                    ("n2", "X", "experienced", "a long time really for the permit then it came")])
        self.put_map(a)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertEqual(r.counts["quotes_unfound_regex"], 0, r.counts)
        # words must be whole and in order
        b = ai_map([("n1", "W", "experienced", "we wait"), ("n2", "X", "experienced", "permit for the")])
        self.put_map(b)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertEqual(r.counts["quotes_unfound_regex"], 2)

    def test_an_accepted_map_whose_quotes_are_mostly_not_in_the_transcript_blocks(self):
        a = ai_map([(f"n{i}", f"L{i}", "experienced", f"words nobody said number {i}") for i in range(4)]
                   + [("n9", "Real", "experienced", "waited a long time")])
        self.put_map(a)
        r = audit_recon_outputs(self.work, self.tr)
        self.assertFalse(r.passed)
        self.assertTrue(any(f.startswith("quote_share_regex") for f in r.failures), r.failures)

    def test_real_corpora_get_anonymous_references_and_safe_ids(self):
        d = self.good()
        d["edges"][0]["target"] = "Maria Silva told me about her cancer"
        self.put_map(d, pid="XY")
        r = audit_recon_outputs(self.work, {"XY": self.tr["W1"]}, anonymize=True)
        text = json.dumps(asdict(r))
        self.assertNotIn("XY", text)
        self.assertNotIn("Maria", text)
        self.assertNotIn("cancer", text)
        self.assertTrue(r.failures)


class TestJudgeAudit(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.map = ai_map([("n1", "A", "experienced", "waited a long time"),
                           ("n2", "B", "wished", "should have called")],
                          [("n1", "n2", "then it came")])
        put(self.work, "recon", f"W1__{SPEC}", {"status": "ok", "map": self.map})

    def put_judge(self, labels, stage="judge_modality", **kw):
        d = {"status": "ok", "labels": labels, "not_judged": [], "n_items": 3, "stats": {"failed_batches": 0}}
        d.update(kw)
        put(self.work, stage, f"W1__{SPEC}", d)

    def test_complete_labels_pass(self):
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"})
        r = audit_judge_outputs(self.work)
        self.assertTrue(r.passed, r.failures)
        self.assertTrue(r.blocking)
        self.assertEqual(r.counts["items_labelled"], 3)

    def test_a_missing_item_fails(self):
        self.put_judge({"n1": "actual", "n2": "wished"})
        r = audit_judge_outputs(self.work)
        self.assertFalse(r.passed)
        self.assertTrue(any(f.startswith("missing_label") for f in r.failures), r.failures)

    def test_an_unknown_label_or_element_fails(self):
        self.put_judge({"n1": "actual", "n2": "sure", "e:n1->n2": "actual", "zz": "actual"})
        r = audit_judge_outputs(self.work)
        codes = {f.split(":")[0] for f in r.failures}
        self.assertTrue({"bad_label", "unknown_element"} <= codes, r.failures)

    def test_n_items_must_match_the_quoted_elements(self):
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"}, n_items=9)
        r = audit_judge_outputs(self.work)
        self.assertTrue(any(f.startswith("n_items_mismatch") for f in r.failures), r.failures)

    def test_a_label_repeated_in_the_file_is_caught(self):
        p = self.work / "judge_modality" / f"W1__{SPEC}.json"
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"})
        p.write_text(p.read_text().replace('"n1": "actual"', '"n1": "actual", "n1": "wished"', 1))
        r = audit_judge_outputs(self.work)
        self.assertTrue(any(f.startswith("duplicate_key") for f in r.failures), r.failures)

    def test_the_cache_is_checked_for_labels_and_repeated_keys(self):
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"})
        (self.work / "judge_cache.json").write_text('{"k1": "actual", "k1": "negated", "k2": "weird"}')
        r = audit_judge_outputs(self.work)
        codes = {f.split(":")[0] for f in r.failures}
        self.assertTrue({"duplicate_key", "bad_label"} <= codes, r.failures)

    def test_not_judged_failed_batches_and_fallbacks_are_counted(self):
        self.put_judge({"n1": "actual", "n2": "unsupported", "e:n1->n2": "actual"}, not_judged=["n2"],
                       stats={"failed_batches": 2})
        put(self.work, "align", "W1__x", {"status": "fallback", "fallback_calls": 1})
        put(self.work, "canonicalize", "batch_0000", {"status": "fallback"})
        put(self.work, "in_transcript", "W1", {"status": "ok"})
        r = audit_judge_outputs(self.work)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["items_not_judged"], 1)
        self.assertEqual(r.counts["failed_batches"], 2)
        self.assertEqual(r.counts["fallback_items"], {"align": 1, "canonicalize": 1})

    def test_a_spec_with_more_than_95_percent_one_class_is_flagged(self):
        n = 30
        m = ai_map([(f"n{i}", f"L{i}", "experienced", f"quote words number {i}") for i in range(n)])
        put(self.work, "recon", f"W2__{SPEC}", {"status": "ok", "map": m})
        put(self.work, "judge_modality", f"W2__{SPEC}", {"status": "ok", "labels": {f"n{i}": "actual" for i in range(n)},
                                                         "not_judged": [], "n_items": n, "stats": {}})
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"})
        r = audit_judge_outputs(self.work)
        self.assertTrue(r.passed, r.failures)              # a warning, not a failure: "actual" may truly dominate
        self.assertTrue(any(w.startswith("one_class") for w in r.warnings), r.warnings)
        self.assertEqual(r.counts["specs_over_95_percent_one_class"], 1)

    def test_shuffled_labels_may_be_none_when_nothing_was_shuffled(self):
        put(self.work, "judge_shuffled", f"W1__{SPEC}", {"status": "ok", "labels": None, "not_judged": [], "n_items": 0})
        self.put_judge({"n1": "actual", "n2": "wished", "e:n1->n2": "actual"})
        self.assertTrue(audit_judge_outputs(self.work).passed)


class TestJudgeCacheKeyConflicts(unittest.TestCase):
    """The same cache key (prompt, element, quote, speaker, reply) must never carry two labels."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.prompts = pipeline.PROMPTS_DIR
        m = ai_map([("n1", "Permit wait", "experienced", "waited a long time for the permit")])
        self.tr = {"W1": transcript(T_TEXT), "W2": transcript(T_TEXT)}
        for pid in ("W1", "W2"):
            put(self.work, "recon", f"{pid}__{SPEC}", {"status": "ok", "map": m})

    def judged(self, pid, label):
        put(self.work, "judge_modality", f"{pid}__{SPEC}",
            {"status": "ok", "labels": {"n1": label}, "not_judged": [], "n_items": 1, "stats": {}})

    def test_the_same_item_with_two_labels_fails(self):
        self.judged("W1", "actual")
        self.judged("W2", "wished")
        r = audit_judge_outputs(self.work, transcripts=self.tr, prompts_dir=self.prompts)
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["cache_key_label_conflicts"], 1)
        self.assertTrue(any(f.startswith("cache_key_conflict") for f in r.failures), r.failures)

    def test_the_same_item_with_the_same_label_and_different_items_pass(self):
        self.judged("W1", "actual")
        self.judged("W2", "actual")
        r = audit_judge_outputs(self.work, transcripts=self.tr, prompts_dir=self.prompts)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["cache_key_label_conflicts"], 0)
        self.assertEqual(r.counts["cache_keys_checked"], 1)
        # a different reply or speaker is a different key: no conflict
        self.tr["W2"] = transcript(T_TEXT, speaker="interviewer")
        self.judged("W2", "wished")
        r = audit_judge_outputs(self.work, transcripts=self.tr, prompts_dir=self.prompts)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["cache_keys_checked"], 2)

    def test_items_not_judged_are_left_out(self):
        self.judged("W1", "actual")
        put(self.work, "judge_modality", f"W2__{SPEC}",
            {"status": "ok", "labels": {"n1": "unsupported"}, "not_judged": ["n1"], "n_items": 1, "stats": {}})
        r = audit_judge_outputs(self.work, transcripts=self.tr, prompts_dir=self.prompts)
        self.assertTrue(r.passed, r.failures)


class TestSecondJudge(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def put_both(self, a, b, name="W1__x"):
        for stage, labels in (("judge_modality", a), ("judge2_modality", b)):
            put(self.work, stage, name, {"status": "ok", "labels": labels, "not_judged": []})

    def test_kappa_on_the_overlap(self):
        a = {f"n{i}": ("actual" if i % 2 else "wished") for i in range(10)}
        self.put_both(a, dict(a))
        r = audit_second_judge(self.work)
        self.assertFalse(r.blocking)
        self.assertTrue(r.passed)
        self.assertEqual(r.counts["n_overlap"], 10)
        self.assertEqual(r.counts["agreement"], 1.0)
        self.assertEqual(r.counts["kappa"], 1.0)

    def test_partial_agreement_and_not_judged_items_are_left_out(self):
        a = {f"n{i}": ("actual" if i % 2 else "wished") for i in range(10)}
        b = dict(a)
        b["n0"], b["n1"] = "negated", "negated"
        put(self.work, "judge_modality", "W1__x", {"status": "ok", "labels": a, "not_judged": ["n9"]})
        put(self.work, "judge2_modality", "W1__x", {"status": "ok", "labels": b, "not_judged": []})
        r = audit_second_judge(self.work)
        self.assertEqual(r.counts["n_overlap"], 9)
        self.assertEqual(r.counts["agreement"], round(7 / 9, 4))
        self.assertLess(r.counts["kappa"], 1.0)

    def test_sample_share_is_a_deterministic_subset(self):
        a = {f"n{i}": "actual" if i % 3 else "wished" for i in range(40)}
        self.put_both(a, dict(a))
        r1 = audit_second_judge(self.work, sample_share=0.25)
        r2 = audit_second_judge(self.work, sample_share=0.25)
        self.assertEqual(r1.counts["n_overlap"], 10)
        self.assertEqual(r1.counts, r2.counts)

    def test_no_second_judge_is_a_skip_not_a_failure(self):
        r = audit_second_judge(self.work)
        self.assertTrue(r.passed)
        self.assertEqual(r.counts["n_overlap"], 0)
        self.assertTrue(r.counts["skipped"])

    def test_one_label_used_by_both_gives_no_kappa_not_a_perfect_one(self):
        a = {f"n{i}": "actual" for i in range(5)}
        self.put_both(a, dict(a))
        r = audit_second_judge(self.work)
        self.assertIsNone(r.counts["kappa"])
        self.assertTrue(any(w.startswith("kappa_undefined") for w in r.warnings))


class TestDeterminism(unittest.TestCase):
    def sample(self, n=30):
        return [{"messages": [{"role": "user", "content": f"q{i}"}], "temperature": 0.0, "seed": i, "max_tokens": 50,
                 "expected_sha256": audits.sha256_text(f"answer {i}")} for i in range(n)]

    def test_a_deterministic_server_is_fully_identical(self):
        c = MockClient(fn=lambda m: "answer " + m[-1]["content"][1:])
        r = audit_determinism(None, c, self.sample(), k=20)
        self.assertFalse(r.blocking)
        self.assertEqual((r.counts["n_checked"], r.counts["n_identical"]), (20, 20))
        self.assertEqual(r.counts["identical_share"], 1.0)

    def test_a_drifting_server_is_reported(self):
        calls = {"n": 0}

        def fn(m):
            calls["n"] += 1
            return "answer " + m[-1]["content"][1:] + (" extra" if calls["n"] % 4 == 0 else "")
        r = audit_determinism(None, MockClient(fn=fn), self.sample(), k=20)
        self.assertEqual(r.counts["n_identical"], 15)
        self.assertEqual(r.counts["identical_share"], 0.75)
        self.assertFalse(r.passed)                  # fail-closed (A8): a mismatch fails the audit
        self.assertTrue(any(f.startswith("not_identical") for f in r.failures))

    def test_the_choice_of_requests_is_seeded(self):
        seen = []
        for _ in range(2):
            got = []
            audit_determinism(None, MockClient(fn=lambda m: got.append(m[-1]["content"]) or "x"), self.sample(), k=5)
            seen.append(got)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(len(seen[0]), 5)

    def test_errors_are_counted(self):
        def boom(m):
            raise RuntimeError("down")
        r = audit_determinism(None, MockClient(fn=boom), self.sample(), k=3)
        self.assertEqual(r.counts["n_errors"], 3)
        self.assertEqual(r.counts["n_identical"], 0)

    def test_sample_from_a_real_log_takes_only_valid_t0_requests(self):
        from second_scribe.audit.registry import AuditLog
        d = Path(tempfile.mkdtemp())
        log = AuditLog(d / "run.jsonl", "r")
        for i, (temp, err) in enumerate([(0.0, None), (0.7, None), (0.0, "bad"), (0.0, None)]):
            rid = f"judge:x:{i}"
            log.append("call_request", {"request_id": rid, "stage": "judge:modality", "item_id": "x", "attempt": 1,
                                        "messages": [{"role": "user", "content": f"m{i}"}], "temperature": temp,
                                        "seed": i, "max_tokens": 9})
            log.append("call_response", {"request_id": rid, "text": f"t{i}", "validation_error": err})
        s = audits.sample_from_log(log.path, k=10, seed=1, roles={"judge"})
        self.assertEqual(sorted(x["messages"][0]["content"] for x in s), ["m0", "m3"])
        self.assertEqual(s[0]["expected_sha256"], audits.sha256_text("t" + s[0]["messages"][0]["content"][1:]))
        self.assertEqual(audits.sample_from_log(log.path, k=10, seed=1, roles={"recon"}), [])


class TestCanary(unittest.TestCase):
    def rows(self, wished_pass, negated_pass, n=10):
        out = []
        for kind, ok in (("wished", wished_pass), ("negated", negated_pass), ("loop", 0)):
            for i in range(n):
                out.append({"kind": kind, "result": "PASS" if i < ok else "FAIL"})
        return out

    def test_passing_the_rule_passes(self):
        r = audit_canary({"aggregates": {"traps": self.rows(10, 9)}})
        self.assertTrue(r.passed, r.failures)
        self.assertTrue(r.blocking)

    def test_below_the_rule_on_wished_or_negated_blocks(self):
        for w, n in ((7, 10), (10, 7)):
            r = audit_canary({"aggregates": {"traps": self.rows(w, n)}})
            self.assertFalse(r.passed)
            self.assertTrue(r.blocking)
            self.assertEqual(len(r.failures), 1)
        r = audit_canary({"aggregates": {"traps": self.rows(8, 8)}})      # exactly the rule: passes
        self.assertTrue(r.passed)

    def test_a_canary_that_did_not_exercise_the_traps_blocks(self):
        for res in ({"aggregates": None}, {"aggregates": {}}, {"aggregates": {"traps": []}}):
            r = audit_canary(res)
            self.assertFalse(r.passed, res)
        rows = [x for x in self.rows(10, 10) if x["kind"] != "negated"]
        self.assertFalse(audit_canary({"aggregates": {"traps": rows}}).passed)

    def test_the_counts_name_the_rule(self):
        r = audit_canary({"aggregates": {"traps": self.rows(9, 10)}})
        self.assertEqual(r.counts["wished"], {"n": 10, "pass": 9, "rate": 0.9})
        self.assertEqual(r.counts["min_pass_rate"], 0.8)


# ----------------------------------------------------------------------------------------- the leak scanner

REAL_SENTENCES = [
    "My daughter Tamsin was refused a place because the clinic lost the referral paperwork twice",
    "We drove four hours every Thursday to reach the specialist who finally listened to us",
]
# built in pieces so that this file, which is in the public tree, never contains an address itself
EMAIL = "someone" + "@" + "clinic-mail.net"
EMAIL2 = "a" + "@" + "b-mail.net"
FILLER = "The report counts items by stage and nothing else."


class TestLeaks(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.idx = LeakIndex.build(REAL_SENTENCES, ["XY", "kc12"])

    def scan(self, *files, **kw):
        paths = []
        for name, text in files:
            p = self.dir / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            paths.append(str(p))
        return audit_leaks(paths, self.idx, **kw)

    def test_a_clean_aggregate_passes(self):
        r = self.scan(("aggregates/aggregates.json", json.dumps({"n": 12, "specs": ["full__p0__t0__r0"],
                                                                   "note": "short"})),
                      ("aggregates/report.md", FILLER))
        self.assertTrue(r.passed, r.failures)
        self.assertTrue(r.blocking)
        self.assertEqual(r.counts["files_scanned"], 2)

    def test_four_kinds_of_leak_are_four_failures(self):
        agg = {"a": "x", "long": "an aggregate string that is much longer than forty characters in total"}
        r = self.scan(
            ("aggregates/aggregates.json", json.dumps(agg)),
            ("notes.md", "Says: we drove four hours every Thursday to reach the place.\n"
                         f"Participant XY said it.\nWrite to {EMAIL} please.\n"))
        kinds = sorted(f.split(":")[0] for f in r.failures)
        self.assertEqual(kinds, ["email", "long_string", "name", "ngram"], r.failures)
        self.assertFalse(r.passed)
        self.assertEqual((r.counts["ngram_hits"], r.counts["name_hits"], r.counts["long_string_hits"],
                          r.counts["email_hits"]), (1, 1, 1, 1))

    def test_the_result_never_contains_the_matched_text(self):
        r = self.scan(("a.txt", f"we drove four hours every Thursday to reach\nXY and {EMAIL}"))
        text = json.dumps(asdict(r))
        for secret in ("drove", "Thursday", "XY", "someone", "clinic-mail", "Tamsin"):
            self.assertNotIn(secret, text)

    def test_nothing_is_written_to_disk_by_the_scan(self):
        before = sorted(p.name for p in self.dir.rglob("*"))
        self.scan(("a.txt", "we drove four hours every Thursday to reach"))
        after = sorted(p.name for p in self.dir.rglob("*") if p.name != "a.txt")
        self.assertEqual(before, after)

    def test_a_five_word_run_is_a_hit_but_four_words_are_not(self):
        self.assertTrue(self.scan(("a.txt", "drove four hours every time")).passed)         # four shared words
        r = self.scan(("c.txt", "Quote: WE DROVE four-hours, every Thursday!"))
        self.assertEqual([f.split(":")[0] for f in r.failures], ["ngram"])   # case and punctuation never matter
        self.assertFalse(self.scan(("b.txt", "so we drove four hours every")).passed)

    def test_an_overlapping_copy_is_one_hit_per_span(self):
        r = self.scan(("a.txt", REAL_SENTENCES[0] + " and then " + FILLER + " " + REAL_SENTENCES[1]))
        self.assertEqual(r.counts["ngram_hits"], 2)

    def test_hits_carry_a_byte_offset_into_the_file(self):
        text = "Ünïcode prefix é — then we drove four hours every Thursday to reach"
        r = self.scan(("a.txt", text))
        off = int(r.failures[0].rsplit("@", 1)[1])
        self.assertEqual(off, len(text[:text.index("we drove")].encode("utf-8")))

    def test_initials_and_ids_are_capitals_only_as_whole_words(self):
        self.assertTrue(self.scan(("a.txt", "xyz and boxy and KC123 and Xylo and xy and Xy and kc12 and k-12")).passed)
        r = self.scan(("b.txt", "see XY, (KC 12) and XY's notes"))
        self.assertEqual(r.counts["name_hits"], 3)

    def test_names_that_are_common_words_match_only_in_capitals(self):
        idx = LeakIndex.build(REAL_SENTENCES, ["An", "me"])
        p = self.dir / "a.txt"
        p.write_text("An answer for me and an officer", encoding="utf-8")
        self.assertTrue(audit_leaks([str(p)], idx).passed)
        p.write_text("Notes from AN and ME", encoding="utf-8")
        self.assertEqual(audit_leaks([str(p)], idx).counts["name_hits"], 2)

    def test_patterns_emails_phones_postal_codes_and_addresses(self):
        text = (f"mail {'a.b+c' + '@' + 'mail.example.ca'}; call {'(519) ' + '555-' + '0273'} or {'416-' + '555-' + '0299'} "
                f"or {'+1 613 ' + '555 ' + '0277'}; postal {'H0H ' + '0H0'} or {'h0h' + '0h0'}; "
                f"server {'10.0.12' + '.7'} and {'192.168.' + '1.20'}")
        r = self.scan(("a.txt", text))
        self.assertEqual((r.counts["email_hits"], r.counts["phone_hits"], r.counts["postal_hits"], r.counts["ip_hits"]),
                         (1, 3, 2, 2))

    def test_phone_numbers_with_nine_digit_mobiles_and_the_north_american_fiction_range(self):
        for number in ("+55 11 9" + "1234-5678", "+55 (11) 9" + "1234 5678", "+44 7911 " + "123456", "+33 6 12 " + "34 56 78"):
            self.assertFalse(self.scan(("a.txt", f"call {number} now")).passed, number)
        # 555-01xx is fiction only in the North American format
        for number in ("(519) 555-" + "0123", "+1 416 555-" + "0100", "613-555-" + "0199"):
            self.assertTrue(self.scan(("b.txt", f"call {number} now")).passed, number)
        self.assertFalse(self.scan(("c.txt", "call +55 11 " + "555-0123")).passed)
        self.assertFalse(self.scan(("d.txt", "call (519) 555-" + "0200")).passed)

    def test_addresses_that_cannot_be_anyones_are_not_a_leak(self):
        ok = ("see " + "noreply" + "@" + "anthropic.com" + ", " + "a" + "@" + "example.org" + ", " + "b" + "@" + "x.test"
              + ", " + "(519) 555-" + "0123")
        self.assertTrue(self.scan(("a.txt", ok)).passed)
        self.assertFalse(self.scan(("b.txt", "mail " + "someone" + "@" + "anthropic.com")).passed)     # not the trailer's address
        self.assertFalse(self.scan(("c.txt", "call " + "(519) 555-" + "0273")).passed)

    def test_loopback_addresses_are_not_a_leak_but_private_ones_are(self):
        self.assertTrue(self.scan(("a.txt", "http://127.0.0.1:8080/v1 and " + "0.0.0." + "0 bound")).passed)
        self.assertFalse(self.scan(("b.txt", "http://" + "10.1.2." + "3:8080/v1")).passed)
        self.assertFalse(self.scan(("c.txt", "host " + "172.16.0." + "9")).passed)

    def test_patterns_do_not_fire_on_ordinary_report_numbers(self):
        text = ("run 2026-10-14 at 12:30, n=49, 0.712 [0.601, 0.8], sha " + "a1" * 32 + ", version 1.13.0, "
                "2026101412 and 12345678")
        self.assertTrue(self.scan(("a.txt", text)).passed)

    def test_long_strings_are_checked_in_aggregate_json_only(self):
        long = {"k": "y" * 41}
        self.assertFalse(self.scan(("aggregates/a.json", json.dumps(long))).passed)
        self.assertTrue(self.scan(("other/a.json", json.dumps(long))).passed)
        self.assertFalse(self.scan(("other/b.json", json.dumps(long)), strict_json=[str(self.dir / "other/b.json")]).passed)
        ok = {"sha": "ab" * 32, "n": 3}
        self.assertTrue(self.scan(("aggregates/ok.json", json.dumps(ok))).passed)
        self.assertFalse(self.scan(("aggregates/k.json", json.dumps({"z" * 41: 1}))).passed)   # keys count too

    def test_invalid_json_in_aggregates_fails_closed(self):
        r = self.scan(("aggregates/a.json", "{oops"))
        self.assertEqual([f.split(":")[0] for f in r.failures], ["invalid_json"])

    def test_missing_and_unscannable_files_fail_closed_and_images_are_skipped(self):
        r = audit_leaks([str(self.dir / "nope.txt")], self.idx)
        self.assertEqual([f.split(":")[0] for f in r.failures], ["missing_path"])
        (self.dir / "x.pdf").write_bytes(b"%PDF-1.4 binary")
        (self.dir / "x.png").write_bytes(b"\x89PNG....")
        r = audit_leaks([str(self.dir / "x.pdf"), str(self.dir / "x.png")], self.idx)
        self.assertEqual([f.split(":")[0] for f in r.failures], ["unscannable"])
        self.assertEqual(r.counts["files_scanned"], 1)             # the image is read for embedded text, not refused

    def test_directories_are_walked_and_word_files_are_read_inside(self):
        import zipfile
        d = self.dir / "export"
        d.mkdir()
        with zipfile.ZipFile(d / "p.docx", "w") as z:
            z.writestr("word/document.xml",
                       "<w:p><w:r><w:t>we drove four hours</w:t></w:r><w:r><w:t> every Thursday to reach</w:t></w:r></w:p>")
        (d / "sub").mkdir()
        (d / "sub" / "t.txt").write_text(FILLER)
        r = audit_leaks([str(d)], self.idx)
        self.assertEqual(r.counts["files_scanned"], 2)
        self.assertEqual(r.counts["ngram_hits"], 1)
        self.assertTrue(r.failures[0].split(":", 1)[1].startswith(str(d / "p.docx")))

    def test_escaped_json_text_is_scanned_decoded(self):
        idx = LeakIndex.build(["Our daughter’s clinic never sent the referral onward", "José Núñez-Gómez"], ["José"])
        agg = self.dir / "aggregates"
        agg.mkdir()
        (agg / "a.json").write_text(json.dumps({"note": "daughter’s clinic never sent the", "who": "José"}))   # \u escapes
        text = (agg / "a.json").read_text()
        self.assertIn("\\u2019", text)
        r = audit_leaks([str(agg / "a.json")], idx)
        self.assertEqual((r.counts["ngram_hits"], r.counts["name_hits"]), (1, 1), r.failures)
        # a file without escapes is not counted twice
        (agg / "b.json").write_text(json.dumps({"note": "daughter’s clinic never sent the", "who": "José"},
                                               ensure_ascii=False))
        r = audit_leaks([str(agg / "b.json")], idx)
        self.assertEqual((r.counts["ngram_hits"], r.counts["name_hits"]), (1, 1), r.failures)

    def test_utf16_text_files_are_read(self):
        p = self.dir / "u16.txt"
        p.write_bytes("we drove four hours every Thursday to reach".encode("utf-16"))
        self.assertEqual(audit_leaks([str(p)], self.idx).counts["ngram_hits"], 1)

    def test_the_scanner_tokenises_like_textnorm_except_for_the_documented_folding(self):
        for t in ["Don’t go — it's 3.5% “odd”…", "A\u00a0B\tC\nD'", "plain words, numbers 12 and x'y a_b"]:
            self.assertEqual([x for x, _, _ in audits.tokens_with_offsets(t)], tokens(t), t)
        # where it differs, on purpose: accents folded not cut, compatibility forms, no invisible characters
        self.assertEqual([x for x, _, _ in audits.tokens_with_offsets("ﬁne café Ⅷ naïve ７")],
                         ["fine", "cafe", "viii", "naive", "7"])

    def test_the_index_is_built_from_texts_and_labels_and_counts_only_are_exposed(self):
        self.assertEqual(self.idx.stats["n_texts"], 2)
        self.assertEqual(self.idx.stats["n_names"], 2)
        self.assertNotIn("Tamsin", json.dumps(self.idx.stats))
        self.assertNotIn("Tamsin", repr(self.idx))

    def test_sources_may_be_callables_and_empty_sources_are_refused(self):
        p = self.dir / "a.txt"
        p.write_text("we drove four hours every Thursday to reach", encoding="utf-8")
        r = audit_leaks([str(p)], lambda: iter(REAL_SENTENCES), lambda: ["XY"])
        self.assertFalse(r.passed)
        with self.assertRaises(ValueError):
            audit_leaks([str(p)], [])
        with self.assertRaises(ValueError):
            audit_leaks([str(p)], [""," "])

    def test_n_is_a_parameter(self):
        p = self.dir / "a.txt"
        p.write_text("we drove four hours", encoding="utf-8")
        self.assertTrue(audit_leaks([str(p)], REAL_SENTENCES).passed)
        self.assertFalse(audit_leaks([str(p)], REAL_SENTENCES, n=4).passed)


# ----------------------------------------------------------------------------------------- wiring

def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


def real_like_config(out: Path, **corpus) -> str:
    """The synthetic demo corpus standing in for "real" files: kind real, so every real-data guard is on."""
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = "real"
    d["prereg_required"] = True              # a real corpus is refused without it (Task 10)
    m = out / "participant_id_map.json"      # fixture id map: a real corpus must name one
    m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
    d["corpus"]["participant_id_map"] = str(m)
    d["corpus"].update(corpus)
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return str(p)


def freeze_real(cfg_path: str) -> str:
    """A prereg fixture for a real-kind config (a real corpus is refused without one): frozen into the run's log, as
    ``prereg freeze`` would (the temp dir is not a git checkout, so ``setUpModule`` patches the code check)."""
    cfg = RunConfig.load(cfg_path)
    log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    info = {r: {"model_id": f"{r}.gguf", "weights": "a" * 64, "params": {}} for r in ("recon", "judge")}
    log = AuditLog(log_path, cfg.run_id)
    prereg.freeze(prereg.build_prereg(cfg, prereg.PROMPTS_DIR, code_commit="0" * 40, model_info=info,
                                      decision_rules=prereg.DECISION_RULES,
                                      calibration={"matcher": {"n": 1}, "in_transcript": {"n": 1}}),
                  prereg.prereg_path(cfg), log)
    log.close()
    return cfg_path


def frozen_real_config(out: Path, **corpus) -> str:
    """``real_like_config`` with its pre-registration frozen: the config of a real corpus that may run."""
    return freeze_real(real_like_config(out, **corpus))


def judge_call_after_the_freeze(cfg_path: str) -> str:
    """The request id of a judge call logged after the freeze, as the one behind a cache entry of an earlier good session."""
    cfg = RunConfig.load(cfg_path)
    rid = "judge:modality:earlier:1:00000000"
    log = AuditLog(Path(cfg.out_dir) / "logs" / "run.jsonl", cfg.run_id)
    log.append("call_request", {"request_id": rid, "stage": "judge:modality", "item_id": "earlier", "attempt": 1,
                                "model_id": "judge.gguf", "params": {}})
    log.append("call_response", {"request_id": rid})
    log.close()
    return rid


def named(client, role: str):
    """The client as a real one reports itself: a config naming the model, which a real corpus's calls must record."""
    c = copy.copy(client)
    c.cfg = types.SimpleNamespace(model_id=f"{role}.gguf", params={}, api_key_env=None)
    return c


_patches: list = []


def setUpModule():
    """A real-kind corpus runs only after a freeze, against the frozen code commit and with calls that name their model:
    the temp dir is no checkout, and the oracle names no model, so both are patched for the whole module."""
    real_build = cli_module.build_clients
    for patch in (mock.patch.object(prereg, "check_code", return_value=None),
                  mock.patch.object(cli_module, "build_clients",
                                    lambda kind, cfg, fixtures=None, **_: {r: named(c, r) for r, c in
                                                                      real_build(kind, cfg, fixtures).items()})):
        patch.start()
        _patches.append(patch)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


@contextlib.contextmanager
def no_canary():
    """Switch the preflight and the post-Q canary off for a test that is about something else (they are tested apart)."""
    with mock.patch.object(audits, "preflight", lambda ctx: None), \
            mock.patch.object(audits, "_canary_result", lambda ctx: None):
        yield


def a_real_5gram() -> str:
    """Five consecutive words of a fixture transcript paragraph (the demo corpus, standing in for real data)."""
    from second_scribe.transcripts import read_docx_text
    path = sorted((Path(DEMO_CONFIG).parent.parent / "demo" / "corpus" / "transcripts").glob("*.docx"))[0]
    for para in read_docx_text(path):
        toks = tokens(para)
        if len(toks) >= 12:
            return " ".join(toks[3:8])
    raise AssertionError("no long paragraph")


class TestPipelineWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.syn = Path(tempfile.mkdtemp())
        cls.rc, _o, _e = cli(["demo", "--out", str(cls.syn), "--client", "oracle"])
        cls.real = Path(tempfile.mkdtemp())
        cls.real_cfg = frozen_real_config(cls.real)
        cls.real_rc, _o, _e = cli(["run", "--config", cls.real_cfg, "--stages", "all", "--client", "oracle"])

    def test_every_stage_group_writes_its_audit_file_and_the_summary(self):
        self.assertEqual(self.rc, 0)
        for stage in "QJM":
            self.assertTrue((self.syn / "audits" / f"{stage}.json").exists(), stage)
        summary = json.loads((self.syn / "aggregates" / "audits.json").read_text())
        names = {r["name"] for rs in summary["stages"].values() for r in rs}
        self.assertTrue({"recon_outputs", "judge_outputs", "second_judge", "determinism_recon", "determinism_judge",
                         "leaks"} <= names, names)
        self.assertTrue(all(r["passed"] for rs in summary["stages"].values() for r in rs))
        self.assertNotIn("canary", names)           # a synthetic corpus is not canaried against itself

    def test_the_summary_has_counts_and_codes_only_and_passes_the_length_rule(self):
        from second_scribe.report import assert_aggregate_only
        summary = json.loads((self.real / "aggregates" / "audits.json").read_text())
        assert_aggregate_only(summary)
        self.assertNotIn("SYN-OW", json.dumps(summary))
        for p in (self.real / "audits").glob("*.json"):
            self.assertNotIn("SYN-OW", p.read_text(), p.name)         # participant ids never reach an audit file

    def test_the_report_embeds_the_audit_summary(self):
        text = (self.real / "aggregates" / "report.md").read_text()
        self.assertIn("## Audits", text)
        for name in ("recon_outputs", "judge_outputs", "leaks", "canary"):
            self.assertIn(f"| {name} |", text)
        self.assertLess(text.index("| Q | recon_outputs"), text.index("| J | judge_outputs"))

    def test_a_real_run_is_canaried_in_the_same_session_and_passes(self):
        self.assertEqual(self.real_rc, 0)
        summary = json.loads((self.real / "aggregates" / "audits.json").read_text())
        canary = next(r for r in summary["stages"]["Q"] if r["name"] == "canary")
        self.assertTrue(canary["passed"])
        self.assertEqual(canary["counts"]["wished"]["rate"], 1.0)
        self.assertTrue((self.real / "canary" / "logs" / "run.jsonl").exists())
        leaks = next(r for r in summary["stages"]["M"] if r["name"] == "leaks")
        self.assertTrue(leaks["passed"])
        self.assertGreaterEqual(leaks["counts"]["files_scanned"], 2)

    def test_the_run_log_has_one_audit_summary_and_still_verifies(self):
        recs = [json.loads(line) for line in (self.real / "logs" / "run.jsonl").read_text().splitlines()]
        summaries = [r for r in recs if r["record_type"] == "audit_summary"]
        self.assertEqual(len(summaries), len([r for r in recs if r["record_type"] == "run_start"]))   # one per run
        self.assertFalse(summaries[0]["payload"]["blocked"])
        self.assertEqual(recs[-1]["record_type"], "run_end")
        self.assertEqual(cli(["verify", str(self.real / "logs" / "run.jsonl")])[0], 0)

    def test_a_planted_leak_in_the_aggregates_is_refused_and_nothing_is_written(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        gram = a_real_5gram()
        real_compute = pipeline.compute_aggregates

        def leaky(r):
            agg, p_list = real_compute(r)
            agg["note"] = gram                      # under 40 characters: only the new scanner can catch it
            return agg, p_list
        with no_canary(), mock.patch.object(pipeline, "compute_aggregates", leaky):
            rc, _o, err = cli(["run", "--config", cfg, "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 4)
        self.assertFalse((out / "aggregates" / "aggregates.json").exists())
        self.assertFalse((out / "aggregates" / "report.md").exists())
        self.assertNotIn(gram, err)
        blocked = json.loads((out / "audits" / "blocked.json").read_text())
        self.assertEqual(blocked["results"][0]["counts"]["ngram_hits"], 1)
        self.assertNotIn(gram, json.dumps(blocked))
        recs = [json.loads(line) for line in (out / "logs" / "run.jsonl").read_text().splitlines()]
        self.assertTrue([r for r in recs if r["record_type"] == "audit_summary" and r["payload"]["blocked"]])
        self.assertEqual(cli(["verify", str(out / "logs" / "run.jsonl")])[0], 0)       # the log closed cleanly

    def test_a_participant_id_in_the_aggregates_is_refused(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        real_compute = pipeline.compute_aggregates

        def leaky(r):
            agg, p_list = real_compute(r)
            agg["first"] = "KC99"          # the fixture id map names KC99 (the demo ids are public)
            return agg, p_list
        with no_canary(), mock.patch.object(pipeline, "compute_aggregates", leaky):
            rc, _o, _e = cli(["run", "--config", cfg, "--stages", "M,Q,J", "--client", "oracle"])
        self.assertEqual(rc, 4)
        self.assertFalse((out / "aggregates" / "aggregates.json").exists())

    def test_a_blocking_recon_failure_stops_the_pipeline_before_the_judge(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        with no_canary():
            self.assertEqual(cli(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"])[0], 0)
        f = next((out / "work" / "recon").glob("*__full__p0__t0__r0.json"))
        d = json.loads(f.read_text())
        d["map"]["edges"].append({"source": d["map"]["nodes"][0]["node_id"], "target": "ghost", "status": "experienced",
                                  "glued": True, "quote": "x y z", "label": None})
        f.write_text(json.dumps(d))
        with no_canary():
            rc, _o, err = cli(["run", "--config", cfg, "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 4)
        self.assertIn("recon_outputs", err)
        self.assertFalse((out / "work" / "judge_modality").exists())
        q = json.loads((out / "audits" / "Q.json").read_text())
        res = next(r for r in q["results"] if r["name"] == "recon_outputs")
        self.assertFalse(res["passed"])
        self.assertTrue(res["failures"][0].startswith("edge_unknown_node:#"), res["failures"])
        self.assertNotIn("SYN-OW", json.dumps(res))

    def test_a_broken_server_is_caught_by_the_preflight_before_any_real_item_exists(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out, audit_determinism_k=0)
        bad = MockClient(fn=lambda m: "not json at all")
        with mock.patch("second_scribe.cli.build_clients", lambda kind, cfg, fixtures=None, **_: {"recon": bad,
                                                                                         "judge": bad}):
            rc, _o, err = cli(["run", "--config", cfg, "--stages", "all", "--client", "http"])
        self.assertEqual(rc, 4)
        self.assertIn("preflight", err)
        self.assertEqual([p.name for p in (out / "work").rglob("*.json")], ["models.json"])   # no item of any stage
        blocked = json.loads((out / "audits" / "blocked.json").read_text())
        self.assertEqual(blocked["results"][0]["name"], "preflight")
        self.assertFalse(blocked["results"][0]["passed"])

    def test_a_real_corpus_cannot_switch_the_canary_off(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out, canary=False)
        rc, _o, err = cli(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"])
        self.assertEqual(rc, 4)
        blocked = json.loads((out / "audits" / "blocked.json").read_text())
        self.assertEqual(blocked["results"][0]["failures"], ["canary_disabled:real_corpus"])
        self.assertFalse((out / "work" / "recon").exists())

    def test_a_resumed_run_adds_only_a_few_log_records_and_the_same_aggregates(self):
        before = (self.real / "aggregates" / "aggregates.json").read_bytes()
        n = len((self.real / "logs" / "run.jsonl").read_text().splitlines())
        self.assertEqual(cli(["run", "--config", self.real_cfg, "--stages", "all", "--client", "oracle"])[0], 0)
        self.assertEqual((self.real / "aggregates" / "aggregates.json").read_bytes(), before)
        self.assertLess(len((self.real / "logs" / "run.jsonl").read_text().splitlines()) - n, 6)


class TestAuditLeaksCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cls.cfg = real_like_config(cls.out)
        cls.files = Path(tempfile.mkdtemp())

    def write(self, name, text):
        p = self.files / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_exit_0_on_a_clean_file_and_1_on_a_leak(self):
        clean = self.write("clean.md", "Aggregates only: 12 participants, 3 specs.")
        rc, out, _e = cli(["audit-leaks", "--paths", clean, "--real-config", self.cfg])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["passed"])
        gram = a_real_5gram()
        leaky = self.write("leaky.md", f"Intro. {gram} outro. Write to {EMAIL2}")
        rc, out, _e = cli(["audit-leaks", "--paths", leaky, "--real-config", self.cfg])
        self.assertEqual(rc, 1)
        res = json.loads(out)
        self.assertEqual(sorted(f.split(":")[0] for f in res["failures"]), ["email", "ngram"])
        self.assertNotIn(gram, out)
        self.assertNotIn(EMAIL2, out)

    def test_exit_2_when_the_scan_cannot_be_done(self):
        rc, _o, err = cli(["audit-leaks", "--paths", "x", "--real-config", str(self.files / "nope.json")])
        self.assertEqual(rc, 2)
        bad = Path(tempfile.mkdtemp())
        cfg = real_like_config(bad, transcripts_glob="../nowhere/*.docx")
        rc, _o, err = cli(["audit-leaks", "--paths", self.write("c.md", "x"), "--real-config", cfg])
        self.assertEqual(rc, 2)               # no real transcripts found: a scan against nothing is refused

    def test_missing_paths_fail_the_scan(self):
        rc, out, _e = cli(["audit-leaks", "--paths", str(self.files / "gone.md"), "--real-config", self.cfg])
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["failures"][0].startswith("missing_path"))

    def test_a_git_repo_is_scanned_the_way_a_push_would_publish_it(self):
        import subprocess
        repo = Path(tempfile.mkdtemp())
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
                                         *a], check=True, capture_output=True)
        git("init", "-q")
        (repo / "a.md").write_text("fine")
        (repo / "b.md").write_text(f"oops {a_real_5gram()} oops")
        (repo / "untracked.md").write_text(f"oops {a_real_5gram()}")
        git("add", "a.md", "b.md")
        git("commit", "-q", "-m", "first")
        rc, out, _e = cli(["audit-leaks", "--repo", str(repo), "--real-config", self.cfg])
        res = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(res["counts"]["ngram_hits"], 1)                # the blob is in the index, in HEAD and in a commit: once
        self.assertIn("b.md", res["failures"][0])
        self.assertNotIn("untracked", out)                               # untracked files are not pushed

    def test_the_id_map_and_file_names_feed_the_name_list(self):
        d = Path(tempfile.mkdtemp())
        idmap = d / "my_id_map.json"
        idmap.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC77"}}}))
        cfg = real_like_config(d, participant_id_map=str(idmap))
        f = self.write("n.md", "Someone called KC77 and later QZ.")
        rc, out, _e = cli(["audit-leaks", "--paths", f, "--real-config", cfg])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["counts"]["name_hits"], 2)
        # public speaker words are exempt (and counted); a pid from corpus.roles is a name
        g = self.write("m.md", "Notes by I1 for Participant 4.")
        rc, out, _e = cli(["audit-leaks", "--paths", g, "--real-config", cfg])
        self.assertEqual(rc, 0, out)
        self.assertGreater(json.loads(out)["counts"]["index_names_public_exempt"], 0)
        h = self.write("o.md", "Notes about SYN-OW03.")
        self.assertEqual(cli(["audit-leaks", "--paths", h, "--real-config", cfg])[0], 1)


# ----------------------------------------------------------------------------------------- session protection

class Flaky:
    """Wraps a client: it answers from ``inner`` for ``good_calls`` calls, then returns garbage, and runs ``on_call(n)``
    at every call (a hook for a test)."""

    def __init__(self, inner, good_calls, on_call=None):
        self.inner, self.good_calls, self.on_call, self.n = inner, good_calls, on_call, 0
        self.model_label = "oracle"
        self.cfg = getattr(inner, "cfg", None)          # a real corpus's calls must name their model

    def complete(self, messages, **kw):
        self.n += 1
        if self.on_call:
            self.on_call(self.n)
        if self.n > self.good_calls:
            return MockClient(fn=lambda m: "garbage, not json").complete(messages, **kw)
        return self.inner.complete(messages, **kw)


def oracle_clients(cfg_path):
    from second_scribe.cli import build_clients
    from second_scribe.pipeline import RunConfig
    return build_clients("oracle", RunConfig.load(cfg_path))


class TestSessionProtection(unittest.TestCase):
    def run_cli(self, cfg, clients, stages="all"):
        with mock.patch("second_scribe.cli.build_clients", lambda kind, c, fixtures=None, **_: clients):
            return cli(["run", "--config", cfg, "--stages", stages, "--client", "http"])

    def test_the_preflight_passes_with_a_good_server_and_is_reported(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        rc, _o, err = cli(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"])
        self.assertEqual(rc, 0, err)
        q = json.loads((out / "audits" / "Q.json").read_text())
        pre = next(r for r in q["results"] if r["name"] == "preflight")
        self.assertTrue(pre["passed"])
        self.assertEqual((pre["counts"]["recon_n"], pre["counts"]["recon_ok"]), (2, 2))
        self.assertEqual(pre["counts"]["wished"]["rate"], 1.0)
        self.assertEqual(pre["counts"]["negated"]["rate"], 1.0)
        self.assertTrue((out / "preflight" / "logs" / "run.jsonl").exists())

    def test_a_judge_that_cannot_tell_wished_from_actual_is_stopped_before_any_real_item(self):
        import re
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        good = oracle_clients(cfg)

        def all_actual(messages):
            n = len(re.findall(r"^\d+\. ELEMENT", messages[-1]["content"], re.M))
            return json.dumps({"labels": [{"n": i, "modality": "actual"} for i in range(1, n + 1)]})
        clients = {"recon": good["recon"], "judge": MockClient(fn=all_actual)}
        rc, _o, err = self.run_cli(cfg, clients)
        self.assertEqual(rc, 4)
        blocked = json.loads((out / "audits" / "blocked.json").read_text())
        self.assertEqual(sorted(blocked["results"][0]["failures"]),
                         ["preflight_below_rule:negated", "preflight_below_rule:wished"])
        self.assertEqual([p.name for p in (out / "work").rglob("*.json")], ["models.json"])

    def test_a_failed_canary_takes_back_everything_the_session_produced_and_the_next_session_redoes_it(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        work = out / "work"
        work.mkdir(parents=True)
        rid = judge_call_after_the_freeze(cfg)
        earlier = {"modality": "actual", "request_id": rid}                           # from an earlier, good session
        (work / "judge_cache.json").write_text(json.dumps({"earlier": earlier}))
        good = oracle_clients(cfg)

        def poison(n):                          # the broken session also pollutes the judge cache
            if n == 12:
                (work / "judge_cache.json").write_text(json.dumps({"earlier": "wished", "bad": "actual"}))
        # the preflight takes 3 calls; a few real items are answered well, then the server breaks
        clients = {"recon": Flaky(good["recon"], good_calls=3 + 15, on_call=poison), "judge": good["judge"]}
        rc, _o, err = self.run_cli(cfg, clients)
        self.assertEqual(rc, 4, err)
        self.assertIn("canary", err)
        self.assertFalse((work / "judge_modality").exists())                          # J never ran
        items = [p for p in work.rglob("*.json") if p.name not in ("models.json", "judge_cache.json")]
        statuses = {json.loads(p.read_text())["status"] for p in items}
        self.assertEqual(statuses, {"quarantined"})
        self.assertTrue(all(json.loads(p.read_text())["quarantined_session"] for p in items))
        q = json.loads((out / "audits" / "Q.json").read_text())
        canary = next(r for r in q["results"] if r["name"] == "canary")
        self.assertFalse(canary["passed"])
        self.assertEqual(canary["counts"]["quarantined_items"], len(items))
        self.assertEqual(json.loads((work / "judge_cache.json").read_text()), {"earlier": earlier})   # cache taken back
        # the next session, with a good server, redoes every quarantined item and finishes
        rc, _o, err = cli(["run", "--config", cfg, "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 0, err)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {})
        self.assertFalse([k for k, v in agg["items"].items() if "quarantined" in v], agg["items"])
        self.assertEqual(agg["n_analysed"], 12)

    def test_a_judge_that_flips_after_the_post_q_canary_is_caught_at_the_end_of_j_and_only_the_j_items_are_taken_back(self):
        import re
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        work = out / "work"
        good = oracle_clients(cfg)
        state = {"flipped": False}

        class FlipJudge:
            model_label = "oracle"
            cfg = good["judge"].cfg                    # a real corpus's calls must name their model

            def complete(self, messages, **kw):
                if not state["flipped"]:
                    return good["judge"].complete(messages, **kw)
                n = len(re.findall(r"^\d+\. ELEMENT", messages[-1]["content"], re.M))
                return MockClient(fn=lambda m: json.dumps({"labels": [{"n": i, "modality": "actual"}
                                                                     for i in range(1, n + 1)]})).complete(messages, **kw)
        real_canary = audits._canary_result

        def canary_then_flip(ctx):
            res = real_canary(ctx)
            if ctx.cfg.corpus.get("kind") == "real":  # (the canary's own synthetic run calls this too)
                state["flipped"] = True               # the server changes after the canary has passed
            return res
        clients = {"recon": good["recon"], "judge": FlipJudge()}
        with mock.patch.object(audits, "_canary_result", canary_then_flip):
            rc, _o, err = self.run_cli(cfg, clients)
        self.assertEqual(rc, 4, err)
        self.assertIn("canary_judge", err)
        j = json.loads((out / "audits" / "J.json").read_text())
        recheck = next(r for r in j["results"] if r["name"] == "canary_judge")
        self.assertFalse(recheck["passed"])
        self.assertEqual(sorted(recheck["failures"]), ["canary_judge_below_rule:negated", "canary_judge_below_rule:wished"])
        self.assertGreater(recheck["counts"]["quarantined_items"], 0)
        j_items = [p for d in ("judge_modality", "judge_shuffled", "judge2_modality") if (work / d).exists()
                   for p in (work / d).glob("*.json")]
        self.assertTrue(j_items)
        self.assertEqual({json.loads(p.read_text())["status"] for p in j_items}, {"quarantined"})
        recon = list((work / "recon").glob("*.json"))
        self.assertTrue(recon)
        self.assertEqual({json.loads(p.read_text())["status"] for p in recon}, {"ok"})      # Q passed its canary: kept
        # the next session with a good server redoes the J items and finishes
        rc, _o, err = cli(["run", "--config", cfg, "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 0, err)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["n_analysed"], 12)

    def test_items_of_an_earlier_good_session_survive_a_later_failed_one(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        self.assertEqual(cli(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"])[0], 0)
        before = {p.name: p.read_bytes() for p in (out / "work" / "recon").glob("*.json")}
        self.assertTrue(before)
        # a new session on the same work folder with a server that is broken from the start: stopped by the preflight
        bad = MockClient(fn=lambda m: "garbage")
        rc, _o, _e = self.run_cli(cfg, {"recon": bad, "judge": bad}, stages="Q")
        self.assertEqual(rc, 4)
        after = {p.name: p.read_bytes() for p in (out / "work" / "recon").glob("*.json")}
        self.assertEqual(before, after)

    def test_quarantine_is_limited_to_what_this_session_wrote(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        self.assertEqual(cli(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"])[0], 0)
        old = sorted((out / "work" / "recon").glob("*.json"))[0]
        old_bytes = old.read_bytes()
        d = json.loads(old.read_text())
        self.assertEqual(d["status"], "ok")
        # a failing canary in a later session that wrote only a new item leaves the old one alone
        recon_good = oracle_clients(cfg)
        from second_scribe.pipeline import RunConfig, _Run
        from second_scribe.audit.registry import AuditLog
        log = AuditLog(out / "l2.jsonl", "x")
        r = _Run(RunConfig.load(cfg), recon_good, log)
        (out / "work" / "recon" / "NEW__full__p0__t0__r0.json").write_text(json.dumps({"status": "ok", "map": None}))
        self.assertEqual(audits.quarantine_session(r), 1)
        self.assertEqual(old.read_bytes(), old_bytes)
        self.assertEqual(json.loads((out / "work" / "recon" / "NEW__full__p0__t0__r0.json").read_text())["status"],
                         "quarantined")


class TestRefusedWritesAndPositionalIds(unittest.TestCase):
    def test_a_refused_write_moves_the_outputs_of_earlier_runs_out_of_aggregates(self):
        out = Path(tempfile.mkdtemp())
        cfg = frozen_real_config(out)
        self.assertEqual(cli(["run", "--config", cfg, "--stages", "all", "--client", "oracle"])[0], 0)
        agg = out / "aggregates"
        self.assertTrue((agg / "aggregates.json").exists() and (agg / "report.md").exists())
        gram = a_real_5gram()
        real_compute = pipeline.compute_aggregates

        def leaky(r):
            a, p_list = real_compute(r)
            a["note"] = gram
            return a, p_list
        with mock.patch.object(pipeline, "compute_aggregates", leaky):
            rc, _o, err = cli(["run", "--config", cfg, "--stages", "M"])
        self.assertEqual(rc, 4)
        self.assertIn("moved to <out_dir>/stale/", err)
        self.assertFalse((agg / "aggregates.json").exists())
        self.assertFalse((agg / "report.md").exists())
        self.assertTrue((out / "stale" / "aggregates.json").exists())
        self.assertNotIn(gram, err)
        self.assertEqual([p.name for p in agg.glob("*") if p.is_file()], [])           # nothing left to pick up

    def test_failures_of_a_real_corpus_carry_positions_never_model_written_ids(self):
        work = Path(tempfile.mkdtemp())
        m = ai_map([("Maria told me about her cancer", "A", "experienced", "waited a long time for the permit"),
                    ("n2", "B", "wished", "should have called")],
                   [("Maria told me about her cancer", "n2", "then it came")])
        put(work, "recon", f"XY__{SPEC}", {"status": "ok", "map": m})
        tr = {"XY": transcript(T_TEXT + " should have called then it came")}
        r = audit_recon_outputs(work, tr, anonymize=True)
        self.assertEqual(r.counts["maps_audited"], 1)
        # break it: an unknown edge target and a bad label vocabulary item
        m["edges"][0]["target"] = "ghost"
        m["nodes"][1]["node_type"] = "banana"
        put(work, "recon", f"XY__{SPEC}", {"status": "ok", "map": m})
        r = audit_recon_outputs(work, tr, anonymize=True)
        self.assertEqual(sorted(r.failures), ["bad_node_type:#0:" + SPEC + ":n#1", "edge_unknown_node:#0:" + SPEC + ":e#0"])
        put(work, "judge_modality", f"XY__{SPEC}", {"status": "ok", "labels": {"Maria told me about her cancer": "sure"},
                                                      "not_judged": [], "n_items": 3})
        j = audit_judge_outputs(work, anonymize=True)
        text = json.dumps(asdict(j))
        self.assertNotIn("Maria", text)
        self.assertNotIn("cancer", text)
        self.assertTrue(any(":n#0" in f for f in j.failures), j.failures)


if __name__ == "__main__":
    unittest.main()
