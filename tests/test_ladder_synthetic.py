"""Ladder item 5: synthetic end-to-end scenarios for the remedy verdict. Fake arms are generated from a seeded RNG
(no model, no real data): rungs 2 and P share a leaky baseline; the "remedy" arm 3 differs per scenario."""
import random
import unittest

from second_scribe.ladder_metrics import Item, MapDraw, ladder_report

N_MAPS, N_DRAWS, N_IDEALS, N_EXP = 24, 3, 10, 8
PARAMS = dict(n_perm=2000, seed=11, B=600)

BASE = dict(leak=0.60, kept=0.40, exp_wished=0.10, actual_wished=0.10, nodes=20)


def make_arm(spec, arm_seed, n_draws=N_DRAWS):
    """spec: leak/kept (the rest of the ideals are missed; kept is capped by 1 - leak), exp_wished (share of endorsed experienced items drawn
    wished), actual_wished (share of actual-quote nodes drawn wished), nodes. Map-level leakiness is shared across
    arms through the map index, so the contrasts are genuinely paired."""
    out = []
    for m in range(N_MAPS):
        shift = random.Random(1000 + m).gauss(0, 0.08)          # the map's own leakiness, same in every arm
        for k in range(n_draws):
            rng = random.Random(arm_seed * 100003 + m * 101 + k)
            leak = min(0.95, max(0.02, spec["leak"] + shift))
            items = []
            for j in range(N_IDEALS):
                u = rng.random()
                st = "experienced" if u < leak else ("wished" if u < leak + spec["kept"] else "missed")
                items.append(Item(f"w{j}", "wished", st))
            for j in range(N_EXP):
                st = "wished" if rng.random() < spec["exp_wished"] else "experienced"
                items.append(Item(f"x{j}", "experienced", st))
            n_act = 8
            marked = sum(rng.random() < spec["actual_wished"] for _ in range(n_act))
            flipped = sum(i.endorsed_status == "experienced" and i.state == "wished" for i in items)
            out.append(MapDraw(f"M{m:02d}", k, tuple(items), n_ai_nodes=spec["nodes"] + rng.randint(-2, 2),
                               n_actual_quote_nodes=n_act, n_actual_quote_marked_wished=marked,
                               n_control=N_EXP, n_flipped=flipped))
    return out


def ladder(remedy_spec, *, placebo_spec=None, remedy_seed=3):
    arms = {"rung1": make_arm({**BASE, "leak": 0.7, "kept": 0.3}, 1), "rung2": make_arm(BASE, 2),
            "placebo": make_arm(placebo_spec or BASE, 4), "remedy": make_arm(remedy_spec, remedy_seed)}
    return arms


class TestSyntheticVerdicts(unittest.TestCase):
    def report(self, arms):
        return ladder_report(arms, **PARAMS)

    def test_a_remedy_beats_placebo_on_leakage_with_mirror_flat_helps(self):
        r = self.report(ladder({**BASE, "leak": 0.30, "kept": 0.70}))
        v = r["verdict"]
        lk = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertEqual(v["verdict"], "helps", v)
        self.assertLess(lk["ci_hi"], 0)
        self.assertLess(lk["p_holm"], 0.05)
        self.assertEqual(v["mirror_worsened"], [])
        # the harness-wobble contrast (placebo vs rung 2) does not move
        self.assertGreater(r["contrasts"]["placebo-rung2"]["metrics"]["leaked_rate"]["p_perm"], 0.05)
        # rung 2 vs rung 1: the first-draft prompt leaks more (descriptive contrast, sign only)
        self.assertGreater(r["contrasts"]["rung2-rung1"]["metrics"]["leaked_rate"]["mean"] * -1, 0)

    def test_b_leak_falls_only_because_everything_is_marked_wished_is_not_help(self):
        spec = {**BASE, "leak": 0.05, "kept": 0.95, "exp_wished": 0.60, "actual_wished": 0.60}
        r = self.report(ladder(spec))
        v = r["verdict"]
        lk = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertLess(lk["ci_hi"], 0)                               # on leakage alone it looks like a win
        fw = r["contrasts"]["remedy-placebo"]["metrics"]["false_wished_rate"]
        self.assertGreater(fw["ci_lo"], 0)                            # false-wished rate rises, CI above 0
        self.assertNotEqual(v["verdict"], "helps")
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("false_wished_rate", v["mirror_worsened"])
        self.assertIn("flip_rate", v["mirror_worsened"])

    def test_c_dropping_leaky_items_shows_as_missed_not_kept(self):
        spec = {**BASE, "leak": 0.30, "kept": 0.40}                    # 0.30 of the ideals now missed, kept unchanged
        r = self.report(ladder(spec))
        m = r["contrasts"]["remedy-placebo"]["metrics"]
        self.assertLess(m["leaked_rate"]["ci_hi"], 0)                  # leakage fell ...
        self.assertGreater(m["missed_rate"]["ci_lo"], 0)               # ... because items went missing
        self.assertTrue(m["kept_rate"]["ci_lo"] <= 0 <= m["kept_rate"]["ci_hi"])   # kept did not rise
        three = r["rungs"]["remedy"]["three_way"]["pooled"]
        placebo = r["rungs"]["placebo"]["three_way"]["pooled"]
        self.assertGreater(three["missed_rate"], placebo["missed_rate"] + 0.2)
        self.assertLess(abs(three["kept_rate"] - placebo["kept_rate"]), 0.06)
        v = r["verdict"]
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("missed_rate", v["mirror_worsened"])          # the non-worsening gate on MISSED catches it
        self.assertTrue(v["mirror_checks"]["missed_rate"]["worse"])

    def test_d_identical_arms_give_no_small_p_and_no_help(self):
        arms = ladder(BASE)
        arms["remedy"] = [MapDraw(d.map_id, d.draw, d.items, n_ai_nodes=d.n_ai_nodes,
                             n_actual_quote_nodes=d.n_actual_quote_nodes,
                             n_actual_quote_marked_wished=d.n_actual_quote_marked_wished,
                             n_control=d.n_control, n_flipped=d.n_flipped) for d in arms["placebo"]]
        r = self.report(arms)
        lk = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertEqual(lk["mean"], 0.0)
        self.assertEqual(lk["p_perm"], 1.0)
        self.assertEqual(r["verdict"]["verdict"], "null")
        self.assertAlmostEqual(r["verdict"]["excludes_leak_drop_larger_than"], 0.0)

    def test_d2_same_distribution_different_noise_is_not_significant(self):
        r = self.report(ladder(BASE, remedy_seed=9))
        lk = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertGreater(lk["p_perm"], 0.05)
        self.assertNotEqual(r["verdict"]["verdict"], "helps")

    def test_e_unequal_draws_is_an_error(self):
        arms = ladder({**BASE, "leak": 0.3, "kept": 0.7})
        arms["remedy"] = make_arm({**BASE, "leak": 0.3, "kept": 0.7}, 3, n_draws=N_DRAWS - 1)
        with self.assertRaises(ValueError) as cm:
            self.report(arms)
        self.assertIn("unequal draw counts", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
