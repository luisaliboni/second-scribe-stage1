"""K12 / K13 (app plan Task 18): the native Anthropic client and the remote endpoint allow-list.

- ``AnthropicClient`` speaks the Messages API (``POST {endpoint}/messages``) with the same ``complete`` contract as
  ``HTTPClient``; the kit's system message is hoisted into ``system``; a thinking block stops the run.
- ``ModelConfig.provider`` comes from ``SS_<P>_PROVIDER``; ``cli.build_clients("http", ...)`` builds the client by it.
- ``guard_real_clients(..., remote_endpoints=...)``: a non-local endpoint passes only when ``{role, host, provider}``
  matches an entry of ``tool.remote_endpoints``; the thinking-off check applies to local endpoints only. A study config
  (no ``tool`` block) keeps today's guard: every non-local endpoint is refused.
- The determinism audit on a remote role is reported (``counts.remote``), not a fault.
- No key ever reaches a file, and no request header is logged.

Every server here is ``tests/mock_models.py`` on 127.0.0.1. A remote endpoint is the made-up name ``FAKE_HOST``: the
guard's resolver is patched to a public address, and the socket layer sends that name to 127.0.0.1. Nothing leaves
this machine; every key is generated at run time.
"""
import contextlib
import io
import json
import os
import secrets
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.addr import ip
from tests.mock_models import MockModels
from second_scribe import audits, llm
from second_scribe.anthropic_client import ANTHROPIC_VERSION, AnthropicClient
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.cli import main as cli_main
from second_scribe.llm import (EgressRefused, HTTPClient, ModelConfig, RealGuardRefused, ThinkingDetected,
                               TransportFailure, guard_real_clients, make_client)
from second_scribe.pipeline import RunConfig, run

FAKE_HOST = "remote-llm.mock.invalid"
OFF = {"chat_template_kwargs": {"enable_thinking": False}}
_REAL_GETADDRINFO = socket.getaddrinfo


def public_resolver(host, port):
    return [ip(104, 18, 0, 1)]


def fake_key() -> str:
    return "sk" + "-test-" + secrets.token_hex(12)      # 24 random hex chars, built at run time


def key_var() -> str:
    return "SS_T18_FAKE_KEY_" + secrets.token_hex(4).upper()


@contextlib.contextmanager
def routed():
    """FAKE_HOST is public to the guard and 127.0.0.1 to the socket layer."""
    def gai(host, *a, **kw):
        return _REAL_GETADDRINFO("127.0.0.1" if host == FAKE_HOST else host, *a, **kw)
    with mock.patch("second_scribe.llm._resolve", side_effect=public_resolver), \
            mock.patch("socket.getaddrinfo", side_effect=gai):
        yield


def real_config(out: Path, tool=None) -> Path:
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = "real"
    d.pop("prereg_required", None)
    m = out / "participant_id_map.json"
    m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
    d["corpus"]["participant_id_map"] = str(m)
    if tool is not None:
        d["tool"] = tool
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


def entry(role, host, provider):
    return {"role": role, "host": host, "provider": provider, "declaration_sha256": "ab" * 32}


def _open_log(cfg):
    (Path(cfg.out_dir) / "logs").mkdir(parents=True, exist_ok=True)
    return AuditLog(Path(cfg.out_dir) / "logs" / "run.jsonl", cfg.run_id)


class _Base(unittest.TestCase):
    def serve(self):
        srv = MockModels([DEMO_CONFIG])
        srv.start()
        self.addCleanup(srv.stop)
        return srv, f"http://{FAKE_HOST}:{srv.server_address[1]}/v1"

    def kit_messages(self):
        """A kit prompt the oracle answers: rung 2 for one shipped synthetic person (system + user)."""
        from second_scribe import ladder
        from second_scribe.pipeline import PROMPTS_DIR, _Run
        tmp = Path(tempfile.mkdtemp())
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(tmp / "out"))
        oracle = build_clients("oracle", cfg)["recon"]
        Path(cfg.out_dir).mkdir(parents=True)
        log = AuditLog(Path(cfg.out_dir) / "r.jsonl", "r")
        self.addCleanup(log.close)
        r = _Run(cfg, {"recon": oracle, "judge": oracle}, log)
        p = next(x for x in r.people if x.pid == "SYN-OW03")
        msgs = ladder.rung_messages(PROMPTS_DIR, ladder.get_rung("rung2"), r.ctx(p.role), r.rule_flagged(p).text("removed"))
        return msgs, oracle

    def anthropic(self, url, *, role="judge", model="claude-mock-1", key_env=None, **kw):
        return AnthropicClient(ModelConfig(name=role, endpoint=url, model_id=model, api_key_env=key_env,
                                           provider="anthropic"), **kw)


class TestAnthropicClient(_Base):
    def test_messages_body_system_hoisted(self):
        srv, url = self.serve()
        msgs, oracle = self.kit_messages()
        self.assertEqual(msgs[0]["role"], "system")
        var, key = key_var(), fake_key()
        sent = []
        real_open = llm._OPENER.open

        def spy(req, *a, **kw):
            sent.append(dict(req.header_items()))
            return real_open(req, *a, **kw)
        with routed(), mock.patch.dict(os.environ, {var: key}), mock.patch.object(llm._OPENER, "open", side_effect=spy):
            resp = self.anthropic(url, key_env=var).complete(json.loads(json.dumps(msgs)), temperature=0.0, seed=7,
                                                              max_tokens=8000)
        self.assertEqual(len(srv.requests), 1)
        req = srv.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", "/v1/messages"))
        body = req["body"]
        self.assertEqual(set(body), {"model", "max_tokens", "temperature", "system", "messages"})
        self.assertEqual(body["system"], msgs[0]["content"])
        self.assertEqual(body["messages"], msgs[1:])
        self.assertEqual((body["model"], body["max_tokens"], body["temperature"]), ("claude-mock-1", 8000, 0.0))
        heads = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(heads["anthropic-version"], ANTHROPIC_VERSION)
        self.assertEqual(ANTHROPIC_VERSION, "2023-06-01")
        self.assertEqual(heads["content-type"], "application/json")
        sent_heads = {k.lower(): v for k, v in sent[0].items()}
        self.assertTrue(sent_heads.get("x-api-key") == key, "the key goes in x-api-key")
        self.assertNotIn("authorization", sent_heads)
        expected = oracle.complete(msgs, temperature=0.0, seed=None, max_tokens=8000).text
        self.assertEqual(resp.text, expected)
        self.assertEqual(resp.finish_reason, "stop")
        self.assertEqual(len(resp.raw_sha256), 64)
        self.assertIsInstance(resp.usage["output_tokens"], int)
        self.assertEqual(resp.usage["completion_tokens"], resp.usage["output_tokens"])

    def test_no_system_message_no_system_field_and_text_blocks_joined(self):
        reply = {"type": "message", "content": [{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}],
                 "stop_reason": "max_tokens", "usage": {"input_tokens": 3, "output_tokens": 2}}
        c = self.anthropic("https://api.anthropic.com/v1")
        with mock.patch.object(llm._OPENER, "open", return_value=_Reply(reply)) as op:
            r = c.complete([{"role": "user", "content": "hi"}], temperature=0.0, seed=None, max_tokens=5)
        body = json.loads(op.call_args.args[0].data)
        self.assertNotIn("system", body)
        self.assertEqual(op.call_args.args[0].full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual((r.text, r.finish_reason), ("abcd", "length"))      # max_tokens -> the kit's truncation

    def test_thinking_block_raises(self):
        srv, url = self.serve()
        srv.set(thinking=True)
        msgs, _ = self.kit_messages()
        with routed(), self.assertRaises(ThinkingDetected) as cm:
            self.anthropic(url).complete(msgs, temperature=0.0, seed=None, max_tokens=8000)
        self.assertTrue(cm.exception.fatal)
        self.assertNotIn("let me think", str(cm.exception))
        red = {"type": "message", "content": [{"type": "redacted_thinking", "data": "xx"}, {"type": "text", "text": "y"}],
               "stop_reason": "end_turn", "usage": {}}
        with mock.patch.object(llm._OPENER, "open", return_value=_Reply(red)), self.assertRaises(ThinkingDetected):
            self.anthropic("https://api.anthropic.com/v1").complete([{"role": "user", "content": "q"}],
                                                                    temperature=0.0, seed=None, max_tokens=5)

    def test_errors_map_to_retry_classes(self):
        srv, url = self.serve()
        srv.set(status=529)
        sleeps = []
        with routed(), self.assertRaises(TransportFailure):
            self.anthropic(url, attempts=3, sleep=sleeps.append).complete(
                [{"role": "user", "content": "q"}], temperature=0.0, seed=None, max_tokens=5)
        self.assertEqual(len(srv.requests), 3)
        self.assertEqual(len(sleeps), 2)
        srv.set(status=400)
        n = len(srv.requests)
        with routed(), self.assertRaises(RuntimeError) as cm:
            self.anthropic(url, attempts=3, sleep=sleeps.append).complete(
                [{"role": "user", "content": "q"}], temperature=0.0, seed=None, max_tokens=5)
        self.assertNotIsInstance(cm.exception, TransportFailure)
        self.assertEqual(len(srv.requests), n + 1)              # a 4xx is not retried
        srv.set(status=None, malformed=True)
        with routed(), self.assertRaises(RuntimeError) as cm:
            self.anthropic(url).complete(*self.kit_messages()[:1], temperature=0.0, seed=None, max_tokens=8000)
        self.assertIn("malformed", str(cm.exception))

    def test_params_are_refused_not_silently_dropped(self):
        with self.assertRaises(ValueError):
            AnthropicClient(ModelConfig(name="judge", endpoint="https://api.anthropic.com/v1", model_id="c",
                                        provider="anthropic", params=dict(OFF)))
        c = self.anthropic("https://api.anthropic.com/v1")
        with mock.patch.object(llm._OPENER, "open") as op, self.assertRaises(ValueError) as cm:
            c.complete([{"role": "user", "content": "q"}], temperature=0.0, seed=None, max_tokens=1,
                       params={"logprobs": True})
        self.assertTrue(getattr(cm.exception, "fatal", False))
        self.assertEqual(op.call_count, 0)


class _Reply:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


class TestProviderFactory(unittest.TestCase):
    def test_provider_from_env_and_factory(self):
        cfg = RunConfig.load(DEMO_CONFIG)
        base = {"SS_RECON_URL": "https://api.anthropic.com/v1", "SS_RECON_MODEL": "claude-mock-1",
                "SS_JUDGE_URL": "http://127.0.0.1:9/v1", "SS_JUDGE_MODEL": "gemma-mock:9b"}
        with mock.patch.dict(os.environ, {**base, "SS_RECON_PROVIDER": "anthropic"}):
            self.assertEqual(ModelConfig.from_env("recon").provider, "anthropic")
            self.assertEqual(ModelConfig.from_env("judge").provider, "openai")
            clients = build_clients("http", cfg)
        self.assertIsInstance(clients["recon"], AnthropicClient)
        self.assertIs(type(clients["judge"]), HTTPClient)
        with mock.patch.dict(os.environ, {**base, "SS_RECON_PROVIDER": "openai"}):
            self.assertIs(type(build_clients("http", cfg)["recon"]), HTTPClient)
        with mock.patch.dict(os.environ, {**base, "SS_RECON_PROVIDER": "gemini"}), self.assertRaises(ValueError):
            ModelConfig.from_env("recon")
        self.assertEqual(ModelConfig(name="r", endpoint="http://127.0.0.1/v1", model_id="m").provider, "openai")
        self.assertIsInstance(make_client(ModelConfig(name="r", endpoint="https://x.invalid/v1", model_id="m",
                                                      provider="anthropic")), AnthropicClient)


def _remote(kind, role, url, model):
    if kind == "anthropic":
        return AnthropicClient(ModelConfig(name=role, endpoint=url, model_id=model, provider="anthropic"))
    return HTTPClient(ModelConfig(name=role, endpoint=url, model_id=model))   # no thinking params: remote


PROVIDER_OF = {"anthropic": "anthropic", "openai": "openai_compat"}
LOCAL = {"recon": ("qwen3:8b", ip(10, 0, 0, 1)), "judge": ("gemma3:4b", ip(10, 0, 0, 2)),
         "judge2": ("llama3.1:8b", ip(10, 0, 0, 3))}
REMOTE_MODEL = {"recon": "remote-recon-1", "judge": "claude-mock-1", "judge2": "remote-judge2-1"}


def _local(role):
    m, h = LOCAL[role]
    return HTTPClient(ModelConfig(name=role, endpoint=f"http://{h}:8000/v1", model_id=m, params=dict(OFF)))


class TestGuard(unittest.TestCase):
    URL = f"https://{FAKE_HOST}/v1"

    def clients_with(self, role, kind, url=None):
        out = {r: _local(r) for r in LOCAL}
        out[role] = _remote(kind, role, url or self.URL, REMOTE_MODEL[role])
        return out

    def test_remote_refused_without_allowlist(self):
        for kind in ("anthropic", "openai"):
            for role in LOCAL:
                for eps in (None, []):
                    with routed(), self.assertRaises(EgressRefused, msg=(kind, role, eps)) as cm:
                        guard_real_clients(self.clients_with(role, kind), remote_endpoints=eps)
                    self.assertIn(f"the {role} endpoint is refused", str(cm.exception))
        # through the pipeline: a tool config without remote_endpoints refuses before any request
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp()), {"reference_type": "A"}))
        log = _open_log(cfg)
        try:
            with routed(), mock.patch.object(llm._OPENER, "open", side_effect=AssertionError("sent")) as op, \
                    self.assertRaises(EgressRefused):
                run(cfg, ["Q", "J"], self.clients_with("judge", "anthropic"), log=log)
        finally:
            log.close()
        self.assertEqual(op.call_count, 0)

    def test_remote_allowed_with_matching_entry_only(self):
        for kind in ("anthropic", "openai"):
            for role in LOCAL:
                good = entry(role, FAKE_HOST, PROVIDER_OF[kind])
                with routed():
                    guard_real_clients(self.clients_with(role, kind), remote_endpoints=[good])
                    other_role = "recon" if role != "recon" else "judge"
                    other_kind = "openai" if kind == "anthropic" else "anthropic"
                    bad = (entry(other_role, FAKE_HOST, PROVIDER_OF[kind]),            # wrong role
                           entry(role, "other-llm.mock.invalid", PROVIDER_OF[kind]),   # wrong host
                           entry(role, FAKE_HOST + ":8443", PROVIDER_OF[kind]),        # wrong port
                           entry(role, FAKE_HOST, PROVIDER_OF[other_kind]))            # wrong provider
                    for b in bad:
                        with self.assertRaises(EgressRefused, msg=(kind, role, b)):
                            guard_real_clients(self.clients_with(role, kind), remote_endpoints=[b])
                    # an entry for one role does not open another remote role
                    two = self.clients_with(role, kind)
                    two[other_role] = _remote(kind, other_role, self.URL, REMOTE_MODEL[other_role])
                    with self.assertRaises(EgressRefused):
                        guard_real_clients(two, remote_endpoints=[good])
        # the host is matched case-insensitively and with its port
        with routed():
            guard_real_clients(self.clients_with("judge", "anthropic", f"https://{FAKE_HOST.upper()}:8443/v1"),
                               remote_endpoints=[entry("judge", FAKE_HOST + ":8443", "anthropic")])

    def test_remote_http_refused_even_when_allowlisted(self):
        """A non-local endpoint must use https, allow-list entry or not; no config or env opens plain http. Only the
        module constant ``llm.REMOTE_SCHEMES`` (patched by the tests that talk to the plain-http mock) changes it."""
        self.assertEqual(llm.REMOTE_SCHEMES, ("https",))
        for kind in ("anthropic", "openai"):
            for role in LOCAL:
                for url, host in ((f"http://{FAKE_HOST}/v1", FAKE_HOST), (f"http://{FAKE_HOST}:443/v1", FAKE_HOST + ":443")):
                    clients = self.clients_with(role, kind, url)
                    with routed(), mock.patch.dict(os.environ, {"SS_REMOTE_SCHEMES": "http,https",
                                                                "SS_ALLOW_HTTP": "1"}), \
                            mock.patch.object(llm._OPENER, "open", side_effect=AssertionError("sent")) as op, \
                            self.assertRaises(EgressRefused, msg=(kind, role, url)) as cm:
                        guard_real_clients(clients, remote_endpoints=[entry(role, host, PROVIDER_OF[kind])])
                    self.assertEqual(str(cm.exception),
                                     f"real corpus: the {role} endpoint is refused: remote endpoint must use https")
                    self.assertEqual(op.call_count, 0)
                    with routed():          # the same entry over https passes
                        guard_real_clients(self.clients_with(role, kind, url.replace("http://", "https://")),
                                           remote_endpoints=[entry(role, host, PROVIDER_OF[kind])])
        # a tool config that lists the endpoint cannot open http either, through the pipeline
        eps = [entry("judge", FAKE_HOST, "anthropic")]
        optins = [{"role": r, "host": f"{h}:8000"} for r, (_, h) in LOCAL.items()]     # audit K7: the LAN locals
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp()), {"remote_endpoints": eps,
                                                                    "endpoint_optins": optins}))
        log = _open_log(cfg)
        try:
            with routed(), mock.patch.object(llm._OPENER, "open", side_effect=AssertionError("sent")) as op, \
                    self.assertRaises(EgressRefused) as cm:
                run(cfg, ["Q", "J"], self.clients_with("judge", "anthropic", f"http://{FAKE_HOST}/v1"), log=log)
        finally:
            log.close()
        self.assertIn("remote endpoint must use https", str(cm.exception))
        self.assertEqual(op.call_count, 0)
        # local endpoints keep plain http
        guard_real_clients({r: _local(r) for r in LOCAL}, resolver=public_resolver)

    def test_judge_rule_still_applies_to_remote_judges(self):
        clients = self.clients_with("judge", "openai")
        clients["judge"] = _remote("openai", "judge", self.URL, "qwen3-max")
        with routed(), self.assertRaises(RealGuardRefused) as cm:
            guard_real_clients(clients, remote_endpoints=[entry("judge", FAKE_HOST, "openai_compat")])
        self.assertIn("Qwen", str(cm.exception))

    def test_thinking_check_skipped_for_remote_only(self):
        with routed():      # the remote client has no thinking params and passes
            guard_real_clients(self.clients_with("judge", "openai"),
                               remote_endpoints=[entry("judge", FAKE_HOST, "openai_compat")])
        # a local endpoint without thinking off is refused, allow-list entry or not
        local = {r: _local(r) for r in LOCAL}
        local["judge"] = HTTPClient(ModelConfig(name="judge", endpoint=f"http://{ip(10, 0, 0, 2)}:8000/v1",
                                                model_id="gemma3:4b"))
        for eps in (None, [entry("judge", f"{ip(10, 0, 0, 2)}:8000", "openai_compat")]):
            with self.assertRaises(RealGuardRefused) as cm:
                guard_real_clients(local, resolver=public_resolver, remote_endpoints=eps)
            self.assertNotIsInstance(cm.exception, EgressRefused)
            self.assertIn("thinking off", str(cm.exception))

    def test_study_config_refuses_every_remote_endpoint(self):
        """Analysis session, 18 Oct (binding): a study config (no ``tool`` block) on a real corpus, given a
        ``remote_endpoints`` list that matches the endpoint exactly, still refuses every non-local endpoint, on each
        role, for the Anthropic and the OpenAI-compatible client, before any request."""
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp())))
        self.assertEqual(cfg.tool, {})
        n = 0
        for role in LOCAL:
            for kind in ("anthropic", "openai"):
                matching = [entry(role, FAKE_HOST, PROVIDER_OF[kind])]
                with routed():          # the list does match: the guard passes when it is handed the list
                    guard_real_clients(self.clients_with(role, kind), remote_endpoints=matching)
                # the study config cannot hand it over: the list is on the config object and in the env too
                cfg.remote_endpoints = matching
                cfg.models = {**cfg.models, "remote_endpoints": matching}
                log = _open_log(cfg)
                try:
                    with routed(), mock.patch.dict(os.environ, {"SS_REMOTE_ENDPOINTS": json.dumps(matching)}), \
                            mock.patch.object(llm._OPENER, "open", side_effect=AssertionError("sent")) as op, \
                            mock.patch("second_scribe.pipeline.guard_real_clients",
                                       side_effect=guard_real_clients) as g, \
                            self.assertRaises(EgressRefused, msg=(role, kind)) as cm:
                        run(cfg, ["Q", "J"], self.clients_with(role, kind), log=log)
                finally:
                    log.close()
                self.assertIsNone(g.call_args.kwargs["remote_endpoints"])
                self.assertIn(f"the {role} endpoint is refused", str(cm.exception))
                self.assertEqual(op.call_count, 0)
                n += 1
        self.assertEqual(n, 6)
        self.assertFalse((Path(cfg.out_dir) / "work").exists())

    def test_tool_config_passes_its_list_to_the_guard(self):
        eps = [entry("judge", FAKE_HOST, "anthropic")]
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp()), {"remote_endpoints": eps}))
        log = _open_log(cfg)
        try:
            with mock.patch("second_scribe.pipeline.guard_real_clients",
                            side_effect=RealGuardRefused("stop here")) as g, self.assertRaises(RealGuardRefused):
                run(cfg, [], {}, log=log)
        finally:
            log.close()
        self.assertEqual(g.call_args.kwargs["remote_endpoints"], eps)



class TestLocalOptins(unittest.TestCase):
    """Audit K7 / S3 (spec 2.1): for a tool config (``endpoint_optins`` passed, ``[]`` when it has none) a LAN or
    Tailscale endpoint needs a ``{role, host}`` opt-in; loopback needs none; a study call (no ``endpoint_optins``)
    keeps today's guard. Addresses are built at run time; nothing is contacted."""
    LAN = (ip(10, 1, 2, 3), ip(172, 16, 5, 6), ip(192, 168, 7, 8))
    TAILSCALE = (ip(100, 64, 9, 10), ip(100, 127, 1, 2))

    def client(self, role, host, port=8000):
        return HTTPClient(ModelConfig(name=role, endpoint=f"http://{host}:{port}/v1", model_id=f"gemma-{role}:4b",
                                      params=dict(OFF)))

    def test_lan_and_tailscale_need_an_optin(self):
        for host in self.LAN + self.TAILSCALE:
            for role in ("recon", "judge", "judge2"):
                c = {role: self.client(role, host)}
                with self.assertRaises(EgressRefused, msg=(host, role)) as cm:
                    guard_real_clients(c, endpoint_optins=[])
                self.assertIn("no opt-in", str(cm.exception))
                self.assertIn(f"{host}:8000", str(cm.exception))
                # the opt-in for the same role and host lets it through
                guard_real_clients(c, endpoint_optins=[{"role": role, "host": f"{host}:8000"}])
                # an opt-in for another role or another host does not
                other = "judge" if role != "judge" else "recon"
                for bad in ({"role": other, "host": f"{host}:8000"}, {"role": role, "host": f"{host}:8001"},
                            {"role": role, "host": host}):
                    with self.assertRaises(EgressRefused, msg=(host, role, bad)):
                        guard_real_clients(c, endpoint_optins=[bad])

    def test_host_case_ignored(self):
        c = {"recon": HTTPClient(ModelConfig(name="recon", endpoint="http://GPU-Box.Lan:8000/v1", model_id="m",
                                             params=dict(OFF)))}
        lan = lambda host, port: [self.LAN[0]]
        with self.assertRaises(EgressRefused):
            guard_real_clients(c, resolver=lan, endpoint_optins=[])
        guard_real_clients(c, resolver=lan, endpoint_optins=[{"role": "recon", "host": "GPU-BOX.lan:8000"}])

    def test_name_resolving_to_lan_needs_optin(self):
        mixed = lambda host, port: ["127.0.0.1", self.TAILSCALE[0]]       # one non-loopback address is enough
        c = {"recon": self.client("recon", "box.example.invalid")}
        with self.assertRaises(EgressRefused):
            guard_real_clients(c, resolver=mixed, endpoint_optins=[])
        guard_real_clients(c, resolver=lambda h, p: ["127.0.0.1", "::1"], endpoint_optins=[])

    def test_loopback_needs_no_optin(self):
        for host in ("127.0.0.1", ip(127, 0, 0, 2), "[::1]"):
            guard_real_clients({"recon": self.client("recon", host)}, endpoint_optins=[])

    def test_study_call_unchanged(self):
        """No ``endpoint_optins`` (every study config): a LAN or Tailscale endpoint passes as it always has."""
        for host in self.LAN + self.TAILSCALE:
            guard_real_clients({"recon": self.client("recon", host)})

    def test_pipeline_passes_optins_for_tool_configs_only(self):
        def kwargs(tool):
            cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp(prefix="ss-tests-k7-")), tool))
            log = _open_log(cfg)
            try:
                with mock.patch("second_scribe.pipeline.guard_real_clients",
                                side_effect=RealGuardRefused("stop here")) as g, self.assertRaises(RealGuardRefused):
                    run(cfg, [], {}, log=log)
            finally:
                log.close()
            return g.call_args.kwargs
        self.assertNotIn("endpoint_optins", kwargs(None))                                  # study: call unchanged
        self.assertEqual(kwargs({"reference_type": "A"})["endpoint_optins"], [])            # tool, no opt-ins
        ops = [{"role": "recon", "host": f"{self.LAN[0]}:8000"}]
        self.assertEqual(kwargs({"reference_type": "A", "endpoint_optins": ops})["endpoint_optins"], ops)

    def test_tool_run_on_lan_without_optin_refused_before_any_request(self):
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp(prefix="ss-tests-k7-")), {"reference_type": "A"}))
        clients = {"recon": self.client("recon", self.LAN[1]), "judge": self.client("judge", ip(10, 0, 0, 2))}
        log = _open_log(cfg)
        try:
            with mock.patch.object(llm._OPENER, "open", side_effect=AssertionError("sent")) as op, \
                    self.assertRaises(EgressRefused) as cm:
                run(cfg, ["Q"], clients, log=log)
        finally:
            log.close()
        self.assertIn("no opt-in", str(cm.exception))
        self.assertEqual(op.call_count, 0)

class TestDeterminism(_Base):
    def _ctx(self, client, tool):
        cfg = types.SimpleNamespace(run_id="t18", corpus={}, tool=tool)
        log = types.SimpleNamespace(path="unused.jsonl")
        return types.SimpleNamespace(cfg=cfg, clients={"judge": client}, log=log)

    def test_determinism_remote_reported_not_blocking(self):
        srv, url = self.serve()
        msgs, _ = self.kit_messages()
        sample = [{"messages": msgs, "temperature": 0.0, "seed": 1, "max_tokens": 8000,
                   "expected_sha256": "0" * 64, "role": "judge"}]      # the re-send differs from the logged reply
        client = self.anthropic(url)
        with routed(), mock.patch("second_scribe.audits.sample_from_log", return_value=sample):
            remote = audits._determinism_results("J", self._ctx(client, {
                "remote_endpoints": [entry("judge", f"{FAKE_HOST}:{srv.server_address[1]}", "anthropic")]}))
            local = audits._determinism_results("J", self._ctx(client, {}))
        (r,), (l,) = remote, local
        self.assertFalse(r.blocking)
        self.assertTrue(r.passed)
        self.assertIs(r.counts["remote"], True)
        self.assertEqual(r.counts["n_identical"], 0)
        self.assertEqual(r.failures, [])
        self.assertEqual(r.warnings, ["remote_not_identical:1"])
        # without the allow-list entry the result is exactly today's: a FAIL, non-blocking, no remote key
        self.assertFalse(l.blocking)
        self.assertFalse(l.passed)
        self.assertNotIn("remote", l.counts)
        self.assertEqual(l.failures, ["not_identical:1"])
        # an error on a remote role is still a failure
        srv.set(status=400)
        with routed(), mock.patch("second_scribe.audits.sample_from_log", return_value=sample):
            (e,) = audits._determinism_results("J", self._ctx(client, {
                "remote_endpoints": [entry("judge", f"{FAKE_HOST}:{srv.server_address[1]}", "anthropic")]}))
        self.assertFalse(e.passed)
        self.assertEqual(e.failures, ["determinism_error:1"])


class TestKeyNeverWritten(unittest.TestCase):
    """A real-corpus tool run with both roles on a remote Anthropic endpoint, through the CLI."""

    @classmethod
    def setUpClass(cls):
        cls.srv = MockModels([DEMO_CONFIG])
        cls.srv.start()
        cls.addClassCleanup(cls.srv.stop)
        port = cls.srv.server_address[1]
        host = f"{FAKE_HOST}:{port}"
        cls.out = Path(tempfile.mkdtemp())
        eps = [entry("recon", host, "anthropic"), entry("judge", host, "anthropic")]
        cls.config = real_config(cls.out, {"remote_endpoints": eps})
        cls.key, var = fake_key(), key_var()
        env = {var: cls.key}
        for role, model in (("RECON", "remote-recon-1"), ("JUDGE", "claude-mock-1")):
            env.update({f"SS_{role}_URL": f"http://{host}/v1", f"SS_{role}_MODEL": model,
                        f"SS_{role}_KEY_ENV": var, f"SS_{role}_PROVIDER": "anthropic"})
        cls.buf = io.StringIO()
        real_open = llm._OPENER.open
        cls.sent = []

        def spy(req, *a, **kw):
            cls.sent.append(req.get_header("X-api-key") == cls.key)
            return real_open(req, *a, **kw)
        # the mock speaks plain http: the module constant is the only way to let the guard pass it
        with routed(), mock.patch.object(llm, "REMOTE_SCHEMES", ("http", "https")), mock.patch.dict(os.environ, env), \
                mock.patch.object(llm._OPENER, "open", side_effect=spy), \
                contextlib.redirect_stdout(cls.buf), contextlib.redirect_stderr(cls.buf):
            cls.rc = cli_main(["run", "--config", str(cls.config), "--stages", "Q,J", "--client", "http"])
        cls.cfg = RunConfig.load(cls.config)

    def test_run_went_through_the_remote_endpoint(self):
        self.assertEqual(self.rc, 0, "the CLI run failed")
        paths = {r["path"] for r in self.srv.requests}
        self.assertEqual(paths, {"/v1/messages"})
        self.assertGreater(len(self.sent), 10)
        self.assertTrue(all(self.sent), "every request carried the key in x-api-key")
        models = {r["model"] for r in self.srv.requests}
        self.assertEqual(models, {"remote-recon-1", "claude-mock-1"})

    def test_key_never_written(self):
        self.assertEqual(self.rc, 0)
        roots = [self.out, Path(self.cfg.out_dir), Path(tempfile.gettempdir())]
        needle = self.key.encode("utf-8")
        n = 0
        for root in roots:
            for p in root.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    n += 1
                    try:
                        data = p.read_bytes()
                    except OSError:
                        continue
                    self.assertFalse(needle in data, f"the key is in {p.name}")
        self.assertGreater(n, 5)
        self.assertNotIn(self.key, self.buf.getvalue())

    def test_headers_not_logged(self):
        self.assertEqual(self.rc, 0)
        log = Path(self.cfg.out_dir) / "logs" / "run.jsonl"
        text = log.read_text(encoding="utf-8")
        self.assertIn('"call_request"', text)
        # ("authorization" is a word of the synthetic corpus, so the header names are checked in their header forms)
        for word in ("x-api-key", "anthropic-version", ANTHROPIC_VERSION, self.key, '"authorization"', "bearer ",
                     '"headers"'):
            self.assertFalse(word.lower() in text.lower(), f"{word[:12]} is in the log")
        hosts = {json.loads(l)["payload"].get("endpoint_host") for l in text.splitlines()
                 if json.loads(l)["record_type"] == "call_request"}
        self.assertEqual(hosts, {f"{FAKE_HOST}:{self.srv.server_address[1]}"})
        # the client's repr and an error message name neither the key nor its header
        c = AnthropicClient(ModelConfig(name="judge", endpoint="https://api.anthropic.com/v1", model_id="c",
                                        api_key_env="SS_T18_UNUSED", provider="anthropic"))
        self.assertNotIn("x-api-key", repr(c).lower())


if __name__ == "__main__":
    unittest.main()
