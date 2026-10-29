"""I-3: the recon:<rung> work stores are covered by the real-run validity check (who owns which call), and a reused
rung-2 draw is an explicit kind of item, not a byte copy. Real Work + RequestTracker, a run_start with corpus_kind real,
fake clients, synthetic transcripts only."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe import ladder, pipeline, prereg
from second_scribe.audit.registry import AuditLog
from second_scribe.reconstruct import ReconSpec, reconstruct

from tests.test_ladder_wiring import CTX, GOOD, PROMPTS, RecordingClient, T, transcript

SPEC = ReconSpec("removed", "p0", 0.0)
NAME = "A1__removed__p0__t0__r0"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-own-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        (self.out / "logs").mkdir()
        self.log_path = self.out / "logs" / "run.jsonl"
        self.log = AuditLog(self.log_path, "r", clock=lambda: "t")
        self.log.append("run_start", {"corpus_kind": "real"})
        self.tracker = prereg.RequestTracker(self.log)
        self.work = pipeline.Work(self.out, tracker=self.tracker)

    def problem(self):
        return prereg.run_validity_problem(self.log_path, self.out / "work")

    def main_run_item(self, pid="A1"):
        """What the pipeline's recon stage stores for the primary draw 0 (unsuffixed id, stage recon:removed)."""
        name = f"{pid}__removed__p0__t0__r0"
        g, st = reconstruct(self.tracker, RecordingClient(), transcript(pid), SPEC, CTX, PROMPTS, item_id=name)
        self.work.put("recon", name, {"status": st, "map": None if g is None else json.loads(json.dumps(
            __import__("dataclasses").asdict(g)))})
        return name

    def ladder_arm(self, rung_key, pids=("A1",), draws=1, client=None):
        return ladder.run_ladder_arm(ladder.get_rung(rung_key), [transcript(p) for p in pids], SPEC, draws,
                                     log=self.tracker, client=client or RecordingClient(), ctx=CTX,
                                     prompts_dir=PROMPTS, work=self.work, real_data=True)


class TestGenuineLadderItems(Base):
    def test_a_ladder_run_passes_the_validity_check(self):
        self.main_run_item()
        for k in ("rung3", "placebo", "rung1"):
            self.ladder_arm(k, pids=("A1", "B2"), draws=1)       # one draw: the primary setting is T=0 (DECISIONS C7)
        self.assertIsNone(self.problem())

    def test_a_ladder_item_that_names_no_call_is_refused(self):
        self.ladder_arm("rung3")
        f = self.out / "work" / "recon:rung3" / f"{NAME}.json"
        d = json.loads(f.read_text())
        d["request_ids"] = []
        f.write_text(json.dumps(d))
        self.assertIn("recon:rung3", self.problem() or "")

    def test_a_ladder_item_cannot_name_another_rungs_call(self):
        self.ladder_arm("rung3")
        self.ladder_arm("placebo")
        src = self.out / "work" / "recon:placebo" / f"{NAME}.json"
        dst = self.out / "work" / "recon:rung3" / f"{NAME}.json"
        dst.write_text(src.read_text())            # rung 3's item now names the placebo's call
        self.assertIn("recon:rung3", self.problem() or "")


class TestMainRunItemCopiedIntoALadderStore(Base):
    def test_a_byte_copy_of_a_main_run_item_is_refused(self):
        name = self.main_run_item()
        self.assertIsNone(self.problem())
        (self.out / "work" / "recon:rung2").mkdir()
        shutil.copy(self.out / "work" / "recon" / f"{name}.json", self.out / "work" / "recon:rung2" / f"{name}.json")
        self.assertIn("recon:rung2", self.problem() or "")


class TestReusedRung2Draw(Base):
    def reuse(self, names=(NAME,)):
        return ladder.preseed_rung2(self.work, PROMPTS, names)

    def test_reused_item_is_explicit_and_passes(self):
        name = self.main_run_item()
        self.assertEqual(self.reuse(), [name])
        item = json.loads((self.out / "work" / "recon:rung2" / f"{name}.json").read_text())
        main = json.loads((self.out / "work" / "recon" / f"{name}.json").read_text())
        self.assertEqual(item["reused_from"], {"stage": "recon", "name": name})
        self.assertEqual(item["request_ids"], main["request_ids"])
        self.assertEqual(item["status"], main["status"])
        self.assertIsNone(self.problem())

    def test_run_ladder_arm_uses_the_reused_item_without_a_call(self):
        self.main_run_item()
        self.reuse()
        client = RecordingClient()
        res = self.ladder_arm("rung2", client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(res[("A1", 0)].status, "ok")
        self.assertIsNone(self.problem())

    def test_a_reused_item_with_other_request_ids_is_refused(self):
        name = self.main_run_item()
        other = self.main_run_item("B2")
        self.reuse([name])
        f = self.out / "work" / "recon:rung2" / f"{name}.json"
        d = json.loads(f.read_text())
        d["request_ids"] = json.loads((self.out / "work" / "recon" / f"{other}.json").read_text())["request_ids"]
        f.write_text(json.dumps(d))
        self.assertIn("recon:rung2", self.problem() or "")

    def test_a_reused_item_without_its_source_is_refused(self):
        name = self.main_run_item()
        self.reuse()
        (self.out / "work" / "recon" / f"{name}.json").unlink()
        self.assertIn("recon:rung2", self.problem() or "")

    def test_only_rung2_may_reuse(self):
        name = self.main_run_item()
        self.reuse()
        d = self.out / "work" / "recon:rung3"
        d.mkdir()
        shutil.copy(self.out / "work" / "recon:rung2" / f"{name}.json", d / f"{name}.json")
        self.assertIn("recon:rung3", self.problem() or "")

    def test_a_reused_item_does_not_claim_the_call_twice(self):
        name = self.main_run_item()
        self.reuse()
        self.assertIsNone(self.problem())          # the call is named by the source and by the reuse, no double claim

    def test_items_the_main_run_has_not_done_are_skipped(self):
        self.assertEqual(self.reuse(), [])


class FailsFirst:
    """Fake client: the first attempt of every item is an invalid reply (one corrective retry), then a good map."""

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        from second_scribe.llm import ModelResponse
        first = len(messages) <= 2
        return ModelResponse("Sure!" if first else json.dumps(GOOD), "stop", {}, 0, "x")


class TestRetriesOfAReusedRung2Draw(Base):
    """I2 / M-1: the retries of a rung-2 draw taken from the main run were logged unsuffixed; they must be found."""

    def setUp(self):
        super().setUp()
        self.rung2 = ladder.get_rung("rung2")
        self.name = "SYN-OW01__removed__p0__t0__r0"
        g, st = reconstruct(self.tracker, FailsFirst(), transcript("SYN-OW01"), SPEC, CTX, PROMPTS, item_id=self.name)
        self.assertEqual(st, "ok")
        self.work.put("recon", self.name, {"status": st, "map": None})
        ladder.preseed_rung2(self.work, PROMPTS, [self.name])

    def results(self, draws=1, client=None, pids=("SYN-OW01",)):
        return ladder.run_ladder_arm(self.rung2, [transcript(p) for p in pids], SPEC, draws, log=self.tracker,
                                     client=client or RecordingClient(), ctx=CTX, prompts_dir=PROMPTS, work=self.work,
                                     real_data=True)

    def test_the_logs_hold_the_retry_under_the_unsuffixed_id(self):
        self.assertEqual(ladder.retries_by_call(self.log_path)[("recon:removed", self.name)], 1)
        self.assertNotIn(("recon:removed:rung2", self.name + "@rung2"), ladder.retries_by_call(self.log_path))

    def test_arm_retries_finds_the_reused_draws_retry(self):
        res = self.results()
        self.assertEqual(ladder.arm_retries(self.log_path, res, self.rung2), {("SYN-OW01", 0): 1})

    def test_a_fresh_rung2_draw_is_found_by_its_suffixed_id_and_not_added_twice(self):
        # one draw per map (T=0, DECISIONS C7): SYN-OW01 draw 0 is the reused main-run item, B2 draw 0 is a fresh call
        res = self.results(client=FailsFirst(), pids=("SYN-OW01", "B2"))
        r = ladder.arm_retries(self.log_path, res, self.rung2)
        self.assertEqual(r, {("SYN-OW01", 0): 1, ("B2", 0): 1})

    def test_other_rungs_do_not_fall_back_to_the_main_runs_call(self):
        res = ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("SYN-OW01")], SPEC, 1, log=self.tracker,
                                    client=RecordingClient(), ctx=CTX, prompts_dir=PROMPTS, work=self.work,
                                    real_data=True)
        self.assertEqual(ladder.arm_retries(self.log_path, res, ladder.get_rung("rung3")), {("SYN-OW01", 0): 0})

    def test_draw_retries_is_the_single_lookup_the_report_uses(self):
        calls = ladder.retries_by_call(self.log_path)
        self.assertEqual(ladder.draw_retries(calls, self.name, self.rung2), 1)
        self.assertEqual(ladder.draw_retries(calls, "B2__removed__p0__t0__r0", self.rung2), 0)


if __name__ == "__main__":
    unittest.main()
