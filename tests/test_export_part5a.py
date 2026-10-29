"""Part 5a (23 Oct): the Export screen's "Open" and "Show in folder" buttons (scout F11), the three plain checks of
the profile preview (F12), the methods paragraph in plain words with its hashes on a final Records line (F13), and the
report's order (F9: plain sentences first, the hashes and panels in a closed "Technical details").

Every opener test fakes ``subprocess.Popen``; nothing is opened on screen. The project is test_export's oracle demo
run (zero model calls)."""
from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from second_scribe.app import api, export, opener, strings
from second_scribe.app.routes import export as export_routes
from tests.test_export import RUN_ID, ExportCase, call
from tests.test_wording import find_hits


def which_only(*names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


class FakePopen:
    """Stands in for subprocess.Popen: records the call, answers ``code`` (or times out)."""

    def __init__(self, code=0, timeout=False, raise_=None):
        self.calls, self.code, self.timeout, self.raise_ = [], code, timeout, raise_

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raise_ is not None:
            raise self.raise_
        outer = self

        def wait(timeout=None):
            if outer.timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            return outer.code
        return SimpleNamespace(wait=wait)


# ----------------------------------------------------------------------------------------------- F11 opener
class TestOpenerCommand(unittest.TestCase):
    PATH = "/p/exports/methods-A-1.txt"

    def test_macos_open_and_reveal(self):
        self.assertEqual(opener.command(self.PATH, False, platform="darwin", which=which_only("open")),
                         ["/usr/bin/open", self.PATH])
        self.assertEqual(opener.command(self.PATH, True, platform="darwin", which=which_only("open")),
                         ["/usr/bin/open", "-R", self.PATH])

    def test_windows_explorer_and_select(self):
        p = r"C:\p\exports\methods-A-1.txt"
        with mock.patch.object(opener.os.path, "isabs", return_value=True):
            self.assertEqual(opener.command(p, False, platform="win32", which=which_only("explorer")),
                             ["/usr/bin/explorer", p])
            self.assertEqual(opener.command(p, True, platform="win32", which=which_only("explorer")),
                             ["/usr/bin/explorer", "/select," + p])

    def test_linux_needs_a_display_and_reveal_opens_the_folder(self):
        disp = {"DISPLAY": ":0"}
        self.assertIsNone(opener.command(self.PATH, False, platform="linux", which=which_only("xdg-open"), env={}))
        self.assertEqual(opener.command(self.PATH, False, platform="linux", which=which_only("xdg-open"), env=disp),
                         ["/usr/bin/xdg-open", self.PATH])
        self.assertEqual(opener.command(self.PATH, True, platform="linux", which=which_only("xdg-open"), env=disp),
                         ["/usr/bin/xdg-open", "/p/exports"])
        self.assertIsNone(opener.command(self.PATH, False, platform="linux", which=which_only(), env=disp))

    def test_relative_path_never_reaches_a_command(self):
        self.assertIsNone(opener.command("exports/x.txt", False, platform="darwin", which=which_only("open")))
        self.assertIsNone(opener.command("-R", False, platform="darwin", which=which_only("open")))

    def test_open_path_fixed_argv_no_shell(self):
        fake = FakePopen()
        opener.open_path(self.PATH, False, popen=fake, platform="darwin", which=which_only("open"))
        argv, kw = fake.calls[0]
        self.assertEqual(argv, ["/usr/bin/open", self.PATH])
        self.assertIs(kw["shell"], False)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)

    def test_open_path_errors(self):
        with self.assertRaises(opener.OpenError) as cm:
            opener.open_path(self.PATH, False, popen=FakePopen(), platform="darwin", which=which_only())
        self.assertEqual(cm.exception.code, "unavailable")
        with self.assertRaises(opener.OpenError) as cm:
            opener.open_path(self.PATH, False, popen=FakePopen(code=1), platform="darwin", which=which_only("open"))
        self.assertEqual(cm.exception.code, "failed")
        with self.assertRaises(opener.OpenError) as cm:
            opener.open_path(self.PATH, False, popen=FakePopen(raise_=FileNotFoundError()), platform="darwin",
                             which=which_only("open"))
        self.assertEqual(cm.exception.code, "failed")
        # an opener still handing over after WAIT seconds is not an error
        opener.open_path(self.PATH, False, popen=FakePopen(timeout=True), platform="darwin", which=which_only("open"))

    def test_explorer_exit_one_is_not_an_error(self):
        p = r"C:\p\exports\methods-A-1.txt"
        with mock.patch.object(opener.os.path, "isabs", return_value=True):
            opener.open_path(p, True, popen=FakePopen(code=1), platform="win32", which=which_only("explorer"))


class TestOpenRoute(ExportCase):
    def setUp(self):
        super().setUp()
        self.fake = FakePopen()
        patcher = mock.patch.object(export_routes, "POPEN", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        # the route must not depend on the test machine having an opener
        p2 = mock.patch.object(opener, "command", lambda path, reveal, **kw: ["/usr/bin/open"] + (["-R"] if reveal else []) + [path])
        p2.start()
        self.addCleanup(p2.stop)

    def saved_methods(self) -> str:
        return export.save_methods(self.p, RUN_ID)["path"]

    def test_open_and_show_a_saved_file(self):
        rel = self.saved_methods()
        out = call(self.p, "POST", "/api/export/open", {"path": rel})
        self.assertEqual(out, {"opened": True, "reveal": False})
        out = call(self.p, "POST", "/api/export/open", {"path": rel, "reveal": True})
        self.assertTrue(out["reveal"])
        target = str(self.p.path(rel).resolve())
        self.assertEqual([c[0] for c in self.fake.calls], [["/usr/bin/open", target], ["/usr/bin/open", "-R", target]])

    def test_report_page_opens(self):
        rel = call(self.p, "POST", "/api/export/report", {"run_id": RUN_ID})["path"]
        call(self.p, "POST", "/api/export/open", {"path": rel})
        self.assertTrue(self.fake.calls[0][0][-1].endswith("index.html"))

    def test_paths_outside_exports_refused(self):
        self.saved_methods()
        for bad in ("project.json", "../x", "/etc/hosts", "exports", "exports/", "exports/.staging/x.json",
                    "runs/A-" + RUN_ID + "/config.json", "", None, 3, "a" * 600):
            with self.subTest(path=bad), self.assertRaises(api.ApiError) as cm:
                call(self.p, "POST", "/api/export/open", {"path": bad})
            self.assertIn(cm.exception.code, ("bad_path",))
        self.assertEqual(self.fake.calls, [])

    def test_link_out_of_exports_refused(self):
        self.saved_methods()
        link = self.p.path("exports") / "escape.txt"
        os.symlink(self.p.path("project.json") if self.p.path("project.json").exists() else "/etc/hosts", link)
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/open", {"path": "exports/escape.txt"})
        self.assertEqual(cm.exception.code, "bad_path")
        self.assertEqual(self.fake.calls, [])

    def test_missing_and_unopenable(self):
        self.saved_methods()
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/open", {"path": "exports/nothing.txt"})
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "export_missing"))
        (self.p.path("exports") / "tool.sh").write_text("echo hi\n", encoding="utf-8")
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/open", {"path": "exports/tool.sh"})
        self.assertEqual(cm.exception.code, "cannot_open")
        # a folder can be shown, never opened
        rel = call(self.p, "POST", "/api/export/report", {"run_id": RUN_ID})["path"]
        folder = rel.rsplit("/", 1)[0]
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/open", {"path": folder})
        self.assertEqual(cm.exception.code, "cannot_open")
        call(self.p, "POST", "/api/export/open", {"path": folder, "reveal": True})
        self.assertEqual(len(self.fake.calls), 1)

    def test_opener_failures_answer_plainly(self):
        rel = self.saved_methods()
        with mock.patch.object(export_routes, "POPEN", FakePopen(code=2)):
            with self.assertRaises(api.ApiError) as cm:
                call(self.p, "POST", "/api/export/open", {"path": rel})
        self.assertEqual((cm.exception.status, cm.exception.code), (502, "open_failed"))
        with mock.patch.object(opener, "command", lambda *a, **k: None):
            with self.assertRaises(api.ApiError) as cm:
                call(self.p, "POST", "/api/export/open", {"path": rel})
        self.assertEqual((cm.exception.status, cm.exception.code), (501, "opener_unavailable"))

    def test_refused_while_b_coding_open(self):
        rel = self.saved_methods()
        self.p.data["references"] = {"SYN-OW01": {"type": "B"}}
        self.p.save()
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/open", {"path": rel})
        self.assertEqual(cm.exception.code, "coding_unlocked")
        self.assertEqual(self.fake.calls, [])

    def test_runs_route_says_whether_files_can_be_opened(self):
        with mock.patch.object(opener, "available", return_value=False):
            self.assertIs(call(self.p, "GET", "/api/export/runs")["can_open"], False)
        with mock.patch.object(opener, "available", return_value=True):
            self.assertIs(call(self.p, "GET", "/api/export/runs")["can_open"], True)

    def test_module_never_uses_a_shell_string(self):
        src = (Path(opener.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        self.assertNotRegex(src, r"\bPopen\(\s*['\"]")


# ----------------------------------------------------------------------------------------------- F12 preview
class TestPreviewChecks(ExportCase):
    def test_checks_counts(self):
        pv = export.profile_preview(self.p, RUN_ID, 10)
        ch = pv["checks"]
        doc = pv["doc"]
        cells = [c for r in doc["rows"] for c in r["cells"]]
        self.assertEqual(ch["cells"], len(cells))
        self.assertEqual(ch["not_run"], sum(1 for c in cells if c.get("state") == "not_run"))
        sizes = [doc["population"]["n_people"]] + [r["n_people"] for r in doc["rows"] if isinstance(r.get("n_people"), int)]
        self.assertEqual(ch["smallest_group"], min(sizes))
        # a profile that passes never shows a smallest group below k
        self.assertTrue(pv["exportable"])
        self.assertGreaterEqual(ch["smallest_group"], 10)

    def test_checks_go_through_the_route(self):
        out = call(self.p, "POST", "/api/export/profile/preview", {"run_id": RUN_ID, "k": 10})
        self.assertEqual(set(out["checks"]), {"smallest_group", "not_run", "cells"})

    def test_checks_on_a_hand_made_doc(self):
        doc = {"population": {"n_people": 40}, "rows": [
            {"row": 1, "n_people": 31, "cells": [{"name": "a"}, {"name": "b", "state": "not_run"}]},
            {"row": 2, "cells": [{"name": "c", "state": "not_run"}]}, "junk"]}
        self.assertEqual(export.preview_checks(doc), {"smallest_group": 31, "not_run": 2, "cells": 3})
        self.assertEqual(export.preview_checks({"rows": []}), {"smallest_group": None, "not_run": 0, "cells": 0})

    def test_people_line_has_no_state(self):
        self.assertNotIn("state", strings.t("export.profile.people", n=12, k=10))


# ----------------------------------------------------------------------------------------------- F13 methods
class TestMethodsPlain(ExportCase):
    def methods(self):
        return export.export_methods(self.p, RUN_ID)

    def test_records_line_is_last_and_holds_every_hash(self):
        text = self.methods()
        body, records = text.rstrip("\n").rsplit("\n\n", 1)
        self.assertTrue(records.startswith("Records: profile schema orgsci-profile/v2; code sha256 "))
        cert = export.run_record(self.p, export.find_run(self.p, RUN_ID))
        r0 = cert["runs"][0]
        for h in (r0["code_sha256"], r0["config_sha256"], r0["prompt_set_sha256"], r0["verify"]["head_hash"]):
            self.assertIn(h, records)
            self.assertNotIn(h, body)
        self.assertNotRegex(body, r"\b[0-9a-f]{64}\b")

    def test_plain_words(self):
        text = self.methods()
        self.assertNotIn("state pending", text)
        self.assertNotIn("(state", text)
        self.assertNotIn("Controls:", text)
        self.assertNotIn("—", text)
        self.assertIn("the study's instructions (rung 2)", text)
        self.assertIn("the kit authors' original draft instructions (rung 1)", text)
        self.assertIn("Each set of instructions was run once, so every contrast between them is descriptive.", text)
        self.assertIn("are never given to the model", text)
        self.assertEqual(find_hits(text), [])
        for banned in ("rather than", "ground truth", "validated", "replicable", "official script"):
            self.assertNotIn(banned, text.lower())

    def test_family_names_never_config_keys(self):
        run = export.find_run(self.p, RUN_ID)
        view = dict(export.load_view(self.p, run))
        view["run"] = dict(view.get("run") or {}, models={"recon": "qwen3.8-flash-next", "judge": "gemma-4-27b"})
        with mock.patch.object(export, "load_view", return_value=view):
            text = export.export_methods(self.p, RUN_ID)
        self.assertIn("from the Qwen family and the judge from the Gemma family", text)
        self.assertNotRegex(text, r"\bqwen2\b|recon: qwen|judge: gemma")

    def test_family_from_settings_when_ids_are_aliases(self):
        self.assertEqual(export._family_name("recon", "qwen2"), "Qwen")
        self.assertEqual(export._family_name("deepseek-r2", None), "DeepSeek")
        self.assertIsNone(export._family_name("judge", None))


# ----------------------------------------------------------------------------------------------- F9 report
class TestReportOrder(ExportCase):
    def test_plain_lead_first_hashes_in_closed_details(self):
        page = self.report().read_text(encoding="utf-8")
        lead = page.index('id="report-lead"')
        results = page.index('id="report-viewer"')
        details = page.index('<details class="report-details" id="report-technical" data-part="technical">')
        self.assertLess(lead, results)
        self.assertLess(results, details)
        self.assertNotIn("<details open", page)
        # every sha256 sits inside Technical details
        for m in re.finditer(r"\b[0-9a-f]{64}\b", page):
            self.assertGreater(m.start(), details, page[m.start() - 80:m.start()])
        for needle in ('id="report-profile"', 'class="report-stamp"', 'class="report-record"'):
            if needle in page:
                self.assertGreater(page.index(needle), details, needle)
        self.assertIn(strings.t("report.details.summary"), page)

    def test_facts_line_in_plain_words(self):
        page = self.report().read_text(encoding="utf-8")
        m = re.search(r'<p class="report-facts"[^>]*>(.*?)</p>', page)
        self.assertIsNotNone(m)
        self.assertIn("UTC", m.group(1))
        self.assertIn("people", m.group(1))


if __name__ == "__main__":
    unittest.main()
