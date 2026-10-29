"""Task 35: the Setup model check as an app job, and the time estimate (spec 6.5 steps 7-8, 3 "Model check").

- ``model_check.run_model_check(..., remote_endpoints=...)`` and ``model-check --remote-endpoints FILE.json`` thread the
  K13 allow-list into the guard, so a declared remote (Anthropic) pair can be model-checked; without the list (every
  study path) a remote endpoint is refused as before.
- ``app/jobs.py``: one kit subprocess (``sys.executable``, kit root as cwd, output to files in the job folder); keys
  reach it only through its environment (``Secrets.env_for``), never argv or disk.
- ``app/timing.py``: call counts from the config shape, checked against the oracle demo log; the estimate times one
  call on a shipped synthetic transcript, so no interview text reaches a model before the run.
- ``routes/model_check.py``: start (Q), the swap point, J, the ``model_check`` app-log record and the input Task 38
  puts in ``tool.model_check``.

Every model answer comes from ``tests/mock_models.py`` on 127.0.0.1 (or the kit's offline oracle). A remote endpoint is
the made-up name ``FAKE_HOST``: the guard's resolver is patched to a public address and the socket layer sends that name
to 127.0.0.1, as in T18's tests. No real model, Ollama, LM Studio or API is contacted; every key is generated at run
time.
"""
import contextlib
import hashlib
import io
import json
import os
import secrets as pysecrets
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import llm, model_check, pipeline
from second_scribe.anthropic_client import AnthropicClient
from second_scribe.audit.verify import verify_run
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.cli import main as cli_main
from second_scribe.llm import EgressRefused, HTTPClient, ModelConfig
from second_scribe.pipeline import KIT_ROOT, RunConfig
from second_scribe.transcripts import read_transcript_paras

from tests.addr import ip
from tests.mock_models import MockModels, default_models

FAKE_HOST = "remote-llm.mock.invalid"
THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
RECON_ID, JUDGE_ID, CLAUDE_ID = "qwen-mock:7b", "gemma-mock:9b", "claude-mock-1"
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
_REAL_GETADDRINFO = socket.getaddrinfo


def fake_key() -> str:
    return "sk" + "-test-" + pysecrets.token_hex(12)


@contextlib.contextmanager
def routed(http_ok=True):
    """FAKE_HOST is public to the guard and 127.0.0.1 to the socket layer. ``http_ok`` lets a declared remote endpoint
    use the mock's plain http (``llm.REMOTE_SCHEMES`` is a module constant the tests patch, never config)."""
    def gai(host, *a, **kw):
        return _REAL_GETADDRINFO("127.0.0.1" if host == FAKE_HOST else host, *a, **kw)
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch("second_scribe.llm._resolve", side_effect=lambda h, p: [ip(104, 18, 0, 1)]))
        st.enter_context(mock.patch("socket.getaddrinfo", side_effect=gai))
        if http_ok:
            st.enter_context(mock.patch.object(llm, "REMOTE_SCHEMES", ("http", "https")))
        yield


def entry(role, host, provider):
    return {"role": role, "host": host, "provider": provider, "declaration_sha256": "ab" * 32}


def clean_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
    env.update(extra)
    return env


def records(log: Path, kind: str | None = None) -> list[dict]:
    out = [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()]
    return [r for r in out if kind is None or r["record_type"] == kind]


class TLSMock(MockModels):
    """The mock behind TLS, with a throwaway self-signed certificate for FAKE_HOST made by ``openssl`` in the test's
    temp folder; ``trusted()`` makes the kit's https client trust that certificate (and only it) for the block."""

    cert: str = ""

    def server_bind(self):
        super().server_bind()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.socket = ctx.wrap_socket(self.socket, server_side=True)

    @classmethod
    def make(cls, test, tmp: Path) -> "TLSMock":
        cert, key = tmp / "tls-cert.pem", tmp / "tls-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-keyout", str(key),
                        "-out", str(cert), "-subj", f"/CN={FAKE_HOST}", "-addext", f"subjectAltName=DNS:{FAKE_HOST}"],
                       check=True, capture_output=True, timeout=60)
        srv = cls([DEMO_CONFIG])
        srv.cert, srv.key = str(cert), str(key)
        srv.start()
        test.addCleanup(srv.stop)
        return srv

    @contextlib.contextmanager
    def trusted(self):
        """The kit's opener (no proxy, no redirects) with an https handler that trusts this certificate only."""
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), llm._RefuseRedirects,
                                             urllib.request.HTTPSHandler(
                                                 context=ssl.create_default_context(cafile=self.cert)))
        with mock.patch("second_scribe.anthropic_client._OPENER", opener), mock.patch.object(llm, "_OPENER", opener):
            yield


class _Mock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = MockModels([DEMO_CONFIG])
        cls.url = cls.srv.start()
        cls.addClassCleanup(cls.srv.stop)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.srv.requests.clear()
        self.srv.set(canary_fail=False)
        self.addCleanup(self.srv.set, canary_fail=False)


# ------------------------------------------------------------------ K13 allow-list through the model check

class TestRemotePairModelCheck(_Mock):
    """A pair whose judge is a declared remote Anthropic endpoint (the mock's /v1/messages behind FAKE_HOST)."""

    def clients(self):
        port = self.srv.server_address[1]
        recon = HTTPClient(ModelConfig(name="recon", endpoint=self.url + "/v1", model_id=RECON_ID,
                                       params=dict(THINK_OFF)))
        judge = AnthropicClient(ModelConfig(name="judge", endpoint=f"http://{FAKE_HOST}:{port}/v1", model_id=CLAUDE_ID,
                                            provider="anthropic"))
        return {"recon": recon, "judge": judge}, f"{FAKE_HOST}:{port}"

    def test_remote_pair_passes_guard_only_with_matching_entry(self):
        clients, host = self.clients()
        fam = {"recon": "qwen", "judge": "claude"}
        bad = [None, [], [entry("recon", host, "anthropic")], [entry("judge", "other.mock.invalid", "anthropic")],
               [entry("judge", host, "openai_compat")]]
        with routed():
            for eps in bad:
                with self.subTest(eps=eps), self.assertRaises(EgressRefused):
                    model_check.run_model_check(self.tmp / "mc", clients, "Q", families=fam, remote_endpoints=eps)
            self.assertEqual(self.srv.requests, [])                     # refused before any call
            good = [entry("judge", host, "anthropic")]
            q = model_check.run_model_check(self.tmp / "mc", clients, "Q", families=fam, remote_endpoints=good)
            self.assertEqual(q["state"], "q_done")
            doc = model_check.run_model_check(self.tmp / "mc", clients, "J", families=fam, remote_endpoints=good)
        self.assertIs(doc["passed"], True, doc["failures"])
        self.assertEqual(doc["models"], {"recon": RECON_ID, "judge": CLAUDE_ID})
        self.assertEqual(doc["remote_endpoints"], good)                 # the declaration the check ran under
        paths = {r["path"] for r in self.srv.requests}
        self.assertIn("/v1/messages", paths)                            # the judge really went through the remote client
        ok, detail = verify_run(str(model_check.log_path(self.tmp / "mc")), None, None, False)
        self.assertTrue(ok, detail)

    def test_remote_needs_https_even_when_declared(self):
        clients, host = self.clients()
        with routed(http_ok=False), self.assertRaises(EgressRefused) as cm:
            model_check.run_model_check(self.tmp / "mc", clients, "Q", families={"recon": "qwen", "judge": "claude"},
                                        remote_endpoints=[entry("judge", host, "anthropic")])
        self.assertIn("https", str(cm.exception))

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make a throwaway test certificate")
    def test_declared_https_remote_pair_checked(self):
        """K6 (audit): the judge is a declared remote Anthropic endpoint on real https (a TLS-wrapped mock with a
        throwaway certificate for FAKE_HOST; the https-only rule is NOT patched). Undeclared it is refused before any
        call; declared, the pair is checked (Q, then J) and passes."""
        tls = TLSMock.make(self, self.tmp)
        host = f"{FAKE_HOST}:{tls.server_address[1]}"
        recon = HTTPClient(ModelConfig(name="recon", endpoint=self.url + "/v1", model_id=RECON_ID,
                                       params=dict(THINK_OFF)))
        judge = AnthropicClient(ModelConfig(name="judge", endpoint=f"https://{host}/v1", model_id=CLAUDE_ID,
                                            provider="anthropic"))
        clients, fam = {"recon": recon, "judge": judge}, {"recon": "qwen", "judge": "claude"}
        good = [entry("judge", host, "anthropic")]
        with routed(http_ok=False), tls.trusted():
            self.assertEqual(llm.REMOTE_SCHEMES, ("https",))
            for eps in (None, [entry("judge", "other.mock.invalid", "anthropic")]):
                with self.subTest(eps=eps), self.assertRaises(EgressRefused):
                    model_check.run_model_check(self.tmp / "mc", clients, "Q", families=fam, remote_endpoints=eps)
            self.assertEqual((self.srv.requests, tls.requests), ([], []))
            model_check.run_model_check(self.tmp / "mc", clients, "Q", families=fam, remote_endpoints=good)
            doc = model_check.run_model_check(self.tmp / "mc", clients, "J", families=fam, remote_endpoints=good)
        self.assertIs(doc["passed"], True, doc["failures"])
        self.assertTrue(any(r["path"] == "/v1/messages" for r in tls.requests))      # over TLS, to the remote judge
        self.assertEqual({r["model"] for r in self.srv.requests if r["path"] == "/v1/chat/completions"}, {RECON_ID})

    def test_local_pair_doc_unchanged_without_allowlist(self):
        """No allow-list (every study path): the document has no ``remote_endpoints`` key, as before."""
        clients = {r: HTTPClient(ModelConfig(name=r, endpoint=self.url + "/v1", model_id=m, params=dict(THINK_OFF)))
                   for r, m in (("recon", RECON_ID), ("judge", JUDGE_ID))}
        model_check.run_model_check(self.tmp / "mc", clients, "Q")
        doc = model_check.run_model_check(self.tmp / "mc", clients, "J")
        self.assertNotIn("remote_endpoints", doc)

    def cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, clean_env(env), clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_cli_remote_endpoints_flag(self):
        port = self.srv.server_address[1]
        host = f"{FAKE_HOST}:{port}"
        env = {"SS_RECON_URL": self.url + "/v1", "SS_RECON_MODEL": RECON_ID, "SS_RECON_PARAMS": json.dumps(THINK_OFF),
               "SS_JUDGE_URL": f"http://{host}/v1", "SS_JUDGE_MODEL": CLAUDE_ID, "SS_JUDGE_PROVIDER": "anthropic",
               "SS_JUDGE_PARAMS": "{}"}
        base = ["model-check", "--out", str(self.tmp / "mc"), "--stages", "Q", "--families", "recon=qwen,judge=claude"]
        good, wrong = self.tmp / "eps.json", self.tmp / "wrong.json"
        good.write_text(json.dumps([entry("judge", host, "anthropic")]))
        wrong.write_text(json.dumps([entry("judge", host, "openai_compat")]))
        broken = self.tmp / "broken.json"
        broken.write_text(json.dumps([{"role": "judge", "host": host}]))
        with routed():
            rc, _, err = self.cli(base, env)                                    # no flag: refused, exit 2
            self.assertEqual(rc, 2, err)
            self.assertIn("refused", err)
            rc, _, err = self.cli(base + ["--remote-endpoints", str(wrong)], env)
            self.assertEqual(rc, 2, err)
            rc, _, err = self.cli(base + ["--remote-endpoints", str(broken)], env)
            self.assertEqual(rc, 2, err)
            self.assertEqual(self.srv.requests, [])
            rc, out, err = self.cli(base + ["--remote-endpoints", str(good)], env)
        self.assertEqual(rc, 0, err)
        self.assertIn("Q session done", out)



# ------------------------------------------------------------------ audit K7: opt-ins through the check and the probe

LAN_NAME = "lan-box.mock.invalid"


@contextlib.contextmanager
def lan_routed():
    """LAN_NAME is a LAN address to the guard's classifier and 127.0.0.1 to the socket layer (built at run time)."""
    def gai(host, *a, **kw):
        return _REAL_GETADDRINFO("127.0.0.1" if host == LAN_NAME else host, *a, **kw)
    with mock.patch("second_scribe.llm._resolve", side_effect=lambda h, p: [ip(192, 168, 7, 20)]), \
            mock.patch("socket.getaddrinfo", side_effect=gai):
        yield


class TestEndpointOptins(_Mock):
    """The model check and the timing probe hand the project's ``endpoint_optins`` to the guard the way they hand it
    ``remote_endpoints``: a LAN or Tailscale host is refused without an opt-in for that role and host, accepted with
    one. Not passed (every study path) keeps today's guard."""

    def host(self):
        return f"{LAN_NAME}:{self.srv.server_address[1]}"

    def recon(self):
        return {"recon": HTTPClient(ModelConfig(name="recon", endpoint=f"http://{self.host()}/v1", model_id=RECON_ID,
                                                params=dict(THINK_OFF)))}

    def env(self):
        return {"SS_RECON_URL": f"http://{self.host()}/v1", "SS_RECON_MODEL": RECON_ID,
                "SS_RECON_PARAMS": json.dumps(THINK_OFF)}

    def cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, clean_env(env), clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_run_model_check_needs_the_optin(self):
        with lan_routed():
            for bad in ([], [{"role": "judge", "host": self.host()}], [{"role": "recon", "host": "other:1"}]):
                with self.subTest(optins=bad), self.assertRaises(EgressRefused) as cm:
                    model_check.run_model_check(self.tmp / "mc", self.recon(), "Q", endpoint_optins=bad)
                self.assertIn("no opt-in", str(cm.exception))
            self.assertEqual(self.srv.requests, [])                              # refused before any call
            doc = model_check.run_model_check(self.tmp / "mc", self.recon(), "Q",
                                              endpoint_optins=[{"role": "recon", "host": self.host()}])
            self.assertIs(doc["passed"], True, doc["failures"])
            self.assertTrue(self.srv.requests)
            self.srv.requests.clear()
            model_check.run_model_check(self.tmp / "mc2", self.recon(), "Q")      # not passed: today's guard

    def test_cli_endpoint_optins_flag(self):
        base = ["model-check", "--out", str(self.tmp / "mc"), "--stages", "Q"]
        none, good, broken = self.tmp / "none.json", self.tmp / "good.json", self.tmp / "broken.json"
        none.write_text("[]")
        good.write_text(json.dumps([{"role": "recon", "host": self.host()}]))
        broken.write_text(json.dumps([{"role": "recon"}]))
        with lan_routed():
            rc, _, err = self.cli(base + ["--endpoint-optins", str(none)], self.env())
            self.assertEqual(rc, 2, err)
            self.assertIn("no opt-in", err)
            rc, _, err = self.cli(base + ["--endpoint-optins", str(broken)], self.env())
            self.assertEqual(rc, 2, err)
            self.assertIn("--endpoint-optins", err)
            self.assertEqual(self.srv.requests, [])
            rc, out, err = self.cli(base + ["--endpoint-optins", str(good)], self.env())
            self.assertEqual(rc, 0, err)
            rc, _, err = self.cli(["model-check", "--out", str(self.tmp / "mc3"), "--stages", "Q"], self.env())
            self.assertEqual(rc, 0, err)                                       # no flag: today's guard

    def test_probe_child_needs_the_optin(self):
        pid = timing.pick_demo(500)
        base = ["probe", "--pid", pid, "--out", str(self.tmp / "probe")]
        none, good = self.tmp / "none.json", self.tmp / "good.json"
        none.write_text("[]")
        good.write_text(json.dumps([{"role": "recon", "host": self.host()}]))
        out, err = io.StringIO(), io.StringIO()
        with lan_routed(), mock.patch.dict(os.environ, clean_env(self.env()), clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(timing.main(base + ["--endpoint-optins", str(none)]), 2)
            self.assertIn("no opt-in", err.getvalue())
            self.assertEqual(self.srv.requests, [])
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(timing.main(base + ["--endpoint-optins", str(good)]), 0, err.getvalue())
            self.assertTrue(self.srv.requests)
            self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1])["status"], "ok")


# ------------------------------------------------------------------ call counts

def oracle_demo_log(tmp: Path) -> Path:
    """configs/demo.json, every stage, oracle client: the log the counts are checked against."""
    from second_scribe.audit.registry import AuditLog
    cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(tmp / "demo"))
    (tmp / "demo" / "logs").mkdir(parents=True)
    log = AuditLog(tmp / "demo" / "logs" / "run.jsonl", cfg.run_id)
    try:
        pipeline.run(cfg, pipeline.STAGE_ORDER, build_clients("oracle", cfg), log=log)
    finally:
        log.close()
    return tmp / "demo" / "logs" / "run.jsonl"


class TestCounts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.log = oracle_demo_log(Path(cls._tmp.name))

    def test_count_calls_matches_oracle_log(self):
        from second_scribe.app import timing
        cfg = RunConfig.load(DEMO_CONFIG)
        self.assertEqual(cfg.sha256(), DEMO_HASH)
        per_stage, per_group = {}, {"Q": 0, "J": 0}
        for r in records(self.log, "call_request"):
            k = timing.stage_key(r["payload"]["stage"])
            per_stage[k] = per_stage.get(k, 0) + 1
            per_group[timing.group(k)] += 1
        n = len(pipeline.load_participants(cfg))
        got = timing.count_calls(cfg, n, overhead=False)
        self.assertEqual(got["Q"], per_group["Q"])                          # exact for Q
        self.assertLessEqual(abs(got["J"] - per_group["J"]), 0.10 * per_group["J"])   # J within 10%
        self.assertEqual(got["M"], 0)
        by = timing.stage_calls(cfg, n)
        for k in per_stage:
            if timing.group(k) == "Q":
                self.assertEqual(by[k], per_stage[k], k)
        # each session's preflight and determinism re-asks (not in the main log) come on top
        full = timing.count_calls(cfg, n)
        self.assertEqual(full["Q"] - got["Q"], timing.PREFLIGHT_CALLS["recon"] + 20)
        self.assertEqual(full["J"] - got["J"], timing.PREFLIGHT_CALLS["judge"] + 20)
        with_check = timing.count_calls(cfg, n, model_check=True)
        mc = timing.model_check_calls()                       # every call of the lean model check (item 36)
        self.assertEqual((with_check["Q"] - full["Q"], with_check["J"] - full["J"]), (mc["Q"], mc["J"]))
        self.assertEqual(mc, {"Q": 20, "J": 39})
        self.assertEqual(timing.count_calls(cfg, n, judge=False)["J"], 0)
        self.assertEqual(timing.count_calls(cfg, n, model_check=True, judge=False)["J"], 0)

    def test_model_check_parts(self):
        """The lean check (item 36): rung 2 and the reading-3 map on six cases. Logged calls, determinism re-asks
        (k = 6 of the check config, all calls at T 0) and the judge check, by part; the reconstructor part alone for a
        pair with no judge."""
        from second_scribe.app import timing
        self.assertEqual(timing.model_check_stages(), {
            "q:mapping_talk": 6, "recon": 6, "recon:rungs": 0, "prior_only": 2, "judge:in_transcript": 6,
            "judge:modality": 6, "judge:shuffled": 6, "judge:align": 12, "judge:canon": 2})
        self.assertEqual(timing.model_check_parts(), {
            "Q": {"role": "recon", "calls": 14, "determinism": 6, "judge_check": 0, "total": 20},
            "J": {"role": "judge", "calls": 32, "determinism": 6, "judge_check": 1, "total": 39}})
        self.assertEqual(timing.model_check_parts(judge=False), {"Q": timing.model_check_parts()["Q"]})
        self.assertEqual(timing.model_check_calls(judge=False), {"Q": 20, "J": 0})
        t = timing.model_check_time()
        self.assertLess(0, t["low_s"])
        self.assertLess(t["low_s"], t["high_s"])
        self.assertEqual(round(t["low_s"] / timing.RANGE[0]), round(t["high_s"] / timing.RANGE[1]))
        self.assertEqual(set(t["per_part"]), {"Q", "J"})
        self.assertEqual(set(timing.model_check_time(judge=False)["per_part"]), {"Q"})
        with mock.patch.object(timing, "recorded_profile", return_value={}):
            self.assertIsNone(timing.model_check_time())               # no recorded basis: no time is made up

    def test_standard_shape_counts(self):
        """Standard: primary spec, rung 1 + rung 2 (rung 2 draw 0 is the main map), 5 prior-only samples per role plus
        the reading-3 map of each role (presets/standard.json's reading3 block)."""
        from second_scribe.app import timing
        cfg = timing.standard_config(["rung1"])
        self.assertEqual(sorted(cfg.ladder["rungs"]), ["rung1", "rung2"])
        by = timing.stage_calls(cfg, 10, n_roles=2)
        self.assertEqual((by["q:mapping_talk"], by["recon"], by["recon:rungs"], by["prior_only"]), (10, 10, 10, 12))
        self.assertEqual(by["judge:align"], 10 * 3 * 3 + 10 * 3)               # 3 AI maps per person, 3 comparisons
        self.assertEqual(sorted(timing.standard_config(["rung3"]).ladder["rungs"]), ["placebo", "rung2", "rung3"])
        with self.assertRaises(ValueError):
            timing.check_rungs(["placebo"])

    def test_recorded_profile_is_counts_and_seconds_only(self):
        from second_scribe.app import timing
        prof = timing.recorded_profile()
        self.assertIn("recon", prof)
        self.assertGreater(prof["recon"]["seconds"], 0)
        for k, v in prof.items():
            self.assertEqual(set(v), {"seconds", "chars"}, k)


class TestModelCheckCountsLive(_Mock):
    """Item 36: every request the tests' stand-in server receives during a whole lean model check, by part, against
    ``timing.model_check_parts`` (logged calls per stage, determinism re-asks, judge check)."""

    def clients(self, roles=("recon", "judge")):
        ids = {"recon": RECON_ID, "judge": JUDGE_ID}
        return {r: HTTPClient(ModelConfig(name=r, endpoint=self.url + "/v1", model_id=ids[r], params=dict(THINK_OFF)))
                for r in roles}

    def chats(self, n0):
        return [r for r in self.srv.requests[n0:] if r["path"] == "/v1/chat/completions"]

    def test_model_check_counts_match_the_stand_in(self):
        from second_scribe.app import timing
        out = self.tmp / "mc"
        parts = timing.model_check_parts()
        got = {}
        for part in ("Q", "J"):
            n0 = len(self.srv.requests)
            doc = model_check.run_model_check(out, self.clients(), part)
            got[part] = self.chats(n0)
        self.assertIs(doc["passed"], True, doc)
        self.assertEqual((doc["counts"]["wished"]["n"], doc["counts"]["negated"]["n"]), (6, 6))
        self.assertEqual(doc["counts"]["n_rows"], 36)                       # six cases, six traps each, rung 2
        self.assertEqual({r["model"] for r in got["Q"]}, {RECON_ID})
        self.assertEqual({r["model"] for r in got["J"]}, {JUDGE_ID})
        self.assertEqual((len(got["Q"]), len(got["J"])), (parts["Q"]["total"], parts["J"]["total"]))
        self.assertEqual((len(got["Q"]), len(got["J"])), (20, 39))
        recs = [json.loads(line) for line in model_check.log_path(out).read_text().splitlines()]
        logged: dict = {}
        for r in recs:
            if r["record_type"] == "call_request":
                k = timing.stage_key(r["payload"]["stage"])
                logged[k] = logged.get(k, 0) + 1
        self.assertEqual(logged, {k: v for k, v in timing.model_check_stages().items() if v})
        jc = [json.loads(line)["record_type"] for line in model_check.judge_check_log(out).read_text().splitlines()]
        self.assertEqual(jc.count("call_request"), parts["J"]["judge_check"])
        n_logged = {g: sum(v for k, v in logged.items() if timing.group(k) == g) for g in ("Q", "J")}
        self.assertEqual(len(got["Q"]) - n_logged["Q"], parts["Q"]["determinism"])           # not in any log
        self.assertEqual(len(got["J"]) - n_logged["J"] - parts["J"]["judge_check"], parts["J"]["determinism"])
        # the progress reads "determinism" after the last stage_end of each part, before its audit_summary
        starts = [i for i, r in enumerate(recs) if r["record_type"] == "run_start"]
        for part, (a, b) in zip(("Q", "J"), ((starts[0], starts[1]), (starts[1], len(recs)))):
            seg = [r["record_type"] if r["record_type"] != "stage_end" else ("stage_end", r["payload"]["stage"])
                   for r in recs[a:b]]
            ends = [x for x in seg if isinstance(x, tuple)]
            self.assertEqual(ends[-1], ("stage_end", timing.CHECK_LAST_STAGE[part]), part)
            self.assertEqual(seg[seg.index(ends[-1]) + 1:], ["audit_summary", "run_end"], part)

    def test_a_pair_without_a_judge_checks_the_reconstructor_part_alone(self):
        from second_scribe.app import timing
        n0 = len(self.srv.requests)
        doc = model_check.run_model_check(self.tmp / "mc", self.clients(("recon",)), "Q")
        self.assertIs(doc["passed"], True, doc)
        self.assertEqual(doc["counts"]["recon"]["n"], 6)
        self.assertEqual(len(self.chats(n0)), timing.model_check_parts(judge=False)["Q"]["total"])


class TestServerSignal(_Mock):
    """F14: a model server that stops answering shows in ``LogTail.server`` (shared by the model check and the runs)
    as a code and the seconds since the last answer; the error text itself is never kept."""
    T0 = 1_800_000_000.0

    def log(self, *recs):
        from datetime import datetime, timezone
        path = self.tmp / "logs" / "run.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for kind, dt, payload in recs:
                ts = datetime.fromtimestamp(self.T0 + dt, tz=timezone.utc).isoformat()
                fh.write(json.dumps({"record_type": kind, "seq": 0, "ts": ts, "payload": payload}) + "\n")
        return path

    def test_transport_errors_give_a_code_and_the_seconds_since_the_last_answer(self):
        from second_scribe.app import jobs
        err = "TransportFailure('model call to 127.0.0.1:9 failed after 4 attempts: HTTP 503')"
        path = self.log(("run_start", 0, {}), ("call_request", 1, {"request_id": "a", "stage": "recon:removed"}),
                        ("call_response", 5, {"request_id": "a", "text": "secret answer"}),
                        ("call_request", 6, {"request_id": "b", "stage": "recon:removed"}),
                        ("call_error", 20, {"request_id": "b", "error": err, "transport": True}),
                        ("call_request", 21, {"request_id": "c", "stage": "recon:removed"}))
        tail = jobs.LogTail(path, started=self.T0).read()
        self.assertEqual(tail.server(at=self.T0 + 45), {"code": "http_503", "seconds": 40, "errors": 1})
        kept = json.dumps(vars(tail), default=str)
        self.assertNotIn("TransportFailure", kept)
        self.assertNotIn("secret", kept)
        self.log(("call_error", 40, {"request_id": "c", "error": "TransportFailure('... timed out')",
                                     "transport": True}))
        self.assertEqual(tail.read().server(at=self.T0 + 45), {"code": "timeout", "seconds": 40, "errors": 2})
        self.log(("call_request", 41, {"request_id": "d", "stage": "recon:removed"}),
                 ("call_response", 50, {"request_id": "d", "text": "ok"}))
        self.assertIsNone(tail.read().server(at=self.T0 + 55))                  # answering again
        self.log(("call_request", 51, {"request_id": "e", "stage": "recon:removed"}),
                 ("call_error", 52, {"request_id": "e", "error": "ValueError('bad json')"}))
        self.assertIsNone(tail.read().server(at=self.T0 + 55))                  # a bad answer is not an outage

    def test_no_reply_for_the_quiet_time(self):
        from second_scribe.app import jobs
        path = self.log(("run_start", 0, {}), ("call_request", 1, {"request_id": "a", "stage": "judge:align"}))
        tail = jobs.LogTail(path, started=self.T0).read()
        self.assertIsNone(tail.server(at=self.T0 + 1 + jobs.SERVER_QUIET_S - 1))
        self.assertEqual(tail.server(at=self.T0 + 1 + jobs.SERVER_QUIET_S),
                         {"code": "no_reply", "seconds": int(1 + jobs.SERVER_QUIET_S), "errors": 0})
        self.log(("run_start", 300, {}))                                      # a new session: that call is over
        self.assertIsNone(tail.read().server(at=self.T0 + 2000))

    def test_a_session_tail_reads_only_what_comes_after(self):
        from second_scribe.app import jobs
        path = self.log(("run_start", 0, {}), ("call_request", 1, {"request_id": "a", "stage": "recon:removed"}),
                        ("call_response", 2, {"request_id": "a"}), ("audit_summary", 3, {}))
        tail = jobs.LogTail.at_end(path, started=self.T0 + 4)
        self.assertEqual((tail.read().counts, tail.marks["audit_summary"]), ({"Q": 0, "J": 0}, 0))
        self.log(("run_start", 5, {}), ("call_request", 6, {"request_id": "b", "stage": "judge:modality"}),
                 ("call_response", 7, {"request_id": "b"}), ("stage_end", 8, {"stage": "judge_modality"}))
        tail.read()
        self.assertEqual((tail.counts, tail.marks["run_start"]), ({"Q": 0, "J": 1}, 1))
        self.assertIn("judge_modality", tail.stage_ends)
        self.assertEqual(jobs.LogTail.at_end(self.tmp / "missing.jsonl").read().counts, {"Q": 0, "J": 0})

    def test_codes(self):
        from second_scribe.app import jobs
        self.assertEqual(jobs.transport_code("failed after 4 attempts: HTTP 502"), "http_502")
        self.assertEqual(jobs.transport_code("URLError: <urlopen error [Errno 61] Connection refused>"),
                         "no_connection")
        self.assertEqual(jobs.transport_code("TimeoutError: timed out"), "timeout")
        self.assertEqual(jobs.transport_code("IncompleteRead(0 bytes read)"), "transport")
        self.assertEqual(jobs.transport_code(None), "transport")

    def test_the_kit_logs_an_outage_the_tail_reads(self):
        """The real record shape: ``llm.log_call`` against the stand-in answering HTTP 503 (no wait between tries)."""
        from second_scribe.app import jobs
        from second_scribe.audit.registry import AuditLog
        path = self.tmp / "real" / "run.jsonl"
        path.parent.mkdir(parents=True)
        log = AuditLog(path, "t")
        client = HTTPClient(ModelConfig(name="recon", endpoint=self.url + "/v1", model_id=RECON_ID,
                                        params=dict(THINK_OFF)), sleep=lambda s: None)
        self.srv.set(status=503)
        self.addCleanup(self.srv.set, status=None)
        try:
            text, _ = llm.log_call(log, client, [{"role": "user", "content": "hello"}], stage="recon:removed",
                                   item_id="x", temperature=0.0, seed=1, max_tokens=10, max_attempts=2)
        finally:
            log.close()
        self.assertIsNone(text)
        sig = jobs.LogTail(path).read().server()
        self.assertEqual((sig["code"], sig["errors"]), ("http_503", 2))
        self.assertIsInstance(sig["seconds"], int)


class TestRecordedProfileLiveOnly(unittest.TestCase):
    """``recorded_profile`` times only live responses (``latency_ms`` > 0) and pools the live latencies of its logs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def log(self, name, calls):
        """A run log with one request/response pair per ``(stage, latency_ms, reply text)``."""
        lines = []
        for i, (stage, latency, text) in enumerate(calls):
            rid = f"{name}-{i}"
            lines.append({"record_type": "call_request",
                          "payload": {"request_id": rid, "stage": stage, "messages": [{"content": "abcd"}]}})
            lines.append({"record_type": "call_response",
                          "payload": {"request_id": rid, "latency_ms": latency, "text": text}})
        path = self.dir / name
        path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
        return path

    def test_a_stage_with_only_replayed_responses_has_no_seconds(self):
        from second_scribe.app import timing
        p = self.log("replayed.jsonl", [("recon:p1", 0, "xx"), ("recon:p2", 0, "xxxx"), ("prior_only", 2000, "y")])
        prof = timing.recorded_profile(p)
        self.assertIsNone(prof["recon"]["seconds"])
        self.assertEqual(prof["recon"]["chars"], 7)                # replayed responses still count for size: 4 + 2, 4 + 4
        self.assertEqual(prof["prior_only"]["seconds"], 2.0)

    def test_replayed_responses_do_not_pull_the_median_down(self):
        from second_scribe.app import timing
        p = self.log("mixed.jsonl", [("recon:p1", 0, ""), ("recon:p2", 0, ""), ("recon:p3", 4000, "")])
        self.assertEqual(timing.recorded_profile(p)["recon"]["seconds"], 4.0)

    def test_pooling_two_logs_gives_the_median_of_their_live_latencies(self):
        from second_scribe.app import timing
        a = self.log("a.jsonl", [("recon:p1", 0, "aa"), ("recon:p2", 1000, "aa"), ("recon:p3", 3000, "aa")])
        b = self.log("b.jsonl", [("recon:p1", 5000, "bbbb"), ("recon:p2", 9000, "bbbb")])
        prof = timing.recorded_profile((a, b))
        self.assertEqual(prof["recon"]["seconds"], 4.0)            # median of 1, 3, 5, 9
        self.assertEqual(prof["recon"]["chars"], 6)                # every response of both logs: 6, 6, 6, 8, 8

    def test_a_single_path_string_still_works(self):
        from second_scribe.app import timing
        a = self.log("one.jsonl", [("recon:p1", 2000, "")])
        self.assertEqual(timing.recorded_profile(str(a))["recon"]["seconds"], 2.0)

    def test_a_missing_or_unreadable_log_gives_no_profile(self):
        from second_scribe.app import timing
        self.assertEqual(timing.recorded_profile(str(self.dir / "absent.jsonl")), {})
        bad = self.dir / "bad.jsonl"
        bad.write_text('{"record_type": "call_request" broken\n', encoding="utf-8")
        self.assertEqual(timing.recorded_profile(bad), {})


# ------------------------------------------------------------------ the job runner

class TestJob(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def wait(self, job, timeout=60):
        t0 = time.time()
        while job.poll()["state"] == "running":
            self.assertLess(time.time() - t0, timeout)
            time.sleep(0.05)
        return job.poll()

    def test_job_runs_the_kit_interpreter_in_the_kit_root(self):
        from second_scribe.app import jobs
        code = "import os, sys; print(os.getcwd()); print(sys.executable); sys.stderr.write('e'); sys.exit(0)"
        job = jobs.Job(["-c", code], jobs.child_env({}), KIT_ROOT, self.tmp / "run" / "logs" / "run.jsonl").start()
        self.assertEqual(job.argv[0], sys.executable)
        p = self.wait(job)
        self.assertEqual((p["state"], p["rc"]), ("done", 0))
        out = (self.tmp / "run" / "job" / "stdout.txt").read_text().splitlines()
        self.assertEqual(Path(out[0]).resolve(), KIT_ROOT.resolve())
        self.assertEqual(out[1], sys.executable)
        self.assertEqual((self.tmp / "run" / "job" / "stderr.txt").read_text(), "e")
        bad = jobs.Job(["-c", "raise SystemExit(4)"], jobs.child_env({}), KIT_ROOT, self.tmp / "b" / "l.jsonl",
                       job_dir=self.tmp / "b" / "job").start()
        self.assertEqual(self.wait(bad)["state"], "failed")
        self.assertEqual(jobs.EXIT_CODES[bad.rc], "stopped")
        slow = jobs.Job(["-c", "import time; time.sleep(60)"], jobs.child_env({}), KIT_ROOT, self.tmp / "s" / "l",
                        job_dir=self.tmp / "s" / "job").start()
        self.assertEqual(slow.poll()["state"], "running")
        slow.terminate()
        self.assertEqual(slow.poll()["state"], "paused")
        with self.assertRaises(TypeError):
            jobs.Job("-c pass", {}, KIT_ROOT, self.tmp / "x")

    def test_progress_counts_call_responses_without_payloads(self):
        from second_scribe.app import jobs
        log = self.tmp / "logs" / "run.jsonl"
        log.parent.mkdir(parents=True)
        job = jobs.Job(["-c", "pass"], {}, KIT_ROOT, log, total=7)
        rows = [{"record_type": "call_request", "payload": {"x": 1}},
                {"record_type": "call_response", "payload": {"text": "secret answer"}}]
        log.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows * 2) + '{"record_type":"call_res')
        self.assertEqual(job.poll()["progress"], {"done": 2, "total": 7})
        with open(log, "a") as fh:                           # the half-written line completes
            fh.write('ponse"}\n')
        self.assertEqual(job.poll()["progress"]["done"], 3)
        self.assertNotIn("secret", json.dumps(job.poll()))

    def test_child_env_drops_callers_ss_variables(self):
        from second_scribe.app import jobs
        with mock.patch.dict(os.environ, {"SS_RECON_URL": "http://stray", "MY_KEY_VAR": "kept", "SS_X": "y"}):
            env = jobs.child_env({"SS_RECON_URL": "http://127.0.0.1:1/v1", "SS_JUDGE_KEY_ENV": "MY_KEY_VAR"})
        self.assertEqual(env["SS_RECON_URL"], "http://127.0.0.1:1/v1")
        self.assertNotIn("SS_X", env)
        self.assertEqual(env["MY_KEY_VAR"], "kept")


# ------------------------------------------------------------------ the app: model check flow and estimate

from second_scribe.app import api, jobs, models, timing  # noqa: E402
from second_scribe.app.project import Project  # noqa: E402
from tests import test_server_security as tss  # noqa: E402

MARKER = "Zebraquill"          # a word in the user's transcripts that no request may carry


def role_entry(url, model, family, *, kind="local", provider="openai", address_class="loopback", digest=None):
    return {"kind": kind, "provider": provider, "url": url, "host": llm.endpoint_host(url),
            "address_class": address_class, "model": model, "family": family, "family_detected": family,
            "digest": digest, "server": "ollama",
            "connection": {"ok": True, "ms": 5, "thinking_seen": False, "reply_ok": True, "error": None}}


class AppCase(tss.ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockModels([DEMO_CONFIG])
        cls.url = cls.mock.start()
        cls.addClassCleanup(cls.mock.stop)

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.mock.requests.clear()
        self.mock.set(canary_fail=False)
        self.addCleanup(self.mock.set, canary_fail=False)
        self.cookie = self.login()
        self.token = self.csrf(self.cookie)
        self.bodies = []
        st, _, data = self.call("/api/project/create", {"path": str(self.tmp / "study"), "name": "Study"})
        self.assertEqual(st, 200, data)
        self.project = self.state.project
        self.addCleanup(self.stop_jobs)

    def stop_jobs(self):
        live = self.state.jobs.get("model_check")
        if live is not None:
            live.stop()

    def call(self, path, obj=None):
        if obj is None:
            st, headers, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        else:
            st, headers, data = self.post(path, obj, self.cookie, self.token)
        self.bodies.append(data)
        return st, headers, data

    def ok(self, path, obj=None):
        st, _, data = self.call(path, obj)
        self.assertEqual(st, 200, data)
        return json.loads(data)

    def refused(self, path, obj, status, code):
        got, _, data = self.call(path, obj)
        self.assertEqual((got, self.error_of(data)), (status, code), data)

    def set_pair(self, *, judge=True, recon_kind="local", judge_kind="local", recon_model=RECON_ID):
        digests = {m: v["digest"] for m, v in default_models().items()}
        prov = {"local": "openai", "openai_compat": "openai", "anthropic": "anthropic"}
        with self.state.lock:
            models.set_role(self.project, "recon", role_entry(self.url + "/v1", recon_model, "qwen", kind=recon_kind,
                                                              provider=prov[recon_kind],
                                                              digest=digests.get(recon_model)))
            fams = {"recon": "qwen"}
            if judge:
                models.set_role(self.project, "judge", role_entry(self.url + "/v1", JUDGE_ID, "gemma",
                                                                  kind=judge_kind, provider=prov[judge_kind],
                                                                  digest=digests[JUDGE_ID]))
                fams["judge"] = "gemma"
            models.confirm_families(self.project, fams)
            self.project.save()

    def add_transcripts(self, words=(400, 900, 2000)):
        with self.state.lock:
            for i, n in enumerate(words):
                rel = f"inputs/transcripts/case-{i}.txt"
                lines = ["Interviewer: Tell me about the permit.",
                         "Participant: " + " ".join([MARKER] + ["word"] * max(0, n - 10))]
                self.project.path(rel).write_text("\n\n".join(lines) + "\n", encoding="utf-8")
                self.project.record_file(rel, kind="transcript", pid=f"CASE-{i:02d}",
                                         labels={"participant": "Participant", "interviewer": "Interviewer"})
            self.project.save()

    def wait_check(self, until=("needs_swap", "passed", "failed", "error"), timeout=600):
        t0 = time.time()
        while True:
            check = self.ok("/api/model-check")["check"]
            if check["state"] in until:
                return check
            self.assertIn(check["state"], ("q_running", "j_running"), check)
            self.assertLess(time.time() - t0, timeout, check)
            time.sleep(0.2)

    def stderr_of(self, stage):
        live = self.state.jobs["model_check"]
        return (live.out_dir / "jobs" / stage / "stderr.txt").read_text()


class TestModelCheckFlow(AppCase):
    def test_model_check_flow_logged(self):
        self.refused("/api/model-check/start", {}, 409, "recon_missing")             # Setup step 6 first
        self.set_pair()
        reply = self.ok("/api/model-check")
        self.assertEqual(reply["check"]["state"], "not_run")
        plan = reply["plan"]                                                          # before it starts (item 33)
        self.assertEqual([(p["part"], p["role"], p["total"]) for p in plan["parts"]], [("Q", "recon", 20),
                                                                                      ("J", "judge", 39)])
        self.assertEqual((plan["total"], plan["cases"], plan["one_server"]), (59, 6, True))
        self.assertLess(plan["time"]["low_s"], plan["time"]["high_s"])
        self.assertIs(reply["swap_allowed"], False)                                    # read only while waiting
        self.refused("/api/model-check/continue", {}, 409, "not_waiting")
        check = self.ok("/api/model-check/start", {})["check"]
        self.assertEqual(check["state"], "q_running")
        self.assertEqual((check["progress"]["total"], check["progress"]["part"]), (59, "Q"))
        check = self.wait_check()
        self.assertEqual(check["state"], "needs_swap", self.stderr_of("Q"))         # one server: load the judge
        prog = check["progress"]                                                      # item 35: every call counted
        self.assertEqual(prog["parts"], [{"part": "Q", "role": "recon", "done": 20, "total": 20},
                                         {"part": "J", "role": "judge", "done": 0, "total": 39}])
        self.assertEqual((prog["done"], prog["total"], prog["now"], prog["server"]), (20, 59, "waiting_for_judge",
                                                                                     None))
        q_models = {r["model"] for r in self.mock.requests if r["path"] == "/v1/chat/completions"}
        self.assertEqual(q_models, {RECON_ID})                                        # Q: the reconstructor only
        n_q = len(self.mock.requests)
        check = self.ok("/api/model-check/continue", {})["check"]
        self.assertEqual(check["state"], "j_running")
        check = self.wait_check()
        self.assertEqual(check["state"], "passed", self.stderr_of("J"))
        j_models = {r["model"] for r in self.mock.requests[n_q:] if r["path"] == "/v1/chat/completions"}
        self.assertEqual(j_models, {JUDGE_ID})                                        # J: the judge only
        live = self.state.jobs["model_check"]
        self.assertEqual((live._part_done("Q"), live._part_done("J")), (20, 39))     # J never counts Q's calls
        self.assertEqual(len([r for r in self.mock.requests if r["path"] == "/v1/chat/completions"]), 59)

        recs = self.project.log.records("model_check")
        self.assertEqual(len(recs), 1)
        pl = recs[0]["payload"]
        doc_path = self.project.path(pl["path"])
        self.assertEqual(pl["sha256"], hashlib.sha256(doc_path.read_bytes()).hexdigest())
        doc = json.loads(doc_path.read_text())
        for k in ("passed", "counts", "models", "digests", "prompt_set_sha256"):
            self.assertEqual(pl[k], doc[k], k)
        self.assertIs(pl["passed"], True)
        self.assertEqual(pl["models"], {"recon": RECON_ID, "judge": JUDGE_ID})
        self.assertEqual(pl["pair_sha256"], models.pair_sha256(self.project))
        self.assertTrue(self.project.log.verify()[0])
        ok, detail = verify_run(str(model_check.log_path(doc_path.parent)), None, None, False)
        self.assertTrue(ok, detail)
        self.assertEqual(jobs.model_check_input(self.project), {"path": str(doc_path.resolve()), "sha256": pl["sha256"]})
        self.assertEqual(jobs.require_model_check(self.project)["sha256"], pl["sha256"])
        self.assertEqual(Project.open(self.project.root).data["model_check"]["passed"], True)
        # a passed pair is not checked again
        n = len(self.mock.requests)
        self.assertEqual(self.ok("/api/model-check/start", {})["check"]["state"], "passed")
        self.assertEqual(len(self.mock.requests), n)
        # another reconstructor is another pair: no check yet, no tool.model_check input
        self.set_pair(recon_model="qwen-mock:14b" if "qwen-mock:14b" in default_models() else "llama-mock:8b")
        self.assertEqual(self.ok("/api/model-check")["check"]["state"], "not_run")
        self.assertIsNone(jobs.model_check_input(self.project))
        with self.assertRaises(api.ApiError) as cm:
            jobs.require_model_check(self.project)
        self.assertEqual(cm.exception.code, "model_check_missing")

    def test_progress_shape_per_session(self):
        """Item 35 and F14 on a written log: one total over both parts, each part counted from where its session's
        log began, the re-asks once ``audit_summary`` is written, the judge check from its own log, the ``now`` code,
        and the server signal."""
        from datetime import datetime, timezone
        self.set_pair()
        mc = jobs.ModelCheck(self.project, self.state.secrets)
        lp, jl = model_check.log_path(mc.out_dir), model_check.judge_check_log(mc.out_dir)
        for f in (lp, jl):
            f.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time() - 1000
        n = [0]

        def write(path, kind, dt, **pl):
            ts = datetime.fromtimestamp(t0 + dt, tz=timezone.utc).isoformat()
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"record_type": kind, "seq": n[0], "ts": ts, "payload": pl}) + "\n")
            n[0] += 1

        def call(path, stage, dt):
            write(path, "call_request", dt, request_id=f"r{n[0]}", stage=stage)
            write(path, "call_response", dt + 1, request_id=f"r{n[0] - 1}")

        def prog():
            return mc.progress(at=t0 + 900)

        mc.tails["Q"], mc.state = jobs.LogTail.at_end(lp, started=t0), "q_running"
        p = prog()
        self.assertEqual((p["done"], p["total"], p["part"], p["now"], p["determinism_k"]), (0, 59, "Q", "starting", 6))
        write(lp, "run_start", 1)
        for i in range(3):
            call(lp, "recon:removed", 2 + i)
        p = prog()
        self.assertEqual((p["done"], p["now"], p["parts"][0]["done"]), (3, "model_calls", 3))
        write(lp, "stage_end", 10, stage="prior_only")
        self.assertEqual(prog()["now"], "determinism")
        write(lp, "audit_summary", 11)
        write(lp, "run_end", 11)
        self.assertEqual((prog()["done"], prog()["now"]), (9, "finishing"))         # 3 calls + 6 re-asks
        mc.frozen["Q"], mc.state = mc._part_done("Q"), "needs_swap"
        self.assertEqual((prog()["now"], prog()["server"]), ("waiting_for_judge", None))
        mc.switching = True
        self.assertEqual(prog()["now"], "switching_models")
        mc.switching = False
        mc.tails["J"] = jobs.LogTail.at_end(lp, started=t0 + 20)
        mc.judge_check_tail = jobs.LogTail.at_end(jl, started=t0 + 20)
        mc.state = "j_running"
        write(lp, "run_start", 21)
        call(lp, "judge:in_transcript", 22)
        call(lp, "judge:modality", 24)
        p = prog()
        self.assertEqual([(x["part"], x["done"], x["total"]) for x in p["parts"]], [("Q", 9, 20), ("J", 2, 39)])
        self.assertEqual((p["done"], p["total"], p["part"]), (11, 59, "J"))
        write(lp, "call_request", 30, request_id="down", stage="judge:align")
        write(lp, "call_error", 60, request_id="down", error="TransportFailure('HTTP 503')", transport=True)
        self.assertEqual(prog()["server"], {"code": "http_503", "seconds": 900 - 25, "errors": 1})
        call(lp, "judge:align", 61)
        self.assertIsNone(prog()["server"])
        write(lp, "stage_end", 70, stage="align")
        p = mc.progress(at=t0 + 70 + 2 * jobs.SERVER_QUIET_S)                    # the re-asks have gone quiet
        self.assertEqual((p["now"], p["server"]["code"]), ("determinism", "no_reply"))
        write(lp, "audit_summary", 400)
        write(lp, "run_end", 400)
        self.assertEqual(prog()["now"], "judge_check")
        call(jl, "model_check_judge:judge", 401)
        p = prog()
        self.assertEqual(p["parts"][1]["done"], 3 + 6 + 1)
        self.assertEqual(p["done"], 9 + 10)
        mc.state = "passed"
        self.assertIsNone(mc.progress())

    def test_progress_total_skips_calls_an_earlier_session_logged(self):
        """A check started again after an error resumes item by item: the calls already in the log are not counted
        again in this check's total."""
        self.set_pair()
        mc = jobs.ModelCheck(self.project, self.state.secrets)
        lp = model_check.log_path(mc.out_dir)
        lp.parent.mkdir(parents=True)
        rows = []
        for i in range(5):
            rows += [{"record_type": "call_request", "payload": {"request_id": f"q{i}", "stage": "recon:removed"}},
                     {"record_type": "call_response", "payload": {"request_id": f"q{i}"}}]
        lp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        with mock.patch.object(jobs.ModelCheck, "_run", lambda self, stages: setattr(self, "state", "q_running")):
            mc.start()
        mc.tails["Q"] = jobs.LogTail.at_end(lp)
        p = mc.progress()
        self.assertEqual(mc.base, {"Q": 5, "J": 0})
        self.assertEqual([(x["part"], x["done"], x["total"]) for x in p["parts"]], [("Q", 0, 15), ("J", 0, 39)])

    def test_continue_can_switch_the_models(self):
        """F2: from ``needs_swap``, ``continue {auto: true}`` switches the models on the pair's Ollama server (the
        stand-in's /api/generate), then runs the judge part; refused when the app may not switch them."""
        self.set_pair()
        self.ok("/api/model-check/start", {})
        check = self.wait_check()
        self.assertEqual(check["state"], "needs_swap", self.stderr_of("Q"))
        self.assertIs(self.ok("/api/model-check")["swap_allowed"], True)
        n0 = len(self.mock.requests)
        check = self.ok("/api/model-check/continue", {"auto": True})["check"]
        self.assertEqual(check["state"], "j_running")
        gen = [r["body"]["model"] for r in self.mock.requests[n0:] if r["path"] == "/api/generate"]
        self.assertEqual(gen, [RECON_ID, JUDGE_ID])                                 # unload, then load
        self.assertEqual(self.wait_check()["state"], "passed", self.stderr_of("J"))

    def test_continue_auto_refused_when_the_app_may_not_switch(self):
        self.set_pair()
        with self.state.lock:
            e = models.role(self.project, "judge")
            e["server"] = "lmstudio"
            models.set_role(self.project, "judge", e)
            models.confirm_families(self.project, {"recon": "qwen", "judge": "gemma"})
            self.project.save()
        self.ok("/api/model-check/start", {})
        self.assertEqual(self.wait_check()["state"], "needs_swap", self.stderr_of("Q"))
        self.assertIs(self.ok("/api/model-check")["swap_allowed"], False)
        self.refused("/api/model-check/continue", {"auto": True}, 409, "swap_not_allowed")
        self.assertEqual(self.ok("/api/model-check")["check"]["state"], "needs_swap")   # the manual way still works
        self.assertEqual(self.ok("/api/model-check/continue", {})["check"]["state"], "j_running")
        self.assertEqual(self.wait_check()["state"], "passed", self.stderr_of("J"))

    def test_failed_check_recorded(self):
        self.set_pair()
        self.mock.set(canary_fail=True)
        self.ok("/api/model-check/start", {})
        self.assertEqual(self.wait_check()["state"], "needs_swap", self.stderr_of("Q"))
        self.ok("/api/model-check/continue", {})
        check = self.wait_check()
        self.assertEqual(check["state"], "failed", self.stderr_of("J"))
        self.assertTrue(check["failures"])
        rec = self.project.log.records("model_check")[-1]["payload"]
        self.assertIs(rec["passed"], False)
        self.assertEqual(rec["failures"], check["failures"])
        data = Project.open(self.project.root).data["model_check"]
        self.assertIs(data["passed"], False)
        self.assertEqual(jobs.model_check_state(self.project), "failed")
        self.assertIsNone(jobs.model_check_input(self.project))
        self.refused("/api/model-check/start", {}, 409, "model_check_failed")        # no override
        with self.assertRaises(api.ApiError) as cm:
            jobs.require_model_check(self.project)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "model_check_failed"))

    def test_ai_maps_only_checks_the_reconstructor_alone(self):
        self.set_pair(judge=False)
        self.ok("/api/model-check/start", {})
        check = self.wait_check()
        self.assertEqual(check["state"], "passed", self.stderr_of("Q"))
        self.assertFalse(check["two_roles"])
        self.assertNotIn("J", [p.name for p in (self.state.jobs["model_check"].out_dir / "jobs").iterdir()])

    def test_keys_reach_the_child_only_through_its_env(self):
        self.set_pair(judge_kind="openai_compat")
        key = fake_key()
        self.state.secrets.set("judge", key)
        seen = []
        real = subprocess.Popen

        def spy(argv, *a, **kw):
            seen.append((list(argv), dict(kw.get("env") or {})))
            return real(argv, *a, **kw)
        with mock.patch("second_scribe.app.jobs.subprocess.Popen", side_effect=spy):
            self.ok("/api/model-check/start", {})
            self.assertEqual(self.wait_check()["state"], "needs_swap", self.stderr_of("Q"))
            self.ok("/api/model-check/continue", {})
            self.assertEqual(self.wait_check()["state"], "passed", self.stderr_of("J"))
        self.assertEqual(len(seen), 2)
        for argv, env in seen:
            self.assertFalse(any(key in a for a in argv), argv)
            self.assertEqual(env["SS_JUDGE_KEY_ENV"], "SS_APP_JUDGE_KEY")
            self.assertEqual(env["SS_APP_JUDGE_KEY"], key)
        for root in (self.project.root,):
            for f in root.rglob("*"):
                if f.is_file():
                    self.assertNotIn(key.encode(), f.read_bytes(), f)
        self.assertFalse(any(key.encode() in b for b in self.bodies))

    def test_remote_endpoints_reach_the_check(self):
        """A declared remote judge: the check's argv names an allow-list file holding exactly the project's
        ``remote_endpoints``; a local pair passes no such flag (the kit then refuses every non-local endpoint)."""
        self.set_pair()
        mc = jobs.ModelCheck(self.project, self.state.secrets)
        mc.out_dir.mkdir(parents=True)
        self.assertNotIn("--remote-endpoints", mc.argv("Q"))
        url = f"https://{FAKE_HOST}/v1"
        with self.state.lock, routed():
            models.set_role(self.project, "judge", role_entry(url, CLAUDE_ID, "claude", kind="anthropic",
                                                              provider="anthropic", address_class="remote"))
            models.confirm_families(self.project, {"recon": "qwen", "judge": "claude"})
            self.assertEqual(models.remote_endpoints(self.project), [])               # not declared yet
            undeclared = jobs.ModelCheck(self.project, self.state.secrets)
            undeclared.out_dir.mkdir(parents=True)
            self.assertNotIn("--remote-endpoints", undeclared.argv("Q"))               # so the kit refuses it
            with self.assertRaises(api.ApiError) as cm:
                models.require_ready(self.project, self.state.secrets)
            self.assertEqual(cm.exception.code, "declaration_missing")              # and Start is refused first
            models.log_declaration(self.project, "judge", "anthropic", FAKE_HOST, "Ethics approval covers it.", True)
            models.confirm_families(self.project, {"recon": "qwen", "judge": "claude"})
            self.project.save()
            mc = jobs.ModelCheck(self.project, self.state.secrets)
            mc.out_dir.mkdir(parents=True, exist_ok=True)
            argv = mc.argv("Q")
            eps = models.remote_endpoints(self.project)
        self.assertEqual(len(eps), 1)
        f = Path(argv[argv.index("--remote-endpoints") + 1])
        self.assertEqual(json.loads(f.read_text()), eps)
        self.assertEqual(model_check.load_remote_endpoints(f), eps)
        self.assertEqual(argv[argv.index("--families") + 1], "judge=claude,recon=qwen")
        self.assertFalse(jobs.needs_swap(self.project))                            # a remote judge: nothing to load


class TestOptinsReachTheChildren(AppCase):
    def lan_pair(self):
        self.set_pair()
        lan = ".".join(("192", "168", "7", "20"))                      # built at run time (leak scan)
        with self.state.lock:
            for r, m, f in (("recon", RECON_ID, "qwen"), ("judge", JUDGE_ID, "gemma")):
                models.set_role(self.project, r, role_entry(f"http://{lan}:11434/v1", m, f, address_class="lan"))
            models.log_optin(self.project, "recon", f"{lan}:11434")
            models.confirm_families(self.project, {"recon": "qwen", "judge": "gemma"})
            self.project.save()
        return lan

    def test_model_check_argv_names_the_optins_file(self):
        self.set_pair()
        mc = jobs.ModelCheck(self.project, self.state.secrets)
        mc.out_dir.mkdir(parents=True)
        argv = mc.argv("Q")                                             # a loopback pair: an empty list, still passed
        self.assertEqual(json.loads(Path(argv[argv.index("--endpoint-optins") + 1]).read_text()), [])
        lan = self.lan_pair()
        mc = jobs.ModelCheck(self.project, self.state.secrets)
        mc.out_dir.mkdir(parents=True, exist_ok=True)
        argv = mc.argv("Q")
        f = Path(argv[argv.index("--endpoint-optins") + 1])
        self.assertEqual(json.loads(f.read_text()), [{"role": "recon", "host": f"{lan}:11434"}])
        self.assertEqual(model_check.load_endpoint_optins(f), models.endpoint_optins(self.project))

    def test_probe_argv_names_the_optins_file(self):
        lan = self.lan_pair()
        seen = {}

        class Stop(Exception):
            pass

        class FakeJob:
            def __init__(self, argv, env, cwd, log, **kw):
                seen["argv"] = argv

            def start(self):
                raise Stop()

        with mock.patch.object(jobs, "Job", FakeJob), self.assertRaises(Stop):
            timing.probe(self.project, self.state.secrets, "SYN-OW03")
        argv = seen["argv"]
        f = Path(argv[argv.index("--endpoint-optins") + 1])
        self.assertEqual(json.loads(f.read_text()), [{"role": "recon", "host": f"{lan}:11434"}])


class TestEstimate(AppCase):
    def test_estimate_sends_no_user_text(self):
        self.set_pair()
        self.add_transcripts()
        doc = self.ok("/api/estimate", {"rungs": ["rung1"]})["estimate"]
        chats = [r for r in self.mock.requests if r["method"] == "POST"]
        self.assertEqual(len(chats), 1)                                               # one timed call
        self.assertEqual(chats[0]["model"], RECON_ID)
        sent = json.dumps(chats[0]["body"])
        self.assertNotIn(MARKER, sent)
        self.assertEqual(doc["words"]["median"], int(statistics.median(timing.user_words(self.project))))
        self.assertEqual(doc["demo_pid"], timing.pick_demo(statistics.median(timing.user_words(self.project))))
        demo = next(p for p in pipeline.load_participants(RunConfig.load(DEMO_CONFIG)) if p.pid == doc["demo_pid"])
        para = max(read_transcript_paras(demo.transcript_path), key=len)
        body = para.split("]", 1)[-1].strip()[:60]                                 # "[PT] ..." -> the words
        self.assertIn(json.dumps(body)[1:-1], sent)                                   # the shipped transcript went
        self.assertLess(doc["low_s"], doc["high_s"])
        self.assertGreater(doc["calls"]["Q"], timing.model_check_calls()["Q"])        # includes the model check
        self.assertEqual(set(doc["tokens"]), {"recon", "judge"})
        self.assertNotIn(MARKER, json.dumps(doc))

    def test_estimate_refusals(self):
        self.refused("/api/estimate", {"rungs": ["rung9"]}, 422, "bad_rungs")
        self.refused("/api/estimate", {"rungs": []}, 409, "recon_missing")
        self.set_pair()
        self.refused("/api/estimate", {"rungs": []}, 409, "no_transcripts")
        self.assertEqual([r for r in self.mock.requests if r["method"] == "POST"], [])

    def test_estimate_scales_with_length_and_calls(self):
        self.set_pair(judge=False)
        self.add_transcripts((1000,))
        fake = {"seconds": 10.0, "prompt_chars": 4000, "reply_chars": 1000, "status": "ok"}
        short = timing.estimate(self.project, [], prober=lambda pid: fake)
        self.assertEqual(short["per_stage"]["J"], 0)                                  # AI maps only: no judge
        self.assertNotIn("judge", short["tokens"])
        self.assertAlmostEqual(short["high_s"] / short["low_s"], 1.6 / 0.7, delta=0.01)     # range x0.7 .. x1.6
        total = sum(short["per_stage"].values())
        self.assertAlmostEqual(short["low_s"], 0.7 * total, delta=0.2)
        more = timing.estimate(self.project, ["rung1", "rung3"], prober=lambda pid: fake)
        self.assertGreater(more["per_stage"]["Q"], short["per_stage"]["Q"])
        slower = timing.estimate(self.project, [], prober=lambda pid: {**fake, "seconds": 20.0})
        self.assertAlmostEqual(slower["per_stage"]["Q"] / short["per_stage"]["Q"], 2.0, delta=0.01)

    def test_prior_only_maps_follow_the_projects_roles(self):
        self.set_pair(judge=False)
        self.add_transcripts((1000, 1200, 1400))
        fake = {"seconds": 1.0, "prompt_chars": 4000, "reply_chars": 1000, "status": "ok"}
        self.assertEqual(timing.user_roles(self.project), 1)                  # nothing recorded: one role
        one = timing.estimate(self.project, [], prober=lambda pid: fake)
        self.project.data["context"]["roles"] = {"owner": "Owner", "staff": "Staff"}
        self.assertEqual(timing.user_roles(self.project), 2)
        two = timing.estimate(self.project, [], prober=lambda pid: fake)
        self.assertGreater(two["calls"]["Q"], one["calls"]["Q"])               # one prior-only draw set per role
        self.assertEqual(two["calls"]["J"], one["calls"]["J"])


if __name__ == "__main__":
    unittest.main()
