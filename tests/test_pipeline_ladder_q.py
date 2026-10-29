"""Wire-b item 3: the Q phase runs the instruction ladder (``_ladder_recon`` at the end of ``_stage_recon``). Synthetic
demo corpus, the oracle behind a recording wrapper (no model), temp dirs only."""
import dataclasses
import json
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe import ladder, ladder_metrics, pipeline, prereg
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.llm import ModelResponse
from second_scribe.pipeline import RunConfig, run

N_PEOPLE = 12
ALL = ["rung2", "rung3", "placebo", "rung1"]


class Named:
    """An offline client that names its model, as a real run's client does (a real run wants a model id on every call)."""
    cfg = types.SimpleNamespace(model_id="alpha-qwen-1", api_key_env=None, params={}, endpoint=None)

    def __init__(self, inner):
        self.inner = inner

    def complete(self, messages, **kw):
        return self.inner.complete(messages, **kw)


class Counting:
    """The oracle, counting and (optionally) failing calls. ``fail(messages)`` -> an exception to raise, or None."""
    model_label = "oracle"

    def __init__(self, inner, fail=None):
        self.inner, self.fail, self.calls = inner, fail, 0

    def complete(self, messages, **kw):
        self.calls += 1
        if self.fail and (exc := self.fail(messages)):
            raise exc
        return self.inner.complete(messages, **kw)


def records(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-ladq-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "out"

    def cfg(self, ladder_cfg="default", **kw) -> RunConfig:
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["base_dir"] = str(DEMO_CONFIG.parent)
        d["out_dir"] = str(self.out)
        d["ladder"] = {"rungs": ALL, "draws": 2, "n_perm": 50, "B": 50} if ladder_cfg == "default" else ladder_cfg
        if not d["ladder"]:
            d.pop("ladder")
        elif d["ladder"]["draws"] > 1:
            # DECISIONS C7: extra draws at T=0 are copies, so a synthetic multi-draw ladder samples its primary (p0) spec
            d["specs"] = [dict(d["specs"][0], temperature=0.7)] + d["specs"][1:]
        d.update(kw)
        return RunConfig(**d)

    def go(self, cfg, stages=("Q",), fail=None):
        clients = build_clients("oracle", cfg)
        counter = Counting(clients["recon"], fail)
        clients = {**clients, "recon": counter}
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = self.out / "logs" / "run.jsonl"
        before = len(records(self.log_path)) if self.log_path.exists() else 0
        log = AuditLog(self.log_path, cfg.run_id)
        try:
            res = run(cfg, list(stages), clients, log=log)
        finally:
            log.close()
        # calls the run LOGGED (the post-stage canary talks to the client without a log_call, so counter.calls is higher)
        self.logged_calls = sum(r["record_type"] == "call_request" for r in records(self.log_path)[before:])
        return res, counter

    def store(self, rung):
        d = self.out / "work" / f"recon:{rung}"
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def call_sequence(self):
        """[(rung key, draw)] in the order the ladder calls first reached the model (retries collapsed)."""
        seq = []
        for r in records(self.log_path):
            if r["record_type"] != "call_request":
                continue
            p = r["payload"]
            stage = p["stage"]
            if stage.startswith("recon:") and stage.count(":") == 2:
                rung = stage.rsplit(":", 1)[1]
                draw = int(p["item_id"].split("__r")[-1].split("@")[0])
                if not seq or seq[-1] != (rung, draw):
                    seq.append((rung, draw))
        return seq


class TestNoLadderNoChange(Base):
    def test_a_config_without_a_ladder_makes_no_ladder_item_or_call(self):
        res, _ = self.go(self.cfg(ladder_cfg={}))
        self.assertEqual([p.name for p in (self.out / "work").iterdir() if p.is_dir() and ":" in p.name], [])
        stages = {r["payload"]["stage"] for r in records(self.log_path) if r["record_type"] == "call_request"}
        self.assertFalse([s for s in stages if s.count(":") == 2])


class TestLadderRuns(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def run_full(self):
        return self.go(self.cfg())

    def test_every_rung_has_its_items_and_draw_zero_of_rung2_is_the_main_run(self):
        res, _ = self.run_full()
        for rung in ALL:
            with self.subTest(rung=rung):
                names = self.store(rung)
                self.assertEqual(len(names), N_PEOPLE * 2)
                self.assertEqual(sum(n.endswith("__r0") for n in names), N_PEOPLE)
        reused = [json.loads((self.out / "work" / "recon:rung2" / f"{n}.json").read_text())
                  for n in self.store("rung2")]
        self.assertEqual(sum("reused_from" in x for x in reused), N_PEOPLE)      # draw 0 only
        self.assertTrue(all(n.endswith("__r0") for n, x in zip(self.store("rung2"), reused) if "reused_from" in x))
        self.assertEqual(res["new_items"]["recon"] > 0, True)

    def test_order_is_rung2_then_remedy_and_placebo_interleaved_by_draw_then_rung1(self):
        self.run_full()
        self.assertEqual(self.call_sequence(),
                         [("rung2", 1), ("rung3", 0), ("placebo", 0), ("rung3", 1), ("placebo", 1),
                          ("rung1", 0), ("rung1", 1)])

    def test_resume_makes_no_call_and_no_new_item(self):
        self.run_full()
        res, _ = self.go(self.cfg())
        self.assertEqual(self.logged_calls, 0)
        self.assertEqual(res["new_items"].get("recon"), 0)

    def test_only_the_configured_rungs_run_and_the_log_stays_valid(self):
        self.go(self.cfg(ladder_cfg={"rungs": ["rung2", "rung1"], "draws": 3, "n_perm": 50, "B": 50}))
        self.assertEqual(self.store("rung3"), [])
        self.assertEqual(self.store("placebo"), [])
        self.assertEqual(len(self.store("rung1")), N_PEOPLE * 3)
        self.assertEqual(len(self.store("rung2")), N_PEOPLE * 3)

    def test_the_log_verifies(self):
        self.run_full()
        self.assertEqual(audit_verify.main([str(self.log_path)]), 0)
        self.assertIsNone(audit_verify.work_integrity_problem(self.log_path, self.out / "work"))


class TestDeclarations(Base):
    def declared(self):
        return [r for r in records(self.log_path) if r["record_type"] == "declared_contrasts"]

    def test_one_declaration_per_rung_before_its_first_call_and_none_again_on_resume(self):
        self.go(self.cfg())
        recs = records(self.log_path)
        decl = self.declared()
        self.assertEqual([d["payload"]["rung"] for d in decl], ["rung2", "rung3", "rung1"])    # 3 + P once, as rung3
        first_call = {}
        for r in recs:
            if r["record_type"] == "call_request" and r["payload"]["stage"].count(":") == 2:
                first_call.setdefault(r["payload"]["stage"].rsplit(":", 1)[1], r["seq"])
        seq = {d["payload"]["rung"]: d["seq"] for d in decl}
        self.assertLess(seq["rung2"], first_call["rung2"])
        self.assertLess(seq["rung3"], min(first_call["rung3"], first_call["placebo"]))
        self.assertLess(seq["rung1"], first_call["rung1"])
        self.assertTrue(all(d["payload"]["corpus_kind"] == "synthetic" for d in decl))
        want_draws = {"rung1": 2, "rung2": 2, "placebo": 2, "remedy": 2}
        self.assertTrue(all(d["payload"]["draws"] == want_draws for d in decl))
        self.assertTrue(all(d["payload"]["parameters"]["n_perm"] == 50 and d["payload"]["parameters"]["B"] == 50
                            for d in decl))
        self.assertIn(pipeline.prompt_set_sha256(), decl[0]["payload"]["note"])
        self.go(self.cfg())
        self.assertEqual(len(self.declared()), 3)

    def test_a_cut_ladder_declares_only_what_it_runs(self):
        self.go(self.cfg(ladder_cfg={"rungs": ["rung2", "rung3", "placebo"], "draws": 2, "n_perm": 50, "B": 50}))
        self.assertEqual([d["payload"]["rung"] for d in self.declared()], ["rung2", "rung3"])
        self.assertEqual(self.declared()[0]["payload"]["draws"], {"rung2": 2, "placebo": 2, "remedy": 2})


class TestOutage(Base):
    def test_an_outage_is_not_stored_and_a_rerun_makes_exactly_those_calls(self):
        def down(messages):
            user = next(m["content"] for m in messages if m["role"] == "user")
            return ConnectionError("down") if "Two worked examples" in user else None
        # the remedy and the placebo prompts both carry worked examples; fail both, rung 1 and rung 2 are fine
        self.go(self.cfg(), fail=down)
        self.assertEqual(self.store("rung3"), [])
        self.assertEqual(self.store("placebo"), [])
        self.assertEqual(len(self.store("rung1")), N_PEOPLE * 2)
        res, _ = self.go(self.cfg())
        self.assertEqual(len(self.store("rung3")), N_PEOPLE * 2)
        self.assertEqual(len(self.store("placebo")), N_PEOPLE * 2)
        self.assertEqual(self.logged_calls, N_PEOPLE * 4)        # exactly the 48 that never got an answer
        self.assertEqual(res["new_items"]["recon"], N_PEOPLE * 4)


    def test_a_refusal_that_is_not_an_outage_is_stored_as_failed_in_every_rung_and_a_rerun_makes_no_call(self):
        """I-1: an HTTP 400 (a transcript over the context window) for one participant is that map's final 'failed',
        like the main run says; the ladder stores it in every rung, so ladder_missing is empty and the rerun is free."""
        cfg = self.cfg(ladder_cfg={"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50})
        people = pipeline.load_participants(cfg)
        marker = "how-to pages on the city website with the help of a friend"

        def refuse(messages):
            if any(marker in (m.get("content") or "") for m in messages):
                return RuntimeError("model call to 127.0.0.1:1 failed: HTTP 400: the request exceeds the available "
                                    "context size")
        self.go(cfg, fail=refuse)
        main = json.loads((self.out / "work" / "recon" / f"{people[0].pid}__removed__p0__t0__r0.json").read_text())
        self.assertEqual(main["status"], "failed")
        for rung in ALL:
            with self.subTest(rung=rung):
                stored = {n: json.loads((self.out / "work" / f"recon:{rung}" / f"{n}.json").read_text())["status"]
                          for n in self.store(rung)}
                self.assertEqual(len(stored), N_PEOPLE)
                self.assertEqual(stored[f"{people[0].pid}__removed__p0__t0__r0"], "failed")
        self.assertEqual(pipeline.ladder_missing(cfg), {})
        self.go(cfg, fail=refuse)
        self.assertEqual(self.logged_calls, 0)


    def test_rung2_draw0_is_never_a_call_of_its_own_when_the_main_item_was_not_done(self):
        """M-2: the main primary item timed out; the ladder must not make a separate rung-2 call for it (it would be a
        different map from the headline's). Rung 2 draw 0 stays a gap until a rerun copies the main item."""
        cfg = self.cfg(ladder_cfg={"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50})
        people = pipeline.load_participants(cfg)
        pid0 = people[0].pid
        marker = "how-to pages on the city website with the help of a friend"
        seen = {}

        def flap(messages):
            user = next(m["content"] for m in messages if m["role"] == "user")
            if marker in user and '"nodes"' in user and "TRANSCRIPT:\n" in user:
                h = json.dumps(messages, sort_keys=True)
                first = seen.setdefault("h", h)
                if h == first:
                    seen["n"] = seen.get("n", 0) + 1
                    if seen["n"] <= 3:                       # the three attempts of the main primary item
                        return ConnectionError("down")
        self.go(cfg, fail=flap)
        main = json.loads((self.out / "work" / "recon" / f"{pid0}__removed__p0__t0__r0.json").read_text())
        self.assertEqual(main["status"], "timeout_excluded")
        self.assertNotIn(f"{pid0}__removed__p0__t0__r0", self.store("rung2"))
        self.assertEqual(len(self.store("rung2")), N_PEOPLE - 1)
        rung2_calls = [r for r in records(self.log_path) if r["record_type"] == "call_request"
                       and r["payload"]["stage"] == "recon:removed:rung2"]
        self.assertEqual(rung2_calls, [])
        self.assertEqual(pipeline.ladder_missing(cfg), {"rung2": 1})
        self.go(cfg)                                          # the endpoint is back: the main item is redone, then copied
        item = json.loads((self.out / "work" / "recon:rung2" / f"{pid0}__removed__p0__t0__r0.json").read_text())
        self.assertEqual(item["reused_from"]["name"], f"{pid0}__removed__p0__t0__r0")
        self.assertEqual(pipeline.ladder_missing(cfg), {})


class TestLadderMissingCountsTheConfiguredCells(Base):
    """M-1: ``ladder_missing`` counts the done cells of the configured (participant, draw) pairs made with the rung's
    current prompt files, not the files on disk."""

    def setUp(self):
        super().setUp()
        self.cfg1 = self.cfg(ladder_cfg={"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50})
        self.go(self.cfg1)
        self.assertEqual(pipeline.ladder_missing(self.cfg1), {})
        self.pid0 = pipeline.load_participants(self.cfg1)[0].pid
        self.name0 = f"{self.pid0}__removed__p0__t0__r0"

    def item(self, rung, name=None):
        return self.out / "work" / f"recon:{rung}" / f"{name or self.name0}.json"

    def test_a_leftover_file_of_another_draw_does_not_hide_a_missing_cell(self):
        leftover = self.item("rung3", f"{self.pid0}__removed__p0__t0__r1")
        leftover.write_text(self.item("rung3").read_text())
        self.item("rung3").unlink()
        self.assertEqual(pipeline.ladder_missing(self.cfg1), {"rung3": 1})

    def test_an_item_made_with_other_prompt_files_is_missing(self):
        d = json.loads(self.item("placebo").read_text())
        d["prompt_sha256"] = "0" * 64
        self.item("placebo").write_text(json.dumps(d))
        self.assertEqual(pipeline.ladder_missing(self.cfg1), {"placebo": 1})

    def test_an_item_that_is_not_done_is_missing(self):
        d = json.loads(self.item("rung1").read_text())
        d["status"] = "timeout_excluded"
        self.item("rung1").write_text(json.dumps(d))
        self.assertEqual(pipeline.ladder_missing(self.cfg1), {"rung1": 1})


class TestRealCorpusGuard(Base):
    def real_run(self):
        # a real corpus runs the primary setting (T=0) and, DECISIONS C7, one draw of it (draws 2+ at T=0 are copies)
        cfg = self.cfg(ladder_cfg={"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50})
        real = dataclasses.replace(cfg, corpus={**cfg.corpus, "kind": "real"})
        clients = {k: Named(v) for k, v in build_clients("oracle", cfg).items()}
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        log = AuditLog(self.out / "logs" / "run.jsonl", cfg.run_id)
        self.addCleanup(log.close)
        r = pipeline._Run(real, clients, log)
        r._preflighted = True
        return r

    def test_real_kind_refuses_a_non_primary_setting_before_any_call(self):
        r = self.real_run()
        pipeline._stage_mapping_talk_confirm(r)
        r.specs[0] = dataclasses.replace(r.specs[0], temperature=0.7)
        before = len(records(r.log.path))
        with self.assertRaises(ValueError):
            pipeline._ladder_recon(r)
        calls = [x for x in records(r.log.path)[before:] if x["record_type"] == "call_request"]
        self.assertEqual(calls, [])

    def test_real_kind_passes_the_work_and_run_validity_checks(self):
        r = self.real_run()
        r.log.append("run_start", {"corpus_kind": "real"})
        pipeline._stage_mapping_talk_confirm(r)
        pipeline._stage_recon(r)
        log_path, work = Path(r.log.path), self.out / "work"
        self.assertIsNone(prereg.run_validity_problem(log_path, work))
        self.assertIsNone(audit_verify.work_integrity_problem(log_path, work))
        self.assertEqual(len(self.store("rung3")), N_PEOPLE)

    def test_real_kind_refuses_a_draw_beyond_the_declared_count(self):
        # was: a bigger count is a new draw. DECISIONS C7: on the T=0 primary setting a second draw is a copy, so a real
        # run takes ONE draw and a count above 1 is refused, in the config and (if the config is bypassed) before any call
        r = self.real_run()
        pipeline._stage_mapping_talk_confirm(r)
        pipeline._stage_recon(r)
        self.assertEqual(len(self.store("rung1")), N_PEOPLE)
        with self.assertRaisesRegex(ValueError, "temperature 0"):
            dataclasses.replace(r.cfg, ladder={**r.cfg.ladder, "draws": 3})
        r.cfg.ladder["draws"] = 3                      # bypass the config check: the ladder's own guard must still hold
        before = len(records(r.log.path))
        with self.assertRaisesRegex(ValueError, "temperature 0"):
            pipeline._ladder_recon(r)
        calls = [x for x in records(r.log.path)[before:] if x["record_type"] == "call_request"]
        self.assertEqual(calls, [])
        self.assertEqual(len(self.store("rung1")), N_PEOPLE)


if __name__ == "__main__":
    unittest.main()
