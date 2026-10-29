"""Run-2 review S3: the quote rule (threshold, span, guard list, version) is fingerprinted in run_start and a real
run refuses to resume when it differs, the same mechanism as prompt_set_sha256. Synthetic / fake clients only."""
import json
import unittest
from unittest import mock

from second_scribe import prereg, textnorm
from tests.test_real_run_validity import Base

L16_ADDED = frozenset("want wanted wants need needed needs hope hoped hopes if unless wishes wishing supposed".split())


class TestFingerprint(unittest.TestCase):
    def test_fingerprint_is_stable_and_names_every_part(self):
        fp = textnorm.quote_rule_fingerprint()
        self.assertEqual(set(fp), {"version", "approx_min_ratio", "approx_max_span", "guard_words", "guard_suffix"})
        self.assertEqual(fp["guard_words"], sorted(textnorm.GUARD_WORDS))
        self.assertEqual(textnorm.quote_rule_sha256(), textnorm.quote_rule_sha256())

    def test_each_part_changes_the_hash(self):
        base = textnorm.quote_rule_sha256()
        with mock.patch.object(textnorm, "APPROX_MIN_RATIO", 0.9):
            self.assertNotEqual(textnorm.quote_rule_sha256(), base)
        with mock.patch.object(textnorm, "APPROX_MAX_SPAN", 1.4):
            self.assertNotEqual(textnorm.quote_rule_sha256(), base)
        with mock.patch.object(textnorm, "GUARD_WORDS", textnorm.GUARD_WORDS | {"zzzneg"}):
            self.assertNotEqual(textnorm.quote_rule_sha256(), base)
        with mock.patch.object(textnorm, "QUOTE_RULE_VERSION", "other"):
            self.assertNotEqual(textnorm.quote_rule_sha256(), base)


class TestRestart(Base):
    def starts(self):
        return [json.loads(l)["payload"] for l in self.log.read_text().splitlines()
                if json.loads(l)["record_type"] == "run_start"]

    def test_run_start_records_the_fingerprint(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        (start,) = self.starts()
        self.assertEqual(start["quote_rule_sha256"], textnorm.quote_rule_sha256())
        self.assertEqual(start["quote_rule"], textnorm.quote_rule_fingerprint())

    def test_resume_with_the_same_rule_works(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J,M")[0], 0)

    def test_resume_after_a_threshold_change_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        n = sum('"call_request"' in l for l in self.log.read_text().splitlines())
        with mock.patch.object(textnorm, "APPROX_MIN_RATIO", 0.9):
            rc, err = self.run_all("J,M")
        self.assertEqual(rc, 2, err)
        self.assertIn("quote rule", err)
        self.assertIn("new out_dir", err)
        self.assertEqual(sum('"call_request"' in l for l in self.log.read_text().splitlines()), n)

    def test_resume_after_a_guard_list_change_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        with mock.patch.object(textnorm, "GUARD_WORDS", textnorm.GUARD_WORDS - {"might"}):
            rc, err = self.run_all("J,M")
        self.assertEqual(rc, 2, err)
        self.assertIn("quote rule", err)

    def test_resume_under_the_old_v1_rule_is_refused_for_real_corpora(self):
        # L16: a real log started under run2-quote-rule-v1 (ratio 0.95, old guard list) must not resume under v2
        old_guards = textnorm.GUARD_WORDS - L16_ADDED
        with mock.patch.object(textnorm, "QUOTE_RULE_VERSION", "run2-quote-rule-v1"), \
                mock.patch.object(textnorm, "APPROX_MIN_RATIO", 0.95), \
                mock.patch.object(textnorm, "GUARD_WORDS", old_guards):
            old_sha = textnorm.quote_rule_sha256()
            self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(textnorm.QUOTE_RULE_VERSION, "run2-quote-rule-v2")
        self.assertNotEqual(old_sha, textnorm.quote_rule_sha256())
        self.assertEqual([s["quote_rule_sha256"] for s in self.starts()], [old_sha])
        n = sum('"call_request"' in l for l in self.log.read_text().splitlines())
        rc, err = self.run_all("J,M")
        self.assertEqual(rc, 2, err)
        self.assertIn("quote rule", err)
        self.assertEqual(sum('"call_request"' in l for l in self.log.read_text().splitlines()), n)
        # and the hash-level check the CLI uses
        self.assertIn("quote rule", prereg.restart_problem(self.log, self.starts()[0]["prompt_set_sha256"],
                                                           self.starts()[0]["config_sha256"],
                                                           textnorm.quote_rule_sha256()))

    def test_restart_problem_ignores_a_log_that_never_recorded_a_rule(self):
        d = self.out / "hand2"
        (d / "logs").mkdir(parents=True)
        from second_scribe.audit.registry import AuditLog
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        log.append("run_start", {"corpus_kind": "real", "prompt_set_sha256": "p", "config_sha256": "c"})
        log.close()
        self.assertIsNone(prereg.restart_problem(d / "logs" / "run.jsonl", "p", "c", "f" * 64))

    def test_restart_problem_refuses_a_different_recorded_rule(self):
        d = self.out / "hand3"
        (d / "logs").mkdir(parents=True)
        from second_scribe.audit.registry import AuditLog
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        log.append("run_start", {"corpus_kind": "real", "prompt_set_sha256": "p", "config_sha256": "c",
                                 "quote_rule_sha256": "a" * 64})
        log.close()
        self.assertIsNone(prereg.restart_problem(d / "logs" / "run.jsonl", "p", "c", "a" * 64))
        self.assertIn("quote rule", prereg.restart_problem(d / "logs" / "run.jsonl", "p", "c", "b" * 64))

    def test_two_rules_in_one_log_fail_run_validity(self):
        d = self.out / "hand"
        (d / "work").mkdir(parents=True)
        (d / "logs").mkdir()
        from second_scribe.audit.registry import AuditLog
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        for h in ("a" * 64, "b" * 64):
            log.append("run_start", {"corpus_kind": "real", "quote_rule_sha256": h})
        log.close()
        self.assertIn("quote rule", prereg.run_validity_problem(d / "logs" / "run.jsonl", d / "work"))


if __name__ == "__main__":
    unittest.main()
