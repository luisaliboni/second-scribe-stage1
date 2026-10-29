"""K1: map reader dispatch (``maps/readers.py``). The study's ``.vsdx`` maps must read exactly as before."""
import glob
import json
import tempfile
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path

from second_scribe.maps import readers
from second_scribe.maps.readers import (DEFAULT_RULES, MAP_SUFFIXES, READERS, REFUSED_SUFFIXES, MapRefused,
                                        MissingRule, map_suffix, read_map, rule_for)
from second_scribe.maps.vsdx import ExtractConfig, extract_vsdx
from second_scribe.pipeline import KIT_ROOT
from second_scribe.schemas import Edge, MapGraph, Node

DEMO_MAPS = sorted(glob.glob(str(KIT_ROOT / "demo" / "corpus" / "maps" / "*.vsdx")))
DEMO_EXTRACT = ExtractConfig(phase_names=("Planning", "Permits", "Inspections", "Operating"))


def _graph(origin="endorsed"):
    return MapGraph(map_id="whatever", origin=origin,
                    nodes=[Node("n1", "Apply for a permit", phase="Permits"),
                           Node("n2", "Inspector visit", status="wished", x=1.5, y=2.0)],
                    edges=[Edge("n1", "n2", label="then")], unresolved_edges=1, meta={"n_missing": 1})


class TestReadMap(unittest.TestCase):
    def test_vsdx_default_identical_to_extract_vsdx(self):
        self.assertEqual(len(DEMO_MAPS), 12)
        for p in DEMO_MAPS:
            pid = Path(p).stem
            for rule in (None, MissingRule("red_line_or_text"), DEFAULT_RULES[".vsdx"]):
                for ext in (None, DEMO_EXTRACT):
                    self.assertEqual(read_map(p, pid, ext, rule).to_json(), extract_vsdx(p, pid, ext).to_json(),
                                     (pid, rule, ext))

    def test_vsdx_default_calls_extract_vsdx_only(self):
        p = DEMO_MAPS[0]
        sentinel = MapGraph(map_id="x", origin="endorsed")
        with unittest.mock.patch.object(readers, "extract_vsdx", return_value=sentinel) as ex, \
                unittest.mock.patch.dict(READERS, {".vsdx": lambda *a: self.fail("READERS used for default vsdx")}):
            self.assertIs(read_map(p, "SYN-X", DEMO_EXTRACT), sentinel)
        ex.assert_called_once_with(p, "SYN-X", DEMO_EXTRACT)

    def test_vsdx_other_rule_without_reader_refused(self):
        with unittest.mock.patch.dict(READERS, clear=True):
            with self.assertRaises(MapRefused) as cm:
                read_map(DEMO_MAPS[0], "SYN-X", None, MissingRule("red_fill"))
        self.assertEqual(cm.exception.code, "unsupported_format")

    def test_mapgraph_json_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        g = _graph()
        p = d / "Q07.mapgraph.json"
        p.write_text(g.to_json(), encoding="utf-8")
        out = read_map(p, "Q07")
        self.assertEqual(out.map_id, "Q07")
        want = asdict(g)
        want["map_id"] = "Q07"
        self.assertEqual(asdict(out), want)
        # upper-case suffix is the same suffix
        p2 = d / "Q08.MapGraph.JSON"
        p2.write_text(g.to_json(), encoding="utf-8")
        self.assertEqual(read_map(p2, "Q08").nodes, g.nodes)

    def test_mapgraph_json_wrong_origin_refused(self):
        d = Path(tempfile.mkdtemp())
        for origin in ("ai", "prior_only", "synthetic_gold"):
            p = d / f"{origin}.mapgraph.json"
            p.write_text(_graph(origin).to_json(), encoding="utf-8")
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
            self.assertEqual(cm.exception.code, "unreadable")
            self.assertNotIn("Apply for a permit", str(cm.exception))

    def test_mapgraph_json_garbage_refused_without_content(self):
        d = Path(tempfile.mkdtemp())
        cases = {"notjson": "SECRET-TEXT {", "list": json.dumps(["SECRET-TEXT"]),
                 "badkey": json.dumps({"map_id": "x", "origin": "endorsed", "SECRET-TEXT": 1}),
                 "badnode": json.dumps({"map_id": "x", "origin": "endorsed", "nodes": [{"SECRET-TEXT": 1}]})}
        for name, body in cases.items():
            p = d / f"{name}.mapgraph.json"
            p.write_text(body, encoding="utf-8")
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
            self.assertEqual(cm.exception.code, "unreadable", name)
            self.assertNotIn("SECRET-TEXT", str(cm.exception) + cm.exception.detail, name)

    def test_pdf_refused_with_code(self):
        d = Path(tempfile.mkdtemp())
        for suf in sorted(REFUSED_SUFFIXES) + [".PDF", ".Png"]:
            p = d / f"map{suf}"
            p.write_bytes(b"%PDF-1.4 not really")
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
            self.assertEqual(cm.exception.code, "image_or_pdf", suf)
            self.assertIsInstance(cm.exception, ValueError)
            self.assertTrue(cm.exception.detail)

    def test_unknown_suffix_refused(self):
        d = Path(tempfile.mkdtemp())
        for name in ("map.vsd", "map.docx", "map.json", "map", "map.txt"):
            p = d / name
            p.write_bytes(b"x")
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
            self.assertEqual(cm.exception.code, "unsupported_format", name)

    def test_registered_reader_is_called_with_default_rule(self):
        d = Path(tempfile.mkdtemp())
        p = d / "m.drawio"
        p.write_text("<mxfile/>", encoding="utf-8")
        seen = []

        def fake(path, map_id, extract, rule):
            seen.append((Path(path), map_id, extract, rule))
            return MapGraph(map_id=map_id, origin="endorsed", nodes=[Node("n1", "a box")])   # A2: never 0 boxes

        with unittest.mock.patch.dict(READERS, {".drawio": fake}):
            g = read_map(p, "Q2", DEMO_EXTRACT)
            read_map(p, "Q3", None, MissingRule("dashed"))
        self.assertEqual(g.map_id, "Q2")
        self.assertEqual(seen[0], (p, "Q2", DEMO_EXTRACT, DEFAULT_RULES[".drawio"]))
        self.assertEqual(seen[1][3], MissingRule("dashed"))

    def test_known_suffix_without_reader_refused(self):
        d = Path(tempfile.mkdtemp())
        p = d / "m.excalidraw"
        p.write_text("{}", encoding="utf-8")
        with unittest.mock.patch.dict(READERS, clear=True):
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
        self.assertEqual(cm.exception.code, "unsupported_format")

    def test_oversize_map_refused(self):
        d = Path(tempfile.mkdtemp())
        p = d / "big.mapgraph.json"
        p.write_text(_graph().to_json(), encoding="utf-8")
        with unittest.mock.patch.object(readers, "MAX_MAP_BYTES", 10):
            with self.assertRaises(MapRefused) as cm:
                read_map(p, "Q1")
        self.assertEqual(cm.exception.code, "unreadable")

    def test_map_suffix(self):
        self.assertEqual(map_suffix("a/B.VSDX"), ".vsdx")
        self.assertEqual(map_suffix(Path("x.mapgraph.json")), ".mapgraph.json")
        self.assertEqual(map_suffix("x.MAPGRAPH.JSON"), ".mapgraph.json")
        self.assertEqual(map_suffix("x.json"), ".json")
        self.assertEqual(map_suffix("noext"), "")
        self.assertIn(".mapgraph.json", MAP_SUFFIXES)
        self.assertEqual(set(DEFAULT_RULES) | {".mapgraph.json"}, set(MAP_SUFFIXES))

    def test_default_rules(self):
        self.assertEqual(DEFAULT_RULES[".mmd"], MissingRule("mermaid_class"))
        self.assertEqual(DEFAULT_RULES[".csv"].kind, "status_column")
        r = MissingRule("red_fill")
        self.assertEqual((r.colors, r.class_names), (("#ff0000",), ("missing",)))
        with self.assertRaises(Exception):
            r.kind = "dashed"                       # frozen
        with self.assertRaises(ValueError):
            MissingRule("purple_glow")


class TestRuleFor(unittest.TestCase):
    def test_rule_for_reads_corpus_rules(self):
        corpus = {"missing_rules": {".drawio": {"kind": "red_fill", "colors": ["#FF0000", "#C00000"]},
                                    ".mmd": {"kind": "mermaid_class", "class_names": ["gap", "missing"]},
                                    ".vsdx": {"kind": "dashed"}}}
        self.assertEqual(rule_for(corpus, "a/m.drawio"), MissingRule("red_fill", colors=("#ff0000", "#c00000")))
        self.assertEqual(rule_for(corpus, "m.MMD"), MissingRule("mermaid_class", class_names=("gap", "missing")))
        self.assertEqual(rule_for(corpus, Path("m.vsdx")), MissingRule("dashed"))
        self.assertIsNone(rule_for(corpus, "m.excalidraw"))
        self.assertIsNone(rule_for({}, "m.vsdx"))
        self.assertIsNone(rule_for({"missing_rules": None}, "m.vsdx"))

    def test_rule_for_bad_kind_refused(self):
        with self.assertRaises(ValueError):
            rule_for({"missing_rules": {".vsdx": {"kind": "glow"}}}, "m.vsdx")

    def test_demo_config_has_no_rules(self):
        cfg = json.loads((KIT_ROOT / "configs" / "demo.json").read_text(encoding="utf-8"))
        for p in DEMO_MAPS:
            self.assertIsNone(rule_for(cfg["corpus"], p))


if __name__ == "__main__":
    unittest.main()
