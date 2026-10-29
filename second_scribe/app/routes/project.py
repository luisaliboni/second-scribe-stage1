"""Project routes (Task 32): create, open, show, and acknowledge the sync-folder warning.

Later route modules get the open project with ``require_project(state)``, which refuses with 409 ``no_project`` when
none is open and 409 ``sync_folder_unacknowledged`` while the sync-folder warning stands::

    from .project import require_project

    @route("POST", "/api/import")
    def do_import(state, body, query):
        with state.lock:
            project = require_project(state)
            ...
"""
from __future__ import annotations

from .. import strings
from ..api import ApiError, Secrets, route
from ..project import Project, ProjectError, user_folder

STATUS = {"bad_path": 422, "bad_name": 422, "not_a_folder": 422, "parent_missing": 422, "path_outside": 422,
          "folder_not_empty": 409, "project_exists": 409, "no_sync_warning": 409, "not_a_project": 404,
          "bad_project_file": 422}


def api_error(e: ProjectError) -> ApiError:
    """The browser-facing form of a ``ProjectError``: its code, and the message from ``project.error.<code>``."""
    return ApiError(STATUS.get(e.code, 422), e.code, strings.t(f"project.error.{e.code}"))


def require_project(state, *, usable: bool = True) -> Project:
    """The open project. 409 ``no_project`` when none is open; with ``usable`` (the default), 409
    ``sync_folder_unacknowledged`` while it sits in a sync folder the researcher has not acknowledged."""
    project = state.project
    if project is None:
        raise ApiError(409, "no_project", strings.t("project.error.no_project"))
    if usable and project.blocked:
        raise ApiError(409, "sync_folder_unacknowledged", strings.t("project.error.sync_folder_unacknowledged"))
    return project


def _require_idle(state):
    """Do not detach a running job or an in-flight model swap from its project. Each job is polled first (as
    ``runs._busy`` does), so one that ended since the last status poll is not taken for busy."""
    from .. import jobs
    if state.estimate_active:
        raise ApiError(409, "project_busy", strings.t("project.error.project_busy"))
    for job in list(state.jobs.values()):
        if isinstance(job, (jobs.RunJob, jobs.ModelCheck)):
            job.poll()
        if ((isinstance(job, jobs.RunJob) and job.active)
                or (isinstance(job, jobs.ModelCheck)
                    and job.state in ("q_running", "j_running", "needs_swap"))):
            raise ApiError(409, "project_busy", strings.t("project.error.project_busy"))


def _select(state, project):
    # Keys and job handles belong to the previous project, even if the next project
    # happens to select the same model pair or contains a run with the same id.
    state.jobs.clear()
    state.secrets = Secrets()
    state.project = project


def _folder(body: dict):
    try:
        return user_folder(body.get("path"))
    except ProjectError as e:
        raise api_error(e) from None


@route("GET", "/api/project")
def get_project(state, body, query):
    with state.lock:
        project = state.project
        return {"project": project.summary() if project is not None else None}


@route("POST", "/api/project/create")
def create_project(state, body, query):
    root = _folder(body)
    with state.lock:
        _require_idle(state)
        try:
            project = Project.create(root, body.get("name"))
        except ProjectError as e:
            raise api_error(e) from None
        _select(state, project)
        return {"project": project.summary()}


@route("POST", "/api/project/open")
def open_project(state, body, query):
    root = _folder(body)
    with state.lock:
        _require_idle(state)
        try:
            project = Project.open(root)
        except ProjectError as e:
            raise api_error(e) from None
        _select(state, project)
        return {"project": project.summary()}


@route("POST", "/api/project/ack-sync")
def ack_sync(state, body, query):
    with state.lock:
        project = require_project(state, usable=False)
        if body.get("understood") is not True:
            raise ApiError(422, "not_understood", strings.t("project.error.not_understood"))
        try:
            project.ack_sync()
        except ProjectError as e:
            raise api_error(e) from None
        return {"project": project.summary()}
