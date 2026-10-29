"""Import routes (Task 33): study context, import, pairing, speaker labels, missing rule, parsed-map confirmation;
item 26 adds ``POST /api/import/remove``, item 27 ``GET`` and ``POST /api/labels/standard``, item 28
``GET /api/missing-rule/colors`` (read only).

Each handler takes ``state.lock``, gets the usable project (``require_project``) and calls ``app.importer``, which
refuses with ``ApiError``. Request and reply shapes are in ``importer``'s docstrings. Removing a file is refused with
409 ``run_in_progress`` while a run, a model check or a time estimate is going (``_require_idle``, as for opening
another project), since a run reads the inputs by path.
"""
from __future__ import annotations

from .. import coding, importer
from ..api import ApiError, route
from .project import _require_idle, require_project


@route("GET", "/api/context")
def get_context(state, body, query):
    with state.lock:
        project = require_project(state)
        return {"context": project.data.get("context") or {}}


@route("POST", "/api/context")
def post_context(state, body, query):
    with state.lock:
        return {"context": importer.set_context(require_project(state), body)}


@route("POST", "/api/import")
def post_import(state, body, query):
    with state.lock:
        return importer.import_file(require_project(state), body.get("kind"), body.get("name"), body.get("data_b64"))


@route("POST", "/api/import/remove")
def post_import_remove(state, body, query):
    with state.lock:
        project = require_project(state)
        try:
            _require_idle(state)
        except ApiError:
            raise importer.refuse(409, "run_in_progress") from None
        return importer.remove_file(project, body.get("kind"), body.get("pid"))


@route("GET", "/api/pairing")
def get_pairing(state, body, query):
    with state.lock:
        return importer.pairing(require_project(state))


@route("POST", "/api/pairing/confirm")
def post_pairing_confirm(state, body, query):
    with state.lock:
        return importer.confirm_pairing(require_project(state), body.get("table_sha256"))


@route("GET", "/api/labels")
def get_labels(state, body, query):
    with state.lock:
        return importer.label_preview(require_project(state), query.get("pid"))


@route("GET", "/api/labels/standard")
def get_labels_standard(state, body, query):
    with state.lock:
        return importer.standard_view(require_project(state))


@route("POST", "/api/labels/standard")
def post_labels_standard(state, body, query):
    with state.lock:
        return importer.set_standard(require_project(state), body)


@route("POST", "/api/labels/confirm")
def post_labels_confirm(state, body, query):
    with state.lock:
        return importer.confirm_labels(require_project(state), body)


@route("GET", "/api/missing-rule")
def get_missing_rule(state, body, query):
    with state.lock:
        return importer.rules_view(require_project(state))


@route("GET", "/api/missing-rule/colors")
def get_missing_rule_colors(state, body, query):
    with state.lock:
        return importer.colors_found(require_project(state))


@route("POST", "/api/missing-rule")
def post_missing_rule(state, body, query):
    with state.lock:
        return importer.set_missing_rule(require_project(state), body)


@route("GET", "/api/maps/preview")
def get_map_preview(state, body, query):
    with state.lock:
        return importer.preview_map(require_project(state), query.get("pid"))


@route("POST", "/api/maps/phases")
def post_map_phases(state, body, query):
    with state.lock:
        return importer.set_phases(require_project(state), body)


@route("POST", "/api/maps/confirm")
def post_map_confirm(state, body, query):
    with state.lock:
        project = require_project(state)
        out = importer.confirm_map(project, body)
        # confirm_map writes the reference as type A; put it back as the pid's chosen type (Task 36)
        try:
            out["reference_type"] = coding.apply_reference_choice(project, out["pid"])
        except ApiError as e:                       # the confirmation stands; say why the reference was not re-placed
            out["reference_type"], out["reference_error"] = coding.reference_type_of(project, out["pid"]), e.code
        return out
