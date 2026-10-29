"""Item 22 (23 Oct): the Setup screen's "Choose folder…" button. The app opens the system's folder window and hands
back the chosen path. Every test fakes ``subprocess.run``; no real window opens."""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from second_scribe.app import folder_dialog as fd
from second_scribe.app import strings
from second_scribe.app.routes import folder_dialog as fdr
from tests import test_server_security as tss

TITLE = "Choose the project folder"


def which_only(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


def fake_run(stdout=b"", stderr=b"", code=0, calls=None, raise_=None, wait=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        if wait is not None:
            wait.wait(5)
        if raise_ is not None:
            raise raise_
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    return run


class TestCommand(unittest.TestCase):
    def test_macos_uses_osascript_with_the_title_as_an_argument(self):
        argv = fd.command(TITLE, platform="darwin", which=which_only("osascript"))
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-1], TITLE)
        script = " ".join(argv[1:-1])
        self.assertIn("choose folder with prompt (item 1 of argv)", script)
        self.assertIn("POSIX path of", script)
        self.assertNotIn(TITLE, script)                        # the title is data, never script text

    def test_windows_uses_powershell_with_a_fixed_script(self):
        argv = fd.command(TITLE, platform="win32", which=which_only("powershell"))
        self.assertEqual(argv[:5], ["/usr/bin/powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command"])
        self.assertEqual(argv[5], fd._WIN)
        self.assertIn("FolderBrowserDialog", argv[5])
        self.assertIn("$env:" + fd.PROMPT_ENV, argv[5])
        self.assertNotIn(TITLE, " ".join(argv))
        self.assertEqual(fd.command(TITLE, platform="win32", which=which_only("pwsh"))[0], "/usr/bin/pwsh")

    def test_linux_zenity_then_kdialog_and_only_with_a_display(self):
        disp = {"DISPLAY": ":0"}
        self.assertEqual(fd.command(TITLE, platform="linux", which=which_only("zenity", "kdialog"), env=disp),
                         ["/usr/bin/zenity", "--file-selection", "--directory", "--title", TITLE])
        argv = fd.command(TITLE, platform="linux", which=which_only("kdialog"), env={"WAYLAND_DISPLAY": "w"})
        self.assertEqual(argv[0:2], ["/usr/bin/kdialog", "--getexistingdirectory"])
        self.assertEqual(argv[-2:], ["--title", TITLE])
        self.assertIsNone(fd.command(TITLE, platform="linux", which=which_only("zenity"), env={}))
        self.assertIsNone(fd.command(TITLE, platform="linux", which=which_only(), env=disp))

    def test_none_when_the_program_is_missing(self):
        self.assertIsNone(fd.command(TITLE, platform="darwin", which=which_only()))
        self.assertIsNone(fd.command(TITLE, platform="win32", which=which_only()))
        self.assertFalse(fd.available(TITLE, platform="darwin", which=which_only()))
        self.assertTrue(fd.available(TITLE, platform="darwin", which=which_only("osascript")))


class TestChoose(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.mac = dict(platform="darwin", which=which_only("osascript"))

    def test_the_chosen_folder_comes_back_without_its_trailing_slash(self):
        calls = []
        got = fd.choose(TITLE, run=fake_run(stdout=(str(self.dir) + "/\n").encode(), calls=calls), **self.mac)
        self.assertEqual(got, str(self.dir))
        argv, kw = calls[0]
        self.assertIsInstance(argv, list)
        self.assertIs(kw["shell"], False)
        self.assertEqual(kw["timeout"], fd.TIMEOUT)
        self.assertEqual(fd.TIMEOUT, 120)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["env"][fd.PROMPT_ENV], TITLE)

    def test_a_path_with_spaces_and_accents_is_kept(self):
        odd = self.dir / "Estudo São Paulo"
        odd.mkdir()
        got = fd.choose(TITLE, run=fake_run(stdout=(str(odd) + "/\n").encode("utf-8")), **self.mac)
        self.assertEqual(got, str(odd))

    def test_cancel_returns_none_on_each_system(self):
        self.assertIsNone(fd.choose(TITLE, run=fake_run(code=1, stderr=b"execution error: User canceled. (-128)"),
                                    **self.mac))
        self.assertIsNone(fd.choose(TITLE, run=fake_run(stdout=b""), platform="win32",
                                    which=which_only("powershell")))
        for prog in ("zenity", "kdialog"):
            with self.subTest(prog=prog):
                self.assertIsNone(fd.choose(TITLE, run=fake_run(code=1), platform="linux", which=which_only(prog),
                                            env={"DISPLAY": ":0"}))

    def test_timeout_other_exits_and_odd_answers_are_refused(self):
        with self.assertRaises(fd.DialogError) as c:
            fd.choose(TITLE, run=fake_run(raise_=subprocess.TimeoutExpired(["x"], 120)), **self.mac)
        self.assertEqual(c.exception.code, "timeout")
        cases = {
            "osascript error": fake_run(code=1, stderr=b"execution error: no user interaction allowed (-1713)"),
            "not started": fake_run(raise_=FileNotFoundError("osascript")),
            "relative": fake_run(stdout=b"Documents/x\n"),
            "missing": fake_run(stdout=str(self.dir / "nope").encode()),
            "two lines": fake_run(stdout=(str(self.dir) + "\n" + str(self.dir)).encode()),
            "a file": fake_run(stdout=str(Path(__file__).resolve()).encode()),
        }
        for name, run in cases.items():
            with self.subTest(name), self.assertRaises(fd.DialogError) as c:
                fd.choose(TITLE, run=run, **self.mac)
            self.assertEqual(c.exception.code, "failed")
        with self.assertRaises(fd.DialogError):
            fd.choose(TITLE, run=fake_run(), platform="darwin", which=which_only())     # no window at all


class TestRoutes(tss.ServerCase):
    """Through the real server: session cookie, CSRF and POST, as for every other app endpoint."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)
        self.calls = []
        self.addCleanup(setattr, fdr, "RUN", None)
        fdr.RUN = fake_run(stdout=(str(self.dir) + "/\n").encode(), calls=self.calls)
        p = mock.patch.object(fd, "command", lambda prompt, **kw: ["/usr/bin/osascript", "-e", "x", prompt])
        p.start()
        self.addCleanup(p.stop)

    def send(self, obj, csrf="default", cookie="default"):
        status, _, data = self.post("/api/project/choose-folder", obj, self.cookie if cookie == "default" else cookie,
                                    self.token_csrf if csrf == "default" else csrf)
        return status, json.loads(data)

    def test_availability_opens_nothing(self):
        status, _, data = self.raw("GET", "/api/project/folder-dialog", headers=[("Cookie", self.cookie)])
        self.assertEqual((status, json.loads(data)), (200, {"available": True}))
        self.assertEqual(self.calls, [])

    def test_choose_returns_the_path_and_ignores_the_body(self):
        status, doc = self.send({"path": "/etc", "prompt": "\" & do shell script \"rm -rf ~"})
        self.assertEqual((status, doc), (200, {"path": str(self.dir)}))
        argv, kw = self.calls[0]
        self.assertEqual(argv, ["/usr/bin/osascript", "-e", "x", strings.t("setup.project.choose_prompt")])
        self.assertIs(kw["shell"], False)
        self.assertIsNone(self.state.project, "choosing opens or creates nothing")

    def test_cancel_and_refusals(self):
        fdr.RUN = fake_run(code=1, stderr=b"User canceled. (-128)")
        self.assertEqual(self.send({}), (200, {"path": None}))
        fdr.RUN = fake_run(raise_=subprocess.TimeoutExpired(["x"], 120))
        self.assertEqual(self.send({}), (504, {"error": "folder_dialog_timeout",
                                               "message": strings.t("setup.project.choose_timeout")}))
        fdr.RUN = fake_run(code=2)
        self.assertEqual(self.send({}), (502, {"error": "folder_dialog_failed",
                                               "message": strings.t("setup.project.choose_failed")}))

    def test_needs_the_session_and_the_csrf_token_and_post(self):
        status, _ = self.send({}, csrf="wrong")
        self.assertEqual(status, 403)
        status, _, _ = self.raw("POST", "/api/project/choose-folder", b"{}",
                                headers=[("X-SS-CSRF", self.token_csrf), ("Content-Type", "application/json")])
        self.assertEqual(status, 401)
        status, _, _ = self.raw("GET", "/api/project/choose-folder", headers=[("Cookie", self.cookie)])
        self.assertIn(status, (404, 405))
        self.assertEqual(self.calls, [])

    def test_one_window_at_a_time(self):
        gate = threading.Event()
        fdr.RUN = fake_run(stdout=str(self.dir).encode(), wait=gate)
        first = {}
        t = threading.Thread(target=lambda: first.update(r=self.send({})))
        t.start()
        try:
            for _ in range(100):
                if fdr._OPEN.locked():
                    break
                threading.Event().wait(0.02)
            self.assertEqual(self.send({}), (409, {"error": "folder_dialog_open",
                                                   "message": strings.t("setup.project.choose_open")}))
        finally:
            gate.set()
            t.join(10)
        self.assertEqual(first["r"], (200, {"path": str(self.dir)}))


if __name__ == "__main__":
    unittest.main()
