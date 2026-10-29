"""Wire-b item 7: the metrics stage writes ``aggregates["ladder"]`` (per-rung results, the ladder report in codes,
the copy audit, parse failures and retries per rung). Synthetic demo corpus, the oracle behind a wrapper, temp dirs."""
import contextlib
import dataclasses
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe import cli, ladder, pipeline
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import RunConfig, run
from second_scribe.report import KIT_NOTES, assert_aggregate_only

N_PEOPLE = 12
ALL = ["rung2", "rung3", "placebo", "rung1"]


class Flaky:
    """The oracle with two kinds of damage, to give the ladder report something to count:
    the REMEDY prompt's wished nodes come back as experienced (a leak, for every map) plus one extra node whose label no
    other rung's map has, and the first two PLACEBO prompts
    (in call order) are damaged, in every draw (a draw repeats the prompt): the first prompt's first reply is not JSON
    once (one corrective retry, then fine), the second prompt never gets a valid reply (three attempts, a parse failure)."""
    model_label = "oracle"

    def __init__(self, inner):
        self.inner = inner
        self.placebo_prompts: list[str] = []

    def complete(self, messages, **kw):
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "Two worked examples" in user and "The transcript follows:" in user:          # the placebo prompt
            if user not in self.placebo_prompts:
                self.placebo_prompts.append(user)
            idx = self.placebo_prompts.index(user)
            first_attempt = len(messages) == 2
            if idx == 1 or (idx == 0 and first_attempt):
                return dataclasses.replace(self.inner.complete(messages, **kw), text="not json at all")
        resp = self.inner.complete(messages, **kw)
        if "according to the participant" in user and "Two worked examples" in user:       # the remedy prompt
            d = json.loads(resp.text)
            for n in d["nodes"]:
                n["status"] = "experienced"
            for e in d["edges"]:
                e["status"] = "experienced"
            d["nodes"].append({"id": "nx", "label": "Remedy only extra", "type": "service", "phase": None,
                               "status": "experienced", "quote": d["nodes"][0]["quote"]})
            return dataclasses.replace(resp, text=json.dumps(d))
        return resp


def build(tmp, name, ladder_cfg, stages=("Q", "J", "M"), wrap=Flaky):
    out = Path(tmp) / name
    d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
    d.pop("script_sources", None)              # the demo's own source (L75): these ladders are read without it
    d["base_dir"], d["out_dir"] = str(DEMO_CONFIG.parent), str(out)
    d["ladder"] = ladder_cfg
    if not ladder_cfg:
        d.pop("ladder")
    else:
        d["specs"] = [dict(d["specs"][0], temperature=0.7)] + d["specs"][1:]   # C7: draws > 1 need a sampled (T>0) primary
    cfg = RunConfig(**d)
    clients = build_clients("oracle", cfg)
    if wrap:
        clients = {k: wrap(v) if k == "recon" else v for k, v in clients.items()}
    (out / "logs").mkdir(parents=True)
    log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            res = run(cfg, list(stages), clients, log=log)
    finally:
        log.close()
    return cfg, out, res


def long_strings(x, where="$", out=None):
    out = [] if out is None else out
    if isinstance(x, str):
        if len(x) > 40 and not re.fullmatch(r"[0-9a-f]{64}", x):
            out.append(where)
    elif isinstance(x, dict):
        for k, v in x.items():
            long_strings(k, where + ".<key>", out)
            long_strings(v, f"{where}.{k}", out)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            long_strings(v, f"{where}[{i}]", out)
    return out


class TestLadderAggregates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-ladagg-")
        cls.addClassCleanup(tmp.cleanup)
        cls.cfg, cls.out, cls.res = build(tmp.name, "lad", {"rungs": ALL, "draws": 2, "n_perm": 50, "B": 50})
        cls.agg = cls.res["aggregates"]
        cls.lad = cls.agg["ladder"]
        cls.rep = cls.lad["report"]
        _c, _o, cls.base_res = build(tmp.name, "none", {}, wrap=None)

    def test_no_ladder_no_key(self):
        self.assertNotIn("ladder", self.base_res["aggregates"])

    def test_shape_and_provenance(self):
        lad = self.lad
        self.assertEqual(lad["rungs"], ALL)
        self.assertEqual(lad["draws"], 2)
        self.assertEqual(lad["arm_of_rung"], {"rung2": "rung2", "rung3": "remedy", "placebo": "placebo", "rung1": "rung1"})
        self.assertEqual(lad["prompt_set_sha256"], pipeline.prompt_set_sha256())
        self.assertEqual((lad["n_perm"], lad["B"], lad["seed"]), (50, 50, 0))
        # one placebo map never parsed: a parse failure is a stored result (counted as such), not a gap
        self.assertIs(lad["complete"], True)

    def test_complete_means_every_cell_is_stored(self):
        self.assertEqual({k: v["by_status"].get("not_run", 0) for k, v in self.lad["per_rung"].items()},
                         {k: 0 for k in ALL})

    def test_the_report_uses_the_metrics_arm_names_and_skips_nothing(self):
        self.assertEqual(set(self.rep["rungs"]), {"rung1", "rung2", "placebo", "remedy"})
        self.assertEqual(self.rep["skipped"], [])
        self.assertEqual(set(self.rep["contrasts"]), {"remedy-placebo", "placebo-rung2", "rung2-rung1"})

    def test_the_declaration_matches_the_analysis(self):
        self.assertEqual(self.rep["declaration_check"]["label"], "as_declared", self.rep["declaration_check"])

    def test_the_damage_shows_in_the_numbers(self):
        remedy, placebo, rung2 = (self.rep["rungs"][k] for k in ("remedy", "placebo", "rung2"))
        self.assertGreater(remedy["three_way"]["pooled"]["leaked"], 0)              # wished -> experienced
        self.assertEqual(rung2["three_way"]["pooled"]["leaked"], 0)                 # the oracle leaks nothing
        self.assertEqual(rung2["three_way"]["pooled"]["n_ideals"], remedy["three_way"]["pooled"]["n_ideals"])
        self.assertEqual(placebo["parse_failures"], 2)      # the second prompt, in both draws
        self.assertEqual(placebo["retries"], 6)         # per draw: one corrective retry for the first prompt + two for the failed one
        self.assertEqual(self.rep["rungs"]["rung1"]["parse_failures"], 0)

    def test_per_rung_counts(self):
        pr = self.lad["per_rung"]
        for k in ALL:
            self.assertEqual(pr[k]["expected"], N_PEOPLE * 2, k)
        self.assertEqual(pr["placebo"]["by_status"], {"ok": N_PEOPLE * 2 - 2, "failed": 2})
        self.assertEqual(pr["placebo"]["retries"], 6)
        self.assertEqual(pr["placebo"]["transport_errors"], 0)
        self.assertEqual(pr["rung1"]["by_status"], {"ok": N_PEOPLE * 2})
        self.assertEqual(pr["rung2"]["retries"], 0)

    def test_copy_audit_null_phase_and_draw_identity_per_rung(self):
        ca = self.lad["copy_audit"]
        self.assertEqual(set(ca), set(ALL))
        self.assertEqual(ca["rung2"]["n_outputs"], N_PEOPLE * 2)
        self.assertEqual(ca["placebo"]["n_missing"], 2)
        for k in ALL:
            self.assertEqual(set(ca[k]), {"n", "n_outputs", "n_missing", "n_flagged", "flagged_ids", "n_in_source",
                                          "by_field", "by_kind"}, k)
        self.assertEqual(set(self.lad["null_phase"]), set(ALL))
        self.assertNotIn("by_map", self.lad["null_phase"]["rung2"])
        self.assertEqual(set(self.lad["draw_identity"]), set(ALL))
        self.assertEqual(self.lad["draw_identity"]["rung2"]["share_identical"], 1.0)      # a deterministic oracle

    def test_no_participant_id_and_no_long_string(self):
        text = json.dumps(self.lad)
        self.assertNotRegex(text, r"SYN-O[WF]\d")
        self.assertEqual(long_strings(self.lad), [])
        assert_aggregate_only(self.lad, set(), allowed=KIT_NOTES)

    def test_the_guard_passes_with_every_label_and_quote_of_the_rung_maps_forbidden(self):
        log = AuditLog(self.out / "logs" / "agg.jsonl", "a")
        self.addCleanup(log.close)
        r = pipeline._Run(self.cfg, build_clients("oracle", self.cfg), log)
        p_list, _ex, _j = pipeline.build_pair_inputs(r)
        forbidden = pipeline._forbidden(r, p_list)
        # a label only the remedy maps carry is in the forbidden set: the rung maps are collected, not just the main ones
        self.assertIn("Remedy only extra", forbidden)
        assert_aggregate_only(self.lad, forbidden, allowed=KIT_NOTES)

    def test_aggregates_are_json_safe_and_written(self):
        written = json.loads((self.out / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(written["ladder"]["report"]["verdict"]["verdict"], self.rep["verdict"]["verdict"])
        json.dumps(self.lad, allow_nan=False)


class TestIncompleteLadder(unittest.TestCase):
    def test_a_missing_item_is_not_run_and_the_ladder_is_not_complete(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-ladagg-")
        self.addCleanup(tmp.cleanup)
        cfg, out, _res = build(tmp.name, "lad", {"rungs": ["rung2", "rung1"], "draws": 2, "n_perm": 50, "B": 50},
                               stages=("Q", "J"), wrap=None)
        (out / "work" / "recon:rung1" / "SYN-OW01__removed__p0__t0.7__r1.json").unlink()
        self.assertEqual(pipeline.ladder_missing(cfg), {"rung1": 1})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._warn_outage(cfg)
        self.assertIn("rung1: 1", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            clients = build_clients("oracle", cfg)
            log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
            try:
                r = pipeline._Run(cfg, clients, log)
                agg, _ = pipeline.compute_aggregates(r)
            finally:
                log.close()
        lad = agg["ladder"]
        self.assertIs(lad["complete"], False)
        self.assertEqual(lad["per_rung"]["rung1"]["by_status"], {"ok": N_PEOPLE * 2 - 1, "not_run": 1})
        self.assertEqual(lad["per_rung"]["rung2"]["by_status"], {"ok": N_PEOPLE * 2})

    def test_no_ladder_nothing_missing(self):
        cfg = RunConfig.load(DEMO_CONFIG, out_dir="/tmp/ss-never-written")
        self.assertEqual(pipeline.ladder_missing(dataclasses.replace(cfg, ladder={})), {})


if __name__ == "__main__":
    unittest.main()
