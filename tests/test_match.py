import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient
from second_scribe.match import (PRIMARY_SETTING, SETTINGS_GRID, MatchSetting, align_labels, canonicalize,
                                 cluster_labels, edge_recovered, in_transcript, lexical_sim, match_nodes)
from second_scribe.schemas import Edge, MapGraph, Node, Transcript, Turn

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

A = MapGraph("x", "ai", [Node("a1", "City business licence"), Node("a2", "GP visit"), Node("a3", "Fire inspection")],
             [Edge("a1", "a3")])
R = MapGraph("x", "endorsed", [Node("r1", "Business licences"), Node("r2", "Family doctor"), Node("r3", "Commissary kitchen")],
             [Edge("r1", "r2"), Edge("r2", "r1")])


class TestMatch(unittest.TestCase):
    def test_lexical(self):
        self.assertAlmostEqual(lexical_sim("City business licence", "Business licences"), 2 / 3)
        self.assertEqual(lexical_sim("", "x"), 0.0)
        self.assertEqual(lexical_sim("the of", "the"), 0.0)   # only stopwords: no content tokens

    def test_settings_change_result(self):
        judged = {(1, 1): "same", (2, 2): "close"}
        strict = match_nodes(A, R, MatchSetting(0.6, "same"), judged)
        lenient = match_nodes(A, R, MatchSetting(0.6, "same_or_close"), judged)
        lex_only = match_nodes(A, R, MatchSetting(0.6, "none"), judged)
        self.assertEqual(strict.right_matched, {"r1", "r2"})
        self.assertEqual(lenient.right_matched, {"r1", "r2", "r3"})
        self.assertEqual(lex_only.right_matched, {"r1"})
        self.assertEqual(strict.pairs, {("a1", "r1"), ("a2", "r2")})
        self.assertEqual(strict.left_matched, {"a1", "a2"})

    def test_lex_threshold_gates_pairs(self):
        self.assertEqual(match_nodes(A, R, MatchSetting(0.7, "none"), None).pairs, set())
        self.assertEqual(match_nodes(A, R, MatchSetting(0.5, "none"), None).pairs, {("a1", "r1")})

    def test_edge_direction(self):
        m = match_nodes(A, R, MatchSetting(0.6, "same_or_close"), {(1, 1): "same", (2, 2): "close"})
        self.assertFalse(edge_recovered(R.edges[0], A, m))   # r1->r2 needs a1->a2; AI has a1->a3

    def test_edge_recovered_and_reversed(self):
        ai = MapGraph("x", "ai", [Node("a1", "x"), Node("a2", "y")], [Edge("a1", "a2")])
        ref = MapGraph("x", "endorsed", [Node("r1", "x"), Node("r2", "y")], [Edge("r1", "r2"), Edge("r2", "r1")])
        m = match_nodes(ai, ref, MatchSetting(0.6, "none"), None)
        self.assertTrue(edge_recovered(ref.edges[0], ai, m))
        self.assertFalse(edge_recovered(ref.edges[1], ai, m))   # direction respected

    def test_set_based_many_to_one(self):
        ai = MapGraph("x", "ai", [Node("a1", "doctor visit"), Node("a2", "doctor appointment")])
        ref = MapGraph("x", "endorsed", [Node("r1", "family doctor")])
        m = match_nodes(ai, ref, MatchSetting(0.3, "none"), None)
        self.assertEqual(m.pairs, {("a1", "r1"), ("a2", "r1")})   # not one-to-one

    def test_grid(self):
        self.assertEqual([(s.lex_threshold, s.judge_accept) for s in SETTINGS_GRID],
                         [(0.4, "same"), (0.4, "same_or_close"), (0.5, "same"), (0.5, "same_or_close"),
                          (0.6, "same"), (0.6, "same_or_close"), (0.7, "same"), (0.7, "same_or_close")])
        self.assertEqual([s.key() for s in SETTINGS_GRID],
                         ["lex0.4_same", "lex0.4_same_or_close", "lex0.5_same", "lex0.5_same_or_close",
                          "lex0.6_same", "lex0.6_same_or_close", "lex0.7_same", "lex0.7_same_or_close"])
        self.assertEqual(PRIMARY_SETTING, MatchSetting(0.6, "same"))
        self.assertEqual(PRIMARY_SETTING.key(), "lex0.6_same")
        with self.assertRaises(ValueError):
            MatchSetting(0.6, "strict")

    def test_cluster_deterministic(self):
        labs = ["waiting list", "wait lists", "fire inspection", "inspection by fire dept"]
        self.assertEqual(cluster_labels(labs), cluster_labels(labs))
        self.assertEqual(cluster_labels(labs)[0], cluster_labels(labs)[1])
        self.assertEqual(cluster_labels(labs), [0, 0, 1, 1])
        self.assertEqual(cluster_labels(["", ""]), [0, 1])   # no content tokens: never clustered


class LLMBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def fallbacks(self):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r["payload"] for r in recs if r["record_type"] == "judge_fallback"]


class TestAlign(LLMBase):
    def test_chunks_and_global_indexes(self):
        left = [f"left {i}" for i in range(130)]
        right = ["r one", "r two"]
        calls = []

        def fn(msgs):
            p = msgs[-1]["content"]
            calls.append(p)
            # every chunk proposes: its first A item -> B 2 "same"; plus A item 1 -> B 1 "close" in chunk 1
            first = int(p.split("LIST A:\n")[1].split(".")[0])
            pairs = [{"a": first, "b": 2, "rel": "same"}]
            if first == 1:
                pairs.append({"a": 1, "b": 1, "rel": "close"})
            return json.dumps({"pairs": pairs})
        out = align_labels(self.log, MockClient(fn=fn), left, right, PROMPTS, item_id="m")
        self.assertEqual(len(calls), 3)                      # 60 + 60 + 10
        self.assertTrue(all("r one" in c and "r two" in c for c in calls))
        self.assertEqual(out, {(0, 1): "same", (0, 0): "close", (60, 1): "same", (120, 1): "same"})

    def test_prompt_placeholders_literal(self):
        seen = []
        align_labels(self.log, MockClient(fn=lambda m: seen.append(m[-1]["content"]) or '{"pairs": []}'),
                     ["{right} tricky"], ["{left}"], PROMPTS, item_id="m")
        self.assertIn("1. {right} tricky", seen[0])
        self.assertIn("1. {left}", seen[0])

    def test_invalid_pairs_retry_then_nothing(self):
        out = align_labels(self.log, MockClient(fn=lambda m: '{"pairs": [{"a": 9, "b": 1, "rel": "same"}]}'),
                           ["x"], ["y"], PROMPTS, item_id="m")
        self.assertEqual(out, {})   # out-of-range number never accepted; failure leaves lexical floor only

    def run_bad(self, reply, left, right):
        """Reply is rejected by validation on every attempt: no pairs, one fallback record."""
        out = align_labels(self.log, MockClient(fn=lambda m: json.dumps(reply)), left, right, PROMPTS, item_id="m")
        self.assertEqual(out, {})
        self.assertEqual(len(self.fallbacks()), 1)

    def test_a_outside_its_chunk_rejected(self):
        # second chunk covers A numbers 61-70; a=1 belongs to the first chunk
        calls = []

        def fn(m):
            calls.append(1)
            return '{"pairs": []}' if len(calls) == 1 else '{"pairs": [{"a": 1, "b": 1, "rel": "same"}]}'
        out = align_labels(self.log, MockClient(fn=fn), [f"l{i}" for i in range(70)], ["y"], PROMPTS, item_id="m")
        self.assertEqual(out, {})
        self.assertEqual(self.fallbacks(), [{"stage": "judge:align", "item_id": "m:1", "n_items": 10}])

    def test_b_out_of_range_rejected(self):
        self.run_bad({"pairs": [{"a": 1, "b": 2, "rel": "same"}]}, ["x"], ["y"])
        self.setUp()
        self.run_bad({"pairs": [{"a": 1, "b": 0, "rel": "same"}]}, ["x"], ["y"])

    def test_duplicate_pair_merges(self):
        reply = {"pairs": [{"a": 1, "b": 1, "rel": "same"}, {"a": 1, "b": 1, "rel": "same"},
                           {"a": 1, "b": 2, "rel": "close"}]}
        out = align_labels(self.log, MockClient(fn=lambda m: json.dumps(reply)), ["x"], ["y", "z"], PROMPTS, item_id="m")
        self.assertEqual(out, {(0, 0): "same", (0, 1): "close"})
        self.assertEqual(self.fallbacks(), [])

    def test_strict_integers(self):
        for bad in (1.7, True, "1"):
            self.setUp()
            self.run_bad({"pairs": [{"a": bad, "b": 1, "rel": "same"}]}, ["x"], ["y"])
            self.setUp()
            self.run_bad({"pairs": [{"a": 1, "b": bad, "rel": "same"}]}, ["x"], ["y"])

    def test_fallback_record_per_failed_chunk(self):
        calls = []

        def fn(m):
            calls.append(1)
            return '{"pairs": []}' if len(calls) == 1 else "garbage"   # chunk 0 fine; chunk 1 fails all 3 attempts
        align_labels(self.log, MockClient(fn=fn), [f"l{i}" for i in range(70)], ["y"], PROMPTS, item_id="m")
        self.assertEqual(self.fallbacks(), [{"stage": "judge:align", "item_id": "m:1", "n_items": 10}])

    def test_same_beats_close_and_empty(self):
        reply = '{"pairs": [{"a": 1, "b": 1, "rel": "close"}, {"a": 1, "b": 1, "rel": "same"}]}'
        out = align_labels(self.log, MockClient(fn=lambda m: reply), ["x"], ["y"], PROMPTS, item_id="m")
        self.assertEqual(out, {(0, 0): "same"})
        self.assertEqual(align_labels(self.log, MockClient(fn=lambda m: 1 / 0), [], ["y"], PROMPTS, item_id="m"), {})


class TestInTranscript(LLMBase):
    T = Transcript("t", [Turn(0, "interviewer", "Tell me about the clinic."),
                         Turn(1, "participant", "I waited months on the waiting list for the clinic."),
                         Turn(2, "participant", "My cousin helped with the paperwork at city hall.")], "synthetic")

    def run_it(self, reply, labels):
        return in_transcript(self.log, MockClient(fn=lambda m: json.dumps(reply)), labels, self.T, PROMPTS, item_id="t")

    def test_verbatim_quote_found_any_speaker(self):
        labels = ["Waiting list", "Tell me about it", "Taxi"]
        out = self.run_it({"answers": [{"n": 1, "quote": "waited months on the waiting list"},
                                       {"n": 2, "quote": "Tell me about the clinic"},
                                       {"n": 3, "quote": None}]}, labels)
        self.assertEqual(out, {0: True, 1: True, 2: False})

    def test_paraphrased_quote_falls_back_to_lexical(self):
        # quote is not verbatim; label "paperwork help city hall" has 4 content tokens, 3 of them in one turn
        out = self.run_it({"answers": [{"n": 1, "quote": "a relative assisted with forms"},
                                       {"n": 2, "quote": "something made up"}]},
                          ["paperwork help city hall", "dental surgery"])
        self.assertEqual(out, {0: True, 1: False})

    def test_lexical_needs_single_turn(self):
        # "waiting" is in turn 1, "paperwork" only in turn 2: neither turn has 60% of the tokens
        out = self.run_it({"answers": [{"n": 1, "quote": None}]}, ["waiting paperwork"])
        self.assertEqual(out, {0: False})

    def test_model_failure_leaves_lexical_only(self):
        out = in_transcript(self.log, MockClient(fn=lambda m: "garbage"), ["Waiting list", "Taxi"], self.T, PROMPTS,
                            item_id="t")
        self.assertEqual(out, {0: True, 1: False})

    def test_one_word_quote_is_not_enough(self):
        t = Transcript("t", [Turn(0, "participant", "We saw a doctor about the long paperwork backlog.")], "synthetic")
        # label has 3 content tokens (wait, family, doctor): "doctor" is 1 word, covers 1/3 < 50%,
        # and the lexical rule (1/3 < 60%) fails too
        out = in_transcript(self.log, MockClient(fn=lambda m: json.dumps({"answers": [{"n": 1, "quote": "doctor"}]})),
                            ["Waiting for the family doctor"], t, PROMPTS, item_id="t")
        self.assertEqual(out, {0: False})

    def test_short_quote_covering_half_the_label_counts(self):
        t = Transcript("t", [Turn(0, "participant", "We saw a doctor about the long paperwork backlog.")], "synthetic")
        out = in_transcript(self.log, MockClient(fn=lambda m: json.dumps({"answers": [{"n": 1, "quote": "doctor"}]})),
                            ["family doctor"], t, PROMPTS, item_id="t")   # "doctor" = 1 of 2 content tokens
        self.assertEqual(out, {0: True})

    def test_long_quote_in_a_turn_counts_without_label_overlap(self):
        # 6-word verbatim quote (3 content tokens: cousin, help, paperwork), label shares no token with the
        # turn: only the quote path can say yes
        out = self.run_it({"answers": [{"n": 1, "quote": "my cousin helped with the paperwork"}]},
                          ["kinship support"])
        self.assertEqual(out, {0: True})

    def test_trivial_quotes_do_not_count(self):
        # "I was in the" appears in a turn but has no content tokens; the ellipsis quote has 4 words
        # and 3 content tokens overall, but every piece is a single word
        t = Transcript("t", [Turn(0, "participant", "I was in the room. We waited for the clinic about the paperwork.")],
                       "synthetic")
        for quote in ("I was in the", "the ... for ... clinic ... paperwork"):
            out = in_transcript(self.log, MockClient(fn=lambda m: json.dumps({"answers": [{"n": 1, "quote": quote}]})),
                                ["kinship support"], t, PROMPTS, item_id="t")
            self.assertEqual(out, {0: False}, quote)

    def test_ellipsis_quote_with_long_pieces_counts(self):
        t = Transcript("t", [Turn(0, "participant", "We waited for the clinic about the paperwork.")], "synthetic")
        out = in_transcript(self.log, MockClient(fn=lambda m: json.dumps(
            {"answers": [{"n": 1, "quote": "we waited ... the paperwork"}]})), ["kinship support"], t, PROMPTS, item_id="t")
        self.assertEqual(out, {0: False})   # content tokens: waited->wait, paperwork = 2 < 3
        out = in_transcript(self.log, MockClient(fn=lambda m: json.dumps(
            {"answers": [{"n": 1, "quote": "we waited for the clinic ... the paperwork"}]})),
            ["kinship support"], t, PROMPTS, item_id="t")
        self.assertEqual(out, {0: True})

    def test_label_numbers_validated(self):
        labels = [f"label{i} x" for i in range(60)]
        good = [{"n": k, "quote": None} for k in range(1, 61)]
        for bad in (good + [{"n": 70, "quote": None}], good + [{"n": 5, "quote": None}],
                    good[:-1] + [{"n": 1.7, "quote": None}], good[:-1] + [{"n": True, "quote": None}],
                    good[:-1] + [{"n": "60", "quote": None}], good[:-1] + [{"n": 0, "quote": None}]):
            self.setUp()
            in_transcript(self.log, MockClient(fn=lambda m: json.dumps({"answers": bad})), labels, self.T, PROMPTS,
                          item_id="t")
            self.assertEqual(self.fallbacks(), [{"stage": "in_transcript", "item_id": "t:0", "n_items": 60}], bad[-1])
        self.setUp()
        in_transcript(self.log, MockClient(fn=lambda m: json.dumps({"answers": good})), labels, self.T, PROMPTS,
                      item_id="t")
        self.assertEqual(self.fallbacks(), [])

    def test_quote_must_sit_in_one_turn(self):
        # words of the quote are spread over two turns: contains_quote fails on each turn
        out = self.run_it({"answers": [{"n": 1, "quote": "the clinic. I waited months on the"}]}, ["kinship support"])
        self.assertEqual(out, {0: False})

    def test_fallback_record_on_model_failure(self):
        in_transcript(self.log, MockClient(fn=lambda m: "garbage"), ["a", "b"], self.T, PROMPTS, item_id="t")
        self.assertEqual(self.fallbacks(), [{"stage": "in_transcript", "item_id": "t:0", "n_items": 2}])

    def test_no_fallback_record_on_success(self):
        self.run_it({"answers": [{"n": 1, "quote": None}]}, ["x"])
        self.assertEqual(self.fallbacks(), [])

    def test_empty_labels(self):
        self.assertEqual(in_transcript(self.log, MockClient(fn=lambda m: 1 / 0), [], self.T, PROMPTS, item_id="t"), {})


class TestCanonicalize(LLMBase):
    def test_batches_and_order(self):
        labels = [f"label {i}" for i in range(7)]

        def fn(msgs):
            p = msgs[-1]["content"]
            nums = [int(l.split(".")[0]) for l in p.split("LABELS:\n")[1].split("\nREMINDER:")[0].splitlines() if l.strip()]
            return json.dumps({"concepts": [{"n": n, "concept": f"c{n}"} for n in nums]})
        out = canonicalize(self.log, MockClient(fn=fn), labels, PROMPTS, item_id="c", batch_size=3)
        self.assertEqual(out, ["c1", "c2", "c3", "c1", "c2", "c3", "c1"])   # numbering restarts per batch

    def test_label_numbers_validated(self):
        labels = [f"label {i}" for i in range(60)]
        good = [{"n": k, "concept": "c"} for k in range(1, 51)]
        for bad in (good + [{"n": 70, "concept": "c"}], good + [{"n": 5, "concept": "c"}],
                    good[:-1] + [{"n": 1.7, "concept": "c"}], good[:-1] + [{"n": True, "concept": "c"}],
                    good[:-1] + [{"n": "50", "concept": "c"}]):
            self.setUp()
            out = canonicalize(self.log, MockClient(fn=lambda m: json.dumps({"concepts": bad})), labels[:50], PROMPTS,
                               item_id="c")
            self.assertEqual(out, labels[:50], bad[-1])   # rejected -> originals kept
            self.assertEqual(self.fallbacks(), [{"stage": "canonicalize", "item_id": "c:0", "n_items": 50}])

    def test_failed_batch_keeps_original(self):
        out = canonicalize(self.log, MockClient(fn=lambda m: "nope"), ["  Family   doctor "], PROMPTS, item_id="c")
        self.assertEqual(out, ["Family doctor"])
        self.assertEqual(self.fallbacks(), [{"stage": "canonicalize", "item_id": "c:0", "n_items": 1}])


if __name__ == "__main__":
    unittest.main()
