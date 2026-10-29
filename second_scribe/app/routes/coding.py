"""Reference and mode-B coding routes (Task 36; spec section 6.6, 6.7). The logic lives in ``app/coding.py``.

None of these routes returns anything AI-derived: they read the transcript, the coding store and the app log only.
A route that serves results, the no-interview map or an AI map registers with ``coding.results_route`` instead of
``route``, so the blindness gate runs first (Task 40's ``GET /api/view``)::

    from ..coding import results_route

    @results_route("GET", "/api/view")
    def view(state, body, query):
        ...
"""
from __future__ import annotations

from .. import coding, strings
from ..api import ApiError, route
from .project import require_project


@route("GET", "/api/references")
def list_references(state, body, query):
    with state.lock:
        return coding.references_summary(require_project(state))


@route("POST", "/api/references")
def set_reference(state, body, query):
    """``{"pid": <pid>, "type": "A"|"B"|"C"}``."""
    with state.lock:
        project = require_project(state)
        return {**coding.set_reference(project, body.get("pid"), body.get("type")),
                "gate": coding.gate_summary(project)}


@route("POST", "/api/references/bulk")
def bulk_references(state, body, query):
    """``{"confirmed_maps": "A"}``: every pid with a confirmed map becomes A."""
    if body.get("confirmed_maps") != "A":
        raise ApiError(422, "bad_bulk", strings.t("coding.error.bad_bulk"))
    with state.lock:
        project = require_project(state)
        return {"set": coding.set_confirmed_maps_to_a(project), "gate": coding.gate_summary(project)}


@route("GET", "/api/results/gate")
def results_gate(state, body, query):
    """Whether results may open, and which B codings are still open. Reading it never logs a reveal."""
    with state.lock:
        return coding.gate_summary(require_project(state))


@route("GET", "/api/coding")
def get_coding(state, body, query):
    with state.lock:
        return coding.coding_view(require_project(state), query.get("pid"))


@route("POST", "/api/coding/mark")
def mark(state, body, query):
    """``{pid, turn, start, end, text, status: "happened"|"missing", missing_kind?, step, phase}``."""
    with state.lock:
        project = require_project(state)
        m = coding.add_mark(project, body.get("pid"), body)
        return {"mark": m, "n_marks": len(coding.load_coding(project, body.get("pid"))["marks"])}


@route("POST", "/api/coding/unmark")
def unmark(state, body, query):
    """``{pid, mark_id}``."""
    with state.lock:
        return {"n_marks": coding.remove_mark(require_project(state), body.get("pid"), body.get("mark_id"))}


@route("GET", "/api/coding/suggest")
def suggest(state, body, query):
    """``?prefix=``: step names from this project's own marks only."""
    with state.lock:
        return {"names": coding.suggest(require_project(state), query.get("prefix", ""))}


@route("POST", "/api/coding/tick")
def tick(state, body, query):
    """``{pid, seconds}``: visible coding time."""
    with state.lock:
        return {"seconds": coding.tick(require_project(state), body.get("pid"), body.get("seconds"))}


@route("POST", "/api/coding/lock")
def lock(state, body, query):
    """``{pid}``: Finish and lock."""
    with state.lock:
        project = require_project(state)
        return {**coding.lock(project, coding.check_pid(body.get("pid")))}
