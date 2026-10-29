"""Task 5: the mock model server (tests/mock_models.py). Loopback only, no real model, every server stopped by addCleanup."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from second_scribe import ladder
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.llm import HTTPClient, ModelConfig, ThinkingDetected
from second_scribe.pipeline import PROMPTS_DIR, RunConfig, _Run

from tests.mock_models import DEFAULT_REMOTE_HOST, MockModels

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy


def call(base, path, body=None, *, headers=None, method=None):
    """-> (status, parsed JSON or raw text). Error statuses are returned, not raised."""
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    req = urllib.request.Request(base + path, data=data, method=method or ("GET" if data is None else "POST"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _OPENER.open(req, timeout=20) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
        exc.close()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        return status, raw.decode("utf-8", "replace")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-mock-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(Path(cls._tmp.name) / "out"))
        cls.oracle = build_clients("oracle", cls.cfg)["recon"]
        Path(cls.cfg.out_dir).mkdir(parents=True)
        log = AuditLog(Path(cls.cfg.out_dir) / "r.jsonl", "r")
        cls.addClassCleanup(log.close)
        cls.run_ = _Run(cls.cfg, {"recon": cls.oracle, "judge": cls.oracle}, log)

    def serve(self, **kw):
        srv = MockModels([DEMO_CONFIG], **kw)
        base = srv.start()
        self.addCleanup(srv.stop)
        return srv, base

    def messages(self, rung="rung2", pid="SYN-OW03", arm="removed"):
        p = next(x for x in self.run_.people if x.pid == pid)
        t = self.run_.rule_flagged(p)
        return ladder.rung_messages(PROMPTS_DIR, ladder.get_rung(rung), self.run_.ctx(p.role), t.text(arm))

    def chat(self, base, messages, **extra):
        return call(base, "/v1/chat/completions",
                    {"model": "qwen-mock:7b", "messages": messages, "temperature": 0.0, "max_tokens": 8000, **extra})


class TestChat(_Base):
    def test_chat_answers_from_oracle(self):
        srv, base = self.serve()
        msgs = self.messages()
        status, out = self.chat(base, msgs)
        self.assertEqual(status, 200)
        choice = out["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(out["model"], "qwen-mock:7b")
        self.assertIn("usage", out)
        self.assertEqual(choice["message"]["role"], "assistant")
        got = json.loads(choice["message"]["content"])
        want = json.loads(self.oracle.complete(msgs, temperature=0.0, seed=1, max_tokens=8000).text)
        self.assertEqual(got, want)
        self.assertGreater(len(got["nodes"]), 3)
        self.assertTrue(any(n["quote"] for n in got["nodes"]))
        self.assertNotIn("reasoning_content", choice["message"])

    def test_judge_prompt_answered_by_the_oracle(self):
        srv, base = self.serve()
        user = ('ITEMS:\n1. ELEMENT: Apply for a permit\n   QUOTE (pt): "I applied for the permit."\n'
                'Answer as {"labels": [{"n": 1, "modality": "..."}]}')
        status, out = self.chat(base, [{"role": "user", "content": user}])
        self.assertEqual(status, 200)
        labels = json.loads(out["choices"][0]["message"]["content"])["labels"]
        self.assertEqual([x["n"] for x in labels], [1])

    def test_canary_fail_judge_says_actual_for_every_element(self):
        srv, base = self.serve()
        items = "".join(f'{i}. ELEMENT: Concept {i}\n   QUOTE (pt): "nothing about it here {i}"\n' for i in (1, 2, 3))
        user = 'ITEMS:\n' + items.rstrip("\n") + '\nAnswer as {"labels": [{"n": 1, "modality": "..."}]}'
        msgs = [{"role": "user", "content": user}]
        honest = json.loads(self.chat(base, msgs)[1]["choices"][0]["message"]["content"])["labels"]
        self.assertTrue(all(x["modality"] == "unsupported" for x in honest), honest)
        srv.set(canary_fail=True)
        bad = json.loads(self.chat(base, msgs)[1]["choices"][0]["message"]["content"])["labels"]
        self.assertEqual([x["n"] for x in bad], [1, 2, 3])
        self.assertTrue(all(x["modality"] == "actual" for x in bad), bad)

    def test_unanswerable_prompt_is_a_http_error_not_a_hang(self):
        srv, base = self.serve()
        status, out = call(base, "/v1/chat/completions", {"model": "qwen-mock:7b", "messages": []})
        self.assertGreaterEqual(status, 400)

    def test_configs_are_tried_in_order(self):
        # the first config's oracle cannot answer a transcript it has never seen; with the same corpus twice the first answers
        srv = MockModels([DEMO_CONFIG, DEMO_CONFIG])
        base = srv.start()
        self.addCleanup(srv.stop)
        self.assertEqual(len(srv.oracles), 2)
        self.assertEqual(self.chat(base, self.messages())[0], 200)

    def test_chat_template_kwargs_reach_the_request_log(self):
        srv, base = self.serve()
        self.chat(base, self.messages(), chat_template_kwargs={"enable_thinking": False})
        self.assertEqual(srv.requests[-1]["body"]["chat_template_kwargs"], {"enable_thinking": False})


class TestFlags(_Base):
    def test_thinking_flag(self):
        srv, base = self.serve()
        msgs = self.messages()
        self.assertNotIn("reasoning_content", self.chat(base, msgs)[1]["choices"][0]["message"])
        srv.set(thinking=True)
        message = self.chat(base, msgs)[1]["choices"][0]["message"]
        self.assertTrue(message["reasoning_content"])
        self.assertTrue(json.loads(message["content"]))     # the answer is still there
        srv.set(thinking=False)
        self.assertNotIn("reasoning_content", self.chat(base, msgs)[1]["choices"][0]["message"])

    def test_ignore_thinking_flag(self):
        srv, base = self.serve(); srv.set(ignore_thinking_flag=True)
        msgs = self.messages()
        off = self.chat(base, msgs, chat_template_kwargs={"enable_thinking": False})[1]["choices"][0]["message"]
        self.assertTrue(off["reasoning_content"])           # asked not to think, thinks anyway
        none = self.chat(base, msgs)[1]["choices"][0]["message"]
        self.assertNotIn("reasoning_content", none)         # no switch sent: nothing added
        on = self.chat(base, msgs, chat_template_kwargs={"enable_thinking": True})[1]["choices"][0]["message"]
        self.assertNotIn("reasoning_content", on)

    def test_kit_client_detects_thinking_from_the_mock(self):
        srv, base = self.serve()
        cfg = ModelConfig("recon", base + "/v1", "qwen-mock:7b",
                          params={"chat_template_kwargs": {"enable_thinking": False}})
        client = HTTPClient(cfg, attempts=1)
        self.assertTrue(client.complete(self.messages(), temperature=0.0, seed=1, max_tokens=8000).text)
        srv.set(ignore_thinking_flag=True)
        with self.assertRaises(ThinkingDetected):
            client.complete(self.messages(), temperature=0.0, seed=1, max_tokens=8000)

    def test_malformed(self):
        srv, base = self.serve(); srv.set(malformed=True)
        status, out = self.chat(base, self.messages())
        self.assertEqual(status, 200)
        self.assertIsInstance(out, str)                      # not JSON
        with self.assertRaises(ValueError):
            json.loads(out)
        # the kit's client turns that into its "malformed response" error
        client = HTTPClient(ModelConfig("recon", base + "/v1", "qwen-mock:7b"), attempts=1)
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            client.complete(self.messages(), temperature=0.0, seed=1, max_tokens=8000)

    def test_slow_s(self):
        srv, base = self.serve(); srv.set(slow_s=0.4)
        t0 = time.monotonic()
        self.assertEqual(self.chat(base, self.messages())[0], 200)
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    def test_slow_request_does_not_block_stop(self):
        srv = MockModels([DEMO_CONFIG])
        base = srv.start()
        self.addCleanup(srv.stop)
        srv.set(slow_s=30)
        t = threading.Thread(target=lambda: call(base, "/v1/chat/completions",
                                                 {"model": "m", "messages": self.messages()}), daemon=True)
        t.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        srv.stop()
        self.assertLess(time.monotonic() - t0, 5)
        t.join(timeout=5)
        self.assertFalse(t.is_alive())

    def test_status_flag(self):
        srv, base = self.serve(); srv.set(status=503)
        for path, body in (("/v1/chat/completions", {"model": "m", "messages": self.messages()}),
                           ("/v1/messages", {"model": "m", "max_tokens": 5,
                                             "messages": [{"role": "user", "content": "hi"}]}),
                           ("/v1/models", None), ("/api/tags", None)):
            with self.subTest(path=path):
                self.assertEqual(call(base, path, body)[0], 503)
        srv.set(status=429)
        self.assertEqual(self.chat(base, self.messages())[0], 429)
        srv.set(status=None)
        self.assertEqual(self.chat(base, self.messages())[0], 200)

    def test_unknown_flag_is_refused(self):
        srv, _ = self.serve()
        with self.assertRaises(TypeError):
            srv.set(thinkng=True)


class TestDiscovery(_Base):
    def test_v1_models(self):
        srv, base = self.serve()
        status, out = call(base, "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(out["object"], "list")
        self.assertEqual([m["id"] for m in out["data"]], ["qwen-mock:7b", "gemma-mock:9b"])

    def test_tags_and_show_shapes(self):
        srv, base = self.serve()
        status, tags = call(base, "/api/tags")
        self.assertEqual(status, 200)
        by_name = {m["name"]: m for m in tags["models"]}
        self.assertEqual(set(by_name), {"qwen-mock:7b", "gemma-mock:9b"})
        q = by_name["qwen-mock:7b"]
        self.assertEqual(q["model"], "qwen-mock:7b")
        self.assertEqual(q["details"]["family"], "qwen2")
        self.assertEqual(q["details"]["families"], ["qwen2"])
        self.assertRegex(q["digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("remote_host", q)
        self.assertEqual(by_name["gemma-mock:9b"]["details"]["family"], "gemma2")
        self.assertNotEqual(q["digest"], by_name["gemma-mock:9b"]["digest"])
        status, show = call(base, "/api/show", {"model": "gemma-mock:9b"})
        self.assertEqual(status, 200)
        self.assertEqual(show["details"]["family"], "gemma2")
        self.assertEqual(show["details"]["families"], ["gemma2"])
        self.assertEqual(show["model_info"]["general.architecture"], "gemma2")
        self.assertNotIn("remote_host", show)
        # older clients send "name" instead of "model"
        self.assertEqual(call(base, "/api/show", {"name": "qwen-mock:7b"})[1]["details"]["family"], "qwen2")
        self.assertEqual(call(base, "/api/show", {"model": "nope"})[0], 404)

    def test_cloud_flag_sets_remote_host(self):
        models = {"qwen-mock:7b": {"family": "qwen2", "digest": "a" * 64, "remote_host": "https://ollama.example:443"},
                  "local-mock:1b": {"family": "llama", "digest": "b" * 64}}
        srv, base = self.serve(models=models)
        self.assertNotIn("remote_host", call(base, "/api/show", {"model": "qwen-mock:7b"})[1])
        self.assertTrue(all("remote_host" not in m for m in call(base, "/api/tags")[1]["models"]))
        srv.set(cloud=True)
        self.assertEqual(call(base, "/api/show", {"model": "qwen-mock:7b"})[1]["remote_host"],
                         "https://ollama.example:443")
        by_name = {m["name"]: m for m in call(base, "/api/tags")[1]["models"]}
        self.assertEqual(by_name["qwen-mock:7b"]["remote_host"], "https://ollama.example:443")
        self.assertEqual(by_name["qwen-mock:7b"]["digest"], "a" * 64)
        self.assertEqual(by_name["local-mock:1b"]["remote_host"], DEFAULT_REMOTE_HOST)   # no host of its own: default

    def test_cloud_flag_default_models_get_a_host(self):
        srv, base = self.serve(); srv.set(cloud=True)
        self.assertTrue(all(m.get("remote_host") for m in call(base, "/api/tags")[1]["models"]))

    def test_lm_studio_v1_and_v0(self):
        srv, base = self.serve()
        status, v1 = call(base, "/api/v1/models")
        self.assertEqual(status, 200)
        by_key = {m["key"]: m for m in v1["models"]}
        self.assertEqual(by_key["qwen-mock:7b"]["architecture"], "qwen2")
        self.assertEqual(by_key["qwen-mock:7b"]["type"], "llm")
        status, v0 = call(base, "/api/v0/models")
        self.assertEqual(status, 200)
        by_id = {m["id"]: m for m in v0["data"]}
        self.assertEqual(by_id["gemma-mock:9b"]["arch"], "gemma2")
        self.assertEqual(by_id["gemma-mock:9b"]["type"], "llm")

    def test_unknown_path_is_404(self):
        srv, base = self.serve()
        self.assertEqual(call(base, "/nope")[0], 404)
        self.assertEqual(call(base, "/nope", {"a": 1})[0], 404)


class TestGenerate(_Base):
    def test_load_and_unload(self):
        srv, base = self.serve()
        status, out = call(base, "/api/generate", {"model": "qwen-mock:7b", "keep_alive": "5m"})
        self.assertEqual((status, out["done"], out["done_reason"]), (200, True, "load"))
        self.assertEqual(srv.loaded, {"qwen-mock:7b"})
        # unload is not honoured until unload_ok
        out = call(base, "/api/generate", {"model": "qwen-mock:7b", "keep_alive": 0})[1]
        self.assertEqual(out["done_reason"], "load")
        self.assertEqual(srv.loaded, {"qwen-mock:7b"})
        srv.set(unload_ok=True)
        out = call(base, "/api/generate", {"model": "qwen-mock:7b", "keep_alive": 0})[1]
        self.assertEqual((out["done"], out["done_reason"]), (True, "unload"))
        self.assertEqual(srv.loaded, set())

    def test_unknown_model_is_404(self):
        srv, base = self.serve()
        self.assertEqual(call(base, "/api/generate", {"model": "nope", "keep_alive": 0})[0], 404)


class TestMessages(_Base):
    def body(self, **extra):
        msgs = self.messages()
        system = next((m["content"] for m in msgs if m["role"] == "system"), None)
        user = next(m["content"] for m in msgs if m["role"] == "user")
        body = {"model": "claude-mock", "max_tokens": 4000, "messages": [{"role": "user", "content": user}]}
        if system:
            body["system"] = system
        return {**body, **extra}

    def test_messages_endpoint_shape(self):
        srv, base = self.serve()
        status, out = call(base, "/v1/messages", self.body(), headers={"x-api-key": "sk-secret-123",
                                                                       "anthropic-version": "2023-06-01"})
        self.assertEqual(status, 200)
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["model"], "claude-mock")
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertEqual([b["type"] for b in out["content"]], ["text"])
        self.assertTrue(json.loads(out["content"][0]["text"])["nodes"])
        self.assertEqual(set(out["usage"]), {"input_tokens", "output_tokens"})

    def test_messages_accepts_block_content(self):
        srv, base = self.serve()
        b = self.body()
        b["messages"] = [{"role": "user", "content": [{"type": "text", "text": b["messages"][0]["content"]}]}]
        status, out = call(base, "/v1/messages", b)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(out["content"][0]["text"])["nodes"])

    def test_messages_thinking_block(self):
        srv, base = self.serve(); srv.set(thinking=True)
        out = call(base, "/v1/messages", self.body())[1]
        self.assertEqual([b["type"] for b in out["content"]], ["thinking", "text"])
        self.assertTrue(out["content"][0]["thinking"])

    def test_messages_malformed_and_status(self):
        srv, base = self.serve(); srv.set(malformed=True)
        status, out = call(base, "/v1/messages", self.body())
        self.assertIsInstance(out, str)
        srv.set(malformed=False, status=500)
        self.assertEqual(call(base, "/v1/messages", self.body())[0], 500)


class TestRequestLog(_Base):
    def test_requests_record_path_model_and_drop_secrets(self):
        srv, base = self.serve()
        self.chat(base, self.messages(), )
        call(base, "/v1/messages", {"model": "claude-mock", "max_tokens": 5,
                                    "messages": [{"role": "user", "content": "hi"}]},
             headers={"x-api-key": "sk-secret-123", "Authorization": "Bearer sk-secret-456"})
        call(base, "/api/tags")
        self.assertEqual([r["path"] for r in srv.requests], ["/v1/chat/completions", "/v1/messages", "/api/tags"])
        self.assertEqual(srv.requests[0]["model"], "qwen-mock:7b")
        self.assertEqual(srv.requests[1]["model"], "claude-mock")
        self.assertIsNone(srv.requests[2]["model"])
        self.assertEqual(srv.requests[0]["method"], "POST")
        self.assertEqual(srv.requests[2]["method"], "GET")
        for r in srv.requests:
            self.assertFalse({k.lower() for k in r["headers"]} & {"authorization", "x-api-key"})
        self.assertNotIn("sk-secret", json.dumps(srv.requests))
        self.assertEqual(srv.requests[1]["headers"].get("Content-Type"), "application/json")


class TestLifecycle(_Base):
    def test_binds_loopback_only(self):
        srv, base = self.serve()
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        self.assertEqual(int(base.rsplit(":", 1)[1]), srv.server_address[1])
        self.assertNotEqual(srv.server_address[1], 0)

    def test_stop_releases_thread_and_socket(self):
        before = {t.ident for t in threading.enumerate()}
        srv = MockModels([DEMO_CONFIG])
        base = srv.start()
        self.assertEqual(self.chat(base, self.messages())[0], 200)
        srv.stop()
        srv.stop()                                          # idempotent
        with self.assertRaises(urllib.error.URLError):
            _OPENER.open(base + "/v1/models", timeout=3)
        self.assertEqual({t.ident for t in threading.enumerate()} - before, set())

    def test_context_manager(self):
        with MockModels([DEMO_CONFIG]) as srv:
            base = srv.base_url
            self.assertEqual(call(base, "/v1/models")[0], 200)
        with self.assertRaises(urllib.error.URLError):
            _OPENER.open(base + "/v1/models", timeout=3)

    def test_two_servers_get_two_ports(self):
        a, base_a = self.serve()
        b, base_b = self.serve()
        self.assertNotEqual(base_a, base_b)


if __name__ == "__main__":
    unittest.main()
