"""A3: the mapping-talk confirmation stage screens EVERY interviewer turn with the model (not only the regex-flagged
ones) and reports rule-only vs model counts and their agreement. Synthetic corpus, oracle clients with model ids."""
import json
import unittest

from second_scribe.prereg import _no_call_by_construction
from tests.test_real_run_validity import Base


class TestStage(Base):
    def calls(self):
        recs = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [r["payload"] for r in recs if r["record_type"] == "call_request"]

    def items(self):
        return [json.loads(f.read_text()) for f in sorted((self.work / "mapping_talk_confirm").glob("*.json"))]

    def test_every_interviewer_turn_is_sent_and_stats_report_both_counts(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        its = [d for d in self.items() if d.get("speaker_format") != "none"]
        self.assertTrue(its)
        for d in its:
            st = d["stats"]
            self.assertGreater(st["n_interviewer_turns"], st["rule_flagged"], d)     # not only the flagged ones
            self.assertEqual(st["n_screened"], st["n_interviewer_turns"])
            self.assertEqual(st["unscreened_turns"], 0)
            self.assertEqual(st["rule_only"] + st["model_only"], st["disagreements"])
            self.assertTrue(d["request_ids"], "an item that screened turns names its calls")
        sent = 0
        for c in self.calls():
            if c["stage"] == "q:mapping_talk":
                sent += 1
        self.assertGreaterEqual(sent, len(its))

    def test_aggregates_report_rule_only_vs_model_and_agreement(self):
        self.assertEqual(self.run_all("all")[0], 0)
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        mt = agg["mapping_talk"]
        for k in ("rule_flagged", "model_flagged", "model_confirmed", "rule_only", "model_only", "disagreements",
                  "n_interviewer_turns", "n_screened", "unscreened_turns", "agreement", "files_without_speakers"):
            self.assertIn(k, mt)
        self.assertGreater(mt["n_interviewer_turns"], 0)
        self.assertEqual(mt["n_screened"], mt["n_interviewer_turns"])
        self.assertTrue(0.0 <= mt["agreement"] <= 1.0)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)

    def test_a_participant_with_interviewer_turns_but_no_rule_hit_still_makes_a_call(self):
        # screening every turn means "rule_flagged == 0" no longer implies "no call by construction"
        self.assertFalse(_no_call_by_construction(
            "mapping_talk_confirm", {"status": "ok", "speaker_format": "pt_iv",
                                     "stats": {"rule_flagged": 0, "n_interviewer_turns": 4}}))
        self.assertTrue(_no_call_by_construction(
            "mapping_talk_confirm", {"status": "ok", "speaker_format": "pt_iv",
                                     "stats": {"rule_flagged": 0, "n_interviewer_turns": 0}}))
        self.assertTrue(_no_call_by_construction(                       # an earlier item (rule-flagged-only stats)
            "mapping_talk_confirm", {"status": "ok", "speaker_format": "pt_iv", "stats": {"rule_flagged": 0}}))


if __name__ == "__main__":
    unittest.main()
