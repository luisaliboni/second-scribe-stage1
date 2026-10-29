"""Transcript parsing and mapping-talk flags.

Interviews arrive as Word files whose speaker labelling varies: ``[XY] text``
(bracket), ``XY 0:05 text`` (first token) or no labels at all. ``parse_paragraphs``
detects which, and marks each turn participant / interviewer / unknown.
``flag_mapping_talk`` marks interviewer turns that are about drawing the map
itself (rule-based); ``confirm_mapping_talk`` asks a model to confirm each
rule-flagged turn, and a turn stays flagged only if both agree.
"""
from __future__ import annotations

import json
import re
import zipfile
import zlib
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .llm import derive_seed, log_call
from .prompts import DEFAULT_PROMPT_VERSION, helper_messages, load_helper_prompt
from .schemas import Transcript, Turn

MAPPING_PATTERNS: tuple[str, ...] = (
    r"\bmap\b", r"\bmapp?ing\b", r"\bdraw(ing|n)?\b", r"\bbox(es)?\b", r"\barrow(s)?\b",
    r"\bin red\b", r"\bred\b.*\b(ideal|colou?r)\b", r"\b(put|add|write|place|move)\b.{0,30}\b(here|there|down|in|on)\b",
    r"\bsticky\b", r"\bscreen\b", r"\bshare (my|the) screen\b", r"\blegend\b", r"\bvisio\b",
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BRACKET = re.compile(r"^\[([^\]]{1,40})\]\s*")
_TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*")
_BARE_TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
# First tokens too ordinary to be speaker labels (controller ruling), plus backchannel and question words
# (an anchored file loosens one partner label, which must not be "Right" or "What").
_COMMON_WORDS = frozenset(
    "we it i so and then yes yeah no okay ok um uh well the but oh they he she you that this my a "
    "right thanks thank mm mmhmm hmm mhm sure great good alright exactly true indeed cool wow yep nope hi hello "
    "what how did tell can could would do does is are was were why when where who which please let lets".split())
# Bracketed stage directions: never speakers, even when repeated, unless named in a label set.
_ANNOTATIONS = frozenset("inaudible unintelligible indiscernible indistinct unclear laughs laughter laughing "
                         "crosstalk overlapping pause silence sighs sigh phone noise background music "
                         "cough".split())
_BATCH = 20
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class TranscriptRefused(ValueError):
    """A transcript file the kit will not read. The message never quotes the file's text or names the file."""


TRANSCRIPT_SUFFIXES = (".docx", ".txt")
MAX_TRANSCRIPT_BYTES = 20 * 1024 * 1024
_MAGIC = ((b"%PDF", "a PDF"), (b"\x89PNG", "a PNG image"), (b"\xff\xd8\xff", "a JPEG image"), (b"GIF8", "a GIF image"),
          (b"{\\rtf", "an RTF file"), (b"\xd0\xcf\x11\xe0", "an old Word .doc file"), (b"PK", "a damaged or truncated zip"))


def sniff_kind(path) -> str | None:
    """What the first bytes of a file say it is ("a PDF", "a PNG image", ...), or None when they say nothing known."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    return next((kind for magic, kind in _MAGIC if head.startswith(magic)), None)


MAX_DOCX_MEMBERS = 10000                       # parts in the zip (the same cap as a Visio file)
MAX_DOCX_XML_BYTES = 200 * 1024 * 1024         # all XML parts together, unpacked
_DTD = re.compile(rb"<!\s*(?:doctype|entity)")   # as the draw.io and xlsx readers: also "<! ENTITY"


def check_docx(path) -> None:
    """Refuse, before any XML is parsed, a ``.docx`` that is too large, has too many or too large parts, lists a part
    twice, or declares a DOCTYPE or ENTITY in any XML part. The refusal is a ``ValueError`` that never quotes the file."""
    try:
        if Path(path).stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise TranscriptRefused(f"transcript is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB")
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_DOCX_MEMBERS:
                raise TranscriptRefused(f"the transcript has more than {MAX_DOCX_MEMBERS} parts")
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):
                raise TranscriptRefused("the transcript lists the same part twice")
            if "word/document.xml" not in names:
                raise TranscriptRefused("the file is not a readable .docx transcript (no word/document.xml inside)")
            total = 0
            for info in infos:
                if not info.filename.lower().endswith((".xml", ".rels")):
                    continue
                if info.file_size > MAX_TRANSCRIPT_BYTES:
                    raise TranscriptRefused("a part of the transcript is too large")
                with z.open(info) as f:
                    data = f.read(MAX_TRANSCRIPT_BYTES + 1)           # the cap counts bytes actually unpacked
                if len(data) > MAX_TRANSCRIPT_BYTES:
                    raise TranscriptRefused("a part of the transcript is too large")
                total += len(data)
                if total > MAX_DOCX_XML_BYTES:
                    raise TranscriptRefused("the transcript is too large when unpacked")
                if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                    data = data.decode("utf-16", errors="replace").encode("utf-8", errors="replace")
                else:
                    data = data.replace(b"\x00", b"")
                if _DTD.search(data.lower()):
                    raise TranscriptRefused("a part of the transcript declares a DOCTYPE or ENTITY, which is not allowed")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError,
            zlib.error):
        raise TranscriptRefused(f"not a readable .docx transcript ({sniff_kind(path) or 'not a zip file'})") from None


MAX_PARAGRAPH_NESTING = 32          # w:p inside w:p inside ...; an ordinary Word file, text boxes included, nests 1-3


def _paragraph_nesting(root, limit: int) -> int:
    """The deepest chain of ``w:p`` elements inside one another (a text box holds paragraphs, so 2 or 3 is ordinary),
    counted without recursion; stops as soon as it passes ``limit``. The extraction loop visits each paragraph's whole
    subtree, so nested paragraphs cost time in proportion to the square of their depth: a few KB of crafted XML could
    keep a reader busy for minutes."""
    deepest = 0
    stack = [(root, 0)]
    tag = _W + "p"
    while stack:
        el, depth = stack.pop()
        if el.tag == tag:
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > limit:
                    return deepest
        stack.extend((child, depth) for child in el)
    return deepest


def read_docx_text(path) -> list[str]:
    """One string per paragraph (w:p) of word/document.xml, runs joined; stdlib only. A file that is not a readable
    Word document (empty, not a zip, a damaged zip, no word/document.xml, that part over ``MAX_DOCX_XML_BYTES``
    uncompressed, not well-formed XML, or paragraphs nested more than ``MAX_PARAGRAPH_NESTING`` deep) raises
    ``TranscriptRefused``."""
    p = Path(path)
    if p.stat().st_size == 0:
        raise TranscriptRefused("the file is empty (0 bytes)")
    check_docx(p)
    try:
        with zipfile.ZipFile(p) as z:
            try:
                info = z.getinfo("word/document.xml")
            except KeyError:
                raise TranscriptRefused("not a Word document (no word/document.xml inside)") from None
            if info.file_size > MAX_TRANSCRIPT_BYTES:
                raise TranscriptRefused(f"word/document.xml is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB "
                                        "uncompressed")
            data = z.read(info)
    except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError, RuntimeError):
        raise TranscriptRefused(f"not a .docx ({sniff_kind(p) or 'not a zip file'})") from None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise TranscriptRefused("word/document.xml is not well-formed XML (a damaged file)") from None
    if _paragraph_nesting(root, MAX_PARAGRAPH_NESTING) > MAX_PARAGRAPH_NESTING:
        raise TranscriptRefused(f"the document nests paragraphs more than {MAX_PARAGRAPH_NESTING} levels deep, which "
                                "Word does not produce (a damaged or crafted file)")
    out: list[str] = []
    for p in root.iter(_W + "p"):
        parts: list[str] = []
        for el in p.iter():
            if el.tag == _W + "t":
                parts.append(el.text or "")
            elif el.tag in (_W + "tab", _W + "br"):
                parts.append(" ")
        out.append("".join(parts))
    return out


def read_txt_paras(path) -> list[str]:
    """One string per line of a UTF-8 text file (a leading BOM is dropped; any line ending). The text itself is
    kept as written: non-breaking spaces and curly quotes are folded later, where quotes are compared."""
    p = Path(path)
    if p.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise TranscriptRefused(f"transcript is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB")
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        raise TranscriptRefused("transcript is not UTF-8 text" + ("; it is UTF-16, save it as UTF-8" if utf16 else
                                                                "")) from None
    return text.splitlines()


def read_transcript_paras(path) -> list[str]:
    """The paragraphs of a transcript file, by suffix: ``.docx`` -> ``read_docx_text``, ``.txt`` -> ``read_txt_paras``."""
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return read_docx_text(path)
    if suffix == ".txt":
        return read_txt_paras(path)
    raise TranscriptRefused(f"unsupported transcript format {suffix or '(no extension)'!r} "
                     f"(want one of {', '.join(TRANSCRIPT_SUFFIXES)})")


def label_candidates(paras: list[str], top: int = 6) -> list[tuple[str, int]]:
    """Speaker labels Setup can offer: each paragraph's bracket label (normalised, as the kit matches it) or else its
    first token with punctuation stripped (case kept), counted, most frequent first (ties: first seen first)."""
    counts: Counter = Counter()
    for p in paras:
        p = (p or "").strip()
        if not p:
            continue
        lab = _bracket_label(p)
        if lab is None:
            lab = _first_raw(p)
        if lab:
            counts[lab] += 1
    return counts.most_common(top)


def norm_label(s: str) -> str:
    return _norm_label(s)


def _norm_label(s: str) -> str:
    """Strip punctuation and whitespace, lowercase."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


def _first_raw(para: str) -> str | None:
    """First token with punctuation stripped, case kept; None for an empty token or a paragraph
    that opens with a bracket (a bracketed token is never a first-token label)."""
    if para.startswith("["):
        return None
    parts = para.split(None, 1)
    raw = re.sub(r"[\W_]+", "", parts[0], flags=re.UNICODE) if parts else ""
    return raw or None


def _strict_shape(raw: str) -> bool:
    """1-4 letters, UPPERCASE (as initials are), and not a stage direction."""
    return raw.isalpha() and len(raw) <= 4 and raw.isupper() and raw.lower() not in _ANNOTATIONS


def _bracket_label(para: str) -> str | None:
    """Normalised label of a leading ``[...]``; None for no bracket, a bare timestamp, or
    content with no letters/digits (``[...]``)."""
    m = _BRACKET.match(para)
    if not m or _BARE_TIMESTAMP.match(m.group(1).strip()):
        return None
    return _norm_label(m.group(1)) or None


def _detect_format(paras: list[str], known: frozenset[str] = frozenset()) -> tuple[str, dict[str, str]]:
    """Return (format, {normalised label: its observed casing}); the dict is empty unless first_token.

    First-token labels, all counted in their observed casing only:
    - strict: UPPERCASE 1-4 letters (initials), or a known label (``known``);
    - partner: if a known label opens >=2 paragraphs, the file is anchored and exactly ONE more label
      is loosened, the most frequent first token that is not a known label, provided it is 2-12 letters,
      not a common word, not a stage direction and not bracketed (a role word or first name such as
      "Mara" is not uppercase initials). Everything else keeps the strict shape rule.
    """
    n = len(paras)
    if n == 0:
        return "none", {}
    if sum(1 for p in paras if _bracket_label(p) is not None) / n >= 0.30:
        return "bracket", {}
    forms: dict[str, Counter] = {}
    for p in paras:
        raw = _first_raw(p)
        if raw:
            forms.setdefault(raw.lower(), Counter())[raw] += 1
    cand: dict[str, tuple[str, int]] = {}                # norm -> (casing, count in that casing)
    for norm, fc in forms.items():
        if norm in known:
            raw, c = fc.most_common(1)[0]
            cand[norm] = (raw, c)
        else:
            raw, c = fc.most_common(1)[0]
            if _strict_shape(raw):                       # its dominant casing is initials-like
                cand[norm] = (raw, c)
    if any(c >= 2 for norm, (_, c) in cand.items() if norm in known):
        rest = [(sum(fc.values()), norm) for norm, fc in forms.items() if norm not in known]
        if rest:
            _, norm = max(rest, key=lambda x: x[0])      # first maximal in insertion order
            raw, c = forms[norm].most_common(1)[0]
            if (norm not in cand and raw.isalpha() and 2 <= len(raw) <= 12
                    and norm not in _COMMON_WORDS and norm not in _ANNOTATIONS):
                cand[norm] = (raw, c)
    top = sorted(cand.items(), key=lambda kv: -kv[1][1])[:4]
    if len(top) >= 2:
        (t1, (_, c1)), (t2, (_, c2)) = top[0], top[1]
        if (c1 >= 2 and c2 >= 2 and (c1 + c2) / n >= 0.60
                and t1 not in _COMMON_WORDS and t2 not in _COMMON_WORDS
                and sum(c for _, (_, c) in top) / n >= 0.80):
            labels = {t: raw for t, (raw, c) in top if c >= 2 and t not in _COMMON_WORDS}
            labels.update({t: raw for t, (raw, _) in cand.items() if t in known})
            return "first_token", labels
    return "none", {}


def parse_paragraphs(paras: list[str], participant_labels: set[str], transcript_id: str,
                     interviewer: str | None = None, fallback: str = "word_share",
                     interviewer_labels: set[str] | None = None, label_counts: Counter | None = None) -> Transcript:
    """Detect the speaker format and build turns.

    A label (bracket content or first token, punctuation stripped, lowercased) found in
    ``participant_labels`` marks a participant turn; any other recognised label marks an
    interviewer turn. Paragraphs without a label continue the previous turn (a leading
    unlabelled paragraph is its own "unknown" turn). Format "none": one "unknown" turn
    per paragraph.

    Two situations are settled without ``participant_labels`` (``speaker_inference`` records which):
    - one label opens >=90% of the labelled paragraphs: speakers cannot be told apart, so every
      turn is "unknown", ``speaker_format`` becomes "none", inference "ambiguous_labels";
    - no recognised label is in ``participant_labels`` and ``fallback == "word_share"``: the
      participant is the wordier of the 2 labels with the most turns, inference "word_share".
      ``fallback="none"`` keeps the plain label rule (all labelled turns become interviewer).

    ``interviewer_labels`` (optional): a label present in both ``participant_labels`` and
    ``interviewer_labels`` cannot identify the participant (same initials), so it is removed from
    ``participant_labels`` for this file; if nothing is left, the word-share fallback decides.

    ``label_counts`` (optional): a Counter that gets {normalised label: turns it opened}, so a caller can see the
    labels in neither set (read as interviewer turns: the two-party assumption). Nothing else changes.
    """
    if fallback not in ("word_share", "none"):
        raise ValueError(f"unknown fallback {fallback!r} (want 'word_share' or 'none')")
    paras = [p.strip() for p in paras if p and p.strip()]
    ilabels = {_norm_label(x) for x in (interviewer_labels or ())}
    all_plabels = {_norm_label(x) for x in participant_labels}
    plabels = all_plabels - ilabels
    known = frozenset(all_plabels | ilabels)
    fmt, token_forms = _detect_format(paras, known)
    turns: list[Turn] = []
    labels: list[str | None] = []      # label that opened each turn (None: unlabelled)

    def add(label: str | None, text: str) -> None:
        turns.append(Turn(idx=len(turns), speaker="unknown", text=text))
        labels.append(label)

    if fmt == "none":
        for p in paras:
            add(None, p)
        return Transcript(transcript_id, turns, fmt, interviewer)

    bracket_uses = Counter(_bracket_label(p) for p in paras) if fmt == "bracket" else Counter()
    for p in paras:
        label, text = None, p
        if fmt == "bracket":
            label = _bracket_label(p)
            # A bracket is a speaker only if it opens >=2 turns (and is not a stage direction),
            # or is a known label; otherwise the paragraph continues the previous turn, bracket kept.
            if label is not None and not (label in known or (bracket_uses[label] >= 2
                                                              and label not in _ANNOTATIONS)):
                label = None
            if label is not None:
                text = _BRACKET.sub("", p, count=1)
        else:
            raw = _first_raw(p)
            # case-sensitive: a label "AM" does not split "Am I ..."; a partner keeps its observed casing
            if raw is not None and token_forms.get(raw.lower()) == raw:
                label = raw.lower()
                rest = p.split(None, 1)[1] if len(p.split(None, 1)) > 1 else ""
                text = _TIMESTAMP.sub("", rest.lstrip(), count=1)
        if label is None:
            if turns:
                turns[-1].text = f"{turns[-1].text} {p}".strip()
            else:
                add(None, p)
            continue
        add(label, text.strip())

    opened = Counter(l for l in labels if l is not None)
    if label_counts is not None:
        label_counts.update(opened)
    if not opened:     # nothing survived as a speaker label: same as format "none"
        return Transcript(transcript_id, [Turn(i, "unknown", p) for i, p in enumerate(paras)], "none", interviewer)
    if max(opened.values()) / sum(opened.values()) >= 0.90:
        return Transcript(transcript_id, turns, "none", interviewer, "ambiguous_labels")

    inference = "label"
    participant = set(plabels)
    if fallback == "word_share" and opened and not (set(opened) & plabels):
        top2 = [l for l, _ in opened.most_common(2)]
        words = Counter()
        for l, tn in zip(labels, turns):
            if l in top2:
                words[l] += len(tn.text.split())
        participant = {max(top2, key=lambda l: (words[l], -top2.index(l)))}
        inference = "word_share"
    for l, tn in zip(labels, turns):
        tn.speaker = "unknown" if l is None else ("participant" if l in participant else "interviewer")
    return Transcript(transcript_id, turns, fmt, interviewer, inference)


def label_lookalikes(paras: list[str], participant_labels: set[str], transcript_id: str = "",
                     interviewer_labels: set[str] | None = None) -> Counter:
    """Lines that look like speaker labels the parser does not read as ones, {the label as written: lines}. For a
    transcript read as ``first_token`` only, and only a report: nothing here changes how turns are parsed.

    - the same label in another letter case than the file's own (``PARTICIPANT:`` among ``Participant:`` lines): the
      parser keeps each label's observed casing, so the line is joined to the previous turn;
    - a space before the colon (``Participant : text``): read as a label, but the turn text starts with a stray ``:``
      (counted as ``<label> :``).
    A line counts only when what follows the first word is a colon or a timestamp, so a sentence that happens to open
    with the same word in another case (``Am I ...`` under the label ``AM``) is not reported."""
    paras = [p.strip() for p in paras if p and p.strip()]
    ilabels = {_norm_label(x) for x in (interviewer_labels or ())}
    known = frozenset({_norm_label(x) for x in participant_labels} | ilabels)
    if parse_paragraphs(paras, set(participant_labels), transcript_id, interviewer_labels=set(interviewer_labels or ()),
                        ).speaker_format != "first_token":
        return Counter()
    _fmt, forms = _detect_format(paras, known)
    found: Counter = Counter()
    for p in paras:
        raw = _first_raw(p)
        if raw is None or raw.lower() not in forms:
            continue
        parts = p.split(None, 1)
        word, rest = parts[0], (parts[1].lstrip() if len(parts) > 1 else "")
        if forms[raw.lower()] == raw:
            if rest.startswith(":") and not word.endswith(":"):
                found[f"{raw} :"] += 1
        elif word.endswith(":") or rest.startswith(":") or _TIMESTAMP.match(rest):
            found[raw] += 1
    return found


def parse_synthetic(text: str, transcript_id: str, interviewer: str | None = None) -> Transcript:
    """Lines ``[P] ...`` / ``[I] ...``; other non-empty lines continue the previous turn."""
    turns: list[Turn] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[([PI])\]\s*(.*)$", line)
        if m:
            speaker = "participant" if m.group(1) == "P" else "interviewer"
            turns.append(Turn(idx=len(turns), speaker=speaker, text=m.group(2)))
        elif turns:
            turns[-1].text = f"{turns[-1].text} {line}"
        else:
            turns.append(Turn(idx=0, speaker="unknown", text=line))
    return Transcript(transcript_id, turns, "synthetic", interviewer)


def flag_mapping_talk(t: Transcript, patterns=MAPPING_PATTERNS) -> Transcript:
    """Copy of ``t`` with ``mapping_talk`` set on interviewer turns matching any pattern."""
    rx = [re.compile(p, re.IGNORECASE) for p in patterns]
    turns = [replace(x, mapping_talk=(x.speaker == "interviewer" and any(r.search(x.text) for r in rx)))
             for x in t.turns]
    return replace(t, turns=turns)


def _parse_answers(text: str, n_turns: int) -> dict[int, bool] | str:
    """{n: is_map_talk} for every n in 1..n_turns, or an error message."""
    body = text.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    try:
        data = json.loads(body)
        out = {}
        for a in data["answers"]:
            n = a["n"]
            if not isinstance(n, int) or isinstance(n, bool):
                return "turn number must be an integer"
            if not 1 <= n <= n_turns:
                return f"turn number is outside 1..{n_turns}"
            if n in out:
                return f"turn {n} appears more than once"
            v = str(a["map_talk"]).strip().lower()
            if v not in ("yes", "no"):
                return f"map_talk must be \"yes\" or \"no\", got {v!r}"
            out[n] = v == "yes"
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"not the requested JSON ({type(exc).__name__})"
    missing = [n for n in range(1, n_turns + 1) if n not in out]
    return f"missing answers for turns {missing}" if missing else out


def confirm_mapping_talk(t: Transcript, log, client, *, item_id: str, prompts_dir=None,
                         prompt_version: str = DEFAULT_PROMPT_VERSION,
                         stage: str = "confirm_mapping_talk", screen_all: bool = True) -> tuple[Transcript, dict]:
    """Ask the model, in batches of at most 20, whether an interviewer turn is map talk.

    ``screen_all`` True (default): the model screens EVERY interviewer turn, not only the ones the regex flagged, so
    a map-talk turn the patterns missed is still found (and a regex hit the model rejects is dropped). The final flag
    is the model's answer. A batch the model never answers validly (after ``log_call``'s retries) keeps the RULE flag
    for its turns (a floor: the regex hits stay removed) and counts them in ``unscreened_turns``; the call_error
    records show it happened. Stats: rule_flagged, model_flagged, model_confirmed (both), rule_only, model_only,
    disagreements (rule_only + model_only), n_interviewer_turns, n_screened, unscreened_turns and ``agreement``
    (share of screened turns on which rule and model agree; None when nothing was screened).

    ``screen_all`` False: the earlier rule: only rule-flagged turns are asked, final flag = rule AND model, a batch
    never answered leaves its turns unflagged; stats are {"rule_flagged", "model_confirmed", "disagreements"}.

    ``prompt_version`` "v2" (default) sends a system message and the sandwich-layout prompt from ``prompts_dir``
    (default: the kit's ``prompts/``); "v1" reproduces the earlier single user message.
    Returns (new transcript, stats).
    """
    prompts_dir = _PROMPTS_DIR if prompts_dir is None else prompts_dir
    template = load_helper_prompt(prompts_dir, "confirm_mapping_talk", prompt_version)
    flagged = [x for x in t.turns if x.mapping_talk]
    asked = [x for x in t.turns if x.speaker == "interviewer"] if screen_all else flagged
    answered: dict[int, bool] = {}              # turn idx -> the model's yes/no, for turns in a batch that got a valid answer
    for b in range(0, len(asked), _BATCH):
        batch = asked[b:b + _BATCH]
        listing = "\n".join(f"{k}. {' '.join(x.text.split())}" for k, x in enumerate(batch, 1))
        prompt = template.replace("{turns}", listing)   # not .format: turn text may hold braces
        parsed: dict[int, bool] = {}

        def validate(text: str, n=len(batch)) -> str | None:
            res = _parse_answers(text, n)
            if isinstance(res, str):
                return res
            parsed.clear()
            parsed.update(res)
            return None
        text, _ = log_call(log, client, helper_messages(prompts_dir, prompt, prompt_version),
                           stage=stage, item_id=f"{item_id}:{b // _BATCH}",
                           temperature=0.0, seed=derive_seed("confirm_mapping_talk", item_id, b // _BATCH),
                           max_tokens=60 + 25 * len(batch), validate=validate)
        if text is None:
            continue
        answered.update({x.idx: bool(parsed.get(k)) for k, x in enumerate(batch, 1)})
    if not screen_all:
        confirmed = {i for i, yes in answered.items() if yes}
        turns = [replace(x, mapping_talk=x.mapping_talk and x.idx in confirmed) for x in t.turns]
        return replace(t, turns=turns), {"rule_flagged": len(flagged), "model_confirmed": len(confirmed),
                                         "disagreements": len(flagged) - len(confirmed)}
    turns = [replace(x, mapping_talk=answered[x.idx] if x.idx in answered else x.mapping_talk) for x in t.turns]
    both = sum(1 for i, yes in answered.items() if yes and t.turns[i].mapping_talk)
    rule_only = sum(1 for i, yes in answered.items() if not yes and t.turns[i].mapping_talk)
    model_only = sum(1 for i, yes in answered.items() if yes and not t.turns[i].mapping_talk)
    return replace(t, turns=turns), {
        "rule_flagged": len(flagged), "model_flagged": both + model_only, "model_confirmed": both,
        "rule_only": rule_only, "model_only": model_only, "disagreements": rule_only + model_only,
        "n_interviewer_turns": len(asked), "n_screened": len(answered), "unscreened_turns": len(asked) - len(answered),
        "agreement": (1 - (rule_only + model_only) / len(answered)) if answered else None}
