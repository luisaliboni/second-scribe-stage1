"""The report renderer builds the ladder's prose from codes and numbers; the aggregates keep none."""
from __future__ import annotations

import copy
import tempfile
import unittest

from second_scribe import report
from tests.test_pipeline_ladder_aggregates import ALL, build


class TestLadderMarkdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-ladrep-")
        cls.addClassCleanup(tmp.cleanup)
        _cfg, _out, res = build(tmp.name, "lad", {"rungs": ALL, "draws": 2, "n_perm": 50, "B": 50})
        cls.agg = res["aggregates"]
        cls.md = report.report_markdown(cls.agg)

    def test_section_is_present_and_marked_in_distribution(self):
        self.assertIn("## Instruction ladder", self.md)
        self.assertIn("in-distribution", self.md)

    def test_verdict_prose_comes_from_the_codes(self):
        self.assertIn("verdict: **harms**", self.md)
        self.assertIn("the share drawn as received rose with the remedy", self.md)
        self.assertNotIn("leak_rose", self.md)

    def test_rules_are_explained_in_words(self):
        for phrase in ("percentile bootstrap", "sign-flip permutation", "not tested"):
            self.assertIn(phrase, self.md)

    def test_table_has_a_row_per_rung_with_the_damage(self):
        row = next(l for l in self.md.splitlines() if l.startswith("| placebo |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[1:5], ["24", "22", "2", "0"])        # expected, ok, failed, other
        self.assertEqual(cells[6], "6")                             # retries

    def test_interpretation_and_script_source_are_stated(self):
        self.assertIn("- interpretation: interpreted; script of the script-conditional gaps: own (family own)", self.md)

    def test_a_script_source_family_label_is_said_to_rest_on_the_sent_model_id(self):
        """F5: the label is derived from the model id each call was SENT with; the endpoint's own reply is not logged."""
        self.assertNotIn("model id each call was sent with", self.md)
        agg = copy.deepcopy(self.agg)
        agg["script_sources"] = {"judgeprior": {"family": "other", "reason": "different_family", "role": "judge"}}
        md = report.report_markdown(agg)
        self.assertIn("rests on the model id each call was sent with", md)

    def test_declaration_line(self):
        self.assertIn("**as_declared**", self.md)

    def test_null_verdict_states_the_excluded_drop(self):
        agg = copy.deepcopy(self.agg)
        v = agg["ladder"]["report"]["verdict"]
        v.update(verdict="null", reason_codes=["leak_ci_includes_zero"], excludes_leak_drop_larger_than=0.25)
        md = report.report_markdown(agg)
        self.assertIn("exclude a drop in the share drawn as received larger than 0.250", md)

    def test_an_incomplete_ladder_states_it_in_words_and_has_no_contrast_verdict(self):
        agg = copy.deepcopy(self.agg)
        agg["ladder"]["complete"] = False
        agg["ladder"]["report"]["verdict"] = {"verdict": "incomplete", "reason_codes": ["incomplete"]}
        md = report.report_markdown(agg)
        self.assertIn("verdict: **incomplete**", md)
        self.assertIn("judge labels or alignments are missing", md)
        self.assertNotIn("(incomplete)", md)

    def test_no_ladder_no_section(self):
        agg = copy.deepcopy(self.agg)
        agg.pop("ladder")
        self.assertNotIn("Instruction ladder", report.report_markdown(agg))


if __name__ == "__main__":
    unittest.main()
