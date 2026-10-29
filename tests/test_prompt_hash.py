"""A2 prompt hash: the prompt-set hash is computed from the prompt files when the run starts (not read back from
MANIFEST.json), a real run refuses to start when it differs from the manifest, and the computed hash is printed in
the aggregates. Synthetic corpus and fake clients only."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import pipeline, prompt_manifest
from second_scribe.pipeline import KIT_ROOT
from tests.test_real_run_validity import Base as RealBase


class PromptsCopy(unittest.TestCase):
    def setUp(self):
        self._pt = tempfile.TemporaryDirectory()
        self.addCleanup(self._pt.cleanup)
        self.prompts = Path(self._pt.name) / "prompts"
        shutil.copytree(KIT_ROOT / "prompts", self.prompts)
        p = mock.patch.object(pipeline, "PROMPTS_DIR", self.prompts)
        p.start()
        self.addCleanup(p.stop)

    def edit(self, name="align.v2.txt"):
        f = self.prompts / name
        f.write_text(f.read_text() + "\nEdited after the freeze.\n")


class TestComputedHash(PromptsCopy):
    def test_the_hash_is_computed_from_the_files_not_read_from_the_manifest(self):
        recorded = json.loads((self.prompts / "MANIFEST.json").read_text())["set_sha256"]
        self.assertEqual(pipeline.prompt_set_sha256(), recorded)             # honest copy: same
        self.edit()
        self.assertNotEqual(pipeline.prompt_set_sha256(), recorded)          # edited: the computed one moves
        self.assertEqual(pipeline.prompt_set_sha256(), prompt_manifest.build_manifest(self.prompts)["set_sha256"])

    def test_a_problem_is_reported_for_an_edit_a_new_file_and_a_stale_set_hash(self):
        self.assertIsNone(pipeline.prompt_manifest_problem())
        self.edit()
        self.assertIn("align.v2.txt", pipeline.prompt_manifest_problem())
        (self.prompts / "new.v1.txt").write_text("new prompt")
        self.assertIn("new.v1.txt", pipeline.prompt_manifest_problem())

    def test_a_manifest_whose_set_hash_was_edited_by_hand_is_a_problem(self):
        m = json.loads((self.prompts / "MANIFEST.json").read_text())
        m["set_sha256"] = "0" * 64
        (self.prompts / "MANIFEST.json").write_text(json.dumps(m))
        self.assertIn("set_sha256", pipeline.prompt_manifest_problem())


class TestRealRunRefuses(PromptsCopy, RealBase):
    def setUp(self):
        PromptsCopy.setUp(self)
        RealBase.setUp(self)

    def test_a_real_run_with_an_edited_prompt_refuses_before_any_call(self):
        self.edit()
        rc, err = self.run_all("Q")
        self.assertEqual(rc, 2, err)
        self.assertIn("prompt", err)
        self.assertIn("MANIFEST", err)
        calls = ([l for l in self.log.read_text().splitlines() if '"call_request"' in l]
                 if self.log.exists() else [])
        self.assertEqual(calls, [])

    def test_a_real_run_with_the_frozen_prompts_runs_and_prints_the_computed_hash(self):
        rc, err = self.run_all("all")
        self.assertEqual(rc, 0, err)
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["prompt_set_sha256"], prompt_manifest.build_manifest(self.prompts)["set_sha256"])
        self.assertTrue(agg["prompt_manifest_ok"])
        start = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(start["payload"]["prompt_set_sha256"], agg["prompt_set_sha256"])


class TestSyntheticRunReportsButDoesNotRefuse(PromptsCopy):
    def test_a_synthetic_run_with_an_edited_prompt_runs_and_flags_it(self):
        import contextlib
        import io
        from second_scribe.cli import DEMO_CONFIG, main
        self.edit()
        out = Path(self._pt.name) / "demo"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = main(["demo", "--out", str(out), "--client", "oracle"])
        self.assertEqual(rc, 0)
        agg = json.loads((out / "aggregates.json").read_text())
        self.assertFalse(agg["prompt_manifest_ok"])
        self.assertEqual(agg["prompt_set_sha256"], prompt_manifest.build_manifest(self.prompts)["set_sha256"])


if __name__ == "__main__":
    unittest.main()
