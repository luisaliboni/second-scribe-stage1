"""T8: the Excalidraw map reader (``maps/excalidraw.py``). Documents are built in the tests; one small synthetic
file lives in ``tests/fixtures/maps/``. No real data, and no ids shaped like participant ids."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.maps import readers
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MapRefused, MissingRule, read_map

try:
    from second_scribe.maps.excalidraw import read_excalidraw
except ImportError:                                    # RED: the module does not exist yet
    read_excalidraw = None

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "maps"
RED = MissingRule("red_line_or_text", colors=("#e03131",))
_TMP = Path(tempfile.mkdtemp(prefix="exc-"))


def el(i, t, x=0, y=0, w=100, h=50, **kw):
    d = {"id": i, "type": t, "x": x, "y": y, "width": w, "height": h, "strokeColor": "#1e1e1e",
         "backgroundColor": "transparent", "strokeStyle": "solid", "isDeleted": False, "frameId": None}
    d.update(kw)
    return d


def txt(i, s, container=None, **kw):
    return el(i, "text", text=s, originalText=s, containerId=container, **kw)


def box(i, s, t="rectangle", **kw):
    """A shape plus its bound text element."""
    return [el(i, t, **kw), txt(i + "-t", s, i)]


def arrow(i, a, b, form="new", **kw):
    if form == "new":
        sb = {"elementId": a, "fixedPoint": [1, 0.5], "mode": "orbit"} if a else None
        eb = {"elementId": b, "fixedPoint": [0, 0.5], "mode": "orbit"} if b else None
    else:
        sb = {"elementId": a, "focus": 0.1, "gap": 4} if a else None
        eb = {"elementId": b, "focus": -0.2, "gap": 4} if b else None
    return el(i, kw.pop("kind", "arrow"), points=[[0, 0], [10, 0]], startBinding=sb, endBinding=eb, **kw)


def doc(*groups, **top):
    elements = []
    for g in groups:
        elements.extend(g if isinstance(g, list) else [g])
    d = {"type": "excalidraw", "version": 2, "elements": elements}
    d.update(top)
    return d


def write(d, name="m.excalidraw", raw=None):
    p = _TMP / name
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(json.dumps(d), encoding="utf-8")
    return p


def read(d, rule=None, **kw):
    return read_excalidraw(write(d, **kw), "MAP-A", None, rule or RED)


class TestShapes(unittest.TestCase):
    def test_registered_with_one_line(self):
        self.assertIsNotNone(read_excalidraw)
        self.assertIs(READERS[".excalidraw"], read_excalidraw)

    def test_shape_with_bound_text(self):
        g = read(doc(box("box-a", "Apply for a permit", x=10, y=20, w=100, h=40),
                     box("box-b", "Review", "diamond"), box("box-c", "Visit", "ellipse")))
        self.assertEqual(g.map_id, "MAP-A")
        self.assertEqual(g.origin, "endorsed")
        self.assertEqual([(n.node_id, n.text) for n in g.nodes],
                         [("box-a", "Apply for a permit"), ("box-b", "Review"), ("box-c", "Visit")])
        a = g.node("box-a")
        self.assertEqual((a.x, a.y), (60.0, 40.0))              # centre of the box
        self.assertEqual(a.status, "experienced")
        self.assertIsNone(a.phase)
        self.assertEqual(g.meta["n_shapes"], 3)

    def test_text_is_original_text_and_spaces_folded(self):
        e = box("box-a", "x")
        e[1]["originalText"] = "Apply for   a\npermit"
        e[1]["text"] = "Apply for a\npermit"
        self.assertEqual(read(doc(e)).node("box-a").text, "Apply for a permit")

    def test_only_bound_text_names_a_shape(self):
        g = read(doc(el("box-a", "rectangle"), txt("lone", "elsewhere"),
                     txt("t1", "inside", "box-b"), el("box-b", "rectangle")))
        self.assertEqual([n.node_id for n in g.nodes], ["box-b"])       # box-a has no text

    def test_shape_without_text_is_not_a_node_and_arrows_to_it_are_unresolved(self):
        g = read(doc(box("box-a", "One"), el("box-empty", "rectangle"), arrow("arr", "box-a", "box-empty")))
        self.assertEqual([n.node_id for n in g.nodes], ["box-a"])
        self.assertEqual(g.edges, [])
        self.assertEqual(g.unresolved_edges, 1)
        self.assertEqual(g.meta["n_empty_shapes"], 1)

    def test_deleted_elements_skipped(self):
        g = read(doc(box("box-a", "Kept"),
                     [el("box-d", "rectangle", isDeleted=True), txt("box-d-t", "Gone", "box-d", isDeleted=True)],
                     box("box-e", "Also kept"),
                     arrow("arr-d", "box-a", "box-e", isDeleted=True),
                     el("fr-d", "frame", name="Old", isDeleted=True)))
        self.assertEqual([n.node_id for n in g.nodes], ["box-a", "box-e"])
        self.assertEqual(g.edges, [])
        self.assertEqual(g.unresolved_edges, 0)
        self.assertEqual(g.meta["n_phases"], 0)

    def test_deleted_bound_text_does_not_name_the_shape(self):
        e = [el("box-a", "rectangle"), txt("t-old", "Old name", "box-a", isDeleted=True), txt("t-new", "New name", "box-a")]
        self.assertEqual(read(doc(e)).node("box-a").text, "New name")

    def test_free_text_counted(self):
        g = read(doc(box("box-a", "One"), txt("free-1", "a note"), txt("free-2", "another note"),
                     txt("free-3", "deleted note", isDeleted=True)))
        self.assertEqual(g.meta["n_free_text"], 2)
        self.assertEqual([n.node_id for n in g.nodes], ["box-a"])
        self.assertNotIn("a note", json.dumps(g.meta))

    def test_other_element_types_ignored(self):
        g = read(doc(box("box-a", "One"), el("img", "image"), el("pen", "freedraw"), el("rule", "line")))
        self.assertEqual(len(g.nodes), 1)
        self.assertEqual(g.meta["n_shapes"], 1)


class TestArrows(unittest.TestCase):
    def test_arrow_bindings_old_and_new_forms(self):
        els = [box("box-a", "A"), box("box-b", "B"),
               arrow("arr-new", "box-a", "box-b", "new"), arrow("arr-old", "box-b", "box-a", "old")]
        g = read(doc(*els))
        self.assertEqual([(e.source, e.target, e.glued) for e in g.edges],
                         [("box-a", "box-b", True), ("box-b", "box-a", True)])
        self.assertEqual(g.unresolved_edges, 0)
        self.assertEqual(g.meta["n_connectors"], 2)

    def test_binding_reads_only_element_id(self):
        a = arrow("arr", "box-a", "box-b")
        a["startBinding"] = {"elementId": "box-a", "junk": {"nested": [1, 2]}}
        a["endBinding"] = {"elementId": "box-b"}
        g = read(doc(box("box-a", "A"), box("box-b", "B"), a))
        self.assertEqual(len(g.edges), 1)

    def test_arrow_label(self):
        g = read(doc(box("box-a", "A"), box("box-b", "B"), arrow("arr", "box-a", "box-b"),
                     txt("lbl", "then  we   wait", "arr")))
        self.assertEqual(g.edges[0].label, "then we wait")
        self.assertIsNone(read(doc(box("box-a", "A"), box("box-b", "B"), arrow("arr", "box-a", "box-b"))).edges[0].label)
        self.assertEqual(read(doc(box("box-a", "A"), box("box-b", "B"), arrow("arr", "box-a", "box-b"))).meta["n_free_text"], 0)

    def test_unbound_arrows_count_as_unresolved(self):
        g = read(doc(box("box-a", "A"), box("box-b", "B"),
                     arrow("none", None, None), arrow("start-only", "box-a", None), arrow("end-only", None, "box-b"),
                     arrow("ok", "box-a", "box-b")))
        self.assertEqual(len(g.edges), 1)
        self.assertEqual(g.unresolved_edges, 3)
        self.assertEqual(g.meta["n_connectors"], 4)

    def test_missing_binding_keys_count_as_unbound(self):
        a = arrow("arr", "box-a", "box-b")
        del a["startBinding"]
        a["endBinding"] = {}                              # no elementId
        g = read(doc(box("box-a", "A"), box("box-b", "B"), a))
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))

    def test_binding_must_point_at_a_drawn_shape(self):
        free = txt("free", "free words")
        g = read(doc(box("box-a", "A"), box("box-b", "B"), free,
                     el("fr", "frame", name="F"),
                     el("box-del", "rectangle", isDeleted=True),
                     arrow("to-missing", "box-a", "no-such-id"),
                     arrow("to-text", "box-a", "free"),
                     arrow("to-frame", "box-a", "fr"),
                     arrow("to-deleted", "box-a", "box-del"),
                     arrow("to-arrow", "box-a", "to-text"),
                     arrow("to-self", "to-self", "box-b"),
                     arrow("ok", "box-a", "box-b")))
        self.assertEqual([e.source for e in g.edges], ["box-a"])
        self.assertEqual(g.unresolved_edges, 6)

    def test_binding_of_wrong_type_is_unbound(self):
        a = arrow("arr", "box-a", "box-b")
        a["startBinding"] = "box-a"                       # not an object
        a["endBinding"] = {"elementId": ["box-b"]}        # not a string id
        g = read(doc(box("box-a", "A"), box("box-b", "B"), a))
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))

    def test_line_is_an_edge_only_with_both_bindings(self):
        g = read(doc(box("box-a", "A"), box("box-b", "B"),
                     arrow("ln-ok", "box-a", "box-b", kind="line"),
                     arrow("ln-half", "box-a", None, kind="line"),
                     el("ln-free", "line")))
        self.assertEqual([(e.source, e.target) for e in g.edges], [("box-a", "box-b")])
        self.assertEqual(g.unresolved_edges, 0)            # a loose line is decoration, not a connector
        self.assertEqual(g.meta["n_connectors"], 1)

    def test_edges_keep_file_order(self):
        g = read(doc(box("box-a", "A"), box("box-b", "B"), arrow("z", "box-a", "box-b"), arrow("a", "box-b", "box-a")))
        self.assertEqual([e.source for e in g.edges], ["box-a", "box-b"])

    def test_binding_cost_is_linear(self):
        n = 3000
        els = []
        for k in range(n):
            els.extend(box(f"b{k:05d}", f"Step {k}"))
        for k in range(n - 1):
            els.append(arrow(f"a{k:05d}", f"b{k:05d}", f"b{k + 1:05d}"))
        g = read(doc(els))
        self.assertEqual((len(g.nodes), len(g.edges), g.unresolved_edges), (n, n - 1, 0))


class TestFrames(unittest.TestCase):
    def test_frame_is_phase(self):
        g = read(doc(el("fr-1", "frame", name="Permits"), el("fr-2", "magicframe", name="  Inspections "),
                     box("box-a", "A", frameId="fr-1"), box("box-b", "B", frameId="fr-2"), box("box-c", "C"),
                     box("box-d", "D", frameId="fr-missing")))
        self.assertEqual([n.phase for n in g.nodes], ["Permits", "Inspections", None, None])
        self.assertEqual([n.node_id for n in g.nodes], ["box-a", "box-b", "box-c", "box-d"])   # frames are not nodes
        self.assertEqual(g.meta["n_phases"], 2)

    def test_unnamed_frame_gives_no_phase(self):
        g = read(doc(el("fr-1", "frame", name=None), box("box-a", "A", frameId="fr-1")))
        self.assertIsNone(g.node("box-a").phase)

    def test_arrow_can_not_end_at_a_frame(self):
        g = read(doc(el("fr-1", "frame", name="F"), box("box-a", "A"), arrow("arr", "box-a", "fr-1")))
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))


class TestRules(unittest.TestCase):
    def _g(self, rule):
        els = [box("red-stroke", "Red stroke", strokeColor="#E03131"),
               box("red-fill", "Red fill", backgroundColor="#e03131"),
               box("dashed", "Dashed", strokeStyle="dashed"),
               box("dotted", "Dotted", strokeStyle="dotted"),
               box("plain", "Plain"),
               [el("red-text-box", "rectangle"), txt("rt", "Red text", "red-text-box", strokeColor="#e03131")],
               arrow("arr-red", "plain", "dashed", strokeColor="#e03131"),
               arrow("arr-dashed", "plain", "dotted", strokeStyle="dashed"),
               arrow("arr-plain", "plain", "red-fill")]
        return read(doc(*els), rule)

    def _wished(self, g):
        return ({n.node_id for n in g.nodes if n.status == "wished"},
                {e.source + ">" + e.target for e in g.edges if e.status == "wished"})

    def test_rules_stroke_fill_dashed(self):
        g = self._g(MissingRule("red_line_or_text", colors=("#e03131",)))
        self.assertEqual(self._wished(g), ({"red-stroke", "red-text-box"}, {"plain>dashed"}))
        self.assertEqual(g.meta["n_missing"], 3)
        g = self._g(MissingRule("red_fill", colors=("#e03131",)))
        self.assertEqual(self._wished(g), ({"red-fill"}, set()))
        g = self._g(MissingRule("dashed"))
        self.assertEqual(self._wished(g), ({"dashed", "dotted"}, {"plain>dotted"}))
        self.assertEqual(g.meta["n_missing"], 3)

    def test_colours_compare_as_rrggbb_lower_case(self):
        for stroke, rule_colors in (("#F00", ("#ff0000",)), ("#FF0000", ("#ff0000",)), ("#ff0000", ("#F00",)),
                                    (" #E03131 ", ("#e03131",)), ("#e0313188", ("#e03131",))):
            g = read(doc(box("b", "B", strokeColor=stroke)), MissingRule("red_line_or_text", colors=rule_colors))
            self.assertEqual(g.node("b").status, "wished", (stroke, rule_colors))
        for stroke in ("transparent", "red", "#e03132", "#e0313100", None, 5, "", "#xyz"):
            g = read(doc(box("b", "B", strokeColor=stroke)), MissingRule("red_line_or_text", colors=("#e03131",)))
            self.assertEqual(g.node("b").status, "experienced", stroke)

    def test_default_rule_is_pure_red_and_excalidraw_red(self):
        # coordinator: Excalidraw's palette red is #e03131, so the default rule marks it as well as #ff0000
        g = read_map(write(doc(box("a", "A", strokeColor="#ff0000"), box("b", "B", strokeColor="#e03131"),
                               box("c", "C", strokeColor="#1e1e1e"))), "MAP-A")
        self.assertEqual([n.status for n in g.nodes], ["wished", "wished", "experienced"])
        self.assertEqual(DEFAULT_RULES[".excalidraw"].kind, "red_line_or_text")

    def test_rule_kind_for_another_format_is_refused(self):
        for kind in ("mermaid_class", "classdef_style", "status_column"):
            with self.assertRaises(MapRefused) as cm:
                read(doc(box("a", "A")), MissingRule(kind))
            self.assertEqual(cm.exception.code, "unsupported_format")


class TestRefusals(unittest.TestCase):
    def refused(self, code="unreadable", **kw):
        with self.assertRaises(MapRefused) as cm:
            read_excalidraw(write(kw.pop("d", None), **kw), "MAP-A", None, RED)
        self.assertEqual(cm.exception.code, code)
        return cm.exception

    def test_not_excalidraw_refused(self):
        self.refused(d={"type": "something-else", "elements": []})
        self.refused(d={"elements": []})
        self.refused(d=[1, 2, 3])
        self.refused(d="a string")
        self.refused(d={"type": "excalidraw", "elements": "no"})
        self.refused(d={"type": "excalidraw"})
        self.refused(d={"type": "excalidraw", "elements": [1]})
        self.refused(d={"type": "excalidraw", "elements": [{"type": "rectangle"}]})          # no id
        self.refused(raw=b"not json at all")
        self.refused(raw=b"")
        self.refused(raw=b"\xff\xfe\x00bad")

    def test_duplicate_ids_refused(self):
        self.refused(d=doc(box("same", "A"), box("same", "B")))

    def test_duplicate_keys_refused(self):
        raw = b'{"type": "excalidraw", "elements": [], "elements": []}'
        self.refused(raw=raw)
        raw = (b'{"type": "excalidraw", "elements": [{"id": "a", "type": "rectangle", "x": 1, "x": 2}]}')
        self.refused(raw=raw)

    def test_nan_and_infinity_refused(self):
        for bad in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"):
            raw = (b'{"type": "excalidraw", "elements": [{"id": "a", "type": "rectangle", "x": ' + bad +
                   b', "y": 0, "width": 1, "height": 1}]}')
            self.refused(raw=raw)

    def test_absurd_nesting_refused(self):
        deep = b"[" * 5000 + b"]" * 5000
        self.refused(raw=b'{"type": "excalidraw", "elements": [], "appState": {"x": ' + deep + b"}}")
        n = 60
        nest = b'{"a": ' * n + b"1" + b"}" * n
        self.refused(raw=b'{"type": "excalidraw", "elements": [], "appState": ' + nest + b"}")

    def test_brackets_inside_strings_do_not_count_as_nesting(self):
        s = "[" * 500 + "{" * 500
        g = read(doc(box("a", s)))
        self.assertEqual(g.node("a").text, s)

    def test_ordinary_nesting_is_fine(self):
        d = doc(box("a", "A"), appState={"a": {"b": {"c": [[1, 2], [3, {"d": [4]}]]}}}, files={"f": {"x": [1]}})
        self.assertEqual(len(read(d).nodes), 1)

    def test_too_many_elements_refused(self):
        from second_scribe.maps import excalidraw
        d = doc([el(f"e{k}", "ellipse") for k in range(excalidraw.MAX_ELEMENTS + 1)])
        self.refused(d=d)

    def test_element_count_at_the_limit_is_fine(self):
        from second_scribe.maps import excalidraw
        d = doc([el(f"e{k}", "freedraw") for k in range(excalidraw.MAX_ELEMENTS)])
        self.assertEqual(read(d).nodes, [])

    def test_utf8_bom_accepted(self):
        raw = b"\xef\xbb\xbf" + json.dumps(doc(box("a", "Café visit"))).encode("utf-8")
        self.assertEqual(read_excalidraw(write(None, raw=raw), "MAP-A", None, RED).node("a").text, "Café visit")

    def test_detail_never_quotes_the_file(self):
        secret = "ZEBRA-QUOKKA-17"
        cases = [json.dumps({"type": secret, "elements": []}).encode(),
                 json.dumps({"type": "excalidraw", "elements": [{"id": secret}]}).encode(),
                 json.dumps({"type": "excalidraw", "elements": [{"id": secret, "type": "rectangle"}, 3]}).encode(),
                 b'{"' + secret.encode() + b'": 1, "' + secret.encode() + b'": 2}',
                 b"{" + secret.encode(),
                 json.dumps({"type": "excalidraw", "elements": [box("a", "A")[0], box("a", "A")[0]]})
                 .replace('"a"', '"' + secret + '"').encode()]
        for raw in cases:
            with self.assertRaises(MapRefused) as cm:
                read_excalidraw(write(None, raw=raw), "MAP-A", None, RED)
            self.assertNotIn(secret, cm.exception.detail)
            self.assertNotIn(secret, str(cm.exception))
            self.assertLess(len(cm.exception.detail), 200)

    def test_missing_file_is_unreadable(self):
        with self.assertRaises(MapRefused) as cm:
            read_excalidraw(_TMP / "no-such.excalidraw", "MAP-A", None, RED)
        self.assertEqual(cm.exception.code, "unreadable")


class TestThroughReadMap(unittest.TestCase):
    def test_read_map_dispatches_by_suffix_including_upper_case(self):
        p = write(doc(box("a", "A")), name="UPPER.EXCALIDRAW")
        self.assertEqual(read_map(p, "MAP-Z").map_id, "MAP-Z")

    def test_fixture_file(self):
        g = read_map(FIXTURES / "permit-flow.excalidraw", "SYN-FLOW", None,
                     MissingRule("red_line_or_text", colors=("#e03131",)))
        self.assertEqual([(n.node_id, n.text, n.phase, n.status) for n in g.nodes],
                         [("box-apply", "Apply for a permit", "Permits", "experienced"),
                          ("box-review", "Review the file", "Permits", "experienced"),
                          ("box-visit", "Inspector visit", "Permits", "wished")])
        self.assertEqual([(e.source, e.target, e.label, e.status) for e in g.edges],
                         [("box-apply", "box-review", "then", "experienced"),
                          ("box-review", "box-visit", None, "experienced")])
        self.assertEqual((g.unresolved_edges, g.meta["n_free_text"], g.meta["n_phases"], g.meta["n_missing"]),
                         (0, 1, 1, 1))
        g2 = read_map(FIXTURES / "permit-flow.excalidraw", "SYN-FLOW", None, MissingRule("dashed"))
        self.assertEqual([e.status for e in g2.edges], ["experienced", "wished"])

    def test_result_roundtrips_through_json(self):
        from second_scribe.schemas import MapGraph
        g = read_map(FIXTURES / "permit-flow.excalidraw", "SYN-FLOW", None, RED)
        self.assertEqual(MapGraph.from_json(g.to_json()), g)

    def test_readers_file_has_only_the_one_registration_line(self):
        src = Path(readers.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('READERS[".excalidraw"] = read_excalidraw'), 1)


if __name__ == "__main__":
    unittest.main()
