"""Wire-b item 6: the ladder report is aggregate-safe at the source. ``ladder_report`` emits codes and short enums
(no sentence, no string over the aggregate-only guard's 40 characters); the sentences are built by the report renderer
from those codes and the numbers (``report.ladder_markdown``). The guard is NOT relaxed: these tests run it."""
import json
import re
import unittest
from dataclasses import replace

from second_scribe.ladder_metrics import (analysis_config, ladder_contrasts, ladder_report, remedy_verdict,
                                          script_conditional_by_rung, verdict_rules)
from second_scribe.report import MAX_STRING, assert_aggregate_only

from tests.test_ladder_contrasts import arm, cell, contrasts_with, stat

CODE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_]+)?$")


def long_strings(x, where="$", out=None):
    out = [] if out is None else out
    if isinstance(x, str):
        if len(x) > MAX_STRING:
            out.append(where)
    elif isinstance(x, dict):
        for k, v in x.items():
            long_strings(k, where + ".<key>", out)
            long_strings(v, f"{where}.{k}", out)
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            long_strings(v, f"{where}[{i}]", out)
    return out


def four_arms(remedy_leak=lambda m, k: 1, units=False):
    kw = {}
    mk = lambda leaks: arm(12, 2, leaks, **kw)  # noqa: E731
    out = {"rung1": mk(lambda m, k: 5), "rung2": mk(lambda m, k: 4 + (m % 2)), "placebo": mk(lambda m, k: 4 + (m % 2)),
           "remedy": mk(lambda m, k: remedy_leak(m, k))}
    if units:
        for key in out:
            out[key] = [replace(d, script_units=((True, True), (True, False), (False, False))) for d in out[key]]
    return out


class TestVerdictReasonCodes(unittest.TestCase):
    def codes(self, c, **kw):
        v = remedy_verdict(c, **kw)
        self.assertIn("reason_codes", v, v["verdict"])
        for x in v["reason_codes"]:
            self.assertRegex(x, CODE)
            self.assertLessEqual(len(x), MAX_STRING)
        return v["verdict"], v["reason_codes"]

    def test_every_branch_has_codes(self):
        self.assertEqual(self.codes(contrasts_with(stat(-0.2, -0.3, -0.1))), ("helps", ["leak_drop_no_mirror_worse"]))
        self.assertEqual(self.codes(contrasts_with(stat(-0.04, -0.12, 0.04, p=0.4))), ("null", ["leak_ci_includes_zero"]))
        self.assertEqual(self.codes(contrasts_with(stat(0.2, 0.1, 0.3))), ("harms", ["leak_rose"]))
        self.assertEqual(self.codes(contrasts_with(stat(0.2, 0.1, 0.3, p=0.2))), ("inconclusive", ["leak_rose_holm_fails"]))
        self.assertEqual(self.codes(contrasts_with(stat(-0.2, -0.3, -0.1, p=0.2))),
                         ("inconclusive", ["leak_drop_holm_fails"]))
        self.assertEqual(self.codes({"contrasts": {}}), ("not_run", ["contrast_not_run"]))
        nan = {"mean": None, "ci_lo": None, "ci_hi": None, "p_perm": None, "p_holm": None}
        self.assertEqual(self.codes(contrasts_with(nan)), ("inconclusive", ["leak_too_few_maps"]))

    def test_not_helps_names_the_worsened_metrics_and_the_kept_gain(self):
        v, codes = self.codes(contrasts_with(stat(-0.2, -0.3, -0.1), false_wished_rate=stat(0.15, 0.08, 0.22),
                                             missed_rate=stat(0.15, 0.08, 0.22)))
        self.assertEqual(v, "not_helps")
        self.assertEqual(sorted(codes), ["mirror_worse:false_wished_rate", "mirror_worse:missed_rate"])
        v, codes = self.codes(contrasts_with(stat(-0.2, -0.3, -0.1), kept=stat(0.0, -0.05, 0.05)), require_kept_gain=True)
        self.assertEqual((v, codes), ("not_helps", ["kept_not_risen"]))

    def test_not_assessed_metric_is_a_code(self):
        nan = {"mean": None, "ci_lo": None, "ci_hi": None, "p_perm": None, "p_holm": None}
        v, codes = self.codes(contrasts_with(stat(-0.2, -0.3, -0.1), flip_rate=nan))
        self.assertEqual((v, codes), ("inconclusive", ["not_assessed:flip_rate"]))

    def test_the_prose_is_still_there_for_direct_callers(self):
        v = remedy_verdict(contrasts_with(stat(-0.04, -0.12, 0.04, p=0.4)))
        self.assertIn("0.12", v["statement"])
        self.assertTrue(v["reasons"])


class TestReportIsAggregateSafe(unittest.TestCase):
    def report(self, **kw):
        return ladder_report(four_arms(**kw), n_perm=100, seed=1, B=50)

    def test_no_string_over_the_guard_limit_and_the_guard_passes(self):
        for name, kw in (("remedy helps", dict(remedy_leak=lambda m, k: 0)),
                         ("remedy null", dict(remedy_leak=lambda m, k: 4 + (m % 2))),
                         ("remedy harms", dict(remedy_leak=lambda m, k: 9)),
                         ("with units", dict(units=True))):
            with self.subTest(name):
                r = self.report(**kw)
                self.assertEqual(long_strings(r), [], r["verdict"]["verdict"])
                assert_aggregate_only(r, set())
                json.dumps(r, allow_nan=False)

    def test_the_verdict_carries_codes_and_numbers_not_sentences(self):
        r = self.report(remedy_leak=lambda m, k: 4 + (m % 2))
        v = r["verdict"]
        self.assertEqual(v["verdict"], "null")
        self.assertNotIn("statement", v)
        self.assertNotIn("reasons", v)
        self.assertEqual(v["reason_codes"], ["leak_ci_includes_zero"])
        self.assertIn("excludes_leak_drop_larger_than", v)
        self.assertEqual(r["parameters"]["ci"], "boot_maps")
        self.assertEqual(r["verdict"]["rules"]["ci"], "boot_maps")
        self.assertEqual(r["verdict"]["rules"]["test"], "signflip")
        self.assertEqual(r["verdict"]["rules"]["exact_max_nonzero"], 12)
        self.assertEqual(r["script_conditional"]["note"], "no_test")

    def test_the_numbers_are_those_of_the_prose_report(self):
        # the contrasts, summaries and declaration inputs did not change: only strings became codes
        arms = four_arms(remedy_leak=lambda m, k: 1)
        a = ladder_contrasts(arms, n_perm=100, seed=1, B=50)
        b = ladder_report(arms, n_perm=100, seed=1, B=50)
        self.assertEqual(json.loads(json.dumps(a["contrasts"], allow_nan=False, default=str).replace("NaN", "null")),
                         json.loads(json.dumps(b["contrasts"])))

    def test_rules_in_the_declaration_and_in_the_report_are_the_same_codes(self):
        rules = verdict_rules()
        self.assertEqual(analysis_config()["verdict_rules"], rules)
        self.assertEqual(rules["ci"], "boot_maps")
        self.assertEqual(rules["worse"], "ci_excludes_zero_bad_side")
        for v in rules.values():
            if isinstance(v, str):
                self.assertLessEqual(len(v), MAX_STRING)

    def test_script_conditional_stays_descriptive_and_short(self):
        r = script_conditional_by_rung(four_arms(units=True), seed=1, B=50)
        self.assertTrue(r["descriptive_only"])
        self.assertEqual(r["note"], "no_test")


if __name__ == "__main__":
    unittest.main()
