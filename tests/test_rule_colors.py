"""Item 28 of the author's app feedback (23 Oct), server side: "Colours found in your maps" for the missing rule.

``readers.box_colors`` reads, with each format's own reader, the outline, text and fill colours of every box; a box
the default rule marks missing has a rule colour in its outline or text, and a box the fill rule marks missing has it
in its fill (the same reading as the rule). ``GET /api/missing-rule/colors`` (``importer.colors_found``) tallies them
over the imported maps of each colour format: colours and counts only, no file name, no box text; it logs and saves
nothing; a map it cannot read is counted, not quoted.
"""
import json
from unittest import mock

from second_scribe.app import importer
from second_scribe.maps import readers
from second_scribe.maps.readers import DEFAULT_RULES, MissingRule
from tests.test_importer import FIXTURES, MERMAID_RED, ImportCase


def wished(path, rule=None):
    return {n.node_id for n in readers.read_map(path, "x", None, rule).nodes if n.status == "wished"}


def having(colors_by_box, places, colors):
    return {nid for nid, p in colors_by_box.items() if any(c in colors for pl in places for c in p[pl])}


class TestBoxColors(ImportCase):
    def test_colours_agree_with_the_rules_reading(self):
        for name in ("journey_plain.drawio", "journey_compressed.drawio", "permit-flow.excalidraw"):
            path = FIXTURES / name
            found = readers.box_colors(path)
            self.assertEqual(set(found), {n.node_id for n in readers.read_map(path, "x").nodes}, name)
            rule = DEFAULT_RULES[readers.map_suffix(path)]
            self.assertEqual(having(found, ("line", "text"), rule.colors), wished(path), name)
            self.assertTrue(wished(path), name)
        for name in ("vsdx-rule-fill.vsdx", "vsdx-rule-inherit.vsdx"):
            path = FIXTURES / name
            found = readers.box_colors(path)
            red = ("#ff0000",)
            self.assertEqual(having(found, ("line", "text"), red),
                             wished(path, MissingRule("red_line_or_text", inherit=True)), name)
            self.assertEqual(having(found, ("fill",), red), wished(path, MissingRule("red_fill")), name)
        self.assertEqual(readers.box_colors(FIXTURES / "vsdx-rule-fill.vsdx")["p1.7"],
                         {"line": set(), "text": set(), "fill": {"#00ff00"}})

    def test_refuses_formats_without_colour_rules_and_broken_files(self):
        with self.assertRaises(readers.MapRefused):
            readers.box_colors(FIXTURES / "permit_flow.mmd")
        bad = self.tmp / "broken.drawio"
        bad.write_text("not a drawing", encoding="utf-8")
        with self.assertRaises(readers.MapRefused):
            readers.box_colors(bad)


class TestColorsFound(ImportCase):
    def setUp(self):
        super().setUp()
        self.context()

    def records(self):
        return self.project.path("logs/app.jsonl").read_text(encoding="utf-8")

    def test_nothing_before_maps(self):
        self.assertEqual(self.ok_get("/api/missing-rule/colors"), {"formats": {}, "max_colors": importer.MAX_COLORS})

    def test_tallies_per_format_and_place(self):
        self.upload("map", "MAP-A1.drawio", (FIXTURES / "journey_plain.drawio").read_bytes())
        self.upload("map", "MAP-A2.drawio", (FIXTURES / "journey_compressed.drawio").read_bytes())
        self.upload("map", "MAP-A3.excalidraw", (FIXTURES / "permit-flow.excalidraw").read_bytes())
        self.upload("map", "MAP-A4.vsdx", (FIXTURES / "vsdx-rule-fill.vsdx").read_bytes())
        self.upload("map", "MAP-A5.mmd", MERMAID_RED)
        before_log, before_data = self.records(), json.dumps(self.project.data, sort_keys=True)
        doc = self.ok_get("/api/missing-rule/colors")
        self.assertEqual(set(doc["formats"]), {".drawio", ".excalidraw", ".vsdx"})      # no Mermaid
        self.assertEqual(doc["formats"][".drawio"], {
            "maps": 2, "boxes": 10, "unreadable": 0, "more": 0,
            "colors": [{"color": "#ff0000", "boxes": 4, "line": 2, "text": 2, "fill": 0}]})
        self.assertEqual(doc["formats"][".excalidraw"], {
            "maps": 1, "boxes": 3, "unreadable": 0, "more": 0,
            "colors": [{"color": "#1e1e1e", "boxes": 3, "line": 2, "text": 3, "fill": 0},
                       {"color": "#e03131", "boxes": 1, "line": 1, "text": 0, "fill": 0}]})
        self.assertEqual(doc["formats"][".vsdx"]["colors"],
                         [{"color": "#ff0000", "boxes": 4, "line": 1, "text": 0, "fill": 3},
                          {"color": "#00ff00", "boxes": 1, "line": 0, "text": 0, "fill": 1}])
        self.assertEqual(self.records(), before_log)                                # nothing logged
        self.assertEqual(json.dumps(self.project.data, sort_keys=True), before_data)  # nothing saved

    def test_reply_names_no_file_and_quotes_no_box(self):
        self.upload("map", "Owner Maria.drawio", (FIXTURES / "journey_plain.drawio").read_bytes())
        self.ok_get("/api/missing-rule/colors")
        reply = self.replies[-1]
        for word in ("Maria", "Owner", "Planning", "Draft the plan", "ID-01"):
            self.assertNotIn(word, reply)

    def test_unreadable_map_counted_never_quoted(self):
        self.upload("map", "MAP-A1.drawio", (FIXTURES / "journey_plain.drawio").read_bytes())
        self.upload("map", "MAP-A2.drawio", (FIXTURES / "journey_plain.drawio").read_bytes())
        self.project.path("inputs/maps/MAP-A2.drawio").write_text("<secret words", encoding="utf-8")
        doc = self.ok_get("/api/missing-rule/colors")
        f = doc["formats"][".drawio"]
        self.assertEqual((f["maps"], f["unreadable"], f["boxes"]), (2, 1, 5))
        self.assertNotIn("secret", self.replies[-1])

    def test_keeps_the_most_used_and_counts_the_rest(self):
        self.upload("map", "MAP-A3.excalidraw", (FIXTURES / "permit-flow.excalidraw").read_bytes())
        with mock.patch.object(importer, "MAX_FOUND", 1):
            f = self.ok_get("/api/missing-rule/colors")["formats"][".excalidraw"]
        self.assertEqual(([c["color"] for c in f["colors"]], f["more"]), (["#1e1e1e"], 1))

    def test_needs_the_session(self):
        status, _, _ = self.raw("GET", "/api/missing-rule/colors")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    import unittest
    unittest.main()
