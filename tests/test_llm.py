import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.audit.verify import find_orphan_requests, verify_internal_chain, verify_run
from second_scribe.llm import (HTTPClient, MockClient, ModelConfig, ModelResponse, RecordingClient, ReplayClient,
                               derive_seed, log_call, messages_sha256, safe_endpoint)


def ok_json(text):
    try:
        json.loads(text)
        return None
    except Exception as e:
        return f"invalid JSON: {e}"


class TestLLM(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_retry_then_success_is_logged_and_verifies(self):
        replies = iter(["not json", '{"ok": true}'])
        client = MockClient(fn=lambda msgs: next(replies))
        log = AuditLog(Path(self.tmp) / "r.jsonl", "run1", clock=lambda: "t")
        text, recs = log_call(log, client, [{"role": "user", "content": "hi"}], stage="recon",
                              item_id="SYN-OW01", temperature=0.0, seed=1, max_tokens=50, validate=ok_json)
        self.assertEqual(text, '{"ok": true}')
        types = [r["record_type"] for r in recs]
        self.assertEqual(types, ["call_request", "call_response", "call_request", "call_response"])
        self.assertEqual(recs[1]["payload"]["validation_error"][:12], "invalid JSON")
        m, h = log.close()
        ok, d = verify_run(Path(self.tmp) / "r.jsonl", m)
        self.assertTrue(ok, d)

    def test_all_attempts_fail_returns_none(self):
        client = MockClient(fn=lambda msgs: "nope")
        log = AuditLog(Path(self.tmp) / "r2.jsonl", "run2", clock=lambda: "t")
        text, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="recon",
                              item_id="i", temperature=0, seed=None, max_tokens=5, validate=ok_json)
        self.assertIsNone(text)
        self.assertEqual(len(recs), 6)

    def test_transport_error_logged_as_call_error(self):
        def boom(msgs):
            raise TimeoutError("busy")
        log = AuditLog(Path(self.tmp) / "r3.jsonl", "run3", clock=lambda: "t")
        text, recs = log_call(log, MockClient(fn=boom), [{"role": "user", "content": "x"}],
                              stage="judge", item_id="i", temperature=0, seed=None, max_tokens=5,
                              max_attempts=2)
        self.assertIsNone(text)
        self.assertEqual([r["record_type"] for r in recs], ["call_request", "call_error"] * 2)
        m, _ = log.close()
        self.assertTrue(verify_run(Path(self.tmp) / "r3.jsonl", m)[0])

    def test_retry_message_is_fed_back_and_request_records_what_was_sent(self):
        seen = []
        replies = iter(["bad", '{"a": 1}'])

        def fn(msgs):
            seen.append([dict(m) for m in msgs])
            return next(replies)
        log = AuditLog(Path(self.tmp) / "r4.jsonl", "run4", clock=lambda: "t")
        msgs = [{"role": "user", "content": "go"}]
        text, recs = log_call(log, MockClient(fn=fn), msgs, stage="recon", item_id="i",
                              temperature=0, seed=7, max_tokens=9, validate=ok_json)
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(len(seen[1]), 3)       # original, the model's bad reply, the correction
        self.assertEqual(seen[1][1], {"role": "assistant", "content": "bad"})
        self.assertEqual(seen[1][2]["role"], "user")
        self.assertTrue(seen[1][2]["content"].startswith("Your previous answer was invalid: invalid JSON"))
        self.assertTrue(seen[1][2]["content"].endswith("Return only valid JSON. Begin your answer with {"))
        req0, req1 = recs[0]["payload"], recs[2]["payload"]
        self.assertEqual(len(req0["messages"]), 1)          # snapshot at send time, not later mutation
        self.assertEqual(len(req1["messages"]), 3)
        self.assertEqual(req0["messages_sha256"],
                         hashlib.sha256(json.dumps(req0["messages"], sort_keys=True,
                                                   ensure_ascii=False).encode()).hexdigest())
        self.assertNotEqual(req0["messages_sha256"], req1["messages_sha256"])
        self.assertEqual((req0["stage"], req0["item_id"], req0["attempt"], req0["seed"], req0["max_tokens"]),
                         ("recon", "i", 1, 7, 9))
        self.assertEqual(recs[1]["payload"]["request_id"], req0["request_id"])
        self.assertNotEqual(req0["request_id"], req1["request_id"])
        self.assertIsNone(recs[3]["payload"]["validation_error"])

    def test_raising_validator_does_not_escape_and_chain_verifies(self):
        def bad_validator(text):
            raise ValueError("validator bug " + "x" * 600)
        path = Path(self.tmp) / "v.jsonl"
        log = AuditLog(path, "runv", clock=lambda: "t")
        text, recs = log_call(log, MockClient(fn=lambda m: "reply"), [{"role": "user", "content": "x"}],
                              stage="recon", item_id="i", temperature=0, seed=None, max_tokens=5,
                              validate=bad_validator, max_attempts=2)
        self.assertIsNone(text)
        self.assertEqual([r["record_type"] for r in recs], ["call_request", "call_response"] * 2)
        err = recs[1]["payload"]["validation_error"]
        self.assertTrue(err.startswith("validator raised: ValueError("))
        self.assertLessEqual(len(err), 500)
        m, _ = log.close()
        ok, d = verify_run(path, m)
        self.assertTrue(ok, d)

    def test_raising_validator_then_recovery_on_retry(self):
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("once")
            return None
        log = AuditLog(Path(self.tmp) / "v2.jsonl", "runv2", clock=lambda: "t")
        text, recs = log_call(log, MockClient(fn=lambda m: "reply"), [{"role": "user", "content": "x"}],
                              stage="s", item_id="i", temperature=0, seed=None, max_tokens=5, validate=flaky)
        self.assertEqual(text, "reply")
        self.assertEqual(len(recs), 4)

    def test_unloggable_response_gets_call_error_not_orphan(self):
        class OddClient:
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse("reply", "stop", {"tokens": float("nan")}, 0, "h")
        path = Path(self.tmp) / "u.jsonl"
        log = AuditLog(path, "runu", clock=lambda: "t")
        text, recs = log_call(log, OddClient(), [{"role": "user", "content": "x"}], stage="s",
                              item_id="i", temperature=0, seed=None, max_tokens=5, max_attempts=2)
        self.assertIsNone(text)    # a reply that could not be logged is not returned
        self.assertEqual([r["record_type"] for r in recs], ["call_request", "call_error"] * 2)
        self.assertIn("could not be logged", recs[1]["payload"]["error"])
        self.assertEqual(recs[1]["payload"]["request_id"], recs[0]["payload"]["request_id"])
        m, _ = log.close()
        ok, d = verify_run(path, m)
        self.assertTrue(ok, d)

    def test_api_key_value_is_scrubbed_from_every_logged_error_string(self):
        key = "sk-test-98765-abcdef"
        os.environ["SS_TEST_SCRUB_KEY"] = key
        self.addCleanup(os.environ.pop, "SS_TEST_SCRUB_KEY", None)

        def boom(msgs):
            raise RuntimeError(f"401 for Bearer {key}")
        client = MockClient(fn=boom)
        client.cfg = ModelConfig(name="r", endpoint="http://x/v1", model_id="m", api_key_env="SS_TEST_SCRUB_KEY")
        path = Path(self.tmp) / "s.jsonl"
        log = AuditLog(path, "runs", clock=lambda: "t")
        text, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                              temperature=0, seed=None, max_tokens=5, max_attempts=1)
        self.assertIsNone(text)
        self.assertEqual(recs[1]["payload"]["error"], "RuntimeError('401 for Bearer [REDACTED]')")
        # validator output and validator crashes are error strings too
        client.fn = lambda msgs: "reply"
        _, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                           temperature=0, seed=None, max_tokens=5, max_attempts=1,
                           validate=lambda t: f"echoed {key}")
        self.assertEqual(recs[1]["payload"]["validation_error"], "echoed [REDACTED]")

        def crashing(t):
            raise ValueError(key)
        _, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                           temperature=0, seed=None, max_tokens=5, max_attempts=1, validate=crashing)
        self.assertEqual(recs[1]["payload"]["validation_error"], "validator raised: ValueError('[REDACTED]')")
        self.assertNotIn(key, path.read_text())

    def test_scrub_ignores_unset_or_empty_key(self):
        os.environ["SS_TEST_EMPTY_KEY"] = ""
        self.addCleanup(os.environ.pop, "SS_TEST_EMPTY_KEY", None)
        for env_name in ("SS_TEST_EMPTY_KEY", "SS_TEST_NEVER_SET_KEY"):
            client = MockClient(fn=lambda m: (_ for _ in ()).throw(RuntimeError("plain error")))
            client.cfg = ModelConfig(name="r", endpoint="http://x/v1", model_id="m", api_key_env=env_name)
            log = AuditLog(Path(self.tmp) / f"{env_name}.jsonl", "r", clock=lambda: "t")
            _, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                               temperature=0, seed=None, max_tokens=5, max_attempts=1)
            self.assertEqual(recs[1]["payload"]["error"], "RuntimeError('plain error')")

    def test_model_id_logged_when_client_has_cfg(self):
        cfg = ModelConfig(name="recon", endpoint="http://x/v1", model_id="m-1")
        client = MockClient(fn=lambda m: "ok")
        client.cfg = cfg
        log = AuditLog(Path(self.tmp) / "r5.jsonl", "run5", clock=lambda: "t")
        _, recs = log_call(log, client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                           temperature=0, seed=None, max_tokens=1)
        self.assertEqual(recs[0]["payload"]["model_id"], "m-1")

    # ---- Task 5B harness guards ----

    def _log(self, name):
        return AuditLog(Path(self.tmp) / f"{name}.jsonl", name, clock=lambda: "t")

    def test_empty_reply_is_a_validation_error_even_without_validate(self):
        seen = []
        replies = iter(["", "   \n", "fine"])

        def fn(msgs):
            seen.append([dict(m) for m in msgs])
            return next(replies)
        text, recs = log_call(self._log("g1"), MockClient(fn=fn), [{"role": "user", "content": "go"}],
                              stage="s", item_id="i", temperature=0, seed=None, max_tokens=5)
        self.assertEqual(text, "fine")
        resp = [r["payload"] for r in recs if r["record_type"] == "call_response"]
        self.assertEqual(resp[0]["validation_error"], "empty or truncated output (finish_reason=stop)")
        self.assertEqual(resp[1]["validation_error"], "empty or truncated output (finish_reason=stop)")
        self.assertIsNone(resp[2]["validation_error"])
        # an empty reply is retried with the messages unchanged: no echo, no extra user turn
        self.assertEqual(seen[1], [{"role": "user", "content": "go"}])
        self.assertEqual(seen[2], [{"role": "user", "content": "go"}])

    def test_length_finish_is_a_validation_error_and_validate_is_not_called(self):
        class Truncating(MockClient):
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse('{"a": 1', "length", {}, 0, "h")
        calls = []
        text, recs = log_call(self._log("g2"), Truncating(fn=lambda m: ""), [{"role": "user", "content": "go"}],
                              stage="s", item_id="i", temperature=0, seed=None, max_tokens=5,
                              validate=lambda t: calls.append(t) or None, max_attempts=2)
        self.assertIsNone(text)
        self.assertEqual(calls, [])
        errs = [r["payload"]["validation_error"] for r in recs if r["record_type"] == "call_response"]
        self.assertEqual(errs, ["empty or truncated output (finish_reason=length)"] * 2)

    def test_truncated_reply_is_not_echoed_retry_gets_the_correction_only(self):
        seen = []

        class Truncating(MockClient):
            def complete(self, messages, *, temperature, seed, max_tokens):
                seen.append([dict(m) for m in messages])
                return ModelResponse("a long cut off reply", "length" if len(seen) == 1 else "stop", {}, 0, "h")
        text, _ = log_call(self._log("g2b"), Truncating(fn=lambda m: ""), [{"role": "user", "content": "go"}],
                           stage="s", item_id="i", temperature=0, seed=None, max_tokens=5)
        self.assertEqual(text, "a long cut off reply")
        self.assertEqual([m["role"] for m in seen[1]], ["user", "user"])
        self.assertNotIn("a long cut off reply", json.dumps(seen[1]))
        self.assertTrue(seen[1][1]["content"].startswith("Your previous answer was invalid: empty or truncated"))
        self.assertTrue(seen[1][1]["content"].endswith("Begin your answer with {"))

    def test_length_finish_with_valid_json_is_still_rejected(self):
        class Truncating(MockClient):
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse('{"a": 1}', "length", {}, 0, "h")
        text, _ = log_call(self._log("g3"), Truncating(fn=lambda m: ""), [{"role": "user", "content": "go"}],
                           stage="s", item_id="i", temperature=0, seed=None, max_tokens=5,
                           validate=ok_json, max_attempts=1)
        self.assertIsNone(text)

    def test_finish_reason_none_with_text_is_accepted(self):
        class NoFinish(MockClient):
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse("hello", None, {}, 0, "h")
        text, _ = log_call(self._log("g4"), NoFinish(fn=lambda m: ""), [{"role": "user", "content": "go"}],
                           stage="s", item_id="i", temperature=0, seed=None, max_tokens=5)
        self.assertEqual(text, "hello")

    def test_retry_feeds_back_previous_reply_as_assistant_turn_then_correction(self):
        seen = []
        replies = iter(["bad reply", '{"a": 1}'])

        def fn(msgs):
            seen.append([dict(m) for m in msgs])
            return next(replies)
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        log_call(self._log("g5"), MockClient(fn=fn), msgs, stage="s", item_id="i", temperature=0,
                 seed=None, max_tokens=5, validate=ok_json)
        self.assertEqual([m["role"] for m in seen[1]], ["system", "user", "assistant", "user"])
        self.assertEqual(seen[1][2]["content"], "bad reply")
        self.assertTrue(seen[1][3]["content"].startswith("Your previous answer was invalid: invalid JSON"))

    def test_call_request_records_params_redacted_and_max_tokens(self):
        os.environ["SS_TEST_KEY"] = "sk-very-secret-value"
        self.addCleanup(os.environ.pop, "SS_TEST_KEY", None)
        cfg = ModelConfig(name="recon", endpoint="http://x/v1", model_id="m", api_key_env="SS_TEST_KEY",
                          params={"chat_template_kwargs": {"enable_thinking": False}, "top_p": 0.8,
                                  "api_key": "sk-inline", "extra_headers": {"Authorization": "Bearer abc"},
                                  "note": "uses sk-very-secret-value somewhere", "stop_token": "END"})
        client = MockClient(fn=lambda m: "ok")
        client.cfg = cfg
        _, recs = log_call(self._log("g6"), client, [{"role": "user", "content": "x"}], stage="s",
                           item_id="i", temperature=0.3, seed=1, max_tokens=77)
        req = recs[0]["payload"]
        self.assertEqual(req["max_tokens"], 77)
        self.assertEqual(req["params"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(req["params"]["top_p"], 0.8)
        self.assertEqual(req["params"]["stop_token"], "END")   # only exact secret key names are redacted
        # redact_params marks them; the audit log's own backstop may re-mark string secrets
        redacted = ("[REDACTED]", "***REDACTED***")
        self.assertIn(req["params"]["api_key"], redacted)
        self.assertIn(req["params"]["extra_headers"]["Authorization"], redacted)
        self.assertNotIn("sk-very-secret-value", json.dumps(req))
        self.assertNotIn("sk-inline", json.dumps(req))
        self.assertEqual(cfg.params["api_key"], "sk-inline")   # the config itself is untouched

    def test_redact_params_redacts_names_containing_a_secret_word(self):
        from second_scribe.llm import redact_params
        out = redact_params({"api_key": "a", "API-Key": "b", "key": "c", "token": "d", "Authorization": "e",
                             "password": "f", "secret": "g", "bearer": "h", "access_token": "i",
                             "client_secret": "j", "x-api-key": "k", "passwd": "l", "Cookie": "m",
                             "credentials": "n", "n": {"token": "x", "refresh_token": "y"}})
        for k in ("api_key", "API-Key", "key", "token", "Authorization", "password", "secret", "bearer",
                  "access_token", "client_secret", "x-api-key", "passwd", "Cookie", "credentials"):
            self.assertEqual(out[k], "[REDACTED]", k)
        self.assertEqual(out["n"], {"token": "[REDACTED]", "refresh_token": "[REDACTED]"})

    def test_redact_params_lets_known_inference_settings_through_even_when_nested(self):
        from second_scribe.llm import redact_params
        params = {"stop_token": "END", "stop_tokens": ["a"], "max_tokens": 5, "max_new_tokens": 9, "top_k": 20,
                  "n_probs": 3, "top_logprobs": 2, "logprobs": True, "cache_prompt": True, "keep_alive": "5m",
                  "options": {"stop_token": "###", "max_tokens": 7, "top_k": 40, "access_token": "t"},
                  "Max-Tokens": 3}
        out = redact_params(params)
        self.assertEqual({k: v for k, v in out.items() if k != "options"},
                         {k: v for k, v in params.items() if k != "options"})
        self.assertEqual(out["options"], {"stop_token": "###", "max_tokens": 7, "top_k": 40,
                                          "access_token": "[REDACTED]"})

    def test_auth_names_are_redacted_everywhere(self):
        from second_scribe.audit.registry import redact
        from second_scribe.llm import redact_params
        params = {"auth": "a", "authentication": "b", "auth_header": "c", "x-auth": "d", "Auth-Token": "e",
                  "top_k": 20}
        out = redact_params(params)
        self.assertEqual({k: out[k] for k in params if k != "top_k"}, {k: "[REDACTED]" for k in params if k != "top_k"})
        self.assertEqual(out["top_k"], 20)
        back = redact({k: str(v) for k, v in params.items()})
        for k in ("auth", "authentication", "auth_header", "x-auth", "Auth-Token"):
            self.assertEqual(back[k], "***REDACTED***", k)

    def test_audit_backstop_redacts_secret_names_and_exempts_the_allowlist(self):
        from second_scribe.audit.registry import redact
        out = redact({"stop_token": "END", "top_k": "20", "keep_alive": "5m", "access_token": "t",
                      "client_secret": "s", "x-api-key": "k", "token": "t", "api_key": "k",
                      "nested": [{"max_tokens": "7", "Cookie": "c"}]})
        self.assertEqual((out["stop_token"], out["top_k"], out["keep_alive"]), ("END", "20", "5m"))
        for k in ("access_token", "client_secret", "x-api-key", "token", "api_key"):
            self.assertEqual(out[k], "***REDACTED***", k)
        self.assertEqual(out["nested"], [{"max_tokens": "7", "Cookie": "***REDACTED***"}])

    def test_call_request_log_redacts_secret_params_and_keeps_inference_settings(self):
        cfg = ModelConfig(name="n", endpoint="http://x/v1", model_id="m",
                          params={"access_token": "tok-123", "extra": {"client_secret": "cs-456", "top_k": 20,
                                                                          "max_tokens_cap": 3},
                                  "stop_token": "END"})
        client = MockClient(fn=lambda m: "ok")
        client.cfg = cfg
        _, recs = log_call(self._log("g8"), client, [{"role": "user", "content": "x"}], stage="s", item_id="i",
                           temperature=0, seed=None, max_tokens=1)
        raw = json.dumps(recs[0]["payload"])
        self.assertNotIn("tok-123", raw); self.assertNotIn("cs-456", raw)
        self.assertEqual(recs[0]["payload"]["params"]["stop_token"], "END")
        self.assertEqual(recs[0]["payload"]["params"]["extra"]["top_k"], 20)

    def test_params_may_not_override_what_log_call_records(self):
        for k in ("model", "messages", "temperature", "max_tokens", "seed"):
            with self.assertRaises(ValueError, msg=k) as cm:
                ModelConfig(name="n", endpoint="http://x/v1", model_id="m", params={k: 1})
            self.assertIn(k, str(cm.exception))
        ModelConfig(name="n", endpoint="http://x/v1", model_id="m", params={"top_p": 0.9, "stop": ["x"]})
        os.environ["SS_RSV_URL"], os.environ["SS_RSV_MODEL"] = "http://x/v1", "m"
        os.environ["SS_RSV_PARAMS"] = '{"temperature": 0.1}'
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("SS_RSV_URL", "SS_RSV_MODEL", "SS_RSV_PARAMS")])
        with self.assertRaises(ValueError):
            ModelConfig.from_env("rsv")

    def test_call_request_params_empty_when_client_has_no_cfg(self):
        _, recs = log_call(self._log("g7"), MockClient(fn=lambda m: "ok"), [{"role": "user", "content": "x"}],
                           stage="s", item_id="i", temperature=0, seed=None, max_tokens=1)
        self.assertEqual(recs[0]["payload"]["params"], {})

    def test_derive_seed_stable_and_small(self):
        self.assertEqual(derive_seed("a", 1), derive_seed("a", 1))
        self.assertNotEqual(derive_seed("a", 1), derive_seed("a", 2))
        self.assertLess(derive_seed("a", 1), 2**60)
        self.assertEqual(derive_seed("a", 1),
                         int(hashlib.sha256(b"a:1").hexdigest()[:15], 16))

    def test_safe_endpoint_drops_userinfo_and_query(self):
        self.assertEqual(safe_endpoint("https://u:p@host:8080/v1?token=abc"), "https://host:8080/v1")
        self.assertIsNone(safe_endpoint(None))

    def test_model_config_from_env_and_repr_hides_key_name(self):
        env = {"SS_RECON_URL": "http://localhost:1/v1", "SS_RECON_MODEL": "m", "SS_RECON_KEY_ENV": "MY_SECRET_VAR",
               "SS_RECON_PARAMS": '{"chat_template_kwargs": {"enable_thinking": false}}'}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            cfg = ModelConfig.from_env("recon")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual((cfg.name, cfg.endpoint, cfg.model_id, cfg.api_key_env),
                         ("recon", "http://localhost:1/v1", "m", "MY_SECRET_VAR"))
        self.assertEqual(cfg.params, {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("MY_SECRET_VAR", repr(cfg))

    def test_model_config_timeout_from_env(self):
        """q27b: a slow server (a dense 27B at ~10 tokens/s writes a long map in more than 600 s) needs a longer
        per-request timeout; SS_<PREFIX>_TIMEOUT_S sets it, unset keeps 600 s, a non-positive or non-number refuses."""
        base = {"SS_TQ_URL": "http://127.0.0.1:1/v1", "SS_TQ_MODEL": "m"}
        with mock.patch.dict(os.environ, base, clear=False):
            os.environ.pop("SS_TQ_TIMEOUT_S", None)
            self.assertEqual(ModelConfig.from_env("tq").timeout_s, 600.0)
        with mock.patch.dict(os.environ, {**base, "SS_TQ_TIMEOUT_S": "1800"}, clear=False):
            self.assertEqual(ModelConfig.from_env("tq").timeout_s, 1800.0)
        for bad in ("0", "-5", "soon", "nan", "inf"):
            with mock.patch.dict(os.environ, {**base, "SS_TQ_TIMEOUT_S": bad}, clear=False):
                with self.assertRaises(ValueError, msg=bad):
                    ModelConfig.from_env("tq")

    def test_model_config_from_env_missing_raises(self):
        with self.assertRaises(KeyError):
            ModelConfig.from_env("definitely_unset_prefix_xyz")

    def test_http_client_retries_on_transport_error(self):
        calls = []
        cfg = ModelConfig(name="recon", endpoint="http://127.0.0.1:9/v1", model_id="m", timeout_s=0.01)
        c = HTTPClient(cfg, attempts=3, sleep=lambda s: calls.append(s))
        with self.assertRaises(RuntimeError):
            c.complete([{"role": "user", "content": "x"}], temperature=0, seed=None, max_tokens=1)
        self.assertEqual(len(calls), 2)   # slept between 3 attempts
        self.assertEqual(calls, [2, 4])   # exponential backoff from backoff[0]

    def test_backoff_is_capped(self):
        calls = []
        cfg = ModelConfig(name="r", endpoint="http://127.0.0.1:9/v1", model_id="m", timeout_s=0.01)
        c = HTTPClient(cfg, attempts=5, backoff=(2, 5), sleep=lambda s: calls.append(s))
        with self.assertRaises(RuntimeError):
            c.complete([{"role": "user", "content": "x"}], temperature=0, seed=None, max_tokens=1)
        self.assertEqual(calls, [2, 4, 5, 5])


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        srv = self.server
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        srv.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "body": json.loads(body)})
        status, payload = srv.script.pop(0) if srv.script else srv.default
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


GOOD = (200, {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


class TestHTTPClientAgainstLocalServer(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.seen, self.srv.script, self.srv.default = [], [], GOOD
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.srv.server_port}/v1"
        self.sleeps = []

    def client(self, **kw):
        cfg = ModelConfig(name="recon", endpoint=self.endpoint, model_id="m-1", timeout_s=5, **kw)
        return HTTPClient(cfg, attempts=3, sleep=self.sleeps.append)

    MSGS = [{"role": "user", "content": "x"}]

    def test_success_body_headers_and_response_fields(self):
        os.environ["SS_TEST_KEY_VALUE"] = "sk-test-12345"
        self.addCleanup(os.environ.pop, "SS_TEST_KEY_VALUE", None)
        c = self.client(api_key_env="SS_TEST_KEY_VALUE",
                        params={"chat_template_kwargs": {"enable_thinking": False}})
        r = c.complete(self.MSGS, temperature=0.2, seed=42, max_tokens=17)
        seen = self.srv.seen[0]
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-test-12345")
        self.assertEqual(seen["body"], {"model": "m-1", "messages": self.MSGS, "temperature": 0.2,
                                        "max_tokens": 17, "seed": 42,
                                        "chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual((r.text, r.finish_reason, r.usage), ("hello", "stop",
                                                              {"prompt_tokens": 3, "completion_tokens": 1}))
        self.assertIsInstance(r.latency_ms, int)
        self.assertEqual(len(r.raw_sha256), 64)
        self.assertNotIn("sk-test-12345", repr(r) + repr(c) + repr(c.cfg))

    def test_params_mutated_after_construction_cannot_override_what_is_logged(self):
        c = self.client(params={"top_p": 0.9})
        c.cfg.params.update({"max_tokens": 99999, "temperature": 5, "model": "evil", "messages": [], "seed": 7,
                             "top_k": 20})
        c.complete(self.MSGS, temperature=0.2, seed=None, max_tokens=17)
        body = self.srv.seen[0]["body"]
        self.assertEqual((body["max_tokens"], body["temperature"], body["model"], body["messages"]),
                         (17, 0.2, "m-1", self.MSGS))
        self.assertNotIn("seed", body)                 # seed=None sent no seed; params cannot add one
        self.assertEqual((body["top_p"], body["top_k"]), (0.9, 20))   # other params still go through
        c.complete(self.MSGS, temperature=0.2, seed=42, max_tokens=17)
        self.assertEqual(self.srv.seen[1]["body"]["seed"], 42)

    def test_no_seed_no_auth_when_absent(self):
        self.client().complete(self.MSGS, temperature=0, seed=None, max_tokens=1)
        seen = self.srv.seen[0]
        self.assertNotIn("seed", seen["body"])
        self.assertIsNone(seen["auth"])

    def test_retries_429_and_5xx_then_succeeds(self):
        self.srv.script = [(429, {"error": "slow down"}), (503, {"error": "down"}), GOOD]
        r = self.client().complete(self.MSGS, temperature=0, seed=None, max_tokens=1)
        self.assertEqual(r.text, "hello")
        self.assertEqual(len(self.srv.seen), 3)
        self.assertEqual(self.sleeps, [2, 4])

    def test_persistent_5xx_raises_runtime_error_after_attempts(self):
        self.srv.default = (500, {"error": "boom"})
        with self.assertRaises(RuntimeError):
            self.client().complete(self.MSGS, temperature=0, seed=None, max_tokens=1)
        self.assertEqual(len(self.srv.seen), 3)

    def test_4xx_other_than_429_raises_immediately_with_body_prefix(self):
        self.srv.default = (400, {"error": "bad request " + "z" * 500})
        with self.assertRaises(RuntimeError) as cm:
            self.client().complete(self.MSGS, temperature=0, seed=None, max_tokens=1)
        self.assertEqual(len(self.srv.seen), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("bad request", str(cm.exception))
        self.assertIn("400", str(cm.exception))
        self.assertLess(len(str(cm.exception)), 600)

    def test_http_error_body_echoing_the_key_is_scrubbed(self):
        key = "sk-test-98765-abcdef"
        os.environ["SS_TEST_KEY_VALUE"] = key
        self.addCleanup(os.environ.pop, "SS_TEST_KEY_VALUE", None)
        # key sits across the 300-char cut: truncating before scrubbing would leak a prefix of it
        self.srv.default = (400, {"error": "x" * 270 + " bad key " + key})
        with self.assertRaises(RuntimeError) as cm:
            self.client(api_key_env="SS_TEST_KEY_VALUE").complete(
                self.MSGS, temperature=0, seed=None, max_tokens=1)
        msg = str(cm.exception)
        self.assertNotIn("sk-test", msg)
        self.assertIn("[REDA", msg)      # marker present (possibly cut by the 300-char limit)

    def test_malformed_success_body_raises_runtime_error(self):
        self.srv.default = (200, b"<html>proxy</html>")
        with self.assertRaises(RuntimeError):
            self.client().complete(self.MSGS, temperature=0, seed=None, max_tokens=1)

    def test_key_never_reaches_audit_log(self):
        os.environ["SS_TEST_KEY_VALUE"] = "sk-test-12345"
        self.addCleanup(os.environ.pop, "SS_TEST_KEY_VALUE", None)
        tmp = tempfile.mkdtemp()
        log = AuditLog(Path(tmp) / "k.jsonl", "k", clock=lambda: "t")
        self.srv.default = (401, {"error": "nope"})
        c = self.client(api_key_env="SS_TEST_KEY_VALUE")
        text, recs = log_call(log, c, self.MSGS, stage="s", item_id="i", temperature=0, seed=None,
                              max_tokens=1, max_attempts=1)
        self.assertIsNone(text)
        self.assertNotIn("sk-test-12345", (Path(tmp) / "k.jsonl").read_text())
        self.assertEqual(recs[1]["record_type"], "call_error")


class _CountingListener:
    """A loopback TCP listener that accepts and counts connections and records the bytes it receives."""

    def __init__(self):
        import socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.received = b""
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.settimeout(0.2)
            try:
                self.received += conn.recv(65536)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop.set()
        self._t.join(timeout=5)
        self.sock.close()


class TestHTTPClientIgnoresProxiesAndRedirects(unittest.TestCase):
    """C-F1-1: the real corpus must go only to the configured endpoint, never to a proxy named by the
    environment or the system, and never to a redirect target."""

    MSGS = [{"role": "user", "content": "SYNTHETIC-TRANSCRIPT-TEXT"}]

    def setUp(self):
        # D-1: urllib.request.urlopen caches a process-wide opener whose ProxyHandler reads the environment on
        # its first call, so an earlier test can make a revert to urlopen look fine. Make any use of urlopen
        # (or of the cached default opener) fail loudly, whatever ran before.
        p = mock.patch("urllib.request.urlopen",
                       side_effect=AssertionError("model calls must use llm._OPENER, not urlopen"))
        p.start()
        self.addCleanup(p.stop)

    def test_the_module_opener_has_no_proxy_and_refuses_redirects(self):
        from second_scribe import llm
        handlers = llm._OPENER.handlers
        # ProxyHandler({}) is dropped by build_opener (an empty one defines no *_open method) AND it replaces
        # the default env-reading ProxyHandler, so the check is: no ProxyHandler with proxies is installed, and
        # only the plain HTTP(S) handlers open http/https URLs.
        self.assertFalse([h for h in handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies])
        for scheme, cls in (("http", urllib.request.HTTPHandler), ("https", urllib.request.HTTPSHandler)):
            self.assertEqual([type(h) for h in llm._OPENER.handle_open[scheme]], [cls])
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([type(h) for h in redirects], [llm._RefuseRedirects])      # no stock redirect handler
        with self.assertRaises(urllib.error.HTTPError) as cm:
            redirects[0].redirect_request(urllib.request.Request("http://127.0.0.1:1/x"), None, 302, "Found",
                                          {}, "http://127.0.0.1:2/y")
        self.assertIn("redirect refused", cm.exception.msg)

    def test_client_uses_the_module_opener(self):
        srv = self._server()
        # urlopen is patched to raise in setUp, so a client that used it could not return text
        r = self._client(srv).complete(self.MSGS, temperature=0.0, seed=1, max_tokens=5)
        self.assertEqual(r.text, "hello")

    def _server(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        srv.seen, srv.script, srv.default = [], [], GOOD
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def _client(self, srv):
        cfg = ModelConfig(name="recon", endpoint=f"http://127.0.0.1:{srv.server_port}/v1", model_id="m-1",
                          timeout_s=5)
        return HTTPClient(cfg, attempts=1, sleep=lambda s: None)

    def test_proxy_environment_is_ignored(self):
        proxy = _CountingListener()
        self.addCleanup(proxy.close)
        srv = self._server()
        url = f"http://127.0.0.1:{proxy.port}"
        env = {k: url for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy",
                                "ALL_PROXY")}
        with mock.patch.dict(os.environ, env):
            for k in ("no_proxy", "NO_PROXY"):
                os.environ.pop(k, None)
            r = self._client(srv).complete(self.MSGS, temperature=0.0, seed=1, max_tokens=5)
        self.assertEqual(r.text, "hello")
        self.assertEqual(len(srv.seen), 1)            # the request reached the configured endpoint
        self.assertEqual(proxy.connections, 0)         # and the proxy listener received nothing
        self.assertEqual(proxy.received, b"")

    def test_redirects_are_refused_and_not_followed(self):
        hits = []

        class Target(_Handler):
            def do_GET(self):
                hits.append(self.path)
                raw = json.dumps(GOOD[1]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        target = HTTPServer(("127.0.0.1", 0), Target)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        origin = self._server()
        loc = f"http://127.0.0.1:{target.server_port}/elsewhere"

        class Redir(_Handler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                self.send_response(302)
                self.send_header("Location", loc)
                self.send_header("Content-Length", "0")
                self.end_headers()

        origin.RequestHandlerClass = Redir
        with self.assertRaises(RuntimeError) as cm:
            self._client(origin).complete(self.MSGS, temperature=0.0, seed=1, max_tokens=5)
        self.assertIn("HTTP 302", str(cm.exception))   # refused by the opener, not a connection failure
        self.assertEqual(hits, [])                     # the redirect target was never contacted


class TestMockAndReplay(unittest.TestCase):
    def test_mock_canned_by_last_user_sha(self):
        h = hashlib.sha256(b"question").hexdigest()
        c = MockClient(canned={h: "answer"})
        r = c.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "question"}],
                       temperature=0, seed=None, max_tokens=1)
        self.assertEqual((r.text, r.finish_reason, r.usage, r.latency_ms), ("answer", "stop", {}, 0))
        self.assertEqual(r.raw_sha256, hashlib.sha256(b"answer").hexdigest())

    def test_mock_unknown_prompt_raises_keyerror_with_hash(self):
        with self.assertRaises(KeyError) as cm:
            MockClient(canned={}).complete([{"role": "user", "content": "q"}],
                                           temperature=0, seed=None, max_tokens=1)
        self.assertIn(hashlib.sha256(b"q").hexdigest(), str(cm.exception))

    def test_replay_serves_by_request_sha256_and_raises_on_miss(self):
        msgs = [{"role": "user", "content": "q"}]
        key = hashlib.sha256(json.dumps(msgs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        p = Path(tempfile.mkdtemp()) / "fx.json"
        p.write_text(json.dumps({key: "recorded"}))
        c = ReplayClient(str(p))
        self.assertEqual(c.complete(msgs, temperature=0, seed=None, max_tokens=1).text, "recorded")
        with self.assertRaises(KeyError) as cm:
            c.complete([{"role": "user", "content": "other"}], temperature=0, seed=None, max_tokens=1)
        self.assertIn(hashlib.sha256(json.dumps([{"role": "user", "content": "other"}], sort_keys=True,
                                                ensure_ascii=False).encode()).hexdigest(), str(cm.exception))

    def test_replay_serves_a_full_response_record(self):
        msgs = [{"role": "user", "content": "q"}]
        p = Path(tempfile.mkdtemp()) / "fx.json"
        p.write_text(json.dumps({messages_sha256(msgs): {"text": "cut", "finish_reason": "length",
                                                         "usage": {"completion_tokens": 3}}}))
        r = ReplayClient(str(p)).complete(msgs, temperature=0, seed=None, max_tokens=1)
        self.assertEqual((r.text, r.finish_reason, r.usage), ("cut", "length", {"completion_tokens": 3}))

    def test_recording_client_writes_fixtures_that_replay_identically(self):
        class Inner:
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse("echo " + messages[-1]["content"], "stop", {"total_tokens": 7}, 12, "abc")
        p = Path(tempfile.mkdtemp()) / "fx.json"
        rec = RecordingClient(Inner(), str(p))
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        r1 = rec.complete(msgs, temperature=0, seed=1, max_tokens=5)
        rec.save()
        stored = json.loads(p.read_text())
        self.assertEqual(stored[messages_sha256(msgs)],
                         {"text": "echo q", "finish_reason": "stop", "usage": {"total_tokens": 7}, "temperature": 0.0})
        r2 = ReplayClient(str(p)).complete(msgs, temperature=0, seed=1, max_tokens=5)
        self.assertEqual((r2.text, r2.finish_reason, r2.usage), (r1.text, r1.finish_reason, r1.usage))

    def test_a_replay_miss_stops_log_call_and_leaves_no_orphan(self):
        from second_scribe.llm import FixtureMissing
        d = Path(tempfile.mkdtemp())
        (d / "fx.json").write_text("{}")
        log = AuditLog(d / "r.jsonl", "r")
        with self.assertRaises(FixtureMissing):
            log_call(log, ReplayClient(str(d / "fx.json")), [{"role": "user", "content": "q"}], stage="s",
                     item_id="i", temperature=0, seed=None, max_tokens=5)
        log.close()
        types = [json.loads(line)["record_type"] for line in open(d / "r.jsonl")]
        self.assertEqual(types, ["call_request", "call_error"])          # one attempt, closed, no retry

    def test_recording_clients_share_one_store_with_the_existing_file(self):
        class Inner:
            def __init__(self, tag):
                self.tag = tag

            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse(self.tag, "stop", {}, 0, "x")
        p = Path(tempfile.mkdtemp()) / "fx.json"
        p.write_text(json.dumps({"earlier": {"text": "old", "finish_reason": "stop", "usage": {}}}))
        store: dict = {}
        a = RecordingClient(Inner("a"), str(p), store=store)
        b = RecordingClient(Inner("b"), str(p), store=store)
        b.complete([{"role": "user", "content": "qb"}], temperature=0, seed=None, max_tokens=1)
        a.complete([{"role": "user", "content": "qa"}], temperature=0, seed=None, max_tokens=1)
        a.save()
        saved = json.loads(p.read_text())
        self.assertEqual(set(saved), {"earlier", messages_sha256([{"role": "user", "content": "qa"}]),
                                      messages_sha256([{"role": "user", "content": "qb"}])})
        self.assertIs(a.fixtures, store)

    def test_replay_serves_logprobs_when_stored(self):
        msgs = [{"role": "user", "content": "q"}]
        p = Path(tempfile.mkdtemp()) / "fx.json"
        p.write_text(json.dumps({messages_sha256(msgs): {"text": "t", "finish_reason": "stop", "usage": {},
                                                         "logprobs": {"content": [{"token": "t", "logprob": -0.1}]}}}))
        r = ReplayClient(str(p)).complete(msgs, temperature=0, seed=None, max_tokens=1)
        self.assertEqual(r.logprobs, {"content": [{"token": "t", "logprob": -0.1}]})


def lp_body(top, finish="length", text="A"):
    """A chat-completions body whose first token carries ``top`` = [(token, logprob), ...]."""
    return (200, {"choices": [{"message": {"content": text}, "finish_reason": finish,
                               "logprobs": {"content": [{"token": text, "logprob": top[0][1] if top else 0.0,
                                                         "top_logprobs": [{"token": t, "logprob": lp, "bytes": [1]}
                                                                          for t, lp in top]}]}}],
                  "usage": {"prompt_tokens": 9, "completion_tokens": 1}})


class TestLogprobs(unittest.TestCase):
    MSGS = [{"role": "user", "content": "x"}]

    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.seen, self.srv.script, self.srv.default = [], [], GOOD
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        cfg = ModelConfig(name="judge", endpoint=f"http://127.0.0.1:{self.srv.server_port}/v1", model_id="m-1",
                          timeout_s=5, params={"provider": {"order": ["x"], "allow_fallbacks": False}})
        self.client = HTTPClient(cfg, attempts=2, sleep=lambda s: None)
        self.tmp = tempfile.mkdtemp()

    def test_per_call_params_are_sent_and_merged_with_the_config_params(self):
        self.client.complete(self.MSGS, temperature=0, seed=None, max_tokens=1,
                             params={"logprobs": True, "top_logprobs": 10, "max_tokens": 999, "model": "evil"})
        body = self.srv.seen[0]["body"]
        self.assertEqual((body["logprobs"], body["top_logprobs"], body["max_tokens"], body["model"]),
                         (True, 10, 1, "m-1"))
        self.assertEqual(body["provider"], {"order": ["x"], "allow_fallbacks": False})

    def test_first_token_top_logprobs_become_pairs(self):
        self.srv.default = lp_body([("A", -0.1), (" B", -2.5), ("b", -3.0)])
        r = self.client.complete(self.MSGS, temperature=0, seed=None, max_tokens=1,
                                 params={"logprobs": True, "top_logprobs": 10})
        self.assertEqual(r.logprobs, [["A", -0.1], [" B", -2.5], ["b", -3.0]])
        self.assertEqual((r.text, r.finish_reason), ("A", "length"))

    def test_no_logprobs_in_the_reply_gives_none(self):
        r = self.client.complete(self.MSGS, temperature=0, seed=None, max_tokens=1,
                                 params={"logprobs": True, "top_logprobs": 10})
        self.assertIsNone(r.logprobs)            # GOOD has no logprobs field: ignored by the server
        self.srv.default = (200, {"choices": [{"message": {"content": "A"}, "finish_reason": "length",
                                               "logprobs": {"content": None}}]})
        self.assertIsNone(self.client.complete(self.MSGS, temperature=0, seed=None, max_tokens=1).logprobs)
        self.srv.default = lp_body([("A", float("-inf")), ("B", -1.0)])      # -inf is dropped (not loggable)
        self.assertEqual(self.client.complete(self.MSGS, temperature=0, seed=None, max_tokens=1).logprobs,
                         [["B", -1.0]])

    def test_log_call_accepts_length_only_when_allowed_and_records_logprobs_unredacted(self):
        log = AuditLog(Path(self.tmp) / "l.jsonl", "l", clock=lambda: "t")
        self.srv.default = lp_body([("A", -0.1), ("B", -2.5)])
        params = {"logprobs": True, "top_logprobs": 10}
        text, _ = log_call(log, self.client, list(self.MSGS), stage="s", item_id="i", temperature=0, seed=None,
                           max_tokens=1, max_attempts=1, params=params)
        self.assertIsNone(text)                                          # finish_reason=length is rejected by default
        text, recs = log_call(log, self.client, list(self.MSGS), stage="s", item_id="i2", temperature=0, seed=None,
                              max_tokens=1, max_attempts=1, params=params, allow_length=True)
        self.assertEqual(text, "A")
        req, resp = recs
        self.assertEqual(req["payload"]["params"]["logprobs"], True)
        self.assertEqual(req["payload"]["params"]["top_logprobs"], 10)
        self.assertEqual(req["payload"]["params"]["provider"], {"order": ["x"], "allow_fallbacks": False})
        self.assertEqual(resp["payload"]["logprobs"], [["A", -0.1], ["B", -2.5]])   # tokens survive the redactor
        self.assertEqual(find_orphan_requests(Path(self.tmp) / "l.jsonl"), [])
        self.assertTrue(verify_internal_chain(Path(self.tmp) / "l.jsonl")[0])

    def test_recording_then_replay_round_trip_serves_the_recorded_logprobs(self):
        log = AuditLog(Path(self.tmp) / "r.jsonl", "r", clock=lambda: "t")
        fx_path = str(Path(self.tmp) / "fx.json")
        rec = RecordingClient(self.client, fx_path, merge=False)       # records over HTTP, params and all
        self.srv.script = [lp_body([("A", -0.1), ("B", -2.5)]), (200, GOOD[1])]
        log_call(log, rec, [{"role": "user", "content": "q1"}], stage="s", item_id="1", temperature=0,
                 seed=None, max_tokens=1, max_attempts=1, params={"logprobs": True}, allow_length=True)
        log_call(log, rec, [{"role": "user", "content": "q2"}], stage="s", item_id="2", temperature=0,
                 seed=None, max_tokens=5, max_attempts=1)
        self.assertTrue(self.srv.seen[0]["body"]["logprobs"])             # the params reached the server
        rec.save()
        saved = json.loads(Path(fx_path).read_text())
        self.assertEqual(saved[messages_sha256([{"role": "user", "content": "q1"}])]["logprobs"],
                         [["A", -0.1], ["B", -2.5]])
        rc = ReplayClient(fx_path)
        r1 = rc.complete([{"role": "user", "content": "q1"}], temperature=0, seed=None, max_tokens=1,
                         params={"logprobs": True})
        self.assertEqual((r1.text, r1.logprobs, r1.finish_reason), ("A", [["A", -0.1], ["B", -2.5]], "length"))
        r2 = rc.complete([{"role": "user", "content": "q2"}], temperature=0, seed=None, max_tokens=5)
        self.assertEqual((r2.text, r2.logprobs), ("hello", None))
        # and the replayed reply goes through log_call exactly like the live one
        log2 = AuditLog(Path(self.tmp) / "r2.jsonl", "r2", clock=lambda: "t")
        text, recs = log_call(log2, rc, [{"role": "user", "content": "q1"}], stage="s", item_id="1", temperature=0,
                              seed=None, max_tokens=1, max_attempts=1, params={"logprobs": True}, allow_length=True)
        self.assertEqual((text, recs[1]["payload"]["logprobs"]), ("A", [["A", -0.1], ["B", -2.5]]))

    def test_mock_client_accepts_params_and_clients_without_them_still_work(self):
        log = AuditLog(Path(self.tmp) / "m.jsonl", "m", clock=lambda: "t")

        class Old:
            def complete(self, messages, *, temperature, seed, max_tokens):
                return ModelResponse("ok", "stop", {}, 0, "h")
        self.assertEqual(log_call(log, Old(), list(self.MSGS), stage="s", item_id="i", temperature=0, seed=None,
                                  max_tokens=5)[0], "ok")
        self.assertEqual(MockClient(fn=lambda m: "A").complete(self.MSGS, temperature=0, seed=None, max_tokens=1,
                                                               params={"logprobs": True}).text, "A")


if __name__ == "__main__":
    unittest.main()
