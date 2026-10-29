"""smoke-q item 3: budget.json. The arithmetic is tested on hand-made log rows (no server); the fields of the real
file are tested on a run against the fake server in test_smoke_q."""
import json
import math
import tempfile
import unittest
from pathlib import Path

from second_scribe import smoke_q
from second_scribe.pipeline import KIT_ROOT
from second_scribe.transcripts import _BATCH

CONFIG = Path(KIT_ROOT) / "configs" / "smoke-q.json"


def att(stage, item, ms, *, t=0.0, prompt=1000, completion=100, attempt=1):
    return {"stage": stage, "item_id": item, "attempt": attempt, "temperature": t, "model_id": "m", "endpoint_host": "h",
            "latency_ms": ms, "prompt_tokens": prompt, "completion_tokens": completion, "ts": None}


def padded_attempts():
    """A padded run: every main-grid spec 60 s (full arm 70 s), rung 3 / placebo / rung 1 at 65 / 66 / 40 s, mapping-talk
    batches 2 s, all with 3500 completion tokens."""
    cfg, _ = smoke_q.load_config(CONFIG)
    out = [att("q:mapping_talk", f"P:{b}", 2000, completion=100) for b in range(10)]
    for spec in cfg.recon_specs():
        out.append(att(f"recon:{spec.arm}", f"P__{spec.key()}", 70000 if spec.arm == "full" else 60000, t=spec.temperature,
                       prompt=16000, completion=3500))
    for rung, ms in (("rung3", 65000), ("placebo", 66000), ("rung1", 40000)):
        out.append(att(f"recon:removed:{rung}", f"P__removed__p0__t0__r0@{rung}", ms, prompt=16000, completion=3500))
    return out


def synthetic_attempts():
    return [att("prior_only", f"owner__prior_only__{v}__t0.7__r{k}", 8000, t=0.7, prompt=300, completion=400)
            for v in ("p0", "p1", "p2") for k in range(2)]


class TestClassify(unittest.TestCase):
    def test_groups_and_arms(self):
        self.assertEqual(smoke_q.classify("q:mapping_talk", "SYN-OW01:3"), ("mapping_talk", "batch"))
        self.assertEqual(smoke_q.classify("recon:removed", "SYN-OW01__removed__p0__t0.7__r0"),
                         ("main_grid", "removed__p0__t0.7__r0"))
        self.assertEqual(smoke_q.classify("recon:removed:rung3", "SYN-OW01__removed__p0__t0__r0@rung3"), ("ladder", "rung3"))
        self.assertEqual(smoke_q.classify("prior_only", "owner__prior_only__p1__t0.7__r4"), ("prior_only", "p1"))


class TestRows(unittest.TestCase):
    def test_median_p90_tokens_per_second_and_retries(self):
        xs = [att("recon:full", f"A{k}__full__p0__t0__r0", ms, completion=3000) for k, ms in enumerate(range(10000, 101000, 10000))]
        xs.append(att("recon:full", "A0__full__p0__t0__r0", 5000, attempt=2, completion=3000))     # a retry of call A0
        (row,) = smoke_q.measured_rows("padded", xs)
        self.assertEqual((row["n_calls"], row["n_attempts"]), (10, 11))
        self.assertAlmostEqual(row["attempts_per_call"], 1.1, places=3)
        self.assertEqual(row["latency_ms"]["median"], 50000)
        self.assertEqual(row["latency_ms"]["p90"], 90000)               # nearest rank: ceil(0.9 * 11) = 10th of 11
        self.assertTrue(row["p90_meaningful"])
        self.assertEqual(row["completion_tokens_median"], 3000)
        self.assertAlmostEqual(row["tokens_per_s_median"], 3000 / 50, places=1)

    def test_a_handful_of_replies_has_no_meaningful_p90_and_missing_usage_gives_none(self):
        xs = [att("q:mapping_talk", f"P:{b}", 2000, completion=None) for b in range(3)]
        (row,) = smoke_q.measured_rows("padded", xs)
        self.assertFalse(row["p90_meaningful"])
        self.assertIsNone(row["tokens_per_s_median"])


class TestProjection(unittest.TestCase):
    def setUp(self):
        self.cfg, self.smoke = smoke_q.load_config(CONFIG)
        self.rows = smoke_q.measured_rows("padded", padded_attempts()) + smoke_q.measured_rows("synthetic", synthetic_attempts())
        self.proj = smoke_q.project(self.rows, self.cfg, self.smoke)
        self.n = self.smoke["real"]["n_maps"]

    def comp(self, option, name_start):
        (c,) = [c for c in self.proj["options"][option]["qwen_phase"]["components"] if c["component"].startswith(name_start)]
        return c

    def test_the_three_options_and_the_extra_are_there_with_both_phases(self):
        self.assertEqual(list(self.proj["options"]), ["full", "without_real_rung1", "minimum"])
        self.assertEqual(list(self.proj["extras"]), ["optional_sensitivity_t07"])
        for o in self.proj["options"].values():
            self.assertIn("qwen_phase", o)
            self.assertIn("judge_phase", o)
        self.assertEqual(self.proj["judge_phase_label"], "estimate, unmeasured until the judge window")
        self.assertIn("unmeasured", self.proj["options"]["full"]["judge_phase"]["label"])

    def test_main_grid_hours_are_calls_times_measured_seconds(self):
        c = self.comp("full", "main_grid removed__p0__t0__r0")
        self.assertEqual(c["calls_real"], 46)
        self.assertEqual(c["per_call_s_median"], 60.0)
        self.assertAlmostEqual(c["hours_median"], 46 * 60 / 3600, places=3)
        self.assertAlmostEqual(self.comp("full", "main_grid full__p0__t0__r0")["hours_median"], 46 * 70 / 3600, places=3)

    def test_mapping_talk_uses_the_assumed_turns_per_transcript(self):
        c = self.comp("full", "mapping_talk")
        batches = math.ceil(self.smoke["real"]["interviewer_turns_per_transcript"] / _BATCH)
        self.assertEqual(c["calls_real"], 46 * batches)
        self.assertAlmostEqual(c["hours_median"], 46 * batches * 2 / 3600, places=3)

    def test_prior_only_is_roles_times_variants_times_samples_from_the_synthetic_pass(self):
        c = self.comp("full", "prior_only")
        r = self.smoke["real"]
        self.assertEqual(c["calls_real"], r["n_roles"] * 3 * r["prior_only_samples"])
        self.assertEqual(c["per_call_s_median"], 8.0)

    def test_ladder_arms_per_option_and_rung2_adds_no_call_at_the_primary_setting(self):
        names = lambda o: [c["component"] for c in self.proj["options"][o]["qwen_phase"]["components"] if c["component"].startswith("ladder")]
        self.assertEqual(names("full"), ["ladder rung3 x1", "ladder placebo x1", "ladder rung1 x1"])
        self.assertEqual(names("without_real_rung1"), ["ladder rung3 x1", "ladder placebo x1"])
        self.assertAlmostEqual(self.comp("full", "ladder rung1")["hours_median"], 46 * 40 / 3600, places=3)
        self.assertEqual(self.proj["options"]["minimum"]["same_as"], "without_real_rung1")      # C7: draws cannot differ at T=0
        self.assertNotIn("same_as", self.proj["options"]["full"])

    def test_totals_add_up_and_cutting_rung1_saves_exactly_its_hours(self):
        full, cut = (self.proj["options"][k]["qwen_phase"] for k in ("full", "without_real_rung1"))
        for q in (full, cut):
            self.assertAlmostEqual(q["hours_median"], round(sum(c["hours_median"] for c in q["components"]) + q["determinism_audit_h"], 3), places=3)
        self.assertGreater(full["hours_median"], cut["hours_median"])
        self.assertGreaterEqual(full["hours_p90"], full["hours_median"])
        self.assertGreaterEqual(full["hours_upper_bound"], full["hours_p90"])
        self.assertTrue(full["complete"])

    def test_the_determinism_audit_is_costed_at_the_mean_t0_call(self):
        q = self.proj["options"]["full"]["qwen_phase"]
        t0 = [c for c in q["components"] if c["temperature"] == 0 and c["calls_real"]]
        mean_s = sum(c["per_call_s_median"] * c["calls_real"] for c in t0) / sum(c["calls_real"] for c in t0)
        self.assertAlmostEqual(q["determinism_audit_h"], 20 * mean_s / 3600, places=3)

    def test_judge_phase_is_the_plan_estimate_per_arm_draw(self):
        j = {k: v["judge_phase"] for k, v in self.proj["options"].items()}
        self.assertEqual(j["full"]["ladder_arm_draws"], 3)                   # rung3, placebo, rung1 (rung2's map is the grid's)
        self.assertEqual(j["without_real_rung1"]["ladder_arm_draws"], 2)
        self.assertEqual((j["full"]["hours_low"], j["full"]["hours_high"]), (8 + 3 * 1.0, 12 + 3 * 1.5))
        x = self.proj["extras"]["optional_sensitivity_t07"]
        self.assertEqual(x["judge_phase"]["ladder_arm_draws"], 9)            # rung2 + rung3 + placebo, 3 draws each, all new calls
        self.assertEqual(self.comp_extra("ladder rung2")["calls_real"], 46 * 3)

    def comp_extra(self, start):
        (c,) = [c for c in self.proj["extras"]["optional_sensitivity_t07"]["qwen_phase"]["components"] if c["component"].startswith(start)]
        return c

    def test_the_t07_sensitivity_is_marked_not_runnable_until_a_config_can_express_it(self):
        """F3 (smoke-q): check_rung_spec(real_data=True) admits only the T=0 primary setting, so the extra is a cost
        estimate for a decision, not something the code can run on real data yet."""
        x = self.proj["extras"]["optional_sensitivity_t07"]
        self.assertIs(x["runnable"], False)
        self.assertIn("real", x["runnable_note"])
        self.assertNotIn("runnable", self.proj["options"]["full"])

    def test_total_is_qwen_plus_judge_and_says_it_is_partly_an_estimate(self):
        o = self.proj["options"]["full"]
        self.assertAlmostEqual(o["total_h"]["low"], round(o["qwen_phase"]["hours_median"] + o["judge_phase"]["hours_low"], 2), places=2)
        self.assertAlmostEqual(o["total_h"]["high"], round(o["qwen_phase"]["hours_p90"] + o["judge_phase"]["hours_high"], 2), places=2)
        self.assertIn("estimate", o["total_h"]["note"])

    def test_an_unmeasured_spec_falls_back_to_the_padded_median_and_a_missing_run_leaves_nulls(self):
        rows = [r for r in self.rows if r["arm"] != "full__p1__t0__r0"]
        proj = smoke_q.project(rows, self.cfg, self.smoke)
        (c,) = [c for c in proj["options"]["full"]["qwen_phase"]["components"] if c["component"].endswith("full__p1__t0__r0")]
        self.assertIn("not measured", c["basis"])
        self.assertIsNotNone(c["hours_median"])
        none = smoke_q.project([r for r in self.rows if r["run"] != "padded"], self.cfg, self.smoke)
        q = none["options"]["full"]["qwen_phase"]
        self.assertFalse(q["complete"])
        self.assertIsNone(q["hours_median"])
        self.assertIsNone(none["options"]["full"]["total_h"]["low"])

    def test_output_shorter_than_planned_stretches_the_upper_bound(self):
        c = self.comp("full", "main_grid removed__p0__t0__r0")
        self.assertEqual(c["output_scale"], round(3500 / 3500, 2))           # padded maps were as long as planned
        short = [att(a["stage"], a["item_id"], a["latency_ms"], t=a["temperature"], prompt=a["prompt_tokens"],
                     completion=1750 if a["prompt_tokens"] == 16000 else a["completion_tokens"]) for a in padded_attempts()]
        rows = smoke_q.measured_rows("padded", short) + smoke_q.measured_rows("synthetic", synthetic_attempts())
        c2 = [c for c in smoke_q.project(rows, self.cfg, self.smoke)["options"]["full"]["qwen_phase"]["components"]
              if c["component"] == "main_grid removed__p0__t0__r0"][0]
        self.assertEqual(c2["output_scale"], 2.0)
        self.assertAlmostEqual(c2["hours_upper_bound"], round(c2["hours_p90"] * 2, 3), places=3)


class TestWarnings(unittest.TestCase):
    def test_a_smoke_resumed_against_another_model_is_flagged(self):
        import dataclasses
        from second_scribe.audit.registry import AuditLog
        cfg, smoke = smoke_q.load_config(CONFIG)
        with tempfile.TemporaryDirectory(prefix="ss-budget-w-") as d:
            for run, model in (("synthetic", "qwen-a"), ("padded", "qwen-b")):
                (Path(d) / run / "logs").mkdir(parents=True)
                log = AuditLog(Path(d) / run / "logs" / "run.jsonl", "r")
                log.append("call_request", {"request_id": "x", "stage": "q:mapping_talk", "item_id": "P:0", "attempt": 1,
                                            "temperature": 0.0, "model_id": model, "endpoint_host": "127.0.0.1:1"})
                log.append("call_response", {"request_id": "x", "latency_ms": 5, "usage": {}})
                log.close()
            as_dict = dataclasses.asdict(cfg)
            syn = smoke_q.RunConfig(**{**as_dict, "out_dir": str(Path(d) / "synthetic")})
            pad = smoke_q.RunConfig(**{**as_dict, "out_dir": str(Path(d) / "padded")})
            b = smoke_q.build_budget(syn, smoke, pad, verified=True)
        self.assertEqual(b["model_ids"], ["qwen-a", "qwen-b"])
        self.assertTrue(any("mix instruments" in w for w in b["warnings"]))


class TestWrite(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory(prefix="ss-budget-") as d:
            smoke_q.write_budget(Path(d) / "budget.json", {"a": 1})
            self.assertEqual(json.loads((Path(d) / "budget.json").read_text()), {"a": 1})
            self.assertEqual([p.name for p in Path(d).iterdir()], ["budget.json"])


if __name__ == "__main__":
    unittest.main()
