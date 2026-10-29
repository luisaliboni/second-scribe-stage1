"""The run routes (Task 39; spec 3, 6.8). The runner is ``jobs.RunJob``; the config and env come from Task 38's
``orchestrate`` (``build_run_config``, ``run_env``).

``POST /api/runs/start  {group, rungs, preset}``   start the group's run, or go on with its unfinished one
``GET  /api/runs/status [?run=<run_id>]``          the run's progress (no ``run``: the run in progress, or null)
``POST /api/runs/pause  {run}``                    stop the running stage; resume picks up item by item
``POST /api/runs/resume {run}``                    run the stopped stage again on the same config
``POST /api/runs/swap   {run, auto}``              from ``needs_swap``: ``auto`` true switches the models on an
                                                   Ollama server the app may contact; false means the user did it
``GET  /api/runs/ready``                           what a run still needs, as counts with the Setup step to fix it,
                                                   and per reference group whether Start would go ahead (``ready``)

The route table matches paths exactly, so the run id travels as ``?run=`` or in the body, never in the path. A run id
is ``<group>-<8 hex>`` (``results.RUN_ID``). The answers carry codes and counts only (``RunJob.status``): progress is
not a result, so these routes are not behind the B coding gate, and nothing in them comes from a model or a
transcript. One run uses the models at a time: ``state.jobs["run"]`` is the run in progress.
"""
from __future__ import annotations

from .. import jobs, results, strings
from ..api import ApiError, route
from .project import require_project

LIVE = "run"


def _err(status: int, code: str) -> ApiError:
    return ApiError(status, code, strings.t(f"runs.error.{code}"))


def _orchestrate():
    from .. import orchestrate                 # Task 38; imported when a run starts
    return orchestrate


def _live(state) -> jobs.RunJob | None:
    live = state.jobs.get(LIVE)
    return live if isinstance(live, jobs.RunJob) else None


def _busy(state, but: str | None = None) -> bool:
    if state.estimate_active:
        return True
    live = _live(state)
    if live is not None and live.run_id != but:
        live.poll()
        if live.active:
            return True
    check = state.jobs.get("model_check")
    return isinstance(check, jobs.ModelCheck) and check.state in ("q_running", "j_running", "needs_swap")


def _env(state, project) -> dict:
    """The child env of a run (Task 38: ``jobs.child_env`` over the roles the run uses; 409 ``key_missing``)."""
    return _orchestrate().run_env(project, state.secrets)


def _settings(body: dict) -> tuple[str, list, str, dict]:
    group = body.get("group")
    if group not in results.GROUPS:
        raise _err(422, "bad_group")
    rungs = body.get("rungs", [])
    if not isinstance(rungs, list) or not all(isinstance(r, str) and r in results.PICKABLE_RUNGS for r in rungs):
        raise _err(422, "bad_rungs")
    preset = body.get("preset", "standard")
    if preset not in results.PRESETS:
        raise _err(422, "bad_preset")
    picked = [r for r in results.PICKABLE_RUNGS if r in set(rungs) | {"rung2"}]
    return group, picked, preset, {"rungs": picked, "preset": preset}


def _job(state, project, run_id) -> jobs.RunJob:
    """The live job of ``run_id``, else the run read back from its folder (422 ``bad_run``, 404 ``unknown_run``)."""
    ref = results.find_run(project, run_id)
    live = _live(state)
    if live is not None and live.run_id == ref.run_id:
        return live
    return jobs.RunJob(project, ref.config_path, env={})


def _recheck(project, job: jobs.RunJob) -> None:
    """Task 38's refusals again before a run goes on (a changed file, a failed model check...): its
    ``build_run_config`` returns the existing config unchanged when nothing refuses."""
    s = job.settings
    if isinstance(s, dict) and isinstance(s.get("rungs"), list) and s.get("preset") in results.PRESETS:
        _orchestrate().build_run_config(project, job.group, list(s["rungs"]), s["preset"], job.run_id)


def _take(state, job: jobs.RunJob, env: dict) -> jobs.RunJob:
    """Make ``job`` the run in progress, with a fresh child env (keys may have been set since)."""
    live = _live(state)
    if live is not None and live is not job:
        live.stop()
    job.env = env
    state.jobs[LIVE] = job
    return job


def _reply(job: jobs.RunJob) -> dict:
    return {"job": job.poll()}


_READY_CODES = {          # the refusal code of a group -> the readiness item that says what to do
    "no_context": "context", "pairing_unconfirmed": "pairing", "id_map_missing": "pairing", "file_changed": "pairing",
    "labels_unconfirmed": "labels", "map_unconfirmed": "maps", "reference_changed": "maps",
    "group_empty": "references", "reference_missing": "references",
    "coding_unlocked": "coding", "coding_changed": "coding",
    "model_check_failed": "model_check", "model_check_missing": "model_check",
    **{c: "models" for c in ("recon_missing", "optin_missing", "remote_needs_https", "declaration_missing",
                             "cloud_model", "key_missing", "connection_untested", "connection_failed", "thinking_on",
                             "families_unconfirmed")},
}


def readiness(state, project) -> dict:
    """``{"items": [{key, ok, done?, total?}], "groups": {A|B|C: {ready, code?, item?, message?}}}``. Items in Setup
    order: context, pairing, labels, maps (only when a map is paired), references, coding (only with a B reference),
    models, model_check (``state`` passed / failed / missing / running). A group is listed when a transcript has its
    reference; ``ready`` is whether ``orchestrate.check_ready`` lets it start, else its first refusal (``code``, the
    ``item`` that fixes it, the fixed ``message``). Counts and codes only."""
    from .. import coding, importer, models
    orch = _orchestrate()
    items: list[dict] = []
    try:
        orch._context(project)
        items.append({"key": "context", "ok": True})
    except ApiError:
        items.append({"key": "context", "ok": False})
    todo = importer.unconfirmed(project)
    pairs = (project.data.get("pairing") or {}).get("pids") or {}
    items.append({"key": "pairing", "ok": not todo["pairing"] and bool(pairs)})
    n = len(pairs)
    items.append({"key": "labels", "ok": n > 0 and not todo["labels"], "done": n - len(todo["labels"]), "total": n})
    with_map = sum(1 for r in pairs.values() if isinstance(r, dict) and r.get("map"))
    if with_map:
        items.append({"key": "maps", "ok": not todo["maps"], "done": with_map - len(todo["maps"]), "total": with_map})
    pids = importer.confirmed_pids(project) or sorted(pairs)
    typed = [p for p in pids if coding.reference_type_of(project, p)]
    items.append({"key": "references", "ok": bool(pids) and len(typed) == len(pids), "done": len(typed),
                  "total": len(pids)})
    bs = [p for p in pids if coding.reference_type_of(project, p) == "B"]
    if bs:
        locked = sum(1 for p in bs if coding.is_locked(project, p))
        items.append({"key": "coding", "ok": locked == len(bs), "done": locked, "total": len(bs)})
    items.append({"key": "models", "ok": not models.problems(project)})
    check = state.jobs.get("model_check")
    mc = "running" if isinstance(check, jobs.ModelCheck) and check.state in ("q_running", "j_running", "needs_swap") \
        else jobs.model_check_state(project)
    items.append({"key": "model_check", "ok": mc == "passed", "state": mc})
    groups: dict = {}
    for g in results.GROUPS:
        if not any(coding.reference_type_of(project, p) == g for p in pids):
            continue
        try:
            orch.check_ready(project, g, ["rung2"], "standard", orch.new_run_id(g))
            groups[g] = {"ready": True}
        except ApiError as e:
            code = e.code if isinstance(e.code, str) else "refused"
            groups[g] = {"ready": False, "code": code, "item": _READY_CODES.get(code), "message": e.message}
    return {"items": items, "groups": groups}


@route("GET", "/api/runs/ready")
def run_ready(state, body, query):
    with state.lock:
        return readiness(state, require_project(state))


@route("POST", "/api/runs/start")
def start_run(state, body, query):
    with state.lock:
        project = require_project(state)
        group, rungs, preset, settings = _settings(body)
        live = _live(state)
        if live is not None and live.group == group:
            live.poll()
            if live.active:
                return _reply(live)
        if _busy(state):
            raise _err(409, "run_busy")
        env = _env(state, project)                       # 409 key_missing before anything is written
        for ref in results.list_runs(project):          # an unfinished run of this group and settings goes on
            if ref.group != group:
                continue
            saved = jobs.read_run_state(ref.folder)
            if saved.get("settings") != settings:
                continue
            job = jobs.RunJob(project, ref.config_path, env={})
            if job.finished:
                continue
            _recheck(project, job)
            _take(state, job, env)
            if job.state in ("paused", "failed"):
                job.resume()
            return _reply(job)
        orch = _orchestrate()
        runs = project.path("runs")
        run_id = orch.new_run_id(group)
        while (runs / run_id).exists():
            run_id = orch.new_run_id(group)
        config = orch.build_run_config(project, group, rungs, preset, run_id)    # refusals: ApiError 409
        job = jobs.RunJob(project, config, env={}, settings=settings)
        _take(state, job, env)
        job.start()
        return _reply(job)


@route("GET", "/api/runs/status")
def run_status(state, body, query):
    with state.lock:
        project = require_project(state)
        run_id = query.get("run")
        if run_id is None:
            live = _live(state)
            return {"job": live.poll() if live is not None else None}
        return _reply(_job(state, project, run_id))


@route("POST", "/api/runs/pause")
def pause_run(state, body, query):
    with state.lock:
        project = require_project(state)
        job = _job(state, project, body.get("run"))
        job.poll()
        if job is not _live(state) or job.state != "running" or job.child is None:
            raise _err(409, "not_running")
        job.pause()
        return _reply(job)


@route("POST", "/api/runs/resume")
def resume_run(state, body, query):
    with state.lock:
        project = require_project(state)
        job = _job(state, project, body.get("run"))
        job.poll()
        if job.state not in ("paused", "failed") or job.finished:
            raise _err(409, "not_paused")
        if _busy(state, but=job.run_id):
            raise _err(409, "run_busy")
        env = _env(state, project)
        _recheck(project, job)
        _take(state, job, env)
        job.resume()
        return _reply(job)


@route("POST", "/api/runs/swap")
def swap_run(state, body, query):
    auto = body.get("auto") is True
    with state.lock:
        project = require_project(state)
        job = _job(state, project, body.get("run"))
        job.poll()
        if job.state != "needs_swap":
            raise _err(409, "not_waiting")
        if _busy(state, but=job.run_id):
            raise _err(409, "run_busy")
        if auto and not jobs.swap_allowed(project):
            raise _err(409, "swap_not_allowed")
        env = _env(state, project)
        _recheck(project, job)
        _take(state, job, env)
        if not auto:
            job.proceed_swap(auto=False)
            return _reply(job)
        job.state = "swapping"
    try:                                       # the model server may take a while to load: outside the lock
        jobs.swap_models(project)
    except Exception:                          # noqa: BLE001 - any failure: the user switches by hand
        with state.lock:
            if job.state == "swapping":
                job.state = "needs_swap"
        raise _err(502, "swap_failed") from None
    with state.lock:
        if job.state == "swapping":
            job.proceed_swap(auto=True)
        return _reply(job)
