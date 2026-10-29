"""Privacy ruling F (analysis, 19 Oct): a participant's file name never leaves the project's ``private/`` folder.

A transcript and a map named after a person (the name is built at run time, so no tracked file holds it) go through
the real HTTP routes: context, import (and a re-import), pairing, labels, maps, references, a mode-A run on the mock
models on 127.0.0.1 (``tests/mock_models``; no real model is contacted), the report and the methods text. Then every
file under the project folder outside ``private/``, and every file the run left in the temp folder, is read as bytes:
the name, its first name and its last name appear nowhere, in any case, plain, URL-encoded or JSON-escaped, and no
file or folder is named after them. ``private/`` keeps them (the file-name map is not lost). Eleven more people of
the synthetic corpus come in under code names, so the profile for comparison (k = 10) is saved too.

The project sits under a home-like folder (also built at run time). What leaves the machine (the report folder with
its run record and per-run CSS, the methods text, the run record, the profile for comparison) names no folder of
this machine: not the project's, not the one above it, not the user's home, not the kit's. Run configs and logs
inside the project may keep absolute paths: they never leave the machine.

A second test opens a project made before this change and checks that its ``second-scribe-project.json`` gives the
names up to ``private/``.
"""
import base64
import gzip
import io
import json
import os
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path

from second_scribe import pipeline
from second_scribe.app import export, importer, jobs, models
from second_scribe.app.project import PROJECT_FILE, Project
from tests import test_server_security as tss
from tests.mock_models import MockModels
from tests.realpath_corpus import DEMO_CONFIG
from tests.test_importer import MERMAID_RED, WHOLE_WORD
from tests.test_jobs import JUDGE_ID, RECON_ID, role_entry
from tests.test_orchestrate import CONTEXT

CORPUS = Path(__file__).resolve().parent.parent / "demo" / "corpus"
FIRST, LAST = "Mar" + "ia", "Sil" + "va"          # built here: no tracked file names this person
PERSON = f"{FIRST} {LAST}"
STEM = f"{PERSON} interview"
CODE = "CASE-OF02"                                 # a code-like name stays the participant id
# the other eleven people of the synthetic corpus, under code-like names (the profile for comparison needs ten)
OTHERS = {f"CASE-{src[4:]}": src
          for src in ["SYN-OW%02d" % i for i in range(2, 8)] + ["SYN-OF%02d" % i for i in range(1, 6)]}
HOME_SEGMENT = "Jo" + "ana" + "Per" + "eira" + "-home"   # a home-like folder above the project, built here too


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def forms(word: str) -> set[bytes]:
    """The byte forms a string may take in a file: plain, URL-encoded (both ways), JSON-escaped (both ways), UTF-16;
    all lower-cased (the scan is case-insensitive)."""
    out = set()
    for w in (word, word.lower()):
        for s in (w, urllib.parse.quote(w), urllib.parse.quote_plus(w), json.dumps(w)[1:-1],
                  json.dumps(w, ensure_ascii=True)[1:-1], w.replace(" ", "_"), w.replace(" ", "-")):
            out.add(s.lower().encode("utf-8"))
            out.add(s.lower().encode("utf-16-le"))
    return out


NEEDLES = {w: forms(w) for w in (PERSON, STEM, FIRST, LAST)}


def contents(path: Path) -> list[bytes]:
    """The file's bytes and, for a zip or gzip file (a .docx, an export), the bytes of each member."""
    data = path.read_bytes()
    out = [data]
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out += [z.read(n) for n in z.namelist()] + [n.encode("utf-8") for n in z.namelist()]
        except zipfile.BadZipFile:
            pass
    if data[:2] == b"\x1f\x8b":
        try:
            out.append(gzip.decompress(data))
        except OSError:
            pass
    return out


def hits(folder: Path, *, skip: Path | None = None, since: float | None = None) -> list[str]:
    """``<path>: <word>`` for every file (or file / folder name) under ``folder`` that holds a form of the name."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        if skip is not None and (here == skip or skip in here.parents):
            dirnames[:] = []
            continue
        for n in dirnames + filenames:
            for word, needles in NEEDLES.items():
                if any(x in n.lower().encode("utf-8") for x in needles):
                    found.append(f"{here / n}: name holds {word!r}")
        for n in filenames:
            p = here / n
            if since is not None and p.stat().st_mtime < since:
                continue
            for blob in contents(p):
                low = blob.lower()
                for word, needles in NEEDLES.items():
                    if any(x in low for x in needles):
                        found.append(f"{p}: holds {word!r}")
    return sorted(set(found))


def passed_model_check(project) -> None:
    """A passed Setup model check of the mock pair, as the kit's canary reads it (no model is called here)."""
    rel = f"{jobs.check_rel(project)}/model_check.json"
    f = project.path(rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "model-check/1", "passed": True, "models": {"recon": RECON_ID, "judge": JUDGE_ID},
           "digests": models.tool_fields(project)["model_digests"], "prompt_set_sha256": pipeline.prompt_set_sha256()}
    f.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    project.data["model_check"] = {"pair_sha256": models.pair_sha256(project), "path": rel,
                                   "sha256": jobs.sha256_file(f), "passed": True, "failures": [], "models": {}}
    project.save()


def folder_needles(root: Path) -> dict:
    """The byte forms of the folders a leak would name: the project folder (as given and resolved), the folder above
    it, its distinctive segment, the user's home folder and the kit's own folder."""
    words = {"project folder": str(root), "project folder (resolved)": str(root.resolve()),
             "home-like folder": str(root.parent), "home-like segment": HOME_SEGMENT,
             "user's home": str(Path.home()) + "/", "kit folder": str(Path(__file__).resolve().parent.parent)}
    out = {}
    for label, w in words.items():
        fs = set()
        for v in (w, w.lower()):
            for f in (v, json.dumps(v)[1:-1], v.replace("/", "\\/"), urllib.parse.quote(v),
                      urllib.parse.quote(v, safe=""), v.replace("/", "\\")):
                fs.add(f.lower().encode("utf-8"))
        out[label] = fs
    return out


def path_hits(paths: list, needles: dict) -> list[str]:
    found = []
    for top in paths:
        files = [top] if top.is_file() else [p for p in top.rglob("*") if p.is_file()]
        for p in files:
            for blob in contents(p):
                low = blob.lower()
                for label, fs in needles.items():
                    if any(x in low for x in fs):
                        found.append(f"{p}: holds the {label}")
    return sorted(set(found))


class TestNamesStayPrivate(tss.ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockModels([DEMO_CONFIG])
        cls.url = cls.mock.start()
        cls.addClassCleanup(cls.mock.stop)

    def setUp(self):
        super().setUp()
        self.t0 = time.time() - 1
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-ff-")
        self.addCleanup(self._tmp.cleanup)
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)
        self.addCleanup(self._stop_run)

    def _stop_run(self):
        live = self.state.jobs.get("run")
        if live is not None:
            live.stop()

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        return status, json.loads(data)

    def ok(self, path, obj):
        status, doc = self.send(path, obj)
        self.assertEqual(status, 200, doc)
        return doc

    def ok_get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        doc = json.loads(data)
        self.assertEqual(status, 200, doc)
        return doc

    def upload(self, kind, name, data):
        return self.ok("/api/import", {"kind": kind, "name": name, "data_b64": b64(data)})

    def test_no_name_outside_private(self):
        root = Path(self._tmp.name).resolve() / HOME_SEGMENT / "study"
        root.parent.mkdir()
        self.ok("/api/project/create", {"path": str(root), "name": "Study"})
        project = self.state.project
        self.ok("/api/context", CONTEXT)
        tr = (CORPUS / "transcripts" / "SYN-OW01.docx").read_bytes()
        mp = (CORPUS / "maps" / "SYN-OW01.vsdx").read_bytes()
        first = self.upload("transcript", f"{STEM}.docx", tr)
        pid = first["file"]["pid"]
        self.assertRegex(pid, r"^ID-\d{2,}$")                     # a person's name is not made into an id
        self.assertEqual(first["file"]["name"], f"{STEM}.docx")    # the screen still shows the file as named
        again = self.upload("transcript", f"{STEM}.docx", tr)      # a re-import replaces the copy
        self.assertEqual((again["file"]["pid"], again["replaced"]), (pid, f"{STEM}.docx"))
        self.upload("map", f"{STEM}.vsdx", mp)
        for code, src in OTHERS.items():
            self.upload("transcript", f"{code}.docx", (CORPUS / "transcripts" / f"{src}.docx").read_bytes())
            self.upload("map", f"{code}.vsdx", (CORPUS / "maps" / f"{src}.vsdx").read_bytes())
        table = self.ok_get("/api/pairing")
        rows = {r["pid"]: r for r in table["rows"]}
        self.assertEqual(sorted(rows), sorted([pid, *OTHERS]))
        self.assertEqual((rows[pid]["transcript"], rows[pid]["map"], rows[pid]["paired"]),
                         (f"{STEM}.docx", f"{STEM}.vsdx", True))
        self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        roles = {pid: "owner", **{c: "owner" if "OW" in c else "officer" for c in OTHERS}}
        for p in roles:
            self.ok("/api/labels/confirm", {"pid": p, "participant": "PT", "interviewer": "IV", "role": roles[p]})
            preview = self.ok_get(f"/api/maps/preview?pid={p}")
            self.assertEqual(self.ok("/api/maps/confirm", {"pid": p, "sha256": preview["sha256"]})["confirmed"], True)
            self.ok("/api/references", {"pid": p, "type": "A"})
        # a change after import: the screen names the file, the app log does not
        copy = project.path(project.data["pairing"]["pids"][pid]["transcript"])
        saved = copy.read_bytes()
        copy.write_bytes(saved + b"x")
        status, doc = self.send("/api/labels/confirm", {"pid": pid, "participant": "PT", "interviewer": "IV",
                                                         "role": "owner"})
        self.assertEqual((status, doc["error"]), (409, "file_changed"))
        self.assertIn(f"{STEM}.docx", doc["message"])
        copy.write_bytes(saved)
        # the models (the mock on 127.0.0.1) and a passed model check, then a mode-A run through the run route
        models.set_role(project, "recon", role_entry(self.url, RECON_ID, "qwen"))
        models.set_role(project, "judge", role_entry(self.url, JUDGE_ID, "gemma"))
        models.confirm_families(project, {"recon": "qwen", "judge": "gemma"})
        project.save()
        passed_model_check(project)
        job = self.ok("/api/runs/start", {"group": "A", "rungs": ["rung2"], "preset": "standard"})["job"]
        run_id = job["run_id"]
        config = json.loads(project.path(f"runs/{run_id}/config.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(config["corpus"]["pairs"]), sorted([pid, *OTHERS]))
        t_end = time.time() + 240
        while job["state"] not in ("done", "failed"):
            self.assertLess(time.time(), t_end, job)
            time.sleep(0.1)
            job = self.ok_get(f"/api/runs/status?run={run_id}")["job"]
            if job["state"] == "needs_swap":           # one mock server holds both: the user's manual "continue"
                job = self.ok("/api/runs/swap", {"run": run_id})["job"]
        audits = sorted(project.path(f"runs/{run_id}/audits").glob("*.json")) \
            if project.path(f"runs/{run_id}/audits").is_dir() else []
        self.assertEqual(job["state"], "done", [job] + [a.read_text(encoding="utf-8")[:1500] for a in audits])
        export.export_report(project, run_id)
        export.save_methods(project, run_id)
        self.ok("/api/export/certificate/save", {"run_id": run_id})
        pv = self.ok("/api/export/profile/preview", {"run_id": run_id, "k": export.K_FLOOR})
        self.assertTrue(pv["exportable"], pv)
        self.ok("/api/export/profile/save", {"run_id": run_id, "k": export.K_FLOOR, "sha256": pv["sha256"]})
        self.assertTrue(project.path(export.profile_rel(run_id)[0]).is_file())
        # what leaves the machine (exports/: report, methods, record, profile; the run's profile for comparison)
        # names no folder of this machine: not the project's, not the home-like folder above it, not the user's home
        outgoing = [project.path("exports")] + sorted(project.path("views").glob("*.profile.json"))
        self.assertTrue(list(project.path("exports").glob("report-*/run-record.json")))
        self.assertTrue(list(project.path("exports").glob("methods-*.txt")))
        self.assertTrue(list(project.path("exports").glob("run-record-*.md")))
        self.assertEqual(path_hits(outgoing, folder_needles(root)), [])
        # the scan sees a path where one is: the run's config, which stays on this machine, holds the project folder
        self.assertTrue(path_hits([project.path(f"runs/{run_id}/config.json")], folder_needles(root)))
        # every file outside private/ (the project's own and the run's temp files): no form of the name
        private = project.path("private")
        self.assertEqual(hits(root, skip=private), [])
        self.assertEqual(hits(Path(tempfile.gettempdir()).resolve(), skip=private.resolve(), since=self.t0), [])
        # private/ keeps the names, keyed by participant id
        self.assertTrue(hits(private))
        names = json.loads(project.path(importer.NAMES).read_text(encoding="utf-8"))
        self.assertEqual(names["pids"][pid], {"stem": STEM, "transcript": f"{STEM}.docx", "map": f"{STEM}.vsdx"})
        self.assertEqual(names["pids"][CODE]["transcript"], f"{CODE}.docx")
        idmap = json.loads(project.path(importer.PID_MAP).read_text(encoding="utf-8"))
        self.assertEqual(idmap["map"][pid], {"file_stem": STEM, "file_name": f"{STEM}.docx"})
        # the Profile's forbidden strings still know the names (they are read from private/, in memory only)
        forbidden = export.forbidden_strings(project, {"maps": []})
        self.assertIn(f"{STEM}.docx", forbidden)
        self.assertIn(STEM, forbidden)
        # the copies are stored under the participant id
        self.assertEqual(sorted(project.data["files"]),
                         sorted([f"inputs/{k}s/{p}.{x}" for p in (pid, *OTHERS) for k, x in (("map", "vsdx"),
                                                                                             ("transcript", "docx"))]))


class TestOldProjectGivesNamesUp(unittest.TestCase):
    """A project made before the ruling kept ``name`` / ``stem`` and the file name in each ``files`` key; opening it
    moves them to ``private/file_names.json`` and stores the copies under the participant id. The app log is chained
    and is left as it is."""

    def test_open_migrates_project_file(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-ff-") as tmp:
            root = Path(tmp).resolve() / "old"
            p = Project.create(root, "Study")
            importer.set_context(p, CONTEXT)
            importer.import_file(p, "transcript", "OLD-01.txt", b64("\n".join(WHOLE_WORD).encode("utf-8")))
            importer.import_file(p, "map", "OLD-01.mmd", b64(MERMAID_RED.encode("utf-8")))
            importer.confirm_pairing(p, importer.pairing(p)["table_sha256"])
            importer.confirm_labels(p, {"pid": "OLD-01", "participant": "Participant", "interviewer": "Interviewer",
                                        "role": "owner"})
            # rewrite it the way the importer stored it before: the file name in the key and in the entry
            old = {"transcript": f"{STEM}.txt", "map": f"{STEM}.mmd"}
            files, pairing = {}, p.data["pairing"]["pids"]["OLD-01"]
            for rel, meta in list(p.data["files"].items()):
                kind = meta["kind"]
                new_rel = f"{importer.FOLDERS[kind]}/{old[kind]}"
                os.replace(p.path(rel), p.root / new_rel)
                files[new_rel] = {**meta, "name": old[kind], "stem": STEM}
                pairing[kind] = new_rel
            p.data["files"] = files
            p.data["pairing"]["table_sha256"] = importer._table_sha(importer._table(p))
            p.save()
            p.path(importer.NAMES).unlink()
            stale = p.root / "inputs" / "maps" / f".import-0123-{STEM}.mmd"     # a crash left a staged copy
            stale.write_text("x", encoding="utf-8")
            self.assertIn(STEM, (root / PROJECT_FILE).read_text(encoding="utf-8"))
            log_before = (root / "logs" / "app.jsonl").read_bytes()

            q = Project.open(root)
            text = (root / PROJECT_FILE).read_text(encoding="utf-8")
            for word in (STEM, FIRST, LAST):
                self.assertNotIn(word.lower(), text.lower())
            self.assertEqual(sorted(q.data["files"]), ["inputs/maps/OLD-01.mmd", "inputs/transcripts/OLD-01.txt"])
            self.assertTrue(all("name" not in m and "stem" not in m for m in q.data["files"].values()))
            self.assertEqual(q.data["pairing"]["pids"]["OLD-01"],
                             {"transcript": "inputs/transcripts/OLD-01.txt", "map": "inputs/maps/OLD-01.mmd"})
            self.assertEqual(q.changed_files(), [])
            self.assertTrue(importer.pairing_current(q))               # the confirmations hold
            self.assertTrue(importer.labels_confirmed(q, "OLD-01"))
            self.assertFalse(stale.exists())
            names = json.loads(q.path(importer.NAMES).read_text(encoding="utf-8"))
            self.assertEqual(names["pids"]["OLD-01"], {"stem": STEM, **old})
            self.assertEqual(importer.pairing(q)["rows"][0]["transcript"], f"{STEM}.txt")   # still shown
            self.assertEqual((root / "logs" / "app.jsonl").read_bytes(), log_before)       # the log is not rewritten
            self.assertTrue(q.log.verify()[0])
            # opening again changes nothing
            Project.open(root)
            self.assertEqual((root / PROJECT_FILE).read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()
