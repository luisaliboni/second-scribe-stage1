"""Export routes (Task 42; spec 6.9, 9; plan V6, V13, V14). The logic lives in ``app/export.py``.

Every route that reads or writes a run's results registers with ``coding.results_route``: refused with 409
``coding_unlocked`` while any mode-B coding is open, and the reveal is logged once one answers. ``GET
/api/export/runs`` lists run ids and groups only (config files, no result) and says whether the gate is open, so the
screen can explain why it is closed without revealing anything.
"""
from __future__ import annotations

from .. import coding, export, opener, strings
from ..api import ApiError, route
from ..coding import results_route
from .project import require_project


@route("GET", "/api/export/runs")
def export_runs(state, body, query):
    with state.lock:
        project = require_project(state)
        gate = coding.gate_summary(project)
        return {"runs": export.list_runs(project), "k_min": export.K_MIN, "can_open": opener.available(),
                "gate": {"open": coding.results_gate(project), "waiting": len(coding.waiting(project)),
                         "summary": gate}}


@results_route("GET", "/api/export/list")
def export_list(state, body, query):
    """``?run_id=``: the four outputs of the run, in screen order, with the latest logged save of each."""
    project = state.project
    run = export.find_run(project, query.get("run_id"))
    return {"run_id": run.run_id, "outputs": export.outputs(project, run.run_id)}


@results_route("POST", "/api/export/report")
def make_report(state, body, query):
    """``{run_id}``: write the report folder; the path is relative to the project, ``folder`` is absolute (shown
    so the researcher can open the file in the browser)."""
    project = state.project
    index = export.export_report(project, body.get("run_id"), state)
    return {"path": project.rel(index), "folder": str(index.parent), "file": str(index),
            "sha256": export.export_records(project)[-1]["sha256"]}


@results_route("POST", "/api/export/profile/preview")
def profile_preview(state, body, query):
    """``{run_id, k}``: every line that would be saved, the refusals and the warnings. Nothing is saved."""
    out = export.profile_preview(state.project, body.get("run_id"), body.get("k"))
    out.pop("doc", None)
    return out


@results_route("POST", "/api/export/profile/save")
def profile_save(state, body, query):
    """``{run_id, k, sha256}``: save the previewed file (the sha256 the preview showed)."""
    return export.save_profile(state.project, body.get("run_id"), body.get("k"), body.get("sha256"))


@results_route("POST", "/api/export/methods")
def methods(state, body, query):
    """``{run_id}``: the paragraph's text (nothing saved)."""
    return {"text": export.export_methods(state.project, body.get("run_id"))}


@results_route("POST", "/api/export/methods/save")
def methods_save(state, body, query):
    return export.save_methods(state.project, body.get("run_id"))


@results_route("POST", "/api/export/certificate/save")
def certificate_save(state, body, query):
    return export.save_certificate(state.project, body.get("run_id"))


# Tests replace this with a fake ``subprocess.Popen``; the real one opens the file on screen.
POPEN = None


@results_route("POST", "/api/export/open")
def open_export(state, body, query):
    """``{path, reveal}`` (scout F11): hand a saved export to the system's opener ("Open"), or show it in its folder
    (``reveal: true``). ``path`` is project-relative and must be inside ``exports/`` (``export.export_target``); the
    command is fixed (``opener.command``) and no shell runs. 501 ``opener_unavailable``, 502 ``open_failed``."""
    reveal = body.get("reveal") is True
    target = export.export_target(state.project, body.get("path"), reveal)
    kw = {"popen": POPEN} if POPEN is not None else {}
    try:
        opener.open_path(str(target), reveal, **kw)
    except opener.OpenError as e:
        if e.code == "unavailable":
            raise ApiError(501, "opener_unavailable", strings.t("export.error.opener_unavailable")) from None
        raise ApiError(502, "open_failed", strings.t("export.error.open_failed")) from None
    return {"opened": True, "reveal": reveal}
