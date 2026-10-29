"""Items 25 to 27 of the author's app feedback (23 Oct), server side: Remove an imported file (item 26) and the
speaker-label standard (item 27).

Driven through the real routes on a server on 127.0.0.1 with the session and CSRF of Task 31 (``ImportCase``).
Remove: the copy, the ``files`` entry and the confirmations that depended on it go; one ``removed`` record is appended
to the hash-chained app log (which still verifies) and earlier records are untouched; the browser never sends a path,
and a registered entry that points anywhere but directly into the kind's inputs folder is refused; refused while a
run, a model check or a time estimate is going. Standard: saved in ``private/``, never logged, fills every
transcript (later imports too), flags what it cannot fill; each confirmation is still its own ``labels_confirmed``.
"""
import json
import os
from unittest import mock

from second_scribe.app import importer
from second_scribe.audit.verify import verify_run
from tests.test_importer import CONTEXT, ImportCase, MERMAID_RED, WHOLE_WORD


def bracket(part="PT", iv="IV", n=3):
    lines = []
    for i in range(n):
        lines.append(f"[{iv}] Question number {i} about the permit?")
        lines.append(f"[{part}] Answer number {i}: I went to the town office.")
    return "\n".join(lines)


def records(project):
    path = project.path("logs/app.jsonl")
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestRemove(ImportCase):
    def setUp(self):
        super().setUp()
        self.context()

    def full_pid(self, stem="MAP-A1"):
        """A transcript and a map for ``stem``, paired, labels and map confirmed."""
        self.upload("transcript", f"{stem}.txt", "\n".join(WHOLE_WORD))
        self.upload("map", f"{stem}.mmd", MERMAID_RED)
        self.pair()
        self.ok("/api/labels/confirm", {"pid": stem, "participant": "Participant", "interviewer": "Interviewer",
                                        "role": "owner"})
        prev = self.ok_get(f"/api/maps/preview?pid={stem}")
        self.ok("/api/maps/confirm", {"pid": stem, "sha256": prev["sha256"]})

    def test_remove_transcript_clears_its_dependants_and_logs_once(self):
        self.full_pid()
        before = records(self.project)
        copy = self.project.path("inputs/transcripts/MAP-A1.txt")
        self.assertTrue(copy.is_file())
        doc = self.ok("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"})
        self.assertEqual(doc["removed"], {"kind": "transcript", "pid": "MAP-A1", "name": "MAP-A1.txt"})
        self.assertEqual(doc["reset"], {"pairing": True, "labels": True, "map": False})
        self.assertFalse(copy.exists())
        self.assertEqual([p.name for p in copy.parent.iterdir()], [])          # nothing left aside
        self.assertNotIn("inputs/transcripts/MAP-A1.txt", self.project.data["files"])
        self.assertIn("inputs/maps/MAP-A1.mmd", self.project.data["files"])     # the map stays
        self.assertEqual(self.project.data["pairing"], {})
        self.assertNotIn("MAP-A1", self.project.data["labels"])
        self.assertIsNone(importer.original_names(self.project)["MAP-A1"]["transcript"])
        self.assertEqual(importer.original_names(self.project)["MAP-A1"]["map"], "MAP-A1.mmd")
        after = records(self.project)
        self.assertEqual(after[:len(before)], before)                           # history unchanged
        self.assertEqual(len(after), len(before) + 1)
        rec = after[-1]
        self.assertEqual(rec["record_type"], "removed")
        meta_sha = [r for r in before if r["record_type"] == "import" and r["payload"]["kind"] == "transcript"][-1]
        self.assertEqual(rec["payload"], {"kind": "transcript", "pid": "MAP-A1", "sha256": meta_sha["payload"]["sha256"],
                                          "bytes": meta_sha["payload"]["bytes"],
                                          "reset": {"pairing": True, "labels": True, "map": False}})
        ok, detail = verify_run(str(self.project.path("logs/app.jsonl")))
        self.assertTrue(ok, detail)
        # the table shows the map alone and asks for the pairing again
        table = self.ok_get("/api/pairing")
        self.assertEqual([(r["pid"], r["transcript"], r["map"]) for r in table["rows"]], [("MAP-A1", None, "MAP-A1.mmd")])
        self.assertFalse(table["confirmed"])

    def test_remove_map_clears_its_confirmation_only(self):
        self.full_pid()
        self.assertTrue(importer.map_confirmed(self.project, "MAP-A1"))
        doc = self.ok("/api/import/remove", {"kind": "map", "pid": "MAP-A1"})
        self.assertEqual(doc["reset"], {"pairing": True, "labels": False, "map": True})
        self.assertNotIn("MAP-A1", self.project.data["maps"])
        self.assertIn("MAP-A1", self.project.data["labels"])
        self.assertFalse(self.project.path("inputs/maps/MAP-A1.mmd").exists())
        self.pair()                                                               # transcript alone: B or C
        self.assertEqual(importer.unconfirmed(self.project), {"pairing": False, "labels": [], "maps": []})
        self.assertFalse(importer.map_confirmed(self.project, "MAP-A1"))

    def test_reimport_after_remove_gives_the_same_id_and_asks_again(self):
        self.upload("transcript", "Maria Silva.txt", "\n".join(WHOLE_WORD))
        self.upload("transcript", "Joao Souza.txt", "\n".join(WHOLE_WORD[:4]))
        self.pair()
        self.ok("/api/import/remove", {"kind": "transcript", "pid": "ID-01"})
        self.assertNotIn("Maria", self.project.path("logs/app.jsonl").read_text(encoding="utf-8"))
        again = self.upload("transcript", "Maria Silva.txt", "\n".join(WHOLE_WORD))
        self.assertEqual(again["file"]["pid"], "ID-01")
        self.assertFalse(self.ok_get("/api/pairing")["confirmed"])               # same table, still asks again

    def test_refusals(self):
        self.full_pid()
        n = len(records(self.project))
        self.refused("/api/import/remove", {"kind": "photo", "pid": "MAP-A1"}, 422, "bad_kind")
        self.refused("/api/import/remove", {"kind": "transcript", "pid": "MAP-ZZ"}, 404, "not_imported")
        self.refused("/api/import/remove", {"kind": "transcript"}, 404, "not_imported")
        self.refused("/api/import/remove", {"kind": "transcript", "pid": ["MAP-A1"]}, 404, "not_imported")
        # a path in the body is ignored: only kind and pid are read
        self.refused("/api/import/remove", {"kind": "transcript", "pid": "../../logs/app", "rel": "logs/app.jsonl"},
                     404, "not_imported")
        self.assertEqual(len(records(self.project)), n)
        self.assertTrue(self.project.path("inputs/transcripts/MAP-A1.txt").is_file())

    def test_needs_the_session_the_csrf_token_and_post(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        status, _, _ = self.post("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"}, self.cookie, "wrong")
        self.assertEqual(status, 403)
        status, _, _ = self.raw("POST", "/api/import/remove", b'{"kind": "transcript", "pid": "MAP-A1"}',
                                headers=[("X-SS-CSRF", self.token_csrf), ("Content-Type", "application/json")])
        self.assertEqual(status, 401)
        status, _, _ = self.raw("GET", "/api/import/remove", headers=[("Cookie", self.cookie)])
        self.assertIn(status, (404, 405))
        self.assertTrue(self.project.path("inputs/transcripts/MAP-A1.txt").is_file())

    def test_refused_while_a_run_or_an_estimate_is_going(self):
        self.full_pid()
        n = len(records(self.project))
        self.state.estimate_active = True
        try:
            self.refused("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"}, 409, "run_in_progress")
        finally:
            self.state.estimate_active = False
        from second_scribe.app import jobs
        live = mock.MagicMock(spec=jobs.RunJob)
        live.active = True
        self.state.jobs["run"] = live
        try:
            self.refused("/api/import/remove", {"kind": "map", "pid": "MAP-A1"}, 409, "run_in_progress")
        finally:
            self.state.jobs.clear()
        self.assertEqual(len(records(self.project)), n)
        self.assertTrue(self.project.path("inputs/transcripts/MAP-A1.txt").is_file())
        self.assertTrue(importer.map_confirmed(self.project, "MAP-A1"))

    def test_only_a_copy_directly_in_its_inputs_folder_can_go(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        files = self.project.data["files"]
        meta = files.pop("inputs/transcripts/MAP-A1.txt")
        outside = self.project.path("reference")
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        for rel in ("reference/keep.txt", "inputs/maps/MAP-A1.txt", "inputs/transcripts/sub/MAP-A1.txt",
                    "inputs/transcripts/.hidden"):
            files.clear()
            files[rel] = dict(meta)
            self.refused("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"}, 422, "bad_remove")
        self.assertTrue((outside / "keep.txt").is_file())
        # a link inside inputs/transcripts that leads out of the project is refused too, and its target kept
        target = self.tmp / "elsewhere.txt"
        target.write_text("not the project's", encoding="utf-8")
        link = self.project.path("inputs/transcripts") / "MAP-A1.txt"
        link.unlink()
        os.symlink(target, link)
        files.clear()
        files["inputs/transcripts/MAP-A1.txt"] = dict(meta)
        self.refused("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"}, 422, "bad_remove")
        self.assertTrue(target.is_file())

    def test_a_log_that_refuses_changes_nothing(self):
        self.full_pid()
        copy = self.project.path("inputs/transcripts/MAP-A1.txt")
        data_before = json.dumps(self.project.data, sort_keys=True)
        with mock.patch.object(self.project.log, "append", side_effect=RuntimeError("refusing to extend")):
            with self.assertRaises(RuntimeError):
                importer.remove_file(self.project, "transcript", "MAP-A1")
        self.assertTrue(copy.is_file())
        self.assertEqual([p.name for p in copy.parent.iterdir()], ["MAP-A1.txt"])
        self.assertEqual(json.dumps(self.project.data, sort_keys=True), data_before)

    def test_a_copy_already_gone_from_disk_can_still_be_removed(self):
        self.upload("transcript", "MAP-A1.txt", "\n".join(WHOLE_WORD))
        self.project.path("inputs/transcripts/MAP-A1.txt").unlink()
        self.ok("/api/import/remove", {"kind": "transcript", "pid": "MAP-A1"})
        self.assertEqual(self.project.data["files"], {})
        self.assertEqual(records(self.project)[-1]["record_type"], "removed")


class TestStandard(ImportCase):
    STD = {"participant": "PT", "interviewer": "IV",
           "rules": [{"contains": "OW", "role": "owner"}, {"contains": "OF", "role": "builder"}]}

    def setUp(self):
        super().setUp()
        self.context()

    def test_none_until_saved(self):
        self.upload("transcript", "SYN-OW01.txt", bracket())
        self.assertEqual(self.ok_get("/api/labels/standard"), {"standard": None, "rows": []})

    def test_fills_every_transcript_and_flags_what_it_cannot(self):
        self.upload("transcript", "SYN-OW01.txt", bracket())
        self.upload("transcript", "SYN-OF02.txt", bracket(n=2))
        self.upload("transcript", "SYN-XX03.txt", bracket())                        # no rule matches
        self.upload("transcript", "SYN-OWOF4.txt", bracket())                       # two rules match
        self.upload("transcript", "SYN-OW05.txt", bracket(iv="Q"))                  # no IV label
        n = len(records(self.project))
        doc = self.ok("/api/labels/standard", self.STD)
        self.assertEqual(doc["standard"], self.STD)
        rows = {r["pid"]: r for r in doc["rows"]}
        self.assertEqual(rows["SYN-OW01"], {"pid": "SYN-OW01", "participant": "pt", "interviewer": "iv", "role": "owner",
                                            "turns": {"participant": 3, "interviewer": 3, "unknown": 0}, "problems": []})
        self.assertEqual((rows["SYN-OF02"]["role"], rows["SYN-OF02"]["turns"]["participant"]), ("builder", 2))
        self.assertEqual(rows["SYN-XX03"]["problems"], ["no_rule"])
        self.assertEqual(rows["SYN-OWOF4"]["problems"], ["many_rules"])
        self.assertIsNone(rows["SYN-OWOF4"]["role"])
        self.assertEqual(rows["SYN-OW05"]["problems"], ["interviewer_missing"])
        # saved in private/, not in the project file, and not logged (nothing is confirmed by saving it)
        saved = json.loads(self.project.path(importer.STANDARD).read_text(encoding="utf-8"))
        self.assertEqual(saved, {"schema": "label-standard/1", **self.STD})
        self.assertNotIn("label_standard", json.dumps(self.project.data))
        self.assertEqual(len(records(self.project)), n)
        self.assertFalse(self.project.data.get("labels"))

    def test_reused_for_files_imported_later(self):
        self.upload("transcript", "SYN-OW01.txt", bracket())
        self.ok("/api/labels/standard", self.STD)
        self.upload("transcript", "SYN-OF07.txt", bracket())
        rows = {r["pid"]: r for r in self.ok_get("/api/labels/standard")["rows"]}
        self.assertEqual(sorted(rows), ["SYN-OF07", "SYN-OW01"])
        self.assertEqual((rows["SYN-OF07"]["participant"], rows["SYN-OF07"]["role"], rows["SYN-OF07"]["problems"]),
                         ("pt", "builder", []))

    def test_each_confirmation_is_still_its_own_record(self):
        for stem in ("SYN-OW01", "SYN-OW02", "SYN-OF03"):
            self.upload("transcript", f"{stem}.txt", bracket())
        self.pair()
        rows = self.ok("/api/labels/standard", self.STD)["rows"]
        n = len(records(self.project))
        for r in rows:                                    # what "Confirm all shown" sends, one call per transcript
            self.ok("/api/labels/confirm", {"pid": r["pid"], "participant": r["participant"],
                                            "interviewer": r["interviewer"], "role": r["role"]})
        new = records(self.project)[n:]
        self.assertEqual([(x["record_type"], x["payload"]["pid"]) for x in new],
                         [("labels_confirmed", "SYN-OF03"), ("labels_confirmed", "SYN-OW01"),
                          ("labels_confirmed", "SYN-OW02")])
        self.assertEqual({p: v["role"] for p, v in self.project.data["labels"].items()},
                         {"SYN-OF03": "builder", "SYN-OW01": "owner", "SYN-OW02": "owner"})

    def test_the_rule_reads_the_name_as_imported(self):
        self.upload("transcript", "Owner Maria.txt", bracket())                     # pid ID-01, name only in private/
        doc = self.ok("/api/labels/standard", {"participant": "PT", "interviewer": "IV",
                                               "rules": [{"contains": "owner", "role": "owner"}]})
        self.assertEqual(doc["rows"][0]["pid"], "ID-01")
        self.assertEqual(doc["rows"][0]["role"], "owner")
        self.assertNotIn("Maria", json.dumps(doc))
        self.assertNotIn("Maria", self.project.path("logs/app.jsonl").read_text(encoding="utf-8"))

    def test_bad_standards_refused(self):
        self.upload("transcript", "SYN-OW01.txt", bracket())
        for body in ({"participant": "PT", "interviewer": "pt", "rules": []},
                     {"participant": "[PT]", "interviewer": "P.T.", "rules": []},
                     {"participant": "", "interviewer": "IV", "rules": []},
                     {"participant": "PT", "interviewer": "IV\n", "rules": []},
                     {"participant": "PT", "interviewer": "IV", "rules": [{"contains": "OW", "role": "pilot"}]},
                     {"participant": "PT", "interviewer": "IV", "rules": [{"contains": " ", "role": "owner"}]},
                     {"participant": "PT", "interviewer": "IV", "rules": [{"contains": "x" * 41, "role": "owner"}]},
                     {"participant": "PT", "interviewer": "IV", "rules": [{"contains": "OW", "role": "owner"}] * 21},
                     {"participant": "PT", "interviewer": "IV", "rules": "OW=owner"}):
            self.refused("/api/labels/standard", body, 422, "bad_standard")
        self.assertFalse(self.project.path(importer.STANDARD).exists())

    def test_needs_the_session_and_the_csrf_token(self):
        status, _, _ = self.post("/api/labels/standard", self.STD, self.cookie, "wrong")
        self.assertEqual(status, 403)
        status, _, _ = self.raw("GET", "/api/labels/standard")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    import unittest
    unittest.main()
