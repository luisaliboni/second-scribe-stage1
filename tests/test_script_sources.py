"""F1b item 6: script sources from config -> the F2 script-conditional test. A judge-family model writes a
prior-only script in the judge phase (no participant text), the judge aligns the endorsed labels against it, and the
"other family" label is DERIVED from the logged model ids (M-F2-1), never taken from config. Synthetic corpus and
fake (oracle) clients only."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import cli as cli_module
from second_scribe.cli import DEMO_CONFIG, main as cli_main
from second_scribe.pipeline import RunConfig
from tests.test_real_run_validity import Base as RealBase
from tests.test_real_run_validity import fake_build_clients

SRC = {"name": "judgeprior", "role": "judge", "variants": ["p0", "p1"], "samples": 2, "temperature": 0.7}


def write_config(out: Path, **top) -> Path:
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.pop("script_sources", None)              # the demo's own source (L75): each test sets its own or none
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["stats"] = {"B": 50, "n_perm": 20, "n_derangements": 3, "n_perm_script": 40, "alpha_script": 0.2, "seed": 3}
    d.update(top)
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.out = Path(self._t.name)

    def load(self, sources):
        return RunConfig.load(write_config(self.out, script_sources=sources))

    def test_a_source_loads_and_an_empty_list_leaves_the_config_hash_alone(self):
        base = RunConfig.load(write_config(self.out))
        self.assertEqual(self.load([]).sha256(), base.sha256())
        self.assertNotEqual(self.load([SRC]).sha256(), base.sha256())
        self.assertEqual(self.load([SRC]).script_sources, [SRC])

    def test_the_family_cannot_be_set_from_config(self):
        with self.assertRaises(ValueError) as cm:
            self.load([{**SRC, "family": "other"}])
        self.assertIn("family", str(cm.exception))

    def test_bad_sources_are_refused(self):
        for bad in ({**SRC, "name": "own"}, {**SRC, "name": "a__b"}, {**SRC, "name": "x y"},
                    {**SRC, "role": "recon2"}, {**SRC, "variants": []}, {**SRC, "samples": 0},
                    {k: v for k, v in SRC.items() if k != "name"}, {**SRC, "stray": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.load([bad])
        with self.assertRaises(ValueError):
            self.load([SRC, SRC])                      # two sources of one name


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.out = Path(self._t.name)
        self.work = self.out / "run" / "work"
        self.log = self.out / "run" / "logs" / "run.jsonl"

    def run_cli(self, recon_model, judge_model, **top):
        cfg = write_config(self.out, **top)
        env = {"SS_RECON_MODEL": recon_model, "SS_JUDGE_MODEL": judge_model}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(cli_module, "build_clients", fake_build_clients), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])
        self.assertEqual(rc, 0, err.getvalue())
        return json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())

    def records(self, rtype):
        return [json.loads(l) for l in self.log.read_text().splitlines() if json.loads(l)["record_type"] == rtype]

    def sc(self, agg):
        return agg["headline"]["absences"]["script_conditional"]


class TestPipelineWiring(Base):
    def test_the_judge_phase_writes_a_script_without_any_participant_text(self):
        agg = self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        items = sorted(p.stem for p in (self.work / "script_prior_only").glob("*.json"))
        self.assertEqual(len(items), 2 * 2 * 2)                           # 2 roles x 2 variants x 2 samples
        self.assertTrue(all(i.startswith("judgeprior__") for i in items))
        calls = [r["payload"] for r in self.records("call_request") if r["payload"]["stage"] == "judge:script_prior_only"]
        self.assertEqual(len(calls), len(items))
        self.assertEqual({c["model_id"] for c in calls}, {"beta-deepseek-1"})            # the judge-family model
        for c in calls:
            text = json.dumps(c["messages"])
            self.assertNotIn("TRANSCRIPT", text)                          # prior-only: no participant text
        self.assertEqual(agg["items"]["script_prior_only"], {"ok": 8})

    def test_alignment_items_exist_for_every_participant_and_the_source_gets_a_row(self):
        agg = self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        al = sorted(p.name for p in (self.work / "align").glob("*__endorsed_src__judgeprior.json"))
        self.assertEqual(len(al), 12)
        sc = self.sc(agg)
        self.assertEqual(sc["primary_source"], "judgeprior")
        self.assertEqual(set(sc["rows"]), {"judgeprior", "own"})
        self.assertEqual(sc["rows"]["judgeprior"]["family"], "other")
        self.assertTrue(sc["rows"]["judgeprior"]["headline_eligible"])
        self.assertEqual(sc["rows"]["own"]["family"], "own")
        self.assertEqual(sc["rows"]["own"]["label"], "own-script (sensitivity only)")

    def test_n_perm_script_and_alpha_script_come_from_config(self):
        agg = self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        sc = self.sc(agg)
        self.assertEqual(sc["n_perm"], 40)
        self.assertEqual(sc["alpha"], 0.2)
        self.assertEqual(sc["rows"]["judgeprior"]["n_perm"], 40)

    def test_defaults_are_10000_and_0_05(self):
        agg = self.run_cli("alpha-qwen-1", "beta-deepseek-1",
                           stats={"B": 20, "n_perm": 5, "n_derangements": 2, "seed": 1})
        sc = self.sc(agg)
        self.assertEqual((sc["n_perm"], sc["alpha"]), (10000, 0.05))
        self.assertEqual(set(sc["rows"]), {"own"})                         # no source configured: own only
        self.assertIsNone(sc["primary_source"])

    def test_the_source_description_is_in_the_aggregates(self):
        agg = self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        d = agg["script_sources"]["judgeprior"]
        self.assertEqual((d["family"], d["model"]), ("other", "beta-deepseek-1"))
        self.assertEqual(d["reconstructor_family"], "qwen")
        self.assertEqual(d["script_family"], "deepseek")


class TestFamilyIsDerivedNotTrusted(Base):
    def test_a_script_by_the_reconstructors_own_family_is_own_and_never_sets_the_flag(self):
        agg = self.run_cli("alpha-qwen-1", "gamma-qwen-2", script_sources=[SRC])      # judge is a Qwen-family model
        sc = self.sc(agg)
        self.assertEqual(sc["rows"]["judgeprior"]["family"], "own")
        self.assertFalse(sc["rows"]["judgeprior"]["headline_eligible"])
        self.assertIsNone(sc["primary_source"])
        self.assertFalse(sc["claim_script_conditional_leakage"])
        self.assertEqual(agg["script_sources"]["judgeprior"]["reason"], "script_is_qwen_family")

    def test_the_same_model_id_is_own(self):
        agg = self.run_cli("same-model-1", "same-model-1", script_sources=[SRC])
        self.assertEqual(self.sc(agg)["rows"]["judgeprior"]["family"], "own")

    def test_an_alias_model_id_never_makes_the_source_the_other_family(self):
        """Review M-3: the judge endpoint is served under the alias "judge"; its true family is unknown."""
        agg = self.run_cli("alpha-qwen-1", "judge", script_sources=[SRC])
        sc = self.sc(agg)
        self.assertEqual(sc["rows"]["judgeprior"]["family"], "own")
        self.assertFalse(sc["rows"]["judgeprior"]["headline_eligible"])
        self.assertIsNone(sc["primary_source"])
        self.assertEqual(agg["script_sources"]["judgeprior"]["reason"], "family_unknown")
        rows = sc["rows"]
        self.assertEqual(rows["own"]["label"], "own-script (sensitivity only)")             # the reconstructor's own
        self.assertEqual(rows["judgeprior"]["label"], "family not established; sensitivity")     # NOT the same label
        self.assertEqual(rows["judgeprior"]["family_reason"], "family_unknown")
        self.assertNotIn("family_reason", rows["own"])
        self.assertLessEqual(max(len(r["label"]) for r in rows.values()), 40)              # the aggregate-only rule

    def test_a_source_whose_role_has_no_client_fails_loudly(self):
        cfg = write_config(self.out, script_sources=[{**SRC, "role": "judge2"}])        # no judge2 configured
        env = {"SS_RECON_MODEL": "alpha-qwen-1", "SS_JUDGE_MODEL": "beta-deepseek-1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(cli_module, "build_clients", fake_build_clients), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])
        self.assertIn("judge2", str(cm.exception))

    def test_no_model_id_on_record_is_own(self):
        cfg = write_config(self.out, script_sources=[SRC])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])      # plain oracle: no ids
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(self.sc(agg)["rows"]["judgeprior"]["family"], "own")
        self.assertIsNone(self.sc(agg)["primary_source"])
        self.assertNotEqual(self.sc(agg)["rows"]["judgeprior"]["label"], self.sc(agg)["rows"]["own"]["label"])
        self.assertEqual(self.sc(agg)["rows"]["judgeprior"]["family_reason"], "script_model_not_on_record")


class TestResume(Base):
    def test_a_rerun_makes_no_new_script_or_alignment_calls(self):
        self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        n = len(self.log.read_text().splitlines())
        self.run_cli("alpha-qwen-1", "beta-deepseek-1", script_sources=[SRC])
        again = [json.loads(l) for l in self.log.read_text().splitlines()[n:]]
        self.assertFalse([r for r in again if r["record_type"] == "call_request"])


class TestSourceScriptsAreScoped(unittest.TestCase):
    """Review M-7 + M-4/M-3: a source's script per role holds only that role's nodes of that source, built from the
    draws the CURRENT config lists (not from every file that happens to sit in the store)."""

    ROLE_LABELS = {("judgeprior", "owner"): ["zebra crossing permit", "quantum ledger audit"],
                   ("judgeprior", "officer"): ["violet kiln inspection", "marble harbour tariff"],
                   ("secondsrc", "owner"): ["nimbus orchard levy", "copper lantern census"],
                   ("secondsrc", "officer"): ["velvet tundra ballot", "amber glacier clause"]}

    def setUp(self):
        from second_scribe import pipeline
        from second_scribe.schemas import MapGraph, Node
        self.pipeline = pipeline
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(Path(self._t.name) / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        d["script_sources"] = [SRC, {**SRC, "name": "secondsrc"}]
        cfg_path = Path(self._t.name) / "c.json"
        cfg_path.write_text(json.dumps(d))
        self.cfg = RunConfig.load(cfg_path)
        self.work = pipeline.Work(self.cfg.out_dir)

        def put(name, labels):
            g = MapGraph("s", "prior_only", [Node(f"n{i}", t) for i, t in enumerate(labels)])
            self.work.put("script_prior_only", name, {"status": "ok", "map": pipeline.asdict(g)})
        self.put = put
        for src in self.cfg.script_sources:
            for spec in self.cfg.source_specs(src):
                for role in ("owner", "officer"):
                    put(f"{src['name']}__{role}__{spec.key()}", self.ROLE_LABELS[(src["name"], role)])

        class R:
            cfg = self.cfg
            work = self.work

            def roles(self):
                return ["owner", "officer"]
        self.r = R()

    def labels(self, source, role):
        sc = self.pipeline.source_scripts(self.r, source).get(role)
        return sorted(n.text for n in sc.nodes) if sc else []

    def test_each_role_script_holds_only_its_own_roles_nodes(self):
        for src in self.cfg.script_sources:
            for role in ("owner", "officer"):
                self.assertEqual(self.labels(src, role), sorted(self.ROLE_LABELS[(src["name"], role)]),
                                 (src["name"], role))

    def test_a_second_source_does_not_leak_into_the_first(self):
        first = self.cfg.script_sources[0]
        everything = {t for v in self.ROLE_LABELS.values() for t in v}
        got = set(self.labels(first, "owner")) | set(self.labels(first, "officer"))
        self.assertTrue(got)
        self.assertFalse(got & {t for k, v in self.ROLE_LABELS.items() if k[0] == "secondsrc" for t in v}, everything)

    def test_a_stale_draw_the_config_no_longer_lists_is_not_pooled(self):
        first = self.cfg.script_sources[0]
        # a sample index the config does not list (samples=2): an earlier session's draw, still on disk
        self.put("judgeprior__owner__p0__t0.7__r9", ["stale lighthouse mortgage", "stale lighthouse mortgage rider"])
        self.put("judgeprior__owner__p0__t0.7__r8", ["stale lighthouse mortgage", "stale lighthouse mortgage rider"])
        self.assertEqual(self.labels(first, "owner"), sorted(self.ROLE_LABELS[("judgeprior", "owner")]))


class TestRealRunWithASource(RealBase):
    def test_a_real_run_with_a_script_source_runs_and_verifies(self):
        d = json.loads(self.cfg.read_text())
        d["script_sources"] = [SRC]
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        self.cfg.write_text(json.dumps(d))
        os.environ.update(SS_RECON_MODEL="alpha-qwen-1", SS_JUDGE_MODEL="beta-deepseek-1")   # families the table knows
        rc, err = self.run_all("all")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["script_sources"]["judgeprior"]["family"], "other")      # qwen vs deepseek
        self.assertEqual(agg["headline"]["absences"]["script_conditional"]["primary_source"], "judgeprior")


class TestScriptItemsAreInTheProvenanceCheck(RealBase):
    """Review M-4 (prereg): script_prior_only items are checked like any model-stage item (own stage, names a call)."""

    def setUp(self):
        super().setUp()
        d = json.loads(self.cfg.read_text())
        d["script_sources"] = [SRC]
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        self.cfg.write_text(json.dumps(d))
        self.assertEqual(self.run_all("all")[0], 0)
        self.item = sorted((self.work / "script_prior_only").glob("*.json"))[0]
        self.data = json.loads(self.item.read_text())

    def problem(self, **change):
        from second_scribe import prereg
        self.item.write_text(json.dumps({**self.data, **change}))
        return prereg.run_validity_problem(self.log, self.work)

    def test_untouched_items_pass(self):
        self.assertIsNone(self.problem())

    def test_an_item_that_names_no_call_is_a_problem(self):
        self.assertIn("script_prior_only", self.problem(request_ids=[]))

    def test_an_item_that_names_another_stages_call_is_a_problem(self):
        calls = [json.loads(l)["payload"] for l in self.log.read_text().splitlines()
                 if json.loads(l)["record_type"] == "call_request"]
        other = next(c["request_id"] for c in calls if c["stage"].startswith("recon:"))
        self.assertIn("another stage", self.problem(request_ids=[other]))


class TestRealRunWithAJudge2Source(RealBase):
    """Review fix I-1: a script source of role judge2 is logged under the judge2 role's stage, so the real run
    verifies and resumes (it used to log under the judge role, which then held two model ids)."""

    def setUp(self):
        super().setUp()
        d = json.loads(self.cfg.read_text())
        d["models"]["judge2"] = "JUDGE2"
        d["script_sources"] = [{**SRC, "name": "second", "role": "judge2"}]
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        self.cfg.write_text(json.dumps(d))
        patcher = mock.patch.dict(os.environ, {"SS_JUDGE2_MODEL": "gamma-gemma-2", "SS_RECON_MODEL": "alpha-qwen-1",
                                               "SS_JUDGE_MODEL": "beta-deepseek-1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_runs_verifies_and_resumes(self):
        rc, err = self.run_all("all")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)
        calls = [json.loads(l)["payload"] for l in self.log.read_text().splitlines()
                 if json.loads(l)["record_type"] == "call_request"]
        mine = [c for c in calls if "script_prior_only" in c["stage"]]
        self.assertTrue(mine)
        self.assertEqual({c["stage"] for c in mine}, {"judge2:script_prior_only"})
        self.assertEqual({c["model_id"] for c in mine}, {"gamma-gemma-2"})
        rc, err = self.run_all("all")                       # resume: the instrument is the one the log records
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["script_sources"]["second"]["family"], "other")      # qwen vs gemma
        self.assertEqual(agg["script_sources"]["second"]["role"], "judge2")


if __name__ == "__main__":
    unittest.main()
