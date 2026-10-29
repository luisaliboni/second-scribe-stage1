"""The local app server (Task 31; spec section 11): stdlib ``http.server`` on 127.0.0.1 only.

Every request passes these checks, in this order:

1. **Host** must be exactly ``127.0.0.1:<port>`` or ``localhost:<port>`` (one header), else 403 ``bad_host``, before
   anything else: a page on another name that resolves to 127.0.0.1 (DNS rebinding) gets nothing.
2. **Method**: GET and POST only. OPTIONS and every other method get 405, and no response ever carries an
   ``Access-Control-*`` header, so no other origin may read or send anything (no CORS, no JSONP).
3. **Session**: the printed link ``/?t=<launch token>`` (constant-time compare) sets an HttpOnly SameSite=Strict
   session cookie and answers 303 to ``/``, so the token leaves the address bar. The token works once: the first
   successful exchange replaces it and prints the new link in the terminal (for a second tab), so the used link in
   the browser's history or a shell's scrollback is no longer a key (audit S5). ``/`` without a session, or with a
   used or wrong token, is a plain 401 page that says to open the link in the terminal. Every ``/api/*`` needs the cookie, else 401 ``no_session``.
4. **CSRF**: every POST needs ``X-SS-CSRF`` equal to the per-launch token from ``GET /api/session`` (cookie
   required), else 403 ``bad_csrf``; an ``Origin`` header, when sent, must be this server's own (403 ``bad_origin``).
5. **Body**: a POST carries one ``Content-Length`` (no chunked), at most ``api.MAX_BODY`` (413), ``Content-Type:
   application/json`` (415), and a JSON object (400 ``bad_json``).
6. **Files**: ``/static/<path>`` is an exact lookup in ``STATIC_ALLOW``, built at start from the files under
   ``static/`` (resolved, inside it, no links, no dot-files, not ``dev/``); any other path is 404, so no traversal
   form reaches the disk. The project folder is never served.

Every response carries the CSP below, ``X-Content-Type-Options: nosniff`` and ``Referrer-Policy: no-referrer``;
everything but static files also carries ``Cache-Control: no-store``. The app page has no inline script or style.
"""
from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import socketserver
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs

from . import api, strings

HOST = "127.0.0.1"
COOKIE = "ss_session"
CSRF_HEADER = "X-SS-CSRF"
CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self'; "
       "script-src 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = (("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                    ("Referrer-Policy", "no-referrer"))

STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_PREFIX = "/static/"
STATIC_EXCLUDE = ("dev",)              # developer pages, not shipped (they carry inline scripts)
APP_PAGE = "app.html"                  # served at "/" behind the session, never under /static/
SCREENS_MARKER = "<!-- ss:screens -->"
SHELL_SCRIPT = "/static/js/app-shell.js"
STRINGS_PATH = "/strings.js"
_SAFE_REL = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$")
_LENGTH = re.compile(r"^[0-9]{1,12}$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def build_static_allow(root: Path = STATIC_ROOT) -> dict[str, str]:
    """``{"/static/<rel>": absolute path}`` for every servable file under ``root``. Left out: links (a link could
    point anywhere), dot-files, ``dev/``, the app page, names outside ``[A-Za-z0-9._-]`` and anything that does not
    resolve inside ``root``."""
    root = Path(root)
    real_root = root.resolve()
    allow: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        posix = rel.as_posix()
        if rel.parts[0] in STATIC_EXCLUDE or posix == APP_PAGE or not _SAFE_REL.match(posix):
            continue
        if any((root / Path(*rel.parts[:i + 1])).is_symlink() for i in range(len(rel.parts))):
            continue
        if not p.is_file():
            continue
        real = p.resolve()
        if not real.is_relative_to(real_root):
            continue
        allow[STATIC_PREFIX + posix] = str(real)
    return allow


def content_type(path: str) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def build_app_page(allow: dict[str, str], root: Path = STATIC_ROOT) -> bytes:
    """``static/app.html`` with the screens' scripts in place of the marker: every ``static/js/app-*.js`` except
    the shell, sorted, after the shell (each screen registers itself with ``SS.shell.register``)."""
    page = (Path(root) / APP_PAGE).read_text(encoding="utf-8")
    if page.count(SCREENS_MARKER) != 1:
        raise RuntimeError(f"{APP_PAGE} must hold the marker {SCREENS_MARKER} exactly once")
    screens = sorted(u for u in allow if re.fullmatch(r"/static/js/app-[a-z0-9-]+\.js", u) and u != SHELL_SCRIPT)
    tags = "\n".join(f'<script src="{u}"></script>' for u in screens)
    return page.replace(SCREENS_MARKER, tags).encode("utf-8")


def build_open_link_page() -> bytes:
    """The plain page for a browser without a session: no script, just what to do."""
    name, text = html.escape(strings.t("app.name")), html.escape(strings.t("server.open_link"))
    css = "".join(f'<link rel="stylesheet" href="/static/css/{c}.css">' for c in ("fonts", "tokens", "base", "app"))
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            # the app page names this icon; without a link here a browser asks for /favicon.ico and gets a 404
            '<link rel="icon" href="/static/favicon.png" type="image/png">\n'
            f"<title>{name}</title>\n{css}\n</head>\n<body>\n"
            f'<main class="ss-open-link">\n<h1>{name}</h1>\n<p>{text}</p>\n</main>\n</body>\n</html>\n').encode("utf-8")


def build_strings_js() -> bytes:
    """``window.SS_STRINGS = {...};`` from en.json (ASCII-escaped JSON, so no line separator can break the script)."""
    table = json.dumps(strings.load_strings("en"), ensure_ascii=True, sort_keys=True)
    return f"window.SS_STRINGS = {table};\n".encode("ascii")


class _Refusal(Exception):
    def __init__(self, status: int, code: str, message_key: str) -> None:
        super().__init__(code)
        self.status, self.code, self.message_key = status, code, message_key


class AppServer(ThreadingHTTPServer):
    """The bound server. ``serve()`` fills the per-launch fields and starts ``serve_forever`` on ``thread``."""

    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind looks the host name up (getfqdn), which can stall; the name is fixed anyway
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def setup_launch(self, state: api.AppState) -> None:
        self.state = state
        self.launch_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self._sessions: list[str] = []
        self._sessions_lock = threading.Lock()
        port = self.server_address[1]
        self.allowed_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        self.allowed_origins = tuple(f"http://{h}" for h in self.allowed_hosts)
        self.static_allow = build_static_allow(STATIC_ROOT)
        self.app_page = build_app_page(self.static_allow)
        self.open_link_page = build_open_link_page()
        self.strings_js = build_strings_js()
        self.link = f"http://{HOST}:{port}/?t={self.launch_token}"
        self.out: TextIO | None = None          # where ``serve`` printed the link; a new link is printed there too
        self._launch_lock = threading.Lock()

    def exchange_launch_token(self, candidate: str) -> str | None:
        """A new session id for the current launch token (constant-time compare), or None. A successful exchange
        replaces the token at once, under a lock (two requests racing with one token: one session), and prints the
        new link where the first one was printed."""
        with self._launch_lock:
            if not _same(candidate, self.launch_token):
                return None
            self.launch_token = secrets.token_urlsafe(32)
            self.link = f"http://{HOST}:{self.server_address[1]}/?t={self.launch_token}"
            link = self.link
            sid = self.new_session()
        print(strings.t("server.link_new", link=link), file=self.out if self.out is not None else sys.stdout,
              flush=True)
        return sid

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._sessions_lock:
            self._sessions.append(sid)
        return sid

    def has_session(self, candidates: list[str]) -> bool:
        with self._sessions_lock:
            sessions = list(self._sessions)
        ok = False
        for c in candidates:
            for s in sessions:       # every pair compared, constant time each
                ok |= hmac.compare_digest(c.encode("utf-8", "surrogateescape"), s.encode("ascii"))
        return ok


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "surrogateescape"), b.encode("utf-8", "surrogateescape"))


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.0"      # one request per connection: an unread body never poisons the next one
    server_version = "SecondScribe"
    sys_version = ""

    # -- plumbing -------------------------------------------------------------------------------------------------
    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args) -> None:   # noqa: A002 - the stdlib's name
        pass                       # no request log: request lines can carry the launch token

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        if not getattr(self, "_static", False):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(self, status: int, body: bytes, ctype: str, headers: tuple = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, doc: dict) -> None:
        self._send(status, json.dumps(doc, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": code, "message": message})

    # -- methods --------------------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = do_OPTIONS

    def _handle(self, method: str) -> None:
        self._static = False
        try:
            self._route(method)
        except _Refusal as r:
            self._error(r.status, r.code, strings.t(r.message_key))
        except api.ApiError as e:
            self._error(e.status, e.code, e.message)
        except Exception:                          # details stay in the terminal, never in the reply
            traceback.print_exc(file=sys.stderr)
            self._error(500, "internal", strings.t("server.internal"))

    # -- checks ---------------------------------------------------------------------------------------------------
    def _check_host(self) -> None:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.server.allowed_hosts:
            raise _Refusal(403, "bad_host", "server.bad_host")

    def _session_ids(self) -> list[str]:
        ids = []
        for header in self.headers.get_all("Cookie") or []:
            for part in header.split(";"):
                name, eq, value = part.strip().partition("=")
                if eq and name == COOKIE and value:
                    ids.append(value)
        return ids

    def _has_session(self) -> bool:
        ids = self._session_ids()
        return bool(ids) and self.server.has_session(ids)

    def _check_csrf(self) -> None:
        sent = self.headers.get_all(CSRF_HEADER) or []
        if len(sent) != 1 or not _same(sent[0], self.server.csrf_token):
            raise _Refusal(403, "bad_csrf", "server.bad_csrf")
        for origin in self.headers.get_all("Origin") or []:
            if origin.strip().lower() not in self.server.allowed_origins:
                raise _Refusal(403, "bad_origin", "server.bad_origin")

    def _read_json(self) -> dict:
        if self.headers.get_all("Transfer-Encoding"):
            raise _Refusal(400, "bad_length", "server.bad_length")
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            raise _Refusal(411, "length_required", "server.length_required")
        if len(lengths) != 1 or not _LENGTH.match(lengths[0].strip()):
            raise _Refusal(400, "bad_length", "server.bad_length")
        n = int(lengths[0])
        if n > api.MAX_BODY:
            raise _Refusal(413, "too_large", "server.too_large")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _Refusal(415, "unsupported_type", "server.unsupported_type")
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise _Refusal(400, "bad_length", "server.bad_length")
        if not data.strip():
            return {}
        try:
            doc = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
        except (UnicodeDecodeError, ValueError):
            raise _Refusal(400, "bad_json", "server.bad_json") from None
        if not isinstance(doc, dict):
            raise _Refusal(400, "bad_json", "server.bad_json")
        return doc

    @staticmethod
    def _query(raw: str) -> dict:
        try:
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=200)
        except ValueError:
            raise _Refusal(400, "bad_query", "server.bad_query") from None
        if any(len(v) != 1 for v in parsed.values()):
            raise _Refusal(400, "bad_query", "server.bad_query")
        return {k: v[0] for k, v in parsed.items()}

    # -- routing --------------------------------------------------------------------------------------------------
    def _route(self, method: str) -> None:
        self._check_host()                         # before anything else (DNS rebinding)
        if method not in ("GET", "POST"):
            raise _Refusal(405, "method_not_allowed", "server.method_not_allowed")
        target = self.path
        if not target.startswith("/") or target.startswith("//"):
            raise _Refusal(404, "not_found", "server.not_found")
        path, _, raw_query = target.partition("?")

        if path == "/" or path == STRINGS_PATH or path.startswith(STATIC_PREFIX):
            if method != "GET":
                raise _Refusal(405, "method_not_allowed", "server.method_not_allowed")
            if path == "/":
                return self._root(raw_query)
            if path == STRINGS_PATH:
                return self._send(200, self.server.strings_js, CONTENT_TYPES[".js"])
            file = self.server.static_allow.get(path)
            if file is None:
                raise _Refusal(404, "not_found", "server.not_found")
            self._static = True
            return self._send(200, Path(file).read_bytes(), content_type(file))

        if path == "/api" or path.startswith("/api/"):
            if not self._has_session():
                raise _Refusal(401, "no_session", "server.no_session")
            body: dict = {}
            if method == "POST":
                self._check_csrf()
                body = self._read_json()
            query = self._query(raw_query)
            if path == "/api/session":
                if method != "GET":
                    raise _Refusal(405, "method_not_allowed", "server.method_not_allowed")
                return self._json(200, {"csrf": self.server.csrf_token})
            result = api.dispatch(self.server.state, method, path, body, query)
            payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return self._send(200, payload, "application/json; charset=utf-8")

        raise _Refusal(404, "not_found", "server.not_found")

    def _root(self, raw_query: str) -> None:
        try:
            query = parse_qs(raw_query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            query = {}
        if "t" in query:
            tokens = query["t"]
            if len(tokens) == 1 and (sid := self.server.exchange_launch_token(tokens[0])) is not None:
                return self._send(303, b"", "text/plain; charset=utf-8", (
                    ("Location", "/"), ("Set-Cookie", f"{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/")))
            return self._send(401, self.server.open_link_page, CONTENT_TYPES[".html"])
        if self._has_session():
            return self._send(200, self.server.app_page, CONTENT_TYPES[".html"])
        return self._send(401, self.server.open_link_page, CONTENT_TYPES[".html"])


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def serve(state: api.AppState, port: int = 0, open_browser: bool = True, *,
          out: TextIO | None = None) -> AppServer:
    """Bind ``(127.0.0.1, port)`` (0: a free port), start serving on a daemon thread (``server.thread``), print
    ``Open this link: http://127.0.0.1:<port>/?t=<token>`` and open it in the browser unless ``open_browser`` is
    false. Stop with ``server.shutdown(); server.server_close()``."""
    api.load_route_modules()
    srv = AppServer((HOST, port), Handler)
    try:
        srv.setup_launch(state)
    except BaseException:
        srv.server_close()
        raise
    srv.out = out
    srv.thread = threading.Thread(target=srv.serve_forever, name="second-scribe-server", daemon=True)
    srv.thread.start()
    print(strings.t("server.link", link=srv.link), file=out if out is not None else sys.stdout, flush=True)
    if open_browser:
        try:
            webbrowser.open(srv.link)
        except Exception:           # no browser here: the printed link is enough
            pass
    return srv
