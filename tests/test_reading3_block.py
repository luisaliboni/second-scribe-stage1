"""The optional ``reading3`` config block: reading 3 (one no-interview map per role, the lead row and the floors) drawn
apart from the script set (``prior_only``: the script, canon, homogenization and phantom). Without the block every
output is what it was. Synthetic corpus, oracle client, temp dirs, no network."""
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from second_scribe import export_view, metrics, phantom, pipeline, profile_export
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients, main as cli_main
from second_scribe.pipeline import RunConfig
from second_scribe.schemas import MapGraph, Node

BLOCK = {"variant": "p0", "samples": 1, "temperature": 0}
R3 = "prior_only__p0__t0__r0"
ONLY_R3 = "zz reading three only"            # a label only the reading-3 map carries


def demo_dict(**top) -> dict:
    d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
    d["base_dir"] = str(Path(DEMO_CONFIG).parent)
    d.update(top)
    return d


def legacy_dict(**top) -> dict:
    d = demo_dict(**top)
    d.pop("reading3", None)
    return d


def write(out: Path, d: dict) -> Path:
    p = out / "config.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return p


def g(map_id, *labels, origin="prior_only") -> MapGraph:
    return MapGraph(map_id, origin, [Node(f"n{k}", t) for k, t in enumerate(labels)], [])


def pair(prior: dict, *, spec=None, separate=False, pid="m1", role="owner", canon=None) -> metrics.PairInputs:
    return metrics.PairInputs(pid=pid, role=role, interviewer=None, endorsed=g(pid, "a", origin="endorsed"),
                              ai=dict(prior), prov={}, modality={}, in_transcript={}, judged_ai_vs_endorsed={},
                              judged_prior_vs_endorsed=None, judged_ai_vs_script={}, judged_endorsed_vs_script=None,
                              prior_spec=spec, canon_prior=canon, reading3_separate=separate)


class TestConfig(unittest.TestCase):
    def test_the_demo_block_loads_and_gives_one_spec(self):
        cfg = RunConfig(**demo_dict())
        self.assertEqual(cfg.reading3, BLOCK)
        self.assertEqual([s.key() for s in cfg.reading3_specs()], [R3])
        self.assertEqual(cfg.reading3_specs()[0].arm, "prior_only")
        self.assertIsInstance(cfg.reading3_specs()[0].temperature, float)
        self.assertEqual([s.key() for s in RunConfig(**demo_dict(reading3={**BLOCK, "temperature": 0.5}))
                          .reading3_specs()], ["prior_only__p0__t0.5__r0"])
        self.assertEqual(len(cfg.prior_specs()), 6)                  # the script set is unchanged: p0-p2 x 2

    def test_no_block_gives_no_spec_and_the_old_hash(self):
        legacy = RunConfig(**legacy_dict())
        self.assertIsNone(legacy.reading3)
        self.assertEqual(legacy.reading3_specs(), [])
        self.assertEqual(RunConfig(**legacy_dict(reading3=None)).sha256(), legacy.sha256())
        self.assertNotEqual(RunConfig(**demo_dict()).sha256(), legacy.sha256())

    def refused(self, block, *words):
        with self.assertRaises(ValueError, msg=str(block)) as cm:
            RunConfig(**demo_dict(reading3=block))
        for w in words:
            self.assertIn(w, str(cm.exception))

    def test_refusals(self):
        self.refused({**BLOCK, "stray": 1}, "unknown key", "stray")
        self.refused({**BLOCK, "variants": ["p0"]}, "unknown key", "variants")
        for k in BLOCK:
            self.refused({x: v for x, v in BLOCK.items() if x != k}, "missing key", k)
        for n in (2, 0, True, "1", 1.0):
            self.refused({**BLOCK, "samples": n},
                         "reading3.samples: only 1 is supported (reading 3 is one map per role)")
        for t in (-0.1, True, "0", None, float("nan")):
            self.refused({**BLOCK, "temperature": t}, "reading3.temperature")
        for v in ("p9", "P0", 0, None):
            self.refused({**BLOCK, "variant": v}, "reading3.variant")
        self.refused([BLOCK], "reading3 must be an object")


class RunBase(unittest.TestCase):
    """A ``_Run`` over the demo corpus with the oracle client, in a temp out dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def run_for(self, d: dict) -> pipeline._Run:
        cfg = RunConfig.load(write(self.out, {**d, "out_dir": str(self.out / "run")}))
        path = self.out / "run" / "logs" / "run.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        log = AuditLog(path, cfg.run_id, clock=lambda: "t")
        self.addCleanup(log.close)
        r = pipeline._Run(cfg, build_clients("oracle", cfg, roles=("recon",)), log)
        r._preflighted = True              # unit level: no canary before the first prior-only call
        return r

    def view(self, d: dict) -> pipeline._Run:
        """A read-only ``_Run`` over the same out dir with config ``d`` (as ``export_view`` opens a run)."""
        return export_view._open_run(RunConfig.load(write(self.out, {**d, "out_dir": str(self.out / "run")})))


class TestDraw(RunBase):
    def test_the_script_set_then_one_reading3_item_per_role(self):
        r = self.run_for(demo_dict())
        self.assertEqual(pipeline._stage_prior_only(r), 7 * 2)
        names = r.work.names("prior_only")
        scripts = [f"{role}__{s.key()}" for s in r.prior_specs for role in r.roles()]
        self.assertEqual(sorted(names), sorted(scripts + [f"{role}__{R3}" for role in r.roles()]))
        self.assertEqual(pipeline._stage_prior_only(r), 0)          # resume: nothing redrawn
        requests = [json.loads(x)["payload"] for x in (self.out / "run" / "logs" / "run.jsonl").read_text()
                    .splitlines() if json.loads(x)["record_type"] == "call_request"]
        self.assertEqual([p["item_id"] for p in requests][-2:], [f"{role}__{R3}" for role in r.roles()])  # drawn last
        self.assertEqual({p["stage"] for p in requests}, {"prior_only"})

    def test_a_shared_key_is_drawn_once_and_serves_both(self):
        r = self.run_for(demo_dict(prior_only={"variants": ["p0", "p1"], "samples": 1, "temperature": 0}))
        self.assertEqual(pipeline._stage_prior_only(r), 2 * 2)       # p0 T 0 is a script key: not drawn twice
        self.assertFalse(r.reading3_separate())
        for role in r.roles():
            self.assertIn(R3, r.prior_maps(role))                     # in the script set ...
            self.assertEqual(pipeline._prior_key(r, role), R3)        # ... and reading 3

    def test_prior_key_with_and_without_the_block(self):
        r = self.run_for(demo_dict())
        pipeline._stage_prior_only(r)
        legacy = self.view(legacy_dict())                             # the same work dir, read without the block
        for role in r.roles():
            self.assertEqual(pipeline._prior_key(r, role), R3)
            self.assertNotIn(R3, r.prior_maps(role))                  # prior_maps = the script set only
            self.assertIsNotNone(r.reading3_map(role))
            self.assertEqual(pipeline._prior_key(legacy, role), "prior_only__p0__t0.7__r0")   # sorted(pm)[0]
            self.assertEqual(legacy.prior_maps(role).keys(), r.prior_maps(role).keys())
            self.assertIsNone(legacy.reading3_map(role))
        r.work.put("prior_only", f"owner__{R3}", {"status": "failed", "map": None, "accepted_attempt": None})
        self.assertIsNone(pipeline._prior_key(r, "owner"))           # never a script draft in its place
        self.assertEqual(pipeline._prior_key(r, "officer"), R3)

    def test_canonicalization_and_the_scripts_leave_the_reading3_map_out(self):
        r = self.run_for(demo_dict())
        pipeline._stage_prior_only(r)
        legacy = self.view(legacy_dict())
        before = pipeline._all_labels(legacy)
        names = pipeline.canonical_names(r.work)
        scripts_before = pipeline._scripts(legacy, names)
        d = r.work.get("prior_only", f"owner__{R3}")
        d["map"]["nodes"][0]["text"] = ONLY_R3
        r.work.put("prior_only", f"owner__{R3}", d)
        self.assertEqual(r.reading3_map("owner").nodes[0].text, ONLY_R3)
        labels = pipeline._all_labels(r)
        self.assertNotIn(ONLY_R3, labels)
        self.assertEqual(labels, before)                              # the canon batches do not move
        self.assertEqual(pipeline._scripts(r, names), scripts_before)


class TestSeparation(unittest.TestCase):
    """The consumers of the prior-only maps on PairInputs whose reading-3 map differs from every script draft."""

    def setUp(self):
        a, b = g("owner", "file form", "pay fee"), g("owner", "pay fee", "file form", "wait")
        self.prior = {"prior_only__p0__t0.7__r0": a, "prior_only__p1__t0.7__r0": b, R3: g("owner", ONLY_R3, "x")}

    def labels(self, script):
        return {n.text for n in script.nodes}

    def test_script_keys(self):
        p = pair(self.prior, spec=R3, separate=True)
        self.assertEqual(metrics.script_keys(p), ["prior_only__p0__t0.7__r0", "prior_only__p1__t0.7__r0"])
        self.assertEqual(metrics.script_keys(replace(p, reading3_separate=False)), sorted(self.prior))
        self.assertEqual(metrics._prior_key(p), R3)

    def test_scripts_by_role_leaves_the_separate_map_out(self):
        sep = [pair(self.prior, spec=R3, separate=True, pid=f"P{k}") for k in range(3)]
        s = metrics.scripts_by_role(sep)["owner"]
        self.assertEqual(s.meta["n_maps"], 2)
        self.assertNotIn(ONLY_R3, self.labels(s))
        missing: list = []
        metrics.scripts_by_role(sep, canonical=True, missing=missing)
        self.assertNotIn(R3, " ".join(missing))                       # no canon_prior needed for it
        self.assertEqual(metrics.scripts_by_role([replace(p, reading3_separate=False) for p in sep])["owner"]
                         .meta["n_maps"], 3)                          # legacy: every prior-only map is a draft

    def test_assigned_priors_and_phantom_leave_it_out(self):
        sep = [pair(self.prior, spec=R3, separate=True, pid=f"P{k}") for k in range(4)]
        self.assertEqual({k for _g, _c, (_r, k, _m) in metrics._assigned_priors(sep)},
                         {"prior_only__p0__t0.7__r0", "prior_only__p1__t0.7__r0"})
        self.assertNotIn(R3, {k for k, _j in phantom.prior_pool(sep[0])})
        self.assertIn(R3, {k for k, _j in phantom.prior_pool(replace(sep[0], reading3_separate=False))})

    def test_no_reading3_map_never_falls_back_to_a_draft(self):
        p = pair({k: v for k, v in self.prior.items() if k != R3}, spec=None, separate=True)
        self.assertIsNone(metrics._prior_key(p))
        self.assertEqual(metrics._prior_key(replace(p, reading3_separate=False)), "prior_only__p0__t0.7__r0")


class TestOracleDemoWithTheBlock(unittest.TestCase):
    """The demo config (with the block) run end to end with the oracle client, once for the class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        cls.out = Path(cls._tmp.name) / "run"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cls.rc = cli_main(["demo", "--out", str(cls.out), "--client", "oracle"])
        cls.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(cls.out))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_run_completes_and_every_audit_passes(self):
        self.assertEqual(self.rc, 0)
        audits = json.loads((self.out / "aggregates" / "audits.json").read_text(encoding="utf-8"))
        results = [x for stage in audits["stages"].values() for x in stage]
        self.assertTrue(results)
        self.assertTrue(all(x["passed"] for x in results), [x["name"] for x in results if not x["passed"]])
        agg = json.loads((self.out / "aggregates" / "aggregates.json").read_text(encoding="utf-8"))
        self.assertEqual(agg["items"]["prior_only"], {"ok": 14})       # 12 script drafts + 1 reading 3 per role

    def test_pair_inputs_carry_reading3_apart_without_canon_labels(self):
        r = export_view._open_run(self.cfg)
        p_list, _ex, _j = pipeline.build_pair_inputs(r)
        self.assertEqual(len(p_list), 12)
        for p in p_list:
            self.assertEqual(p.prior_spec, R3)
            self.assertTrue(p.reading3_separate)
            self.assertIsNotNone(p.ai[R3])
            self.assertIsNotNone(p.judged_prior_vs_endorsed)
            self.assertNotIn(R3, p.canon_prior)                       # not canonicalized ...
            self.assertEqual(len(p.canon_prior), 6)                   # ... the script drafts are
        missing: list = []
        metrics.scripts_by_role(p_list, canonical=True, missing=missing)
        audit = metrics._canonical_audit(p_list, [self.cfg.recon_specs()[0].key()], missing)
        self.assertEqual(audit["missing_prior_samples"], [])          # nothing blocked by the missing canon labels
        self.assertNotIn("canon_prior_missing", audit["fallback_counts"])

    def test_profile_settings_and_size_note(self):
        ctx = SimpleNamespace(cfg=self.cfg, B=50, seed=0)
        s = profile_export._settings(ctx)
        self.assertEqual((s["reading3_temperature"], s["reading3_draws"]), (0.0, 1))
        self.assertEqual(profile_export._reading3_note(self.cfg), " " + R3)
        legacy = RunConfig(**legacy_dict(out_dir=str(self.out)))
        s = profile_export._settings(SimpleNamespace(cfg=legacy, B=50, seed=0))
        self.assertEqual((s["reading3_temperature"], s["reading3_draws"]), (0.7, 2))     # the prior_only block
        self.assertEqual(profile_export._reading3_note(legacy), "")


if __name__ == "__main__":
    unittest.main()
