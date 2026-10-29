"""Task 33: import, speaker labels, missing rule and parsed-map confirmation (spec section 6.5 steps 2-5, section 4
pairing, section 3.1 "Speaker labels (F5)", section 7; Review Focus 1-2; plan V12).

The model (``second_scribe.app.importer``) is driven through the real routes (``routes/importer.py``) on a server
on 127.0.0.1, with the session and CSRF of Task 31. Every reply is kept, so the tests can show that nothing private
(the file-name <-> pid map) ever reaches the browser.
"""
import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe.app import importer, strings
from second_scribe.app.project import Project
from second_scribe.audit.verify import verify_run
from second_scribe.maps import readers
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MissingRule, read_map, rule_for
from second_scribe.maps.vsdx import extract_vsdx
from second_scribe.maps.vsdx_rule import apply_vsdx_rule
from second_scribe.schemas import MapGraph
from tests import test_server_security as tss

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "maps"
VSDX_INHERIT = FIXTURES / "vsdx-rule-inherit.vsdx"        # red set by a master shape and a stylesheet
VSDX_NO_RED = FIXTURES / "vsdx-rule-dashed.vsdx"          # dashed lines, no red at all: zero missing by default

WHOLE_WORD = [
    "Interviewer: Where did the process start for you?",
    "Participant 3 (00:04:12): I went to the town office to ask about the permit.",
    "Interviewer: What happened next?",
    "Participant 3 (00:05:40): Nobody told me about the fire inspection, I wished someone had.",
    "Interviewer: And then?",
    "Participant 3 (00:07:02): Then the letter came and I paid the fee.",
]
NO_LABELS = [
    "Where did the process start for you?",
    "I went to the town office to ask about the permit.",
    "What happened next?",
    "Nobody told me about the fire inspection.",
]
MERMAID_RED = "flowchart LR\n  a[Ask at the office] --> b[Get the permit]\n  b --> c[Fire inspection]:::missing\n"
MERMAID_PLAIN = "flowchart LR\n  a[Ask at the office] --> b[Get the permit]\n  b --> c[Pay the fee]\n"
CONTEXT = {"domain": "Getting a building permit in a small town.",
           "phases": ["Planning", "Permits", "Building"],
           "roles": {"owner": "A person who owns the building site.", "builder": "A contractor hired to build."}}


def b64(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def mapgraph_with_x(xs, phases=None) -> str:
    """A ``.mapgraph.json`` reference whose nodes sit at ``xs`` with no phase (or the given ones)."""
    phases = phases or [None] * len(xs)
    nodes = [{"node_id": f"n{i}", "text": f"Step {i}", "phase": ph, "status": "experienced", "x": x}
             for i, (x, ph) in enumerate(zip(xs, phases))]
    return json.dumps({"map_id": "x", "origin": "endorsed", "nodes": nodes, "edges": []})


class ImportCase(tss.ServerCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)
        self.replies = []
        status, doc = self.send("/api/project/create", {"path": str(self.tmp / "study"), "name": "Study"})
        self.assertEqual(status, 200, doc)
        self.project = self.state.project

    # -- HTTP -------------------------------------------------------------------------------------------------
    def get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        self.replies.append(data.decode("utf-8"))
        return status, json.loads(data)

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        self.replies.append(data.decode("utf-8"))
        return status, json.loads(data)

    def ok(self, path, obj):
        status, doc = self.send(path, obj)
        self.assertEqual(status, 200, doc)
        return doc

    def ok_get(self, path):
        status, doc = self.get(path)
        self.assertEqual(status, 200, doc)
        return doc

    def refused(self, path, obj, status, code):
        got, doc = self.send(path, obj)
        self.assertEqual((got, doc.get("error")), (status, code), doc)
        self.assertEqual(set(doc), {"error", "message"})
        self.assertFalse(re.search(r"Traceback|Error\(", doc["message"]))
        return doc

    # -- steps ------------------------------------------------------------------------------------------------
    def context(self):
        return self.ok("/api/context", CONTEXT)

    def upload(self, kind, name, data):
        return self.ok("/api/import", {"kind": kind, "name": name, "data_b64": b64(data)})

    def pair(self):
        table = self.ok_get("/api/pairing")
        return self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})

    def mermaid_pair(self, stem="MAP-A1", text=MERMAID_RED):
        self.upload("transcript", f"{stem}.txt", "\n".join(WHOLE_WORD))
        self.upload("map", f"{stem}.mmd", text)


class TestImport(ImportCase):
    def test_pdf_and_png_refused_with_reason(self):
        for name in ("MAP-A1.pdf", "MAP-A1.PNG", "photo.jpeg", "scan.heic", "drawing.svg"):
            for kind in ("map", "transcript"):
                with self.subTest(name=name, kind=kind):
                    doc = self.refused("/api/import", {"kind": kind, "name": name, "data_b64": b64(b"%PDF-1.4 x")},
                                       422, "refused_format")
                    self.assertEqual(doc["message"], strings.t("importer.error.refused_format"))
                    self.assertIn("contaminate the comparison", doc["message"])
        for kind, name in (("map", "MAP-A1.txt"), ("transcript", "MAP-A1.vsdx"), ("transcript", "MAP-A1.rtf"),
                           ("map", "MAP-A1")):
            with self.subTest(name=name, kind=kind):
                self.refused("/api/import", {"kind": kind, "name": name, "data_b64": b64(b"x")},
                             422, "unsupported_format")
        self.assertEqual(self.project.data["files"], {})
        self.assertEqual(list((self.project.root / "inputs" / "maps").iterdir()), [])
        self.assertEqual(self.project.log.records("import"), [])

    def test_size_limit_413(self):
        with mock.patch.object(importer, "MAX_TRANSCRIPT_BYTES", 64), mock.patch.object(importer, "MAX_MAP_BYTES", 80):
            self.refused("/api/import", {"kind": "transcript", "name": "MAP-A1.txt", "data_b64": b64("x" * 65)},
                         413, "too_large")
            self.refused("/api/import", {"kind": "map", "name": "MAP-A1.mmd", "data_b64": b64("x" * 81)},
                         413, "too_large")
            # the encoded length alone is enough to refuse, before anything is decoded (this is not even base64)
            self.refused("/api/import", {"kind": "map", "name": "MAP-A1.mmd", "data_b64": "*" * 4000},
                         413, "too_large")
            self.upload("transcript", "MAP-A1.txt", "Interviewer: hello there\nParticipant: yes")
        self.assertEqual(importer.MAX_TRANSCRIPT_BYTES, 20 * 1024 * 1024)
        self.assertEqual(importer.MAX_MAP_BYTES, 50 * 1024 * 1024)
        self.assertEqual(sorted(self.project.data["files"]), ["inputs/transcripts/MAP-A1.txt"])

    def test_bad_requests_refused(self):
        cases = [
            ({"kind": "audio", "name": "MAP-A1.txt", "data_b64": b64("x")}, "bad_kind"),
            ({"kind": "transcript", "name": "../MAP-A1.txt", "data_b64": b64("x")}, "bad_file_name"),
            ({"kind": "transcript", "name": "a/b.txt", "data_b64": b64("x")}, "bad_file_name"),
            ({"kind": "transcript", "name": ".hidden.txt", "data_b64": b64("x")}, "bad_file_name"),
            ({"kind": "transcript", "name": "x\ny.txt", "data_b64": b64("x")}, "bad_file_name"),
            ({"kind": "transcript", "name": "MAP-A1.txt", "data_b64": "not base64!"}, "bad_data"),
            ({"kind": "transcript", "name": "MAP-A1.txt", "data_b64": ""}, "empty_file"),
            ({"kind": "transcript", "name": "MAP-A1.txt", "data_b64": b64("caf\xe9".encode("latin-1"))},
             "unreadable_transcript"),
            ({"kind": "transcript", "name": "MAP-A1.docx", "data_b64": b64(b"not a zip")}, "unreadable_transcript"),
            ({"kind": "map", "name": "MAP-A1.mmd", "data_b64": b64("flowchart LR\n a@{ shape: rect }\n")},
             "map_refused"),
        ]
        for body, code in cases:
            with self.subTest(code=code, name=body["name"]):
                self.refused("/api/import", body, 422, code)
        self.assertEqual(self.project.data["files"], {})
        leftovers = [p.name for d in ("inputs/transcripts", "inputs/maps") for p in (self.project.root / d).iterdir()]
        self.assertEqual(leftovers, [])

    def test_routes_need_a_usable_project(self):
        self.state.project = None
        for path, body in (("/api/import", {"kind": "map", "name": "a.mmd", "data_b64": b64("x")}),
                           ("/api/context", CONTEXT), ("/api/pairing/confirm", {}),
                           ("/api/labels/confirm", {}), ("/api/missing-rule", {}), ("/api/maps/confirm", {}),
                           ("/api/maps/phases", {})):
            with self.subTest(path=path):
                self.refused(path, body, 409, "no_project")
        for path in ("/api/pairing", "/api/labels?pid=x", "/api/maps/preview?pid=x", "/api/missing-rule",
                     "/api/context"):
            with self.subTest(path=path):
                status, doc = self.get(path)
                self.assertEqual((status, doc["error"]), (409, "no_project"))

    def test_import_copies_hashes_and_logs(self):
        doc = self.upload("transcript", "MAP-A1.txt", "Interviewer: hi\nParticipant: hello")
        f = doc["file"]
        self.assertEqual((f["kind"], f["name"], f["rel"], f["pid"]), ("transcript", "MAP-A1.txt",
                                                                      "inputs/transcripts/MAP-A1.txt", "MAP-A1"))
        on_disk = self.project.root / "inputs" / "transcripts" / "MAP-A1.txt"
        self.assertEqual(on_disk.read_text(encoding="utf-8"), "Interviewer: hi\nParticipant: hello")
        self.assertEqual(self.project.data["files"]["inputs/transcripts/MAP-A1.txt"]["sha256"], f["sha256"])
        recs = self.project.log.records("import")
        self.assertEqual([r["payload"] for r in recs],
                         [{"kind": "transcript", "pid": "MAP-A1", "sha256": f["sha256"], "bytes": f["bytes"],
                           "replaced": None}])                # the pid, never the file's name (privacy ruling F)
        self.assertNotIn("Participant: hello", json.dumps(doc))       # the reply names the file, never its words

    def test_reimport_replaces_and_clears_change(self):
        self.upload("transcript", "MAP-A1.txt", "Interviewer: hi\nParticipant: hello")
        (self.project.root / "inputs" / "transcripts" / "MAP-A1.txt").write_text("edited", encoding="utf-8")
        self.assertEqual(self.project.changed_files(), ["inputs/transcripts/MAP-A1.txt"])
        self.upload("transcript", "MAP-A1.txt", "Interviewer: hi again\nParticipant: hello")
        self.assertEqual(self.project.changed_files(), [])
        # the same stem in the other transcript format replaces the earlier file
        doc = self.upload("transcript", "MAP-A1.docx", _docx(["Interviewer: hi", "Participant: hello"]))
        self.assertEqual(doc["replaced"], "MAP-A1.txt")
        self.assertEqual(sorted(self.project.data["files"]), ["inputs/transcripts/MAP-A1.docx"])
        self.assertFalse((self.project.root / "inputs" / "transcripts" / "MAP-A1.txt").exists())


class TestPids(ImportCase):
    def test_pid_sanitised_and_collision(self):
        self.assertEqual(importer.pid_from_stem("MAP-A1"), "MAP-A1")
        self.assertEqual(importer.pid_from_stem("site visit (2)"), "site_visit_2_")
        self.assertEqual(importer.pid_from_stem("café-ünïcode"), "caf_-_n_code")
        self.assertEqual(importer.pid_from_stem("a__b"), "a_b")              # the kit splits item names on "__"
        self.assertEqual(importer.pid_from_stem("x" * 100), "x" * 64)
        for stem in ("MAP-A1", "a b c", "ok_1-2", "é" * 80):
            self.assertRegex(importer.pid_from_stem(stem), r"^[A-Za-z0-9_-]{1,64}$")
            self.assertNotIn("__", importer.pid_from_stem(stem))
        # a stem written as a code is the id; any other stem (it may be a person's name) gets a neutral id
        for stem in ("MAP-A1", "CASE-01", "SYN_OW03", "A1"):
            self.assertTrue(importer.is_code(stem), stem)
        for stem in ("site visit", "site_visit", "map-a1", "Case-01", "A__B1", "x" * 65, "", "SILVA", "MAP-" + "A",
                     "SYN-OW"):                        # no digit: a name in capitals is not a code (analysis ruling)
            self.assertFalse(importer.is_code(stem), stem)
        doc = self.upload("transcript", "site visit.txt", "Interviewer: hi\nParticipant: hello")
        self.assertEqual((doc["file"]["pid"], doc["file"]["rel"]), ("ID-01", "inputs/transcripts/ID-01.txt"))
        self.assertEqual(self.upload("map", "site_visit.mmd", MERMAID_RED)["file"]["pid"], "ID-02")
        self.assertEqual(self.upload("transcript", "SITE VISIT.txt", "Interviewer: hi\nParticipant: hey")["file"]["pid"],
                         "ID-03")
        self.assertEqual(self.upload("map", "site visit.mmd", MERMAID_RED)["file"]["pid"], "ID-01")   # same stem pairs
        # a code that is already another file's id, in any case, is refused
        doc = self.refused("/api/import", {"kind": "transcript", "name": "ID-02.txt", "data_b64": b64("x y")},
                           409, "pid_collision")
        self.assertIn("site_visit.mmd", doc["message"])                        # the screen names both files
        self.upload("transcript", "MAP-A1.txt", "Interviewer: hi\nParticipant: hello")
        self.assertEqual(self.upload("map", "MAP-A1.mmd", MERMAID_RED)["file"]["rel"], "inputs/maps/MAP-A1.mmd")
        self.assertEqual(self.upload("transcript", "SILVA.txt", "Interviewer: hi\nParticipant: yo")["file"]["pid"],
                         "ID-04")                            # capitals with no digit: a neutral id
        self.assertEqual(len(self.project.data["files"]), 7)

    def test_identical_transcripts_are_flagged_but_still_stored_and_paired(self):
        """K2 U05 (app part): two transcripts with the same bytes get an "identical to" warning, in the import reply
        and on the pairing rows. Warning only: both stay stored, both stay in the table, and the table hash (what the
        confirmation covers) is the same as without the flag."""
        same = "Interviewer: hi\nParticipant: hello"
        first = self.upload("transcript", "CASE-01.txt", same)
        self.assertNotIn("identical_to", first)
        second = self.upload("transcript", "CASE-02.txt", same)
        self.assertEqual(second["identical_to"], ["CASE-01.txt"])
        self.assertEqual(first["file"]["sha256"], second["file"]["sha256"])
        other = self.upload("transcript", "CASE-03.txt", "Interviewer: hi\nParticipant: another")
        self.assertNotIn("identical_to", other)
        self.upload("map", "CASE-01.mmd", MERMAID_RED)
        self.assertNotIn("identical_to", self.upload("map", "CASE-02.mmd", MERMAID_RED))   # maps: not flagged
        table = self.ok_get("/api/pairing")
        rows = {r["pid"]: r for r in table["rows"]}
        self.assertEqual(sorted(rows), ["CASE-01", "CASE-02", "CASE-03"])                 # nothing excluded
        self.assertEqual(rows["CASE-01"]["identical_to"], ["CASE-02.txt"])
        self.assertEqual(rows["CASE-02"]["identical_to"], ["CASE-01.txt"])
        self.assertEqual(rows["CASE-03"]["identical_to"], [])
        self.assertEqual(len(self.project.data["files"]), 5)
        self.assertEqual(rows["CASE-02"]["reference_options"], ["A", "B", "C"])
        confirmed = self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        self.assertTrue(self.ok_get("/api/pairing")["confirmed"], confirmed)
        # the stored copy is the same as ever: the flag adds nothing to the project file
        self.assertEqual(set(self.project.data["files"]["inputs/transcripts/CASE-02.txt"]),
                         set(self.project.data["files"]["inputs/transcripts/CASE-03.txt"]))
        # changing one of them removes the flag
        self.upload("transcript", "CASE-02.txt", "Interviewer: hi\nParticipant: changed")
        self.assertEqual({r["pid"]: r["identical_to"] for r in self.ok_get("/api/pairing")["rows"]},
                         {"CASE-01": [], "CASE-02": [], "CASE-03": []})

    def test_pairing_by_stem(self):
        self.upload("transcript", "MAP-A1.txt", "Interviewer: hi\nParticipant: hello")
        self.upload("map", "MAP-A1.mmd", MERMAID_RED)
        self.upload("transcript", "MAP-B1.docx", _docx(["Interviewer: hi", "Participant: hello"]))
        self.upload("map", "MAP-C1.mapgraph.json", mapgraph_with_x([0, 10]))
        table = self.ok_get("/api/pairing")
        rows = {r["pid"]: r for r in table["rows"]}
        self.assertEqual(sorted(rows), ["MAP-A1", "MAP-B1", "MAP-C1"])
        self.assertEqual((rows["MAP-A1"]["transcript"], rows["MAP-A1"]["map"], rows["MAP-A1"]["paired"]),
                         ("MAP-A1.txt", "MAP-A1.mmd", True))
        self.assertEqual(rows["MAP-A1"]["reference_options"], ["A", "B", "C"])
        self.assertEqual((rows["MAP-B1"]["map"], rows["MAP-B1"]["paired"]), (None, False))
        self.assertEqual(rows["MAP-B1"]["reference_options"], ["B", "C"])         # unpaired: B or C only
        self.assertEqual((rows["MAP-C1"]["transcript"], rows["MAP-C1"]["reference_options"]), (None, []))
        self.assertFalse(table["confirmed"])
        self.refused("/api/pairing/confirm", {"table_sha256": "0" * 64}, 409, "stale_pairing")
        doc = self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        self.assertTrue(doc["confirmed"])
        self.assertEqual(sorted(self.project.data["pairing"]["pids"]), ["MAP-A1", "MAP-B1"])   # a map alone: no pid
        self.assertEqual(self.project.data["pairing"]["pids"]["MAP-A1"],
                         {"transcript": "inputs/transcripts/MAP-A1.txt", "map": "inputs/maps/MAP-A1.mmd"})
        self.assertEqual(self.project.data["pairing"]["pids"]["MAP-B1"],
                         {"transcript": "inputs/transcripts/MAP-B1.docx", "map": None})
        self.assertTrue(self.ok_get("/api/pairing")["confirmed"])
        self.upload("map", "MAP-B1.mmd", MERMAID_RED)          # a new file changes the table: confirm again
        self.assertFalse(self.ok_get("/api/pairing")["confirmed"])
        self.assertEqual(importer.confirmed_pids(self.project), [])
        self.assertTrue(importer.unconfirmed(self.project)["pairing"])

    def test_pid_map_written_and_nonempty(self):
        self.mermaid_pair("MAP-A1")
        self.upload("transcript", "site visit (2).txt", "Interviewer: hi\nParticipant: hello")
        path = self.project.root / "private" / "pid_map.json"
        self.assertFalse(path.exists())
        self.pair()
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc, {"map": {"MAP-A1": {"file_stem": "MAP-A1", "file_name": "MAP-A1.txt"},
                                       "ID-01": {"file_stem": "site visit (2)", "file_name": "site visit (2).txt"}}})
        self.assertEqual(importer.PID_MAP, "private/pid_map.json")
        # every other step, and the project summary: none of them carries the private map
        self.context()
        self.ok_get("/api/labels?pid=MAP-A1")
        self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.ok_get("/api/project")
        self.ok_get("/api/pairing")
        for reply in self.replies:
            self.assertNotIn("file_stem", reply)
            self.assertNotIn("pid_map", reply)
            self.assertNotIn(path.read_text(encoding="utf-8").strip(), reply)
        self.assertNotIn("private/pid_map.json", self.project.data["files"])
        self.assertNotIn("file_stem", json.dumps(self.project.log.records()))      # nor in the app log


class TestLabels(ImportCase):
    def setUp(self):
        super().setUp()
        self.context()

    def test_confirm_labels_refuses_none_format(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(NO_LABELS))
        self.pair()
        prev = self.ok_get("/api/labels?pid=MAP-A1")
        self.assertEqual(prev["format"], "none")
        doc = self.refused("/api/labels/confirm", {"pid": "MAP-A1", "participant": "I", "interviewer": "Where",
                                                   "role": "owner"}, 422, "no_speaker_labels")
        self.assertIn(importer.EXAMPLE_URL, doc["message"])
        self.assertEqual(importer.EXAMPLE_URL, "/static/examples/labelled-transcript.txt")
        self.assertEqual(self.project.log.records("labels_confirmed"), [])
        self.assertFalse(importer.labels_confirmed(self.project, "MAP-A1"))

    def test_confirm_labels_whole_word(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.pair()
        prev = self.ok_get("/api/labels?pid=MAP-A1")
        labels = {c["label"]: c["turns"] for c in prev["candidates"]}
        self.assertEqual(labels, {"Interviewer": 3, "Participant": 3})
        doc = self.ok("/api/labels/confirm", {"pid": "MAP-A1", "participant": "Participant",
                                              "interviewer": "Interviewer", "role": "owner"})
        self.assertEqual(doc["format"], "first_token")
        self.assertEqual(doc["turns"], {"participant": 3, "interviewer": 3, "unknown": 0})
        stored = self.project.data["labels"]["MAP-A1"]
        self.assertEqual((stored["participant"], stored["interviewer"], stored["role"]),
                         ("Participant", "Interviewer", "owner"))
        self.assertTrue(importer.labels_confirmed(self.project, "MAP-A1"))
        self.assertEqual(self.project.data["files"]["inputs/transcripts/MAP-A1.txt"]["labels"],
                         {"participant": "Participant", "interviewer": "Interviewer"})
        rec = self.project.log.records("labels_confirmed")[-1]["payload"]
        self.assertEqual(rec["pid"], "MAP-A1")
        self.assertEqual(rec["turns"], {"participant": 3, "interviewer": 3, "unknown": 0})
        self.assertNotIn("Participant", json.dumps(rec))           # label text stays in the project file only
        # the kit, given the confirmed labels, reads both speakers the same way
        from second_scribe.transcripts import parse_paragraphs, read_transcript_paras
        t = parse_paragraphs(read_transcript_paras(self.project.root / "inputs/transcripts/MAP-A1.txt"),
                             {stored["participant"]}, "MAP-A1", interviewer_labels={stored["interviewer"]})
        self.assertEqual([x.speaker for x in t.turns], ["interviewer", "participant"] * 3)
        # a changed transcript needs its labels confirmed again
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD[:4]))
        self.assertFalse(importer.labels_confirmed(self.project, "MAP-A1"))
        self.assertNotIn("labels", self.project.data["files"]["inputs/transcripts/MAP-A1.txt"])

    def test_confirm_labels_refusals(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        base = {"pid": "MAP-A1", "participant": "Participant", "interviewer": "Interviewer", "role": "owner"}
        self.refused("/api/labels/confirm", base, 409, "pairing_unconfirmed")
        self.pair()
        self.refused("/api/labels/confirm", {**base, "pid": "MAP-Z1"}, 404, "unknown_pid")
        self.refused("/api/labels/confirm", {**base, "role": "judge"}, 422, "bad_role")
        self.refused("/api/labels/confirm", {**base, "interviewer": "participant"}, 422, "same_label")
        self.refused("/api/labels/confirm", {**base, "participant": ""}, 422, "bad_label")
        self.refused("/api/labels/confirm", {**base, "participant": "Nobody"}, 422, "label_not_found")

    def test_labels_preview_warns_timestamp_prefix(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        prev = self.ok_get("/api/labels?pid=MAP-A1")
        part = [c for c in prev["candidates"] if c["label"] == "Participant"][0]
        self.assertEqual(part["shown"], "Participant 3 (00:04:12):")
        self.assertEqual(part["prefix"], "3 (00:04:12):")
        iv = [c for c in prev["candidates"] if c["label"] == "Interviewer"][0]
        self.assertEqual((iv["shown"], iv["prefix"]), ("Interviewer:", None))
        warn = [w for w in prev["warnings"] if w["code"] == "timestamp_prefix"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]["label"], "Participant")
        self.assertEqual(warn[0]["message"], strings.t("importer.warning.timestamp_prefix", label="Participant",
                                                       prefix="3 (00:04:12):"))
        self.assertIn("stays", warn[0]["message"])
        # the preview never carries the turns' words
        self.assertNotIn("town office", json.dumps(prev))
        plain = ["Interviewer: hi there", "Participant: hello", "Interviewer: so", "Participant: yes"]
        self.upload("transcript", "MAP-B1.txt", "\n".join(plain))
        self.assertEqual(self.ok_get("/api/labels?pid=MAP-B1")["warnings"], [])

    def test_bracket_labels(self):
        lines = ["[I] Where did it start?", "[P] At the town office.", "[I] Then?", "[P] The permit came."]
        self.upload("transcript", "MAP-A1.txt", "\n".join(lines))
        self.pair()
        prev = self.ok_get("/api/labels?pid=MAP-A1")
        self.assertEqual(prev["format"], "bracket")
        self.assertEqual(sorted(c["label"] for c in prev["candidates"]), ["i", "p"])
        doc = self.ok("/api/labels/confirm", {"pid": "MAP-A1", "participant": "p", "interviewer": "i",
                                              "role": "builder"})
        self.assertEqual(doc["turns"], {"participant": 2, "interviewer": 2, "unknown": 0})


class TestMissingRule(ImportCase):
    def test_kinds_per_suffix(self):
        opts = self.ok_get("/api/missing-rule")
        self.assertEqual(opts["options"][".vsdx"], ["red_line_or_text", "red_fill", "dashed"])
        self.assertEqual(opts["options"][".mmd"], ["mermaid_class", "classdef_style"])
        self.assertEqual(opts["options"][".csv"], ["status_column"])
        self.assertEqual(opts["rules"][".vsdx"], {"kind": "red_line_or_text", "colors": ["#ff0000"],
                                                  "class_names": ["missing"], "inherit": False, "default": True})
        for body in ({"suffix": ".mmd", "kind": "red_fill"}, {"suffix": ".pdf", "kind": "red_fill"},
                     {"suffix": ".mapgraph.json", "kind": "red_fill"}, {"suffix": ".drawio", "kind": "nope"},
                     {"suffix": ".drawio", "kind": "red_fill", "colors": ["red"]},
                     {"suffix": ".drawio", "kind": "red_fill", "colors": []},
                     {"suffix": ".mmd", "kind": "mermaid_class", "class_names": ["no spaces"]},
                     {"suffix": ".drawio", "kind": "red_line_or_text", "inherit": True},
                     {"suffix": ".vsdx", "kind": "dashed", "inherit": True},
                     {"suffix": ".vsdx", "kind": "red_line_or_text", "inherit": "yes"}):
            with self.subTest(body=body):
                self.refused("/api/missing-rule", body, 422, "bad_rule")
        doc = self.ok("/api/missing-rule", {"suffix": ".drawio", "kind": "red_fill", "colors": ["#FF0000", "#c00000"]})
        self.assertEqual(doc["rule"], {"kind": "red_fill", "colors": ["#ff0000", "#c00000"],
                                       "class_names": ["missing"], "inherit": False, "default": False})
        self.assertEqual(importer.rule_of(self.project, ".drawio"), MissingRule("red_fill", ("#ff0000", "#c00000")))
        self.assertEqual(self.project.log.records("missing_rule")[-1]["payload"],
                         {"suffix": ".drawio", "kind": "red_fill", "colors": ["#ff0000", "#c00000"],
                          "class_names": ["missing"], "inherit": False})

    def test_vsdx_inherit_flag_reaches_resolver(self):
        """V12: ``MissingRule.inherit`` sends a .vsdx to T11's resolver; the default rule still goes straight to
        ``extract_vsdx`` (T6's identity test stays green)."""
        rule = MissingRule("red_line_or_text", inherit=True)
        self.assertNotEqual(rule, DEFAULT_RULES[".vsdx"])
        self.assertFalse(DEFAULT_RULES[".vsdx"].inherit)
        seen = []
        real = READERS[".vsdx"]

        def spy(path, map_id, extract, r):
            seen.append(r)
            return real(path, map_id, extract, r)

        with mock.patch.dict(READERS, {".vsdx": spy}):
            g = read_map(VSDX_INHERIT, "MAP-A1", None, rule)
            kit = read_map(VSDX_INHERIT, "MAP-A1")                        # default: never reaches the resolver
        self.assertEqual(seen, [rule])
        resolved = apply_vsdx_rule(VSDX_INHERIT, extract_vsdx(VSDX_INHERIT, "MAP-A1"), MissingRule("red_line_or_text"))
        self.assertEqual(g.to_json(), resolved.to_json())
        self.assertEqual(kit.to_json(), extract_vsdx(VSDX_INHERIT, "MAP-A1").to_json())
        wished = lambda m: sum(n.status == "wished" for n in m.nodes)    # noqa: E731
        self.assertGreater(wished(g), wished(kit))
        # the flag travels through a corpus rule table too, and is refused for any other format
        self.assertEqual(rule_for({"missing_rules": {".vsdx": {"kind": "red_line_or_text", "inherit": True}}},
                                  "x.vsdx"), rule)
        self.assertFalse(rule_for({"missing_rules": {".vsdx": {"kind": "red_line_or_text"}}}, "x.vsdx").inherit)
        with self.assertRaises(ValueError):
            MissingRule("red_line_or_text", inherit="yes")
        with self.assertRaises(readers.MapRefused) as cm:
            read_map(FIXTURES / "journey_plain.drawio", "MAP-A1", None, MissingRule("red_line_or_text", inherit=True))
        self.assertEqual(cm.exception.code, "unsupported_format")
        # and through the app: the Setup choice reaches the preview
        self.context()
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.upload("map", "MAP-A1.vsdx", VSDX_INHERIT.read_bytes())
        before = self.ok_get("/api/maps/preview?pid=MAP-A1")["counts"]["missing"]
        self.ok("/api/missing-rule", {"suffix": ".vsdx", "kind": "red_line_or_text", "inherit": True})
        after = self.ok_get("/api/maps/preview?pid=MAP-A1")["counts"]["missing"]
        self.assertEqual((before, after), (wished(kit), wished(g)))


def mapgraph_with_texts(texts, xs=None) -> str:
    """A ``.mapgraph.json`` reference with one box per text, left to right, no arrows."""
    xs = xs or [10 * i for i in range(len(texts))]
    nodes = [{"node_id": f"n{i}", "text": t, "phase": None, "status": "experienced", "x": x}
             for i, (t, x) in enumerate(zip(texts, xs))]
    return json.dumps({"map_id": "x", "origin": "endorsed", "nodes": nodes, "edges": []})


class TestPhaseHeadingFlag(ImportCase):
    """R2: a box whose text is a Setup phase name is flagged "phase heading?" in the preview only. Nothing is
    dropped: the counts, the preview's boxes and the map the run reads stay as the kit reader gave them."""
    TEXTS = ["Planning", "Ask at the office", "  permits ", "Pay the fee", "BUILDING", "Plan review"]

    def setUp(self):
        super().setUp()
        self.context()                       # phases: Planning, Permits, Building
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.upload("map", "MAP-A1.mapgraph.json", mapgraph_with_texts(self.TEXTS))

    def test_flag_matching_ignores_case_and_whitespace(self):
        flags = importer.phase_heading_flags
        phases = ["Planning", "Permits", "Building"]
        self.assertEqual(flags(["planning", " PERMITS", "Build  ing", "Building\n", "Plan", "", None], phases),
                         [True, True, False, True, False, False, False])
        self.assertEqual(flags(["Planning  Permits"], phases), [False])         # a phase name, not two of them
        self.assertEqual(flags(["Planning"], []), [False])                      # no Setup phases: nothing to match
        self.assertEqual(flags(["Open  Plan"], ["open plan"]), [True])           # inner runs of spaces collapse

    def test_preview_flags_and_counts_apart(self):
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual({n["id"]: n["phase_heading"] for n in prev["nodes"]},
                         {"n0": True, "n1": False, "n2": True, "n3": False, "n4": True, "n5": False})
        self.assertEqual(prev["phase_headings"], 3)
        # the count to confirm is the full one; the headings are not dropped from it or from the nodes
        self.assertEqual(prev["counts"]["boxes"], 6)
        self.assertEqual(len(prev["nodes"]), 6)
        self.assertEqual(prev["counts_text"], "3 boxes, plus 3 that look like phase headings, 0 marked missing, "
                                              "0 arrows")

    def test_counts_text_unchanged_without_headings_and_singular(self):
        self.upload("map", "MAP-A1.mapgraph.json", mapgraph_with_texts(["Ask", "Pay"]))
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual((prev["phase_headings"], prev["counts_text"]), (0, "2 boxes, 0 marked missing, 0 arrows"))
        self.upload("map", "MAP-A1.mapgraph.json", mapgraph_with_texts(["Ask", "Pay", "Permits"]))
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual(prev["counts_text"], "2 boxes, plus 1 that looks like a phase heading, 0 marked missing, "
                                              "0 arrows")

    def test_the_map_the_run_reads_is_byte_identical_with_and_without_the_flag(self):
        self.pair()
        ref = self.project.root / "reference" / "MAP-A1.mapgraph.json"
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        doc = self.ok("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]})
        with_flag = ref.read_bytes()
        self.assertEqual(doc["counts"]["boxes"], 6)                              # the full count is what is confirmed
        g = MapGraph.from_json(with_flag.decode("utf-8"))
        self.assertEqual([n.text for n in g.nodes], self.TEXTS)                  # headings still there, text untouched
        self.assertNotIn("phase_heading", with_flag.decode("utf-8"))             # the flag never reaches the map
        with mock.patch.object(importer, "phase_heading_flags", lambda texts, phases: [False] * len(texts)):
            self.ok("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]})
        self.assertEqual(ref.read_bytes(), with_flag)
        # and the kit's own reader gives the same boxes as the reference it was handed
        self.assertEqual(len(read_map(self.project.path(self.project.data["pairing"]["pids"]["MAP-A1"]["map"]),
                                      "MAP-A1").nodes), 6)


class TestPreview(ImportCase):
    def setUp(self):
        super().setUp()
        self.context()

    def test_preview_warns_zero_missing(self):
        self.mermaid_pair("MAP-A1", MERMAID_PLAIN)
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual(prev["counts"], {"boxes": 3, "missing": 0, "missing_arrows": 0, "arrows": 2,
                                          "unresolved": 0})
        codes = [w["code"] for w in prev["warnings"]]
        self.assertIn("zero_missing", codes)
        self.assertEqual([w["message"] for w in prev["warnings"] if w["code"] == "zero_missing"],
                         ["No box is marked missing. Check the rule for this format."])
        self.assertFalse(prev["offer_inherit"])                 # Mermaid has no inherited styles
        self.assertEqual(prev["counts_text"], "3 boxes, 0 marked missing, 2 arrows")
        self.mermaid_pair("MAP-B1", MERMAID_RED)
        prev = self.ok_get("/api/maps/preview?pid=MAP-B1")
        self.assertEqual(prev["counts"]["missing"], 1)
        self.assertNotIn("zero_missing", [w["code"] for w in prev["warnings"]])
        self.assertEqual(prev["counts_text"], "3 boxes, 1 marked missing, 2 arrows")

    def test_zero_missing_vsdx_offers_inherited_red(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.upload("map", "MAP-A1.vsdx", VSDX_NO_RED.read_bytes())
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual(prev["counts"]["missing"], 0)
        self.assertTrue(prev["offer_inherit"])
        self.assertIn("zero_missing", [w["code"] for w in prev["warnings"]])
        self.ok("/api/missing-rule", {"suffix": ".vsdx", "kind": "red_line_or_text", "inherit": True})
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertFalse(prev["offer_inherit"])                 # already chosen
        self.assertTrue(prev["rule"]["inherit"])

    def test_confirm_stale_sha_refused(self):
        self.mermaid_pair("MAP-A1")
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.refused("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]}, 409, "pairing_unconfirmed")
        self.pair()
        self.refused("/api/maps/confirm", {"pid": "MAP-A1", "sha256": "0" * 64}, 409, "stale_map")
        self.upload("map", "MAP-A1.mmd", MERMAID_RED + "  c --> d[Build]\n")     # re-imported after the preview
        self.refused("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]}, 409, "stale_map")
        (self.project.root / "inputs" / "maps" / "MAP-A1.mmd").write_text(MERMAID_PLAIN, encoding="utf-8")
        fresh = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertTrue(fresh["changed"])
        self.refused("/api/maps/confirm", {"pid": "MAP-A1", "sha256": fresh["sha256"]}, 409, "file_changed")
        self.assertEqual(self.project.log.records("map_confirmed"), [])
        self.assertFalse((self.project.root / "reference" / "MAP-A1.mapgraph.json").exists())
        self.refused("/api/maps/confirm", {"pid": "MAP-Z1", "sha256": "0" * 64}, 404, "unknown_pid")

    def test_phase_from_x_order_and_override(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.upload("map", "MAP-A1.mapgraph.json",
                    mapgraph_with_x([0, 32, 34, 67, 100, None, 50], [None, None, None, None, None, None, "Building"]))
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual(prev["phases"], CONTEXT["phases"])
        got = {n["id"]: (n["phase"], n["phase_from"]) for n in prev["nodes"]}
        self.assertEqual(got, {"n0": ("Planning", "position"), "n1": ("Planning", "position"),
                               "n2": ("Permits", "position"), "n3": ("Building", "position"),
                               "n4": ("Building", "position"), "n5": ("Planning", "position"),
                               "n6": ("Building", "map")})
        self.ok("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"n1": "Permits", "n6": "Planning"}})
        got = {n["id"]: (n["phase"], n["phase_from"]) for n in self.ok_get("/api/maps/preview?pid=MAP-A1")["nodes"]}
        self.assertEqual((got["n1"], got["n6"], got["n0"]),
                         (("Permits", "you"), ("Planning", "you"), ("Planning", "position")))
        self.refused("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"n1": "Elsewhere"}}, 422, "bad_phase")
        self.refused("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"zz": "Permits"}}, 422, "unknown_node")
        self.refused("/api/maps/phases", {"pid": "MAP-A1", "node_phase": []}, 422, "bad_phase")
        # all at one x: every node in the earliest phase
        self.upload("map", "MAP-B1.mapgraph.json", mapgraph_with_x([5, 5]))
        self.upload("transcript", "MAP-B1.txt", "\n".join(WHOLE_WORD))
        self.assertEqual({n["phase"] for n in self.ok_get("/api/maps/preview?pid=MAP-B1")["nodes"]}, {"Planning"})
        self.assertEqual(importer.phase_by_position([None, 0.0, 99.0, 100.0], ["a", "b"]), ["a", "a", "b", "b"])
        self.assertEqual(importer.phase_by_position([1.0, 2.0], []), [None, None])

    def test_confirm_writes_normalised_reference(self):
        self.mermaid_pair("MAP-A1")
        self.pair()
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.assertFalse(prev["confirmed"])
        self.ok("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"c": "Building"}})
        doc = self.ok("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]})
        self.assertEqual(doc["reference"], "reference/MAP-A1.mapgraph.json")
        ref = self.project.root / "reference" / "MAP-A1.mapgraph.json"
        g = MapGraph.from_json(ref.read_text(encoding="utf-8"))
        self.assertEqual((g.map_id, g.origin), ("MAP-A1", "endorsed"))
        self.assertEqual(g.meta["reference_type"], "A")
        self.assertEqual(g.meta["source_sha256"], prev["sha256"])
        self.assertEqual({n.node_id: (n.phase, n.status) for n in g.nodes},
                         {"a": ("Planning", "experienced"), "b": ("Planning", "experienced"),
                          "c": ("Building", "wished")})
        self.assertEqual(len(g.edges), 2)
        # the kit reads it as it reads any reference, with no rule
        self.assertEqual(read_map(ref, "MAP-A1").to_json(), g.to_json())
        self.assertEqual(self.project.root / "inputs" / "maps" / "MAP-A1.mmd",
                         self.project.path(self.project.data["pairing"]["pids"]["MAP-A1"]["map"]))
        rec = self.project.log.records("map_confirmed")[-1]["payload"]
        self.assertEqual(rec, {"pid": "MAP-A1", "sha256": prev["sha256"],
                               "counts": {"boxes": 3, "missing": 1, "missing_arrows": 0, "arrows": 2, "unresolved": 0},
                               "rule": {"kind": "mermaid_class", "colors": ["#ff0000"], "class_names": ["missing"],
                                        "inherit": False},
                               "phases_sha256": importer._canon_sha({"a": "Planning", "b": "Planning",
                                                                     "c": "Building"}),
                               "n_phases_set": 1,
                               "reference_sha256": importer.file_sha256(ref)})
        self.assertTrue(self.ok_get("/api/maps/preview?pid=MAP-A1")["confirmed"])
        self.assertEqual(importer.unconfirmed(self.project), {"pairing": False, "labels": ["MAP-A1"], "maps": []})
        # a new rule, new phases or new context phases each undo the confirmation
        self.ok("/api/missing-rule", {"suffix": ".mmd", "kind": "classdef_style"})
        self.assertFalse(importer.map_confirmed(self.project, "MAP-A1"))
        self.ok("/api/missing-rule", {"suffix": ".mmd", "kind": "mermaid_class"})
        self.assertTrue(importer.map_confirmed(self.project, "MAP-A1"))
        self.ok("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"c": "Permits"}})
        self.assertFalse(importer.map_confirmed(self.project, "MAP-A1"))
        self.assertEqual(importer.unconfirmed(self.project)["maps"], ["MAP-A1"])

    def test_preview_of_unknown_or_mapless_pid(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        status, doc = self.get("/api/maps/preview?pid=MAP-A1")
        self.assertEqual((status, doc["error"]), (404, "no_map"))
        status, doc = self.get("/api/maps/preview?pid=MAP-Q1")
        self.assertEqual((status, doc["error"]), (404, "unknown_pid"))
        status, doc = self.get("/api/labels?pid=MAP-Q1")
        self.assertEqual((status, doc["error"]), (404, "unknown_pid"))


class TestContextAndLog(ImportCase):
    def test_context_validation(self):
        good = self.context()
        self.assertEqual(good["context"], CONTEXT)
        self.assertEqual(self.ok_get("/api/context")["context"], CONTEXT)
        bad = [
            {**CONTEXT, "domain": ""}, {**CONTEXT, "domain": "two\nlines"}, {**CONTEXT, "phases": []},
            {**CONTEXT, "phases": ["Planning", "planning"]}, {**CONTEXT, "phases": ["ok", ""]},
            {**CONTEXT, "phases": "Planning"}, {**CONTEXT, "roles": {}}, {**CONTEXT, "roles": {"owner": 3}},
            {**CONTEXT, "roles": ["owner"]}, {**CONTEXT, "phases": [f"phase {i}" for i in range(30)]},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.refused("/api/context", body, 422, "bad_context")
        self.assertEqual(self.project.data["context"], CONTEXT)

    def test_all_records_in_app_log_and_verify(self):
        self.context()
        self.mermaid_pair("MAP-A1")
        self.pair()
        self.ok("/api/labels/confirm", {"pid": "MAP-A1", "participant": "Participant", "interviewer": "Interviewer",
                                        "role": "owner"})
        self.ok("/api/missing-rule", {"suffix": ".mmd", "kind": "mermaid_class", "class_names": ["missing", "gap"]})
        prev = self.ok_get("/api/maps/preview?pid=MAP-A1")
        self.ok("/api/maps/phases", {"pid": "MAP-A1", "node_phase": {"c": "Building"}})
        self.ok("/api/maps/confirm", {"pid": "MAP-A1", "sha256": prev["sha256"]})
        types = [r["record_type"] for r in self.project.log.records()]
        self.assertEqual(types, ["project_created", "context_set", "import", "import", "labels_confirmed",
                                 "missing_rule", "map_confirmed"])
        ok, detail = verify_run(str(self.project.root / "logs" / "app.jsonl"))
        self.assertTrue(ok, detail)
        self.assertEqual(importer.unconfirmed(self.project), {"pairing": False, "labels": [], "maps": []})
        self.assertEqual(importer.confirmed_pids(self.project), ["MAP-A1"])
        ctx = self.project.log.records("context_set")[0]["payload"]
        self.assertEqual((ctx["n_phases"], ctx["n_roles"]), (3, 2))
        self.assertRegex(ctx["sha256"], r"^[0-9a-f]{64}$")
        # the project reopens with every setting in place
        again = Project.open(self.project.root)
        self.assertTrue(importer.map_confirmed(again, "MAP-A1"))
        self.assertTrue(importer.labels_confirmed(again, "MAP-A1"))
        # changing the phases undoes map confirmations; dropping the role undoes the labels
        self.ok("/api/context", {**CONTEXT, "phases": ["Planning", "Building"], "roles": {"builder": "x"}})
        self.assertEqual(importer.unconfirmed(self.project), {"pairing": False, "labels": ["MAP-A1"], "maps": ["MAP-A1"]})


def _docx(paras):
    import io
    import zipfile
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
