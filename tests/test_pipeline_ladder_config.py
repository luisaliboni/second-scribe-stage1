"""Wire-b item 1: the ``ladder`` key of RunConfig (which rungs run, how many draws) and its place in the configs."""
import copy
import json
import unittest
from pathlib import Path

from second_scribe import ladder
from second_scribe.pipeline import KIT_ROOT, RunConfig

DEMO = Path(KIT_ROOT) / "configs" / "demo.json"
TEMPLATE = Path(KIT_ROOT) / "configs" / "real.template.json"
ALL_RUNGS = ["rung2", "rung3", "placebo", "rung1"]       # DECISIONS C6 / synthesis item 8: the run order


def demo_dict(**over) -> dict:
    d = json.loads(DEMO.read_text(encoding="utf-8"))
    d.pop("ladder", None)
    d["base_dir"] = str(DEMO.parent)
    d["out_dir"] = "/tmp/ss-never-written"
    d.update(over)
    return d


def sampled_primary(d: dict, temperature: float = 0.7) -> dict:
    """The demo dict with its first (primary, p0, removed) spec sampled at ``temperature`` instead of greedy, so a ladder
    with draws > 1 is legal (DECISIONS C7: at T=0 extra draws are copies, not replicates)."""
    d["specs"] = [dict(d["specs"][0], temperature=temperature)] + d["specs"][1:]
    return d


class TestLadderKey(unittest.TestCase):
    def test_absent_means_no_ladder_and_keeps_the_hash(self):
        a = RunConfig(**demo_dict())
        self.assertEqual(a.ladder, {})
        b = RunConfig(**demo_dict(ladder={}))
        self.assertEqual(a.sha256(), b.sha256())
        # the hash of a ladder-less config is what it was before the key existed (pinned at 96f4021 for the demo
        # config minus its ladder key, and minus the reading3 and script_sources keys the demo gained later)
        old = {k: v for k, v in demo_dict().items() if k not in ("reading3", "script_sources")}
        self.assertEqual(RunConfig(**old).sha256(), "e3d4b54eaaa12dbebc1e0829c40c053c1071304ac91864da462e5bf970f9b500")

    def test_a_ladder_changes_the_hash(self):
        a = RunConfig(**demo_dict())
        b = RunConfig(**sampled_primary(demo_dict(ladder={"rungs": ALL_RUNGS, "draws": 2})))
        c = RunConfig(**sampled_primary(demo_dict(ladder={"rungs": ALL_RUNGS, "draws": 3})))
        self.assertEqual(len({a.sha256(), b.sha256(), c.sha256()}), 3)

    def test_valid_ladders_load(self):
        for lad in ({"rungs": ALL_RUNGS, "draws": 3},
                    {"rungs": ["rung2"], "draws": 1},
                    {"rungs": ["rung2", "rung3", "placebo"], "draws": 2, "n_perm": 200, "B": 100, "seed": 1},
                    {"rungs": ["placebo", "rung3"], "draws": 2}):
            with self.subTest(ladder=lad):
                self.assertEqual(RunConfig(**sampled_primary(demo_dict(ladder=lad))).ladder, lad)

    def test_bad_ladders_are_refused(self):
        bad = {
            "not a dict": ["rung2"],
            "unknown key": {"rungs": ["rung2"], "draws": 2, "order": "x"},
            "no rungs": {"draws": 2},
            "empty rungs": {"rungs": [], "draws": 2},
            "unknown rung": {"rungs": ["rung2", "rung4"], "draws": 2},
            "the arm name instead of the registry key": {"rungs": ["rung2", "remedy", "placebo"], "draws": 2},
            "duplicate rung": {"rungs": ["rung2", "rung2"], "draws": 2},
            "remedy without placebo": {"rungs": ["rung2", "rung3"], "draws": 2},
            "no draws": {"rungs": ["rung2"]},
            "zero draws": {"rungs": ["rung2"], "draws": 0},
            "bool draws": {"rungs": ["rung2"], "draws": True},
            "str draws": {"rungs": ["rung2"], "draws": "3"},
            "float draws": {"rungs": ["rung2"], "draws": 2.0},
            "bad n_perm": {"rungs": ["rung2"], "draws": 2, "n_perm": 0},
            "bad B": {"rungs": ["rung2"], "draws": 2, "B": -1},
            "bad seed": {"rungs": ["rung2"], "draws": 2, "seed": "0"},
        }
        for name, lad in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    RunConfig(**sampled_primary(demo_dict(ladder=lad)))

    def test_the_primary_spec_must_be_the_ladder_variant(self):
        # sampled (T>0, draws 2) so the refusal is about the variant, not about the T=0 draws rule
        d = demo_dict(ladder={"rungs": ["rung2"], "draws": 2})
        d["specs"] = [{"arm": "removed", "variant": "p1", "temperature": 0.7, "replicate": 0}] + d["specs"][1:]
        with self.assertRaisesRegex(ValueError, "p0"):
            RunConfig(**d)
        d1 = demo_dict(ladder={"rungs": ["rung2"], "draws": 1})        # ... and with one draw at T=0 as well
        d1["specs"] = [{"arm": "removed", "variant": "p1", "temperature": 0.0, "replicate": 0}] + d1["specs"][1:]
        with self.assertRaisesRegex(ValueError, "p0"):
            RunConfig(**d1)

    def test_extra_draws_at_temperature_zero_are_refused(self):
        # DECISIONS C7: at T=0 (greedy) a second draw is a copy of the first, not an independent replicate
        d = demo_dict(ladder={"rungs": ["rung2"], "draws": 2})
        self.assertEqual(d["specs"][0]["temperature"], 0.0)
        with self.assertRaisesRegex(ValueError, "temperature 0"):
            RunConfig(**d)

    def test_extra_draws_at_a_sampled_primary_load(self):
        d = sampled_primary(demo_dict(ladder={"rungs": ["rung2"], "draws": 2}))
        self.assertEqual(d["specs"][0]["temperature"], 0.7)
        self.assertEqual(RunConfig(**d).ladder["draws"], 2)

    def test_one_draw_at_temperature_zero_loads(self):
        d = demo_dict(ladder={"rungs": ["rung2"], "draws": 1})
        self.assertEqual(d["specs"][0]["temperature"], 0.0)
        self.assertEqual(RunConfig(**d).ladder["draws"], 1)

    def test_a_real_config_needs_the_primary_setting_as_first_spec(self):
        d = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        d["base_dir"] = str(TEMPLATE.parent)
        d["specs"] = [dict(d["specs"][0], temperature=0.7)] + d["specs"][1:]
        with self.assertRaises(ValueError):
            RunConfig(**d)


class TestConfigsShipTheLadder(unittest.TestCase):
    def test_demo_runs_every_rung(self):
        cfg = RunConfig.load(DEMO)
        self.assertEqual(cfg.corpus["kind"], "synthetic")
        self.assertEqual(sorted(cfg.ladder["rungs"]), sorted(r.key for r in ladder.RUNGS))
        self.assertEqual(cfg.ladder["draws"], 1)           # C7: one draw at the primary setting (T=0)
        self.assertEqual(cfg.ladder["draws"], ladder.REAL_DRAWS)

    def test_real_template_default(self):
        d = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(d["ladder"]["rungs"], ALL_RUNGS)         # rung 2, then 3 + P, then rung 1 (the cut order reversed)
        self.assertEqual(d["ladder"]["draws"], 1)
        cfg = RunConfig.load(TEMPLATE)
        self.assertEqual(cfg.ladder["draws"], ladder.REAL_DRAWS)
        # rung 2 "full grid as today": the main run's specs are untouched by the ladder
        self.assertEqual(len(cfg.specs), 8)
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("prereg", text)


if __name__ == "__main__":
    unittest.main()
