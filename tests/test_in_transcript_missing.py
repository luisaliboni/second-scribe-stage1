"""Review M-5 (F1b round 1): a missing off-record check is counted as `in_transcript_missing`, not as a Q-phase
label; a judge whose context window cannot hold the transcript (HTTP 400, context length) marks the item
`too_long_excluded` (final, not redone on every rerun) and the participant is excluded as `in_transcript_too_long`.
Synthetic corpus, fake clients."""
import json
import unittest

from tests.test_outage import Base, is_recon, main, records

TRANSCRIPT_PROMPT = "Below is the transcript of a research interview"


def is_off_record(user):
    return TRANSCRIPT_PROMPT in user


CTX = RuntimeError('model call to http://127.0.0.1:8081/v1 failed: HTTP 400: {"error": {"message": "the request '
                   'exceeds the available context size (8192 tokens), try increasing it", '
                   '"type": "exceed_context_size_error"}}')
BAD_REQUEST = RuntimeError('model call to http://127.0.0.1:8081/v1 failed: HTTP 400: {"error": "unknown parameter"}')


class TestInTranscriptMissing(Base):
    def agg(self):
        return json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())

    def test_the_missing_check_is_its_own_exclusion_reason(self):
        self.assertEqual(self.run_stages("Q,M"), 0)                      # the judge group has not run
        ex = self.agg()["exclusions"]
        self.assertEqual(ex, {"in_transcript_missing": 12})

    def test_a_context_length_400_marks_the_item_too_long_excluded_and_it_is_not_redone(self):
        self.assertEqual(self.run_stages("Q"), 0)
        self.assertEqual(self.run_stages("in_transcript",
                                         judge_fail=lambda u: CTX if is_off_record(u) else None), 0)
        st = self.statuses("in_transcript")
        self.assertEqual(st, {"too_long_excluded": 12}, st)
        n = len(records(self.log))
        self.assertEqual(self.run_stages("J,M"), 0)
        self.assertFalse([s for s in self.calls_after(n) if s == "judge:in_transcript"])     # final: not asked again
        ex = self.agg()["exclusions"]
        self.assertEqual(ex.get("in_transcript_too_long"), 12)
        self.assertNotIn("in_transcript_missing", ex)
        self.assertNotIn("q_inputs_missing", ex)
        self.assertEqual(self.agg()["n_analysed"], 0)

    def test_any_other_400_is_still_a_fallback_that_is_redone_and_counted_as_missing(self):
        self.assertEqual(self.run_stages("Q"), 0)
        self.run_stages("in_transcript", judge_fail=lambda u: BAD_REQUEST if is_off_record(u) else None)
        self.assertEqual(set(self.statuses("in_transcript")), {"fallback"})
        self.assertEqual(self.run_stages("J,M"), 0)                      # the judge is fine now: redone, then ok
        self.assertEqual(set(self.statuses("in_transcript")), {"ok"})

    def test_an_outage_is_still_timeout_excluded(self):
        self.assertEqual(self.run_stages("Q"), 0)
        self.run_stages("in_transcript", judge_fail=lambda u: ConnectionError("x") if is_off_record(u) else None)
        self.assertEqual(set(self.statuses("in_transcript")), {"timeout_excluded"})


if __name__ == "__main__":
    unittest.main()
