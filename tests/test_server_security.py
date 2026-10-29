"""Task 31: the local server's security (spec section 11, section 15.4; Review Focus 4).

Every check runs against a real server on an ephemeral port on 127.0.0.1, through ``http.client``; each test
starts its own server and stops it in tearDown. The launch token is read from the printed link, as a user would.
"""
import copy
import html
import http.client
import io
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe.app import api, server, strings

ROOT = Path(__file__).resolve().parent.parent
CSP = ("default-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self'; "
       "script-src 'self'; frame-ancestors 'none'")
LINK = re.compile(r"[Oo]pen this link: (http://127\.0\.0\.1:(\d+)/\?t=([A-Za-z0-9_-]+))")
SECRET = "sk-test-DO-NOT-LEAK-7f3a9c"


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.state = api.AppState()
        self.srv = server.serve(self.state, 0, open_browser=False, out=self.out)
        self.port = self.srv.server_address[1]
        m = LINK.search(self.out.getvalue())
        self.assertIsNotNone(m, self.out.getvalue())
        self.assertEqual(int(m.group(2)), self.port)
        self.link, self.token = m.group(1), m.group(3)
        self.host = f"127.0.0.1:{self.port}"
        self._routes = dict(api.ROUTES)

    def tearDown(self):
        api.ROUTES.clear()
        api.ROUTES.update(self._routes)
        self.srv.shutdown()
        self.srv.server_close()

    # -- raw HTTP ---------------------------------------------------------------------------------------------
    def raw(self, method, path, body=None, headers=(), host="default"):
        """One request; ``headers`` is a list of (name, value) pairs so a header can repeat. ``host=None`` sends
        no Host header at all."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                conn.putheader("Host", self.host if host == "default" else host)
            for k, v in headers:
                conn.putheader(k, v)
            if body is not None and not any(k.lower() == "content-length" for k, _ in headers):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.getheaders(), data
        finally:
            conn.close()

    @staticmethod
    def header(headers, name):
        vals = [v for k, v in headers if k.lower() == name.lower()]
        return vals[0] if len(vals) == 1 else (None if not vals else vals)

    def current_token(self):
        """The launch token of the newest link printed in the terminal (audit S5: each use prints a fresh one)."""
        return LINK.findall(self.out.getvalue())[-1][2]

    def login(self):
        status, headers, _ = self.raw("GET", f"/?t={self.current_token()}")
        self.assertEqual(status, 303)
        cookie = self.header(headers, "Set-Cookie")
        m = re.match(r"ss_session=([A-Za-z0-9_-]+);", cookie)
        self.assertIsNotNone(m, cookie)
        return f"ss_session={m.group(1)}"

    def csrf(self, cookie):
        status, _, data = self.raw("GET", "/api/session", headers=[("Cookie", cookie)])
        self.assertEqual(status, 200, data)
        return json.loads(data)["csrf"]

    def post(self, path, obj, cookie, csrf, extra=()):
        body = json.dumps(obj).encode()
        return self.raw("POST", path, body, headers=[("Cookie", cookie), ("X-SS-CSRF", csrf),
                                                     ("Content-Type", "application/json"), *extra])

    def error_of(self, data):
        doc = json.loads(data)
        self.assertEqual(set(doc), {"error", "message"}, doc)
        return doc["error"]


class TestHost(ServerCase):
    def test_wrong_host_403(self):
        cookie = self.login()
        bad_hosts = ["evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}", "127.0.0.1",
                     f"localhost:{self.port}.evil.example", f"127.0.0.1:{self.port}@evil.example",
                     f"[::1]:{self.port}", f"0.0.0.0:{self.port}", ""]
        for host in bad_hosts:
            for method, path, hdrs in (("GET", "/api/session", [("Cookie", cookie)]),
                                       ("GET", f"/?t={self.token}", []),
                                       ("GET", "/static/js/core.js", []),
                                       ("OPTIONS", "/api/session", [])):
                with self.subTest(host=host, path=path, method=method):
                    status, headers, data = self.raw(method, path, headers=hdrs, host=host)
                    self.assertEqual(status, 403)
                    self.assertEqual(self.error_of(data), "bad_host")
                    self.assertIsNone(self.header(headers, "Set-Cookie"))
        with self.subTest("no Host header"):
            status, _, data = self.raw("GET", "/api/session", headers=[("Cookie", cookie)], host=None)
            self.assertEqual((status, self.error_of(data)), (403, "bad_host"))
        with self.subTest("two Host headers"):
            status, _, data = self.raw("GET", "/api/session",
                                       headers=[("Cookie", cookie), ("Host", "evil.example")])
            self.assertEqual((status, self.error_of(data)), (403, "bad_host"))

    def test_good_hosts_accepted(self):
        cookie = self.login()
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}"):
            with self.subTest(host=host):
                status, _, _ = self.raw("GET", "/api/session", headers=[("Cookie", cookie)], host=host)
                self.assertEqual(status, 200)


class TestSession(ServerCase):
    def test_no_cookie_401_json(self):
        cases = [[], [("Cookie", "ss_session=forged-value")], [("Cookie", "ss_session=")],
                 [("Cookie", "other=1")], [("Cookie", "ss_session=\"x;;\\")], [("Cookie", f"ss_session={self.token}")]]
        for hdrs in cases:
            for method, path in (("GET", "/api/session"), ("GET", "/api/nope"), ("POST", "/api/nope")):
                with self.subTest(headers=hdrs, path=path, method=method):
                    body = b"{}" if method == "POST" else None
                    status, headers, data = self.raw(method, path, body, headers=hdrs)
                    self.assertEqual(status, 401)
                    self.assertTrue(self.header(headers, "Content-Type").startswith("application/json"))
                    self.assertEqual(self.error_of(data), "no_session")
                    self.assertNotIn(b"csrf", data)

    def test_root_without_session_shows_open_link_page(self):
        text = html.escape(strings.t("server.open_link")).encode()
        for path, hdrs in (("/", []), ("/?t=wrong-token", []), ("/?t=", []), (f"/?t={self.token}&t=x", []),
                           ("/", [("Cookie", "ss_session=forged")])):
            with self.subTest(path=path, headers=hdrs):
                status, headers, data = self.raw("GET", path, headers=hdrs)
                self.assertEqual(status, 401)
                self.assertTrue(self.header(headers, "Content-Type").startswith("text/html"))
                self.assertIn(text, data)
                self.assertIn(b"<!doctype html>", data.lower())
                self.assertNotIn(b"Traceback", data)
                self.assertNotIn(b"<script", data)          # a plain page: nothing to run
                self.assertNotIn(self.token.encode(), data)
                self.assertIsNone(self.header(headers, "Set-Cookie"))

    def test_open_link_page_names_its_icon_so_the_browser_asks_for_no_favicon_ico(self):
        # 2.2 issue 7: a page with no icon link makes the browser request /favicon.ico, which is a 404 in its console
        status, _, data = self.raw("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn(b'<link rel="icon" href="/static/favicon.png" type="image/png">', data)
        # the icon it names is served with no session, as a PNG, and is tiny
        status, headers, icon = self.raw("GET", "/static/favicon.png")
        self.assertEqual(status, 200)
        self.assertEqual(self.header(headers, "Content-Type"), "image/png")
        self.assertTrue(icon.startswith(b"\x89PNG"))
        self.assertLess(len(icon), 1024)

    def test_launch_token_303_sets_cookie_and_strips_token(self):
        status, headers, _ = self.raw("GET", f"/?t={self.token}")
        self.assertEqual(status, 303)
        self.assertEqual(self.header(headers, "Location"), "/")
        self.assertNotIn("t=", self.header(headers, "Location"))
        cookie = self.header(headers, "Set-Cookie")
        parts = [p.strip() for p in cookie.split(";")]
        self.assertRegex(parts[0], r"^ss_session=[A-Za-z0-9_-]{43,}$")
        self.assertNotIn(self.token, parts[0])
        self.assertEqual(set(parts[1:]), {"HttpOnly", "SameSite=Strict", "Path=/"})
        # the cookie opens the app shell, which never carries the launch token
        status, headers, data = self.raw("GET", "/", headers=[("Cookie", parts[0])])
        self.assertEqual(status, 200)
        self.assertTrue(self.header(headers, "Content-Type").startswith("text/html"))
        self.assertIn(b'<script src="/static/js/app-shell.js"></script>', data)
        self.assertNotIn(self.token.encode(), data)
        # two opens of the link give two different sessions, both valid
        other = self.login()
        self.assertNotEqual(other, parts[0])
        self.assertEqual(self.raw("GET", "/api/session", headers=[("Cookie", parts[0])])[0], 200)
        self.assertEqual(self.raw("GET", "/api/session", headers=[("Cookie", other)])[0], 200)

    def test_launch_token_rotates_after_first_use(self):
        """Audit S5: the used link is no longer a key (the plain open-link page, no cookie); the terminal gets a new
        link, which works once in turn; the sessions already made stay valid."""
        first = self.login()
        self.assertEqual(len(LINK.findall(self.out.getvalue())), 2)
        new_token = self.current_token()
        self.assertNotEqual(new_token, self.token)
        self.assertGreaterEqual(len(new_token), 43)
        self.assertIn(strings.t("server.link_new", link=f"http://127.0.0.1:{self.port}/?t={new_token}"),
                      self.out.getvalue())
        status, headers, data = self.raw("GET", f"/?t={self.token}")         # the used link, e.g. from history
        self.assertEqual(status, 401)
        self.assertIsNone(self.header(headers, "Set-Cookie"))
        self.assertIn(strings.t("server.open_link").encode(), data)
        self.assertNotIn(new_token.encode(), data)
        second = self.login()                                                 # the new link opens a second tab
        self.assertNotEqual(first, second)
        self.assertEqual(self.raw("GET", f"/?t={new_token}")[0], 401)        # and is spent in turn
        for c in (first, second):
            self.assertEqual(self.raw("GET", "/api/session", headers=[("Cookie", c)])[0], 200)
        self.assertNotEqual(self.current_token(), new_token)

    def test_launch_token_one_winner_under_concurrency(self):
        """Two requests racing with the same token: exactly one gets a session."""
        import concurrent.futures
        tok = self.current_token()
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda _: self.raw("GET", f"/?t={tok}")[0], range(8)))
        self.assertEqual(sorted(got), [303] + [401] * 7)

    def test_launch_token_strength(self):
        self.assertGreaterEqual(len(self.token), 43)        # secrets.token_urlsafe(32)
        self.assertEqual(server.CSRF_HEADER, "X-SS-CSRF")

    def test_session_returns_csrf_only(self):
        cookie = self.login()
        status, headers, data = self.raw("GET", "/api/session", headers=[("Cookie", cookie)])
        self.assertEqual(status, 200)
        doc = json.loads(data)
        self.assertEqual(set(doc), {"csrf"})
        self.assertGreaterEqual(len(doc["csrf"]), 43)
        self.assertNotEqual(doc["csrf"], self.token)
        self.assertNotIn(doc["csrf"], cookie)


class TestCsrf(ServerCase):
    def setUp(self):
        super().setUp()

        @api.route("POST", "/api/_test/echo")
        def echo(state, body, query):
            return {"got": body}

    def test_post_without_csrf_403(self):
        cookie = self.login()
        good = self.csrf(cookie)
        body = b'{"a": 1}'
        cases = {
            "missing": [],
            "empty": [("X-SS-CSRF", "")],
            "wrong": [("X-SS-CSRF", "x" * len(good))],
            "launch token": [("X-SS-CSRF", self.token)],
            "session id": [("X-SS-CSRF", cookie.split("=", 1)[1])],
            "twice": [("X-SS-CSRF", good), ("X-SS-CSRF", good)],
        }
        for name, hdrs in cases.items():
            with self.subTest(name):
                status, _, data = self.raw("POST", "/api/_test/echo", body,
                                           headers=[("Cookie", cookie), ("Content-Type", "application/json"), *hdrs])
                self.assertEqual(status, 403)
                self.assertEqual(self.error_of(data), "bad_csrf")
        with self.subTest("cross-origin Origin with the right token"):
            status, _, data = self.post("/api/_test/echo", {"a": 1}, cookie, good,
                                        extra=[("Origin", "http://evil.example")])
            self.assertEqual((status, self.error_of(data)), (403, "bad_origin"))
        with self.subTest("the right token"):
            status, _, data = self.post("/api/_test/echo", {"a": 1}, cookie, good)
            self.assertEqual(status, 200, data)
            self.assertEqual(json.loads(data), {"got": {"a": 1}})
        with self.subTest("the right token and the app's own Origin"):
            status, _, _ = self.post("/api/_test/echo", {"a": 1}, cookie, good,
                                     extra=[("Origin", f"http://127.0.0.1:{self.port}")])
            self.assertEqual(status, 200)


class TestCors(ServerCase):
    def test_no_cors_headers_and_options_405(self):
        cookie = self.login()
        evil = [("Origin", "http://evil.example"), ("Access-Control-Request-Method", "POST"),
                ("Access-Control-Request-Headers", "x-ss-csrf, content-type")]
        responses = []
        for path in ("/", "/api/session", "/api/nope", "/static/js/core.js"):
            with self.subTest(path=path):
                status, headers, data = self.raw("OPTIONS", path, headers=evil)
                self.assertEqual(status, 405)
                self.assertEqual(self.error_of(data), "method_not_allowed")
                responses.append(headers)
        for method in ("PUT", "DELETE", "PATCH", "HEAD"):
            with self.subTest(method=method):
                status, headers, _ = self.raw(method, "/api/session", headers=[("Cookie", cookie)])
                self.assertEqual(status, 405)
                responses.append(headers)
        responses.append(self.raw("GET", "/api/session", headers=[("Cookie", cookie), *evil[:1]])[1])
        responses.append(self.raw("GET", f"/?t={self.token}", headers=evil[:1])[1])
        responses.append(self.raw("GET", "/static/js/core.js", headers=evil[:1])[1])
        responses.append(self.raw("GET", "/api/session", headers=evil[:1])[1])
        for headers in responses:
            names = [k.lower() for k, _ in headers]
            self.assertFalse([n for n in names if n.startswith("access-control-")], headers)


class TestHeaders(ServerCase):
    def test_csp_header_exact(self):
        cookie = self.login()
        csrf = self.csrf(cookie)
        samples = {
            "open-link page": self.raw("GET", "/"),
            "303": self.raw("GET", f"/?t={self.token}"),
            "app shell": self.raw("GET", "/", headers=[("Cookie", cookie)]),
            "static": self.raw("GET", "/static/css/base.css"),
            "strings": self.raw("GET", "/strings.js"),
            "api": self.raw("GET", "/api/session", headers=[("Cookie", cookie)]),
            "api 401": self.raw("GET", "/api/session"),
            "api 404": self.post("/api/nope", {}, cookie, csrf),
            "403 host": self.raw("GET", "/", host="evil.example"),
            "404": self.raw("GET", "/nope"),
            "405": self.raw("OPTIONS", "/"),
            "413": self.raw("POST", "/api/nope", b"", headers=[
                ("Cookie", cookie), ("X-SS-CSRF", csrf), ("Content-Type", "application/json"),
                ("Content-Length", str(api.MAX_BODY + 1))]),
        }
        for name, (status, headers, _) in samples.items():
            with self.subTest(name, status=status):
                self.assertEqual(self.header(headers, "Content-Security-Policy"), CSP)
                self.assertEqual(self.header(headers, "X-Content-Type-Options"), "nosniff")
                self.assertEqual(self.header(headers, "Referrer-Policy"), "no-referrer")
                server_hdr = self.header(headers, "Server") or ""
                self.assertNotIn("Python", server_hdr)
        for name in ("api", "api 401", "api 404", "413", "app shell", "303", "open-link page"):
            with self.subTest(name, cache=True):
                self.assertEqual(self.header(samples[name][1], "Cache-Control"), "no-store")
        self.assertEqual(samples["413"][0], 413)

    def test_app_shell_has_no_inline_script_or_style(self):
        cookie = self.login()
        _, _, data = self.raw("GET", "/", headers=[("Cookie", cookie)])
        page = data.decode()
        for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S):
            self.assertEqual(tag.strip(), "", "inline script in app.html")
        self.assertNotRegex(page, r"<style\b")
        self.assertNotRegex(page, r"\sstyle\s*=")
        self.assertNotRegex(page, r"\son[a-z]+\s*=")
        self.assertNotRegex(page, r"https?://")
        for src in re.findall(r'(?:src|href)="([^"]+)"', page):
            with self.subTest(src=src):
                self.assertTrue(src.startswith("/static/") or src == "/strings.js", src)
                self.assertEqual(self.raw("GET", src)[0], 200, src)

    def test_strings_js_is_the_strings_table(self):
        status, headers, data = self.raw("GET", "/strings.js")
        self.assertEqual(status, 200)
        self.assertTrue(self.header(headers, "Content-Type").startswith("text/javascript"))
        text = data.decode()
        prefix = "window.SS_STRINGS = "
        self.assertTrue(text.startswith(prefix), text[:60])
        self.assertEqual(json.loads(text[len(prefix):].rstrip().rstrip(";")), strings.load_strings("en"))
        self.assertNotIn(" ", text)


class TestStatic(ServerCase):
    def test_static_traversal_404(self):
        attacks = [
            "/static/../server.py", "/static/../../second_scribe/app/server.py", "/static/%2e%2e/server.py",
            "/static/%2E%2E%2Fserver.py", "/static/..%2fserver.py", "/static/..%2F..%2Fpyproject.toml",
            "/static/css/../../server.py", "/static/css/..%2f..%2fserver.py", "/static//etc/passwd",
            "/static/%2fetc%2fpasswd", "/static/..\\server.py", "/static/..%5cserver.py", "/static/js/core.js%00",
            "/static/%252e%252e/server.py", "/static/./js/core.js", "/static/js//core.js", "/static/",
            "/static", "/static/dev/viewer.html", "/static/dev/data/tryit.js", "/static/app.html",
            "/../pyproject.toml", "/server.py", "/second-scribe-project.json", "/logs/app.jsonl",
            "/static/JS/CORE.JS", "/static/js/core.js/", "/static/js", "/strings.js/../server.py",
            f"http://127.0.0.1:{self.port}/static/../server.py",
        ]
        for path in attacks:
            with self.subTest(path=path):
                status, _, data = self.raw("GET", path)
                self.assertEqual(status, 404, data[:200])
                self.assertNotIn(b"import ", data)
                self.assertNotIn(b"[project]", data)

    def test_static_allowed_files_served_with_types(self):
        for path, ctype in (("/static/js/core.js", "text/javascript"), ("/static/css/base.css", "text/css"),
                            ("/static/fonts/SchibstedGrotesk-Variable-latin.woff2", "font/woff2"),
                            ("/static/js/api.js", "text/javascript"), ("/static/css/app.css", "text/css")):
            with self.subTest(path=path):
                status, headers, data = self.raw("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(self.header(headers, "Content-Type").startswith(ctype))
                rel = path[len("/static/"):]
                self.assertEqual(data, (server.STATIC_ROOT / rel).read_bytes())

    def test_static_allow_skips_links_outside_and_hidden(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-static-") as tmp:
            root = Path(tmp) / "static"
            (root / "js").mkdir(parents=True)
            (root / "js" / "a.js").write_text("1", encoding="utf-8")
            (root / ".hidden").write_text("x", encoding="utf-8")
            (root / "dev").mkdir()
            (root / "dev" / "page.html").write_text("x", encoding="utf-8")
            outside = Path(tmp) / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                os.symlink(outside, root / "js" / "link.js")
            except (OSError, NotImplementedError):
                self.skipTest("no symlinks here")
            allow = server.build_static_allow(root)
            self.assertEqual(sorted(allow), ["/static/js/a.js"])
            for p in allow.values():
                self.assertTrue(Path(p).resolve().is_relative_to(root.resolve()))


class TestBind(ServerCase):
    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.port, 0)
        self.assertEqual(server.HOST, "127.0.0.1")
        self.assertTrue(self.link.startswith(f"http://127.0.0.1:{self.port}/?t="))


class TestApiSkeleton(ServerCase):
    def setUp(self):
        super().setUp()
        self.cookie = self.login()
        self.tok = self.csrf(self.cookie)

        @api.route("GET", "/api/_test/query")
        def query(state, body, q):
            return {"q": q, "body": body, "state_is": type(state).__name__}

        @api.route("POST", "/api/_test/refuse")
        def refuse(state, body, q):
            raise api.ApiError(409, "file_changed", "This file changed since you imported it.")

        @api.route("POST", "/api/_test/crash")
        def crash(state, body, q):
            raise RuntimeError(f"boom {SECRET}")

        @api.route("POST", "/api/_test/bad_return")
        def bad_return(state, body, q):
            return ["not", "a", "dict"]

    def test_get_route_gets_query_and_state(self):
        status, headers, data = self.raw("GET", "/api/_test/query?pid=MAP-A&x=a%20b", headers=[("Cookie", self.cookie)])
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data), {"q": {"pid": "MAP-A", "x": "a b"}, "body": {}, "state_is": "AppState"})
        self.assertEqual(self.header(headers, "Cache-Control"), "no-store")
        status, _, data = self.raw("GET", "/api/_test/query?pid=a&pid=b", headers=[("Cookie", self.cookie)])
        self.assertEqual((status, self.error_of(data)), (400, "bad_query"))

    def test_api_error_becomes_json(self):
        status, _, data = self.post("/api/_test/refuse", {}, self.cookie, self.tok)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(data), {"error": "file_changed",
                                            "message": "This file changed since you imported it."})

    def test_crash_is_500_without_details(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            status, _, data = self.post("/api/_test/crash", {}, self.cookie, self.tok)
            status2, _, data2 = self.post("/api/_test/bad_return", {}, self.cookie, self.tok)
        for st, d in ((status, data), (status2, data2)):
            self.assertEqual(st, 500)
            self.assertEqual(self.error_of(d), "internal")
            self.assertNotIn(b"boom", d)
            self.assertNotIn(SECRET.encode(), d)
            self.assertNotIn(b"Traceback", d)

    def test_body_rules(self):
        base = [("Cookie", self.cookie), ("X-SS-CSRF", self.tok)]
        json_ct = [("Content-Type", "application/json")]
        cases = [
            ("not json", b"{nope", base + json_ct, 400, "bad_json"),
            ("not an object", b"[1, 2]", base + json_ct, 400, "bad_json"),
            ("not utf-8", b'{"a": "\xff"}', base + json_ct, 400, "bad_json"),
            ("form type", b"a=1", base + [("Content-Type", "application/x-www-form-urlencoded")], 415,
             "unsupported_type"),
            ("text/plain", b"{}", base + [("Content-Type", "text/plain")], 415, "unsupported_type"),
            ("too large", b"", base + json_ct + [("Content-Length", str(api.MAX_BODY + 1))], 413, "too_large"),
            ("bad length", b"", base + json_ct + [("Content-Length", "-5")], 400, "bad_length"),
            ("chunked", b"", base + json_ct + [("Transfer-Encoding", "chunked"), ("Content-Length", "0")], 400,
             "bad_length"),
        ]
        for name, body, hdrs, want_status, want_code in cases:
            with self.subTest(name):
                status, _, data = self.raw("POST", "/api/_test/refuse", body, headers=hdrs)
                self.assertEqual((status, self.error_of(data)), (want_status, want_code))
        with self.subTest("no length"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.putrequest("POST", "/api/_test/refuse", skip_host=True, skip_accept_encoding=True)
            for k, v in [("Host", self.host)] + base + json_ct:
                conn.putheader(k, v)
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual((resp.status, self.error_of(resp.read())), (411, "length_required"))
            conn.close()
        self.assertEqual(api.MAX_BODY, 64 * 1024 * 1024)

    def test_unknown_route_and_wrong_method(self):
        status, _, data = self.raw("GET", "/api/nope", headers=[("Cookie", self.cookie)])
        self.assertEqual((status, self.error_of(data)), (404, "not_found"))
        status, _, data = self.post("/api/_test/query", {}, self.cookie, self.tok)
        self.assertEqual((status, self.error_of(data)), (405, "method_not_allowed"))

    def test_route_decorator_rules(self):
        with self.assertRaises(ValueError):
            api.route("GET", "/api/session")(lambda s, b, q: {})       # the server's own route
        with self.assertRaises(ValueError):
            api.route("GET", "/api/_test/query")(lambda s, b, q: {})   # already registered
        for method, path in (("PUT", "/api/x"), ("GET", "/x"), ("GET", "/api/x/"), ("GET", "/api/x?y=1"),
                             ("GET", "/api/../x"), ("get", "/api/x")):
            with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                api.route(method, path)(lambda s, b, q: {})

    def test_api_error_fields(self):
        e = api.ApiError(422, "span_too_short", "Mark at least eight words.")
        self.assertEqual((e.status, e.code, e.message), (422, "span_too_short", "Mark at least eight words."))
        with self.assertRaises(ValueError):
            api.ApiError(200, "ok", "not an error")


class TestSecrets(unittest.TestCase):
    def test_secrets_not_in_repr(self):
        s = api.Secrets()
        s.set("judge", SECRET)
        state = api.AppState(secrets=s)
        for text in (repr(s), str(s), repr(state), str(state), f"{s!r} {state}"):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET[-6:], text)
        self.assertIn("judge", repr(s))
        for attempt in (lambda: pickle.dumps(s), lambda: copy.copy(s), lambda: copy.deepcopy(s)):
            with self.assertRaisesRegex(TypeError, "never serialised"):
                attempt()
        with self.assertRaises(TypeError):
            json.dumps(s)
        with self.assertRaisesRegex(TypeError, "never serialised"):
            pickle.dumps(state)
        self.assertTrue(s.has("judge"))
        self.assertFalse(s.has("recon"))

    def test_env_for_names_the_variable_only_for_its_role(self):
        s = api.Secrets()
        s.set("judge", SECRET)
        self.assertEqual(s.env_for("judge"), {"SS_JUDGE_KEY_ENV": "SS_APP_JUDGE_KEY", "SS_APP_JUDGE_KEY": SECRET})
        self.assertEqual(s.env_for("recon"), {})
        s.set_env_name("recon", "MY_PROVIDER_KEY")
        self.assertEqual(s.env_for("recon"), {"SS_RECON_KEY_ENV": "MY_PROVIDER_KEY"})
        for bad_role in ("Judge", "other", ""):
            with self.subTest(role=bad_role), self.assertRaises(ValueError):
                s.set(bad_role, "k")
        for bad in ("", "  "):
            with self.assertRaises(ValueError):
                s.set("judge", bad)
        for bad_name in ("lower", "1ABC", "A-B", "A B", ""):
            with self.subTest(name=bad_name), self.assertRaises(ValueError):
                s.set_env_name("recon", bad_name)
        s.clear("judge")
        self.assertEqual(s.env_for("judge"), {})


class TestLaunch(unittest.TestCase):
    def test_open_browser_called_with_link(self):
        out = io.StringIO()
        with mock.patch("webbrowser.open") as opener:
            srv = server.serve(api.AppState(), 0, open_browser=True, out=out)
        try:
            link = LINK.search(out.getvalue()).group(1)
            opener.assert_called_once_with(link)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_main_prints_link_and_serves(self):
        proc = subprocess.Popen([sys.executable, "-m", "second_scribe.app", "--no-browser"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            m = LINK.search(line)
            self.assertIsNotNone(m, line + proc.stderr.read() if proc.poll() is not None else line)
            port = int(m.group(2))
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", f"/?t={m.group(3)}")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 303)
            self.assertEqual(resp.getheader("Location"), "/")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
