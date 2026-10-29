"""Tagged fixture entries (fix round 2): replay across sessions when different roles or temperatures send identical
messages.

The fixture key is the hash of the messages alone. In the demo the reconstructor's prior-only drafts (T 0.7), its
reading-3 draft (T 0), that draft's determinism re-ask (T 0, not logged) and the judge's script drafts (T 0.7) share
keys. One process replays them in recording order; a new process (a judge-only session after the reconstructor's, a
resumed run) cannot tell from its log which positions the other role or the un-logged re-asks took. Entries tagged with
``role`` and ``temperature`` are served per (key, role, temperature); untagged values keep the positional behaviour.
Also: ``pipeline._logged_models`` takes a replay's model ids from ``run_start``. Offline, temp dirs only."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import (FixtureMissing, ModelConfig, ModelResponse, RecordingClient, ReplayClient,
                               ReplayLiveClient, log_call, messages_sha256)
from second_scribe.pipeline import _logged_models

REPO = Path(__file__).resolve().parents[1]
MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "the same prior-only prompt"}]
KEY = messages_sha256(MSGS)
OTHER = [{"role": "user", "content": "another prompt"}]


def entry(text, role, temperature):
    return {"text": text, "finish_reason": "stop", "usage": {}, "role": role, "temperature": temperature}


# the brief's shared key, in recording order: two recon drafts at T 0.7, the reading-3 draft at T 0, its determinism
# re-ask at T 0 (un-logged), then two judge script drafts at T 0.7
SHARED = {"calls": [entry("recon 0.7 a", "recon", 0.7), entry("recon 0.7 b", "recon", 0.7),
                    entry("recon 0 draft", "recon", 0.0), entry("recon 0 re-ask", "recon", 0.0),
                    entry("judge 0.7 a", "judge", 0.7), entry("judge 0.7 b", "judge", 0.7)]}


def ask(client, temperature, msgs=MSGS):
    return client.complete(msgs, temperature=temperature, seed=1, max_tokens=50).text


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.fixtures = self.dir / "fx.json"
        self.log_path = self.dir / "logs" / "run.jsonl"
        self.log_path.parent.mkdir()

    def write(self, store):
        self.fixtures.write_text(json.dumps(store))

    def log_recon_session(self):
        """What the reconstructor's session logs for KEY: the two T 0.7 drafts and the T 0 draft (the re-ask is not
        logged), each answered."""
        log = AuditLog(self.log_path, "r", clock=lambda: "t")
        for i, (stage, item, t) in enumerate([("prior_only", "owner__prior_only__p0__t0.7__r0", 0.7),
                                              ("prior_only", "owner__prior_only__p0__t0.7__r1", 0.7),
                                              ("prior_only", "owner__prior_only__p0__t0__r0", 0.0)]):
            rid = f"{stage}:{item}:1:{i:08x}"
            log.append("call_request", {"request_id": rid, "stage": stage, "item_id": item, "attempt": 1,
                                        "messages": MSGS, "messages_sha256": KEY, "temperature": t})
            log.append("call_response", {"request_id": rid, "text": "x", "validation_error": None})
        log.close()


class TestTaggedStore(Base):
    def test_one_process_serves_each_role_and_temperature_its_own_entries_in_order(self):
        self.write({KEY: SHARED})
        recon = ReplayClient(str(self.fixtures), role="recon")
        judge = ReplayClient(str(self.fixtures), share_with=recon, role="judge")
        self.assertIs(judge.served, recon.served)
        self.assertEqual([ask(recon, 0.7), ask(recon, 0.7), ask(recon, 0), ask(recon, 0.0)],
                         ["recon 0.7 a", "recon 0.7 b", "recon 0 draft", "recon 0 re-ask"])
        self.assertEqual([ask(judge, 0.7), ask(judge, 0.7)], ["judge 0.7 a", "judge 0.7 b"])

    def test_a_fresh_judge_client_after_a_log_holding_the_recon_session_gets_the_judge_entries(self):
        self.write({KEY: SHARED})
        self.log_recon_session()
        judge = ReplayClient(str(self.fixtures), role="judge")      # a new process: J only, after Q
        judge.resume_from_log(self.log_path)
        self.assertEqual([ask(judge, 0.7), ask(judge, 0.7)], ["judge 0.7 a", "judge 0.7 b"])

    def test_a_resumed_recon_re_ask_gets_the_recorded_t0_re_ask(self):
        self.write({KEY: SHARED})
        self.log_recon_session()
        recon = ReplayClient(str(self.fixtures), role="recon")
        recon.resume_from_log(self.log_path)
        self.assertEqual(ask(recon, 0), "recon 0 re-ask")          # not a judge T 0.7 answer, not the draft again
        self.assertEqual(recon.served[(KEY, "recon", 0.0)], 2)

    def test_a_logged_response_of_an_unmapped_stage_is_counted_once(self):
        """``_role("preflight:recon")`` is None, so the (key, role, T) and (key, None, T) slots are one slot: the
        response must count once (review note 1a), or the next call skips an unserved entry."""
        self.write({KEY: {"calls": [entry("a", "recon", 0.0), entry("b", "recon", 0.0), entry("c", "recon", 0.0)]}})
        log = AuditLog(self.log_path, "r", clock=lambda: "t")
        rid = "preflight:recon:canary:1:00000000"
        log.append("call_request", {"request_id": rid, "stage": "preflight:recon", "item_id": "canary", "attempt": 1,
                                    "messages": MSGS, "messages_sha256": KEY, "temperature": 0.0})
        log.append("call_response", {"request_id": rid, "text": "a", "validation_error": None})
        log.close()
        client = ReplayClient(str(self.fixtures))                  # a client without a role matches on temperature only
        client.resume_from_log(self.log_path)
        self.assertEqual(client.served, {(KEY, None, 0.0): 1})
        self.assertEqual(ask(client, 0), "b")

    def test_exhaustion_is_a_miss(self):
        self.write({KEY: SHARED})
        self.log_recon_session()
        judge = ReplayClient(str(self.fixtures), role="judge")
        judge.resume_from_log(self.log_path)
        ask(judge, 0.7)
        ask(judge, 0.7)
        with self.assertRaises(FixtureMissing) as cm:
            ask(judge, 0.7)                                     # judge entries remain for no one: strict
        self.assertEqual(cm.exception.args[0], KEY)
        recon = ReplayClient(str(self.fixtures), share_with=judge, role="recon")
        with self.assertRaises(FixtureMissing):
            ask(recon, 0.7)                                     # both recon T 0.7 drafts were logged

    def test_a_tag_mismatch_is_a_miss(self):
        self.write({KEY: SHARED, messages_sha256(OTHER): entry("single", "recon", 0.0)})
        judge2 = ReplayClient(str(self.fixtures), role="judge2")
        with self.assertRaises(FixtureMissing):
            ask(judge2, 0.7)                                    # no judge2 entry in the list
        recon = ReplayClient(str(self.fixtures), role="recon")
        with self.assertRaises(FixtureMissing):
            ask(recon, 0.3)                                     # no entry at this temperature
        judge = ReplayClient(str(self.fixtures), role="judge")
        with self.assertRaises(FixtureMissing):
            ask(judge, 0.0, OTHER)                              # a tagged single record of another role
        with self.assertRaises(FixtureMissing):
            ask(recon, 0.7, OTHER)                              # ... or of another temperature
        self.assertEqual([ask(recon, 0, OTHER), ask(recon, 0.0, OTHER)], ["single", "single"])   # every call

    def test_a_client_without_a_role_matches_on_temperature_only(self):
        self.write({KEY: SHARED})
        anyone = ReplayClient(str(self.fixtures))
        self.assertEqual([ask(anyone, 0.7) for _ in range(4)],
                         ["recon 0.7 a", "recon 0.7 b", "judge 0.7 a", "judge 0.7 b"])
        self.assertEqual([ask(anyone, 0), ask(anyone, 0)], ["recon 0 draft", "recon 0 re-ask"])

    def test_one_untagged_entry_keeps_the_whole_list_positional(self):
        calls = [dict(e) for e in SHARED["calls"]]
        del calls[0]["role"]
        self.write({KEY: {"calls": calls}})
        judge = ReplayClient(str(self.fixtures), role="judge")
        self.assertEqual(ask(judge, 0.7), "recon 0.7 a")         # position 0, as before tags existed
        self.log_recon_session()
        judge.resume_from_log(self.log_path)
        self.assertEqual(judge.served, {KEY: 3})                 # per-key count, unchanged

    def test_untagged_values_keep_the_positional_behaviour(self):
        legacy = {"calls": [{"text": f"c{i}", "finish_reason": "stop", "usage": {}} for i in range(3)]}
        self.write({KEY: legacy, messages_sha256(OTHER): "bare"})
        c = ReplayClient(str(self.fixtures), role="judge")
        self.assertEqual([ask(c, 0.7), ask(c, 0), ask(c, 0.3)], ["c0", "c1", "c2"])
        with self.assertRaises(FixtureMissing):
            ask(c, 0.7)
        self.assertEqual([ask(c, 0, OTHER), ask(c, 0.7, OTHER)], ["bare", "bare"])
        self.assertEqual(c.served, {KEY: 3})


class TestBuildClientsGivesEachClientItsRole(Base):
    def test_replay_clients_carry_their_role(self):
        """Removing ``role=r`` in ``cli.build_clients`` must fail a test (review item 9a): a role-less client matches on
        temperature only and can land on another role's entry when the two share a key and a temperature."""
        from second_scribe.cli import DEMO_CONFIG, build_clients
        from second_scribe.pipeline import RunConfig
        self.write({KEY: SHARED})
        cfg = RunConfig.load(DEMO_CONFIG)
        clients = build_clients("replay", cfg, str(self.fixtures))
        self.assertEqual({r: c.role for r, c in clients.items()}, {"recon": "recon", "judge": "judge"})
        self.assertEqual(ask(clients["judge"], 0.7), "judge 0.7 a")     # not "recon 0.7 a"
        self.assertEqual(ask(clients["recon"], 0.7), "recon 0.7 a")


class FakeLive:
    def __init__(self):
        self.cfg = ModelConfig(name="recon", endpoint="http://127.0.0.1:9/v1", model_id="live-model-1")
        self.asked = []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.asked.append(temperature)
        return ModelResponse("live", "stop", {}, 5, "raw")


class TestReplayLiveOnATaggedStore(Base):
    def test_a_logged_request_and_its_re_asks_use_the_role_and_temperature_sub_sequence(self):
        store = {KEY: {"calls": [entry("recon 0 draft", "recon", 0.0), entry("judge 0 a", "judge", 0.0),
                                 entry("recon 0 re-ask", "recon", 0.0)]}}
        self.write(store)
        log = AuditLog(self.log_path, "r", clock=lambda: "t")
        live = FakeLive()
        recon = ReplayLiveClient(str(self.fixtures), live, self.log_path, reading3_keys=("prior_only__p9__t0__r0",),
                                 role="recon")
        log.append("call_request", {"request_id": "recon:full:SYN__full:1:x", "stage": "recon:full",
                                    "item_id": "SYN__full", "messages": MSGS, "messages_sha256": KEY})
        self.assertEqual(ask(recon, 0), "recon 0 draft")          # logged: replayed, recon's first T 0 entry
        log.append("call_response", {"request_id": "recon:full:SYN__full:1:x", "text": "recon 0 draft"})
        self.assertEqual(ask(recon, 0), "recon 0 re-ask")         # un-logged re-ask: recon's next T 0 entry
        self.assertEqual(ask(recon, 0), "live")                   # recon's T 0 entries are spent: a live re-ask,
        self.assertEqual((recon.live_reasks, live.asked), (1, [0]))   # though a judge entry is still unserved
        self.assertEqual(recon.live_reasks_by_role, {"recon": 1})
        log.close()


class TestRecordingWritesTags(Base):
    def test_records_carry_role_and_temperature(self):
        store: dict = {}
        answers = iter(["r1", "r2", "j1"])
        inner = SimpleNamespace(complete=lambda m, **kw: ModelResponse(next(answers), "stop", {}, 1, "h"))
        recon = RecordingClient(inner, str(self.fixtures), store=store, role="recon")
        judge = RecordingClient(inner, str(self.fixtures), store=store, role="judge")
        recon.complete(MSGS, temperature=0.7, seed=1, max_tokens=5)
        recon.complete(MSGS, temperature=0, seed=None, max_tokens=5)
        judge.complete(OTHER, temperature=0, seed=None, max_tokens=5)
        recon.save()
        saved = json.loads(self.fixtures.read_text())
        self.assertEqual([(c["role"], c["temperature"]) for c in saved[KEY]["calls"]], [("recon", 0.7), ("recon", 0.0)])
        self.assertEqual(saved[messages_sha256(OTHER)], {"text": "j1", "finish_reason": "stop", "usage": {},
                                                         "role": "judge", "temperature": 0.0})
        self.assertIsInstance(saved[KEY]["calls"][1]["temperature"], float)

    def test_a_recorded_run_replays_in_a_fresh_process_by_role(self):
        """Record a Q-like session (two T 0.7 drafts, a T 0 draft and its un-logged re-ask) and a J-like session (two
        T 0.7 script drafts with the same messages) through log_call, then replay the J session alone in a NEW process
        that resumes from the log, and a resumed Q re-ask in another: each gets what was recorded for it."""
        counter = iter(range(100))
        inner = SimpleNamespace(complete=lambda m, temperature, **kw: ModelResponse(
            f"answer {next(counter)} at {temperature}", "stop", {}, 1, "h"))
        store: dict = {}
        recon = RecordingClient(inner, str(self.fixtures), store=store, role="recon")
        judge = RecordingClient(inner, str(self.fixtures), store=store, role="judge")
        log = AuditLog(self.log_path, "r", clock=lambda: "t")
        got = {}
        for i, t in enumerate((0.7, 0.7, 0.0)):
            got[("recon", i)], _ = log_call(log, recon, list(MSGS), stage="prior_only", item_id=f"owner__d{i}",
                                            temperature=t, seed=i, max_tokens=5)
        got["re-ask"] = recon.complete(list(MSGS), temperature=0.0, seed=2, max_tokens=5).text     # not logged
        for i in range(2):
            got[("judge", i)], _ = log_call(log, judge, list(MSGS), stage="judge:script_prior_only",
                                            item_id=f"judgeprior__owner__d{i}", temperature=0.7, seed=i,
                                            max_tokens=5)
        log.close()
        recon.save()
        self.assertEqual(len(json.loads(self.fixtures.read_text())[KEY]["calls"]), 6)
        script = ("import json, sys\n"
                  "from second_scribe.llm import ReplayClient\n"
                  "fx, log, role, t, n = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5])\n"
                  "c = ReplayClient(fx, role=role)\n"
                  "c.resume_from_log(log)\n"
                  f"msgs = {MSGS!r}\n"
                  "print(json.dumps([c.complete(msgs, temperature=t, seed=0, max_tokens=5).text for _ in range(n)]))\n")

        def fresh(role, t, n):
            out = subprocess.run([sys.executable, "-c", script, str(self.fixtures), str(self.log_path), role, str(t),
                                  str(n)], cwd=REPO, capture_output=True, text=True, check=True)
            return json.loads(out.stdout)
        # the log holds both sessions: a fresh judge client has served none of its own entries yet
        self.log_path.rename(self.dir / "full.jsonl")
        lines = (self.dir / "full.jsonl").read_text().splitlines()
        self.log_path.write_text("\n".join(lines[:6]) + "\n")       # the Q session only (3 requests + responses)
        self.assertEqual(fresh("judge", 0.7, 2), [got[("judge", 0)], got[("judge", 1)]])
        self.assertEqual(fresh("recon", 0.0, 1), [got["re-ask"]])
        self.assertTrue(got["re-ask"].endswith("at 0.0"))


class TestLoggedModels(Base):
    def run_with(self, records):
        with open(self.log_path, "w", encoding="utf-8") as fh:
            for rtype, payload in records:
                fh.write(json.dumps({"record_type": rtype, "payload": payload}) + "\n")
        return _logged_models(SimpleNamespace(log=SimpleNamespace(path=self.log_path)))

    def test_a_null_model_id_falls_back_to_the_run_start_model_of_its_role(self):
        scripts, recon = self.run_with([
            ("run_start", {"models": {"recon": "mtplx-qwen38-flashnext"}}),
            ("call_request", {"stage": "prior_only", "item_id": "owner__x", "model_id": None}),
            ("run_start", {"models": {"judge": "deepseek-v4-flash-0731"}}),
            ("call_request", {"stage": "judge:script_prior_only", "item_id": "judgeprior__owner__x"})])
        self.assertEqual(scripts, {"judgeprior": {"deepseek-v4-flash-0731"}})
        self.assertEqual(recon, {"mtplx-qwen38-flashnext"})

    def test_a_logged_model_id_wins_over_run_start(self):
        scripts, recon = self.run_with([
            ("run_start", {"models": {"recon": "qwen-a", "judge": "deepseek-b"}}),
            ("call_request", {"stage": "recon:full", "item_id": "x", "model_id": "qwen-logged"}),
            ("call_request", {"stage": "judge:script_prior_only", "item_id": "judgeprior__o", "model_id": "llama-c"})])
        self.assertEqual(scripts, {"judgeprior": {"llama-c"}})
        self.assertEqual(recon, {"qwen-logged"})

    def test_no_run_start_model_leaves_the_id_off_the_record(self):
        scripts, recon = self.run_with([
            ("run_start", {"models": {"recon": "qwen-a"}}),
            ("call_request", {"stage": "judge:script_prior_only", "item_id": "judgeprior__o"})])
        self.assertEqual(scripts, {"judgeprior": {None}})        # families.script_family: script_model_not_on_record

    def test_the_oracle_and_replay_placeholder_labels_are_not_model_ids(self):
        scripts, recon = self.run_with([
            ("run_start", {"models": {"recon": "oracle", "judge": "replay"}}),
            ("call_request", {"stage": "recon:full", "item_id": "x"}),
            ("call_request", {"stage": "judge:script_prior_only", "item_id": "judgeprior__o"})])
        self.assertEqual((scripts, recon), ({"judgeprior": {None}}, {None}))


if __name__ == "__main__":
    unittest.main()
