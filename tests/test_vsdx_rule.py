"""T11: opt-in Visio missing rules with inheritance (``maps/vsdx_rule.py``).

The study's maps read with the default rule and must never reach this code. Fixtures are small synthetic ``.vsdx``
files built by ``build_fixtures`` below (``maps/vsdx_writer.write_vsdx`` plus masters, rels and StyleSheets added by
rewriting the zip). They are committed under ``tests/fixtures/maps/vsdx-rule-*.vsdx``; regenerate them with
``SS_WRITE_VSDX_RULE_FIXTURES=1 python3 -m unittest tests.test_vsdx_rule``.
"""
import glob
import os
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from second_scribe.maps import readers, vsdx_rule
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MapRefused, MissingRule, read_map
from second_scribe.maps.vsdx import extract_vsdx
from second_scribe.maps.vsdx_rule import apply_vsdx_rule, read_vsdx_rule, resolve_cell
from second_scribe.maps.vsdx_writer import NS, write_vsdx
from second_scribe.pipeline import KIT_ROOT
from xml.etree import ElementTree as ET

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "maps"
MAP_ID = "fixture-vsdx-rule"
DEMO_MAPS = sorted(glob.glob(str(KIT_ROOT / "demo" / "corpus" / "maps" / "*.vsdx")))
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

RED_LINE = MissingRule("red_line_or_text")                       # == the default: read_map never applies it
RED_LINE_OPT_IN = MissingRule("red_line_or_text", colors=("#ff0000", "#c00000"))
RED_FILL = MissingRule("red_fill")
DASHED = MissingRule("dashed")


# --------------------------------------------------------------------------------------------- fixture helper
def _cell(n, v):
    return f'<Cell N="{n}" V="{v}"/>'


def _char(*colors):
    rows = "".join(f'<Row IX="{i}">{_cell("Color", c)}</Row>' for i, c in enumerate(colors))
    return f'<Section N="Character">{rows}</Section>'


def _inject(page: str, sid, attrs: str = "", inner: str = "") -> str:
    """Add attributes to shape ``sid``'s opening tag and XML right after it."""
    tag = f'<Shape ID="{sid}" NameU="Shape{sid}"'
    i = page.index(tag)
    j = page.index(">", i)
    return page[:i] + tag + attrs + page[i + len(tag):j + 1] + inner + page[j + 1:]


def _master_file(shapes_xml: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><MasterContents xmlns="{NS}"><Shapes>{shapes_xml}</Shapes>' \
           f'</MasterContents>'


def _masters_index(entries: dict) -> str:
    """entries: {master id: (NameU, rel id or None)}"""
    ms = "".join(f'<Master ID="{i}" NameU="{n}">' + (f'<Rel r:id="{r}"/>' if r else "") + "</Master>"
                 for i, (n, r) in entries.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><Masters xmlns="{NS}" xmlns:r="{R_NS}">{ms}</Masters>'


def _rels(targets: dict) -> str:
    rs = "".join(f'<Relationship Id="{r}" Type="http://schemas.microsoft.com/visio/2010/relationships/master" '
                 f'Target="{t}"/>' for r, t in targets.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG_NS}">{rs}</Relationships>'


def _document(styles_xml: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><VisioDocument xmlns="{NS}"><StyleSheets>{styles_xml}' \
           f'</StyleSheets></VisioDocument>'


def _style(sid, name, inner="", **based):
    attrs = "".join(f' {k}="{v}"' for k, v in based.items())
    return f'<StyleSheet ID="{sid}" NameU="{name}"{attrs}>{inner}</StyleSheet>'


def _rezip(src: Path, dst: Path, page_edit, extra: dict) -> None:
    """Rewrite ``src`` into ``dst``: ``page_edit(str) -> str`` on page1.xml, ``extra`` members added or replaced.
    Same byte-deterministic settings as ``write_vsdx``."""
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
    files["visio/pages/page1.xml"] = page_edit(files["visio/pages/page1.xml"])
    files.update(extra)
    with zipfile.ZipFile(dst, "w") as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, files[name])


def _box(sid, text, x, **kw):
    return {"id": sid, "text": text, "pin": (x, 1.0), "w": 1, "h": 0.5, **kw}


def _link(sid, a, b, ax, bx, **kw):
    return {"id": sid, "connector": True, "begin": (ax, 1.0), "end": (bx, 1.0), **kw}


def _glue(cid, a, b):
    return [{"from_sheet": cid, "from_cell": "BeginX", "to_sheet": a},
            {"from_sheet": cid, "from_cell": "EndX", "to_sheet": b}]


def _build_inherit(dst: Path, tmp: Path) -> None:
    shapes = [_box(1, "Box from red master", 1, master="1"),
              _box(2, "Own black line beats red master", 3, master="1", line="#000000"),
              _box(3, "Style based on a red style", 5),
              _box(4, "Text red by master", 7, master="2"),
              _box(5, "Text red by style", 9),
              _box(7, "Plain box", 11),
              _box(8, "Style cycle", 13),
              {"id": 9, "pin": (15, 1.0), "w": 2, "h": 1, "master": "3"},          # group, no text
              _box(10, "Literal red outline", 17, line="#FF0000"),
              _link(11, 1, 7, 1, 11, master="4"),
              _link(12, 3, 4, 5, 7),
              _link(13, 4, 5, 7, 9, line="#ff0000"),
              _link(14, 7, None, 11, 40),
              _link(15, 5, 7, 9, 11, char="#ff0000", text="red label")]       # kit: edges ignore text colour
    connects = _glue(11, 1, 7) + _glue(12, 3, 4) + _glue(13, 4, 5) + _glue(15, 5, 7) + [
        {"from_sheet": 14, "from_cell": "BeginX", "to_sheet": 7}]
    base = tmp / "inherit-base.vsdx"
    write_vsdx(base, shapes, connects, masters={"1": "Box", "2": "Box", "3": "Group", "4": "Link"})

    def page(p):
        p = _inject(p, 3, ' LineStyle="3"')
        p = _inject(p, 5, ' TextStyle="6"')
        p = _inject(p, 7, ' LineStyle="0" FillStyle="0" TextStyle="0"')
        p = _inject(p, 8, ' LineStyle="4"')
        child = ('<Shapes><Shape ID="20" NameU="Shape20" MasterShape="6">' + _cell("PinX", 0.5) + _cell("PinY", 0.5)
                 + _cell("Width", 1) + _cell("Height", 0.5) + "<Text>Group child red by master shape</Text>"
                 "</Shape></Shapes>")
        return _inject(p, 9, "", child)

    extra = {
        "visio/masters/masters.xml": _masters_index({"1": ("Box", "rId1"), "2": ("Box", "rId2"),
                                                     "3": ("Group", "rId3"), "4": ("Link", None)}),
        "visio/masters/_rels/masters.xml.rels": _rels({"rId1": "master1.xml", "rId2": "master7.xml",
                                                       "rId3": "master3.xml"}),
        "visio/masters/master1.xml": _master_file('<Shape ID="5">' + _cell("LineColor", "#FF0000") + "</Shape>"),
        # master 2 lives in master7.xml (only the rels say so); its text is red
        "visio/masters/master7.xml": _master_file('<Shape ID="5">' + _char("#000000", "#ff0000") + "</Shape>"),
        "visio/masters/master3.xml": _master_file(
            '<Shape ID="5"><Shapes><Shape ID="6">' + _cell("LineColor", "#ff0000") + "</Shape></Shapes></Shape>"),
        # master 4 has no Rel: found by the file-name fallback master4.xml
        "visio/masters/master4.xml": _master_file('<Shape ID="5">' + _cell("LineColor", "#ff0000") + "</Shape>"),
        "visio/document.xml": _document(
            _style(0, "No Style", _cell("LineColor", "#000000") + _cell("FillPattern", 1) + _cell("LinePattern", 1)
                   + _char("#000000"))
            + _style(2, "Red line", _cell("LineColor", "#FF0000"), LineStyle=0)
            + _style(3, "Based on red", LineStyle=2)
            + _style(4, "Cycle A", LineStyle=5)
            + _style(5, "Cycle B", LineStyle=4)
            + _style(6, "Red text", _char("#FF0000"), TextStyle=0)),
    }
    _rezip(base, dst, page, extra)


def _build_fill(dst: Path, tmp: Path) -> None:
    shapes = [_box(1, "Red fill, pattern 0", 1, fill="#FF0000"),
              _box(2, "Red fill, pattern 1", 3, fill="#ff0000"),
              _box(3, "Red fill, no pattern anywhere", 5, fill="#ff0000"),
              _box(4, "Red fill, style says no fill", 7, fill="#ff0000"),
              _box(5, "Red fill from master", 9, master="1"),
              _box(6, "Red fill from master, master pattern 0", 11, master="2"),
              _box(7, "Green fill", 13, fill="#00ff00"),
              _box(8, "Red outline only", 15, line="#ff0000"),
              _link(9, 2, 5, 3, 9)]
    base = tmp / "fill-base.vsdx"
    write_vsdx(base, shapes, _glue(9, 2, 5), masters={"1": "Box", "2": "Box"})

    def page(p):
        p = _inject(p, 1, "", _cell("FillPattern", 0))
        p = _inject(p, 2, "", _cell("FillPattern", 1))
        p = _inject(p, 4, ' FillStyle="7"')
        p = _inject(p, 7, "", _cell("FillPattern", 1))
        return p

    extra = {
        "visio/masters/masters.xml": _masters_index({"1": ("Box", None), "2": ("Box", None)}),
        "visio/masters/master1.xml": _master_file(
            '<Shape ID="5">' + _cell("FillForegnd", "#FF0000") + _cell("FillPattern", 1) + "</Shape>"),
        "visio/masters/master2.xml": _master_file(
            '<Shape ID="5">' + _cell("FillForegnd", "#FF0000") + _cell("FillPattern", 0) + "</Shape>"),
        "visio/document.xml": _document(_style(0, "No Style", _cell("FillPattern", 1))
                                        + _style(7, "No fill", _cell("FillPattern", 0), FillStyle=0)),
    }
    _rezip(base, dst, page, extra)


DASH_VALUES = {1: "1", 2: "2", 3: "3", 4: "9", 5: "254", 6: "0", 7: "1.0"}


def _build_dashed(dst: Path, tmp: Path) -> None:
    shapes = [_box(sid, f"Line pattern {v}", 2 * sid) for sid, v in DASH_VALUES.items()]
    shapes += [_box(8, "Dashed by master", 16, master="1"),
               _box(9, "Dashed by style", 18),
               _box(10, "No pattern anywhere", 20),
               _link(11, 1, 2, 2, 4), _link(12, 2, 3, 4, 6)]
    base = tmp / "dashed-base.vsdx"
    write_vsdx(base, shapes, _glue(11, 1, 2) + _glue(12, 2, 3), masters={"1": "Box"})

    def page(p):
        for sid, v in DASH_VALUES.items():
            p = _inject(p, sid, "", _cell("LinePattern", v))
        p = _inject(p, 9, ' LineStyle="8"')
        p = _inject(p, 12, "", _cell("LinePattern", 2))
        return p

    extra = {
        "visio/masters/masters.xml": _masters_index({"1": ("Box", None)}),
        "visio/masters/master1.xml": _master_file('<Shape ID="5">' + _cell("LinePattern", 2) + "</Shape>"),
        "visio/document.xml": _document(_style(8, "Long dash", _cell("LinePattern", 16))),
    }
    _rezip(base, dst, page, extra)


BUILDERS = {"vsdx-rule-inherit.vsdx": _build_inherit, "vsdx-rule-fill.vsdx": _build_fill,
            "vsdx-rule-dashed.vsdx": _build_dashed}


def build_fixtures(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ss-tests-vsdx-rule-") as t:
        for name, build in BUILDERS.items():
            build(out_dir / name, Path(t))


def setUpModule():
    if os.environ.get("SS_WRITE_VSDX_RULE_FIXTURES") == "1":
        build_fixtures(FIXTURES)


def _fx(name):
    return FIXTURES / f"vsdx-rule-{name}.vsdx"


def _status(g):
    return ({n.node_id: n.status for n in g.nodes}, [(e.source, e.target, e.status) for e in g.edges])


def _wished(g):
    return sorted(n.node_id for n in g.nodes if n.status == "wished")


def _copy_with(src: Path, edits: dict) -> Path:
    """A temp copy of fixture ``src`` with members replaced (name -> str/bytes)."""
    d = Path(tempfile.mkdtemp(prefix="ss-tests-vsdx-rule-"))
    dst = d / src.name
    with zipfile.ZipFile(src) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files.update({k: (v.encode("utf-8") if isinstance(v, str) else v) for k, v in edits.items()})
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(files):
            z.writestr(n, files[n])
    return dst


# --------------------------------------------------------------------------------------------- tests
class TestFixtures(unittest.TestCase):
    def test_committed_fixtures_match_helper(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-vsdx-rule-") as t:
            build_fixtures(Path(t))
            for name in BUILDERS:
                self.assertEqual((Path(t) / name).read_bytes(), (FIXTURES / name).read_bytes(), name)


class TestDispatch(unittest.TestCase):
    def test_registered_for_vsdx(self):
        self.assertIs(READERS[".vsdx"], read_vsdx_rule)

    def test_default_rule_never_calls_apply(self):
        p = _fx("inherit")
        boom = unittest.mock.Mock(side_effect=AssertionError("apply_vsdx_rule called on the default rule"))
        with unittest.mock.patch.object(vsdx_rule, "apply_vsdx_rule", boom):
            for rule in (None, RED_LINE, DEFAULT_RULES[".vsdx"]):
                self.assertEqual(read_map(p, MAP_ID, None, rule).to_json(), extract_vsdx(p, MAP_ID).to_json())
            for demo in DEMO_MAPS:
                read_map(demo, "SYN-X", None, None)
            self.assertEqual(boom.call_count, 0)
            with self.assertRaises(AssertionError):            # the patch is live: an opt-in rule does reach it
                read_map(p, MAP_ID, None, RED_FILL)

    def test_rule_kind_not_for_vsdx_refused(self):
        for kind in ("mermaid_class", "classdef_style", "status_column"):
            with self.assertRaises(MapRefused) as cm:
                read_map(_fx("inherit"), MAP_ID, None, MissingRule(kind))
            self.assertEqual(cm.exception.code, "unsupported_format", kind)

    def test_demo_maps_literal_colours_same_as_kit(self):
        """On the shipped synthetic maps (literal colours, no master files or styles), the opt-in red rule with an
        extra unused colour gives exactly the kit's graph: edge-to-connector matching and statuses agree."""
        self.assertEqual(len(DEMO_MAPS), 12)
        for p in DEMO_MAPS:
            self.assertEqual(read_map(p, "SYN-X", None, MissingRule("red_line_or_text", colors=("#ff0000", "#123456")))
                             .to_json(), extract_vsdx(p, "SYN-X").to_json(), p)


class TestRedLineOrText(unittest.TestCase):
    WANT = ["p1.1", "p1.10", "p1.20", "p1.3", "p1.4", "p1.5"]

    def test_vsdx_rule_reads_master_and_stylesheet(self):
        p = _fx("inherit")
        kit = extract_vsdx(p, MAP_ID)
        self.assertEqual(_wished(kit), ["p1.10"])                 # the kit sees only the literal red outline
        for g in (apply_vsdx_rule(p, kit, RED_LINE), read_map(p, MAP_ID, None, RED_LINE_OPT_IN)):
            self.assertEqual(_wished(g), self.WANT)
            self.assertEqual([(e.source, e.target, e.status) for e in g.edges],
                             [("p1.1", "p1.7", "wished"),          # connector red by master (file-name fallback)
                              ("p1.3", "p1.4", "experienced"),
                              ("p1.4", "p1.5", "wished"),          # literal red connector
                              ("p1.5", "p1.7", "experienced")])    # red label text only
            self.assertEqual(g.unresolved_edges, 1)

    def test_own_cell_beats_master(self):
        g = apply_vsdx_rule(_fx("inherit"), extract_vsdx(_fx("inherit"), MAP_ID), RED_LINE)
        self.assertEqual(g.node("p1.2").status, "experienced")
        self.assertEqual(g.node("p1.7").status, "experienced")
        self.assertEqual(g.node("p1.8").status, "experienced")    # style cycle terminates with no value

    def test_other_colours(self):
        p = _fx("inherit")
        g = apply_vsdx_rule(p, extract_vsdx(p, MAP_ID), MissingRule("red_line_or_text", colors=("#000000",)))
        # own black line (2); black from style 0 (7); master 2's first text row is black (4)
        self.assertEqual(_wished(g), ["p1.2", "p1.4", "p1.7"])

    def test_resolve_cell_order(self):
        with zipfile.ZipFile(_fx("inherit")) as z:
            masters, styles = vsdx_rule.read_masters(z), vsdx_rule.read_styles(z)
            page = ET.fromstring(z.read("visio/pages/page1.xml"))
        shapes = {s.get("ID"): s for s in page.iter("{%s}Shape" % NS)}
        self.assertEqual(resolve_cell(shapes["1"], "LineColor", masters, styles), "#FF0000")       # master
        self.assertEqual(resolve_cell(shapes["2"], "LineColor", masters, styles), "#000000")       # own
        self.assertEqual(resolve_cell(shapes["3"], "LineColor", masters, styles), "#FF0000")       # style BasedOn
        self.assertEqual(resolve_cell(shapes["7"], "LinePattern", masters, styles), "1")           # style 0
        self.assertIsNone(resolve_cell(shapes["8"], "LineColor", masters, styles))                 # cycle
        self.assertIsNone(resolve_cell(shapes["10"], "FillForegnd", masters, styles))


class TestRedFill(unittest.TestCase):
    def test_red_fill_needs_pattern(self):
        p = _fx("fill")
        kit = extract_vsdx(p, MAP_ID)
        self.assertEqual(_wished(kit), ["p1.8"])
        for g in (apply_vsdx_rule(p, kit, RED_FILL), read_map(p, MAP_ID, None, RED_FILL)):
            # 1: pattern 0; 2: pattern 1; 3: no pattern anywhere = Visio's solid default; 4: style pattern 0;
            # 5: master red + solid; 6: master pattern 0; 7: green; 8: red outline is not a red fill
            self.assertEqual(_wished(g), ["p1.2", "p1.3", "p1.5"])
            self.assertEqual([e.status for e in g.edges], ["experienced"])


class TestDashed(unittest.TestCase):
    def test_dashed_line_pattern_values(self):
        p = _fx("dashed")
        kit = extract_vsdx(p, MAP_ID)
        self.assertEqual(_wished(kit), [])
        for g in (apply_vsdx_rule(p, kit, DASHED), read_map(p, MAP_ID, None, DASHED)):
            # 2, 3, 9 dashed; 1, 254, 0 and "1.0" not; master 2 and style 16 dashed; nothing set: not dashed
            self.assertEqual(_wished(g), ["p1.2", "p1.3", "p1.4", "p1.8", "p1.9"])
            self.assertEqual([e.status for e in g.edges], ["experienced", "wished"])


class TestIdsUnchanged(unittest.TestCase):
    def test_ids_unchanged(self):
        for name in ("inherit", "fill", "dashed"):
            p = _fx(name)
            kit = extract_vsdx(p, MAP_ID)
            before = kit.to_json()
            for rule in (RED_LINE, RED_LINE_OPT_IN, RED_FILL, DASHED):
                g = apply_vsdx_rule(p, kit, rule)
                self.assertIsNot(g, kit)
                self.assertEqual(kit.to_json(), before, "input graph must not be mutated")
                for n in g.nodes + g.edges:
                    n.status = "experienced"
                flat = extract_vsdx(p, MAP_ID)
                for n in flat.nodes + flat.edges:
                    n.status = "experienced"
                self.assertEqual(g.to_json(), flat.to_json(), (name, rule))

    def test_mismatched_graph_refused(self):
        p = _fx("inherit")
        g = extract_vsdx(p, MAP_ID)
        g.edges.pop()
        with self.assertRaises(MapRefused) as cm:
            apply_vsdx_rule(p, g, RED_FILL)
        self.assertEqual(cm.exception.code, "unreadable")


class TestSafety(unittest.TestCase):
    DOCTYPE = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]><VisioDocument xmlns="%s"/>' % NS

    def test_doctype_refused(self):
        for member in ("visio/document.xml", "visio/masters/master1.xml"):
            p = _copy_with(_fx("inherit"), {member: self.DOCTYPE})
            with self.assertRaises(MapRefused) as cm:
                read_map(p, MAP_ID, None, RED_FILL)
            self.assertEqual(cm.exception.code, "unreadable")
            self.assertNotIn("<!", cm.exception.detail)          # the detail never quotes the file
            with self.assertRaises(MapRefused):
                apply_vsdx_rule(p, extract_vsdx(_fx("inherit"), MAP_ID), RED_FILL)

    def test_doctype_in_page_refused_before_kit_parses(self):
        page = '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><PageContents xmlns="%s"/>' % NS
        p = _copy_with(_fx("fill"), {"visio/pages/page1.xml": page})
        with unittest.mock.patch.object(vsdx_rule, "extract_vsdx", side_effect=AssertionError("kit parsed it")):
            with self.assertRaises(MapRefused):
                read_map(p, MAP_ID, None, DASHED)

    def test_utf16_doctype_refused(self):
        p = _copy_with(_fx("fill"), {"visio/document.xml": self.DOCTYPE.replace('"1.0"?', '"1.0" encoding="UTF-16"?')
                                     .encode("utf-16")})
        with self.assertRaises(MapRefused):
            read_map(p, MAP_ID, None, DASHED)

    def test_member_size_capped(self):
        with unittest.mock.patch.object(vsdx_rule, "MAX_MEMBER_BYTES", 200):
            with self.assertRaises(MapRefused) as cm:
                read_map(_fx("inherit"), MAP_ID, None, RED_FILL)
        self.assertEqual(cm.exception.code, "unreadable")

    def test_not_a_zip_refused(self):
        d = Path(tempfile.mkdtemp(prefix="ss-tests-vsdx-rule-"))
        p = d / "broken.vsdx"
        p.write_bytes(b"not a zip")
        with self.assertRaises(MapRefused) as cm:
            read_map(p, MAP_ID, None, RED_FILL)
        self.assertEqual(cm.exception.code, "unreadable")


if __name__ == "__main__":
    unittest.main()
