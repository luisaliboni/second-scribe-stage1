"""Where each AI quote comes from in the transcript, and whether it may count in an arm.

A quote is located by searching ALL turns of the full transcript (never a filtered
view), so a quote that only exists in interviewer or mapping talk is still found and
can then be ruled out by arm rather than silently reported as missing.

Run 2: the search is ``textnorm.locate_quote_in_turns``, the tiered rule the reconstruction validator uses: an exact
quote sits inside one paragraph; an approximate one (>= 0.85 of its words, in order, inside one same-speaker turn,
guard words equal) is found too and says so in ``Provenance.tier``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .schemas import MapGraph, Transcript
from .textnorm import locate_quote_in_turns, token_seq, word_count


@dataclass
class Provenance:
    element_id: str            # node id, or "e:<source>-><target>"
    kind: str                  # "node" | "edge"
    found: bool
    speaker: str | None        # "participant" | "interviewer" | "unknown" (None if not found)
    turn_idx: int | None       # the chosen matching turn (see locate_quote)
    occurrences: int           # number of turns containing the quote
    span_words: int
    whole_turn: bool           # the quote's words equal the chosen turn's words
    mapping_talk_turn: bool    # the chosen matching turn is flagged mapping_talk
    tier: str = "exact"        # "exact" | "approximate" | "unfound" (found is False exactly when "unfound")
    # approximate tier only (R68): the TRANSCRIPT's own tokens of the matched span ("..." parts joined by " ... "), and
    # the idx of the turn holding the last matched word (``turn_idx`` is the turn holding the first)
    matched_text: str | None = None
    last_turn_idx: int | None = None


def locate_quote(quote: str | None, transcript: Transcript, element_id: str, kind: str) -> Provenance:
    """Exact tier: a turn matches iff ``contains_quote(turn.text, quote)`` (parts split on "...", in order, inside
    ONE turn). The chosen turn is the first matching participant turn if any turn by a participant matches, else
    the first matching turn; speaker, turn_idx, whole_turn and mapping_talk_turn all describe that one turn.
    Approximate tier (no exact match): the turn where the matched span starts; ``occurrences`` is the number of
    same-speaker turns that qualified, ``whole_turn`` is False, ``mapping_talk_turn`` is True if any paragraph of
    the matched span is flagged; ``matched_text`` is the transcript's own (normalised) words of the span and
    ``last_turn_idx`` the turn of its last word. An empty or None quote is never found."""
    span = word_count(quote) if quote else 0
    tier, info = locate_quote_in_turns(transcript.units("full"), quote) if quote else ("unfound", {})
    if tier == "unfound":
        return Provenance(element_id, kind, False, None, None, 0, span, False, False, "unfound")
    if tier == "approximate":
        chosen = transcript.turns[info["unit_start"]]
        flagged = any(t.mapping_talk for t in transcript.turns[info["unit_start"]:info["unit_end"] + 1])
        return Provenance(element_id, kind, True, chosen.speaker, chosen.idx, info["candidates"], span, False,
                          flagged, "approximate",
                          matched_text=" ... ".join(" ".join(part) for part in info["span_parts"]),
                          last_turn_idx=transcript.turns[info["unit_end"]].idx)
    matches = [transcript.turns[i] for i in info["turn_indices"]]
    chosen = next((t for t in matches if t.speaker == "participant"), matches[0])
    return Provenance(
        element_id=element_id, kind=kind, found=True, speaker=chosen.speaker,
        turn_idx=chosen.idx, occurrences=len(matches), span_words=span,
        whole_turn=token_seq(quote) == token_seq(chosen.text),
        mapping_talk_turn=chosen.mapping_talk, tier="exact",
    )


def eligible(p: Provenance, arm: str, min_words: int = 8) -> tuple[bool, str]:
    """May this quote count as support in ``arm``?  Returns (ok, reason).

    Order of checks: not_found, too_short (fewer than ``min_words`` words and not a whole
    turn), then for arm "removed" not_participant and mapping_talk. Arm "full" accepts any
    speaker and says "interviewer_voiced" when the interviewer is the one who said it."""
    if arm not in ("full", "removed"):
        raise ValueError(f"unknown arm {arm!r} (want 'full' or 'removed')")
    if not p.found:
        return False, "not_found"
    if p.span_words < min_words and not p.whole_turn:
        return False, "too_short"
    if arm == "removed":
        if p.speaker != "participant":
            return False, "not_participant"
        if p.mapping_talk_turn:
            return False, "mapping_talk"
        return True, "ok"
    return (True, "interviewer_voiced") if p.speaker == "interviewer" else (True, "ok")


def provenance_for(ai: MapGraph, transcript: Transcript) -> dict[str, Provenance]:
    """Provenance of every node quote (key: node_id) and edge quote (key: "e:<source>-><target>")."""
    out: dict[str, Provenance] = {}
    for n in ai.nodes:
        out[n.node_id] = locate_quote(n.quote, transcript, n.node_id, "node")
    for e in ai.edges:
        eid = f"e:{e.source}->{e.target}"
        out[eid] = locate_quote(e.quote, transcript, eid, "edge")
    return out
