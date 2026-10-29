"""Task 44 (slow): the app end to end on the REAL path (spec 15.2, 11 "Endpoint guard", N1).

The shipped synthetic corpus is copied into a project under renamed, code-like pids and file names (``CASE-A01`` ...,
``tests/realpath_corpus``), so its hash differs from the shipped one and the kit treats it as ``kind: real``. Every
step goes through the app's own HTTP routes on a server started per test (token, cookie and CSRF as a browser gets
them). The models are ``tests/mock_models.MockModels`` on 127.0.0.1, answering from the oracle; no real model,
endpoint or API is contacted, and the cloud-metadata address is only ever refused, never connected to.

``test_real_path`` (one project): context, import, pairing, labels, maps; the models (two families, connection
tests, the guard's refusals, a slow server); the model check (a canary failure is refused, then Q, the swap, J);
references 4 A, 4 B (coded from the gold maps, locked), 4 C; the time estimate; all rungs per group (A and C while
B is still coded: results stay shut); results; the four exports; a changed rung-1 prompt; a run stopped by thinking
text and resumed; a malformed reply retried then given up; the kit's own refusals on the app's config (a non-primary
first spec; a temp copy of the kit with an altered prompt file). After every step both kinds of log verify.

``test_one_family_then_gate`` (a second, short project): an unlabelled transcript is refused; one model family gives
AI maps only; then a second family, the automatic swap, and the profile for comparison passes the gate (k = 10, which
the 4-person groups of the first project cannot reach).

``test_oracle_no_drift``: the plain oracle client's run shows "No model at work: no drift possible" on Try it.

Run alone with ``make e2e`` (``python3 -m unittest tests.test_e2e_app -v``); ``make test`` finds it too.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import profile_export, publish
from second_scribe.app import build_site, export, importer, jobs, models, orchestrate, strings
from second_scribe.app.project import Project
from second_scribe.audit import verify as audit_verify
from second_scribe.pipeline import KIT_ROOT
from second_scribe.synth.bank import CONCEPTS
from tests import test_server_security as tss
from tests.addr import ip
from tests.mock_models import MockModels, default_models
from tests.realpath_corpus import DEMO_CONFIG, DEMO_CORPUS, make_realpath_corpus

SLOW = True                       # Task 44: a slow test (under a minute here); `make e2e` runs it alone

DEMO = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
CONTEXT = {k: DEMO["context"][k] for k in ("domain", "phases", "roles")}
RECON_ID, JUDGE_ID, JUDGE_BAD_ID = "qwen-mock:7b", "gemma-mock:9b", "gemma-mock:2b"
JUDGE_SAME_ID = "qwen-mock:14b"                # the reconstructor's family: one family, AI maps only
MOCK_MODELS = {**default_models(), JUDGE_BAD_ID: {"family": "gemma2", "digest": "b" * 64},
               JUDGE_SAME_ID: {"family": "qwen2", "digest": "c" * 64}}
GROUPS = {"A": ["CASE-A01", "CASE-A02", "CASE-B01", "CASE-B02"],
          "B": ["CASE-A03", "CASE-A04", "CASE-B03", "CASE-B04"],
          "C": ["CASE-A05", "CASE-A06", "CASE-A07", "CASE-B05"]}
ALL_RUNGS = ["rung1", "rung2", "rung3"]
# Steps whose failure is reported as a subtest while the later steps still run. None since fix H (prereg accepts the
# mode-B in_transcript items made with no call by construction, L33): every step stops the flow when it fails.
SOFT_STEPS: tuple[str, ...] = ()
DEMO_CONFIG_SHA256 = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"     # the demo hash
POLL_S = 0.1
RUN_TIMEOUT_S = 300.0


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def old_pid(pid: str) -> str:
    """CASE-A01 -> SYN-OW01, CASE-B01 -> SYN-OF01 (tests/realpath_corpus.PREFIX, the other way)."""
    return {"CASE-A": "SYN-OW", "CASE-B": "SYN-OF"}[pid[:6]] + pid[6:]


def role_of(pid: str) -> str:
    return DEMO["corpus"]["roles"][old_pid(pid)]


class E2ECase(tss.ServerCase):
    """The app's server (per test, from ``ServerCase``), a logged-in browser session and the mock models."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self._stop_jobs)
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2e-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.mock = MockModels([DEMO_CONFIG], models=MOCK_MODELS, ping=True)
        self.url = self.mock.start()
        self.addCleanup(self.mock.stop)
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)
        self.view_calls: list[tuple[int, str]] = []          # every /api/view answer: (status, error code)

    def _stop_jobs(self):
        for job in list(self.state.jobs.values()):
            try:
                job.stop()
            except Exception:                    # noqa: BLE001 - cleanup must reach the other jobs
                pass

    # -- HTTP -------------------------------------------------------------------------------------------------
    def get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        doc = json.loads(data)
        if path.startswith("/api/view"):
            self.view_calls.append((status, doc.get("error")))
        return status, doc

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        return status, json.loads(data)

    def ok(self, path, obj):
        status, doc = self.send(path, obj)
        self.assertEqual(status, 200, (path, doc))
        return doc

    def ok_get(self, path):
        status, doc = self.get(path)
        self.assertEqual(status, 200, (path, doc))
        return doc

    def refused(self, answer, status, code, message_key=None, **kw):
        """``answer`` = (status, doc) is the refusal ``status`` / ``code``; with ``message_key`` its message is that
        string. Returns the doc."""
        got, doc = answer
        self.assertEqual((got, doc.get("error")), (status, code), doc)
        if message_key is not None:
            self.assertEqual(doc.get("message"), strings.t(message_key, **kw))
        return doc

    # -- logs -------------------------------------------------------------------------------------------------
    def check_chains(self, project: Project, step: str) -> None:
        """After ``step``: the app log and every run log of ``project`` verify (chain and manifest, from disk)."""
        ok, detail = Project.open(project.root).log.verify()
        self.assertTrue(ok, f"app log after {step}: {detail}")
        for log in sorted(project.path("runs").glob("*/logs/run.jsonl")) if project.path("runs").is_dir() else []:
            manifest = log.with_name("run.manifest.json")
            if not manifest.is_file():                     # a run that has not closed a session yet
                continue
            with mock.patch("sys.stdout"):
                rc = audit_verify.main([str(log), str(manifest)])
            self.assertEqual(rc, 0, f"run log {log.parent.parent.name} after {step}")

    # -- setup steps ------------------------------------------------------------------------------------------
    def create_project(self, name: str) -> Project:
        self.ok("/api/project/create", {"path": str(self.tmp / name), "name": "Study"})
        self.ok("/api/context", CONTEXT)
        return self.state.project

    def import_corpus(self, corpus: Path, pids: list[str]) -> None:
        for pid in pids:
            for kind, folder, suffix in (("transcript", "transcripts", "docx"), ("map", "maps", "vsdx")):
                got = self.ok("/api/import", {"kind": kind, "name": f"{pid}.{suffix}",
                                              "data_b64": b64((corpus / folder / f"{pid}.{suffix}").read_bytes())})
                self.assertEqual(got["file"]["pid"], pid)          # a code-like name stays the participant id

    def confirm_all(self, pids: list[str]) -> None:
        table = self.ok_get("/api/pairing")
        self.assertEqual(sorted(r["pid"] for r in table["rows"] if r["paired"]), sorted(pids))
        self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        for pid in pids:
            labels = self.ok_get(f"/api/labels?pid={pid}")
            offered = {c["label"].casefold(): c["label"] for c in labels["candidates"]}      # what the screen offers
            self.assertEqual(set(offered), {"pt", "iv"}, labels)
            self.ok("/api/labels/confirm", {"pid": pid, "participant": offered["pt"], "interviewer": offered["iv"],
                                            "role": role_of(pid)})
            preview = self.ok_get(f"/api/maps/preview?pid={pid}")
            self.assertTrue(self.ok("/api/maps/confirm", {"pid": pid, "sha256": preview["sha256"]})["confirmed"])

    def set_model(self, role: str, kind: str, model: str, family: str) -> dict:
        url = self.url if kind == "local" else self.url + "/v1"
        found = self.ok("/api/models/discover", {"role": role, "kind": kind, "url": url})
        self.assertIn(model, [m["name"] for m in found["models"]])
        return self.ok("/api/models/set", {"role": role, "kind": kind, "url": url, "model": model,
                                           "family": family})["models"]

    def connection_ok(self, role: str) -> None:
        got = self.ok("/api/models/test", {"role": role})
        self.assertEqual((got["result"]["ok"], got["result"]["error"], got["result"]["thinking_seen"]),
                         (True, None, False), got)

    def model_check(self, *, until=("passed", "failed")) -> dict:
        """Start the model check and drive it like the screen: poll, and press "continue" once the judge is
        loaded (the swap). Returns the final check."""
        self.ok("/api/model-check/start", {})
        seen = []
        t_end = time.time() + RUN_TIMEOUT_S
        while True:
            check = self.ok_get("/api/model-check")["check"]
            if not seen or seen[-1] != check["state"]:
                seen.append(check["state"])
            if check["state"] in until or check["state"] == "error":
                check["seen"] = seen
                return check
            if check["state"] == "needs_swap":
                self.ok("/api/model-check/continue", {})
            self.assertLess(time.time(), t_end, check)
            time.sleep(POLL_S)

    def drive_run(self, job: dict, *, swap_auto: bool = False, until=("done", "failed")) -> dict:
        """Poll ``/api/runs/status`` until the run is in ``until``; a swap is done by the user (``auto`` false) or
        by the app. Returns the last status, with ``seen`` (the states in order)."""
        run_id, seen = job["run_id"], [job["state"]]
        t_end = time.time() + RUN_TIMEOUT_S
        while job["state"] not in until:
            self.assertLess(time.time(), t_end, job)
            if job["state"] == "needs_swap":
                job = self.ok("/api/runs/swap", {"run": run_id, "auto": swap_auto})["job"]
            else:
                time.sleep(POLL_S)
                job = self.ok_get(f"/api/runs/status?run={run_id}")["job"]
            if seen[-1] != job["state"]:
                seen.append(job["state"])
        job["seen"] = seen
        return job

    def stderr_of(self, project: Project, run_id: str) -> str:
        """The tail of each stage session's stderr (for a failure message: kit output, never model text)."""
        out = []
        for f in sorted(self.run_folder(project, run_id).glob("job/*/std*.txt")):
            out.append(f"--- {f.parent.name}/{f.name}\n" + f.read_text(encoding="utf-8", errors="replace")[-1500:])
        return "\n".join(out)

    def run_folder(self, project: Project, run_id: str) -> Path:
        return project.path(f"runs/{run_id}")

    def config_of(self, project: Project, run_id: str) -> dict:
        return json.loads((self.run_folder(project, run_id) / "config.json").read_text(encoding="utf-8"))

    def run_started(self, project: Project) -> list[dict]:
        return [r["payload"] for r in project.log.records("run_started")]


class TestRealPath(E2ECase):
    def test_real_path(self):
        t0 = time.time()
        self.key = "sk-e2e-" + secrets.token_hex(16)            # a generated fake key, held in memory only
        self.corpus = self.tmp / "corpus"
        mapping = make_realpath_corpus(self.corpus)
        self.pids = sorted(mapping.values())
        self.assertEqual(self.pids, sorted(p for g in GROUPS.values() for p in g))
        project = self.create_project("study")
        self.check_chains(project, "create")
        self.runs: dict[str, str] = {}                           # group -> its run of all rungs
        steps = [("import", self.phase_import), ("models", self.phase_models),
                 ("model check", self.phase_model_check), ("references", self.phase_references),
                 ("runs A and C, B open", self.phase_runs_while_b_open), ("B coding", self.phase_b_coding),
                 ("run B", self.phase_run_b), ("results", self.phase_results), ("exports", self.phase_exports),
                 ("own rung-1 prompt", self.phase_rung1_own), ("thinking text", self.phase_thinking),
                 ("malformed reply", self.phase_malformed), ("kit refusals", self.phase_kit_refusals),
                 ("final checks", self.phase_final)]
        for name, step in steps:
            t1 = time.time()
            if name in SOFT_STEPS:                  # its failure is reported, and the steps after it still run
                with self.subTest(step=name):
                    step(project)
            else:
                step(project)
            self.check_chains(project, name)
            print(f"[e2e] {name}: {time.time() - t1:.1f} s", file=sys.stderr)
        print(f"[e2e] done in {time.time() - t0:.0f} s", file=sys.stderr)

    # -- 1. import ----------------------------------------------------------------------------------------------
    def phase_import(self, project):
        self.import_corpus(self.corpus, self.pids)
        self.confirm_all(self.pids)
        kinds = [r["record_type"] for r in project.log.records()]
        self.assertEqual(kinds.count("import"), 24)
        self.assertEqual(kinds.count("labels_confirmed"), 12)
        self.assertEqual(kinds.count("map_confirmed"), 12)

    # -- 2. models ----------------------------------------------------------------------------------------------
    def phase_models(self, project):
        # cloud-model metadata (Ollama's remote_host): refused at Setup, the role is not stored
        self.mock.set(cloud=True)
        found = self.ok("/api/models/discover", {"role": "recon", "kind": "local", "url": self.url})
        self.assertTrue(all(m["remote"] for m in found["models"]), found)
        self.refused(self.send("/api/models/set", {"role": "recon", "kind": "local", "url": self.url,
                                                   "model": RECON_ID, "family": "qwen2"}),
                     422, "cloud_model", "models.error.cloud_model")
        self.assertNotIn("recon", self.ok_get("/api/models")["models"]["roles"])
        self.mock.set(cloud=False)
        # the cloud metadata address: refused by the endpoint guard before any connection is made
        meta = ip(169, 254, 169, 254)
        touched = []
        real_connect, real_create = socket.socket.connect, socket.create_connection

        def connect(sock, address, *a, **kw):
            if str(address[0]).startswith(meta):
                touched.append(address)
                raise OSError("the test refuses this address")
            return real_connect(sock, address, *a, **kw)

        def create(address, *a, **kw):
            if str(address[0]).startswith(meta):
                touched.append(address)
                raise OSError("the test refuses this address")
            return real_create(address, *a, **kw)
        with mock.patch.object(socket.socket, "connect", connect), mock.patch("socket.create_connection", create):
            for kind, url, status, code in (("local", f"http://{meta}:11434", 422, "remote_needs_https"),
                                            ("openai_compat", f"http://{meta}/latest/meta-data", 422,
                                             "remote_needs_https"),
                                            ("openai_compat", f"https://{meta}/v1", 409, "declaration_missing")):
                with self.subTest(url=url):
                    c = self.ok("/api/models/classify", {"role": "judge", "kind": kind, "url": url})
                    self.assertEqual((c["address_class"], c["problem"]), ("remote", code))
                    self.refused(self.send("/api/models/discover", {"role": "judge", "kind": kind, "url": url}),
                                 status, code, f"models.error.{code}")
                    self.refused(self.send("/api/models/set", {"role": "judge", "kind": kind, "url": url,
                                                               "model": JUDGE_ID, "family": "gemma2"}),
                                 status, code, f"models.error.{code}")
        self.assertEqual(touched, [])
        self.assertNotIn("judge", self.ok_get("/api/models")["models"]["roles"])
        # the reconstructor: the local (Ollama-like) server on 127.0.0.1
        self.set_model("recon", "local", RECON_ID, "qwen2")
        # a slow server: the connection test times out, the role is not ready; then it answers in time
        self.mock.set(slow_s=2.0)
        real_test = models.connection_test
        with mock.patch.object(models, "connection_test",
                               lambda r, cfg, **kw: real_test(r, cfg, **{**kw, "timeout": 0.5})):
            slow = self.ok("/api/models/test", {"role": "recon"})
        self.mock.set(slow_s=0.0)
        self.assertEqual((slow["result"]["ok"], slow["result"]["error"]), (False, "timeout"), slow)
        self.assertEqual(slow["message"], strings.t(models.connection_error_key("timeout")))
        self.assertIn({"code": "connection_failed", "role": "recon"},
                      [{k: p[k] for k in ("code", "role")} for p in self.ok_get("/api/models")["models"]["problems"]])
        self.connection_ok("recon")
        # the judge: an OpenAI-compatible endpoint on the same machine that takes a key (pasted, memory only)
        k = self.ok("/api/models/key", {"role": "judge", "key": self.key})
        self.assertEqual(k, {"role": "judge", "key": {"set": True, "source": "pasted"}})
        self.set_model("judge", "openai_compat", JUDGE_BAD_ID, "gemma2")
        self.connection_ok("judge")
        got = self.ok("/api/models/confirm-families", {"recon": "qwen2", "judge": "gemma2"})["models"]
        self.assertEqual((got["pipeline_mode"], got["problems"]), ("two_family", []), got)
        self.assertTrue(got["keys"]["judge"])
        self.assertNotIn(self.key, json.dumps(got))

    # -- 3. model check -----------------------------------------------------------------------------------------
    def phase_model_check(self, project):
        # a canary failure: the check fails and that pair is refused from then on
        self.mock.set(canary_fail=True)
        check = self.model_check()
        self.mock.set(canary_fail=False)
        self.assertEqual(check["state"], "failed", check)
        self.assertIn("needs_swap", check["seen"])                  # one machine: Q, then the judge is loaded, J
        # the canary's planted modality items, judged "actual" every time, fall below the rule
        self.assertTrue(check["failures"] and all(f.startswith("model_check_judge_below_rule:")
                                                  for f in check["failures"]), check)
        self.refused(self.send("/api/model-check/start", {}), 409, "model_check_failed",
                     "model_check.error.model_check_failed")
        # another judge model: a new pair, checked afresh, passes
        self.set_model("judge", "openai_compat", JUDGE_ID, "gemma2")
        self.connection_ok("judge")
        self.ok("/api/models/confirm-families", {"recon": "qwen2", "judge": "gemma2"})
        check = self.model_check()
        self.assertEqual((check["state"], check["failures"]), ("passed", []), check)
        self.assertEqual(check["seen"][:2], ["q_running", "needs_swap"], check)
        recs = project.log.records("model_check")
        self.assertEqual([r["payload"]["passed"] for r in recs], [False, True])

    # -- 4. references, instructions, estimate --------------------------------------------------------------------
    def phase_references(self, project):
        for group, pids in GROUPS.items():
            for pid in pids:
                got = self.ok("/api/references", {"pid": pid, "type": group})
                self.assertEqual(got["type"], group)
        gate = self.ok_get("/api/results/gate")
        self.assertEqual((gate["open"], sorted(gate["waiting"]), gate["revealed"]), (False, GROUPS["B"], False))
        ins = self.ok("/api/instructions", {"rung1": "basic", "rungs": ALL_RUNGS, "preset": "standard"})
        self.assertEqual(ins["run_rungs"], ["rung1", "rung2", "placebo", "rung3"])
        est = self.ok("/api/estimate", {"rungs": ALL_RUNGS})["estimate"]
        self.assertIsInstance(est, dict)

    # -- 5. A and C run while B is coded; nothing AI-derived is shown -------------------------------------------
    def shut(self, project, run_id):
        """Every results route is shut while a B coding is open (409 coding_unlocked)."""
        for answer in (self.get(f"/api/view?run={run_id}"), self.get(f"/api/results/profile?run={run_id}"),
                       self.get(f"/api/export/list?run_id={run_id}"),
                       self.send("/api/export/report", {"run_id": run_id})):
            self.refused(answer, 409, "coding_unlocked")
        self.assertEqual(project.log.records("reveal"), [])

    def start_run(self, group, rungs=ALL_RUNGS):
        return self.ok("/api/runs/start", {"group": group, "rungs": rungs, "preset": "standard"})["job"]

    def phase_runs_while_b_open(self, project):
        self.refused(self.send("/api/runs/start", {"group": "B", "rungs": ALL_RUNGS, "preset": "standard"}),
                     409, "coding_unlocked")
        for group in ("A", "C"):
            job = self.start_run(group)
            self.assertEqual(job["state"], "running", job)
            self.shut(project, job["run_id"])                       # while it runs
            job = self.drive_run(job, until=("needs_swap", "done", "failed"))
            self.assertEqual(job["state"], "needs_swap", job)
            # both models sit on one machine; the app may not switch them itself on a server that is not Ollama
            self.refused(self.send("/api/runs/swap", {"run": job["run_id"], "auto": True}), 409, "swap_not_allowed")
            job = self.drive_run(job)
            self.assertEqual((job["state"], job["rc"], job["stages"]), ("done", 0, ["Q", "J", "M"]), job)
            self.assertEqual(job["seen"], ["needs_swap", "running", "done"], job)
            self.runs[group] = job["run_id"]
            self.shut(project, job["run_id"])                       # done, but B is still open
            self.check_chains(project, f"run {group}")
        started = self.run_started(project)
        self.assertEqual([s["group"] for s in started], ["A", "C"])

    # -- 6. B: coded blind from the gold maps' steps, locked ----------------------------------------------------
    def gold_marks(self, pid, turns):
        """One mark per gold step the participant names: the whole participant turn that holds the step's transcript
        wording (the kit's whole-turn rule), with the step's phase and status."""
        old = old_pid(pid)
        gold = json.loads((DEMO_CORPUS / "gold" / f"{old}.json").read_text(encoding="utf-8"))
        cids = json.loads((DEMO_CORPUS / "gold" / f"{old}.concepts.json").read_text(encoding="utf-8"))["gold"]
        wording = {c.cid: c.forms[1].casefold() for c in CONCEPTS}
        marks = []
        for node in gold["nodes"]:
            words = wording[cids[node["node_id"]]]
            at = next((i for i, t in enumerate(turns) if t["speaker"] == "participant" and words in t["text"].casefold()),
                      None)
            if at is None:
                continue
            text = turns[at]["text"]
            marks.append({"pid": pid, "turn": at, "start": 0, "end": len(text), "text": text,
                          "status": "happened" if node["status"] == "experienced" else "missing",
                          "step": node["text"], "phase": node["phase"]})
        return marks

    def phase_b_coding(self, project):
        for i, pid in enumerate(GROUPS["B"]):
            view = self.ok_get(f"/api/coding?pid={pid}")
            self.assertEqual((view["type"], view["locked"], view["phases"]), ("B", False, CONTEXT["phases"]))
            marks = self.gold_marks(pid, view["turns"])
            self.assertGreaterEqual(len(marks), 3, pid)
            for n, m in enumerate(marks, 1):
                self.assertEqual(self.ok("/api/coding/mark", m)["n_marks"], n)
            self.ok("/api/coding/tick", {"pid": pid, "seconds": 60})
            locked = self.ok("/api/coding/lock", {"pid": pid})
            self.assertEqual(locked["n_marks"], len(marks))
            gate = self.ok_get("/api/results/gate")
            self.assertEqual(gate["open"], i == len(GROUPS["B"]) - 1, gate)
            if gate["open"]:
                self.gate_opened_at = len(self.view_calls)          # every /api/view answer before this: refused
            if not gate["open"]:
                self.shut(project, self.runs["A"])
        self.assertEqual(len(project.log.records("coding_locked")), 4)
        self.assertEqual(project.log.records("reveal"), [])

    def phase_run_b(self, project):
        job = self.drive_run(self.start_run("B"))
        self.assertEqual((job["state"], job["rc"]), ("done", 0), (job, self.stderr_of(project, job["run_id"])))
        self.runs["B"] = job["run_id"]
        cfg = self.config_of(project, job["run_id"])
        self.assertEqual(sorted(cfg["corpus"]["pairs"]), GROUPS["B"])
        self.assertEqual(cfg["tool"]["reference_type"], "B")

    # -- 7. results ---------------------------------------------------------------------------------------------
    def phase_results(self, project):
        listed = {r["run_id"]: r for r in self.ok_get("/api/results/runs")["runs"]}
        for group, run_id in self.runs.items():
            self.assertEqual((listed[run_id]["group"], listed[run_id]["finished"]), (group, True))
            cfg = self.config_of(project, run_id)
            self.assertEqual(sorted(cfg["corpus"]["pairs"]), GROUPS[group])
            self.assertEqual(cfg["corpus"]["kind"], "real")
            self.assertEqual(cfg["ladder"]["rungs"], ["rung1", "rung2", "placebo", "rung3"])
            view = self.ok_get(f"/api/view?run={run_id}")
            self.assertEqual((view["run_id"], view["group"], view["view"]["source"]), (run_id, group, "own"))
            self.assertEqual(sorted(m["pid"] for m in view["view"]["maps"]), GROUPS[group])
            prof = self.ok_get(f"/api/results/profile?run={run_id}")
            # four people per group: the exporter refuses below its floor of ten, and the screen gets its reasons
            self.assertNotIn("profile", prof)
            self.assertEqual(prof["refused"], [profile_export.k_floor_reason(4)],
                             prof)
        self.assertEqual(len(project.log.records("reveal")), 1)

    # -- 8. the four exports ------------------------------------------------------------------------------------
    def phase_exports(self, project):
        for group, run_id in self.runs.items():
            report = self.ok("/api/export/report", {"run_id": run_id})
            self.assertTrue(project.path(report["path"]).is_file())
            methods = self.ok("/api/export/methods/save", {"run_id": run_id})
            self.assertTrue(project.path(methods["path"]).is_file())
            cert = self.ok("/api/export/certificate/save", {"run_id": run_id})
            self.assertTrue(cert["all_logs_verify"], cert)
            pv = self.ok("/api/export/profile/preview", {"run_id": run_id, "k": export.K_FLOOR})
            self.assertFalse(pv["exportable"], pv)
            self.assertEqual([r["code"] for r in pv["refusals"]], ["k_floor"], pv)
            self.assertNotIn("n_analysed", pv["refusals"][0]["message"], "plain words, not the exporter's (R12)")
            self.refused(self.send("/api/export/profile/save", {"run_id": run_id, "k": export.K_FLOOR,
                                                                "sha256": pv["sha256"]}), 409, "no_preview")
            outs = {o["kind"]: o["saved"] for o in self.ok_get(f"/api/export/list?run_id={run_id}")["outputs"]}
            self.assertEqual(outs, {"report": True, "profile": False, "methods": True, "certificate": True})
            self.check_chains(project, f"exports of {group}")
        self.assertEqual(list(project.path("exports").glob("profile*")), [], "a refused run saves no profile")

    # -- 9. a changed rung-1 prompt: a new config, a new run -----------------------------------------------------
    def phase_rung1_own(self, project):
        first = self.runs["A"]
        before = {s["run_id"]: s["config_sha256"] for s in self.run_started(project)}
        self.assertNotIn("rung1_user", self.config_of(project, first)["tool"])
        own = ("Read the interview below and draw the participant's journey as a map of steps, "
               "in the order the participant lived them.")
        ins = self.ok("/api/instructions", {"rung1": "own", "rung1_text": own, "rungs": ALL_RUNGS,
                                            "preset": "standard"})["instructions"]
        self.assertEqual(ins["rung1"], "own")
        job = self.drive_run(self.start_run("A"))
        self.assertEqual((job["state"], job["rc"]), ("done", 0), (job, self.stderr_of(project, job["run_id"])))
        new = job["run_id"]
        self.assertNotEqual(new, first)
        after = {s["run_id"]: s["config_sha256"] for s in self.run_started(project)}
        self.assertEqual(set(after) - set(before), {new})
        self.assertNotEqual(after[new], before[first])
        tool = self.config_of(project, new)["tool"]
        self.assertEqual(tool["rung1_user"], ins["rung1_user"])
        self.assertIn(own, json.dumps(tool["rung1_user"]))
        self.runs["A-own"] = new
        self.ok("/api/instructions", {"rung1": "basic", "rungs": ALL_RUNGS, "preset": "standard"})

    # -- 10. thinking text: the run stops (exit 4) before any participant call; resumed once it is off --------------
    def phase_thinking(self, project):
        self.mock.set(thinking=True)
        try:
            job = self.drive_run(self.start_run("C"))
        finally:
            self.mock.set(thinking=False)
        self.assertEqual((job["state"], job["rc"], job["code"], job["done"], job["resumable"]),
                         ("failed", 4, "stopped", 0, True), (job, self.stderr_of(project, job["run_id"])))
        self.assertEqual(job["message"], strings.t("runs.exit.stopped", audit="preflight"))
        self.assertNotIn("payload", json.dumps(job))
        job = self.ok("/api/runs/resume", {"run": job["run_id"]})["job"]
        job = self.drive_run(job)
        self.assertEqual((job["state"], job["rc"]), ("done", 0), (job, self.stderr_of(project, job["run_id"])))
        self.runs["C-resumed"] = job["run_id"]

    # -- 11. a malformed reply: retried, then that item is given up and the run goes on ---------------------------
    def phase_malformed(self, project):
        from second_scribe.transcripts import read_transcript_paras
        pid = GROUPS["C"][0]
        others = [" ".join(read_transcript_paras(self.corpus / "transcripts" / f"{q}.docx")) for q in self.pids
                  if q != pid]
        paras = read_transcript_paras(self.corpus / "transcripts" / f"{pid}.docx")
        needle = next(x.split("]", 1)[-1].strip()[:60] for x in sorted(paras, key=len, reverse=True)
                      if not any(x.split("]", 1)[-1].strip()[:60] in o for o in others))
        self.mock.set(malformed_match=needle)
        try:
            job = self.drive_run(self.start_run("C"))
        finally:
            self.mock.set(malformed_match=None)
        run_id = job["run_id"]
        log = self.run_folder(project, run_id) / "logs" / "run.jsonl"
        recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        reqs = {r["payload"]["request_id"]: r["payload"] for r in recs if r["record_type"] == "call_request"}
        errs = [r["payload"] for r in recs if r["record_type"] == "call_error"]
        bad = [reqs[e["request_id"]] for e in errs if "malformed response" in e["error"]]
        self.assertTrue(bad, (job, self.stderr_of(project, run_id)))
        self.assertEqual({b["item_id"].split("__")[0] for b in bad}, {pid}, sorted({b["item_id"] for b in bad}))
        by_item: dict[tuple, list] = {}
        for b in bad:
            by_item.setdefault((b["stage"], b["item_id"]), []).append(b["attempt"])
        self.assertTrue(all(sorted(a) == [1, 2, 3] for a in by_item.values()), by_item)    # tried three times
        work = self.run_folder(project, run_id) / "work"
        recon = [json.loads(f.read_text(encoding="utf-8")) for f in work.glob("recon/*.json") if pid in f.name]
        self.assertTrue(recon and all(it["status"] == "failed" for it in recon), recon)   # then given up
        self.malformed = (job, run_id, pid)
        self.assertEqual(job["state"], "done", (job, self.stderr_of(project, run_id)))
        self.runs["C-malformed"] = run_id

    # -- 12. the kit's own refusals on the app's config ---------------------------------------------------------
    def kit_run(self, config: Path, env: dict, cwd: Path = KIT_ROOT) -> subprocess.CompletedProcess:
        """One kit session as RunJob starts it (``cli run --stages Q --client http``), in ``cwd``."""
        return subprocess.run([sys.executable, "-m", "second_scribe.cli", "run", "--config", str(config),
                               "--stages", "Q", "--client", "http"], cwd=str(cwd), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def config_copy(self, project, run_id: str, folder: Path, change) -> Path:
        cfg = self.config_of(project, run_id)
        folder.mkdir(parents=True)
        cfg["out_dir"] = str(folder)
        change(cfg)
        path = folder / "config.json"
        path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def phase_kit_refusals(self, project):
        env = orchestrate.run_env(project, self.state.secrets)
        self.assertIn(self.key, env.values())        # the key travels in the child's environment only (positive control)
        calls = len(self.mock.requests)
        # a non-primary first spec: refused before any call (pipeline: the primary-spec check on a real corpus)

        def not_primary(cfg):
            cfg["specs"][0] = {**cfg["specs"][0], "arm": "full"}
        cfg = self.config_copy(project, self.runs["A"], self.tmp / "refusals" / "not-primary", not_primary)
        done = self.kit_run(cfg, env)
        self.assertNotEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("the first spec must be the primary setting", done.stderr)
        with self.subTest(refusal="non-primary first spec: exit 2"):     # on 7ff9be7 the ValueError escapes cli.main
            self.assertEqual(done.returncode, 2, done.stderr[-2000:])   # (exit 1 with a traceback; reported, T44)
        # a temp copy of the kit whose prompts/ holds an edited file: refused (the prompt manifest), exit 2
        kit = self.tmp / "kit-copy"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(KIT_ROOT / "second_scribe", kit / "second_scribe", ignore=ignore)
        shutil.copytree(KIT_ROOT / "prompts", kit / "prompts", ignore=ignore)
        shutil.copytree(KIT_ROOT / "configs", kit / "configs", ignore=ignore)
        shutil.copytree(KIT_ROOT / "demo" / "corpus", kit / "demo" / "corpus", ignore=ignore)
        edited = kit / "prompts" / "prior_only.p0.v2.txt"
        edited.write_bytes(edited.read_bytes() + b"\nAlso list every office the participant visited.\n")
        cfg = self.config_copy(project, self.runs["A"], self.tmp / "refusals" / "prompts", lambda c: None)
        done = self.kit_run(cfg, env, cwd=kit)
        self.assertNotEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("the prompt files differ from prompts/MANIFEST.json", done.stderr)
        with self.subTest(refusal="edited prompt file: exit 2"):
            self.assertEqual(done.returncode, 2, done.stderr[-2000:])
        self.assertIn("prior_only.p0.v2.txt", done.stderr)
        # the worktree's own prompts are untouched, and no model was called by either session
        from second_scribe import prompt_manifest
        self.assertEqual(prompt_manifest.verify_manifest(KIT_ROOT / "prompts"), [])
        self.assertEqual(len(self.mock.requests), calls)
        for leftover in (self.tmp / "refusals").rglob("logs/run.jsonl"):
            self.assertNotIn(b'"call_request"', leftover.read_bytes())

    # -- 13. what holds at the end ------------------------------------------------------------------------------
    def phase_final(self, project):
        # every finished run: the log verifies (check_chains), the work store matches it, M's leak audit passed
        finished = {g: r for g, r in self.runs.items()}
        for name, run_id in finished.items():
            with self.subTest(run=name):
                folder = self.run_folder(project, run_id)
                self.assertIsNone(audit_verify.work_integrity_problem(folder / "logs" / "run.jsonl", folder / "work"))
                m = json.loads((folder / "audits" / "M.json").read_text(encoding="utf-8"))
                leaks = [r for r in m["results"] if r["name"] == "leaks"]
                self.assertEqual([r["passed"] for r in leaks], [True], m["results"])
        # every lock precedes the reveal; one reveal; every /api/view answer before the gate opened was refused
        kinds = [r["record_type"] for r in project.log.records()]
        self.assertEqual(kinds.count("reveal"), 1)
        locks = [i for i, k in enumerate(kinds) if k == "coding_locked"]
        self.assertEqual(len(locks), 4)
        self.assertLess(max(locks), kinds.index("reveal"))
        before = self.view_calls[:self.gate_opened_at]
        self.assertTrue(before)
        self.assertEqual(set(before), {(409, "coding_unlocked")})
        self.assertTrue(all(c == (200, None) for c in self.view_calls[self.gate_opened_at:]), self.view_calls)
        # the key string is in no file: not under the project, not anywhere in the temp folder of this run
        needle = self.key.encode("utf-8")
        found = set()
        for top in {self.tmp, Path(tempfile.gettempdir()).resolve()}:
            for f in top.rglob("*"):
                if f.is_file() and not f.is_symlink():
                    try:
                        if needle in f.read_bytes():
                            found.add(str(f))
                    except OSError:
                        pass
        self.assertEqual(found, set())
        self.assertTrue(project.path("runs").is_dir() and any(project.path("runs").rglob("*.jsonl")))



class TestOneFamilyThenGate(E2ECase):
    """A second, short project: an unlabelled transcript is refused, a one-family pair yields AI maps only, then a
    second family on the same Ollama-like server (the app swaps itself) yields a profile that passes the gate."""

    def test_one_family_then_gate(self):
        corpus = self.tmp / "corpus"
        pids = sorted(make_realpath_corpus(corpus).values())
        project = self.create_project("short")
        self.import_corpus(corpus, pids)
        # a transcript in plain prose (no speaker labels): imported, then refused when its labels are confirmed
        prose = b"We talked about the move over coffee.\n\nNothing else was planned for that week.\n"
        self.ok("/api/import", {"kind": "transcript", "name": "CASE-B06.txt", "data_b64": b64(prose)})
        table = self.ok_get("/api/pairing")
        self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        labels = self.ok_get("/api/labels?pid=CASE-B06")
        self.assertEqual(labels["format"], "none", labels)
        guess = [c["label"] for c in labels["candidates"]][:2] + ["pt", "iv"]      # what the screen offers, or not
        refusal = self.refused(self.send("/api/labels/confirm", {"pid": "CASE-B06", "participant": guess[0],
                                                                 "interviewer": guess[1], "role": role_of("CASE-B01")}),
                               422, "no_speaker_labels")
        self.assertIn(importer.EXAMPLE_URL, json.dumps(refusal))
        self.assertEqual([r for r in project.log.records("labels_confirmed") if r["payload"]["pid"] == "CASE-B06"],
                         [])
        for pid in pids:
            labels = self.ok_get(f"/api/labels?pid={pid}")
            offered = {c["label"].casefold(): c["label"] for c in labels["candidates"]}
            self.ok("/api/labels/confirm", {"pid": pid, "participant": offered["pt"], "interviewer": offered["iv"],
                                            "role": role_of(pid)})
            preview = self.ok_get(f"/api/maps/preview?pid={pid}")
            self.ok("/api/maps/confirm", {"pid": pid, "sha256": preview["sha256"]})
        self.check_chains(project, "import")
        # one family: the judge is a second Qwen model, so the app runs the reconstructor and the maps only
        self.set_model("recon", "local", RECON_ID, "qwen2")
        self.set_model("judge", "local", JUDGE_SAME_ID, "qwen2")
        self.connection_ok("recon")
        self.connection_ok("judge")
        got = self.ok("/api/models/confirm-families", {"recon": "qwen2", "judge": "qwen2"})["models"]
        self.assertEqual(got["pipeline_mode"], "ai_maps_only", got)
        check = self.model_check()
        self.assertEqual((check["state"], check["failures"]), ("passed", []), check)
        self.assertNotIn("needs_swap", check["seen"])                # no judge part: Q only
        self.assertEqual(self.ok("/api/references/bulk", {"confirmed_maps": "A"})["set"], pids)
        self.ok("/api/instructions", {"rung1": "basic", "rungs": ["rung2"], "preset": "standard"})
        before = len(self.mock.requests)
        job = self.drive_run(self.ok("/api/runs/start", {"group": "A", "rungs": ["rung2"],
                                                         "preset": "standard"})["job"])
        self.assertEqual((job["state"], job["rc"], job["stages"]), ("done", 0, ["Q", "M"]),
                         (job, self.stderr_of(project, job["run_id"])))
        asked = {r["model"] for r in self.mock.requests[before:]}
        self.assertNotIn(JUDGE_SAME_ID, asked)                       # the judge was never asked
        self.check_chains(project, "AI-maps-only run")
        prof = self.ok_get(f"/api/results/profile?run={job['run_id']}")
        self.assertNotIn("profile", prof)
        # no judge, no judged items: nobody is analysed, so the exporter refuses (its floor is ten people)
        self.assertEqual(prof["refused"], [profile_export.k_floor_reason(0)])
        # a second family on the same server: the app may swap the models itself
        self.mock.set(unload_ok=True)
        self.set_model("judge", "local", JUDGE_ID, "gemma2")
        self.connection_ok("judge")
        got = self.ok("/api/models/confirm-families", {"recon": "qwen2", "judge": "gemma2"})["models"]
        self.assertEqual(got["pipeline_mode"], "two_family", got)
        self.assertTrue(jobs.swap_allowed(project))
        check = self.model_check()
        self.assertEqual((check["state"], check["failures"]), ("passed", []), check)
        job = self.drive_run(self.ok("/api/runs/start", {"group": "A", "rungs": ["rung2"],
                                                         "preset": "standard"})["job"], swap_auto=True)
        self.assertEqual((job["state"], job["rc"], job["stages"]), ("done", 0, ["Q", "J", "M"]),
                         (job, self.stderr_of(project, job["run_id"])))
        self.assertIn("needs_swap", job["seen"])
        unloads = [r["model"] for r in self.mock.requests if r["path"] == "/api/generate" and r["body"]
                   and r["body"].get("keep_alive") in (0, "0")]
        self.assertIn(RECON_ID, unloads)                             # the app unloaded the reconstructor itself
        self.check_chains(project, "two-family run")
        # the numbers file: previewed at k = 10, saved, and it passes the publish gate
        pv = self.ok("/api/export/profile/preview", {"run_id": job["run_id"], "k": export.K_FLOOR})
        self.assertTrue(pv["exportable"], pv)
        self.assertEqual(pv["refusals"], [])
        saved = self.ok("/api/export/profile/save", {"run_id": job["run_id"], "k": export.K_FLOOR,
                                                     "sha256": pv["sha256"]})
        self.assertEqual(saved["path"], export.profile_rel(job["run_id"])[0])
        doc = json.loads(project.path(export.profile_rel(job["run_id"])[0]).read_text(encoding="utf-8"))
        self.assertIsNone(publish.assert_publishable(doc, export.K_FLOOR, export.SCHEMA))
        logged = [r["payload"] for r in project.log.records("export") if r["payload"]["kind"] == "profile"]
        self.assertEqual([(e["run_id"], e["sha256"], e["k"]) for e in logged],
                         [(job["run_id"], pv["sha256"], export.K_FLOOR)])
        self.check_chains(project, "profile export")


class TestOracleNoDrift(unittest.TestCase):
    """The demo run with the oracle client: the Try it page says no model was at work, so no drift is possible."""

    def test_oracle_run_shows_no_drift_line(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-e2e-oracle-") as d:
            d = Path(d)
            done = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(d / "run"),
                                   "--client", "oracle"], cwd=str(KIT_ROOT), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            with (d / "run" / "logs" / "run.jsonl").open(encoding="utf-8") as f:
                start = json.loads(f.readline())["payload"]
            self.assertEqual(start["models"], {"recon": "oracle", "judge": "oracle"})
            self.assertEqual(start["config_sha256"], DEMO_CONFIG_SHA256)      # the demo's config, unchanged
            tryit = build_site.write_tryit_js(d / "run", d / "tryit.js")
            with mock.patch("sys.stdout"):
                self.assertEqual(build_site.main(["tryit-profile", "--run", str(d / "run"),
                                                  "--out", str(d / "profile.json")]), 0)
            build_site.build_site(d / "site", tryit=tryit, tryit_profile=d / "profile.json")
            page = (d / "site" / "try.html").read_text(encoding="utf-8")
            self.assertIn(strings.t("try.oracle_state"), page)
            self.assertEqual(strings.t("try.oracle_state"), "No model at work: no drift possible")
            # a view whose reconstructor is a model gets no such line
            table = strings.load_strings("en")
            view = {"run": {"models": {"recon": RECON_ID}}}
            self.assertNotIn(strings.t("try.oracle_state"), "".join(build_site._try_slots(view, [], None,
                                                                                          table).values()))


if __name__ == "__main__":
    unittest.main()
