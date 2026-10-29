"""Wire-b item 4: the judge phase judges and aligns the maps of every ladder rung (RungWork / rung_view), and touches
nothing shared. Synthetic demo corpus, the oracle behind a wrapper, temp dirs only."""
import dataclasses
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe import pipeline, prereg
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import RunConfig, run

N_PEOPLE = 12
ALL = ["rung2", "rung3", "placebo", "rung1"]


class Revising:
    """The oracle, except that the REMEDY prompt (the one with 'narrator' wording) gets map labels with a suffix, so the
    rung 3 maps differ from every other rung's and need judge calls of their own."""

    def __init__(self, inner):
        self.inner = inner
        self.model_label = "oracle"

    def complete(self, messages, **kw):
        resp = self.inner.complete(messages, **kw)
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "according to the participant" in user and "Two worked examples" in user:
            d = json.loads(resp.text)
            for n in d["nodes"]:
                n["label"] += " revised"
            return dataclasses.replace(resp, text=json.dumps(d))
        return resp


def digest_dir(d: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.json"))} if d.exists() else {}


def records(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


class Base(unittest.TestCase):
    runs: dict = {}

    @classmethod
    def build(cls, name, ladder_cfg, wrap=None):
        tmp = tempfile.TemporaryDirectory(prefix="ss-ladj-")
        cls.addClassCleanup(tmp.cleanup)
        out = Path(tmp.name) / name
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["base_dir"], d["out_dir"] = str(DEMO_CONFIG.parent), str(out)
        d["ladder"] = ladder_cfg
        if not ladder_cfg:
            d.pop("ladder")
        # C7: draws > 1 need a sampled (T>0) primary; the no-ladder baseline gets the same specs, so the two runs'
        # shared stores (same item names) stay comparable
        d["specs"] = [dict(d["specs"][0], temperature=0.7)] + d["specs"][1:]
        cfg = RunConfig(**d)
        clients = build_clients("oracle", cfg)
        if wrap:
            clients = {k: wrap(v) for k, v in clients.items()}
        (out / "logs").mkdir(parents=True)
        log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
        try:
            run(cfg, ["Q", "J"], clients, log=log)
        finally:
            log.close()
        return out

    @classmethod
    def setUpClass(cls):
        cls.base = cls.build("none", {})
        cls.lad = cls.build("lad", {"rungs": ALL, "draws": 2, "n_perm": 50, "B": 50}, Revising)


class TestJudgeAndAlignRungMaps(Base):
    def count(self, out, store):
        d = out / "work" / store
        return len(list(d.glob("*.json"))) if d.exists() else 0

    def test_every_rung_map_is_judged_and_aligned_in_a_store_of_its_own(self):
        for rung in ALL:
            with self.subTest(rung=rung):
                self.assertEqual(self.count(self.lad, f"judge_modality:{rung}"), N_PEOPLE * 2)
                names = [p.stem for p in (self.lad / "work" / f"align:{rung}").glob("*.json")]
                self.assertEqual(len([n for n in names if "__ai_endorsed__" in n]), N_PEOPLE * 2)
                self.assertTrue(all("__ai_" in n for n in names))
                for p in (self.lad / "work" / f"judge_modality:{rung}").glob("*.json"):
                    self.assertEqual(json.loads(p.read_text())["status"], "ok")

    def test_the_shared_stores_are_exactly_what_a_run_without_a_ladder_makes(self):
        for store in ("judge_modality", "judge_shuffled", "canonicalize", "in_transcript", "mapping_talk_confirm",
                      "prior_only", "recon"):
            with self.subTest(store=store):
                self.assertEqual(digest_dir(self.lad / "work" / store).keys(), digest_dir(self.base / "work" / store).keys())
        # the main run's alignments are untouched too: the rung maps' alignments are in align:<rung>
        self.assertEqual(sorted(digest_dir(self.lad / "work" / "align")), sorted(digest_dir(self.base / "work" / "align")))
        self.assertEqual(digest_dir(self.lad / "work" / "canonicalize"), digest_dir(self.base / "work" / "canonicalize"))

    def test_the_remedy_maps_have_judge_labels_of_their_own(self):
        j = json.loads((self.lad / "work" / "judge_modality:rung3" / "SYN-OW01__removed__p0__t0.7__r1.json").read_text())
        main = json.loads((self.lad / "work" / "judge_modality" / "SYN-OW01__removed__p0__t0.7__r0.json").read_text())
        self.assertEqual(set(j["labels"]), set(main["labels"]))           # node ids; the labels' text differs
        self.assertEqual(j["map_sha256"] != main["map_sha256"], True)

    def test_judge_and_align_calls_of_a_rung_carry_the_rung_in_their_item_id(self):
        calls = [r["payload"] for r in records(self.lad / "logs" / "run.jsonl") if r["record_type"] == "call_request"]
        jm = [c["item_id"] for c in calls if c["stage"] == "judge:modality" and "@" in c["item_id"]]
        self.assertTrue(jm)                                                   # the revised rung 3 labels were judged
        self.assertEqual({i.rsplit("@", 1)[1].split(":")[0] for i in jm}, {"rung3"})        # (":0": the batch number)
        al = [c["item_id"] for c in calls if c["stage"] == "judge:align" and "@" in c["item_id"]]
        self.assertEqual({i.rsplit("@", 1)[1].split(":")[0] for i in al}, set(ALL))
        main = [c["item_id"] for c in calls if c["stage"] == "judge:align" and "@" not in c["item_id"]]
        self.assertTrue(main)

    def test_the_log_and_work_verify(self):
        log = self.lad / "logs" / "run.jsonl"
        self.assertEqual(audit_verify.main([str(log)]), 0)
        self.assertIsNone(audit_verify.work_integrity_problem(log, self.lad / "work"))

    def test_rung_view_exposes_the_rung_maps_to_build_pair_inputs(self):
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))        # the run's own config: 2 draws, sampled primary
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(self.lad), ladder={"rungs": ALL, "draws": 2, "n_perm": 50, "B": 50},
                             specs=[dict(d["specs"][0], temperature=0.7)] + d["specs"][1:])
        log = AuditLog(self.lad / "logs" / "view.jsonl", "v")
        self.addCleanup(log.close)
        r = pipeline._Run(cfg, build_clients("oracle", cfg), log)
        v = pipeline.rung_view(r, "rung3")
        self.assertEqual([s.replicate for s in v.specs], [0, 1])
        pl, _ex, _judging = pipeline.build_pair_inputs(v)
        self.assertEqual(len(pl), N_PEOPLE)
        self.assertTrue(all(p.ai[s.key()] is not None for p in pl for s in v.specs))
        self.assertTrue(all(n.text.endswith(" revised") for n in pl[0].ai[v.specs[0].key()].nodes))
        main_pl, _, _ = pipeline.build_pair_inputs(r)
        self.assertFalse(any(n.text.endswith(" revised") for n in main_pl[0].ai[r.specs[0].key()].nodes))


class Named:
    """An offline client that names its model, as a real run's client does."""
    cfg = types.SimpleNamespace(model_id="alpha-qwen-1", api_key_env=None, params={}, endpoint=None)

    def __init__(self, inner):
        self.inner = inner

    def complete(self, messages, **kw):
        return self.inner.complete(messages, **kw)


class TestRealKindValidity(unittest.TestCase):
    def test_a_real_kind_run_of_the_stages_passes_run_validity_and_work_integrity(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-ladj-real-")
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["base_dir"], d["out_dir"] = str(DEMO_CONFIG.parent), str(out)
        d["ladder"] = {"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50}     # real kind: T=0 primary, one draw (C7)
        cfg = RunConfig(**d)
        real = dataclasses.replace(cfg, corpus={**cfg.corpus, "kind": "real"})
        clients = {k: Named(Revising(v)) for k, v in build_clients("oracle", cfg).items()}
        (out / "logs").mkdir(parents=True)
        log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
        self.addCleanup(log.close)
        log.append("run_start", {"corpus_kind": "real"})
        r = pipeline._Run(real, clients, log)
        r._preflighted = True
        for stage in ("mapping_talk_confirm", "in_transcript", "recon", "judge_modality", "canonicalize", "align"):
            pipeline.STAGES[stage](r)
        path = out / "logs" / "run.jsonl"
        self.assertIsNone(prereg.run_validity_problem(path, out / "work"))
        self.assertIsNone(audit_verify.work_integrity_problem(path, out / "work"))
        self.assertTrue(list((out / "work" / "judge_modality:rung3").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
