"""cli fork: a finished Q out_dir -> a new out_dir for the judge phase (a pilot subset or all pairs). The source is only
read; the fork's log is the source log byte for byte plus one run_fork record and the removals of the pairs not kept;
a judge-only session (no recon client) runs J then M on it and verify passes. Synthetic files only (corpus kind "real"
so the real-run guards are live), oracle answers, no network."""
import contextlib
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import pipeline, prereg
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import build_clients, main
from tests.test_real_run_validity import Base, fake_build_clients

PILOT = ["SYN-OF01", "SYN-OW01", "SYN-OW02"]      # one provider-like role (officer), two of the other (owner)
OTHER_PROMPT_SET = "a" * 64


def tree_digest(root: Path) -> dict:
    """{relative path: sha256 of the bytes} of every file under ``root``."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def records(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


class ForkBase(Base):
    def run_cfg(self, cfg, stages, seen=None):
        def spy(kind, c, fixtures=None, roles=None):
            out = fake_build_clients(kind, c, fixtures, roles=roles)
            if seen is not None:
                seen.append(sorted(out))
            return out
        err = io.StringIO()
        with mock.patch("second_scribe.cli.build_clients", spy), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = main(["run", "--config", str(cfg), "--stages", stages, "--client", "oracle"])
        return rc, err.getvalue()

    def fork(self, *extra, out=None):
        out = out or self.out / "pilot"
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = main(["fork", "--config", str(self.cfg), "--out", str(out), *extra])
        return rc, so.getvalue(), se.getvalue(), out

    def q_phase(self, prompt_set=None):
        if prompt_set is None:
            rc, err = self.run_cfg(self.cfg, "Q")
        else:      # the Q phase ran under an earlier prompt set (run 2: 431da7d, before judge prompts v3)
            with mock.patch("second_scribe.pipeline.prompt_set_sha256", return_value=prompt_set), \
                    mock.patch("second_scribe.pipeline.prompt_manifest_problem", return_value=None):
                rc, err = self.run_cfg(self.cfg, "Q")
        self.assertEqual(rc, 0, err)

    def verify(self, log: Path) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["verify", str(log)])


class TestPilotFork(ForkBase):
    def test_pilot_fork_then_judge_only_j_and_m_verify_and_the_source_is_untouched(self):
        self.q_phase()
        before = tree_digest(self.out / "run")
        rc, out, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err + out)
        self.assertIn("fork: 3 of 12 pairs (officer 1, owner 2)", out)
        self.assertIn("verify of the fork: PASS", out)
        os.environ.pop("SS_RECON_MODEL")            # no reconstructor anywhere in the judge sessions
        seen: list = []
        fcfg = pilot / "config.json"
        rc, err = self.run_cfg(fcfg, "J", seen)
        self.assertEqual(rc, 0, err)
        rc, err = self.run_cfg(fcfg, "M", seen)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [["judge"]])          # J built the judge only; M built nothing
        flog = pilot / "out" / "logs" / "run.jsonl"
        self.assertEqual(self.verify(flog), 0)
        self.assertEqual(tree_digest(self.out / "run"), before)       # the source out_dir, byte for byte
        # the fork's log: the source log verbatim, then the fork, then judge calls only
        src_lines = (self.out / "run" / "logs" / "run.jsonl").read_text().splitlines()
        self.assertEqual(flog.read_text().splitlines()[:len(src_lines)], src_lines)
        recs = records(flog)
        i = next(k for k, r in enumerate(recs) if r["record_type"] == prereg.FORK_RECORD)
        self.assertEqual(i, len(src_lines))
        stages = {r["payload"]["stage"] for r in recs[i:] if r["record_type"] == "call_request"}
        self.assertTrue(stages and all(s.startswith("judge:") for s in stages), stages)
        self.assertIn("judge:in_transcript", stages)
        # the work store holds the kept pairs only
        work = pilot / "out" / "work"
        pids = {n.split("__")[0] for d in ("recon", "judge_modality", "in_transcript", "mapping_talk_confirm")
                for n in (p.stem for p in (work / d).glob("*.json"))}
        self.assertEqual(pids, set(PILOT))
        self.assertEqual(len(list((work / "in_transcript").glob("*.json"))), 3)
        self.assertTrue(list((work / "judge_modality:rung3").glob("*.json")))
        agg = json.loads((pilot / "out" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["n_participants"], 3)

    def test_the_fork_prints_counts_only(self):
        self.q_phase()
        rc, out, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err)
        texts = set()
        for f in (pilot / "out" / "work").rglob("*.json"):
            d = json.loads(f.read_text())
            for n in ((d.get("map") or {}).get("nodes") or []):
                texts.update(t for t in (n.get("text"), n.get("quote")) if t and len(t) > 6)
        self.assertTrue(texts)
        self.assertFalse([t for t in texts if t in out + err])

    def test_run_ids_and_seeds_are_kept_so_the_pilot_judges_the_full_runs_shuffled_copies(self):
        self.q_phase()
        _rc, _o, _e, pilot = self.fork("--pairs", ",".join(PILOT))
        src = json.loads(self.cfg.read_text())
        new = json.loads((pilot / "config.json").read_text())
        self.assertEqual(new["run_id"], src["run_id"])
        self.assertEqual(sorted(new["corpus"]["roles"]), PILOT)
        self.assertEqual({k: v for k, v in new.items() if k not in ("corpus", "out_dir", "base_dir")},
                         {k: v for k, v in src.items() if k not in ("corpus", "out_dir", "base_dir")})


class TestPromptSetBlocker(ForkBase):
    """Run 2: Q ran under one prompt set, J will run under the v3 judge prompts. The Q out_dir refuses the J session
    (unchanged guard); a fork of all pairs declares the new prompt set and runs J."""

    def test_j_on_the_q_out_dir_is_refused_after_a_prompt_change_and_a_full_fork_runs(self):
        self.q_phase(prompt_set=OTHER_PROMPT_SET)
        os.environ.pop("SS_RECON_MODEL")
        rc, err = self.run_cfg(self.cfg, "J")
        self.assertEqual(rc, 2)
        self.assertIn("prompt set hash differs", err)
        before = tree_digest(self.out / "run")
        rc, out, err, full = self.fork()
        self.assertEqual(rc, 0, err + out)
        self.assertIn("fork: 12 of 12 pairs", out)
        self.assertTrue((full / "out" / "audits" / "Q.json").is_file())       # all pairs: the Q audits still apply
        rc, err = self.run_cfg(full / "config.json", "J")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.verify(full / "out" / "logs" / "run.jsonl"), 0)
        self.assertEqual(tree_digest(self.out / "run"), before)
        fork = next(r for r in records(full / "out" / "logs" / "run.jsonl") if r["record_type"] == prereg.FORK_RECORD)
        self.assertEqual(fork["payload"]["source_prompt_set_sha256"], OTHER_PROMPT_SET)
        self.assertEqual(fork["payload"]["prompt_set_sha256"], pipeline.prompt_set_sha256())


class TestForkGuards(ForkBase):
    def test_refusals(self):
        self.q_phase()
        cases = [
            (("--pairs", "SYN-NOPE"), "not in the source config"),
            (("--pairs", "SYN-OW01", "--script-samples", "0"), "at least 1"),
        ]
        for extra, msg in cases:
            rc, out, err, pilot = self.fork(*extra, out=self.out / f"p{len(msg)}")
            self.assertEqual(rc, 2, out)
            self.assertIn(msg, err)
            self.assertFalse(pilot.exists() and any(pilot.iterdir()))
        rc, _o, err, _p = self.fork(out=self.out / "run" / "inside")
        self.assertEqual(rc, 2)
        self.assertIn("must not contain each other", err)
        (self.out / "busy").mkdir()
        (self.out / "busy" / "x").write_text("1")
        rc, _o, err, _p = self.fork(out=self.out / "busy")
        self.assertEqual(rc, 2)
        self.assertIn("not empty", err)

    def test_a_source_with_judge_items_or_a_broken_log_is_refused(self):
        self.q_phase()
        log = self.out / "run" / "logs" / "run.jsonl"
        good = log.read_bytes()
        log.write_bytes(good.replace(b'"record_type":"run_start"', b'"record_type":"run_strt"', 1))
        rc, _o, err, _p = self.fork(out=self.out / "a")
        self.assertEqual(rc, 2)
        self.assertIn("does not verify", err)
        log.write_bytes(good)
        rc, err = self.run_cfg(self.cfg, "J")
        self.assertEqual(rc, 0, err)
        rc, _o, err, _p = self.fork(out=self.out / "b")
        self.assertEqual(rc, 2)
        self.assertIn("Q-phase out_dir", err)

    def test_a_fork_of_a_fork_is_refused(self):
        self.q_phase()
        rc, _o, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err)
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = main(["fork", "--config", str(pilot / "config.json"), "--out", str(self.out / "again")])
        self.assertEqual(rc, 2)
        self.assertIn("itself a fork", se.getvalue())

    def test_an_edited_fork_config_is_refused_like_any_config_change(self):
        self.q_phase()
        _rc, _o, _e, pilot = self.fork("--pairs", ",".join(PILOT))
        cfgp = pilot / "config.json"
        d = json.loads(cfgp.read_text())
        d["corpus"]["roles"].pop("SYN-OW02")
        d["corpus"]["pairs"] = None
        cfgp.write_text(json.dumps(d))
        rc, err = self.run_cfg(cfgp, "J")
        self.assertEqual(rc, 2)
        self.assertIn("config hash differs", err)

    def test_a_second_or_misplaced_fork_record_fails_restart_and_validity(self):
        self.q_phase()
        _rc, _o, _e, pilot = self.fork("--pairs", ",".join(PILOT))
        flog = pilot / "out" / "logs" / "run.jsonl"
        fork = next(r for r in records(flog) if r["record_type"] == prereg.FORK_RECORD)["payload"]
        log = AuditLog(flog, "x")
        log.append(prereg.FORK_RECORD, fork)          # a second fork, appended later, naming the first one's place
        log.close()
        work = pilot / "out" / "work"
        self.assertIn("does not sit right after", prereg.run_validity_problem(flog, work))
        self.assertIn("does not sit right after",
                      prereg.restart_problem(flog, fork["prompt_set_sha256"], fork["config_sha256"]))
        # the Q out_dir with a fork record appended at its end: not right after a copied prefix it names
        slog = self.out / "run" / "logs" / "run.jsonl"
        log = AuditLog(slog, "x")
        log.append(prereg.FORK_RECORD, {**fork, "source_records": 3})
        log.close()
        self.assertIn("does not sit right after", prereg.restart_problem(slog, "p", "c"))

    def test_a_log_without_a_fork_is_checked_as_before(self):
        recs = [{"record_type": "run_start", "payload": {"prompt_set_sha256": "p1", "config_sha256": "c"}},
                {"record_type": "run_start", "payload": {"prompt_set_sha256": "p2", "config_sha256": "c"}}]
        self.assertIn("2 different prompt set", prereg._start_disagreement(recs))
        self.assertEqual(prereg.session_starts(recs), [r["payload"] for r in recs])


class TestForkSyntheticOracle(unittest.TestCase):
    """The plain synthetic demo (corpus kind synthetic) with the oracle client: Q, fork a pilot, J and M without recon."""

    def test_demo_fork_runs_j_and_m(self):
        import tempfile
        from second_scribe.cli import DEMO_CONFIG
        with tempfile.TemporaryDirectory(prefix="ss-tests-fork-") as t:
            d = json.loads(Path(DEMO_CONFIG).read_text())
            d.update(out_dir=str(Path(t) / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
            cfgp = Path(t) / "config.json"
            cfgp.write_text(json.dumps(d))
            seen = []

            def spy(kind, cfg, fixtures=None, roles=None):
                out = build_clients(kind, cfg, fixtures, roles=roles)
                seen.append(sorted(out))
                return out
            with mock.patch("second_scribe.cli.build_clients", spy), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--config", str(cfgp), "--stages", "Q", "--client", "oracle"]), 0)
                before = tree_digest(Path(t) / "run")
                self.assertEqual(main(["fork", "--config", str(cfgp), "--out", str(Path(t) / "pilot"),
                                       "--pairs", ",".join(PILOT)]), 0)
                pc = str(Path(t) / "pilot" / "config.json")
                self.assertEqual(main(["run", "--config", pc, "--stages", "J", "--client", "oracle"]), 0)
                self.assertEqual(main(["run", "--config", pc, "--stages", "M", "--client", "oracle"]), 0)
                self.assertEqual(main(["verify", str(Path(t) / "pilot" / "out" / "logs" / "run.jsonl")]), 0)
            self.assertEqual(seen, [["recon"], ["judge"]])
            self.assertEqual(tree_digest(Path(t) / "run"), before)


if __name__ == "__main__":
    unittest.main()


class TestSampleDraw(ForkBase):
    def test_draw_is_proportional_seeded_and_deterministic(self):
        from types import SimpleNamespace as P
        from second_scribe.fork import draw_pairs, ForkRefused
        people = {f"PC{i:02d}": P(role="pc") for i in range(24)} | {f"SP{i:02d}": P(role="sp") for i in range(22)}
        a = draw_pairs(people, set(people), 12, 0)
        self.assertEqual(a, draw_pairs(people, set(people), 12, 0))
        self.assertEqual((sum(p.startswith("PC") for p in a), sum(p.startswith("SP") for p in a)), (6, 6))
        self.assertNotEqual(a, draw_pairs(people, set(people), 12, 1))
        self.assertEqual(len(draw_pairs(people, set(people), 5, 0)), 5)
        with self.assertRaises(ForkRefused):
            draw_pairs(people, {p for p in people if p.startswith("PC")}, 12, 0)    # no eligible SP
        with self.assertRaises(ForkRefused):
            draw_pairs(people, set(people), 0, 0)

    def test_fork_sample_prints_counts_per_role_only_and_records_the_draw(self):
        self.q_phase()
        rc, out, err, pilot = self.fork("--sample", "6", "--seed", "3")
        self.assertEqual(rc, 0, err + out)
        self.assertIn("fork: 6 of 12 pairs (officer 3, owner 3)", out)   # 5 officers, 7 owners: 2.5 / 3.5, tie to officer
        self.assertNotIn("SYN-", out + err)
        fork = next(r for r in records(pilot / "out" / "logs" / "run.jsonl")
                    if r["record_type"] == prereg.FORK_RECORD)["payload"]
        self.assertEqual(fork["sample"]["n"], 6)
        self.assertEqual(fork["sample"]["seed"], 3)
        self.assertEqual(len(fork["pairs"]), 6)
        rc, _o, err, _p = self.fork("--sample", "6", "--pairs", "SYN-OW01", out=self.out / "both")
        self.assertEqual(rc, 2)
        self.assertIn("not both", err)


CODE = {"code_commit": "c" * 40, "code_clean": True}


class TestWiden(ForkBase):
    """Pilot subset -> J + M -> widen to all pairs -> J judges only what is missing -> M -> verify."""

    def setUp(self):
        super().setUp()
        p = mock.patch("second_scribe.prereg.code_state", return_value=dict(CODE))
        p.start()
        self.addCleanup(p.stop)
        os.environ.update(SS_JUDGE_URL="http://127.0.0.1:9/v1",
                          SS_JUDGE_PARAMS=json.dumps({"chat_template_kwargs": {"enable_thinking": False}}))
        self.addCleanup(os.environ.pop, "SS_JUDGE_URL", None)
        self.addCleanup(os.environ.pop, "SS_JUDGE_PARAMS", None)

    def widen(self, pilot, out=None):
        out = out or self.out / "full"
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = main(["widen", "--pilot-config", str(pilot / "config.json"), "--config", str(self.cfg),
                       "--out", str(out)])
        return rc, so.getvalue(), se.getvalue(), out

    def judged_pilot(self):
        self.q_phase()
        rc, _o, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err)
        os.environ.pop("SS_RECON_MODEL")
        for g in ("J", "M"):
            rc, err = self.run_cfg(pilot / "config.json", g)
            self.assertEqual(rc, 0, err)
        return pilot

    def test_widen_carries_the_pilot_items_and_j_judges_only_the_rest(self):
        pilot = self.judged_pilot()
        q_before, p_before = tree_digest(self.out / "run"), tree_digest(pilot / "out")
        rc, out, err, full = self.widen(pilot)
        self.assertEqual(rc, 0, err + out)
        self.assertIn("widen: 12 pairs, 3 of them carried", out)
        self.assertIn("verify of the widened run: PASS", out)
        self.assertNotIn("SYN-", out + err)
        flog = full / "out" / "logs" / "run.jsonl"
        n0 = len(records(flog))
        self.assertEqual(flog.read_text().splitlines()[:len((pilot / "out" / "logs" / "run.jsonl").read_text()
                                                             .splitlines())],
                         (pilot / "out" / "logs" / "run.jsonl").read_text().splitlines())
        for g in ("J", "M"):
            rc, err = self.run_cfg(full / "config.json", g)
            self.assertEqual(rc, 0, err)
        self.assertEqual(self.verify(flog), 0)
        calls = [r["payload"] for r in records(flog)[n0:] if r["record_type"] == "call_request"]

        def pid(c):
            return c["item_id"].split(":")[0].split("__")[0].split("@")[0]
        carried_stages = ("judge:in_transcript", "judge:modality", "judge:shuffled")
        self.assertEqual([c for c in calls if c["stage"].startswith(carried_stages) and pid(c) in PILOT], [])
        self.assertEqual([c for c in calls if c["stage"] == "judge:align" and pid(c) in PILOT
                          and "__ai_endorsed__" in c["item_id"]], [])
        new = {pid(c) for c in calls if c["stage"] == "judge:in_transcript"}
        self.assertEqual(len(new), 9)
        self.assertFalse(new & set(PILOT))
        work = full / "out" / "work"
        self.assertEqual(len(list((work / "in_transcript").glob("*.json"))), 12)
        self.assertEqual(len(list((work / "judge_shuffled").glob("*.json"))), 12)
        agg = json.loads((full / "out" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["n_participants"], 12)
        forks = [r for r in records(flog) if r["record_type"] == prereg.FORK_RECORD]
        self.assertEqual([f["payload"].get("kind") for f in forks], [None, "widen"])
        self.assertEqual(tree_digest(self.out / "run"), q_before)
        self.assertEqual(tree_digest(pilot / "out"), p_before)

    def test_widen_refuses_any_change(self):
        pilot = self.judged_pilot()
        k = iter(range(100))

        def refused(msg):
            rc, out, err, where = self.widen(pilot, out=self.out / f"w{next(k)}")
            self.assertEqual(rc, 2, out)
            self.assertIn(msg, err)
            self.assertFalse(where.exists() and any(where.iterdir()))
        os.environ["SS_JUDGE_MODEL"] = "other-judge-2"
        refused("judge model id differs")
        os.environ["SS_JUDGE_MODEL"] = "beta-judge-1"
        os.environ["SS_JUDGE_PARAMS"] = json.dumps({"chat_template_kwargs": {"enable_thinking": False},
                                                    "reasoning": {"enabled": False}})
        refused("inference params differs")
        os.environ["SS_JUDGE_PARAMS"] = json.dumps({"chat_template_kwargs": {"enable_thinking": False}})
        with mock.patch("second_scribe.pipeline.prompt_set_sha256", return_value=OTHER_PROMPT_SET):
            refused("prompt set of this code differs")
        with mock.patch("second_scribe.textnorm.quote_rule_sha256", return_value="q" * 64):
            refused("quote rule of this code differs")
        with mock.patch("second_scribe.prereg.code_state", return_value={"code_commit": "d" * 40, "code_clean": True}):
            refused("another code commit")
        with mock.patch("second_scribe.prereg.code_state", return_value={"code_commit": "c" * 40, "code_clean": False}):
            refused("uncommitted changes")
        os.environ.pop("SS_JUDGE_URL")
        refused("judge environment variables")
        os.environ["SS_JUDGE_URL"] = "http://127.0.0.1:9/v1"
        rc, _o, err, _w = self.widen(pilot, out=self.out / "ok")
        self.assertEqual(rc, 0, err)                     # unchanged: it goes through

    def test_a_pilot_with_fewer_script_samples_or_unjudged_cannot_be_widened(self):
        self.q_phase()
        rc, _o, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err)
        rc, _o, err, _w = self.widen(pilot, out=self.out / "w1")
        self.assertEqual(rc, 2)
        self.assertIn("made no judge call", err)
        d = json.loads((pilot / "config.json").read_text())
        d["stats"] = {**d.get("stats", {}), "seed": 99}            # any change beyond the participant tables
        cfgp = self.out / "edited.json"
        cfgp.write_text(json.dumps(d))
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = main(["widen", "--pilot-config", str(cfgp), "--config", str(self.cfg), "--out", str(self.out / "w2")])
        self.assertEqual(rc, 2)
        self.assertIn("beyond the participant tables", se.getvalue())
