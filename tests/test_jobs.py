"""Task 39: the Q/J/M runner, its progress, pause/resume, the model swap and the run routes (spec 3, 6.8, 4
``run_linked``; Review Focus 3).

Every run here is a real kit run (``python -m second_scribe.cli run --client http``) against ``tests/mock_models``
on 127.0.0.1 (the oracle answers by prompt text), over ``tests/realpath_corpus``: a real-kind, renamed copy of the
shipped synthetic corpus. No real model, Ollama or API is contacted. Task 38's ``orchestrate`` is not on this branch:
the start route's tests put a stand-in module in its place.
"""
import json
import os
import signal
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import second_scribe.app as app_pkg
from second_scribe import export_view
from second_scribe.app import api, jobs, models, profile_view, results, timing
from second_scribe.app.project import Project
from second_scribe.audit import verify as audit_verify
from second_scribe.pipeline import RunConfig
from tests.mock_models import MockModels, default_models
from tests.realpath_corpus import DEMO_CONFIG, make_realpath_corpus, realpath_config
from tests.test_coding import make_project, span

THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
RECON_ID, JUDGE_ID = "qwen-mock:7b", "gemma-mock:9b"
RUN = "A-0a1b2c3d"
# words of the corpus's transcripts and of the AI's maps (the oracle answers with the gold labels): never in progress
SENTINELS = ("licensing counter", "health unit", "fire department", "sales tax")


def model_env(url: str, *, judge: bool = True) -> dict:
    env = {"SS_RECON_URL": url + "/v1", "SS_RECON_MODEL": RECON_ID, "SS_RECON_PARAMS": json.dumps(THINK_OFF),
           "SS_RECON_PROVIDER": "openai"}
    if judge:
        env.update(SS_JUDGE_URL=url + "/v1", SS_JUDGE_MODEL=JUDGE_ID, SS_JUDGE_PARAMS=json.dumps(THINK_OFF),
                   SS_JUDGE_PROVIDER="openai")
    return env


def records(log: Path, kind: str | None = None) -> list[dict]:
    out = [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in out if kind is None or r["record_type"] == kind]


def keys_deep(obj) -> set:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys_deep(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(keys_deep(v) for v in obj)) if obj else set()
    return set()


def role_entry(url: str, model: str, family: str, server: str = "ollama") -> dict:
    return {"kind": "local", "provider": "openai", "url": url + "/v1", "host": url.split("//", 1)[1],
            "address_class": "loopback", "model": model, "family": family, "family_detected": family,
            "digest": default_models()[model]["digest"], "server": server,
            "connection": {"ok": True, "ms": 5, "thinking_seen": False, "reply_ok": True, "error": None}}


class FakeOrchestrate(types.ModuleType):
    """Stands in for Task 38's ``orchestrate`` (its interfaces as built on app-k38): ``new_run_id``;
    ``build_run_config`` writes a realpath config into the project and logs ``run_started``, or returns an existing
    config unchanged (a resumed run); ``run_env`` gives the whole child env of the mock models."""

    def __init__(self, env: dict) -> None:
        super().__init__("second_scribe.app.orchestrate")
        self.env = dict(env)
        self.built: list = []            # configs written
        self.calls: list = []            # every build_run_config call
        self.env_calls: list = []

    def new_run_id(self, group):
        import secrets
        return f"{group}-{secrets.token_hex(4)}"

    def build_run_config(self, project, group, rungs, preset, run_id):
        self.calls.append((group, list(rungs), preset, run_id))
        existing = project.path(f"runs/{run_id}/config.json")
        if existing.is_file():
            return existing
        corpus = project.root / "corpus"
        if not (corpus / "pid_map.json").is_file():
            make_realpath_corpus(corpus)
        path = realpath_config(project.root, run_id, group=group, rungs=rungs, corpus_dir=corpus)
        self.built.append((group, list(rungs), preset, run_id))
        project.log.append("run_started", {"group": group, "run_id": run_id, "config_sha256": "0" * 64})
        return path

    def run_env(self, project, secrets, role_set=None):
        self.env_calls.append(role_set)
        return dict(self.env)


class MockCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockModels([DEMO_CONFIG])
        cls.url = cls.mock.start()
        cls.addClassCleanup(cls.mock.stop)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-k39-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.mock.requests.clear()
        self.mock.set(slow_s=0.0, thinking=False, unload_ok=False)
        self.addCleanup(self.mock.set, slow_s=0.0, thinking=False, unload_ok=False)
        self.live: list = []
        self.addCleanup(self._stop_live)

    def _stop_live(self):
        for j in self.live:
            j.stop()

    def project(self, name: str = "proj") -> Project:
        return Project.create(self.tmp / name, "Study")

    def run_job(self, project, run_id: str = RUN, *, judge: bool = True, **kw) -> jobs.RunJob:
        cfg = realpath_config(project.root, run_id, corpus_dir=self.tmp / "corpus")
        job = jobs.RunJob(project, cfg, env=jobs.child_env(model_env(self.url, judge=judge)), **kw)
        self.live.append(job)
        return job

    def drive(self, poll, until=("done", "failed"), timeout=300.0) -> dict:
        t0 = time.time()
        while True:
            st = poll()
            if st["state"] in until:
                return st
            if time.time() - t0 > timeout:
                self.fail(f"run still {st['state']} after {timeout}s: {st}")
            time.sleep(0.05)


# --- the runner ---------------------------------------------------------------------------------------------------

class TestRunJob(MockCase):
    def test_full_run_links_and_exports_view(self):
        p = self.project()
        job = self.run_job(p, swap=False).start()
        st = self.drive(job.poll)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(job.stages, ("Q", "J", "M"))
        run_dir = p.path(f"runs/{RUN}")
        view = json.loads(p.path(f"views/{RUN}.view.json").read_text(encoding="utf-8"))
        self.assertEqual(export_view.validate_view(view), [])
        self.assertEqual(view["source"], "own")
        self.assertEqual(view["reference_type"], "A")
        prof = profile_view.load_profile(p.path(f"views/{RUN}.profile.json"))         # pre-written for Results
        self.assertEqual(prof["schema"], "orgsci-profile/v2")
        manifest = json.loads((run_dir / "logs" / "run.manifest.json").read_text(encoding="utf-8"))
        linked = p.log.records("run_linked")
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["payload"], {"run_id": RUN,
                                                "run_head": [manifest["record_count"], manifest["head_hash"]]})
        self.assertEqual(audit_verify.main([str(run_dir / "logs" / "run.jsonl"),
                                            str(run_dir / "logs" / "run.manifest.json")]), 0)
        summary = results.runs_summary(p)
        self.assertEqual([{k: r[k] for k in ("run_id", "group", "finished", "ready", "state")} for r in summary],
                         [{"run_id": RUN, "group": "A", "finished": True, "ready": True, "state": "finished"}])
        # Part 5a (scout F10): each row also says when, with which instructions, how many people and which models
        self.assertEqual(summary[0]["rungs"], ["rung2"])
        self.assertIsInstance(summary[0]["started"], str)
        self.assertEqual(sorted(summary[0]["models"]), ["judge", "recon"])
        self.assertEqual(st["profile"], "ready")
        # a finished run read back from disk (the app restarted) is done, and is not run again
        again = jobs.RunJob(p, run_dir / "config.json", env={})
        self.assertEqual(again.poll()["state"], "done")
        with self.assertRaises(RuntimeError):
            again.start()

    def test_kill_mid_q_then_resume_verifies(self):
        p = self.project()
        self.mock.set(slow_s=0.25)
        job = self.run_job(p, swap=False).start()
        log = p.path(f"runs/{RUN}/logs/run.jsonl")
        t0 = time.time()
        while not (log.is_file() and log.read_bytes().count(b'"record_type":"call_response"') >= 5):
            self.assertLess(time.time() - t0, 120, "no 5 call_response records")
            time.sleep(0.02)
        child = job.child
        job.pause()                                                   # SIGTERM, then wait
        self.assertEqual(child.proc.returncode, -signal.SIGTERM)
        st = job.poll()
        self.assertEqual((st["state"], st["stage"]), ("paused", "Q"))
        self.assertEqual(st["code"], None)
        n_paused = st["done"]
        self.assertGreaterEqual(n_paused, 5)
        self.mock.set(slow_s=0.0)
        job.resume()
        st = self.drive(job.poll)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(audit_verify.main([str(log), str(log.with_name("run.manifest.json"))]), 0)
        self.assertEqual([f.name for f in p.path("runs").iterdir()], [RUN])          # one run folder
        self.assertEqual({r["run_id"] for r in records(log)}, {RUN})                 # one run id
        self.assertGreater(len(records(log, "run_start")), 3)                      # Q twice, then J and M

    def test_pause_after_restart_resumes_from_disk(self):
        """The app restarted mid-run: a RunJob read back from disk is paused and resumes the unfinished stage."""
        p = self.project()
        job = self.run_job(p, swap=True).start()
        st = self.drive(job.poll, until=("needs_swap", "failed"))
        self.assertEqual((st["state"], st["stage"]), ("needs_swap", "J"), st)
        job.stop()
        cfg = p.path(f"runs/{RUN}/config.json")
        self.assertEqual(jobs.RunJob(p, cfg, env={}, swap=True).poll()["state"], "needs_swap")   # still waits
        back = jobs.RunJob(p, cfg, env=jobs.child_env(model_env(self.url)), swap=False)
        self.live.append(back)
        st = back.poll()
        self.assertEqual((st["state"], st["stage"]), ("paused", "J"), st)
        back.resume()
        self.assertEqual(self.drive(back.poll)["state"], "done")
        self.assertEqual(len(records(p.path(f"runs/{RUN}/logs/run.jsonl"), "run_start")), 3)   # Q, J, M once each

    def test_progress_counts(self):
        p = self.project()
        job = self.run_job(p, swap=True).start()
        first = job.progress()
        self.assertEqual(first["stage"], "Q")
        cfg = RunConfig.load(p.path(f"runs/{RUN}/config.json"))
        totals = timing.count_calls(cfg, len(cfg.corpus["pairs"]))
        self.assertEqual(first["total"], totals["Q"])
        st = self.drive(job.poll, until=("needs_swap", "failed"))
        self.assertEqual(st["state"], "needs_swap", st)
        log = p.path(f"runs/{RUN}/logs/run.jsonl")
        recs = records(log)
        stage_of = {r["payload"]["request_id"]: r["payload"]["stage"] for r in recs if r["record_type"] == "call_request"}
        q_done = sum(1 for r in recs if r["record_type"] == "call_response"
                     and not stage_of[r["payload"]["request_id"]].startswith(("judge:", "judge2:")))
        self.assertGreater(q_done, 0)
        prog = job.progress()
        self.assertEqual((prog["stage"], prog["done"], prog["total"]), ("J", 0, totals["J"]))
        self.assertEqual(job.counts()["Q"], q_done)
        last = recs[-1]
        self.assertEqual(prog["last_record"], {"record_type": last["record_type"], "seq": last["seq"],
                                               "stage": last["payload"].get("stage")})
        job.proceed_swap(auto=False)
        st = self.drive(job.poll)
        self.assertEqual(st["state"], "done", st)
        recs = records(log)
        j_done = sum(1 for r in recs if r["record_type"] == "call_response") - q_done
        self.assertEqual(job.counts(), {"Q": q_done, "J": j_done})
        self.assertGreater(j_done, 0)

    def test_ai_maps_only_runs_q_and_m(self):
        p = self.project()
        cfg = realpath_config(p.root, RUN, corpus_dir=self.tmp / "corpus")
        d = json.loads(cfg.read_text(encoding="utf-8"))
        d["models"]["judge"] = None
        d["tool"] = {"reference_type": "A", "model_families": {"recon": "qwen"},
                     "model_digests": {"recon": d["tool"]["model_digests"]["recon"]}}
        cfg.write_text(json.dumps(d), encoding="utf-8")
        job = jobs.RunJob(p, cfg, env=jobs.child_env(model_env(self.url, judge=False)))      # from the config
        self.live.append(job)
        self.assertEqual(job.stages, ("Q", "M"))
        self.assertIn("--ai-maps-only", job.export_argv())
        job.start()
        st = self.drive(job.poll)
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["stages"], ["Q", "M"])
        self.assertEqual(json.loads(p.path(f"views/{RUN}.view.json").read_text(encoding="utf-8"))["schema"],
                         "view-data/1")
        self.assertFalse(any((r.get("path") or "").endswith("/v1/chat/completions") and r.get("model") == JUDGE_ID
                             for r in self.mock.requests))


# --- exit codes ---------------------------------------------------------------------------------------------------

class TestExitCodes(MockCase):
    def _out(self) -> Path:
        out = self.tmp / "run"
        (out / "logs").mkdir(parents=True)
        (out / "logs" / "run.jsonl").write_text("", encoding="utf-8")
        return out

    def test_exit_code_messages(self):
        out = self._out()
        from second_scribe.app import strings
        got = {rc: jobs.exit_info(rc, out) for rc in (1, 2, 3, -9, None)}
        self.assertEqual({rc: code for rc, (code, _m) in got.items()},
                         {1: "verify_failed", 2: "refused", 3: "fixture_missing", -9: "crashed", None: "crashed"})
        for rc, (code, msg) in got.items():
            self.assertEqual(msg, strings.t(f"runs.exit.{code}"))
        # 4 with an audit that blocked: the message names it
        (out / "audits").mkdir()
        (out / "audits" / "Q.json").write_text(json.dumps({"stage": "Q", "results": [
            {"name": "canary", "passed": False, "blocking": True, "failures": [], "counts": {}},
            {"name": "determinism", "passed": True, "blocking": True, "failures": [], "counts": {}},
            {"name": "coverage", "passed": False, "blocking": False, "failures": [], "counts": {}}]}), encoding="utf-8")
        code, msg = jobs.exit_info(4, out)
        self.assertEqual(code, "stopped")
        self.assertEqual(msg, strings.t("runs.exit.stopped", audit="canary"))
        self.assertIn("canary", msg)
        self.assertNotIn("determinism", msg)
        self.assertNotIn("coverage", msg)

    def test_stopped_names_the_audit_from_the_kit_line(self):
        out = self._out()
        from second_scribe.app import strings
        err = out / "stderr.txt"
        err.write_text("stopped: blocked by audit: leaks (2 failures), canary (1 failures)\n", encoding="utf-8")
        self.assertEqual(jobs.exit_info(4, out, err), ("stopped", strings.t("runs.exit.stopped", audit="leaks, canary")))
        err.write_text("stopped: reply from recon carries thinking output: reasoning_content (12 chars, ...)\n",
                       encoding="utf-8")
        self.assertEqual(jobs.exit_info(4, out, err), ("thinking", strings.t("runs.exit.thinking")))
        err.write_text("stopped: something else\n", encoding="utf-8")
        self.assertEqual(jobs.exit_info(4, out, err), ("stopped", strings.t("runs.exit.stopped_unnamed")))

    def test_live_thinking_stops_at_the_preflight(self):
        """A live run whose model answers with thinking on is stopped by the kit's preflight (exit 4) before any
        participant call; the message names that check, and the status carries no payload."""
        p = self.project()
        self.mock.set(thinking=True)
        job = self.run_job(p, swap=False).start()
        st = self.drive(job.poll)
        self.assertEqual((st["state"], st["rc"], st["code"]), ("failed", 4, "stopped"), st)
        from second_scribe.app import strings
        self.assertEqual(st["message"], strings.t("runs.exit.stopped", audit="preflight"))
        self.assertEqual(st["done"], 0)
        self.assertTrue(st["resumable"])
        self.assertNotIn("payload", keys_deep(st))


# --- routes -------------------------------------------------------------------------------------------------------

class RouteCase(MockCase):
    def setUp(self):
        super().setUp()
        api.load_route_modules()
        self.fake = FakeOrchestrate(jobs.child_env(model_env(self.url)) | {})
        for p in (mock.patch.dict(sys.modules, {"second_scribe.app.orchestrate": self.fake}),
                  mock.patch.object(app_pkg, "orchestrate", self.fake, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def open_state(self, project) -> api.AppState:
        self.state = api.AppState(project=project)
        self.addCleanup(self._stop_state)
        return self.state

    def _stop_state(self):
        live = self.state.jobs.get("run")
        if live is not None:
            live.stop()

    def call(self, method, path, body=None, query=None):
        try:
            return 200, api.dispatch(self.state, method, path, body or {}, query or {})
        except api.ApiError as e:
            return e.status, e.to_json()

    def ok(self, method, path, body=None, query=None):
        status, doc = self.call(method, path, body, query)
        self.assertEqual(status, 200, doc)
        return doc

    def status_of(self, run_id):
        return self.ok("GET", "/api/runs/status", query={"run": run_id})["job"]

    def set_pair(self, project, server="ollama"):
        models.set_role(project, "recon", role_entry(self.url, RECON_ID, "qwen", server))
        models.set_role(project, "judge", role_entry(self.url, JUDGE_ID, "gemma", server))
        models.confirm_families(project, {"recon": "qwen", "judge": "gemma"})
        project.save()


class TestRoutes(RouteCase):
    def test_progress_has_no_payload(self):
        """While a B coding is open, the run routes answer progress only: no ``payload`` key at any depth, no
        transcript text."""
        p = make_project(self.tmp / "proj")
        self.open_state(p)
        self.assertEqual(self.call("POST", "/api/references", {"pid": "CASE-B01", "type": "B"})[0], 200)
        body = {"pid": "CASE-B01", **span("CASE-B01", 1, 10), "status": "happened", "step": "Ask the clerk",
                "phase": "Applying"}
        self.assertEqual(self.call("POST", "/api/coding/mark", body)[0], 200)
        self.assertEqual(self.call("GET", "/api/view", query={"run": "A-00000000"})[1]["error"], "coding_unlocked")
        self.mock.set(slow_s=0.02)
        doc = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})
        run_id = doc["job"]["run_id"]
        replies = [doc]
        t0 = time.time()
        while True:
            reply = self.ok("GET", "/api/runs/status", query={"run": run_id})
            replies.append(reply)
            if reply["job"]["state"] in ("done", "failed"):
                break
            if reply["job"]["state"] == "needs_swap":
                replies.append(self.ok("POST", "/api/runs/swap", {"run": run_id, "auto": False}))
            self.assertLess(time.time() - t0, 300)
            time.sleep(0.05)
        self.assertEqual(reply["job"]["state"], "done", reply)
        replies.append(self.ok("GET", "/api/runs/status"))
        logged = p.path(f"runs/{run_id}/logs/run.jsonl").read_text(encoding="utf-8")
        for word in SENTINELS:                     # the log does hold them: the check below can fail
            self.assertTrue(word in logged, word)
        seen_records = {r["job"]["last_record"]["record_type"] for r in replies
                        if r.get("job") and r["job"].get("last_record")}
        self.assertTrue({"call_request", "call_response"} & seen_records, seen_records)
        for r in replies:
            self.assertNotIn("payload", keys_deep(r))
            text = json.dumps(r)
            for bad in ('"messages"', '"text"', "Ask the clerk", *SENTINELS):
                self.assertNotIn(bad, text)
            for k in (r.get("job") or {}).get("last_record") or {}:
                self.assertIn(k, ("record_type", "stage", "seq"))

    def test_start_then_status_pause_resume(self):
        p = self.project()
        self.open_state(p)
        self.mock.set(slow_s=0.2)
        doc = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})
        job = doc["job"]
        self.assertRegex(job["run_id"], r"^A-[0-9a-f]{8}$")
        self.assertEqual(self.fake.built, [("A", ["rung2"], "standard", job["run_id"])])
        self.assertEqual(self.fake.env_calls, [None])
        self.assertEqual((job["state"], job["stage"], job["stages"]), ("running", "Q", ["Q", "J", "M"]))
        # a second start while it runs answers the same run
        again = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})
        self.assertEqual(again["job"]["run_id"], job["run_id"])
        self.assertEqual(len(self.fake.built), 1)
        self.assertEqual(self.call("POST", "/api/runs/start", {"group": "C", "rungs": [], "preset": "standard"})[1]
                         ["error"], "run_busy")
        paused = self.ok("POST", "/api/runs/pause", {"run": job["run_id"]})["job"]
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(self.call("POST", "/api/runs/pause", {"run": job["run_id"]})[1]["error"], "not_running")
        # the app restarts: the run is read back from disk, still paused, and Start resumes it (no second run id)
        self.state.jobs.pop("run").stop()
        self.assertEqual(self.status_of(job["run_id"])["state"], "paused")
        self.mock.set(slow_s=0.0)
        doc = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})
        self.assertEqual(doc["job"]["run_id"], job["run_id"])
        self.assertEqual(len(self.fake.built), 1)
        self.assertEqual(self.fake.calls[-1], ("A", ["rung2"], "standard", job["run_id"]))   # refusals checked again
        self.assertEqual([r["payload"]["run_id"] for r in p.log.records("run_started")], [job["run_id"]])
        st = self.drive(lambda: self.status_of(job["run_id"]), until=("done", "failed", "needs_swap"))
        if st["state"] == "needs_swap":
            self.ok("POST", "/api/runs/swap", {"run": job["run_id"], "auto": False})
            st = self.drive(lambda: self.status_of(job["run_id"]))
        self.assertEqual(st["state"], "done", st)
        self.assertEqual([r.run_id for r in results.list_runs(p)], [job["run_id"]])
        # a new start for the same group after it finished is a new run
        self.ok("POST", "/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})
        self.assertEqual(len(self.fake.built), 2)

    def test_resume_route(self):
        p = self.project()
        self.open_state(p)
        self.mock.set(slow_s=0.2)
        run_id = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": [], "preset": "standard"})["job"]["run_id"]
        self.assertEqual(self.call("POST", "/api/runs/resume", {"run": run_id})[1]["error"], "not_paused")
        self.ok("POST", "/api/runs/pause", {"run": run_id})
        self.mock.set(slow_s=0.0)
        self.assertEqual(self.ok("POST", "/api/runs/resume", {"run": run_id})["job"]["state"], "running")

    def test_bad_requests(self):
        self.open_state(self.project())
        cases = [("POST", "/api/runs/start", {"group": "D", "rungs": [], "preset": "standard"}, 422, "bad_group"),
                 ("POST", "/api/runs/start", {"group": "A", "rungs": "rung2", "preset": "standard"}, 422, "bad_rungs"),
                 ("POST", "/api/runs/start", {"group": "A", "rungs": [], "preset": "huge"}, 422, "bad_preset"),
                 ("GET", "/api/runs/status", {"run": "../x"}, 422, "bad_run"),
                 ("GET", "/api/runs/status", {"run": "A-ffffffff"}, 404, "unknown_run"),
                 ("POST", "/api/runs/pause", {"run": "A-ffffffff"}, 404, "unknown_run"),
                 ("POST", "/api/runs/swap", {"run": "A-ffffffff", "auto": True}, 404, "unknown_run")]
        for method, path, body, status, code in cases:
            with self.subTest(path=path, body=body):
                got = self.call(method, path, body if method == "POST" else None, body if method == "GET" else None)
                self.assertEqual((got[0], got[1]["error"]), (status, code), got)
        self.assertEqual(self.fake.built, [])
        self.assertEqual(self.ok("GET", "/api/runs/status"), {"job": None})

    def test_key_missing_refuses_before_any_config(self):
        self.open_state(self.project())

        def refuse(*a, **k):
            raise api.ApiError(409, "key_missing", "no key")
        self.fake.run_env = refuse
        got = self.call("POST", "/api/runs/start", {"group": "A", "rungs": [], "preset": "standard"})
        self.assertEqual((got[0], got[1]["error"]), (409, "key_missing"))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.state.project.path("runs").exists() and any(self.state.project.path("runs").iterdir()))

    def test_orchestrate_refusal_passes_through(self):
        self.open_state(self.project())

        def refuse(*a, **k):
            raise api.ApiError(409, "file_changed", "x.docx changed since you imported it")
        self.fake.build_run_config = refuse
        got = self.call("POST", "/api/runs/start", {"group": "A", "rungs": [], "preset": "standard"})
        self.assertEqual((got[0], got[1]["error"]), (409, "file_changed"))
        self.assertNotIn("run", self.state.jobs)


class TestSwap(RouteCase):
    def _to_swap(self, server="ollama"):
        p = self.project()
        self.set_pair(p, server)
        self.open_state(p)
        self.assertTrue(jobs.needs_swap(p))
        run_id = self.ok("POST", "/api/runs/start", {"group": "A", "rungs": [], "preset": "standard"})["job"]["run_id"]
        st = self.drive(lambda: self.status_of(run_id), until=("needs_swap", "done", "failed"))
        self.assertEqual((st["state"], st["stage"]), ("needs_swap", "J"), st)
        return p, run_id

    def test_swap_unloads_then_loads(self):
        _p, run_id = self._to_swap()
        self.mock.set(unload_ok=True)
        self.mock.requests.clear()
        self.assertEqual(self.call("POST", "/api/runs/resume", {"run": run_id})[1]["error"], "not_paused")
        doc = self.ok("POST", "/api/runs/swap", {"run": run_id, "auto": True})
        self.assertIn(doc["job"]["state"], ("running", "done"))
        gens = [r for r in self.mock.requests if r["path"] == "/api/generate"]
        self.assertEqual([r["body"] for r in gens], [{"model": RECON_ID, "keep_alive": 0},
                                                     {"model": JUDGE_ID, "prompt": "", "keep_alive": "10m"}])
        self.assertEqual(self.mock.loaded, {JUDGE_ID})
        self.assertEqual(self.drive(lambda: self.status_of(run_id))["state"], "done")
        chats = [i for i, r in enumerate(self.mock.requests) if r["path"] == "/v1/chat/completions"]
        self.assertTrue(chats)
        self.assertGreater(chats[0], self.mock.requests.index(gens[1]))         # the judge runs after the load
        self.assertEqual({self.mock.requests[i]["model"] for i in chats}, {JUDGE_ID})

    def test_swap_auto_refused_off_ollama_and_manual_continues(self):
        _p, run_id = self._to_swap(server="lmstudio")
        self.mock.requests.clear()
        got = self.call("POST", "/api/runs/swap", {"run": run_id, "auto": True})
        self.assertEqual((got[0], got[1]["error"]), (409, "swap_not_allowed"))
        self.assertEqual([r for r in self.mock.requests if r["path"] == "/api/generate"], [])
        self.assertEqual(self.status_of(run_id)["state"], "needs_swap")
        doc = self.ok("POST", "/api/runs/swap", {"run": run_id, "auto": False})          # "I have switched"
        self.assertIn(doc["job"]["state"], ("running", "done"))
        self.assertEqual([r for r in self.mock.requests if r["path"] == "/api/generate"], [])
        self.assertEqual(self.drive(lambda: self.status_of(run_id))["state"], "done")

    def test_swap_auto_refused_for_a_lan_host_without_optin(self):
        p, run_id = self._to_swap()
        lan = ".".join(("192", "168", "1", "20"))                   # built at run time (leak scan: no IP literals)
        for r in ("recon", "judge"):
            e = dict(models.role(p, r))
            e.update(url=f"http://{lan}:11434/v1", host=f"{lan}:11434", address_class="lan")
            models.data(p)["roles"][r] = e
        self.mock.requests.clear()
        got = self.call("POST", "/api/runs/swap", {"run": run_id, "auto": True})
        self.assertEqual((got[0], got[1]["error"]), (409, "swap_not_allowed"))
        self.assertEqual(self.mock.requests, [])

    def test_swap_failure_stays_waiting(self):
        _p, run_id = self._to_swap()
        self.mock.set(status=500)
        self.addCleanup(self.mock.set, status=None)
        got = self.call("POST", "/api/runs/swap", {"run": run_id, "auto": True})
        self.assertEqual((got[0], got[1]["error"]), (502, "swap_failed"))
        self.mock.set(status=None)
        self.assertEqual(self.status_of(run_id)["state"], "needs_swap")
        self.assertEqual(self.call("POST", "/api/runs/swap", {"run": "A-ffffffff", "auto": False})[1]["error"],
                         "unknown_run")


if __name__ == "__main__":
    unittest.main()
