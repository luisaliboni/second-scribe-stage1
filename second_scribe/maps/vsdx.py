"""Extract an interviewer-drawn, participant-endorsed journey map from a Visio .vsdx file.

A .vsdx is a zip of XML. We read only what the extraction rules need: shape text, positions,
line/fill/text colours, master (stencil) names, and the Connect rows that glue connectors to shapes.
Everything is standard library.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from statistics import median
from xml.etree import ElementTree as ET

from second_scribe.schemas import Edge, MapGraph, Node

NS = {"v": "http://schemas.microsoft.com/office/visio/2012/main"}
PHASE_NAMES = ("Early Concerns", "Diagnosis", "Services", "School")   # configurable
LEGEND_TEXTS = ("Services", "Information & Source", "Actor", "Actor + Information Source",
                "Scenario")                                            # exact legend labels, configurable
LEGEND_TYPE_LABELS = {"Services": "service", "Information & Source": "information_source",
                      "Actor": "actor", "Actor + Information Source": "actor_information_source",
                      "Scenario": "scenario"}
RED = "#ff0000"
_V = "{%s}" % NS["v"]
_FOLD_SPACES = str.maketrans({" ": " ", " ": " ", " ": " "})
_LONG = 40


@dataclass
class ExtractConfig:
    phase_names: tuple[str, ...] = PHASE_NAMES
    legend_texts: tuple[str, ...] = LEGEND_TEXTS
    legend_prefixes: tuple[str, ...] = ("Anything in red",)
    emotion_prefix: str = "Word:"
    type_rules: dict = field(default_factory=dict)   # {"master:<NameU>"|"fill:<hex>"|"line:<hex>": node_type}
    geometry_tolerance_in: float = 0.15
    legend_type_labels: dict = field(default_factory=lambda: dict(LEGEND_TYPE_LABELS))


@dataclass
class _Shape:
    sid: str
    text: str = ""
    x: float | None = None
    y: float | None = None
    w: float = 0.0
    h: float = 0.0
    line: str | None = None
    fill: str | None = None
    char_color: str | None = None        # "#ff0000" if ANY Character-row Color is red, else the first one
    master: str | None = None
    is_connector: bool = False
    begin: tuple[float, float] | None = None
    end: tuple[float, float] | None = None


def _clean(s: str) -> str:
    """Fold exotic spaces, collapse runs, strip. Keeps case and typos (textnorm.normalize lowercases)."""
    return " ".join(s.translate(_FOLD_SPACES).split())


def _num(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _cells(shape: ET.Element) -> dict[str, str]:
    """This shape's own cells (not those of nested shapes), name -> V."""
    return {c.get("N"): c.get("V") for c in shape.findall("v:Cell", NS) if c.get("N")}


def _char_color(shape: ET.Element) -> str | None:
    colors = [c.get("V") for sec in shape.findall("v:Section", NS) if sec.get("N") == "Character"
              for row in sec.findall("v:Row", NS) for c in row.findall("v:Cell", NS)
              if c.get("N") == "Color" and c.get("V")]
    return next((c for c in colors if _is_red(c)), colors[0] if colors else None)


def _emotion_word(text: str, prefix: str) -> str | None:
    """Emotion label text if this shape is the emotion-word box, else None. The box reads
    "Word: <label>", or "<one token> Word: <label>" (interviewers write a possessive before it).
    Returns "" when the box is there but empty."""
    m = re.match(r"(?:\S+\s+)?" + re.escape(prefix), text)
    return text[m.end():].strip() if m else None


def _is_red(color: str | None) -> bool:
    return bool(color) and color.strip().lower() == RED


def _read_masters(z: zipfile.ZipFile) -> dict[str, str]:
    if "visio/masters/masters.xml" not in z.namelist():
        return {}
    root = ET.fromstring(z.read("visio/masters/masters.xml"))
    return {m.get("ID"): (m.get("NameU") or m.get("Name") or "") for m in root.iter(_V + "Master") if m.get("ID")}


def _page_names(z: zipfile.ZipFile) -> list[str]:
    pages = [n for n in z.namelist() if re.fullmatch(r"visio/pages/page\d+\.xml", n)]
    return sorted(pages, key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))


def _walk(container: ET.Element, ox: float = 0.0, oy: float = 0.0):
    """Yield (shape element, x offset, y offset) in document order. A group's children are
    positioned in the group's own coordinates; the offset puts them on the page."""
    for el in container.findall("v:Shape", NS):
        yield el, ox, oy
        sub = el.find("v:Shapes", NS)
        if sub is not None:
            c = _cells(el)
            px, py = _num(c.get("PinX")), _num(c.get("PinY"))
            lx = _num(c.get("LocPinX")) if _num(c.get("LocPinX")) is not None else (_num(c.get("Width")) or 0.0) / 2
            ly = _num(c.get("LocPinY")) if _num(c.get("LocPinY")) is not None else (_num(c.get("Height")) or 0.0) / 2
            yield from _walk(sub, ox + (px - lx if px is not None else 0.0), oy + (py - ly if py is not None else 0.0))


def _read_shapes(root: ET.Element, masters: dict[str, str]) -> list[_Shape]:
    out = []
    top = root.find("v:Shapes", NS)
    for el, ox, oy in (_walk(top) if top is not None else ()):
        c = _cells(el)
        master = masters.get(el.get("Master")) if el.get("Master") else None
        bx, by, ex, ey = (_num(c.get(k)) for k in ("BeginX", "BeginY", "EndX", "EndY"))
        px, py = _num(c.get("PinX")), _num(c.get("PinY"))
        has_ends = "BeginX" in c and "EndX" in c
        by_master = master is not None and ("connector" in master.lower() or "line" in master.lower())
        t = el.find("v:Text", NS)
        out.append(_Shape(
            sid=el.get("ID") or "", text=_clean("".join(t.itertext())) if t is not None else "",
            x=px + ox if px is not None else None, y=py + oy if py is not None else None,
            w=_num(c.get("Width")) or 0.0, h=_num(c.get("Height")) or 0.0,
            line=c.get("LineColor"), fill=c.get("FillForegnd"), char_color=_char_color(el), master=master,
            is_connector=has_ends or by_master,
            begin=(bx + ox, by + oy) if bx is not None and by is not None else None,
            end=(ex + ox, ey + oy) if ex is not None and ey is not None else None))
    return out


def _read_glue(root: ET.Element) -> dict[str, dict[str, str]]:
    """connector id -> {"begin": sheet id, "end": sheet id}"""
    glue: dict[str, dict[str, str]] = {}
    for c in root.iter(_V + "Connect"):
        cell = c.get("FromCell") or ""
        which = "begin" if cell.startswith("Begin") else "end" if cell.startswith("End") else None
        if which and c.get("FromSheet") and c.get("ToSheet"):
            glue.setdefault(c.get("FromSheet"), {})[which] = c.get("ToSheet")
    return glue


def _lane_orientation(headers: list[tuple[str, float, float]]) -> str | None:
    if not headers:
        return None
    xs, ys = [h[1] for h in headers], [h[2] for h in headers]
    return "vertical" if (max(xs) - min(xs)) > (max(ys) - min(ys)) else "horizontal"


def _classify(shapes: list[_Shape], cfg: ExtractConfig):
    """Split text-bearing non-connector shapes into legend / emotion / headers / nodes / unplaced headers."""
    canon = {p.casefold(): p for p in cfg.phase_names}
    legend, emotion, headers, ambiguous, nodes, unplaced = [], [], [], [], [], []
    for s in shapes:
        if s.is_connector or not s.text:
            continue
        is_legend = s.text in cfg.legend_texts or any(s.text.startswith(p) for p in cfg.legend_prefixes)
        is_phase = s.text.casefold() in canon
        if is_legend and is_phase and s.x is not None and s.y is not None:
            ambiguous.append(s)
        elif is_legend:
            legend.append(s)
        elif _emotion_word(s.text, cfg.emotion_prefix) is not None:
            emotion.append(s)
        elif is_phase and s.x is not None and s.y is not None:
            headers.append(s)
        elif is_phase:                      # a phase label with no position cannot define a lane
            unplaced.append(s)
        else:
            nodes.append(s)
    # A label that is both legend text and a phase name ("Services") can appear twice: once in the
    # legend, once as a phase header. The legend copy is the one sitting with the other legend labels.
    base = [(canon[s.text.casefold()], s.x, s.y) for s in headers]
    for text in dict.fromkeys(s.text for s in ambiguous):
        group = [s for s in ambiguous if s.text == text]
        keep = group[0]
        if len(group) > 1:
            placed = [s for s in legend if s.x is not None and s.y is not None]
            if placed:
                cx = sum(s.x for s in placed) / len(placed)
                cy = sum(s.y for s in placed) / len(placed)
                keep = min(group, key=lambda s: (s.x - cx) ** 2 + (s.y - cy) ** 2)
            elif len(base) >= 2:
                orient = _lane_orientation(base)
                cross = median([h[2] if orient == "vertical" else h[1] for h in base])
                keep = max(group, key=lambda s: abs((s.y if orient == "vertical" else s.x) - cross))
        legend.append(keep)
        headers.extend(s for s in group if s is not keep)
    return legend, emotion, headers, nodes, unplaced


def _sig(s: _Shape) -> str:
    """Stencil + colour signature used to match a shape to its map's legend. A red outline marks a
    wished item, not a type, so it is ignored here."""
    line = s.line.lower() if s.line and not _is_red(s.line) else None
    return f"sig:{s.master or '-'}|{s.fill.lower() if s.fill else '-'}|{line or '-'}"


def type_rules_from_legend(shapes, labels: dict | None = None) -> tuple[dict, int]:
    """Node-type rules read from one map's own legend: (rules, n_ambiguous). `shapes` are the legend
    shapes; each whose text is a legend type label maps its master + fill/line signature to that
    type. A signature shared by two different types is ambiguous: it is left out and counted."""
    labels = LEGEND_TYPE_LABELS if labels is None else labels
    seen: dict[str, set] = {}
    for s in shapes:
        if s.text in labels:
            seen.setdefault(_sig(s), set()).add(labels[s.text])
    rules = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}
    return rules, sum(1 for v in seen.values() if len(v) > 1)


def _node_type(s: _Shape, legend_rules: dict, rules: dict) -> str:
    if _sig(s) in legend_rules:
        return legend_rules[_sig(s)]
    keys = []
    if s.master:
        keys.append(f"master:{s.master}")
    if s.fill:
        keys.append(f"fill:{s.fill.lower()}")
    if s.line:
        keys.append(f"line:{s.line.lower()}")
    norm = {(k.lower() if k.split(":", 1)[0] in ("fill", "line") else k): v for k, v in rules.items()}
    for k in keys:
        if k in norm:
            return norm[k]
    return "other"


def _in_box(pt, s: _Shape, tol: float) -> bool:
    return (abs(pt[0] - s.x) <= s.w / 2 + tol) and (abs(pt[1] - s.y) <= s.h / 2 + tol)


NO_BOXES = "the map has no boxes"   # every reader's refusal of a map with zero boxes


def extract_vsdx(path, map_id: str, cfg: ExtractConfig | None = None) -> MapGraph:
    cfg = cfg or ExtractConfig()
    canon = {p.casefold(): p for p in cfg.phase_names}
    g = MapGraph(map_id=map_id, origin="endorsed")
    counts = {"n_shapes": 0, "n_connectors": 0, "n_glued_edges": 0, "n_geometry_edges": 0, "n_legend": 0,
              "n_phase_headers": 0, "unplaced_headers": 0, "ambiguous_type_signatures": 0, "legend_types_found": 0}
    orientations: list[str] = []
    with zipfile.ZipFile(path) as z:
        masters = _read_masters(z)
        pages = _page_names(z)
        for pi, name in enumerate(pages, start=1):
            root = ET.fromstring(z.read(name))
            shapes = _read_shapes(root, masters)
            glue = _read_glue(root)
            by_id = {s.sid: s for s in shapes}
            legend, emotion, headers, nodes, unplaced = _classify(shapes, cfg)
            counts["n_shapes"] += len(shapes)
            counts["n_legend"] += len(legend)
            counts["n_phase_headers"] += len(headers)
            counts["unplaced_headers"] += len(unplaced)
            if emotion and g.emotion_word is None:
                g.emotion_word = _emotion_word(emotion[0].text, cfg.emotion_prefix) or None

            legend_rules, n_ambiguous = type_rules_from_legend(legend, cfg.legend_type_labels)
            counts["ambiguous_type_signatures"] += n_ambiguous
            counts["legend_types_found"] = max(counts["legend_types_found"], len(set(legend_rules.values())))

            hdr = [(canon[s.text.casefold()], s.x, s.y) for s in headers]
            if len(hdr) < 2:      # one header says nothing about lanes
                hdr = []
            orient = _lane_orientation(hdr)
            if orient:
                orientations.append(orient)

            def phase_of(s: _Shape) -> str | None:
                if not hdr or s.x is None or s.y is None:
                    return None
                axis = 1 if orient == "vertical" else 2
                val = s.x if orient == "vertical" else s.y
                return min(hdr, key=lambda h: abs(h[axis] - val))[0]

            node_ids: dict[str, str] = {}
            for s in nodes:
                nid = f"p{pi}.{s.sid}"
                node_ids[s.sid] = nid
                wished = _is_red(s.line) or _is_red(s.char_color)
                g.nodes.append(Node(node_id=nid, text=s.text, node_type=_node_type(s, legend_rules, cfg.type_rules),
                                    phase=phase_of(s), status="wished" if wished else "experienced",
                                    x=s.x, y=s.y))
            node_shapes = [s for s in nodes if s.x is not None and s.y is not None]

            def glued_end(cid: str, which: str, hop: int = 0) -> str | None:
                """Node shape id this connector end is glued to, following one connector hop."""
                to = glue.get(cid, {}).get(which)
                if to is None:
                    return None
                if to in node_ids:
                    return to
                other = by_id.get(to)
                if hop == 0 and other is not None and other.is_connector and to != cid:
                    # a branch leaving a line starts where the line started;
                    # a line merging into a line ends where that line ends
                    first, second = ("begin", "end") if which == "begin" else ("end", "begin")
                    return glued_end(to, first, 1) or glued_end(to, second, 1)
                return None

            def geometry_end(pt) -> str | None:
                if pt is None:
                    return None
                hit = [n.sid for n in node_shapes if _in_box(pt, n, cfg.geometry_tolerance_in)]
                return hit[0] if len(hit) == 1 else None

            for c in (s for s in shapes if s.is_connector):
                counts["n_connectors"] += 1
                ends, via_glue = {}, True
                for which, pt in (("begin", c.begin), ("end", c.end)):
                    if which in glue.get(c.sid, {}):
                        ends[which] = glued_end(c.sid, which)
                    else:
                        ends[which] = geometry_end(pt)
                        via_glue = False
                if ends["begin"] is None or ends["end"] is None:
                    g.unresolved_edges += 1
                    continue
                g.edges.append(Edge(source=node_ids[ends["begin"]], target=node_ids[ends["end"]],
                                    status="wished" if _is_red(c.line) else "experienced", glued=via_glue,
                                    label=c.text or None))
                counts["n_glued_edges" if via_glue else "n_geometry_edges"] += 1
    g.meta = {**counts, "phase_orientation": orientations[0] if orientations else None}
    if not g.nodes:             # no page, or pages with no box: never an empty endorsed map (A2)
        from .readers import MapRefused
        raise MapRefused("unreadable", NO_BOXES)
    return g


def _short(v: str | None) -> str:
    if v is None or v == "":
        return "-"
    return "<long>" if len(v) > _LONG else v


def signature_counts(path) -> dict[str, int]:
    """Counts of "master|fill|line" signatures over text-bearing shapes: template information only.
    No shape text is ever included; a value longer than 40 chars is replaced by "<long>"."""
    out: dict[str, int] = {}
    with zipfile.ZipFile(path) as z:
        masters = _read_masters(z)
        for name in _page_names(z):
            for s in _read_shapes(ET.fromstring(z.read(name)), masters):
                if s.text:
                    sig = f"{_short(s.master)}|{_short(s.fill.lower() if s.fill else None)}|" \
                          f"{_short(s.line.lower() if s.line else None)}"
                    out[sig] = out.get(sig, 0) + 1
    return out
