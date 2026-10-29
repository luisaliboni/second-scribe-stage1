"""smoke-q items 2 and 4: the command against a local fake of the Qwen server (no real endpoint, no model: L12/L13).

One full run of the command is made once for the class (the fake answers instantly); its logs, plan and budget are
read by several tests. The refusals and ``--plan`` are tested on their own, with a request counter on the fake and a
patched ``socket.connect`` that fails the test if anything is opened.
"""
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tests.addr import ip
from tests.smoke_q_fake import FakeQwen
from second_scribe import smoke_q
from second_scribe.audit import verify as audit_verify
from second_scribe.cli import main
from second_scribe.pipeline import KIT_ROOT

CONFIG = Path(KIT_ROOT) / "configs" / "smoke-q.json"
PRIMARY_CONFIG = Path(KIT_ROOT) / "configs" / "smoke-q-primary.json"
OFF = '{"chat_template_kwargs": {"enable_thinking": false}}'


def env(url, model="fake-qwen38-flashnext", params=OFF, key_env=None):
    e = {"SS_RECON_URL": url, "SS_RECON_MODEL": model, "SS_RECON_PARAMS": params, "SS_RECON_KEY_ENV": key_env}
    return {k: v for k, v in e.items() if v is not None}


def run_cli(argv, environ):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, environ, clear=False), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def call_requests(log_path):
    """[(stage, item_id)] of the first attempt of every call in a log, in order."""
    rows = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("record_type") == "call_request" and rec["payload"]["attempt"] == 1:
            rows.append((rec["payload"]["stage"], rec["payload"]["item_id"]))
    return rows


class TestSmokeQRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-smoke-q-")
        cls.out = (Path(cls._tmp.name) / "out").resolve()
        cls._srv = FakeQwen(delay_s=0.01)
        cls.srv = cls._srv.__enter__()
        cls.rc, cls.stdout, cls.stderr = run_cli(
            ["smoke-q", "--config", str(CONFIG), "--out", str(cls.out)],
            env(cls.srv.base_url, key_env="SS_TEST_SMOKE_TOKEN") | {"SS_TEST_SMOKE_TOKEN": "t-not-a-real-secret"})

    @classmethod
    def tearDownClass(cls):
        cls._srv.__exit__(None, None, None)
        cls._tmp.cleanup()

    def test_the_command_completes_and_both_logs_verify(self):
        self.assertEqual(self.rc, 0, self.stderr + self.stdout)
        for run in ("synthetic", "padded"):
            log = self.out / run / "logs" / "run.jsonl"
            self.assertTrue(log.exists(), run)
            self.assertEqual(audit_verify.main([str(log)]), 0, run)

    def test_the_calls_made_are_exactly_the_planned_calls(self):
        cfg, smoke = smoke_q.load_config(CONFIG, out_dir=str(self.out / "synthetic"))
        padded = smoke_q.prepare_padded(cfg, smoke, self.out)
        for run, c in (("synthetic", cfg), ("padded", padded)):
            planned = [(x.stage, x.item_id) for x in smoke_q.plan_calls(c, run)]
            self.assertEqual(call_requests(self.out / run / "logs" / "run.jsonl"), planned, run)

    def test_every_request_sent_thinking_off_and_the_token_only_in_the_header(self):
        self.assertTrue(self.srv.requests)
        for body in self.srv.requests:
            self.assertIs(body["chat_template_kwargs"]["enable_thinking"], False)
        self.assertEqual(set(self.srv.paths), {"/v1/chat/completions"})
        self.assertEqual(set(self.srv.auth), {"Bearer t-not-a-real-secret"})
        for f in self.out.rglob("*"):
            if f.is_file():
                self.assertNotIn(b"t-not-a-real-secret", f.read_bytes(), str(f))

    def test_the_log_records_where_the_data_went_and_nothing_goes_to_a_judge(self):
        hosts = set()
        for run in ("synthetic", "padded"):
            for line in (self.out / run / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("record_type") == "call_request":
                    hosts.add(rec["payload"].get("endpoint_host"))
                    self.assertTrue(rec["payload"]["stage"].split(":")[0] in ("q", "recon", "prior_only"))
        self.assertEqual(len(hosts), 1)
        self.assertTrue(next(iter(hosts)).startswith("127.0.0.1:"))

    def test_the_padded_transcript_has_real_length(self):
        docx = next((self.out / "padded-corpus" / "transcripts").glob("*.docx"))
        from second_scribe.transcripts import read_docx_text
        self.assertGreaterEqual(smoke_q._words(read_docx_text(docx)), 12000)


    def budget(self):
        return json.loads((self.out / "budget.json").read_text(encoding="utf-8"))

    def test_budget_json_has_the_documented_fields(self):
        b = self.budget()
        self.assertEqual(b["schema"], "smoke-q-budget/1")
        self.assertTrue(b["runs_verified"])
        self.assertEqual(b["model_ids"], ["fake-qwen38-flashnext"])
        self.assertEqual(b["warnings"], [])
        self.assertEqual(len(b["endpoint_hosts"]), 1)
        self.assertIn(f"wrote {self.out / 'budget.json'}", self.stdout)
        rows = {(r["run"], r["group"], r["arm"]): r for r in b["measured"]["rows"]}
        for key in (("padded", "mapping_talk", "batch"), ("padded", "ladder", "rung3"), ("padded", "ladder", "placebo"),
                    ("padded", "ladder", "rung1"), ("padded", "main_grid", "removed__p0__t0__r0"),
                    ("padded", "main_grid", "full__p2__t0__r0"), ("padded", "main_grid", "removed__p0__t0.7__r0"),
                    ("synthetic", "prior_only", "p0"), ("synthetic", "ladder", "rung3")):
            self.assertIn(key, rows)
        for r in b["measured"]["rows"]:
            self.assertGreaterEqual(r["n_calls"], 1)
            self.assertGreater(r["latency_ms"]["median"], 0)
            self.assertGreaterEqual(r["latency_ms"]["p90"], r["latency_ms"]["median"])
            self.assertGreater(r["tokens_per_s_median"], 0)             # the fake reports usage like llama-server does
        self.assertNotIn(("padded", "ladder", "rung2"), rows)           # rung 2 draw 0 is the main-grid map, no extra call

    def test_the_rows_count_exactly_the_calls_of_the_plan(self):
        cfg, smoke = smoke_q.load_config(CONFIG, out_dir=str(self.out / "synthetic"))
        padded = smoke_q.prepare_padded(cfg, smoke, self.out)
        for run, c in (("synthetic", cfg), ("padded", padded)):
            self.assertEqual(sum(r["n_calls"] for r in self.budget()["measured"]["rows"] if r["run"] == run),
                             len(smoke_q.plan_calls(c, run)), run)

    def test_the_projection_has_three_options_each_with_a_qwen_and_a_judge_phase(self):
        proj = self.budget()["projection"]
        self.assertEqual(list(proj["options"]), ["full", "without_real_rung1", "minimum"])
        for name, o in proj["options"].items():
            q = o["qwen_phase"]
            self.assertTrue(q["complete"], name)
            self.assertGreater(q["hours_median"], 0)
            self.assertGreaterEqual(q["hours_p90"], q["hours_median"])
            self.assertIn("unmeasured", o["judge_phase"]["label"])
            self.assertGreater(o["judge_phase"]["hours_high"], o["judge_phase"]["hours_low"])
            self.assertIn("estimate", o["total_h"]["note"])
        self.assertGreater(proj["options"]["full"]["qwen_phase"]["hours_median"],
                           proj["options"]["without_real_rung1"]["qwen_phase"]["hours_median"])
        self.assertIn("optional_sensitivity_t07", proj["extras"])

    def test_running_the_same_command_again_makes_no_new_logged_call(self):
        before = {r: len(call_requests(self.out / r / "logs" / "run.jsonl")) for r in ("synthetic", "padded")}
        rc, _o, err = run_cli(["smoke-q", "--config", str(CONFIG), "--out", str(self.out)],
                              env(self.srv.base_url, key_env="SS_TEST_SMOKE_TOKEN") | {"SS_TEST_SMOKE_TOKEN": "x"})
        self.assertEqual(rc, 0, err)
        after = {r: len(call_requests(self.out / r / "logs" / "run.jsonl")) for r in ("synthetic", "padded")}
        self.assertEqual(before, after)


class TestPaddedDraws(unittest.TestCase):
    def test_a_smoke_config_without_padded_draws_runs_one_draw(self):
        """F5 (smoke-q): the padded run is at T=0, where DECISIONS C7 refuses draws above 1 (they would be copies)."""
        cfg, smoke = smoke_q.load_config(CONFIG)
        smoke = {**smoke, "padded": {k: v for k, v in smoke["padded"].items() if k != "draws"}}
        with tempfile.TemporaryDirectory(prefix="ss-padded-") as t:
            padded = smoke_q.prepare_padded(cfg, smoke, t)
        self.assertEqual(padded.ladder["draws"], 1)

    def test_a_smoke_config_without_a_ladder_pads_without_one(self):
        """q27b: a primary-setting-only smoke (ladder {}) must not turn into a ladder of no rungs in the padded run."""
        cfg, smoke = smoke_q.load_config(PRIMARY_CONFIG)
        self.assertEqual(cfg.ladder, {})
        self.assertEqual([s.key() for s in cfg.recon_specs()], ["removed__p0__t0__r0"])
        with tempfile.TemporaryDirectory(prefix="ss-padded-") as t:
            padded = smoke_q.prepare_padded(cfg, smoke, t)
            stages = Counter(c.stage for c in smoke_q.plan_calls(padded, "padded"))
        self.assertEqual(padded.ladder, {})
        self.assertEqual(stages["recon:removed"], 1)
        self.assertFalse(any(":rung" in s or ":placebo" in s for s in stages))


class TestSmokeQPlan(unittest.TestCase):
    def test_plan_lists_endpoint_and_calls_and_opens_no_connection(self):
        with tempfile.TemporaryDirectory(prefix="ss-smoke-q-plan-") as tmp, FakeQwen() as srv:
            out = Path(tmp) / "never-created"

            def boom(self, *a, **k):
                raise AssertionError("--plan opened a connection")
            with mock.patch.object(socket.socket, "connect", boom), \
                    mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("--plan resolved a name")):
                rc, stdout, stderr = run_cli(["smoke-q", "--config", str(CONFIG), "--out", str(out), "--plan"],
                                             env(srv.base_url))
            self.assertEqual(rc, 0, stderr + stdout)
            self.assertEqual(srv.requests, [])
            self.assertEqual(srv.paths, [])
            self.assertFalse(out.exists())                       # a plan writes nothing under --out
            self.assertIn(srv.base_url, stdout)
            self.assertIn("fake-qwen38-flashnext", stdout)
            self.assertIn("synthetic q:mapping_talk", stdout)
            self.assertIn("padded    recon:removed:rung3", stdout)
            self.assertIn("SYN-PAD01__removed__p0__t0__r0@rung3", stdout)
            cfg, smoke = smoke_q.load_config(CONFIG)
            with tempfile.TemporaryDirectory(prefix="ss-smoke-q-pad-") as t2:
                n = len(smoke_q.plan_calls(cfg, "synthetic")) + len(
                    smoke_q.plan_calls(smoke_q.prepare_padded(cfg, smoke, t2), "padded"))
            listed = stdout.split("exact list", 1)[1].splitlines()[1:]
            self.assertEqual(len([x for x in listed if x.strip()]), n)      # one line per planned call

    def test_plan_of_a_primary_only_config_lists_no_ladder(self):
        """q27b: the primary-setting-only smoke plans the main spec and the prior-only maps, and no rung."""
        with tempfile.TemporaryDirectory(prefix="ss-smoke-q-plan-") as tmp:
            rc, stdout, stderr = run_cli(["smoke-q", "--config", str(PRIMARY_CONFIG), "--out", tmp + "/o", "--plan"],
                                         env("http://127.0.0.1:1/v1"))
        self.assertEqual(rc, 0, stderr + stdout)
        self.assertIn("no ladder)", stdout)
        listed = stdout.split("exact list", 1)[1]
        self.assertIn("SYN-PAD01__removed__p0__t0__r0", listed)
        self.assertNotIn("@rung", listed)
        self.assertNotIn("@placebo", listed)
        self.assertNotIn("__full__", listed)

    def test_plan_says_it_would_be_refused_when_the_environment_is_wrong(self):
        with tempfile.TemporaryDirectory(prefix="ss-smoke-q-plan-") as tmp:
            for e, needle in (({}, "SS_RECON_URL is not set"),
                              (env(f"http://{ip(8, 8, 8, 8)}:8142/v1"), "not loopback"),
                              (env("http://127.0.0.1:1/v1", params="{}"), "enable_thinking"),
                              (env("http://127.0.0.1:1/v1", model="deepseek-v4"), "Qwen")):
                with mock.patch.dict(os.environ, {k: "" for k in ("SS_RECON_URL", "SS_RECON_MODEL", "SS_RECON_PARAMS")}):
                    rc, stdout, _ = run_cli(["smoke-q", "--config", str(CONFIG), "--out", tmp + "/o", "--plan"], e)
                self.assertEqual(rc, 2, needle)
                self.assertIn("WOULD BE REFUSED", stdout)
                self.assertIn(needle, stdout)
                self.assertIn("exact list", stdout)               # the list is printed either way


class TestSmokeQRefusals(unittest.TestCase):
    def go(self, srv, environ, config=CONFIG):
        with tempfile.TemporaryDirectory(prefix="ss-smoke-q-ref-") as tmp:
            rc, stdout, stderr = run_cli(["smoke-q", "--config", str(config), "--out", tmp + "/o"], environ)
            return rc, stdout, stderr, srv.requests, Path(tmp, "o").exists() and any(Path(tmp, "o").rglob("run.jsonl"))

    def test_each_refusal_comes_before_any_request(self):
        with FakeQwen() as srv:
            cases = {
                "non-local endpoint": env(f"http://{ip(8, 8, 8, 8)}:8142/v1"),
                "thinking not off": env(srv.base_url, params="{}"),
                "top-level enable_thinking is not the switch": env(srv.base_url, params='{"enable_thinking": false}'),
                "not a Qwen": env(srv.base_url, model="deepseek-v4-flash"),
            }
            for name, e in cases.items():
                rc, _out, err, reqs, logged = self.go(srv, e)
                self.assertEqual(rc, 2, name)
                self.assertIn("refused:", err, name)
                self.assertEqual(reqs, [], name)
                self.assertFalse(logged, name)

    def test_a_real_corpus_config_and_a_config_that_names_a_judge_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="ss-smoke-q-cfg-") as tmp, FakeQwen() as srv:
            d = json.loads(CONFIG.read_text())
            d["base_dir"] = str(CONFIG.parent)
            real = Path(tmp) / "real.json"
            real.write_text(json.dumps({**d, "corpus": {**d["corpus"], "kind": "real"}}))
            judge = Path(tmp) / "judge.json"
            judge.write_text(json.dumps({**d, "models": {**d["models"], "judge": "JUDGE"}}))
            for cfg, needle in ((real, "synthetic corpus only"), (judge, "never contacts a judge")):
                rc, _o, err, reqs, _l = self.go(srv, env(srv.base_url), config=cfg)
                self.assertEqual(rc, 2)
                self.assertIn(needle, err)
                self.assertEqual(reqs, [])

    def test_a_server_that_thinks_anyway_stops_the_run(self):
        with FakeQwen(always_think=True) as srv:
            rc, _o, err, reqs, _l = self.go(srv, env(srv.base_url))
            self.assertEqual(rc, 4)
            self.assertIn("thinking", err)
            self.assertEqual(len(reqs), 1)                 # stopped at the first reply


if __name__ == "__main__":
    unittest.main()
