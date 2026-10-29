"""Pass-rule floors added after the audit (A6 Important 1 and 3): actual recall, wished precision, off-record mention
recall and the size of the pooled off-record set. All thresholds live in judge_bench's PASS_* constants."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe import judge_bench as jb
from second_scribe.audit.registry import AuditLog
from second_scribe.judge_bench import OracleClient, bench, pass_rule, score_modality
from second_scribe.schemas import MODALITIES

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"


def good():
    """A results dict that meets every floor exactly as ``bench`` would shape it."""
    return {"modality": {"accuracy": 0.95, "recall": {c: 0.9 for c in MODALITIES},
                         "precision": {c: 0.9 for c in MODALITIES}, "style_gap": 0.01},
            "alignment": {"strict": {"f1": 0.9}},
            "off_record": {"recall": 0.9, "mention_recall": 0.9, "n_off_record": 120},
            "not_judged_rate": 0.0}


def edit(**path_values):
    r = good()
    for path, v in path_values.items():
        node = r
        *head, last = path.split("__")
        for k in head:
            node = node[k]
        node[last] = v
    return r


class TestConstants(unittest.TestCase):
    def test_new_floors_are_the_bench_constants(self):
        self.assertEqual(jb.PASS_ACTUAL_RECALL, 0.85)
        self.assertEqual(jb.PASS_WISHED_PRECISION, 0.80)

    def test_the_whole_rule_is_one_dict_derived_from_the_constants(self):
        rule = jb.pass_rule_thresholds()
        self.assertEqual(rule["actual_recall_min"], jb.PASS_ACTUAL_RECALL)
        self.assertEqual(rule["wished_precision_min"], jb.PASS_WISHED_PRECISION)
        self.assertEqual(rule["accuracy_min"], jb.PASS_ACCURACY)
        self.assertEqual(rule["recall_min"], jb.PASS_CLASS_RECALL)
        self.assertEqual(rule["recall_classes"], list(jb.PASS_CLASSES))
        self.assertEqual(rule["letter_mass_min"], jb.PASS_LETTER_MASS)

    def test_the_letter_mass_floor_is_the_extractions_own_constant(self):
        from second_scribe.judge import LETTER_MASS_MIN
        self.assertIs(jb.PASS_LETTER_MASS, LETTER_MASS_MIN)

    def test_audits_canary_floor_still_follows_the_bench(self):
        from second_scribe import audits
        self.assertEqual(audits.CANARY_MIN_PASS, jb.PASS_CLASS_RECALL)


class TestFloors(unittest.TestCase):
    def test_the_good_result_passes(self):
        self.assertEqual(pass_rule(good()), (True, []))

    def test_actual_recall_floor_boundaries(self):
        self.assertEqual(pass_rule(edit(modality__recall={**good()["modality"]["recall"], "actual": 0.85})),
                         (True, []))
        ok, reasons = pass_rule(edit(modality__recall={**good()["modality"]["recall"], "actual": 0.849}))
        self.assertFalse(ok)
        self.assertEqual(reasons, ["recall on actual 0.849 < 0.85"])

    def test_wished_precision_floor_boundaries(self):
        r = good()
        r["modality"]["precision"]["wished"] = 0.80
        self.assertEqual(pass_rule(r), (True, []))
        r["modality"]["precision"]["wished"] = 0.799
        ok, reasons = pass_rule(r)
        self.assertFalse(ok)
        self.assertEqual(reasons, ["precision on wished 0.799 < 0.8"])

    def test_missing_metrics_fail_closed(self):
        for path in ("modality__precision",):
            r = good()
            node = r
            *head, last = path.split("__")
            for k in head:
                node = node[k]
            node[last] = None if last != "precision" else {"wished": None}
            ok, reasons = pass_rule(r)
            self.assertFalse(ok, path)

    def test_the_audit_probe_a_judge_that_calls_29_percent_of_actual_quotes_wished_now_fails(self):
        # recall actual 0.71 (audit probe: 0.714) with every other class at 1.0 and wished precision 0.578
        r = good()
        r["modality"]["recall"]["actual"] = 0.714
        r["modality"]["precision"]["wished"] = 0.578
        ok, reasons = pass_rule(r)
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("recall on actual" in x for x in reasons))
        self.assertTrue(any("precision on wished" in x for x in reasons))


class TestPrecisionScoring(unittest.TestCase):
    def items(self):
        mk = lambda i, exp, kind="plain": {"element_id": f"i{i}", "expected": exp, "kind": kind, "style": "prose"}
        return [mk(0, "actual"), mk(1, "actual"), mk(2, "wished", "wished"), mk(3, "wished", "wished"),
                mk(4, "negated", "negated"), mk(5, "actual", "wished"), mk(6, "wished", "mapping_talk")]

    def test_precision_is_the_column_share_over_judged_scored_items(self):
        labels = {"i0": "wished", "i1": "actual", "i2": "wished", "i3": "wished", "i4": "wished", "i5": "actual",
                  "i6": "wished"}
        s = score_modality(self.items(), labels, set())
        # predicted wished among scored: i0 (actual), i2, i3 (wished), i4 (negated) -> 2/4; i6 is mapping_talk: excluded
        self.assertEqual(s["precision_n"]["wished"], 4)
        self.assertAlmostEqual(s["precision"]["wished"], 2 / 4)
        self.assertAlmostEqual(s["precision"]["actual"], 1.0)           # i1 and i5 are predicted actual and are
        self.assertIsNone(s["precision"]["hypothetical"])
        self.assertEqual(len(s["precision_ci"]["wished"]), 2)

    def test_not_judged_items_do_not_enter_precision(self):
        labels = {f"i{i}": "wished" for i in range(7)}
        s = score_modality(self.items(), labels, {"i0", "i4"})
        # judged and scored: i1, i2, i3, i5, all predicted wished; i2 and i3 are right
        self.assertEqual(s["precision_n"]["wished"], 4)
        self.assertAlmostEqual(s["precision"]["wished"], 0.5)


class ActualToWished(OracleClient):
    """The audit probe: calls about 30% of the experienced quotes "wished" and is right everywhere else."""

    def _label(self, element, quote, reply=""):
        label = super()._label(element, quote, reply)
        if label == "actual" and (sum(map(ord, quote + element)) % 10) < 3:
            return "wished"
        return label


class TestEndToEnd(unittest.TestCase):
    def run_bench(self, client, mode):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return bench(client, PROMPTS, None, AuditLog(Path(tmp) / "l.jsonl", "l"), mode=mode, seeds=(0, 1, 2))

    def test_a_judge_that_turns_experienced_quotes_into_wished_fails_on_the_new_floors(self):
        for mode in ("generative", "logprob"):
            r = self.run_bench(ActualToWished(seeds=(0, 1, 2)), mode)
            m = r["modality"]
            self.assertLess(m["recall"]["actual"], jb.PASS_ACTUAL_RECALL, mode)
            self.assertLess(m["precision"]["wished"], jb.PASS_WISHED_PRECISION, mode)
            self.assertFalse(r["pass"]["passed"], mode)
            self.assertTrue(any("recall on actual" in x for x in r["pass"]["reasons"]), mode)
            self.assertTrue(any("precision on wished" in x for x in r["pass"]["reasons"]), mode)
            # the old rule's classes are all fine: only the new floors catch it
            for c in jb.PASS_CLASSES:
                self.assertGreaterEqual(m["recall"][c], jb.PASS_CLASS_RECALL, c)

    def test_the_oracle_still_passes_and_the_report_and_arms_show_the_new_rows(self):
        r = self.run_bench(OracleClient(seeds=(0, 1, 2)), "logprob")
        self.assertEqual(r["pass"], {"passed": True, "reasons": []})
        self.assertEqual(r["modality"]["precision"]["wished"], 1.0)
        self.assertIn("recall on actual", r["arms"])
        self.assertIn("precision on wished", r["arms"])
        md = jb.render_markdown(r)
        self.assertIn("Precision, wished", md)
        self.assertIn(f">= {jb.PASS_ACTUAL_RECALL}", md)
        self.assertIn("letter mass", md.lower())


if __name__ == "__main__":
    unittest.main()
