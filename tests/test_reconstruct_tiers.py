"""Run-2 validator (council round 8, items 1-2): only "unfound" quotes count toward max_unfound_share, the tier is
recorded per element, and an invalid node type is coerced to "other" (original kept, counted, never a retry)."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient
from second_scribe.reconstruct import ReconContext, ReconSpec, parse_recon, reconstruct, validate_recon
from second_scribe.schemas import MapGraph, Transcript, Turn

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
CTX = ReconContext(domain="getting a food-truck licence", role="food-truck owner",
                   phases=("Planning", "Permits", "Inspections", "Operating"))


def words(n, start=0, tag="a"):
    return [f"{tag}{start + i}w" for i in range(n)]


TXA, TXB, TXC = words(30, tag="p"), words(30, tag="q"), words(30, tag="r")
T = Transcript("SYN-OW01", [Turn(0, "interviewer", " ".join(words(8, tag="i"))),
                            Turn(1, "participant", " ".join(TXA)), Turn(2, "participant", " ".join(TXB)),
                            Turn(3, "interviewer", " ".join(words(8, tag="j"))), Turn(4, "participant", " ".join(TXC))],
               "synthetic")
OTHER = Transcript("SYN-OW02", [Turn(0, "interviewer", " ".join(words(8, tag="x"))),
                                Turn(1, "participant", " ".join(words(60, tag="y")))], "synthetic")


def near(ws, at=4):
    out = list(ws)
    out[at] = "zzz"
    return " ".join(out)


def node(i, quote, **kw):
    return {"id": f"n{i}", "label": f"Step {i}", "type": "service", "phase": "Permits", "status": "experienced",
            "quote": quote, **kw}


def reply(nodes, edges=()):
    return json.dumps({"nodes": list(nodes), "edges": list(edges), "emotion_word": None})


def check(nodes, edges=(), text=None, arm="full", **kw):
    return validate_recon(reply(nodes, edges), arm=arm, transcript_text=(text or T).text("full"), **kw)


class TestUnfoundShare(unittest.TestCase):
    def test_approximate_quotes_do_not_count_toward_the_unfound_share(self):
        nodes = [node(i, near(TXA, i)) for i in range(10)]            # ten approximate quotes
        self.assertIsNone(check(nodes))

    def test_unfound_counts_and_the_limit_is_half(self):
        # L16 (council round 10): max_unfound_share 0.50, no longer 0.10
        good = [node(i, " ".join(TXA[:12])) for i in range(5)]
        bad = [node(5 + i, f"completely invented words number {i} nowhere") for i in range(5)]
        self.assertIsNone(check(good + bad))                         # 5 of 10 = 0.50: tolerated
        err = check(good[:4] + bad + [node(10, "invented words again")])   # 6 of 11 > 0.50
        self.assertIn("not verbatim", err)
        self.assertIn("n5", err)
        self.assertNotIn("n0,", err)

    def test_default_is_the_same_everywhere(self):
        import inspect
        from second_scribe import audits, prereg
        self.assertEqual(inspect.signature(validate_recon).parameters["max_unfound_share"].default, 0.50)
        self.assertEqual(prereg.MAX_UNFOUND_SHARE, 0.50)
        self.assertEqual(audits.MAX_UNFOUND_SHARE, 0.50)

    def test_a_quote_joining_a_question_and_its_answer_is_unfound(self):
        q = " ".join(words(8, tag="i")[3:] + TXA[:14])                  # tail of the [I] turn + head of the [P] turn
        self.assertIn("not verbatim", check([node(1, q)]))

    def test_a_quote_from_another_transcript_is_unfound(self):
        q = " ".join(words(60, tag="y")[:25])
        self.assertIn("not verbatim", check([node(1, q)]))
        self.assertIsNone(check([node(1, q)], text=OTHER))

    def test_a_dropped_not_is_unfound(self):
        t = Transcript("t", [Turn(0, "participant", " ".join(TXA[:12] + ["not"] + TXA[12:]))], "synthetic")
        q = " ".join(TXA)                                              # the quote forgets the "not"
        self.assertIn("not verbatim", check([node(1, q)], text=t))
        self.assertIsNone(check([node(1, " ".join(TXA[:12]))], text=t))   # exact words, no gap: fine

    def test_old_exact_behaviour_is_unchanged_for_exact_quotes(self):
        nodes = [node(1, "  " + " ".join(TXA[:10]).upper() + "!! "), node(2, " ".join(TXB[:5]) + " ... " + " ".join(TXB[10:15]))]
        self.assertIsNone(check(nodes))
        # run-2 review B1: a "..." part in another turn is no longer exact
        self.assertIn("not verbatim", check([node(1, " ".join(TXB[:5]) + " ... " + " ".join(TXC[:5]))]))
        self.assertIn("not verbatim", check([node(1, "nowhere in sight")]))


class TestTiersRecorded(unittest.TestCase):
    def test_tier_per_node_and_edge_and_counts(self):
        nodes = [node(1, " ".join(TXA[:12])), node(2, near(TXB)), node(3, "this was never said by anyone here")]
        edges = [{"source": "n1", "target": "n2", "status": "experienced", "quote": near(TXC, 9)}]
        g = parse_recon(reply(nodes, edges), "m", "ai", spec_key="k", transcript_text=T.text("full"))
        self.assertEqual(g.meta["quote_tiers"], {"n1": "exact", "n2": "approximate", "n3": "unfound",
                                                 "e:n1->n2": "approximate"})
        self.assertEqual(g.meta["quote_tier_counts"], {"exact": 1, "approximate": 2, "unfound": 1})

    def test_tiers_survive_the_json_round_trip(self):
        g = parse_recon(reply([node(1, near(TXA))]), "m", "ai", transcript_text=T.text("full"))
        back = MapGraph.from_json(g.to_json())
        self.assertEqual(back.meta["quote_tiers"], {"n1": "approximate"})

    def test_element_classes_and_serialised_nodes_are_unchanged(self):
        g = parse_recon(reply([node(1, near(TXA))]), "m", "ai", transcript_text=T.text("full"))
        self.assertEqual(sorted(json.loads(g.to_json())["nodes"][0]),
                         sorted(["node_id", "phase", "quote", "status", "text", "node_type", "x", "y"]))

    def test_without_transcript_no_tier_is_claimed(self):
        g = parse_recon(reply([node(1, near(TXA))]), "m", "ai")
        self.assertNotIn("quote_tiers", g.meta)
        self.assertNotIn("quote_tier_counts", g.meta)


class TestTypeCoercion(unittest.TestCase):
    def test_invalid_type_is_accepted_not_an_error(self):
        for bad in ("Diagnosis", "bogus", "", "Service ", 5, None, ["service"], {"a": 1}):
            with self.subTest(bad=bad):
                self.assertIsNone(check([node(1, " ".join(TXA[:12]), type=bad)]))

    def test_missing_type_key_is_accepted_too(self):
        n = node(1, " ".join(TXA[:12]))
        del n["type"]
        self.assertIsNone(check([n]))

    def test_coerced_to_other_with_the_raw_value_kept_and_counted(self):
        nodes = [node(1, " ".join(TXA[:12]), type="Diagnosis"), node(2, " ".join(TXB[:12]), type="service"),
                 node(3, " ".join(TXC[:12]), type=7)]
        g = parse_recon(reply(nodes), "m", "ai")
        self.assertEqual([n.node_type for n in g.nodes], ["other", "service", "other"])
        self.assertEqual(g.meta["raw_types"], {"n1": "Diagnosis", "n3": "7"})
        self.assertEqual(g.meta["type_coercions"], 2)

    def test_valid_types_are_never_touched(self):
        from second_scribe.schemas import NODE_TYPES
        nodes = [node(i, " ".join(TXA[:12]), type=t) for i, t in enumerate(NODE_TYPES)]
        g = parse_recon(reply(nodes), "m", "ai")
        self.assertEqual([n.node_type for n in g.nodes], list(NODE_TYPES))
        self.assertNotIn("raw_types", g.meta)
        self.assertEqual(g.meta["type_coercions"], 0)

    def test_no_retry_on_type(self):
        log = AuditLog(Path(tempfile.mkdtemp()) / "r.jsonl", "r", clock=lambda: "t")
        calls = []

        def fn(messages):
            calls.append(1)
            return reply([node(1, " ".join(TXA[:12]), type="Diagnosis"), node(2, " ".join(TXB[:12]), type="Services")])
        g, st = reconstruct(log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="SYN-OW01")
        self.assertEqual((st, len(calls)), ("ok", 1))
        self.assertEqual([n.node_type for n in g.nodes], ["other", "other"])
        self.assertEqual(g.meta["raw_types"], {"n1": "Diagnosis", "n2": "Services"})
        self.assertEqual(g.meta["type_coercions"], 2)

    def test_status_and_phase_checks_are_unchanged(self):
        self.assertIn("status", check([node(1, " ".join(TXA[:12]), status="maybe")]))
        self.assertIn("phase", check([node(1, " ".join(TXA[:12]), phase="Retirement")], phases=CTX.phases))


class TestReconstructEndToEnd(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog(Path(tempfile.mkdtemp()) / "r.jsonl", "r", clock=lambda: "t")

    def _map_with_unfound(self, n_unfound, total=10):
        return reply([node(i, f"completely invented words number {i} nowhere" if i < n_unfound
                           else " ".join(TXA[:12])) for i in range(total)])

    def test_a_map_with_40_percent_unfound_is_accepted_at_attempt_1_without_a_retry(self):
        calls = []

        def fn(messages):
            calls.append(1)
            return self._map_with_unfound(4)
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW01")
        self.assertEqual((st, len(calls)), ("ok", 1))
        self.assertEqual(g.meta["quote_tier_counts"], {"exact": 6, "approximate": 0, "unfound": 4})

    def test_a_map_with_60_percent_unfound_is_retried(self):
        calls = []

        def fn(messages):
            calls.append(1)
            return self._map_with_unfound(6)
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW01")
        self.assertEqual((g, st, len(calls)), (None, "failed", 3))

    def test_a_retry_that_fixes_the_quotes_is_accepted_at_attempt_2(self):
        replies = [self._map_with_unfound(6), self._map_with_unfound(1)]
        calls = []

        def fn(messages):
            calls.append(1)
            return replies[len(calls) - 1]
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW01")
        self.assertEqual((st, len(calls)), ("ok", 2))

    def test_other_validation_errors_still_retry_even_with_no_unfound_quotes(self):
        calls = []

        def fn(messages):
            calls.append(1)
            return "not json at all"
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW01")
        self.assertEqual((g, st, len(calls)), (None, "failed", 3))

    def test_a_bad_edge_still_retries_even_when_quotes_are_fine(self):
        calls = []

        def fn(messages):
            calls.append(1)
            return reply([node(1, " ".join(TXA[:12])), node(2, " ".join(TXB[:12]))],
                         [{"source": "n1", "target": "n9", "status": "experienced", "quote": " ".join(TXC[:12])}])
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW01")
        self.assertEqual((g, st, len(calls)), (None, "failed", 3))

    def test_approximate_quotes_make_an_ok_map_with_tiers_in_it(self):
        g, st = reconstruct(self.log, MockClient(fn=lambda m: reply([node(1, near(TXA)), node(2, " ".join(TXB[:12]))])),
                            T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="SYN-OW01")
        self.assertEqual(st, "ok")
        self.assertEqual(g.meta["quote_tiers"], {"n1": "approximate", "n2": "exact"})
        self.assertEqual(g.meta["quote_tier_counts"], {"exact": 1, "approximate": 1, "unfound": 0})

    def test_a_map_scored_against_the_wrong_transcript_fails_after_three_attempts(self):
        calls = []

        def fn(messages):
            calls.append(1)
            return reply([node(1, " ".join(TXA[:20])), node(2, " ".join(TXB[:20]))])
        g, st = reconstruct(self.log, MockClient(fn=fn), OTHER, ReconSpec("full", "p0", 0.0), CTX, PROMPTS,
                            item_id="SYN-OW02")
        self.assertEqual((g, st, len(calls)), (None, "failed", 3))


if __name__ == "__main__":
    unittest.main()
