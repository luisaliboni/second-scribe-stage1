"""Excalidraw map reader (T8): ``.excalidraw`` (JSON) -> ``MapGraph``, called as ``read_excalidraw(path, map_id, extract, rule)``.

What becomes what:
  * ``rectangle`` / ``diamond`` / ``ellipse`` -> a node (``node_id`` = the element id). Its text is the text element(s)
    whose ``containerId`` is the shape. A shape with no text is not a node (the VSDX reader does the same), and an arrow
    that ends on one is unresolved. ``meta["n_empty_shapes"]`` counts them.
  * ``arrow`` (and a ``line`` with both bindings) -> an edge from ``startBinding.elementId`` to ``endBinding.elementId``.
    Only ``elementId`` is read, so the old (``focus``/``gap``) and new (``fixedPoint``/``mode``) forms both work. An end
    that is unbound, or bound to anything but a drawn node, makes the arrow unresolved (``unresolved_edges``). Binding is
    a dictionary lookup per end: no geometry, linear in the number of elements.
  * Text bound to an arrow is the edge ``label``.
  * ``frame`` / ``magicframe`` -> a phase: a node whose ``frameId`` is a frame gets ``phase`` = the frame's ``name``.
  * Text bound to nothing (and not an arrow end) is counted in ``meta["n_free_text"]`` and not drawn.
  * Deleted elements (``isDeleted``) are ignored everywhere.

The file is parsed with stdlib ``json`` only, strictly: duplicate keys, NaN/Infinity (also ``1e999``), nesting deeper than
``MAX_DEPTH`` and more than ``MAX_ELEMENTS`` elements are refused. A refusal is ``MapRefused`` whose ``detail`` is a fixed
phrase and never quotes the file. The readers module is imported inside the refusal helper, not at the top, so this
module can be imported first or second without a cycle with the one-line registration at the bottom of ``readers.py``.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ..schemas import Edge, MapGraph, Node
from .vsdx import ExtractConfig, _clean

MAX_ELEMENTS = 20_000
MAX_DEPTH = 32
SHAPE_TYPES = ("rectangle", "diamond", "ellipse")
FRAME_TYPES = ("frame", "magicframe")
RULE_KINDS = ("red_line_or_text", "red_fill", "dashed")
DASHED_STYLES = ("dashed", "dotted")


def _refuse(code: str, detail: str):
    from .readers import MapRefused
    return MapRefused(code, detail)


def _unreadable(detail: str = "not an Excalidraw file"):
    return _refuse("unreadable", detail)


def _no_constant(_name):
    raise ValueError("non-finite number")


def _finite_float(s):
    v = float(s)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


def _no_duplicate_keys(pairs):
    d = dict(pairs)
    if len(d) != len(pairs):
        raise ValueError("duplicate key")
    return d


_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"', re.S)
_BRACKET = re.compile(r"[\[\]{}]")


def _nested_too_deep(text: str) -> bool:
    """True when the JSON text nests deeper than ``MAX_DEPTH``. Checked on the text, before parsing, so a file of
    brackets cannot make the parser build millions of nested containers (Python 3.14's parser has no depth limit)."""
    depth = 0
    for m in _BRACKET.finditer(_STRING.sub('""', text)):
        if m.group() in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                return True
        else:
            depth -= 1
    return False


def _load(path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise _unreadable("the map file could not be opened") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _unreadable("the file is not UTF-8 text") from None
    if _nested_too_deep(text):
        raise _unreadable("the file is nested too deeply")
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant,
                         parse_float=_finite_float)
    except (ValueError, RecursionError, MemoryError):      # JSONDecodeError is a ValueError
        raise _unreadable("the file is not valid JSON (or has duplicate keys, NaN or infinite numbers)") from None
    if not isinstance(doc, dict) or doc.get("type") != "excalidraw":
        raise _unreadable("the file is not an Excalidraw drawing")
    elements = doc.get("elements")
    if not isinstance(elements, list):
        raise _unreadable("the Excalidraw file has no list of elements")
    if len(elements) > MAX_ELEMENTS:
        raise _unreadable(f"the drawing has more than {MAX_ELEMENTS} elements")
    seen = set()
    for e in elements:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"] \
                or not isinstance(e.get("type"), str):
            raise _unreadable("an element has no id or type")
        if e["id"] in seen:
            raise _unreadable("two elements share one id")
        seen.add(e["id"])
    return doc


_HEX = re.compile(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})")


def _rgb(c) -> str | None:
    """``#rrggbb`` lower case, or None for anything else (names, ``transparent``, a fully transparent ``#rrggbb00``)."""
    if not isinstance(c, str):
        return None
    m = _HEX.fullmatch(c.strip().lower())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    elif len(h) == 8:
        if h[6:] == "00":
            return None
        h = h[:6]
    return "#" + h


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text_of(e: dict) -> str:
    v = e.get("originalText")
    if not isinstance(v, str):
        v = e.get("text")
    return v if isinstance(v, str) else ""


def _bound_id(binding) -> str | None:
    if isinstance(binding, dict) and isinstance(binding.get("elementId"), str):
        return binding["elementId"]
    return None


def read_excalidraw(path, map_id: str, extract: "ExtractConfig | None", rule) -> MapGraph:
    if rule.kind not in RULE_KINDS:
        raise _refuse("unsupported_format", "this missing rule does not apply to an Excalidraw map")
    wanted = {c for c in (_rgb(c) for c in rule.colors) if c}

    def style_matches(e: dict, texts: list) -> bool:
        if rule.kind == "red_line_or_text":
            return _rgb(e.get("strokeColor")) in wanted or any(_rgb(t.get("strokeColor")) in wanted for t in texts)
        if rule.kind == "red_fill":
            return _rgb(e.get("backgroundColor")) in wanted
        return e.get("strokeStyle") in DASHED_STYLES

    live = [e for e in _load(path)["elements"] if not e.get("isDeleted")]
    by_id = {e["id"]: e for e in live}

    frames = {}
    for e in live:
        if e["type"] in FRAME_TYPES:
            name = _clean(e["name"]) if isinstance(e.get("name"), str) else ""
            frames[e["id"]] = name or None

    def is_edge(e: dict) -> bool:
        if e["type"] == "arrow":
            return True
        return e["type"] == "line" and isinstance(e.get("startBinding"), dict) and isinstance(e.get("endBinding"), dict)

    bound_texts: dict[str, list] = {}
    for e in live:
        if e["type"] == "text" and isinstance(e.get("containerId"), str):
            host = by_id.get(e["containerId"])
            if host is not None and (host["type"] in SHAPE_TYPES or host["type"] in ("arrow", "line")):
                bound_texts.setdefault(e["containerId"], []).append(e)

    arrow_ends = set()
    for e in live:
        if is_edge(e):
            arrow_ends.update(i for i in (_bound_id(e.get("startBinding")), _bound_id(e.get("endBinding"))) if i)

    bound_ids = {t["id"] for ts in bound_texts.values() for t in ts}
    n_free = sum(1 for e in live if e["type"] == "text" and e["id"] not in bound_ids and e["id"] not in arrow_ends)

    g = MapGraph(map_id=map_id, origin="endorsed")
    node_ids = set()
    n_shapes = n_empty = n_missing = 0
    for e in live:
        if e["type"] not in SHAPE_TYPES:
            continue
        n_shapes += 1
        texts = bound_texts.get(e["id"], [])
        text = _clean(" ".join(_text_of(t) for t in texts))
        if not text:
            n_empty += 1
            continue
        wished = style_matches(e, texts)
        n_missing += wished
        centre = (e["x"] + e["width"] / 2, e["y"] + e["height"] / 2) \
            if all(_is_num(e.get(k)) for k in ("x", "y", "width", "height")) else (None, None)
        fid = e.get("frameId")
        g.nodes.append(Node(node_id=e["id"], text=text, phase=frames.get(fid) if isinstance(fid, str) else None,
                            status="wished" if wished else "experienced",
                            x=None if centre[0] is None else float(centre[0]),
                            y=None if centre[1] is None else float(centre[1])))
        node_ids.add(e["id"])

    n_connectors = 0
    for e in live:
        if not is_edge(e):
            continue
        n_connectors += 1
        texts = bound_texts.get(e["id"], [])
        src, dst = _bound_id(e.get("startBinding")), _bound_id(e.get("endBinding"))
        if src not in node_ids or dst not in node_ids:
            g.unresolved_edges += 1
            continue
        wished = style_matches(e, texts)
        n_missing += wished
        g.edges.append(Edge(source=src, target=dst, status="wished" if wished else "experienced", glued=True,
                            label=_clean(" ".join(_text_of(t) for t in texts)) or None))
    g.meta = {"n_shapes": n_shapes, "n_connectors": n_connectors, "n_missing": n_missing,
              "n_phases": len(frames), "n_free_text": n_free, "n_empty_shapes": n_empty}
    return g



def box_colors(path) -> dict:
    """``{node_id: {"line": set, "text": set, "fill": set}}`` for the boxes ``read_excalidraw`` reads from ``path``:
    the shape's ``strokeColor`` and ``backgroundColor`` and the ``strokeColor`` of its bound text, as
    ``style_matches`` compares them. For the Setup step's "Colours found in your maps"."""
    from .readers import MissingRule
    wanted = {n.node_id for n in read_excalidraw(path, "colors", None, MissingRule("dashed")).nodes}
    live = [e for e in _load(path)["elements"] if not e.get("isDeleted")]
    texts: dict = {}
    for e in live:
        if e["type"] == "text" and isinstance(e.get("containerId"), str):
            texts.setdefault(e["containerId"], []).append(e)
    one = lambda v: {c} if (c := _rgb(v)) else set()    # noqa: E731
    out: dict = {}
    for e in live:
        if e["id"] in wanted and e["id"] not in out:
            out[e["id"]] = {"line": one(e.get("strokeColor")),
                            "text": {c for t in texts.get(e["id"], []) for c in one(t.get("strokeColor"))},
                            "fill": one(e.get("backgroundColor"))}
    return out
