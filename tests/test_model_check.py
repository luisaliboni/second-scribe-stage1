"""Task 17 (K10): the Setup model check (a kit run on the synthetic corpus, Q then J+M, in two processes) and its reuse in
tool configs (the canary is satisfied by the hashed model_check.json; a J session reuses the Q session's reconstructor
preflight, so it calls the judge only). Study configs (no ``tool`` block) keep today's canary and preflight.

Every model answer comes from ``tests/mock_models.py`` on 127.0.0.1: no real model, no Ollama, no API."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from second_scribe import audits, codehash, model_check, pipeline
from second_scribe.audit.registry import AuditLog
from second_scribe.audit.verify import verify_run
from second_scribe.cli import DEMO_CONFIG, build_clients, main
from second_scribe.llm import HTTPClient, ModelConfig
from second_scribe.pipeline import KIT_ROOT, RunConfig

from tests.mock_models import MockModels, default_models
from tests.test_real_run_validity import real_config

THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
RECON_ID, JUDGE_ID = "qwen-mock:7b", "gemma-mock:9b"
DIGESTS = {"recon": default_models()[RECON_ID]["digest"], "judge": default_models()[JUDGE_ID]["digest"]}
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"


def model_env(url: str, *, judge: bool = True, recon_id: str = RECON_ID, judge_id: str = JUDGE_ID) -> dict:
    env = {"SS_RECON_URL": url + "/v1", "SS_RECON_MODEL": recon_id, "SS_RECON_PARAMS": json.dumps(THINK_OFF)}
    if judge:
        env.update(SS_JUDGE_URL=url + "/v1", SS_JUDGE_MODEL=judge_id, SS_JUDGE_PARAMS=json.dumps(THINK_OFF))
    return env


def clean_env(extra: dict) -> dict:
    """os.environ without any SS_ variable of the caller, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
    env.update(extra)
    return env


def http_clients(url: str, roles=("recon", "judge")) -> dict:
    ids = {"recon": RECON_ID, "judge": JUDGE_ID}
    return {r: HTTPClient(ModelConfig(name=r, endpoint=url + "/v1", model_id=ids[r], params=dict(THINK_OFF)))
            for r in roles}


def records(log: Path, kind: str | None = None) -> list[dict]:
    out = [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines()]
    return [r for r in out if kind is None or r["record_type"] == kind]


def results_of(out_run: Path, stage: str) -> dict:
    return {r["name"]: r for r in json.loads((out_run / "audits" / f"{stage}.json").read_text())["results"]}


def sha_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class _MockBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = MockModels([DEMO_CONFIG])
        cls.url = cls.srv.start()
        cls.addClassCleanup(cls.srv.stop)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.srv.set(canary_fail=False)
        self.addCleanup(self.srv.set, canary_fail=False)

    def cli(self, argv, env, *, all_roles=False):
        """``cli.main`` in this process with ``env`` as the whole SS_ environment. ``all_roles`` builds every client
        role whatever the stages (an app that passes both endpoints to a J session)."""
        out, err = io.StringIO(), io.StringIO()
        patches = [mock.patch.dict(os.environ, clean_env(env), clear=True)]
        if all_roles:
            patches.append(mock.patch("second_scribe.cli.roles_for_stages", return_value=None))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            st.enter_context(contextlib.redirect_stdout(out))
            st.enter_context(contextlib.redirect_stderr(err))
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ the model check itself

class TestModelCheck(_MockBase):
    def test_q_then_j_two_processes_one_run(self):
        out = self.tmp / "mc"
        env = clean_env(model_env(self.url))
        digests = ",".join(f"{r}={d}" for r, d in DIGESTS.items())
        base = [sys.executable, "-m", "second_scribe.cli", "model-check", "--out", str(out), "--digests", digests]
        q = subprocess.run(base + ["--stages", "Q"], cwd=KIT_ROOT, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertFalse((out / "model_check.json").exists())        # a judge is configured: the check needs J too
        j = subprocess.run(base + ["--stages", "J"], cwd=KIT_ROOT, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(j.returncode, 0, j.stderr)
        doc = json.loads((out / "model_check.json").read_text())
        self.assertEqual(doc["schema"], "model-check/1")
        self.assertIs(doc["passed"], True)
        self.assertEqual(doc["failures"], [])
        self.assertEqual(doc["models"], {"recon": RECON_ID, "judge": JUDGE_ID})
        self.assertEqual(doc["digests"], DIGESTS)
        self.assertEqual(doc["prompt_set_sha256"], pipeline.prompt_set_sha256())
        self.assertEqual(doc["code_sha256"], codehash.code_sha256())
        self.assertGreaterEqual(doc["counts"]["wished"]["n"], 1)
        self.assertGreaterEqual(doc["counts"]["negated"]["n"], 1)
        self.assertIn(sha_file(out / "model_check.json"), j.stdout)   # the app stores this sha in tool.model_check
        logs = sorted(model_check.run_dir(out).rglob("run.jsonl"))
        self.assertEqual(logs, [model_check.log_path(out)])           # one run, one log
        self.assertEqual(model_check.log_path(out), out / "lean" / "run" / "logs" / "run.jsonl")
        self.assertEqual(doc["counts"]["judge_check"]["wished"]["rate"], 1.0)
        ok, detail = verify_run(str(model_check.judge_check_log(out)), None, None, False)
        self.assertTrue(ok, detail)
        starts = records(logs[0], "run_start")
        self.assertEqual([s["payload"]["stages"] for s in starts],
                         [pipeline.STAGE_GROUPS["Q"], pipeline.STAGE_GROUPS["J"] + pipeline.STAGE_GROUPS["M"]])
        ok, detail = verify_run(str(logs[0]), None, None, False)
        self.assertTrue(ok, detail)
        recs = records(logs[0])
        self.assertEqual(doc["log_head"], [len(recs), recs[-1]["hash"]])
        # the J session resumed the Q session's work item by item: every recon item is still the Q session's
        q_items = {r["payload"]["name"] for r in recs[:recs.index(starts[1])]
                   if r["record_type"] == "work_item" and r["payload"].get("stage") == "recon"}
        later = {r["payload"]["name"] for r in recs[recs.index(starts[1]):]
                 if r["record_type"] == "work_item" and r["payload"].get("stage") == "recon"}
        self.assertTrue(q_items)
        self.assertEqual(later, set())

    def test_failing_pair_exit_4(self):
        out = self.tmp / "mc"
        env = model_env(self.url)
        rc, _o, err = self.cli(["model-check", "--out", str(out), "--stages", "Q"], env)
        self.assertEqual(rc, 0, err)
        self.srv.set(canary_fail=True)                 # the judge now answers "actual" for every element
        rc, _o, err = self.cli(["model-check", "--out", str(out), "--stages", "J"], env)
        self.assertEqual(rc, 4, err)
        doc = json.loads((out / "model_check.json").read_text())
        self.assertIs(doc["passed"], False)
        self.assertIn("model_check_judge_below_rule:wished", doc["failures"])
        self.assertIn("model_check_judge_below_rule:negated", doc["failures"])
        self.assertEqual(doc["counts"]["judge_check"]["wished"]["rate"], 0.0)

    def test_q_only_pair_scores_the_reconstruction_canary(self):
        out = self.tmp / "mc"
        rc, _o, err = self.cli(["model-check", "--out", str(out), "--stages", "Q"], model_env(self.url, judge=False))
        self.assertEqual(rc, 0, err)
        doc = json.loads((out / "model_check.json").read_text())
        self.assertIs(doc["passed"], True)
        self.assertEqual(doc["models"], {"recon": RECON_ID})
        self.assertGreaterEqual(doc["counts"]["recon"]["n"], 1)

    def test_two_family_guard_runs_before_any_call(self):
        out = self.tmp / "mc"
        n0 = len(self.srv.requests)
        env = model_env(self.url, judge_id="qwq-mock:32b")        # the judge is the reconstructor's family
        rc, _o, err = self.cli(["model-check", "--out", str(out), "--stages", "J"], env)
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(self.srv.requests), n0)
        self.assertFalse((out / "model_check.json").exists())

    def test_j_without_a_q_session_is_refused(self):
        out = self.tmp / "mc"
        n0 = len(self.srv.requests)
        rc, _o, err = self.cli(["model-check", "--out", str(out), "--stages", "J"], model_env(self.url))
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(self.srv.requests), n0)

    def test_run_model_check_rejects_an_unknown_stage(self):
        with self.assertRaises(ValueError):
            model_check.run_model_check(self.tmp / "mc", http_clients(self.url), "M")


    def test_an_earlier_full_check_folder_is_not_resumed(self):
        """A pair's folder left by the earlier check (the whole demo config in ``<out>/run``) stays as it was: the
        lean check writes its own run in ``<out>/lean/run``."""
        out = self.tmp / "mc"
        old = out / "run" / "logs" / "run.jsonl"
        old.parent.mkdir(parents=True)
        old.write_text('{"record_type":"run_start"}\n', encoding="utf-8")
        before = old.read_bytes()
        model_check.run_model_check(out, http_clients(self.url, roles=("recon",)), "Q")
        self.assertEqual(old.read_bytes(), before)
        self.assertTrue(model_check.log_path(out).is_file())
        ok, detail = verify_run(str(model_check.log_path(out)), None, None, False)
        self.assertTrue(ok, detail)


class TestLeanCheckConfig(unittest.TestCase):
    """Item 36 (b): the model check runs rung 2 only and the one no-interview map per role at the study's reading-3
    setting, on six of the twelve synthetic cases that keep both roles and every planted kind."""
    CASES = ["SYN-OF01", "SYN-OF03", "SYN-OF05", "SYN-OW01", "SYN-OW03", "SYN-OW06"]

    def setUp(self):
        self.demo = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        self.lean = json.loads(model_check.CHECK_CONFIG.read_text(encoding="utf-8"))
        self.traps_dir = KIT_ROOT / "demo" / "corpus" / "traps"

    def traps(self, pid):
        return json.loads((self.traps_dir / f"{pid}.json").read_text(encoding="utf-8"))

    def test_it_is_the_demo_config_cut_to_the_study_settings(self):
        self.assertEqual(model_check.CHECK_CONFIG, KIT_ROOT / "configs" / "model-check.json")
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(), DEMO_HASH)          # the demo config is untouched
        cfg = RunConfig.load(model_check.CHECK_CONFIG)
        self.assertTrue(pipeline.is_synthetic(cfg))
        self.assertEqual(self.lean["specs"], [self.demo["specs"][0]])               # rung 2: the primary spec only
        self.assertEqual(self.lean["specs"][0], {"arm": "removed", "variant": "p0", "temperature": 0.0,
                                                 "replicate": 0})
        self.assertEqual(self.lean["ladder"], {})
        self.assertNotIn("script_sources", self.lean)
        self.assertEqual(cfg.prior_specs(), [])                                     # no prior-only script set
        self.assertEqual(self.lean["reading3"], {"variant": "p0", "samples": 1, "temperature": 0})
        standard = json.loads((KIT_ROOT / "second_scribe" / "app" / "presets" / "standard.json").read_text())
        self.assertEqual(self.lean["reading3"], standard["reading3"])               # the study's reading-3 setting
        for k in ("context", "settings", "models", "stats", "prereg_required"):
            self.assertEqual(self.lean[k], self.demo[k], k)
        corpus = {k: v for k, v in self.lean["corpus"].items() if k not in ("roles", "interviewers",
                                                                            "audit_determinism_k")}
        self.assertEqual(corpus, {k: v for k, v in self.demo["corpus"].items() if k not in ("roles", "interviewers")})
        self.assertEqual(self.lean["corpus"]["audit_determinism_k"], 6)
        self.assertEqual(sorted(self.lean["corpus"]["roles"]), self.CASES)
        for k in ("roles", "interviewers"):
            self.assertEqual(self.lean["corpus"][k], {p: self.demo["corpus"][k][p] for p in self.CASES})
        self.assertEqual([p.pid for p in pipeline.load_participants(cfg)], self.CASES)

    def test_the_six_cases_keep_both_roles_and_every_planted_kind(self):
        roles = self.lean["corpus"]["roles"]
        self.assertEqual(sorted(set(roles.values())), sorted(self.demo["context"]["roles"]))
        every = {t["kind"] for f in self.traps_dir.glob("*.json") for t in json.loads(f.read_text())}
        self.assertEqual(len(every), 11)
        self.assertEqual({t["kind"] for pid in self.CASES for t in self.traps(pid)}, every)
        self.assertEqual(sorted(set(self.lean["corpus"]["interviewers"].values())), ["I1", "I2", "I3"])

    def test_each_gated_kind_keeps_six_rows(self):
        """The trap rule gates on wished and negated (``audits.CANARY_KINDS``): every chosen case carries both, and
        no other case does."""
        for pid in self.CASES:
            self.assertTrue(set(audits.CANARY_KINDS) <= {t["kind"] for t in self.traps(pid)}, pid)
        others = [f.stem for f in self.traps_dir.glob("*.json") if f.stem not in self.CASES]
        for pid in others:
            self.assertFalse(set(audits.CANARY_KINDS) <= {t["kind"] for t in self.traps(pid)}, pid)

    def test_the_judge_check_items_are_the_twelve_case_ones(self):
        from types import SimpleNamespace
        full = RunConfig.load(DEMO_CONFIG)
        lean = RunConfig.load(model_check.CHECK_CONFIG)
        a = audits._planted_items(full, SimpleNamespace(people=pipeline.load_participants(full)))
        b = audits._planted_items(lean, SimpleNamespace(people=pipeline.load_participants(lean)))
        self.assertEqual(a, b)
        self.assertEqual(len(b[0]), 4)


# ------------------------------------------------------------------ reuse in tool configs

class _ToolRunBase(_MockBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._mc_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._mc_tmp.cleanup)
        cls.mc_dir = Path(cls._mc_tmp.name) / "mc"
        clients = http_clients(cls.url)
        model_check.run_model_check(cls.mc_dir, clients, "Q", digests=DIGESTS)
        doc = model_check.run_model_check(cls.mc_dir, clients, "J", digests=DIGESTS)
        assert doc["passed"], doc
        cls.mc_path = cls.mc_dir / "model_check.json"
        cls.mc_sha = sha_file(cls.mc_path)

    def config(self, *, tool: bool = True, digests: dict | None = None, mc_sha: str | None = None) -> Path:
        p = real_config(self.tmp)
        d = json.loads(p.read_text())
        d["corpus"]["audit_determinism_k"] = 0       # no re-sends: every request to the mock is a logged call
        if tool:
            d["tool"] = {"judge_rule": "two_family", "model_families": {"recon": "qwen", "judge": "gemma"},
                         "model_digests": dict(digests or DIGESTS),
                         "model_check": {"path": str(self.mc_path), "sha256": mc_sha or self.mc_sha}}
        p.write_text(json.dumps(d))
        self.run_dir = self.tmp / "run"
        self.log = self.run_dir / "logs" / "run.jsonl"
        return p

    def logged_calls(self, log: Path, start: int = 0) -> int:
        return sum(r["record_type"] == "call_request" for r in records(log)[start:]) if log.exists() else 0


class TestReuse(_ToolRunBase):
    def test_reuse_skips_canary_calls(self):
        cfg = self.config()
        n0 = len(self.srv.requests)
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "Q", "--client", "http"], model_env(self.url))
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.run_dir / "canary").exists())
        sent = len(self.srv.requests) - n0
        logged = self.logged_calls(self.log) + self.logged_calls(self.run_dir / "preflight" / "logs" / "run.jsonl")
        self.assertGreater(logged, 0)
        self.assertEqual(sent, logged)                  # nothing went to the mock outside the run and its preflight
        canary = results_of(self.run_dir, "Q")["canary"]
        self.assertTrue(canary["passed"])
        self.assertIs(canary["counts"]["reused"], True)
        summary = records(self.log, "audit_summary")[-1]["payload"]["audits"]
        self.assertTrue(any(a["name"] == "canary" and a["counts"].get("reused") is True for a in summary))

    def test_mismatch_blocks(self):
        other = {**DIGESTS, "judge": "f" * 64}
        cfg = self.config(digests=other)
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "Q", "--client", "http"], model_env(self.url))
        self.assertEqual(rc, 4, err)
        canary = results_of(self.run_dir, "Q")["canary"]
        self.assertFalse(canary["passed"])
        self.assertTrue(canary["blocking"])
        self.assertEqual(canary["failures"], ["model_check_mismatch:digests"])
        self.assertFalse((self.run_dir / "canary").exists())

    def test_j_session_sends_no_recon_calls(self):
        cfg = self.config()
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "Q", "--client", "http"], model_env(self.url))
        self.assertEqual(rc, 0, err)
        n0 = len(self.srv.requests)
        # both endpoints are passed to the J session, so a reconstructor client exists and could be called
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "J", "--client", "http"], model_env(self.url),
                               all_roles=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(records(self.log, "run_start")[-1]["payload"]["models"], {"recon": RECON_ID, "judge": JUDGE_ID})
        models = {r["model"] for r in self.srv.requests[n0:]}
        self.assertEqual(models, {JUDGE_ID})
        pre = results_of(self.run_dir, "J")["preflight"]
        self.assertTrue(pre["passed"])
        self.assertEqual(pre["counts"]["recon_n"], 0)
        self.assertEqual(pre["counts"]["recon_reused"], 1)
        self.assertEqual(pre["warnings"], [])
        ok, detail = verify_run(str(self.log), None, None, False)
        self.assertTrue(ok, detail)

    def test_study_config_canary_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(), DEMO_HASH)
        cfg = self.config(tool=False)
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "Q", "--client", "http"], model_env(self.url))
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.run_dir / "canary" / "logs" / "run.jsonl").exists())     # the canary ran, as today
        canary = results_of(self.run_dir, "Q")["canary"]
        self.assertTrue(canary["passed"])
        self.assertNotIn("reused", canary["counts"])
        self.assertIn("recon", canary["counts"])                     # a Q-only session: the reconstruction canary
        n0 = len(self.srv.requests)
        rc, _o, err = self.cli(["run", "--config", str(cfg), "--stages", "J", "--client", "http"], model_env(self.url),
                               all_roles=True)
        self.assertEqual(rc, 0, err)
        self.assertIn(RECON_ID, {r["model"] for r in self.srv.requests[n0:]})   # preflight and canary still call it
        pre = results_of(self.run_dir, "J")["preflight"]
        self.assertEqual(pre["counts"]["recon_n"], 2)
        self.assertNotIn("recon_reused", pre["counts"])
        self.assertNotIn("reused", results_of(self.run_dir, "J")["canary"]["counts"])


# ------------------------------------------------------------------ unit checks of the reuse rules (no server calls)

class _NoCall:
    """A client that must never be called; its cfg names a model id as a live client's would."""

    def __init__(self, role, model_id):
        self.cfg = ModelConfig(name=role, endpoint="http://127.0.0.1:9/v1", model_id=model_id, params=dict(THINK_OFF))

    def complete(self, *a, **k):
        raise AssertionError("no model call expected")


class TestCanaryReuseFields(_ToolRunBase):
    def ctx(self, cfg_path: Path, clients=None):
        cfg = RunConfig.load(cfg_path)
        return SimpleNamespace(cfg=cfg, clients=clients or {"recon": _NoCall("recon", RECON_ID),
                                                             "judge": _NoCall("judge", JUDGE_ID)})

    def doc_copy(self, **changes) -> tuple[Path, str]:
        doc = json.loads(self.mc_path.read_text())
        doc.update(changes)
        p = self.tmp / "mc_copy.json"
        p.write_text(json.dumps(doc, sort_keys=True, indent=2))
        return p, sha_file(p)

    def with_check(self, path: Path, sha: str) -> Path:
        cfg = self.config()
        d = json.loads(cfg.read_text())
        d["tool"]["model_check"] = {"path": str(path), "sha256": sha}
        cfg.write_text(json.dumps(d))
        return cfg

    def test_match_is_reused_without_any_call(self):
        r = audits._canary_result(self.ctx(self.config()))
        self.assertEqual((r.name, r.blocking, r.passed, r.failures), ("canary", True, True, []))
        self.assertIs(r.counts["reused"], True)

    def test_judge_only_session_is_reused_too(self):
        r = audits._canary_result(self.ctx(self.config(), {"judge": _NoCall("judge", JUDGE_ID)}))
        self.assertEqual((r.name, r.passed), ("canary", True))

    def test_file_changed_after_hashing(self):
        p, _sha = self.doc_copy()
        r = audits._canary_result(self.ctx(self.with_check(p, "0" * 64)))
        self.assertEqual((r.name, r.blocking, r.passed, r.failures), ("canary", True, False, ["model_check_mismatch:sha256"]))

    def test_missing_file(self):
        r = audits._canary_result(self.ctx(self.with_check(self.tmp / "nope.json", self.mc_sha)))
        self.assertEqual(r.failures, ["model_check_mismatch:path"])

    def test_failed_check_is_not_reused(self):
        r = audits._canary_result(self.ctx(self.with_check(*self.doc_copy(passed=False))))
        self.assertEqual(r.failures, ["model_check_mismatch:passed"])

    def test_other_model(self):
        r = audits._canary_result(self.ctx(self.config(), {"recon": _NoCall("recon", "qwen-other:14b"),
                                                           "judge": _NoCall("judge", JUDGE_ID)}))
        self.assertEqual(r.failures, ["model_check_mismatch:models"])

    def test_session_role_the_check_never_saw(self):
        clients = {"judge": _NoCall("judge", JUDGE_ID), "judge2": _NoCall("judge2", "llama-mock:8b")}
        r = audits._canary_result(self.ctx(self.config(), clients))
        self.assertEqual(r.failures, ["model_check_mismatch:models"])

    def test_models_read_from_the_hashed_file_not_the_config(self):
        r = audits._canary_result(self.ctx(self.with_check(*self.doc_copy(models={"recon": "x", "judge": "y"}))))
        self.assertEqual(r.failures, ["model_check_mismatch:models"])

    def test_prompt_set_changed(self):
        with mock.patch("second_scribe.pipeline.prompt_set_sha256", return_value="a" * 64):
            r = audits._canary_result(self.ctx(self.config()))
        self.assertEqual(r.failures, ["model_check_mismatch:prompt_set_sha256"])

    def test_wrong_schema(self):
        r = audits._canary_result(self.ctx(self.with_check(*self.doc_copy(schema="model-check/0"))))
        self.assertEqual(r.failures, ["model_check_mismatch:schema"])

    def test_once_per_run_call(self):
        ctx = self.ctx(self.config())
        self.assertIsNotNone(audits._canary_result(ctx))
        self.assertIsNone(audits._canary_result(ctx))


class TestPreflightRoles(unittest.TestCase):
    """The preflight of a tool session calls only the roles of its own stages; the reconstructor's preflight of a J
    session is the logged one of the Q session (same model id, same config hash, so the same digests)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        p = real_config(self.tmp)
        d = json.loads(p.read_text())
        d["tool"] = {"judge_rule": "two_family", "model_families": {"recon": "qwen", "judge": "gemma"}}
        p.write_text(json.dumps(d))
        self.cfg = RunConfig.load(p)
        oracle = build_clients("oracle", RunConfig.load(DEMO_CONFIG))["judge"]
        self.judge = SimpleNamespace(cfg=ModelConfig(name="judge", endpoint="http://127.0.0.1:9/v1", model_id=JUDGE_ID,
                                                     params=dict(THINK_OFF)),
                                     complete=lambda m, **k: oracle.complete(m, **k))
        self.log_path = self.tmp / "run" / "logs" / "run.jsonl"
        self.log_path.parent.mkdir(parents=True)
        self.log = AuditLog(self.log_path, self.cfg.run_id)
        self.addCleanup(self.log.close)

    def start(self, stages, models, config_sha=None):
        self.log.append("run_start", {"config_sha256": config_sha or self.cfg.sha256(), "stages": stages,
                                      "models": models})

    def passed_preflight(self, recon_n=2, recon_ok=2, passed=True):
        res = audits.AuditResult("preflight", True, passed, {"recon_n": recon_n, "recon_ok": recon_ok}, [])
        self.log.append("audit_summary", {"audits": [res.summary()], "blocked": False})

    def preflight(self, clients):
        ctx = SimpleNamespace(cfg=self.cfg, clients=clients, log=self.log,
                              recorded_models=lambda: {"recon": RECON_ID})
        audits.preflight(ctx)
        return ctx.preflight_result

    def test_j_session_reuses_the_logged_q_preflight(self):
        self.start(pipeline.STAGE_GROUPS["Q"], {"recon": RECON_ID})
        self.passed_preflight()
        self.start(pipeline.STAGE_GROUPS["J"], {"recon": RECON_ID, "judge": JUDGE_ID})
        res = self.preflight({"recon": _NoCall("recon", RECON_ID), "judge": self.judge})
        self.assertTrue(res.passed, res.failures)
        self.assertEqual((res.counts["recon_n"], res.counts["recon_reused"]), (0, 1))
        self.assertEqual(res.warnings, [])
        self.assertEqual(res.counts["wished"]["n"], 2)                 # the judge part ran

    def test_nothing_to_reuse_still_sends_no_recon_call_and_says_so(self):
        self.start(pipeline.STAGE_GROUPS["J"], {"recon": RECON_ID, "judge": JUDGE_ID})
        res = self.preflight({"recon": _NoCall("recon", RECON_ID), "judge": self.judge})
        self.assertTrue(res.passed)
        self.assertEqual(res.counts["recon_reused"], 0)
        self.assertEqual(res.warnings, ["preflight_recon_not_reused:0"])

    def test_no_reuse_of_another_recon_model_or_config_or_a_failed_preflight(self):
        for models, sha, kw in (({"recon": "qwen-other:14b"}, None, {}), ({"recon": RECON_ID}, "b" * 64, {}),
                                ({"recon": RECON_ID}, None, {"passed": False}),
                                ({"recon": RECON_ID}, None, {"recon_n": 0, "recon_ok": 0}),
                                ({"recon": RECON_ID}, None, {"recon_ok": 1})):
            with self.subTest(models=models, sha=sha, kw=kw):
                self.log_path.unlink(missing_ok=True)
                self.start(pipeline.STAGE_GROUPS["Q"], models, sha)
                self.passed_preflight(**kw)
                self.start(pipeline.STAGE_GROUPS["J"], {"judge": JUDGE_ID})
                res = self.preflight({"judge": self.judge})
                self.assertEqual(res.counts["recon_reused"], 0)

    def test_q_session_does_not_judge_the_planted_items(self):
        self.start(pipeline.STAGE_GROUPS["Q"], {"recon": RECON_ID, "judge": JUDGE_ID})
        recon = build_clients("oracle", RunConfig.load(DEMO_CONFIG))["recon"]
        live_recon = SimpleNamespace(cfg=ModelConfig(name="recon", endpoint="http://127.0.0.1:9/v1", model_id=RECON_ID,
                                                     params=dict(THINK_OFF)),
                                     complete=lambda m, **k: recon.complete(m, **k))
        res = self.preflight({"recon": live_recon, "judge": _NoCall("judge", JUDGE_ID)})
        self.assertTrue(res.passed, res.failures)
        self.assertEqual(res.counts["recon_n"], 2)
        self.assertNotIn("wished", res.counts)

    def test_study_config_keeps_both_roles(self):
        (self.tmp / "study").mkdir()
        study = RunConfig.load(real_config(self.tmp / "study"))  # no tool block
        self.assertEqual(study.tool, {})
        self.start(pipeline.STAGE_GROUPS["J"], {"recon": RECON_ID, "judge": JUDGE_ID})
        recon = build_clients("oracle", RunConfig.load(DEMO_CONFIG))["recon"]
        calls = []
        live_recon = SimpleNamespace(cfg=ModelConfig(name="recon", endpoint="http://127.0.0.1:9/v1", model_id=RECON_ID,
                                                     params=dict(THINK_OFF)),
                                     complete=lambda m, **k: calls.append(1) or recon.complete(m, **k))
        ctx = SimpleNamespace(cfg=study, clients={"recon": live_recon, "judge": self.judge}, log=self.log)
        audits.preflight(ctx)
        self.assertEqual(ctx.preflight_result.counts["recon_n"], 2)
        self.assertNotIn("recon_reused", ctx.preflight_result.counts)
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
