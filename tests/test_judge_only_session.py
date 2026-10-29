"""B1 (JUDGE-WINDOW-RUNBOOK 0.1): a J-only session (Qwen stopped) builds only the judge client, its preflight sends the
planted items through the judge only, and its end-of-group canary (which would run the Q stages) is skipped with a
recorded reason. A Q session still builds recon and runs its canary; a mixed session builds every role."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from second_scribe import audits, pipeline
from second_scribe.cli import DEMO_CONFIG, build_clients, main, roles_for_stages
from tests.test_real_run_validity import THINK_OFF, Base, CfgClient

J_STAGES = list(pipeline.STAGE_GROUPS["J"])
Q_STAGES = list(pipeline.STAGE_GROUPS["Q"])


class TestRolesForStages(unittest.TestCase):
    def test_a_session_whose_model_stages_are_all_judge_stages_asks_for_the_judge_roles_only(self):
        self.assertEqual(roles_for_stages(J_STAGES), ("judge", "judge2"))
        self.assertEqual(roles_for_stages(J_STAGES + ["metrics", "report"]), ("judge", "judge2"))
        self.assertEqual(roles_for_stages(["align"]), ("judge", "judge2"))

    def test_q_only_is_unchanged_and_mixed_or_modelless_sessions_ask_for_everything_or_nothing(self):
        self.assertEqual(roles_for_stages(Q_STAGES), ("recon",))
        self.assertIsNone(roles_for_stages(Q_STAGES + J_STAGES))
        self.assertIsNone(roles_for_stages(["recon", "align"]))
        self.assertIsNone(roles_for_stages(["metrics", "report"]))

    def test_build_clients_for_the_judge_roles_needs_no_recon_environment(self):
        cfg = pipeline.RunConfig.load(DEMO_CONFIG)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
        env.update(SS_JUDGE_URL="http://127.0.0.1:9/v1", SS_JUDGE_MODEL="beta-judge-1")
        with mock.patch.dict(os.environ, env, clear=True):
            clients = build_clients("http", cfg, roles=roles_for_stages(J_STAGES))
        self.assertEqual(sorted(clients), ["judge"])          # judge2 only when the config names one


def judge_only_fake(seen):
    """build_clients stand-in: builds only the roles asked for, so a recon client cannot exist in a judge-only session."""
    def fake(kind, cfg, fixtures=None, roles=None):
        seen.append(roles)
        base = build_clients("oracle", cfg, fixtures, roles=roles)
        return {r: CfgClient(c, r, os.environ[f"SS_{r.upper()}_MODEL"], dict(THINK_OFF)) for r, c in base.items()}
    return fake


class TestJudgeOnlyRealSession(Base):
    """Real corpus kind (so the preflight, canary and guards are live), oracle answers, Q first then J on its own."""

    def session(self, stages, seen):
        err = io.StringIO()
        with mock.patch("second_scribe.cli.build_clients", judge_only_fake(seen)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(self.cfg), "--stages", stages, "--client", "oracle"])
        return rc, err.getvalue()

    def new_calls(self, start):
        return [json.loads(l)["payload"] for l in self.log.read_text().splitlines()[start:]
                if json.loads(l)["record_type"] == "call_request"]

    def test_q_then_j_alone_without_a_recon_client(self):
        seen: list = []
        rc, err = self.session("Q,M", seen)       # Q (M builds nothing): the full canary runs here as before
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [("recon",)])
        n = len(self.log.read_text().splitlines())
        os.environ.pop("SS_RECON_MODEL")           # no recon environment at all in the judge session
        seen.clear()
        rc, err = self.session("J", seen)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [("judge", "judge2")])
        calls = self.new_calls(n)
        stages = {c["stage"] for c in calls}
        self.assertTrue(any(s.startswith("judge") for s in stages), stages)
        pre = [json.loads(l)["payload"]["stage"] for l in
               (self.out / "run" / "preflight" / "logs" / "run.jsonl").read_text().splitlines()
               if json.loads(l)["record_type"] == "call_request"]
        self.assertTrue(pre)                                      # the planted items went through the judge...
        self.assertEqual(set(pre), {"preflight:judge"}, pre)      # ...and nothing went to a reconstructor
        self.assertFalse([s for s in stages if s.startswith(("recon", "prior_only", "mapping_talk", "preflight:recon"))],
                         stages)
        self.assertTrue(list((self.work / "judge_modality").glob("*.json")))
        summary = json.loads((self.out / "run" / "audits" / "J.json").read_text())
        names = {r["name"]: r for r in summary["results"]}
        self.assertTrue(names["preflight"]["passed"])
        self.assertTrue(names["canary_judge"]["passed"])                  # the end-of-J planted-items recheck still runs
        self.assertNotIn("canary", names)                                 # no pretend Q canary
        skipped = names["canary_skipped"]
        self.assertTrue(skipped["passed"] and not skipped["blocking"])
        self.assertEqual(skipped["warnings"], ["canary_skipped:no_recon_client"])

    def test_a_q_session_still_runs_the_full_recon_canary(self):
        seen: list = []
        rc, err = self.session("Q", seen)
        self.assertEqual(rc, 0, err)
        names = {r["name"] for r in json.loads((self.out / "run" / "audits" / "Q.json").read_text())["results"]}
        self.assertIn("canary", names)
        self.assertNotIn("canary_skipped", names)

    def test_a_mixed_session_builds_every_role_and_runs_the_full_canary(self):
        seen: list = []
        rc, err = self.session("Q,J", seen)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [None])
        names = {r["name"] for g in ("Q", "J")
                 for r in json.loads((self.out / "run" / "audits" / f"{g}.json").read_text())["results"]}
        self.assertIn("canary", names)
        self.assertNotIn("canary_skipped", names)

    def test_the_real_client_guard_still_refuses_a_qwen_judge_in_a_judge_only_session(self):
        from second_scribe.llm import RealGuardRefused, guard_real_clients
        qwen = CfgClient(None, "judge", "Qwen3-Next-80B", dict(THINK_OFF))
        with self.assertRaises(RealGuardRefused):
            guard_real_clients({"judge": qwen})


class TestJudgeOnlyReplayOnTheDemo(unittest.TestCase):
    """The synthetic demo with the replay client: Q, then J alone with no recon client in the session."""

    def test_judge_only_session_on_the_demo_finishes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as t:
            d = json.loads(Path(DEMO_CONFIG).read_text())
            d.update(out_dir=str(Path(t) / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
            cfgp = Path(t) / "config.json"
            cfgp.write_text(json.dumps(d))
            seen = []
            real_build = build_clients

            def spy(kind, cfg, fixtures=None, roles=None):
                out = real_build(kind, cfg, fixtures, roles=roles)
                seen.append(sorted(out))
                return out
            with mock.patch("second_scribe.cli.build_clients", spy), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--config", str(cfgp), "--stages", "Q", "--client", "replay"]), 0)
                self.assertEqual(main(["run", "--config", str(cfgp), "--stages", "J", "--client", "replay"]), 0)
                self.assertEqual(main(["run", "--config", str(cfgp), "--stages", "M", "--client", "replay"]), 0)
            self.assertEqual(seen, [["recon"], ["judge"]])
            self.assertTrue(list((Path(t) / "run" / "work" / "judge_modality").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
