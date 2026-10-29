import contextlib
import copy
import io
import json
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

from second_scribe import cli as cli_module
from second_scribe import pipeline, prereg
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG
from second_scribe.cli import main as cli_main
from second_scribe.pipeline import STAGE_GROUPS, RunConfig, expand_stages


def records(log_path):
    with open(log_path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def main(argv):
    """The CLI, with its printed trap table and verify line kept out of the test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return cli_main(argv)


def demo(out, *extra):
    return main(["demo", "--out", str(out), "--client", "oracle", *extra])


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cls.fixtures = str(Path(tempfile.mkdtemp()) / "fixtures.json")
        cls.rc = demo(cls.out, "--record", cls.fixtures)
        cls.log = cls.out / "logs" / "run.jsonl"

    def test_oracle_demo_end_to_end(self):
        self.assertEqual(self.rc, 0)
        agg = json.loads((self.out / "aggregates.json").read_text())
        self.assertEqual(agg["primary_spec"], "removed__p0__t0__r0")
        self.assertEqual(agg["absences"]["primary"]["leaked_as_existing"], 0)
        self.assertGreater(agg["absences"]["primary"]["n_wished"], 0)
        self.assertGreaterEqual(agg["buckets"]["by_spec"]["full__p0__t0__r0"]["node_recall"], 0.95)
        self.assertEqual(agg["n_analysed"], 12)
        self.assertEqual(agg["exclusions"], {})
        traps = (self.out / "traps.md").read_text()
        self.assertNotIn("FAIL", traps)
        for arm in ("full", "removed"):               # per-arm expectations are both checked
            self.assertIn(f"| {arm} |", traps)
        self.assertIn("| interviewer_voiced | C", traps)
        self.assertTrue((self.out / "report.md").exists())
        self.assertEqual(main(["verify", str(self.log)]), 0)

    def test_run_records_prompt_set_and_models_without_endpoints(self):
        recs = records(self.log)
        start = recs[0]
        self.assertEqual(start["record_type"], "run_start")
        manifest = json.loads((Path(DEMO_CONFIG).parent.parent / "prompts" / "MANIFEST.json").read_text())
        self.assertEqual(start["payload"]["prompt_set_sha256"], manifest["set_sha256"])
        agg = json.loads((self.out / "aggregates.json").read_text())
        self.assertEqual(agg["prompt_set_sha256"], manifest["set_sha256"])
        self.assertEqual(agg["models"], {"recon": "oracle", "judge": "oracle"})
        self.assertEqual(recs[-1]["record_type"], "run_end")
        stages = {r["payload"]["stage"] for r in recs if r["record_type"] == "call_request"}
        self.assertEqual(stages, {"q:mapping_talk", "judge:in_transcript", "recon:full", "recon:removed", "prior_only",
                                  "judge:modality", "judge:shuffled", "judge:canon", "judge:align",
                                  "judge:script_prior_only",          # the demo's script source (L75)
                                  # the demo config carries the instruction ladder (one draw per arm, C7): one recon
                                  # stage per rung; rung 2 draw 0 is the main run's own map, so it makes no call
                                  "recon:removed:rung1", "recon:removed:rung3", "recon:removed:placebo"})

    def test_controls_behave_on_the_oracle(self):
        h = json.loads((self.out / "aggregates.json").read_text())["headline"]
        sq = h["controls"]["shuffled_quotes"]
        self.assertEqual((sq["real_share_mean"], sq["shuffled_share_mean"]), (1.0, 0.0))
        self.assertEqual(h["pull_toward_script"]["label_source"], "canonical")
        self.assertLess(h["prior_only_baseline"]["mean_prior_recall"], h["prior_only_baseline"]["mean_ai_recall"])

    def test_resume_skips_done_items(self):
        out = Path(tempfile.mkdtemp())
        demo(out)
        n1 = len(records(out / "logs" / "run.jsonl"))
        first = (out / "aggregates.json").read_bytes()
        self.assertEqual(demo(out), 0)
        recs = records(out / "logs" / "run.jsonl")
        self.assertLess(len(recs) - n1, 5)       # only run_start/run_end style records, no new calls
        self.assertFalse([r for r in recs[n1:] if r["record_type"] == "call_request"])
        self.assertEqual((out / "aggregates.json").read_bytes(), first)
        self.assertEqual(main(["verify", str(out / "logs" / "run.jsonl")]), 0)

    def test_replay_of_recorded_fixtures_reproduces_aggregates_byte_for_byte(self):
        stored = json.loads(Path(self.fixtures).read_text())
        rec = next(v for k, v in stored.items() if k != "_meta" and "calls" not in v)   # a request made once
        self.assertEqual(set(rec), {"text", "finish_reason", "usage", "role", "temperature"})   # tagged (fix round 2)
        for k, v in stored.items():       # a request made again (a determinism re-ask) keeps every call, in order
            if k != "_meta" and "calls" in v:
                self.assertTrue(len(v["calls"]) >= 2 and all(set(c) == set(rec) for c in v["calls"]), k)
        out = Path(tempfile.mkdtemp())
        self.assertEqual(main(["demo", "--out", str(out), "--client", "replay", "--fixtures", self.fixtures]), 0)
        self.assertEqual((out / "aggregates.json").read_bytes(), (self.out / "aggregates.json").read_bytes())
        self.assertEqual((out / "traps.md").read_bytes(), (self.out / "traps.md").read_bytes())

    def test_fresh_recording_replaces_stale_fixtures_and_resume_keeps_them(self):
        out = Path(tempfile.mkdtemp())
        fx = out / "fx.json"
        fx.write_text(json.dumps({"stale": "old answer"}))
        demo(out / "run", "--record", str(fx))
        first = json.loads(fx.read_text())
        self.assertNotIn("stale", first)
        demo(out / "run", "--record", str(fx))              # resumed: no pipeline calls, nothing lost
        again = json.loads(fx.read_text())
        self.assertEqual(set(again), set(first))
        for k, v in first.items():
            if k == "_meta":
                self.assertEqual(again[k], v)
                continue
            before = v["calls"] if "calls" in v else [v]
            after = again[k]["calls"] if "calls" in again[k] else [again[k]]
            # what was recorded stays first and in order; the resumed session's determinism re-asks (sent straight
            # to the client, so they are calls too) are appended after it
            self.assertEqual(after[:len(before)], before, k)

    def test_committed_fixtures_replay_without_misses(self):
        out = Path(tempfile.mkdtemp())
        self.assertEqual(main(["demo", "--out", str(out), "--client", "replay"]), 0)
        agg = json.loads((out / "aggregates.json").read_text())
        self.assertFalse([s for s, c in agg["items"].items() if set(c) - {"ok"}], agg["items"])
        self.assertFalse([r for r in records(out / "logs" / "run.jsonl") if r["record_type"] == "call_error"])

    def test_a_two_stage_recording_keeps_both_stages_and_replays(self):
        out = Path(tempfile.mkdtemp())
        (out / "run").mkdir()
        cfg = TestStagesAndFailures._config(out / "run")
        fx = str(out / "fx.json")
        self.assertEqual(main(["run", "--config", cfg, "--stages", "Q", "--client", "oracle", "--record", fx]), 0)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "J,M", "--client", "oracle", "--record", fx]), 0)
        replay = Path(tempfile.mkdtemp())
        self.assertEqual(main(["demo", "--out", str(replay), "--client", "replay", "--fixtures", fx]), 0)
        self.assertEqual((replay / "aggregates.json").read_bytes(), (self.out / "aggregates.json").read_bytes())

    def test_a_replay_with_a_missing_fixture_exits_non_zero(self):
        stored = json.loads(Path(self.fixtures).read_text())
        dropped = next(k for k, v in stored.items()
                       if k != "_meta" and '"labels"' in (v["calls"][0] if "calls" in v else v)["text"])
        del stored[dropped]
        fx = Path(tempfile.mkdtemp()) / "fx.json"
        fx.write_text(json.dumps(stored))
        out = Path(tempfile.mkdtemp())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = main(["demo", "--out", str(out), "--client", "replay", "--fixtures", str(fx)])
        self.assertNotEqual(rc, 0)
        self.assertIn(dropped, err.getvalue())
        self.assertEqual(main(["verify", str(out / "logs" / "run.jsonl")]), 0)   # the log is still closed cleanly

    def test_item_files_are_written_per_stage(self):
        work = self.out / "work"
        self.assertEqual(len(list((work / "recon").glob("*.json"))), 12 * 6)
        self.assertEqual(len(list((work / "prior_only").glob("*.json"))), 2 * 3 * 2 + 2)   # + one reading-3 map per role
        d = json.loads((work / "recon" / "SYN-OW01__removed__p0__t0__r0.json").read_text())
        self.assertEqual(d["status"], "ok")
        self.assertFalse(list(work.rglob("*.tmp")))


class TestStagesAndFailures(unittest.TestCase):
    def test_relative_paths_resolve_against_the_config_folder(self):
        cfg = RunConfig.load(DEMO_CONFIG)
        self.assertEqual(Path(cfg.out_dir), (Path(DEMO_CONFIG).parent / "../out/demo").resolve())
        self.assertEqual(RunConfig.load(DEMO_CONFIG, out_dir="/tmp/x").out_dir, "/tmp/x")
        self.assertEqual(cfg.sha256(), RunConfig.load(DEMO_CONFIG, out_dir="/tmp/x").sha256())

    def test_stage_groups_expand_in_order(self):
        self.assertEqual(expand_stages("Q"), STAGE_GROUPS["Q"])
        self.assertEqual(expand_stages("M,Q"), STAGE_GROUPS["Q"] + STAGE_GROUPS["M"])
        self.assertEqual(expand_stages("all", only="align"), ["align"])
        with self.assertRaises(ValueError):
            expand_stages("X")

    def test_groups_run_separately_then_resume(self):
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "Q", "--client", "oracle"]), 0)
        self.assertFalse((out / "work" / "judge_modality").exists())
        self.assertEqual(main(["run", "--config", cfg, "--stages", "M", "--client", "oracle"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        # Q done, J missing (the off-record check is a judge stage now): every participant is excluded, counted
        self.assertEqual(agg["n_analysed"], 0)
        self.assertEqual(agg["exclusions"]["in_transcript_missing"], 12)
        self.assertEqual(agg["models"], {"recon": "oracle"})       # a Q-only session builds the recon client alone
        self.assertEqual(main(["run", "--config", cfg, "--stages", "J,M", "--client", "oracle"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {})
        recs = records(out / "logs" / "run.jsonl")
        recon_calls = [r for r in recs if r["record_type"] == "call_request"
                       and r["payload"]["stage"] in ("recon:full", "recon:removed", "prior_only")]
        self.assertEqual(len(recon_calls), 12 * 6 + 12 + 2)      # J,M never re-ran a reconstruction (+2: reading 3)
        self.assertEqual(main(["report", "--config", cfg]), 0)
        self.assertTrue((out / "aggregates" / "report.md").exists())

    def test_failed_reconstructions_are_recorded_and_excluded(self):
        from second_scribe import cli

        out = Path(tempfile.mkdtemp())
        real_build = cli.build_clients

        def build(kind, cfg, fixtures=None):
            clients = real_build("oracle", cfg, fixtures)
            oracle = clients["recon"]

            class FailRecon:
                model_label = "oracle"

                def complete(self, messages, **kw):
                    user = next(m["content"] for m in messages if m["role"] == "user")
                    if '"nodes"' in user and "TRANSCRIPT:\n" in user:
                        raise RuntimeError("model call failed: HTTP 400")      # a refusal, not an outage (see test_outage)
                    return oracle.complete(messages, **kw)
            return {**clients, "recon": FailRecon()}
        cli.build_clients = build
        try:
            self.assertEqual(demo(out), 0)
        finally:
            cli.build_clients = real_build
        agg = json.loads((out / "aggregates.json").read_text())
        self.assertEqual(agg["items"]["recon"], {"failed": 72})
        self.assertEqual(agg["buckets"]["primary"]["n_excluded"], 12)
        self.assertIn("ai_failed", agg["buckets"]["primary"]["excluded_reasons"])
        # a failed item is done: the rerun does not call the model again
        n1 = len(records(out / "logs" / "run.jsonl"))
        cli.build_clients = build
        try:
            demo(out)
        finally:
            cli.build_clients = real_build
        main_stages = ("recon:full", "recon:removed")
        self.assertFalse([r for r in records(out / "logs" / "run.jsonl")[n1:]
                          if r["record_type"] == "call_request" and r["payload"]["stage"] in main_stages])
        # the ladder's rungs classify the call like the main run (final review I-1): an HTTP 400 is no outage, so a
        # rung's refused map is a stored "failed" (like the main run's) and ladder_missing has no gap to redo. Only a
        # call whose last attempt was a transport error is "error" (not stored, listed by ladder_missing).
        self.assertEqual(pipeline.ladder_missing(RunConfig.load(DEMO_CONFIG, out_dir=str(out))), {})

    def test_unanswered_judge_batches_are_not_judged_and_flag_the_spec(self):
        from second_scribe import cli
        out = Path(tempfile.mkdtemp())
        real_build = cli.build_clients

        def build(kind, cfg, fixtures=None):
            clients = real_build("oracle", cfg, fixtures)
            oracle = clients["judge"]

            class NoModalityJudge:
                model_label = "oracle"

                def complete(self, messages, **kw):
                    user = next(m["content"] for m in messages if m["role"] == "user")
                    if '{"labels"' in user:                    # every modality batch fails
                        raise RuntimeError("model call failed: HTTP 400")      # a refusal, not an outage (see test_outage)
                    return oracle.complete(messages, **kw)
            return {**clients, "judge": NoModalityJudge()}
        cli.build_clients = build
        try:
            self.assertEqual(demo(out), 0)
        finally:
            cli.build_clients = real_build
        agg = json.loads((out / "aggregates.json").read_text())
        j = agg["judging"]["primary"]
        self.assertEqual(j["not_judged_share"], 1.0)
        self.assertEqual(j["interpretation"], "reported_not_interpreted")
        b = agg["buckets"]["primary"]
        self.assertEqual(b["interpretation"], "reported_not_interpreted")
        self.assertEqual((b["unanchored"], b["matched"], b["attested_absent_from_map"]), (0, 0, 0))
        n_nodes = sum(len(json.loads(f.read_text())["map"]["nodes"])
                      for f in (out / "work" / "recon").glob("*__removed__p0__t0__r0.json"))
        self.assertEqual(b["n_not_judged"], n_nodes)
        a = agg["absences"]["primary"]
        self.assertEqual(a["leaked_as_existing"], 0)               # every wished node is recovered as wished
        self.assertEqual(agg["headline"]["interpretation"], "reported_not_interpreted")

    def test_helper_fallbacks_are_visible_retried_and_never_used(self):
        """Every judge-endpoint answer is invalid (as if the judge were broken): alignments and canonical labels
        fall back. They must be stored as "fallback", counted, redone on resume, and never feed a claim."""
        from second_scribe import cli
        from second_scribe.llm import ModelResponse
        out = Path(tempfile.mkdtemp())
        real_build = cli.build_clients

        def build(kind, cfg, fixtures=None):
            clients = real_build("oracle", cfg, fixtures)

            class BrokenJudge:
                model_label = "broken"

                def complete(self, messages, **kw):
                    user = next(m["content"] for m in messages if m["role"] == "user")
                    if '{"answers"' in user:             # the off-record check (a judge stage) still works
                        return clients["judge"].complete(messages, **kw)
                    return ModelResponse("this is not JSON", "stop", {}, 0, "x")
            return {**clients, "judge": BrokenJudge()}
        cli.build_clients = build
        try:
            self.assertEqual(demo(out), 0)
            agg = json.loads((out / "aggregates.json").read_text())
            n_items = sum(agg["items"]["align"].values())
            self.assertEqual(agg["items"]["align"], {"fallback": n_items})
            self.assertEqual(set(agg["items"]["canonicalize"]), {"fallback"})
            self.assertEqual(agg["fallbacks"]["align"]["items"], n_items)
            # ai_endorsed and ai_script; no canonical item, as canonicalization fell back too
            self.assertEqual(agg["fallbacks"]["align"]["by_spec"]["removed__p0__t0__r0"], 12 * 2)
            self.assertEqual(agg["exclusions"]["j_inputs_missing"]["removed__p0__t0__r0"], 12)
            h = agg["headline"]
            self.assertNotEqual(h["pull_toward_script"]["label_source"], "canonical")
            claims = []

            def walk(x):
                if isinstance(x, dict):
                    for k, v in x.items():
                        if k.startswith("claim_"):
                            claims.append(v)
                        walk(v)
            walk(h)
            self.assertTrue(claims)
            self.assertNotIn(True, claims)
            # resume redoes the fallback items (they are not done)
            n1 = len(records(out / "logs" / "run.jsonl"))
            demo(out)
            again = [r for r in records(out / "logs" / "run.jsonl")[n1:] if r["record_type"] == "call_request"]
            self.assertTrue({"judge:align", "judge:canon"} <= {r["payload"]["stage"] for r in again})
        finally:
            cli.build_clients = real_build

    def test_missing_alignments_are_left_out_not_scored_as_nothing_matched(self):
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "all", "--client", "oracle"]), 0)
        (out / "work" / "align" / "SYN-OW01__prior_endorsed.json").unlink()
        (out / "work" / "align" / "SYN-OW02__endorsed_script.json").unlink()
        (out / "work" / "align" / "SYN-OW03__ai_script__removed__p0__t0__r0.json").unlink()
        self.assertEqual(main(["run", "--config", cfg, "--stages", "M"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {"prior_endorsed_alignment_missing": 1,
                                             "endorsed_script_alignment_missing": 1,
                                             "ai_script_alignment_missing": {"removed__p0__t0__r0": 1}})
        h = agg["headline"]
        self.assertEqual(h["prior_only_baseline"]["excluded_reasons"], {"alignment_missing": 1})
        self.assertEqual(h["pull_toward_script"]["robustness"]["raw_labels"]["excluded_reasons"],
                         {"alignment_missing": 2})
        self.assertEqual(agg["buckets"]["primary"]["n"], 12)            # the spec itself is still analysed

    def test_shuffled_items_render_like_real_ones_and_count_toward_the_flag(self):
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "all", "--client", "oracle"]), 0)
        shuffled = [r["payload"]["messages"][-1]["content"] for r in records(out / "logs" / "run.jsonl")
                    if r["record_type"] == "call_request" and r["payload"]["stage"] == "judge:shuffled"]
        self.assertTrue(shuffled)
        self.assertTrue(any("QUOTE (participant):" in m for m in shuffled))
        self.assertTrue(any("REPLY (participant):" in m for m in shuffled))   # an interviewer quote keeps its reply
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["judging"]["primary"]["interpretation"], "interpreted")
        for f in (out / "work" / "judge_shuffled").glob("*.json"):           # the re-judge never answered
            d = json.loads(f.read_text())
            d["not_judged"] = sorted(d["labels"] or [])
            d["n_items"] = len(d["labels"] or [])
            f.write_text(json.dumps(d))
        self.assertEqual(main(["run", "--config", cfg, "--stages", "M"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["judging"]["primary"]["shuffled"]["not_judged_share"], 1.0)
        self.assertEqual(agg["judging"]["primary"]["interpretation"], "reported_not_interpreted")
        self.assertEqual(agg["headline"]["controls"]["shuffled_quotes"]["excluded_reasons"], {"no_judged_nodes": 12})

    def test_a_judge_item_for_a_replaced_map_is_redone(self):
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "all", "--client", "oracle"]), 0)
        f = out / "work" / "recon" / "SYN-OW01__full__p0__t0__r0.json"
        d = json.loads(f.read_text())
        d["map"]["nodes"] = d["map"]["nodes"][:-1]                          # the map changed after judging
        d["map"]["edges"] = []
        f.write_text(json.dumps(d))
        self.assertEqual(main(["run", "--config", cfg, "--stages", "M"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {"j_inputs_missing": {"full__p0__t0__r0": 1}})   # stale labels never used
        self.assertEqual(main(["run", "--config", cfg, "--stages", "J,M", "--client", "oracle"]), 0)
        from second_scribe.pipeline import map_digest
        from second_scribe.schemas import MapGraph
        item = json.loads((out / "work" / "judge_modality" / "SYN-OW01__full__p0__t0__r0.json").read_text())
        self.assertEqual(item["map_sha256"], map_digest(MapGraph.from_json(json.dumps(d["map"]))))
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {})

    def test_a_leaky_oracle_is_caught(self):
        """Negative control: an oracle that draws every wished concept as experienced must show leakage and fail
        the wished traps (otherwise a clean trap table would prove nothing)."""
        from second_scribe import cli
        from second_scribe.demo_oracle import OracleClient
        out = Path(tempfile.mkdtemp())
        real_build = cli.build_clients

        class LeakyOracle(OracleClient):
            def _reconstruct(self, user):
                ans = super()._reconstruct(user)
                for x in ans["nodes"] + ans["edges"]:
                    x["status"] = "experienced"
                return ans

        def build(kind, cfg, fixtures=None):
            c = cfg.corpus
            leaky = LeakyOracle(cfg.path(c["synthetic_dir"]), cfg.context["roles"])
            leaky.model_label = "leaky"
            return {"recon": leaky, "judge": leaky}
        cli.build_clients = build
        try:
            self.assertEqual(demo(out), 0)
        finally:
            cli.build_clients = real_build
        agg = json.loads((out / "aggregates.json").read_text())
        a = agg["absences"]["primary"]
        self.assertEqual(a["leaked_as_existing"], a["n_wished"])
        self.assertEqual(a["leaked_quote_wished"], a["n_wished"])        # the AI contradicts its own quotes
        self.assertGreater(a["n_wished"], 0)
        traps = (out / "traps.md").read_text()
        self.assertIn("| wished | C", traps)
        wished = [line for line in traps.splitlines() if "| wished |" in line]
        self.assertTrue(wished)
        self.assertTrue(all(line.endswith("| leaked_as_existing | FAIL |") for line in wished), wished)

    def test_stale_canonicalization_batches_are_removed(self):
        from second_scribe.pipeline import Work, canonical_names
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "all", "--client", "oracle"]), 0)
        stale = out / "work" / "canonicalize" / "batch_0099.json"
        stale.write_text(json.dumps({"status": "ok", "labels": ["startup plan"], "concepts": ["WRONG"]}))
        self.assertEqual(canonical_names(Work(out))["startup plan"], "WRONG")      # what a stale batch would do
        self.assertEqual(main(["run", "--config", cfg, "--only", "canonicalize", "--client", "oracle"]), 0)
        self.assertFalse(stale.exists())
        self.assertEqual(canonical_names(Work(out))["startup plan"], "C01")

    def test_a_second_judge_runs_as_judge2_modality(self):
        out = Path(tempfile.mkdtemp())
        d = json.loads(Path(self._config(out)).read_text())
        d["models"]["judge2"] = "JUDGE2"
        cfg = out / "config2.json"
        cfg.write_text(json.dumps(d))
        self.assertEqual(main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"]), 0)
        stages = {r["payload"]["stage"] for r in records(out / "logs" / "run.jsonl") if r["record_type"] == "call_request"}
        self.assertIn("judge2:modality", stages)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["models"]["judge2"], "oracle")
        self.assertEqual(agg["headline"]["second_judge"]["agreement"], 1.0)

    def test_a_transcript_without_speaker_labels_is_left_out_of_the_removed_arm(self):
        import re
        import shutil
        from second_scribe.synth.docx_writer import write_docx
        from second_scribe.transcripts import read_docx_text
        out = Path(tempfile.mkdtemp())
        corpus = out / "corpus"
        shutil.copytree(Path(DEMO_CONFIG).parent.parent / "demo" / "corpus", corpus)
        doc = corpus / "transcripts" / "SYN-OW01.docx"
        paras = [re.sub(r"^\[[A-Z]+\]\s*|^[A-Z]+ \d+:\d\d\s*", "", x) for x in read_docx_text(doc)]
        write_docx(doc, paras)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out / "run"), base_dir=str(out))
        d["corpus"].update(maps_glob="corpus/maps/*.vsdx", transcripts_glob="corpus/transcripts/*.docx",
                           synthetic_dir="corpus")
        cfg = out / "config.json"
        cfg.write_text(json.dumps(d))
        self.assertEqual(main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"]), 0)
        agg = json.loads((out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {"no_speaker_labels": 1})
        self.assertEqual(agg["mapping_talk"]["files_without_speakers"], 1)
        self.assertEqual(agg["items"]["recon"], {"ok": 69, "no_speakers_excluded": 3})
        self.assertEqual(agg["buckets"]["by_spec"]["removed__p0__t0__r0"]["n"], 11)
        self.assertEqual(agg["buckets"]["by_spec"]["full__p0__t0__r0"]["n"], 12)

    def test_real_corpus_aggregates_pass_the_guard(self):
        out = Path(tempfile.mkdtemp())
        cfg = self._config(out, kind="real")
        self._freeze(cfg)
        patch = unittest.mock.patch.object(prereg, "check_code", return_value=None)   # the temp dir is not a checkout
        patch.start()
        self.addCleanup(patch.stop)
        real_build = cli_module.build_clients

        def with_model_ids(kind, cfg, fixtures=None):      # a real corpus needs calls that name their model
            out = {}
            for role, c in real_build(kind, cfg, fixtures).items():
                c = copy.copy(c)
                c.cfg = types.SimpleNamespace(model_id=f"{role}.gguf", params={}, api_key_env=None)
                out[role] = c
            return out
        patch2 = unittest.mock.patch.object(cli_module, "build_clients", with_model_ids)
        patch2.start()
        self.addCleanup(patch2.stop)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "all", "--client", "oracle"]), 0)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertNotIn("traps", agg)
        self.assertFalse((out / "aggregates" / "traps.md").exists())
        text = (out / "aggregates" / "aggregates.json").read_text()
        self.assertNotIn("startup plan", text)                   # a gold label: never in the aggregates
        # without canonical labels the pull reports missing prior samples: as a count, never with participant ids
        for f in (out / "work" / "canonicalize").glob("*.json"):
            f.unlink()
        # (deleting logged work items by hand is exactly what the post-run verify now reports: rc 1; the aggregates
        # are written before that check)
        self.assertEqual(main(["run", "--config", cfg, "--stages", "M"]), 1)
        agg = json.loads((out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["headline"]["pull_toward_script"]["missing_prior_samples"], 2 * 3 * 2)   # roles x variants x samples, each once
        self.assertNotIn("SYN-OW01:", (out / "aggregates" / "aggregates.json").read_text())
        pull = agg["headline"]["pull_toward_script"]
        self.assertIn("canon_ai_missing", pull["fallback_reasons"])
        self.assertEqual(pull["fallback_counts"]["canon_ai_missing"]["removed__p0__t0__r0"], 12)

    @staticmethod
    def _config(out: Path, kind: str = "synthetic") -> str:
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d["out_dir"] = str(out)
        d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        d["corpus"]["kind"] = kind
        if kind == "real":                       # a real corpus must name its participant id map (fixture, not real)
            m = out / "participant_id_map.json"
            m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
            d["corpus"]["participant_id_map"] = str(m)
        d["prereg_required"] = kind == "real"
        p = out / "config.json"
        p.write_text(json.dumps(d))
        return str(p)

    @staticmethod
    def _freeze(config: str) -> None:
        """A prereg fixture for a real-kind config: frozen into the run's log, as `prereg freeze` would."""
        cfg = RunConfig.load(config)
        log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        info = {r: {"model_id": f"{r}.gguf", "weights": "a" * 64, "params": {}} for r in ("recon", "judge")}
        log = AuditLog(log_path, cfg.run_id)
        prereg.freeze(prereg.build_prereg(cfg, prereg.PROMPTS_DIR, code_commit="0" * 40, model_info=info,
                                          decision_rules=prereg.DECISION_RULES,
                                          calibration={"matcher": {"n": 1}, "in_transcript": {"n": 1}}),
                      prereg.prereg_path(cfg), log)
        log.close()


class TestPrivacyPaths(unittest.TestCase):
    def cfg_dict(self, out):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out), base_dir=str(Path(DEMO_CONFIG).parent))
        return d

    def test_an_unknown_or_missing_corpus_kind_is_refused_at_load(self):
        out = Path(tempfile.mkdtemp())
        for kind in ("Real", "REAL", "", None, "pilot"):
            d = self.cfg_dict(out)
            if kind is None:
                d["corpus"].pop("kind")
            else:
                d["corpus"]["kind"] = kind
            p = out / "c.json"
            p.write_text(json.dumps(d))
            with self.assertRaises(ValueError, msg=repr(kind)):
                RunConfig.load(p)

    def test_a_real_corpus_does_not_need_the_prereg_requirement(self):
        # decision L6: no pre-registration lock; the optional flag may be absent, false or true
        out = Path(tempfile.mkdtemp())
        d = self.cfg_dict(out)
        d["corpus"]["kind"] = "real"
        p = out / "c.json"
        for value in (False, None, True):
            d["prereg_required"] = value
            p.write_text(json.dumps(d))
            self.assertEqual(bool(RunConfig.load(p).prereg_required), bool(value))
        d.pop("prereg_required")
        p.write_text(json.dumps(d))
        self.assertFalse(RunConfig.load(p).prereg_required)

    def test_record_is_refused_for_a_real_corpus(self):
        out = Path(tempfile.mkdtemp())
        d = self.cfg_dict(out / "run")
        d["corpus"]["kind"] = "real"
        d["prereg_required"] = True
        p = out / "c.json"
        p.write_text(json.dumps(d))
        fx = out / "fx.json"
        with contextlib.redirect_stderr(io.StringIO()):
            rc = main(["run", "--config", str(p), "--stages", "Q", "--client", "oracle", "--record", str(fx)])
        self.assertNotEqual(rc, 0)
        self.assertFalse(fx.exists())
        self.assertFalse((out / "run" / "work").exists())        # nothing ran

    def test_demo_live_does_not_record_into_a_tracked_file(self):
        text = (Path(DEMO_CONFIG).parent.parent / "Makefile").read_text()
        live = next(line for line in text.splitlines() if "--client http" in line)
        self.assertNotIn("demo/fixtures.json", live)

    def test_model_ids_are_reduced_to_a_basename(self):
        from second_scribe.pipeline import model_ids

        class C:
            class cfg:
                model_id = "/Users/x/models/foo.gguf"
        self.assertEqual(model_ids({"recon": C(), "judge": C()}), {"recon": "foo.gguf", "judge": "foo.gguf"})

        class W:
            model_label = "C:\\models\\bar.gguf"
        self.assertEqual(model_ids({"judge": W()}), {"judge": "bar.gguf"})


class TestAggregateGuard(unittest.TestCase):
    def test_aggregate_guard_blocks_labels(self):
        from second_scribe.report import assert_aggregate_only
        with self.assertRaises(ValueError):
            assert_aggregate_only({"x": "Family doctor visit"}, forbidden={"Family doctor visit"})

    def test_guard_blocks_long_strings_keys_and_nested_labels_without_echoing_them(self):
        from second_scribe.report import assert_aggregate_only
        with self.assertRaises(ValueError) as cm:
            assert_aggregate_only({"a": ["x" * 41]})
        self.assertNotIn("xxxx", str(cm.exception))
        with self.assertRaises(ValueError):
            assert_aggregate_only({"family  DOCTOR visit": 3}, forbidden={"Family doctor visit"})
        with self.assertRaises(ValueError) as cm:
            assert_aggregate_only({"a": [{"b": "Family doctor visit"}]}, forbidden={"Family doctor visit"})
        self.assertNotIn("doctor", str(cm.exception))
        assert_aggregate_only({"n": 3, "rate": 0.5, "spec": "removed__p0__t0__r0", "ok": [None, True],
                               "sha256": "0" * 64}, forbidden={"Family doctor visit"})
        with self.assertRaises(ValueError):                    # only a whole hex digest is exempt
            assert_aggregate_only({"x": "0" * 63 + " family doctor visit"})

    def test_kit_notes_are_an_exact_match_allowlist_still_checked_against_labels(self):
        from second_scribe.metrics import CONTRADICTION_NOTE
        from second_scribe.report import KIT_NOTES, assert_aggregate_only
        self.assertEqual(KIT_NOTES, frozenset({CONTRADICTION_NOTE}))
        assert_aggregate_only({"note": CONTRADICTION_NOTE}, allowed=KIT_NOTES)
        with self.assertRaises(ValueError):                    # not allowed unless listed
            assert_aggregate_only({"note": CONTRADICTION_NOTE})
        with self.assertRaises(ValueError):                    # exact match only
            assert_aggregate_only({"note": CONTRADICTION_NOTE + " "}, allowed=KIT_NOTES)
        with self.assertRaises(ValueError):                    # still checked against labels and quotes
            assert_aggregate_only({"note": CONTRADICTION_NOTE}, forbidden={CONTRADICTION_NOTE}, allowed=KIT_NOTES)
        with self.assertRaises(ValueError):                    # a digest is exempt only as a whole digest
            assert_aggregate_only({"h": "a" * 64 + "\n"})
        with self.assertRaises(ValueError):
            assert_aggregate_only({"h": "A" * 64})
        with self.assertRaises(ValueError):                    # a digest is still checked against labels
            assert_aggregate_only({"h": "a" * 64}, forbidden={"a" * 64})

    def test_outputs_are_written_atomically_with_fsync(self):
        import os
        from unittest import mock
        from second_scribe import report
        out = Path(tempfile.mkdtemp())
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
        with mock.patch.object(os, "fsync", wraps=os.fsync) as fs, mock.patch.object(os, "replace", wraps=os.replace) as rp:
            report.write_aggregates(cfg, {"run_id": "r", "corpus_kind": "synthetic", "n": 1})
        self.assertEqual((fs.call_count, rp.call_count), (1, 1))
        self.assertFalse(list(out.rglob("*.tmp")))

    def test_real_aggregates_are_not_written_when_the_guard_fails(self):
        from second_scribe.report import aggregates_path, write_aggregates
        out = Path(tempfile.mkdtemp())
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
        with self.assertRaises(ValueError):
            write_aggregates(cfg, {"n": 1, "label": "Family doctor visit"}, forbidden={"Family doctor visit"})
        self.assertFalse(aggregates_path(cfg).exists())


if __name__ == "__main__":
    unittest.main()
