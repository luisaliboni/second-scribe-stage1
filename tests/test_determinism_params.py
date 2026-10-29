"""Determinism compares the same request settings, using only offline clients."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from second_scribe.audits import audit_determinism, sample_from_log, sha256_text


class CapturingClient:
    def __init__(self, defaults=None):
        self.cfg = SimpleNamespace(params=defaults or {})
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="same")


class TestDeterminismParams(unittest.TestCase):
    def sample(self, **extra):
        return {"messages": [{"role": "user", "content": "synthetic question"}],
                "temperature": 0.0, "seed": 17, "max_tokens": 60,
                "expected_sha256": sha256_text("same"), **extra}

    def test_logged_per_call_parameters_survive_sampling_and_resend(self):
        params = {"top_p": 0.7, "stop": ["END"], "logprobs": True,
                  "chat_template_kwargs": {"enable_thinking": False}}
        req = {**self.sample(params=params), "request_id": "synthetic", "stage": "judge:modality"}
        records = [{"record_type": "call_request", "payload": req},
                   {"record_type": "call_response", "payload": {
                       "request_id": "synthetic", "text": "same", "validation_error": None}}]
        with tempfile.TemporaryDirectory(prefix="ss-tests-determinism-params-") as directory:
            path = Path(directory) / "log.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            sample = sample_from_log(path, 1, 0)
        client = CapturingClient({"top_p": 0.9})
        result = audit_determinism(None, client, sample)
        self.assertTrue(result.passed)
        self.assertEqual(client.calls[0]["params"], params)
        self.assertEqual(client.calls[0]["seed"], 17)
        self.assertEqual(client.calls[0]["max_tokens"], 60)

    def test_unavailable_parameters_fail_without_a_call_even_for_remote(self):
        for params in ({"nested": [{"value": "prefix[REDACTED]suffix"}]},
                       {"api_key": "synthetic-secret"}, None, [], {"top_p": float("nan")}):
            with self.subTest(params=params):
                client = CapturingClient()
                result = audit_determinism(None, client, [self.sample(params=params)], remote=True)
                self.assertFalse(result.passed)
                self.assertEqual(client.calls, [])
                self.assertEqual(result.counts["n_errors"], 1)
                self.assertIn("determinism_params_unavailable:1", result.failures)
                self.assertNotIn("synthetic-secret", json.dumps(result.as_dict() if hasattr(result, "as_dict")
                                                                else result.__dict__))

    def test_new_client_default_is_not_silently_added(self):
        client = CapturingClient({"top_p": 0.9})
        result = audit_determinism(None, client, [self.sample(params={})])
        self.assertFalse(result.passed)
        self.assertEqual(client.calls, [])

    def test_old_log_without_params_and_empty_client_defaults_still_works(self):
        client = CapturingClient()
        result = audit_determinism(None, client, [self.sample()])
        self.assertTrue(result.passed)
        self.assertNotIn("params", client.calls[0])

    def test_nested_params_are_isolated_from_client_mutation(self):
        params = {"stop": ["END"]}
        class MutatingClient(CapturingClient):
            def complete(self, messages, **kwargs):
                kwargs["params"]["stop"].append("changed")
                return super().complete(messages, **kwargs)
        self.assertTrue(audit_determinism(None, MutatingClient(), [self.sample(params=params)]).passed)
        self.assertEqual(params, {"stop": ["END"]})
