"""A7: the off-record check (`in_transcript`) runs on the JUDGE model, the one it is calibrated on, in group J; the
reconstructor (Qwen) never sees it. Synthetic and real-labelled corpora, oracle clients with model ids."""
import json
import unittest

from second_scribe.pipeline import STAGE_GROUPS, STAGE_ORDER
from tests.test_real_run_validity import Base


class TestStageGroups(unittest.TestCase):
    def test_in_transcript_is_a_judge_group_stage(self):
        self.assertIn("in_transcript", STAGE_GROUPS["J"])
        self.assertNotIn("in_transcript", STAGE_GROUPS["Q"])
        self.assertLess(STAGE_ORDER.index("recon"), STAGE_ORDER.index("in_transcript"))


class TestOnTheJudge(Base):
    def calls(self):
        recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [r["payload"] for r in recs if r["record_type"] == "call_request"]

    def test_the_calls_go_to_the_judge_model_under_a_judge_stage(self):
        rc, err = self.run_all("all")
        self.assertEqual(rc, 0, err)
        it = [c for c in self.calls() if c["stage"].endswith("in_transcript")]
        self.assertTrue(it)
        self.assertEqual({c["stage"] for c in it}, {"judge:in_transcript"})
        self.assertEqual({c["model_id"] for c in it}, {"beta-judge-1"})
        self.assertFalse([c for c in self.calls() if c["stage"] == "q:in_transcript"])
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)         # the work-store checks know the new stage

    def test_group_q_alone_makes_no_off_record_calls(self):
        rc, err = self.run_all("Q")
        self.assertEqual(rc, 0, err)
        self.assertFalse([c for c in self.calls() if "in_transcript" in c["stage"]])
        self.assertFalse((self.work / "in_transcript").exists())

    def test_group_j_makes_them(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J")[0], 0)
        self.assertTrue((self.work / "in_transcript").exists())
        self.assertTrue([c for c in self.calls() if c["stage"] == "judge:in_transcript"])


if __name__ == "__main__":
    unittest.main()
