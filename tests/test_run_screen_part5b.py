"""Part 5b phase 2 (23 Oct): what the Run screen reads from the server.

- ``GET /api/runs/ready``: the readiness list (counts per Setup item) and, per reference group, whether Start would go
  ahead, with the refusal that would stop it (item 39: Start never fails silently).
- ``RunJob.status``: ``left_s`` (about how long the model stages still need) and ``left_out`` (items the model server
  never answered, once the run has finished; scout F14).
- ``LogTail.per_call``: the seconds per answered call of the current session.

No model is contacted: the projects are built as ``tests/test_orchestrate`` builds them (the model check is a stored
result), and the job tests set the job's counters by hand.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from second_scribe import report
from second_scribe.app import api, jobs
from second_scribe.pipeline import RunConfig
from tests import test_orchestrate as torch
from tests.realpath_corpus import realpath_config


class _Ready(torch.Base):
    def ready(self) -> dict:
        api.load_route_modules()
        state = api.AppState(project=self.project)
        return api.dispatch(state, "GET", "/api/runs/ready", {}, {})


class TestReadyAllSet(_Ready):
    def test_every_item_is_ready_and_every_group_can_start(self):
        doc = self.ready()
        self.assertEqual([i["key"] for i in doc["items"]],
                         ["context", "pairing", "labels", "maps", "references", "coding", "models", "model_check"])
        self.assertTrue(all(i["ok"] for i in doc["items"]), doc["items"])
        by = {i["key"]: i for i in doc["items"]}
        self.assertEqual((by["labels"]["done"], by["labels"]["total"]), (4, 4))
        self.assertEqual((by["references"]["done"], by["references"]["total"]), (4, 4))
        self.assertEqual((by["coding"]["done"], by["coding"]["total"]), (1, 1))
        self.assertEqual(by["model_check"]["state"], "passed")
        self.assertEqual(doc["groups"], {"A": {"ready": True}, "B": {"ready": True}, "C": {"ready": True}})

    def test_item_39_labels_unconfirmed_names_the_step_before_any_click(self):
        self.project.data["labels"] = {}
        self.project.save()
        doc = self.ready()
        by = {i["key"]: i for i in doc["items"]}
        self.assertEqual((by["labels"]["ok"], by["labels"]["done"], by["labels"]["total"]), (False, 0, 4))
        for g in ("A", "B", "C"):
            with self.subTest(group=g):
                self.assertEqual((doc["groups"][g]["ready"], doc["groups"][g]["code"], doc["groups"][g]["item"]),
                                 (False, "labels_unconfirmed", "labels"))
                self.assertIsInstance(doc["groups"][g]["message"], str)

    def test_a_failed_model_check_blocks_every_group(self):
        torch.pass_model_check(self.project, passed=False)
        doc = self.ready()
        by = {i["key"]: i for i in doc["items"]}
        self.assertEqual((by["model_check"]["ok"], by["model_check"]["state"]), (False, "failed"))
        self.assertEqual({g: (v["code"], v["item"]) for g, v in doc["groups"].items()},
                         {g: ("model_check_failed", "model_check") for g in "ABC"})

    def test_the_answer_holds_counts_and_codes_only(self):
        text = json.dumps(self.ready())
        for pid in torch.SOURCES:                      # the refusal messages may name a pid; a ready project names none
            self.assertNotIn(pid, text)
        self.assertNotIn("payload", text)


class TestReadyBOpen(_Ready):
    lock_b = False

    def test_an_open_b_reading_blocks_b_only(self):
        doc = self.ready()
        by = {i["key"]: i for i in doc["items"]}
        self.assertEqual((by["coding"]["ok"], by["coding"]["done"], by["coding"]["total"]), (False, 0, 1))
        self.assertEqual(doc["groups"]["A"], {"ready": True})
        self.assertEqual((doc["groups"]["B"]["code"], doc["groups"]["B"]["item"]), ("coding_unlocked", "coding"))


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class TestPerCall(unittest.TestCase):
    T0 = 1_800_000_000.0

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-part5b-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "run.jsonl"

    def log(self, *recs):
        with open(self.path, "a", encoding="utf-8") as fh:
            for kind, dt, payload in recs:
                fh.write(json.dumps({"record_type": kind, "seq": 0, "ts": _ts(self.T0 + dt), "payload": payload}) + "\n")

    def calls(self, start, n, step, prefix):
        out = []
        for i in range(n):
            rid = f"{prefix}{i}"
            out += [("call_request", start + i * step, {"request_id": rid, "stage": "recon:removed"}),
                    ("call_response", start + (i + 1) * step, {"request_id": rid})]
        return out

    def test_seconds_per_answer_of_this_session_after_three_answers(self):
        self.log(("run_start", 0, {}), *self.calls(0, 2, 10, "a"))
        tail = jobs.LogTail(self.path, started=self.T0).read()
        self.assertIsNone(tail.per_call())                         # two answers: too few
        self.log(*self.calls(20, 1, 10, "b"))
        self.assertEqual(tail.read().per_call(), 10.0)             # 30 s for 3 answers
        self.log(("run_start", 1000, {}), *self.calls(1000, 3, 2, "c"))
        self.assertEqual(tail.read().per_call(), 2.0)              # a resumed session counts on its own


class TestLeftAndLeftOut(unittest.TestCase):
    """``left_s`` and ``left_out`` on a job whose counters are set by hand (no child process runs)."""

    class _Tail:
        def __init__(self, q, j, per):
            self.counts, self._per = {"Q": q, "J": j}, per

        def per_call(self, least=3):
            return self._per

    class _Child:
        rc = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-part5b-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.project = torch.Project.create(root / "proj", "Study")
        self.cfg = realpath_config(self.project.root, "A-0a1b2c3d", corpus_dir=root / "corpus")
        self.job = jobs.RunJob(self.project, self.cfg, env={})

    def running(self, stage, q, j, per, totals=None):
        job = self.job
        job.state, job.stage, job.child = "running", stage, self._Child()
        job.tail = self._Tail(q, j, per)
        job._totals = totals if totals is not None else {"Q": 20, "J": 30}
        return job

    def test_left_counts_the_calls_still_to_come_at_this_sessions_pace(self):
        self.assertEqual(self.running("Q", 5, 0, 10.0).left_s(), (15 + 30) * 10)
        self.job.done_stages = ["Q"]
        self.assertEqual(self.running("J", 20, 10, 4.0).left_s(), 20 * 4)
        self.assertIsNone(self.running("Q", 1, 0, None).left_s())          # too few answers yet
        self.assertIsNone(self.running("M", 20, 30, 4.0).left_s())         # matching calls no model
        self.assertIsNone(self.running("Q", 5, 0, 10.0, totals={}).left_s())   # no total, no guess
        self.job.state = "paused"
        self.assertIsNone(self.job.left_s())

    def test_ai_maps_only_has_no_judge_part(self):
        job = jobs.RunJob(self.project, self.cfg, env={}, stages=("Q", "M"))
        job.state, job.stage, job.child = "running", "Q", self._Child()
        job.tail, job._totals = self._Tail(5, 0, 10.0), {"Q": 20, "J": 0}
        self.assertEqual(job.left_s(), 150)

    def test_left_out_reads_the_finished_runs_outage_count(self):
        self.assertIsNone(self.job.left_out())                             # not finished
        path = report.aggregates_path(RunConfig.load(self.cfg))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"outage": {"timeout_excluded_items": 2, "by_stage": {"recon": 2}}}), encoding="utf-8")
        self.job.finished = True
        self.assertEqual(self.job.left_out(), 2)
        self.assertEqual(self.job.status()["left_out"], 2)
        other = jobs.RunJob(self.project, self.cfg, env={})
        other.finished = True
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(other.left_out())                                # unreadable: no line, never a wrong one


if __name__ == "__main__":
    unittest.main()
