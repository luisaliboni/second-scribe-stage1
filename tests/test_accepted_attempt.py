"""Run-2 call-level fix 2 (review 3.4): the attempt at which a map was accepted (1, 2, 3; None when the item failed) is
stored in the reconstruction work item, main grid and ladder alike, and its distribution per spec, arm and rung is in
aggregates["quote_type"] and the report. Fake clients only."""
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from second_scribe import ladder, pipeline, report
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.llm import MockClient, ModelResponse
from second_scribe.pipeline import RunConfig, _Run
from second_scribe.reconstruct import ReconSpec, reconstruct

from tests.test_ladder_wiring import CTX, GOOD, PROMPTS, SPEC, T, RecordingClient
from tests.test_pipeline_ladder_q import ALL, Base as PipeBase


class Scripted:
    """Answers the n-th call from ``replies`` (a list of (text, finish_reason))."""

    def __init__(self, replies):
        self.replies, self.n = list(replies), 0

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        text, fin = self.replies[self.n]
        self.n += 1
        return ModelResponse(text, fin, {}, 0, "h")


BAD = ("Sure! Here is the map", "stop")
GOODR = (json.dumps(GOOD), "stop")
CUT = ('{"nodes": [{"id"', "length")


class TestReconstructReportsTheAttempt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-att-")
        self.addCleanup(self._tmp.cleanup)
        self.log = AuditLog(Path(self._tmp.name) / "r.jsonl", "r", clock=lambda: "t")

    def go(self, replies, **kw):
        spec = kw.pop("spec", ReconSpec("removed", "p0", 0.0, 1))
        return reconstruct(self.log, Scripted(replies), T, spec, CTX, PROMPTS, item_id="SYN-OW01__x", **kw)

    def test_attempt_1_2_3(self):
        for replies, want in (([GOODR], 1), ([BAD, GOODR], 2), ([CUT, BAD, GOODR], 3)):
            with self.subTest(want=want):
                g, st = self.go(replies)
                self.assertEqual((st, g.meta["accepted_attempt"]), ("ok", want))

    def test_a_ladder_rung_gets_it_too(self):
        g, st = self.go([BAD, GOODR], spec=SPEC, rung=ladder.get_rung("rung1"), real_data=False)
        self.assertEqual((st, g.meta["accepted_attempt"], g.meta["rung"]), ("ok", 2, "rung1"))

    def test_a_failed_item_has_no_graph(self):
        g, st = self.go([BAD, BAD, BAD])
        self.assertEqual((g, st), (None, "failed"))

    def test_every_rung_uses_the_same_unfound_share_limit(self):
        # L16 (council round 10): 40% unfound is accepted at attempt 1 and 60% is retried, in every ladder rung
        real = GOOD["nodes"][0]["quote"]

        def m(n_unfound):
            nodes = [dict(GOOD["nodes"][0], id=f"n{i}",
                          quote=f"completely invented words number {i} nowhere" if i < n_unfound else real)
                     for i in range(10)]
            return (json.dumps({"nodes": nodes, "edges": [], "emotion_word": None}), "stop")
        for r in ladder.RUNGS:
            with self.subTest(rung=r.key, share=0.4):
                client = Scripted([m(4)])
                g, st = reconstruct(self.log, client, T, SPEC, CTX, PROMPTS, item_id=f"SYN-OW01__{r.key}1", rung=r,
                                    real_data=False)
                self.assertEqual((st, g.meta["accepted_attempt"], client.n), ("ok", 1, 1))
            with self.subTest(rung=r.key, share=0.6):
                client = Scripted([m(6)] * 3)
                g, st = reconstruct(self.log, client, T, SPEC, CTX, PROMPTS, item_id=f"SYN-OW01__{r.key}2", rung=r,
                                    real_data=False)
                self.assertEqual((g, st, client.n), (None, "failed", 3))


class TestItemsCarryIt(PipeBase):
    def test_main_grid_and_ladder_items_store_it_and_old_items_do_not_crash(self):
        res, _ = self.go(self.cfg())
        work = self.out / "work"
        for stage in ["recon"] + [f"recon:{k}" for k in ALL]:
            files = sorted((work / stage).glob("*.json"))
            self.assertTrue(files, stage)
            for f in files:
                item = json.loads(f.read_text())
                if item["status"] == "ok":
                    self.assertIn(item["accepted_attempt"], (1, 2, 3), (stage, f.name))
                elif item["status"] == "failed":
                    self.assertIn("accepted_attempt", item)
                    self.assertIsNone(item["accepted_attempt"])
        # rung 2 draw 0 is the main run's item: it carries the main item's attempt
        reused = [f for f in sorted((work / "recon:rung2").glob("*.json")) if "reused_from" in json.loads(f.read_text())]
        self.assertTrue(reused)
        for f in reused:
            main = json.loads((work / "recon" / f.name).read_text())
            self.assertEqual(json.loads(f.read_text()).get("accepted_attempt"), main["accepted_attempt"])
        # an item written before the field existed: strip it everywhere, resume and aggregate without a crash
        for f in [*work.glob("recon*/*.json"), *work.glob("prior_only/*.json")]:
            item = json.loads(f.read_text())
            item.pop("accepted_attempt", None)
            f.write_text(json.dumps(item))
        cfg = self.cfg()
        self.go(cfg)                             # resume over the stripped items: nothing to redo, no crash
        log = AuditLog(self.out / "logs" / "agg.jsonl", "agg")
        self.addCleanup(log.close)
        sec = pipeline.quote_type_section(_Run(cfg, build_clients("oracle", cfg), log))
        for part in ("by_spec", "by_arm", "by_rung"):
            self.assertTrue(sec[part], part)
            for cell in sec[part].values():
                dist = cell["accepted_attempt"]
                self.assertEqual(dist["1"] + dist["2"] + dist["3"], 0)
                self.assertGreater(dist["unknown"], 0)


class TestDistribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-att-agg-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(Path(cls._tmp.name) / "out"))
        Path(cls.cfg.out_dir).mkdir(parents=True)
        cls.log = AuditLog(Path(cls.cfg.out_dir) / "r.jsonl", "r")
        cls.addClassCleanup(cls.log.close)
        cls.r = _Run(cls.cfg, build_clients("oracle", cls.cfg), cls.log)

    def test_counts_per_spec_arm_and_the_failed_and_unknown_buckets(self):
        r = self.r
        k = r.specs[0].key()
        ok = {"nodes": [], "edges": [], "map_id": "m", "origin": "ai", "meta": {}}
        r.work.put("recon", f"SYN-OW01__{k}", {"status": "ok", "map": ok, "accepted_attempt": 1})
        r.work.put("recon", f"SYN-OW02__{k}", {"status": "ok", "map": ok, "accepted_attempt": 3})
        r.work.put("recon", f"SYN-OW03__{k}", {"status": "failed", "map": None, "accepted_attempt": None})
        r.work.put("recon", f"SYN-OW04__{k}", {"status": "ok", "map": ok})      # saved before the field existed
        sec = pipeline.quote_type_section(r)
        self.assertEqual(sec["by_spec"][k]["accepted_attempt"], {"1": 1, "2": 0, "3": 1, "failed": 1, "unknown": 1})
        self.assertEqual(sec["by_arm"][k.split("__")[0]]["accepted_attempt"],
                         {"1": 1, "2": 0, "3": 1, "failed": 1, "unknown": 1})

    def test_report_prints_the_distribution(self):
        cell = {"maps": 3, "nodes": 9, "type_coercions": 0, "maps_with_coercion": 0,
                "quote_tiers": {"exact": 1, "approximate": 0, "unfound": 0},
                "accepted_attempt": {"1": 5, "2": 2, "3": 1, "failed": 4, "unknown": 0}}
        text = "\n".join(report.quote_type_markdown({"by_spec": {"full__p0__t0__r0": cell}, "by_arm": {},
                                                      "by_rung": {"rung1": cell}, "type_share_affected": False}))
        self.assertIn("Accepted at attempt", text)
        self.assertIn("| spec full__p0__t0__r0 | 5 | 2 | 1 | 4 | 0 |", text)
        self.assertIn("| rung rung1 | 5 | 2 | 1 | 4 | 0 |", text)

    def test_report_of_an_aggregate_without_the_field_does_not_crash(self):
        cell = {"maps": 1, "nodes": 2, "type_coercions": 0, "maps_with_coercion": 0,
                "quote_tiers": {"exact": 1, "approximate": 0, "unfound": 0}}
        text = "\n".join(report.quote_type_markdown({"by_spec": {"a": cell}, "by_arm": {}, "by_rung": {}}))
        self.assertIn("a", text)


if __name__ == "__main__":
    unittest.main()
