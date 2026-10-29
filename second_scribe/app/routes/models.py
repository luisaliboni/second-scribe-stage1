"""Model routes (Task 34; spec 6.5 step 6, 8, 8.1). The logic lives in ``second_scribe.app.models``.

    GET  /api/models                      the step's state: roles, keys (set or not), opt-ins, declarations, problems
    POST /api/models/classify             {role, kind, url} -> address class, host, endpoint, what it needs
    POST /api/models/discover             {role, kind, url} -> {server, endpoint, models: [...]}
    POST /api/models/set                  {role, kind, url, model, family[, provider, digest]} -> stores the role
    POST /api/models/clear                {role}
    POST /api/models/test                 {role} -> {result: {ok, ms, thinking_seen, reply_ok, error}, message}
    POST /api/models/optin                {role, host, understood: true} -> logs ``optin``
    POST /api/models/declaration          {role, provider, host, basis, ticked: true} -> logs ``declaration``
    POST /api/models/key                  {role, key} | {role, env_name} | {role, clear: true} -> Secrets only
    POST /api/models/confirm-families     {recon, judge?} -> pipeline mode

Network calls (discovery, the connection test) run outside ``state.lock``; what they found is stored only if the
same project is still open and the role has not changed meanwhile. A key is never echoed, logged or stored.
"""
from __future__ import annotations

import os

from .. import models, strings
from ..api import ApiError, route
from .project import require_project

STATUS_409 = frozenset({"optin_missing", "declaration_missing", "role_not_set", "families_changed",
                        "recon_missing", "families_unconfirmed"})


def api_error(e: models.ModelsError) -> ApiError:
    return ApiError(409 if e.code in STATUS_409 else 422, e.code, strings.t(f"models.error.{e.code}"))


def _refuse(code: str) -> ApiError:
    return api_error(models.ModelsError(code))


def _role(body: dict) -> str:
    r = body.get("role")
    if r not in models.ROLES:
        raise _refuse("bad_role")
    return r


def _kind(body: dict) -> str:
    k = body.get("kind")
    if k not in models.KINDS:
        raise _refuse("bad_kind")
    return k


def _key(secrets, r: str) -> str | None:
    """The role's key from memory (pasted) or from the named variable of the app's own environment."""
    env = secrets.env_for(r)
    var = env.get(f"SS_{r.upper()}_KEY_ENV")
    if not var:
        return None
    return env.get(var) or os.environ.get(var) or None


def summary(project, secrets) -> dict:
    """What the browser sees. Keys appear only as set / not set."""
    m = models.data(project)
    roles = {}
    for r, e in m["roles"].items():
        if r in models.ROLES:
            roles[r] = {**{k: e.get(k) for k in models.ROLE_FIELDS}, "key_set": secrets.has(r)}
    mode = models.pipeline_mode(project)
    same = models.same_family(project)
    notice = None
    if mode == "ai_maps_only":
        notice = strings.t("setup.models.same_family" if same else "setup.models.no_judge")
    probs = [{**p, "message": strings.t(f"models.error.{p['code']}")} for p in models.problems(project, secrets)]
    decls = [{k: d.get(k) for k in ("role", "provider", "host", "basis", "sha256")} for d in models.declarations(project)]
    return {"roles": roles, "keys": {r: secrets.has(r) for r in models.ROLES},
            "confirmed": m.get("confirmed"), "families_confirmed": models.families_confirmed(project),
            "pipeline_mode": mode, "same_family": same, "notice": notice,
            "optins": [{k: o.get(k) for k in ("role", "host", "address_class")} for o in models.optins(project)],
            "declarations": decls, "problems": probs, "pair_sha256": models.pair_sha256(project),
            "defaults": dict(models.DEFAULT_URLS)}


def _contact(project, r: str, url, kind: str) -> dict:
    """The address info for contacting ``url``; refuses (422 ``bad_url`` / ``name_not_found`` / ``remote_needs_https``, 409
    ``optin_missing`` / ``declaration_missing``) while the app may not contact it."""
    problem, info = models.contact_problem(project, r, url, kind)
    if problem:
        raise _refuse(problem)
    return info


@route("GET", "/api/models")
def get_models(state, body, query):
    with state.lock:
        project = require_project(state)
        return {"models": summary(project, state.secrets)}


@route("POST", "/api/models/classify")
def classify(state, body, query):
    r, kind = _role(body), _kind(body)
    with state.lock:
        project = require_project(state)
        problem, info = models.contact_problem(project, r, body.get("url"), kind)
    if problem in ("bad_url", "name_not_found"):
        raise _refuse(problem)
    return {**info, "problem": problem or None,
            "message": strings.t(f"models.error.{problem}") if problem else None}


@route("POST", "/api/models/discover")
def discover(state, body, query):
    r, kind = _role(body), _kind(body)
    with state.lock:
        project = require_project(state)
        info = _contact(project, r, body.get("url"), kind)
        key = _key(state.secrets, r)
    try:
        found = models.discover(info["endpoint"], kind=kind, key=key)
    except models.ModelsError as e:
        raise api_error(e) from None
    return {"server": found[0]["server"], "endpoint": info["endpoint"], "models": found}


@route("POST", "/api/models/set")
def set_model(state, body, query):
    r, kind = _role(body), _kind(body)
    model = body.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > models.MAX_MODEL \
            or models._CONTROL.search(model):
        raise _refuse("bad_model")
    model = model.strip()
    family = models.normalize_family(body.get("family"))
    if family is None:
        raise _refuse("family_missing")
    provider = models.KIND_PROVIDER[kind]
    if body.get("provider") not in (None, "", provider):
        raise _refuse("bad_provider")
    if models.is_cloud_model({"name": model}):
        raise _refuse("cloud_model")
    with state.lock:
        project = require_project(state)
        problem, info = models.contact_problem(project, r, body.get("url"), kind)
        if problem in ("bad_url", "name_not_found"):
            raise _refuse(problem)
        if kind == "anthropic" and info["address_class"] != "remote":
            raise _refuse("anthropic_remote_only")
        if problem:
            raise _refuse(problem)
        key = _key(state.secrets, r)
    digest, detected, server = None, models.family_hint(model, provider), None
    if kind == "local":                    # the server's own list is the truth for digest, family and cloud
        try:
            found = models.discover(info["endpoint"], kind=kind, key=key)
        except models.ModelsError as e:
            raise api_error(e) from None
        entry = next((m for m in found if m["name"] == model), None)
        if entry is None:
            raise _refuse("model_not_found")
        if entry["remote"]:
            raise _refuse("cloud_model")
        digest, server = entry["digest"], entry["server"]
        detected = entry["family"] or entry["family_hint"]
    with state.lock:
        if state.project is not project:
            raise ApiError(409, "no_project", strings.t("project.error.no_project"))
        models.set_role(project, r, {"kind": kind, "provider": provider, "url": info["endpoint"],
                                     "host": info["host"], "address_class": info["address_class"], "model": model,
                                     "family": family, "family_detected": detected, "digest": digest,
                                     "server": server, "connection": None})
        project.save()
        return {"models": summary(project, state.secrets)}


@route("POST", "/api/models/clear")
def clear(state, body, query):
    r = _role(body)
    with state.lock:
        project = require_project(state)
        models.clear_role(project, r)
        project.save()
        return {"models": summary(project, state.secrets)}


@route("POST", "/api/models/test")
def test_connection(state, body, query):
    r = _role(body)
    with state.lock:
        project = require_project(state)
        entry = models.role(project, r)
        if not entry:
            raise _refuse("role_not_set")
        entry = dict(entry)
        _contact(project, r, entry["url"], entry["kind"])
        key = _key(state.secrets, r)
    result = models.connection_test(r, entry, key=key)
    with state.lock:
        now = models.role(project, r) if state.project is project else None
        if now and all(now.get(k) == entry.get(k) for k in ("url", "model", "provider", "kind")):
            now["connection"] = dict(result)
            project.save()
        message = None
        if result["error"]:
            message = strings.t(models.connection_error_key(result["error"]))
        elif result["thinking_seen"]:
            message = strings.t("setup.models.thinking_ignored")
        return {"result": result, "message": message,
                "models": summary(project, state.secrets) if state.project is project else None}


@route("POST", "/api/models/optin")
def optin(state, body, query):
    r = _role(body)
    with state.lock:
        project = require_project(state)
        if body.get("understood") is not True:
            raise _refuse("not_understood")
        try:
            rec = models.log_optin(project, r, body.get("host"))
        except models.ModelsError as e:
            raise api_error(e) from None
        return {"optin": rec, "models": summary(project, state.secrets)}


@route("POST", "/api/models/declaration")
def declaration(state, body, query):
    r = _role(body)
    with state.lock:
        project = require_project(state)
        try:
            d = models.log_declaration(project, r, body.get("provider"), body.get("host"), body.get("basis"),
                                       body.get("ticked"))
        except models.ModelsError as e:
            raise api_error(e) from None
        return {"declaration": d, "models": summary(project, state.secrets)}


@route("POST", "/api/models/key")
def set_key(state, body, query):
    r = _role(body)
    with state.lock:
        require_project(state)
        secrets = state.secrets
        if body.get("clear") is True:
            secrets.clear(r)
            return {"role": r, "key": {"set": False}}
        if "key" in body:
            try:
                secrets.set(r, body.get("key"))
            except ValueError:
                raise _refuse("bad_key") from None
            return {"role": r, "key": {"set": True, "source": "pasted"}}
        if "env_name" in body:
            name = body.get("env_name")
            try:
                secrets.set_env_name(r, name)
            except ValueError:
                raise _refuse("bad_env_name") from None
            return {"role": r, "key": {"set": True, "source": "env", "env_present": bool(os.environ.get(name))}}
        raise _refuse("key_missing")


@route("POST", "/api/models/confirm-families")
def confirm(state, body, query):
    with state.lock:
        project = require_project(state)
        try:
            models.confirm_families(project, {r: body.get(r) for r in models.ROLES})
        except models.ModelsError as e:
            raise api_error(e) from None
        project.save()
        return {"models": summary(project, state.secrets)}
