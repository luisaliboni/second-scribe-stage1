"""T9: the Mermaid flowchart reader (``maps/mermaid.py``). A bounded subset; everything else is refused loudly."""
import tempfile
import time
import unittest
from pathlib import Path

from second_scribe.maps.mermaid import MermaidUnsupported, read_mermaid
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MapRefused, MissingRule, read_map

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "maps"
CLASS_RULE = MissingRule("mermaid_class")
STYLE_RULE = MissingRule("classdef_style")


def _read(text, rule=CLASS_RULE, name="m.mmd"):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / name
        p.write_text(text, encoding="utf-8")
        return read_mermaid(p, "m1", None, rule)


def _refused(testcase, text, rule=CLASS_RULE):
    with testcase.assertRaises(MapRefused) as cm:
        _read(text, rule)
    return cm.exception


def _texts(g):
    return {n.node_id: n.text for n in g.nodes}


def _edges(g):
    return [(e.source, e.target) for e in g.edges]


class TestShapes(unittest.TestCase):
    def test_all_node_shapes(self):
        src = "\n".join([
            "flowchart TD",
            "n1", "n2[square]", "n3(round)", "n4([stadium])", "n5[[sub]]", "n6[(db)]", "n7((circle))",
            "n8(((double)))", "n9>flag]", "n10{rhombus}", "n11{{hexagon}}", "n12[/para/]", "n13[\\alt\\]",
            "n14[/trap\\]", "n15[\\trap2/]", 'n16["quoted (text)"]'])
        g = _read(src)
        self.assertEqual(_texts(g), {
            "n1": "n1", "n2": "square", "n3": "round", "n4": "stadium", "n5": "sub", "n6": "db",
            "n7": "circle", "n8": "double", "n9": "flag", "n10": "rhombus", "n11": "hexagon", "n12": "para",
            "n13": "alt", "n14": "trap", "n15": "trap2", "n16": "quoted (text)"})
        self.assertEqual(g.origin, "endorsed")
        self.assertEqual(g.map_id, "m1")
        self.assertEqual(g.unresolved_edges, 0)

    def test_label_cleanup_and_last_definition_wins(self):
        g = _read("graph TD\nfirst[One<br/>two &amp; #35;three#59;]\nfirst[Final   text]\nsecond[é ü 日本]")
        self.assertEqual(_texts(g)["first"], "Final text")
        self.assertEqual(_texts(g)["second"], "é ü 日本")
        g = _read("graph TD\nfirst[One<br/>two &amp; three #35; x]")
        self.assertEqual(_texts(g)["first"], "One two & three # x")

    def test_end_inside_label_quoted_ok(self):
        g = _read('flowchart TD\nfirst["the end"] --> second[end of road]\nthird["x"]\n')
        self.assertEqual(_texts(g)["first"], "the end")
        self.assertEqual(_texts(g)["second"], "end of road")

    def test_comment_lines_and_semicolons(self):
        g = _read("%% top\nflowchart TD;\n  %% a comment\n  one[One]; two[Two] ;\n  one --> two;\n\n")
        self.assertEqual(_texts(g), {"one": "One", "two": "Two"})
        self.assertEqual(_edges(g), [("one", "two")])

    def test_semicolon_inside_label_kept(self):
        g = _read('flowchart TD\nfirst["a; b"] --> second[c; d]')
        self.assertEqual(_texts(g), {"first": "a; b", "second": "c; d"})


class TestEdges(unittest.TestCase):
    def test_edge_kinds_and_labels(self):
        src = "\n".join([
            "flowchart TD",
            "a1 --> b1", "a2 --- b2", "a3 -.-> b3", "a4 ==> b4", "a5 --o b5", "a6 --x b6", "a7 -.- b7",
            "a8 === b8", "a9 -->|yes| b9", 'a10 -->|"quoted, text"| b10', "a11 -- spoken words --> b11",
            "a12 -. dotted words .-> b12", "a13 == thick words ==> b13", "a14 -- plain --- b14",
            'a15 -- "has --> inside" --> b15'])
        g = _read(src)
        self.assertEqual(len(g.edges), 15)
        labels = {e.source: e.label for e in g.edges}
        self.assertIsNone(labels["a1"])
        self.assertEqual(labels["a9"], "yes")
        self.assertEqual(labels["a10"], "quoted, text")
        self.assertEqual(labels["a11"], "spoken words")
        self.assertEqual(labels["a12"], "dotted words")
        self.assertEqual(labels["a13"], "thick words")
        self.assertEqual(labels["a14"], "plain")
        self.assertEqual(labels["a15"], "has --> inside")
        self.assertTrue(all(e.status == "experienced" and e.glued for e in g.edges))

    def test_inline_text_closer_needs_white_space_before_it(self):
        g = _read("flowchart TD\nfirst -- see foo--x bar --> second")
        self.assertEqual(g.edges[0].label, "see foo--x bar")

    def test_bidirectional_makes_two_edges(self):
        g = _read("flowchart TD\nleft <--> right\nleft <-.-> other\nleft <==> third")
        self.assertEqual(_edges(g), [("left", "right"), ("right", "left"), ("left", "other"), ("other", "left"),
                                     ("left", "third"), ("third", "left")])
        self.assertEqual(g.meta["n_bidirectional"], 3)
        self.assertEqual(g.meta["n_connectors"], 3)

    def test_chain_and_ampersand(self):
        g = _read("flowchart TD\none --> two --> three\nfoo & bar --> baz & qux\nsolo")
        self.assertEqual(_edges(g), [("one", "two"), ("two", "three"), ("foo", "baz"), ("foo", "qux"),
                                     ("bar", "baz"), ("bar", "qux")])
        self.assertEqual([n.node_id for n in g.nodes], ["one", "two", "three", "foo", "bar", "baz", "qux", "solo"])

    def test_hyphen_inside_id_and_adjacent_arrow(self):
        g = _read("flowchart TD\nstep-one-->step-two\nx1-->y1")
        self.assertEqual(_edges(g), [("step-one", "step-two"), ("x1", "y1")])

    def test_o_and_x_glued_to_dashes_is_refused_not_guessed(self):
        e = _refused(self, "flowchart TD\nfirst---other")
        self.assertIsInstance(e, MermaidUnsupported)
        self.assertEqual(e.line_no, 2)


class TestSubgraphs(unittest.TestCase):
    def test_subgraph_phases_nested(self):
        src = "\n".join([
            "flowchart TB",
            "outside[Outside]",
            "subgraph outer [Outer phase]",
            "  direction LR",
            "  one[One]",
            "  subgraph inner",
            "    two[Two]",
            "  end",
            "  three[Three]",
            "end",
            'subgraph Spaced Title Words',
            "  four[Four]",
            "end",
            'subgraph quoted_id ["Quoted Title"]',
            "  five[Five]",
            "end",
            "outside --> one --> two --> three --> four --> five"])
        g = _read(src)
        phase = {n.node_id: n.phase for n in g.nodes}
        self.assertEqual(phase, {"outside": None, "one": "Outer phase", "two": "inner", "three": "Outer phase",
                                 "four": "Spaced Title Words", "five": "Quoted Title"})
        self.assertEqual(g.meta["n_phases"], 4)

    def test_node_mentioned_in_inner_after_outer_gets_inner(self):
        g = _read("flowchart TB\nsubgraph outer\n  one\n  subgraph inner\n    one\n  end\nend")
        self.assertEqual(g.nodes[0].phase, "inner")

    def test_node_in_two_subgraphs_refused(self):
        e = _refused(self, "flowchart TB\nsubgraph s1\n  one\nend\nsubgraph s2\n  one\nend")
        self.assertIsInstance(e, MermaidUnsupported)
        self.assertEqual(e.line_no, 6)

    def test_unclosed_and_stray_end(self):
        e = _refused(self, "flowchart TB\nsubgraph s1\n  one")
        self.assertIsInstance(e, MermaidUnsupported)
        e = _refused(self, "flowchart TB\none\nend")
        self.assertEqual(e.line_no, 3)

    def test_edge_to_subgraph_id_refused(self):
        e = _refused(self, "flowchart TB\nsubgraph block1\n  one\nend\nblock1 --> two")
        self.assertIsInstance(e, MermaidUnsupported)

    def test_direction_outside_subgraph_refused(self):
        _refused(self, "flowchart TB\ndirection LR\none")


class TestMissingRule(unittest.TestCase):
    def test_class_missing_rule(self):
        src = ("flowchart TD\nfirst[One]:::missing --> second[Two]\nthird[Three]\nclass second,third missing\n"
               "fourth:::other\nclass fourth other\nclassDef missing stroke:#f00\nclassDef other fill:#eee")
        g = _read(src)
        self.assertEqual({n.node_id: n.status for n in g.nodes},
                         {"first": "wished", "second": "wished", "third": "wished", "fourth": "experienced"})
        self.assertEqual(g.meta["n_missing"], 3)

    def test_class_rule_custom_names_and_case(self):
        rule = MissingRule("mermaid_class", class_names=("gap", "absent"))
        g = _read("flowchart TD\none:::gap\ntwo:::Gap\nthree:::absent\nfour:::missing", rule)
        self.assertEqual([n.status for n in g.nodes], ["wished", "experienced", "wished", "experienced"])

    def test_class_before_node_declared_is_applied(self):
        g = _read("flowchart TD\nclass later missing\nlater[Later]")
        self.assertEqual(g.nodes[0].status, "wished")

    def test_class_on_unknown_node_refused(self):
        e = _refused(self, "flowchart TD\none\nclass nothere missing")
        self.assertEqual(e.line_no, 3)

    def test_classdef_style_rule(self):
        src = "\n".join([
            "flowchart TD",
            "red1:::redhex", "red2:::redshort", "red3:::rednamed", "dash:::dashed", "solid:::solid",
            "blue:::bluehex", "styled", "plain", "upper:::redupper", "zero:::zerodash", "dflt",
            "classDef redhex fill:#fff,stroke:#ff0000,stroke-width:2px",
            "classDef redshort stroke:#f00",
            "classDef rednamed stroke:red",
            "classDef dashed stroke:#333,stroke-dasharray: 5 5",
            "classDef solid stroke:#333,stroke-dasharray: none",
            "classDef zerodash stroke-dasharray: 0",
            "classDef bluehex stroke:#0000ff",
            "classDef redupper stroke:#FF0000 !important",
            "style styled stroke:#f00,fill:#fff"])
        g = _read(src, STYLE_RULE)
        st = {n.node_id: n.status for n in g.nodes}
        self.assertEqual({k for k, v in st.items() if v == "wished"},
                         {"red1", "red2", "red3", "dash", "styled", "upper"})

    def test_classdef_style_style_line_overrides_class_and_default_applies(self):
        src = ("flowchart TD\none:::redcls\ntwo\nthree\nclassDef redcls stroke:#f00\nstyle one stroke:#0000ff\n"
               "classDef default stroke:#f00\nstyle three stroke:#0000ff")
        g = _read(src, STYLE_RULE)
        self.assertEqual({n.node_id: n.status for n in g.nodes},
                         {"one": "experienced", "two": "wished", "three": "experienced"})

    def test_classdef_rule_custom_colors(self):
        rule = MissingRule("classdef_style", colors=("#b8442f",))
        g = _read("flowchart TD\none:::a\ntwo:::b\nclassDef a stroke:#b8442f\nclassDef b stroke:#ff0000", rule)
        self.assertEqual([n.status for n in g.nodes], ["wished", "experienced"])

    def test_classdef_rule_ignores_class_name_and_class_rule_ignores_style(self):
        src = "flowchart TD\none:::missing\ntwo:::paint\nclassDef paint stroke:#f00"
        self.assertEqual([n.status for n in _read(src, STYLE_RULE).nodes], ["experienced", "wished"])
        self.assertEqual([n.status for n in _read(src, CLASS_RULE).nodes], ["wished", "experienced"])

    def test_linkstyle_marks_edge(self):
        src = ("flowchart TD\none --> two\ntwo --> three\nthree --> four\nfour --> five\n"
               "linkStyle 1 stroke:#ff0000\nlinkStyle 2,3 stroke-dasharray: 3 3\nlinkStyle 0 stroke:#0000ff")
        for rule in (CLASS_RULE, STYLE_RULE):
            g = _read(src, rule)
            self.assertEqual([e.status for e in g.edges], ["experienced", "wished", "wished", "wished"])

    def test_linkstyle_default_and_override(self):
        src = "flowchart TD\none --> two\ntwo --> three\nlinkStyle default stroke:#f00\nlinkStyle 1 stroke:#00f"
        g = _read(src)
        self.assertEqual([e.status for e in g.edges], ["wished", "experienced"])

    def test_linkstyle_index_counts_mermaid_edges(self):
        src = ("flowchart TD\nfoo & bar --> baz\nleft <--> right\nlast --> end1\n"
               "linkStyle 1 stroke:#f00\nlinkStyle 2 stroke:#f00\nlinkStyle 3 stroke:#f00")
        g = _read(src)
        wished = {(e.source, e.target) for e in g.edges if e.status == "wished"}
        self.assertEqual(wished, {("bar", "baz"), ("left", "right"), ("right", "left"), ("last", "end1")})

    def test_linkstyle_out_of_range_refused(self):
        e = _refused(self, "flowchart TD\none --> two\nlinkStyle 5 stroke:#f00")
        self.assertEqual(e.line_no, 3)

    def test_fixture_file_through_read_map(self):
        g = read_map(FIXTURES / "permit_flow.mmd", "fx")
        self.assertEqual(g.map_id, "fx")
        ph = {n.node_id: n.phase for n in g.nodes}
        self.assertEqual(ph["start"], "Planning")
        self.assertEqual(ph["review"], "Permits")
        self.assertIsNone(ph["build"])
        self.assertEqual({n.node_id for n in g.nodes if n.status == "wished"}, {"chaser", "wish"})
        self.assertEqual([(e.source, e.target) for e in g.edges if e.status == "wished"], [("wish", "build")])
        self.assertEqual(g.meta["n_missing"], 3)       # two boxes and one arrow
        self.assertEqual(len(g.nodes), 7)
        self.assertEqual(len(g.edges), 7)
        st = {n.node_id: n.text for n in g.nodes}
        self.assertEqual(st["start"], "Decide to build")
        self.assertEqual([e.label for e in g.edges if e.label], ["approved", "submit"])

    def test_rule_kind_not_for_mermaid_refused(self):
        for kind in ("red_line_or_text", "red_fill", "dashed", "status_column"):
            e = _refused(self, "flowchart TD\none", MissingRule(kind))
            self.assertEqual(e.code, "unsupported_format", kind)


class TestRefusals(unittest.TestCase):
    def test_at_shape_refused_names_line(self):
        with self.assertRaises(MermaidUnsupported) as cm:
            read_mermaid(FIXTURES / "unsupported_at_shape.mmd", "m", None, CLASS_RULE)
        e = cm.exception
        self.assertEqual(e.line_no, 3)
        self.assertEqual(e.code, "unsupported_format")
        self.assertIn("line 3", e.detail)
        self.assertIn("@{", e.detail)
        self.assertIsInstance(e, MapRefused)

    def test_named_constructs(self):
        cases = [
            ("flowchart TD\none\nclick one callback", 3, "click"),
            ("flowchart TD\none --> two\ntwo -->:::missing three", 3, ":::"),
            ("flowchart TD\none e1@--> two", 2, "edge id"),
            ("flowchart TD\none e1@==> two", 2, "edge id"),
        ]
        for src, line, word in cases:
            e = _refused(self, src)
            self.assertIsInstance(e, MermaidUnsupported, src)
            self.assertEqual(e.line_no, line, src)
            self.assertIn(word, e.detail, src)

    def test_unparseable_lines_refused(self):
        bad = ["flowchart TD\none --> ", "flowchart TD\none[unclosed", "flowchart TD\n--> two",
               "flowchart TD\none two", "flowchart TD\none --> two garbage", "flowchart TD\none [spaced]",
               "flowchart TD\none --->> two", "flowchart TD\nclassDef", "flowchart TD\nclass one",
               "flowchart TD\nlinkStyle x stroke:#f00\none --> two", "flowchart TD\none:::a:::b",
               "flowchart TD\none:::", "flowchart TD\nstyle one", "flowchart TD\none@two",
               "flowchart TD\nsubgraph", "flowchart TD\nbox.id", "flowchart TD\nflowchart LR",
               "flowchart TD\naccTitle: x", "flowchart TD\nA o--o B"]
        for src in bad:
            with self.assertRaises(MapRefused, msg=src):
                _read(src)

    def test_not_a_flowchart_refused(self):
        for head in ("sequenceDiagram", "classDiagram", "stateDiagram-v2", "gantt", "erDiagram", "mindmap",
                     "flowchart-elk TD", "flowchart XX", "graph"):
            if head == "graph":
                _read("graph\none")            # direction is optional
                continue
            e = _refused(self, head + "\none --> two")
            self.assertEqual(e.code, "unsupported_format", head)
            self.assertEqual(e.line_no, 1, head)

    def test_no_header_and_empty_and_frontmatter(self):
        self.assertEqual(_refused(self, "one --> two").code, "unsupported_format")
        self.assertEqual(_refused(self, "").code, "unreadable")
        self.assertEqual(_refused(self, "%% only comments\n").code, "unreadable")
        self.assertEqual(_refused(self, "flowchart TD\n%% nothing drawn").code, "unreadable")
        e = _refused(self, "---\ntitle: x\n---\nflowchart TD\none")
        self.assertIsInstance(e, MermaidUnsupported)
        self.assertEqual(e.line_no, 1)

    def test_detail_never_quotes_file_content(self):
        secret = "zebraquartz"
        lines = [f"flowchart TD\none --> {secret} garbage", f"flowchart TD\n{secret}[unclosed",
                 f"flowchart TD\n{secret}@{{ shape: cloud }}", f"flowchart TD\nclick {secret} cb",
                 f"flowchart TD\nclass {secret} {secret}", f"flowchart TD\n{secret} e1@--> two",
                 f"{secret}Diagram\none", f"flowchart TD\nsubgraph s\n{secret}\nend\nsubgraph t\n{secret}\nend",
                 f"flowchart TD\n{secret}:::{secret} -->{secret}:::x"]
        for src in lines:
            try:
                _read(src)
            except MapRefused as e:
                self.assertNotIn(secret, str(e))
                self.assertNotIn(secret, e.detail)

    def test_not_utf8_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.mmd"
            p.write_bytes(b"flowchart TD\none[\xff\xfe]")
            with self.assertRaises(MapRefused) as cm:
                read_mermaid(p, "m", None, CLASS_RULE)
            self.assertEqual(cm.exception.code, "unreadable")

    def test_bom_and_crlf_ok(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.mmd"
            p.write_bytes("﻿flowchart TD\r\none[One] --> two[Two]\r\n".encode("utf-8"))
            g = read_mermaid(p, "m", None, CLASS_RULE)
            self.assertEqual(_edges(g), [("one", "two")])

    def test_no_eval_in_source(self):
        src = (Path(__file__).resolve().parent.parent / "second_scribe" / "maps" / "mermaid.py").read_text()
        for word in ("eval(", "exec(", "compile(", "__import__"):
            self.assertNotIn(word, src)


class TestBounds(unittest.TestCase):
    def test_pathological_inputs_finish_fast(self):
        n = 3000
        cases = ["flowchart TD\na" + "[" * n, "flowchart TD\na" + "(" * n + "x", "flowchart TD\n" + "a --" * n,
                 "flowchart TD\n" + "-" * n, "flowchart TD\na " + "-- x " * n + "--> b",
                 "flowchart TD\n" + "a & " * n + "b", 'flowchart TD\na["' + "x" * n, "flowchart TD\n" + "a:" * n,
                 "flowchart TD\n" + "subgraph s\n" * 500, "flowchart TD\n" + "classDef a " + "x:y," * n,
                 "flowchart TD\na -->|" + "|" * n, "flowchart TD\na --> b" + " --> b" * n,
                 "flowchart TD\n" + "a[b]" * n]
        for src in cases:
            t0 = time.time()
            try:
                _read(src)
            except MapRefused:
                pass
            self.assertLess(time.time() - t0, 3.0, src[:40])

    def test_edge_explosion_refused(self):
        left = " & ".join(f"l{i}" for i in range(400))
        right = " & ".join(f"r{i}" for i in range(400))
        e = _refused(self, f"flowchart TD\n{left} --> {right}")
        self.assertEqual(e.code, "unreadable")

    def test_long_line_refused(self):
        e = _refused(self, "flowchart TD\nfirst[" + "x" * 30000 + "]")
        self.assertEqual(e.code, "unreadable")


class TestRegistration(unittest.TestCase):
    def test_registered_and_dispatched(self):
        self.assertIn(".mmd", READERS)
        self.assertEqual(DEFAULT_RULES[".mmd"], CLASS_RULE)
        g = read_map(FIXTURES / "permit_flow.mmd", "viaread")
        self.assertEqual(g.map_id, "viaread")
        g2 = read_map(FIXTURES / "permit_flow.mmd", "viaread", None, MissingRule("classdef_style"))
        self.assertEqual({n.node_id for n in g2.nodes if n.status == "wished"}, {"chaser", "wish"})

    def test_uppercase_suffix_dispatches(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "M.MMD"
            p.write_text("flowchart TD\none --> two", encoding="utf-8")
            self.assertEqual(len(read_map(p, "u").nodes), 2)


if __name__ == "__main__":
    unittest.main()
