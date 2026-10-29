"""``--client replay-live``: the recorded responses are replayed, except the reading-3 map (stage ``prior_only``, item
id ending in ``__<reading-3 key>``) and the judge calls that align it (``judge:align``, item_id containing
``__prior_endorsed``), which go to a live client even when an older recording holds the same messages. A script draft
with the same messages (same variant, other temperature) replays. Fake live client, temp dirs, no network."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, _CallMeter, build_clients, build_clients_or_refuse
from second_scribe.llm import (FixtureMissing, ModelConfig, ModelResponse, RecordingClient, ReplayClient,
                               ReplayLiveClient, log_call, messages_sha256)
from second_scribe.pipeline import RunConfig
from second_scribe.run_safety import RunRefused

REPO = Path(__file__).resolve().parents[1]
ALIGN_PRIOR = "SYN-OW01__prior_endorsed:0"
ALIGN_OTHER = "SYN-OW01__ai_endorsed__full__p0__t0__r0:0"
R3_KEY = "prior_only__p0__t0__r0"
R3_ITEM = "owner__" + R3_KEY                         # the reading-3 draft of a role
SCRIPT_ITEM = "owner__prior_only__p0__t0.7__r0"      # a script draft of the same variant: the same messages


def msgs(text):
    return [{"role": "user", "content": text}]


class FakeLive:
    """Records what it was asked and answers ``live: <last message>``."""
    def __init__(self):
        self.cfg = ModelConfig(name="recon", endpoint="http://127.0.0.1:9/v1", model_id="live-model-1")
        self.asked = []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.asked.append({"messages": messages, "temperature": temperature, "seed": seed, "params": params})
        return ModelResponse("live: " + messages[-1]["content"], "stop", {}, 5, "raw")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fixtures = self.dir / "fx.json"
        self.log_path = self.dir / "logs" / "run.jsonl"
        self.log_path.parent.mkdir()
        self.log = AuditLog(self.log_path, "r", clock=lambda: "t")
        self.live = FakeLive()

    def fixtures_with(self, **by_text):
        """Write fixtures {messages_sha256(msgs(text)): recorded text}."""
        self.fixtures.write_text(json.dumps({messages_sha256(msgs(t)): r for t, r in by_text.items()}))

    def client(self, **kw):
        return ReplayLiveClient(str(self.fixtures), self.live, self.log_path, reading3_keys=(R3_KEY,), **kw)

    def request(self, text, stage, item_id):
        """What log_call writes before it calls the client."""
        self.log.append("call_request", {"request_id": f"{stage}:{item_id}:1:x", "stage": stage, "item_id": item_id,
                                         "attempt": 1, "messages": msgs(text),
                                         "messages_sha256": messages_sha256(msgs(text))})

    def ask(self, client, text):
        return client.complete(msgs(text), temperature=0.0, seed=1, max_tokens=5)


class TestRouting(Base):
    def test_a_a_recorded_request_of_another_stage_is_replayed(self):
        self.fixtures_with(q="recorded")
        self.request("q", "recon:full", "SYN-OW01__full")
        c = self.client()
        self.assertEqual(self.ask(c, "q").text, "recorded")
        self.assertEqual(self.live.asked, [])
        self.assertEqual((c.live_calls, c.replayed_calls), (0, 1))

    def test_b_a_recorded_prior_only_request_goes_live_and_the_fixture_is_not_served(self):
        self.fixtures_with(q={"calls": [{"text": "old T 0.7", "finish_reason": "stop", "usage": {}}]})
        self.request("q", "prior_only", R3_ITEM)
        c = self.client()
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual(len(self.live.asked), 1)
        self.assertEqual(c.served, {})                       # the recorded answer was neither used nor counted
        self.assertEqual((c.live_calls, c.replayed_calls), (1, 0))

    def test_c_an_unrecorded_prior_endorsed_alignment_goes_live(self):
        self.fixtures_with()
        self.request("q", "judge:align", ALIGN_PRIOR)
        self.assertEqual(self.ask(self.client(), "q").text, "live: q")
        self.assertEqual(len(self.live.asked), 1)

    def test_d_an_unrecorded_alignment_of_another_map_stops_without_a_live_call(self):
        self.fixtures_with()
        self.request("q", "judge:align", ALIGN_OTHER)
        with self.assertRaises(FixtureMissing) as cm:
            self.ask(self.client(), "q")
        self.assertEqual(cm.exception.args[0], messages_sha256(msgs("q")))
        self.assertEqual(self.live.asked, [])

    def test_e_a_logged_request_that_is_not_the_open_newest_record_is_an_unlogged_re_ask_and_goes_live(self):
        self.fixtures_with()
        c = self.client()
        self.request("q", "recon:full", "SYN-OW01__full")
        self.request("other", "recon:full", "SYN-OW01__full")
        self.assertEqual(self.ask(c, "q").text, "live: q")   # logged earlier, but its newest request is another one
        self.assertEqual((c.live_calls, c.live_reasks, c.replayed_calls), (1, 1, 0))

    def test_e1_a_request_the_log_never_held_stops_without_a_live_call(self):
        self.fixtures_with(q="recorded")
        c = self.client()
        with self.assertRaises(FixtureMissing):              # a log that holds no request at all
            self.ask(c, "q")
        self.request("other", "recon:full", "SYN-OW01__full")
        with self.assertRaises(FixtureMissing) as cm:        # a log, but q was never in it (recorded or not)
            self.ask(c, "q")
        self.assertEqual(cm.exception.args[0], messages_sha256(msgs("q")))
        self.assertEqual(self.live.asked, [])
        self.assertEqual((c.live_calls, c.live_reasks, c.replayed_calls), (0, 0, 0))

    def test_e2_a_missing_log_file_stops_without_a_live_call(self):
        self.fixtures_with(q="recorded")
        c = ReplayLiveClient(str(self.fixtures), self.live, self.dir / "nope" / "run.jsonl", reading3_keys=(R3_KEY,))
        with self.assertRaises(FixtureMissing) as cm:
            self.ask(c, "q")
        self.assertIn("does not exist", str(cm.exception))
        self.assertEqual(self.live.asked, [])
        self.assertEqual((c.live_calls, c.live_reasks), (0, 0))

    def test_f_a_re_ask_of_an_allowed_request_goes_live_again(self):
        self.fixtures_with(q="recorded")
        self.request("q", "prior_only", R3_ITEM)      # logged once; the auditor's re-ask writes no record
        c = self.client()
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual(len(self.live.asked), 2)

    def test_params_are_passed_to_the_live_client_only_when_given(self):
        self.fixtures_with()
        self.request("q", "prior_only", R3_ITEM)
        c = self.client()
        self.ask(c, "q")
        c.complete(msgs("q"), temperature=0.7, seed=2, max_tokens=5, params={"logprobs": True})
        self.assertIsNone(self.live.asked[0]["params"])
        self.assertEqual(self.live.asked[1]["params"], {"logprobs": True})
        self.assertEqual(self.live.asked[1]["temperature"], 0.7)

    def test_the_log_is_read_incrementally_and_a_half_written_line_waits(self):
        self.fixtures_with()
        c = self.client()
        with self.assertRaises(FixtureMissing):              # not in the log yet: stops
            self.ask(c, "q")
        self.request("q", "prior_only", R3_ITEM)      # appears after the first call: only the tail is read
        self.assertEqual(self.ask(c, "q").text, "live: q")
        offset = c._scan["offset"]
        self.assertEqual(offset, self.log_path.stat().st_size)
        with open(self.log_path, "ab") as fh:                # the start of the next record, no newline yet
            fh.write(b'{"record_type": "call_request", "payload": {"stage": "prior_only", "messages_sha256": "')
        with self.assertRaises(FixtureMissing):                          # the half line is not a logged request
            self.ask(c, "other")
        self.assertEqual(c._scan["offset"], offset)

    def test_clients_sharing_a_recording_share_fixtures_counters_and_the_allowed_requests(self):
        self.fixtures_with(r="recorded")
        first = self.client()
        second_live = FakeLive()
        second = ReplayLiveClient(str(self.fixtures), second_live, self.log_path, share_with=first,
                                  reading3_keys=(R3_KEY,))
        self.assertIs(second.fixtures, first.fixtures)
        self.assertIs(second.served, first.served)
        self.assertIs(second._scan, first._scan)
        self.assertIsNot(second.live, first.live)
        self.request("q", "prior_only", R3_ITEM)
        self.assertEqual(self.ask(second, "q").text, "live: q")
        self.assertEqual((len(self.live.asked), len(second_live.asked)), (0, 1))
        self.request("r", "recon:full", "SYN-OW01__full")
        self.assertEqual(self.ask(first, "r").text, "recorded")
        first.role, second.role = "judge", "recon"
        self.request("z", "recon:full", "SYN-OW01__full")
        self.request("r", "recon:full", "SYN-OW01__full")
        self.ask(second, "z")                                # logged earlier, no recording: a live re-ask
        self.assertEqual((first.live_reasks, second.live_reasks), (0, 1))
        self.assertEqual(first.live_reasks_by_role, {"recon": 1})

    def test_b2_a_recorded_script_draft_with_the_same_messages_replays_and_reading3_goes_live(self):
        self.fixtures_with(q={"calls": [{"text": "old T 0.7", "finish_reason": "stop", "usage": {}}]})
        c = self.client()
        self.request("q", "prior_only", SCRIPT_ITEM)        # the script draft is asked first, as _stage_prior_only does
        self.assertEqual(self.ask(c, "q").text, "old T 0.7")
        self.request("q", "prior_only", R3_ITEM)            # then reading 3: same messages hash, goes live
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual((c.live_calls, c.replayed_calls), (1, 1))
        self.assertEqual(len(self.live.asked), 1)

    def test_b3_the_newest_request_decides_even_after_reading3_was_allowed(self):
        self.fixtures_with(q="recorded")
        c = self.client()
        self.request("q", "prior_only", R3_ITEM)
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.request("q", "prior_only", SCRIPT_ITEM)        # a script draft logged after reading 3 (a resumed run)
        self.assertEqual(self.ask(c, "q").text, "recorded")
        self.assertEqual(len(self.live.asked), 1)

    def test_b4_a_prior_only_item_of_another_key_never_goes_live(self):
        self.fixtures_with()
        for item in (SCRIPT_ITEM, "owner__prior_only__p1__t0__r0", "SYN-OW01__t0"):
            self.request("q", "prior_only", item)
            with self.assertRaises(FixtureMissing):
                self.ask(self.client(), "q")
        self.assertEqual(self.live.asked, [])

    def test_b5_a_re_ask_of_reading3_after_other_requests_goes_live(self):
        self.fixtures_with(r="recorded")
        c = self.client()
        self.request("q", "prior_only", R3_ITEM)
        self.ask(c, "q")
        self.request("r", "recon:full", "SYN-OW01__full")
        self.assertEqual(self.ask(c, "r").text, "recorded")
        self.assertEqual(self.ask(c, "q").text, "live: q")   # the auditor's re-ask: no record of its own
        self.assertEqual(len(self.live.asked), 2)

    def test_l75_the_other_familys_script_goes_live_even_with_a_recorded_prior_only_hash(self):
        self.fixtures_with(q={"calls": [{"text": "old T 0.7", "finish_reason": "stop", "usage": {}}]})
        c = self.client()
        self.request("q", "prior_only", SCRIPT_ITEM)        # the reconstructor's draft: same messages, replayed
        self.assertEqual(self.ask(c, "q").text, "old T 0.7")
        self.request("q", "judge:script_prior_only", "judgescript__owner__prior_only__p0__t0.7__r0")
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual(self.ask(c, "q").text, "live: q")   # its re-ask: live too
        self.assertEqual((c.live_calls, c.live_reasks), (2, 0))

    def test_l75_an_endorsed_src_alignment_goes_live(self):
        self.fixtures_with(q="recorded")
        self.request("q", "judge:align", "SYN-OW01__endorsed_src__judgescript:0")
        self.assertEqual(self.ask(self.client(), "q").text, "live: q")

    def test_l75_a_judge2_stage_is_never_live(self):
        self.fixtures_with()
        for stage, item in (("judge2:script_prior_only", "other__owner__prior_only__p0__t0.7__r0"),
                            ("judge2:modality", "SYN-OW01__full__p0__t0__r0"),
                            ("judge2:align", "SYN-OW01__endorsed_src__other:0")):
            self.request("q", stage, item)
            with self.assertRaises(FixtureMissing, msg=stage):
                self.ask(self.client(), "q")
        self.assertEqual(self.live.asked, [])

    def test_it_is_a_replay_client_and_exposes_the_live_cfg(self):
        self.fixtures_with()
        c = self.client()
        self.assertIsInstance(c, ReplayClient)
        self.assertIs(c.cfg, self.live.cfg)


class TestThroughLogCall(Base):
    def test_g_log_call_records_both_and_a_recording_of_it_keeps_both(self):
        self.fixtures_with(old={"calls": [{"text": "old T 0.7", "finish_reason": "stop", "usage": {}}]},
                           replayed="recorded")
        c = self.client()
        out = self.dir / "new-fx.json"
        rec = RecordingClient(c, str(out), merge=False)
        text1, recs1 = log_call(self.log, rec, msgs("old"), stage="prior_only", item_id=R3_ITEM,
                                temperature=0.0, seed=1, max_tokens=5)
        text2, recs2 = log_call(self.log, rec, msgs("replayed"), stage="recon:full", item_id="SYN-OW01__full",
                                temperature=0.0, seed=1, max_tokens=5)
        self.log.close()
        self.assertEqual((text1, text2), ("live: old", "recorded"))
        self.assertEqual(len(self.live.asked), 1)
        self.assertEqual((c.live_calls, c.replayed_calls), (1, 1))
        # the request that went out names the live model and host, and was logged before the client was called
        self.assertEqual(recs1[0]["payload"]["model_id"], "live-model-1")
        self.assertEqual(recs1[0]["payload"]["endpoint_host"], "127.0.0.1:9")
        rec.save()
        stored = json.loads(out.read_text())
        self.assertEqual(stored[messages_sha256(msgs("old"))]["text"], "live: old")
        self.assertEqual(stored[messages_sha256(msgs("replayed"))]["text"], "recorded")

    def test_a_miss_through_log_call_is_fatal_and_makes_no_live_call(self):
        self.fixtures_with()
        with self.assertRaises(FixtureMissing):
            log_call(self.log, self.client(), msgs("q"), stage="recon:full", item_id="SYN-OW01__full",
                     temperature=0.0, seed=1, max_tokens=5)
        self.log.close()
        types = [json.loads(line)["record_type"] for line in self.log_path.read_text().splitlines()]
        self.assertEqual(types, ["call_request", "call_error"])
        self.assertEqual(self.live.asked, [])


class TestReAsks(Base):
    """Determinism re-asks: an auditor re-sends a logged request through ``complete`` with no record of its own."""
    REC = {"text": "recorded 1", "finish_reason": "stop", "usage": {}}

    def answer(self, request_id="recon:full:SYN-OW01__full:1:x"):
        self.log.append("call_response", {"request_id": request_id, "text": "-"})

    def test_i_a_re_ask_of_a_single_record_goes_live_and_the_recording_keeps_both_answers(self):
        self.fixtures_with(q=self.REC)
        c = self.client()
        out = self.dir / "new-fx.json"
        rec = RecordingClient(c, str(out), merge=False)
        text1, _ = log_call(self.log, rec, msgs("q"), stage="recon:full", item_id="SYN-OW01__full",
                            temperature=0.0, seed=1, max_tokens=5)
        text2 = rec.complete(msgs("q"), temperature=0.0, seed=1, max_tokens=5).text      # the re-ask: no record
        self.log.close()
        self.assertEqual((text1, text2), ("recorded 1", "live: q"))
        self.assertEqual((c.replayed_calls, c.live_calls, c.live_reasks), (1, 1, 1))
        rec.save()
        calls = json.loads(out.read_text())[messages_sha256(msgs("q"))]["calls"]
        self.assertEqual([x["text"] for x in calls], ["recorded 1", "live: q"])
        self.assertNotEqual(calls[1], calls[0])             # never a fake {"calls": [rec, rec]}

    def test_ii_a_re_ask_of_a_two_entry_list_serves_the_second_entry_without_a_live_call(self):
        self.fixtures_with(q={"calls": [self.REC, {**self.REC, "text": "recorded 2"}]})
        c = self.client()
        self.request("q", "recon:full", "SYN-OW01__full")
        self.assertEqual(self.ask(c, "q").text, "recorded 1")
        self.answer()
        self.assertEqual(self.ask(c, "q").text, "recorded 2")
        self.assertEqual((c.replayed_calls, c.live_calls, c.live_reasks), (2, 0, 0))
        self.assertEqual(self.live.asked, [])

    def test_iii_a_re_ask_of_an_exhausted_list_goes_live(self):
        self.fixtures_with(q={"calls": [self.REC]})
        c = self.client()
        self.request("q", "recon:full", "SYN-OW01__full")
        self.ask(c, "q")
        self.answer()
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual((c.replayed_calls, c.live_calls, c.live_reasks), (1, 1, 1))

    def test_iv_a_logged_miss_that_is_not_reading3_is_still_fatal(self):
        self.fixtures_with(q={"calls": [self.REC]})
        c = self.client()
        self.request("q", "recon:full", "SYN-OW01__full")
        self.ask(c, "q")
        self.request("q", "recon:full", "SYN-OW01__full")    # logged again (a retry): the list is spent
        with self.assertRaises(FixtureMissing):
            self.ask(c, "q")
        self.request("new", "judge:align", ALIGN_OTHER)
        with self.assertRaises(FixtureMissing):
            self.ask(c, "new")
        self.assertEqual(self.live.asked, [])

    def test_a_re_ask_of_the_newest_request_after_its_answer_is_a_re_ask(self):
        self.fixtures_with(q=self.REC)
        c = self.client()
        self.request("q", "recon:full", "SYN-OW01__full")
        self.ask(c, "q")
        self.log.append("call_error", {"request_id": "x", "error": "-"})      # any answer or error closes it
        self.assertEqual(self.ask(c, "q").text, "live: q")
        self.assertEqual(c.live_reasks, 1)


class TestMeter(Base):
    def test_the_run_summary_counts_the_live_and_the_replayed_calls_of_the_inner_client(self):
        self.fixtures_with(r="recorded")
        self.request("q", "prior_only", R3_ITEM)
        meter = _CallMeter(self.client())
        self.assertEqual(meter.kind, "replay-live")
        self.ask(meter, "q")
        self.request("r", "recon:full", "SYN-OW01__full")
        self.ask(meter, "r")
        self.request("unknown", "recon:full", "SYN-OW02__full")
        with self.assertRaises(FixtureMissing):
            self.ask(meter, "unknown")
        self.assertEqual((meter.model_calls, meter.replayed), (1, 1))
        self.ask(meter, "r")                                 # the auditor's re-ask of a single record: live
        self.assertEqual((meter.model_calls, meter.replayed), (2, 1))


class TestBuildClients(unittest.TestCase):
    ENV = {"SS_RECON_URL": "http://127.0.0.1:9/v1", "SS_RECON_MODEL": "alpha-recon-1",
           "SS_JUDGE_URL": "http://127.0.0.1:9/v1", "SS_JUDGE_MODEL": "beta-judge-1"}

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = RunConfig.load(DEMO_CONFIG)

    def fixtures_meta(self, models):
        """A fixtures file whose ``_meta.models`` is ``models`` (None: no ``_meta``); the path is cleaned up."""
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "fx.json"
        path.write_text(json.dumps({} if models is None else {"_meta": {"models": models}}))
        return str(path)

    def test_h_a_synthetic_corpus_gets_a_replay_live_client_per_role(self):
        fx = self.fixtures_meta({"recon": "alpha-recon-1", "judge": "beta-judge-1"})     # the live models, as recorded
        with mock.patch.dict(os.environ, self.ENV):
            clients = build_clients("replay-live", self.cfg, fx)
        self.assertEqual(sorted(clients), ["judge", "recon"])
        for role, c in clients.items():
            self.assertIsInstance(c, ReplayLiveClient)
            self.assertEqual(c.model_label, self.ENV[f"SS_{role.upper()}_MODEL"])
            self.assertEqual(Path(c.log_path), Path(self.cfg.out_dir) / "logs" / "run.jsonl")
        self.assertIs(clients["judge"].fixtures, clients["recon"].fixtures)
        self.assertIs(clients["judge"]._scan, clients["recon"]._scan)
        for c in clients.values():
            self.assertEqual(c.reading3_keys, ("prior_only__p0__t0__r0",))     # configs/demo.json's reading3 block

    def test_replay_live_clients_carry_their_role(self):
        fx = self.fixtures_meta({"recon": "alpha-recon-1", "judge": "beta-judge-1"})
        with mock.patch.dict(os.environ, self.ENV):
            clients = build_clients("replay-live", self.cfg, fx)
        self.assertEqual({r: c.role for r, c in clients.items()}, {"recon": "recon", "judge": "judge"})

    def test_s3_a_live_model_that_is_not_the_recorded_one_is_refused(self):
        fx = self.fixtures_meta({"recon": "alpha-recon-1", "judge": "gamma-judge-9"})
        want = ("--client replay-live: the live judge model is beta-judge-1, the recording's is gamma-judge-9; a mixed "
                "recording would credit one model's answers to another")
        with mock.patch.dict(os.environ, self.ENV):
            with self.assertRaises(ValueError) as cm:
                build_clients("replay-live", self.cfg, fx)
            self.assertEqual(str(cm.exception), want)
            with self.assertRaises(RunRefused) as cm:
                build_clients_or_refuse("replay-live", self.cfg, fx)
            self.assertEqual(str(cm.exception), want)
            self.assertEqual(sorted(build_clients("replay-live", self.cfg, fx, roles=("recon",))), ["recon"])
            for models in (None, {}, {"recon": "alpha-recon-1"}):      # no entry for a role: nothing to compare it with
                self.assertEqual(sorted(build_clients("replay-live", self.cfg, self.fixtures_meta(models))),
                                 ["judge", "recon"])
        with mock.patch.dict(os.environ, {**self.ENV, "SS_RECON_MODEL": "other"}):
            with self.assertRaises(RunRefused) as cm:                  # the recording's own ids are the shipped fixtures'
                build_clients_or_refuse("replay-live", self.cfg)
            self.assertIn("the live recon model is other, the recording's is mtplx-qwen38-flashnext", str(cm.exception))

    def test_h_a_config_without_a_reading3_block_is_refused(self):
        cfg = copy.deepcopy(self.cfg)
        cfg.reading3 = None
        with mock.patch.dict(os.environ, self.ENV):
            with self.assertRaises(ValueError) as cm:
                build_clients("replay-live", cfg)
            self.assertEqual(str(cm.exception), "--client replay-live needs a reading3 block: it re-records only "
                                                "reading 3 and the other family's script (llm.LIVE_RULE)")
            with self.assertRaises(RunRefused) as cm:
                build_clients_or_refuse("replay-live", cfg)
            self.assertIn("needs a reading3 block", str(cm.exception))

    def test_h_a_corpus_that_is_not_synthetic_is_refused(self):
        for kind in ("real", "unknown"):
            cfg = copy.deepcopy(self.cfg)
            cfg.corpus["kind"] = kind
            with mock.patch.dict(os.environ, self.ENV):
                with self.assertRaises(ValueError) as cm:
                    build_clients("replay-live", cfg)
                self.assertIn("only for a synthetic corpus", str(cm.exception))
                with self.assertRaises(RunRefused) as cm:
                    build_clients_or_refuse("replay-live", cfg)
                self.assertIn("--client replay-live is only for a synthetic corpus", str(cm.exception))

    def test_missing_endpoint_variables_and_a_bad_fixtures_file_are_refusals(self):
        with self.assertRaises(RunRefused) as cm:
            build_clients_or_refuse("replay-live", self.cfg)
        self.assertIn("SS_RECON_URL", str(cm.exception))
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as d, mock.patch.dict(os.environ, self.ENV):
            bad = Path(d) / "fx.json"
            bad.write_text("not json")
            with self.assertRaises(RunRefused) as cm:
                build_clients_or_refuse("replay-live", self.cfg, str(bad))
            self.assertIn("not a recorded-responses file", str(cm.exception))
            with self.assertRaises(RunRefused) as cm:
                build_clients_or_refuse("replay-live", self.cfg, str(Path(d) / "absent.json"))
            self.assertIn("cannot be opened", str(cm.exception))
        with mock.patch.dict(os.environ, {**self.ENV, "SS_RECON_TIMEOUT_S": "soon"}):
            with self.assertRaises(RunRefused) as cm:
                build_clients_or_refuse("replay-live", self.cfg)
            self.assertIn("endpoint settings", str(cm.exception))


class TestRunSubparser(unittest.TestCase):
    def help(self, cmd):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        return subprocess.run([sys.executable, "-m", "second_scribe.cli", cmd, "--help"], cwd=REPO, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout

    def test_run_offers_replay_live_and_demo_does_not(self):
        self.assertIn("replay-live", self.help("run"))
        self.assertNotIn("replay-live", self.help("demo"))


if __name__ == "__main__":
    unittest.main()
