"""Review I2: common random numbers across rungs. The rung suffix keeps log/work-store keys unique but never
enters the seed, so a paired contrast (3 - P, P - 2, 2 - 1) compares the same sampling stream per map and draw."""
import unittest

from second_scribe.ladder import (RUNGS, Stages, base_item_id, get_rung, rung_item_id, rung_seed, rung_stage)
from second_scribe.llm import derive_seed
from second_scribe.reconstruct import ReconSpec

SPEC = ReconSpec("removed", "p0", 0.0)
KEYS = [r.key for r in RUNGS]


class TestRungSeeds(unittest.TestCase):
    def test_same_map_and_draw_gives_the_same_seed_in_every_rung(self):
        for replicate in (0, 1, 2):
            spec = ReconSpec("removed", "p0", 0.0, replicate)
            seeds = {r.key: rung_seed(rung_item_id("KC12", r), spec.key()) for r in RUNGS}
            self.assertEqual(len(set(seeds.values())), 1, seeds)
            self.assertEqual(seeds["rung2"], derive_seed("KC12", spec.key()))      # = what the pipeline derives today

    def test_different_map_or_draw_gives_a_different_seed(self):
        r = get_rung("rung3")
        a = rung_seed(rung_item_id("KC12", r), SPEC.key())
        self.assertNotEqual(a, rung_seed(rung_item_id("KC13", r), SPEC.key()))
        self.assertNotEqual(a, rung_seed(rung_item_id("KC12", r), ReconSpec("removed", "p0", 0.0, 1).key()))

    def test_naive_derive_seed_on_the_suffixed_id_would_differ(self):
        # why the helper exists: seeding on the suffixed id silently breaks the pairing
        r1, r2 = get_rung("rung2"), get_rung("rung3")
        self.assertNotEqual(derive_seed(rung_item_id("KC12", r1), SPEC.key()),
                            derive_seed(rung_item_id("KC12", r2), SPEC.key()))


class TestRungIdentity(unittest.TestCase):
    def test_item_ids_are_unique_per_rung_and_round_trip(self):
        ids = [rung_item_id("KC12", r) for r in RUNGS]
        self.assertEqual(len(set(ids)), len(RUNGS))
        for r, i in zip(RUNGS, ids):
            self.assertEqual(base_item_id(i), "KC12")
            self.assertTrue(i.endswith(r.key))

    def test_base_item_id_only_strips_a_known_rung_suffix(self):
        self.assertEqual(base_item_id("KC12"), "KC12")
        self.assertEqual(base_item_id("KC12@other"), "KC12@other")
        self.assertEqual(base_item_id("a@rung3@rung2"), "a@rung3")        # one suffix only

    def test_stages_are_unique_per_rung_and_arm(self):
        seen = set()
        for r in RUNGS:
            for arm in ("full", "removed"):
                st = rung_stage(r, ReconSpec(arm, "p0", 0.0))
                self.assertIsInstance(st, Stages)
                self.assertEqual(st.log, f"recon:{arm}:{r.key}")
                self.assertEqual(st.store, f"recon:{r.key}")
                seen.add((st.log, st.store))
        self.assertEqual(len({s[0] for s in seen}), len(RUNGS) * 2)      # log stage differs for every rung x arm
        self.assertEqual(len({s[1] for s in seen}), len(RUNGS))          # store stage differs for every rung

    def test_stage_checks_the_variant(self):
        with self.assertRaises(ValueError):
            rung_stage(get_rung("rung2"), ReconSpec("removed", "p1", 0.0))


if __name__ == "__main__":
    unittest.main()
