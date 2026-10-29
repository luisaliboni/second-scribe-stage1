"""Fixtures keep EVERY response for a request, in call order (the live-recording replay bug).

A fixtures file maps ``messages_sha256`` to a record. With live models the same messages are sent more than once with
different seeds (the prior-only samples, T=0.7) and again by the determinism audits; a last-write-wins store served the
last answer for every call, so a replay diverged from the recording. A key called once keeps the legacy form (a record
dict); from the second call on it holds ``{"calls": [rec1, rec2, ...]}`` and ``ReplayClient`` serves ``calls[i]`` on the
i-th call. Legacy values (a str or a record dict) are served on every call, unchanged. Offline, temp dirs only."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.llm import FixtureMissing, ModelResponse, RecordingClient, ReplayClient, messages_sha256

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "same request"}]
KEY = messages_sha256(MSGS)


class Scripted:
    """An inner client that answers from a list, in order, and may raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seeds = []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.seeds.append(seed)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return ModelResponse(a, "stop", {"n": len(self.seeds)}, 5, hashlib.sha256(a.encode()).hexdigest())


def call(client, msgs=MSGS, seed=0):
    return client.complete(msgs, temperature=0.7, seed=seed, max_tokens=50)


def rec(text, n=1):
    return {"text": text, "finish_reason": "stop", "usage": {"n": n}}


def recorded(text, n=1):
    """What a ``RecordingClient`` without a role stores: the record plus the request's temperature (no role tag)."""
    return {**rec(text, n), "temperature": 0.7}


def tmp():
    return Path(tempfile.mkdtemp(prefix="ss-tests-")) / "fx.json"


class TestRecordingKeepsEveryCall(unittest.TestCase):
    def test_a_single_call_stays_in_the_legacy_record_form(self):
        p = tmp()
        rc = RecordingClient(Scripted("only"), str(p))
        call(rc)
        rc.save()
        self.assertEqual(json.loads(p.read_text())[KEY], recorded("only"))

    def test_a_second_call_with_the_same_messages_makes_a_calls_list(self):
        p = tmp()
        rc = RecordingClient(Scripted("r0", "r1"), str(p))
        call(rc, seed=0)
        call(rc, seed=1)
        rc.save()
        self.assertEqual(json.loads(p.read_text())[KEY], {"calls": [recorded("r0", 1), recorded("r1", 2)]})

    def test_later_calls_append(self):
        p = tmp()
        rc = RecordingClient(Scripted("a", "b", "c"), str(p))
        for s in range(3):
            call(rc, seed=s)
        self.assertEqual([c["text"] for c in rc.fixtures[KEY]["calls"]], ["a", "b", "c"])

    def test_other_requests_are_unaffected(self):
        other = [{"role": "user", "content": "different"}]
        rc = RecordingClient(Scripted("a", "x", "b"), str(tmp()))
        call(rc)
        call(rc, other)
        call(rc)
        self.assertEqual(rc.fixtures[messages_sha256(other)], recorded("x", 2))
        self.assertEqual(len(rc.fixtures[KEY]["calls"]), 2)

    def test_a_call_that_raises_records_nothing(self):
        rc = RecordingClient(Scripted("a", RuntimeError("down"), "b"), str(tmp()))
        call(rc)
        with self.assertRaises(RuntimeError):
            call(rc)
        call(rc)
        self.assertEqual([c["text"] for c in rc.fixtures[KEY]["calls"]], ["a", "b"])

    def test_clients_sharing_a_store_append_in_call_order(self):
        store: dict = {}
        p = tmp()
        a = RecordingClient(Scripted("from a 1", "from a 2"), str(p), store=store)
        b = RecordingClient(Scripted("from b"), str(p), store=store)
        call(a)
        call(b)
        call(a)
        self.assertEqual([c["text"] for c in store[KEY]["calls"]], ["from a 1", "from b", "from a 2"])

    def test_save_is_deterministic_with_sorted_keys(self):
        outs = []
        for _ in range(2):
            p = tmp()
            rc = RecordingClient(Scripted("r0", "r1", "z"), str(p))
            call(rc, seed=0)
            call(rc, seed=1)
            call(rc, [{"role": "user", "content": "zzz"}])
            rc.save()
            outs.append(p.read_bytes())
        self.assertEqual(outs[0], outs[1])
        keys = list(json.loads(outs[0]))
        self.assertEqual(keys, sorted(keys))

    def test_a_resumed_session_appends_after_what_the_file_holds(self):
        p = tmp()
        first = RecordingClient(Scripted("s1 a", "s1 b"), str(p))
        call(first)
        first.save()                                    # the file holds one legacy record for KEY
        second = RecordingClient(Scripted("s2"), str(p), merge=True)
        call(second)
        second.save()
        self.assertEqual([c["text"] for c in json.loads(p.read_text())[KEY]["calls"]], ["s1 a", "s2"])
        third = RecordingClient(Scripted("s3"), str(p), merge=True)     # the file now holds a calls list
        call(third)
        third.save()
        self.assertEqual([c["text"] for c in json.loads(p.read_text())[KEY]["calls"]], ["s1 a", "s2", "s3"])

    def test_merge_false_starts_from_an_empty_store(self):
        p = tmp()
        old = RecordingClient(Scripted("old"), str(p))
        call(old)
        old.save()
        fresh = RecordingClient(Scripted("new"), str(p), merge=False)
        call(fresh)
        self.assertEqual(fresh.fixtures[KEY], recorded("new"))

    def test_a_legacy_bare_text_value_in_the_file_becomes_the_first_call(self):
        p = tmp()
        p.write_text(json.dumps({KEY: "bare"}))
        rc = RecordingClient(Scripted("next"), str(p), merge=True)
        call(rc)
        self.assertEqual(rc.fixtures[KEY], {"calls": ["bare", recorded("next")]})
        rc.save()
        self.assertEqual(call(ReplayClient(str(p))).text, "bare")

    def test_the_meta_entry_a_session_stores_is_left_alone(self):
        p = tmp()
        p.write_text(json.dumps({"_meta": {"models": {"recon": "m"}}}))
        rc = RecordingClient(Scripted("a"), str(p), merge=True)
        call(rc)
        self.assertEqual(rc.fixtures["_meta"], {"models": {"recon": "m"}})


class TestReplayServesInOrder(unittest.TestCase):
    def record(self, *answers, p=None):
        p = p or tmp()
        rc = RecordingClient(Scripted(*answers), str(p))
        for s in range(len(answers)):
            call(rc, seed=s)
        rc.save()
        return p

    def test_same_messages_different_seeds_replay_in_order(self):
        # the prior-only samples: identical messages, replicates r0 and r1 with different seeds
        p = self.record("map r0", "map r1")
        rp = ReplayClient(str(p))
        self.assertEqual(call(rp, seed=0).text, "map r0")
        self.assertEqual(call(rp, seed=1).text, "map r1")

    def test_a_determinism_re_ask_with_a_different_answer_keeps_the_first_first(self):
        # the pipeline consumed "consumed"; the audit re-sent the logged request and the live model said otherwise
        p = self.record("consumed", "audit re-ask differs")
        rp = ReplayClient(str(p))
        self.assertEqual(call(rp).text, "consumed")
        self.assertEqual(call(rp).text, "audit re-ask differs")

    def test_the_whole_record_comes_back_for_each_call(self):
        p = self.record("a", "b")
        rp = ReplayClient(str(p))
        r0, r1 = call(rp), call(rp)
        self.assertEqual((r0.text, r0.finish_reason, r0.usage), ("a", "stop", {"n": 1}))
        self.assertEqual((r1.text, r1.finish_reason, r1.usage), ("b", "stop", {"n": 2}))

    def test_logprobs_come_back_per_call(self):
        p = tmp()
        p.write_text(json.dumps({KEY: {"calls": [dict(rec("a"), logprobs=[["A", -0.1]]), rec("b")]}}))
        rp = ReplayClient(str(p))
        self.assertEqual(call(rp).logprobs, [["A", -0.1]])
        self.assertIsNone(call(rp).logprobs)

    def test_asking_past_the_end_raises_fixture_missing_with_the_hash(self):
        p = self.record("a", "b")
        rp = ReplayClient(str(p))
        call(rp)
        call(rp)
        with self.assertRaises(FixtureMissing) as cm:
            call(rp)
        self.assertEqual(cm.exception.args[0], KEY)
        self.assertTrue(cm.exception.fatal)

    def test_counters_are_per_key(self):
        other = [{"role": "user", "content": "other"}]
        p = tmp()
        p.write_text(json.dumps({KEY: {"calls": [rec("k0"), rec("k1")]}, messages_sha256(other): rec("o")}))
        rp = ReplayClient(str(p))
        self.assertEqual(call(rp).text, "k0")
        self.assertEqual(call(rp, other).text, "o")
        self.assertEqual(call(rp, other).text, "o")        # a legacy record is served on every call
        self.assertEqual(call(rp).text, "k1")

    def test_legacy_fixtures_still_replay_on_every_call(self):
        p = tmp()
        p.write_text(json.dumps({KEY: "bare text", messages_sha256([{"role": "user", "content": "q"}]):
                                 {"text": "full", "finish_reason": "length", "usage": {"u": 1}}}))
        rp = ReplayClient(str(p))
        for _ in range(3):
            r = call(rp)
            self.assertEqual((r.text, r.finish_reason), ("bare text", "stop"))
            r = call(rp, [{"role": "user", "content": "q"}])
            self.assertEqual((r.text, r.finish_reason, r.usage), ("full", "length", {"u": 1}))

    def test_a_calls_list_may_hold_bare_text_entries(self):
        p = tmp()
        p.write_text(json.dumps({KEY: {"calls": ["bare", rec("full")]}}))
        rp = ReplayClient(str(p))
        r = call(rp)
        self.assertEqual((r.text, r.finish_reason), ("bare", "stop"))
        self.assertEqual(call(rp).text, "full")

    def test_an_empty_calls_list_is_a_miss(self):
        p = tmp()
        p.write_text(json.dumps({KEY: {"calls": []}}))
        with self.assertRaises(FixtureMissing):
            call(ReplayClient(str(p)))

    def test_clients_built_to_share_fixtures_share_the_counters(self):
        # build_clients makes one ReplayClient per role over the same file; the recording was one store, in call order
        p = self.record("recon-or-judge first", "second role asked the same")
        a = ReplayClient(str(p))
        b = ReplayClient(str(p), share_with=a)
        self.assertEqual(call(a).text, "recon-or-judge first")
        self.assertEqual(call(b).text, "second role asked the same")
        with self.assertRaises(FixtureMissing):
            call(a)

    def test_unshared_clients_count_on_their_own(self):
        p = self.record("x", "y")
        self.assertEqual(call(ReplayClient(str(p))).text, "x")
        self.assertEqual(call(ReplayClient(str(p))).text, "x")


class TestBuildClientsReplayShares(unittest.TestCase):
    def test_every_role_replays_from_one_shared_counter(self):
        from second_scribe import pipeline
        from second_scribe.cli import DEMO_CONFIG, build_clients
        p = tmp()
        p.write_text(json.dumps({KEY: {"calls": [rec("c0"), rec("c1")]},
                                 "_meta": {"models": {"recon": "mr", "judge": "mj"}}}))
        clients = build_clients("replay", pipeline.RunConfig.load(DEMO_CONFIG), str(p))
        self.assertGreaterEqual(len(clients), 2)
        first, second = list(clients.values())[:2]
        self.assertEqual(call(first).text, "c0")
        self.assertEqual(call(second).text, "c1")
        self.assertEqual(clients["recon"].model_label, "mr")


if __name__ == "__main__":
    unittest.main()
