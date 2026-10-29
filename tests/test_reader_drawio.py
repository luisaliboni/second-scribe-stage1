"""T7: the draw.io reader (``maps/drawio.py``). Fixtures are small synthetic XML strings built here, plus two files
under ``tests/fixtures/maps/``. No real data; ids are neutral."""
import base64
import tempfile
import unittest
import unittest.mock
import urllib.parse
import zlib
from pathlib import Path

from second_scribe.maps import drawio
from second_scribe.maps.drawio import read_drawio
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MapRefused, MissingRule, read_map

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "maps"
RULE = DEFAULT_RULES[".drawio"]


def model(*cells: str) -> str:
    return ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>' + "".join(cells) +
            "</root></mxGraphModel>")


def v(cid, label, style="", parent="1", x=None, y=None, w=80, h=40):
    geo = "" if x is None else f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/>'
    return (f'<mxCell id="{cid}" value="{label}" style="{style}" vertex="1" parent="{parent}">{geo}</mxCell>')


def e(cid, src, tgt, label="", style="", parent="1"):
    s = f' source="{src}"' if src else ""
    t = f' target="{tgt}"' if tgt else ""
    return (f'<mxCell id="{cid}" value="{label}" style="{style}" edge="1" parent="{parent}"{s}{t}>'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>')


def compress(xml: str) -> str:
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = co.compress(urllib.parse.quote(xml, safe="").encode("utf-8")) + co.flush()
    return base64.b64encode(raw).decode("ascii")


def mxfile(*pages: str, compressed=False) -> str:
    out = "<mxfile>"
    for i, m in enumerate(pages, start=1):
        body = compress(m) if compressed else m
        out += f'<diagram id="g{i}" name="Page-{i}">{body}</diagram>'
    return out + "</mxfile>"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)

    def read(self, xml, rule=RULE, name="m.drawio", map_id="Q1"):
        p = self.dir / name
        p.write_bytes(xml.encode("utf-8") if isinstance(xml, str) else xml)
        return read_drawio(p, map_id, None, rule)

    def refused(self, xml, **kw):
        with self.assertRaises(MapRefused) as cm:
            self.read(xml, **kw)
        return cm.exception


SIMPLE = model(v("c2", "Apply for a permit", x=10, y=20), v("c3", "Inspector visit", x=200, y=20),
               e("c4", "c2", "c3", "then"))


class TestStructure(Base):
    def test_plain_and_compressed_equal(self):
        plain = self.read(mxfile(SIMPLE))
        comp = self.read(mxfile(SIMPLE, compressed=True), name="z.drawio")
        self.assertEqual(plain.to_json(), comp.to_json())
        self.assertEqual([n.text for n in plain.nodes], ["Apply for a permit", "Inspector visit"])
        self.assertEqual(len(plain.edges), 1)

    def test_bare_mxgraphmodel_and_map_id_and_origin(self):
        g = self.read(SIMPLE, map_id="Q7")
        self.assertEqual((g.map_id, g.origin), ("Q7", "endorsed"))
        self.assertEqual(g.meta["n_pages"], 1)

    def test_node_edge_fields(self):
        g = self.read(SIMPLE)
        self.assertEqual([n.node_id for n in g.nodes], ["d1.c2", "d1.c3"])
        self.assertEqual((g.nodes[0].x, g.nodes[0].y, g.nodes[0].node_type, g.nodes[0].status),
                         (10.0, 20.0, "other", "experienced"))
        ed = g.edges[0]
        self.assertEqual((ed.source, ed.target, ed.label, ed.status, ed.glued), ("d1.c2", "d1.c3", "then",
                                                                                  "experienced", True))

    def test_meta_keys_and_counts(self):
        g = self.read(model(v("a1", "Start", x=0, y=0), v("a2", "End", x=1, y=1),
                            v("a3", "Wish", "strokeColor=#FF0000;", x=2, y=2),
                            e("a4", "a1", "a2"), e("a5", "a2", "a9")))
        self.assertEqual(g.meta, {"n_pages": 1, "n_shapes": 3, "n_connectors": 2, "n_missing": 1, "n_phases": 0})
        self.assertEqual(g.unresolved_edges, 1)

    def test_absolute_coordinates_add_parent_offsets(self):
        g = self.read(model(v("g1", "Group", "swimlane;", x=100, y=50, w=300, h=200),
                            v("g2", "Inside", parent="g1", x=10, y=30),
                            v("g3", "Elsewhere", x=5, y=6), v("g4", "No geometry")))
        by = {n.text: n for n in g.nodes}
        self.assertEqual((by["Inside"].x, by["Inside"].y), (110.0, 80.0))
        self.assertEqual((by["Elsewhere"].x, by["Elsewhere"].y), (5.0, 6.0))
        self.assertEqual((by["No geometry"].x, by["No geometry"].y), (None, None))

    def test_swimlane_children_get_phase(self):
        g = self.read(model(v("s1", "Planning", "swimlane;startSize=20;", x=0, y=0, w=200, h=300),
                            v("s2", "Draft the plan", parent="s1", x=10, y=30),
                            v("s3", "Operating", "container=1;collapsible=0;", x=300, y=0, w=200, h=300),
                            v("s4", "Run it", parent="s3", x=10, y=30),
                            v("s5", "Loose box", x=700, y=0),
                            e("s6", "s2", "s4")))
        by = {n.text: n for n in g.nodes}
        self.assertEqual(sorted(by), ["Draft the plan", "Loose box", "Run it"])     # containers are not nodes
        self.assertEqual((by["Draft the plan"].phase, by["Run it"].phase, by["Loose box"].phase),
                         ("Planning", "Operating", None))
        self.assertEqual(g.meta["n_phases"], 2)
        self.assertEqual(len(g.edges), 1)

    def test_nested_container_innermost_phase_wins(self):
        g = self.read(model(v("o1", "Outer", "swimlane;", x=0, y=0, w=500, h=500),
                            v("o2", "Inner", "swimlane;", parent="o1", x=10, y=10, w=200, h=200),
                            v("o3", "Deep box", parent="o2", x=5, y=25)))
        self.assertEqual([(n.text, n.phase) for n in g.nodes], [("Deep box", "Inner")])
        self.assertEqual(g.nodes[0].x, 15.0)

    def test_container_without_children_is_a_node(self):
        g = self.read(model(v("k1", "Lonely lane", "swimlane;", x=0, y=0)))
        self.assertEqual([(n.text, n.phase) for n in g.nodes], [("Lonely lane", None)])
        self.assertEqual(g.meta["n_phases"], 0)

    def test_html_label_stripped(self):
        label = "&lt;div&gt;Call the &lt;b&gt;clinic&lt;/b&gt;&amp;nbsp;now&lt;br&gt;at &amp;amp; noon&lt;/div&gt;"
        g = self.read(model(v("h1", label, "html=1;", x=0, y=0)))
        self.assertEqual(g.nodes[0].text, "Call the clinic now at & noon")

    def test_object_wrapper_label_and_edge_label_cell(self):
        xml = model('<object label="Wrapped &lt;i&gt;step&lt;/i&gt;" id="w1" owner="x">'
                    '<mxCell style="strokeColor=#FF0000;" vertex="1" parent="1">'
                    '<mxGeometry x="3" y="4" width="80" height="40" as="geometry"/></mxCell></object>',
                    v("w2", "Other", x=9, y=9), e("w3", "w1", "w2"),
                    v("w4", "via the portal", "edgeLabel;html=1;", parent="w3"))
        g = self.read(xml)
        self.assertEqual([n.text for n in g.nodes], ["Wrapped step", "Other"])
        self.assertEqual(g.nodes[0].status, "wished")
        self.assertEqual(g.edges[0].label, "via the portal")
        self.assertEqual(g.meta["n_shapes"], 2)

    def test_empty_text_vertex_is_not_a_node(self):
        g = self.read(model(v("e1", "Real", x=0, y=0), v("e2", "", x=1, y=1), e("e3", "e1", "e2")))
        self.assertEqual([n.text for n in g.nodes], ["Real"])
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))

    def test_multipage_merged_with_page_prefix(self):
        one = model(v("c2", "Alpha", x=0, y=0), v("c3", "Beta", x=1, y=1), e("c4", "c2", "c3"))
        two = model(v("c2", "Gamma", x=0, y=0), v("c3", "Delta", x=1, y=1), e("c4", "c3", "c2"))
        for comp in (False, True):
            g = self.read(mxfile(one, two, compressed=comp), name=f"mp{comp}.drawio")
            self.assertEqual([n.node_id for n in g.nodes], ["d1.c2", "d1.c3", "d2.c2", "d2.c3"])
            self.assertEqual([(x.source, x.target) for x in g.edges], [("d1.c2", "d1.c3"), ("d2.c3", "d2.c2")])
            self.assertEqual(g.meta["n_pages"], 2)

    def test_dangling_edge_counts_unresolved(self):
        g = self.read(model(v("c2", "Alpha", x=0, y=0), v("c3", "Beta", x=1, y=1),
                            e("c4", "c2", ""), e("c5", "", "c3"), e("c6", "c2", "zz"), e("c7", "c2", "c3")))
        self.assertEqual((len(g.edges), g.unresolved_edges, g.meta["n_connectors"]), (1, 3, 4))

    def test_edge_to_phase_container_is_unresolved(self):
        g = self.read(model(v("s1", "Phase", "swimlane;", x=0, y=0), v("s2", "In", parent="s1", x=1, y=1),
                            v("s3", "Out", x=500, y=0), e("s4", "s3", "s1")))
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))


class TestRules(Base):
    def test_red_stroke_marks_missing(self):
        g = self.read(model(v("r1", "Wished box", "rounded=1;strokeColor=#FF0000;", x=0, y=0),
                            v("r2", "Normal", "strokeColor=#000000;", x=1, y=1),
                            v("r3", "Red text", "fontColor=#ff0000;", x=2, y=2),
                            e("r4", "r1", "r2", style="strokeColor=#FF0000;"),
                            e("r5", "r2", "r3")))
        self.assertEqual({n.text: n.status for n in g.nodes},
                         {"Wished box": "wished", "Normal": "experienced", "Red text": "wished"})
        self.assertEqual([x.status for x in g.edges], ["wished", "experienced"])
        self.assertEqual(g.meta["n_missing"], 3)

    def test_dashed_rule(self):
        rule = MissingRule("dashed")
        g = self.read(model(v("d1", "Dashed", "dashed=1;strokeColor=#FF0000;", x=0, y=0),
                            v("d2", "Solid", "dashed=0;", x=1, y=1), v("d3", "Plain", x=2, y=2),
                            e("d4", "d2", "d3", style="dashed=1;")), rule=rule)
        self.assertEqual({n.text: n.status for n in g.nodes},
                         {"Dashed": "wished", "Solid": "experienced", "Plain": "experienced"})
        self.assertEqual(g.edges[0].status, "wished")

    def test_fill_rule(self):
        rule = MissingRule("red_fill", colors=("#F8CECC",))
        g = self.read(model(v("f1", "Pink", "fillColor=#f8cecc;strokeColor=#FF0000;", x=0, y=0),
                            v("f2", "Red line only", "strokeColor=#F8CECC;", x=1, y=1)), rule=rule)
        self.assertEqual({n.text: n.status for n in g.nodes}, {"Pink": "wished", "Red line only": "experienced"})

    def test_light_dark_uses_light_value(self):
        g = self.read(model(v("l1", "Light red", "strokeColor=light-dark(#FF0000, #ffffff);", x=0, y=0),
                            v("l2", "Dark red only", "strokeColor=light-dark(#000000,#ff0000);", x=1, y=1),
                            v("l3", "Font", "fontColor=light-dark(#ff0000,#00ff00);", x=2, y=2)))
        self.assertEqual({n.text: n.status for n in g.nodes},
                         {"Light red": "wished", "Dark red only": "experienced", "Font": "wished"})

    def test_short_hex_and_custom_colour(self):
        g = self.read(model(v("c1", "Short", "strokeColor=#F00;", x=0, y=0),
                            v("c2", "Blue", "strokeColor=#0000FF;", x=1, y=1)), rule=MissingRule("red_line_or_text",
                                                                                                    colors=("#0000ff",)))
        self.assertEqual({n.text: n.status for n in g.nodes}, {"Short": "experienced", "Blue": "wished"})

    def test_rule_kind_that_is_not_for_drawio_refused(self):
        exc = self.refused(SIMPLE, rule=MissingRule("status_column"))
        self.assertEqual(exc.code, "unsupported_format")


class TestRefusals(Base):
    def test_doctype_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                + mxfile(SIMPLE).replace("<mxfile>", "<mxfile>&b;"))
        exc = self.refused(bomb)
        self.assertEqual(exc.code, "unreadable")
        self.assertNotIn("lolz", str(exc))
        self.assertNotIn("aaaa", str(exc))

    def test_entity_declaration_and_doctype_case_variants_refused(self):
        for head in ('<!doctype mxfile>', '<!DOCTYPE mxfile SYSTEM "file:///etc/hosts">',
                     '<!ENTITY x "y">'):
            with self.subTest(head=head):
                self.assertEqual(self.refused(head + mxfile(SIMPLE)).code, "unreadable")

    def test_doctype_inside_compressed_diagram_refused(self):
        inner = '<!DOCTYPE m [<!ENTITY q "zz">]>' + SIMPLE
        self.assertEqual(self.refused(mxfile(inner, compressed=True)).code, "unreadable")

    def test_decompression_bomb_refused(self):
        big = model(*[v(f"b{i}", "Same words again " * 4, x=i, y=i) for i in range(400)])
        with unittest.mock.patch.object(drawio, "MAX_DECOMPRESSED_BYTES", 4096):
            exc = self.refused(mxfile(big, compressed=True))
        self.assertEqual(exc.code, "unreadable")
        self.assertIn("too large", exc.detail)
        self.assertEqual(len(self.read(mxfile(big, compressed=True)).nodes), 400)    # fine under the real cap

    def test_blob_that_does_not_decode_refused(self):
        for blob in ("this is not base64 !!!", base64.b64encode(b"not a deflate stream at all").decode(),
                     base64.b64encode(zlib.compress(b"x")[2:-4] + b"\xff\xff").decode()):
            with self.subTest(blob=blob[:12]):
                exc = self.refused(f'<mxfile><diagram id="a">{blob}</diagram></mxfile>')
                self.assertEqual((exc.code, exc.detail), ("unreadable", "draw.io diagram could not be decoded"))

    def test_decoded_blob_that_is_not_a_graph_model_refused(self):
        self.assertEqual(self.refused(mxfile("<note>hello</note>", compressed=True)).code, "unreadable")

    def test_no_mxgraphmodel_found_refused(self):
        for xml in ("<mxfile></mxfile>", '<mxfile><diagram id="a"></diagram></mxfile>', "<html><body/></html>",
                    "<svg xmlns='http://www.w3.org/2000/svg'/>"):
            with self.subTest(xml=xml):
                exc = self.refused(xml)
                self.assertEqual((exc.code, exc.detail), ("unreadable", "draw.io diagram could not be decoded"))

    def test_not_xml_or_not_text_refused_without_quoting(self):
        for raw in (b"secret words here not xml", b"\xff\xfe\x00\x01garbage", b"<mxfile><diagram>unclosed",
                    b"<mxfile>&nope;</mxfile>"):
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(MapRefused) as cm:
                    self.read(raw)
                self.assertEqual(cm.exception.code, "unreadable")
                self.assertNotIn("secret", str(cm.exception))

    def test_utf8_bom_and_non_ascii_read(self):
        g = self.read(b"\xef\xbb\xbf" + mxfile(model(v("c2", "Café visit — “ok”", x=0, y=0)))
                      .encode("utf-8"))
        self.assertEqual(g.nodes[0].text, "Café visit — “ok”")

    def test_xml_declaration_with_encoding_ok(self):
        g = self.read('<?xml version="1.0" encoding="UTF-8"?>\n' + mxfile(SIMPLE))
        self.assertEqual(len(g.nodes), 2)


class TestDispatchAndFixtures(Base):
    def test_registered_in_readers(self):
        self.assertIs(READERS[".drawio"], read_drawio)

    def test_read_map_dispatches_drawio(self):
        p = self.dir / "x.DRAWIO"
        p.write_text(mxfile(SIMPLE), encoding="utf-8")
        g = read_map(p, "Q3")
        self.assertEqual((g.map_id, len(g.nodes)), ("Q3", 2))

    def test_fixture_files_plain_and_compressed_agree(self):
        plain = read_drawio(FIXTURES / "journey_plain.drawio", "F", None, RULE)
        comp = read_drawio(FIXTURES / "journey_compressed.drawio", "F", None, RULE)
        self.assertEqual(plain.to_json(), comp.to_json())
        self.assertEqual(plain.meta, {"n_pages": 2, "n_shapes": 5, "n_connectors": 4, "n_missing": 2,
                                      "n_phases": 2})
        self.assertEqual(plain.unresolved_edges, 1)
        self.assertEqual({n.phase for n in plain.nodes}, {"Planning", "Operating", None})


if __name__ == "__main__":
    unittest.main()
