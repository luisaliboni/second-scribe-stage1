"""Task 34: models in Setup step 6 (spec 6.5 step 6, 8, 8.1, 2.1-2.2).

``second_scribe.app.models`` classifies addresses, discovers models, reads families, refuses cloud models, runs the
connection test and keeps the opt-ins and declarations; ``routes/models.py`` serves it.

Every network call goes to ``tests/mock_models.py`` on 127.0.0.1. A LAN, Tailscale or remote address is a made-up
``*.mock.invalid`` name: the kit's resolver (``llm._resolve``) is patched to give it the address under test, and the
socket layer sends it to 127.0.0.1 (as in T18's tests). No real Ollama, LM Studio or API is contacted; every key is
generated at run time.
"""
import contextlib
import hashlib
import ipaddress
import json
import os
import secrets as pysecrets
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from second_scribe import llm, toolcfg
from second_scribe.app import api, models, strings
from second_scribe.app.project import Project
from second_scribe.llm import ModelConfig
from tests import test_server_security as tss
from tests.addr import ip
from tests.mock_models import MockModels

_REAL_GETADDRINFO = socket.getaddrinfo
LAN_HOST = "gpu-box.mock.invalid"
TS_HOST = "tail-box.mock.invalid"
REMOTE_HOST = "remote-llm.mock.invalid"
NAMES = {LAN_HOST: ip(192, 168, 1, 20), TS_HOST: ip(100, 101, 2, 3), REMOTE_HOST: ip(104, 18, 0, 1),
         "localhost": "127.0.0.1"}


def fake_key() -> str:
    return "sk" + "-test-" + pysecrets.token_hex(12)


@contextlib.contextmanager
def routed(names=None):
    """Each made-up name resolves (for the guard and the classifier) to its address under test, and connects to
    127.0.0.1."""
    table = dict(NAMES if names is None else names)

    def resolver(host, port):
        if host in table:
            return [table[host]]
        raise OSError("unknown host")

    def gai(host, *a, **kw):
        return _REAL_GETADDRINFO("127.0.0.1" if host in table else host, *a, **kw)
    with mock.patch("second_scribe.llm._resolve", side_effect=resolver), \
            mock.patch("socket.getaddrinfo", side_effect=gai):
        yield


def mock_server(test, *, models_=None, **flags) -> MockModels:
    """A started mock that also answers the connection test's tiny prompt."""
    srv = MockModels([], models=models_)
    inner = srv.answer

    def answer(messages, *a, **kw):
        if any(models.PING_PROMPT in str(m.get("content")) for m in messages or [] if isinstance(m, dict)):
            return '{"ok": true}'
        return inner(messages, *a, **kw)
    srv.answer = answer
    if flags:
        srv.set(**flags)
    srv.start()
    test.addCleanup(srv.stop)
    return srv


def without(srv: MockModels, *paths: str) -> None:
    srv.routes = {k: v for k, v in MockModels.routes.items() if k[1] not in paths}


class TestClassify(unittest.TestCase):
    def test_classify_address_table(self):
        cases = [
            ("http://127.0.0.1:11434", "loopback"),
            ("http://" + ip(127, 8, 0, 1) + ":8080/v1", "loopback"),
            ("http://[::1]:1234/v1", "loopback"),
            ("http://[::ffff:" + ip(127, 0, 0, 1) + "]:1234", "loopback"),
            ("http://localhost:11434", "loopback"),
            ("http://" + ip(10, 2, 3, 4) + ":11434", "lan"),
            ("http://" + ip(172, 16, 0, 9) + ":11434", "lan"),
            ("http://" + ip(172, 31, 255, 1) + ":11434", "lan"),
            ("http://" + ip(192, 168, 0, 7), "lan"),
            ("http://" + LAN_HOST + ":11434", "lan"),
            ("http://" + ip(100, 64, 0, 1) + ":11434", "tailscale"),
            ("http://" + ip(100, 127, 255, 254), "tailscale"),
            ("http://" + TS_HOST + ":11434", "tailscale"),
            ("http://" + ip(100, 128, 0, 1), "remote"),
            ("http://" + ip(172, 32, 0, 1), "remote"),
            ("https://" + ip(8, 8, 8, 8) + "/v1", "remote"),
            ("https://" + REMOTE_HOST + "/v1", "remote"),
            ("http://[fd00::1]:1234", "remote"),           # the kit's nets have no IPv6 private range
            ("", "invalid"), ("http://", "invalid"), ("ftp://" + ip(127, 0, 0, 1), "invalid"),
            ("127.0.0.1:11434", "invalid"), ("http://unknown.mock.invalid", "invalid"),
            ("http://[::1", "invalid"), (None, "invalid"), (42, "invalid"),
        ]
        with routed():
            for url, want in cases:
                with self.subTest(url=url):
                    self.assertEqual(models.classify_address(url), want)

    def test_mixed_resolution_takes_the_widest_class(self):
        def res(addrs):
            return lambda host, port: addrs
        lo, lan, ts, pub = "127.0.0.1", ip(192, 168, 1, 2), ip(100, 64, 1, 1), ip(104, 18, 0, 1)
        for addrs, want in (([lo, lo], "loopback"), ([lo, lan], "lan"), ([lan, ts], "tailscale"),
                            ([lo, pub], "remote"), ([], "invalid")):
            with self.subTest(addrs=addrs):
                self.assertEqual(models.classify_address("http://multi.mock.invalid", resolver=res(addrs)), want)

    def test_classify_agrees_with_the_kit_guard(self):
        """loopback/lan/tailscale exactly when the kit's ``endpoint_problem`` lets a real corpus go there."""
        nets = {str(n) for n in models.LOOPBACK_NETS + models.LAN_NETS + models.TAILSCALE_NETS}
        self.assertEqual(nets, {str(n) for n in llm._LOCAL_NETS})
        urls = ["http://127.0.0.1:1", "http://" + ip(10, 0, 0, 1), "http://" + ip(100, 64, 0, 1),
                "http://" + ip(100, 128, 0, 1), "https://" + REMOTE_HOST, "http://" + LAN_HOST, "http://[::1]:2"]
        with routed():
            for url in urls:
                with self.subTest(url=url):
                    local = models.classify_address(url) in ("loopback", "lan", "tailscale")
                    self.assertEqual(local, llm.endpoint_problem(url) is None)

    def test_endpoint_for_kind(self):
        self.assertEqual(models.endpoint_url("http://127.0.0.1:11434", "local"), "http://127.0.0.1:11434/v1")
        self.assertEqual(models.endpoint_url("http://127.0.0.1:11434/v1/", "local"), "http://127.0.0.1:11434/v1")
        self.assertEqual(models.endpoint_url("https://openrouter.mock.invalid/api/v1/", "openai_compat"),
                         "https://openrouter.mock.invalid/api/v1")
        for bad in ("http://user:pw@127.0.0.1:1", "http://127.0.0.1:1/?key=x", "http://127.0.0.1:1/#x", "ftp://x",
                    "http://127.0.0.1:1 /v1"):
            with self.subTest(bad=bad), self.assertRaises(models.ModelsError) as cm:
                models.endpoint_url(bad, "local")
            self.assertEqual(cm.exception.code, "bad_url")


class TestDiscover(unittest.TestCase):
    def test_discover_ollama_and_lmstudio_fallback(self):
        srv = mock_server(self)
        base = srv.base_url
        found = models.discover(base)
        self.assertEqual([m["name"] for m in found], ["qwen-mock:7b", "gemma-mock:9b"])
        g = found[1]
        self.assertEqual(g["server"], "ollama")
        self.assertEqual(g["family"], "gemma2")                       # model_info["general.architecture"]
        self.assertEqual(g["families"], ["gemma2"])
        self.assertEqual(g["digest"], hashlib.sha256(b"gemma-mock:9b").hexdigest())
        self.assertFalse(g["remote"])
        paths = [(r["method"], r["path"]) for r in srv.requests]
        self.assertEqual(paths[0], ("GET", "/api/tags"))
        self.assertEqual(paths.count(("POST", "/api/show")), 2)

        without(srv, "/api/tags")                                     # LM Studio v1
        found = models.discover(base + "/v1")
        self.assertEqual([(m["name"], m["family"], m["server"]) for m in found],
                         [("qwen-mock:7b", "qwen2", "lmstudio"), ("gemma-mock:9b", "gemma2", "lmstudio")])
        self.assertTrue(all(m["digest"] is None for m in found))

        without(srv, "/api/tags", "/api/v1/models")                   # LM Studio v0
        found = models.discover(base)
        self.assertEqual([(m["family"], m["server"]) for m in found], [("qwen2", "lmstudio"), ("gemma2", "lmstudio")])

        without(srv, "/api/tags", "/api/v1/models", "/api/v0/models")  # any OpenAI-compatible server
        found = models.discover(base)
        self.assertEqual([m["server"] for m in found], ["openai", "openai"])
        self.assertEqual([m["family"] for m in found], [None, None])  # the user types it
        self.assertEqual([m["family_hint"] for m in found], ["qwen", "gemma"])

        without(srv, "/api/tags", "/api/v1/models", "/api/v0/models", "/v1/models")
        with self.assertRaises(models.ModelsError) as cm:
            models.discover(base)
        self.assertEqual(cm.exception.code, "no_models_listed")

    def test_discover_unreachable_and_timeout(self):
        srv = mock_server(self)
        port = srv.server_address[1]
        srv.stop()
        with self.assertRaises(models.ModelsError) as cm:
            models.discover(f"http://127.0.0.1:{port}")
        self.assertEqual(cm.exception.code, "unreachable")
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.full_url, timeout))
                raise urllib.error.URLError(socket.timeout("timed out"))
        with self.assertRaises(models.ModelsError) as cm:
            models.discover("http://127.0.0.1:9", opener=Opener())
        self.assertEqual(cm.exception.code, "unreachable")
        self.assertTrue(seen)
        self.assertTrue(all(t == models.DISCOVERY_TIMEOUT_S == 3.0 for _, t in seen), seen)
        self.assertEqual(seen[0][0], "http://127.0.0.1:9/api/tags")

    def test_default_opener_ignores_proxies_and_redirects(self):
        import urllib.request as ur
        hs = models._OPENER.handlers
        self.assertFalse([h for h in hs if isinstance(h, ur.ProxyHandler) and getattr(h, "proxies", None)])
        self.assertEqual([type(h) for h in hs if isinstance(h, ur.HTTPRedirectHandler)], [llm._RefuseRedirects])
        # a fresh opener built the same way under a proxy env still has no proxy (the default one would)
        with mock.patch.dict(os.environ, {"http_proxy": "http://" + ip(127, 0, 0, 2) + ":9"}):
            fresh = ur.build_opener(ur.ProxyHandler({}), llm._RefuseRedirects)
            default = ur.build_opener()
        self.assertFalse([h for h in fresh.handlers if isinstance(h, ur.ProxyHandler) and h.proxies])
        self.assertTrue([h for h in default.handlers if isinstance(h, ur.ProxyHandler) and h.proxies])

    def test_remote_discovery_lists_v1_models_with_the_key(self):
        srv = mock_server(self)
        key = fake_key()
        seen = []

        class Spy:
            def open(self, req, timeout=None):
                seen.append({k.lower(): v for k, v in req.header_items()})
                return models._OPENER.open(req, timeout=timeout)
        found = models.discover(srv.base_url + "/v1", kind="anthropic", key=key, opener=Spy())
        self.assertEqual([m["server"] for m in found], ["openai", "openai"])
        self.assertEqual([r["path"] for r in srv.requests], ["/v1/models"])     # no Ollama paths on an API
        self.assertEqual(seen[0]["x-api-key"], key)
        self.assertIn("anthropic-version", seen[0])
        self.assertEqual([m["family_hint"] for m in found], ["qwen", "gemma"])


class TestCloud(unittest.TestCase):
    def test_cloud_models_refused(self):
        for entry, want in (({"name": "gpt-oss:120b-cloud"}, True), ({"name": "glm-4.6:cloud"}, True),
                            ({"name": "Kimi-K2:1T-CLOUD"}, True), ({"name": "x", "remote_host": "https://h"}, True),
                            ({"name": "x", "remote_model": "x"}, True), ({"name": "x", "remote": True}, True),
                            ({"name": "cloudy:7b"}, False), ({"name": "gemma3:12b"}, False),
                            ({"name": "x", "remote_host": ""}, False), ({}, False)):
            with self.subTest(entry=entry):
                self.assertIs(models.is_cloud_model(entry), want)
        srv = mock_server(self, cloud=True)
        found = models.discover(srv.base_url)
        self.assertTrue(all(m["remote"] for m in found))
        self.assertTrue(all(m["cloud_reason"] == "remote_host" for m in found))


class TestFamilies(unittest.TestCase):
    def test_families_differ_prefix_rule(self):
        same = [("gemma3", "gemma2"), ("Gemma3", "gemma"), ("qwen2", "qwen3moe"), ("llama", "llama3.1"),
                ("phi-3", "phi4"), ("qwen2.5", "QWEN"), ("", "gemma"), (None, "gemma"), ("gemma", "gemma")]
        differ = [("gemma2", "llama"), ("qwen2", "deepseek2"), ("llama", "llava"), ("claude", "gpt-oss"),
                  ("mistral", "mixtral")]
        for a, b in same:
            with self.subTest(a=a, b=b):
                self.assertFalse(models.families_differ(a, b))
                self.assertFalse(models.families_differ(b, a))
        for a, b in differ:
            with self.subTest(a=a, b=b):
                self.assertTrue(models.families_differ(a, b))
                self.assertTrue(models.families_differ(b, a))

    def test_family_hint(self):
        self.assertEqual(models.family_hint("claude-opus-4-1-20250805", "anthropic"), "claude")
        self.assertEqual(models.family_hint("some-alias", "anthropic"), "claude")
        self.assertEqual(models.family_hint("mistralai/mistral-large", "openai"), "mistral")
        self.assertEqual(models.family_hint("acmeai/acme-large", "openai"), "acmeai")
        self.assertIsNone(models.family_hint("judge", "openai"))


# --- routes ---------------------------------------------------------------------------------------------------

class RoutesCase(tss.ServerCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.cookie = self.login()
        self.token = self.csrf(self.cookie)
        self.bodies = []
        status, _, data = self.call("/api/project/create", {"path": str(self.tmp / "study"), "name": "Study"})
        self.assertEqual(status, 200, data)
        self.project = self.state.project

    def call(self, path, obj=None):
        if obj is None:
            status, headers, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        else:
            status, headers, data = self.post(path, obj, self.cookie, self.token)
        self.bodies.append(data)
        return status, headers, data

    def ok(self, path, obj=None):
        status, _, data = self.call(path, obj)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def refused(self, path, obj, status, code):
        got, _, data = self.call(path, obj)
        self.assertEqual((got, self.error_of(data)), (status, code), data)
        return json.loads(data)

    def codes(self):
        return {(p["code"], p["role"]) for p in self.ok("/api/models")["models"]["problems"]}

    def set_local(self, srv, role, model, family=None, url=None):
        fam = family or {"qwen-mock:7b": "qwen", "gemma-mock:9b": "gemma"}.get(model, "llama")
        return self.ok("/api/models/set", {"role": role, "kind": "local", "url": url or srv.base_url,
                                           "model": model, "family": fam})


class TestRoutes(RoutesCase):
    def test_set_stores_non_secret_fields_and_verifies_against_the_server(self):
        srv = mock_server(self)
        doc = self.set_local(srv, "recon", "qwen-mock:7b")
        r = doc["models"]["roles"]["recon"]
        self.assertEqual(r["url"], srv.base_url + "/v1")
        self.assertEqual(r["address_class"], "loopback")
        self.assertEqual(r["digest"], hashlib.sha256(b"qwen-mock:7b").hexdigest())
        self.assertEqual(r["family"], "qwen")
        self.assertEqual(r["family_detected"], "qwen2")
        self.assertEqual(r["provider"], "openai")
        stored = Project.open(self.project.root).data["models"]["roles"]["recon"]
        self.assertEqual(set(stored), set(models.ROLE_FIELDS))
        self.assertEqual(self.ok("/api/models")["models"]["defaults"], models.DEFAULT_URLS)
        self.assertEqual(models.classify_address(models.DEFAULT_URLS["local"]), "loopback")
        self.refused("/api/models/set", {"role": "recon", "kind": "local", "url": srv.base_url,
                                         "model": "absent:1b", "family": "x"}, 422, "model_not_found")
        self.refused("/api/models/set", {"role": "recon", "kind": "local", "url": srv.base_url,
                                         "model": "qwen-mock:7b", "family": ""}, 422, "family_missing")
        self.refused("/api/models/set", {"role": "judge2", "kind": "local", "url": srv.base_url,
                                         "model": "qwen-mock:7b", "family": "q"}, 422, "bad_role")
        self.refused("/api/models/set", {"role": "recon", "kind": "cloudy", "url": srv.base_url,
                                         "model": "qwen-mock:7b", "family": "q"}, 422, "bad_kind")
        self.refused("/api/models/set", {"role": "recon", "kind": "anthropic", "url": srv.base_url,
                                         "model": "claude-x", "family": "claude"}, 422, "anthropic_remote_only")

    def test_unresolvable_name_has_its_own_refusal_and_is_still_refused(self):
        """K2 U21: a well-formed hosted address whose name cannot be looked up is refused as ``name_not_found``
        ("could not be looked up"), not ``bad_url`` ("type the full address"); a malformed one stays ``bad_url``.
        The lookup is the patched resolver (no network); nothing is stored."""
        want = strings.t("models.error.name_not_found")
        self.assertIn("looked up", want)
        self.assertNotEqual(want, strings.t("models.error.bad_url"))
        body = {"role": "recon", "kind": "openai_compat", "url": "https://nowhere.mock.invalid/v1"}
        with routed():
            for path in ("/api/models/classify", "/api/models/discover"):
                with self.subTest(path=path):
                    doc = self.refused(path, body, 422, "name_not_found")
                    self.assertEqual(doc["message"], want)
            self.refused("/api/models/set", {**body, "model": "m", "family": "llama"}, 422, "name_not_found")
            doc = self.refused("/api/models/classify", {**body, "url": "nowhere.mock.invalid"}, 422, "bad_url")
            self.assertEqual(doc["message"], strings.t("models.error.bad_url"))
        problem, info = models.contact_problem(self.project, "recon", body["url"], "openai_compat",
                                               resolver=lambda host, port: [])
        self.assertEqual((problem, info["allowed"]), ("name_not_found", False))
        self.assertEqual(models.contact_problem(self.project, "recon", "ftp://x", "openai_compat")[0], "bad_url")
        self.assertEqual(self.ok("/api/models")["models"]["roles"], {})

    def test_cloud_models_refused_over_http(self):
        srv = mock_server(self, cloud=True)
        doc = self.ok("/api/models/discover", {"role": "recon", "kind": "local", "url": srv.base_url})
        self.assertTrue(all(m["remote"] for m in doc["models"]))
        self.refused("/api/models/set", {"role": "recon", "kind": "local", "url": srv.base_url,
                                         "model": "qwen-mock:7b", "family": "qwen"}, 422, "cloud_model")
        srv.set(cloud=False)
        self.refused("/api/models/set", {"role": "recon", "kind": "local", "url": srv.base_url,
                                         "model": "gpt-oss:120b-cloud", "family": "gpt-oss"}, 422, "cloud_model")
        self.assertNotIn("recon", Project.open(self.project.root).data["models"].get("roles", {}))

    def test_same_family_ai_maps_only(self):
        fam = {"gemma-a:2b": {"family": "gemma3", "digest": "a" * 64},
               "gemma-b:9b": {"family": "gemma2", "digest": "b" * 64},
               "llama-c:8b": {"family": "llama", "digest": "c" * 64}}
        srv = mock_server(self, models_=fam)
        self.set_local(srv, "recon", "gemma-a:2b", "gemma3")
        doc = self.set_local(srv, "judge", "gemma-b:9b", "gemma2")
        self.assertIn(("families_unconfirmed", None), {(p["code"], p["role"]) for p in doc["models"]["problems"]})
        self.refused("/api/models/confirm-families", {"recon": "gemma3", "judge": "llama"}, 409, "families_changed")
        doc = self.ok("/api/models/confirm-families", {"recon": "gemma3", "judge": "gemma2"})
        m = doc["models"]
        self.assertEqual(m["pipeline_mode"], "ai_maps_only")
        self.assertTrue(m["same_family"])
        self.assertEqual(m["notice"], strings.t("setup.models.same_family"))
        self.assertEqual(models.pipeline_mode(self.project), "ai_maps_only")
        self.assertNotIn("families_unconfirmed", {c for c, _ in self.codes()})
        # no override: there is no field that turns it into a two-family run
        doc = self.ok("/api/models/confirm-families", {"recon": "gemma3", "judge": "gemma2", "override": True,
                                                       "pipeline_mode": "two_family"})
        self.assertEqual(doc["models"]["pipeline_mode"], "ai_maps_only")
        # a different-family judge: two families
        self.set_local(srv, "judge", "llama-c:8b", "llama")
        self.assertIn(("families_unconfirmed", None), self.codes())
        self.assertIsNone(models.pipeline_mode(self.project))
        doc = self.ok("/api/models/confirm-families", {"recon": "gemma3", "judge": "llama"})
        self.assertEqual(doc["models"]["pipeline_mode"], "two_family")
        tf = models.tool_fields(self.project)
        self.assertEqual(tf["judge_rule"], "two_family")
        self.assertEqual(tf["model_families"], {"recon": "gemma3", "judge": "llama"})
        self.assertEqual(tf["model_digests"], {"recon": "a" * 64, "judge": "c" * 64})
        toolcfg.check_tool(tf, {})

    def test_connection_test_flags_thinking(self):
        srv = mock_server(self)
        self.set_local(srv, "recon", "qwen-mock:7b")
        self.assertIn(("connection_untested", "recon"), self.codes())
        doc = self.ok("/api/models/test", {"role": "recon"})
        t = doc["result"]
        self.assertTrue(t["ok"])
        self.assertFalse(t["thinking_seen"])
        self.assertTrue(t["reply_ok"])
        self.assertIsInstance(t["ms"], int)
        self.assertIsNone(t["error"])
        chat = [r for r in srv.requests if r["path"] == "/v1/chat/completions"][-1]
        self.assertIs(chat["body"]["chat_template_kwargs"]["enable_thinking"], False)
        self.assertEqual(chat["body"]["model"], "qwen-mock:7b")
        self.assertEqual(chat["body"]["temperature"], 0)
        self.assertNotIn(("connection_untested", "recon"), self.codes())

        srv.set(ignore_thinking_flag=True)               # the server ignores the switch
        doc = self.ok("/api/models/test", {"role": "recon"})
        self.assertTrue(doc["result"]["thinking_seen"])
        self.assertEqual(doc["message"], strings.t("setup.models.thinking_ignored"))
        self.assertIn(("thinking_on", "recon"), self.codes())
        stored = Project.open(self.project.root).data["models"]["roles"]["recon"]["connection"]
        self.assertTrue(stored["thinking_seen"])

        srv.set(ignore_thinking_flag=False, status=500)
        doc = self.ok("/api/models/test", {"role": "recon"})
        self.assertFalse(doc["result"]["ok"])
        self.assertEqual(doc["result"]["error"], "http_500")
        self.assertIn(("connection_failed", "recon"), self.codes())
        self.refused("/api/models/test", {"role": "judge"}, 409, "role_not_set")

    def test_connection_test_thinking_in_text_and_malformed(self):
        srv = mock_server(self)
        cfg = {"url": srv.base_url + "/v1", "model": "qwen-mock:7b", "provider": "openai",
               "address_class": "loopback"}
        orig = srv.answer
        srv.answer = lambda *a, **kw: "<think>hm</think>" + orig(*a, **kw)
        self.assertTrue(models.connection_test("recon", cfg)["thinking_seen"])
        srv.answer = orig
        srv.set(malformed=True)
        r = models.connection_test("recon", cfg)
        self.assertEqual((r["ok"], r["error"]), (False, "malformed"))

    def test_lan_needs_logged_optin(self):
        srv = mock_server(self)
        url = f"http://{LAN_HOST}:{srv.server_address[1]}"
        host = f"{LAN_HOST}:{srv.server_address[1]}"
        with routed():
            doc = self.ok("/api/models/classify", {"role": "recon", "kind": "local", "url": url})
            self.assertEqual((doc["address_class"], doc["needs"], doc["allowed"]), ("lan", "optin", False))
            self.assertEqual(doc["host"], host)
            n = len(srv.requests)
            self.refused("/api/models/discover", {"role": "recon", "kind": "local", "url": url}, 409, "optin_missing")
            self.refused("/api/models/set", {"role": "recon", "kind": "local", "url": url, "model": "qwen-mock:7b",
                                             "family": "qwen"}, 409, "optin_missing")
            self.assertEqual(len(srv.requests), n, "the app contacted a LAN address before the opt-in")
            self.refused("/api/models/optin", {"role": "recon", "host": host}, 422, "not_understood")
            self.refused("/api/models/optin", {"role": "recon", "host": "127.0.0.1:1", "understood": True},
                         422, "optin_not_needed")
            self.assertEqual(self.project.log.records("optin"), [])
            self.ok("/api/models/optin", {"role": "recon", "host": host, "understood": True})
            self.ok("/api/models/optin", {"role": "recon", "host": host, "understood": True})    # once only
            recs = self.project.log.records("optin")
            self.assertEqual([r["payload"] for r in recs], [{"role": "recon", "host": host, "address_class": "lan"}])
            # an opt-in is per role: the judge on the same box asks again
            self.refused("/api/models/discover", {"role": "judge", "kind": "local", "url": url}, 409,
                         "optin_missing")
            self.assertEqual(len(self.ok("/api/models/discover", {"role": "recon", "kind": "local",
                                                                  "url": url})["models"]), 2)
            self.set_local(srv, "recon", "qwen-mock:7b", url=url)
            self.ok("/api/models/test", {"role": "recon"})
            self.assertEqual(models.endpoint_optins(self.project), [{"role": "recon", "host": host}])
            self.assertNotIn("optin_missing", {c for c, _ in self.codes()})
            env = models.role_env(self.project, self.state.secrets, "recon", "RECON")
            self.assertEqual(json.loads(env["SS_RECON_PARAMS"]), {"chat_template_kwargs": {"enable_thinking": False}})
            ok, _ = self.project.log.verify()
            self.assertTrue(ok)
        # Tailscale goes the same way
        with routed():
            doc = self.ok("/api/models/classify", {"role": "judge", "kind": "local",
                                                   "url": f"http://{TS_HOST}:{srv.server_address[1]}"})
            self.assertEqual((doc["address_class"], doc["needs"]), ("tailscale", "optin"))

    def test_remote_needs_declaration(self):
        srv = mock_server(self)
        port = srv.server_address[1]
        host = f"{REMOTE_HOST}:{port}"
        url = f"http://{host}/v1"
        key = fake_key()
        with routed():
            doc = self.ok("/api/models/classify", {"role": "recon", "kind": "anthropic", "url": url})
            self.assertEqual((doc["address_class"], doc["needs"], doc["https_ok"]), ("remote", "declaration", False))
            self.refused("/api/models/set", {"role": "recon", "kind": "anthropic", "url": url,
                                             "model": "claude-mock", "family": "claude"}, 422, "remote_needs_https")
            with mock.patch.object(llm, "REMOTE_SCHEMES", ("http", "https")):
                self.ok("/api/models/key", {"role": "recon", "key": key})
                n = len(srv.requests)
                self.refused("/api/models/discover", {"role": "recon", "kind": "anthropic", "url": url}, 409,
                             "declaration_missing")
                self.refused("/api/models/set", {"role": "recon", "kind": "anthropic", "url": url,
                                                 "model": "claude-mock", "family": "claude"}, 409,
                             "declaration_missing")
                self.assertEqual(len(srv.requests), n, "the app contacted a remote address before the declaration")
                body = {"role": "recon", "provider": "anthropic", "host": host, "basis": "REB 1234 approves this"}
                self.refused("/api/models/declaration", dict(body), 422, "not_ticked")
                self.refused("/api/models/declaration", {**body, "ticked": True, "basis": " "}, 422, "basis_missing")
                self.refused("/api/models/declaration", {**body, "ticked": True, "provider": "gemini"}, 422,
                             "bad_provider")
                self.refused("/api/models/declaration", {**body, "ticked": True, "host": "127.0.0.1:9"}, 422,
                             "declaration_not_needed")
                self.assertEqual(self.project.log.records("declaration"), [])
                doc = self.ok("/api/models/declaration", {**body, "ticked": True})
                d = doc["declaration"]
                canon = {"schema": "declaration/1", "role": "recon", "provider": "anthropic", "host": host,
                         "basis": "REB 1234 approves this"}
                want = hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()
                self.assertEqual(d, {"role": "recon", "provider": "anthropic", "host": host, "sha256": want})
                rec = self.project.log.records("declaration")
                self.assertEqual(len(rec), 1)
                self.assertEqual(rec[0]["payload"], {**canon, "sha256": want, "schema": "declaration/1"})
                self.ok("/api/models/discover", {"role": "recon", "kind": "anthropic", "url": url})
                self.ok("/api/models/set", {"role": "recon", "kind": "anthropic", "url": url,
                                            "model": "claude-mock", "family": "claude"})
                eps = models.remote_endpoints(self.project)
                self.assertEqual(eps, [{"role": "recon", "host": host, "provider": "anthropic",
                                        "declaration_sha256": want}])
                self.assertNotIn("declaration_missing", {c for c, _ in self.codes()})
                tf = models.tool_fields(self.project)
                self.assertEqual(tf["remote_endpoints"], eps)
                toolcfg.check_tool(tf, {})
                # the kit's own guard accepts exactly this allow-list, and refuses it for the judge role
                env = models.role_env(self.project, self.state.secrets, "recon", "RECON")
                self.assertEqual(env["SS_RECON_PARAMS"], "{}")
                self.assertEqual(env["SS_RECON_PROVIDER"], "anthropic")
                with mock.patch.dict(os.environ, env):
                    cfg = ModelConfig.from_env("RECON")
                client = llm.make_client(cfg)
                llm.guard_real_clients({"recon": client}, remote_endpoints=eps)
                with self.assertRaises(llm.EgressRefused):
                    llm.guard_real_clients({"judge": client}, remote_endpoints=eps)
                # the declaration is bound to role, host and provider: another host needs its own
                other = f"http://{REMOTE_HOST.replace('remote', 'other')}:{port}/v1"
                with routed({**NAMES, REMOTE_HOST.replace("remote", "other"): ip(104, 18, 0, 2)}):
                    self.refused("/api/models/discover", {"role": "recon", "kind": "anthropic", "url": other}, 409,
                                 "declaration_missing")
            # back to https only: the stored http remote is a problem, and set refuses it
            self.assertIn(("remote_needs_https", "recon"), self.codes())
            self.refused("/api/models/set", {"role": "recon", "kind": "anthropic", "url": url,
                                             "model": "claude-mock", "family": "claude"}, 422, "remote_needs_https")

    def test_key_not_in_project_or_log_or_response(self):
        srv = mock_server(self)
        port = srv.server_address[1]
        host = f"{REMOTE_HOST}:{port}"
        url = f"http://{host}/v1"
        key = fake_key()
        with routed(), mock.patch.object(llm, "REMOTE_SCHEMES", ("http", "https")):
            self.refused("/api/models/key", {"role": "recon"}, 422, "key_missing")
            self.refused("/api/models/key", {"role": "recon", "key": "a\nb"}, 422, "bad_key")
            self.refused("/api/models/key", {"role": "recon", "env_name": "lower-case"}, 422, "bad_env_name")
            doc = self.ok("/api/models/key", {"role": "recon", "key": key})
            self.assertEqual(doc["key"], {"set": True, "source": "pasted"})
            self.ok("/api/models/declaration", {"role": "recon", "provider": "openai_compat", "host": host,
                                                "basis": "Institutional gateway, REB 77", "ticked": True})
            self.ok("/api/models/discover", {"role": "recon", "kind": "openai_compat", "url": url})
            self.ok("/api/models/set", {"role": "recon", "kind": "openai_compat", "url": url,
                                        "model": "qwen-mock:7b", "family": "qwen"})
            self.assertTrue(self.ok("/api/models/test", {"role": "recon"})["result"]["ok"])
            self.ok("/api/models")
            # the key did reach the server's Authorization header (the mock keeps no copy of it)
            self.assertTrue(all("authorization" not in {k.lower() for k in r["headers"]} for r in srv.requests))
            env = models.role_env(self.project, self.state.secrets, "recon", "RECON")
            self.assertEqual(env[env["SS_RECON_KEY_ENV"]], key)
        for data in self.bodies:
            self.assertNotIn(key.encode(), data)
        self.assertNotIn(key, repr(self.state.secrets))
        for root in (self.project.root, self.tmp):
            for p in root.rglob("*"):
                if p.is_file():
                    self.assertNotIn(key.encode(), p.read_bytes(), str(p))
        self.assertNotIn(key, self.out.getvalue())
        # an env var name instead of a pasted key: the name is kept, the value never read into the response
        name = "SS_T34_FAKE_KEY_" + pysecrets.token_hex(3).upper()
        with mock.patch.dict(os.environ, {name: key}):
            doc = self.ok("/api/models/key", {"role": "judge", "env_name": name})
            self.assertEqual(doc["key"], {"set": True, "source": "env", "env_present": True})
            self.assertNotIn(key.encode(), self.bodies[-1])
        self.assertEqual(self.ok("/api/models/key", {"role": "judge", "clear": True})["key"], {"set": False})

    def test_connection_test_sends_the_key_in_the_header(self):
        key = fake_key()
        seen = []

        class Spy:
            def open(self, req, timeout=None):
                seen.append(({k.lower(): v for k, v in req.header_items()}, json.loads(req.data)))
                raise urllib.error.URLError("stop here")
        for provider, header, value in (("openai", "authorization", "Bearer " + key),
                                        ("anthropic", "x-api-key", key)):
            cfg = {"url": "https://" + REMOTE_HOST + "/v1", "model": "m", "provider": provider,
                   "address_class": "remote"}
            r = models.connection_test("recon", cfg, key=key, opener=Spy())
            self.assertEqual((r["ok"], r["error"]), (False, "unreachable"))
            headers, body = seen[-1]
            self.assertEqual(headers[header], value)
            self.assertNotIn("chat_template_kwargs", body)       # a local-server field: never sent to a remote
            self.assertNotIn(key, json.dumps(r))

    def test_role_env_builds_model_config(self):
        srv = mock_server(self)
        self.set_local(srv, "recon", "qwen-mock:7b")
        self.set_local(srv, "judge", "gemma-mock:9b")
        for role, prefix in (("recon", "RECON"), ("judge", "JUDGE")):
            env = models.role_env(self.project, self.state.secrets, role, prefix)
            with mock.patch.dict(os.environ, env):
                cfg = ModelConfig.from_env(prefix)
            self.assertEqual(cfg.endpoint, srv.base_url + "/v1")
            self.assertTrue(llm.thinking_off(cfg.params))
            self.assertEqual(cfg.provider, "openai")
        clients = {r: llm.make_client(self._cfg(r)) for r in ("recon", "judge")}
        llm.guard_real_clients(clients, judge_rule="two_family", families=models.tool_fields(self.project)
                               ["model_families"])
        self.assertRegex(models.pair_sha256(self.project), r"^[0-9a-f]{64}$")
        before = models.pair_sha256(self.project)
        self.set_local(srv, "judge", "qwen-mock:7b")
        self.assertNotEqual(models.pair_sha256(self.project), before)

    def _cfg(self, role):
        env = models.role_env(self.project, self.state.secrets, role, role.upper())
        with mock.patch.dict(os.environ, env):
            return ModelConfig.from_env(role.upper())

    def test_require_ready_and_clear(self):
        srv = mock_server(self)
        with self.assertRaises(api.ApiError) as cm:
            models.require_ready(self.project, self.state.secrets)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "recon_missing"))
        self.set_local(srv, "recon", "qwen-mock:7b")
        self.set_local(srv, "judge", "gemma-mock:9b")
        self.ok("/api/models/test", {"role": "recon"})
        self.ok("/api/models/test", {"role": "judge"})
        with self.assertRaises(api.ApiError) as cm:
            models.require_ready(self.project, self.state.secrets)
        self.assertEqual(cm.exception.code, "families_unconfirmed")
        self.ok("/api/models/confirm-families", {"recon": "qwen", "judge": "gemma"})
        models.require_ready(self.project, self.state.secrets)
        self.assertEqual(self.ok("/api/models")["models"]["problems"], [])
        self.ok("/api/models/clear", {"role": "judge"})
        self.assertIsNone(models.pipeline_mode(self.project))
        doc = self.ok("/api/models/confirm-families", {"recon": "qwen"})
        self.assertEqual(doc["models"]["pipeline_mode"], "ai_maps_only")    # no judge: AI maps only
        self.assertEqual(doc["models"]["notice"], strings.t("setup.models.no_judge"))

    def test_problems_catch_a_hand_edited_project(self):
        """The routes never store a LAN role without its opt-in or a remote role without its declaration; a
        project file edited by hand can, and ``problems`` (which Task 38 refuses on) still catches it, because the
        app log, not the project file, is the record."""
        entry = {"kind": "local", "provider": "openai", "url": f"http://{LAN_HOST}:9/v1", "host": f"{LAN_HOST}:9",
                 "address_class": "lan", "model": "qwen-mock:7b", "family": "qwen", "family_detected": "qwen2",
                 "digest": None, "server": "ollama", "connection": {"ok": True, "ms": 5, "thinking_seen": False,
                                                                    "reply_ok": True, "error": None}}
        models.set_role(self.project, "recon", entry)
        models.set_role(self.project, "judge", {**entry, "kind": "anthropic", "provider": "anthropic",
                                                "url": f"https://{REMOTE_HOST}/v1", "host": REMOTE_HOST,
                                                "address_class": "remote", "model": "claude-x:cloud",
                                                "family": "claude"})
        self.project.save()
        codes = self.codes()
        for want in (("optin_missing", "recon"), ("declaration_missing", "judge"), ("cloud_model", "judge"),
                     ("key_missing", "judge"), ("families_unconfirmed", None)):
            self.assertIn(want, codes)
        self.assertEqual(models.remote_endpoints(self.project), [])
        self.assertEqual(models.endpoint_optins(self.project), [])
        with self.assertRaises(api.ApiError) as cm:
            models.require_ready(self.project, self.state.secrets)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "optin_missing"))

    def test_routes_need_a_project(self):
        self.state.project = None
        self.refused("/api/models/discover", {"role": "recon", "kind": "local", "url": "http://127.0.0.1:9"},
                     409, "no_project")
        st, _, data = self.call("/api/models")
        self.assertEqual((st, self.error_of(data)), (409, "no_project"))

    def test_every_error_code_has_a_message(self):
        for code in models.ERROR_CODES:
            with self.subTest(code=code):
                self.assertTrue(strings.t(f"models.error.{code}"))
        for code in models.CONNECTION_ERRORS:
            with self.subTest(code=code):
                self.assertTrue(strings.t(f"models.connection.{code}"))


class TestRequireDeclarations(unittest.TestCase):
    """Audit K5/S4 (app side): a run config's ``tool.remote_endpoints[*].declaration_sha256`` must match a logged
    ``declaration`` (same role, host, provider; its own text hashing to it) written before the run starts (below
    ``tool.app_log_head`` when the config carries one), else 409 ``declaration_missing``. No network: the records are
    appended directly."""
    HOST = "api.anthropic.com"
    BASIS = "REB 1234 approves sending transcript text to this provider."

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-decl-")
        self.addCleanup(tmp.cleanup)
        self.project = Project.create(Path(tmp.name).resolve() / "study", "Study")

    def declare(self, basis=None, **kw):
        doc = {**models.declaration_doc("recon", "anthropic", self.HOST, basis or self.BASIS), **kw}
        payload = {**doc, "sha256": models.declaration_sha256(doc)}
        self.project.log.append("declaration", payload)
        return payload["sha256"]

    def tool(self, sha, head=None):
        t = {"remote_endpoints": [{"role": "recon", "host": self.HOST, "provider": "anthropic",
                                   "declaration_sha256": sha}]}
        if head is not None:
            t["app_log_head"] = {"records": head[0], "head": head[1]}
        return t

    def refused(self, tool):
        with self.assertRaises(api.ApiError) as cm:
            models.require_declarations(self.project, tool)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "declaration_missing"))

    def test_matching_earlier_record_passes(self):
        sha = self.declare()
        models.require_declarations(self.project, self.tool(sha))
        models.require_declarations(self.project, self.tool(sha, self.project.log.head()))
        models.require_declarations(self.project, {})                    # no remote endpoint: nothing to check
        models.require_declarations(self.project, {"remote_endpoints": []})

    def test_missing_record_refused(self):
        self.declare()
        self.refused(self.tool("c" * 64))

    def test_record_written_after_the_run_start_refused(self):
        head = self.project.log.head()                                  # the run's app_log_head is taken first
        sha = self.declare()
        self.refused(self.tool(sha, head))

    def test_hash_of_different_text_refused(self):
        sha = models.declaration_sha256(models.declaration_doc("recon", "anthropic", self.HOST, self.BASIS))
        forged = {**models.declaration_doc("recon", "anthropic", self.HOST, "Another basis."), "sha256": sha}
        self.project.log.append("declaration", forged)                 # names the hash, but its text is other
        self.refused(self.tool(sha))
        self.assertIsNone(models.find_declaration(self.project, "recon", self.HOST, "anthropic"))
        other = self.declare(basis="Another basis.")                    # a genuine record of other text
        self.refused(self.tool(sha))
        models.require_declarations(self.project, self.tool(other))

    def test_another_role_refused(self):
        sha = self.declare()
        t = self.tool(sha)
        t["remote_endpoints"][0]["role"] = "judge"
        self.refused(t)

    def test_logged_record_carries_the_typed_basis(self):
        """K4/S1: the record holds the basis as typed (trimmed), capped at ``MAX_BASIS``; empty is refused."""
        pub = lambda host, port: [ip(104, 18, 0, 1)]
        d = models.log_declaration(self.project, "recon", "anthropic", self.HOST, "  " + self.BASIS + " ", True, pub)
        (rec,) = self.project.log.records("declaration")
        self.assertEqual(rec["payload"]["basis"], self.BASIS)
        self.assertEqual(rec["payload"]["sha256"], d["sha256"])
        models.require_declarations(self.project, self.tool(d["sha256"], self.project.log.head()))
        self.assertEqual(models.MAX_BASIS, 1000)
        models.log_declaration(self.project, "recon", "anthropic", self.HOST, "x" * models.MAX_BASIS, True, pub)
        for bad in ("", "   ", "x" * (models.MAX_BASIS + 1), "line\nbreak"):
            with self.assertRaises(models.ModelsError) as cm:
                models.log_declaration(self.project, "recon", "anthropic", self.HOST, bad, True, pub)
            self.assertEqual(cm.exception.code, "basis_missing")

    def test_kit_and_app_hash_agree(self):
        from second_scribe import certificate
        doc = models.declaration_doc("judge2", "openai_compat", "llm.example.invalid", "Basis ü")
        self.assertEqual(models.declaration_sha256(doc), certificate.declaration_record_sha256(doc))


if __name__ == "__main__":
    unittest.main()
