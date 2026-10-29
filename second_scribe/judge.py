"""Batched modality judge: what does a quote say about the map element it supports?

The judge sees only the element text and the quote (never the transcript) and answers with
one label from ``MODALITIES``.  Labels are cached on disk so a repeated run asks the model
nothing it already answered.  A batch the model never answers validly counts as
"unsupported" for its items and is recorded in the audit log as ``judge_failed_batch``.

Prompt v3 (default, ``prompts.JUDGE_PROMPT_VERSION``; v2 is kept for the record) is a system message plus a
rubric card (a two-step instruction: first whether the quote is about the element itself, then what it says about it;
the construct definitions shared with the reconstruction prompt, the six labels, one reference example per label
written in a third domain, then the numbered items and a short reminder).  Replies are parsed strictly; a
batch whose model replies never parsed is also counted as UNPARSED (``judge_unparsed`` record
and the optional ``stats`` tally) so auditors can tell "the judge said unsupported" from
"the judge's answer could not be read".
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from .audit.verify import log_work_item
from .llm import derive_seed, log_call, redact_params
from .prompts import DEFAULT_PROMPT_VERSION, JUDGE_PROMPT_VERSION, check_version, load_definitions, load_system
from .provenance import Provenance, provenance_for
from .schemas import MODALITIES, MapGraph, Transcript

MAX_REPLY_WORDS = 60


def _squash(s: str | None) -> str:
    return " ".join((s or "").split())


def _reply_text(speaker: str | None, reply: str | None) -> str:
    """The participant's reply as it is shown to the judge: only for an interviewer quote, cut to
    ``MAX_REPLY_WORDS`` words."""
    if speaker != "interviewer":
        return ""
    return " ".join((reply or "").split()[:MAX_REPLY_WORDS])


def _render_item(k: int, it: dict) -> str:
    speaker = it.get("speaker")
    tag = f" ({speaker})" if speaker in ("participant", "interviewer") else ""
    text = f'{k}. ELEMENT: {_squash(it["element"])}\n   QUOTE{tag}: "{_squash(it["quote"])}"'
    reply = _reply_text(speaker, it.get("reply"))
    if reply:
        text += f'\n   REPLY (participant): "{reply}"'
    return text


def _render_template(prompts_dir, version: str, name: str = "judge_modality") -> tuple[str, str | None]:
    """(rubric card with the shared definitions filled in, system text or None for v1)."""
    template = (Path(prompts_dir) / f"{name}.{check_version(version)}.txt").read_text(encoding="utf-8")
    if "{definitions}" in template:
        template = template.replace("{definitions}", load_definitions(prompts_dir, version))
    system = None if version == "v1" else load_system(prompts_dir, "judge", version)
    return template, system


LABEL_ORDERS = ("forward", "reversed")
_LABEL_LINE = re.compile(r"^- (" + "|".join(MODALITIES) + r"):")
_EXAMPLE_LINE = re.compile(r"^Example ([A-F])\. (.*)$")


def reverse_label_order(template: str) -> str:
    """The rubric card with its six label lines in reverse order and its reference examples in reverse order too (the
    example letters stay A-F top to bottom, so "Example A" is now the unsupported example). Nothing else moves; the
    tie-break sentence names the labels, so it is unchanged. Used to measure label-position bias (R58); the study's
    judge runs forward. Raises ValueError for a card that has no complete label block."""
    lines = template.split("\n")
    def label_at(i):
        m = _LABEL_LINE.match(lines[i])
        return m.group(1) if m else None
    # the six consecutive label lines, in MODALITIES order or its reverse (the shared definitions above them may use "- wished:" too)
    start = next((i for i in range(len(lines) - len(MODALITIES) + 1)
                  if [label_at(i + k) for k in range(len(MODALITIES))] in (list(MODALITIES), list(MODALITIES)[::-1])),
                 None)
    if start is None:
        raise ValueError("the rubric card has no complete '- <label>:' block to reverse")
    lab = list(range(start, start + len(MODALITIES)))
    for idx in (lab, [i for i, ln in enumerate(lines) if _EXAMPLE_LINE.match(ln)]):
        body = [lines[i] for i in idx]
        for i, ln in zip(idx, reversed(body)):
            lines[i] = ln
    ex = [i for i, ln in enumerate(lines) if _EXAMPLE_LINE.match(ln)]
    for k, i in enumerate(ex):
        lines[i] = _EXAMPLE_LINE.sub(lambda m: f"Example {LETTERS[k]}. {m.group(2)}", lines[i])
    return "\n".join(lines)


def _check_order(label_order: str) -> None:
    if label_order not in LABEL_ORDERS:
        raise ValueError(f"label_order must be one of {LABEL_ORDERS}, got {label_order!r}")


def prompt_fingerprint(prompts_dir, version: str = JUDGE_PROMPT_VERSION, label_order: str = "forward") -> str:
    """sha256 of the system text plus the rendered rubric card (definitions filled in, ``{items}``
    left as the slot). The judge cache is keyed on it. ``label_order`` "reversed" fingerprints the reversed card
    (R58), so a forward cache never answers a reversed run."""
    _check_order(label_order)
    template, system = _render_template(prompts_dir, version)
    if label_order == "reversed":
        template = reverse_label_order(template)
    return hashlib.sha256(f"{system or ''}\x1e{template}".encode("utf-8")).hexdigest()


class JudgeCache:
    """JSON file ``{key: modality | {"modality", "request_id"}}``; key = sha256(prompt fingerprint [+ model id and
    params], element, quote, speaker, reply).

    A missing file is an empty cache; a corrupt one raises ValueError (delete it to start
    over) so a cache is never silently reset.  Every ``put`` rewrites the file atomically
    (temp file in the same directory, then ``os.replace``).
    """

    def __init__(self, path, log=None):
        self.path = Path(path)
        self.log = log      # with a log, every stored label is announced there first (a work_item record with its hash)
        self._data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ValueError(f"judge cache {self.path.name} is not valid JSON: {exc}") from None
            if not isinstance(data, dict):
                raise ValueError(f"judge cache {self.path.name} is not a JSON object")
            self._data = data

    @staticmethod
    def key(element: str, quote: str, prompt_id: str, speaker: str | None = None,
            reply: str | None = None) -> str:
        """sha256 over (prompt fingerprint, element, quote, speaker, reply as rendered).

        ``prompt_id`` is ``prompt_fingerprint(...)``: the rendered rubric card plus the system
        text, so editing either invalidates every cached label (a file name would not)."""
        raw = "\x1f".join((prompt_id, _squash(element), _squash(quote), speaker or "", _reply_text(speaker, reply)))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        v = self._data.get(key)
        return v.get("modality") if isinstance(v, dict) else v

    def request_id(self, key: str) -> str | None:
        """The request_id of the call that produced this label (None for an entry stored without one)."""
        v = self._data.get(key)
        return v.get("request_id") if isinstance(v, dict) else None

    def put(self, key: str, modality: str, request_id: str | None = None) -> None:
        """Store a label. With ``request_id`` (the call that produced it) the entry is
        ``{"modality", "request_id"}``, so a label served from the cache can be traced to a logged call."""
        if modality not in MODALITIES:
            raise ValueError(f"unknown modality {modality!r}")
        self._data[key] = {"modality": modality, "request_id": request_id} if request_id else modality
        if self.log is not None:
            log_work_item(self.log, "cache:" + self.path.stem, key, self._data[key])     # before the write
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, sort_keys=True, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


def _parse_labels(text: str, n_items: int) -> dict[int, str] | str:
    """{n: modality} for every n in 1..n_items exactly once, or an error message."""
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        rows = json.loads(body)["labels"]
        pairs = [(r["n"], str(r["modality"]).strip().lower()) for r in rows]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return f"not the requested JSON ({type(exc).__name__})"
    out: dict[int, str] = {}
    for n, m in pairs:
        if not isinstance(n, int) or isinstance(n, bool):
            return "item number must be an integer"
        if not 1 <= n <= n_items:
            return f"n={n} is not one of the items 1..{n_items}"
        if n in out:
            return f"item {n} appears more than once"
        if m not in MODALITIES:
            return f"modality {m!r} for item {n} is not one of {', '.join(MODALITIES)}"
        out[n] = m
    missing = [n for n in range(1, n_items + 1) if n not in out]
    return f"missing labels for items {missing[:5]}" if missing else out


def judge_modality(log, client, items: list[dict], prompts_dir, *, item_id: str, batch_size: int = 20,
                   stage: str = "judge:modality", cache: JudgeCache | None = None,
                   prompt_version: str = JUDGE_PROMPT_VERSION, stats: dict | None = None) -> dict[str, str]:
    """``judge_modality_detailed(...)[0]``: the labels only (a not-judged item is "unsupported")."""
    return judge_modality_detailed(log, client, items, prompts_dir, item_id=item_id, batch_size=batch_size,
                                   stage=stage, cache=cache, prompt_version=prompt_version, stats=stats)[0]


def judge_modality_detailed(log, client, items: list[dict], prompts_dir, *, item_id: str, batch_size: int = 20,
                            stage: str = "judge:modality", cache: JudgeCache | None = None,
                            prompt_version: str = JUDGE_PROMPT_VERSION,
                            stats: dict | None = None,
                            label_order: str = "forward") -> tuple[dict[str, str], set[str]]:
    """Label every item with a modality: ``({element_id: modality in MODALITIES}, not_judged_ids)``.

    ``not_judged_ids`` holds the items of every batch that failed or was UNPARSED: their label
    is "unsupported" only as a placeholder, and a metric must leave them out of its denominator
    rather than score them as unsupported. An item with no quote words is "unsupported" by rule
    and is not in the set.

    ``items``: ``[{"element_id", "element", "quote"}]`` with optional ``speaker`` ("participant" |
    "interviewer" | "unknown") and ``reply`` (the participant's next turn, shown for an interviewer
    quote only, cut to 60 words).  An item with no words in its quote
    is "unsupported" without a model call.  Cached items are never sent; items that share
    the same (element, quote) are sent once.  Only misses are batched (``batch_size`` at
    most, numbered 1.. inside each batch).  A batch that fails validation after
    ``log_call``'s retries gives "unsupported" to its items, is NOT cached, and is logged
    as ``log.append("judge_failed_batch", {"batch_index", "item_ids"})``.  If the model did
    reply but no reply ever parsed, the batch is also UNPARSED:
    ``log.append("judge_unparsed", {"batch_index", "n_items"})`` (a batch that got no reply at
    all, only transport errors, is failed but not UNPARSED).  ``stats``, if given, is filled
    with ``batches``, ``failed_batches``, ``unparsed_batches`` and ``unparsed_items`` (ids).

    ``prompt_version`` "v3" (default, ``JUDGE_PROMPT_VERSION``) and "v2" send a system message and the rubric card
    (v3 asks first whether the quote is about the element itself); "v1" runs the original single-user-message prompt
    for the record.

    ``label_order`` "forward" (default) is the study's prompt; "reversed" (R58, bench only) lists the labels and
    the reference examples backwards (``reverse_label_order``) and fingerprints the cache accordingly.
    """
    _check_order(label_order)
    template, system = _render_template(prompts_dir, prompt_version)
    if label_order == "reversed":
        template = reverse_label_order(template)
    fingerprint = prompt_fingerprint(prompts_dir, prompt_version, label_order)
    cfg = getattr(client, "cfg", None)
    if getattr(cfg, "model_id", None) is not None:      # a label is the answer of one model with one set of params
        fingerprint += "\x1d" + hashlib.sha256(json.dumps(
            [cfg.model_id, redact_params(getattr(cfg, "params", None) or {})], sort_keys=True).encode("utf-8")).hexdigest()
    tally = {"batches": 0, "failed_batches": 0, "unparsed_batches": 0, "unparsed_items": [], "cache_hits": 0}
    cache_ids: set[str] = set()                  # request ids of the calls whose labels were served from the cache
    not_judged: set[str] = set()
    out: dict[str, str] = {}
    pending: dict[str, list[dict]] = {}          # cache key -> items awaiting a label
    for it in items:
        if not _squash(it.get("quote")):
            out[it["element_id"]] = "unsupported"
            continue
        key = JudgeCache.key(it["element"], it["quote"], fingerprint, it.get("speaker"), it.get("reply"))
        hit = cache.get(key) if cache is not None else None
        if hit in MODALITIES:
            out[it["element_id"]] = hit
            tally["cache_hits"] += 1
            if cache.request_id(key):
                cache_ids.add(cache.request_id(key))
        else:
            pending.setdefault(key, []).append(it)
    keys = list(pending)
    for batch_index, b in enumerate(range(0, len(keys), batch_size)):
        batch = keys[b:b + batch_size]
        listing = "\n".join(_render_item(k, pending[key][0]) for k, key in enumerate(batch, 1))
        prompt = template.replace("{items}", listing)     # not .format: quotes may hold braces
        parsed: dict[int, str] = {}

        def validate(text: str, n=len(batch)) -> str | None:
            res = _parse_labels(text, n)
            if isinstance(res, str):
                return res
            parsed.clear()
            parsed.update(res)
            return None
        messages = [{"role": "user", "content": prompt}]
        if system is not None:      # never an empty system message (digest 3.1.8)
            messages.insert(0, {"role": "system", "content": system})
        tally["batches"] += 1
        text, recs = log_call(log, client, messages, stage=stage,
                              item_id=f"{item_id}:{batch_index}", temperature=0.0,
                              seed=derive_seed(item_id, batch_index),
                              max_tokens=60 * len(batch) + 100, validate=validate)
        if text is None:
            ids = [it["element_id"] for key in batch for it in pending[key]]
            log.append("judge_failed_batch", {"batch_index": batch_index, "item_ids": ids})
            tally["failed_batches"] += 1
            if any(r["record_type"] == "call_response" for r in recs):
                log.append("judge_unparsed", {"batch_index": batch_index, "n_items": len(ids)})
                tally["unparsed_batches"] += 1
                tally["unparsed_items"] += ids
            for i in ids:
                out[i] = "unsupported"
            not_judged.update(ids)
            continue
        for k, key in enumerate(batch, 1):
            for it in pending[key]:
                out[it["element_id"]] = parsed[k]
            if cache is not None:
                cache.put(key, parsed[k], request_id=next(r["payload"]["request_id"] for r in reversed(recs)
                                                         if r["record_type"] == "call_request"))
    tally["cache_request_ids"] = sorted(cache_ids)
    if cache_ids and hasattr(log, "note_request_ids"):    # the work store lists them among the item's request ids
        log.note_request_ids(sorted(cache_ids))
    if stats is not None:
        stats.update(tally)
    return out, not_judged


# ---------------------------------------------------------------- log-probability letter mode

LETTERS = "ABCDEF"                                    # A=actual, B=wished, ... F=unsupported (MODALITIES order)
LETTER_PARAMS = {"logprobs": True, "top_logprobs": 10}
LETTER_PROMPT = "judge_modality_letter"
# Minimum probability the first token must put on the letters A-F (summed over all of them and all spellings) for an
# item to count as judged. Below it the model did not mean to answer with a letter (a markdown opener, "The", "Answer"):
# renormalising a sliver to 1.0 would turn it into a confident judgement and hide the format failure from the
# not-judged gates. Not a pass-rule threshold of the bench: the bench counts such items as not judged.
LETTER_MASS_MIN = 0.5


def letter_prompt_fingerprint(prompts_dir, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """Like ``prompt_fingerprint`` for the one-item letter prompt (its own cache namespace)."""
    template, system = _render_template(prompts_dir, version, LETTER_PROMPT)
    return hashlib.sha256(f"letter\x1e{system or ''}\x1e{template}".encode("utf-8")).hexdigest()


def letter_mass(logprobs) -> float:
    """Raw probability (before renormalising) that the first token is one of the letters A-F: the sum of exp(logprob)
    of every top token that, stripped, is exactly one upper-case letter A..F. 0.0 for no logprobs or no letter."""
    total = 0.0
    for token, lp in logprobs or ():
        t = str(token).strip()
        if len(t) == 1 and t in LETTERS:
            total += math.exp(lp)
    return total


def letter_distribution(logprobs) -> dict[str, float] | None:
    """Distribution over ``MODALITIES`` from the first generated token's ``[[token, logprob], ...]``.

    A token counts when, stripped of spaces and newlines, it is exactly one of the UPPER-case letters ``A``..``F``
    (a lower-case ``a`` is the article, not an answer); several tokens for one letter (``"B"``, ``" B"``,
    ``"\nB"``) add their probabilities. The mass of the six letters is then
    renormalised to 1. ``None`` if there are no logprobs or no letter among them: the caller must treat the
    item as not judged, never guess."""
    mass = {m: 0.0 for m in MODALITIES}
    found = False
    for token, lp in logprobs or ():
        t = str(token).strip()
        if len(t) == 1 and t in LETTERS:
            mass[MODALITIES[LETTERS.index(t)]] += math.exp(lp)
            found = True
    total = sum(mass.values())
    if not found or total <= 0.0:
        return None
    return {m: v / total for m, v in mass.items()}


def judge_modality_logprob(log, client, items: list[dict], prompts_dir, *, item_id: str,
                           cache: JudgeCache | None = None, stage: str = "judge:modality_letter",
                           prompt_version: str = DEFAULT_PROMPT_VERSION,
                           stats: dict | None = None) -> tuple[dict[str, str], dict[str, dict[str, float]], set[str]]:
    """Judge mode (b): ONE call per item, answer is one letter, read from the first token's logprobs.

    Returns ``(labels, distributions, not_judged_ids)``. ``items`` are as for ``judge_modality_detailed``.
    Each distinct (element, quote, speaker, reply) is sent once with ``max_tokens=1``, temperature 0 and
    ``logprobs``/``top_logprobs=10``; the label is the argmax of the distribution over the six labels
    (ties go to the earlier label in ``MODALITIES``). If the server returns no logprobs, or none of the
    top tokens is a letter, or the letters hold less than ``LETTER_MASS_MIN`` (0.5) of the probability of the first
    token (reason ``low_letter_mass``: the model did not mean to answer with a letter), or the call fails, the item is NOT judged: its label is "unsupported" only as a
    placeholder, it is in ``not_judged_ids``, it has no distribution, and a ``judge_logprob_failed`` record
    ``{"item_ids", "reason"}`` goes to the log. The reply text is never read as an answer.
    An item with no quote words is "unsupported" without a call (distribution {"unsupported": 1.0}).
    A cache hit gives its label but no distribution: calibration must come from an uncached run.
    ``stats``, if given, is filled with ``calls``, ``no_logprobs``, ``no_letter_token`` (logprobs came back but
    none of them is a letter, e.g. the first token was whitespace), ``low_letter_mass``, ``failed_calls`` (the call
    itself failed), and ``letter_mass_n`` / ``letter_mass_sum`` (calls that returned a letter and the sum of their
    letter mass: mean = sum / n). The mass of every such call is also logged (``judge_letter_mass``)."""
    template, system = _render_template(prompts_dir, prompt_version, LETTER_PROMPT)
    fingerprint = letter_prompt_fingerprint(prompts_dir, prompt_version)
    tally = {"calls": 0, "no_logprobs": 0, "no_letter_token": 0, "low_letter_mass": 0, "failed_calls": 0,
             "letter_mass_n": 0, "letter_mass_sum": 0.0}
    labels: dict[str, str] = {}
    dists: dict[str, dict[str, float]] = {}
    not_judged: set[str] = set()
    pending: dict[str, list[dict]] = {}
    for it in items:
        if not _squash(it.get("quote")):
            labels[it["element_id"]] = "unsupported"
            dists[it["element_id"]] = {m: float(m == "unsupported") for m in MODALITIES}
            continue
        key = JudgeCache.key(it["element"], it["quote"], fingerprint, it.get("speaker"), it.get("reply"))
        hit = cache.get(key) if cache is not None else None
        if hit in MODALITIES:
            labels[it["element_id"]] = hit
        else:
            pending.setdefault(key, []).append(it)
    for k, (key, group) in enumerate(pending.items()):
        first = group[0]
        speaker = first.get("speaker")
        tag = f" ({speaker})" if speaker in ("participant", "interviewer") else ""
        shown = f'ELEMENT: {_squash(first["element"])}\nQUOTE{tag}: "{_squash(first["quote"])}"'
        reply = _reply_text(speaker, first.get("reply"))
        if reply:
            shown += f'\nREPLY (participant): "{reply}"'
        messages = [{"role": "user", "content": template.replace("{item}", shown)}]
        if system is not None:
            messages.insert(0, {"role": "system", "content": system})
        tally["calls"] += 1
        text, recs = log_call(log, client, messages, stage=stage, item_id=f"{item_id}:{k}", temperature=0.0,
                              seed=derive_seed(item_id, k), max_tokens=1, max_attempts=1,
                              params=dict(LETTER_PARAMS), allow_length=True, allow_empty=True)
        responses = [r for r in recs if r["record_type"] == "call_response"]
        top = responses[-1]["payload"].get("logprobs") if text is not None and responses else None
        dist = letter_distribution(top)
        mass = letter_mass(top)
        ids = [it["element_id"] for it in group]
        if dist is not None:
            tally["letter_mass_n"] += 1
            tally["letter_mass_sum"] += mass
            log.append("judge_letter_mass", {"item_ids": ids, "letter_mass": mass})
        if dist is None or mass < LETTER_MASS_MIN - 1e-9:
            reason = ("failed_call" if text is None else "no_logprobs" if not top else
                      "no_letter_token" if dist is None else "low_letter_mass")
            tally["failed_calls" if reason == "failed_call" else reason] += len(group)
            log.append("judge_logprob_failed", {"item_ids": ids, "reason": reason, "letter_mass": mass})
            for i in ids:
                labels[i] = "unsupported"
            not_judged.update(ids)
            continue
        label = max(MODALITIES, key=lambda m: dist[m])        # first maximum wins ties
        for i in ids:
            labels[i] = label
            dists[i] = dict(dist)
        if cache is not None:
            cache.put(key, label)
    if stats is not None:
        stats.update(tally)
    return labels, dists, not_judged


def _next_participant_reply(transcript: Transcript, turn_idx: int | None) -> str | None:
    """The next participant turn after ``turn_idx``, unless another interviewer turn comes first: when the
    interviewer asks twice in a row, the reply answers the LAST question, and the earlier one has none."""
    if turn_idx is None:
        return None
    for t in sorted(transcript.turns, key=lambda t: t.idx):
        if t.idx <= turn_idx:
            continue
        if t.speaker == "participant":
            return t.text
        if t.speaker == "interviewer":
            return None
    return None


def modality_items(ai: MapGraph, provenance: dict[str, Provenance] | None = None,
                   transcript: Transcript | None = None) -> list[dict]:
    """Judge items for every node and edge of ``ai`` that carries a quote.

    Node: ``{"element_id": node_id, "element": label}``; edge: ``element_id`` "e:<source>-><target>"
    and element "<source label> → <target label>" (the same ids ``provenance_for`` uses).

    With ``provenance`` (or just ``transcript``, from which it is computed) each item whose quote was
    found also gets ``speaker``; an interviewer quote also gets ``reply``, the text of the next
    participant turn after the matched turn (when the transcript is given and there is one).

    An APPROXIMATE-tier quote (not word for word in the transcript) is shown to the judge as the transcript's own
    words of the matched span (``Provenance.matched_text``: normalised tokens, lower-case, no punctuation; "..." parts
    joined by " ... "); the reconstructor's wording is kept in ``reconstructor_quote`` and is not rendered or hashed.
    The reply of an interviewer quote then follows the LAST matched line, not the first. Exact-tier quotes are
    unchanged."""
    if provenance is None and transcript is not None:
        provenance = provenance_for(ai, transcript)
    labels = {n.node_id: n.text for n in ai.nodes}
    out = [{"element_id": n.node_id, "element": n.text, "quote": n.quote} for n in ai.nodes if n.quote]
    out += [{"element_id": f"e:{e.source}->{e.target}",
             "element": f"{labels.get(e.source, e.source)} → {labels.get(e.target, e.target)}",
             "quote": e.quote} for e in ai.edges if e.quote]
    for it in out:
        p = (provenance or {}).get(it["element_id"])
        if p is None or not p.found or p.speaker is None:
            continue
        it["speaker"] = p.speaker
        anchor = p.turn_idx
        if p.tier == "approximate" and p.matched_text:
            # R68: the judge reads what the participant (or interviewer) SAID, not the reconstructor's near-copy, and
            # the reply that follows is the one after the LAST matched line. The cache key hashes this rendered text.
            it["reconstructor_quote"] = it["quote"]
            it["quote"] = p.matched_text
            if p.last_turn_idx is not None:
                anchor = p.last_turn_idx
        if p.speaker == "interviewer" and transcript is not None:
            reply = _next_participant_reply(transcript, anchor)
            if reply:
                it["reply"] = reply
    return out


def attested(status: str, modality: str) -> bool:
    """Does the judged modality back the element's drawn status?
    experienced needs "actual"; wished needs "wished"; anything else is not attested."""
    return modality == {"experienced": "actual", "wished": "wished"}.get(status)
