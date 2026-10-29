"""F2 items 1, 2 and 4 on synthetic fixtures: the base-rate claim, the script-conditional ("pull toward the
script") test with its within-map permutation null, and why the old status-permutation null is no headline.

The fixtures are deterministic: no randomness in the data, so a failing p-value is a real change."""
import json
import math
import random
import unittest
from dataclasses import replace

from second_scribe.match import PRIMARY_SETTING, SETTINGS_GRID
from second_scribe.metrics import (PairInputs, ScriptSource, base_rate, headline, script_conditional,
                                   script_conditional_row, status_permutation_null)
from second_scribe.provenance import Provenance
from second_scribe.schemas import MapGraph, Node

FULL = "full__p0__t0__r0"
VAR = "full__p1__t0__r0"
S = PRIMARY_SETTING
OTHER = "judge-family"


def prov():
    return Provenance("x", "node", True, "participant", 1, 1, 12, False, False)


def lab(i, k, tag="e"):
    return f"zq{tag}{i:03d}x{k:02d} zr{tag}{i:03d}x{k:02d} zs{tag}{i:03d}x{k:02d}"


def script_for(role="r1"):
    return {role: MapGraph("script", "prior_only", [Node("s0", "scriptconcept")])}


def other_source():
    return ScriptSource(OTHER, "other", script_for())


def own_source():
    return ScriptSource("own", "own", script_for())


def participant(i, n_ideals, n_controls, *, scripted=(), leaks=(), flips=(), spec=FULL, specs=(FULL,),
                role="r1", prior=False):
    """Endorsed map: ``n_ideals`` wished nodes (indexes 0..), then ``n_controls`` experienced ones, all in the
    transcript. One AI node per endorsed node, same words. Ideal k is drawn experienced (a leak) if k in ``leaks``;
    control c is drawn wished (a flip) if c in ``flips``. Ideals in ``scripted`` match the script concept."""
    ends = [Node(f"e{k}", lab(i, k), status="wished") for k in range(n_ideals)]
    ends += [Node(f"e{n_ideals + c}", lab(i, n_ideals + c)) for c in range(n_controls)]
    e = MapGraph(f"E{i}", "endorsed", ends, [])
    ai_nodes, mod = [], {}
    for k, n in enumerate(ends):
        if k < n_ideals:
            st = "experienced" if k in leaks else "wished"
        else:
            st = "wished" if (k - n_ideals) in flips else "experienced"
        ai_nodes.append(Node(f"a{k}", n.text, status=st, quote="q"))
        mod[f"a{k}"] = "wished" if st == "wished" else "actual"
    g = MapGraph(f"A{i}", "ai", ai_nodes, [])
    ai, pv, md, jj = {}, {}, {}, {}
    for sp in specs:
        ai[sp], pv[sp], md[sp], jj[sp] = g, {n.node_id: prov() for n in ai_nodes}, dict(mod), {}
    return PairInputs(
        pid=f"P{i}", role=role, interviewer="I1", endorsed=e, ai=ai, prov=pv, modality=md,
        in_transcript={n.node_id: True for n in ends}, judged_ai_vs_endorsed=jj, judged_prior_vs_endorsed={},
        judged_ai_vs_script={sp: {} for sp in specs},
        judged_endorsed_vs_script={(k, 0): "same" for k in scripted},
        judged_endorsed_vs_sources={OTHER: {(k, 0): "same" for k in scripted}})


def scenario_a(n=48):
    """Scripted ideals (0-2) leak 90% of the time, unscripted ideals (3-5) 40%."""
    return [participant(m, 6, 4, scripted=(0, 1, 2),
                        leaks=[j for j in range(3) if (m * 3 + j) % 10 != 0]
                        + [j for j in range(3, 6) if (m * 3 + j) % 10 < 4])
            for m in range(n)]


def scenario_b(n=48):
    """Leak rate 5/6 in BOTH groups: in map m the one ideal that does not leak is index m % 6."""
    return [participant(m, 6, 4, scripted=(0, 1, 2), leaks=[j for j in range(6) if j != m % 6]) for m in range(n)]


def scenario_c(n=48):
    """A realistic informative leaky AI: 5 ideals (3 leak, 2 recovered) and 15 experienced nodes, ~3% of them flipped."""
    return [participant(m, 5, 15, leaks=[j for j in range(5) if (j + m) % 5 < 3],
                        flips=[c for c in range(15) if (m * 15 + c) % 33 == 0]) for m in range(n)]


class TestScriptConditional(unittest.TestCase):
    def test_a_scripted_ideals_leak_more_gives_small_p(self):
        r = script_conditional_row(scenario_a(), FULL, S, other_source(), n_perm=2000, B=300, seed=1)
        self.assertEqual((r["n"], r["n_scripted"], r["n_unscripted"]), (48, 144, 144))
        self.assertAlmostEqual(r["leak_rate_scripted"], 0.9, delta=0.02)
        self.assertAlmostEqual(r["leak_rate_unscripted"], 0.4, delta=0.02)
        self.assertAlmostEqual(r["estimate"], r["leak_rate_scripted"] - r["leak_rate_unscripted"])
        self.assertLess(r["p_perm"], 0.01)
        self.assertGreater(r["ci_lo"], 0.3)
        self.assertEqual((r["alternative"], r["n_perm"], r["seed"]), ("greater", 2000, 1))
        self.assertFalse(r["low_power"])
        print(f"\n[F2 test a] estimate={r['estimate']:.3f} perm p={r['p_perm']:.5f} CI=[{r['ci_lo']:.3f}, {r['ci_hi']:.3f}]")

    def test_b_script_independent_leakage_gives_no_small_p_at_any_overall_rate(self):
        r = script_conditional_row(scenario_b(), FULL, S, other_source(), n_perm=2000, B=300, seed=1)
        self.assertAlmostEqual(r["leak_rate_scripted"], 5 / 6)          # the 83% leak of the council simulation
        self.assertAlmostEqual(r["leak_rate_unscripted"], 5 / 6)
        self.assertAlmostEqual(r["estimate"], 0.0)
        self.assertGreater(r["p_perm"], 0.2)
        self.assertLessEqual(r["ci_lo"], 0.0)
        print(f"\n[F2 test b] 83% leak, same in both groups: estimate={r['estimate']:.3f} perm p={r['p_perm']:.4f}")
        # and at other overall rates, still no pull: everything leaks, nothing leaks
        for leaks in ((0, 1, 2, 3, 4, 5), ()):
            ps = [participant(m, 6, 4, scripted=(0, 1, 2), leaks=leaks) for m in range(48)]
            q = script_conditional_row(ps, FULL, S, other_source(), n_perm=500, B=100, seed=1)
            self.assertAlmostEqual(q["estimate"], 0.0)
            self.assertGreater(q["p_perm"], 0.2)

    def test_c_the_old_status_null_cannot_see_a_leaky_informative_ai(self):
        ps = scenario_c()
        old = status_permutation_null(ps, FULL, S, n_perm=500, seed=1)
        self.assertAlmostEqual(old["observed_mean"], 0.6 - 0.03, delta=0.03)       # leak .60 minus flip ~.03
        self.assertGreater(old["null_mean"], old["observed_mean"] + 0.1)           # the null leaks MORE than the AI
        self.assertGreater(old["p"], 0.95)                                         # so "worse than chance" never fires
        self.assertFalse(old["observed_exceeds_null_p95"])
        print(f"\n[F2 test c] realistic leaky AI: observed={old['observed_mean']:.3f} null mean={old['null_mean']:.3f} old-null p={old['p']:.4f}")


class TestScriptConditionalMechanics(unittest.TestCase):
    def test_a_map_level_confound_does_not_fire_the_within_map_null(self):
        # leaky maps happen to hold more scripted ideals (4 of 6) than clean maps (2 of 6), but inside every map
        # the leak does not follow the script: the pooled difference is positive, the within-map p is not small
        ps = [participant(m, 6, 4, scripted=(0, 1, 2, 3), leaks=(0, 1, 2, 3, 4, 5)) for m in range(24)]
        ps += [participant(24 + m, 6, 4, scripted=(0, 1), leaks=()) for m in range(24)]
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=500, B=100, seed=1)
        self.assertAlmostEqual(r["estimate"], 2 / 3 - 1 / 3)
        self.assertEqual(r["p_perm"], 1.0)           # every within-map permutation gives the same statistic
        print(f"\n[F2 confound] pooled estimate={r['estimate']:.3f} within-map perm p={r['p_perm']:.3f}")

    def test_deterministic_seeded_and_global_random_untouched(self):
        ps = scenario_a(12)
        a = script_conditional_row(ps, FULL, S, other_source(), n_perm=200, B=50, seed=3)
        self.assertEqual(a, script_conditional_row(ps, FULL, S, other_source(), n_perm=200, B=50, seed=3))
        random.seed(5)
        expected = random.random()
        random.seed(5)
        script_conditional_row(ps, FULL, S, other_source(), n_perm=20, B=10, seed=3)
        self.assertEqual(random.random(), expected)

    def test_permutation_keeps_the_per_map_counts(self):
        # every map has exactly one scripted ideal; the null therefore cannot move more leaks onto the scripted
        # side than there are leaks in the map, so a perfectly aligned pattern gets the smallest possible p
        ps = [participant(m, 4, 2, scripted=(0,), leaks=(0,)) for m in range(10)]     # only the scripted one leaks
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=400, B=50, seed=1)
        self.assertEqual((r["leak_rate_scripted"], r["leak_rate_unscripted"], r["estimate"]), (1.0, 0.0, 1.0))
        self.assertAlmostEqual(r["p_perm"], 1 / 401)          # the observed statistic is the maximum, reached 0 times by chance

    def test_low_power_flag_and_exclusions(self):
        ps = scenario_a(4)                                    # 12 scripted and 12 unscripted ideals
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=100, B=50, seed=1)
        self.assertTrue(r["low_power"])
        self.assertEqual((r["n_scripted"], r["n_unscripted"]), (12, 12))
        self.assertFalse(script_conditional_row(scenario_a(10), FULL, S, other_source(), n_perm=100, B=50)["low_power"])
        ps = scenario_a(6)
        ps[1] = replace(ps[1], ai={FULL: None})
        ps[2] = replace(ps[2], judged_endorsed_vs_sources={OTHER: None})
        ps[3] = replace(ps[3], role="r2")                     # no script for this role
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=100, B=50)
        self.assertEqual((r["n"], r["n_excluded"]), (3, 3))
        self.assertEqual(r["excluded_reasons"], {"ai_failed": 1, "alignment_missing": 1, "no_script": 1})

    def test_missing_alignment_is_never_read_as_nothing_scripted(self):
        ps = [replace(p, judged_endorsed_vs_sources=None) for p in scenario_a(6)]
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=50, B=20)
        self.assertEqual(r["n"], 0)                                           # an "other" source has no fallback
        self.assertEqual(r["excluded_reasons"], {"alignment_missing": 6})
        self.assertTrue(math.isnan(r["estimate"]) and math.isnan(r["p_perm"]))
        own = script_conditional_row(ps, FULL, S, own_source(), n_perm=50, B=20)    # "own" uses judged_endorsed_vs_script
        self.assertEqual(own["n"], 6)

    def test_a_group_with_no_ideals_has_no_statistic(self):
        ps = [participant(m, 3, 2, scripted=(), leaks=(0,)) for m in range(5)]      # nothing is scripted
        r = script_conditional_row(ps, FULL, S, other_source(), n_perm=50, B=20)
        self.assertEqual(r["n_scripted"], 0)
        self.assertTrue(math.isnan(r["estimate"]) and math.isnan(r["p_perm"]))

    def test_only_matched_ideals_count(self):
        # an ideal the AI never drew (no AI node) is missed, and is neither a leak nor a non-leak
        p = participant(0, 3, 1, scripted=(0, 1), leaks=(0,))
        g = p.ai[FULL]
        g2 = replace(g, nodes=[n for n in g.nodes if n.node_id != "a2"])
        p = replace(p, ai={FULL: g2})
        r = script_conditional_row([p], FULL, S, other_source(), n_perm=20, B=10)
        self.assertEqual((r["n_scripted"], r["n_unscripted"]), (2, 0))

    def test_two_sources_one_row_each_and_only_the_other_family_is_primary(self):
        ps = scenario_a(12)
        out = script_conditional(ps, FULL, [FULL], [S], S, [own_source(), other_source()], n_perm=300, B=50, seed=2)
        self.assertEqual(out["primary_source"], OTHER)
        self.assertEqual(set(out["rows"]), {"own", OTHER})
        self.assertEqual(out["rows"]["own"]["label"], "own-script (sensitivity only)")
        self.assertFalse(out["rows"]["own"]["headline_eligible"])
        self.assertTrue(out["rows"][OTHER]["headline_eligible"])
        self.assertIn("sign_stability", out["rows"][OTHER])

    def test_own_script_alone_never_sets_the_headline_flag(self):
        out = script_conditional(scenario_a(48), FULL, [FULL], [S], S, [own_source()], n_perm=300, B=50)
        self.assertLess(out["rows"]["own"]["p_perm"], 0.01)                     # the pattern is there ...
        self.assertIsNone(out["primary_source"])
        self.assertFalse(out["claim_script_conditional_leakage"])               # ... but the flag stays unset

    def test_flag_needs_small_p_and_ci_above_zero(self):
        yes = script_conditional(scenario_a(48), FULL, [FULL], [S], S, [other_source()], n_perm=300, B=100)
        self.assertTrue(yes["claim_script_conditional_leakage"])
        self.assertFalse(yes["primary_low_power"])
        no = script_conditional(scenario_b(48), FULL, [FULL], [S], S, [other_source()], n_perm=300, B=100)
        self.assertFalse(no["claim_script_conditional_leakage"])
        none = script_conditional(scenario_a(48), FULL, [FULL], [S], S, [], n_perm=50, B=20)
        self.assertEqual((none["primary_source"], none["claim_script_conditional_leakage"]), (None, False))

    def test_duplicate_source_names_are_refused(self):
        with self.assertRaises(ValueError):
            script_conditional(scenario_a(4), FULL, [FULL], [S], S, [other_source(), other_source()], n_perm=10, B=10)
        with self.assertRaises(ValueError):
            ScriptSource("x", "third", {})


class TestBaseRate(unittest.TestCase):
    def test_leaky_informative_ai_marks_fewer_wished_than_the_endorsed_map(self):
        r = base_rate(scenario_c(), FULL, S, B=300, seed=1)
        # per map: 5 of 20 matched nodes are endorsed wished; the AI draws 2 recovered + ~0.5 flipped as wished
        self.assertAlmostEqual(r["endorsed_wished_share"], 0.25)
        self.assertAlmostEqual(r["ai_wished_share"], 0.1 + 0.03 / 1 * 0.75, delta=0.02)
        self.assertLess(r["mean"], -0.1)
        self.assertLess(r["ci_hi"], 0)
        self.assertTrue(r["ai_below_endorsed"])
        self.assertEqual((r["n"], r["n_matched_nodes"]), (48, 48 * 20))

    def test_perfect_ai_has_no_gap(self):
        ps = [participant(m, 5, 15) for m in range(12)]
        r = base_rate(ps, FULL, S, B=100)
        self.assertAlmostEqual(r["mean"], 0.0)
        self.assertFalse(r["ai_below_endorsed"])

    def test_unmatched_and_failed_maps_are_excluded_with_reasons(self):
        ps = scenario_c(4)
        ps[0] = replace(ps[0], ai={FULL: None})
        g = ps[1].ai[FULL]
        ps[1] = replace(ps[1], ai={FULL: replace(g, nodes=[])})
        r = base_rate(ps, FULL, S, B=50)
        self.assertEqual(r["excluded_reasons"], {"ai_failed": 1, "no_matched_in_transcript": 1})
        self.assertEqual(r["n"], 2)


class TestInTheHeadline(unittest.TestCase):
    def run_h(self, ps, **kw):
        kw.setdefault("B", 60)
        kw.setdefault("n_perm_script", 200)
        kw.setdefault("n_perm", 20)
        return headline(ps, FULL, [VAR], list(SETTINGS_GRID), **kw)

    def two_spec(self, ps):
        return [replace(p, ai={**p.ai, VAR: p.ai[FULL]}, prov={**p.prov, VAR: p.prov[FULL]},
                        modality={**p.modality, VAR: p.modality[FULL]},
                        judged_ai_vs_endorsed={FULL: {}, VAR: {}}, judged_ai_vs_script={FULL: {}, VAR: {}})
                for p in ps]

    def test_absence_block_has_the_new_rows_and_no_old_headline_names(self):
        h = self.run_h(self.two_spec(scenario_a(12)), script_sources=[other_source(), own_source()])
        a = h["absences"]
        for k in ("base_rate", "script_conditional", "leak_split", "worse_than_chance_check", "pooled", "paired_diff"):
            self.assertIn(k, a)
        self.assertNotIn("mechanism", a)
        self.assertNotIn("claim_counterfactual_leakage", a)
        sc = a["script_conditional"]
        self.assertEqual(sc["primary_source"], OTHER)
        self.assertEqual((sc["rows"][OTHER]["n_perm"], sc["rows"]["own"]["label"]), (200, "own-script (sensitivity only)"))
        self.assertEqual((sc["n_perm"], sc["seed"]), (200, 0))                  # recorded for the log
        w = a["worse_than_chance_check"]
        self.assertIn("p", w)
        self.assertNotIn("label", w)
        json.dumps(h, allow_nan=False)

    def test_default_sources_is_the_own_script_only(self):
        h = self.run_h(self.two_spec(scenario_a(12)))
        sc = h["absences"]["script_conditional"]
        self.assertEqual(set(sc["rows"]), {"own"})
        self.assertIsNone(sc["primary_source"])
        self.assertFalse(sc["claim_script_conditional_leakage"])

    def test_leak_split_block_reports_counts_and_rates(self):
        ps = self.two_spec([participant(m, 5, 5, leaks=(0, 1, 2)) for m in range(6)])
        ls = self.run_h(ps)["absences"]["leak_split"]
        self.assertEqual(ls["n_leaked"], 18)
        self.assertEqual(ls["adequacy_flattening"]["count"], 18)               # fixtures quote "actual" for leaks
        self.assertEqual(ls["counterfactual_leakage"]["count"], 0)
        self.assertAlmostEqual(ls["adequacy_flattening"]["rate_of_wished"], 18 / 30)
        self.assertAlmostEqual(ls["adequacy_flattening"]["share_of_leaks"], 1.0)
        self.assertEqual(ls["counterfactual_leakage"]["share_of_leaks"], 0.0)

    def test_only_the_split_carries_the_counterfactual_name(self):
        h = self.run_h(self.two_spec(scenario_a(8)))

        def walk(x, path=""):
            if isinstance(x, dict):
                for k, v in x.items():
                    yield path + "/" + str(k), k
                    yield from walk(v, path + "/" + str(k))
        paths = list(walk(h["absences"]))
        for path, k in paths:
            if "counterfactual" in k:
                self.assertIn(k, ("counterfactual_leakage", "counterfactual_leakage_rate"), path)
                self.assertTrue("/leak_split/" in path or "/pooled/" in path, path)
            self.assertNotIn("targeted", k)
        self.assertNotIn("claim_counterfactual_leakage", dict(paths).values())
