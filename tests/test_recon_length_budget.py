"""Run-2 call-level fix 1 (review 3.3): one max_tokens for every reconstruction and ladder call (12000), and a
length-specific retry text for a reply cut off at the limit. Fake clients only."""
import json
import tempfile
import unittest
from pathlib import Path

import second_scribe.reconstruct as R
from second_scribe import ladder
from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient, ModelResponse, log_call
from second_scribe.reconstruct import ReconSpec, reconstruct

from tests.test_ladder_wiring import CTX, GOOD, PROMPTS, SPEC, T, RecordingClient

EXPECTED = ("Your previous answer was cut off before it ended (too long). Return the complete JSON again, shorter: "
            "keep each quote to 8-20 words. Begin your answer with {")


class Cutting:
    """Cut off (finish_reason length) on the first call of an item, then a good map; records every message list."""

    def __init__(self):
        self.seen, self.max_tokens = [], []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.seen.append(json.loads(json.dumps(messages)))
        self.max_tokens.append(max_tokens)
        if len(self.seen) == 1:
            return ModelResponse('{"nodes": [{"id": "n1", "label": "Busin', "length", {}, 0, "h")
        return ModelResponse(json.dumps(GOOD), "stop", {}, 0, "h")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-len-")
        self.addCleanup(self._tmp.cleanup)
        self.log = AuditLog(Path(self._tmp.name) / "r.jsonl", "r", clock=lambda: "t")


class TestBudget(Base):
    def test_constants(self):
        self.assertEqual(R.RECON_MAX_TOKENS, 12000)
        self.assertLessEqual(len(R.LENGTH_CORRECTION), R.MAX_MESSAGE_CHARS)
        self.assertEqual(R.LENGTH_CORRECTION, EXPECTED)

    def test_reconstruct_default_is_12000_with_and_without_rung(self):
        c = RecordingClient()
        reconstruct(self.log, c, T, ReconSpec("removed", "p0", 0.0, 1), CTX, PROMPTS, item_id="SYN-OW01__x")
        for key in [r.key for r in ladder.RUNGS]:
            reconstruct(self.log, c, T, SPEC, CTX, PROMPTS, item_id="SYN-OW01__x", rung=ladder.get_rung(key),
                        real_data=False)
        self.assertEqual({x["max_tokens"] for x in c.calls}, {12000})
        self.assertEqual(len(c.calls), 1 + len(ladder.RUNGS))

    def test_run_ladder_arm_default_is_12000_for_every_rung(self):
        for rung in ladder.RUNGS:
            c = RecordingClient()
            ladder.run_ladder_arm(rung, [T], SPEC, [1], log=self.log, client=c, ctx=CTX, prompts_dir=PROMPTS,
                                  real_data=False)
            self.assertEqual({x["max_tokens"] for x in c.calls}, {12000}, rung.key)


class TestRung1GuardStillHolds(unittest.TestCase):
    def test_rung1_prompt_is_byte_identical_and_the_rung_table_validates(self):
        import hashlib
        data = (PROMPTS / "reconstruct.p0.v1.txt").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), ladder.RUNG1_SHA256)
        self.assertEqual(ladder.validate_rungs(PROMPTS), [])
        self.assertNotIn("cut off before it ended", data.decode("utf-8"))    # the correction is not a prompt file


class TestLengthCorrection(Base):
    def test_a_cut_off_reply_gets_the_length_text_not_the_generic_one(self):
        c = Cutting()
        g, st = reconstruct(self.log, c, T, ReconSpec("removed", "p0", 0.0, 1), CTX, PROMPTS, item_id="SYN-OW01__x")
        self.assertEqual(st, "ok")
        retry = c.seen[1]
        self.assertEqual(retry[-1], {"role": "user", "content": EXPECTED})
        self.assertEqual(len(retry), len(c.seen[0]) + 1)             # the cut-off reply is not echoed
        self.assertNotIn("Your previous answer was invalid", retry[-1]["content"])

    def test_identical_for_every_rung(self):
        texts = set()
        for rung in ladder.RUNGS:
            c = Cutting()
            reconstruct(self.log, c, T, SPEC, CTX, PROMPTS, item_id="SYN-OW01__x", rung=rung, real_data=False)
            texts.add(c.seen[1][-1]["content"])
        self.assertEqual(texts, {EXPECTED})

    def test_other_failures_keep_the_generic_correction(self):
        seen = []
        replies = iter(["Sure! Here is the map", json.dumps(GOOD)])

        def fn(m):
            seen.append(json.loads(json.dumps(m)))
            return next(replies)
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("removed", "p0", 0.0, 1), CTX, PROMPTS,
                            item_id="SYN-OW01__x")
        self.assertEqual(st, "ok")
        last = seen[1][-1]["content"]
        self.assertTrue(last.startswith("Your previous answer was invalid: "))
        self.assertTrue(last.endswith("Return only valid JSON. Begin your answer with {"))

    def test_log_call_without_length_correction_is_unchanged(self):
        seen = []

        class Trunc:
            def complete(self, messages, *, temperature, seed, max_tokens):
                seen.append(json.loads(json.dumps(messages)))
                return ModelResponse("cut", "length" if len(seen) == 1 else "stop", {}, 0, "h")
        log_call(self.log, Trunc(), [{"role": "user", "content": "go"}], stage="s", item_id="i", temperature=0,
                 seed=None, max_tokens=5)
        self.assertTrue(seen[1][-1]["content"].startswith("Your previous answer was invalid: empty or truncated"))

    def test_length_text_only_for_length_a_validation_error_on_a_complete_reply_is_generic(self):
        seen = []

        class Bad:
            def complete(self, messages, *, temperature, seed, max_tokens):
                seen.append(json.loads(json.dumps(messages)))
                return ModelResponse("x" if len(seen) == 1 else "ok", "stop", {}, 0, "h")
        log_call(self.log, Bad(), [{"role": "user", "content": "go"}], stage="s", item_id="i", temperature=0,
                 seed=None, max_tokens=5, validate=lambda t: "bad" if t == "x" else None, length_correction=EXPECTED)
        self.assertEqual(seen[1][-1]["content"], "Your previous answer was invalid: bad. Return only valid JSON. "
                                                 "Begin your answer with {")


if __name__ == "__main__":
    unittest.main()
