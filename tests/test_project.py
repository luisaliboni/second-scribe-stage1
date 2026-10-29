"""Task 32: the project folder (spec section 4, section 6.5 step 1; Review Focus 5).

The model (``second_scribe.app.project``) is tested on temp folders; the routes (``routes/project.py``) through a
real server on 127.0.0.1, with the session and CSRF of Task 31.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from second_scribe.app import api
from second_scribe.app.project import (LAYOUT, PROJECT_FILE, Project, ProjectError, sync_folder_kind,
                                       user_folder)
from second_scribe.audit.verify import verify_run
from tests import test_server_security as tss


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()


class TestLayout(TmpCase):
    def test_layout_created_and_logged(self):
        root = self.tmp / "study"
        p = Project.create(root, "Court pathways")
        for rel in LAYOUT:
            self.assertTrue((root / rel).is_dir(), rel)
        self.assertIn("private", LAYOUT)
        doc = json.loads((root / PROJECT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "project/1")
        self.assertRegex(doc["project_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(doc["name"], "Court pathways")
        for key, empty in (("context", {}), ("files", {}), ("maps", {}), ("references", {}), ("models", {}),
                           ("runs", [])):
            self.assertEqual(doc[key], empty, key)
        self.assertEqual(p.project_id, doc["project_id"])
        recs = p.log.records()
        self.assertEqual([r["record_type"] for r in recs], ["project_created"])
        self.assertEqual(recs[0]["run_id"], doc["project_id"])
        ok, detail = verify_run(str(root / "logs" / "app.jsonl"))
        self.assertTrue(ok, detail)
        self.assertTrue((root / "logs" / "app.manifest.json").is_file())
        # nothing absolute is stored: neither in the project file nor in the log
        for f in (root / PROJECT_FILE, root / "logs" / "app.jsonl", root / "logs" / "app.manifest.json"):
            self.assertNotIn(str(self.tmp), f.read_text(encoding="utf-8"), f.name)

    def test_create_refusals(self):
        Project.create(self.tmp / "a", "A")
        cases = [
            (self.tmp / "a", "A", "project_exists"),
            (self.tmp / "missing-parent" / "b", "B", "parent_missing"),
            (self.tmp / "c", "", "bad_name"),
            (self.tmp / "c", "x\ny", "bad_name"),
        ]
        (self.tmp / "full").mkdir()
        (self.tmp / "full" / "notes.txt").write_text("x", encoding="utf-8")
        cases.append((self.tmp / "full", "F", "folder_not_empty"))
        (self.tmp / "file").write_text("x", encoding="utf-8")
        cases.append((self.tmp / "file", "F", "not_a_folder"))
        for root, name, code in cases:
            with self.subTest(code=code, root=root.name):
                with self.assertRaises(ProjectError) as cm:
                    Project.create(root, name)
                self.assertEqual(cm.exception.code, code)
        self.assertFalse((self.tmp / "c").exists())

    def test_create_in_empty_folder_and_finder_files(self):
        (self.tmp / "empty").mkdir()
        (self.tmp / "empty" / ".DS_Store").write_bytes(b"\0")
        p = Project.create(self.tmp / "empty", "E")
        self.assertTrue((self.tmp / "empty" / PROJECT_FILE).is_file())
        self.assertEqual(p.name, "E")

    def test_open_refusals(self):
        with self.assertRaises(ProjectError) as cm:
            Project.open(self.tmp)
        self.assertEqual(cm.exception.code, "not_a_project")
        (self.tmp / "a-file.txt").write_text("x", encoding="utf-8")      # E19: a file is not a folder
        with self.assertRaises(ProjectError) as cm:
            Project.open(self.tmp / "a-file.txt")
        self.assertEqual(cm.exception.code, "not_a_folder")
        p = Project.create(self.tmp / "p", "P")
        f = p.root / PROJECT_FILE
        for text in ("{", "[]", json.dumps({"schema": "project/9"}),
                     json.dumps({**json.loads(f.read_text()), "project_id": "../x"}),
                     json.dumps({**json.loads(f.read_text()), "files": []})):
            good = f.read_text()
            f.write_text(text, encoding="utf-8")
            with self.subTest(text=text[:40]):
                with self.assertRaises(ProjectError) as cm:
                    Project.open(p.root)
                self.assertEqual(cm.exception.code, "bad_project_file")
            f.write_text(good, encoding="utf-8")

    def test_open_or_create(self):
        p = Project.open_or_create(self.tmp / "oc")
        self.assertEqual(p.name, "oc")
        q = Project.open_or_create(self.tmp / "oc")
        self.assertEqual(q.project_id, p.project_id)
        self.assertEqual(len(q.log.records("project_created")), 1)

    def test_save_is_atomic(self):
        p = Project.create(self.tmp / "s", "S")
        before = (p.root / PROJECT_FILE).read_bytes()
        p.data["context"] = {"domain": "courts"}
        with mock.patch("second_scribe.app.project.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                p.save()
        self.assertEqual((p.root / PROJECT_FILE).read_bytes(), before)
        self.assertEqual(sorted(x.name for x in p.root.iterdir() if x.is_file()), [PROJECT_FILE])
        p.save()
        self.assertEqual(Project.open(p.root).data["context"], {"domain": "courts"})


class TestSyncFolders(TmpCase):
    def test_sync_folder_detection_table(self):
        home = self.tmp / "home"           # no iCloud Desktop & Documents here
        home.mkdir()
        table = [
            # macOS
            ("/Users/ana/Library/Mobile Documents/com~apple~CloudDocs/Study", "icloud"),
            ("/Users/ana/Library/Mobile Documents/iCloud~md~obsidian/Documents/x", "icloud"),
            ("/Volumes/Data/com~apple~CloudDocs/Study", "icloud"),
            ("/Users/ana/Library/CloudStorage/OneDrive-ExampleUniversity/Study", "cloudstorage"),
            ("/Users/ana/Library/CloudStorage/GoogleDrive-ana@example.org/My Drive/Study", "cloudstorage"),
            ("/Users/ana/Library/CloudStorage/Dropbox/Study", "cloudstorage"),
            ("/Users/ana/Dropbox/Study", "dropbox"),
            ("/Users/ana/Dropbox (Personal)/Study", "dropbox"),
            ("/Users/ana/OneDrive/Study", "onedrive"),
            ("/Users/ana/Google Drive/Study", "google_drive"),
            # Linux
            ("/home/ana/Dropbox/interviews", "dropbox"),
            ("/home/ana/OneDrive/interviews", "onedrive"),
            # Windows
            (r"C:\Users\ana\Dropbox\Study", "dropbox"),
            (r"C:\Users\ana\Dropbox (Lab)\Study", "dropbox"),
            (r"C:\Users\ana\OneDrive - Example University\Study", "onedrive"),
            (r"C:\Users\ana\OneDrive\Documents\Study", "onedrive"),
            (r"G:\My Drive\Study", "google_drive"),
            (r"C:\Users\ana\Google Drive\Study", "google_drive"),
            (r"C:\Users\ana\iCloudDrive\Study", "icloud"),
            (r"C:\Users\ana\iCloud Drive\Study", "icloud"),
            # not synced
            ("/Users/ana/Studies/court-pathways", None),
            ("/Users/ana/Documents/Study", None),
            ("/home/ana/dropbox-notes/Study", None),
            ("/Users/ana/my-onedrive-export/Study", None),
            ("/Users/ana/Drive/Study", None),
            (r"C:\Users\ana\Documents\Study", None),
            (r"D:\research\interviews", None),
        ]
        self.assertGreaterEqual(len(table), 8)
        for raw, kind in table:
            with self.subTest(path=raw):
                path = PureWindowsPath(raw) if "\\" in raw else Path(raw)
                self.assertEqual(sync_folder_kind(path, home=home), kind)
                self.assertEqual(sync_folder_kind(raw, home=home), kind)

    def test_more_providers_by_folder_name(self):
        """E18: Box, pCloud, Nextcloud, MEGA, Sync.com and SharePoint folders, and anything under CloudStorage."""
        home = self.tmp / "home"
        home.mkdir()
        table = [
            ("/Users/ana/Box/Study", "box"),
            ("/Users/ana/Box Sync/Study", "box"),
            (r"C:\Users\ana\Box\Study", "box"),
            ("/Users/ana/Library/CloudStorage/Box-Box/Study", "cloudstorage"),
            ("/Users/ana/Library/CloudStorage/SomeNewProvider/Study", "cloudstorage"),
            ("/Users/ana/pCloud Drive/Study", "pcloud"),
            ("/Users/ana/pCloudDrive/Study", "pcloud"),
            ("/home/ana/pCloudSync/Study", "pcloud"),
            ("/Users/ana/Nextcloud/Study", "nextcloud"),
            (r"C:\Users\ana\Nextcloud\Study", "nextcloud"),
            ("/Users/ana/MEGA/Study", "mega"),
            ("/home/ana/MEGAsync/Study", "mega"),
            (r"C:\Users\ana\MEGA\Study", "mega"),
            ("/Users/ana/Sync/Study", "sync_com"),
            ("/home/ana/Sync/Study", "sync_com"),
            (r"C:\Users\ana\Sync\Study", "sync_com"),
            ("/Users/ana/SharePoint/Study", "sharepoint"),
            (r"C:\Users\ana\SharePoint - Lab\Study", "sharepoint"),
        ]
        for raw, kind in table:
            with self.subTest(path=raw):
                path = PureWindowsPath(raw) if "\\" in raw else Path(raw)
                self.assertEqual(sync_folder_kind(path, home=home), kind)
                self.assertEqual(sync_folder_kind(raw, home=home), kind)

    def test_sync_com_is_the_folder_named_sync_directly_in_home(self):
        home = self.tmp / "home"
        (home / "Sync" / "Study").mkdir(parents=True)
        (home / "Work" / "sync").mkdir(parents=True)
        self.assertEqual(sync_folder_kind(home / "Sync" / "Study", home=home), "sync_com")
        self.assertIsNone(sync_folder_kind(home / "Work" / "sync" / "x", home=home))
        self.assertIsNone(sync_folder_kind("/Users/ana/Work/Sync/Study", home=home))
        self.assertIsNone(sync_folder_kind("/srv/Sync/Study", home=home))

    def test_look_alike_folder_names_are_not_flagged(self):
        home = self.tmp / "home"
        home.mkdir()
        names = ["boxes", "boxing-notes", "Megan", "megabytes", "my-sync-notes", "sync-tools", "async", "Syncing",
                 "pcloudy", "nextcloud-notes", "sharepoints-export", "Sandbox", "inbox", "Dropboxes-old"]
        for name in names:
            for raw in (f"/Users/ana/{name}/Study", f"/Users/ana/Studies/{name}", rf"C:\Users\ana\{name}\Study"):
                with self.subTest(path=raw):
                    path = PureWindowsPath(raw) if "\\" in raw else Path(raw)
                    self.assertIsNone(sync_folder_kind(path, home=home))
                    self.assertIsNone(sync_folder_kind(raw, home=home))

    def test_icloud_desktop_and_documents(self):
        """With iCloud's "Desktop & Documents" on, ~/Documents and ~/Desktop are synced although the path says
        nothing; iCloud Drive then holds Documents/ and Desktop/ folders."""
        home = self.tmp / "home"
        (home / "Documents" / "Study").mkdir(parents=True)
        (home / "Desktop").mkdir()
        self.assertIsNone(sync_folder_kind(home / "Documents" / "Study", home=home))
        cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        (cloud / "Documents").mkdir(parents=True)
        self.assertEqual(sync_folder_kind(home / "Documents" / "Study", home=home), "icloud")
        self.assertIsNone(sync_folder_kind(home / "Desktop" / "Study", home=home))
        (cloud / "Desktop").mkdir()
        self.assertEqual(sync_folder_kind(home / "Desktop" / "Study", home=home), "icloud")
        self.assertIsNone(sync_folder_kind(home / "Studies" / "x", home=home))

    def test_symlink_into_sync_folder_detected(self):
        real = self.tmp / "home" / "Dropbox" / "Study"
        real.mkdir(parents=True)
        link = self.tmp / "study-link"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(sync_folder_kind(link, home=self.tmp / "home"), "dropbox")

    def test_sync_blocks_until_ack_logged(self):
        root = self.tmp / "Dropbox" / "study"
        root.parent.mkdir()
        p = Project.create(root, "S")
        self.assertEqual(p.sync_kind, "dropbox")
        self.assertEqual(p.blocked, "dropbox")
        self.assertFalse(p.summary()["sync"]["acknowledged"])
        self.assertTrue(p.summary()["blocked"])
        rec = p.ack_sync()
        self.assertEqual(rec["record_type"], "sync_folder_ack")
        self.assertEqual(rec["payload"], {"kind": "dropbox"})
        self.assertIsNone(p.blocked)
        again = Project.open(root)
        self.assertIsNone(again.blocked)                       # the logged ack is what counts
        self.assertEqual(len(again.log.records("sync_folder_ack")), 1)
        ok, detail = again.log.verify()
        self.assertTrue(ok, detail)
        # moved into another kind of sync folder: asked again
        other = self.tmp / "OneDrive" / "study"
        other.parent.mkdir()
        os.rename(root, other)
        moved = Project.open(other)
        self.assertEqual(moved.blocked, "onedrive")
        # no warning, nothing to acknowledge
        plain = Project.create(self.tmp / "plain", "P")
        self.assertIsNone(plain.blocked)
        with self.assertRaises(ProjectError) as cm:
            plain.ack_sync()
        self.assertEqual(cm.exception.code, "no_sync_warning")


class TestFilesAndPaths(TmpCase):
    def _with_file(self, name="study"):
        p = Project.create(self.tmp / name, "S")
        t = p.root / "inputs" / "transcripts" / "case-a.txt"
        t.write_text("Q: hello\nA: hi\n", encoding="utf-8")
        m = p.root / "inputs" / "maps" / "case-a.drawio"
        m.write_text("<mxfile/>", encoding="utf-8")
        p.record_file("inputs/transcripts/case-a.txt", kind="transcript")
        p.record_file("inputs/maps/case-a.drawio", kind="map")
        p.save()
        return p, t, m

    def test_record_file_stores_hash_and_relative_key(self):
        p, t, _ = self._with_file()
        meta = Project.open(p.root).data["files"]["inputs/transcripts/case-a.txt"]
        import hashlib
        self.assertEqual(meta["sha256"], hashlib.sha256(t.read_bytes()).hexdigest())
        self.assertEqual(meta["bytes"], t.stat().st_size)
        self.assertEqual(meta["kind"], "transcript")

    def test_changed_files_names_edited_file(self):
        p, t, m = self._with_file()
        self.assertEqual(p.changed_files(), [])
        t.write_text("Q: hello\nA: hi, edited\n", encoding="utf-8")
        self.assertEqual(p.changed_files(), ["inputs/transcripts/case-a.txt"])
        m.unlink()                                       # a deleted file is a changed file too
        self.assertEqual(p.changed_files(), ["inputs/maps/case-a.drawio", "inputs/transcripts/case-a.txt"])
        self.assertEqual(p.summary()["changed_files"], ["inputs/maps/case-a.drawio", "inputs/transcripts/case-a.txt"])

    def test_moved_project_reopens_with_relative_paths(self):
        p, _, _ = self._with_file("before")
        p.log.append("context_set", {"domain": "courts"})
        old_root = str(p.root)
        new_root = self.tmp / "elsewhere" / "after"
        new_root.parent.mkdir()
        os.rename(p.root, new_root)
        q = Project.open(new_root)
        self.assertEqual(q.project_id, p.project_id)
        ok, detail = q.log.verify()
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 2)
        self.assertEqual(q.changed_files(), [])
        for rel in q.data["files"]:
            self.assertFalse(rel.startswith("/"), rel)
            self.assertTrue(q.path(rel).is_file(), rel)
            self.assertTrue(str(q.path(rel)).startswith(str(new_root)), rel)
        self.assertNotIn(old_root, (new_root / PROJECT_FILE).read_text(encoding="utf-8"))
        q.log.append("context_set", {"domain": "courts, again"})   # the chain continues in the new place
        self.assertTrue(q.log.verify()[0])

    def test_paths_cannot_escape_root(self):
        p = Project.create(self.tmp / "p", "P")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x", encoding="utf-8")
        (p.root / "inputs" / "link").symlink_to(outside, target_is_directory=True)
        bad = ["../outside/secret.txt", "inputs/../../outside/secret.txt", "/etc/passwd", str(outside),
               "C:\\Windows\\win.ini", "C:/x", "inputs\\..\\..\\x", "", ".", "./inputs", "inputs//maps",
               "inputs/./maps", "a\x00b", "inputs/link/secret.txt", "~/x", 42, None]
        for rel in bad:
            with self.subTest(rel=rel):
                with self.assertRaises(ProjectError) as cm:
                    p.path(rel)
                self.assertEqual(cm.exception.code, "path_outside")
        self.assertEqual(p.path("inputs/maps"), p.root / "inputs" / "maps")
        self.assertEqual(p.rel(p.root / "inputs" / "maps" / "x.drawio"), "inputs/maps/x.drawio")
        with self.assertRaises(ProjectError):
            p.rel(outside / "secret.txt")
        with self.assertRaises(ProjectError):
            p.record_file("../outside/secret.txt", kind="map")
        # save refuses a non-relative key, so nothing absolute can be stored
        p.data["files"]["/etc/passwd"] = {"sha256": "0" * 64, "bytes": 1}
        with self.assertRaises(ProjectError):
            p.save()

    def test_user_folder(self):
        self.assertEqual(user_folder(str(self.tmp)), self.tmp)
        self.assertEqual(user_folder("~/x"), Path.home() / "x")
        for raw in ("relative/dir", "", "   ", "a\x00b", "/x\ny", 7, None, "/" + "a" * 5000):
            with self.subTest(raw=raw if not isinstance(raw, str) else raw[:20]):
                with self.assertRaises(ProjectError) as cm:
                    user_folder(raw)
                self.assertEqual(cm.exception.code, "bad_path")


class TestRoutes(tss.ServerCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)

    def get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        return status, json.loads(data)

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        return status, json.loads(data)

    def test_create_open_get(self):
        self.assertEqual(self.get("/api/project"), (200, {"project": None}))
        status, doc = self.send("/api/project/create", {"path": str(self.tmp / "one"), "name": "One"})
        self.assertEqual(status, 200, doc)
        summary = doc["project"]
        self.assertEqual(summary["name"], "One")
        self.assertEqual(summary["root"], str(self.tmp / "one"))
        self.assertFalse(summary["blocked"])
        self.assertEqual(summary["log"]["records"], 1)
        self.assertTrue(summary["log"]["ok"])
        self.assertEqual(self.get("/api/project")[1]["project"]["project_id"], summary["project_id"])
        status, doc = self.send("/api/project/create", {"path": str(self.tmp / "one"), "name": "Again"})
        self.assertEqual((status, doc["error"]), (409, "project_exists"))
        Project.create(self.tmp / "two", "Two")
        status, doc = self.send("/api/project/open", {"path": str(self.tmp / "two")})
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc["project"]["name"], "Two")
        self.assertEqual(self.state.project.name, "Two")
        status, doc = self.send("/api/project/open", {"path": str(self.tmp)})
        self.assertEqual((status, doc["error"]), (404, "not_a_project"))
        (self.tmp / "a-file.txt").write_text("x", encoding="utf-8")
        status, doc = self.send("/api/project/open", {"path": str(self.tmp / "a-file.txt")})
        self.assertEqual((status, doc["error"]), (422, "not_a_folder"))
        self.assertEqual(doc["message"], "This path points to a file, and the app needs a folder.")
        self.assertEqual(self.state.project.name, "Two")      # a refused open keeps the open project

    def test_bad_paths_refused(self):
        for body in ({"path": "relative/x", "name": "N"}, {"path": "", "name": "N"}, {"name": "N"},
                     {"path": ["/x"], "name": "N"}, {"path": str(self.tmp) + "/a\u0000b", "name": "N"}):
            with self.subTest(body=body):
                status, doc = self.send("/api/project/create", body)
                self.assertEqual((status, doc["error"]), (422, "bad_path"))
                self.assertEqual(set(doc), {"error", "message"})
        status, doc = self.send("/api/project/open", {"path": "../.."})
        self.assertEqual((status, doc["error"]), (422, "bad_path"))
        self.assertIsNone(self.state.project)

    def test_sync_blocks_until_ack_over_http(self):
        from second_scribe.app.routes.project import require_project

        @api.route("GET", "/api/test-guarded")
        def guarded(state, body, query):                  # what Tasks 33-42 do before touching the project
            return {"name": require_project(state).name}

        status, doc = self.get("/api/test-guarded")
        self.assertEqual((status, doc["error"]), (409, "no_project"))
        (self.tmp / "Dropbox").mkdir()
        status, doc = self.send("/api/project/create", {"path": str(self.tmp / "Dropbox" / "s"), "name": "S"})
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc["project"]["sync"], {"kind": "dropbox", "acknowledged": False})
        self.assertTrue(doc["project"]["blocked"])
        status, doc = self.get("/api/test-guarded")
        self.assertEqual((status, doc["error"]), (409, "sync_folder_unacknowledged"))
        for body in ({}, {"understood": False}, {"understood": "true"}, {"understood": 1}):
            with self.subTest(body=body):
                status, doc = self.send("/api/project/ack-sync", body)
                self.assertEqual((status, doc["error"]), (422, "not_understood"))
        self.assertEqual(self.state.project.log.records("sync_folder_ack"), [])
        status, doc = self.send("/api/project/ack-sync", {"understood": True})
        self.assertEqual(status, 200, doc)
        self.assertFalse(doc["project"]["blocked"])
        self.assertEqual(doc["project"]["sync"], {"kind": "dropbox", "acknowledged": True})
        recs = self.state.project.log.records("sync_folder_ack")
        self.assertEqual([r["payload"] for r in recs], [{"kind": "dropbox"}])
        self.assertEqual(self.get("/api/test-guarded"), (200, {"name": "S"}))
        status, doc = self.send("/api/project/ack-sync", {"understood": True})
        self.assertEqual((status, doc["error"]), (409, "no_sync_warning"))

    def test_ack_without_project(self):
        status, doc = self.send("/api/project/ack-sync", {"understood": True})
        self.assertEqual((status, doc["error"]), (409, "no_project"))

    def test_summary_has_no_file_contents(self):
        p = Project.create(self.tmp / "c", "C")
        t = p.root / "inputs" / "transcripts" / "case-a.txt"
        t.write_text("A: my private words\n", encoding="utf-8")
        p.record_file("inputs/transcripts/case-a.txt", kind="transcript")
        p.save()
        status, doc = self.send("/api/project/open", {"path": str(p.root)})
        self.assertEqual(status, 200, doc)
        self.assertNotIn("private words", json.dumps(doc))
        self.assertEqual(sorted(doc["project"]["files"]), ["inputs/transcripts/case-a.txt"])
        self.assertEqual(doc["project"]["changed_files"], [])
        t.write_text("A: changed\n", encoding="utf-8")
        self.assertEqual(self.get("/api/project")[1]["project"]["changed_files"], ["inputs/transcripts/case-a.txt"])

    def test_messages_come_from_strings(self):
        from second_scribe.app import strings
        status, doc = self.send("/api/project/ack-sync", {"understood": True})
        self.assertEqual(doc["message"], strings.t("project.error.no_project"))
        self.assertFalse(re.search(r"Traceback|Error\(", doc["message"]))


class TestEntryAndPage(TmpCase):
    def test_app_page_loads_setup_steps_before_the_registry(self):
        """The server sorts app-*.js by name, so app-setup-<step>.js comes before app-setup.js; the registry's
        queue (app-setup.js) is built for that order."""
        from second_scribe.app import server
        page = server.build_app_page(server.build_static_allow()).decode("utf-8")
        a = page.index('<script src="/static/js/app-setup-project.js"></script>')
        b = page.index('<script src="/static/js/app-setup.js"></script>')
        self.assertLess(page.index('<script src="/static/js/app-shell.js"></script>'), a)
        self.assertLess(a, b)

    def _main(self, argv):
        from second_scribe.app import __main__ as entry
        seen = {}

        class FakeServer:
            class thread:
                @staticmethod
                def is_alive():
                    return False

            def shutdown(self):
                pass

            def server_close(self):
                pass

        def fake_serve(state, port, open_browser=True):
            seen["state"] = state
            return FakeServer()

        err = __import__("io").StringIO()
        with mock.patch("second_scribe.app.server.serve", fake_serve), mock.patch("sys.stderr", err):
            rc = entry.main(argv)
        return rc, seen.get("state"), err.getvalue()

    def test_main_project_flag_creates_then_opens(self):
        root = self.tmp / "cli-study"
        rc, state, _ = self._main(["--project", str(root), "--no-browser"])
        self.assertEqual(rc, 0)
        self.assertEqual(state.project.name, "cli-study")
        self.assertTrue((root / PROJECT_FILE).is_file())
        rc, state2, _ = self._main(["--project", str(root), "--no-browser"])
        self.assertEqual(rc, 0)
        self.assertEqual(state2.project.project_id, state.project.project_id)

    def test_main_project_flag_refusal(self):
        (self.tmp / "full").mkdir()
        (self.tmp / "full" / "x.txt").write_text("x", encoding="utf-8")
        rc, state, err = self._main(["--project", str(self.tmp / "full"), "--no-browser"])
        self.assertEqual(rc, 2)
        self.assertIsNone(state)
        from second_scribe.app import strings
        self.assertIn(strings.t("project.error.folder_not_empty"), err)


if __name__ == "__main__":
    unittest.main()
