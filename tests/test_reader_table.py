"""T10: spreadsheet maps (``maps/table.py``): .csv and .xlsx readers, the two templates, and the file-safety limits."""
import re
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from second_scribe.maps import readers, table
from second_scribe.maps.readers import DEFAULT_RULES, READERS, MapRefused, MissingRule, read_map
from second_scribe.maps.table import read_table, write_template_csv, write_template_xlsx
from second_scribe.maps.vsdx import ExtractConfig

TEMPLATES = Path(__file__).resolve().parent.parent / "second_scribe" / "app" / "static" / "templates"
RULE = DEFAULT_RULES[".csv"]
HEADER = "kind,id,label,phase,status,missing_kind,from,to\n"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _tmp():
    d = tempfile.TemporaryDirectory()
    return d, Path(d.name)


def _csv(text, name="m.csv", raw=None):
    d, p = _tmp()
    f = p / name
    f.write_bytes(raw if raw is not None else text.encode("utf-8"))
    return d, f


def _xlsx_parts(boxes, arrows=None, shared=None, extra=None, doctype=""):
    """Build the members of a small workbook. Rows are lists of cell XML strings (use the c()/s()/i() helpers)."""
    def sheet(rows):
        body = "".join(f'<row r="{n}">{"".join(r)}</row>' for n, r in enumerate(rows, start=1))
        return f'<?xml version="1.0"?>{doctype}<worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>'
    names = [("boxes", boxes)] + ([("arrows", arrows)] if arrows is not None else [])
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>' + "".join(
            f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(names, start=1))
            + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(names) + 1))
            + (f'<Relationship Id="rId9" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/>' if shared else "")
            + "</Relationships>",
    }
    for i, (_, rows) in enumerate(names, start=1):
        parts[f"xl/worksheets/sheet{i}.xml"] = sheet(rows)
    if shared:
        parts["xl/sharedStrings.xml"] = f'<sst xmlns="{MAIN}">' + "".join(f"<si>{x}</si>" for x in shared) + "</sst>"
    parts.update(extra or {})
    return parts


def _write_zip(parts, compress=zipfile.ZIP_DEFLATED):
    d, p = _tmp()
    f = p / "m.xlsx"
    with zipfile.ZipFile(f, "w", compress) as z:
        for k, v in parts.items():
            z.writestr(k, v)
    return d, f


def i(ref, text):
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def sidx(ref, n):
    return f'<c r="{ref}" t="s"><v>{n}</v></c>'


BOX_HEAD = [i("A1", "id"), i("B1", "label"), i("C1", "phase"), i("D1", "status"), i("E1", "missing_kind")]
ARROW_HEAD = [i("A1", "from"), i("B1", "to"), i("C1", "status")]


class TestCsv(unittest.TestCase):
    def test_csv_boxes_and_arrows(self):
        d, f = _csv(HEADER + "box,a,Apply,Plan,happened,,,\nbox,b,Wait,Plan,missing,never came,,\n"
                             "arrow,,,,happened,,a,b\n")
        with d:
            g = read_table(f, "M1", None, RULE)
        self.assertEqual((g.map_id, g.origin), ("M1", "endorsed"))
        self.assertEqual([(n.node_id, n.text, n.phase, n.status) for n in g.nodes],
                         [("a", "Apply", "Plan", "experienced"), ("b", "Wait", "Plan", "wished")])
        self.assertEqual([(e.source, e.target, e.status) for e in g.edges], [("a", "b", "experienced")])
        self.assertEqual(g.unresolved_edges, 0)

    def test_registered_for_both_suffixes_and_read_map_dispatches(self):
        self.assertIn(".csv", READERS)
        self.assertIn(".xlsx", READERS)
        d, f = _csv(HEADER + "box,a,Apply,,happened,,,\n")
        with d:
            self.assertEqual(read_map(f, "X").nodes[0].text, "Apply")

    def test_status_synonyms(self):
        rows = "".join(f"box,b{n},L{n},,{s},,,\n" for n, s in enumerate(
            ["happened", "Experienced", " HAPPENED ", "missing", "Wished", "MISSING"]))
        d, f = _csv(HEADER + rows)
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual([n.status for n in g.nodes], ["experienced"] * 3 + ["wished"] * 3)

    def test_bad_status_names_row_and_does_not_quote_it(self):
        d, f = _csv(HEADER + "box,a,Apply,,happened,,,\nbox,b,Wait,,maybe-SECRETWORD,,,\n")
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertIn("row 3", cm.exception.detail)
        self.assertIn("status must be happened or missing", cm.exception.detail)
        self.assertNotIn("SECRETWORD", str(cm.exception))

    def test_blank_status_is_refused_not_defaulted(self):
        d, f = _csv(HEADER + "box,a,Apply,,,,,\n")
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertIn("row 2", cm.exception.detail)

    def test_missing_kind_kept_in_meta(self):
        d, f = _csv(HEADER + "box,a,A,,missing,Never Came,,\nbox,b,B,,missing,wished for,,\n"
                             "box,c,C,,missing,denied,,\nbox,e,E,,happened,,,\n")
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual(g.meta["missing_kinds"], {"a": "never came", "b": "wished for", "c": "denied"})

    def test_bad_missing_kind_refused(self):
        d, f = _csv(HEADER + "box,a,A,,missing,whatever-PRIVATE,,\n")
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertIn("row 2", cm.exception.detail)
        self.assertNotIn("PRIVATE", str(cm.exception))

    def test_unknown_arrow_end_unresolved(self):
        d, f = _csv(HEADER + "box,a,A,,happened,,,\narrow,,,,happened,,a,zz\narrow,,,,happened,,qq,a\n"
                             "arrow,,,,missing,,a,a\n")
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual(g.unresolved_edges, 2)
        self.assertEqual([(e.source, e.target, e.status) for e in g.edges], [("a", "a", "wished")])

    def test_arrow_missing_status_is_wished(self):
        d, f = _csv(HEADER + "box,a,A,,happened,,,\nbox,b,B,,happened,,,\narrow,,,,missing,,a,b\n")
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual(g.edges[0].status, "wished")

    def test_utf8_bom_and_utf8_plain_and_nonascii(self):
        text = HEADER + "box,a,Café “quoted” step,,happened,,,\n"
        for raw in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"), text.encode("utf-16")):
            d, f = _csv(None, raw=raw)
            with d:
                g = read_table(f, "M", None, RULE)
            self.assertEqual(g.nodes[0].text, "Café “quoted” step")

    def test_not_utf8_refused(self):
        d, f = _csv(None, raw=(HEADER + "box,a,Caf\xe9,,happened,,,\n").encode("latin-1"))
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertIn("UTF-8", cm.exception.detail)

    def test_semicolon_and_crlf_and_quoted_newline(self):
        d, f = _csv("kind;id;label;phase;status;missing_kind;from;to\r\nbox;a;\"Two\nlines, here\";;happened;;;\r\n")
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual(g.nodes[0].text, "Two lines, here")

    def test_header_must_have_required_columns(self):
        d, f = _csv("kind,id,name,status\nbox,a,Apply,happened\n")
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertIn("header", cm.exception.detail)

    def test_header_case_and_spaces_tolerated(self):
        d, f = _csv("Kind, ID ,Label,Phase,Status,Missing Kind,From,To\nbox,a,Apply,,happened,,,\n")
        with d:
            self.assertEqual(len(read_table(f, "M", None, RULE).nodes), 1)

    def test_bad_kind_duplicate_id_empty_label_refused(self):
        for body, row in (("thing,a,A,,happened,,,\n", "row 2"),
                          ("box,a,A,,happened,,,\nbox,a,B,,happened,,,\n", "row 3"),
                          ("box,a,,,happened,,,\n", "row 2"),
                          ("arrow,,,,happened,,a,\n", "row 2")):
            d, f = _csv(HEADER + body)
            with d, self.assertRaises(MapRefused) as cm:
                read_table(f, "M", None, RULE)
            self.assertIn(row, cm.exception.detail)

    def test_empty_file_and_blank_lines(self):
        d, f = _csv("")
        with d, self.assertRaises(MapRefused):
            read_table(f, "M", None, RULE)
        d, f = _csv(HEADER + "\n,,,,,,,\nbox,a,A,,happened,,,\n\n")
        with d:
            self.assertEqual(len(read_table(f, "M", None, RULE).nodes), 1)

    def test_phase_canonicalised_by_extract_config(self):
        d, f = _csv(HEADER + "box,a,A,permits,happened,,,\nbox,b,B,Other,happened,,,\n")
        with d:
            g = read_table(f, "M", ExtractConfig(phase_names=("Planning", "Permits")), RULE)
        self.assertEqual([n.phase for n in g.nodes], ["Permits", "Other"])

    def test_formulas_are_text_never_evaluated(self):
        evil = '=HYPERLINK("http://x.invalid","y")'
        d, f = _csv(HEADER + f'box,a,"{evil.replace(chr(34), chr(34) * 2)}",,happened,,,\nbox,b,+1+1,,happened,,,\n'
                             'box,c,@SUM(A1),,happened,,,\n')
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual([n.text for n in g.nodes], [evil, "+1+1", "@SUM(A1)"])

    def test_nul_byte_refused(self):
        d, f = _csv(None, raw=(HEADER + "box,a,A\x00B,,happened,,,\n").encode())
        with d, self.assertRaises(MapRefused):
            read_table(f, "M", None, RULE)


class TestXlsx(unittest.TestCase):
    def test_xlsx_roundtrip_from_template_writer(self):
        d, p = _tmp()
        with d:
            f = p / "t.xlsx"
            write_template_xlsx(f)
            g = read_table(f, "T", None, DEFAULT_RULES[".xlsx"])
        self.assertEqual([(n.node_id, n.status, n.phase) for n in g.nodes],
                         [("s1", "experienced", "Phase A"), ("s2", "wished", "Phase A"), ("s3", "experienced", "Phase B")])
        self.assertEqual([(e.source, e.target, e.status) for e in g.edges],
                         [("s1", "s3", "experienced"), ("s2", "s3", "wished")])
        self.assertEqual(g.meta["missing_kinds"], {"s2": "wished for"})
        self.assertEqual(g.meta["n_formula_cells"], 0)
        self.assertTrue(all(n.text.startswith("EXAMPLE") for n in g.nodes))

    def test_csv_template_roundtrip_matches_xlsx_template(self):
        d, p = _tmp()
        with d:
            write_template_csv(p / "t.csv")
            write_template_xlsx(p / "t.xlsx")
            a = read_table(p / "t.csv", "T", None, RULE)
            b = read_table(p / "t.xlsx", "T", None, RULE)
        self.assertEqual([(n.node_id, n.text, n.phase, n.status) for n in a.nodes],
                         [(n.node_id, n.text, n.phase, n.status) for n in b.nodes])
        self.assertEqual([(e.source, e.target, e.status) for e in a.edges], [(e.source, e.target, e.status) for e in b.edges])

    def test_shared_and_inline_strings_and_rich_text(self):
        shared = ["<t>id</t>", "<r><t>Ap</t></r><r><t>ply</t></r>", "<t>x</t><rPh sb=\"0\" eb=\"1\"><t>FURIGANA</t></rPh>",
                  "<t>status</t>", "<t>label</t>", "<t>happened</t>", "<t>missing</t>", "<t>from</t>", "<t>to</t>"]
        boxes = [[sidx("A1", 0), sidx("B1", 4), sidx("D1", 3)],
                 [i("A2", "a"), sidx("B2", 1), sidx("D2", 5)],
                 [i("A3", "b"), sidx("B3", 2), sidx("D3", 6)]]
        arrows = [[sidx("A1", 7), sidx("B1", 8), sidx("C1", 3)], [i("A2", "a"), i("B2", "b"), i("C2", "happened")]]
        d, f = _write_zip(_xlsx_parts(boxes, arrows, shared))
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual([(n.node_id, n.text, n.status) for n in g.nodes],
                         [("a", "Apply", "experienced"), ("b", "x", "wished")])
        self.assertEqual(len(g.edges), 1)

    def test_numeric_cells_read_as_text_and_sparse_columns(self):
        boxes = [BOX_HEAD,
                 [i("A2", "1"), i("B2", "One"), i("D2", "happened")],
                 ['<c r="A3"><v>2</v></c>', '<c r="B3" t="inlineStr"><is><t>Two</t></is></c>', '<c r="D3" t="inlineStr"><is><t>missing</t></is></c>']]
        arrows = [ARROW_HEAD, ['<c r="A2"><v>1</v></c>', '<c r="B2"><v>2</v></c>', i("C2", "happened")]]
        d, f = _write_zip(_xlsx_parts(boxes, arrows))
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual([n.node_id for n in g.nodes], ["1", "2"])
        self.assertEqual(len(g.edges), 1)
        self.assertEqual(g.unresolved_edges, 0)

    def test_formula_cell_is_never_evaluated_and_counted(self):
        boxes = [BOX_HEAD, [i("A2", "a"), '<c r="B2" t="str"><f>1+1</f><v>CACHED</v></c>', i("D2", "happened")]]
        d, f = _write_zip(_xlsx_parts(boxes))
        with d:
            g = read_table(f, "M", None, RULE)
        self.assertEqual(g.nodes[0].text, "CACHED")
        self.assertEqual(g.meta["n_formula_cells"], 1)

    def test_formula_looking_text_stays_text(self):
        boxes = [BOX_HEAD, [i("A2", "a"), i("B2", "=1+1"), i("D2", "happened")]]
        d, f = _write_zip(_xlsx_parts(boxes))
        with d:
            self.assertEqual(read_table(f, "M", None, RULE).nodes[0].text, "=1+1")

    def test_arrows_sheet_optional_boxes_sheet_required(self):
        d, f = _write_zip(_xlsx_parts([BOX_HEAD, [i("A2", "a"), i("B2", "A"), i("D2", "happened")]]))
        with d:
            self.assertEqual(read_table(f, "M", None, RULE).edges, [])
        parts = _xlsx_parts([BOX_HEAD], None)
        parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace('name="boxes"', 'name="other"')
        d, f = _write_zip(parts)
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertIn("boxes", cm.exception.detail)

    def test_xlsx_bad_status_names_row(self):
        boxes = [BOX_HEAD, [i("A2", "a"), i("B2", "A"), i("D2", "happened")], [i("A3", "b"), i("B3", "B"), i("D3", "no-PRIVATE")]]
        d, f = _write_zip(_xlsx_parts(boxes))
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertIn("row 3", cm.exception.detail)
        self.assertNotIn("PRIVATE", str(cm.exception))

    def test_not_a_zip_refused(self):
        d, f = _csv(None, name="m.xlsx", raw=b"this is not a zip file")
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertEqual(cm.exception.code, "unreadable")


class TestXlsxSafety(unittest.TestCase):
    def _refused(self, parts, **kw):
        d, f = _write_zip(parts, **kw)
        with d, self.assertRaises(MapRefused) as cm:
            read_table(f, "M", None, RULE)
        self.assertEqual(cm.exception.code, "unreadable")
        return cm.exception

    def test_doctype_refused_in_sheet(self):
        boxes = [BOX_HEAD]
        e = self._refused(_xlsx_parts(boxes, doctype='<!DOCTYPE x [<!ENTITY a "AAAA">]>'))
        self.assertIn("DOCTYPE", e.detail)

    def test_entity_and_lowercase_doctype_refused_in_every_part(self):
        for member in ("xl/workbook.xml", "xl/sharedStrings.xml", "xl/_rels/workbook.xml.rels", "_rels/.rels"):
            parts = _xlsx_parts([BOX_HEAD], None, shared=["<t>id</t>"])
            body = parts[member]
            parts[member] = '<?xml version="1.0"?><!doctype r [<!entity e "x">]>' + body
            self._refused(parts)

    def test_billion_laughs_never_expanded(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
                f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&b;</t></is></c></row>'
                "</sheetData></worksheet>")
        parts = _xlsx_parts([BOX_HEAD])
        parts["xl/worksheets/sheet1.xml"] = bomb
        self._refused(parts)

    def test_utf16_part_refused(self):
        parts = _xlsx_parts([BOX_HEAD])
        parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].encode("utf-16")
        self._refused(parts)

    def test_non_utf8_encoding_declaration_refused(self):
        parts = _xlsx_parts([BOX_HEAD])
        parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
            '<?xml version="1.0"?>', '<?xml version="1.0" encoding="ISO-8859-1"?>')
        self._refused(parts)

    def test_too_many_members_refused(self):
        parts = _xlsx_parts([BOX_HEAD])
        parts.update({f"junk/{n}.txt": "x" for n in range(table.MAX_ZIP_MEMBERS + 1)})
        e = self._refused(parts)
        self.assertIn("parts", e.detail)

    def test_zip_bomb_member_refused_by_declared_and_by_actual_size(self):
        small = unittest.mock.patch.object(table, "MAX_MEMBER_BYTES", 50_000)
        parts = _xlsx_parts([BOX_HEAD])
        pad = " " * 200_000
        parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace("<sheetData>", pad + "<sheetData>")
        with small:
            e = self._refused(parts)
        self.assertIn("too large", e.detail)

    def test_actual_bytes_are_capped_even_if_the_header_claims_less(self):
        parts = _xlsx_parts([BOX_HEAD])
        name = "xl/worksheets/sheet1.xml"
        parts[name] = parts[name].replace("<sheetData>", " " * 200_000 + "<sheetData>")
        d, f = _write_zip(parts)
        with d, unittest.mock.patch.object(table, "MAX_MEMBER_BYTES", 50_000):
            zp = table._Zip(f)
            zp.sizes[name] = 100                              # a lying header
            with self.assertRaises(MapRefused) as cm:
                zp.read(name)
            self.assertIn("too large", cm.exception.detail)
            zp.z.close()

    def test_declared_size_over_the_cap_is_refused_before_reading(self):
        d, f = _write_zip(_xlsx_parts([BOX_HEAD]))
        with d:
            zp = table._Zip(f)
            zp.sizes["xl/workbook.xml"] = table.MAX_MEMBER_BYTES + 1
            with unittest.mock.patch.object(zp.z, "open", side_effect=AssertionError("read despite the cap")), \
                    self.assertRaises(MapRefused):
                zp.read("xl/workbook.xml")
            zp.z.close()

    def test_total_unpacked_cap(self):
        with unittest.mock.patch.object(table, "MAX_TOTAL_BYTES", 300):
            self._refused(_xlsx_parts([BOX_HEAD]))

    def test_duplicate_zip_member_names_refused(self):
        d, p = _tmp()
        with d:
            f = p / "m.xlsx"
            parts = _xlsx_parts([BOX_HEAD])
            with zipfile.ZipFile(f, "w") as z:
                for k, v in parts.items():
                    z.writestr(k, v)
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    z.writestr("xl/workbook.xml", parts["xl/workbook.xml"])
            with self.assertRaises(MapRefused):
                read_table(f, "M", None, RULE)

    def test_relationship_escaping_the_package_is_not_followed(self):
        parts = _xlsx_parts([BOX_HEAD])
        parts["xl/_rels/workbook.xml.rels"] = parts["xl/_rels/workbook.xml.rels"].replace(
            "worksheets/sheet1.xml", "../../etc/passwd")
        self._refused(parts)

    def test_dangling_shared_string_refused(self):
        boxes = [[sidx("A1", 5)]]
        e = self._refused(_xlsx_parts(boxes, shared=["<t>id</t>"]))
        self.assertIn("shared string", e.detail)

    def test_malformed_xml_refused(self):
        parts = _xlsx_parts([BOX_HEAD])
        parts["xl/worksheets/sheet1.xml"] = "<worksheet><sheetData>"
        self._refused(parts)

    def test_read_map_applies_the_size_limit_before_the_reader(self):
        d, f = _csv(HEADER + "box,a,A,,happened,,,\n")
        with d, unittest.mock.patch.object(readers, "MAX_MAP_BYTES", 10), \
                unittest.mock.patch.dict(readers.READERS, {".csv": unittest.mock.Mock()}) as m:
            with self.assertRaises(MapRefused):
                read_map(f, "M")
            m[".csv"].assert_not_called()


class TestTemplates(unittest.TestCase):
    def test_committed_templates_match_writer(self):
        d, p = _tmp()
        with d:
            write_template_csv(p / "t.csv")
            write_template_xlsx(p / "t.xlsx")
            self.assertEqual((p / "t.csv").read_bytes(), (TEMPLATES / "map-template.csv").read_bytes())
            self.assertEqual((p / "t.xlsx").read_bytes(), (TEMPLATES / "map-template.xlsx").read_bytes())

    def test_xlsx_writer_is_deterministic_across_calls_and_time(self):
        d, p = _tmp()
        with d:
            write_template_xlsx(p / "a.xlsx")
            with unittest.mock.patch("time.time", return_value=1.9e9), unittest.mock.patch("time.localtime", side_effect=AssertionError):
                write_template_xlsx(p / "b.xlsx")
            self.assertEqual((p / "a.xlsx").read_bytes(), (p / "b.xlsx").read_bytes())

    def test_xlsx_template_has_the_required_parts_and_is_well_formed(self):
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(TEMPLATES / "map-template.xlsx") as z:
            names = z.namelist()
            self.assertEqual(names[0], "[Content_Types].xml")
            for need in ("_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                         "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
                self.assertIn(need, names)
            self.assertIsNone(z.testzip())
            for n in names:
                ET.fromstring(z.read(n))                      # every part is well-formed XML
                self.assertEqual({zi.date_time for zi in z.infolist()}, {(2020, 1, 1, 0, 0, 0)})
            ct = z.read("[Content_Types].xml").decode()
            for n in names:
                if n.startswith("xl/") and n.endswith(".xml") and "_rels" not in n:
                    self.assertIn("/" + n, ct)                # every part has an Override

    def test_templates_have_no_formulas(self):
        with zipfile.ZipFile(TEMPLATES / "map-template.xlsx") as z:
            for n in z.namelist():
                data = z.read(n).decode()
                self.assertNotRegex(data, r"<f[ >/]", n)
                for t in re.findall(r"<t[^>]*>([^<]*)</t>", data):
                    self.assertNotIn(t[:1], ("=", "+", "-", "@"), (n, t))
        for line in (TEMPLATES / "map-template.csv").read_text().splitlines():
            for cell in line.split(","):
                self.assertNotIn(cell[:1], ("=", "+", "@"), line)

    def test_templates_are_neutral_examples(self):
        csv_text = (TEMPLATES / "map-template.csv").read_text()
        with zipfile.ZipFile(TEMPLATES / "map-template.xlsx") as z:
            blob = " ".join(t for n in z.namelist() for t in re.findall(r"<t[^>]*>([^<]*)</t>", z.read(n).decode()))
        self.assertIn("EXAMPLE", blob)
        for text in (csv_text, blob):
            self.assertNotRegex(text, r"\bP\d{1,3}\b")
            self.assertNotRegex(text, r"\bPC?\d{2}\b")
            self.assertNotRegex(text, r"\b(?:I|p)\d\b")
            self.assertNotIn("http", text)
        for line in csv_text.splitlines()[1:]:
            fields = line.split(",")
            if fields[0] == "box":
                self.assertTrue(fields[2].startswith("EXAMPLE"), line)

    def test_csv_template_header_is_the_documented_one(self):
        self.assertEqual((TEMPLATES / "map-template.csv").read_text().splitlines()[0], HEADER.strip())


if __name__ == "__main__":
    unittest.main()
