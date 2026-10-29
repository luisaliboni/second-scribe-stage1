"""Kit subprocess jobs (Task 35; Task 39 extends this module with the Q/J/M runner, pause/resume and the model swap).

``Job`` runs one kit command, ``python -m second_scribe...``, as a child process:

- the interpreter is always ``sys.executable`` and the working folder is the kit root;
- stdout and stderr go to files in the job folder (``stdout.txt``, ``stderr.txt``) and are never sent to the browser
  as they are: the app maps the exit code to a fixed message (``EXIT_CODES``);
- API keys reach the child only through its environment (``child_env`` with ``api.Secrets.env_for``), never argv,
  never a file;
- ``poll()`` gives ``{"state", "rc", "progress"}``; progress is the number of ``call_response`` records in the kit
  run log the job writes (counted from the record type only, never a payload) and the expected total, if known.

``ModelCheck`` is the Setup model check of the project's current model pair (spec 6.5 step 7, Task 17's
``model-check``): a Q session, then (after the user loads the judge, when both models share one local server) a J
session that also runs M and scores the canary. On the end it appends ``model_check{passed, counts, models, digests,
prompt_set_sha256, sha256, ...}`` to the app log and stores the result in ``project.data["model_check"]``;
``model_check_input`` is what Task 38 puts in ``tool.model_check``. A failed check is final for that pair: there is
no override, only another model. Its progress is one count of every call of the check, both parts together (the
logged calls, the determinism re-asks and the judge check, ``timing.model_check_parts``), read per session from where
that session's log began, with a ``now`` code for what it is doing and a ``server`` signal (``LogTail.server``) when
the model server stops answering.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import model_check as kit_model_check
from ..pipeline import KIT_ROOT
from . import models

STATES = ("running", "done", "failed", "paused")
# The kit's exit codes (cli.py) -> the code the app shows (strings ``jobs.exit.<code>``).
EXIT_CODES = {0: "ok", 1: "verify_failed", 2: "refused", 3: "fixture_missing", 4: "stopped"}
TERMINATE_WAIT_S = 10.0
_RESPONSE = b'"record_type":"call_response"'


def child_env(extra: dict[str, str]) -> dict[str, str]:
    """The environment of a kit child process: this process's environment without any ``SS_`` variable (so a stray
    ``SS_RECON_URL`` in the user's shell never reaches the kit), plus ``extra`` (``models.role_env``, which includes
    ``Secrets.env_for``). A ``SS_<P>_KEY_ENV`` that names a variable of the app's own environment (a key the user
    keeps in their shell) keeps that one variable."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
    for k, v in extra.items():
        if k.endswith("_KEY_ENV") and v not in extra and v in os.environ:
            env[v] = os.environ[v]
    env.update(extra)
    env["PYTHONUNBUFFERED"] = "1"
    return env


class Job:
    """One kit command as a child process. ``argv`` are the interpreter's arguments (``["-m", "second_scribe.cli",
    ...]``); the process is ``[sys.executable, *argv]`` in ``cwd`` (the kit root). ``log_path`` is the kit run log
    the command writes (for progress); ``job_dir`` (default: ``log_path``'s run folder ``/job``) gets stdout.txt and
    stderr.txt. ``total`` is the expected number of model calls, shown with the progress."""

    def __init__(self, argv: list[str], env: dict, cwd: Path, log_path: Path, *, job_dir: Path | None = None,
                 total: int | None = None) -> None:
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise TypeError("argv must be a list of strings")
        self.argv = [sys.executable, *argv]
        self.env = dict(env)
        self.cwd = Path(cwd)
        self.log_path = Path(log_path)
        self.job_dir = Path(job_dir) if job_dir is not None else self.log_path.parent.parent / "job"
        self.total = total
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self._paused = False
        self._offset = 0
        self._done_calls = 0

    def start(self) -> "Job":
        if self.proc is not None:
            raise RuntimeError("a job starts once")
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        with open(self.job_dir / "stdout.txt", "ab") as out, open(self.job_dir / "stderr.txt", "ab") as err:
            self.proc = subprocess.Popen(self.argv, cwd=str(self.cwd), env=self.env, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=err, close_fds=True)
        return self

    @property
    def rc(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def _progress(self) -> dict:
        """``call_response`` records written so far (read on from where the last poll stopped, whole lines only)."""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n") + 1
        if end:
            self._done_calls += sum(1 for line in chunk[:end].splitlines() if _RESPONSE in line)
            self._offset += end
        return {"done": self._done_calls, "total": self.total}

    def poll(self) -> dict:
        rc = self.rc
        if self.proc is None:
            state = "paused" if self._paused else "running"
        elif rc is None:
            state = "running"
        elif self._paused:
            state = "paused"
        else:
            state = "done" if rc == 0 else "failed"
        return {"state": state, "rc": rc, "progress": self._progress()}

    def wait(self, timeout: float | None = None) -> int:
        if self.proc is None:
            raise RuntimeError("the job has not started")
        return self.proc.wait(timeout)

    def terminate(self) -> None:
        """Stop the child (SIGTERM, then kill after ``TERMINATE_WAIT_S``); the job reads ``paused``: the same command
        run again resumes item by item."""
        self._paused = True
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


# --- the Setup model check ---------------------------------------------------------------------------------------

MODEL_CHECK_DIR = "model_check"
CHECK_STATES = ("not_run", "q_running", "needs_swap", "j_running", "passed", "failed", "error")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_rel(project) -> str:
    """The pair's model-check folder, relative to the project: ``model_check/<first 12 hex of pair_sha256>``."""
    return f"{MODEL_CHECK_DIR}/{models.pair_sha256(project)[:12]}"


def stored(project) -> dict | None:
    """The stored result for the CURRENT pair (``project.data["model_check"]``), or None (none, or another pair's)."""
    d = project.data.get("model_check")
    if isinstance(d, dict) and d.get("pair_sha256") == models.pair_sha256(project):
        return d
    return None


def model_check_state(project) -> str:
    """``"passed"``, ``"failed"`` or ``"missing"`` (no check of the current pair, or its file changed)."""
    d = stored(project)
    if d is None:
        return "missing"
    if d.get("passed") is not True:
        return "failed"
    return "passed" if model_check_input(project) is not None else "missing"


def model_check_input(project) -> dict | None:
    """``tool.model_check`` for Task 38: ``{"path": <absolute model_check.json>, "sha256": <of the file>}`` when the
    current pair's check passed and its file is unchanged; else None."""
    d = stored(project)
    if d is None or d.get("passed") is not True:
        return None
    try:
        path = project.path(d["path"])
        if sha256_file(path) != d.get("sha256"):
            return None
    except Exception:                    # noqa: BLE001 - a missing, moved or outside file is no input
        return None
    return {"path": str(path.resolve()), "sha256": d["sha256"]}


def require_model_check(project) -> dict:
    """``model_check_input`` or ``ApiError`` 409 ``model_check_failed`` / ``model_check_missing``."""
    from . import strings
    from .api import ApiError
    state = model_check_state(project)
    if state == "failed":
        raise ApiError(409, "model_check_failed", strings.t("model_check.error.model_check_failed"))
    got = model_check_input(project)
    if got is None:
        raise ApiError(409, "model_check_missing", strings.t("model_check.error.model_check_missing"))
    return got


def _role_map(d: dict) -> str:
    return ",".join(f"{r}={v}" for r, v in sorted(d.items()))


def needs_swap(project) -> bool:
    """True when the reconstructor and the judge are two models on one local server, so one machine holds them one
    after the other (the user loads the judge between Q and J; Task 39 adds the automatic swap)."""
    rec, jud = models.role(project, "recon"), models.role(project, "judge")
    if not rec or not jud:
        return False
    if rec.get("address_class") == "remote" or jud.get("address_class") == "remote":
        return False
    try:
        return models.server_root(rec["url"]) == models.server_root(jud["url"])
    except (KeyError, TypeError, ValueError):
        return False


CHECK_NOW = ("starting", "model_calls", "determinism", "finishing", "judge_check", "waiting_for_judge",
             "switching_models")


class ModelCheck:
    """The model check of the project's pair as it is at ``start()``. Call ``poll()`` (under ``state.lock``) to read
    and advance it; ``proceed()`` continues from ``needs_swap``.

    ``progress()`` (in ``status()`` while the check runs or waits for the judge): ``{"done", "total", "part",
    "parts": [{"part", "role", "done", "total"}], "now", "determinism_k", "server"}``. ``done`` and ``total`` are
    every call of the check, both parts together. Each part's total is its calls not yet in the log when this check
    started (an earlier session of the same pair's check may have done some; the kit resumes item by item), plus its
    determinism re-asks and, for the judge part, the judge check; it never falls below what is done. A part's
    logged calls are counted from where its session's log began, never from an earlier session; its re-asks (sent
    straight to the model, not logged) count once the session's ``audit_summary`` record is written. ``now`` is one of
    ``CHECK_NOW``. ``server`` is ``LogTail.server`` of the running session (also when the re-asks go quiet for
    ``2 * SERVER_QUIET_S``), else None."""

    def __init__(self, project, secrets) -> None:
        from . import timing
        self.project = project
        self.secrets = secrets
        self.pair = models.pair_sha256(project)
        self.rel = check_rel(project)
        self.out_dir = project.path(self.rel)
        self.two_roles = models.pipeline_mode(project) == "two_family"
        self.state = "not_run"
        self.job: Job | None = None
        self.error: str | None = None
        self.result: dict | None = None
        self.plan = timing.model_check_parts(judge=self.two_roles)
        self.base = {"Q": 0, "J": 0}
        self.tails: dict[str, LogTail] = {}
        self.judge_check_tail: LogTail | None = None
        self.frozen: dict[str, int] = {}
        self.switching = False

    # -- the sessions -------------------------------------------------------------------------------------------
    def env(self) -> dict:
        extra = models.role_env(self.project, self.secrets, "recon")
        if self.two_roles:
            extra.update(models.role_env(self.project, self.secrets, "judge"))
        return child_env(extra)

    def argv(self, stages: str) -> list[str]:
        fields = models.tool_fields(self.project)
        roles = ("recon", "judge") if self.two_roles else ("recon",)
        fams = {r: f for r, f in fields["model_families"].items() if r in roles}
        digs = {r: d for r, d in fields["model_digests"].items() if r in roles}
        out = ["-m", "second_scribe.cli", "model-check", "--out", str(self.out_dir), "--stages", stages]
        if fams:
            out += ["--families", _role_map(fams)]
        if digs:
            out += ["--digests", _role_map(digs)]
        eps = [e for e in models.remote_endpoints(self.project) if e["role"] in roles]
        if eps:
            f = self.out_dir / "remote_endpoints.json"
            f.write_text(json.dumps(eps, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            out += ["--remote-endpoints", str(f)]
        ops = [o for o in models.endpoint_optins(self.project) if o["role"] in roles]       # audit K7: always passed
        f = self.out_dir / "endpoint_optins.json"
        f.write_text(json.dumps(ops, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        out += ["--endpoint-optins", str(f)]
        return out

    def _run(self, stages: str) -> None:
        now = time.time()
        if stages == "J" and "Q" in self.tails:
            self.frozen["Q"] = self._part_done("Q")
        self.tails[stages] = LogTail.at_end(kit_model_check.log_path(self.out_dir), started=now)
        if stages == "J":
            self.judge_check_tail = LogTail.at_end(kit_model_check.judge_check_log(self.out_dir), started=now)
        self.job = Job(self.argv(stages), self.env(), KIT_ROOT, kit_model_check.log_path(self.out_dir),
                       job_dir=self.out_dir / "jobs" / stages).start()
        self.state = "q_running" if stages == "Q" else "j_running"

    def start(self) -> "ModelCheck":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.base = dict(LogTail(kit_model_check.log_path(self.out_dir)).read().counts)
        self._run("Q")
        return self

    def proceed(self) -> None:
        """Run the judge part (from ``needs_swap``; the models were switched by the user or by ``swap_models``)."""
        if self.state != "needs_swap":
            raise RuntimeError("the model check is not waiting for the judge")
        self.switching = False
        self._run("J")

    # -- reading and advancing ----------------------------------------------------------------------------------
    def poll(self) -> dict:
        if self.job is not None and self.state in ("q_running", "j_running"):
            p = self.job.poll()
            if p["state"] != "running":
                self._ended(p["rc"])
        return self.status()

    def _ended(self, rc: int | None) -> None:
        stages = "Q" if self.state == "q_running" else "J"
        doc_path = self.out_dir / kit_model_check.FILE_NAME
        fresh = doc_path.is_file() and self.job is not None and doc_path.stat().st_mtime >= (self.job.started_at or 0)
        if rc == 0 and stages == "Q" and self.two_roles:
            if needs_swap(self.project):
                self.frozen["Q"] = self._part_done("Q")
                self.state = "needs_swap"
            else:
                self._run("J")
            return
        if rc in (0, 4) and fresh:
            self._record(doc_path)
            return
        self.state = "error"
        self.error = EXIT_CODES.get(rc, "crashed") if isinstance(rc, int) else "crashed"

    def _record(self, doc_path: Path) -> None:
        sha = sha256_file(doc_path)
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        passed = doc.get("passed") is True
        payload = {"passed": passed, "counts": doc.get("counts"), "failures": list(doc.get("failures") or []),
                   "models": doc.get("models"), "digests": doc.get("digests"),
                   "prompt_set_sha256": doc.get("prompt_set_sha256"), "sha256": sha, "pair_sha256": self.pair,
                   "path": self.project.rel(doc_path)}
        self.project.log.append("model_check", payload)
        self.project.data["model_check"] = {k: payload[k] for k in ("pair_sha256", "path", "sha256", "passed",
                                                                    "failures", "models")}
        self.project.save()
        self.result = payload
        self.state = "passed" if passed else "failed"

    # -- progress -----------------------------------------------------------------------------------------------
    def _part_total(self, part: str) -> int:
        p = self.plan[part]
        return max(p["calls"] - self.base.get(part, 0), 0) + p["determinism"] + p["judge_check"]

    def _part_done(self, part: str) -> int:
        if part in self.frozen:
            return self.frozen[part]
        tail = self.tails.get(part)
        if tail is None:
            return 0
        tail.read()
        done = tail.counts[part]
        if tail.marks.get("audit_summary"):
            done += self.plan[part]["determinism"]
        if part == "J" and self.judge_check_tail is not None:
            done += sum(self.judge_check_tail.read().counts.values())
        return done

    def _now(self) -> str | None:
        if self.state == "needs_swap":
            return "switching_models" if self.switching else "waiting_for_judge"
        if self.state not in ("q_running", "j_running"):
            return None
        part = "Q" if self.state == "q_running" else "J"
        tail = self.tails[part]
        if part == "J" and tail.marks.get("run_end"):
            return "judge_check"
        if tail.marks.get("audit_summary"):
            return "finishing"
        if not tail.marks.get("run_start"):
            return "starting"
        from . import timing
        return "determinism" if timing.CHECK_LAST_STAGE[part] in tail.stage_ends else "model_calls"

    def _server(self, now_code: str | None, at: float | None = None) -> dict | None:
        if self.state not in ("q_running", "j_running"):
            return None
        at = time.time() if at is None else at
        tail = self.tails["Q" if self.state == "q_running" else "J"]
        if now_code == "judge_check" and self.judge_check_tail is not None:
            return self.judge_check_tail.server(at)
        sig = tail.server(at)
        if sig is None and now_code == "determinism":
            quiet = at - (tail.last_record if tail.last_record is not None else tail.started)
            if quiet >= 2 * SERVER_QUIET_S:
                sig = {"code": "no_reply", "seconds": tail.since_answer(at), "errors": 0}
        return sig

    def progress(self, at: float | None = None) -> dict | None:
        if self.state not in ("q_running", "j_running", "needs_swap"):
            return None
        parts = []
        for part in self.plan:
            done = self._part_done(part)
            parts.append({"part": part, "role": self.plan[part]["role"], "done": done,
                          "total": max(self._part_total(part), done)})
        now_code = self._now()
        current = "Q" if self.state == "q_running" else "J"
        return {"done": sum(p["done"] for p in parts), "total": sum(p["total"] for p in parts), "part": current,
                "parts": parts, "now": now_code, "determinism_k": self.plan["Q"]["determinism"],
                "server": self._server(now_code, at)}

    def status(self) -> dict:
        return {"state": self.state, "pair_sha256": self.pair, "error": self.error, "progress": self.progress(),
                "failures": list((self.result or {}).get("failures") or []), "two_roles": self.two_roles}

    def stop(self) -> None:
        if self.job is not None:
            self.job.terminate()


def check_status(project, live: ModelCheck | None) -> dict:
    """What ``GET /api/model-check`` shows: the live check of the current pair, or the stored result, or
    ``not_run``. Codes and counts only."""
    pair = models.pair_sha256(project)
    if live is not None and live.pair == pair and live.state not in ("passed", "failed"):
        return live.poll()
    d = stored(project)
    if d is not None:
        state = model_check_state(project)
        return {"state": "passed" if state == "passed" else ("failed" if state == "failed" else "not_run"),
                "pair_sha256": pair, "error": None, "progress": None, "failures": list(d.get("failures") or []),
                "two_roles": models.pipeline_mode(project) == "two_family"}
    return {"state": "not_run", "pair_sha256": pair, "error": None, "progress": None, "failures": [],
            "two_roles": models.pipeline_mode(project) == "two_family"}


# --- the Q/J/M runner (Task 39) ----------------------------------------------------------------------------------
#
# ``RunJob(project, config_path)`` runs one reference group's run (Task 38 writes ``runs/<run_id>/config.json``):
# ``python -m second_scribe.cli run --config <c> --stages <s> --client http`` for each stage in order (Q, J, M; Q and M
# when the pipeline mode is ``ai_maps_only``), each session a child ``Job`` with its own folder ``job/<stage>-<n>``;
# then ``python -m second_scribe.export_view ... --out <project>/views/<run_id>.view.json`` and the profile
# (``results.profile_for``, which writes ``views/<run_id>.profile.json``), so Results never waits for the exporter;
# then ``run_linked{run_id, run_head: [records, head]}`` (the run log's manifest) in the app log.
#
# The stages done so far are kept in ``job/state.json``, so a RunJob made again from the folder (the app restarted)
# reads ``paused`` and resumes the next stage; the kit resumes item by item inside a stage. ``pause()`` stops the
# child (SIGTERM, then wait); ``resume()`` runs the same stage command again. Between Q and J the run waits in
# ``needs_swap`` when the reconstructor and the judge share one local server (``needs_swap``); ``proceed_swap()``
# continues, after the user switched the models or after ``swap_models`` did it on an Ollama server.
#
# ``progress()`` reads the run log from where it stopped (whole lines only) and keeps record types, the stage code of
# each call and sequence numbers: never a payload, a prompt, a reply or a label. ``status()`` is what the browser gets.

RUN_STATES = ("running", "paused", "needs_swap", "swapping", "done", "failed")
RUN_STATE_FILE = "state.json"
RUN_STATE_SCHEMA = "run-job/1"
GENERATE_TIMEOUT_S = 120.0          # a load may read the weights from disk
_CODE_RE = re.compile(r"^[a-z][a-z0-9_:\-]*$")


def _code(v: Any, limit: int = 64) -> str | None:
    """``v`` when it is a short code (lower-case letters, digits, ``_``, ``:``, ``-``), else None."""
    return v if isinstance(v, str) and len(v) <= limit and _CODE_RE.match(v) else None


def stage_group(log_stage: str | None) -> str:
    """"J" for a judge call's stage code (``judge:...``, ``judge2:...``), "Q" for any other call."""
    return "J" if isinstance(log_stage, str) and log_stage.startswith(("judge:", "judge2:")) else "Q"


SERVER_QUIET_S = 180.0     # a model call with no reply for this long reads as a server that is not answering
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


def transport_code(text: Any) -> str:
    """A short code for a logged transport error (``call_error`` with ``transport``): ``http_<status>``, ``timeout``,
    ``no_connection`` or ``transport``. Only the code leaves this function, never the error text."""
    t = text if isinstance(text, str) else ""
    m = _HTTP_STATUS.search(t)
    if m:
        return f"http_{m.group(1)}"
    low = t.lower()
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "refused" in low or "urlerror" in low or "connection" in low or "unreachable" in low:
        return "no_connection"
    return "transport"


def _ts(v: Any) -> float | None:
    """A record's ``ts`` (ISO 8601 with a zone) as epoch seconds, or None."""
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo is not None else None


class LogTail:
    """Counts of a kit run log, read on from where the last read stopped (from ``offset``, default the start; a
    session's own tail starts where the log ended when it began: ``at_end``): ``call_response`` records per stage
    group (Q, J), the last record as ``{record_type, stage, seq}``, how many ``run_start``, ``audit_summary`` and
    ``run_end`` records were read (``marks``), the stages whose ``stage_end`` was read, and what ``server`` needs: the
    time of the last answer and of the last record, the calls still waiting for a reply, and the transport errors
    since the last answer (as a code). Nothing else of a record is kept."""

    MARKS = ("run_start", "audit_summary", "run_end")

    def __init__(self, path: Path, offset: int = 0, started: float | None = None) -> None:
        self.path = Path(path)
        self.offset = max(0, int(offset))
        self.started = time.time() if started is None else float(started)
        self.counts = {"Q": 0, "J": 0}
        self.last: dict | None = None
        self.marks = {k: 0 for k in self.MARKS}
        self.stage_ends: set[str] = set()
        self.last_answer: float | None = None
        self.last_record: float | None = None
        self.session_start: float | None = None
        self.session_answers = 0
        self.errors = 0
        self.error_code: str | None = None
        self._pending: dict[str, str | None] = {}
        self._asked: dict[str, float] = {}

    @classmethod
    def at_end(cls, path: Path, started: float | None = None) -> "LogTail":
        """A tail that reads only what is written from now on."""
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        return cls(path, offset=size, started=started)

    def read(self) -> "LogTail":
        try:
            size = self.path.stat().st_size
            if size < self.offset:                     # a new log in its place: start over
                self.__init__(self.path, 0, self.started)
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return self
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].splitlines():
            self._take(line)
        self.offset += end
        return self

    def _take(self, line: bytes) -> None:
        try:
            rec = json.loads(line)
        except ValueError:
            return
        if not isinstance(rec, dict):
            return
        kind = _code(rec.get("record_type"), 40)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        rid = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
        at = _ts(rec.get("ts"))
        at = time.time() if at is None else at
        self.last_record = at
        stage = None
        if kind == "call_request":
            stage = _code(payload.get("stage"))
            if rid is not None:
                self._pending[rid] = stage
                self._asked[rid] = at
        elif kind in ("call_response", "call_error"):
            stage = self._pending.pop(rid, None) if rid is not None else None
            if rid is not None:
                self._asked.pop(rid, None)
            if kind == "call_response":
                self.counts[stage_group(stage)] += 1
                self.session_answers += 1
                self.last_answer = at
                self.errors, self.error_code = 0, None
            elif payload.get("transport") is True:
                self.errors += 1
                self.error_code = transport_code(payload.get("error"))
        elif kind in ("work_item", "stage_end"):
            stage = _code(payload.get("stage"))
            if kind == "stage_end" and stage:
                self.stage_ends.add(stage)
        if kind in self.MARKS:
            self.marks[kind] += 1
            if kind == "run_start":                    # a new session: an earlier one's open calls are over
                self._pending.clear()
                self._asked.clear()
                self.errors, self.error_code = 0, None
                self.session_start = at
                self.session_answers = 0
        seq = rec.get("seq")
        self.last = {"record_type": kind, "stage": stage,
                     "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None}

    def per_call(self, least: int = 3) -> float | None:
        """Seconds per answered call in this session (from its ``run_start`` to its last answer), once at least
        ``least`` calls have been answered; else None. Call ``read()`` first."""
        if self.session_answers < least or self.session_start is None or self.last_answer is None:
            return None
        return max(0.0, self.last_answer - self.session_start) / self.session_answers

    def since_answer(self, at: float | None = None) -> int:
        """Whole seconds since the last answer of this session (or since the session began, when none yet)."""
        at = time.time() if at is None else at
        ref = max(t for t in (self.last_answer, self.session_start, self.started) if t is not None)
        return max(0, int(at - ref))

    def server(self, at: float | None = None, quiet_s: float = SERVER_QUIET_S) -> dict | None:
        """``{"code", "seconds", "errors"}`` when the model server looks down, else None: the calls since the last
        answer ended in transport errors (``code`` from ``transport_code``, ``errors`` how many), or a call has had no
        reply for ``quiet_s`` seconds (``code`` ``no_reply``). ``seconds`` is ``since_answer``. The kit keeps
        retrying in both cases; call ``read()`` first."""
        at = time.time() if at is None else at
        if self.errors:
            return {"code": self.error_code or "transport", "seconds": self.since_answer(at), "errors": self.errors}
        if self._asked and at - min(self._asked.values()) >= quiet_s:
            return {"code": "no_reply", "seconds": self.since_answer(at), "errors": 0}
        return None


def _audit_names(out_dir: Path, stderr_text: str) -> list[str]:
    """Names of the blocking audits that failed: from the kit's ``blocked by audit: <name> (<n> failures), ...``
    line, else from ``<out_dir>/audits/*.json``. Names only (code identifiers)."""
    names: list[str] = []
    for line in stderr_text.splitlines():
        if "blocked by audit:" in line:
            names += re.findall(r"([a-z][a-z0-9_]*) \(\d+ failures\)", line.split("blocked by audit:", 1)[1])
    if not names:
        adir = Path(out_dir) / "audits"
        for f in sorted(adir.glob("*.json")) if adir.is_dir() else []:
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for r in (doc.get("results") if isinstance(doc, dict) else None) or []:
                if isinstance(r, dict) and r.get("blocking") and r.get("passed") is False:
                    names.append(r.get("name"))
    out = []
    for n in names:
        n = _code(n, 40)
        if n and n not in out:
            out.append(n)
    return out


def exit_info(rc: int | None, out_dir: Path, stderr_path: Path | None = None) -> tuple[str, str]:
    """``(code, message)`` for a kit run session that ended with ``rc``: 1 ``verify_failed``, 2 ``refused``,
    3 ``fixture_missing``, 4 ``thinking`` (the model answered with thinking on) or ``stopped`` (the message names
    the audit), anything else ``crashed``. Messages are ``runs.exit.<code>``; the kit's own output stays on disk."""
    from . import strings
    code = {0: "ok", 1: "verify_failed", 2: "refused", 3: "fixture_missing"}.get(rc) if isinstance(rc, int) else None
    if rc == 4:
        text = ""
        if stderr_path is not None:
            try:
                with open(stderr_path, "rb") as fh:
                    fh.seek(max(0, Path(stderr_path).stat().st_size - 65536))
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                text = ""
        if "carries thinking output" in text:
            return "thinking", strings.t("runs.exit.thinking")
        names = _audit_names(out_dir, text)
        if names:
            return "stopped", strings.t("runs.exit.stopped", audit=", ".join(names))
        return "stopped", strings.t("runs.exit.stopped_unnamed")
    if code is None:
        code = "crashed"
    return code, strings.t(f"runs.exit.{code}")


def _write_json_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_run_state(folder: Path) -> dict:
    """``<run folder>/job/state.json`` (``{"schema", "stages", "done", "finished", "settings"}``), or {}."""
    try:
        doc = json.loads((Path(folder) / "job" / RUN_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("schema") == RUN_STATE_SCHEMA else {}


def swap_allowed(project) -> bool:
    """True when the app may switch the models itself: both roles on one Ollama server (``needs_swap``) that the
    app may contact (loopback, or a LAN / Tailscale address with its logged opt-in)."""
    if not needs_swap(project):
        return False
    for r in ("recon", "judge"):
        e = models.role(project, r) or {}
        if e.get("server") != "ollama" or e.get("address_class") == "remote":
            return False
        problem, info = models.contact_problem(project, r, e.get("url"), e.get("kind") or "local")
        if problem or not info.get("allowed"):
            return False
    return True


def swap_models(project, *, opener=None, timeout: float = GENERATE_TIMEOUT_S) -> None:
    """Unload the reconstructor and load the judge on their Ollama server: ``POST /api/generate {"model": <recon>,
    "keep_alive": 0}`` then ``{"model": <judge>, "prompt": "", "keep_alive": "10m"}``. Call only when
    ``swap_allowed``; any failure raises (HTTP status, transport error, or a reply that is not JSON)."""
    rec, jud = models.role(project, "recon"), models.role(project, "judge")
    root = models.server_root(rec["url"])
    headers = {"Accept": "application/json"}
    for body in ({"model": rec["model"], "keep_alive": 0}, {"model": jud["model"], "prompt": "", "keep_alive": "10m"}):
        models._request(opener, "POST", root + "/api/generate", headers, body, timeout)


class RunJob:
    """One reference group's run (see the comment above). ``env`` is the child environment (Task 38's
    ``orchestrate.run_env``). ``stages`` (default Q, J, M; Q, M for AI maps only), ``ai_maps_only`` (default: the
    config's ``tool.model_families`` has no judge; without that block, ``models.pipeline_mode(project)``) and ``swap``
    (default: ``needs_swap(project)`` when Q ends) can be given. ``settings`` (the start route's ``{rungs, preset}``) are kept in ``job/state.json``."""

    def __init__(self, project, config_path: Path, stages: tuple | None = None, *, env: dict,
                 ai_maps_only: bool | None = None, swap: bool | None = None, settings: dict | None = None) -> None:
        self.project = project
        self.config_path = Path(config_path)
        self.folder = self.config_path.parent
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.run_id = cfg["run_id"]
        self.group = (cfg.get("tool") or {}).get("reference_type") or self.run_id[0]
        out = cfg.get("out_dir")
        self.out_dir = Path(out) if out and Path(out).is_absolute() else self.folder
        self.log_path = self.out_dir / "logs" / "run.jsonl"
        if ai_maps_only is None:                 # Task 38 names the judge's family only for a two-family run
            fams = (cfg.get("tool") or {}).get("model_families")
            ai_maps_only = ("judge" not in fams) if isinstance(fams, dict) and fams else (
                models.pipeline_mode(project) == "ai_maps_only")
        self.ai_maps_only = bool(ai_maps_only)
        self.stages = tuple(stages) if stages is not None else (("Q", "M") if self.ai_maps_only else ("Q", "J", "M"))
        self.env = dict(env)
        self._swap = swap
        self.child: Job | None = None
        self.tail = LogTail(self.log_path)
        self.rc: int | None = None
        self.code: str | None = None
        self.message: str | None = None
        self.profile: str | None = None
        self._export: Any = None
        self._export_result: tuple | None = None
        self._totals: dict | None = None
        self._left_out: int | None = None
        saved = read_run_state(self.folder)
        self.settings = settings if settings is not None else saved.get("settings")
        self.done_stages = [s for s in (saved.get("done") or []) if s in self.stages]
        self.finished = bool(saved.get("finished")) and self.view_path.is_file()
        if self.finished:
            self.state, self.stage = "done", self.stages[-1]
            self.profile = "ready" if self.profile_path.is_file() else None
        else:
            nxt = self._next_stage()
            self.stage = nxt or self.stages[-1]
            self.state = "needs_swap" if nxt == "J" and "Q" in self.done_stages and self._swap_now() else "paused"

    # -- paths and argv -----------------------------------------------------------------------------------------
    @property
    def view_path(self) -> Path:
        return self.project.path("views") / f"{self.run_id}.view.json"

    @property
    def profile_path(self) -> Path:
        return self.project.path("views") / f"{self.run_id}.profile.json"

    def stage_argv(self, stage: str) -> list[str]:
        return ["-m", "second_scribe.cli", "run", "--config", str(self.config_path), "--stages", stage,
                "--client", "http"]

    def export_argv(self, out: Path | None = None) -> list[str]:
        argv = ["-m", "second_scribe.export_view", "--config", str(self.config_path), "--source", "own",
                "--reference-type", self.group, "--out", str(out or self.view_path)]
        return argv + (["--ai-maps-only"] if self.ai_maps_only else [])

    # -- state ----------------------------------------------------------------------------------------------------
    def _next_stage(self) -> str | None:
        return next((s for s in self.stages if s not in self.done_stages), None)

    def _swap_now(self) -> bool:
        return needs_swap(self.project) if self._swap is None else bool(self._swap)

    def _save(self) -> None:
        _write_json_atomic(self.folder / "job" / RUN_STATE_FILE,
                           {"schema": RUN_STATE_SCHEMA, "stages": list(self.stages), "done": list(self.done_stages),
                            "finished": self.finished, "settings": self.settings})

    @property
    def active(self) -> bool:
        """Running, exporting, switching models or waiting for the switch: the machine is busy with this run."""
        return self.state in ("running", "swapping", "needs_swap")

    def _launch(self, stage: str) -> None:
        jdir = self.folder / "job"
        n = sum(1 for d in jdir.glob(f"{stage}-*") if d.is_dir()) + 1 if jdir.is_dir() else 1
        self.stage, self.rc, self.code, self.message = stage, None, None, None
        self.child = Job(self.stage_argv(stage), self.env, KIT_ROOT, self.log_path, job_dir=jdir / f"{stage}-{n}")
        self.child.start()
        self.state = "running"

    def start(self) -> "RunJob":
        """Start (or go on with) the run from its next stage. ``RuntimeError`` once finished or while a child runs."""
        if self.finished:
            raise RuntimeError("this run has finished")
        if self.child is not None and self.child.rc is None:
            raise RuntimeError("this run is already running")
        self._save()
        nxt = self._next_stage()
        if nxt is None:
            self._start_export()
        else:
            self._launch(nxt)
        return self

    def resume(self) -> "RunJob":
        if self.state not in ("paused", "failed"):
            raise RuntimeError("this run is not paused")
        return self.start()

    def pause(self) -> None:
        """Stop the running stage (SIGTERM, then wait; ``Job.terminate``). The same stage runs again on resume."""
        if self.state != "running" or self.child is None:
            raise RuntimeError("this run is not running a stage")
        self.child.terminate()
        self.rc = self.child.rc
        self.child = None
        self.state = "paused"

    def proceed_swap(self, auto: bool = False) -> None:
        """Go on from ``needs_swap`` (or ``swapping``) with the J stage; the models are switched (``auto`` only says
        who did it)."""
        if self.state not in ("needs_swap", "swapping"):
            raise RuntimeError("this run is not waiting for a model switch")
        self._launch("J")

    def stop(self) -> None:
        """Stop the child and wait for an export in progress (app shutdown, tests). The run stays resumable."""
        if self.child is not None and self.child.rc is None:
            self.child.terminate()
            self.child = None
            self.state = "paused"
        if self._export is not None:
            self._export.join(60)

    # -- advancing ------------------------------------------------------------------------------------------------
    def poll(self) -> dict:
        if self.state == "running" and self.child is not None:
            rc = self.child.rc
            if rc is not None:
                self.rc = rc
                stderr = self.child.job_dir / "stderr.txt"
                self.child = None
                if rc == 0:
                    self._stage_done()
                else:
                    self.state = "failed"
                    self.code, self.message = exit_info(rc, self.out_dir, stderr)
        elif self.state == "running" and self._export is not None and not self._export.is_alive():
            self._export_done()
        return self.status()

    def _stage_done(self) -> None:
        if self.stage not in self.done_stages:
            self.done_stages.append(self.stage)
        self._save()
        nxt = self._next_stage()
        if nxt is None:
            self._start_export()
        elif nxt == "J" and self.stage == "Q" and self._swap_now():
            self.stage, self.state = "J", "needs_swap"
        else:
            self._launch(nxt)

    def _start_export(self) -> None:
        self.stage, self.state, self._export_result = self.stages[-1], "running", None
        self._export = threading.Thread(target=self._export_work, name=f"export-{self.run_id}", daemon=True)
        self._export.start()

    def _export_work(self) -> None:
        """The view (kit ``export_view``, written to a part file then moved into place) and the profile
        (``results.profile_for``, which caches it in ``views/``). Runs in its own thread; ``poll`` reads the result."""
        from . import profile_view, results
        try:
            views = self.project.path("views")
            views.mkdir(parents=True, exist_ok=True)
            part = views / f".{self.run_id}.view.part.json"
            jdir = self.folder / "job" / "export"
            jdir.mkdir(parents=True, exist_ok=True)
            with open(jdir / "stdout.txt", "ab") as out, open(jdir / "stderr.txt", "ab") as err:
                rc = subprocess.run([sys.executable, *self.export_argv(part)], cwd=str(KIT_ROOT), env=self.env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True).returncode
            if rc != 0:
                self._export_result = ("failed", rc)
                return
            os.replace(part, self.view_path)
            try:
                results.profile_for(self.project, self.run_id)
                prof = "ready"
            except profile_view.ProfileRefused:
                prof = "refused"
            except Exception:                    # noqa: BLE001 - Results makes it again on demand
                prof = "failed"
            self._export_result = ("ok", prof)
        except Exception as e:                   # noqa: BLE001 - reported as export_failed; details to the terminal
            print(f"export of {self.run_id} failed: {e.__class__.__name__}", flush=True)
            self._export_result = ("failed", None)

    def _export_done(self) -> None:
        from . import strings
        result, self._export = self._export_result or ("failed", None), None
        if result[0] != "ok":
            self.state, self.code = "failed", "export_failed"
            self.message = strings.t("runs.exit.export_failed")
            return
        self.profile = result[1]
        manifest = json.loads((self.log_path.parent / "run.manifest.json").read_text(encoding="utf-8"))
        self.project.log.append("run_linked", {"run_id": self.run_id,
                                               "run_head": [manifest["record_count"], manifest["head_hash"]]})
        self.finished = True
        self._save()
        self.state = "done"

    # -- reading --------------------------------------------------------------------------------------------------
    def totals(self) -> dict:
        """Expected model calls per stage (``timing.count_calls`` on the config, with each session's overhead)."""
        if self._totals is None:
            from . import timing
            from ..pipeline import RunConfig
            try:
                cfg = RunConfig.load(self.config_path)
                self._totals = timing.count_calls(cfg, len((cfg.corpus or {}).get("pairs") or {}),
                                                  judge=not self.ai_maps_only)
            except Exception:                    # noqa: BLE001 - no total is shown rather than a wrong one
                self._totals = {}
        return self._totals

    def counts(self) -> dict:
        return dict(self.tail.read().counts)

    def progress(self) -> dict:
        """``{"stage", "done", "total", "last_record"}``; ``done``/``total`` are model calls of the stage (None for
        M, which calls no model). ``last_record`` is ``{record_type, stage, seq}`` only."""
        self.tail.read()
        stage = self.stage
        done = total = None
        if stage in ("Q", "J"):
            done = self.tail.counts[stage]
            t = self.totals().get(stage)
            total = max(t, done) if isinstance(t, int) else None
        return {"stage": stage, "done": done, "total": total,
                "last_record": dict(self.tail.last) if self.tail.last else None}

    def left_s(self) -> int | None:
        """About how many seconds the model stages still need while a stage runs: the calls left in this stage (and
        the judge's, while the reconstructor's stage runs) times this session's seconds per answered call. None
        before three calls are answered, in M, and when a total is unknown. Call ``progress()`` first."""
        if self.state != "running" or self.child is None or self.stage not in ("Q", "J"):
            return None
        per = self.tail.per_call()
        totals = self.totals()
        t = totals.get(self.stage)
        if per is None or not isinstance(t, int):
            return None
        left = max(0, t - self.tail.counts[self.stage])
        if self.stage == "Q" and "J" in self.stages and "J" not in self.done_stages:
            tj = totals.get("J")
            if not isinstance(tj, int):
                return None
            left += max(0, tj - self.tail.counts["J"])
        return int(round(left * per))

    def left_out(self) -> int | None:
        """Once the run has finished: how many items the kit left ``timeout_excluded`` (the model server did not
        answer), from the run's aggregates (``outage.timeout_excluded_items``); None before the end or when the
        aggregates cannot be read. A count only."""
        if not self.finished:
            return None
        if self._left_out is None:
            from .. import report
            from ..pipeline import RunConfig
            try:
                agg = json.loads(report.aggregates_path(RunConfig.load(self.config_path)).read_text(encoding="utf-8"))
                n = (agg.get("outage") or {}).get("timeout_excluded_items")
                self._left_out = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
            except Exception:                    # noqa: BLE001 - no line rather than a wrong one
                self._left_out = None
        return self._left_out

    def status(self) -> dict:
        """What ``GET /api/runs/status`` sends: codes, counts and the fixed message for an ending; nothing a model or
        a participant wrote. ``server`` is ``LogTail.server`` while a stage runs (the model server is not answering:
        a code and the seconds since the last answer), else None. ``left_s`` is ``left_s()``; ``left_out`` is
        ``left_out()`` (items the server never answered, once the run has finished)."""
        p = self.progress()
        resumable = self.state == "paused" or (self.state == "failed" and self.code not in ("verify_failed",))
        server = self.tail.server() if self.state == "running" and self.child is not None else None
        return {"run_id": self.run_id, "group": self.group, "state": self.state, "stages": list(self.stages),
                "stage": p["stage"], "done": p["done"], "total": p["total"], "last_record": p["last_record"],
                "rc": self.rc, "code": self.code, "message": self.message, "profile": self.profile,
                "resumable": resumable, "server": server, "left_s": self.left_s(), "left_out": self.left_out()}
