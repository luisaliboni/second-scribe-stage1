"""Spreadsheet maps (T10): ``.csv`` and ``.xlsx`` read into a ``MapGraph``, plus the writers of the two templates.

A cell is text and nothing else. The reader never evaluates a formula (an ``=SUM(...)`` label stays the string it
is; an ``.xlsx`` formula cell contributes its cached value as text and is counted in ``meta["n_formula_cells"]``).

CSV (one file per map): header ``kind,id,label,phase,status,missing_kind,from,to``. A ``box`` row uses
``id,label,phase,status,missing_kind``; an ``arrow`` row uses ``from,to,status``.
XLSX: sheet ``boxes`` (``id,label,phase,status,missing_kind``) and sheet ``arrows`` (``from,to,status``, optional).

Safety for files from users (stdlib only, no openpyxl): the zip is opened by member name and never extracted; any
member that declares a ``<!DOCTYPE`` or ``<!ENTITY`` is refused before it is parsed; the member count and the
uncompressed size of every member we read are capped, and the cap is enforced on the bytes actually decompressed,
not on the size the zip header claims. Refusals are ``MapRefused`` whose ``detail`` never quotes the file.

The writers are deterministic: ``write_template_xlsx`` stores its members uncompressed with a fixed timestamp and
fixed header fields, so the committed template can be regenerated and compared byte for byte.
"""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from ..schemas import Edge, MapGraph, Node
from .readers import MapRefused, MissingRule
from .vsdx import NO_BOXES, ExtractConfig

MAX_ZIP_MEMBERS = 100
MAX_MEMBER_BYTES = 20 * 1024 * 1024          # uncompressed, per member we read
MAX_TOTAL_BYTES = 40 * 1024 * 1024           # uncompressed, over all members we read
MAX_ROWS = 100_000
MAX_COLS = 40

CSV_HEADER = ("kind", "id", "label", "phase", "status", "missing_kind", "from", "to")
BOX_COLS = ("id", "label", "phase", "status", "missing_kind")
ARROW_COLS = ("from", "to", "status")
MISSING_KINDS = ("never came", "wished for", "denied")
_STATUS = {"happened": "experienced", "experienced": "experienced", "missing": "wished", "wished": "wished"}

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOCTYPE = re.compile(rb"<!\s*(doctype|entity)", re.IGNORECASE)
_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_FOLD_SPACES = str.maketrans({" ": " ", " ": " ", " ": " ", "﻿": ""})


def _clean(s: str) -> str:
    """Fold exotic spaces, collapse runs, strip. Keeps case."""
    return " ".join(s.translate(_FOLD_SPACES).split())


def _norm_header(s: str) -> str:
    return _clean(s).lower().replace(" ", "_")


# --------------------------------------------------------------------------------------------- shared graph building

def _build_graph(map_id: str, boxes: list[tuple[int, dict]], arrows: list[tuple[int, dict]], extract,
                 meta_extra: dict) -> MapGraph:
    """``boxes``: [(row number, {column: text})]; ``arrows`` likewise. Rows are already non-empty. A table with no
    box row (a header-only CSV, an empty boxes sheet) is refused: never an empty endorsed map."""
    canon = {p.casefold(): p for p in (extract.phase_names if extract is not None else ())}
    g = MapGraph(map_id=map_id, origin="endorsed")
    ids: set[str] = set()
    missing_kinds: dict[str, str] = {}
    for rn, row in boxes:
        nid, label = _clean(row.get("id", "")), _clean(row.get("label", ""))
        if not nid or not label:
            raise MapRefused("unreadable", f"row {rn}: a box needs an id and a label")
        if nid in ids:
            raise MapRefused("unreadable", f"row {rn}: this box id is already used by an earlier row")
        ids.add(nid)
        status = _status(row, rn)
        mk = _clean(row.get("missing_kind", "")).lower()
        if mk and mk not in MISSING_KINDS:
            raise MapRefused("unreadable", f"row {rn}: missing_kind must be never came, wished for or denied")
        if mk:
            missing_kinds[nid] = mk
        phase = _clean(row.get("phase", "")) or None
        if phase is not None:
            phase = canon.get(phase.casefold(), phase)
        g.nodes.append(Node(node_id=nid, text=label, node_type="other", phase=phase, status=status))
    for rn, row in arrows:
        a, b = _clean(row.get("from", "")), _clean(row.get("to", ""))
        if not a or not b:
            raise MapRefused("unreadable", f"row {rn}: an arrow needs from and to")
        status = _status(row, rn)
        if a not in ids or b not in ids:
            g.unresolved_edges += 1
            continue
        g.edges.append(Edge(source=a, target=b, status=status, glued=True))
    if not g.nodes:            # after the row checks, so a bad row is still named by its number
        raise MapRefused("unreadable", NO_BOXES)
    g.meta = {"n_boxes": len(g.nodes), "n_arrows": len(g.edges), "missing_kinds": missing_kinds, **meta_extra}
    return g


def _status(row: dict, rn: int) -> str:
    v = _STATUS.get(_clean(row.get("status", "")).lower())
    if v is None:
        raise MapRefused("unreadable", f"row {rn}: status must be happened or missing")
    return v


def _need_columns(header: list[str], need: tuple[str, ...]) -> None:
    if not all(c in header for c in need):
        raise MapRefused("unreadable", f"the header row must include these columns: {', '.join(need)}")


# --------------------------------------------------------------------------------------------------------------- CSV

def _decode_csv(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    else:
        enc = "utf-8-sig"
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError:
        raise MapRefused("unreadable", "the CSV file is not UTF-8 text") from None
    if "\x00" in text:
        raise MapRefused("unreadable", "the CSV file is not plain text")
    return text


def _read_csv(path: Path, map_id: str, extract) -> MapGraph:
    try:
        raw = path.read_bytes()
    except OSError:
        raise MapRefused("unreadable", "the map file could not be opened") from None
    text = _decode_csv(raw)
    first = text.split("\n", 1)[0]
    delim = max((",", ";", "\t"), key=lambda d: (first.count(d), d == ","))
    boxes: list[tuple[int, dict]] = []
    arrows: list[tuple[int, dict]] = []
    header: list[str] | None = None
    try:
        for rn, rec in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim), start=1):
            if rn > MAX_ROWS:
                raise MapRefused("unreadable", f"the table has more than {MAX_ROWS} rows")
            if header is None:
                header = [_norm_header(c) for c in rec]
                _need_columns(header, ("kind", "id", "label", "status", "from", "to"))
                continue
            if not any(c.strip() for c in rec):
                continue
            row: dict = {}
            for name, cell in zip(header, rec):
                if name and name not in row:
                    row[name] = cell
            kind = _clean(row.get("kind", "")).lower()
            if kind == "box":
                boxes.append((rn, row))
            elif kind == "arrow":
                arrows.append((rn, row))
            else:
                raise MapRefused("unreadable", f"row {rn}: kind must be box or arrow")
    except csv.Error:
        raise MapRefused("unreadable", "the file could not be read as CSV") from None
    if header is None:
        raise MapRefused("unreadable", "the CSV file is empty")
    return _build_graph(map_id, boxes, arrows, extract, {"source_format": "csv"})


# -------------------------------------------------------------------------------------------------------------- XLSX

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _Zip:
    """A zip opened for reading members by name, with the size and member-count caps."""

    def __init__(self, path: Path):
        try:
            self.z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, zlib.error, NotImplementedError, EOFError):
            raise MapRefused("unreadable", "the file is not a valid .xlsx workbook") from None
        infos = self.z.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise MapRefused("unreadable", f"the workbook has more than {MAX_ZIP_MEMBERS} parts")
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise MapRefused("unreadable", "the workbook lists the same part twice")
        self.names = set(names)
        self.sizes = {i.filename: i.file_size for i in infos}
        self.total = 0

    def read(self, name: str) -> bytes:
        if name not in self.names:
            raise MapRefused("unreadable", "the workbook is missing a part it points to")
        if self.sizes[name] > MAX_MEMBER_BYTES:
            raise MapRefused("unreadable", "a part of the workbook is too large")
        try:
            with self.z.open(name) as f:
                data = f.read(MAX_MEMBER_BYTES + 1)          # the cap counts bytes actually decompressed
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError, OSError):
            raise MapRefused("unreadable", "a part of the workbook could not be read") from None
        if len(data) > MAX_MEMBER_BYTES:
            raise MapRefused("unreadable", "a part of the workbook is too large")
        self.total += len(data)
        if self.total > MAX_TOTAL_BYTES:
            raise MapRefused("unreadable", "the workbook is too large when unpacked")
        return data

    def xml(self, name: str) -> ET.Element:
        data = self.read(name)
        if b"\x00" in data or data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            raise MapRefused("unreadable", "a part of the workbook is not UTF-8 XML")
        m = _ENCODING.match(data)
        if m and m.group(1).lower().replace(b"_", b"-") not in (b"utf-8", b"utf8"):
            raise MapRefused("unreadable", "a part of the workbook is not UTF-8 XML")
        if _DOCTYPE.search(data):
            raise MapRefused("unreadable", "a part of the workbook declares a DOCTYPE or ENTITY, which is not allowed")
        try:
            return ET.fromstring(data)
        except ET.ParseError:
            raise MapRefused("unreadable", "a part of the workbook is not well-formed XML") from None


def _rels(zp: _Zip, rels_name: str, base_dir: str) -> dict:
    """{Id: (resolved zip path, Type)} for one .rels part; an absent part gives {}."""
    if rels_name not in zp.names:
        return {}
    out = {}
    for el in zp.xml(rels_name):
        if _local(el.tag) != "Relationship" or el.get("TargetMode") == "External":
            continue
        target = el.get("Target", "")
        full = posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join(base_dir, target))
        if full.startswith("../") or full == "..":
            continue
        out[el.get("Id", "")] = (full, el.get("Type", ""))
    return out


def _si_text(si: ET.Element) -> str:
    """Text of a shared or inline string item: plain ``<t>`` plus rich-text runs ``<r><t>``; phonetic runs
    (``rPh``, furigana) are not part of the cell text."""
    parts = []
    for ch in si:
        tag = _local(ch.tag)
        if tag == "t":
            parts.append(ch.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in ch if _local(t.tag) == "t")
    return "".join(parts)


def _col_index(ref: str) -> int | None:
    m = re.match(r"([A-Za-z]{1,3})\d*$", ref)
    if not m:
        return None
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _sheet_rows(root: ET.Element, shared: list[str], counter: dict) -> list[tuple[int, list[str]]]:
    """[(row number, cells as text)] for a worksheet; cells are never evaluated."""
    rows = []
    sheet_data = next((e for e in root if _local(e.tag) == "sheetData"), None)
    if sheet_data is None:
        return rows
    auto_row = 0
    for row_el in sheet_data:
        if _local(row_el.tag) != "row":
            continue
        auto_row += 1
        try:
            rn = int(row_el.get("r", auto_row))
        except ValueError:
            rn = auto_row
        auto_row = rn
        if len(rows) >= MAX_ROWS:
            raise MapRefused("unreadable", f"the sheet has more than {MAX_ROWS} rows")
        cells: dict[int, str] = {}
        auto_col = -1
        for c in row_el:
            if _local(c.tag) != "c":
                continue
            auto_col += 1
            ci = _col_index(c.get("r", "")) if c.get("r") else None
            ci = auto_col if ci is None else ci
            auto_col = ci
            if ci >= MAX_COLS:
                continue
            t = c.get("t", "n")
            v = next((e for e in c if _local(e.tag) == "v"), None)
            if any(_local(e.tag) == "f" for e in c):
                counter["n_formula_cells"] += 1
            if t == "inlineStr":
                is_el = next((e for e in c if _local(e.tag) == "is"), None)
                text = _si_text(is_el) if is_el is not None else ""
            elif t == "s":
                try:
                    if v is not None and v.text is not None:
                        index = int(v.text)
                        if index < 0:
                            raise IndexError
                        text = shared[index]
                    else:
                        text = ""
                except (ValueError, IndexError):
                    raise MapRefused("unreadable", "a cell points to a shared string that does not exist") from None
            else:
                text = (v.text or "") if v is not None else ""
            if text.strip():
                cells[ci] = text
        if cells:
            width = max(cells) + 1
            rows.append((rn, [cells.get(i, "") for i in range(width)]))
    return rows


def _table(rows: list[tuple[int, list[str]]], need: tuple[str, ...]) -> list[tuple[int, dict]]:
    if not rows:
        raise MapRefused("unreadable", "a sheet is empty")
    header = [_norm_header(c) for c in rows[0][1]]
    _need_columns(header, need)
    out = []
    for rn, cells in rows[1:]:
        row: dict = {}
        for name, cell in zip(header, cells):
            if name and name not in row:
                row[name] = cell
        out.append((rn, row))
    return out


def _read_xlsx(path: Path, map_id: str, extract) -> MapGraph:
    zp = _Zip(path)
    try:
        wb_path = "xl/workbook.xml"
        pkg = _rels(zp, "_rels/.rels", "")
        for full, typ in pkg.values():                       # the package's own pointer to the workbook
            if typ.endswith("/officeDocument"):
                wb_path = full
        wb = zp.xml(wb_path)
        base = posixpath.dirname(wb_path)
        wb_rels = _rels(zp, posixpath.join(base, "_rels", posixpath.basename(wb_path) + ".rels"), base)
        sheets: dict[str, str] = {}
        for el in wb.iter():
            if _local(el.tag) == "sheet":
                rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
                if rid in wb_rels:
                    sheets.setdefault(_clean(el.get("name", "")).lower(), wb_rels[rid][0])
        if "boxes" not in sheets:
            raise MapRefused("unreadable", "the workbook needs a sheet named boxes")
        shared: list[str] = []
        for full, typ in wb_rels.values():
            if typ.endswith("/sharedStrings"):
                shared = [_si_text(si) for si in zp.xml(full) if _local(si.tag) == "si"]
        counter = {"n_formula_cells": 0}
        boxes = _table(_sheet_rows(zp.xml(sheets["boxes"]), shared, counter), ("id", "label", "status"))
        arrows: list[tuple[int, dict]] = []
        if "arrows" in sheets:
            rows = _sheet_rows(zp.xml(sheets["arrows"]), shared, counter)
            if rows:
                arrows = _table(rows, ARROW_COLS)
    finally:
        zp.z.close()
    return _build_graph(map_id, boxes, arrows, extract, {"source_format": "xlsx", **counter})


# ------------------------------------------------------------------------------------------------------ entry point

def read_table(path, map_id: str, extract: ExtractConfig | None, rule: MissingRule) -> MapGraph:
    """Read a ``.csv`` or ``.xlsx`` map. The status column is the missing rule here; ``rule`` is accepted for the
    reader contract and a rule of another kind does not change how a spreadsheet is read."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path, map_id, extract)
    if suffix == ".xlsx":
        return _read_xlsx(path, map_id, extract)
    raise MapRefused("unsupported_format", f"{suffix or 'a file with no extension'} is not a spreadsheet map")


# ------------------------------------------------------------------------------------------------------- templates
# Neutral example rows only: no real data, no ids shaped like participant ids. Every label says it is an example.

_EX_BOXES = (
    ("s1", "EXAMPLE (replace this row): a step the person described", "Phase A", "happened", ""),
    ("s2", "EXAMPLE (replace this row): a step the person wished for", "Phase A", "missing", "wished for"),
    ("s3", "EXAMPLE (replace this row): a later step", "Phase B", "happened", ""),
)
_EX_ARROWS = (("s1", "s3", "happened"), ("s2", "s3", "missing"))
_INSTRUCTIONS = (
    "How to fill in this workbook (one workbook per map)",
    "Sheet boxes: one row per box. id is a short unique code. label is the text on the box. phase is optional.",
    "status is happened (or experienced) for what was drawn normally, and missing (or wished) for what is marked "
    "as missing. missing_kind is optional: never came, wished for or denied.",
    "Sheet arrows: one row per arrow. from and to are box ids. status is happened or missing.",
    "Every row already in the boxes and arrows sheets is an example. Delete or replace all of them.",
    "Cells are read as plain text. Nothing in this workbook is calculated.",
)


def write_template_csv(path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(CSV_HEADER)
        for i, lab, ph, st, mk in _EX_BOXES:
            w.writerow(["box", i, lab, ph, st, mk, "", ""])
        for a, b, st in _EX_ARROWS:
            w.writerow(["arrow", "", "", "", st, "", a, b])


_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _cell_xml(ref: str, text: str, style: int = 0) -> str:
    sp = ' xml:space="preserve"' if text != text.strip() else ""
    s = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{s}><is><t{sp}>{escape(text)}</t></is></c>'


def _col_letter(i: int) -> str:
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]


def _sheet_xml(rows: list[list[str]], widths: list[int]) -> str:
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell_xml(f"{_col_letter(c)}{r}", t, 1 if r == 1 else 0) for c, t in enumerate(row) if t != "")
        body.append(f'<row r="{r}">{cells}</row>')
    return (_XML + f'<worksheet xmlns="{_NS_MAIN}"><sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
            f'</worksheet>')


def _template_parts() -> list[tuple[str, str]]:
    sheets = ("boxes", "arrows", "instructions")
    boxes = [list(BOX_COLS)] + [list(r) for r in _EX_BOXES]
    arrows = [list(ARROW_COLS)] + [list(r) for r in _EX_ARROWS]
    notes = [[t] for t in _INSTRUCTIONS]
    content_types = (_XML + f'<Types xmlns="{_NS_CT}">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                     'spreadsheetml.sheet.main+xml"/>'
                     + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.'
                               f'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1, 2, 3))
                     + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                       'spreadsheetml.styles+xml"/></Types>')
    root_rels = (_XML + f'<Relationships xmlns="{_NS_PKG_REL}"><Relationship Id="rId1" Type="{_NS_REL}/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>')
    workbook = (_XML + f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><bookViews><workbookView/></bookViews><sheets>'
                + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, start=1))
                + '</sheets></workbook>')
    wb_rels = (_XML + f'<Relationships xmlns="{_NS_PKG_REL}">'
               + "".join(f'<Relationship Id="rId{i}" Type="{_NS_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in (1, 2, 3))
               + f'<Relationship Id="rId4" Type="{_NS_REL}/styles" Target="styles.xml"/></Relationships>')
    styles = (_XML + f'<styleSheet xmlns="{_NS_MAIN}">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    return [("[Content_Types].xml", content_types), ("_rels/.rels", root_rels), ("xl/workbook.xml", workbook),
            ("xl/_rels/workbook.xml.rels", wb_rels), ("xl/styles.xml", styles),
            ("xl/worksheets/sheet1.xml", _sheet_xml(boxes, [10, 60, 14, 14, 16])),
            ("xl/worksheets/sheet2.xml", _sheet_xml(arrows, [10, 10, 14])),
            ("xl/worksheets/sheet3.xml", _sheet_xml(notes, [120]))]


def write_template_xlsx(path) -> None:
    """Write the template workbook. Byte-deterministic: members stored (not deflated, so the bytes do not depend on
    the zlib build), fixed timestamp, fixed header fields."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, xml in _template_parts():
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            zi.create_system = 0
            zi.external_attr = 0
            z.writestr(zi, xml.encode("utf-8"))
