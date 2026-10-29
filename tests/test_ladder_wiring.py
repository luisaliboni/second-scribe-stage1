"""Wiring of the instruction ladder into ``reconstruct`` (items 1-3 of fix-wire-a): the optional ``rung`` argument,
``run_ladder_arm`` and the null-phase share. Fake clients only; synthetic transcripts only."""
import json
import tempfile
import unittest
from pathlib import Path

import second_scribe.reconstruct as R
from second_scribe import ladder
from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient, ModelResponse, derive_seed
from second_scribe.reconstruct import (ReconContext, ReconSpec, build_messages, load_definitions, load_prompt,
                                       load_system, reconstruct)
from second_scribe.schemas import Transcript, Turn

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
CTX = ReconContext(domain="getting a food-truck licence", role="food-truck owner",
                   phases=("Planning", "Permits", "Inspections", "Operating"))
T = Transcript("SYN-OW01", [Turn(0, "interviewer", "What happened first?"),
                            Turn(1, "participant", "Then we had to get the business licence from city hall, it took weeks."),
                            Turn(2, "participant", "I wish there had been one office that handled every single permit.")], "synthetic")
GOOD = {"nodes": [{"id": "n1", "label": "Business licence", "type": "service", "phase": "Permits", "status": "experienced",
                   "quote": "we had to get the business licence from city hall, it took weeks"},
                  {"id": "n2", "label": "One-stop office", "type": "service", "phase": None, "status": "wished",
                   "quote": "I wish there had been one office that handled every single permit"}],
        "edges": [{"source": "n1", "target": "n2", "status": "wished",
                   "quote": "I wish there had been one office that handled every single permit"}],
        "emotion_word": None}
# Ladder arms run ONE draw at the primary T=0 setting (DECISIONS C7); the multi-draw machinery is exercised on a
# synthetic setting at T=0.7, where replicates are independent draws. SPEC is that setting; GOLDEN_SPEC keeps the
# T=0 replicate-1 spec of the rung-less goldens (reconstruct without a rung does not apply the ladder rule).
SPEC = ReconSpec("removed", "p0", 0.7, 1)
GOLDEN_SPEC = ReconSpec("removed", "p0", 0.0, 1)
RUNG_KEYS = [r.key for r in ladder.RUNGS]


class RecordingClient:
    """Fake client that records exactly what ``log_call`` hands to ``complete``."""

    def __init__(self, reply=None):
        self.reply = json.dumps(GOOD) if reply is None else reply
        self.calls = []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "temperature": temperature, "seed": seed,
                           "max_tokens": max_tokens, "params": params})
        return ModelResponse(self.reply, "stop", {}, 0, "x")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-wire-")
        self.addCleanup(self._tmp.cleanup)
        self.log = AuditLog(Path(self._tmp.name) / "r.jsonl", "r", clock=lambda: "t")


class TestRungNoneUnchanged(Base):
    """rung=None is today's behaviour byte for byte (golden values taken from main 41cacac)."""

    def spy(self, **kw):
        calls = []
        orig = R.log_call

        def wrapper(log, client, messages, **k):
            calls.append((json.loads(json.dumps(messages)), {a: b for a, b in k.items() if a != "validate"}))
            return orig(log, client, messages, **k)
        R.log_call = wrapper
        self.addCleanup(setattr, R, "log_call", orig)
        g, st = reconstruct(self.log, RecordingClient(), T, kw.pop("spec", GOLDEN_SPEC), CTX, PROMPTS,
                            item_id="SYN-OW01__x", **kw)
        return g, st, calls[-1]

    def test_golden_removed(self):
        g, st, (messages, kw) = self.spy()
        self.assertEqual(st, "ok")
        self.assertEqual(kw, {"stage": "recon:removed", "item_id": "SYN-OW01__x", "temperature": 0.0,
                              "seed": 676771549136748286, "max_tokens": 12000, "max_attempts": 3,
                              "length_correction": R.LENGTH_CORRECTION})
        self.assertEqual(g.map_id, "SYN-OW01__x")
        self.assertEqual({k: v for k, v in g.meta.items() if k in ("spec", "rung")}, {"spec": "removed__p0__t0__r1"})
        self.assertEqual(g.origin, "ai")

    def test_golden_full_seed(self):
        _g, _st, (_m, kw) = self.spy(spec=ReconSpec("full", "p0", 0.0, 1))
        self.assertEqual(kw["seed"], 881463816354769012)
        self.assertEqual(kw["stage"], "recon:full")

    def test_messages_are_the_v2_composition(self):
        _g, _st, (messages, _kw) = self.spy()
        want = build_messages(load_prompt(PROMPTS, "reconstruct", "p0", version="v2"), CTX, T.text("removed"),
                              system=load_system(PROMPTS, "reconstruct", "v2"),
                              definitions=load_definitions(PROMPTS, "v2"))
        self.assertEqual(messages, want)

    def test_explicit_rung_none_equals_omitted(self):
        _g, _st, a = self.spy()
        _g, _st, b = self.spy(rung=None)
        self.assertEqual(a, b)

    def test_v1_prompt_version_still_works(self):
        _g, _st, (messages, _kw) = self.spy(prompt_version="v1")
        self.assertEqual([m["role"] for m in messages], ["user"])


class TestRungArgument(Base):
    def run_rung(self, rung_key, spec=SPEC, item_id="SYN-OW01__x", **kw):
        client = RecordingClient()
        kw.setdefault("real_data", False)
        g, st = reconstruct(self.log, client, T, spec, CTX, PROMPTS, item_id=item_id,
                            rung=ladder.get_rung(rung_key), **kw)
        return g, st, client

    def test_messages_come_from_rung_messages(self):
        for key in RUNG_KEYS:
            with self.subTest(rung=key):
                rung = ladder.get_rung(key)
                _g, st, client = self.run_rung(key)
                self.assertEqual(st, "ok")
                self.assertEqual(client.calls[0]["messages"],
                                 ladder.rung_messages(PROMPTS, rung, CTX, T.text("removed"), spec=SPEC))

    def test_seed_identical_across_rungs_and_equal_to_todays_seed(self):
        for replicate in (0, 1, 2):
            spec = ReconSpec("removed", "p0", 0.7, replicate)
            seeds = {k: self.run_rung(k, spec)[2].calls[0]["seed"] for k in RUNG_KEYS}
            self.assertEqual(len(set(seeds.values())), 1, seeds)
            self.assertEqual(seeds["rung2"], derive_seed("SYN-OW01__x", spec.key()))

    def test_every_client_kwarg_but_the_messages_is_the_same_across_rungs(self):
        seen = {}
        for k in RUNG_KEYS:
            c = self.run_rung(k)[2].calls[0]
            seen[k] = {a: b for a, b in c.items() if a != "messages"}
        self.assertEqual(len({json.dumps(v, sort_keys=True) for v in seen.values()}), 1, seen)
        self.assertEqual(seen["rung3"], {"temperature": 0.7, "seed": derive_seed("SYN-OW01__x", SPEC.key()),
                                         "max_tokens": 12000, "params": None})

    def test_stage_and_item_id_of_the_logged_call(self):
        seen = {}
        orig = R.log_call

        def wrapper(log, client, messages, **k):
            seen[len(seen)] = {a: b for a, b in k.items() if a != "validate"}
            return orig(log, client, messages, **k)
        R.log_call = wrapper
        self.addCleanup(setattr, R, "log_call", orig)
        for k in RUNG_KEYS:
            self.run_rung(k)
        for i, k in enumerate(RUNG_KEYS):
            rung = ladder.get_rung(k)
            self.assertEqual(seen[i]["stage"], ladder.rung_stage(rung, SPEC).log)
            self.assertEqual(seen[i]["item_id"], ladder.rung_item_id("SYN-OW01__x", rung))
            self.assertEqual(seen[i]["max_attempts"], 3)
        self.assertEqual(len({s["stage"] for s in seen.values()}), len(RUNG_KEYS))
        self.assertEqual(len({s["item_id"] for s in seen.values()}), len(RUNG_KEYS))

    def test_graph_keeps_the_unsuffixed_map_id_and_records_the_rung(self):
        g, st, _c = self.run_rung("rung3")
        self.assertEqual((st, g.map_id, g.origin), ("ok", "SYN-OW01__x", "ai"))
        self.assertEqual({k: v for k, v in g.meta.items() if k in ("spec", "rung")}, {"spec": SPEC.key(), "rung": "rung3"})

    def test_item_id_already_carrying_a_rung_suffix_is_not_suffixed_twice(self):
        g, _st, _c = self.run_rung("rung3", item_id="SYN-OW01__x@rung3")
        self.assertEqual(g.map_id, "SYN-OW01__x")

    def test_retry_texts_and_attempts_are_the_same_as_without_a_rung(self):
        bad = json.loads(json.dumps(GOOD))
        bad["nodes"][0]["quote"] = "we got a licence"
        bad["nodes"][1]["quote"] = "one office would help"
        outs = {}
        for rung in (None, ladder.get_rung("rung2")):
            replies = iter([json.dumps(bad), json.dumps(GOOD)])
            seen = []

            def fn(m, replies=replies, seen=seen):
                seen.append(json.loads(json.dumps(m)))
                return next(replies)
            reconstruct(self.log, MockClient(fn=fn), T, SPEC, CTX, PROMPTS, item_id="x", rung=rung, real_data=False)
            outs[rung is None] = seen
        self.assertEqual(outs[True], outs[False])        # rung 2 sends exactly today's messages, retry turn included
        self.assertEqual(len(outs[True]), 2)
        self.assertIn("not verbatim", outs[True][1][-1]["content"])

    def test_too_long_is_still_excluded_without_a_call(self):
        ctx = ReconContext(CTX.domain, CTX.role, CTX.phases, max_input_words=5)

        def boom(m):
            raise AssertionError("must not call")
        g, st = reconstruct(self.log, MockClient(fn=boom), T, SPEC, ctx, PROMPTS, item_id="x",
                            rung=ladder.get_rung("rung3"), real_data=False)
        self.assertEqual((g, st), (None, "too_long_excluded"))

    def test_three_failures_mark_failed(self):
        client = RecordingClient(reply="Sure!")
        g, st = reconstruct(self.log, client, T, SPEC, CTX, PROMPTS, item_id="x", rung=ladder.get_rung("rung3"), real_data=False)
        self.assertEqual((g, st), (None, "failed"))
        self.assertEqual(len(client.calls), 3)

    def test_non_p0_spec_is_refused_before_any_call(self):
        client = RecordingClient()
        with self.assertRaises(ValueError):
            reconstruct(self.log, client, T, ReconSpec("removed", "p1", 0.0), CTX, PROMPTS, item_id="x",
                        rung=ladder.get_rung("rung3"), real_data=False)
        self.assertEqual(client.calls, [])

    def test_real_data_restricts_to_the_primary_spec(self):
        client = RecordingClient()
        rung = ladder.get_rung("rung3")
        with self.assertRaises(ValueError):
            reconstruct(self.log, client, T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x", rung=rung,
                        real_data=True)
        with self.assertRaises(ValueError):
            reconstruct(self.log, client, T, ReconSpec("removed", "p0", 0.7, 0), CTX, PROMPTS, item_id="x", rung=rung,
                        real_data=True)
        self.assertEqual(client.calls, [])
        g, st = reconstruct(self.log, client, T, ReconSpec("removed", "p0", 0.0, 0), CTX, PROMPTS, item_id="x",
                            rung=rung, real_data=True)
        self.assertEqual(st, "ok")
        # on synthetic data the same non-primary spec is allowed
        g, st = reconstruct(self.log, client, T, ReconSpec("removed", "p0", 0.7, 0), CTX, PROMPTS, item_id="x", rung=rung, real_data=False)
        self.assertEqual(st, "ok")

    def test_prior_only_arm_and_prompt_version_cannot_be_combined_with_a_rung(self):
        rung = ladder.get_rung("rung3")
        with self.assertRaises(ValueError):
            reconstruct(self.log, RecordingClient(), None, ReconSpec("prior_only", "p0", 0.7), CTX, PROMPTS,
                        item_id="x", rung=rung, real_data=False)
        with self.assertRaises(ValueError):
            reconstruct(self.log, RecordingClient(), T, SPEC, CTX, PROMPTS, item_id="x", rung=rung,
                        prompt_version="v1", real_data=False)

    def test_rung_prompts_really_differ(self):
        msgs = {k: self.run_rung(k)[2].calls[0]["messages"] for k in RUNG_KEYS}
        self.assertEqual(len({json.dumps(m) for m in msgs.values()}), len(RUNG_KEYS))


class DictWork:
    """In-memory stand-in for pipeline.Work (get/put by stage and name)."""

    def __init__(self):
        self.items = {}

    def get(self, stage, name):
        """Like pipeline.Work.get: an item counts as done only with a status of pipeline.DONE."""
        from second_scribe.pipeline import DONE
        d = self.items.get((stage, name))
        return d if isinstance(d, dict) and d.get("status") in DONE else None

    def put(self, stage, name, obj):
        self.items[(stage, name)] = json.loads(json.dumps(obj))


def transcript(pid, speaker_format="synthetic"):
    t = Transcript(pid, list(T.turns), speaker_format)
    return t


class TestRunLadderArm(Base):
    def run_arm(self, rung_key="rung3", pids=("A1", "B2"), draws=2, client=None, spec=None, **kw):
        # default: the synthetic T=0.7 setting, where several draws are independent replicates
        spec = spec or ReconSpec("removed", "p0", 0.7)
        client = client or RecordingClient()
        kw.setdefault("ctx", CTX)
        kw.setdefault("real_data", False)
        res = ladder.run_ladder_arm(ladder.get_rung(rung_key), [transcript(p) for p in pids],
                                    spec, draws, log=self.log, client=client,
                                    prompts_dir=PROMPTS, **kw)
        return res, client

    def test_one_call_per_transcript_per_draw_keyed_by_map_and_draw(self):
        res, client = self.run_arm()
        self.assertEqual(sorted(res), [("A1", 0), ("A1", 1), ("B2", 0), ("B2", 1)])
        self.assertEqual(len(client.calls), 4)
        for key, out in res.items():
            self.assertEqual(out.status, "ok")
            self.assertEqual(out.graph.map_id, f"{key[0]}__removed__p0__t0.7__r{key[1]}")   # the pipeline's naming
            self.assertEqual(out.item_id, out.graph.map_id)
            self.assertEqual(out.graph.meta["rung"], "rung3")
            self.assertEqual(out.graph.meta["spec"], f"removed__p0__t0.7__r{key[1]}")

    def test_draws_may_be_a_list_and_draw_is_the_replicate(self):
        res, client = self.run_arm(pids=("A1",), draws=[2, 0])
        self.assertEqual(sorted(res), [("A1", 0), ("A1", 2)])
        want = {derive_seed(f"A1__removed__p0__t0.7__r{d}", f"removed__p0__t0.7__r{d}") for d in (0, 2)}
        self.assertEqual({c["seed"] for c in client.calls}, want)

    def test_draw_major_order_so_the_last_draw_can_be_cut(self):
        _res, client = self.run_arm(pids=("A1", "B2"), draws=2)
        self.assertEqual([c["seed"] for c in client.calls],
                         [derive_seed(f"{p}__removed__p0__t0.7__r{d}", f"removed__p0__t0.7__r{d}")
                          for d in (0, 1) for p in ("A1", "B2")])

    def test_rung2_draw0_is_the_main_run_call(self):
        """Rung 2 through the arm sends the very messages and seed of a plain reconstruct() (the existing main run)."""
        res, client = self.run_arm("rung2", pids=("A1",), draws=1)
        plain = RecordingClient()
        reconstruct(self.log, plain, transcript("A1"), ReconSpec("removed", "p0", 0.7), CTX, PROMPTS,
                    item_id="A1__removed__p0__t0.7__r0")
        self.assertEqual(client.calls[0], plain.calls[0])

    def test_same_seed_for_the_same_map_and_draw_in_every_rung(self):
        seeds = {k: [c["seed"] for c in self.run_arm(k)[1].calls] for k in RUNG_KEYS}
        self.assertEqual(len({tuple(v) for v in seeds.values()}), 1, seeds)

    def test_gate_validate_rungs_runs_before_any_call(self):
        import shutil
        d = Path(self._tmp.name) / "prompts"
        shutil.copytree(PROMPTS, d)
        f = d / "reconstruct.p0.v2.txt"
        f.write_text(f.read_text(encoding="utf-8") + " tampered", encoding="utf-8")
        self.assertNotEqual(ladder.validate_rungs(d), [])
        client = RecordingClient()
        with self.assertRaises(RuntimeError) as cm:
            ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1")], SPEC, 1, log=self.log, client=client,
                                  ctx=CTX, prompts_dir=d, real_data=False)
        self.assertIn("reconstruct.p0.v2.txt", str(cm.exception))
        self.assertEqual(client.calls, [])
        self.assertEqual(ladder.validate_rungs(PROMPTS), [])      # and the real folder passes

    def test_real_data_refuses_a_non_primary_setting_before_any_call(self):
        client = RecordingClient()
        for spec in (ReconSpec("full", "p0", 0.0), ReconSpec("removed", "p0", 0.7), ReconSpec("removed", "p1", 0.0)):
            with self.assertRaises(ValueError):
                ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1")], spec, 1, log=self.log,
                                      client=client, ctx=CTX, prompts_dir=PROMPTS, real_data=True)
        self.assertEqual(client.calls, [])
        primary = ReconSpec("removed", "p0", 0.0)
        res, _c = self.run_arm(draws=1, pids=("A1",), client=client, spec=primary, real_data=True)   # one draw of the primary
        self.assertEqual(len(res), 1)

    def test_a_second_draw_at_temperature_0_is_refused_before_any_call(self):
        # the primary setting is greedy: draws 1.. would be copies of draw 0 (DECISIONS C7)
        primary = ReconSpec("removed", "p0", 0.0)
        for real in (True, False):
            for draws in (2, [0, 1], [1]):
                client = RecordingClient()
                with self.subTest(real_data=real, draws=draws), self.assertRaises(ValueError) as cm:
                    self.run_arm(draws=draws, pids=("A1",), client=client, spec=primary, real_data=real)
                self.assertIn("temperature 0", str(cm.exception))
                self.assertEqual(client.calls, [])

    def test_ctx_may_be_a_function_of_the_transcript(self):
        seen = []

        def ctx_for(t):
            seen.append(t.transcript_id)
            return ReconContext(CTX.domain, "role-of-" + t.transcript_id, CTX.phases)
        client = RecordingClient()
        ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1"), transcript("B2")], SPEC, 1, log=self.log,
                              client=client, ctx=ctx_for, prompts_dir=PROMPTS, real_data=False)
        self.assertEqual(seen, ["A1", "B2"])
        self.assertIn("role-of-A1", client.calls[0]["messages"][-1]["content"])
        self.assertIn("role-of-B2", client.calls[1]["messages"][-1]["content"])

    def test_work_store_resumes_and_keys_rungs_apart(self):
        work = DictWork()
        res1, c1 = self.run_arm("rung3", pids=("A1",), draws=2, work=work)
        self.assertEqual(len(c1.calls), 2)
        self.assertEqual({k[0] for k in work.items}, {"recon:rung3"})
        res2, c2 = self.run_arm("rung3", pids=("A1",), draws=2, work=work)
        self.assertEqual(c2.calls, [])                                    # resumed from the store, no new call
        self.assertEqual({k: v.graph for k, v in res2.items()}, {k: v.graph for k, v in res1.items()})
        res3, c3 = self.run_arm("placebo", pids=("A1",), draws=2, work=work)
        self.assertEqual(len(c3.calls), 2)                                # another rung does not hit rung 3's items
        self.assertEqual({k[0] for k in work.items}, {"recon:rung3", "recon:placebo"})

    def test_failed_and_too_long_are_stored_and_returned_as_none(self):
        work = DictWork()
        res, c = self.run_arm(pids=("A1",), draws=1, client=RecordingClient(reply="Sure!"), work=work)
        self.assertEqual((res[("A1", 0)].graph, res[("A1", 0)].status), (None, "failed"))
        res2, c2 = self.run_arm(pids=("A1",), draws=1, work=work)
        self.assertEqual(c2.calls, [])                                    # a failure is done (as in the pipeline)
        self.assertEqual((res2[("A1", 0)].graph, res2[("A1", 0)].status), (None, "failed"))   # and stays failed
        ctx = ReconContext(CTX.domain, CTX.role, CTX.phases, max_input_words=5)
        res, c = self.run_arm("rung2", pids=("A1",), draws=1, ctx=ctx)
        self.assertEqual(res[("A1", 0)].status, "too_long_excluded")
        self.assertEqual(c.calls, [])

    def test_removed_arm_without_speakers_is_excluded_without_a_call(self):
        client = RecordingClient()
        res = ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1", speaker_format="none")], SPEC, 1,
                                    log=self.log, client=client, ctx=CTX, prompts_dir=PROMPTS, real_data=False)
        self.assertEqual(res[("A1", 0)].status, "no_speakers_excluded")
        self.assertIsNone(res[("A1", 0)].graph)
        self.assertEqual(client.calls, [])

    def test_audit_inputs_feed_audit_rungs(self):
        res, _c = self.run_arm(pids=("A1", "B2"), draws=1)
        aud = ladder.audit_inputs(res, {"A1": T.text("removed"), "B2": T.text("removed")})
        self.assertEqual(sorted(aud), ["A1#0", "B2#0"])
        summary = ladder.audit_rungs({"rung3": aud})
        self.assertEqual(summary["rung3"]["n_outputs"], 2)
        self.assertEqual(summary["rung3"]["n_missing"], 0)
        failed, _c = self.run_arm(pids=("A1",), draws=1, client=RecordingClient(reply="no"))
        self.assertEqual(ladder.audit_rungs({"r": ladder.audit_inputs(failed, {})})["r"]["n_missing"], 1)


def graph_with_phases(map_id, phases):
    from second_scribe.schemas import MapGraph, Node
    return MapGraph(map_id, "ai", [Node(f"n{i}", f"label {i}", "service", ph, "experienced", "q")
                                   for i, ph in enumerate(phases)])


class TestNullPhaseShare(unittest.TestCase):
    """Review M5: models may imitate the demos' null phases; report the share per rung (descriptive)."""

    def test_share_counts_nodes_with_phase_none(self):
        out = {"a": graph_with_phases("a", [None, "Permits", None, "Planning"]),
               "b": (graph_with_phases("b", ["Permits", "Permits"]), "source text"),
               "c": None}
        r = ladder.null_phase_share(out)
        self.assertEqual((r["n_maps"], r["n_missing"], r["n_nodes"], r["n_null_phase"]), (2, 1, 6, 2))
        self.assertAlmostEqual(r["share"], 2 / 6)
        self.assertEqual(r["n_maps_with_null"], 1)
        self.assertEqual(r["by_map"], {"a": 0.5, "b": 0.0})

    def test_empty_phase_string_counts_as_null_and_nothing_to_count_is_none_not_zero(self):
        self.assertEqual(ladder.null_phase_share({"a": graph_with_phases("a", [""])})["n_null_phase"], 1)
        r = ladder.null_phase_share({"a": None})
        self.assertIsNone(r["share"])
        self.assertEqual((r["n_nodes"], r["n_missing"]), (0, 1))
        self.assertIsNone(ladder.null_phase_share({})["share"])

    def test_per_rung_and_json_safe(self):
        res = ladder.null_phase_by_rung({"rung2": {"a": graph_with_phases("a", ["Permits", None])},
                                         "rung3": {"a": graph_with_phases("a", [None, None])}})
        self.assertEqual({k: v["share"] for k, v in res.items()}, {"rung2": 0.5, "rung3": 1.0})
        json.dumps(res)

    def test_takes_the_audit_inputs_of_an_arm(self):
        d = tempfile.TemporaryDirectory(prefix="ss-wire-")
        self.addCleanup(d.cleanup)
        log = AuditLog(Path(d.name) / "r.jsonl", "r", clock=lambda: "t")
        res = ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1")], SPEC, 1, log=log,
                                    client=RecordingClient(), ctx=CTX, prompts_dir=PROMPTS, real_data=False)
        r = ladder.null_phase_share(ladder.audit_inputs(res))
        self.assertEqual((r["n_nodes"], r["n_null_phase"]), (2, 1))       # GOOD has one null phase out of two


class TestAnonymityAndRetries(Base):
    def test_anonymous_ids_are_positional_stable_and_never_contain_the_pid(self):
        m = ladder.anonymous_ids(["KC12", "AB03", "KC12", "ZZ99"])
        self.assertEqual(m, {"AB03": "#0", "KC12": "#1", "ZZ99": "#2"})
        self.assertEqual(ladder.anonymous_ids(["ZZ99", "KC12", "AB03"]), m)       # input order does not matter

    def test_audit_inputs_can_anonymize_the_keys(self):
        res = ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("KC12"), transcript("AB03")], SPEC, 2,
                                    log=self.log, client=RecordingClient(), ctx=CTX, prompts_dir=PROMPTS, real_data=False)
        plain = ladder.audit_inputs(res)
        anon = ladder.audit_inputs(res, anonymize=ladder.anonymous_ids(p for p, _d in res))
        self.assertEqual(sorted(plain), ["AB03#0", "AB03#1", "KC12#0", "KC12#1"])
        self.assertEqual(sorted(anon), ["#0#0", "#0#1", "#1#0", "#1#1"])
        summary = ladder.audit_rungs({"rung3": anon})["rung3"]
        self.assertNotIn("KC12", json.dumps(summary))
        self.assertNotIn("AB03", json.dumps(summary))

    def test_retries_by_item_counts_corrective_retries_per_logged_item(self):
        bad = json.loads(json.dumps(GOOD))
        bad["nodes"][0]["quote"] = "we got a licence"
        bad["nodes"][1]["quote"] = "one office would help"
        order = iter([json.dumps(bad), json.dumps(bad), json.dumps(GOOD), json.dumps(GOOD)])
        res = ladder.run_ladder_arm(ladder.get_rung("rung3"), [transcript("A1"), transcript("B2")], SPEC, 1,
                                    log=self.log, client=MockClient(fn=lambda m: next(order)), ctx=CTX,
                                    prompts_dir=PROMPTS, real_data=False)
        self.assertEqual({k: v.status for k, v in res.items()}, {("A1", 0): "ok", ("B2", 0): "ok"})
        r = ladder.retries_by_item(self.log.path)
        a = ladder.rung_item_id("A1__removed__p0__t0.7__r0", ladder.get_rung("rung3"))
        b = ladder.rung_item_id("B2__removed__p0__t0.7__r0", ladder.get_rung("rung3"))
        self.assertEqual((r[a], r[b]), (2, 0))
        # per arm result, by the unsuffixed name the arm returns
        self.assertEqual(ladder.arm_retries(self.log.path, res, ladder.get_rung("rung3")), {("A1", 0): 2, ("B2", 0): 0})

    def test_retries_of_a_missing_log_is_empty(self):
        self.assertEqual(ladder.retries_by_item(Path(self._tmp.name) / "nope.jsonl"), {})


class DownClient:
    """Fake client whose endpoint is down: every call raises (the HTTP client already gave up after its own tries)."""

    def __init__(self):
        self.calls = 0

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.calls += 1
        raise ConnectionError("endpoint down")


class TestOutageIsNotAResult(Base):
    """I-1: a call that never got a reply is an error to redo, not a final 'failed' map."""

    RUNG = ladder.get_rung("rung3")

    def test_reconstruct_with_a_rung_reports_error_when_no_attempt_got_a_response(self):
        down = DownClient()
        g, st = reconstruct(self.log, down, T, SPEC, CTX, PROMPTS, item_id="A1__x", rung=self.RUNG, real_data=False)
        self.assertEqual((g, st), (None, "error"))
        self.assertEqual(down.calls, 3)

    def test_invalid_replies_are_still_failed_and_so_is_a_mix(self):
        g, st = reconstruct(self.log, RecordingClient(reply="Sure!"), T, SPEC, CTX, PROMPTS, item_id="A1__x",
                            rung=self.RUNG, real_data=False)
        self.assertEqual((g, st), (None, "failed"))
        seq = iter([None, "Sure!", None])

        class Mixed:
            def complete(self_, messages, *, temperature, seed, max_tokens, params=None):
                r = next(seq)
                if r is None:
                    raise ConnectionError("blip")
                return ModelResponse(r, "stop", {}, 0, "x")
        g, st = reconstruct(self.log, Mixed(), T, SPEC, CTX, PROMPTS, item_id="A2__x", rung=self.RUNG, real_data=False)
        self.assertEqual((g, st), (None, "failed"))      # a reply was seen and failed validation

    def test_a_refusal_that_is_not_an_outage_is_failed_and_stored_not_error(self):
        """I-1: HTTP 400/401, a malformed body or an unloggable reply is no outage (log_call sets ``transport`` only for
        transport errors): the map is a final 'failed', stored, so the 'rerun until empty' loop ends."""
        class Refuses:
            calls = 0

            def complete(self_, messages, *, temperature, seed, max_tokens, params=None):
                self_.calls += 1
                raise RuntimeError("model call to 127.0.0.1:1 failed: HTTP 400: the request exceeds the available "
                                   "context size")
        work = DictWork()
        refuses = Refuses()
        res = ladder.run_ladder_arm(self.RUNG, [transcript("A1")], ReconSpec("removed", "p0", 0.0), 1, log=self.log,
                                    client=refuses, ctx=CTX, prompts_dir=PROMPTS, work=work, real_data=False)
        self.assertEqual(res[("A1", 0)].status, "failed")
        self.assertEqual(ladder.error_items(res), [])
        self.assertEqual(work.items[("recon:rung3", "A1__removed__p0__t0__r0")]["status"], "failed")
        ladder.run_ladder_arm(self.RUNG, [transcript("A1")], ReconSpec("removed", "p0", 0.0), 1, log=self.log,
                              client=refuses, ctx=CTX, prompts_dir=PROMPTS, work=work, real_data=False)
        self.assertEqual(refuses.calls, 3)            # the rerun made no call

    def test_the_last_attempt_decides_an_outage_after_a_non_transport_error(self):
        """The rule is _Tap.transport_failed_calls': error only when the LAST attempt was a transport error and no
        attempt got a reply. A refusal first and an outage last is an outage; an outage first and a refusal last is not."""
        def run(excs, pid):
            seq = iter(excs)

            class C:
                def complete(self_, messages, *, temperature, seed, max_tokens, params=None):
                    raise next(seq)
            return reconstruct(self.log, C(), T, SPEC, CTX, PROMPTS, item_id=pid, rung=self.RUNG, real_data=False)[1]
        self.assertEqual(run([RuntimeError("HTTP 400"), RuntimeError("HTTP 400"), ConnectionError("down")], "A1__x"),
                         "error")
        self.assertEqual(run([ConnectionError("down"), ConnectionError("down"), RuntimeError("HTTP 401")], "A2__x"),
                         "failed")

    def test_without_a_rung_the_status_is_unchanged(self):
        g, st = reconstruct(self.log, DownClient(), T, SPEC, CTX, PROMPTS, item_id="A1__x")
        self.assertEqual((g, st), (None, "failed"))

    def test_error_is_returned_not_stored_and_a_resume_redoes_it(self):
        work = DictWork()
        down = DownClient()
        res = ladder.run_ladder_arm(self.RUNG, [transcript("A1")], ReconSpec("removed", "p0", 0.0), 1, log=self.log,
                                    client=down, ctx=CTX, prompts_dir=PROMPTS, work=work, real_data=False)
        self.assertEqual((res[("A1", 0)].graph, res[("A1", 0)].status), (None, "error"))
        self.assertEqual(work.items, {})
        good = RecordingClient()
        res2 = ladder.run_ladder_arm(self.RUNG, [transcript("A1")], ReconSpec("removed", "p0", 0.0), 1, log=self.log,
                                     client=good, ctx=CTX, prompts_dir=PROMPTS, work=work, real_data=False)
        self.assertEqual(len(good.calls), 1)
        self.assertEqual(res2[("A1", 0)].status, "ok")
        self.assertEqual(work.items[("recon:rung3", "A1__removed__p0__t0__r0")]["status"], "ok")

    def test_error_items_are_listed_for_the_caller(self):
        res = ladder.run_ladder_arm(self.RUNG, [transcript("A1"), transcript("B2")], ReconSpec("removed", "p0", 0.0),
                                    1, log=self.log, client=DownClient(), ctx=CTX, prompts_dir=PROMPTS,
                                    real_data=False)
        self.assertEqual(ladder.error_items(res), [("A1", 0), ("B2", 0)])
        self.assertEqual(ladder.error_items({("A1", 0): ladder.ArmResult(None, "failed", "x")}), [])

    def test_real_work_store_does_not_carry_an_errored_call_into_the_next_item(self):
        from second_scribe import pipeline, prereg
        out = Path(self._tmp.name) / "out"
        tracker = prereg.RequestTracker(self.log)
        work = pipeline.Work(out, tracker=tracker)
        seq = {"n": 0}

        class DownThenUp:
            def complete(self_, messages, *, temperature, seed, max_tokens, params=None):
                seq["n"] += 1
                if seq["n"] <= 3:                  # the first item (A1): three transport errors
                    raise ConnectionError("down")
                return ModelResponse(json.dumps(GOOD), "stop", {}, 0, "x")
        res = ladder.run_ladder_arm(self.RUNG, [transcript("A1"), transcript("B2")], ReconSpec("removed", "p0", 0.0),
                                    1, log=tracker, client=DownThenUp(), ctx=CTX, prompts_dir=PROMPTS, work=work,
                                    real_data=False)
        self.assertEqual({k: v.status for k, v in res.items()}, {("A1", 0): "error", ("B2", 0): "ok"})
        self.assertIsNone(work.read_any("recon:rung3", "A1__removed__p0__t0__r0"))
        b2 = work.read_any("recon:rung3", "B2__removed__p0__t0__r0")
        self.assertEqual(len(b2["request_ids"]), 1)
        self.assertIn("B2__removed__p0__t0__r0@rung3", b2["request_ids"][0])


class TestPromptIdentityOfItems(Base):
    """I-2 / M3: a stored item records which prompt files made it; after an edit it is redone, never mixed in."""

    def copy_prompts(self):
        import shutil
        d = Path(self._tmp.name) / "prompts"
        shutil.copytree(PROMPTS, d)
        return d

    def edit_remedy(self, d, extra=" (edited)"):
        from second_scribe import prompt_manifest
        f = d / "reconstruct.p0.remedy.v1.txt"
        f.write_text(f.read_text(encoding="utf-8") + extra, encoding="utf-8")
        prompt_manifest.write_manifest(d)
        self.assertEqual(ladder.validate_rungs(d), [])         # the pre-run gate does not notice an edited remedy

    def run_arm(self, d, client, work, rung_key="rung3", pids=("A1", "B2")):
        return ladder.run_ladder_arm(ladder.get_rung(rung_key), [transcript(p) for p in pids],
                                     ReconSpec("removed", "p0", 0.0), 1, log=self.log, client=client, ctx=CTX,
                                     prompts_dir=d, work=work, real_data=False)

    def test_items_record_the_identity_of_their_prompt_files(self):
        work = DictWork()
        self.run_arm(PROMPTS, RecordingClient(), work)
        ident = ladder.rung_prompt_sha256(PROMPTS, ladder.get_rung("rung3"))
        self.assertEqual(len(ident), 64)
        for item in work.items.values():
            self.assertEqual(item["prompt_sha256"], ident)
        # different rungs have different identities; same files give the same one
        self.assertEqual(len({ladder.rung_prompt_sha256(PROMPTS, r) for r in ladder.RUNGS}), len(ladder.RUNGS))
        self.assertEqual(ident, ladder.rung_prompt_sha256(PROMPTS, ladder.get_rung("rung3")))

    def test_an_item_made_with_an_older_prompt_is_redone_after_an_edit(self):
        d = self.copy_prompts()
        work = DictWork()
        c1 = RecordingClient()
        self.run_arm(d, c1, work)
        self.assertEqual(len(c1.calls), 2)
        c_same = RecordingClient()
        self.run_arm(d, c_same, work)
        self.assertEqual(c_same.calls, [])                       # nothing edited: resumes as before
        self.edit_remedy(d)
        c2 = RecordingClient()
        res = self.run_arm(d, c2, work)
        self.assertEqual(len(c2.calls), 2)                       # both stale items recomputed with the new prompt
        self.assertIn("(edited)", c2.calls[0]["messages"][-1]["content"])
        new_ident = ladder.rung_prompt_sha256(d, ladder.get_rung("rung3"))
        self.assertTrue(all(i["prompt_sha256"] == new_ident for i in work.items.values()))
        self.assertEqual({v.status for v in res.values()}, {"ok"})

    def test_only_the_stale_items_are_redone(self):
        d = self.copy_prompts()
        work = DictWork()
        self.run_arm(d, RecordingClient(), work, pids=("A1",))
        self.edit_remedy(d)
        c = RecordingClient()
        self.run_arm(d, c, work, pids=("A1", "B2"))
        self.assertEqual(len(c.calls), 2)
        c = RecordingClient()
        self.run_arm(d, c, work, pids=("A1", "B2"))
        self.assertEqual(c.calls, [])

    def test_an_item_with_no_recorded_identity_is_redone(self):
        work = DictWork()
        work.put("recon:rung3", "A1__removed__p0__t0__r0", {"status": "ok", "map": asdict_of_good()})
        c = RecordingClient()
        self.run_arm(PROMPTS, c, work, pids=("A1",))
        self.assertEqual(len(c.calls), 1)

    def test_a_failed_item_of_an_old_prompt_is_redone_too(self):
        d = self.copy_prompts()
        work = DictWork()
        self.run_arm(d, RecordingClient(reply="Sure!"), work, pids=("A1",))
        self.edit_remedy(d)
        c = RecordingClient()
        res = self.run_arm(d, c, work, pids=("A1",))
        self.assertEqual((len(c.calls), res[("A1", 0)].status), (1, "ok"))


def asdict_of_good():
    from dataclasses import asdict
    return asdict(R.parse_recon(json.dumps(GOOD), "A1__removed__p0__t0__r0", "ai"))


class TestCorrectiveRetriesVersusTransportErrors(Base):
    """M-2 / M4: a retry is corrective only when the previous attempt got a reply that failed validation."""

    RUNG = ladder.get_rung("rung3")

    def one_item(self, client):
        ladder.run_ladder_arm(self.RUNG, [transcript("A1")], ReconSpec("removed", "p0", 0.0), 1, log=self.log,
                              client=client, ctx=CTX, prompts_dir=PROMPTS, real_data=False)
        return ("recon:removed:rung3", "A1__removed__p0__t0__r0@rung3")

    def test_an_outage_is_transport_errors_not_corrective_retries(self):
        key = self.one_item(DownClient())
        self.assertEqual(ladder.retries_by_call(self.log.path)[key], 0)
        self.assertEqual(ladder.call_errors_by_call(self.log.path)[key], 3)
        self.assertEqual(ladder.retries_by_item(self.log.path)[key[1]], 0)

    def test_only_attempts_after_an_invalid_reply_count(self):
        bad = json.dumps({"nodes": [], "edges": []})
        seq = iter([None, bad, json.dumps(GOOD)])

        class Client:
            def complete(self_, messages, *, temperature, seed, max_tokens, params=None):
                r = next(seq)
                if r is None:
                    raise ConnectionError("blip")
                return ModelResponse(r, "stop", {}, 0, "x")
        key = self.one_item(Client())
        # attempt 1: transport error; attempt 2: invalid reply (not a retry of a reply); attempt 3: retry after it
        self.assertEqual(ladder.retries_by_call(self.log.path)[key], 1)
        self.assertEqual(ladder.call_errors_by_call(self.log.path)[key], 1)

    def test_a_killed_session_with_only_transport_errors_does_not_inflate_a_resumed_item(self):
        name = "A1__removed__p0__t0__r0@rung3"
        stage = "recon:removed:rung3"
        for a in (1, 2):         # session 1: two attempts, no reply, then killed
            self.log.append("call_request", {"request_id": f"{stage}:{name}:{a}:dead{a}", "stage": stage,
                                             "item_id": name, "attempt": a})
            self.log.append("call_error", {"request_id": f"{stage}:{name}:{a}:dead{a}", "error": "x"})
        self.one_item(RecordingClient())        # session 2: one clean attempt
        self.assertEqual(ladder.retries_by_call(self.log.path)[(stage, name)], 0)
        self.assertEqual(ladder.call_errors_by_call(self.log.path)[(stage, name)], 2)

    def test_three_invalid_replies_are_two_corrective_retries(self):
        key = self.one_item(RecordingClient(reply="Sure!"))
        self.assertEqual(ladder.retries_by_call(self.log.path)[key], 2)
        self.assertEqual(ladder.call_errors_by_call(self.log.path).get(key, 0), 0)


class TestRealDataGuardIsFailClosed(Base):
    """M-3 / M1 / M2: real_data must be said explicitly, and on real data a draw is one of the declared 3."""

    RUNG = ladder.get_rung("rung3")

    def arm(self, spec=SPEC, draws=1, client=None, **kw):
        return ladder.run_ladder_arm(self.RUNG, [transcript("A1")], spec, draws, log=self.log,
                                     client=client or RecordingClient(), ctx=CTX, prompts_dir=PROMPTS, **kw)

    def test_run_ladder_arm_requires_real_data(self):
        client = RecordingClient()
        with self.assertRaises(TypeError):
            self.arm(client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self.arm(real_data=False)), 1)

    def test_reconstruct_with_a_rung_requires_real_data_and_without_a_rung_does_not(self):
        client = RecordingClient()
        with self.assertRaises(TypeError):
            reconstruct(self.log, client, T, SPEC, CTX, PROMPTS, item_id="A1__x", rung=self.RUNG)
        self.assertEqual(client.calls, [])
        g, st = reconstruct(self.log, client, T, SPEC, CTX, PROMPTS, item_id="A1__x")
        self.assertEqual(st, "ok")

    def test_real_data_accepts_only_the_declared_draws(self):
        # the primary setting is T=0 (greedy), so the one declared draw is r0; later draws would be copies (C7)
        primary = ReconSpec("removed", "p0", 0.0)
        ladder.check_rung_spec(self.RUNG, ReconSpec("removed", "p0", 0.0, 0), real_data=True)
        for r in (1, 2, 3, 7, 57, -1):
            with self.subTest(replicate=r), self.assertRaises(ValueError):
                ladder.check_rung_spec(self.RUNG, ReconSpec("removed", "p0", 0.0, r), real_data=True)
        with self.assertRaises(ValueError):                   # a larger declared count does not make a copy a draw
            ladder.check_rung_spec(self.RUNG, ReconSpec("removed", "p0", 0.0, 4), real_data=True, max_draws=5)
        with self.assertRaises(ValueError):
            ladder.check_rung_spec(self.RUNG, ReconSpec("removed", "p0", 0.0, 57))        # synthetic T=0: also a copy
        ladder.check_rung_spec(self.RUNG, ReconSpec("removed", "p0", 0.7, 57))            # synthetic T>0: any replicate
        client = RecordingClient()
        for draws in (2, 4, [0, 1, 3], [5], [1]):
            with self.subTest(draws=draws), self.assertRaises(ValueError):
                self.arm(primary, draws, client=client, real_data=True)
        with self.assertRaises(ValueError):
            reconstruct(self.log, client, T, ReconSpec("removed", "p0", 0.0, 7), CTX, PROMPTS, item_id="A1__x",
                        rung=self.RUNG, real_data=True)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self.arm(primary, 1, client=client, real_data=True)), 1)
        self.assertEqual(len(self.arm(primary, [0], client=client, real_data=True)), 1)

    def test_the_declared_draw_count_can_be_passed_on(self):
        primary = ReconSpec("removed", "p0", 0.0)
        self.assertEqual(len(self.arm(primary, 1, real_data=True, max_draws=1)), 1)
        client = RecordingClient()
        with self.assertRaises(ValueError):                    # a declared count of 0 leaves no draw, even draw 0
            self.arm(primary, 1, client=client, real_data=True, max_draws=0)
        self.assertEqual(client.calls, [])


class TestDrawIdentity(unittest.TestCase):
    """I1: at temperature 0 the draws of a map may be (nearly) identical; the report must be able to say how often."""

    def graph(self, pid, d, label="Business licence"):
        g = json.loads(json.dumps(GOOD))
        g["nodes"][0]["label"] = label
        m = R.parse_recon(json.dumps(g), f"{pid}__removed__p0__t0__r{d}", "ai", spec_key=f"removed__p0__t0__r{d}")
        m.meta["rung"] = "rung3"
        return m

    def res(self, spec):
        """spec: {(pid, draw): label | None (failed)}"""
        out = {}
        for (pid, d), label in spec.items():
            out[(pid, d)] = ladder.ArmResult(None if label is None else self.graph(pid, d, label),
                                             "failed" if label is None else "ok", f"{pid}__removed__p0__t0__r{d}")
        return out

    def test_identical_draws_are_counted_even_though_map_id_and_meta_differ(self):
        r = ladder.draw_identity(self.res({("A", 0): "x", ("A", 1): "x", ("A", 2): "x", ("B", 0): "y", ("B", 1): "z",
                                           ("B", 2): "y"}))
        self.assertEqual((r["n_pairs"], r["n_identical"]), (4, 3))
        self.assertAlmostEqual(r["share_identical"], 0.75)
        self.assertEqual(r["n_maps_all_identical"], 1)
        self.assertEqual(r["n_maps"], 2)
        self.assertEqual(r["by_draw"], {1: {"n_pairs": 2, "n_identical": 1}, 2: {"n_pairs": 2, "n_identical": 2}})

    def test_failed_draws_and_a_missing_draw_zero_are_skipped_and_one_draw_has_no_share(self):
        r = ladder.draw_identity(self.res({("A", 0): "x", ("A", 1): None, ("B", 1): "x", ("B", 2): "x", ("C", 0): "x"}))
        self.assertEqual((r["n_pairs"], r["n_identical"]), (0, 0))
        self.assertIsNone(r["share_identical"])
        self.assertEqual(r["n_maps_all_identical"], 0)

    def test_per_rung_and_json_safe_and_no_participant_id(self):
        by = ladder.draw_identity_by_rung({"rung3": self.res({("KC12", 0): "x", ("KC12", 1): "x"}),
                                           "placebo": self.res({("KC12", 0): "x", ("KC12", 1): "w"})})
        self.assertEqual((by["rung3"]["share_identical"], by["placebo"]["share_identical"]), (1.0, 0.0))
        self.assertNotIn("KC12", json.dumps(by))


class TestResumeOnTheRealWorkStore(Base):
    """M-4: the resume path on pipeline.Work (statuses outside DONE are redone there), statuses asserted."""

    def arm(self, work, client, pids, ctx=CTX, rung="rung3"):
        return ladder.run_ladder_arm(ladder.get_rung(rung), [transcript(p) for p in pids], ReconSpec("removed", "p0", 0.0),
                                     1, log=self.log, client=client, ctx=ctx, prompts_dir=PROMPTS, work=work,
                                     real_data=False)

    def test_statuses_and_maps_survive_a_resume(self):
        from second_scribe import pipeline
        work = pipeline.Work(Path(self._tmp.name) / "out")
        long_ctx = ReconContext(CTX.domain, CTX.role, CTX.phases, max_input_words=5)
        r_ok = self.arm(work, RecordingClient(), ["A1"])
        r_failed = self.arm(work, RecordingClient(reply="Sure!"), ["B2"])
        r_long = self.arm(work, RecordingClient(), ["C3"], ctx=long_ctx)
        self.assertEqual([r_ok[("A1", 0)].status, r_failed[("B2", 0)].status, r_long[("C3", 0)].status],
                         ["ok", "failed", "too_long_excluded"])
        client = RecordingClient()
        res = self.arm(work, client, ["A1", "B2", "C3"], ctx=long_ctx)
        self.assertEqual(client.calls, [])
        self.assertEqual({k[0]: v.status for k, v in res.items()}, {"A1": "ok", "B2": "failed", "C3": "too_long_excluded"})
        self.assertEqual(res[("A1", 0)].graph, r_ok[("A1", 0)].graph)
        self.assertIsNone(res[("B2", 0)].graph)

    def test_an_item_with_a_status_outside_done_is_redone_on_the_real_store(self):
        from second_scribe import pipeline
        work = pipeline.Work(Path(self._tmp.name) / "out")
        f = work.path("recon:rung3", "A1__removed__p0__t0__r0")
        f.parent.mkdir(parents=True)
        pipeline.write_json_atomic(f, {"status": "okk", "map": None})
        client = RecordingClient()
        res = self.arm(work, client, ["A1"])
        self.assertEqual((len(client.calls), res[("A1", 0)].status), (1, "ok"))


class TestArmNames(unittest.TestCase):
    """The registry calls the remedy rung "rung3"; ladder_metrics calls that arm "remedy". Passing the registry key to
    ladder_report silently skips the remedy contrast ("arm remedy not run")."""

    def test_every_registry_rung_has_a_metrics_arm_and_the_primary_contrasts_are_covered(self):
        from second_scribe import ladder_metrics as LM
        self.assertEqual(set(ladder.ARM_OF_RUNG), set(RUNG_KEYS))
        self.assertEqual(len(set(ladder.ARM_OF_RUNG.values())), len(RUNG_KEYS))
        self.assertEqual({a for pair in LM.PRIMARY_CONTRASTS for a in pair}, set(ladder.ARM_OF_RUNG.values()))

    def test_metrics_arms_renames_the_keys_and_ladder_report_then_runs_the_remedy_contrast(self):
        from second_scribe import ladder_metrics as LM

        def draws(state):
            return [LM.MapDraw(f"m{i}", 0, (LM.Item("a", "wished", state if i % 2 else "missed"),
                                            LM.Item("b", "experienced", "experienced")), n_control=1)
                    for i in range(4)]
        by_rung = {"rung1": draws("experienced"), "rung2": draws("experienced"),
                   "rung3": draws("wished"), "placebo": draws("experienced")}
        wrong = LM.ladder_report(by_rung, n_perm=50, B=20)
        self.assertIn({"contrast": "remedy-placebo", "reason": "arm remedy not run"}, wrong["skipped"])
        right = LM.ladder_report(ladder.metrics_arms(by_rung), n_perm=50, B=20)
        self.assertEqual(right["skipped"], [])
        self.assertEqual(set(right["rungs"]), {"rung1", "rung2", "remedy", "placebo"})

    def test_metrics_arms_rejects_an_unknown_rung_key(self):
        with self.assertRaises(KeyError):
            ladder.metrics_arms({"rung9": []})


if __name__ == "__main__":
    unittest.main()
