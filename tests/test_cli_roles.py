"""Final review F2 (cli): a Q-only session needs the reconstructor endpoint only. The judge client is not built, the
preflight and the canary do not call a judge, and a resumed session with nothing left to call makes no call at all.
Synthetic corpus (kind real for the guards), fake clients, temp dirs."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from second_scribe import audits, pipeline
from second_scribe.cli import DEMO_CONFIG, build_clients, main
from tests.test_real_run_validity import THINK_OFF, Base, CfgClient

HTTP_ENV = {"SS_RECON_URL": "http://127.0.0.1:9/v1", "SS_RECON_MODEL": "alpha-recon-1"}


def role_fake(seen, ids=None):
    """A build_clients stand-in that records the ``roles`` it was asked for and builds only those."""
    def fake(kind, cfg, fixtures=None, roles=None):
        seen.append(roles)
        base = build_clients("oracle", cfg, fixtures)
        return {r: CfgClient(c, r, os.environ[f"SS_{r.upper()}_MODEL"], dict(THINK_OFF))
                for r, c in base.items() if roles is None or r in roles}
    return fake


class TestBuildClientsRoles(unittest.TestCase):
    def cfg(self):
        return pipeline.RunConfig.load(DEMO_CONFIG)

    def test_http_clients_for_the_recon_role_need_no_judge_environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(os.environ, HTTP_ENV):
            clients = build_clients("http", self.cfg(), roles=("recon",))
        self.assertEqual(sorted(clients), ["recon"])

    def test_without_roles_every_role_is_built_as_before(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(os.environ, HTTP_ENV):
            with self.assertRaises(KeyError):        # SS_JUDGE_URL is not set
                build_clients("http", self.cfg())

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(ValueError):
            build_clients("oracle", self.cfg(), roles=("nope",))


class TestCmdRunAsksForTheRolesItUses(Base):
    def run_stages(self, stages):
        seen: list = []
        err = io.StringIO()
        with mock.patch("second_scribe.cli.build_clients", role_fake(seen)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(self.cfg), "--stages", stages, "--client", "oracle"])
        return rc, err.getvalue(), seen

    def test_q_asks_for_recon_only(self):
        rc, err, seen = self.run_stages("Q")
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [("recon",)])

    def test_q_and_j_or_all_ask_for_every_role(self):
        for stages in ("Q,J", "all"):
            with self.subTest(stages=stages):
                _rc, _err, seen = self.run_stages(stages)
                self.assertEqual(seen, [None])

    def test_a_metrics_only_run_builds_no_client(self):
        self.assertEqual(self.run_stages("Q")[0], 0)
        _rc, _err, seen = self.run_stages("M")
        self.assertEqual(seen, [])


class TestQOnlySessionWithoutAJudge(Base):
    """A real corpus, Q only, recon client only: the preflight and the canary cannot reach a judge, and must not need one."""

    def q_only(self):
        seen: list = []
        err = io.StringIO()
        with mock.patch("second_scribe.cli.build_clients", role_fake(seen)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(self.cfg), "--stages", "Q", "--client", "oracle"])
        return rc, err.getvalue()

    def test_the_session_passes_its_preflight_and_canary_with_the_recon_client_alone(self):
        rc, err = self.q_only()
        self.assertEqual(rc, 0, err)
        stages = {json.loads(l)["payload"]["stage"] for l in self.log.read_text().splitlines()
                  if json.loads(l)["record_type"] == "call_request"}
        self.assertTrue(stages)
        self.assertFalse([s for s in stages if s.startswith(("judge", "in_transcript", "align"))], stages)
        self.assertTrue(list((self.work / "recon").glob("*.json")))

    def test_a_resumed_q_session_with_nothing_left_makes_no_call_not_even_the_preflight(self):
        self.assertEqual(self.q_only()[0], 0)
        n = len(self.log.read_text().splitlines())
        pre = mock.Mock(wraps=audits.preflight)
        with mock.patch.object(audits, "preflight", pre):
            self.assertEqual(self.q_only()[0], 0)
        # the real session's first client() would run the preflight; with nothing to call it must not be reached
        # (the synthetic canary run has its own _Run, whose preflight returns at once, and is not the subject here)
        self.assertEqual([c for c in pre.call_args_list if c.args[0].cfg.corpus.get("kind") == "real"], [])
        new_calls = [json.loads(l) for l in self.log.read_text().splitlines()[n:]
                     if json.loads(l)["record_type"] == "call_request"]
        self.assertEqual(new_calls, [])


class TestRecconCanary(unittest.TestCase):
    def work(self, tmp, statuses):
        import pathlib
        d = pathlib.Path(tmp) / "recon"
        d.mkdir()
        for i, st in enumerate(statuses):
            (d / f"p{i}.json").write_text(json.dumps({"status": st}))
        return pathlib.Path(tmp)

    def test_all_ok_passes_and_a_low_rate_or_no_reconstruction_blocks(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ss-canrec-") as t:
            self.assertTrue(audits.audit_canary_recon(self.work(t, ["ok"] * 4)).passed)
        with tempfile.TemporaryDirectory(prefix="ss-canrec-") as t:
            res = audits.audit_canary_recon(self.work(t, ["ok", "failed", "failed", "failed"]))
            self.assertEqual(res.failures, ["canary_below_rule:recon"])
        with tempfile.TemporaryDirectory(prefix="ss-canrec-") as t:
            res = audits.audit_canary_recon(t)
            self.assertEqual(res.failures, ["canary_no_rows:recon"])


class TestCmdRunRefusesABadConfig(Base):
    """L33 (fix H): ``cli run`` with a config that RunConfig.load refuses (a ValueError) says "refused: <why>" on
    stderr and exits 2, with no traceback, like the kit's other refusals. Run as a child process: the real exit path."""

    def run_child(self, edit):
        d = json.loads(self.cfg.read_text())
        edit(d)
        bad = self.out / "bad-config.json"
        bad.write_text(json.dumps(d))
        import subprocess
        import sys
        from second_scribe.pipeline import KIT_ROOT
        return subprocess.run([sys.executable, "-m", "second_scribe.cli", "run", "--config", str(bad),
                               "--client", "oracle"], cwd=KIT_ROOT, capture_output=True, text=True, timeout=120)

    def test_a_non_primary_first_spec_exits_2_without_a_traceback(self):
        def not_primary(d):
            d["specs"][0] = {**d["specs"][0], "arm": "full"}
        done = self.run_child(not_primary)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertTrue(done.stderr.startswith("refused: "), done.stderr)
        self.assertIn("the first spec must be the primary setting", done.stderr)
        self.assertFalse((self.out / "run" / "logs").exists())        # refused before any log or call

    def test_another_refused_setting_exits_2_too(self):
        def bad_draws(d):
            d["ladder"] = {**d["ladder"], "seed": "x"}
        done = self.run_child(bad_draws)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("refused: ladder.seed must be an integer", done.stderr)


if __name__ == "__main__":
    unittest.main()
