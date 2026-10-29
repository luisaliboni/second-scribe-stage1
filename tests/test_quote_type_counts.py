"""Run-2 review S1: type coercions and quote tiers are counted per arm and rung, land in the aggregates and the report,
and the report says that type_share:* features include coerced nodes. Synthetic maps only."""
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from second_scribe import pipeline, report
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import RunConfig, _Run
from second_scribe.schemas import Edge, MapGraph, Node


def amap(n_coerced, tiers, rung=None):
    nodes = [Node(f"n{i}", f"S{i}", "other" if i < n_coerced else "service", None, "experienced", "q")
             for i in range(max(n_coerced, 2))]
    meta = {"type_coercions": n_coerced, "quote_tier_counts": dict(zip(("exact", "approximate", "unfound"), tiers))}
    if rung:
        meta["rung"] = rung
    return MapGraph("m", "ai", nodes, [Edge("n0", "n1", "experienced", True, "q")], meta=meta)


class TestCountsFromMaps(unittest.TestCase):
    def test_sums_nodes_coercions_and_tiers(self):
        got = pipeline.quote_type_counts([asdict(amap(2, (5, 1, 0))), asdict(amap(0, (3, 0, 1))), None])
        self.assertEqual(got, {"maps": 2, "nodes": 4, "type_coercions": 2, "maps_with_coercion": 1,
                               "quote_tiers": {"exact": 8, "approximate": 1, "unfound": 1}})

    def test_maps_without_the_meta_count_as_zero(self):
        old = asdict(MapGraph("m", "ai", [Node("n0", "A", "service", None, "experienced", "q")], []))
        got = pipeline.quote_type_counts([old])
        self.assertEqual((got["maps"], got["type_coercions"], got["quote_tiers"]["exact"]), (1, 0, 0))


class TestSectionAndReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-qt-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(Path(cls._tmp.name) / "out"))
        Path(cls.cfg.out_dir).mkdir(parents=True)
        cls.log = AuditLog(Path(cls.cfg.out_dir) / "r.jsonl", "r")
        cls.addClassCleanup(cls.log.close)
        cls.r = _Run(cls.cfg, build_clients("oracle", cls.cfg), cls.log)

    def test_section_groups_by_arm_spec_and_rung(self):
        r = self.r
        keys = [s.key() for s in r.specs]
        r.work.put("recon", f"SYN-OW01__{keys[0]}", {"status": "ok", "map": asdict(amap(1, (4, 1, 0)))})
        r.work.put("recon", f"SYN-OW02__{keys[0]}", {"status": "ok", "map": asdict(amap(0, (2, 0, 0)))})
        r.work.put("recon", f"SYN-OW03__{keys[0]}", {"status": "failed"})
        sec = pipeline.quote_type_section(r)
        cell = sec["by_spec"][keys[0]]
        self.assertEqual((cell["maps"], cell["type_coercions"], cell["quote_tiers"]["approximate"]), (2, 1, 1))
        arm = keys[0].split("__")[0]
        self.assertEqual(sec["by_arm"][arm]["type_coercions"], 1)
        self.assertTrue(sec["type_share_affected"])
        self.assertEqual(sec["note"], "type_share_includes_coerced_other")

    def test_report_prints_the_counts_and_the_one_line_note(self):
        sec = {"by_spec": {"full__p0__t0__r0": {"maps": 2, "nodes": 4, "type_coercions": 1, "maps_with_coercion": 1,
                                                "quote_tiers": {"exact": 6, "approximate": 1, "unfound": 0}}},
               "by_arm": {}, "by_rung": {"rung1": {"maps": 3, "nodes": 9, "type_coercions": 2, "maps_with_coercion": 2,
                                                   "quote_tiers": {"exact": 7, "approximate": 2, "unfound": 1}}},
               "type_share_affected": True, "note": "type_share_includes_coerced_other"}
        lines = report.quote_type_markdown(sec)
        text = "\n".join(lines)
        self.assertIn("full__p0__t0__r0", text)
        self.assertIn("rung1", text)
        self.assertIn("type_share:*", text)
        self.assertIn("'other'", text)


if __name__ == "__main__":
    unittest.main()
