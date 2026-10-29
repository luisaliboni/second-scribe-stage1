"""Map reader dispatch (K1): one entry point, ``read_map``, for every map format the app accepts.

The study's ``.vsdx`` maps with the default missing rule go straight to ``extract_vsdx`` and nothing else, so they
read exactly as before this module existed. ``.mapgraph.json`` is the kit's own ``MapGraph`` JSON (an endorsed map
already parsed). The other formats register a reader in ``READERS`` (draw.io, Excalidraw, Mermaid, CSV/XLSX, and
the opt-in Visio rules), each called as ``reader(path, map_id, extract, rule)``.

Refusals are ``MapRefused`` (a ``ValueError``) with a ``code`` and a short ``detail`` that never quotes the file.
"""
from __future__ import annotations

import importlib

import json
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict
from xml.etree import ElementTree as ET

from ..schemas import MapGraph
from .vsdx import NO_BOXES, ExtractConfig, extract_vsdx
from .vsdx_rule import check_vsdx, read_vsdx_rule

RULE_KINDS = ("red_line_or_text", "red_fill", "dashed", "mermaid_class", "classdef_style", "status_column")


@dataclass(frozen=True)
class MissingRule:
    """How a map marks a box or arrow as missing ("wished"). Colours are ``#rrggbb``, lower case.

    ``inherit`` (V12, Visio only): also count a colour set by a master shape or a stylesheet. The default rule
    (``inherit`` False) keeps the study's path, ``extract_vsdx``, which reads only a shape's own cells; with
    ``inherit`` True a ``.vsdx`` goes to T11's resolver (``READERS[".vsdx"]``)."""
    kind: str
    colors: tuple[str, ...] = ("#ff0000",)
    class_names: tuple[str, ...] = ("missing",)
    inherit: bool = False

    def __post_init__(self):
        if self.kind not in RULE_KINDS:
            raise ValueError(f"unknown missing rule kind {self.kind!r} (want one of {', '.join(RULE_KINDS)})")
        if not isinstance(self.inherit, bool):
            raise ValueError("a missing rule's inherit flag must be true or false")


DEFAULT_RULES: Dict[str, MissingRule] = {
    ".vsdx": MissingRule("red_line_or_text"),
    ".drawio": MissingRule("red_line_or_text"),
    ".excalidraw": MissingRule("red_line_or_text", colors=("#ff0000", "#e03131")),   # #e03131: Excalidraw's own red
    ".mmd": MissingRule("mermaid_class"),
    ".csv": MissingRule("status_column"),
    ".xlsx": MissingRule("status_column"),
}
MAP_SUFFIXES = (".vsdx", ".drawio", ".excalidraw", ".mmd", ".csv", ".xlsx", ".mapgraph.json")
REFUSED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".tif", ".tiff", ".svg"}
MAX_MAP_BYTES = 50 * 1024 * 1024
_MAPGRAPH = ".mapgraph.json"

Reader = Callable[[Path, str, "ExtractConfig | None", MissingRule], MapGraph]
# Filled by the format modules (Tasks 7-11): {suffix: reader(path, map_id, extract, rule) -> MapGraph}.
READERS: Dict[str, Reader] = {}
READERS[".vsdx"] = read_vsdx_rule      # T11: only a non-default rule reaches it (see read_map)


class MapRefused(ValueError):
    """A map file the app will not read. ``code``: image_or_pdf | unsupported_format | unreadable."""

    CODES = ("image_or_pdf", "unsupported_format", "unreadable")

    def __init__(self, code: str, detail: str):
        if code not in self.CODES:
            raise ValueError(f"unknown MapRefused code {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def map_suffix(path) -> str:
    """Lower-case suffix; ``.mapgraph.json`` counts as one suffix."""
    name = Path(path).name.lower()
    if name.endswith(_MAPGRAPH) and len(name) > len(_MAPGRAPH):
        return _MAPGRAPH
    return Path(name).suffix


def map_stem(path) -> str:
    """The file name without its map suffix: ``SYN-PAIR01.mapgraph.json`` -> ``SYN-PAIR01``;
    ``SYN-PAIR01.vsdx`` -> ``SYN-PAIR01``;
    the participant id a map pairs by."""
    name = Path(path).name
    suffix = map_suffix(path)
    return name[:-len(suffix)] if suffix and name.lower().endswith(suffix) else Path(name).stem


def rule_for(corpus: dict, path) -> MissingRule | None:
    """The corpus's missing rule for this file's format (``corpus["missing_rules"][suffix]``), or None."""
    d = (corpus.get("missing_rules") or {}).get(map_suffix(path))
    if d is None:
        return None
    if not isinstance(d, dict) or "kind" not in d:
        raise ValueError(f"missing rule for {map_suffix(path)} needs a 'kind'")
    kw: dict = {"kind": d["kind"]}
    if d.get("colors") is not None:
        kw["colors"] = tuple(str(c).strip().lower() for c in d["colors"])
    if d.get("class_names") is not None:
        kw["class_names"] = tuple(str(c) for c in d["class_names"])
    if d.get("inherit") is not None:
        kw["inherit"] = d["inherit"]
    return MissingRule(**kw)


def _check_size(path) -> None:
    try:
        size = Path(path).stat().st_size
    except OSError:
        raise MapRefused("unreadable", "the map file could not be opened") from None
    if size > MAX_MAP_BYTES:
        raise MapRefused("unreadable", f"the map file is larger than {MAX_MAP_BYTES // (1024 * 1024)} MB")


def _empty(path) -> bool:
    try:
        return Path(path).stat().st_size == 0
    except OSError:
        return False


def _read_mapgraph(path, map_id: str) -> MapGraph:
    _check_size(path)
    try:
        g = MapGraph.from_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError, AttributeError, KeyError):
        raise MapRefused("unreadable", "not a map graph file (.mapgraph.json)") from None
    if g.origin != "endorsed":
        raise MapRefused("unreadable", "a .mapgraph.json reference must have origin 'endorsed'")
    g.map_id = map_id
    return g


def read_map(path, map_id: str, extract: ExtractConfig | None = None, rule: MissingRule | None = None) -> MapGraph:
    """Read one map file into a ``MapGraph`` with ``map_id``. ``rule`` None means the format's default rule."""
    suffix = map_suffix(path)
    if suffix == ".vsdx" and (rule is None or rule == DEFAULT_RULES[".vsdx"]):
        try:
            _check_size(path)                             # refuse unsafe parts before parsing the study path
            if _empty(path):
                raise MapRefused("unreadable", "the map file is empty (0 bytes)")
            check_vsdx(path)
            return extract_vsdx(path, map_id, extract)    # the study's path: exactly as before K1
        except MapRefused:
            raise
        except (zipfile.BadZipFile, ET.ParseError, KeyError, EOFError, zlib.error, NotImplementedError):
            # a file that is not a readable Visio drawing: one refusal that never quotes the file (A1)
            if _empty(path):
                raise MapRefused("unreadable", "the map file is empty (0 bytes)") from None
            raise MapRefused("unreadable", "not a readable Visio (.vsdx) file") from None
    if rule is not None and rule.inherit and suffix != ".vsdx":
        raise MapRefused("unsupported_format", "styles inherited from a master shape apply to Visio maps only")
    if suffix in REFUSED_SUFFIXES:
        raise MapRefused("image_or_pdf", f"{suffix} is an image or PDF, not a map file the app can read")
    if suffix not in MAP_SUFFIXES:
        raise MapRefused("unsupported_format", f"{suffix or 'a file with no extension'} is not a map format the "
                                               f"app reads")
    if suffix == _MAPGRAPH:
        g = _read_mapgraph(path, map_id)
        # the one map that is empty on purpose: the app's mode-C reference (reference.empty_reference, "no reference")
        return g if (g.meta or {}).get("reference_type") == "C" else _with_boxes(g)
    reader = READERS.get(suffix)
    if reader is None:
        raise MapRefused("unsupported_format", f"no reader for {suffix} with this missing rule")
    _check_size(path)
    return _with_boxes(reader(Path(path), map_id, extract, rule if rule is not None else DEFAULT_RULES[suffix]))


def _with_boxes(g: MapGraph) -> MapGraph:
    """Every reader's last check: a map with zero boxes is refused, never read as an empty endorsed map (A2)."""
    if not g.nodes:
        raise MapRefused("unreadable", NO_BOXES)
    return g
# Format readers register here (Tasks 7-11). Imported last: each reader module imports
# MapRefused from this module lazily, so either side can be imported first.
from .drawio import read_drawio  # noqa: E402
READERS[".drawio"] = read_drawio
from .excalidraw import read_excalidraw  # noqa: E402
READERS[".excalidraw"] = read_excalidraw
from .mermaid import read_mermaid  # noqa: E402
READERS[".mmd"] = read_mermaid


def _lazy(module: str, name: str) -> Reader:
    """A reader resolved on first use, for modules that import this one at load time (no import cycle)."""
    def reader(*args):
        return getattr(importlib.import_module(f"{__package__}.{module}"), name)(*args)
    return reader


READERS[".csv"] = _lazy("table", "read_table")        # T10
READERS[".xlsx"] = _lazy("table", "read_table")


def box_colors(path) -> dict:
    """``{node_id: {"line": set, "text": set, "fill": set}}``: the outline, text and fill colours (``#rrggbb``) of
    every box the format's reader reads from ``path``, for the formats whose missing rule is a colour (Visio,
    draw.io, Excalidraw). Read only; refusals as ``read_map`` (``MapRefused``)."""
    from .drawio import box_colors as drawio_colors
    from .excalidraw import box_colors as excalidraw_colors
    from .vsdx_rule import box_colors as vsdx_colors
    by_suffix = {".vsdx": vsdx_colors, ".drawio": drawio_colors, ".excalidraw": excalidraw_colors}
    suffix = map_suffix(path)
    if suffix not in by_suffix:
        raise MapRefused("unsupported_format", f"{suffix or 'a file with no extension'} marks missing without colour")
    _check_size(path)
    if _empty(path):
        raise MapRefused("unreadable", "the map file is empty (0 bytes)")
    return by_suffix[suffix](Path(path))
