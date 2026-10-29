"""I-F1-5: a real run is stamped and checked for validity (items name the calls that made them; one model and one
set of inference params per role) whether or not a pre-registration is in play. This is NOT a pre-registration
lock: no freeze, no frozen rules, no prereg file. Synthetic corpus and fake clients only."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import prereg
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients, main
from second_scribe.llm import ModelConfig

THINK_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


def real_config(out: Path) -> Path:
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = "real"
    d.pop("prereg_required", None)
    m = out / "participant_id_map.json"
    m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
    d["corpus"]["participant_id_map"] = str(m)
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


class CfgClient:
    """The offline oracle with a ModelConfig, so the log records a model id and params as for a live client."""

    def __init__(self, inner, role, model_id, params):
        self.inner = inner
        self.cfg = ModelConfig(name=role, endpoint="http://127.0.0.1:9/v1", model_id=model_id, params=params)

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        return self.inner.complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens)


def fake_build_clients(kind, cfg, fixtures=None, roles=None):
    """Oracle answers; model ids come from SS_RECON_MODEL / SS_JUDGE_MODEL, params from SS_RECON_EXTRA."""
    base = build_clients("oracle", cfg, fixtures, roles=roles)
    extra = json.loads(os.environ.get("SS_RECON_EXTRA", "{}"))
    return {r: CfgClient(c, r, os.environ[f"SS_{r.upper()}_MODEL"],
                         {**THINK_OFF, **(extra if r == "recon" else {})}) for r, c in base.items()}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.cfg = real_config(self.out)
        self.log = self.out / "run" / "logs" / "run.jsonl"
        self.work = self.out / "run" / "work"
        env = {"SS_RECON_MODEL": "alpha-recon-1", "SS_JUDGE_MODEL": "beta-judge-1"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("SS_RECON_EXTRA", None)

    def cli(self, *argv):
        err = io.StringIO()
        with mock.patch("second_scribe.cli.build_clients", fake_build_clients), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, err.getvalue()

    def run_all(self, stages="all"):
        return self.cli("run", "--config", str(self.cfg), "--stages", stages, "--client", "oracle")


class TestStamping(Base):
    def test_a_real_run_with_prereg_off_stamps_every_item(self):
        rc, err = self.run_all()
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.out / "run" / "prereg.json").exists())
        items = sorted(self.work.glob("*/*.json"))
        self.assertTrue(items)
        calls = {json.loads(l)["payload"]["request_id"] for l in self.log.read_text().splitlines()
                 if json.loads(l)["record_type"] == "call_request"}
        self.assertTrue(calls)
        for f in items:
            d = json.loads(f.read_text())
            self.assertIsInstance(d.get("request_ids"), list, f.name)
            self.assertTrue(set(d["request_ids"]) <= calls, f.name)
        recon = [json.loads(f.read_text()) for f in (self.work / "recon").glob("*.json")]
        self.assertTrue(recon and all(d["request_ids"] for d in recon))

    def test_a_synthetic_run_is_left_alone(self):
        d = json.loads(self.cfg.read_text())
        d["corpus"]["kind"] = "synthetic"
        d["corpus"].pop("participant_id_map", None)
        self.cfg.write_text(json.dumps(d))
        rc, err = self.run_all("Q")
        self.assertEqual(rc, 0, err)
        rc, err = self.run_all("J")                    # the off-record check is a judge stage
        self.assertEqual(rc, 0, err)
        item = next((self.work / "in_transcript").glob("*.json"))
        self.assertNotIn("request_ids", json.loads(item.read_text()))


class TestVerifyCatchesEditedWork(Base):
    def test_hand_edited_work_item_makes_verify_fail(self):
        self.assertEqual(self.run_all()[0], 0)
        rc, _ = self.cli("verify", str(self.log))
        self.assertEqual(rc, 0)                        # untouched: verify passes, so the next failure is real
        f = next((self.work / "recon").glob("*.json"))
        d = json.loads(f.read_text())
        d["request_ids"] = ["forged:call:1:deadbeef"]
        f.write_text(json.dumps(d))
        rc, _ = self.cli("verify", str(self.log))
        self.assertEqual(rc, 1)

    def test_an_item_with_its_stamp_removed_fails_the_post_run_verify(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J")[0], 0)      # the off-record check is a judge stage
        f = next((self.work / "in_transcript").glob("*.json"))
        d = json.loads(f.read_text())
        d.pop("request_ids")
        f.write_text(json.dumps(d))
        rc, _ = self.cli("report", "--config", str(self.cfg))
        self.assertEqual(rc, 1)

    def test_a_log_with_two_models_for_one_role_is_flagged(self):
        d = Path(self._tmp.name) / "synth"
        (d / "work").mkdir(parents=True)
        (d / "logs").mkdir()
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        log.append("run_start", {"corpus_kind": "real"})
        for i, model in enumerate(("m-a", "m-b")):
            log.append("call_request", {"request_id": f"recon:full:p:{i}:x", "stage": "recon:full", "item_id": "p",
                                        "model_id": model, "params": THINK_OFF})
        log.close()
        self.assertIn("recon", prereg.run_validity_problem(d / "logs" / "run.jsonl", d / "work"))


class TestResumeRefusesAChangedInstrument(Base):
    def test_resume_with_a_different_recon_model_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        os.environ["SS_RECON_MODEL"] = "alpha-recon-2"
        rc, err = self.run_all("Q")
        self.assertNotEqual(rc, 0)
        self.assertIn("refusing to resume", err)
        self.assertIn("recon", err)

    def test_resume_with_different_params_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        os.environ["SS_RECON_EXTRA"] = json.dumps({"top_k": 5})
        rc, err = self.run_all("Q")
        self.assertNotEqual(rc, 0)
        self.assertIn("refusing to resume", err)

    def test_resume_with_the_same_settings_continues(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        rc, err = self.run_all("all")
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
