"""Task 42: the report, the "Profile for comparison", the methods paragraph and the export screen's routes.

Plan amendments V6, V13 and V14 replace the old numbers file: the export is the lab's profile (``orgsci-profile/v2``,
state ``pending`` for own data, ``synthetic`` for the demo corpus), written by the kit exporter through
``profile_view.run_exporter`` and gated by ``publish.assert_publishable``. The report comes first; the profile is the
secondary action, named "Profile for comparison".

Test data: a finished synthetic demo run (the oracle client, zero model calls) placed in a temp project's
``runs/A-A-0a1b2c3d/``, built once per process and copied per test.
"""
import contextlib
import hashlib
import html
import html.parser
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import certificate, profile_export, publish
from second_scribe.app import api, export, profile_view, results, strings
from second_scribe.app.project import Project
from second_scribe.cli import DEMO_CONFIG
from tests.test_wording import find_hits

REPO = Path(__file__).resolve().parent.parent
RUN_ID = "A-0a1b2c3d"
RUN_FOLDER = "runs/A-" + RUN_ID
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_CACHE: dict = {}


def _template() -> Path:
    """A project folder holding one finished oracle demo run (built once per process)."""
    if "root" not in _CACHE:
        root = Path(tempfile.mkdtemp(prefix="ss-tests-export-tpl-")) / "proj"
        p = Project.create(root, "Export test")
        run = p.path(RUN_FOLDER)
        run.mkdir(parents=True)
        cfg = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        cfg.update(run_id=RUN_ID, base_dir=str(Path(DEMO_CONFIG).resolve().parent), out_dir=str(run))
        (run / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(run), "--client",
                            "oracle", "--config", str(run / "config.json")], cwd=REPO, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError("demo run failed:\n" + r.stdout[-2000:] + r.stderr[-2000:])
        _CACHE["root"] = root
    return _CACHE["root"]


def fresh_project() -> Project:
    """A copy of the template project; config.json's out_dir is pointed at the copy."""
    dest = Path(tempfile.mkdtemp(prefix="ss-tests-export-")) / "proj"
    shutil.copytree(_template(), dest, symlinks=True)
    cfg_path = dest / RUN_FOLDER / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["out_dir"] = str(dest / RUN_FOLDER)
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return Project.open(dest)


def _sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def routes_loaded():
    api.load_route_modules()


def call(project, method, path, body=None, query=None):
    routes_loaded()
    state = api.AppState(project=project)
    return api.dispatch(state, method, path, body or {}, query or {})


class _Refs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs: list[str] = []
        self.inline_scripts = 0
        self.styles = 0
        self.style_attrs = 0
        self.meta_csp = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            if a.get("src"):
                self.refs.append(a["src"])
            else:
                self.inline_scripts += 1
        if tag == "link" and a.get("href"):
            self.refs.append(a["href"])
        if tag in ("img", "source") and a.get("src"):
            self.refs.append(a["src"])
        if tag == "a" and a.get("href") and not a["href"].startswith("#"):
            self.refs.append(a["href"])
        if tag == "style":
            self.styles += 1
        if "style" in a:
            self.style_attrs += 1
        if tag == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy":
            self.meta_csp.append(a.get("content"))


class ExportCase(unittest.TestCase):
    def assertInPage(self, text, page):
        """``text`` in a page or paragraph, as written (methods) or HTML-escaped (report)."""
        self.assertTrue(text in page or html.escape(text, quote=True) in page, text)

    def setUp(self):
        self.p = fresh_project()

    def report(self, **kw) -> Path:
        return export.export_report(self.p, RUN_ID, None, **kw)

    def log_kinds(self):
        return [r["payload"]["kind"] for r in self.p.log.records("export")]


# ----------------------------------------------------------------------------------------------- run lookup
class TestRunLookup(ExportCase):
    def test_find_run_by_id(self):
        run = export.find_run(self.p, RUN_ID)
        self.assertEqual((run.run_id, run.group), (RUN_ID, "A"))
        self.assertEqual(run.dir, self.p.path(RUN_FOLDER))

    def test_unknown_run_is_404(self):
        with self.assertRaises(api.ApiError) as cm:
            export.find_run(self.p, "A-ffffffff")
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "run_not_found"))

    def test_bad_run_id_refused(self):
        for bad in ("../x", "A-0a1b2c3d/..", "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(api.ApiError) as cm:
                export.find_run(self.p, bad)
            self.assertEqual(cm.exception.code, "bad_run_id")

    def test_list_runs(self):
        runs = export.list_runs(self.p)
        self.assertEqual([(r["run_id"], r["group"], r["finished"]) for r in runs], [(RUN_ID, "A", True)])


# ----------------------------------------------------------------------------------------------- report
class TestReport(ExportCase):
    def test_report_self_contained_no_external_refs(self):
        index = self.report()
        folder = index.parent
        self.assertEqual(folder, self.p.path(f"exports/report-{RUN_ID}"))
        text = index.read_text(encoding="utf-8")
        parser = _Refs()
        parser.feed(text)
        self.assertEqual((parser.inline_scripts, parser.styles, parser.style_attrs), (0, 0, 0))
        from second_scribe.app.build_site import CSP
        self.assertEqual(parser.meta_csp, [CSP])
        self.assertNotRegex(text, r"(?i)https?://")
        self.assertTrue(parser.refs)
        for ref in parser.refs:
            with self.subTest(ref=ref):
                self.assertNotIn("..", ref)
                target = (folder / ref).resolve()
                self.assertTrue(target.is_file(), ref)
                self.assertTrue(str(target).startswith(str(folder.resolve()) + os.sep), ref)
        for css in folder.glob("css/*.css"):
            for url in re.findall(r"url\(([^)]+)\)", css.read_text(encoding="utf-8")):
                url = url.strip("'\" ")
                if url.startswith("data:"):
                    continue
                with self.subTest(css=css.name, url=url):
                    self.assertTrue((css.parent / url).resolve().is_file(), url)
                    self.assertTrue(str((css.parent / url).resolve()).startswith(str(folder.resolve())))
        # copied byte for byte from the viewer's static files
        static = REPO / "second_scribe" / "app" / "static"
        for rel in ("js/viewer.js", "js/profile.js", "css/viewer.css", "css/print.css", "js/report.js"):
            self.assertEqual((folder / rel).read_bytes(), (static / rel).read_bytes(), rel)

    @unittest.skipUnless(CHROME.is_file(), "headless Chrome not installed")
    def test_report_loads_in_chrome_without_csp_violation(self):
        index = self.report()
        with tempfile.TemporaryDirectory(prefix="ss-tests-chrome-") as prof:
            r = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-first-run",
                                f"--user-data-dir={prof}", "--enable-logging=stderr", "--v=0",
                                "--virtual-time-budget=10000", "--dump-dom", index.as_uri()],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        dom, log = r.stdout, r.stderr
        self.assertNotIn("Content Security Policy", log)
        self.assertNotIn("Refused to", log)
        self.assertNotRegex(log, r"Uncaught")
        self.assertIn('data-report="ready"', dom)
        self.assertIn('data-report-styled="yes"', dom)
        self.assertIn('data-section="profile"', dom)
        self.assertIn('data-section="views"', dom)

    def test_css_string_escapes_quotes_backslashes_and_breaks(self):
        self.assertEqual(export.css_string('a"b\\c\nd'), '"a\\"b\\\\c\\a d"')
        self.assertEqual(export.css_string("x\x00y</style>"), '"xy</style>"')

    def test_report_stamps_view_and_reference_type(self):
        index = self.report()
        text = index.read_text(encoding="utf-8")
        folder = index.parent
        self.assertIn(RUN_ID, text)
        ref = strings.t("export.reference.a")
        # the reference type in the stamp and in the running header printed on every page
        self.assertGreaterEqual(text.count(ref), 2)
        self.assertIn('class="report-running"', text)
        # printed pages carry it in the page's top margin box (css/report-head.css, written for this run)
        head = (folder / "css" / "report-head.css").read_text(encoding="utf-8")
        self.assertIn('href="css/report-head.css"', text)
        self.assertIn("@top-left", head)
        self.assertIn(RUN_ID, head)
        self.assertIn(export.css_string(ref)[1:-1], head)
        self.assertIn(strings.t("report.stamp.view_value"), text)
        view_sha = hashlib.sha256((folder / "data" / "view.js").read_bytes()).hexdigest()
        prof_sha = hashlib.sha256((folder / "data" / "profile.js").read_bytes()).hexdigest()
        self.assertIn(view_sha, text)
        self.assertIn(prof_sha, text)
        # synthetic corpus: the SYNTHETIC band, exactly
        self.assertIn(strings.t("band.synthetic"), text)
        # the run record summary (T41) and its files
        self.assertIn(strings.t("report.record.title"), text)
        self.assertIn(strings.t("report.record.limit_label"), text)
        self.assertTrue((folder / "run-record.json").is_file())
        self.assertTrue((folder / "run-record.md").is_file())
        self.assertIn("run-record.md", text)           # the page names the files it wrote
        self.assertIn("run-record.json", text)
        cert = json.loads((folder / "run-record.json").read_text(encoding="utf-8"))
        self.assertIn(cert["runs"][0]["verify"]["head_hash"], text)
        self.assertInPage(cert["limit"], text)
        # the profile panel and the lanes come from the same run
        view = (folder / "data" / "view.js").read_text(encoding="utf-8")
        self.assertIn('window.SS_DATA["report_view"]', view)
        self.assertIn(RUN_ID, view)
        prof = (folder / "data" / "profile.js").read_text(encoding="utf-8")
        self.assertIn('window.SS_DATA["report_profile"]', prof)
        self.assertIn("orgsci-profile/v2", prof)
        # one plain sentence per number: the report says how each number reads
        self.assertIn(strings.t("report.how_to_read"), text)
        self.assertIn('id="report-print"', text)

    def test_report_logged_with_sha(self):
        index = self.report()
        recs = self.p.log.records("export")
        self.assertEqual([r["payload"]["kind"] for r in recs], ["report"])
        self.assertEqual(recs[0]["payload"]["sha256"], export.folder_sha256(index.parent))
        self.assertEqual(recs[0]["payload"]["run_id"], RUN_ID)

    def test_no_exported_file_is_named_certificate(self):
        """R18: the report folder and ``exports/`` never carry the word in a file name (nor in the page's text)."""
        index = self.report()
        export.save_certificate(self.p, RUN_ID)
        export.save_methods(self.p, RUN_ID)
        names = [x.name for x in self.p.path("exports").rglob("*") if ".staging" not in x.parts]
        self.assertIn("run-record.json", names)
        self.assertIn("index.html", names)
        self.assertEqual([n for n in names if "certificate" in n.lower()], [])
        self.assertNotIn("certificate", index.read_text(encoding="utf-8").lower())

    def test_report_rewrite_replaces_folder(self):
        index = self.report()
        stale = index.parent / "js" / "stale.js"
        stale.write_text("x", encoding="utf-8")
        self.report()
        self.assertFalse(stale.exists())

    def _remote_declaration(self, run):
        basis = "REB 1234 approves this provider under a zero-retention agreement"
        host = ".".join(("api", "example", "org"))
        doc = {"schema": "declaration/1", "role": "judge", "provider": "anthropic", "host": host, "basis": basis}
        sha = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.p.log.append("declaration", {**doc, "sha256": sha})
        cfg = json.loads(run.config.read_text(encoding="utf-8"))
        cfg["tool"] = {"remote_endpoints": [{"role": "judge", "host": host, "provider": "anthropic",
                                             "declaration_sha256": sha}]}
        run.config.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg, basis, host

    def test_report_declaration_line_when_remote(self):
        """K4: report, methods and profile preview print exactly what certificate.remote_sentences returns."""
        run = export.find_run(self.p, RUN_ID)
        view, prof = export.load_view(self.p, run), export.stage_profile(self.p, run)[0]
        cfg, basis, host = self._remote_declaration(run)
        want = certificate.remote_sentences(self.p.log.records(), cfg, None)
        self.assertEqual(len(want), 1)
        self.assertTrue(want[0].startswith("Transcript text was sent to "), want[0])
        self.assertTrue(want[0].endswith(f"under: {basis}"), want[0])
        self.assertEqual(export.declaration_lines(self.p, run), want)
        with mock.patch.object(export, "load_view", return_value=view), \
                mock.patch.object(export, "stage_profile", return_value=(prof, "0" * 64)):
            text = self.report().read_text(encoding="utf-8")
            methods = export.export_methods(self.p, RUN_ID)
            preview = export.profile_preview(self.p, RUN_ID, 10)
        self.assertIn(html.escape(want[0]), text)
        self.assertIn(want[0], methods)
        self.assertEqual(preview["declaration"], want)

    def test_declaration_sentence_has_a_single_source(self):
        """No sentence of the kind is built in export.py: a change of the certificate's wording shows everywhere."""
        run = export.find_run(self.p, RUN_ID)
        cfg, basis, host = self._remote_declaration(run)
        fake = ["SENTINEL declaration sentence"]
        with mock.patch.object(certificate, "remote_sentences", return_value=fake):
            self.assertEqual(export.declaration_lines(self.p, run), fake)

    def test_no_declaration_line_when_local(self):
        run = export.find_run(self.p, RUN_ID)
        self.assertEqual(export.declaration_lines(self.p, run), [])
        text = self.report().read_text(encoding="utf-8")
        self.assertNotIn("Transcript text was sent to", text)
        self.assertNotIn("Transcript text was sent to", export.export_methods(self.p, RUN_ID))
        self.assertEqual(export.profile_preview(self.p, RUN_ID, 10)["declaration"], [])


# ----------------------------------------------------------------------------------------------- profile
def _planting_exporter(plant):
    """A run_exporter that runs the real one, then edits the written file with ``plant(doc)``."""
    real = profile_view.run_exporter

    def fake(config_path, out_dir, profile_path, **kw):
        real(config_path, out_dir, profile_path, **kw)
        doc = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        plant(doc)
        raw = json.dumps(doc, sort_keys=True, indent=2).encode("utf-8")
        Path(profile_path).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    return fake


def _first_cell_with(doc, key):
    for row in doc["rows"]:
        for c in row["cells"]:
            if key in c:
                return c
    raise AssertionError(key)


class TestProfile(ExportCase):
    def test_preview_shows_every_line_and_passes(self):
        pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual(pv["refusals"], [])
        self.assertTrue(pv["exportable"])
        self.assertEqual(pv["n_people"], 12)
        self.assertEqual(pv["k"], 10)
        self.assertEqual(pv["state"], "synthetic")
        self.assertEqual(pv["preview"][0], ",".join(publish.CSV_COLUMNS))
        n_cells = sum(len(r["cells"]) for r in pv["doc"]["rows"])
        self.assertEqual(len(pv["preview"]), n_cells + 1)
        self.assertRegex(pv["sha256"], r"^[0-9a-f]{64}$")
        # nothing saved by a preview
        self.assertFalse(self.p.path(f"exports/profile-{RUN_ID}.json").exists())
        self.assertEqual(self.log_kinds(), [])

    def test_save_writes_json_and_csv_and_logs(self):
        pv = export.profile_preview(self.p, RUN_ID, 10)
        out = export.save_profile(self.p, RUN_ID, 10, pv["sha256"])
        js, cs = self.p.path(f"exports/profile-{RUN_ID}.json"), self.p.path(f"exports/profile-{RUN_ID}.csv")
        self.assertEqual(hashlib.sha256(js.read_bytes()).hexdigest(), pv["sha256"])
        self.assertEqual(out["sha256"], pv["sha256"])
        doc = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "orgsci-profile/v2")
        publish.assert_publishable(doc, 10, "orgsci-profile/v2")
        self.assertEqual(doc["run"]["settings"]["gate_k_people"], 10)
        self.assertEqual(doc["population"]["n_people"], 12)
        self.assertEqual(cs.read_text(encoding="utf-8").splitlines(), pv["preview"])
        rec = self.p.log.records("export")[-1]["payload"]
        self.assertEqual((rec["kind"], rec["sha256"], rec["k"], rec["n_people"], rec["run_id"]),
                         ("profile", pv["sha256"], 10, 12, RUN_ID))
        self.assertEqual(rec["sha256_csv"], hashlib.sha256(cs.read_bytes()).hexdigest())

    def test_two_runs_keep_two_profile_files(self):
        """R19: saving the second run's profile must not overwrite the first run's file."""
        second = "A-1b2c3d4e"
        folder = self.p.path("runs/A-" + second)
        shutil.copytree(self.p.path(RUN_FOLDER), folder, symlinks=True)
        cfg_path = folder / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg.update(run_id=second, out_dir=str(folder))
        cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        saved = {}
        for rid in (RUN_ID, second):
            pv = export.profile_preview(self.p, rid, 10)
            self.assertTrue(pv["exportable"], pv["refusals"])
            saved[rid] = export.save_profile(self.p, rid, 10, pv["sha256"])
            self.assertEqual(saved[rid]["path"], f"exports/profile-{rid}.json")
            self.assertEqual(saved[rid]["path_csv"], f"exports/profile-{rid}.csv")
        for rid in (RUN_ID, second):
            self.assertTrue(self.p.path(f"exports/profile-{rid}.json").is_file(), rid)
            self.assertTrue(self.p.path(f"exports/profile-{rid}.csv").is_file(), rid)
            self.assertEqual(_sha_of(self.p.path(f"exports/profile-{rid}.json")), saved[rid]["sha256"])
        self.assertEqual(sorted(x.name for x in self.p.path("exports").glob("profile*")),
                         sorted([f"profile-{RUN_ID}.json", f"profile-{RUN_ID}.csv", f"profile-{second}.json",
                                 f"profile-{second}.csv"]))
        # "Saved from this run" names the file of that run
        paths = {r["payload"]["run_id"]: r["payload"]["path"] for r in self.p.log.records("export")}
        self.assertEqual(paths, {RUN_ID: f"exports/profile-{RUN_ID}.json", second: f"exports/profile-{second}.json"})
        self.assertEqual([o["path"] for o in export.outputs(self.p, second) if o["kind"] == "profile"],
                         [f"exports/profile-{second}.json"])

    def test_k_is_stamped(self):
        pv = export.profile_preview(self.p, RUN_ID, 12)
        self.assertTrue(pv["exportable"], pv["refusals"])
        export.save_profile(self.p, RUN_ID, 12, pv["sha256"])
        doc = json.loads(self.p.path(f"exports/profile-{RUN_ID}.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["run"]["settings"]["gate_k_people"], 12)

    def test_save_needs_the_previewed_sha(self):
        export.profile_preview(self.p, RUN_ID, 10)
        with self.assertRaises(api.ApiError) as cm:
            export.save_profile(self.p, RUN_ID, 10, "0" * 64)
        self.assertEqual(cm.exception.code, "preview_changed")
        self.assertFalse(self.p.path(f"exports/profile-{RUN_ID}.json").exists())

    def test_save_without_preview_refused(self):
        with self.assertRaises(api.ApiError) as cm:
            export.save_profile(self.p, RUN_ID, 10, "0" * 64)
        self.assertEqual(cm.exception.code, "no_preview")

    def test_k_below_floor_refused(self):
        for bad in (9, 0, -1, True, "10", None):
            with self.subTest(k=bad), self.assertRaises(api.ApiError) as cm:
                export.profile_preview(self.p, RUN_ID, bad)
            self.assertEqual(cm.exception.code, "bad_k")

    def test_profile_gate_refusal_surfaces(self):
        secret = "ab" * 32

        def plant(doc):
            _first_cell_with(doc, "name")["note"] = secret
        with mock.patch.object(profile_view, "run_exporter", _planting_exporter(plant)):
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertFalse(pv["exportable"])
        codes = [r["code"] for r in pv["refusals"]]
        self.assertIn("hex_outside_run", codes)
        for r in pv["refusals"]:
            self.assertNotIn(secret, json.dumps(r))
            self.assertTrue(r["message"])
        with self.assertRaises(api.ApiError) as cm:
            export.save_profile(self.p, RUN_ID, 10, pv["sha256"])
        self.assertEqual(cm.exception.code, "not_exportable")
        self.assertNotIn(secret, cm.exception.message)
        self.assertFalse(self.p.path(f"exports/profile-{RUN_ID}.json").exists())

    def test_profile_forbidden_set_passed(self):
        run = export.find_run(self.p, RUN_ID)
        view = export.load_view(self.p, run)
        pid = view["maps"][0]["pid"]
        step = view["maps"][0]["items"][0]["label"]
        quote = next(ev["quote"] for m in view["maps"] for it in m["items"]
                     for ev in (it.get("evidence") or {}).values() if isinstance(ev, dict) and ev.get("quote"))
        forbidden = export.forbidden_strings(self.p, view)
        for s in (pid, step, quote):
            self.assertIn(s, forbidden)
        for planted in (pid, step):
            def plant(doc, planted=planted):
                _first_cell_with(doc, "name")["note"] = planted
            with self.subTest(planted=planted), \
                    mock.patch.object(profile_view, "run_exporter", _planting_exporter(plant)):
                pv = export.profile_preview(self.p, RUN_ID, 10)
                self.assertIn("label_or_quote", [r["code"] for r in pv["refusals"]])
                self.assertNotIn(planted, json.dumps(pv["refusals"]))

    def test_forbidden_includes_project_names(self):
        self.p.data["files"]["inputs/transcripts/MAP-Q.txt"] = {"name": "Kowalski interview.txt",
                                                                "stem": "Kowalski interview", "pid": "MAP-Q",
                                                                "sha256": "0" * 64}
        self.p.save()
        f = export.forbidden_strings(self.p, {"maps": []})
        for s in ("Kowalski interview.txt", "Kowalski interview", "MAP-Q"):
            self.assertIn(s, f)

    def test_group_below_k_not_exportable(self):
        pv = export.profile_preview(self.p, RUN_ID, 13)
        self.assertFalse(pv["exportable"])
        self.assertEqual(pv["refusals"][0]["code"], "group_below_k")
        self.assertEqual(pv["refusals"][0]["message"], strings.t("export.refusal.group_below_k"))
        self.assertEqual(strings.t("export.refusal.group_below_k"), "group below k: not exportable")
        with self.assertRaises(api.ApiError):
            export.save_profile(self.p, RUN_ID, 13, pv["sha256"])

    def _refused_by_exporter(self, reasons):
        def refuse(*a, **kw):
            raise profile_view.ProfileRefused(list(reasons))
        return mock.patch.object(profile_view, "run_exporter", refuse)

    def test_k_floor_refusal_is_one_plain_sentence_on_export(self):
        """R12 follow-up: the preview shows the sentence Results shows, not the exporter's developer text."""
        reason = profile_export.k_floor_reason(4)
        with self._refused_by_exporter([reason]), contextlib.redirect_stdout(io.StringIO()) as log:
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertFalse(pv["exportable"])
        self.assertEqual([r["code"] for r in pv["refusals"]], ["k_floor"])
        msg = pv["refusals"][0]["message"]
        self.assertEqual(msg, "Too few participants were analysed to share a profile: 4, and at least 10 are needed.")
        self.assertEqual(msg, results.refusal_messages([reason])[0]["message"], "one function, one sentence")
        for dev in ("n_analysed", "k floor", "nothing is exported"):
            self.assertNotIn(dev, json.dumps(pv["refusals"]))
        self.assertIn(reason, log.getvalue(), "the exporter's own words go to the terminal log")
        self.assertIn(RUN_ID, log.getvalue())

    def test_privacy_check_refusal_is_one_plain_sentence_on_export(self):
        reasons = ["forbidden key $.rows[0].pid", "64-hex string outside the run block"]
        with self._refused_by_exporter(reasons), contextlib.redirect_stdout(io.StringIO()) as log:
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual([r["code"] for r in pv["refusals"]], ["privacy_check"], "one sentence for the kind")
        self.assertEqual(pv["refusals"][0]["message"], results.refusal_messages(reasons)[0]["message"])
        blob = json.dumps(pv["refusals"])
        for dev in ("forbidden key", "64-hex", "$.rows"):
            self.assertNotIn(dev, blob)
        for reason in reasons:
            self.assertIn(reason, log.getvalue())

    def test_unknown_exporter_reason_never_shows_raw_on_export(self):
        with self._refused_by_exporter(["model info: unknown role"]), contextlib.redirect_stdout(io.StringIO()):
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual([r["code"] for r in pv["refusals"]], ["other"])
        self.assertNotIn("model info", pv["refusals"][0]["message"])

    def test_report_of_a_refused_run_shows_the_plain_sentence(self):
        reasons = [profile_export.k_floor_reason(4), "forbidden key $.rows[0].pid"]
        with self._refused_by_exporter(reasons), contextlib.redirect_stdout(io.StringIO()) as log:
            text = self.report().read_text(encoding="utf-8")
        i = text.index('class="report-refused"')
        block = text[i:text.index("</div>", i)]
        for m in results.refusal_messages(reasons):
            self.assertInPage(m["message"], block)
        for dev in ("n_analysed", "k floor", "forbidden key", "$.rows"):
            self.assertNotIn(dev, text)
        self.assertIn(reasons[0], log.getvalue())

    def test_differencing_warning(self):
        pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual(pv["warnings"], [])
        export.save_profile(self.p, RUN_ID, 10, pv["sha256"])
        # the same people again: no warning
        self.assertEqual(export.profile_preview(self.p, RUN_ID, 10)["warnings"], [])
        # an earlier logged export on 9 people: 12 - 9 = 3 < k
        self.p.log.append("export", {"kind": "profile", "sha256": "1" * 64, "k": 10, "n_people": 9,
                                     "run_id": "A-11111111"})
        pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual([w["code"] for w in pv["warnings"]], ["differencing"])
        self.assertTrue(pv["exportable"])
        self.assertIn("3", pv["warnings"][0]["message"])
        # a warning is shown, not a refusal: saving still works and is logged
        export.save_profile(self.p, RUN_ID, 10, pv["sha256"])
        self.assertEqual(self.log_kinds(), ["profile", "profile", "profile"])

    def test_own_data_state_is_pending(self):
        def own(doc):
            doc["state"] = "pending"
        with mock.patch.object(profile_view, "run_exporter", _planting_exporter(own)):
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertEqual(pv["state"], "pending")
        self.assertTrue(pv["exportable"])

    def test_reported_state_never_exported(self):
        def reported(doc):
            doc["state"] = "reported"
        with mock.patch.object(profile_view, "run_exporter", _planting_exporter(reported)):
            pv = export.profile_preview(self.p, RUN_ID, 10)
        self.assertFalse(pv["exportable"])


# ----------------------------------------------------------------------------------------------- methods
class TestMethods(ExportCase):
    def test_methods_no_banned_phrases(self):
        text = export.export_methods(self.p, RUN_ID)
        self.assertTrue(text.startswith(strings.t("methods.header")))
        self.assertEqual(strings.t("methods.header"), "Draft for you to edit")
        self.assertEqual(find_hits(text), [])
        self.assertNotRegex(text.lower(), r"\bpool")
        for need in ("orgsci-profile/v2", "side by side", "one reference type and one prompt set",
                     "never a meta-analysis", "k = 10", "placebo", "oracle"):
            self.assertIn(need, text)
        cert_like = export.run_record(self.p, export.find_run(self.p, RUN_ID))
        r0 = cert_like["runs"][0]
        for h in (r0["code_sha256"], r0["config_sha256"], r0["prompt_set_sha256"], r0["verify"]["head_hash"]):
            self.assertIn(h, text)

    def test_methods_k_follows_last_profile_export(self):
        pv = export.profile_preview(self.p, RUN_ID, 11)
        export.save_profile(self.p, RUN_ID, 11, pv["sha256"])
        self.assertIn("k = 11", export.export_methods(self.p, RUN_ID))

    def test_save_methods_writes_and_logs(self):
        out = export.save_methods(self.p, RUN_ID)
        path = self.p.path(out["path"])
        self.assertEqual(path.read_text(encoding="utf-8"), export.export_methods(self.p, RUN_ID))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), out["sha256"])
        self.assertEqual(self.log_kinds(), ["methods"])

    def test_mode_b_wording_in_report_and_methods(self):
        run = export.find_run(self.p, RUN_ID)
        view = dict(export.load_view(self.p, run))
        view["reference_type"] = "B"
        prof = export.stage_profile(self.p, run)[0]
        note = strings.t("strip.mode_b_note")
        with mock.patch.object(export, "load_view", return_value=view), \
                mock.patch.object(export, "stage_profile", return_value=(prof, "0" * 64)):
            text = self.report().read_text(encoding="utf-8")
            methods = export.export_methods(self.p, RUN_ID)
        for doc in (text, methods):
            self.assertInPage(note, doc)
            self.assertIn("researcher-marked absences", doc)
        self.assertInPage(strings.t("export.reference.b"), text)
        self.assertIn("(reference type B)", methods)
        self.assertNotIn(strings.t("export.reference.a"), methods)
        self.assertEqual(find_hits(methods), [])


# ----------------------------------------------------------------------------------------------- run record
class TestRunRecord(ExportCase):
    def test_save_certificate_writes_and_logs(self):
        out = export.save_certificate(self.p, RUN_ID)
        js, md = self.p.path(out["path"]), self.p.path(out["path_md"])
        self.assertEqual(js.name, f"run-record-{RUN_ID}.json")
        self.assertEqual(md.name, f"run-record-{RUN_ID}.md")
        self.assertTrue(md.is_file())
        self.assertEqual(sorted(x.name for x in self.p.path("exports").glob("certificate*")), [], "no file is named certificate")
        self.assertEqual(hashlib.sha256(js.read_bytes()).hexdigest(), out["sha256"])
        cert = json.loads(js.read_text(encoding="utf-8"))
        self.assertTrue(cert["all_logs_verify"])
        self.assertIsNotNone(cert["app_log"])
        self.assertEqual(self.log_kinds(), ["certificate"])


# ----------------------------------------------------------------------------------------------- routes
class TestRoutes(ExportCase):
    def test_every_export_logged_and_app_log_verifies(self):
        call(self.p, "POST", "/api/export/report", {"run_id": RUN_ID})
        pv = call(self.p, "POST", "/api/export/profile/preview", {"run_id": RUN_ID, "k": 10})
        self.assertNotIn("doc", pv)
        call(self.p, "POST", "/api/export/profile/save", {"run_id": RUN_ID, "k": 10, "sha256": pv["sha256"]})
        call(self.p, "POST", "/api/export/methods/save", {"run_id": RUN_ID})
        call(self.p, "POST", "/api/export/certificate/save", {"run_id": RUN_ID})
        self.assertEqual(self.log_kinds(), ["report", "profile", "methods", "certificate"])
        for r in self.p.log.records("export"):
            self.assertRegex(r["payload"]["sha256"], r"^[0-9a-f]{64}$")
        ok, info = self.p.log.verify()
        self.assertTrue(ok, info)
        listing = call(self.p, "GET", "/api/export/list", {}, {"run_id": RUN_ID})
        self.assertEqual([o["kind"] for o in listing["outputs"]], ["report", "profile", "methods", "certificate"])
        self.assertTrue(all(o["saved"] for o in listing["outputs"]))

    def test_routes_refused_while_b_coding_open(self):
        self.p.data["references"] = {"SYN-OW01": {"type": "B"}}
        self.p.save()
        for path, body in (("/api/export/report", {"run_id": RUN_ID}),
                           ("/api/export/profile/preview", {"run_id": RUN_ID, "k": 10}),
                           ("/api/export/profile/save", {"run_id": RUN_ID, "k": 10, "sha256": "0" * 64}),
                           ("/api/export/methods", {"run_id": RUN_ID}),
                           ("/api/export/methods/save", {"run_id": RUN_ID}),
                           ("/api/export/certificate/save", {"run_id": RUN_ID})):
            with self.subTest(path=path), self.assertRaises(api.ApiError) as cm:
                call(self.p, "POST", path, body)
            self.assertEqual(cm.exception.code, "coding_unlocked")
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "GET", "/api/export/list", {}, {"run_id": RUN_ID})
        self.assertEqual(cm.exception.code, "coding_unlocked")
        self.assertEqual(self.p.log.records("reveal"), [])
        self.assertFalse(self.p.path(f"exports/report-{RUN_ID}").exists())
        # the run list itself is not a result: it answers, and says the gate is closed
        runs = call(self.p, "GET", "/api/export/runs")
        self.assertFalse(runs["gate"]["open"])
        self.assertEqual(self.p.log.records("reveal"), [])

    def test_reveal_logged_once_on_first_export(self):
        call(self.p, "POST", "/api/export/methods", {"run_id": RUN_ID})
        call(self.p, "POST", "/api/export/methods", {"run_id": RUN_ID})
        self.assertEqual(len(self.p.log.records("reveal")), 1)

    def test_methods_route_returns_text(self):
        out = call(self.p, "POST", "/api/export/methods", {"run_id": RUN_ID})
        self.assertEqual(out["text"], export.export_methods(self.p, RUN_ID))

    def test_report_route_returns_relative_path(self):
        out = call(self.p, "POST", "/api/export/report", {"run_id": RUN_ID})
        self.assertEqual(out["path"], f"exports/report-{RUN_ID}/index.html")
        self.assertRegex(out["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(out["folder"].endswith(f"report-{RUN_ID}"))

    def test_profile_refusal_through_route(self):
        out = call(self.p, "POST", "/api/export/profile/preview", {"run_id": RUN_ID, "k": 13})
        self.assertFalse(out["exportable"])
        with self.assertRaises(api.ApiError) as cm:
            call(self.p, "POST", "/api/export/profile/save", {"run_id": RUN_ID, "k": 13, "sha256": out["sha256"]})
        self.assertEqual((cm.exception.status, cm.exception.code), (422, "not_exportable"))


if __name__ == "__main__":
    unittest.main()
