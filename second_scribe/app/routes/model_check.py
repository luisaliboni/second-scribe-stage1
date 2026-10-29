"""Setup steps 7-8 (Task 35): the model check of the pair and the time estimate.

``GET  /api/model-check``            the current pair's check: ``{"check": {state, progress, failures, error, ...},
                                     "ready", "problem", "judge_model", "plan", "swap_allowed"}``
``POST /api/model-check/start``      start it (Q); a passed pair answers its result, a failed pair is refused
``POST /api/model-check/continue {auto}``  run J (from ``needs_swap``): ``auto`` true first switches the models on
                                     an Ollama server the app may contact (``jobs.swap_allowed``), as a run's swap
                                     does; false means the user loaded the judge
``POST /api/estimate {rungs}``       the time estimate (one timed call on a shipped synthetic transcript)

Codes and counts only reach the browser; the kit's own output stays in the job folder.
"""
from __future__ import annotations

from .. import jobs, models, strings, timing
from ..api import ApiError, route
from .project import require_project

LIVE = "model_check"


def _live(state):
    live = state.jobs.get(LIVE)
    return live if isinstance(live, jobs.ModelCheck) else None


def _require_run_idle(state):
    if state.estimate_active:
        raise ApiError(409, "run_busy", strings.t("runs.error.run_busy"))
    live = state.jobs.get("run")
    if isinstance(live, jobs.RunJob):
        live.poll()
        if live.active:
            raise ApiError(409, "run_busy", strings.t("runs.error.run_busy"))


def plan(project) -> dict:
    """What the check will send, for the page before it starts: ``{"parts": [{"part", "role", "calls",
    "determinism", "judge_check", "total"}], "total", "time": timing.model_check_time or None, "cases",
    "one_server"}`` (``one_server``: both models on one local server, so the judge is loaded between the parts)."""
    judge = models.pipeline_mode(project) == "two_family"
    parts = timing.model_check_parts(judge=judge)
    return {"parts": [{"part": g, **p} for g, p in parts.items()], "total": sum(p["total"] for p in parts.values()),
            "time": timing.model_check_time(judge=judge),
            "cases": len((timing.check_config().corpus or {}).get("roles") or {}),
            "one_server": jobs.needs_swap(project)}


def _reply(state, project) -> dict:
    """The check, plus what the step needs around it: whether the models step is done (its first problem's message
    when not), the judge's model name (for the "load the judge" notice), the plan (calls and time before it starts)
    and, while the check waits for the judge, whether the app may switch the models itself."""
    probs = models.problems(project, state.secrets)
    judge = models.role(project, "judge") or {}
    check = jobs.check_status(project, _live(state))
    allowed = False
    if check["state"] == "needs_swap":                 # read only while the check waits for the switch
        try:
            allowed = jobs.swap_allowed(project)
        except Exception:                              # noqa: BLE001 - an incomplete pair cannot be switched
            allowed = False
    return {"check": check, "ready": not probs,
            "problem": strings.t(f"models.error.{probs[0]['code']}") if probs else None,
            "judge_model": judge.get("model") if models.pipeline_mode(project) == "two_family" else None,
            "plan": plan(project), "swap_allowed": allowed}


@route("GET", "/api/model-check")
def get_check(state, body, query):
    with state.lock:
        project = require_project(state)
        return _reply(state, project)


@route("POST", "/api/model-check/start")
def start_check(state, body, query):
    with state.lock:
        project = require_project(state)
        _require_run_idle(state)
        models.require_ready(project, state.secrets)
        live = _live(state)
        if live is not None and live.pair == models.pair_sha256(project) and live.state in (
                "q_running", "j_running", "needs_swap"):
            return _reply(state, project)
        st = jobs.model_check_state(project)
        if st == "passed":
            return _reply(state, project)
        if st == "failed":
            raise ApiError(409, "model_check_failed", strings.t("model_check.error.model_check_failed"))
        if live is not None:
            live.stop()
        state.jobs[LIVE] = jobs.ModelCheck(project, state.secrets).start()
        return _reply(state, project)


@route("POST", "/api/model-check/continue")
def continue_check(state, body, query):
    auto = body.get("auto") is True
    with state.lock:
        project = require_project(state)
        _require_run_idle(state)
        live = _live(state)
        if (live is None or live.pair != models.pair_sha256(project) or live.state != "needs_swap"
                or live.switching):
            raise ApiError(409, "not_waiting", strings.t("model_check.error.not_waiting"))
        if not auto:
            live.proceed()
            return _reply(state, project)
        if not jobs.swap_allowed(project):
            raise ApiError(409, "swap_not_allowed", strings.t("model_check.error.swap_not_allowed"))
        live.switching = True
    try:                                       # the model server may take a while to load: outside the lock
        jobs.swap_models(project)
    except Exception:                          # noqa: BLE001 - any failure: the user loads the judge by hand
        with state.lock:
            live.switching = False
        raise ApiError(502, "swap_failed", strings.t("model_check.error.swap_failed")) from None
    with state.lock:
        if live.switching and live.state == "needs_swap" and _live(state) is live:
            live.proceed()
        else:
            live.switching = False
        return _reply(state, project)


ESTIMATE_ERRORS = ("timeout", "refused", "crashed", "probe_failed", "stopped", "verify_failed", "fixture_missing")


@route("POST", "/api/estimate")
def post_estimate(state, body, query):
    try:
        rungs = timing.check_rungs(body.get("rungs", []))
    except ValueError:
        raise ApiError(422, "bad_rungs", strings.t("estimate.error.bad_rungs")) from None
    with state.lock:
        project = require_project(state)
        _require_run_idle(state)
        live = _live(state)
        if live is not None and live.state in ("q_running", "j_running", "needs_swap"):
            raise ApiError(409, "run_busy", strings.t("runs.error.run_busy"))
        models.require_ready(project, state.secrets)
        secrets = state.secrets
        pair = models.pair_sha256(project)
        state.estimate_active = True
    try:                                     # the timed call runs outside the lock: the app stays usable
        try:
            doc = timing.estimate(project, rungs, secrets=secrets)
        except ValueError as e:
            if str(e) == "no_transcripts":
                raise ApiError(409, "no_transcripts", strings.t("estimate.error.no_transcripts")) from None
            raise
        except RuntimeError as e:
            code = str(e) if str(e) in ESTIMATE_ERRORS else "crashed"
            raise ApiError(502, "estimate_failed", strings.t(f"estimate.error.{code}")) from None
        with state.lock:
            if state.project is not project:
                raise ApiError(409, "no_project", strings.t("project.error.no_project"))
            if models.pair_sha256(project) != pair:
                raise ApiError(409, "families_changed", strings.t("models.error.families_changed"))
            return {"estimate": doc}
    finally:
        with state.lock:
            state.estimate_active = False
