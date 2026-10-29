"""F4' (decision L6): a real run does not need a frozen pre-registration. Every run is still hash-chained."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe.audit import verify as audit_verify
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.pipeline import RunConfig
from tests.test_real_run_validity import fake_build_clients


def real_config(out: Path, **top) -> Path:
    """The synthetic demo corpus standing in for real files, kind real, and NO prereg anywhere."""
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = "real"
    d.pop("prereg_required", None)
    m = out / "participant_id_map.json"
    m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
    d["corpus"]["participant_id_map"] = str(m)
    d.update(top)
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


class TestNoPreregGate(unittest.TestCase):
    def test_a_config_that_opts_in_to_the_prereg_check_still_gets_it(self):
        # the optional tool is kept: opting in (prereg_required true) makes the run refuse until a freeze exists
        out = Path(tempfile.mkdtemp())
        cfg = real_config(out, prereg_required=True)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(cfg), "--stages", "Q", "--client", "oracle"])
        self.assertEqual(rc, 2)
        self.assertIn("prereg", err.getvalue())

    def test_a_real_config_loads_with_the_prereg_flag_absent_false_or_null(self):
        out = Path(tempfile.mkdtemp())
        self.assertFalse(RunConfig.load(real_config(out)).prereg_required)
        for value in (False, None):
            self.assertFalse(bool(RunConfig.load(real_config(out, prereg_required=value)).prereg_required))

    def test_a_real_run_without_a_prereg_runs_and_its_log_verifies(self):
        out = Path(tempfile.mkdtemp())
        cfg = real_config(out)
        err = io.StringIO()
        # a real run's calls must name their model (D-3), so the oracle gets a ModelConfig as a live client has
        env = {"SS_RECON_MODEL": "alpha-recon-1", "SS_JUDGE_MODEL": "beta-judge-1"}
        with mock.patch.dict(os.environ, env), mock.patch("second_scribe.cli.build_clients", fake_build_clients), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 0, err.getvalue())
        log = out / "run" / "logs" / "run.jsonl"
        self.assertTrue(log.exists())
        self.assertEqual(audit_verify.main([str(log)]), 0)
        types = [json.loads(line)["record_type"] for line in log.read_text().splitlines()]
        self.assertIn("run_start", types)
        self.assertIn("call_request", types)
        self.assertFalse((out / "run" / "prereg.json").exists())


if __name__ == "__main__":
    unittest.main()
