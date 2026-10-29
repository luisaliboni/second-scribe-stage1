"""F1a: the real-corpus guards are wired into ``pipeline.run`` and the CLI, and fire before any request."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.addr import ip
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.llm import EgressRefused, HTTPClient, ModelConfig, RealGuardRefused
from second_scribe.pipeline import RunConfig, run

OFF = '{"chat_template_kwargs": {"enable_thinking": false}}'


def config(out: Path, kind: str) -> Path:
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = kind
    d.pop("prereg_required", None)
    if kind == "real":
        m = out / "participant_id_map.json"
        m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
        d["corpus"]["participant_id_map"] = str(m)
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


def env(recon_url, judge_url=f"http://{ip(10,0,0,2)}:8001/v1", recon_params=OFF, judge_params=OFF,
        recon_model="mtplx-qwen38-flashnext", judge_model="deepseek-v4-flash-0731"):
    e = {"SS_RECON_URL": recon_url, "SS_RECON_MODEL": recon_model, "SS_RECON_PARAMS": recon_params,
         "SS_JUDGE_URL": judge_url, "SS_JUDGE_MODEL": judge_model, "SS_JUDGE_PARAMS": judge_params}
    return {k: v for k, v in e.items() if v is not None}


class TestWiring(unittest.TestCase):
    def cli(self, cfg, environ, kind_stage="Q"):
        err = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=False), \
                mock.patch("second_scribe.llm._OPENER.open", side_effect=AssertionError("a request was sent")) as uo, \
                mock.patch("second_scribe.llm._resolve", return_value=[f"{ip(104,18,0,1)}"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(cfg), "--stages", kind_stage, "--client", "http"])
        self.assertEqual(uo.call_count, 0)
        return rc, err.getvalue()

    def test_a_real_run_to_a_public_endpoint_is_refused_before_any_work(self):
        out = Path(tempfile.mkdtemp())
        cfg = config(out, "real")
        for url in ("https://openrouter.ai/api/v1", "https://api.openai.com/v1", f"http://{ip(203,0,113,5)}:8000/v1"):
            rc, err = self.cli(cfg, env(url))
            self.assertEqual(rc, 2, url)
            self.assertIn("refused", err)
            self.assertIn("recon", err)
        self.assertFalse((out / "run" / "work").exists())

    def test_a_public_judge_endpoint_is_refused_too(self):
        out = Path(tempfile.mkdtemp())
        # a judge guard is checked by a session that builds a judge client (J stages); a Q-only session does not
        rc, err = self.cli(config(out, "real"), env("http://127.0.0.1:8000/v1", "https://openrouter.ai/api/v1"),
                           kind_stage="J")
        self.assertEqual(rc, 2)
        self.assertIn("judge", err)

    def test_a_real_run_without_thinking_off_is_refused(self):
        out = Path(tempfile.mkdtemp())
        rc, err = self.cli(config(out, "real"), env("http://127.0.0.1:8000/v1", recon_params="{}"))
        self.assertEqual(rc, 2)
        self.assertIn("thinking", err)

    def test_a_qwen_judge_is_refused_on_a_real_run(self):
        out = Path(tempfile.mkdtemp())
        rc, err = self.cli(config(out, "real"), env("http://127.0.0.1:8000/v1", judge_model="qwen3-32b"),
                           kind_stage="J")
        self.assertEqual(rc, 2)
        self.assertIn("Qwen", err)

    def test_the_guard_runs_inside_pipeline_run_for_a_real_corpus_only(self):
        public = HTTPClient(ModelConfig("recon", "https://openrouter.ai/api/v1", "m"))
        for kind, refused in (("real", True), ("synthetic", False)):
            cfg = RunConfig.load(config(Path(tempfile.mkdtemp()), kind))
            (Path(cfg.out_dir) / "logs").mkdir(parents=True)
            log = AuditLog(Path(cfg.out_dir) / "logs" / "run.jsonl", cfg.run_id)
            with mock.patch("second_scribe.llm._resolve", return_value=[f"{ip(104,18,0,1)}"]):
                if refused:
                    with self.assertRaises(RealGuardRefused):
                        run(cfg, [], {"recon": public}, log=log)
                else:
                    run(cfg, [], {"recon": public}, log=log)      # synthetic: a public host is fine

    def test_a_thinking_reply_stops_the_cli_with_a_message(self):
        out = Path(tempfile.mkdtemp())
        cfg = config(out, "synthetic")

        class Resp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}", "reasoning_content": "hmm"},
                                                "finish_reason": "stop"}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        err = io.StringIO()
        with mock.patch.dict(os.environ, env("https://example.org/v1", "https://example.org/v1"), clear=False), \
                mock.patch("second_scribe.llm._OPENER.open", return_value=Resp()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(cfg), "--stages", "Q", "--client", "http"])
        self.assertEqual(rc, 4)
        self.assertIn("thinking", err.getvalue())


if __name__ == "__main__":
    unittest.main()
