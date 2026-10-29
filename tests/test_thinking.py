"""F1a item 2: thinking must be off for a real corpus (configured) and is detected in replies (always)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.addr import ip
from second_scribe.audit.registry import AuditLog
from second_scribe.llm import (EgressRefused, HTTPClient, MockClient, ModelConfig, RealGuardRefused, ThinkingDetected,
                               guard_real_clients, log_call)

OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def no_dns(host, port):
    raise AssertionError("no lookup expected")


def http(model, params, name="recon", url="http://127.0.0.1:8000/v1"):
    return HTTPClient(ModelConfig(name=name, endpoint=url, model_id=model, params=params))


class FakeResp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(content="{}", **message_extra):
    return {"choices": [{"message": {"content": content, **message_extra}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 3}}


class TestConfiguredThinkingOff(unittest.TestCase):
    def good(self):
        return {"recon": http("qwen-x", OFF), "judge": http("deepseek-j", OFF, "judge", f"http://{ip(10,0,0,2)}:8001/v1")}

    def test_all_roles_with_thinking_off_pass(self):
        c = self.good()
        c["judge2"] = http("gemma-j", {**OFF, "top_p": 0.8}, "judge2", f"http://{ip(10,0,0,3)}:8002/v1")
        guard_real_clients(c, resolver=no_dns)

    def test_a_role_without_the_off_switch_is_refused_and_named(self):
        for role in ("recon", "judge"):
            for params in ({}, {"chat_template_kwargs": {}}, {"chat_template_kwargs": {"enable_thinking": True}},
                           {"chat_template_kwargs": {"enable_thinking": "false"}},
                           {"chat_template_kwargs": {"enable_thinking": 0}}, {"chat_template_kwargs": None},
                           {"chat_template_kwargs": "enable_thinking=false"}, {"enable_thinking": False}):
                c = self.good()
                c[role] = http(c[role].cfg.model_id, params, role, c[role].cfg.endpoint)
                with self.assertRaises(RealGuardRefused, msg=f"{role} {params}") as cm:
                    guard_real_clients(c, resolver=no_dns)
                self.assertIn(role, str(cm.exception))
                self.assertIn("enable_thinking", str(cm.exception))

    def test_an_egress_refusal_is_also_a_guard_refusal(self):
        self.assertTrue(issubclass(EgressRefused, RealGuardRefused))


class TestThinkingInReplies(unittest.TestCase):
    def complete(self, body):
        c = http("m", OFF)
        with mock.patch("second_scribe.llm._OPENER.open", return_value=FakeResp(body)):
            return c.complete([{"role": "user", "content": "x"}], temperature=0, seed=1, max_tokens=5)

    def test_clean_replies_pass(self):
        for body in (reply(), reply(reasoning_content=""), reply(reasoning_content=None), reply(reasoning=""),
                     {**reply(), "usage": {"completion_tokens_details": {"reasoning_tokens": 0}}}):
            self.assertEqual(self.complete(body).text, "{}")

    def test_thinking_output_is_a_fatal_error_that_names_what_was_seen(self):
        cases = {
            "reasoning_content": reply(reasoning_content="step 1: the participant said"),
            "reasoning": reply(reasoning="because of the transcript"),
            "<think> block": reply(content="<think>hmm</think>{}"),
            "unclosed <think> block": reply(content="<think>hmm {}"),
            "reasoning_tokens": {**reply(), "usage": {"completion_tokens_details": {"reasoning_tokens": 12}}},
        }
        for label, body in cases.items():
            with self.assertRaises(ThinkingDetected, msg=label) as cm:
                self.complete(body)
            self.assertTrue(cm.exception.fatal)
            self.assertNotIn("participant said", str(cm.exception))     # the text itself is not echoed

    def test_log_call_records_the_thinking_reply_as_an_error_and_stops(self):
        d = tempfile.mkdtemp()
        log = AuditLog(Path(d) / "r.jsonl", "r")
        c = http("m", OFF)
        with mock.patch("second_scribe.llm._OPENER.open", return_value=FakeResp(reply(reasoning_content="x" * 40))):
            with self.assertRaises(ThinkingDetected):
                log_call(log, c, [{"role": "user", "content": "hi"}], stage="recon:full", item_id="p1",
                         temperature=0, seed=1, max_tokens=5)
        recs = [json.loads(line) for line in Path(d, "r.jsonl").read_text().splitlines()]
        self.assertEqual([r["record_type"] for r in recs], ["call_request", "call_error"])     # no retry
        err = recs[1]["payload"]["error"]
        self.assertIn("ThinkingDetected", err)
        self.assertIn("reasoning_content", err)
        self.assertIn("40 chars", err)

    def test_mock_replies_are_not_scanned(self):
        d = tempfile.mkdtemp()
        log = AuditLog(Path(d) / "r.jsonl", "r")
        text, _ = log_call(log, MockClient(fn=lambda m: "<think>fine for a mock</think>"),
                           [{"role": "user", "content": "x"}], stage="s", item_id="i", temperature=0, seed=1,
                           max_tokens=5)
        self.assertIsNotNone(text)


if __name__ == "__main__":
    unittest.main()
