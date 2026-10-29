"""Task 36: reference choice, mode B coding store, lock, results gate and conversion (spec section 6.6, 6.7).

The model (``second_scribe.app.coding``) is tested on temp projects; the routes (``routes/coding.py``) both through
``api.dispatch`` and through a real server on 127.0.0.1 (session and CSRF of Task 31).

Blindness is the invariant under test: while any mode-B coding in the project is unlocked, no route returns
anything AI-derived, and the one ``reveal`` record comes after every ``coding_locked`` record.

The imported-transcript data is made with Task 33's own functions (``importer``: context, import, pairing, labels,
map confirmation), so the keys the coding store reads are the ones the importer writes.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import certificate, textnorm
from second_scribe.app import api, coding, importer
from second_scribe.app.project import Project
from second_scribe.reference import empty_reference
from second_scribe.schemas import MapGraph, Node
from tests import test_server_security as tss

PHASES = ["Planning", "Applying", "Waiting"]
PARAS = {
    "CASE-B01": [
        "Interviewer: Can you tell me how the permit process started for you?",
        "Participant: Well I went down to the town office and asked the clerk about the building forms.",
        "Interviewer: And then what happened?",
        "Participant: Then I waited six weeks for a letter from the planning board that never came at all.",
        "Participant: I wished someone had phoned me to say what was missing from the application.",
        "Participant: No, nobody ever called me back.",
    ],
    "CASE-B02": [
        "Interviewer: Where did it begin?",
        "Participant: My neighbour told me I needed a permit before I could start building the shed.",
        "Interviewer: What next?",
        "Participant: I filled in the long form online and paid the fee with my credit card that evening.",
    ],
    "CASE-A03": [
        "Interviewer: Tell me about it.",
        "Participant: The inspector came out twice and both times he said the drawings were fine to go.",
        "Interviewer: Did anything surprise you?",
        "Participant: No, the whole thing was quiet once the drawings had been accepted by the town office.",
    ],
}
SENTINEL = "ZEBRAQUARTZ AI DERIVED SENTINEL STEP"


ROLE = "applicant"
MERMAID = "flowchart LR\n  a[Ask the clerk] --> b[Wait for the letter]\n  b --> c[Get the permit]\n"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def make_project(root: Path, pids=("CASE-B01", "CASE-B02", "CASE-A03"), confirmed=("CASE-A03",)) -> Project:
    """A project as Task 33 leaves it: context set, transcripts imported, the pairing and the speaker labels
    confirmed, some maps (``confirmed``) imported and confirmed with their mode-A reference written."""
    p = Project.create(root, "Permit pathways")
    importer.set_context(p, {"domain": "building permits", "phases": list(PHASES),
                             "roles": {ROLE: "A person who applied for a building permit."}})
    for pid in pids:
        importer.import_file(p, "transcript", f"{pid}.txt", b64("\n".join(PARAS[pid]) + "\n"))
    for pid in confirmed:
        importer.import_file(p, "map", f"{pid}.mmd", b64(MERMAID))
    importer.confirm_pairing(p, importer.pairing(p)["table_sha256"])
    for pid in pids:
        importer.confirm_labels(p, {"pid": pid, "participant": "Participant", "interviewer": "Interviewer",
                                    "role": ROLE})
    for pid in confirmed:
        importer.confirm_map(p, {"pid": pid, "sha256": importer.preview_map(p, pid)["sha256"]})
    return p


def span(pid: str, turn: int, words: int, offset_words: int = 0) -> dict:
    """``{turn, start, end, text}`` covering ``words`` words of the parsed turn, from word ``offset_words``."""
    from second_scribe.transcripts import parse_paragraphs
    t = parse_paragraphs(PARAS[pid], {"Participant"}, pid, interviewer_labels={"Interviewer"})
    text = t.turns[turn].text
    parts = text.split(" ")
    start = len(" ".join(parts[:offset_words])) + (1 if offset_words else 0)
    piece = " ".join(parts[offset_words:offset_words + words])
    return {"turn": turn, "start": start, "end": start + len(piece), "text": piece}


def plant_ai_content(p: Project) -> None:
    """AI-derived content a background run could already have written: a view file and a work file."""
    (p.path("views") / "B-0a1b2c3d.view.json").write_text(json.dumps({"ai_nodes": [SENTINEL]}), encoding="utf-8")
    work = p.path("runs") / "B-0a1b2c3d" / "work" / "recon"
    work.mkdir(parents=True)
    (work / "CASE-B01.json").write_text(json.dumps({"nodes": [{"text": SENTINEL}]}), encoding="utf-8")


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.p = make_project(self.tmp / "proj")
        self.state = api.AppState(project=self.p)
        api.load_route_modules()

    def call(self, method, path, body=None, query=None):
        """(status, doc) through the route table, as the server would answer."""
        try:
            return 200, api.dispatch(self.state, method, path, body or {}, query or {})
        except api.ApiError as e:
            return e.status, e.to_json()

    def ok(self, method, path, body=None, query=None):
        status, doc = self.call(method, path, body, query)
        self.assertEqual(status, 200, doc)
        return doc

    def mark(self, pid="CASE-B01", turn=1, words=10, offset=0, status="happened", step="Ask the clerk",
             phase="Applying", **extra):
        body = {"pid": pid, **span(pid, turn, words, offset), "status": status, "step": step, "phase": phase,
                **extra}
        return self.call("POST", "/api/coding/mark", body)

    def set_ref(self, pid, rtype):
        return self.call("POST", "/api/references", {"pid": pid, "type": rtype})

    def types(self, record_type=None):
        return [r["record_type"] for r in self.p.log.records(record_type)]


# --- references ------------------------------------------------------------------------------------------------

class TestReferences(TmpCase):
    def test_reference_set_logged_per_pid(self):
        self.assertEqual(self.set_ref("CASE-B01", "B")[0], 200)
        self.assertEqual(self.set_ref("CASE-B02", "C")[0], 200)
        recs = self.p.log.records("reference_set")
        self.assertEqual([(r["payload"]["pid"], r["payload"]["type"]) for r in recs],
                         [("CASE-B01", "B"), ("CASE-B02", "C")])
        self.assertEqual(self.p.data["references"]["CASE-B01"], {"type": "B"})
        on_disk = json.loads(self.p.path("second-scribe-project.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["references"]["CASE-B02"], {"type": "C"})
        ok, detail = self.p.log.verify()
        self.assertTrue(ok, detail)

    def test_a_only_for_confirmed_maps(self):
        status, doc = self.set_ref("CASE-B01", "A")
        self.assertEqual((status, doc["error"]), (409, "map_not_confirmed"))
        self.assertEqual(self.set_ref("CASE-A03", "A")[0], 200)

    def test_bad_type_and_unknown_pid(self):
        self.assertEqual(self.set_ref("CASE-B01", "D")[1]["error"], "bad_reference_type")
        self.assertEqual(self.set_ref("CASE-ZZ9", "B")[1]["error"], "unknown_pid")
        self.assertEqual(self.set_ref("../x", "B")[1]["error"], "unknown_pid")

    def test_bulk_sets_confirmed_maps_to_a(self):
        self.set_ref("CASE-A03", "C")
        doc = self.ok("POST", "/api/references/bulk", {"confirmed_maps": "A"})
        self.assertEqual(doc["set"], ["CASE-A03"])
        self.assertEqual(self.p.data["references"]["CASE-A03"], {"type": "A"})
        self.assertNotIn("CASE-B01", self.p.data["references"])
        self.assertEqual(self.call("POST", "/api/references/bulk", {"confirmed_maps": "B"})[0], 422)

    def test_mode_c_empty_reference_written(self):
        self.set_ref("CASE-B02", "C")
        g = MapGraph.from_json(self.p.path("reference/CASE-B02.mapgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(g.to_json(), empty_reference("CASE-B02").to_json())
        self.assertEqual(g.meta["reference_type"], "C")

    def test_a_reference_kept_when_switching_away_and_back(self):
        ref = self.p.path("reference/CASE-A03.mapgraph.json")
        original = ref.read_text(encoding="utf-8")
        self.set_ref("CASE-A03", "C")
        self.assertEqual(json.loads(ref.read_text(encoding="utf-8"))["meta"]["reference_type"], "C")
        self.set_ref("CASE-A03", "B")
        self.assertFalse(ref.exists())          # an unlocked B coding has no reference file yet
        self.set_ref("CASE-A03", "A")
        self.assertEqual(ref.read_text(encoding="utf-8"), original)

    def test_list_references(self):
        self.set_ref("CASE-B01", "B")
        self.mark()
        doc = self.ok("GET", "/api/references")
        rows = {r["pid"]: r for r in doc["references"]}
        self.assertEqual(set(rows), {"CASE-B01", "CASE-B02", "CASE-A03"})
        self.assertEqual(rows["CASE-B01"]["type"], "B")
        self.assertEqual(rows["CASE-B01"]["coding"], {"n_marks": 1, "locked": False})
        self.assertTrue(rows["CASE-A03"]["map_confirmed"])
        self.assertIsNone(rows["CASE-B02"]["type"])
        self.assertFalse(doc["gate"]["open"])
        self.assertEqual(doc["gate"]["waiting"], ["CASE-B01"])


# --- marking ---------------------------------------------------------------------------------------------------

class TestMarking(TmpCase):
    def setUp(self):
        super().setUp()
        self.set_ref("CASE-B01", "B")

    def test_short_span_refused(self):
        status, doc = self.mark(words=coding.MIN_WORDS - 1)
        self.assertEqual((status, doc["error"]), (422, "span_too_short"))
        self.assertIn(str(coding.MIN_WORDS), doc["message"])
        self.assertEqual(self.mark(words=coding.MIN_WORDS)[0], 200)
        self.assertEqual(coding.MIN_WORDS, 8)                       # the kit's quote rule (provenance.eligible)

    def test_whole_short_turn_accepted_partial_short_span_refused(self):
        # S6: the kit's rule (provenance.eligible) takes a quote under MIN_WORDS words when it is a whole turn
        short = 5
        status, doc = self.mark(turn=short, words=6)                        # "No, nobody ever called me back."
        self.assertEqual(status, 200, doc)
        self.assertLess(doc["mark"]["end"] - doc["mark"]["start"], 40)
        # the same turn without its final stop is still every word of it: the kit compares word sequences
        body = {"pid": "CASE-B01", "turn": short, "start": 0, "end": len("No, nobody ever called me back"),
                "text": "No, nobody ever called me back", "status": "happened", "step": "Ask again",
                "phase": "Waiting"}
        self.assertEqual(self.call("POST", "/api/coding/mark", body)[0], 200)
        # part of a short turn is a short span like any other
        for words, offset in ((3, 0), (5, 1), (2, 3)):
            with self.subTest(words=words, offset=offset):
                st, d = self.mark(turn=short, words=words, offset=offset)
                self.assertEqual((st, d["error"]), (422, "span_too_short"))
        # a short span inside a long turn is still refused, even when it starts at 0
        self.assertEqual(self.mark(turn=1, words=coding.MIN_WORDS - 1)[1]["error"], "span_too_short")

    def test_span_must_match_text(self):
        body = {"pid": "CASE-B01", **span("CASE-B01", 1, 10), "status": "happened", "step": "Ask", "phase":
                "Applying"}
        bad_text = dict(body, text=body["text"].replace("town", "city"))
        self.assertEqual(self.call("POST", "/api/coding/mark", bad_text)[1]["error"], "span_mismatch")
        shifted = dict(body, start=body["start"] + 1, end=body["end"] + 1)
        self.assertEqual(self.call("POST", "/api/coding/mark", shifted)[1]["error"], "span_mismatch")
        other_turn = dict(body, turn=3)
        self.assertEqual(self.call("POST", "/api/coding/mark", other_turn)[1]["error"], "span_mismatch")
        for bad in ({"turn": 99}, {"turn": -1}, {"start": -1}, {"end": 10 ** 6}, {"start": 5, "end": 5},
                    {"turn": "1"}, {"start": True}):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("POST", "/api/coding/mark", dict(body, **bad))[1]["error"], "bad_span")
        self.assertEqual(self.call("POST", "/api/coding/mark", body)[0], 200)
        self.assertEqual(len(coding.load_coding(self.p, "CASE-B01")["marks"]), 1)

    def test_mark_fields(self):
        self.assertEqual(self.mark(status="maybe")[1]["error"], "bad_status")
        self.assertEqual(self.mark(phase="Nowhere")[1]["error"], "bad_phase")
        self.assertEqual(self.mark(step="  ")[1]["error"], "bad_step")
        self.assertEqual(self.mark(step="a\nb")[1]["error"], "bad_step")
        self.assertEqual(self.mark(status="happened", missing_kind="denied")[1]["error"], "bad_missing_kind")
        self.assertEqual(self.mark(status="missing", missing_kind="lost")[1]["error"], "bad_missing_kind")
        status, doc = self.mark(turn=4, status="missing", missing_kind="wished for", step="Phone call",
                                phase="Waiting")
        self.assertEqual(status, 200, doc)
        m = doc["mark"]
        self.assertEqual((m["status"], m["missing_kind"], m["step"], m["phase"]),
                         ("missing", "wished for", "Phone call", "Waiting"))
        self.assertEqual(self.mark(status="missing")[0], 200)               # the kind is optional

    def test_marks_only_for_mode_b(self):
        self.assertEqual(self.mark(pid="CASE-B02")[1]["error"], "not_mode_b")
        self.set_ref("CASE-B02", "C")
        self.assertEqual(self.mark(pid="CASE-B02")[1]["error"], "not_mode_b")

    def test_unmark(self):
        m1 = self.mark()[1]["mark"]
        m2 = self.mark(turn=3, step="Wait for letter", phase="Waiting")[1]["mark"]
        self.assertNotEqual(m1["mark_id"], m2["mark_id"])
        self.ok("POST", "/api/coding/unmark", {"pid": "CASE-B01", "mark_id": m1["mark_id"]})
        self.assertEqual([m["mark_id"] for m in coding.load_coding(self.p, "CASE-B01")["marks"]], [m2["mark_id"]])
        self.assertEqual(self.call("POST", "/api/coding/unmark", {"pid": "CASE-B01", "mark_id": "nope"})[0], 404)

    def test_suggest_only_own_names(self):
        self.set_ref("CASE-B02", "B")
        self.mark(step="Ask the clerk")
        self.mark(turn=3, step="Await the letter", phase="Waiting")
        self.mark(pid="CASE-B02", turn=1, step="Asked a neighbour", phase="Planning")
        plant_ai_content(self.p)
        # a step name only an AI map holds, and the no-interview map's steps: never offered
        (self.p.path("views") / "x.view.json").write_text(json.dumps({"nodes": [{"text": "Ask the AI clerk"}]}),
                                                          encoding="utf-8")
        doc = self.ok("GET", "/api/coding/suggest", query={"prefix": "as"})
        self.assertEqual(doc["names"], ["Ask the clerk", "Asked a neighbour"])
        self.assertEqual(self.ok("GET", "/api/coding/suggest", query={"prefix": "await"})["names"],
                         ["Await the letter"])
        self.assertEqual(self.ok("GET", "/api/coding/suggest", query={"prefix": "zebra"})["names"], [])
        self.assertEqual(len(self.ok("GET", "/api/coding/suggest", query={"prefix": ""})["names"]), 3)

    def test_tick_adds_seconds(self):
        self.ok("POST", "/api/coding/tick", {"pid": "CASE-B01", "seconds": 30})
        doc = self.ok("POST", "/api/coding/tick", {"pid": "CASE-B01", "seconds": 45})
        self.assertEqual(doc["seconds"], 75)
        self.assertEqual(coding.load_coding(self.p, "CASE-B01")["seconds"], 75)
        for bad in (0, -5, 1.5, "10", True, coding.MAX_TICK + 1):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("POST", "/api/coding/tick", {"pid": "CASE-B01", "seconds": bad})[0], 422)

    def test_get_coding_shows_turns_marks_and_the_stated_limit(self):
        self.mark()
        doc = self.ok("GET", "/api/coding", query={"pid": "CASE-B01"})
        self.assertEqual([t["speaker"] for t in doc["turns"]],
                         ["interviewer", "participant", "interviewer", "participant", "participant",
                          "participant"])
        self.assertTrue(doc["turns"][3]["text"].startswith("Then I waited six weeks"))
        self.assertEqual(len(doc["marks"]), 1)
        self.assertEqual(doc["phases"], PHASES)
        self.assertEqual(doc["min_words"], 8)
        self.assertEqual(doc["missing_kinds"], ["never came", "wished for", "denied"])
        self.assertIs(doc["limit"], certificate.BLINDNESS_LIMIT)            # the one constant, never retyped
        self.assertFalse(doc["locked"])

    def test_coding_file_shape(self):
        self.mark(turn=4, status="missing", missing_kind="never came", step="Phone call", phase="Waiting")
        doc = json.loads(self.p.path("coding/CASE-B01.coding.json").read_text(encoding="utf-8"))
        self.assertEqual(set(doc), {"schema", "pid", "marks", "seconds", "locked"})
        self.assertEqual((doc["schema"], doc["pid"], doc["locked"]), ("coding/1", "CASE-B01", False))
        self.assertEqual(set(doc["marks"][0]), {"mark_id", "turn", "start", "end", "text", "status",
                                                "missing_kind", "step", "phase"})


# --- lock ------------------------------------------------------------------------------------------------------

class TestLock(TmpCase):
    def setUp(self):
        super().setUp()
        self.set_ref("CASE-B01", "B")
        self.mark()
        self.mark(turn=4, status="missing", missing_kind="wished for", step="Phone call", phase="Waiting")
        self.ok("POST", "/api/coding/tick", {"pid": "CASE-B01", "seconds": 120})

    def test_lock_record_and_reference(self):
        before = coding.coding_sha256(coding.load_coding(self.p, "CASE-B01"))
        doc = self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        self.assertEqual(doc, {"sha256": before, "n_marks": 2})
        rec = self.p.log.records("coding_locked")[-1]["payload"]
        self.assertEqual(rec, {"pid": "CASE-B01", "sha256": before, "n_marks": 2, "coding_seconds": 120})
        stored = json.loads(self.p.path("coding/CASE-B01.coding.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["locked"])
        self.assertEqual(coding.coding_sha256(stored), before)        # the hash is of the coding, flag aside
        g = MapGraph.from_json(self.p.path("reference/CASE-B01.mapgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(g.meta["reference_type"], "B")
        ok, detail = self.p.log.verify()
        self.assertTrue(ok, detail)

    def test_locked_is_read_only(self):
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        mark_id = coding.load_coding(self.p, "CASE-B01")["marks"][0]["mark_id"]
        for path, body in (("/api/coding/mark", None),
                           ("/api/coding/unmark", {"pid": "CASE-B01", "mark_id": mark_id}),
                           ("/api/coding/tick", {"pid": "CASE-B01", "seconds": 5}),
                           ("/api/coding/lock", {"pid": "CASE-B01"})):
            with self.subTest(path=path):
                status, doc = self.mark() if body is None else self.call("POST", path, body)
                self.assertEqual((status, doc["error"]), (409, "locked"))
        self.assertEqual(len(self.p.log.records("coding_locked")), 1)

    def test_lock_refusals(self):
        self.set_ref("CASE-B02", "B")
        self.assertEqual(self.call("POST", "/api/coding/lock", {"pid": "CASE-B02"})[1]["error"], "no_marks")
        self.assertEqual(self.call("POST", "/api/coding/lock", {"pid": "CASE-A03"})[1]["error"], "not_mode_b")
        self.assertEqual(self.p.log.records("coding_locked"), [])

    def test_lock_record_before_locked_flag(self):
        real_write = coding._write_json

        def crash_on_coding(path, doc):
            if Path(path).name.endswith(".coding.json") and doc.get("locked"):
                raise OSError("power cut")
            return real_write(path, doc)

        # 1. crash after the record, before the flag: the record is there, so reopening shows it locked
        with mock.patch.object(coding, "_write_json", crash_on_coding):
            with self.assertRaises(OSError):
                coding.lock(self.p, "CASE-B01")
        self.assertFalse(json.loads(self.p.path("coding/CASE-B01.coding.json").read_text(encoding="utf-8"))
                         ["locked"])
        reopened = Project.open(self.p.root)
        self.assertTrue(coding.is_locked(reopened, "CASE-B01"))
        self.assertTrue(coding.load_coding(reopened, "CASE-B01")["locked"])      # and the flag is settled
        self.assertTrue(reopened.path("reference/CASE-B01.mapgraph.json").is_file())

    def test_no_record_means_not_locked(self):
        # 2. the log append fails: no record, so nothing is locked and the flag was never written
        with mock.patch.object(self.p.log, "append", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                coding.lock(self.p, "CASE-B01")
        reopened = Project.open(self.p.root)
        self.assertFalse(coding.is_locked(reopened, "CASE-B01"))
        self.assertFalse(coding.load_coding(reopened, "CASE-B01")["locked"])
        self.assertFalse(reopened.path("reference/CASE-B01.mapgraph.json").exists())
        # 3. a flag set by hand without a record does not lock either, and does not open the results
        f = reopened.path("coding/CASE-B01.coding.json")
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["locked"] = True
        f.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(coding.is_locked(reopened, "CASE-B01"))
        self.assertFalse(coding.load_coding(reopened, "CASE-B01")["locked"])
        self.assertFalse(coding.results_gate(reopened))

    def test_edit_after_lock_is_flagged(self):
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        f = self.p.path("coding/CASE-B01.coding.json")
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["marks"][0]["step"] = "Something else"
        f.write_text(json.dumps(doc), encoding="utf-8")
        self.assertTrue(self.ok("GET", "/api/coding", query={"pid": "CASE-B01"})["changed_after_lock"])


# --- conversion ------------------------------------------------------------------------------------------------

class TestToReference(unittest.TestCase):
    def doc(self, marks):
        return {"schema": "coding/1", "pid": "CASE-B01", "seconds": 0, "locked": False,
                "marks": [{"mark_id": f"mark-{i:04d}", "turn": m[0], "start": 0, "end": 10, "text": m[1],
                           "status": m[2], "missing_kind": m[3], "step": m[4], "phase": m[5]}
                          for i, m in enumerate(marks, 1)]}

    def test_to_reference_merge_and_conflict(self):
        d = self.doc([
            (1, "quote one of the clerk", "happened", None, "Ask the clerk", "Applying"),
            (3, "quote two later on", "happened", None, "ask  the Clerk", "Applying"),     # same step, same phase
            (3, "quote three here", "happened", None, "Ask the clerk", "Waiting"),       # same step, other phase
            (4, "quote four wished", "missing", "wished for", "Phone call", "Waiting"),
            (4, "quote five plain", "missing", None, "Letter", "Waiting"),
        ])
        g = coding.to_reference(d)
        self.assertEqual([(n.text, n.phase, n.status) for n in g.nodes],
                         [("Ask the clerk", "Applying", "experienced"), ("Ask the clerk", "Waiting", "experienced"),
                          ("Phone call", "Waiting", "wished"), ("Letter", "Waiting", "wished")])
        self.assertEqual(g.nodes[0].quote, "quote one of the clerk")
        self.assertEqual(g.edges, [])
        self.assertEqual((g.map_id, g.origin, g.meta["reference_type"]), ("CASE-B01", "endorsed", "B"))
        phone = next(n for n in g.nodes if n.text == "Phone call")
        self.assertEqual(g.meta["missing_kinds"], {phone.node_id: "wished for"})
        self.assertEqual(g.meta["coding_sha256"], coding.coding_sha256(d))
        back = MapGraph.from_json(g.to_json())                    # loads in the kit, with missing_kinds kept
        self.assertEqual(back.meta["missing_kinds"], {phone.node_id: "wished for"})
        self.assertEqual(len({n.node_id for n in back.nodes}), 4)

        bad = self.doc([(1, "q one", "happened", None, "Ask the clerk", "Applying"),
                        (4, "q two", "missing", None, "ASK the clerk", "Applying")])
        with self.assertRaises(api.ApiError) as cm:
            coding.to_reference(bad)
        self.assertEqual((cm.exception.status, cm.exception.code), (422, "conflicting_marks"))
        self.assertIn("Ask the clerk", cm.exception.message)

    def test_written_reference_at_lock_is_the_conversion(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d:
            p = make_project(Path(d) / "proj")
            state = api.AppState(project=p)
            api.load_route_modules()
            api.dispatch(state, "POST", "/api/references", {"pid": "CASE-B01", "type": "B"}, {})
            api.dispatch(state, "POST", "/api/coding/mark", {"pid": "CASE-B01", **span("CASE-B01", 1, 9),
                         "status": "happened", "step": "Ask the clerk", "phase": "Applying"}, {})
            api.dispatch(state, "POST", "/api/coding/mark", {"pid": "CASE-B01", **span("CASE-B01", 1, 8, 2),
                         "status": "missing", "step": "Ask the clerk", "phase": "Applying"}, {})
            with self.assertRaises(api.ApiError) as cm:          # a conflict refuses the lock, nothing is logged
                api.dispatch(state, "POST", "/api/coding/lock", {"pid": "CASE-B01"}, {})
            self.assertEqual(cm.exception.code, "conflicting_marks")
            self.assertEqual(p.log.records("coding_locked"), [])
            self.assertFalse(coding.is_locked(p, "CASE-B01"))


# --- agreement with Task 33's data -------------------------------------------------------------------------------

class TestImporterAgreement(TmpCase):
    def reconfirm(self, pid):
        """Task 33's route, as the researcher presses Confirm again."""
        sha = importer.preview_map(self.p, pid)["sha256"]
        return self.ok("POST", "/api/maps/confirm", {"pid": pid, "sha256": sha})

    def reftype(self, pid):
        f = self.p.path(f"reference/{pid}.mapgraph.json")
        return json.loads(f.read_text(encoding="utf-8"))["meta"]["reference_type"] if f.exists() else None

    def test_labels_come_from_the_confirmed_entry(self):
        src = coding.transcript_source(self.p, "CASE-B01")
        self.assertEqual((src["rel"], src["participant_labels"], src["interviewer_labels"]),
                         ("inputs/transcripts/CASE-B01.txt", ["Participant"], ["Interviewer"]))
        self.p.data["labels"]["CASE-B01"].update(participant="Interviewer", interviewer="Participant")
        self.p.data["files"]["inputs/transcripts/CASE-B01.txt"]["labels"] = {"participant": "Participant",
                                                                            "interviewer": "Interviewer"}
        # the confirmed entry wins over the copy beside the file
        self.assertEqual(coding.transcript_source(self.p, "CASE-B01")["participant_labels"], ["Interviewer"])
        self.assertIsNone(coding.transcript_source(self.p, "CASE-ZZ9"))

    def test_setup_phases_are_the_importers(self):
        self.assertEqual(coding.setup_phases(self.p), PHASES)
        importer.set_context(self.p, {"domain": "d", "phases": ["One", "Two"], "roles": {ROLE: "x"}})
        self.assertEqual(coding.setup_phases(self.p), ["One", "Two"])

    def test_unconfirmed_labels_block_coding(self):
        self.set_ref("CASE-B01", "B")
        del self.p.data["labels"]["CASE-B01"]
        status, doc = self.mark()
        self.assertEqual((status, doc["error"]), (409, "labels_unconfirmed"))
        self.assertEqual(self.call("GET", "/api/coding", query={"pid": "CASE-B01"})[1]["error"],
                         "labels_unconfirmed")
        self.assertEqual(self.call("POST", "/api/coding/tick", {"pid": "CASE-B01", "seconds": 5})[1]["error"],
                         "labels_unconfirmed")

    def test_a_changed_role_makes_the_labels_stale(self):
        self.set_ref("CASE-B01", "B")
        importer.set_context(self.p, {"domain": "d", "phases": PHASES, "roles": {"other": "x"}})
        self.assertEqual(self.mark()[1]["error"], "labels_unconfirmed")

    def test_transcript_edited_after_labels_blocks_coding(self):
        self.set_ref("CASE-B01", "B")
        self.assertEqual(self.mark()[0], 200)
        f = self.p.path("inputs/transcripts/CASE-B01.txt")
        f.write_text(f.read_text(encoding="utf-8") + "Participant: A line added by hand afterwards, in the file.\n",
                     encoding="utf-8")
        for status, doc in (self.mark(turn=3), self.call("GET", "/api/coding", query={"pid": "CASE-B01"}),
                            self.call("POST", "/api/coding/lock", {"pid": "CASE-B01"})):
            self.assertEqual((status, doc["error"]), (409, "file_changed"))

    def test_map_confirmed_goes_stale_with_the_importer(self):
        self.assertTrue(coding.map_confirmed(self.p, "CASE-A03"))
        self.assertFalse(coding.map_confirmed(self.p, "CASE-B01"))
        importer.set_context(self.p, {"domain": "d", "phases": ["One", "Two"], "roles": {ROLE: "x"}})
        self.assertFalse(coding.map_confirmed(self.p, "CASE-A03"))
        self.assertEqual(self.set_ref("CASE-A03", "A")[1]["error"], "map_not_confirmed")
        importer.set_context(self.p, {"domain": "d", "phases": PHASES, "roles": {ROLE: "x"}})
        self.assertTrue(coding.map_confirmed(self.p, "CASE-A03"))
        importer.import_file(self.p, "map", "CASE-A03.mmd", b64(MERMAID + "  c --> d[Pay the fee]\n"))
        self.assertFalse(coding.map_confirmed(self.p, "CASE-A03"))

    def test_reconfirming_a_map_keeps_a_b_open_pid_without_a_file(self):
        self.set_ref("CASE-A03", "B")
        self.assertIsNone(self.reftype("CASE-A03"))
        out = self.reconfirm("CASE-A03")
        self.assertEqual(out["reference_type"], "B")
        self.assertIsNone(self.reftype("CASE-A03"))
        self.assertTrue(self.p.path("reference/CASE-A03.A.mapgraph.json").exists())   # A is kept aside
        self.assertEqual(self.set_ref("CASE-A03", "A")[0], 200)
        self.assertEqual(self.reftype("CASE-A03"), "A")

    def test_reconfirming_a_map_keeps_the_locked_b_reference(self):
        self.set_ref("CASE-A03", "B")
        self.mark(pid="CASE-A03", turn=1)
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-A03"})
        self.assertEqual(self.reftype("CASE-A03"), "B")
        self.assertEqual(self.reconfirm("CASE-A03")["reference_type"], "B")
        self.assertEqual(self.reftype("CASE-A03"), "B")
        g = MapGraph.from_json(self.p.path("reference/CASE-A03.mapgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(g.meta["coding_sha256"], coding.lock_record(self.p, "CASE-A03")["sha256"])

    def test_reconfirming_a_map_keeps_the_c_reference(self):
        self.set_ref("CASE-A03", "C")
        self.assertEqual(self.reftype("CASE-A03"), "C")
        self.assertEqual(self.reconfirm("CASE-A03")["reference_type"], "C")
        self.assertEqual(self.reftype("CASE-A03"), "C")

    def test_reconfirming_an_a_pid_keeps_the_new_a_file(self):
        self.set_ref("CASE-A03", "A")
        self.assertEqual(self.reconfirm("CASE-A03")["reference_type"], "A")
        self.assertEqual(self.reftype("CASE-A03"), "A")
        self.assertFalse(self.p.path("reference/CASE-A03.A.mapgraph.json").exists())

    def test_no_choice_leaves_the_a_file(self):
        self.assertEqual(self.reconfirm("CASE-A03")["reference_type"], None)
        self.assertEqual(self.reftype("CASE-A03"), "A")

    def test_apply_reference_choice_is_public(self):
        self.set_ref("CASE-A03", "C")
        self.p.path("reference/CASE-A03.mapgraph.json").write_text(
            MapGraph(map_id="CASE-A03", origin="endorsed", nodes=[], meta={"reference_type": "A"}).to_json(),
            encoding="utf-8")
        self.assertEqual(coding.apply_reference_choice(self.p, "CASE-A03"), "C")
        self.assertEqual(self.reftype("CASE-A03"), "C")
        self.assertIsNone(coding.apply_reference_choice(self.p, "CASE-B01"))


# --- the results gate and blindness ------------------------------------------------------------------------------

class TestGate(TmpCase):
    def setUp(self):
        super().setUp()
        self._routes = dict(api.ROUTES)
        self.addCleanup(lambda: (api.ROUTES.clear(), api.ROUTES.update(self._routes)))

        @coding.results_route("GET", "/api/test-results-probe")
        def probe(state, body, query):
            return {"ai": SENTINEL}

        plant_ai_content(self.p)

    def test_gate_open_without_b(self):
        self.assertTrue(coding.results_gate(self.p))
        self.set_ref("CASE-B01", "C")
        self.assertTrue(coding.results_gate(self.p))

    def test_view_409_until_all_b_locked(self):
        self.set_ref("CASE-B01", "B")
        self.set_ref("CASE-B02", "B")
        self.mark()
        self.mark(pid="CASE-B02", turn=1)
        status, doc = self.call("GET", "/api/test-results-probe")
        self.assertEqual((status, doc["error"]), (409, "coding_unlocked"))
        self.assertNotIn("ZEBRAQUARTZ", json.dumps(doc))
        with self.assertRaises(api.ApiError) as cm:
            coding.require_results(self.p)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "coding_unlocked"))
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        self.assertEqual(self.call("GET", "/api/test-results-probe")[1]["error"], "coding_unlocked")
        self.assertEqual(self.types("reveal"), [])
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B02"})
        self.assertEqual(self.ok("GET", "/api/test-results-probe"), {"ai": SENTINEL})

    def test_reveal_logged_once_after_all_locks(self):
        self.set_ref("CASE-B01", "B")
        self.set_ref("CASE-B02", "B")
        self.mark()
        self.mark(pid="CASE-B02", turn=1)
        self.call("GET", "/api/test-results-probe")
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B02"})
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        for _ in range(3):
            self.ok("GET", "/api/test-results-probe")
        recs = self.p.log.records()
        kinds = [r["record_type"] for r in recs]
        self.assertEqual(kinds.count("reveal"), 1)
        self.assertEqual(kinds.count("coding_locked"), 2)
        reveal_at = kinds.index("reveal")
        self.assertTrue(all(i < reveal_at for i, k in enumerate(kinds) if k == "coding_locked"))
        self.assertEqual(recs[reveal_at]["payload"], {"scope": "project"})
        ok, detail = self.p.log.verify()
        self.assertTrue(ok, detail)
        # Task 41 reads the same order from the log
        entry = certificate._app_entry(self.p.root / "logs" / "app.jsonl", "2026-10-18")
        self.assertIs(entry["locks_before_reveal"], True)

    def test_b_refused_after_reveal(self):
        self.ok("GET", "/api/test-results-probe")                    # no B coding: the gate is open, reveal logged
        self.assertEqual(self.types("reveal"), ["reveal"])
        status, doc = self.set_ref("CASE-B01", "B")
        self.assertEqual((status, doc["error"]), (409, "revealed"))
        self.assertNotIn("CASE-B01", self.p.data["references"])

    def test_locked_b_may_come_back_after_reveal(self):
        self.set_ref("CASE-A03", "B")
        self.mark(pid="CASE-A03", turn=1)
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-A03"})
        self.set_ref("CASE-A03", "A")
        self.ok("GET", "/api/test-results-probe")
        self.assertEqual(self.set_ref("CASE-A03", "B")[0], 200)       # locked before the reveal: still blind
        g = MapGraph.from_json(self.p.path("reference/CASE-A03.mapgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(g.meta["reference_type"], "B")
        self.assertTrue(coding.results_gate(self.p))

    def test_refusal_from_a_gated_handler_reveals_nothing(self):
        @coding.results_route("GET", "/api/test-results-missing")
        def missing(state, body, query):
            raise api.ApiError(404, "not_found", "no such run")

        self.assertEqual(self.call("GET", "/api/test-results-missing")[0], 404)
        self.assertEqual(self.types("reveal"), [])
        self.ok("GET", "/api/test-results-probe")
        self.assertEqual(self.types("reveal"), ["reveal"])

    def test_edited_locked_coding_refused_before_files_move(self):
        self.set_ref("CASE-A03", "B")
        self.mark(pid="CASE-A03", turn=1)
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-A03"})
        self.set_ref("CASE-A03", "A")
        ref = self.p.path("reference/CASE-A03.mapgraph.json")
        a_text = ref.read_text(encoding="utf-8")
        self.assertEqual(json.loads(a_text)["meta"]["reference_type"], "A")
        f = self.p.path("coding/CASE-A03.coding.json")
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["marks"][0]["step"] = "Edited by hand"
        f.write_text(json.dumps(doc), encoding="utf-8")
        status, err = self.set_ref("CASE-A03", "B")
        self.assertEqual((status, err["error"]), (409, "coding_changed"))
        self.assertEqual(ref.read_text(encoding="utf-8"), a_text)
        self.assertEqual(coding.reference_type_of(self.p, "CASE-A03"), "A")

    def test_no_route_returns_ai_content_while_b_open(self):
        """Every GET route, and every coding/reference route, with a B coding open: no AI-derived content in any
        answer, and no reveal record."""
        self.set_ref("CASE-B01", "B")
        self.mark()
        self.mark(turn=3, step="Wait for letter", phase="Waiting")
        query = {"pid": "CASE-B01", "run": "B-0a1b2c3d", "prefix": "", "path": "views/B-0a1b2c3d.view.json"}
        answers = []
        for (method, path) in sorted(api.ROUTES):
            if method != "GET":
                continue
            try:
                answers.append((path, json.dumps(api.dispatch(self.state, method, path, {}, dict(query)))))
            except api.ApiError as e:
                answers.append((path, json.dumps(e.to_json())))
            except Exception as e:                                   # noqa: BLE001 - a crash is not a leak
                answers.append((path, repr(e)))
        for path, body in (("/api/coding/tick", {"pid": "CASE-B01", "seconds": 3}),
                           ("/api/coding/mark", {"pid": "CASE-B01", **span("CASE-B01", 3, 9), "status": "happened",
                                                 "step": "Letter", "phase": "Waiting"}),
                           ("/api/references", {"pid": "CASE-B02", "type": "C"})):
            answers.append((path, json.dumps(self.call("POST", path, body)[1])))
        self.assertIn("/api/test-results-probe", [p for p, _ in answers])
        self.assertIn("/api/coding", [p for p, _ in answers])
        for path, text in answers:
            with self.subTest(path=path):
                self.assertNotIn("ZEBRAQUARTZ", text.upper())
        self.assertEqual(self.types("reveal"), [])


class TestGateOverHttp(tss.ServerCase):
    """The gate through the real server: session, CSRF and the 409 body as the browser sees it."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.state.project = make_project(Path(self._tmp.name).resolve() / "proj")
        plant_ai_content(self.state.project)

        @coding.results_route("GET", "/api/test-results-probe")
        def probe(state, body, query):
            return {"ai": SENTINEL}

        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)

    def get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        return status, json.loads(data)

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        return status, json.loads(data)

    def test_blind_until_locked_then_revealed(self):
        self.assertEqual(self.send("/api/references", {"pid": "CASE-B01", "type": "B"})[0], 200)
        body = {"pid": "CASE-B01", **span("CASE-B01", 1, 10), "status": "happened", "step": "Ask the clerk",
                "phase": "Applying"}
        self.assertEqual(self.send("/api/coding/mark", body)[0], 200)
        status, doc = self.get("/api/test-results-probe")
        self.assertEqual((status, doc["error"]), (409, "coding_unlocked"))
        status, doc = self.get("/api/coding?pid=CASE-B01")
        self.assertEqual(status, 200)
        self.assertNotIn("ZEBRAQUARTZ", json.dumps(doc))
        self.assertEqual(self.send("/api/coding/lock", {"pid": "CASE-B01"})[0], 200)
        self.assertEqual(self.get("/api/test-results-probe"), (200, {"ai": SENTINEL}))
        kinds = [r["record_type"] for r in self.state.project.log.records()]
        self.assertLess(kinds.index("coding_locked"), kinds.index("reveal"))


class TestHelpers(unittest.TestCase):
    def test_min_words_is_the_kits(self):
        import inspect
        from second_scribe import provenance
        self.assertEqual(coding.MIN_WORDS, inspect.signature(provenance.eligible).parameters["min_words"].default)
        self.assertEqual(textnorm.word_count("one two three four five six seven eight"), 8)


if __name__ == "__main__":
    unittest.main()
