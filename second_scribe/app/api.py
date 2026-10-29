"""The app's JSON API: the route table, ``ApiError``, ``AppState`` and ``Secrets`` (Task 31; routes from Task 32 on).

Adding routes (Tasks 32-42). Put them in a NEW module under ``second_scribe/app/routes/`` (``routes/project.py``,
``routes/importer.py``, ...); ``load_route_modules()`` imports every module there when the server starts, so no task
edits a shared line to register its routes::

    from ..api import ApiError, route

    @route("POST", "/api/project/create")
    def create_project(state, body, query):
        ...                                   # body: the request's JSON object ({} for GET); query: {name: value}
        return {"project": ...}               # a dict, sent as JSON with status 200

A handler refuses with ``raise ApiError(409, "file_changed", strings.t("..."))``, which becomes
``{"error": "file_changed", "message": "..."}`` with that status. Any other exception is a 500 ``internal`` whose
details go to the terminal only. The server has already checked the Host header, the session cookie and, for a POST,
the CSRF header and the body (JSON object, at most ``MAX_BODY`` bytes) before a handler runs. Handlers run on the
server's threads: take ``state.lock`` around anything that reads and then writes shared state.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Tuple

Handler = Callable[["AppState", dict, dict], dict]

MAX_BODY = 64 * 1024 * 1024           # imports (Task 33) arrive as base64 JSON
METHODS = ("GET", "POST")
ROUTE_PACKAGE = "second_scribe.app.routes"
# Routes the server answers itself; nothing else may claim them.
RESERVED = frozenset({("GET", "/api/session"), ("POST", "/api/session")})
_PATH = re.compile(r"^/api(?:/[a-z0-9_-]+)+$")
_CODE = re.compile(r"^[a-z][a-z0-9_]*$")

ROUTES: Dict[Tuple[str, str], Handler] = {}


class ApiError(Exception):
    """A refusal the browser shows: HTTP ``status`` (4xx/5xx), a stable ``code`` and a user-visible ``message``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        if not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError(f"an ApiError status must be 4xx or 5xx, not {status!r}")
        if not isinstance(code, str) or not _CODE.match(code):
            raise ValueError(f"bad ApiError code: {code!r}")
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message = status, code, str(message)

    def to_json(self) -> dict:
        return {"error": self.code, "message": self.message}


def route(method: str, path: str) -> Callable[[Handler], Handler]:
    """Register ``handler(state, body, query) -> dict`` for ``method`` (GET or POST) on ``path`` (``/api/...``,
    lower-case segments, no trailing slash, no query). Registering the same route twice is a ``ValueError``."""
    if method not in METHODS:
        raise ValueError(f"route method must be one of {METHODS}, not {method!r}")
    if not isinstance(path, str) or not _PATH.match(path):
        raise ValueError(f"bad route path: {path!r}")
    if (method, path) in RESERVED:
        raise ValueError(f"{method} {path} is answered by the server itself")

    def register(handler: Handler) -> Handler:
        if not callable(handler):
            raise TypeError("a route handler must be callable")
        if (method, path) in ROUTES:
            raise ValueError(f"route {method} {path} is already registered")
        ROUTES[(method, path)] = handler
        return handler

    return register


def load_route_modules() -> list[str]:
    """Import every module of ``second_scribe.app.routes`` (each registers its routes on import); names, sorted."""
    pkg = importlib.import_module(ROUTE_PACKAGE)
    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__))
    for name in names:
        importlib.import_module(f"{ROUTE_PACKAGE}.{name}")
    return names


def find(method: str, path: str) -> Handler:
    """The handler for ``method path``; ``ApiError`` 404 ``not_found`` or 405 ``method_not_allowed``."""
    from . import strings
    handler = ROUTES.get((method, path))
    if handler is not None:
        return handler
    if any(p == path for _, p in ROUTES):
        raise ApiError(405, "method_not_allowed", strings.t("server.method_not_allowed"))
    raise ApiError(404, "not_found", strings.t("server.not_found"))


def dispatch(state: "AppState", method: str, path: str, body: dict, query: dict) -> dict:
    """Run the handler for ``method path``. A handler that returns anything but a dict is a ``TypeError``."""
    result = find(method, path)(state, body, query)
    if not isinstance(result, dict):
        raise TypeError(f"route {method} {path} returned {type(result).__name__}, not dict")
    return result


# --- secrets ----------------------------------------------------------------------------------------------------

ROLES = ("recon", "judge", "judge2")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Secrets:
    """API keys, in this process's memory only. Never written, logged, pickled or sent to the browser.

    ``set(role, key)`` keeps a key; ``set_env_name(role, name)`` instead names a variable already in the app's own
    environment. ``env_for(role)`` gives the environment a kit subprocess needs: the kit reads
    ``SS_<PREFIX>_KEY_ENV`` (``llm.ModelConfig.from_env``) and then the variable it names, at request time.
    """

    __slots__ = ("_keys", "_env_names", "_lock")

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._env_names: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _role(role: str) -> str:
        if role not in ROLES:
            raise ValueError(f"unknown model role {role!r}; roles are {', '.join(ROLES)}")
        return role

    def set(self, role: str, key: str) -> None:
        self._role(role)
        if not isinstance(key, str) or not key.strip() or _CONTROL.search(key):
            raise ValueError("an API key must be a non-empty single line")
        with self._lock:
            self._keys[role] = key.strip()
            self._env_names.pop(role, None)

    def set_env_name(self, role: str, name: str) -> None:
        self._role(role)
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise ValueError("an environment variable name is upper-case letters, digits and _")
        with self._lock:
            self._env_names[role] = name
            self._keys.pop(role, None)

    def clear(self, role: str) -> None:
        self._role(role)
        with self._lock:
            self._keys.pop(role, None)
            self._env_names.pop(role, None)

    def has(self, role: str) -> bool:
        self._role(role)
        with self._lock:
            return role in self._keys or role in self._env_names

    def env_for(self, role: str, prefix: str | None = None) -> dict[str, str]:
        """Env entries for ``role``'s kit subprocess; ``prefix`` is the config's model prefix (default: the role).
        Empty when no key is set."""
        self._role(role)
        p = (prefix or role).upper()
        if not _ENV_NAME.match(p):
            raise ValueError(f"bad model prefix {prefix!r}")
        with self._lock:
            if role in self._keys:
                var = f"SS_APP_{role.upper()}_KEY"
                return {f"SS_{p}_KEY_ENV": var, var: self._keys[role]}
            if role in self._env_names:
                return {f"SS_{p}_KEY_ENV": self._env_names[role]}
        return {}

    def __repr__(self) -> str:
        with self._lock:
            roles = sorted(set(self._keys) | set(self._env_names))
        return f"Secrets(roles={roles})"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):            # pickle, copy: never serialised
        raise TypeError("Secrets are never serialised")


# --- state ------------------------------------------------------------------------------------------------------

@dataclass(eq=False)
class AppState:
    """What the running app holds: the open project (Task 32's ``Project``, or None), the keys, the jobs (Tasks 35
    and 39) and one re-entrant lock for read-then-write sequences across server threads."""

    project: Any = None
    secrets: Secrets = field(default_factory=Secrets)
    jobs: dict = field(default_factory=dict)
    estimate_active: bool = False
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def __reduce_ex__(self, protocol):
        raise TypeError("the app state is never serialised")
