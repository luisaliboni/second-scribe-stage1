"""q27b: the primary-setting-only configs (a second reconstructor arm runs the primary setting alone) are the demo /
smoke configs with only the specs, the ladder, the run id and the out dir changed."""
import json
import unittest
from pathlib import Path

from second_scribe import audits
from second_scribe.pipeline import KIT_ROOT, RunConfig

CONFIGS = Path(KIT_ROOT) / "configs"
PRIMARY = {"arm": "removed", "variant": "p0", "temperature": 0.0, "replicate": 0}
CHANGED = {"run_id", "specs", "ladder", "out_dir"}


class TestPrimaryOnlyConfigs(unittest.TestCase):
    def check(self, name, base, also=()):
        a = json.loads((CONFIGS / base).read_text(encoding="utf-8"))
        b = json.loads((CONFIGS / name).read_text(encoding="utf-8"))
        self.assertEqual(b["specs"], [PRIMARY])
        self.assertEqual(a["specs"][0], PRIMARY)
        self.assertEqual(b["ladder"], {})
        same = {k for k in set(a) | set(b) if a.get(k) == b.get(k)}
        self.assertEqual(set(a) | set(b), same | CHANGED | set(also))
        return a, b

    def test_canary_primary_is_the_demo_config_at_the_primary_setting(self):
        # the canary stays small: reading 3 and the judge's script source are not primary-setting checks
        self.check("canary-primary.json", "demo.json", also=("reading3", "script_sources"))
        cfg = RunConfig.load(CONFIGS / "canary-primary.json")
        self.assertEqual(cfg.corpus["kind"], "synthetic")
        self.assertEqual([s.key() for s in cfg.recon_specs()], ["removed__p0__t0__r0"])
        self.assertEqual(len(cfg.prior_specs()), 6)

    def test_a_real_config_can_name_it_as_its_canary(self):
        fake = RunConfig.load(CONFIGS / "demo.json")
        fake.corpus = {**fake.corpus, "canary": str(CONFIGS / "canary-primary.json")}
        self.assertEqual(Path(audits._canary_path(fake)), CONFIGS / "canary-primary.json")

    def test_smoke_q_primary_is_the_smoke_config_at_the_primary_setting(self):
        a, b = self.check("smoke-q-primary.json", "smoke-q.json", also=("smoke_q",))
        # the smoke block differs only in its description, the real-run turn count, the options and the extras
        diff = {k for k in set(a["smoke_q"]) | set(b["smoke_q"]) if a["smoke_q"].get(k) != b["smoke_q"].get(k)}
        self.assertLessEqual(diff, {"what", "real", "options", "extras", "options_note", "judge_estimates_h"})
        self.assertEqual(b["smoke_q"]["options"], {"primary_only": {"rungs": [], "draws": 1}})


if __name__ == "__main__":
    unittest.main()
