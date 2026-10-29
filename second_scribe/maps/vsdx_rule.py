"""Opt-in Visio missing rules with inheritance (app T11).

The kit's ``extract_vsdx`` marks a box or arrow "wished" only when the shape's own outline (or, for boxes, its own
text) is literally ``#ff0000``. That stays the study's rule: ``readers.read_map`` sends a ``.vsdx`` with no rule or
the default rule straight to ``extract_vsdx`` and never reaches this module.

When the user picks another rule, ``read_vsdx_rule`` reads the map with ``extract_vsdx`` (same ids, text, phases,
types, edges) and ``apply_vsdx_rule`` recomputes only each node's and edge's ``status``. Each cell is resolved the
way Visio inherits it:

    the shape's own cell
    -> the master shape's cell (``Master`` on the shape or on its group; ``MasterShape`` picks a sub-shape)
    -> the StyleSheet named by the shape's (else the master shape's) ``LineStyle`` / ``FillStyle`` / ``TextStyle``,
       then the StyleSheets that one is based on (its own ``LineStyle``/``FillStyle``/``TextStyle``, or ``BasedOn``)

Rules:
    red_line_or_text  ``LineColor`` in the colours, or (boxes only, as in the kit) any Character-row ``Color``
    red_fill          ``FillForegnd`` in the colours and ``FillPattern`` != 0 (no value anywhere = 1, solid)
    dashed            ``LinePattern`` >= 2 and != 254

Every XML member is read with a size cap and refused if it carries a DOCTYPE or ENTITY declaration; nothing is
extracted to disk. Standard library only.
"""
from __future__ import annotations

import copy
import posixpath
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

from .vsdx import NS, _cells, _in_box, _page_names, _read_glue, _read_shapes, _walk, extract_vsdx
from .vsdx import ExtractConfig, _read_masters

VSDX_RULE_KINDS = ("red_line_or_text", "red_fill", "dashed")
MAX_MEMBER_BYTES = 20 * 1024 * 1024          # one XML part
MAX_TOTAL_BYTES = 200 * 1024 * 1024          # all XML parts together
MAX_MEMBERS = 10000
MAX_STYLE_DEPTH = 64
_DTD = re.compile(rb"<!\s*(?:doctype|entity)")        # as draw.io and xlsx: also "<! ENTITY"
_HEX6 = re.compile(r"^#[0-9a-f]{6}$")
_V = "{%s}" % NS["v"]
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MASTERS = "visio/masters/masters.xml"
_MASTERS_RELS = "visio/masters/_rels/masters.xml.rels"
_DOCUMENT = "visio/document.xml"


def _refuse(detail: str):
    from .readers import MapRefused         # lazy: readers imports this module
    return MapRefused("unreadable", detail)


# --------------------------------------------------------------------------------------------- safe reading
def _read_member(z: zipfile.ZipFile, name: str) -> bytes:
    """One XML member, capped (the header's size is not trusted), refused if it declares a DOCTYPE or ENTITY."""
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise _refuse(f"a part of the Visio file is larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MB")
    with z.open(info) as f:
        data = f.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise _refuse(f"a part of the Visio file is larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MB")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace").encode("utf-8", errors="replace")
    else:
        text = data.replace(b"\x00", b"")
    if _DTD.search(text.lower()):
        raise _refuse("the Visio file contains an XML DOCTYPE or ENTITY declaration")
    return data


def _parse(z: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(_read_member(z, name))
    except ET.ParseError:
        raise _refuse("a part of the Visio file is not well-formed XML") from None


def check_vsdx(path) -> None:
    """Refuse a file that is not a zip, has too many or too large XML parts, or any DOCTYPE/ENTITY in them.
    Run before ``extract_vsdx`` so the kit never parses such a part."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_MEMBERS:
                raise _refuse("the Visio file has too many parts")
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):             # z.read(name) takes the last of two: check them all
                raise _refuse("the Visio file lists the same part twice")
            total = 0
            for info in infos:
                if info.filename.lower().endswith((".xml", ".rels")):
                    total += len(_read_member(z, info.filename))
                    if total > MAX_TOTAL_BYTES:
                        raise _refuse(f"the Visio file's XML is larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError,
            zlib.error):
        raise _refuse("not a readable Visio (.vsdx) file") from None


# --------------------------------------------------------------------------------------------- masters, styles
def read_masters(z: zipfile.ZipFile) -> dict:
    """``{master id: {"top": first top-level master shape | None, "shapes": {shape id: element}}}``.
    The master's part comes from its ``Rel`` in ``masters.xml.rels``; without one, ``masters/master<ID>.xml``."""
    names = set(z.namelist())
    if _MASTERS not in names:
        return {}
    rels = {}
    if _MASTERS_RELS in names:
        for r in _parse(z, _MASTERS_RELS).iter(_PKG + "Relationship"):
            if r.get("Id") and r.get("Target"):
                rels[r.get("Id")] = posixpath.normpath(posixpath.join("visio/masters", r.get("Target")))
    out: dict = {}
    for m in _parse(z, _MASTERS).iter(_V + "Master"):
        mid = m.get("ID")
        if not mid:
            continue
        rel = m.find(_V + "Rel")
        part = rels.get(rel.get(_R + "id")) if rel is not None else None
        if part is None or part not in names:
            part = f"visio/masters/master{mid}.xml"
        if part not in names:
            continue
        root = _parse(z, part)
        top_shapes = root.find(_V + "Shapes")
        top = top_shapes.find(_V + "Shape") if top_shapes is not None else None
        out[mid] = {"top": top, "shapes": {s.get("ID"): s for s in root.iter(_V + "Shape") if s.get("ID")}}
    return out


def read_styles(z: zipfile.ZipFile) -> dict:
    """``{StyleSheet ID: element}`` from ``visio/document.xml``."""
    if _DOCUMENT not in z.namelist():
        return {}
    return {s.get("ID"): s for s in _parse(z, _DOCUMENT).iter(_V + "StyleSheet") if s.get("ID")}


def _master_shape(shape_el: ET.Element, masters: dict, master_id: str | None) -> ET.Element | None:
    mid = shape_el.get("Master") or master_id
    entry = masters.get(mid) if mid else None
    if entry is None:
        return None
    if shape_el.get("MasterShape"):
        return entry["shapes"].get(shape_el.get("MasterShape"))
    return entry["top"] if shape_el.get("Master") else None    # a local shape inside a group instance


def _style_attr(name: str) -> str:
    return "LineStyle" if name.startswith("Line") else "FillStyle" if name.startswith("Fill") else "TextStyle"


def _style_chain(start: str | None, attr: str, styles: dict):
    """The StyleSheets from ``start`` along ``attr`` (then ``BasedOn``), each once, at most MAX_STYLE_DEPTH."""
    seen, sid = set(), start
    while sid is not None and sid not in seen and sid in styles and len(seen) < MAX_STYLE_DEPTH:
        seen.add(sid)
        st = styles[sid]
        yield st
        sid = st.get(attr) or st.get("BasedOn")


def _own(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    v = _cells(el).get(name)
    return v if v not in (None, "") else None


def resolve_cell(shape_el: ET.Element, name: str, masters: dict, styles: dict,
                 master_id: str | None = None) -> str | None:
    """The value of top-level cell ``name`` for this shape: own cell -> master shape cell -> StyleSheet chain.
    ``master_id`` is the master inherited from an enclosing group instance (None at top level)."""
    v = _own(shape_el, name)
    if v is not None:
        return v
    ms = _master_shape(shape_el, masters, master_id)
    v = _own(ms, name)
    if v is not None:
        return v
    attr = _style_attr(name)
    start = shape_el.get(attr) or (ms.get(attr) if ms is not None else None)
    for st in _style_chain(start, attr, styles):
        v = _own(st, name)
        if v is not None:
            return v
    return None


def _char_rows(el: ET.Element | None) -> dict:
    """``{row IX: Color V}`` of this element's own Character section."""
    out = {}
    if el is None:
        return out
    for sec in el.findall("v:Section", NS):
        if sec.get("N") != "Character":
            continue
        for i, row in enumerate(sec.findall("v:Row", NS)):
            ix = row.get("IX") or str(i)
            for c in row.findall("v:Cell", NS):
                if c.get("N") == "Color" and c.get("V"):
                    out[ix] = c.get("V")
    return out


def resolve_char_colors(shape_el: ET.Element, masters: dict, styles: dict,
                        master_id: str | None = None) -> list[str]:
    """Text colours, one per Character row index found on the shape, its master shape or its text styles; each
    row resolved own -> master -> style chain."""
    ms = _master_shape(shape_el, masters, master_id)
    start = shape_el.get("TextStyle") or (ms.get("TextStyle") if ms is not None else None)
    layers = [_char_rows(shape_el), _char_rows(ms)] + [_char_rows(st) for st in _style_chain(start, "TextStyle",
                                                                                             styles)]
    out = []
    for ix in dict.fromkeys(ix for layer in layers for ix in layer):
        out.append(next(layer[ix] for layer in layers if ix in layer))
    return out


# --------------------------------------------------------------------------------------------- rules
def _color(v: str | None) -> str | None:
    return v.strip().lower() if v else None


def _int(v: str | None) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _wished(el: ET.Element, rule, masters: dict, styles: dict, master_id: str | None, is_box: bool) -> bool:
    colors = {c.strip().lower() for c in rule.colors}
    cell = lambda n: resolve_cell(el, n, masters, styles, master_id)    # noqa: E731
    if rule.kind == "red_line_or_text":
        if _color(cell("LineColor")) in colors:
            return True
        return is_box and any(_color(c) in colors for c in resolve_char_colors(el, masters, styles, master_id))
    if rule.kind == "red_fill":
        pattern = _int(cell("FillPattern"))
        return _color(cell("FillForegnd")) in colors and (1 if pattern is None else pattern) != 0
    if rule.kind == "dashed":
        pattern = _int(cell("LinePattern"))
        return pattern is not None and pattern >= 2 and pattern != 254
    raise ValueError(f"rule {rule.kind!r} does not apply to .vsdx")


def _walk_masters(container: ET.Element, master_id: str | None = None):
    """The master id each shape inherits, in ``vsdx._walk``'s order."""
    for el in container.findall("v:Shape", NS):
        mid = el.get("Master") or master_id
        yield mid
        sub = el.find("v:Shapes", NS)
        if sub is not None:
            yield from _walk_masters(sub, mid)


def _page_edges(root: ET.Element, shapes, node_sids: set, tol: float):
    """Connector shape ids in the order ``extract_vsdx`` turned them into edges, with (begin, end) node shape ids,
    plus the count of unresolved connectors. Mirrors ``extract_vsdx``'s glue and geometry steps."""
    glue = _read_glue(root)
    by_id = {s.sid: s for s in shapes}
    node_shapes = [s for s in shapes if s.sid in node_sids and not s.is_connector and s.x is not None
                   and s.y is not None]

    def glued_end(cid, which, hop=0):
        to = glue.get(cid, {}).get(which)
        if to is None:
            return None
        if to in node_sids:
            return to
        other = by_id.get(to)
        if hop == 0 and other is not None and other.is_connector and to != cid:
            first, second = ("begin", "end") if which == "begin" else ("end", "begin")
            return glued_end(to, first, 1) or glued_end(to, second, 1)
        return None

    def geometry_end(pt):
        if pt is None:
            return None
        hit = [n.sid for n in node_shapes if _in_box(pt, n, tol)]
        return hit[0] if len(hit) == 1 else None

    edges, unresolved = [], 0
    for i, c in enumerate(shapes):
        if not c.is_connector:
            continue
        ends = {}
        for which, pt in (("begin", c.begin), ("end", c.end)):
            ends[which] = glued_end(c.sid, which) if which in glue.get(c.sid, {}) else geometry_end(pt)
        if ends["begin"] is None or ends["end"] is None:
            unresolved += 1
        else:
            edges.append((i, ends["begin"], ends["end"]))
    return edges, unresolved


def apply_vsdx_rule(path, g, rule, extract: ExtractConfig | None = None):
    """A copy of ``g`` (read from ``path`` by ``extract_vsdx`` with ``extract``) with every node and edge
    ``status`` recomputed under ``rule``. Ids, text, edges and everything else are unchanged. Raises
    ``MapRefused("unreadable")`` if the file's connectors do not line up with ``g``'s edges."""
    if rule.kind not in VSDX_RULE_KINDS:
        from .readers import MapRefused
        raise MapRefused("unsupported_format", f"the missing rule {rule.kind} does not apply to Visio maps")
    tol = (extract or ExtractConfig()).geometry_tolerance_in
    out = copy.deepcopy(g)
    nodes = {n.node_id: n for n in out.nodes}
    edge_status: list = []
    unresolved = 0
    try:
        with zipfile.ZipFile(path) as z:
            masters, styles = read_masters(z), read_styles(z)
            names = _read_masters(z) if _MASTERS in z.namelist() else {}
            for pi, part in enumerate(_page_names(z), start=1):
                root = _parse(z, part)
                top = root.find("v:Shapes", NS)
                walked = list(_walk(top)) if top is not None else []
                mids = list(_walk_masters(top)) if top is not None else []
                shapes = _read_shapes(root, names)
                prefix = f"p{pi}."
                node_sids = {nid[len(prefix):] for nid in nodes if nid.startswith(prefix)}
                for (el, _ox, _oy), mid, s in zip(walked, mids, shapes):
                    nid = prefix + s.sid
                    if not s.is_connector and nid in nodes:
                        nodes[nid].status = "wished" if _wished(el, rule, masters, styles, mid, True) else \
                            "experienced"
                edges, n_unres = _page_edges(root, shapes, node_sids, tol)
                unresolved += n_unres
                for i, b, e in edges:
                    el, mid = walked[i][0], mids[i]
                    edge_status.append((prefix + b, prefix + e,
                                        "wished" if _wished(el, rule, masters, styles, mid, False) else "experienced"))
    except (zipfile.BadZipFile, KeyError, OSError, EOFError, RuntimeError):
        raise _refuse("not a readable Visio (.vsdx) file") from None
    if [(e.source, e.target) for e in out.edges] != [(b, e) for b, e, _ in edge_status] \
            or unresolved != out.unresolved_edges:
        raise _refuse("the map's connectors could not be matched for this missing rule")
    for edge, (_b, _e, status) in zip(out.edges, edge_status):
        edge.status = status
    return out


def read_vsdx_rule(path, map_id: str, extract: ExtractConfig | None, rule):
    """``READERS[".vsdx"]``: a Visio map under a non-default missing rule (``read_map`` handles the default)."""
    if rule.kind not in VSDX_RULE_KINDS:
        from .readers import MapRefused
        raise MapRefused("unsupported_format", f"the missing rule {rule.kind} does not apply to Visio maps")
    check_vsdx(path)
    try:
        g = extract_vsdx(path, map_id, extract)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, AttributeError, TypeError) as exc:
        from .readers import MapRefused
        if isinstance(exc, MapRefused):          # extract_vsdx's own refusal (a map with no boxes) says it best
            raise
        raise _refuse("not a readable Visio (.vsdx) file") from None
    return apply_vsdx_rule(path, g, rule, extract)


def box_colors(path, extract: ExtractConfig | None = None) -> dict:
    """``{node_id: {"line": set, "text": set, "fill": set}}`` for the boxes ``extract_vsdx`` reads from ``path``:
    each colour resolved as ``_wished`` resolves it (own cell, master shape, style chain), ``#rrggbb`` only, a fill
    only when its pattern is not 0. For the Setup step's "Colours found in your maps"; changes no status."""
    check_vsdx(path)
    try:
        g = extract_vsdx(path, "colors", extract)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, AttributeError, TypeError) as exc:
        from .readers import MapRefused
        if isinstance(exc, MapRefused):
            raise
        raise _refuse("not a readable Visio (.vsdx) file") from None
    wanted = {n.node_id for n in g.nodes}
    out: dict = {}
    hexes = lambda vs: {c for c in (_color(v) for v in vs) if c and _HEX6.match(c)}    # noqa: E731
    try:
        with zipfile.ZipFile(path) as z:
            masters, styles = read_masters(z), read_styles(z)
            names = _read_masters(z) if _MASTERS in z.namelist() else {}
            for pi, part in enumerate(_page_names(z), start=1):
                root = _parse(z, part)
                top = root.find("v:Shapes", NS)
                walked = list(_walk(top)) if top is not None else []
                mids = list(_walk_masters(top)) if top is not None else []
                for (el, _ox, _oy), mid, s in zip(walked, mids, _read_shapes(root, names)):
                    nid = f"p{pi}." + s.sid
                    if s.is_connector or nid not in wanted:
                        continue
                    cell = lambda n: resolve_cell(el, n, masters, styles, mid)    # noqa: E731
                    pattern = _int(cell("FillPattern"))
                    out[nid] = {"line": hexes([cell("LineColor")]),
                                "text": hexes(resolve_char_colors(el, masters, styles, mid)),
                                "fill": hexes([cell("FillForegnd")]) if (1 if pattern is None else pattern) != 0
                                else set()}
    except (zipfile.BadZipFile, KeyError, OSError, EOFError, RuntimeError):
        raise _refuse("not a readable Visio (.vsdx) file") from None
    return out
