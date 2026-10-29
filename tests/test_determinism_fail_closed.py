"""A8 (deferred-scanner exception, moved into F1a): the determinism audit must not turn errors or mismatches into a pass."""
import unittest

from second_scribe import audits
from second_scribe.audits import audit_determinism
from second_scribe.llm import MockClient


def sample(n=10):
    return [{"messages": [{"role": "user", "content": f"q{i}"}], "temperature": 0.0, "seed": i, "max_tokens": 50,
             "expected_sha256": audits.sha256_text(f"answer {i}")} for i in range(n)]


def echo(m):
    return "answer " + m[-1]["content"][1:]


class TestDeterminismFailsClosed(unittest.TestCase):
    def test_a_fully_identical_replay_passes(self):
        r = audit_determinism(None, MockClient(fn=echo), sample(), k=10)
        self.assertTrue(r.passed)
        self.assertEqual(r.failures, [])

    def test_a_mismatch_fails_the_audit(self):
        n = {"i": 0}

        def fn(m):
            n["i"] += 1
            return echo(m) + (" drift" if n["i"] == 3 else "")
        r = audit_determinism(None, MockClient(fn=fn), sample(), k=10)
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["n_identical"], 9)
        self.assertIn("not_identical:1", r.failures)

    def test_an_error_fails_the_audit_and_is_not_swallowed_into_a_warning_only(self):
        def boom(m):
            raise RuntimeError("down")
        r = audit_determinism(None, MockClient(fn=boom), sample(), k=4)
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["n_errors"], 4)
        self.assertIn("determinism_error:4", r.failures)

    def test_one_error_among_identical_replies_still_fails(self):
        n = {"i": 0}

        def fn(m):
            n["i"] += 1
            if n["i"] == 2:
                raise TimeoutError("slow")
            return echo(m)
        r = audit_determinism(None, MockClient(fn=fn), sample(), k=10)
        self.assertFalse(r.passed)
        self.assertEqual((r.counts["n_identical"], r.counts["n_errors"]), (9, 1))

    def test_checking_nothing_is_not_a_pass(self):
        r = audit_determinism(None, MockClient(fn=echo), [], k=10)
        self.assertFalse(r.passed)
        self.assertIn("determinism_nothing_checked:0", r.failures)

    def test_a_failed_determinism_result_shows_as_fail_in_the_summary(self):
        r = audit_determinism(None, MockClient(fn=lambda m: "other"), sample(), k=3)
        s = r.summary()
        self.assertFalse(s["passed"])
        self.assertEqual(s["failure_codes"], {"not_identical": 1})
        self.assertFalse(s["blocking"])           # reported as FAIL in the report; does not stop the run


if __name__ == "__main__":
    unittest.main()
