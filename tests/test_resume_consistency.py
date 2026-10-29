"""Review fixes (F1b round 1): a real run may not mix two prompt sets or two configs across its sessions (I-2, M-4),
verify fails a real log whose run_start records disagree, and a run that sends anything to a live endpoint refuses
edited prompts even on a synthetic corpus (M-6). Synthetic / fake clients only."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import pipeline, prereg, prompt_manifest
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.pipeline import KIT_ROOT
from tests.test_real_run_validity import Base as RealBase
from tests.test_real_run_validity import fake_build_clients


class PromptsCopy(unittest.TestCase):
    def setUp(self):
        self._pt = tempfile.TemporaryDirectory()
        self.addCleanup(self._pt.cleanup)
        self.prompts = Path(self._pt.name) / "prompts"
        shutil.copytree(KIT_ROOT / "prompts", self.prompts)
        p = mock.patch.object(pipeline, "PROMPTS_DIR", self.prompts)
        p.start()
        self.addCleanup(p.stop)

    def edit_and_regenerate(self, name="judge_modality.v2.txt"):
        f = self.prompts / name
        f.write_text(f.read_text() + "\nA line added between two sessions.\n")
        prompt_manifest.write_manifest(self.prompts)       # what the refusal message itself tells the user to do


class TestRealResume(PromptsCopy, RealBase):
    def setUp(self):
        PromptsCopy.setUp(self)
        RealBase.setUp(self)

    def n_calls(self):
        return sum('"call_request"' in l for l in self.log.read_text().splitlines())

    def test_a_resume_after_a_prompt_edit_with_a_regenerated_manifest_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        before = self.n_calls()
        self.edit_and_regenerate()
        self.assertIsNone(pipeline.prompt_manifest_problem())             # the manifest now matches the files
        rc, err = self.run_all("J,M")
        self.assertEqual(rc, 2, err)
        self.assertIn("prompt", err)
        self.assertIn("new out_dir", err)
        self.assertEqual(self.n_calls(), before)                          # refused before any call

    def test_a_resume_with_a_changed_config_is_refused(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        d = json.loads(self.cfg.read_text())
        d["stats"] = {**d.get("stats", {}), "B": 77}
        self.cfg.write_text(json.dumps(d))
        rc, err = self.run_all("J,M")
        self.assertEqual(rc, 2, err)
        self.assertIn("config", err)
        self.assertIn("new out_dir", err)

    def test_the_same_prompts_and_config_resume(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J,M")[0], 0)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)


class TestVerifyFailsADisagreeingLog(RealBase):
    def log_with(self, starts):
        d = self.out / "hand"
        (d / "work").mkdir(parents=True)
        (d / "logs").mkdir()
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        for s in starts:
            log.append("run_start", {"corpus_kind": "real", **s})
        log.close()
        return d / "logs" / "run.jsonl", d / "work"

    def test_two_prompt_sets_in_one_real_log_fail_run_validity(self):
        log, work = self.log_with([{"prompt_set_sha256": "a" * 64, "config_sha256": "c" * 64},
                                   {"prompt_set_sha256": "b" * 64, "config_sha256": "c" * 64}])
        self.assertIn("prompt", prereg.run_validity_problem(log, work))

    def test_two_configs_in_one_real_log_fail_run_validity(self):
        log, work = self.log_with([{"prompt_set_sha256": "a" * 64, "config_sha256": "c" * 64},
                                   {"prompt_set_sha256": "a" * 64, "config_sha256": "d" * 64}])
        self.assertIn("config", prereg.run_validity_problem(log, work))

    def test_agreeing_run_starts_pass(self):
        log, work = self.log_with([{"prompt_set_sha256": "a" * 64, "config_sha256": "c" * 64}] * 2)
        self.assertIsNone(prereg.run_validity_problem(log, work))


class TestEditedPromptsAreRefusedForAnyLiveClient(PromptsCopy):
    def run_demo(self, builder):
        f = self.prompts / "align.v2.txt"
        f.write_text(f.read_text() + "\nEdited.\n")
        out = Path(self._pt.name) / "demo"
        err = io.StringIO()
        env = {"SS_RECON_MODEL": "alpha-recon-1", "SS_JUDGE_MODEL": "beta-judge-1"}
        with mock.patch.dict(os.environ, env), mock.patch("second_scribe.cli.build_clients", builder), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["demo", "--out", str(out), "--client", "oracle"])
        return rc, err.getvalue(), out

    def test_a_synthetic_run_with_a_live_client_refuses_edited_prompts(self):
        rc, err, out = self.run_demo(fake_build_clients)          # the fake clients carry a ModelConfig endpoint
        self.assertEqual(rc, 2, err)
        self.assertIn("MANIFEST", err)

    def test_an_offline_synthetic_run_still_runs_and_the_report_header_shows_the_mismatch(self):
        from second_scribe.cli import build_clients
        rc, err, out = self.run_demo(build_clients)
        self.assertEqual(rc, 0, err)
        self.assertIn("prompt files differ from MANIFEST.json", (out / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
