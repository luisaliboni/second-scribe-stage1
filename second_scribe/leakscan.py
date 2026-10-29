"""The engine of the leak scanner (``audits.audit_leaks``): what counts as the same text, what counts as a name, and
how a file of any kind is turned into text. No model, standard library only.

Same text means the same *folded* tokens: Unicode compatibility-decomposed, combining marks and invisible format
characters (zero-width spaces, soft hyphens) dropped, a small table of Latin look-alike letters (Cyrillic, Greek)
mapped to Latin, case folded, hyphenated line breaks optionally joined. The real texts and the scanned texts go
through the same fold, so an accent, a curly quote, a zero-width space or a Cyrillic "a" does not hide a copy.

Names are matched on the folded text with case kept: initials only in capitals (optionally "X.Y." or "XY2"),
id-shaped names (``K07``, ``KC12``, ``SYN-OW01``) whatever their case and separator, longer explicit names as
whole words. File-name stems give only all-capital initials and id-shaped parts, never ordinary words.

A file is read in several views and a hit counts once: raw text, HTML-entity-decoded text, JSON-decoded strings,
markup with the tags removed (runs of a Word paragraph concatenated, and also separated), RTF/TeX with control
words removed. Archives are opened (nested, bounded); what cannot be read as text fails closed (``Unscannable``).

Accepted limits: a 4-word fragment; deliberate obfuscation (base64, URL percent-encoding, ROT13, synonyms,
paraphrase); text drawn as pixels or as outlines.
"""
from __future__ import annotations

import bz2
import glob
import gzip
import io
import json
import lzma
import re
import subprocess
import tarfile
import unicodedata
import zipfile
import zlib
from collections import Counter
from html import unescape
from pathlib import Path

from .textnorm import _CHAR_MAP, STOPWORDS

NGRAM = 5
MAX_STRING = 40
MAX_UNPACK = 400_000_000             # bytes an archive may expand to
MAX_DEPTH = 4                        # archives inside archives
MAX_UNDECODABLE = 0.05               # share of undecodable characters tolerated in a text file
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class IndexUnavailable(ValueError):
    """The forbidden sets cannot be built (so nothing may be called clean). ``code`` is a short reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Unscannable(Exception):
    """A file that cannot be read as text. ``reason`` is a short code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ folding

_CYR_LOWER = dict(zip("\u0430\u0435\u043e\u0440\u0441\u0445\u0443\u0456\u0458\u0455\u04bb\u0501\u051b\u0461\u043a\u043c\u0442",
                      "aeopcxyijshdqwkmt"))
_CYR_UPPER = dict(zip("\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0423\u0406\u0408\u0405",
                      "ABEKMHOPCTXYIJS"))
_GRK_LOWER = dict(zip("\u03b1\u03bf\u03b9\u03bd\u03c1\u03ba\u03c7\u03c5\u03b5\u03c4\u03c9", "aoivpkxuetw"))
_GRK_UPPER = dict(zip("\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7",
                      "ABEZHIKMNOPTYX"))
CONFUSABLES = {**_CYR_LOWER, **_CYR_UPPER, **_GRK_LOWER, **_GRK_UPPER}
_BREAK = re.compile(r"(?<=\w)[-\u00ad\u2010\u2011\u2012]\s*\r?\n\s*(?=\w)")


def _fold(text: str, lower: bool = True, join_breaks: bool = False) -> tuple[str, list[int] | None]:
    """(folded text, index of the original character for every folded character; None = identical positions)."""
    drop: set[int] = set()
    if join_breaks:
        for m in _BREAK.finditer(text):
            drop.update(range(m.start(), m.end()))
    if text.isascii() and not drop:
        return (text.lower() if lower else text), None
    out: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)
    while i < n:
        if i in drop:
            i += 1
            continue
        j = i + 1
        while j < n and j not in drop and (unicodedata.combining(text[j]) or unicodedata.category(text[j]) == "Cf"):
            j += 1
        seg = unicodedata.normalize("NFKD", text[i:j])
        seg = "".join(c for c in seg if not unicodedata.combining(c) and unicodedata.category(c) not in ("Cf", "Mn", "Me"))
        seg = "".join(CONFUSABLES.get(c, c) for c in seg.translate(_CHAR_MAP))
        if lower:
            seg = seg.lower()
        out.append(seg)
        idx.extend([i] * len(seg))
        i = j
    return "".join(out), idx


_TOKEN = re.compile(r"[a-z0-9']+")


def tokens_with_offsets(text: str, join_breaks: bool = False) -> list[tuple[str, int, int]]:
    """``(token, start, end)`` of the folded tokens of ``text``, offsets in the ORIGINAL text (``end`` exclusive)."""
    norm, idx = _fold(text, True, join_breaks)
    out = []
    for m in _TOKEN.finditer(norm):
        s, e = m.span()
        while s < e and norm[s] == "'":
            s += 1
        while e > s and norm[e - 1] == "'":
            e -= 1
        if s < e:
            if idx is None:
                out.append((norm[s:e], s, e))
            else:
                out.append((norm[s:e], idx[s], idx[e] if e < len(idx) else len(text)))
    return out


def leak_tokens(text: str) -> list[str]:
    return [t for t, _, _ in tokens_with_offsets(text)]


def _stop_heavy(window: list[str], n: int) -> bool:
    """At least n-1 of the n tokens are stopwords: such a run names nothing and would match ordinary prose."""
    return sum(t in STOPWORDS for t in window) >= max(n - 1, 1)


def _gram_hash(window: list[str]) -> int:
    return hash(" ".join(window))


# ------------------------------------------------------------------ names

_ID = re.compile(r"([A-Za-z]+(?:[-_][A-Za-z]+)*)[-_ ]?(\d+)")
_INITIALS = re.compile(r"[A-Za-z]{2,4}")


GENERIC_WORDS = frozenset({"interviewer", "interviewee", "participant", "caregiver", "provider", "researcher",
                           "interview", "transcript", "speaker", "parent", "owner", "officer", "respondent",
                           "patient", "map", "note", "notes"})


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


_DEMO_NAMES: frozenset | None = None


def demo_names() -> frozenset:
    """Normalised names of the synthetic demo config (ids, labels, interviewer codes): they are public, so they are
    never forbidden. Cached; an unreadable demo config gives an empty set."""
    global _DEMO_NAMES
    if _DEMO_NAMES is None:
        out: set[str] = set()
        try:
            d = json.loads((Path(__file__).resolve().parent.parent / "configs" / "demo.json").read_text(encoding="utf-8"))
            c = d.get("corpus") or {}
            for table in (c.get("participant_labels"), c.get("interviewer_labels")):
                for labels in (table or {}).values():
                    out |= {_key(x) for x in labels if isinstance(x, str)}
            out |= {_key(x) for x in (c.get("roles") or {}) if isinstance(x, str)}
            out |= {_key(x) for x in (c.get("interviewers") or {}).values() if isinstance(x, str)}
        except (OSError, ValueError, AttributeError):
            pass
        _DEMO_NAMES = frozenset(out - {""})
    return _DEMO_NAMES


_DEMO_PUBLIC: frozenset | None = None


def demo_public_words() -> frozenset:
    """The demo config's speaker labels and interviewer codes (not its ``SYN-*`` ids): what a name of the real corpus must
    not collide with, because the public kit uses those words."""
    global _DEMO_PUBLIC
    if _DEMO_PUBLIC is None:
        out: set[str] = set()
        try:
            d = json.loads((Path(__file__).resolve().parent.parent / "configs" / "demo.json").read_text(encoding="utf-8"))
            c = d.get("corpus") or {}
            for table in (c.get("participant_labels"), c.get("interviewer_labels")):
                for labels in (table or {}).values():
                    out |= {_key(x) for x in labels if isinstance(x, str)}
            out |= {_key(x) for x in (c.get("interviewers") or {}).values() if isinstance(x, str)}
        except (OSError, ValueError, AttributeError):
            pass
        _DEMO_PUBLIC = frozenset(out - {""})
    return _DEMO_PUBLIC


def collides_with_public(raw: str) -> bool:
    """A name (or the prefix of an id-shaped name, ``PT01``) that is a demo label, a demo interviewer code or a generic
    role word."""
    s = _fold(raw.strip(), lower=False)[0].strip()
    public = demo_public_words() | GENERIC_WORDS
    if _key(s) in public:
        return True
    m = _ID.fullmatch(s)
    return bool(m) and (_key(m.group(1)) in public or any(_key(w) in public for w in re.split(r"[-_ ]", m.group(1))))


def _id_regex(prefix: str, digits: str) -> str:
    pre = r"[-_ ]?".join(re.escape(p) for p in re.split(r"[-_ ]", prefix.upper()))
    d = digits.lstrip("0")
    return rf"{pre}[-_ ]?{'0*' + d if d else '0+'}"


def _initials_regex(s: str) -> str:
    return r"[.\s]?".join(re.escape(c) for c in s.upper()) + r"(?:\d{1,2})?"


def classify_name(raw: str, explicit: bool, filter_public: bool = True) -> tuple[str, str] | None:
    """("cs" | "ci", regex) for a name, None when it is not a name or is public. An id (``K07``: letters, digits)
    matches in CAPITALS only, with any separator or zero padding (``K7``, ``K-07``, ``K 07``), never ``p7``. Initials
    (2-4 capitals, or 2-3 letters of an explicit name) match in capitals only. An explicit name of 4+ letters or with
    a space matches as whole word(s) in any case. A name taken from a file name (``explicit`` False) must be id-shaped.
    With ``filter_public`` (speaker labels, file stems) the names of the synthetic demo config and generic role words
    (Interviewer, Participant...) are not names; without it (the id map, roles, pairs) nothing is dropped."""
    s = _fold(raw.strip(), lower=False)[0].strip()
    if not s:
        return None
    if filter_public and (_key(s) in demo_names() or _key(s) in GENERIC_WORDS):
        return None
    m = _ID.fullmatch(s)
    if m:
        if filter_public and (any(_key(w) in GENERIC_WORDS or _key(w) in demo_names()
                                  for w in re.split(r"[-_ ]", m.group(1))) or _key(m.group(1)) in GENERIC_WORDS):
            return None
        return "cs", _id_regex(*m.groups())
    if not explicit:
        return None
    if _INITIALS.fullmatch(s):
        if s.isupper() or len(s) <= 3:
            return "cs", _initials_regex(s)
        return "ci", re.escape(s)                       # a 4-letter name that is not initials, such as "Jose"
    if len(s) >= 5 or " " in s:
        return "ci", r"[\s._\-]+".join(re.escape(w) for w in re.split(r"[\s._\-]+", s) if w)
    return None


def stem_parts(stem: str) -> list[str]:
    """The pieces of a file-name stem (split on ``_ - . space``) plus the stem itself."""
    return [stem, *[x for x in re.split(r"[_\-.\s]+", stem) if x]]


def _compile_names(explicit: list[str], stems: list[str], strict: list[str] = ()):
    """(case-insensitive regex, case-sensitive regex, count, public exempt count, public collision count). ``explicit``
    (speaker labels) and ``stems`` are filtered against the public names; ``strict`` (id map, roles, pairs) never is, and a
    strict name that collides with a public one is counted."""
    ci: set[str] = set()
    cs: set[str] = set()
    exempt = collisions = 0
    for raw in explicit:
        c = classify_name(raw, True)
        if c:
            (cs if c[0] == "cs" else ci).add(c[1])
        elif isinstance(raw, str) and raw.strip():
            exempt += 1
    for raw in strict:
        c = classify_name(raw, True, filter_public=False)
        if c:
            (cs if c[0] == "cs" else ci).add(c[1])
        collisions += collides_with_public(raw)
    for raw in stems:                       # a file name gives away only its id-shaped parts (the id map names the rest)
        if _key(raw) in demo_names():       # a public file name (the demo corpus) names nobody
            exempt += 1
            continue
        for part in stem_parts(raw):
            c = classify_name(part, False)
            if c:
                (cs if c[0] == "cs" else ci).add(c[1])
    # a whole stem (letters, digits, separators) is matched literally too, unless it is public
    for raw in stems:
        if len(raw) >= 6 and re.search(r"[\d_]", raw) and _key(raw) not in demo_names() \
                and _key(raw) not in GENERIC_WORDS:
            ci.add(r"[-_\s]?".join(re.escape(p) for p in re.split(r"[-_\s]+", _fold(raw, lower=False)[0]) if p))

    def build(items, flags):
        if not items:
            return None
        alt = "|".join(sorted(items, key=lambda s: (-len(s), s)))
        return re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", flags)
    return build(ci, re.IGNORECASE), build(cs, 0), len(ci) + len(cs), exempt, collisions


# ------------------------------------------------------------------ patterns (contact details)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\w])"
                    r"|(?<![\w])\+\d{1,3}[\s.\-]?(?:\(\d{1,4}\)[\s.\-]?)?\d{1,5}(?:[\s.\-]\d{2,6}){1,4}(?![\w])")
_POSTAL = re.compile(r"(?<![A-Za-z0-9])[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ \-]?\d[ABCEGHJ-NPRSTV-Z]\d(?![A-Za-z0-9])",
                     re.IGNORECASE)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
# loopback (127.x.x.x) and 0.0.0.0 name no machine of anyone's: tests and docs use them
_IPV4 = re.compile(rf"(?<![\d.])(?!127\.|0\.0\.0\.0(?!\.?\d))(?:{_OCTET}\.){{3}}{_OCTET}(?!\.?\d)")
_PATTERNS = (("email", _EMAIL), ("phone", _PHONE), ("postal", _POSTAL), ("ip", _IPV4))
# 555-0100 to 555-0199 are reserved for fiction: North American format only (optional +1, 3-digit area code)
_FICTIONAL_PHONE = re.compile(r"(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])555[\s.\-]01\d\d")
_RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net", "localhost", "anthropic.com",
                               "users.noreply.github.com"})
_RESERVED_SUFFIXES = (".test", ".invalid", ".example", ".localhost", ".example.com", ".example.org", ".example.net")


def _benign(kind: str, text: str) -> bool:
    """Matches that cannot be anyone's contact details: addresses at reserved example domains and the
    ``noreply@anthropic.com`` of commit trailers, fictional 555-01xx numbers."""
    if kind == "phone":
        return len(re.sub(r"\D", "", text)) < 9 or bool(_FICTIONAL_PHONE.fullmatch(text.strip()))
    if kind == "email":
        local, _, domain = text.lower().rpartition("@")
        if domain == "anthropic.com":
            return local == "noreply"
        return domain in _RESERVED_DOMAINS or domain.endswith(_RESERVED_SUFFIXES)
    return False


# ------------------------------------------------------------------ views of a text

_ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,8});")
_BLOCK = re.compile(r"</(?:w:p|a:p|p|div|li|tr|td|th|si|row|h[1-6]|text:p)>|<(?:br|w:br|w:tab|a:br|w:cr)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]*>")
_MARKUP_EXT = frozenset({".html", ".htm", ".xhtml", ".xml", ".svg", ".rels", ".opf", ".ncx", ".kml"})
_JSON_EXT = frozenset({".json", ".ipynb", ".jsonl", ".ndjson", ".geojson"})
_CONTROL_EXT = frozenset({".rtf", ".tex", ".latex", ".sty"})


def _json_strings(text: str, suffix: str) -> list[str] | None:
    docs = []
    try:
        docs.append(json.loads(text))
    except ValueError:
        if suffix not in (".jsonl", ".ndjson") and "\n" not in text.strip():
            return None
        for line in text.splitlines():
            if line.strip():
                try:
                    docs.append(json.loads(line))
                except ValueError:
                    continue
        if not docs:
            return None
    out: list[str] = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    for d in docs:
        walk(d)
    return out


def _markup_views(text: str) -> list[str]:
    t = _BLOCK.sub("\n", text)
    return [unescape(unescape(_TAG.sub("", t))), unescape(unescape(_TAG.sub(" ", t)))]


_CTRL = re.compile(r"\\[A-Za-z]+-?\d* ?|[{}]")


def _control_views(text: str) -> list[str]:
    t = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "replace"), text)
    t = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), t)
    return [_CTRL.sub("", t), _CTRL.sub(" ", t)]


def extra_views(text: str, suffix: str) -> list[str]:
    """The decoded views of ``text`` besides the raw one (see the module docstring)."""
    s = suffix.lower()
    out: list[str] = []
    if _ENTITY.search(text):
        out.append(unescape(unescape(text)))
    head = text.lstrip()[:1]
    if s in _JSON_EXT or head in ("{", "["):
        strings = _json_strings(text, s)
        if strings is not None:
            out += ["\n".join(strings), "".join(strings)]
    if s in _MARKUP_EXT or (head == "<" and "</" in text):
        out += _markup_views(text)
    if s in _CONTROL_EXT or text.startswith("{\\rtf"):
        out += _control_views(text)
    return out


def long_strings(text: str) -> list[tuple[str, int]]:
    """Strings (and keys) over 40 characters in a JSON text, except a sha256 digest or a fixed kit note;
    ``invalid_json`` when it does not parse."""
    try:
        obj = json.loads(text)
    except ValueError:
        return [("invalid_json", 0)]
    from .report import KIT_NOTES
    out: list[tuple[str, int]] = []

    def walk(x):
        if isinstance(x, str):
            if len(x) > MAX_STRING and not _DIGEST.fullmatch(x) and x not in KIT_NOTES:
                pos = text.find(json.dumps(x, ensure_ascii=False)[1:-1])
                out.append(("long_string", max(pos, 0)))
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(obj)
    return out


# ------------------------------------------------------------------ the index

class LeakIndex:
    """The forbidden sets, in memory only: hashes of every ``n``-token run of the real texts, and the compiled
    names. ``repr`` and ``stats`` carry counts, never content."""

    def __init__(self, n: int, grams: set[int], names: tuple, stats: dict, problems: list[str]):
        self.n, self._grams, self._names, self.stats, self.problems = n, grams, names, stats, problems

    def __repr__(self) -> str:
        return f"LeakIndex(n={self.n}, {self.stats})"

    @classmethod
    def build(cls, texts, names=(), n: int = NGRAM, problems=(), *, stems=(), id_map_entries: int = 0,
              strict_names=(), exempt_codes: int = 0) -> "LeakIndex":
        grams: set[int] = set()
        n_texts = 0
        for t in texts:
            if not t or not t.strip():
                continue
            seen = False
            for join in ((False, True) if _BREAK.search(t) else (False,)):
                toks = [x for x, _, _ in tokens_with_offsets(t, join)]
                for i in range(len(toks) - n + 1):
                    w = toks[i:i + n]
                    if not _stop_heavy(w, n):
                        grams.add(_gram_hash(w))
                seen = seen or bool(toks)
            n_texts += seen
        if not n_texts:
            raise IndexUnavailable("no_real_text", "the forbidden n-gram source holds no text: a leak scan against "
                                   "nothing would pass everything")
        ci, cs, n_names, exempt, collisions = _compile_names(list(names), list(stems), list(strict_names))
        problems = list(problems) + (["public_name_collision"] if collisions else [])
        return cls(n, grams, (ci, cs), {"n_texts": n_texts, "n_ngrams": len(grams), "n_names": n_names,
                                        "id_map_entries": id_map_entries, "names_public_exempt": exempt + exempt_codes,
                                        "public_name_collisions": collisions}, problems)

    @classmethod
    def from_config(cls, cfg, n: int = NGRAM) -> "LeakIndex":
        """From a run config: every transcript paragraph (headers, footers and comments of a Word file too), every
        endorsed map label, every string of ``<out_dir>/work`` (the AI maps' labels and quotes, canonical labels...),
        participant and interviewer labels, interviewer codes, pids, file-name initials and the id map. A real
        corpus must name its id map (``corpus.participant_id_map``)."""
        from .maps.readers import read_map, rule_for
        from .maps.vsdx import ExtractConfig
        from .transcripts import read_transcript_paras
        c = cfg.corpus
        tpaths, mpaths = corpus_files(cfg)
        if not tpaths:
            raise IndexUnavailable("no_transcripts", "the config names no real transcript files: a leak scan against "
                                   "nothing would pass everything")
        if c.get("kind") == "real" and not c.get("participant_id_map"):
            raise IndexUnavailable("id_map_required", "a real corpus must name its participant id map "
                                   "(corpus.participant_id_map): without it the scan cannot know the participants' "
                                   "initials")
        texts: list[str] = []
        stems: set[str] = set()
        problems: list[str] = []
        for p in tpaths:
            try:
                texts.extend(read_transcript_paras(p))
                if Path(p).suffix.lower() == ".docx":
                    texts.extend(docx_extra_paragraphs(p))
            except Exception:  # noqa: BLE001
                problems.append("transcript_unreadable")
            stems.add(Path(p).stem)
        ext = ExtractConfig(**{k: tuple(v) if isinstance(v, list) else v
                               for k, v in ((cfg.context or {}).get("extract") or {}).items()})
        for p in mpaths:
            try:
                g = read_map(p, Path(p).stem, ext, rule_for(c, p))
                texts += [x.text for x in g.nodes] + [e.label for e in g.edges if e.label]
            except Exception:  # noqa: BLE001
                problems.append("map_unreadable")
            stems.add(Path(p).stem)
        labels: set[str] = set()                    # speaker labels: public ones (pt, iv, Interviewer) are exempt
        strict: set[str] = set()                    # id map, roles, pairs, interviewer codes: never dropped
        exempt_codes = 0
        for table in (c.get("participant_labels"), c.get("interviewer_labels")):
            for lab in (table or {}).values():
                labels |= set(lab)
        strict |= set(c.get("roles") or {}) | set(c.get("pairs") or {})
        for code in (c.get("interviewers") or {}).values():
            if isinstance(code, str) and re.fullmatch(r"I\d+", code):
                exempt_codes += 1                   # public pseudonyms (I1, I2...): counted, not names
            else:
                strict.add(code)
        labels = {x for x in labels if isinstance(x, str)}
        strict = {x for x in strict if isinstance(x, str)}
        n_map = 0
        idmap = c.get("participant_id_map")
        if idmap:
            try:
                data = json.loads(cfg.path(idmap).read_text(encoding="utf-8-sig"))
                for key, val in (data.get("map") or {}).items():
                    n_map += 1
                    strict.add(key)
                    if isinstance(val, dict):
                        strict |= {v for v in val.values() if isinstance(v, str)}
            except (OSError, ValueError, AttributeError):
                problems.append("id_map_unreadable")
            if not n_map and "id_map_unreadable" not in problems:
                problems.append("id_map_empty")
        texts += work_strings(Path(cfg.out_dir) / "work")
        return cls.build(texts, labels, n, problems, stems=stems, id_map_entries=n_map, strict_names=strict,
                         exempt_codes=exempt_codes)

    # ---- scanning

    def _grams_hits(self, view: str) -> list[tuple[str, int]]:
        passes = []
        for join in ((False, True) if _BREAK.search(view) else (False,)):
            toks = tokens_with_offsets(view, join)
            hits, run_end = [], -1
            for i in range(len(toks) - self.n + 1):
                w = [t for t, _, _ in toks[i:i + self.n]]
                if not _stop_heavy(w, self.n) and _gram_hash(w) in self._grams:
                    if i > run_end:                       # a new span (not overlapping the previous hit)
                        hits.append(("ngram", toks[i][1]))
                    run_end = max(run_end, i + self.n - 1)
            passes.append(hits)
        out = passes[0]
        if len(passes) > 1 and len(passes[1]) > len(out):          # found only with the line breaks joined
            out = out + passes[1][len(out):]
        return out

    def _scan_view(self, view: str) -> list[tuple[str, int]]:
        hits = self._grams_hits(view)
        folded, idx = _fold(view, lower=False)

        def at(pos: int) -> int:
            return pos if idx is None else idx[min(pos, len(idx) - 1)]
        spans = sorted((m.start(), m.end()) for pat in self._names if pat is not None for m in pat.finditer(folded))
        merged: list[list[int]] = []
        for a, b in spans:                       # one hit per name, however many of its parts or forms matched
            if merged and a < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        hits += [("name", at(a)) for a, _b in merged]
        for kind, pat in _PATTERNS:
            hits += [(kind, at(m.start())) for m in pat.finditer(folded) if not _benign(kind, m.group())]
        return hits

    def scan(self, text: str, *, json_aggregate: bool = False, suffix: str = "",
             more_views: tuple = ()) -> list[tuple[str, int]]:
        """(kind, character offset in ``text``) of every hit. The raw view gives the offsets; a kind found more often
        in another view adds the surplus at offset 0 (so nothing is counted twice)."""
        hits = self._scan_view(text)
        if json_aggregate:
            hits += long_strings(text)
        best = Counter(k for k, _ in hits)
        for view in (*extra_views(text, suffix), *more_views):
            for kind, n in Counter(k for k, _ in self._scan_view(view)).items():
                if n > best[kind]:
                    hits += [(kind, 0)] * (n - best[kind])
                    best[kind] = n
        return sorted(hits, key=lambda h: (h[1], h[0]))


def work_strings(work: Path) -> list[str]:
    """Every string with a space in it, of every JSON file under ``work`` (a model's labels and quotes included; the
    kit's own identifiers, such as spec keys, are not text and would only make false matches)."""
    out: list[str] = []
    for p in sorted(work.rglob("*.json")) if work.exists() else []:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stack = [obj]
        while stack:
            x = stack.pop()
            if isinstance(x, str):
                if " " in x.strip() and not _DIGEST.fullmatch(x):      # prose, not an identifier of the kit's own
                    out.append(x)
            elif isinstance(x, dict):
                stack.extend(x.keys())
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return out


def corpus_files(cfg) -> tuple[list[Path], list[Path]]:
    c = cfg.corpus
    pairs = c.get("pairs")
    if pairs:
        maps = [cfg.path(str(m)) for m, _t in pairs.values()]
        trs = [cfg.path(str(t)) for _m, t in pairs.values()]
    else:
        maps = [Path(p) for p in sorted(glob.glob(str(cfg.path(c["maps_glob"]))))] if c.get("maps_glob") else []
        trs = [Path(p) for p in sorted(glob.glob(str(cfg.path(c["transcripts_glob"]))))] \
            if c.get("transcripts_glob") else []
    return sorted(set(trs)), sorted(set(maps))


def docx_extra_paragraphs(path) -> list[str]:
    """Paragraphs of every other text part of a Word file (headers, footers, footnotes, endnotes, comments)."""
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if re.fullmatch(r"word/(header|footer|footnotes|endnotes|comments)\d*\.xml", name):
                xml = z.read(name).decode("utf-8", "replace")
                out += [x for x in _markup_views(xml)[0].split("\n") if x.strip()]
    return out


# ------------------------------------------------------------------ files and archives

_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".woff", ".woff2",
                        ".ttf", ".otf", ".odttf", ".emf", ".wmf"})
_BENIGN_MEMBER_EXT = _IMAGE_EXT | frozenset({".bin", ".dat", ".dll"})     # inside a document: scanned as strings
_ZIP_EXT = frozenset({".docx", ".xlsx", ".pptx", ".vsdx", ".odt", ".ods", ".odp", ".epub", ".zip", ".jar", ".nupkg",
                      ".whl", ".ipa"})
_UNSCANNABLE_EXT = frozenset({".pdf", ".doc", ".xls", ".ppt", ".vsd", ".7z", ".rar", ".mp3", ".mp4", ".mov", ".wav",
                              ".m4a", ".sqlite", ".db", ".pkl", ".npy", ".npz", ".pyc", ".so", ".dylib", ".exe"})
_SVG_DATA_SIBLINGS = (".json", ".csv", ".tsv", ".md", ".txt")


def _strings_view(data: bytes) -> str:
    return "\n".join(m.decode("ascii") for m in re.findall(rb"[\x20-\x7e\t\r\n]{6,}", data))


def _png_texts(data: bytes) -> list[str]:
    out, i = [], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + length]
        try:
            if kind == b"tEXt":
                out.append(body.decode("latin-1").replace("\x00", " "))
            elif kind == b"zTXt":
                k, _, rest = body.partition(b"\x00")
                out.append(k.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("utf-8", "replace"))
            elif kind == b"iTXt":
                k, _, rest = body.partition(b"\x00")
                flag, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _tk, _, txt = rest.partition(b"\x00")
                out.append(k.decode("utf-8", "replace") + " " +
                           (zlib.decompress(txt) if flag else txt).decode("utf-8", "replace"))
        except (zlib.error, IndexError, ValueError):
            raise Unscannable("bad_png_text") from None
        i += 12 + length
    return out


def _decode_text(data: bytes) -> tuple[str, str, list[str]]:
    """(text, encoding, extra views): UTF-8/16/32 by byte-order mark or by the pattern of zero bytes; undecodable UTF-8
    is read as cp1252 as well. Raises ``Unscannable`` for binary data or when more than 5% cannot be decoded."""
    boms = ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"), (b"\xef\xbb\xbf", "utf-8-sig"),
            (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
    for bom, enc in boms:
        if data.startswith(bom):
            text = data.decode(enc, "replace")
            if text.count("\ufffd") > MAX_UNDECODABLE * max(len(text), 1):
                raise Unscannable("undecodable")
            return text, enc, []
    head = data[:4096]
    if b"\x00" in head:
        enc = _guess_wide(head)
        if enc is None:
            raise Unscannable("binary")
        text = data.decode(enc, "replace")
        if text.count("\ufffd") > MAX_UNDECODABLE * max(len(text), 1) or "\x00" in text[:2000]:
            raise Unscannable("binary")
        return text, enc, []
    try:
        return data.decode("utf-8"), "utf-8", []
    except UnicodeDecodeError:
        pass
    cp = data.decode("cp1252", "replace")
    if cp.count("\ufffd") > MAX_UNDECODABLE * max(len(cp), 1) or b"\x00" in data:
        raise Unscannable("undecodable")
    return data.decode("utf-8", "surrogateescape"), "utf-8", [cp]


def _guess_wide(head: bytes) -> str | None:
    n = len(head) - len(head) % 4
    if n < 8:
        return None
    z = [sum(1 for k in range(p, n, 4) if head[k] == 0) / (n // 4) for p in range(4)]
    if z[1] > .8 and z[2] > .8 and z[3] > .8 and z[0] < .2:
        return "utf-32-le"
    if z[0] > .8 and z[1] > .8 and z[2] > .8 and z[3] < .2:
        return "utf-32-be"
    even, odd = (z[0] + z[2]) / 2, (z[1] + z[3]) / 2
    if odd > .4 and even < .1:
        return "utf-16-le"
    if even > .4 and odd < .1:
        return "utf-16-be"
    return None


def _bounded(read) -> bytes:
    data = read(MAX_UNPACK + 1)
    if len(data) > MAX_UNPACK:
        raise Unscannable("too_large")
    return data


def read_items(name: str, data: bytes, depth: int = 0, member: bool = False) -> list[tuple[str, str, str, str, list[str]]]:
    """[(location suffix, text, encoding, file suffix, extra views)] for a file or archive member. Archives are
    opened (zip, gzip, bzip2, xz, tar; at most 4 deep); images and fonts give their embedded text and their
    printable strings; anything else that is not text raises ``Unscannable``."""
    if depth > MAX_DEPTH:
        raise Unscannable("nested_too_deep")
    ext = Path(name).suffix.lower()
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06") or (ext in _ZIP_EXT and data[:2] == b"PK"):
        return _zip_items(data, depth)
    for magic, mod in ((b"\x1f\x8b", gzip), (b"BZh", bz2), (b"\xfd7zXZ\x00", lzma)):
        if data.startswith(magic):
            try:
                inner = _bounded(mod.open(io.BytesIO(data)).read)
            except (OSError, EOFError, lzma.LZMAError) as exc:
                raise Unscannable("bad_compressed") from exc
            return [(f"!0{loc}", *rest) for loc, *rest in read_items(re.sub(r"\.(gz|bz2|xz)$", "", name), inner,
                                                                     depth + 1, True)]
    if data.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\xd0\xcf\x11\xe0")) or ext in _UNSCANNABLE_EXT \
            or data.startswith(b"%PDF-"):
        raise Unscannable("unreadable_format")
    if ext in (".tar", ".tgz") or data[257:262] == b"ustar":
        return _tar_items(data, depth)
    if ext in _IMAGE_EXT or data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM")):
        texts = _png_texts(data) if data.startswith(b"\x89PNG\r\n\x1a\n") else []
        strings = _strings_view(data)
        return [("", "\n".join(texts) if texts else strings, "latin-1", ext, [strings] if texts else [])]
    try:
        text, enc, extra = _decode_text(data)
    except Unscannable:
        if member and ext in _BENIGN_MEMBER_EXT:
            return [("", _strings_view(data), "latin-1", ext, [])]
        raise
    return [("", text, enc, ext, extra)]


def _zip_items(data: bytes, depth: int):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = sorted(z.infolist(), key=lambda i: i.filename)
    except (zipfile.BadZipFile, OSError) as exc:
        raise Unscannable("bad_zip") from exc
    if sum(i.file_size for i in infos) > MAX_UNPACK:
        raise Unscannable("too_large")
    out = []
    for k, info in enumerate(infos):
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise Unscannable("encrypted")
        try:
            raw = z.read(info)
        except (RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            raise Unscannable("bad_zip_member") from exc
        out += [(f"!{k}{loc}", *rest) for loc, *rest in read_items(info.filename, raw, depth + 1, True)]
    return out


def _tar_items(data: bytes, depth: int):
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data))
        members = sorted((m for m in tf.getmembers() if m.isfile()), key=lambda m: m.name)
    except (tarfile.TarError, OSError) as exc:
        raise Unscannable("bad_tar") from exc
    if sum(m.size for m in members) > MAX_UNPACK:
        raise Unscannable("too_large")
    out = []
    for k, m in enumerate(members):
        raw = tf.extractfile(m).read()
        out += [(f"!{k}{loc}", *rest) for loc, *rest in read_items(m.name, raw, depth + 1, True)]
    return out


def byte_offset(text: str, char_off: int, enc: str) -> int:
    """Where a character offset of a decoded text sits in the file's bytes."""
    if enc in ("utf-8", "utf-8-sig"):
        return len(text[:char_off].encode("utf-8", "surrogateescape"))
    if enc.startswith("utf-16"):
        return 2 * char_off
    if enc.startswith("utf-32"):
        return 4 * char_off
    return char_off


def scan_named_bytes(index: LeakIndex, name: str, data: bytes, *, aggregate_json: bool = False,
                     ) -> tuple[list[tuple[str, str, int]], str | None]:
    """([(location suffix, kind, byte offset)], unscannable reason | None) for a file's bytes under ``name``."""
    try:
        items = read_items(name, data)
    except Unscannable as exc:
        return [], exc.reason
    hits: list[tuple[str, str, int]] = []
    for loc, text, enc, suffix, extra in items:
        is_agg = aggregate_json and suffix == ".json" and not loc
        for kind, off in index.scan(text, json_aggregate=is_agg, suffix=suffix, more_views=tuple(extra)):
            hits.append((loc, kind, byte_offset(text, off, enc)))
    if Path(name).suffix.lower() == ".svg" and not re.search(rb"<text[\s>]", data):
        hits.append(("", "unverifiable_svg", 0))
    return hits, None


# ------------------------------------------------------------------ git

def git(repo, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, check=True).stdout


def cat_blobs(repo, shas: list[str]) -> dict[str, bytes]:
    """{sha: content} through one ``git cat-file --batch``."""
    out: dict[str, bytes] = {}
    if not shas:
        return out
    raw = git(repo, "cat-file", "--batch", input=("\n".join(shas) + "\n").encode())
    pos = 0
    for sha in shas:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].split()
        if len(header) != 3:                       # "<sha> missing"
            out[sha] = b""
            pos = nl + 1
            continue
        size = int(header[2])
        out[sha] = raw[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return out
