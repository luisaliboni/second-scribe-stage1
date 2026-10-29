"""Matching of AI maps against interviewer-drawn, participant-endorsed maps.

Maps describe the same thing at different grain and in different words ("GP" vs "family
doctor"), so matching is set-based (a reference node counts as recovered if any node of the
other map denotes it) and uses ONE batched alignment call per map pair to a judge model, with
lexical overlap as a floor. ``match_nodes`` is a pure function of the judge output, so the
call is made once and reused across every threshold in ``SETTINGS_GRID``.

Also here: off-record detection (``in_transcript``), plain-concept labels (``canonicalize``)
and a deterministic clustering of labels.

Failure policy: a judge call that never returns a valid answer (after ``log_call``'s retries;
the audit log holds the call_error records) contributes nothing, so the lexical floor alone decides.
Each such failed call also appends a ``judge_fallback`` record ``{"stage", "item_id", "n_items"}`` to the
audit log, so auditors can count them (``item_id`` is the one passed to ``log_call``, chunk suffix included).

Quote checks use ``textnorm.locate_quote_in_turns`` (the tiered rule, the same function ``provenance.locate_quote`` calls).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .llm import derive_seed, log_call
from .prompts import DEFAULT_PROMPT_VERSION, IN_TRANSCRIPT_PROMPT_VERSION, helper_messages, load_helper_prompt
from .schemas import Edge, MapGraph, Transcript
from .textnorm import content_tokens, locate_quote_in_turns, normalize, word_count

_CHUNK = 60
_FRACTION = 0.6        # share of a label's content tokens that must sit in one turn (lexical fallback)
# A judge quote is non-trivial if it has >= 3 content tokens and every "..." piece has >= 2 words,
# or if it holds >= 50% of the label's content tokens.
_MIN_QUOTE_CONTENT = 3
_MIN_PIECE_WORDS = 2
_QUOTE_COVER = 0.5
_PLACEHOLDER = re.compile(r"\{(left|right|transcript|labels)\}")


def _fill(template: str, **values: str) -> str:
    """Single-pass placeholder substitution: text inserted for one placeholder is never re-scanned,
    so labels or transcript text containing ``{right}`` etc. stay literal."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def _fallback(log, stage: str, item_id: str, n_items: int) -> None:
    log.append("judge_fallback", {"stage": stage, "item_id": item_id, "n_items": n_items})


class _BadAnswer(ValueError):
    """A well-formed JSON answer that breaks the contract; the message goes back to the model."""


def _int(x, what: str) -> int:
    """A JSON integer. Not a bool, a float such as 1.7, or a string such as "1"."""
    if not isinstance(x, int) or isinstance(x, bool):
        raise _BadAnswer(f"{what} must be an integer, got {x!r}")
    return x


def _json_body(text: str):
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(body)


def _numbered(labels: list[str], start: int = 1) -> str:
    return "\n".join(f"{k}. {' '.join(l.split())}" for k, l in enumerate(labels, start))


def lexical_sim(a: str, b: str) -> float:
    """Jaccard similarity of content tokens; 0.0 if either side has none."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------- alignment

def _parse_pairs(text: str, lo: int, hi: int, n_right: int) -> dict[tuple[int, int], str] | str:
    """{(a-1, b-1): rel} with a in lo..hi (1-based, global) and b in 1..n_right, or an error message."""
    try:
        data = _json_body(text)
        out: dict[tuple[int, int], str] = {}
        for p in data["pairs"]:
            a, b, rel = _int(p["a"], "a"), _int(p["b"], "b"), str(p["rel"]).strip().lower()
            if not (lo <= a <= hi and 1 <= b <= n_right):
                return f"pair a={a} b={b} is out of range (A {lo}-{hi}, B 1-{n_right})"
            if rel not in ("same", "close"):
                return f"rel must be \"same\" or \"close\", got {rel!r}"
            key = (a - 1, b - 1)
            if out.get(key) != "same":      # "same" beats "close" if both are listed
                out[key] = rel
        return out
    except _BadAnswer as exc:
        return str(exc)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"not the requested JSON ({type(exc).__name__})"


def align_labels(log, client, left: list[str], right: list[str], prompts_dir, *, item_id: str,
                 stage: str = "judge:align",
                 prompt_version: str = DEFAULT_PROMPT_VERSION) -> dict[tuple[int, int], str]:
    """Ask the judge which RIGHT labels each LEFT label denotes. ``left`` goes in chunks of at most
    60 (numbers are global, 1-based in the prompt); the whole ``right`` list is sent with every chunk.
    Returns {(i, j): "same" | "close"} with 0-based indexes into ``left`` / ``right``.
    ``prompt_version`` "v2" (default) sends a system message and the sandwich-layout prompt; "v1" reproduces
    the earlier single user message."""
    if not left or not right:
        return {}
    template = load_helper_prompt(prompts_dir, "align", prompt_version)
    right_block = _numbered(right)
    result: dict[tuple[int, int], str] = {}
    for c in range(0, len(left), _CHUNK):
        chunk = left[c:c + _CHUNK]
        lo, hi = c + 1, c + len(chunk)
        prompt = _fill(template, left=_numbered(chunk, lo), right=right_block)
        parsed: dict[tuple[int, int], str] = {}

        def validate(text: str, lo=lo, hi=hi) -> str | None:
            res = _parse_pairs(text, lo, hi, len(right))
            if isinstance(res, str):
                return res
            parsed.clear()
            parsed.update(res)
            return None
        text, _ = log_call(log, client, helper_messages(prompts_dir, prompt, prompt_version), stage=stage,
                           item_id=f"{item_id}:{c // _CHUNK}", temperature=0.0,
                           seed=derive_seed(stage, item_id, c // _CHUNK),
                           max_tokens=400 + 60 * len(chunk), validate=validate)
        if text is None:
            _fallback(log, stage, f"{item_id}:{c // _CHUNK}", len(chunk))
        else:
            result.update(parsed)
    return result


# ---------------------------------------------------------------- matching

@dataclass(frozen=True)
class MatchSetting:
    lex_threshold: float      # pairs with lexical_sim >= this are accepted without the judge
    judge_accept: str         # "same" (strict) | "same_or_close" (lenient) | "none" (lexical only)

    def __post_init__(self):
        if self.judge_accept not in ("same", "same_or_close", "none"):
            raise ValueError(f"judge_accept must be same | same_or_close | none, got {self.judge_accept!r}")

    def key(self) -> str:
        return f"lex{self.lex_threshold:g}_{self.judge_accept}"


SETTINGS_GRID: tuple[MatchSetting, ...] = tuple(
    MatchSetting(lex, acc) for lex in (0.4, 0.5, 0.6, 0.7) for acc in ("same", "same_or_close"))
# The DEFAULT primary setting: used by ``metrics.headline`` when no primary is given and by the judge bench. A run's own
# primary setting is the first of its configured settings (``cfg.settings[0]`` in the pipeline), recorded in the aggregates.
PRIMARY_SETTING = MatchSetting(0.6, "same")


@dataclass
class Matching:
    pairs: set[tuple[str, str]]          # (left node_id, right node_id)
    left_matched: set[str]
    right_matched: set[str]


def match_nodes(left: MapGraph, right: MapGraph, setting: MatchSetting,
                judged: dict[tuple[int, int], str] | None) -> Matching:
    """Set-based matching: every (left, right) pair that clears the lexical threshold or that the
    judge accepted under ``setting``. Many-to-many on purpose. ``judged`` indexes refer to
    ``left.nodes`` / ``right.nodes`` order."""
    accepted = {"same": ("same",), "same_or_close": ("same", "close"), "none": ()}[setting.judge_accept]
    judged = judged or {}
    pairs: set[tuple[str, str]] = set()
    for i, ln in enumerate(left.nodes):
        for j, rn in enumerate(right.nodes):
            if lexical_sim(ln.text, rn.text) >= setting.lex_threshold or judged.get((i, j)) in accepted:
                pairs.add((ln.node_id, rn.node_id))
    return Matching(pairs, {a for a, _ in pairs}, {b for _, b in pairs})


def edge_recovered(ref_edge: Edge, ai: MapGraph, m: Matching) -> bool:
    """True if some AI edge a->b has (a, ref.source) and (b, ref.target) in ``m.pairs`` (direction respected).
    ``m`` must come from ``match_nodes(ai, ref, ...)``."""
    return any((e.source, ref_edge.source) in m.pairs and (e.target, ref_edge.target) in m.pairs
               for e in ai.edges)


# ---------------------------------------------------------------- off-record detection

def _label_number(raw, n: int, seen) -> int:
    """A label number in 1..n that has not been answered yet."""
    k = _int(raw, "n")
    if not 1 <= k <= n:
        raise _BadAnswer(f"n={k} is out of range (labels 1-{n})")
    if k in seen:
        raise _BadAnswer(f"n={k} is answered more than once")
    return k


def _parse_quotes(text: str, n: int) -> dict[int, str | None] | str:
    try:
        data = _json_body(text)
        out: dict[int, str | None] = {}
        for a in data["answers"]:
            q = a["quote"]
            if q is not None and not isinstance(q, str):
                return "quote must be a string or null"
            k = _label_number(a["n"], n, out)
            out[k] = q
    except _BadAnswer as exc:
        return str(exc)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"not the requested JSON ({type(exc).__name__})"
    missing = [k for k in range(1, n + 1) if k not in out]
    return f"missing answers for labels {missing}" if missing else out


def _quote_supports(quote: str, label: str, transcript: Transcript) -> bool:
    """(a) the quote is located (exact within one turn, or approximate within one same-speaker turn: the tiered rule
    of ``textnorm.locate_quote_in_turns``) and (b) it is not trivial: at least 3 content tokens AND
    every "..." piece at least 2 words, or else at least 50% of the label's content tokens."""
    if locate_quote_in_turns(transcript.units("full"), quote)[0] == "unfound":
        return False
    pieces = [p for p in re.split(r"\.{3,}", normalize(quote)) if word_count(p)]
    if (len(content_tokens(quote)) >= _MIN_QUOTE_CONTENT
            and all(word_count(p) >= _MIN_PIECE_WORDS for p in pieces)):
        return True
    lt = content_tokens(label)
    return bool(lt) and len(lt & content_tokens(quote)) / len(lt) >= _QUOTE_COVER


def in_transcript(log, client, labels: list[str], transcript: Transcript, prompts_dir, *,
                  item_id: str, prompt_version: str = IN_TRANSCRIPT_PROMPT_VERSION,
                  stage: str = "in_transcript") -> dict[int, bool]:
    """For each label (0-based index) is it mentioned anywhere in the full transcript (any speaker)?

    The model proposes a verbatim quote (or null). The quote marks the label as mentioned only if
    (a) the tiered rule finds it (exact within ONE turn, or approximate within one same-speaker turn) and (b) it is not trivial: at least 3 content
    tokens and every ellipsis-separated piece at least 2 words, OR it holds at least 50% of the
    label's content tokens. Otherwise (paraphrased,
    absent, too short to mean anything) the lexical rule decides: at least 60% of the label's content
    tokens occur in a single turn. A failed model call leaves the lexical rule alone."""
    if not labels:
        return {}
    template = load_helper_prompt(prompts_dir, "in_transcript", prompt_version)
    full = transcript.text("full")
    turn_tokens = [content_tokens(t.text) for t in transcript.turns]
    quotes: dict[int, str | None] = {}
    for c in range(0, len(labels), _CHUNK):
        chunk = labels[c:c + _CHUNK]
        prompt = _fill(template, transcript=full, labels=_numbered(chunk))
        parsed: dict[int, str | None] = {}

        def validate(text: str, n=len(chunk)) -> str | None:
            res = _parse_quotes(text, n)
            if isinstance(res, str):
                return res
            parsed.clear()
            parsed.update(res)
            return None
        text, _ = log_call(log, client, helper_messages(prompts_dir, prompt, prompt_version), stage=stage,
                           item_id=f"{item_id}:{c // _CHUNK}", temperature=0.0,
                           seed=derive_seed("in_transcript", item_id, c // _CHUNK),
                           max_tokens=300 + 120 * len(chunk), validate=validate)
        if text is None:
            _fallback(log, stage, f"{item_id}:{c // _CHUNK}", len(chunk))
        else:
            quotes.update({c + k - 1: q for k, q in parsed.items()})
    out: dict[int, bool] = {}
    for i, label in enumerate(labels):
        q = quotes.get(i)
        found = bool(q) and _quote_supports(q, label, transcript)
        if not found:
            lt = content_tokens(label)
            found = bool(lt) and any(len(lt & tt) / len(lt) >= _FRACTION for tt in turn_tokens)
        out[i] = found
    return out


# ---------------------------------------------------------------- canonical labels

def _parse_concepts(text: str, n: int) -> list[str] | str:
    try:
        data = _json_body(text)
        out: dict[int, str] = {}
        for a in data["concepts"]:
            c = " ".join(str(a["concept"]).split())
            if not c:
                return "concept must not be empty"
            out[_label_number(a["n"], n, out)] = c
    except _BadAnswer as exc:
        return str(exc)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"not the requested JSON ({type(exc).__name__})"
    missing = [k for k in range(1, n + 1) if k not in out]
    return f"missing answers for labels {missing}" if missing else [out[k] for k in range(1, n + 1)]


def canonicalize(log, client, labels: list[str], prompts_dir, *, item_id: str, batch_size: int = 50,
                 prompt_version: str = DEFAULT_PROMPT_VERSION, stage: str = "canonicalize") -> list[str]:
    """Map each label to a 1-4 word plain concept name. ONE prompt for every label source (AI, endorsed,
    prior-only): it names no source, and the call takes none, because the pull-toward-script estimate relies on it.
    A batch the model never answers validly keeps its original labels (whitespace-collapsed)."""
    template = load_helper_prompt(prompts_dir, "canonicalize", prompt_version)
    out: list[str] = []
    for b in range(0, len(labels), batch_size):
        batch = labels[b:b + batch_size]
        prompt = _fill(template, labels=_numbered(batch))
        parsed: list[str] = []

        def validate(text: str, n=len(batch)) -> str | None:
            res = _parse_concepts(text, n)
            if isinstance(res, str):
                return res
            parsed[:] = res
            return None
        text, _ = log_call(log, client, helper_messages(prompts_dir, prompt, prompt_version), stage=stage,
                           item_id=f"{item_id}:{b // batch_size}", temperature=0.0,
                           seed=derive_seed("canonicalize", item_id, b // batch_size),
                           max_tokens=100 + 30 * len(batch), validate=validate)
        if text is None:
            _fallback(log, stage, f"{item_id}:{b // batch_size}", len(batch))
        out.extend(parsed if text is not None else [" ".join(l.split()) for l in batch])
    return out


def cluster_labels(labels: list[str], threshold: float = 0.5) -> list[int]:
    """Deterministic greedy clustering, in input order: a label joins the first existing cluster whose
    representative (its first member) has lexical_sim >= threshold, else it starts a new cluster.
    Returns one cluster id per label, numbered by first appearance. A label with no content tokens
    never joins anything (lexical_sim is 0.0), so it forms its own cluster."""
    reps: list[str] = []
    ids: list[int] = []
    for label in labels:
        for cid, rep in enumerate(reps):
            if lexical_sim(label, rep) >= threshold:
                ids.append(cid)
                break
        else:
            reps.append(label)
            ids.append(len(reps) - 1)
    return ids
