"""Ladder item 3: paired contrasts (remedy-placebo, placebo-rung2, rung2-rung1), sign-flip permutation test, map-cluster bootstrap CI,
Holm over the three leak contrasts, equal-draw enforcement, descriptive script-conditional gap, verdict helper."""
import json
import math
import unittest
from dataclasses import replace

from second_scribe.ladder_metrics import (Item, MapDraw, holm, sign_flip_p, ladder_contrasts, ladder_report, paired_contrast,
                                          remedy_verdict, script_conditional_by_rung)


def cell(mid, k, n_leak, n_kept, n_miss=0, *, n_exp=4, exp_wished=0, nodes=10, actual=6, actual_wished=0, units=None,
         n_flipped=None):
    """One draw: ideals (leaked/kept/missed counts) + n_exp endorsed experienced items (exp_wished drawn wished)."""
    its = []
    for j in range(n_leak):
        its.append(Item(f"w{j}", "wished", "experienced"))
    for j in range(n_kept):
        its.append(Item(f"w{n_leak + j}", "wished", "wished"))
    for j in range(n_miss):
        its.append(Item(f"w{n_leak + n_kept + j}", "wished", "missed"))
    for j in range(n_exp):
        its.append(Item(f"x{j}", "experienced", "wished" if j < exp_wished else "experienced"))
    return MapDraw(mid, k, tuple(its), n_ai_nodes=nodes, n_actual_quote_nodes=actual,
                   n_actual_quote_marked_wished=actual_wished, script_units=units,
                   n_control=n_exp, n_flipped=exp_wished if n_flipped is None else n_flipped)


def arm(n_maps, draws, leaks_for, **kw):
    """leaks_for(m, k) -> number of leaked ideals out of 10."""
    out = []
    for m in range(n_maps):
        for k in range(draws):
            nl = leaks_for(m, k)
            out.append(cell(f"M{m:02d}", k, nl, 10 - nl, **kw))
    return out


class TestPairedContrast(unittest.TestCase):
    def test_known_differences(self):
        a = {f"M{i}": 0.2 for i in range(12)}
        b = {f"M{i}": 0.5 + 0.01 * i for i in range(12)}
        r = paired_contrast(a, b, name="x", n_perm=2000, seed=3, B=500)
        self.assertEqual(r["n_used"], 12)
        self.assertAlmostEqual(r["mean"], sum(0.2 - (0.5 + 0.01 * i) for i in range(12)) / 12)
        self.assertLess(r["ci_hi"], 0)
        self.assertLess(r["p_perm"], 0.01)
        self.assertEqual((r["n_perm"], r["seed"], r["B"]), (2000, 3, 500))       # parameters are recorded
        self.assertEqual(r["alternative"], "two-sided")

    def test_identical_arms_p_is_one_and_ci_straddles_zero(self):
        a = {f"M{i}": 0.1 * (i % 5) for i in range(10)}
        r = paired_contrast(a, dict(a), name="x", n_perm=500, seed=1, B=200)
        self.assertEqual(r["mean"], 0.0)
        self.assertEqual(r["p_perm"], 1.0)
        self.assertFalse(r["ci_excludes_zero"])

    def test_seeded_and_reproducible(self):
        a = {f"M{i}": 0.3 + 0.05 * ((i * 7) % 5) for i in range(15)}
        b = {f"M{i}": 0.35 + 0.05 * ((i * 3) % 5) for i in range(15)}
        r1 = paired_contrast(a, b, name="x", n_perm=1000, seed=5, B=300)
        r2 = paired_contrast(a, b, name="x", n_perm=1000, seed=5, B=300)
        self.assertEqual(r1, r2)

    def test_nan_maps_are_excluded_and_counted(self):
        a = {"A": 0.1, "B": math.nan, "C": 0.2, "D": 0.3}
        b = {"A": 0.2, "B": 0.5, "C": 0.4, "D": math.nan}
        r = paired_contrast(a, b, name="x", n_perm=100, seed=1, B=50, min_maps=2)
        self.assertEqual((r["n_maps"], r["n_used"], r["n_excluded"]), (4, 2, 2))

    def test_too_few_maps_gives_no_inference(self):
        r = paired_contrast({"A": 0.1, "B": 0.1}, {"A": 0.5, "B": 0.5}, name="x", n_perm=100, seed=1, B=50, min_maps=3)
        self.assertTrue(math.isnan(r["p_perm"]) and math.isnan(r["ci_lo"]))
        self.assertFalse(r["ci_excludes_zero"])

    def test_different_map_sets_is_an_error(self):
        with self.assertRaises(ValueError):
            paired_contrast({"A": 0.1}, {"B": 0.1}, name="x", n_perm=10, seed=1, B=10)


class TestSignFlipP(unittest.TestCase):
    def test_small_samples_are_enumerated_exactly(self):
        self.assertEqual(sign_flip_p([-0.5] * 6, n_perm=10, seed=1), 2 / 64)         # only all-minus and all-plus reach |sum|
        self.assertEqual(sign_flip_p([-0.5, 0.0, -0.5], n_perm=10, seed=1), 0.5)     # the zero is irrelevant
        self.assertEqual(sign_flip_p([-0.5] * 6, n_perm=10, seed=99), 2 / 64)        # no seed dependence

    def test_exact_p_matches_a_hand_count(self):
        # diffs (3, 1, 1): sums over the 8 sign patterns are +-5, +-3, +-1 (x2): |sum| >= 5 in 2 of 8
        self.assertEqual(sign_flip_p([3.0, 1.0, 1.0], n_perm=10, seed=1), 2 / 8)

    def test_monte_carlo_p_is_never_zero(self):
        # 20 maps (above the exact limit), a perfectly separated sample: p is the +1 floor 1/(1+n_perm), not 0
        p = sign_flip_p([-0.5] * 20, n_perm=100, seed=1)
        self.assertEqual(p, 1 / 101)

    def test_method_is_recorded(self):
        a = {f"M{i}": 0.1 for i in range(6)}
        b = {f"M{i}": 0.6 for i in range(6)}
        self.assertEqual(paired_contrast(a, b, name="x", n_perm=50, seed=1, B=50)["p_method"], "exact")
        a = {f"M{i}": 0.1 for i in range(20)}
        b = {f"M{i}": 0.6 for i in range(20)}
        r = paired_contrast(a, b, name="x", n_perm=50, seed=1, B=50)
        self.assertEqual((r["p_method"], r["p_perm"]), ("monte_carlo", 1 / 51))


class TestHolm(unittest.TestCase):
    def test_holm_adjusted(self):
        adj = holm({"a": 0.01, "b": 0.04, "c": 0.03})
        self.assertAlmostEqual(adj["a"], 0.03)
        self.assertAlmostEqual(adj["c"], 0.06)
        self.assertAlmostEqual(adj["b"], 0.06)               # monotone: max(0.04*1, 0.06)

    def test_holm_caps_at_one_and_ignores_nan(self):
        adj = holm({"a": 0.6, "b": 0.7, "c": math.nan})
        self.assertEqual(adj["a"], 1.0)
        self.assertTrue(math.isnan(adj["c"]))


class TestLadderContrasts(unittest.TestCase):
    def arms(self):
        return {"rung1": arm(12, 3, lambda m, k: 7 + (m % 2)),
                "rung2": arm(12, 3, lambda m, k: 6 + (m % 3 == 0)),
                "placebo": arm(12, 3, lambda m, k: 6 + (m % 2)),
                "remedy": arm(12, 3, lambda m, k: 3 + (m + k) % 2)}

    def test_all_three_contrasts_and_holm_over_leak(self):
        r = ladder_contrasts(self.arms(), n_perm=1000, seed=2, B=300)
        self.assertEqual(set(r["contrasts"]), {"remedy-placebo", "placebo-rung2", "rung2-rung1"})
        c = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertLess(c["mean"], -0.2)
        self.assertLess(c["ci_hi"], 0)
        self.assertEqual(r["holm_leak"]["m"], 3)
        self.assertEqual(set(r["holm_leak"]["adjusted"]), {"remedy-placebo", "placebo-rung2", "rung2-rung1"})
        self.assertGreaterEqual(c["p_holm"], c["p_perm"])
        for k in ("false_wished_rate", "actual_recall", "ai_node_count", "flip_rate", "kept_rate", "missed_rate",
                  "disagreement_vs_rung2", "disagreement_vs_rung2_experienced"):
            self.assertIn(k, r["contrasts"]["remedy-placebo"]["metrics"])
        self.assertEqual(r["parameters"]["n_perm"], 1000)
        self.assertEqual(r["parameters"]["seed"], 2)

    def test_draws_averaged_within_map_before_pairing(self):
        # map M00 has 3 draws (leaks 0,0,6 -> mean 2) vs one draw of 4: weight of a map does not depend on its draws
        a = [cell("M00", k, n, 10 - n) for k, n in enumerate((0, 0, 6))] + [cell(f"M0{i}", 0, 1, 9) for i in (1, 2, 3)]
        b = [cell("M00", k, 4, 6) for k in range(3)] + [cell(f"M0{i}", 0, 1, 9) for i in (1, 2, 3)]
        r = ladder_contrasts({"remedy": a, "placebo": b}, contrasts=(("remedy", "placebo"),), n_perm=100, seed=1, B=50)
        m = r["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]
        self.assertAlmostEqual(m["mean"], (0.2 - 0.4) / 4)

    def test_unequal_draw_counts_across_arms_is_an_error(self):
        arms = self.arms()
        arms["placebo"] = arms["placebo"][:-1]                      # one draw short in the placebo
        with self.assertRaises(ValueError) as cm:
            ladder_contrasts(arms, n_perm=100, seed=1, B=50)
        self.assertIn("draw", str(cm.exception))

    def test_different_map_sets_is_an_error(self):
        arms = self.arms()
        arms["placebo"] = [replace(d, map_id=d.map_id + "x") for d in arms["placebo"]]
        with self.assertRaises(ValueError):
            ladder_contrasts(arms, n_perm=100, seed=1, B=50)

    def test_missing_arm_skips_its_contrasts_and_says_so(self):
        arms = self.arms()
        del arms["rung1"]
        r = ladder_contrasts(arms, n_perm=100, seed=1, B=50)
        self.assertEqual(set(r["contrasts"]), {"remedy-placebo", "placebo-rung2"})
        self.assertEqual([s["contrast"] for s in r["skipped"]], ["rung2-rung1"])
        self.assertEqual(r["holm_leak"]["m"], 2)

    def test_disagreement_is_descriptive_and_not_applicable_against_rung_two_itself(self):
        r = ladder_contrasts(self.arms(), n_perm=100, seed=1, B=50)
        # remedy vs placebo: both measured against rung 2, so the contrast is computed ...
        c = r["contrasts"]["remedy-placebo"]["metrics"]["disagreement_vs_rung2"]
        self.assertFalse(c["gating"])
        self.assertGreater(c["mean_a"], 0.0)
        # ... but where one side IS rung 2 the row would compare rung 2 with itself: n/a, not a number
        for name in ("placebo-rung2", "rung2-rung1"):
            for k in ("disagreement_vs_rung2", "disagreement_vs_rung2_experienced"):
                self.assertEqual(r["contrasts"][name]["metrics"][k]["status"], "not_applicable")
        self.assertTrue(r["contrasts"]["placebo-rung2"]["metrics"]["flip_rate"]["gating"])

    def test_dropping_leaky_ideals_lowers_leak_and_leaves_kept_in_the_contrast_path(self):
        # contrast path (_metric_parts), not three_way: a leak rate conditioned on a match would NOT fall here
        kept = [cell(f"M{m}", 0, 6, 4) for m in range(6)]
        dropped = [cell(f"M{m}", 0, 0, 4, n_miss=6) for m in range(6)]
        r = ladder_contrasts({"remedy": dropped, "placebo": kept}, contrasts=(("remedy", "placebo"),), n_perm=100,
                             seed=1, B=50)
        m = r["contrasts"]["remedy-placebo"]["metrics"]
        self.assertAlmostEqual(m["leaked_rate"]["mean"], -0.6)
        self.assertAlmostEqual(m["kept_rate"]["mean"], 0.0)
        self.assertAlmostEqual(m["missed_rate"]["mean"], 0.6)

    def test_report_passes_the_gate_flags_and_echoes_the_rules(self):
        r = ladder_report(self.arms(), n_perm=200, seed=1, B=50, require_kept_gain=True, require_holm=False)
        rules = r["verdict"]["rules"]
        self.assertEqual((rules["require_kept_gain"], rules["require_holm"], rules["n_perm"], rules["seed"]),
                         (True, False, 200, 1))
        self.assertEqual(r["parameters"]["verdict_rules"], rules)

    def test_report_labels_a_change_from_the_declaration_without_blocking(self):
        import os
        import tempfile
        from second_scribe.audit.registry import AuditLog
        from second_scribe.ladder_metrics import declare_contrasts
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "run.jsonl")
            log = AuditLog(path, "r")
            declare_contrasts(log, rung="remedy", contrasts=["remedy-placebo"], draws={"remedy": 3, "placebo": 3},
                              n_perm=200, seed=1, B=50)
            r = ladder_report(self.arms(), n_perm=200, seed=1, B=50, declaration_log=path, declaration_rung="remedy")
        self.assertEqual(r["declaration_check"]["label"], "exploratory_after_real_data")   # more contrasts/arms than declared
        self.assertEqual(r["verdict"]["verdict"], "helps")                                 # still computed
        r2 = ladder_report(self.arms(), n_perm=200, seed=1, B=50)
        self.assertNotIn("declaration_check", r2)

    def test_json_safe(self):
        r = ladder_report(self.arms(), n_perm=200, seed=1, B=50)
        json.dumps(r, allow_nan=False)
        self.assertEqual(set(r["rungs"]), {"rung1", "rung2", "placebo", "remedy"})


class TestScriptConditionalDescriptive(unittest.TestCase):
    def units(self, m, k, scripted_leak, unscripted_leak):
        return tuple([(True, True)] * scripted_leak + [(True, False)] * (3 - scripted_leak)
                     + [(False, True)] * unscripted_leak + [(False, False)] * (3 - unscripted_leak))

    def test_gap_per_rung_with_no_between_rung_test(self):
        arms = {"rung2": [cell(f"M{m}", k, 3, 3, units=self.units(m, k, 3, 1)) for m in range(10) for k in range(2)],
                "remedy": [cell(f"M{m}", k, 2, 4, units=self.units(m, k, 2, 2)) for m in range(10) for k in range(2)]}
        r = script_conditional_by_rung(arms, seed=1, B=200)
        self.assertAlmostEqual(r["rungs"]["rung2"]["estimate"], 1.0 - 1 / 3)
        self.assertAlmostEqual(r["rungs"]["remedy"]["estimate"], 2 / 3 - 2 / 3)
        self.assertEqual(r["rungs"]["rung2"]["n_maps"], 10)
        self.assertTrue(r["descriptive_only"])
        self.assertNotIn("p_between", json.dumps(r))
        self.assertEqual(r["gaps"], {"rung2": r["rungs"]["rung2"]["estimate"], "remedy": r["rungs"]["remedy"]["estimate"]})
        self.assertNotIn("p_perm", r["rungs"]["rung2"])      # draws pooled -> pseudo-replicated units: no p is printed
        self.assertEqual(r["rungs"]["rung2"]["B"], 200)

    def test_adapter_fills_units_from_metrics_script_maps(self):
        from second_scribe.ladder_metrics import draw_from_pair_inputs
        from second_scribe.match import PRIMARY_SETTING
        from tests.test_f2_script_conditional import FULL, other_source, participant
        p = participant(1, 6, 2, scripted=(0, 1, 2), leaks=(0, 3))
        d = draw_from_pair_inputs(p, FULL, 0, PRIMARY_SETTING, source=other_source())
        self.assertEqual(sorted(d.script_units), sorted([(True, True), (True, False), (True, False),
                                                          (False, True), (False, False), (False, False)]))
        self.assertIsNone(draw_from_pair_inputs(p, FULL, 0, PRIMARY_SETTING).script_units)

    def test_rung_without_units_is_reported_as_not_supplied(self):
        r = script_conditional_by_rung({"rung2": [cell("M0", 0, 3, 3)]}, seed=1, B=50)
        self.assertEqual(r["rungs"]["rung2"]["status"], "no_script_units")


def stat(mean, lo, hi, p=0.01):
    return {"mean": mean, "ci_lo": lo, "ci_hi": hi, "p_perm": p, "p_holm": p, "n_used": 20,
            "ci_excludes_zero": lo > 0 or hi < 0}


def contrasts_with(leak, kept=None, **mirror):
    base = {"false_wished_rate": stat(0.0, -0.05, 0.05), "actual_recall": stat(0.0, -0.05, 0.05),
            "ai_node_count": stat(0.0, -1.0, 1.0), "flip_rate": stat(0.0, -0.03, 0.03),
            "missed_rate": stat(0.0, -0.05, 0.05), "disagreement_vs_rung2": stat(0.2, 0.1, 0.3)}
    base.update(mirror)
    return {"contrasts": {"remedy-placebo": {"metrics": {"leaked_rate": leak, "kept_rate": kept or stat(0.2, 0.1, 0.3), **base}}}}


class TestVerdict(unittest.TestCase):
    def test_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1)))
        self.assertEqual(v["verdict"], "helps")

    def test_null_reports_ci_and_the_largest_effect_it_excludes(self):
        v = remedy_verdict(contrasts_with(stat(-0.04, -0.12, 0.04, p=0.4)))
        self.assertEqual(v["verdict"], "null")
        self.assertEqual((v["leak_ci_lo"], v["leak_ci_hi"]), (-0.12, 0.04))
        self.assertAlmostEqual(v["excludes_leak_drop_larger_than"], 0.12)
        self.assertIn("0.12", v["statement"])

    def test_leak_rise_is_harm(self):
        v = remedy_verdict(contrasts_with(stat(0.2, 0.1, 0.3)))
        self.assertEqual(v["verdict"], "harms")

    def test_false_wished_rise_blocks_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), false_wished_rate=stat(0.15, 0.08, 0.22)))
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("false_wished_rate", v["mirror_worsened"])

    def test_actual_recall_drop_blocks_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), actual_recall=stat(-0.1, -0.15, -0.05)))
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("actual_recall", v["mirror_worsened"])

    def test_mirror_worse_but_inside_its_ci_does_not_block(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), false_wished_rate=stat(0.02, -0.04, 0.08)))
        self.assertEqual(v["verdict"], "helps")

    def test_kept_gain_is_not_required_by_default_but_can_be(self):
        c = contrasts_with(stat(-0.2, -0.3, -0.1), kept=stat(0.0, -0.05, 0.05))
        self.assertEqual(remedy_verdict(c)["verdict"], "helps")
        v = remedy_verdict(c, require_kept_gain=True)
        self.assertEqual(v["verdict"], "not_helps")
        self.assertTrue(any("kept rate did not rise" in x for x in v["reasons"]))

    def test_missed_rising_beyond_its_ci_blocks_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), missed_rate=stat(0.15, 0.08, 0.22)))
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("missed_rate", v["mirror_worsened"])

    def test_missed_rising_inside_its_ci_does_not_block(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), missed_rate=stat(0.03, -0.04, 0.10)))
        self.assertEqual(v["verdict"], "helps")

    def test_node_count_drop_blocks_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), ai_node_count=stat(-4.0, -6.0, -2.0)))
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("ai_node_count", v["mirror_worsened"])

    def test_node_count_rise_does_not_block(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), ai_node_count=stat(4.0, 2.0, 6.0)))
        self.assertEqual(v["verdict"], "helps")

    def test_harms_also_needs_the_holm_p(self):
        self.assertEqual(remedy_verdict(contrasts_with(stat(0.2, 0.1, 0.3, p=0.01)))["verdict"], "harms")
        v = remedy_verdict(contrasts_with(stat(0.2, 0.1, 0.3, p=0.2)))
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("Holm", v["reasons"][0])
        self.assertEqual(remedy_verdict(contrasts_with(stat(0.2, 0.1, 0.3, p=0.2)), require_holm=False)["verdict"], "harms")

    def test_verdict_reports_the_number_of_gating_checks_and_their_nominal_false_alarm(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1)))
        self.assertEqual(v["n_gating_checks"], 5)                  # false-wished, actual recall, node count, flip, missed
        self.assertAlmostEqual(v["nominal_false_alarm_rate"], 1 - 0.975 ** 5)

    def test_verdict_accepts_a_json_cleaned_report(self):
        arms = {"remedy": arm(6, 2, lambda m, k: 4), "placebo": arm(6, 2, lambda m, k: 4),
                "rung2": arm(6, 2, lambda m, k: 4)}
        rep = ladder_report(arms, n_perm=100, seed=1, B=50, min_maps=10)       # too few maps -> nan -> None
        self.assertIsNone(rep["contrasts"]["remedy-placebo"]["metrics"]["leaked_rate"]["ci_lo"])
        self.assertEqual(remedy_verdict(rep)["verdict"], "inconclusive")

    def test_unassessable_mirror_metric_withholds_helps(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), flip_rate=stat(math.nan, math.nan, math.nan, math.nan)))
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("flip_rate", v["not_assessed"])

    def test_remedy_flipping_experienced_items_to_wished_is_not_help(self):
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), flip_rate=stat(0.12, 0.05, 0.19)))
        self.assertEqual(v["verdict"], "not_helps")
        self.assertIn("flip_rate", v["mirror_worsened"])

    def test_item_churn_alone_does_not_block_help(self):
        # the remedy picks different experienced items than rung 2 (large disagreement), but flips none to wished
        v = remedy_verdict(contrasts_with(stat(-0.2, -0.3, -0.1), disagreement_vs_rung2=stat(0.5, 0.4, 0.6),
                                          disagreement_vs_rung2_experienced=stat(0.4, 0.3, 0.5)))
        self.assertEqual(v["verdict"], "helps")

    def test_ci_excludes_zero_but_holm_p_not_small_is_inconclusive(self):
        lk = stat(-0.2, -0.3, -0.1, p=0.2)
        self.assertEqual(remedy_verdict(contrasts_with(lk))["verdict"], "inconclusive")
        self.assertEqual(remedy_verdict(contrasts_with(lk), require_holm=False)["verdict"], "helps")

    def test_missing_remedy_contrast(self):
        self.assertEqual(remedy_verdict({"contrasts": {}})["verdict"], "not_run")


if __name__ == "__main__":
    unittest.main()
