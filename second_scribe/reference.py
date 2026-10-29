"""Reference modes of a tool run (K3, K11): what the AI's map is compared with.

``tool.reference_type`` (``toolcfg``) names the mode; a study config has no ``tool`` block and never reaches this code.

- ``"A"``: the interviewer-drawn, participant-endorsed map, read as the study reads it. Aggregates are unchanged.
- ``"B"``: one researcher's own span-coded reading of the transcript. Each node is anchored on a span of the
  transcript, so the in_transcript stage marks every node present without a model call. A coding has no drawn
  edges and no box types, so the structure numbers (edge recall, linearization, homogenization, type shares) are
  ``{"status": "not_available"}``; node-level numbers stay.
- ``"C"``: no reference. The app writes an empty map per participant (``empty_reference``), so the in_transcript stage
  makes no call (no labels). Every number that reads the reference is ``{"status": "no_reference"}``; the AI-only
  numbers stay (the contradiction rate, the AI node count of each rung).

``mask_aggregates`` is pure: it returns a new dict and never changes its input.
"""
from __future__ import annotations

import copy

from .schemas import MapGraph
from .toolcfg import REFERENCE_TYPES

NO_REFERENCE = {"status": "no_reference"}
NOT_AVAILABLE = {"status": "not_available"}

# headline keys that do not read the reference (mode C keeps them)
_C_HEADLINE_KEEP = ("n_participants", "primary_spec", "primary_setting")
# the one absences block that needs no reference: AI wished/experienced statuses against the quotes' modality
_C_ABSENCES_KEEP = "contradiction_rate"
# the one ladder metric that needs no reference
_C_LADDER_KEEP = "ai_node_count"
_B_BUCKET_KEYS = ("edge_recall", "n_edges_eligible", "n_edges_recovered")
_B_HEADLINE_KEYS = ("linearization", "homogenization")


def reference_type(cfg) -> str | None:
    """The run's reference mode (``"A"``, ``"B"`` or ``"C"``) from ``cfg.tool``, or None for a study config."""
    return (getattr(cfg, "tool", None) or {}).get("reference_type")


def empty_reference(map_id: str) -> MapGraph:
    """The mode-C reference of one participant: an endorsed map with no nodes and no edges. The app writes it as
    ``reference/<pid>.mapgraph.json``."""
    return MapGraph(map_id=map_id, origin="endorsed", meta={"reference_type": "C"})


def _headline_c(h: dict) -> dict:
    out = {}
    for k, v in h.items():
        if k in _C_HEADLINE_KEEP:
            out[k] = v
        elif k == "absences" and isinstance(v, dict):
            out[k] = {sk: (sv if sk == _C_ABSENCES_KEEP else dict(NO_REFERENCE)) for sk, sv in v.items()}
        else:
            out[k] = dict(NO_REFERENCE)
    return out


def _mask_c(agg: dict) -> None:
    for k in ("absences", "buckets"):
        if k in agg:
            agg[k] = dict(NO_REFERENCE)
    if isinstance(agg.get("headline"), dict):
        agg["headline"] = _headline_c(agg["headline"])
    for arm, h in (agg.get("other_arms") or {}).items():     # the other arm's headline reads the reference the same way
        if isinstance(h, dict):
            agg["other_arms"][arm] = _headline_c(h)
    rep = (agg.get("ladder") or {}).get("report")
    if not isinstance(rep, dict):
        return
    for rung in (rep.get("rungs") or {}).values():
        if "three_way" in rung:
            rung["three_way"] = dict(NO_REFERENCE)
        if isinstance(rung.get("mirror"), dict):
            rung["mirror"] = {k: v for k, v in rung["mirror"].items() if k == _C_LADDER_KEEP}
    for c in (rep.get("contrasts") or {}).values():
        if isinstance(c.get("metrics"), dict):
            c["metrics"] = {k: v for k, v in c["metrics"].items() if k == _C_LADDER_KEEP}
    for k in ("holm_leak", "script_conditional"):              # leak tests: a leak is a reference item
        if k in rep:
            rep[k] = dict(NO_REFERENCE)
    if "verdict" in rep:
        rep["verdict"] = {"verdict": "no_reference", "reason_codes": ["no_reference"]}


def _type_shares_b(d):
    if isinstance(d, dict):
        for k in d:
            if k.startswith("type_share:"):
                d[k] = dict(NOT_AVAILABLE)
            else:
                _type_shares_b(d[k])
    elif isinstance(d, list):
        for v in d:
            _type_shares_b(v)


def _mask_b(agg: dict) -> None:
    b = agg.get("buckets")
    if isinstance(b, dict):
        for block in [b.get("primary"), *(b.get("by_spec") or {}).values()]:
            if isinstance(block, dict):
                for k in _B_BUCKET_KEYS:
                    if k in block:
                        block[k] = dict(NOT_AVAILABLE)
    for h in [agg.get("headline"), *(agg.get("other_arms") or {}).values()]:
        if isinstance(h, dict):
            for k in _B_HEADLINE_KEYS:
                if k in h:
                    h[k] = dict(NOT_AVAILABLE)
    _type_shares_b(agg)


def mask_aggregates(agg: dict, ref_type: str) -> dict:
    """A copy of ``agg`` with the numbers that the reference mode cannot support replaced by a status block
    (see the module docstring). ``"A"`` returns an unchanged copy. An unknown mode raises ``ValueError``."""
    if ref_type not in REFERENCE_TYPES:
        raise ValueError(f"unknown reference type {ref_type!r} (want one of {REFERENCE_TYPES})")
    out = copy.deepcopy(agg)
    if ref_type == "C":
        _mask_c(out)
    elif ref_type == "B":
        _mask_b(out)
    return out
