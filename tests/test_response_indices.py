"""Malformed answer indices cannot silently select a different transcript/map item."""
import json
import unittest
from pathlib import Path

from second_scribe.judge import _parse_labels, judge_modality_detailed
from second_scribe.llm import MockClient
from second_scribe.transcripts import _parse_answers, confirm_mapping_talk, flag_mapping_talk, parse_synthetic


class MemoryLog:
    def __init__(self):
        self.records = []

    def append(self, record_type, payload):
        record = {"record_type": record_type, "payload": payload}
        self.records.append(record)
        return record


class TestResponseIndices(unittest.TestCase):
    def test_mapping_talk_rejects_non_integer_indices(self):
        for n in (True, False, 1.7, 1.0, "1", None, [], {}):
            with self.subTest(n=n):
                text = json.dumps({"answers": [{"n": n, "map_talk": "yes"}]})
                self.assertIsInstance(_parse_answers(text, 1), str)

    def test_mapping_talk_rejects_duplicate_and_extra_answers(self):
        for n in (1, 0, 2, -1):
            with self.subTest(n=n):
                text = json.dumps({"answers": [{"n": 1, "map_talk": "yes"},
                                               {"n": n, "map_talk": "no"}]})
                self.assertIsInstance(_parse_answers(text, 1), str)

    def test_modality_rejects_non_integer_indices(self):
        for n in (True, False, 1.7, 1.0, "1", None, [], {}):
            with self.subTest(n=n):
                text = json.dumps({"labels": [{"n": n, "modality": "actual"}]})
                self.assertIsInstance(_parse_labels(text, 1), str)

    def test_complete_integer_answers_remain_accepted_in_any_order(self):
        self.assertEqual(_parse_answers(json.dumps({"answers": [
            {"n": 2, "map_talk": "no"}, {"n": 1, "map_talk": "yes"}]}), 2),
            {2: False, 1: True})
        self.assertEqual(_parse_labels(json.dumps({"labels": [
            {"n": 2, "modality": "actual"}, {"n": 1, "modality": "actual"}]}), 2),
            {2: "actual", 1: "actual"})

    def test_invalid_mapping_answers_keep_rule_flags_and_count_unscreened(self):
        transcript = flag_mapping_talk(parse_synthetic(
            "[I] I'll add a box here.\n[P] We waited.", "SYN-INDEX"))
        client = MockClient(fn=lambda messages: json.dumps({"answers": [
            {"n": 1, "map_talk": "yes"}, {"n": 1, "map_talk": "no"}]}))
        log = MemoryLog()
        result, stats = confirm_mapping_talk(transcript, log, client, item_id="SYN-INDEX")
        self.assertTrue(result.turns[0].mapping_talk)
        self.assertEqual(stats["unscreened_turns"], 1)
        self.assertEqual(stats["n_screened"], 0)
        self.assertEqual(sum(r["record_type"] == "call_response" for r in log.records), 3)

    def test_invalid_modality_answer_is_not_counted_as_judged(self):
        client = MockClient(fn=lambda messages: json.dumps({"labels": [
            {"n": 1.7, "modality": "actual"}]}))
        log = MemoryLog()
        _, not_judged = judge_modality_detailed(log, client, [
            {"element_id": "n1", "element": "permit", "quote": "We received a permit."}],
            Path(__file__).resolve().parent.parent / "prompts", item_id="SYN-INDEX")
        self.assertEqual(not_judged, {"n1"})
        self.assertTrue(any(r["record_type"] == "judge_unparsed" for r in log.records))
