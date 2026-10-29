"""Reconstruction: turn a transcript into an AI-drawn ``MapGraph`` with verbatim quotes,
or (arm ``prior_only``) draw the "official script" map from the role and domain alone.

The model's reply is checked before it is accepted; a failed check is sent back to
the model as a short message (``llm.log_call`` appends it) and the call is retried, three
attempts in all. Quotes that are not verbatim are the one thing the check tolerates in
large numbers (L16, council round 10): up to ``max_unfound_share`` (default 0.50, the same for every ladder
rung) of the quotes may be unfound (the third tier of ``textnorm.locate_quote_tiered``; exact and approximate quotes
never count), and Task 6 later buckets those elements as ``unanchored``. A node type outside ``NODE_TYPES`` is never a reason to retry: ``parse_recon``
coerces it to "other" and keeps the original in ``raw_type``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .llm import derive_seed, log_call
from .prompts import DEFAULT_PROMPT_VERSION, check_version, load_definitions, load_system  # noqa: F401 (re-exported)
from .schemas import NODE_TYPES, STATUSES, Edge, MapGraph, Node, Transcript
from .textnorm import locate_quote_in_turns, locate_quote_tiered, word_count

ARMS = ("full", "removed", "prior_only")
VARIANTS = ("p0", "p1", "p2")
KINDS = ("reconstruct", "prior_only")
MAX_NODES = 120
MAX_LABEL_CHARS = 120
MAX_MESSAGE_CHARS = 300      # a validation message is shown to the model on retry: keep it short
MAX_IDS_IN_MESSAGE = 5
# One output budget for every reconstruction and ladder call, every rung alike (run-2 review 3.3: 8000 was tight for the
# longest real maps, and a length hit costs the whole decode).
RECON_MAX_TOKENS = 12000
# The retry text after a reply cut off at the limit (finish_reason "length"): the same for every arm and rung, and not
# a prompt file. Any other failure keeps the generic correction of ``llm.log_call``.
LENGTH_CORRECTION = ("Your previous answer was cut off before it ended (too long). Return the complete JSON again, "
                     "shorter: keep each quote to 8-20 words. Begin your answer with {")

# {phases} is filled by build_messages. The prior-only schema is the same shape with null quotes.
OUTPUT_SCHEMA_TEXT = (
    '{"nodes": [{"id": "n1", "label": "short label (up to 8 words)", '
    '"type": "service|information_source|actor|actor_information_source|scenario|other", '
    '"phase": "one of: {phases}; or null", "status": "experienced|wished", '
    '"quote": "verbatim words copied from the transcript"}],\n'
    ' "edges": [{"source": "n1", "target": "n2", "status": "experienced|wished", '
    '"quote": "verbatim words copied from the transcript"}],\n'
    ' "emotion_word": "one word or null"}'
)
PRIOR_ONLY_SCHEMA_TEXT = OUTPUT_SCHEMA_TEXT.replace(
    '"quote": "verbatim words copied from the transcript"', '"quote": null').replace(
    "experienced|wished", "experienced")

_PLACEHOLDER = re.compile(r"\{(role|domain|phases|schema|transcript|definitions)\}")
_PROMPT_NAME = re.compile(r"p[0-9]+")


@dataclass(frozen=True)
class ReconSpec:
    arm: str                 # one of ARMS
    variant: str             # "p0" | "p1" | "p2"
    temperature: float       # 0.0 primary; 0.7 for variation samples
    replicate: int = 0

    def key(self) -> str:
        return f"{self.arm}__{self.variant}__t{self.temperature:g}__r{self.replicate}"


@dataclass
class ReconContext:
    domain: str              # a NOUN PHRASE, e.g. "food-truck licensing in a mid-sized city"; the prompts
                             # read it after "about", "in", "on" and "dealing with"
    role: str                # e.g. "food-truck owner"
    phases: tuple[str, ...]  # template phases the map must use
    max_input_words: int = 45000


def load_prompt(prompts_dir, kind: str, variant: str, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """Read ``<kind>.<variant>.<version>.txt`` (default v2; the v1 files stay for the record).
    Anything outside KINDS / pN / vN raises ValueError."""
    if kind not in KINDS:
        raise ValueError(f"unknown prompt kind {kind!r} (want one of {KINDS})")
    if not _PROMPT_NAME.fullmatch(variant):
        raise ValueError(f"bad prompt variant {variant!r} (want p0, p1, p2)")
    check_version(version)
    return (Path(prompts_dir) / f"{kind}.{variant}.{version}.txt").read_text(encoding="utf-8")


def build_messages(prompt: str, ctx: ReconContext, transcript_text: str | None, *,
                   system: str | None = None, definitions: str | None = None) -> list[dict]:
    """Fill the prompt placeholders in ONE pass (so braces inside the transcript, or a
    transcript that happens to contain "{schema}", are never re-expanded).

    A prompt with a ``{transcript}`` placeholder needs ``transcript_text``; a prompt without
    one (prior-only) never sends a transcript, even if one is passed. A prompt with a
    ``{definitions}`` slot needs ``definitions``. With ``system`` the result is
    ``[system, user]``, otherwise ``[user]`` (a v1 prompt).
    """
    with_transcript = "{transcript}" in prompt
    if with_transcript and transcript_text is None:
        raise ValueError("this prompt needs a transcript but transcript_text is None")
    if "{definitions}" in prompt and definitions is None:
        raise ValueError("this prompt has a {definitions} slot but definitions is None")
    phases = ", ".join(ctx.phases)
    schema = (OUTPUT_SCHEMA_TEXT if with_transcript else PRIOR_ONLY_SCHEMA_TEXT).replace("{phases}", phases)
    values = {"role": ctx.role, "domain": ctx.domain, "phases": phases, "schema": schema,
              "transcript": transcript_text or "", "definitions": definitions or ""}
    user = {"role": "user", "content": _PLACEHOLDER.sub(lambda m: values[m.group(1)], prompt)}
    return [{"role": "system", "content": system}, user] if system is not None else [user]


def _strip_fence(text: str) -> str:
    s = text.strip()
    s = re.sub(r"^```[A-Za-z]*", "", s).strip() if s.startswith("```") else s
    return (s[:-3] if s.endswith("```") else s).strip()


def _ids(items) -> str:
    items = list(items)
    more = f" (+{len(items) - MAX_IDS_IN_MESSAGE} more)" if len(items) > MAX_IDS_IN_MESSAGE else ""
    return ", ".join(items[:MAX_IDS_IN_MESSAGE]) + more


def _short(msg: str) -> str:
    return msg if len(msg) <= MAX_MESSAGE_CHARS else msg[:MAX_MESSAGE_CHARS - 3] + "..."


def _edge_id(e) -> str:
    return f"{e.get('source')}->{e.get('target')}"


def _quote_tier(transcript_text: str, turn_units, quote) -> str:
    """The tier of ``quote``: from the arm's turns when given (``Transcript.validation_units``, which keeps the seam
    where the removed arm lost a mapping-talk turn), else from the rendered text."""
    if turn_units is not None:
        return locate_quote_in_turns(turn_units, quote)[0]
    return locate_quote_tiered(transcript_text, quote)[0]


def validate_recon(text: str, *, arm: str, transcript_text: str | None, max_unfound_share: float = 0.50,
                   phases: tuple | None = None, turn_units=None) -> str | None:
    """None if the reply is a usable map, else a short message (<= 300 chars, <= 5 ids named).

    ``phases`` is checked only when given. For arms full/removed, ``transcript_text`` is the
    text of that arm; for prior_only it is ignored. ``turn_units``: the arm's turns as
    ``Transcript.validation_units(arm)`` (default: cut ``transcript_text`` into turns).
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}")
    if arm != "prior_only" and transcript_text is None:
        raise ValueError(f"arm {arm!r} needs transcript_text")
    try:
        d = json.loads(_strip_fence(text))
    except ValueError:
        return "the reply is not valid JSON; reply with only the JSON object"
    if not isinstance(d, dict):
        return "the top level must be a JSON object with the keys nodes and edges"
    missing = [k for k in ("nodes", "edges") if not isinstance(d.get(k), list)]
    if missing:
        return f"the JSON object must have a list under each of the keys nodes and edges; problem with: {', '.join(missing)}"
    nodes, edges = d["nodes"], d["edges"]
    if not all(isinstance(x, dict) for x in nodes + edges):
        return "every node and edge must be a JSON object"
    if not 1 <= len(nodes) <= MAX_NODES:
        return f"nodes must hold at least 1 and at most {MAX_NODES} items, got {len(nodes)}"

    bad_id = [str(i) for i, n in enumerate(nodes) if not isinstance(n.get("id"), str) or not n["id"]]
    if bad_id:
        return _short(f"every node needs a non-empty string id; check nodes at positions {_ids(bad_id)}")
    seen, dup = set(), []
    for n in nodes:
        if n["id"] in seen and n["id"] not in dup:
            dup.append(n["id"])
        seen.add(n["id"])
    if dup:
        return _short(f"duplicate node ids: {_ids(dup)}")
    # an invalid "type" is not an error: parse_recon coerces it to "other" (run 2, council round 8)
    bad = [n["id"] for n in nodes if n.get("status") not in STATUSES]
    if bad:
        return _short(f"invalid status on nodes {_ids(bad)}; allowed: {', '.join(STATUSES)}")
    bad = [n["id"] for n in nodes
           if not isinstance(n.get("label"), str) or not 1 <= len(n["label"].strip()) <= MAX_LABEL_CHARS]
    if bad:
        return _short(f"label must be 1 to {MAX_LABEL_CHARS} characters on nodes {_ids(bad)}")
    if phases is not None:
        bad = [n["id"] for n in nodes if n.get("phase") is not None and n.get("phase") not in phases]
        if bad:
            return _short(f"invalid phase on nodes {_ids(bad)}; allowed: {', '.join(phases)} or null")

    problems = []
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if not isinstance(src, str) or not isinstance(tgt, str):
            problems.append(f"edge {_edge_id(e)}: source and target must be node id strings")
        elif src not in seen:
            problems.append(f"edge {_edge_id(e)}: unknown node {src}")
        elif tgt not in seen:
            problems.append(f"edge {_edge_id(e)}: unknown node {tgt}")
        elif src == tgt:
            problems.append(f"edge {_edge_id(e)}: self-loop")
        elif e.get("status") not in STATUSES:
            problems.append(f"edge {_edge_id(e)}: invalid status, allowed: {', '.join(STATUSES)}")
    if problems:
        return _short("; ".join(problems[:MAX_IDS_IN_MESSAGE]))

    items = [(n["id"], n.get("quote")) for n in nodes] + [(_edge_id(e), e.get("quote")) for e in edges]
    if arm == "prior_only":
        bad = [n["id"] for n in nodes if n["status"] == "wished"] + \
              [_edge_id(e) for e in edges if e["status"] == "wished"]
        if bad:
            return _short(f'the official script has no "wished" items; use "experienced" on: {_ids(bad)}')
        bad = [i for i, q in items if q is not None]
        return _short(f"quote must be null (there is no transcript) on: {_ids(bad)}") if bad else None
    bad = [i for i, q in items if not isinstance(q, str) or not q.strip()]
    if bad:
        return _short(f"every node and edge needs a non-empty quote string; missing on: {_ids(bad)}")
    unfound = [i for i, q in items if _quote_tier(transcript_text, turn_units, q) == "unfound"]
    if len(unfound) / len(items) > max_unfound_share:
        return _short(f"these quotes are not verbatim: {_ids(unfound)}. Copy words exactly from the transcript.")
    return None


def _coerce_type(raw) -> tuple[str, str | None]:
    """(node_type, raw_type): a listed type stays; anything else (a phase name, a wrong string, a non-string, a
    missing key) becomes "other" and the model's value is kept as text."""
    if isinstance(raw, str) and raw in NODE_TYPES:
        return raw, None
    if raw is _MISSING:
        return "other", "<missing>"
    return "other", raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, ensure_ascii=False)


_MISSING = object()
QUOTE_TIERS = ("exact", "approximate", "unfound")


def parse_recon(text: str, map_id: str, origin: str, spec_key: str | None = None,
                transcript_text: str | None = None, turn_units=None) -> MapGraph:
    """Build a MapGraph from a reply that already passed ``validate_recon``.

    ``label`` -> ``Node.text``, ``type`` -> ``Node.node_type``. AI edges are glued with no label.

    A ``type`` outside NODE_TYPES becomes "other": ``meta["raw_types"]`` ({node id: what the model wrote}) keeps the
    original and ``meta["type_coercions"]`` counts them. With ``transcript_text`` (the arm's text the model saw)
    ``meta["quote_tiers"]`` holds the tier of every quote ({node id | "e:<source>-><target>": exact |
    approximate | unfound}, the keys ``provenance.provenance_for`` uses) and ``meta["quote_tier_counts"]`` counts
    them; without it (prior-only) no tier is claimed. The element classes keep their fields, so maps saved before
    run 2 and the synthetic gold files read and write as they always did.
    """
    d = json.loads(_strip_fence(text))
    nodes, raw_types = [], {}
    for n in d["nodes"]:
        node_type, raw = _coerce_type(n.get("type", _MISSING))
        if raw is not None:
            raw_types[n["id"]] = raw
        nodes.append(Node(node_id=n["id"], text=n["label"].strip(), node_type=node_type, phase=n.get("phase"),
                          status=n["status"], quote=n.get("quote")))
    edges = [Edge(source=e["source"], target=e["target"], status=e["status"], glued=True,
                  quote=e.get("quote"), label=None) for e in d["edges"]]
    emo = d.get("emotion_word")
    emo = emo.strip() if isinstance(emo, str) and emo.strip() else None
    meta = {"spec": spec_key} if spec_key else {}
    meta["type_coercions"] = len(raw_types)
    if raw_types:
        meta["raw_types"] = raw_types
    if transcript_text is not None:
        tiers = {x.node_id: _quote_tier(transcript_text, turn_units, x.quote) for x in nodes if isinstance(x.quote, str)}
        edge_tiers = [(f"e:{x.source}->{x.target}", _quote_tier(transcript_text, turn_units, x.quote))
                      for x in edges if isinstance(x.quote, str)]
        tiers.update(edge_tiers)
        # counted from the list, not the dict: a repeated edge (same source and target) shares one dict key
        every = [*(tiers[x.node_id] for x in nodes if isinstance(x.quote, str)), *(t for _k, t in edge_tiers)]
        meta["quote_tiers"] = tiers
        meta["quote_tier_counts"] = {t: every.count(t) for t in QUOTE_TIERS}
    return MapGraph(map_id=map_id, origin=origin, nodes=nodes, edges=edges, emotion_word=emo, meta=meta)


def _outage_without_reply(records: list[dict]) -> bool:
    """The rule of ``pipeline._Tap.transport_failed_calls`` for one call: no attempt got a reply (a ``call_response``) and
    the LAST attempt ended in a ``call_error`` that ``log_call`` flagged ``transport`` (endpoint down, timeout, 429/5xx
    after every retry). Any other failure with no reply (HTTP 400/401, a malformed body, a reply that could not be
    logged) is the call's final answer: a rerun would meet it again."""
    if any(r.get("record_type") == "call_response" for r in records):
        return False
    errors = [r for r in records if r.get("record_type") == "call_error"]
    return bool(errors) and bool((errors[-1].get("payload") or {}).get("transport"))


def accepted_attempt(records: list[dict]) -> int | None:
    """The attempt (1, 2 or 3) at which ``log_call`` accepted the reply: the number on its last ``call_request``. A map
    accepted at attempt 2 or 3 was written with a rejected draft and a correction in context (review 3.4). None when
    the records hold no request."""
    for rec in reversed(records):
        if rec.get("record_type") == "call_request":
            n = (rec.get("payload") or {}).get("attempt")
            return int(n) if n is not None else None
    return None


def reconstruct(log, client, transcript: Transcript | None, spec: ReconSpec, ctx: ReconContext,
                prompts_dir, *, item_id: str, max_tokens: int = RECON_MAX_TOKENS,
                prompt_version: str = DEFAULT_PROMPT_VERSION, stage: str | None = None,
                rung=None, real_data: bool | None = None,
                max_draws: int | None = None, rung_template=None) -> tuple[MapGraph | None, str]:
    """Return (graph, status), status in {"ok", "failed", "too_long_excluded"}; with a ``rung`` also "error" (every
    attempt ended in a transport error, none got a reply: the item was not judged and must be redone).

    ``prompt_version`` "v2" (default) sends a system message and the shared construct
    definitions; "v1" re-runs the original single-user-message prompt for the record.

    Arms full/removed need a transcript; an over-long one is excluded without any model call.
    Arm prior_only never sends a transcript. ``map_id`` is ``item_id``. ``stage`` names the audit-log stage
    (default ``recon:<arm>``).

    ``rung`` (a ``ladder.Rung``, default None = everything above, unchanged) runs one condition of the instruction
    ladder: the messages come from ``ladder.rung_messages`` (the rung names its own prompt, system message and
    definitions, so ``prompt_version`` must stay at its default), the spec is checked with ``ladder.check_rung_spec``
    (``real_data=True``: primary spec only, replicate below ``max_draws``, default 3; ``real_data`` must be given
    whenever a rung is), the audit-log stage and item id are made unique per rung
    (``ladder.rung_stage(...).log``, ``ladder.rung_item_id``) and the seed is ``ladder.rung_seed``, equal in every rung
    for the same map and draw. Validation, retries, retry texts, max_tokens, sampling and the call itself are exactly
    those of a call without a rung. The returned graph keeps the unsuffixed ``map_id`` (so rungs pair by map) and
    records the rung in ``meta["rung"]``. Reconstruction arms only (a rung has no prior_only prompt).
    ``rung_template`` (rung 1 only, a tool run's ``tool.rung1_user`` block) is passed to ``ladder.rung_messages``.
    """
    if spec.arm not in ARMS:
        raise ValueError(f"unknown arm {spec.arm!r}")
    prior_only = spec.arm == "prior_only"
    if rung_template is not None and rung is None:
        raise ValueError("rung_template needs a rung (rung 1)")
    if rung is not None:
        from . import ladder     # imported here: ladder imports this module
        if prior_only:
            raise ValueError("a ladder rung applies to reconstruction arms; prior_only has no rung prompt")
        if prompt_version != DEFAULT_PROMPT_VERSION:
            raise ValueError("prompt_version cannot be combined with rung: the rung names its own prompt files")
        if real_data is None:
            raise TypeError("reconstruct(rung=...) needs real_data=True (real corpus) or False (synthetic) explicitly")
        ladder.check_rung_spec(rung, spec, real_data=real_data,
                               max_draws=ladder.REAL_DRAWS if max_draws is None else max_draws)
    turn_units = None
    if prior_only:
        transcript_text = None
    else:
        if transcript is None:
            raise ValueError(f"arm {spec.arm!r} needs a transcript")
        transcript_text = transcript.text(spec.arm)
        turn_units = transcript.validation_units(spec.arm)
        if word_count(transcript_text) > ctx.max_input_words:
            return None, "too_long_excluded"
    if rung is not None:
        messages = ladder.rung_messages(prompts_dir, rung, ctx, transcript_text, spec=spec, template=rung_template)
        map_id = ladder.base_item_id(item_id)
        log_stage = stage or ladder.rung_stage(rung, spec).log
        log_item_id = ladder.rung_item_id(map_id, rung)
        seed = ladder.rung_seed(map_id, spec.key())
    else:
        prompt = load_prompt(prompts_dir, "prior_only" if prior_only else "reconstruct", spec.variant,
                             version=prompt_version)
        if prompt_version == "v1":
            messages = build_messages(prompt, ctx, transcript_text)
        else:   # never an empty system message (digest 3.1.8)
            messages = build_messages(prompt, ctx, transcript_text,
                                      system=load_system(prompts_dir, "reconstruct", prompt_version),
                                      definitions=load_definitions(prompts_dir, prompt_version))
        map_id, log_stage, log_item_id = item_id, stage or f"recon:{spec.arm}", item_id
        seed = derive_seed(item_id, spec.key())

    def validate(reply: str) -> str | None:
        return validate_recon(reply, arm=spec.arm, transcript_text=transcript_text, phases=ctx.phases,
                              turn_units=turn_units)

    text, records = log_call(log, client, messages, stage=log_stage, item_id=log_item_id,
                              temperature=spec.temperature, seed=seed,
                              max_tokens=max_tokens, validate=validate, max_attempts=3,
                              length_correction=LENGTH_CORRECTION)
    if text is None:
        if rung is not None and _outage_without_reply(records):
            return None, "error"        # no attempt got a reply and the last one was an outage: redo, not a failed map
        return None, "failed"
    graph = parse_recon(text, map_id, "prior_only" if prior_only else "ai", spec_key=spec.key(),
                        transcript_text=transcript_text, turn_units=turn_units)
    if rung is not None:
        graph.meta["rung"] = rung.key
    graph.meta["accepted_attempt"] = accepted_attempt(records)
    return graph, "ok"
