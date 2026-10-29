"""Integration of wire-a (ladder stores, prereg ownership) with f1b (every Work write is announced in the log with
its sha256, and verify checks the store against those records). A ladder run and a reused rung-2 draw, on a real
Work that has a log, must pass BOTH checks. Fake clients, synthetic transcripts, temp dirs only."""
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe import ladder, pipeline, prereg
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from second_scribe.reconstruct import ReconSpec, reconstruct

from tests.test_ladder_wiring import CTX, PROMPTS, RecordingClient, transcript

SPEC = ReconSpec("removed", "p0", 0.0)


class ModelClient(RecordingClient):
    """A fake that names its model, as a real run's client does (D-3 wants a model id on every role call)."""
    cfg = types.SimpleNamespace(model_id="alpha-qwen-1", api_key_env=None, params={}, endpoint=None)


class TestLadderStoresPassTheWorkIntegrityCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-lwi-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        (self.out / "logs").mkdir()
        self.log_path = self.out / "logs" / "run.jsonl"
        self.log = AuditLog(self.log_path, "r", clock=lambda: "t")
        self.log.append("run_start", {"corpus_kind": "real"})
        self.tracker = prereg.RequestTracker(self.log)
        self.work = pipeline.Work(self.out, self.tracker, log=self.log)     # as pipeline.Runner builds it

    def problems(self):
        return (prereg.run_validity_problem(self.log_path, self.out / "work"),
                audit_verify.work_integrity_problem(self.log_path, self.out / "work"))

    def main_item(self, pid="A1"):
        name = f"{pid}__removed__p0__t0__r0"
        g, st = reconstruct(self.tracker, ModelClient(), transcript(pid), SPEC, CTX, PROMPTS, item_id=name)
        import dataclasses
        self.work.put("recon", name, {"status": st, "map": dataclasses.asdict(g) if g else None})
        return name

    def test_a_ladder_run_passes_both_checks(self):
        self.main_item()
        for k in ("rung3", "placebo", "rung1"):
            ladder.run_ladder_arm(ladder.get_rung(k), [transcript("A1")], SPEC, 1, log=self.tracker,
                                  client=ModelClient(), ctx=CTX, prompts_dir=PROMPTS, work=self.work,
                                  real_data=True)
        self.assertEqual(self.problems(), (None, None))

    def test_a_reused_rung2_draw_passes_both_checks(self):
        name = self.main_item()
        self.assertEqual(ladder.preseed_rung2(self.work, PROMPTS, [name]), [name])
        self.assertEqual(self.problems(), (None, None))


if __name__ == "__main__":
    unittest.main()
