"""draw.io (``.drawio``) map reader (T7).

Reads ``<mxfile>`` (one or more ``<diagram>``) or a bare ``<mxGraphModel>``. A diagram holds either a child
``<mxGraphModel>`` or a text blob (URL-quoted XML, raw deflate, base64). Vertices become nodes, edges become edges,
swimlane/container vertices with children become phases. Standard library only; a DOCTYPE or ENTITY declaration
is refused before parsing, and the decompressed size is capped.

Refusals are ``MapRefused`` and never quote the file.
"""
from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

from ..schemas import Edge, MapGraph, Node

if TYPE_CHECKING:       # readers.py imports this module to register it, so it is imported lazily here
    from .readers import MapRefused, MissingRule

MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024        # same cap as the map file itself
_UNDECODABLE = "draw.io diagram could not be decoded"
_DTD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_LIGHT_DARK = re.compile(r"^light-dark\(\s*([^,()]+?)\s*,[^()]*\)$")
_BLOCK_TAGS = {"br", "div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"}
_RULES_FOR_DRAWIO = ("red_line_or_text", "red_fill", "dashed")


def _refuse(detail: str = _UNDECODABLE) -> "MapRefused":
    from .readers import MapRefused
    return MapRefused("unreadable", detail)


# --- text, XML, compressed blobs ------------------------------------------------------------------------------

def _decode_text(raw: bytes) -> str:
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16")
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse("the draw.io file is not text") from None


def _parse(text: str, inner: bool = False) -> ET.Element:
    """Parse XML text after refusing any DOCTYPE/ENTITY declaration (no external entities, no entity expansion)."""
    if _DTD.search(text):
        raise _refuse("draw.io files with a DOCTYPE or ENTITY declaration are refused")
    try:
        return ET.fromstring(_XML_DECL.sub("", text, count=1))
    except (ET.ParseError, ValueError):
        raise _refuse(_UNDECODABLE if inner else "the draw.io file is not well-formed XML") from None


def _inflate(blob: str) -> str:
    """The XML text inside a compressed ``<diagram>`` blob, or a refusal."""
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", blob), validate=True)
        d = zlib.decompressobj(-15)
        out = d.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
    except (binascii.Error, ValueError, zlib.error):
        raise _refuse() from None
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise _refuse("a draw.io diagram is too large once decompressed")
    try:
        return urllib.parse.unquote(out.decode("utf-8"))
    except UnicodeDecodeError:
        raise _refuse() from None


def _models(root: ET.Element) -> list[ET.Element]:
    """The ``mxGraphModel`` elements of the file, one per page."""
    if root.tag == "mxGraphModel":
        return [root]
    if root.tag != "mxfile":
        raise _refuse()
    models: list[ET.Element] = []
    for d in root.findall("diagram"):
        child = d.find("mxGraphModel")
        if child is not None:
            models.append(child)
            continue
        blob = (d.text or "").strip()
        if not blob:
            continue
        inner = _parse(_inflate(blob), inner=True)
        if inner.tag != "mxGraphModel":
            raise _refuse()
        models.append(inner)
    if not models:
        raise _refuse()
    return models


# --- labels, styles, colours ----------------------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        self.parts.append(data)


def _label(value: str | None) -> str:
    """A cell label with HTML stripped, entities unescaped, whitespace (NBSP too) collapsed."""
    if not value:
        return ""
    p = _Text()
    p.feed(value)
    p.close()
    return " ".join("".join(p.parts).split())


def _style(s: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in (s or "").split(";"):
        tok = tok.strip()
        if not tok:
            continue
        k, eq, val = tok.partition("=")
        out[k.strip()] = val.strip() if eq else ""
    return out


def _color(v: str | None) -> str | None:
    """``#rrggbb`` lower case; ``light-dark(a, b)`` means ``a``; ``#rgb`` is expanded."""
    if v is None:
        return None
    v = v.strip().lower()
    m = _LIGHT_DARK.match(v)
    if m:
        v = m.group(1).strip()
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        v = "#" + "".join(ch * 2 for ch in v[1:])
    return v


def _matcher(rule: "MissingRule"):
    if rule.kind not in _RULES_FOR_DRAWIO:
        from .readers import MapRefused
        raise MapRefused("unsupported_format", "this missing rule does not apply to draw.io maps")
    colors = {_color(c) for c in rule.colors}
    if rule.kind == "red_line_or_text":
        return lambda st: _color(st.get("strokeColor")) in colors or _color(st.get("fontColor")) in colors
    if rule.kind == "red_fill":
        return lambda st: _color(st.get("fillColor")) in colors
    return lambda st: st.get("dashed") == "1"


# --- cells ------------------------------------------------------------------------------------------------------

def _num(v: str | None) -> float:
    try:
        return float(v) if v is not None else 0.0
    except ValueError:
        return 0.0


def _cells(model: ET.Element) -> list[dict]:
    root = model.find("root")
    out: list[dict] = []
    for el in (list(root) if root is not None else []):
        if el.tag == "mxCell":
            cell, value = el, el.get("value")
        elif el.tag in ("object", "UserObject"):
            cell, value = el.find("mxCell"), el.get("label", el.get("value"))
            if cell is None:
                continue
        else:
            continue
        cid = el.get("id")
        if cid is None:
            continue
        g = cell.find("mxGeometry")
        out.append({"id": cid, "text": _label(value), "style": _style(cell.get("style")),
                    "vertex": cell.get("vertex") == "1", "edge": cell.get("edge") == "1",
                    "parent": cell.get("parent"), "source": cell.get("source"), "target": cell.get("target"),
                    "geo": None if g is None else (_num(g.get("x")), _num(g.get("y")))})
    return out


def _is_container(style: dict[str, str]) -> bool:
    return "swimlane" in style or style.get("shape") == "swimlane" or style.get("container") == "1"


def read_drawio(path, map_id: str, extract, rule: "MissingRule") -> MapGraph:
    """Read a draw.io file into a ``MapGraph`` (origin "endorsed"). ``extract`` is unused (vsdx phase config)."""
    wished = _matcher(rule)
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise _refuse("the map file could not be opened") from None
    models = _models(_parse(_decode_text(raw)))

    g = MapGraph(map_id=map_id, origin="endorsed")
    n_connectors = n_phases = 0
    for page, model in enumerate(models, start=1):
        cells = _cells(model)
        by_id = {c["id"]: c for c in cells}
        kids: dict[str, list[dict]] = {}
        for c in cells:
            kids.setdefault(c["parent"], []).append(c)

        def is_edge_label(c) -> bool:
            p = by_id.get(c["parent"])
            return c["vertex"] and "edgeLabel" in c["style"] and p is not None and p["edge"]

        containers = {c["id"] for c in cells if c["vertex"] and _is_container(c["style"]) and not is_edge_label(c)
                      and any(k["vertex"] and not is_edge_label(k) for k in kids.get(c["id"], []))}
        n_phases += len(containers)

        def ancestors(c):
            seen, cur = {c["id"]}, by_id.get(c["parent"])
            while cur is not None and cur["id"] not in seen:
                yield cur
                seen.add(cur["id"])
                cur = by_id.get(cur["parent"])

        def position(c):
            if c["geo"] is None:
                return None, None
            x, y = c["geo"]
            for a in ancestors(c):
                if a["vertex"] and a["geo"] is not None:
                    x, y = x + a["geo"][0], y + a["geo"][1]
            return x, y

        node_ids: dict[str, str] = {}
        for c in cells:
            if not c["vertex"] or c["id"] in containers or is_edge_label(c) or not c["text"]:
                continue
            phase = next((by_id[a["id"]]["text"] for a in ancestors(c) if a["id"] in containers
                          and by_id[a["id"]]["text"]), None)
            x, y = position(c)
            nid = f"d{page}.{c['id']}"
            node_ids[c["id"]] = nid
            g.nodes.append(Node(node_id=nid, text=c["text"], phase=phase, x=x, y=y,
                                status="wished" if wished(c["style"]) else "experienced"))

        for c in cells:
            if not c["edge"]:
                continue
            n_connectors += 1
            s, t = node_ids.get(c["source"] or ""), node_ids.get(c["target"] or "")
            if s is None or t is None:
                g.unresolved_edges += 1
                continue
            label = c["text"] or next((k["text"] for k in kids.get(c["id"], []) if is_edge_label(k) and k["text"]),
                                      "")
            g.edges.append(Edge(source=s, target=t, glued=True, label=label or None,
                                status="wished" if wished(c["style"]) else "experienced"))

    n_missing = sum(1 for n in g.nodes if n.status == "wished") + sum(1 for e in g.edges if e.status == "wished")
    g.meta = {"n_pages": len(models), "n_shapes": len(g.nodes), "n_connectors": n_connectors,
              "n_missing": n_missing, "n_phases": n_phases}
    return g



def box_colors(path) -> dict:
    """``{node_id: {"line": set, "text": set, "fill": set}}`` for the boxes ``read_drawio`` reads from ``path``
    (``strokeColor``, ``fontColor``, ``fillColor``; ``#rrggbb`` only, as ``_matcher`` compares them). For the Setup
    step's "Colours found in your maps"."""
    from .readers import MissingRule
    wanted = {n.node_id for n in read_drawio(path, "colors", None, MissingRule("dashed")).nodes}
    models = _models(_parse(_decode_text(Path(path).read_bytes())))
    hexes = lambda v: {c} if (c := _color(v)) and re.fullmatch(r"#[0-9a-f]{6}", c) else set()    # noqa: E731
    out: dict = {}
    for page, model in enumerate(models, start=1):
        for c in _cells(model):
            nid = f"d{page}.{c['id']}"
            if nid in wanted and nid not in out:
                st = c["style"]
                out[nid] = {"line": hexes(st.get("strokeColor")), "text": hexes(st.get("fontColor")),
                            "fill": hexes(st.get("fillColor"))}
    return out
