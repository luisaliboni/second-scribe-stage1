import unittest, tempfile, time, zipfile
from pathlib import Path
from second_scribe.maps.vsdx_writer import write_vsdx
from second_scribe.maps.vsdx import extract_vsdx, signature_counts, ExtractConfig, type_rules_from_legend


def fixture(tmp):
    shapes = [
        {"id": "1", "text": "Early Concerns", "pin": (1, 8)},
        {"id": "2", "text": "Diagnosis", "pin": (5, 8)},
        {"id": "3", "text": "Family doctor  visit", "pin": (1.2, 5), "line": "#205867", "w": 1, "h": 0.5},
        {"id": "4", "text": "Waitlist", "pin": (4.8, 5), "w": 1, "h": 0.5},
        {"id": "5", "text": "One coordinator", "pin": (5.1, 3), "line": "#FF0000", "w": 1, "h": 0.5},
        {"id": "6", "text": "Word: Exhausing", "pin": (8, 1)},
        {"id": "7", "text": "Actor", "pin": (9, 1)},                       # legend
        {"id": "8", "text": "Anything in red is the ideal", "pin": (9, 0.5)},
        {"id": "9", "connector": True, "begin": (1.7, 5), "end": (4.3, 5)},  # glued both
        {"id": "10", "connector": True, "begin": (4.8, 4.75), "end": (5.1, 3.25), "line": "#ff0000"},  # unglued, geometry
        {"id": "11", "connector": True, "begin": (20, 20), "end": (21, 21)}, # dangling
    ]
    connects = [{"from_sheet": "9", "from_cell": "BeginX", "to_sheet": "3"},
                {"from_sheet": "9", "from_cell": "EndX", "to_sheet": "4"}]
    p = Path(tmp) / "m.vsdx"; write_vsdx(p, shapes, connects); return p


class TestVsdx(unittest.TestCase):
    def setUp(self): self.g = extract_vsdx(fixture(tempfile.mkdtemp()), "SYN-OW01")

    def test_nodes_exclude_legend_headers_emotion(self):
        self.assertEqual(sorted(n.text for n in self.g.nodes), ["Family doctor visit", "One coordinator", "Waitlist"])

    def test_emotion_word_keeps_typo(self):
        self.assertEqual(self.g.emotion_word, "Exhausing")

    def test_red_node_is_wished(self):
        st = {n.text: n.status for n in self.g.nodes}
        self.assertEqual(st["One coordinator"], "wished"); self.assertEqual(st["Waitlist"], "experienced")

    def test_phases_vertical_lanes(self):
        ph = {n.text: n.phase for n in self.g.nodes}
        self.assertEqual(ph["Family doctor visit"], "Early Concerns"); self.assertEqual(ph["Waitlist"], "Diagnosis")

    def test_edges_glued_geometry_unresolved(self):
        kinds = sorted((e.glued, e.status) for e in self.g.edges)
        self.assertEqual(kinds, [(False, "wished"), (True, "experienced")])
        self.assertEqual(self.g.unresolved_edges, 1)

    def test_edge_endpoints_and_meta(self):
        by_text = {n.node_id: n.text for n in self.g.nodes}
        glued = [e for e in self.g.edges if e.glued][0]
        self.assertEqual((by_text[glued.source], by_text[glued.target]), ("Family doctor visit", "Waitlist"))
        geom = [e for e in self.g.edges if not e.glued][0]
        self.assertEqual((by_text[geom.source], by_text[geom.target]), ("Waitlist", "One coordinator"))
        m = self.g.meta
        self.assertEqual((m["n_connectors"], m["n_glued_edges"], m["n_geometry_edges"], m["n_legend"]), (3, 1, 1, 2))
        self.assertEqual(m["n_phase_headers"], 2)
        self.assertEqual(m["phase_orientation"], "vertical")
        self.assertEqual(self.g.origin, "endorsed")


def write(shapes, connects=(), masters=None):
    p = Path(tempfile.mkdtemp()) / "x.vsdx"
    write_vsdx(p, shapes, list(connects), masters)
    return p


class TestVsdxEdgeCases(unittest.TestCase):
    def test_horizontal_lanes_nearest_y(self):
        g = extract_vsdx(write([
            {"id": "1", "text": "Diagnosis", "pin": (0, 8)},
            {"id": "2", "text": "School", "pin": (0, 2)},
            {"id": "3", "text": "A", "pin": (5, 7)},
            {"id": "4", "text": "B", "pin": (5, 3)},
        ]), "m")
        self.assertEqual({n.text: n.phase for n in g.nodes}, {"A": "Diagnosis", "B": "School"})
        self.assertEqual(g.meta["phase_orientation"], "horizontal")

    def test_no_headers_means_no_phase(self):
        g = extract_vsdx(write([{"id": "1", "text": "A", "pin": (1, 1)}]), "m")
        self.assertIsNone(g.nodes[0].phase); self.assertIsNone(g.meta["phase_orientation"])

    def test_header_match_is_case_insensitive_and_text_keeps_case(self):
        g = extract_vsdx(write([
            {"id": "1", "text": "early concerns", "pin": (1, 8)},
            {"id": "2", "text": "Diagnosis", "pin": (5, 8)},
            {"id": "3", "text": "Mixed CASE text", "pin": (1, 5)},
        ]), "m")
        self.assertEqual(g.nodes[0].text, "Mixed CASE text")
        self.assertEqual(g.nodes[0].phase, "Early Concerns")

    def test_emotion_box_with_possessive_token_before_prefix(self):
        g = extract_vsdx(write([{"id": "1", "text": "Alex's Word: Tired", "pin": (1, 1)},
                                {"id": "2", "text": "Word of mouth", "pin": (2, 1)}]), "m")
        self.assertEqual(g.emotion_word, "Tired")
        self.assertEqual([n.text for n in g.nodes], ["Word of mouth"])

    def test_whitespace_only_shapes_are_ignored(self):
        g = extract_vsdx(write([{"id": "1", "text": "   ", "pin": (1, 1)},
                                {"id": "2", "text": "A", "pin": (2, 2)}]), "m")
        self.assertEqual([n.text for n in g.nodes], ["A"])

    def test_type_rules_priority_master_fill_line(self):
        cfg = ExtractConfig(type_rules={"master:Process": "service", "fill:#c4d6a0": "actor",
                                        "line:#205867": "scenario"})
        shapes = [
            {"id": "1", "text": "m", "pin": (1, 1), "master": "1", "fill": "#c4d6a0", "line": "#205867"},
            {"id": "2", "text": "f", "pin": (2, 1), "fill": "#C4D6A0", "line": "#205867"},
            {"id": "3", "text": "l", "pin": (3, 1), "line": "#205867"},
            {"id": "4", "text": "o", "pin": (4, 1)},
        ]
        g = extract_vsdx(write(shapes, masters={"1": "Process"}), "m", cfg)
        self.assertEqual({n.text: n.node_type for n in g.nodes},
                         {"m": "service", "f": "actor", "l": "scenario", "o": "other"})

    def test_master_named_connector_is_a_connector_not_a_node(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1)}, {"id": "2", "text": "B", "pin": (5, 1)},
                  {"id": "3", "text": "label", "pin": (3, 1), "master": "2"}]
        g = extract_vsdx(write(shapes, masters={"2": "Dynamic connector"}), "m")
        self.assertEqual(sorted(n.text for n in g.nodes), ["A", "B"])
        self.assertEqual(g.meta["n_connectors"], 1)

    def test_connector_glued_to_connector_resolves_one_hop(self):
        shapes = [
            {"id": "1", "text": "A", "pin": (1, 1)}, {"id": "2", "text": "B", "pin": (9, 1)},
            {"id": "3", "text": "C", "pin": (5, 9)},
            {"id": "4", "connector": True, "begin": (1, 1), "end": (9, 1)},       # A -> B
            {"id": "5", "connector": True, "begin": (5, 9), "end": (5, 1)},       # C -> (line 4)
        ]
        connects = [{"from_sheet": "4", "from_cell": "BeginX", "to_sheet": "1"},
                    {"from_sheet": "4", "from_cell": "EndX", "to_sheet": "2"},
                    {"from_sheet": "5", "from_cell": "BeginX", "to_sheet": "3"},
                    {"from_sheet": "5", "from_cell": "EndX", "to_sheet": "4"}]
        g = extract_vsdx(write(shapes, connects), "m")
        names = {n.node_id: n.text for n in g.nodes}
        self.assertEqual(sorted((names[e.source], names[e.target]) for e in g.edges), [("A", "B"), ("C", "B")])
        self.assertEqual(g.unresolved_edges, 0)

    def test_connector_glued_to_nonnode_is_unresolved(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1)}, {"id": "2", "text": "Actor", "pin": (9, 9)},
                  {"id": "3", "connector": True, "begin": (1, 1), "end": (9, 9)}]
        connects = [{"from_sheet": "3", "from_cell": "BeginX", "to_sheet": "1"},
                    {"from_sheet": "3", "from_cell": "EndX", "to_sheet": "2"}]
        g = extract_vsdx(write(shapes, connects), "m")
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))

    def test_ambiguous_geometry_is_unresolved(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1), "w": 1, "h": 1},
                  {"id": "2", "text": "B", "pin": (1.3, 1), "w": 1, "h": 1},   # overlaps A
                  {"id": "3", "text": "C", "pin": (6, 1), "w": 1, "h": 1},
                  {"id": "4", "connector": True, "begin": (1.2, 1), "end": (6, 1)}]
        g = extract_vsdx(write(shapes), "m")
        self.assertEqual((len(g.edges), g.unresolved_edges), (0, 1))

    def test_services_legend_vs_phase_header(self):
        # "Services" is both a legend label and a phase name: the one lined up with the other
        # phase headers is a header; the one off in the legend corner is a legend entry.
        shapes = [
            {"id": "1", "text": "Early Concerns", "pin": (1, 8)},
            {"id": "2", "text": "Services", "pin": (7, 8)},
            {"id": "3", "text": "School", "pin": (11, 8)},
            {"id": "4", "text": "Services", "pin": (14, 1)},
            {"id": "5", "text": "N", "pin": (7.2, 4)},
        ]
        g = extract_vsdx(write(shapes), "m")
        self.assertEqual([(n.text, n.phase) for n in g.nodes], [("N", "Services")])
        self.assertEqual(g.meta["n_legend"], 1)

    def test_multi_page_ids_are_prefixed_and_distinct(self):
        p = Path(tempfile.mkdtemp()) / "mp.vsdx"
        write_vsdx(p, [{"id": "1", "text": "A", "pin": (1, 1)}], [])
        # add a second page by hand
        with zipfile.ZipFile(p) as z:
            items = {n: z.read(n) for n in z.namelist()}
        items["visio/pages/page2.xml"] = items["visio/pages/page1.xml"].replace(b">A<", b">B<")
        with zipfile.ZipFile(p, "w") as z:
            for n, b in items.items(): z.writestr(n, b)
        g = extract_vsdx(p, "m")
        self.assertEqual(sorted(n.text for n in g.nodes), ["A", "B"])
        self.assertEqual(len({n.node_id for n in g.nodes}), 2)

    def test_signature_counts_only_text_bearing_shapes(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1), "master": "1", "line": "#205867", "fill": "#c4d6a0"},
                  {"id": "2", "text": "B", "pin": (2, 1), "master": "1", "line": "#205867", "fill": "#c4d6a0"},
                  {"id": "3", "text": "", "pin": (3, 1), "master": "1"}]
        sc = signature_counts(write(shapes, masters={"1": "Process"}))
        self.assertEqual(sc, {"Process|#c4d6a0|#205867": 2})

    def test_signature_counts_never_leaks_long_text(self):
        long_name = "x" * 60
        sc = signature_counts(write([{"id": "1", "text": "A", "pin": (1, 1), "master": "1"}],
                                    masters={"1": long_name}))
        self.assertEqual(list(sc), ["<long>|-|-"])


def legend_shapes(extra=()):
    """A synthetic per-map legend: one swatch per type, each a different stencil master."""
    base = [
        {"id": "L1", "text": "Services", "pin": (20, 1), "master": "d"},
        {"id": "L2", "text": "Information & Source", "pin": (20, 2), "master": "c"},
        {"id": "L3", "text": "Actor", "pin": (20, 3), "master": "p"},
        {"id": "L4", "text": "Scenario", "pin": (20, 4), "master": "o"},
        {"id": "L5", "text": "Actor + Information Source", "pin": (20, 5), "master": "p", "line": "#c4d6a0"},
    ]
    return base + list(extra)


LEGEND_MASTERS = {"d": "Data", "c": "Custom 4", "p": "Process", "o": "On-page reference"}


class TestPhasesNeedTwoHeaders(unittest.TestCase):
    def test_single_header_gives_no_phase(self):
        g = extract_vsdx(write([{"id": "1", "text": "Diagnosis", "pin": (1, 8)},
                                {"id": "2", "text": "A", "pin": (1, 5)},
                                {"id": "3", "text": "B", "pin": (9, 5)}]), "m")
        self.assertEqual([n.phase for n in g.nodes], [None, None])
        self.assertIsNone(g.meta["phase_orientation"])
        self.assertEqual([n.text for n in g.nodes], ["A", "B"])      # the header is still not a node
        self.assertEqual(g.meta["n_phase_headers"], 1)

    def test_two_headers_give_phases(self):
        g = extract_vsdx(write([{"id": "1", "text": "Diagnosis", "pin": (1, 8)},
                                {"id": "2", "text": "School", "pin": (9, 8)},
                                {"id": "3", "text": "A", "pin": (1, 5)}]), "m")
        self.assertEqual(g.nodes[0].phase, "Diagnosis")
        self.assertEqual(g.meta["phase_orientation"], "vertical")


class TestLegendTypes(unittest.TestCase):
    def nodes_fixture(self):
        return [
            {"id": "1", "text": "a data box", "pin": (1, 1), "master": "d"},
            {"id": "2", "text": "an actor box", "pin": (2, 1), "master": "p"},
            {"id": "3", "text": "a red actor box", "pin": (3, 1), "master": "p", "line": "#FF0000"},
            {"id": "4", "text": "actor and info", "pin": (4, 1), "master": "p", "line": "#C4D6A0"},
            {"id": "5", "text": "info box", "pin": (5, 1), "master": "c"},
            {"id": "6", "text": "scenario box", "pin": (6, 1), "master": "o"},
            {"id": "7", "text": "unknown style", "pin": (7, 1), "master": "p", "fill": "#eeeaf2", "line": "#205867"},
            {"id": "8", "text": "no master", "pin": (8, 1)},
        ]

    def test_types_come_from_the_maps_own_legend(self):
        g = extract_vsdx(write(legend_shapes(self.nodes_fixture()), masters=LEGEND_MASTERS), "m")
        got = {n.text: n.node_type for n in g.nodes}
        self.assertEqual(got, {"a data box": "service", "an actor box": "actor", "a red actor box": "actor",
                               "actor and info": "actor_information_source", "info box": "information_source",
                               "scenario box": "scenario", "unknown style": "other", "no master": "other"})
        self.assertEqual(g.meta["legend_types_found"], 5)
        self.assertEqual(g.meta["ambiguous_type_signatures"], 0)
        self.assertEqual(g.meta["n_legend"], 5)        # legend swatches stay out of the nodes

    def test_red_node_is_still_wished_and_typed(self):
        g = extract_vsdx(write(legend_shapes(self.nodes_fixture()), masters=LEGEND_MASTERS), "m")
        n = [n for n in g.nodes if n.text == "a red actor box"][0]
        self.assertEqual((n.status, n.node_type), ("wished", "actor"))

    def test_identical_signatures_are_ambiguous_and_left_out(self):
        shapes = [{"id": "L1", "text": "Services", "pin": (20, 1), "master": "p"},
                  {"id": "L3", "text": "Actor", "pin": (20, 3), "master": "p"},
                  {"id": "L4", "text": "Scenario", "pin": (20, 4), "master": "o"},
                  {"id": "1", "text": "p box", "pin": (1, 1), "master": "p"},
                  {"id": "2", "text": "o box", "pin": (2, 1), "master": "o"}]
        g = extract_vsdx(write(shapes, masters=LEGEND_MASTERS), "m")
        self.assertEqual({n.text: n.node_type for n in g.nodes}, {"p box": "other", "o box": "scenario"})
        self.assertEqual(g.meta["ambiguous_type_signatures"], 1)

    def test_cfg_type_rules_apply_after_legend_rules(self):
        shapes = legend_shapes([{"id": "1", "text": "p", "pin": (1, 1), "master": "p"},
                                {"id": "2", "text": "other", "pin": (2, 1), "master": "z"}])
        cfg = ExtractConfig(type_rules={"master:Zed": "service", "master:Process": "scenario"})
        g = extract_vsdx(write(shapes, masters={**LEGEND_MASTERS, "z": "Zed"}), "m", cfg)
        self.assertEqual({n.text: n.node_type for n in g.nodes}, {"p": "actor", "other": "service"})

    def test_type_rules_from_legend_returns_rules_and_ambiguity_count(self):
        from second_scribe.maps import vsdx as V
        rules, n_amb = type_rules_from_legend([
            V._Shape(sid="1", text="Actor", master="Process"),
            V._Shape(sid="2", text="not a type label", master="Data"),
            V._Shape(sid="3", text="Scenario", master="Same"), V._Shape(sid="4", text="Services", master="Same")])
        self.assertEqual(rules, {"sig:Process|-|-": "actor"})
        self.assertEqual(n_amb, 1)

    def test_legend_services_vs_services_header_far_from_other_headers(self):
        # Real maps put a "Services" phase header away from the other headers; the legend entry is
        # the one that sits with the other legend labels.
        shapes = [
            {"id": "1", "text": "Early Concerns", "pin": (1, 8)},
            {"id": "2", "text": "Diagnosis", "pin": (5, 8)},
            {"id": "3", "text": "Services", "pin": (14, 14)},            # header, away from the other two
            {"id": "4", "text": "Services", "pin": (20, 1), "master": "d"},   # legend
            {"id": "5", "text": "Actor", "pin": (20, 2), "master": "p"},
            {"id": "6", "text": "N", "pin": (14.2, 5), "master": "d"},
        ]
        g = extract_vsdx(write(shapes, masters=LEGEND_MASTERS), "m")
        self.assertEqual([(n.text, n.phase, n.node_type) for n in g.nodes], [("N", "Services", "service")])
        self.assertEqual(g.meta["n_legend"], 2)


class TestEdgeLabels(unittest.TestCase):
    def test_connector_text_becomes_edge_label(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1)}, {"id": "2", "text": "B", "pin": (5, 1)},
                  {"id": "3", "text": "  waited\u00a0six   weeks ", "connector": True, "begin": (1, 1), "end": (5, 1)},
                  {"id": "4", "connector": True, "begin": (5, 1), "end": (1, 1)}]
        connects = [{"from_sheet": s, "from_cell": c, "to_sheet": t} for s, a, b in (("3", "1", "2"), ("4", "2", "1"))
                    for c, t in (("BeginX", a), ("EndX", b))]
        g = extract_vsdx(write(shapes, connects), "m")
        self.assertEqual(sorted((e.label or "") for e in g.edges), ["", "waited six weeks"])
        self.assertEqual([n.text for n in g.nodes], ["A", "B"])


class TestEmotionTokenNotStored(unittest.TestCase):
    def test_token_before_word_is_not_stored_anywhere(self):
        g = extract_vsdx(write([{"id": "1", "text": "Zoltan's Word: Tired", "pin": (1, 1)},
                                {"id": "2", "text": "Clinic", "pin": (2, 1)}]), "m")
        self.assertEqual(g.emotion_word, "Tired")
        self.assertNotIn("Zoltan", g.to_json())
        self.assertNotIn("zoltan", repr(g).lower())


class TestFixRound1(unittest.TestCase):
    def test_write_vsdx_is_byte_deterministic(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1), "master": "1"}]
        d = Path(tempfile.mkdtemp())
        write_vsdx(d / "a.vsdx", shapes, [], {"1": "Process"})
        time.sleep(1.1)
        write_vsdx(d / "b.vsdx", shapes, [], {"1": "Process"})
        self.assertEqual((d / "a.vsdx").read_bytes(), (d / "b.vsdx").read_bytes())

    def test_phase_header_without_pin_is_not_a_node(self):
        g = extract_vsdx(write([{"id": "1", "text": "Diagnosis", "pin": None},
                                {"id": "2", "text": "A", "pin": (1, 1)}]), "m")
        self.assertEqual([n.text for n in g.nodes], ["A"])
        self.assertEqual(g.meta["unplaced_headers"], 1)

    def test_services_pair_with_unplaced_legend_shape_does_not_crash(self):
        shapes = [
            {"id": "1", "text": "Early Concerns", "pin": (1, 8)},
            {"id": "2", "text": "Diagnosis", "pin": (5, 8)},
            {"id": "3", "text": "Services", "pin": (14, 14)},
            {"id": "4", "text": "Services", "pin": (20, 1)},
            {"id": "5", "text": "Actor", "pin": None},                # legend shape with no position
            {"id": "6", "text": "N", "pin": (14.2, 5)},
        ]
        g = extract_vsdx(write(shapes), "m")
        self.assertEqual([(n.text, n.phase) for n in g.nodes], [("N", "Services")])

    def test_node_red_only_through_character_colour(self):
        g = extract_vsdx(write([{"id": "1", "text": "A", "pin": (1, 1), "char": ["#000000", "#FF0000"]},
                                {"id": "2", "text": "B", "pin": (2, 1), "char": "#000000"},
                                {"id": "3", "text": "C", "pin": (3, 1)}]), "m")
        self.assertEqual({n.text: n.status for n in g.nodes},
                         {"A": "wished", "B": "experienced", "C": "experienced"})

    def test_connector_with_uppercase_red_is_wished(self):
        shapes = [{"id": "1", "text": "A", "pin": (1, 1)}, {"id": "2", "text": "B", "pin": (5, 1)},
                  {"id": "3", "connector": True, "begin": (1, 1), "end": (5, 1), "line": "#FF0000"}]
        connects = [{"from_sheet": "3", "from_cell": "BeginX", "to_sheet": "1"},
                    {"from_sheet": "3", "from_cell": "EndX", "to_sheet": "2"}]
        g = extract_vsdx(write(shapes, connects), "m")
        self.assertEqual([e.status for e in g.edges], ["wished"])

    def test_group_offset_is_applied_to_child_pins_and_connector_ends(self):
        ns = "http://schemas.microsoft.com/office/visio/2012/main"
        def cells(**kw): return "".join(f'<Cell N="{k}" V="{v}"/>' for k, v in kw.items())
        page = (f'<PageContents xmlns="{ns}"><Shapes>'
                f'<Shape ID="1" Type="Group">{cells(PinX=10, PinY=20, Width=4, Height=4, LocPinX=2, LocPinY=2)}'
                f'<Shapes>'
                f'<Shape ID="2">{cells(PinX=1, PinY=1, Width=1, Height=1)}<Text>Inside</Text></Shape>'
                f'<Shape ID="4">{cells(PinX=3, PinY=1, Width=1, Height=1)}<Text>Inside2</Text></Shape>'
                f'<Shape ID="5">{cells(PinX=0, PinY=0, Width=0, Height=0, BeginX=1.5, BeginY=1, EndX=2.5, EndY=1)}</Shape>'
                f'</Shapes></Shape>'
                f'<Shape ID="3">{cells(PinX=5, PinY=5, Width=1, Height=1)}<Text>Outside</Text></Shape>'
                f'<Shape ID="6">{cells(PinX=0, PinY=0, Width=1, Height=1, BeginX=0, BeginY=0, EndX=0, EndY=0)}</Shape>'
                f'</Shapes></PageContents>')
        p = Path(tempfile.mkdtemp()) / "g.vsdx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("visio/pages/page1.xml", page)
        g = extract_vsdx(p, "m")
        pos = {n.text: (n.x, n.y) for n in g.nodes}
        self.assertEqual(pos, {"Inside": (9.0, 19.0), "Inside2": (11.0, 19.0), "Outside": (5.0, 5.0)})
        # the group's inner connector runs between its two children once offset (8.5..9.5, 19) -> (10.5.. , 19)
        self.assertEqual(len(g.edges), 1)
        e = g.edges[0]
        self.assertEqual((g.node(e.source).text, g.node(e.target).text), ("Inside", "Inside2"))


if __name__ == "__main__":
    unittest.main()
