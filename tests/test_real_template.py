"""F1a item 6 and F4': the adopters' template is a real-corpus config, and the README points to it without a prereg step."""
import json
import unittest
from pathlib import Path

from second_scribe.pipeline import KIT_ROOT, RunConfig, is_synthetic

TEMPLATE = Path(KIT_ROOT) / "configs" / "real.template.json"
README = (Path(KIT_ROOT) / "README.md").read_text(encoding="utf-8")


class TestRealTemplate(unittest.TestCase):
    def test_the_template_is_a_real_corpus_config_that_loads(self):
        d = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(d["corpus"]["kind"], "real")
        self.assertNotIn("synthetic_dir", d["corpus"])
        self.assertNotIn("prereg_required", d)                 # no run needs a pre-registration
        cfg = RunConfig.load(TEMPLATE)
        self.assertFalse(is_synthetic(cfg))
        self.assertTrue(cfg.corpus["participant_id_map"])       # a real corpus must name its id map for the leak scan
        self.assertEqual(set(cfg.models), {"recon", "judge", "judge2"})

    def test_the_template_holds_no_endpoint_key_or_real_name(self):
        text = TEMPLATE.read_text(encoding="utf-8").lower()
        for bad in ("http://", "https://", "api_key", "10.", "192.168"):
            self.assertNotIn(bad, text)

    def test_the_demo_config_stays_synthetic(self):
        self.assertEqual(json.loads((Path(KIT_ROOT) / "configs" / "demo.json").read_text())["corpus"]["kind"],
                         "synthetic")

    def test_the_readme_points_to_the_template_and_has_no_prereg_step(self):
        self.assertTrue("configs/real.template.json" in README)
        for gone in ("prereg freeze", "prereg_required", "Pre-registration."):
            self.assertFalse(gone in README, gone)

    def test_the_readme_states_the_egress_and_thinking_rules(self):
        low = README.lower()
        for needle in ("loopback", "100.64/10", "enable_thinking", "qwen"):
            self.assertTrue(needle.lower() in low, needle)


if __name__ == "__main__":
    unittest.main()
