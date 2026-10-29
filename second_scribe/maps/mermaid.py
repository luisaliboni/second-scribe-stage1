"""Mermaid flowchart reader (``.mmd``): a small, bounded, hand-written parser for one subset of the language.

Supported (line based; ``;`` optional; whole-line ``%%`` comments):
  * header ``flowchart`` or ``graph``, optionally followed by TD, TB, BT, RL or LR;
  * boxes ``a``, ``a[t]``, ``a(t)``, ``a([t])``, ``a[[t]]``, ``a[(t)]``, ``a((t))``, ``a(((t)))``, ``a>t]``,
    ``a{t}``, ``a{{t}}``, ``a[/t/]``, ``a[\\t\\]``, ``a[/t\\]``, ``a[\\t/]`` and quoted text ``a["t"]``;
  * arrows ``-->``, ``---``, ``-.->``, ``-.-``, ``==>``, ``===``, ``--o``, ``--x`` and ``<-->``, ``<-.->``, ``<==>``,
    with a label ``-->|t|`` or ``-- t -->``; chains ``a-->b-->c`` and groups ``a & b --> c``;
  * ``subgraph id [Title]`` / ``subgraph Title`` ... ``end`` (the phase is the title, the innermost wins);
  * ``classDef``, ``class``, ``a:::name``, ``style`` and ``linkStyle`` (they carry the missing rule);
  * ``direction`` inside a subgraph (ignored).

Everything else is refused with ``MermaidUnsupported`` naming the line number and the kind of construct, never the
file's own text (``@{ ... }`` shapes, ``click``, ``:::`` on an arrow, edge ids, a box in two subgraphs, a class on a
box that is never drawn, ...). Nothing is guessed: a statement the parser cannot account for end to end is refused.

The parser scans each statement once with plain string operations (no regular expressions, no evaluation), and
limits line length, boxes, arrows and nesting, so a hostile file costs time proportional to its size at most.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import TYPE_CHECKING

from ..schemas import Edge, MapGraph, Node

if TYPE_CHECKING:
    from .readers import MissingRule

MAX_LINE = 20000            # characters in one line
MAX_NODES = 20000
MAX_EDGES = 20000
MAX_DEPTH = 50              # nested subgraphs
_DIRECTIONS = ("TD", "TB", "BT", "RL", "LR")
_RESERVED = frozenset({"end", "subgraph", "class", "classDef", "style", "linkStyle", "click", "direction",
                       "graph", "flowchart", "default"})
_RULE_KINDS = ("mermaid_class", "classdef_style")
_NOT_DASHED = frozenset({"", "none", "0", "0px", "inherit", "initial", "unset"})
# (opener, closers). The first opener that matches wins, so longer openers come first.
_SHAPES = (("(((", (")))",)), ("((", ("))",)), ("([", ("])",)), ("(", (")",)), ("[[", ("]]",)), ("[(", (")]",)),
           ("[/", ("/]", "\\]")), ("[\\", ("\\]", "/]")), ("[", ("]",)), ("{{", ("}}",)), ("{", ("}",)),
           (">", ("]",)))


class _Bad(Exception):
    """A statement outside the subset. ``what`` describes the construct in our words, never the file's."""

    def __init__(self, what: str, code: str = "unsupported_format"):
        super().__init__(what)
        self.what = what
        self.code = code


# --- text helpers -----------------------------------------------------------------------------------------------

def _is_idchar(c: str) -> bool:
    return c.isalnum() or c == "_"


def _hash_entities(s: str) -> str:
    """Mermaid's ``#35;`` and ``#quot;`` entity codes."""
    if "#" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "#":
            j = s.find(";", i + 1, i + 12)
            body = s[i + 1:j] if j > 0 else ""
            rep = None
            if body.isdigit() and int(body) <= 0x10FFFF:
                try:
                    rep = chr(int(body))
                except ValueError:
                    rep = None
            elif body.isalpha():
                u = html.unescape("&" + body + ";")
                rep = u if u != "&" + body + ";" else None
            if rep is not None:
                out.append(rep)
                i = j + 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_tags(s: str) -> str:
    """Drop HTML tags (``<br/>`` becomes a space), the way Mermaid renders a label."""
    if "<" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "<":
            j = s.find(">", i + 1)
            if j < 0:
                out.append(s[i:])
                break
            k = i + 1
            if k < j and s[k] == "/":
                k += 1
            name_end = k
            while name_end < j and s[name_end].isalpha():
                name_end += 1
            if name_end > k and (name_end == j or s[name_end] in " \t/"):
                out.append(" " if s[k:name_end].lower() == "br" else "")
                i = j + 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _clean(text: str) -> str:
    return " ".join(html.unescape(_hash_entities(_strip_tags(text))).split())


def _norm_color(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v.endswith("!important"):
        v = v[:-len("!important")].strip()
    if v == "red":
        return "#ff0000"
    if v.startswith("#") and all(c in "0123456789abcdef" for c in v[1:]):
        if len(v) == 4:
            return "#" + "".join(c * 2 for c in v[1:])
        if len(v) == 7:
            return v
    return None


def _split_top(s: str, sep: str) -> list[str]:
    """Split on ``sep`` outside quotes and brackets (a ``;`` or a style comma)."""
    parts, depth, quote, start = [], 0, False, 0
    for i, c in enumerate(s):
        if c == '"':
            quote = not quote
        elif quote:
            continue
        elif c in "[({":
            depth += 1
        elif c in "])}":
            depth = max(0, depth - 1)
        elif c == sep and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def _parse_style(text: str) -> dict:
    out = {}
    for piece in _split_top(text, ","):
        piece = piece.strip()
        if not piece:
            continue
        key, colon, val = piece.partition(":")
        key, val = key.strip().lower(), val.strip()
        if not colon or not key or not val:
            raise _Bad("a style list that is not key:value pairs")
        if val.lower().endswith("!important"):
            val = val[:-len("!important")].strip()
        out[key] = val
    if not out:
        raise _Bad("a style list with no style")
    return out


def _is_missing_style(style: dict, colors: frozenset) -> bool:
    if _norm_color(style.get("stroke")) in colors:
        return True
    dash = style.get("stroke-dasharray")
    return dash is not None and dash.strip().lower() not in _NOT_DASHED


def _valid_name(s: str) -> bool:
    """A class or box name: letters, digits, ``_``, and ``-`` between them."""
    if not s:
        return False
    for i, c in enumerate(s):
        if _is_idchar(c):
            continue
        if c == "-" and 0 < i < len(s) - 1 and _is_idchar(s[i + 1]):
            continue
        return False
    return True


# --- scanning one statement -------------------------------------------------------------------------------------

class _Scan:
    def __init__(self, s: str):
        self.s, self.i, self.n = s, 0, len(s)

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i].isspace():
            self.i += 1

    def eof(self) -> bool:
        return self.i >= self.n

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < self.n else ""

    def ident(self) -> str:
        """A name starting at the cursor: letters/digits/``_``, and ``-`` when a name character follows it."""
        s, i, n = self.s, self.i, self.n
        j = i
        while j < n:
            c = s[j]
            if _is_idchar(c):
                j += 1
            elif c == "-" and j > i and j + 1 < n and _is_idchar(s[j + 1]):
                j += 1
            else:
                break
        self.i = j
        return s[i:j]

    def quoted_or_until(self, closers: tuple, label: str) -> str:
        """Text up to the first closer, or a double-quoted text followed by a closer. Moves past the closer."""
        s = self.s
        self.skip_ws()
        if self.peek() == '"':
            k = s.find('"', self.i + 1)
            if k < 0:
                raise _Bad("a quoted text that is never closed")
            text = s[self.i + 1:k]
            self.i = k + 1
            self.skip_ws()
            for c in closers:
                if s.startswith(c, self.i):
                    self.i += len(c)
                    return text
            raise _Bad(label)
        best, which = -1, ""
        for c in closers:
            k = s.find(c, self.i)
            if k >= 0 and (best < 0 or k < best):
                best, which = k, c
        if best < 0:
            raise _Bad(label)
        text = s[self.i:best]
        self.i = best + len(which)
        return text


class _N:
    __slots__ = ("order", "text", "classes", "style", "path", "line")

    def __init__(self, order: int, line: int):
        self.order, self.text, self.classes, self.style, self.path, self.line = order, None, [], {}, (), line


class _Doc:
    def __init__(self):
        self.direction = None
        self.nodes: dict[str, _N] = {}
        self.medges: list[tuple[str, str, str | None, bool]] = []     # source, target, label, bidirectional
        self.sg_titles: list[str] = []
        self.sg_ids: dict[str, int] = {}
        self.stack: list[tuple[int, int]] = []                        # (subgraph index, line of its header)
        self.classdefs: dict[str, dict] = {}
        self.pending_class: list[tuple[int, list, str]] = []
        self.pending_style: list[tuple[int, list, dict]] = []
        self.linkstyles: list[tuple[int, list | str, dict]] = []
        self.line = 0

    # -- boxes
    def mention(self, nid: str, text: str | None, cls: str | None) -> None:
        if nid in _RESERVED:
            raise _Bad("a box named with a reserved word")
        if nid in self.sg_ids:
            raise _Bad("a box or arrow that uses a subgraph's name (links to subgraphs are not supported)")
        node = self.nodes.get(nid)
        if node is None:
            if len(self.nodes) >= MAX_NODES:
                raise _Bad(f"more than {MAX_NODES} boxes", "unreadable")
            node = self.nodes[nid] = _N(len(self.nodes), self.line)
        if text is not None:
            node.text = text
        if cls is not None and cls not in node.classes:
            node.classes.append(cls)
        here = tuple(i for i, _ in self.stack)
        cur = node.path
        if here == cur[:len(here)]:
            pass                                    # the same place or an outer one: the inner path stays
        elif cur == here[:len(cur)]:
            node.path = here                        # first seen outside, or in an outer subgraph: take the inner
        else:
            raise _Bad("a box that appears inside two different subgraphs")

    # -- statements
    def statement(self, st: str) -> None:
        parts = st.split(None, 1)
        word = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
        if word == "subgraph":
            self.subgraph(rest)
        elif word == "end":
            if rest or not self.stack:
                raise _Bad("an 'end' with no open subgraph" if not self.stack else "text after 'end'")
            self.stack.pop()
        elif word == "direction":
            if not self.stack or rest not in _DIRECTIONS:
                raise _Bad("a 'direction' outside a subgraph or with an unknown direction")
        elif word == "classDef":
            self.classdef(rest)
        elif word == "class":
            self.klass(rest)
        elif word == "style":
            self.style(rest)
        elif word == "linkStyle":
            self.linkstyle(rest)
        elif word == "click":
            raise _Bad("click (interactions) is not supported")
        elif word in ("graph", "flowchart"):
            raise _Bad("a second diagram header")
        else:
            self.chain(st)

    def subgraph(self, rest: str) -> None:
        if not rest:
            raise _Bad("a subgraph with no name")
        if len(self.stack) >= MAX_DEPTH:
            raise _Bad(f"subgraphs nested deeper than {MAX_DEPTH}", "unreadable")
        if rest.endswith("]") and "[" in rest:
            lb = rest.find("[")
            sid, inner = rest[:lb].strip(), rest[lb + 1:-1].strip()
            if len(inner) >= 2 and inner[0] == '"' and inner[-1] == '"':
                inner = inner[1:-1]
            elif '"' in inner:
                raise _Bad("a subgraph title with stray quotes")
            title = _clean(inner)
            if not _valid_name(sid):
                raise _Bad("a subgraph id that is not a plain name")
        elif len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"' and '"' not in rest[1:-1]:
            sid, title = rest[1:-1], _clean(rest[1:-1])
        elif any(c in rest for c in '[]{}()"|&:;'):
            raise _Bad("a subgraph header this reader cannot read")
        else:
            sid, title = rest, _clean(rest)
        if not title:
            raise _Bad("a subgraph with an empty title")
        if sid in self.sg_ids:
            raise _Bad("two subgraphs with the same name")
        if sid in self.nodes:
            raise _Bad("a subgraph that uses a box's name")
        self.sg_ids[sid] = len(self.sg_titles)
        self.stack.append((len(self.sg_titles), self.line))
        self.sg_titles.append(title)

    def classdef(self, rest: str) -> None:
        parts = rest.split(None, 1)
        if len(parts) != 2:
            raise _Bad("a classDef without names and styles")
        names = parts[0].split(",")
        if not all(_valid_name(x) for x in names):
            raise _Bad("a classDef with a class name this reader cannot read")
        style = _parse_style(parts[1])
        for name in names:
            self.classdefs.setdefault(name, {}).update(style)

    def _ids(self, token: str) -> list:
        ids = token.split(",")
        if not all(_valid_name(x) for x in ids):
            raise _Bad("a list of box names this reader cannot read")
        return ids

    def klass(self, rest: str) -> None:
        parts = rest.split()
        if len(parts) != 2:
            raise _Bad("a class line that is not 'class <boxes> <name>'")
        if not _valid_name(parts[1]):
            raise _Bad("a class line with a class name this reader cannot read")
        self.pending_class.append((self.line, self._ids(parts[0]), parts[1]))

    def style(self, rest: str) -> None:
        parts = rest.split(None, 1)
        if len(parts) != 2:
            raise _Bad("a style line without boxes and styles")
        self.pending_style.append((self.line, self._ids(parts[0]), _parse_style(parts[1])))

    def linkstyle(self, rest: str) -> None:
        parts = rest.split(None, 1)
        if len(parts) != 2:
            raise _Bad("a linkStyle line without arrows and styles")
        target = parts[0]
        if target == "default":
            idx: list | str = "default"
        else:
            bits = target.split(",")
            if not all(b.isascii() and b.isdigit() and len(b) <= 9 for b in bits):
                raise _Bad("a linkStyle line that does not name arrows by number")
            idx = [int(b) for b in bits]
        self.linkstyles.append((self.line, idx, _parse_style(parts[1])))

    # -- chains: node (& node)* (arrow node (& node)*)*
    def chain(self, st: str) -> None:
        sc = _Scan(st)
        prev = self.group(sc)
        while True:
            sc.skip_ws()
            if sc.eof():
                return
            label, bidir = self.arrow(sc)
            cur = self.group(sc)
            if len(self.medges) + len(prev) * len(cur) > MAX_EDGES:
                raise _Bad(f"more than {MAX_EDGES} arrows", "unreadable")
            for a in prev:
                for b in cur:
                    self.medges.append((a, b, label, bidir))
            prev = cur

    def group(self, sc: _Scan) -> list:
        ids = [self.node(sc)]
        while True:
            sc.skip_ws()
            if sc.peek() != "&":
                return ids
            sc.i += 1
            sc.skip_ws()
            ids.append(self.node(sc))

    def node(self, sc: _Scan) -> str:
        sc.skip_ws()
        if sc.s.startswith(":::", sc.i):
            raise _Bad("a class (:::) on an arrow is not supported")
        nid = sc.ident()
        if not nid:
            raise _Bad("a box name was expected here")
        text = None
        c = sc.peek()
        if c == "@":
            raise _Bad("the node shape syntax @{ ... } (and other @ forms) is not supported")
        for opener, closers in _SHAPES:
            if sc.s.startswith(opener, sc.i):
                sc.i += len(opener)
                text = _clean(sc.quoted_or_until(closers, "a box shape that is not closed"))
                if not text:
                    raise _Bad("a box with empty text")
                break
        cls = None
        if sc.s.startswith(":::", sc.i):
            sc.i += 3
            cls = sc.ident()
            if not cls:
                raise _Bad("a ::: with no class name")
        self.mention(nid, text, cls)
        return nid

    def arrow(self, sc: _Scan) -> tuple:
        """Parse one arrow, its inline text and its pipe label. Returns (label, bidirectional)."""
        s = sc.s
        i = sc.i
        left = s.startswith("<", i)
        if left:
            i += 1
        fam = None
        for f, two in (("-", "--"), (".", "-."), ("=", "==")):
            if s.startswith(two, i):
                fam = f
                break
        if fam is None:
            if i < len(s) and _is_idchar(s[i]):
                sc.i = i
                name = sc.ident()
                if name and sc.peek() == "@":
                    raise _Bad("an edge id (id@-->) is not supported")
            raise _Bad("an arrow was expected here, or the text is outside the supported subset")
        label = None
        c3 = s[i + 2] if i + 2 < len(s) else ""
        if c3.isspace() and not left:
            # inline text:  -- text -->    -. text .->    == text ==>
            sc.i = i + 2
            closers = {"-": ("-->", "---", "--o", "--x"), ".": (".->", ".-"), "=": ("==>", "===")}[fam]
            label, end, head = self._inline_text(sc, closers)
            sc.i = end
            arrow = head
        else:
            arrow, end = self._op(s, i, fam, left)
            sc.i = end
        if arrow == "plain" and left:
            raise _Bad("a bidirectional arrow must be <-->, <-.-> or <==>")
        self._after_op(s, sc.i, arrow)
        # pipe label
        save = sc.i
        sc.skip_ws()
        if sc.peek() == "|":
            if label is not None:
                raise _Bad("an arrow with two labels")
            sc.i += 1
            sc.skip_ws()
            label = sc.quoted_or_until(("|",), "an arrow label that is not closed")
        else:
            sc.i = save
        label = _clean(label) if label is not None else None
        return (label or None), left

    def _op(self, s: str, i: int, fam: str, left: bool) -> tuple:
        n = len(s)
        if fam == "-":
            c = s[i + 2] if i + 2 < n else ""
            if c == ">":
                return "arrow", i + 3
            if c == "-":
                return "plain", i + 3
            if c in ("o", "x") and not left:
                return "head", i + 3
        elif fam == ".":
            c = s[i + 2] if i + 2 < n else ""
            if c == "-":
                if i + 3 < n and s[i + 3] == ">":
                    return "arrow", i + 4
                return "plain", i + 3
        else:
            c = s[i + 2] if i + 2 < n else ""
            if c == ">":
                return "arrow", i + 3
            if c == "=":
                return "plain", i + 3
        raise _Bad("an arrow style this reader does not support")

    def _after_op(self, s: str, end: int, kind: str) -> None:
        nxt = s[end] if end < len(s) else ""
        if kind == "plain" and nxt in ("o", "x", "-", ">"):
            raise _Bad("an ambiguous arrow (a longer arrow, or o/x glued to the arrow); put a space after the arrow")
        if kind == "head" and nxt and _is_idchar(nxt):
            raise _Bad("an ambiguous arrow (o/x glued to the next box); put a space after the arrow")

    def _inline_text(self, sc: _Scan, closers: tuple) -> tuple:
        s = sc.s
        sc.skip_ws()
        if sc.peek() == '"':
            k = s.find('"', sc.i + 1)
            if k < 0:
                raise _Bad("a quoted text that is never closed")
            text, pos = s[sc.i + 1:k], k + 1
            while pos < len(s) and s[pos].isspace():
                pos += 1
            for c in closers:
                if s.startswith(c, pos):
                    return text, pos + len(c), self._kind(c)
            raise _Bad("an arrow text with no closing arrow")
        # raw text: the first closer that follows white space
        j = sc.i
        first = closers[0][:2] if closers[0][0] != "." else ".-"
        k = s.find(first, j)
        while k >= 0:
            if k > j and s[k - 1].isspace():
                for c in closers:
                    if s.startswith(c, k):
                        if c in ("--o", "--x") and k + 3 < len(s) and _is_idchar(s[k + 3]):
                            continue
                        return s[j:k], k + len(c), self._kind(c)
            k = s.find(first, k + 1)
        raise _Bad("an arrow text with no closing arrow")

    @staticmethod
    def _kind(closer: str) -> str:
        if closer.endswith(">"):
            return "arrow"
        if closer in ("--o", "--x"):
            return "head"
        return "plain"


# --- the reader -------------------------------------------------------------------------------------------------

def _header(st: str, doc: _Doc) -> None:
    if st == "---":
        raise _Bad("YAML front matter is not supported; remove it")
    parts = st.split()
    if parts[0] not in ("flowchart", "graph"):
        raise _Bad("only flowchart / graph diagrams are supported (this is a different diagram type or no header)")
    if len(parts) > 2 or (len(parts) == 2 and parts[1] not in _DIRECTIONS):
        raise _Bad("a flowchart header with an unknown direction")
    doc.direction = parts[1] if len(parts) == 2 else None


def read_mermaid(path, map_id: str, extract, rule: "MissingRule") -> MapGraph:
    """Read one ``.mmd`` flowchart into a ``MapGraph`` (origin ``endorsed``). ``extract`` is unused."""
    if rule.kind not in _RULE_KINDS:
        raise MapRefused("unsupported_format", "this missing rule does not apply to Mermaid maps "
                                               "(use a class name or a classDef style)")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise MapRefused("unreadable", "the Mermaid file could not be opened") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MapRefused("unreadable", "the Mermaid file is not UTF-8 text") from None
    doc = _Doc()
    try:
        _parse(text, doc)
    except _Bad as e:
        raise MermaidUnsupported(doc.line, e.what, e.code) from None
    return _build(doc, map_id, rule)


def _parse(text: str, doc: _Doc) -> None:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    seen_header = False
    for no, line in enumerate(lines, 1):
        doc.line = no
        if len(line) > MAX_LINE:
            raise _Bad(f"a line longer than {MAX_LINE} characters", "unreadable")
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        pieces = _split_top(s, ";")
        if not seen_header:
            _header(pieces[0].strip(), doc)
            seen_header = True
            pieces = pieces[1:]
        for st in pieces:
            st = st.strip()
            if st:
                doc.statement(st)
    if not seen_header:
        raise MapRefused("unreadable", "the Mermaid file has no flowchart header")
    if doc.stack:
        doc.line = doc.stack[-1][1]
        raise _Bad("a subgraph that is never closed with 'end'")
    for line, ids, cls in doc.pending_class:
        doc.line = line
        for nid in ids:
            node = doc.nodes.get(nid)
            if node is None:
                raise _Bad("a class on a name that is not a drawn box (classes on subgraphs or arrows are "
                           "not supported)")
            if cls not in node.classes:
                node.classes.append(cls)
    for line, ids, style in doc.pending_style:
        doc.line = line
        for nid in ids:
            node = doc.nodes.get(nid)
            if node is None:
                raise _Bad("a style on a name that is not a drawn box")
            node.style.update(style)
    for line, idx, _ in doc.linkstyles:
        doc.line = line
        if idx != "default" and max(idx) >= len(doc.medges):
            raise _Bad("a linkStyle that names an arrow number the map does not have")
    if not doc.nodes:
        raise MapRefused("unreadable", "the Mermaid file draws no boxes")


def _build(doc: _Doc, map_id: str, rule: "MissingRule") -> MapGraph:
    colors = frozenset(c for c in (_norm_color(x) for x in rule.colors) if c)
    by_style = rule.kind == "classdef_style"
    default_style = doc.classdefs.get("default", {})

    def node_wished(n: _N) -> bool:
        if not by_style:
            return any(c in rule.class_names for c in n.classes)
        merged = dict(default_style)
        for c in n.classes:
            merged.update(doc.classdefs.get(c, {}))
        merged.update(n.style)
        return _is_missing_style(merged, colors)

    link_default: dict = {}
    link_by_idx: dict[int, dict] = {}
    for _, idx, style in doc.linkstyles:
        if idx == "default":
            link_default.update(style)
        else:
            for k in idx:
                link_by_idx.setdefault(k, {}).update(style)

    nodes, n_missing = [], 0
    for nid, n in doc.nodes.items():
        wished = node_wished(n)
        n_missing += wished
        phase = doc.sg_titles[n.path[-1]] if n.path else None
        nodes.append(Node(node_id=nid, text=n.text if n.text is not None else nid, node_type="other", phase=phase,
                          status="wished" if wished else "experienced"))
    edges, n_bidir = [], 0
    for k, (a, b, label, bidir) in enumerate(doc.medges):
        merged = dict(link_default)
        merged.update(link_by_idx.get(k, {}))
        wished = _is_missing_style(merged, colors)
        n_missing += wished
        st = "wished" if wished else "experienced"
        edges.append(Edge(source=a, target=b, status=st, glued=True, label=label))
        if bidir:
            n_bidir += 1
            edges.append(Edge(source=b, target=a, status=st, glued=True, label=label))
    g = MapGraph(map_id=map_id, origin="endorsed", nodes=nodes, edges=edges, unresolved_edges=0)
    g.meta = {"n_pages": 1, "n_shapes": len(nodes), "n_connectors": len(doc.medges), "n_missing": n_missing,
              "n_phases": len(doc.sg_titles), "n_bidirectional": n_bidir, "direction": doc.direction}
    return g


# readers.py imports this module to register it, so the base classes are imported last: whichever module is
# imported first, ``read_mermaid`` (defined above) already exists when the other one asks for it.
from .readers import MapRefused  # noqa: E402


class MermaidUnsupported(MapRefused):
    """Mermaid text outside the supported subset. ``line_no`` is the 1-based line; ``detail`` names the construct."""

    def __init__(self, line_no: int, what: str, code: str = "unsupported_format"):
        super().__init__(code, f"line {line_no}: {what}")
        self.line_no = line_no
