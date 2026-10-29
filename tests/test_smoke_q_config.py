"""smoke-q item 1: configs/smoke-q.json is a synthetic, Qwen-only template with no secrets and no addresses, and it
mirrors the real template's specs and ladder so the smoke measures what the real run will call."""
import json
import re
import unittest
from pathlib import Path

from second_scribe.pipeline import KIT_ROOT, RunConfig, is_synthetic

SMOKE = Path(KIT_ROOT) / "configs" / "smoke-q.json"
REAL = Path(KIT_ROOT) / "configs" / "real.template.json"


def raw() -> dict:
    return json.loads(SMOKE.read_text(encoding="utf-8"))


class TestSmokeQConfig(unittest.TestCase):
    def test_it_is_a_synthetic_config_that_loads_once_the_smoke_block_is_set_aside(self):
        d = raw()
        self.assertEqual(d["corpus"]["kind"], "synthetic")
        self.assertIn("smoke_q", d)                       # extra settings live in their own block, not in RunConfig
        from second_scribe.smoke_q import load_config
        cfg, smoke = load_config(SMOKE)
        self.assertIsInstance(cfg, RunConfig)
        self.assertTrue(is_synthetic(cfg))
        self.assertIsInstance(smoke, dict)

    def test_only_the_reconstructor_role_is_configured(self):
        d = raw()
        self.assertEqual(d["models"]["recon"], "RECON")
        self.assertIsNone(d["models"].get("judge"))
        self.assertIsNone(d["models"].get("judge2"))

    def test_the_specs_and_the_ladder_are_the_real_ones(self):
        d, real = raw(), json.loads(REAL.read_text(encoding="utf-8"))
        self.assertEqual(d["specs"], real["specs"])
        self.assertEqual(d["ladder"]["rungs"], real["ladder"]["rungs"])
        self.assertEqual(d["prior_only"]["variants"], real["prior_only"]["variants"])
        s = raw()["smoke_q"]["real"]
        self.assertEqual(s["draws"], d["ladder"]["draws"])      # the real run's draws are this config's (DECISIONS C7: 1)
        self.assertEqual(s["prior_only_samples"], real["prior_only"]["samples"])

    def test_the_options_are_the_three_named_ones(self):
        opts = raw()["smoke_q"]["options"]
        self.assertEqual(list(opts), ["full", "without_real_rung1", "minimum"])
        self.assertNotIn("rung1", opts["without_real_rung1"]["rungs"])
        for o in opts.values():
            self.assertEqual(o["draws"], 1)               # DECISIONS C7: the primary setting is T=0, one draw per arm
        self.assertEqual(sorted(opts["minimum"]["rungs"]), ["placebo", "rung2", "rung3"])

    def test_the_optional_t07_sensitivity_is_an_extra_not_an_option(self):
        x = raw()["smoke_q"]["extras"]["optional_sensitivity_t07"]
        self.assertEqual((x["temperature"], x["draws"]), (0.7, 3))
        self.assertNotIn("optional_sensitivity_t07", raw()["smoke_q"]["options"])

    def test_no_secret_no_address_no_url(self):
        text = SMOKE.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", text))
        low = text.lower()
        for bad in ("http://", "https://", "bearer", "api_key", "password", "token="):
            self.assertNotIn(bad, low)
        usage = " ".join(raw()["smoke_q"]["env_usage"].values())
        for name in ("SS_RECON_URL", "SS_RECON_MODEL", "SS_RECON_KEY_ENV", "SS_RECON_PARAMS"):
            self.assertIn(name, usage)
        self.assertIn("enable_thinking", usage)

    def test_the_judge_estimates_are_labelled_as_unmeasured_estimates(self):
        j = raw()["smoke_q"]["judge_estimates_h"]
        self.assertIn("unmeasured", j["label"])
        self.assertEqual(len(j["main_grid"]), 2)
        self.assertEqual(len(j["per_ladder_arm_draw"]), 2)


if __name__ == "__main__":
    unittest.main()
