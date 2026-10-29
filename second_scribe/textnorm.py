"""Text normalisation helpers shared by every task that compares text.

Nobody re-implements these: quote support checks, overlap scores and word counts
all go through this module so they agree with each other.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

STOPWORDS = frozenset("a an the and or of to in on for with at by from is was were be been are it this that "
                      "we our us i my me you your they their them he she his her so then just very".split())

_CHAR_MAP = str.maketrans({
    " ": " ", " ": " ", " ": " ",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-",
    "…": "...",
})
_ELLIPSIS = re.compile(r"\.{3,}")


def normalize(s: str) -> str:
    """NFKC; non-breaking spaces to space; curly quotes to straight; en/em dash to '-';
    ellipsis character to '...'; lowercase; collapse whitespace; strip."""
    s = unicodedata.normalize("NFKC", s).translate(_CHAR_MAP).lower()
    return " ".join(s.split())


def tokens(s: str) -> list[str]:
    """Words and numbers; leading/trailing apostrophes are stripped (they are quote marks),
    internal ones stay ("it's"), and a token that was only apostrophes is dropped."""
    return [t for t in (w.strip("'") for w in re.findall(r"[a-z0-9']+", normalize(s))) if t]


def stem(t: str) -> str:
    """Strip one suffix, trying "ing", "ed", "s" in that order, only if len(t) > 4.
    No "es" rule, so licence/licences stay equal."""
    if len(t) > 4:
        for suffix in ("ing", "ed", "s"):
            if t.endswith(suffix):
                return t[: -len(suffix)]
    return t


def content_tokens(s: str) -> set[str]:
    return {stem(t) for t in tokens(s) if t not in STOPWORDS}


def word_count(s: str) -> int:
    return len(tokens(s))


def token_seq(s: str) -> str:
    """Tokens joined by single spaces with a space at each end, so a substring
    search can only hit whole words."""
    return " " + " ".join(tokens(s)) + " "


def contains_quote(hay: str, quote: str) -> bool:
    """True iff every "..."-separated part of ``quote`` occurs in ``hay``, in order and
    without overlapping words. Punctuation and case never matter.

    A quote with no words at all (empty, or only ellipses) is never supported: False.
    """
    parts = [p for p in _ELLIPSIS.split(normalize(quote)) if tokens(p)]
    if not parts:
        return False
    hay_seq = token_seq(hay)
    pos = 0
    for part in parts:
        seq = token_seq(part)
        i = hay_seq.find(seq, pos)
        if i < 0:
            return False
        pos = i + len(seq) - 1   # the shared boundary space may open the next part
    return True


# ---------------------------------------------------------------------------------------------------------------
# Quote tiers (run 2, council round 8): exact | approximate | unfound
# ---------------------------------------------------------------------------------------------------------------
# One rule for every consumer: the reconstruction validator, Task-6 bucketing (provenance.locate_quote), the
# in-transcript check (match._quote_supports) and the audits' cross-check.
# in-order matched words / quote words, for the whole quote and for every "..." part. L16 (council round 10): 0.85,
# was 0.95. At 0.85 a quote under 7 words may differ by 0 words, 7-13 words by 1, 14-19 by 2, 20-26 by 3.
APPROX_MIN_RATIO = 0.85
APPROX_MAX_SPAN = 1.5        # matched span in the turn, in words, <= this x the quote's words
# Words that flip what a sentence claims. Their multiset must be identical in the quote and in the matched span,
# so a quote that drops the transcript's "not", or invents one, is never approximate. Every token ending in
# "n't" is a guard word too (tokens() keeps the internal apostrophe; curly apostrophes are normalised first).
GUARD_WORDS = frozenset({"not", "no", "never", "would", "wish", "wished", "could", "should", "can't", "cannot",
                         "won't", "didn't", "don't", "wasn't", "isn't", "nothing", "nobody",
                         # run-2 review S2: apostrophe-less contractions (no "n't" suffix) and other polarity words
                         "dont", "didnt", "doesnt", "cant", "wont", "wouldnt", "couldnt", "shouldnt", "isnt",
                         "wasnt", "arent", "werent", "havent", "hasnt", "hadnt", "aint", "nor", "neither", "none",
                         "nowhere", "without", "hardly", "barely", "scarcely", "rarely", "seldom", "might", "may",
                         "must",
                         # L16 (council round 10): "I need" can describe what happened, "I want" is a wish; a
                         # condition ("if", "unless") cannot be dropped or invented either
                         "want", "wanted", "wants", "need", "needed", "needs", "hope", "hoped", "hopes", "if",
                         "unless", "wishes", "wishing", "supposed"})
GUARD_SUFFIX = "n't"

QUOTE_RULE_VERSION = "run2-quote-rule-v2"   # bump on ANY change to the matching code (a run records it in run_start)


def quote_rule_fingerprint() -> dict:
    """Everything that decides whether a quote is exact / approximate / unfound, as plain values: the rule's version
    string, the two thresholds, the guard list (sorted) and the contraction suffix. Read from the module globals at
    call time, so an edit between two sessions of a run changes it."""
    return {"version": QUOTE_RULE_VERSION, "approx_min_ratio": APPROX_MIN_RATIO, "approx_max_span": APPROX_MAX_SPAN,
            "guard_words": sorted(GUARD_WORDS), "guard_suffix": GUARD_SUFFIX}


def quote_rule_sha256() -> str:
    """SHA-256 of ``quote_rule_fingerprint()`` (canonical JSON). ``run_start`` records it; a real run refuses to resume
    under another one."""
    return hashlib.sha256(json.dumps(quote_rule_fingerprint(), sort_keys=True).encode("utf-8")).hexdigest()


_MARKER_LINE = re.compile(r"^\[([IP?])\](?: |$)")
_MERGEABLE = ("I", "P")      # consecutive lines with one of these markers form ONE turn; "?" lines never merge


def _is_guard(t: str) -> bool:
    return t in GUARD_WORDS or t.endswith(GUARD_SUFFIX)


def _guards(toks) -> Counter:
    return Counter(t for t in toks if _is_guard(t))


def transcript_units(transcript_text: str) -> list[tuple[str | None, str]]:
    """The rendered transcript (``Transcript.text``) as [(marker, text)], one unit per line that starts with
    "[I] ", "[P] " or "[?] "; a line without a marker continues the unit before it. Lines before the first
    marker form a unit with marker None."""
    units: list[list] = []
    for ln in transcript_text.split("\n"):
        m = _MARKER_LINE.match(ln)
        if m:
            units.append([m.group(1), ln[m.end():]])
        elif units:
            units[-1][1] += "\n" + ln
        else:
            units.append([None, ln])
    return [(a, b) for a, b in units]


def _runs(units):
    """Group units into speaker turns: consecutive units with the same I/P marker are one run. Returns
    [(marker, [unit indices], tokens, token offset of each unit in the run)]."""
    runs: list[list] = []
    prev = None
    for i, (marker, text) in enumerate(units):
        if marker in _MERGEABLE and runs and prev == marker:
            runs[-1][1].append(i)
            runs[-1][3].append(len(runs[-1][2]))
            runs[-1][2].extend(tokens(text))
        else:
            runs.append([marker, [i], tokens(text), [0]])
        prev = marker
    return runs


def _align_part(run_toks: list[str], part: list[str], lo: int):
    """Best in-order alignment of ``part`` inside ``run_toks[lo:]``: (matched, start, end, unlimited) with end
    exclusive, in ``run_toks`` coordinates (``unlimited``: words matched when the span is not limited), or None
    when no word matches at all. Picks the cluster of matching blocks that matches the most words while
    spanning at most APPROX_MAX_SPAN x len(part) words, so a stray early match of one common word cannot widen the
    span (nor a stray match add to the count)."""
    sm = SequenceMatcher(None, run_toks[lo:], part, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    limit = APPROX_MAX_SPAN * len(part)
    unlimited = sum(b.size for b in blocks)
    best = None
    for i in range(len(blocks)):
        matched = 0
        for j in range(i, len(blocks)):
            span = blocks[j].a + blocks[j].size - blocks[i].a
            if span > limit:
                break
            matched += blocks[j].size
            key = (matched, -span, -blocks[i].a)
            if best is None or key > best[0]:
                best = (key, matched, lo + blocks[i].a, lo + blocks[j].a + blocks[j].size)
    return None if best is None else (*best[1:], unlimited)


def _approximate(units, quote: str):
    """('approximate', info) or ('unfound', info) for the same-speaker-turn rule; see ``locate_quote_tiered``."""
    parts = [tokens(p) for p in _ELLIPSIS.split(normalize(quote))]
    parts = [p for p in parts if p]
    if not parts:
        return "unfound", {"reason": "empty_quote"}
    if not any(m is not None for m, _ in units):
        return "unfound", {"reason": "no_markers"}
    qtoks = [t for p in parts for t in p]
    n = len(qtoks)
    need = Counter(qtoks)
    reasons: set[str] = set()
    good = []                                    # (ratio, -span, run index, unit_start, unit_end, matched, span, marker)
    for ri, (marker, idxs, rtoks, offs) in enumerate(_runs(units)):
        if marker is None or len(rtoks) == 0:
            continue
        have = Counter(rtoks)
        if sum(min(c, have[t]) for t, c in need.items()) < APPROX_MIN_RATIO * n:
            continue                              # cheap necessary condition: too few of the quote's words occur here
        matched = span = 0
        lo = 0
        first = last = None
        covered: list[str] = []
        spans: list[list[str]] = []              # the run's own tokens of each part's matched span
        ok = True
        for p in parts:
            hit = _align_part(rtoks, p, lo)
            if hit is None or hit[0] / len(p) < APPROX_MIN_RATIO:
                ok = False
                if hit is not None and hit[3] / len(p) >= APPROX_MIN_RATIO:
                    reasons.add("span")            # the words are there, but too spread out
                break
            m, a, b, _ = hit
            matched += m
            span += b - a
            covered += rtoks[a:b]
            spans.append(rtoks[a:b])
            first = a if first is None else first
            last, lo = b, b
        if not ok:
            continue
        if matched / n < APPROX_MIN_RATIO or span > APPROX_MAX_SPAN * n:    # cannot happen once every part passed
            continue
        if _guards(qtoks) != _guards(covered):
            reasons.add("guard")
            continue
        us = max(k for k, o in enumerate(offs) if o <= first)
        ue = max(k for k, o in enumerate(offs) if o <= last - 1)
        good.append((matched / n, -span, ri, idxs[us], idxs[ue], matched, span, marker, first, last, spans))
    if not good:
        reason = "guard" if "guard" in reasons else "span" if "span" in reasons else "below_threshold"
        return "unfound", {"reason": reason}
    # a participant turn beats an interviewer turn before ratio / span decide (the exact path does the same):
    # an interviewer's echo of the answer is usually shorter, and must not win the attribution
    ratio, negspan, _ri, u0, u1, matched, span, _marker, first, last, spans = max(
        good, key=lambda g: (g[7] == "P", g[0], g[1], -g[2]))
    return "approximate", {"ratio": ratio, "matched_tokens": matched, "quote_tokens": n, "span_tokens": span,
                           "unit_start": u0, "unit_end": u1, "candidates": len(good),
                           # R68: where the match sits, so the judge can be shown the transcript's own words. Token
                           # offsets within the speaker turn (end exclusive) and the turn's tokens of each "..." part.
                           # Fields only; no tier decision reads them.
                           "span_start_token": first, "span_end_token": last, "span_parts": spans}


def locate_quote_tiered(transcript_text: str, quote: str) -> tuple[str, dict]:
    """Which tier a quote reaches in a rendered transcript (lines "[I] ..." / "[P] ...", as sent to the model).

    The rendered text is cut into its turns (``transcript_units``) and ``locate_quote_in_turns`` decides, so the
    validator, the audits, Task-6 bucketing and the in-transcript check cannot disagree (run-2 review B1):

    ("exact", info)      ONE turn (one "[I]"/"[P]"/"[?]" paragraph, its marker not part of the text) contains the
                         quote by ``contains_quote``; every "..." part lies in that same turn. A quote never
                         bridges a speaker change or two paragraphs, and the marker tokens never match words.
    ("approximate", info) not exact, and ONE speaker turn (consecutive lines with the same I/P marker; a line
                         without a marker continues the line before; "[?]" lines are single-line turns) holds the
                         quote's words in order with ratio >= APPROX_MIN_RATIO (0.85; difflib, ``tokens()``
                         normalisation) inside a span of at most APPROX_MAX_SPAN x the quote's words; every
                         "..." part lies in that turn, in order, with the same ratio on its own; the guard
                         words (GUARD_WORDS, "...n't") are identical in the quote and the matched span.
                         info: ratio, matched_tokens, quote_tokens, span_tokens (parts' spans summed, gaps left
                         out by "..." not counted), unit_start / unit_end (indices of the lines holding the
                         first and last matched word), candidates (turns that qualified; a participant turn is
                         preferred over an interviewer turn, then the best ratio / shortest span is taken),
                         span_start_token / span_end_token (offsets of the matched span in the turn's tokens, end
                         exclusive) and span_parts (the turn's own tokens of the span, one list per "..." part).
    ("unfound", {"reason": ...})  reason: empty_quote | no_markers (text without markers has no approximate tier)
                         | below_threshold | span | guard.
    """
    return locate_quote_in_turns(transcript_units(transcript_text), quote)


def locate_quote_in_turns(turns, quote: str) -> tuple[str, dict]:
    """Same tiers for a transcript given as turns, ``[(marker, text)]`` with marker "I", "P" or "?" (what
    ``Transcript.units`` returns): the exact tier is ONE turn containing the quote (``contains_quote`` on that turn's
    text, info ``turn_indices`` lists every such turn); the approximate and unfound tiers are exactly those of
    ``locate_quote_tiered``. Task-6 bucketing and the in-transcript check use this."""
    hits = [i for i, (_m, text) in enumerate(turns) if contains_quote(text, quote)]
    if hits:
        return "exact", {"turn_indices": hits}
    return _approximate(list(turns), quote)
