"""Run-2 call-level fix 3 (review 3.2): the ladder runs condition-major (rung 2, then rung 3 and the placebo draw by
draw, then rung 1; DECISIONS C6 puts rung 1 last so that a cut removes it first). Interleaving per transcript would
move rung 1's declaration and its position in the hash chain, so the order is NOT changed; the run says so in its
run_start record and in the report. Fake clients only."""
import json
import unittest

from second_scribe import pipeline, report

from tests.test_pipeline_ladder_q import ALL, Base, records


class TestOrderIsRecorded(Base):
    def test_run_start_says_condition_major_when_a_ladder_is_configured(self):
        self.go(self.cfg())
        starts = [r["payload"] for r in records(self.log_path) if r["record_type"] == "run_start"]
        self.assertEqual([s["ladder_order"] for s in starts], ["condition-major"])

    def test_run_start_without_a_ladder_has_none(self):
        self.go(self.cfg(ladder_cfg={}))
        starts = [r["payload"] for r in records(self.log_path) if r["record_type"] == "run_start"]
        self.assertEqual([s["ladder_order"] for s in starts], [None])

    def test_the_order_itself_is_unchanged(self):
        self.go(self.cfg())
        self.assertEqual(self.call_sequence(),
                         [("rung2", 1), ("rung3", 0), ("placebo", 0), ("rung3", 1), ("placebo", 1),
                          ("rung1", 0), ("rung1", 1)])


class TestReportLine(unittest.TestCase):
    LAD = {"rungs": ["rung1"], "draws": 1, "n_perm": 1, "B": 1, "seed": 1, "arm_of_rung": {"rung1": "x"},
           "complete": True, "per_rung": {"rung1": {"expected": 1, "retries": 0, "transport_errors": 0,
                                                    "by_status": {"ok": 1}}},
           "copy_audit": {}, "null_phase": {}, "draw_identity": {}}

    def test_ladder_section_has_the_order_line(self):
        text = report.ladder_markdown({**self.LAD, "call_order": "condition-major"}, synthetic=True)
        self.assertIn("- order of the calls: condition-major", text)

    def test_an_aggregate_without_the_field_does_not_crash(self):
        self.assertNotIn("order of the calls", report.ladder_markdown(self.LAD, synthetic=True))


if __name__ == "__main__":
    unittest.main()
