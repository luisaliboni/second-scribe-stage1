"""Task 33 x Task 36: the importer and the coding store agree on the project data.

One synthetic participant of the kit's own corpus (SYN-OW03: a ``.docx`` transcript and a ``.vsdx`` map, labels
``PT`` / ``IV``) goes through the real HTTP routes on a server started per test: context, import, pairing, labels,
map confirmation, reference B, marks, lock, the results gate and the reveal. The app log is read at the end.

No results route exists in the app yet (Task 40), so a probe registered with ``coding.results_route`` stands in
for the one that serves results.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.app import api, coding, importer
from second_scribe.app.project import Project
from second_scribe.schemas import MapGraph
from tests import test_server_security as tss

CORPUS = Path(__file__).resolve().parent.parent / "demo" / "corpus"
PID = "SYN-OW03"
CONTEXT = {"domain": "food-truck licensing",
           "phases": ["Planning", "Permits", "Inspections", "Operating"],
           "roles": {"owner": "food-truck owner who applied for and holds the licences",
                     "officer": "municipal licensing or inspection officer who processes the applications"}}
PROBE = "/api/test-results-probe"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FlowCase(tss.ServerCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.cookie = self.login()
        self.token_csrf = self.csrf(self.cookie)

        @coding.results_route("GET", PROBE)
        def probe(state, body, query):
            return {"results": "shown"}

    def get(self, path):
        status, _, data = self.raw("GET", path, headers=[("Cookie", self.cookie)])
        return status, json.loads(data)

    def send(self, path, obj):
        status, _, data = self.post(path, obj, self.cookie, self.token_csrf)
        return status, json.loads(data)

    def ok(self, path, obj):
        status, doc = self.send(path, obj)
        self.assertEqual(status, 200, doc)
        return doc

    def ok_get(self, path):
        status, doc = self.get(path)
        self.assertEqual(status, 200, doc)
        return doc

    def create_project(self):
        self.ok("/api/project/create", {"path": str(Path(self._tmp.name).resolve() / "study"), "name": "Study"})
        return self.state.project

    def upload(self, kind, name, data):
        return self.ok("/api/import", {"kind": kind, "name": name, "data_b64": b64(data)})

    def import_and_confirm(self):
        """Steps 1 to 5 of the flow; returns the project."""
        project = self.create_project()
        self.ok("/api/context", CONTEXT)
        self.upload("transcript", f"{PID}.docx", (CORPUS / "transcripts" / f"{PID}.docx").read_bytes())
        self.upload("map", f"{PID}.vsdx", (CORPUS / "maps" / f"{PID}.vsdx").read_bytes())
        table = self.ok_get("/api/pairing")
        self.assertEqual([(r["pid"], r["paired"]) for r in table["rows"]], [(PID, True)])
        self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        self.ok("/api/labels/confirm", {"pid": PID, "participant": "PT", "interviewer": "IV", "role": "owner"})
        return project

    def confirm_map(self):
        preview = self.ok_get(f"/api/maps/preview?pid={PID}")
        return self.ok("/api/maps/confirm", {"pid": PID, "sha256": preview["sha256"]})

    def reference_type(self, project):
        f = project.path(f"reference/{PID}.mapgraph.json")
        return json.loads(f.read_text(encoding="utf-8"))["meta"]["reference_type"] if f.exists() else None

    def span_of_first_participant_turn(self):
        view = self.ok_get(f"/api/coding?pid={PID}")
        for t in view["turns"]:
            if t["speaker"] == "participant" and len(t["text"].split()) >= coding.MIN_WORDS:
                words = t["text"].split(" ")[: coding.MIN_WORDS + 2]
                text = " ".join(words)
                return {"turn": t["idx"], "start": 0, "end": len(text), "text": text}
        self.fail("no participant turn with enough words")


class TestImportThenCode(FlowCase):
    def test_import_to_lock_to_reveal(self):
        project = self.import_and_confirm()
        # the importer's data is what the coding store reads
        self.assertEqual(coding.known_pids(project), [PID])
        self.assertEqual(coding.transcript_source(project, PID)["participant_labels"], ["PT"])
        self.assertEqual(coding.setup_phases(project), CONTEXT["phases"])
        self.assertFalse(coding.map_confirmed(project, PID))
        self.assertEqual(self.send("/api/references", {"pid": PID, "type": "A"})[1]["error"], "map_not_confirmed")

        out = self.confirm_map()
        self.assertEqual(out["reference_type"], None)               # no choice yet: the A file stays
        self.assertTrue(coding.map_confirmed(project, PID))
        self.assertEqual(self.reference_type(project), "A")

        # B: no reference file while the coding is open, and re-confirming the map does not bring A back
        self.assertEqual(self.ok("/api/references", {"pid": PID, "type": "B"})["type"], "B")
        self.assertIsNone(self.reference_type(project))
        self.assertEqual(self.confirm_map()["reference_type"], "B")
        self.assertIsNone(self.reference_type(project))
        self.assertEqual(self.ok_get("/api/results/gate")["waiting"], [PID])
        self.assertEqual(self.get(PROBE)[1]["error"], "coding_unlocked")

        view = self.ok_get(f"/api/coding?pid={PID}")
        self.assertEqual(view["phases"], CONTEXT["phases"])
        self.assertTrue(any(t["speaker"] == "participant" for t in view["turns"]))
        mark = {"pid": PID, **self.span_of_first_participant_turn(), "status": "happened",
                "step": "Read the how-to pages", "phase": "Planning"}
        self.assertEqual(self.ok("/api/coding/mark", mark)["n_marks"], 1)
        self.ok("/api/coding/tick", {"pid": PID, "seconds": 30})
        self.ok("/api/coding/lock", {"pid": PID})

        # locked: the B reference is on disk, a later confirmation of the map leaves it in place
        self.assertEqual(self.reference_type(project), "B")
        self.assertEqual(self.confirm_map()["reference_type"], "B")
        self.assertEqual(self.reference_type(project), "B")
        g = MapGraph.from_json(project.path(f"reference/{PID}.mapgraph.json").read_text(encoding="utf-8"))
        self.assertEqual([n.text for n in g.nodes], ["Read the how-to pages"])

        gate = self.ok_get("/api/results/gate")
        self.assertEqual((gate["open"], gate["waiting"], gate["revealed"]), (True, [], False))
        self.assertEqual(self.ok_get(PROBE), {"results": "shown"})

        kinds = [r["record_type"] for r in project.log.records()]
        self.assertEqual(kinds.count("coding_locked"), 1)
        self.assertEqual(kinds.count("reveal"), 1)
        self.assertLess(kinds.index("coding_locked"), kinds.index("reveal"))
        for k in ("context_set", "import", "labels_confirmed", "map_confirmed", "reference_set"):
            self.assertIn(k, kinds)
        ok, detail = project.log.verify()
        self.assertTrue(ok, detail)
        # a fresh read of the log from disk says the same
        reread = Project.open(project.root)
        self.assertTrue(reread.log.verify()[0])
        self.assertTrue(coding.is_locked(reread, PID))

    def test_c_pid_keeps_the_empty_reference_when_the_map_is_confirmed_again(self):
        project = self.import_and_confirm()
        self.confirm_map()
        self.ok("/api/references", {"pid": PID, "type": "C"})
        self.assertEqual(self.confirm_map()["reference_type"], "C")
        self.assertEqual(self.reference_type(project), "C")

    def test_transcript_edited_after_labels_blocks_coding(self):
        project = self.import_and_confirm()
        self.confirm_map()
        self.ok("/api/references", {"pid": PID, "type": "B"})
        self.assertEqual(self.ok("/api/coding/mark", {"pid": PID, **self.span_of_first_participant_turn(),
                                                      "status": "happened", "step": "Read the how-to pages",
                                                      "phase": "Planning"})["n_marks"], 1)
        f = project.path(f"inputs/transcripts/{PID}.docx")
        f.write_bytes(f.read_bytes() + b"\x00edited")
        for status, doc in (self.get(f"/api/coding?pid={PID}"),
                            self.send("/api/coding/mark", {"pid": PID, **{"turn": 1, "start": 0, "end": 5,
                                                                          "text": "x"}}),
                            self.send("/api/coding/lock", {"pid": PID})):
            self.assertEqual((status, doc["error"]), (409, "file_changed"))
        self.assertEqual(project.log.records("coding_locked"), [])
        self.assertEqual(self.get(PROBE)[1]["error"], "coding_unlocked")

    def test_coding_needs_the_labels_confirmed(self):
        project = self.create_project()
        self.ok("/api/context", CONTEXT)
        self.upload("transcript", f"{PID}.docx", (CORPUS / "transcripts" / f"{PID}.docx").read_bytes())
        table = self.ok_get("/api/pairing")
        self.ok("/api/pairing/confirm", {"table_sha256": table["table_sha256"]})
        self.ok("/api/references", {"pid": PID, "type": "B"})
        status, doc = self.get(f"/api/coding?pid={PID}")
        self.assertEqual((status, doc["error"]), (409, "labels_unconfirmed"))
        self.assertEqual(self.send("/api/coding/mark", {"pid": PID})[1]["error"], "labels_unconfirmed")


if __name__ == "__main__":
    unittest.main()
