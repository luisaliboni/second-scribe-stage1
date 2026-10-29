"""Headline metrics: buckets, absences (leakage), pull toward the script, homogenization,
linearization, leakage across arms, controls, and the assembled ``headline``.

Everything here is a pure function of pre-computed inputs (``PairInputs``): no model calls, no
clock, no global randomness. Maps are "interviewer-drawn, participant-endorsed maps" (endorsed)
and AI reconstructions (ai). An AI element counts only if it is transcript-anchored ("attested"):
its quote is eligible in the arm (``provenance.eligible``) AND the judged modality backs the
drawn status (``judge.attested``).

Conventions
  * ``spec_key`` is ``ReconSpec.key()``; its first ``__`` piece is the arm ("full", "removed",
    "prior_only"). Missing provenance, modality, in_transcript or alignment entries raise
    KeyError on purpose (a silently empty input would silently change a headline number);
    the one exception is an AI *edge* without a provenance or modality entry, which is simply
    not an element (see ``leakage``).
  * Every reported block carries ``n`` (units used), ``n_excluded`` and ``excluded_reasons``
    (reason -> count); the reasons add up to ``n_excluded``. ``ai_failed`` means the AI map for
    that spec is None or absent.
  * Per-map quantities with an empty denominator are nan inside the functions and become
    exclusions in the summaries. ``headline`` returns JSON-safe data (nan -> None).
  * Bootstraps resample participants (or participant pairs), seeded; ``B`` is the number of
    resamples.
  * The modality labels come from ``PairInputs.modality``; ``headline(modality_source=...)``
    swaps in the second judge or the no-AI keyword classifier.
"""
from __future__ import annotations

import math
import random
import zlib
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache

from .graphs import structure
from .judge import attested
from .keywords import keyword_modality
from .match import PRIMARY_SETTING, Matching, MatchSetting, edge_recovered
from .provenance import Provenance, eligible
from .reconstruct import ARMS, ReconSpec
from .schemas import BUCKETS, MapGraph, Node
from .stats import (_percentile_ci, bootstrap_ci, cohen_kappa, derangements, paired_bootstrap_ci,
                    pairwise_bootstrap_ci, permutation_rank, sign_test)
from .textnorm import content_tokens

PRIMARY_SPEC = ReconSpec("removed", "p0", 0.0).key()      # the primary estimate: arm without interviewer and mapping talk
FULL_SPEC = ReconSpec("full", "p0", 0.0).key()            # its full-arm twin (the mapping-talk ablation's other side)
NOT_JUDGED_LIMIT = 0.10                                   # above this share of unjudged AI elements: reported, not interpreted
MIN_WORDS_GRID = (5, 8, 12)
_NAN = math.nan
ABSENCE_COUNTS = ("recovered_as_wished", "leaked_as_existing", "leaked_quote_wished", "leaked_quote_actual",
                  "leaked_quote_unjudged", "leaked_quote_other", "leaked_unanchored", "unanchored_wished", "missed", "n_wished",
                  "flipped", "n_control", "counterfactual_leakage", "adequacy_flattening", "leak_split_indeterminate")


@dataclass
class PairInputs:
    """Everything the metrics need for one participant, all pre-computed.

    ``judged_*`` are ``align_labels`` outputs: ``(i, j)`` index the LEFT map's nodes and the RIGHT
    map's nodes in list order, with left = the AI (or prior-only, or endorsed) map and right = the
    endorsed (or script) map. ``judged_prior_vs_endorsed`` refers to the prior-only map named by
    ``prior_spec`` (default: the first key, in sorted order, that starts with "prior_only__").
    ``judged_ai_vs_script`` / ``judged_endorsed_vs_script`` refer to the script returned by
    ``scripts_by_role`` for ``role`` (same prior-only maps, same order). A raw alignment that was never obtained
    is None (``judged_prior_vs_endorsed``, ``judged_endorsed_vs_script``) or absent for its spec
    (``judged_ai_vs_script``): the participant is left out of that comparison (``alignment_missing``), never
    scored as "nothing matched". An empty dict is a supplied "nothing matched".

    Optional fields:
      modality_judge2   second judge's labels, same shape as ``modality``.
      modality_shuffled modality labels of the quote-shuffled re-judge (stage "judge:shuffled"),
                        same shape as ``modality``.
      canon_endorsed / canon_ai   canonical (plain-concept) labels aligned with the nodes of the
                        endorsed map / of each AI map (spec key -> list). When absent the raw node
                        text stands in for the canonical label.
      canon_prior       canonical labels of each prior-only map (spec key -> list aligned with its nodes);
                        the canonical script is built from them (``scripts_by_role(canonical=True)``).
      judged_ai_vs_script_canon / judged_endorsed_vs_script_canon   alignments of the CANONICAL labels of
                        the AI (spec key -> pairs) / endorsed map against the CANONICAL script. Without them
                        the canonical pull estimate matches lexically only (and its claim gate stays closed:
                        ``canonical_used`` / ``judge_channel_canonical``). The un-suffixed ``judged_*_script``
                        fields are about raw labels and the raw script (the raw-label robustness line).
      not_judged        spec key -> AI element ids (node ids, "e:<s>-><t>") the judge never answered for (a failed
                        or unparsed batch; ``judge_modality_detailed``). Their ``modality`` entry is a placeholder:
                        such an element is never attested and never unanchored; it is left out of the AI-side
                        bucket counts (``n_not_judged``), the contradiction rate, the contaminated share and the
                        quote-shuffled control, and a leak it carries is ``leaked_quote_unjudged``.
      not_judged_judge2 the same for ``modality_judge2`` (used when the second judge's labels are swapped in).
      judged_endorsed_vs_sources   script source name -> alignment of the endorsed map's RAW labels against that
                        source's script (the ``align_labels`` pairs, ``(i, j)`` = endorsed node i, script node j);
                        None = never obtained. The source named "own" falls back to ``judged_endorsed_vs_script``.
      not_judged_shuffled the same for ``modality_shuffled``; the quote-shuffled control leaves out, on both sides,
                        every node that either pass did not answer for.
      reading3_separate True when the map named by ``prior_spec`` is reading 3 drawn apart from the script set (the
                        config's ``reading3`` block): it then stays out of the script, the homogenization samples and
                        the phantom scripts (``script_keys``), it has no ``canon_prior`` entry, and ``_prior_key``
                        never falls back to a script draft. False (default): every prior-only map is a script draft.
    """
    pid: str
    role: str
    interviewer: str | None
    endorsed: MapGraph
    ai: dict[str, MapGraph | None]
    prov: dict[str, dict[str, Provenance]]
    modality: dict[str, dict[str, str]]
    in_transcript: dict[str, bool]
    judged_ai_vs_endorsed: dict[str, dict[tuple[int, int], str]]
    judged_prior_vs_endorsed: dict[tuple[int, int], str] | None
    judged_ai_vs_script: dict[str, dict[tuple[int, int], str]]
    judged_endorsed_vs_script: dict[tuple[int, int], str] | None
    modality_judge2: dict[str, dict[str, str]] | None = None
    modality_shuffled: dict[str, dict[str, str]] | None = None
    canon_endorsed: list[str] | None = None
    canon_ai: dict[str, list[str]] | None = None
    prior_spec: str | None = None
    canon_prior: dict[str, list[str]] | None = None
    judged_ai_vs_script_canon: dict[str, dict[tuple[int, int], str]] | None = None
    judged_endorsed_vs_script_canon: dict[tuple[int, int], str] | None = None
    not_judged: dict[str, set[str]] | None = None
    not_judged_judge2: dict[str, set[str]] | None = None
    not_judged_shuffled: dict[str, set[str]] | None = None
    judged_endorsed_vs_sources: dict[str, dict[tuple[int, int], str] | None] | None = None
    reading3_separate: bool = False


def _nj(p: PairInputs, spec: str) -> set[str]:
    """AI element ids of ``spec`` the judge never answered for (empty if none recorded)."""
    return set((p.not_judged or {}).get(spec) or ())


def _nj_shuffled(p: PairInputs, spec: str) -> set[str]:
    return set((p.not_judged_shuffled or {}).get(spec) or ())


# ------------------------------------------------------------------ small helpers

def spec_arm(spec_key: str) -> str:
    arm = spec_key.split("__", 1)[0]
    if arm not in ARMS:
        raise ValueError(f"spec key {spec_key!r} does not start with an arm in {ARMS}")
    return arm


def swap_arm(spec_key: str, arm: str) -> str:
    return arm + "__" + spec_key.split("__", 1)[1]


def _ratio(a: float, b: float) -> float:
    return a / b if b else _NAN


def _mean(xs) -> float:
    xs = list(xs)
    return math.fsum(xs) / len(xs) if xs else _NAN


def _nonnan(xs) -> list[float]:
    return [x for x in xs if not math.isnan(x)]


def _seed(seed: int, name: str) -> int:
    return (seed * 1000003 + zlib.crc32(name.encode())) & 0xFFFFFFFF


class _Ex:
    """Exclusion tally: one reason per excluded unit."""

    def __init__(self):
        self.c: Counter = Counter()

    def add(self, reason: str) -> None:
        self.c[reason] += 1

    def report(self, n_total: int, n_used: int) -> dict:
        assert sum(self.c.values()) == n_total - n_used, "exclusion reasons must add up"
        return {"n": n_used, "n_excluded": n_total - n_used, "excluded_reasons": dict(sorted(self.c.items()))}


def _paired(a: list[float], b: list[float], ex: _Ex, n_total: int, seed: int, B: int) -> dict:
    """Paired summary of a - b (one pair per participant): means, bootstrap CI (pairs resampled), sign test."""
    point, lo, hi = paired_bootstrap_ci(a, b, B=B, seed=seed)
    return {**ex.report(n_total, len(a)), "mean_a": _mean(a), "mean_b": _mean(b), "mean": point,
            "ci_lo": lo, "ci_hi": hi, "sign": sign_test([x - y for x, y in zip(a, b)])}


MIN_CELL_N = 3


def sign_stability(values_by_setting: dict[str, float], cell_n: dict[str, int] | None = None,
                   primary_key: str | None = None) -> dict:
    """Is the sign of the estimate the same in every setting?

    A cell is VALID when it has a value (not nan) and, if ``cell_n`` (cell key -> number of maps behind the
    value) is given, at least max(3, 50% of the primary cell's n) maps; the primary cell is ``primary_key``
    (default: the cell with the most maps). ``n_nan`` counts cells without a value, ``n_low_n`` cells with a
    value but too few maps, ``coverage`` = valid cells / cells, ``min_cell_n`` the smallest n over all cells.
    stable = all valid cells > 0 or all < 0 (a zero is no sign; no valid cell is not stable). ``stable`` ignores
    the invalid cells, so a DIRECTIONAL CLAIM must use ``claim_stable`` (every cell valid and positive)."""
    n_cells = len(values_by_setting)
    min_n = None
    if cell_n:
        ref = cell_n.get(primary_key, max(cell_n.values())) if primary_key is not None else max(cell_n.values())
        min_n = max(MIN_CELL_N, 0.5 * ref)
    nan_keys = {k for k, v in values_by_setting.items() if math.isnan(v)}
    low_keys = {k for k, v in values_by_setting.items()
                if k not in nan_keys and min_n is not None and cell_n.get(k, 0) < min_n}
    vals = [v for k, v in values_by_setting.items() if k not in nan_keys and k not in low_keys]
    n_pos = sum(1 for v in vals if v > 0)
    n_neg = sum(1 for v in vals if v < 0)
    return {"stable": bool(vals) and (n_pos == len(vals) or n_neg == len(vals)),
            "n_cells": n_cells, "coverage": len(vals) / n_cells if n_cells else 0.0,
            "n_pos": n_pos, "n_neg": n_neg, "n_zero": len(vals) - n_pos - n_neg,
            "n_nan": len(nan_keys), "n_low_n": len(low_keys),
            "min_cell_n": min(cell_n.values()) if cell_n else None, "min_n_required": min_n,
            "min": min(vals) if vals else None, "max": max(vals) if vals else None}


def claim_stable(stab: dict) -> bool:
    """Full coverage (no nan cell, no cell with too few maps) and every cell positive (all claims in this
    module are "above zero" claims)."""
    return stab["n_cells"] > 0 and stab["n_nan"] == 0 and stab["n_low_n"] == 0 and stab["n_pos"] == stab["n_cells"]


def _clean(x):
    """JSON-safe copy: nan/inf -> None, tuples -> lists, keys -> str."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


# ------------------------------------------------------------------ cached matching

@lru_cache(maxsize=None)
def _tok1(text: str) -> frozenset:
    return frozenset(content_tokens(text))


def _jacc(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0     # same as match.lexical_sim on token sets


@lru_cache(maxsize=16384)
def _lex_matrix(a: tuple, b: tuple) -> tuple:
    """Lexical similarities of two label lists, cached by CONTENT (never by object identity), because
    ``headline`` asks for the same map pair under many settings and specs."""
    ta, tb = [_tok1(x) for x in a], [_tok1(x) for x in b]
    return tuple(tuple(_jacc(x, y) for y in tb) for x in ta)


def match_nodes(left: MapGraph, right: MapGraph, setting: MatchSetting,
                judged: dict[tuple[int, int], str] | None) -> Matching:
    """Same result as ``match.match_nodes`` (a pair matches iff its lexical similarity reaches the setting's
    threshold or the judge accepted it under the setting), with the lexical part cached."""
    accepted = {"same": ("same",), "same_or_close": ("same", "close"), "none": ()}[setting.judge_accept]
    judged = judged or {}
    sims = _lex_matrix(tuple(n.text for n in left.nodes), tuple(n.text for n in right.nodes))
    pairs = {(ln.node_id, rn.node_id)
             for i, ln in enumerate(left.nodes) for j, rn in enumerate(right.nodes)
             if sims[i][j] >= setting.lex_threshold or judged.get((i, j)) in accepted}
    return Matching(pairs, {a for a, _ in pairs}, {b for _, b in pairs})


def cluster_labels(labels: list[str], threshold: float = 0.5) -> list[int]:
    """Same result as ``match.cluster_labels`` (greedy, input order, first cluster whose representative has
    lexical similarity >= threshold), on cached token sets: it is run over thousands of labels."""
    reps: list[frozenset] = []
    ids: list[int] = []
    for label in labels:
        t = _tok1(label)
        for cid, rep in enumerate(reps):
            if _jacc(t, rep) >= threshold:
                ids.append(cid)
                break
        else:
            reps.append(t)
            ids.append(len(reps) - 1)
    return ids


# ------------------------------------------------------------------ buckets and absences

def _attested_nodes(p: PairInputs, spec: str, arm: str, min_words: int) -> set[str]:
    g = p.ai[spec]
    pv, md, nj = p.prov[spec], p.modality[spec], _nj(p, spec)
    return {n.node_id for n in g.nodes if n.node_id not in nj
            if eligible(pv[n.node_id], arm, min_words)[0] and attested(n.status, md[n.node_id])}


def _matching(p: PairInputs, spec: str, setting: MatchSetting) -> Matching:
    g = p.ai.get(spec)
    if g is None:
        raise ValueError(f"{p.pid}: no AI map for {spec}")
    return match_nodes(g, p.endorsed, setting, p.judged_ai_vs_endorsed[spec])


def _attested_matching(p: PairInputs, spec: str, arm: str, setting: MatchSetting, min_words: int):
    """(all pairs, attested pairs, attested node ids): pairs are (ai id, endorsed id)."""
    m = _matching(p, spec, setting)
    att = _attested_nodes(p, spec, arm, min_words)
    pairs = {(a, e) for a, e in m.pairs if a in att}
    return m, Matching(pairs, {a for a, _ in pairs}, {e for _, e in pairs}), att


def buckets(p: PairInputs, spec_key: str, arm: str, setting: MatchSetting, *, min_words: int = 8) -> dict:
    """Counts of the five BUCKETS for nodes, plus recall.

    Endorsed node: ``off_record`` if not in the transcript; else ``matched`` if some ATTESTED AI node
    matches it; else ``missed``. AI node: ``n_not_judged`` (not a bucket) if the judge never answered for it
    (``PairInputs.not_judged``); else ``unanchored`` if not attested; ``attested_absent_from_map`` if
    attested and matched to no endorsed node (off-record ones included); an attested AI node that
    matches some endorsed node is counted in ``ai_attested_matched`` (not a bucket).
    ``n_endorsed_not_judged``: an in-transcript endorsed node that no attested AI node matches, but that an
    otherwise eligible AI node the judge never answered for does match, is neither matched nor missed (the
    missing judge answer is not a measured miss); edges with such an end leave ``edge_recall`` too.
    ``node_recall`` = matched / (matched + missed); ``edge_recall`` over endorsed glued edges whose two
    ends are both in the transcript, an edge being recovered by an AI edge between attested AI nodes
    that match its ends (nan when the denominator is 0)."""
    m, m_att, att = _attested_matching(p, spec_key, arm, setting, min_words)
    ai = p.ai[spec_key]
    out = {k: 0 for k in BUCKETS}
    out["ai_attested_matched"] = out["n_not_judged"] = out["n_endorsed_not_judged"] = 0
    nj = _nj(p, spec_key)
    nj_end: set[str] = set()          # endorsed nodes whose only possible match is an AI node the judge never answered for
    if nj:
        pv = p.prov[spec_key]
        nj_end = {e for a, e in m.pairs if a in nj and e not in m_att.right_matched
                  and eligible(pv[a], arm, min_words)[0]}
    for n in p.endorsed.nodes:
        if not p.in_transcript[n.node_id]:
            out["off_record"] += 1
        elif n.node_id in m_att.right_matched:
            out["matched"] += 1
        elif n.node_id in nj_end:
            out["n_endorsed_not_judged"] += 1
        else:
            out["missed"] += 1
    for n in ai.nodes:
        if n.node_id in nj:
            out["n_not_judged"] += 1
        elif n.node_id not in att:
            out["unanchored"] += 1
        elif n.node_id in m.left_matched:
            out["ai_attested_matched"] += 1
        else:
            out["attested_absent_from_map"] += 1
    out["node_recall"] = _ratio(out["matched"], out["matched"] + out["missed"])
    ref_edges = [e for e in p.endorsed.edges
                 if e.glued and p.in_transcript[e.source] and p.in_transcript[e.target]
                 and e.source not in nj_end and e.target not in nj_end]
    rec = sum(1 for e in ref_edges if edge_recovered(e, ai, m_att))
    out.update(edge_recall=_ratio(rec, len(ref_edges)), n_edges_eligible=len(ref_edges), n_edges_recovered=rec,
               n_endorsed_nodes=len(p.endorsed.nodes), n_ai_nodes=len(ai.nodes))
    return out


def _absence_structure(p: PairInputs, spec_key: str, arm: str, setting: MatchSetting, min_words: int) -> dict:
    """Everything ``absences`` needs that does not depend on the AI nodes' drawn statuses."""
    m = _matching(p, spec_key, setting)
    ai = p.ai[spec_key]
    pv, md = p.prov[spec_key], p.modality[spec_key]
    located = {n.node_id for n in ai.nodes if eligible(pv[n.node_id], arm, min_words)[0]}
    loc: dict[str, list[str]] = {}
    unloc: dict[str, list[str]] = {}
    for a, e in sorted(m.pairs):
        (loc if a in located else unloc).setdefault(e, []).append(a)
    return {"ends": [(n.node_id, n.status) for n in p.endorsed.nodes if p.in_transcript[n.node_id]],
            "loc": loc, "unloc": unloc, "mod": md, "nj": _nj(p, spec_key),
            "status": {n.node_id: n.status for n in ai.nodes}}


def absence_category(st: dict, status: dict[str, str], nid: str) -> tuple[str, str | None]:
    """(category, leak sub-split or None) of one endorsed WISHED in-transcript node ``nid``: "recovered_as_wished",
    "leaked_as_existing" (sub-split "leaked_quote_wished" | "leaked_quote_actual" | "leaked_quote_unjudged" |
    "leaked_quote_other"), "unanchored_wished", "leaked_unanchored" or "missed". ``st`` is
    ``_absence_structure``; the one classification used by ``absences`` and by the demo's trap table."""
    near, far = st["loc"].get(nid, []), st["unloc"].get(nid, [])
    if "wished" in {status[a] for a in near}:
        return "recovered_as_wished", None
    if near:                                     # located, and all of them drawn as experienced
        mods = {st["mod"][a] for a in near if a not in st["nj"]}
        return "leaked_as_existing", ("leaked_quote_wished" if "wished" in mods else
                                      "leaked_quote_actual" if "actual" in mods else
                                      "leaked_quote_unjudged" if any(a in st["nj"] for a in near) else
                                      "leaked_quote_other")
    if far:
        return ("unanchored_wished" if any(status[a] == "wished" for a in far) else "leaked_unanchored"), None
    return "missed", None


CONTRADICTING_MODALITIES = ("wished", "negated", "hypothetical", "other_person")


def leak_split(st: dict, nid: str) -> str:
    """The leak split by the AI's OWN quote, for one endorsed wished ideal ``nid`` that leaked (matched only by
    located AI nodes drawn as experienced). Over the judged quotes of those nodes:
      "adequacy_flattening"      some quote is judged ``actual``: the transcript says it happened, so the service is
                                 drawn as received and the inadequacy the participant described is lost;
      "counterfactual_leakage"   no quote says it happened and some quote contradicts the drawn status (judged
                                 wished, negated, hypothetical or other_person): the AI renders as existing what
                                 its own evidence says did not happen;
      "leak_split_indeterminate" nothing decides it: the only quotes are ``unsupported`` or were never judged.
    The name "counterfactual_leakage" is reserved for the second case. If a wished-quoted and an actual-quoted node
    both match the ideal, the transcript says it happened somewhere, so the strong name is NOT claimed (this
    differs from the ``leaked_quote_*`` precedence, wished before actual, which is kept as it was)."""
    near = st["loc"].get(nid, [])
    mods = {st["mod"][a] for a in near if a not in st["nj"]}
    if "actual" in mods:
        return "adequacy_flattening"
    if mods & set(CONTRADICTING_MODALITIES):
        return "counterfactual_leakage"
    return "leak_split_indeterminate"


def _absence_counts(st: dict, status: dict[str, str]) -> Counter:
    """Counts of ``absences`` for AI node statuses ``status`` (node id -> "experienced" | "wished")."""
    c = Counter()
    for nid, nstatus in st["ends"]:
        if nstatus == "wished":
            c["n_wished"] += 1
            cat, sub = absence_category(st, status, nid)
            c[cat] += 1
            if sub:
                c[sub] += 1
                c[leak_split(st, nid)] += 1
        else:
            c["n_control"] += 1
            c["flipped"] += {status[a] for a in st["loc"].get(nid, [])} == {"wished"}
    return c


def _absence_result(c: Counter) -> dict:
    out = {k: c[k] for k in ABSENCE_COUNTS}
    nw = c["n_wished"]
    out.update(recovery_rate=_ratio(c["recovered_as_wished"], nw), leak_rate=_ratio(c["leaked_as_existing"], nw),
               leak_quote_wished_rate=_ratio(c["leaked_quote_wished"], nw),
               counterfactual_leakage_rate=_ratio(c["counterfactual_leakage"], nw),
               adequacy_flattening_rate=_ratio(c["adequacy_flattening"], nw),
               leaked_unanchored_rate=_ratio(c["leaked_unanchored"], nw), miss_rate=_ratio(c["missed"], nw),
               flip_rate=_ratio(c["flipped"], c["n_control"]))
    return out


def absences(p: PairInputs, spec_key: str, arm: str, setting: MatchSetting, *, min_words: int = 8) -> dict:
    """Over endorsed WISHED nodes that are in the transcript, what did the AI do?

    An AI node is LOCATED when ``provenance.eligible(prov, arm, min_words)`` is true; its judged modality
    plays no part in that. Only located nodes are looked at first, classified by their drawn status:
      recovered_as_wished   matched by at least one located AI node drawn as wished,
      leaked_as_existing    matched, but only by located AI nodes drawn as experienced. Split by the
                            modality the judge gave the matched quote (if several located experienced nodes
                            match, the strongest wins: wished, then actual, then unjudged, then other):
        leaked_quote_wished   the quote itself says wished (the AI contradicts its own evidence),
        leaked_quote_actual,
        leaked_quote_unjudged the judge never answered for the matched quote (``PairInputs.not_judged``),
        leaked_quote_other    negated, hypothetical, other_person or unsupported,
      leaked_unanchored     matched only by AI nodes whose quote is NOT located, all drawn as experienced
                            (not part of ``leaked_as_existing``),
      unanchored_wished     matched only by AI nodes whose quote is not located, at least one drawn as
                            wished (neither a leak nor a recovery; kept so the counts add up),
      missed                matched by no AI node at all.
    Every leak is also split by ``leak_split`` into ``counterfactual_leakage`` (the AI's own quote contradicts
    the drawn status), ``adequacy_flattening`` (its quote says it happened) and ``leak_split_indeterminate``;
    the three add up to ``leaked_as_existing``.
    Control: of the endorsed EXPERIENCED in-transcript nodes (``n_control``), ``flipped`` are matched only
    by located AI nodes drawn as wished. Rates are per map (nan when the denominator is 0)."""
    st = _absence_structure(p, spec_key, arm, setting, min_words)
    return _absence_result(_absence_counts(st, st["status"]))


def status_permutation_null(p_list: list[PairInputs], spec_key: str, setting: MatchSetting, *, n_perm: int = 1000,
                            seed: int = 0, min_words: int = 8) -> dict:
    """The "worse than chance" check (``absences.worse_than_chance_check``); NEVER a headline and never a gate.

    Within each map the drawn statuses of the AI nodes are permuted (the number of wished nodes per map is
    kept; matching, location and modality are untouched), and the per-map (leak rate - flip rate) and its
    mean over maps are recomputed, ``n_perm`` times with ``random.Random(seed)``. Same maps as the paired
    absences summary (an AI map, wished and control nodes in the transcript). Reports the observed mean, the
    null mean and 95th percentile, and p = (1 + #{null >= observed}) / (1 + n_perm), and q, the share of AI nodes
    drawn as wished per map (mean, min, max).
    Why it is not the headline (council round 4, simulated): under this null leak - flip is about 1 - 2q, so any
    INFORMATIVE AI, however much it leaks (even 83% of the ideals), sits BELOW the null and gets p near 1; only an
    AI that places its wished statuses worse than chance gets a small p. The test that asks whether the leak
    follows the script is ``script_conditional``."""
    arm = spec_arm(spec_key)
    ex, structs = _Ex(), []
    for p in p_list:
        if p.ai.get(spec_key) is None:
            ex.add("ai_failed")
            continue
        st = _absence_structure(p, spec_key, arm, setting, min_words)
        c = _absence_counts(st, st["status"])
        if c["n_wished"] == 0:
            ex.add("no_wished_in_transcript")
        elif c["n_control"] == 0:
            ex.add("no_control_nodes")
        else:
            structs.append(st)
    out = ex.report(len(p_list), len(structs))
    out["n_perm"] = n_perm
    if not structs:
        out.update(observed_mean=_NAN, null_mean=_NAN, null_p95=_NAN, p=_NAN, observed_exceeds_null_p95=False,
                   wished_rate_q={"mean": _NAN, "min": _NAN, "max": _NAN})
        return out

    def diff(c: Counter) -> float:
        return c["leaked_as_existing"] / c["n_wished"] - c["flipped"] / c["n_control"]
    observed = _mean(diff(_absence_counts(st, st["status"])) for st in structs)
    rng = random.Random(seed)
    nulls = []
    for _ in range(n_perm):
        vals = []
        for st in structs:
            ids = list(st["status"])
            drawn = [st["status"][i] for i in ids]
            rng.shuffle(drawn)
            vals.append(diff(_absence_counts(st, dict(zip(ids, drawn)))))
        nulls.append(_mean(vals))
    nulls.sort()
    p95 = nulls[max(0, math.ceil(0.95 * len(nulls)) - 1)] if nulls else _NAN
    q = [sum(1 for v in st["status"].values() if v == "wished") / len(st["status"]) for st in structs]
    out.update(wished_rate_q={"mean": _mean(q), "min": min(q), "max": max(q)})
    out.update(observed_mean=observed, null_mean=_mean(nulls) if nulls else _NAN, null_p95=p95,
               p=(1 + sum(1 for v in nulls if v >= observed - 1e-12)) / (1 + n_perm),
               observed_exceeds_null_p95=bool(nulls and observed > p95 + 1e-12))
    return out


CONTRADICTION_NOTE = ("These shares depend on the base rate of wished statuses: an AI that rarely draws anything "
                      "as wished will contradict wished quotes often and almost never mark actual quotes as wished. "
                      "Read them together with absences.mechanism.wished_rate_q.")


def contradiction_rate(p_list: list[PairInputs], spec_key: str, *, min_words: int = 8, seed: int = 0,
                       B: int = 2000) -> dict:
    """The evidence-based form of leakage: does the AI contradict its own quotes?

    Over LOCATED AI nodes (``eligible`` in the spec's arm), per participant:
      quote_wished_marked_experienced   among nodes whose quote the judge labels wished, the share the AI drew
                                        as experienced (the leak that contradicts its own evidence),
      quote_actual_marked_wished        the mirror: among nodes whose quote the judge labels actual, the share
                                        drawn as wished.
    Both directions are reported over the SAME participants: those with at least one located wished-quoted
    AND one located actual-quoted node (``n_same_set``), so the two shares describe the same maps. Each block
    has the ``mean`` of the per-participant shares with a 95% bootstrap CI over participants and the pooled
    share over nodes. ``separate_samples`` holds the same two blocks with each direction on every participant
    that has nodes of that kind (secondary). The shares depend on the base rate of wished statuses
    (``note``): read them with ``wished_rate_q`` of ``absences.mechanism``."""
    arm, n_total = spec_arm(spec_key), len(p_list)
    cases = {"quote_wished_marked_experienced": ("wished", "experienced"),
             "quote_actual_marked_wished": ("actual", "wished")}
    per: list[dict | str] = []                     # per participant: {case: (n nodes, n contradicting)} or a reason
    for p in p_list:
        g = p.ai.get(spec_key)
        if g is None:
            per.append("ai_failed")
            continue
        pv, md, nj = p.prov[spec_key], p.modality[spec_key], _nj(p, spec_key)
        located = [n for n in g.nodes if n.node_id not in nj and eligible(pv[n.node_id], arm, min_words)[0]]
        rec = {}
        for k, (quote_mod, bad_status) in cases.items():
            sel = [n for n in located if md[n.node_id] == quote_mod]
            rec[k] = (len(sel), sum(1 for n in sel if n.status == bad_status))
        per.append(rec)

    def blocks(keep, reason) -> dict:
        out = {}
        for k in cases:
            ex, rates, tot = _Ex(), [], [0, 0]
            for rec in per:
                if isinstance(rec, str):
                    ex.add(rec)
                elif not keep(rec, k):
                    ex.add(reason(rec, k))
                else:
                    n, bad = rec[k]
                    rates.append(bad / n)
                    tot[0] += n
                    tot[1] += bad
            point, lo, hi = bootstrap_ci(rates, B=B, seed=_seed(seed, k))
            out[k] = {**ex.report(n_total, len(rates)), "mean": point, "ci_lo": lo, "ci_hi": hi,
                      "n_nodes": tot[0], "n_contradicting": tot[1], "pooled_share": _ratio(tot[1], tot[0])}
        return out

    def has_both(rec, _k):
        return rec["quote_wished_marked_experienced"][0] > 0 and rec["quote_actual_marked_wished"][0] > 0

    def why_both(rec, _k):
        return ("no_located_wished_quotes" if rec["quote_wished_marked_experienced"][0] == 0
                else "no_located_actual_quotes")
    same = blocks(has_both, why_both)
    sep = blocks(lambda rec, k: rec[k][0] > 0, lambda rec, k: f"no_located_{cases[k][0]}_quotes")
    return {"note": CONTRADICTION_NOTE, "n_same_set": same["quote_wished_marked_experienced"]["n"],
            **same, "separate_samples": sep}


# ------------------------------------------------------------------ the script

def script_share(g: MapGraph, judged_vs_script, script: MapGraph, setting: MatchSetting, *,
                 only: set[str] | None = None) -> float:
    """Share of g's nodes (only those in ``only``, if given) matched to any script concept node;
    nan if there is no node to count."""
    nodes = [n for n in g.nodes if only is None or n.node_id in only]
    if not nodes:
        return _NAN
    matched = match_nodes(g, script, setting, judged_vs_script).left_matched
    return sum(1 for n in nodes if n.node_id in matched) / len(nodes)


def build_script(prior_maps: list[MapGraph], support: int = 2, cluster_threshold: float = 0.5) -> MapGraph:
    """The generic script: concept nodes are clusters (``cluster_labels``) of the labels of ALL the
    prior-only maps given (one role, all samples), kept if members come from at least ``support``
    different maps. Label = the most frequent member label (whitespace-collapsed, exact case); ties
    go to the alphabetically first. Nodes are numbered s0, s1, ... in order of first appearance."""
    labels: list[str] = []
    owner: list[int] = []
    for k, g in enumerate(prior_maps):
        for n in g.nodes:
            labels.append(" ".join(n.text.split()))
            owner.append(k)
    ids = cluster_labels(labels, cluster_threshold)
    members: dict[int, list[int]] = {}
    for k, cid in enumerate(ids):
        members.setdefault(cid, []).append(k)
    nodes: list[Node] = []
    for cid in sorted(members):
        idx = members[cid]
        if len({owner[k] for k in idx}) < support:
            continue
        counts = Counter(labels[k] for k in idx)
        best = min(counts, key=lambda lab: (-counts[lab], lab))
        nodes.append(Node(f"s{len(nodes)}", best))
    return MapGraph("script", "prior_only", nodes, [], meta={"support": support, "n_maps": len(prior_maps)})


def _prior_key(p: PairInputs) -> str | None:
    if p.prior_spec:
        return p.prior_spec
    if p.reading3_separate:
        return None                   # reading 3 was not drawn (or failed): never a script draft in its place
    return next((k for k in sorted(p.ai) if k.startswith("prior_only__")), None)


def script_keys(p: PairInputs) -> list[str]:
    """The spec keys of ``p``'s non-failed script drafts, sorted: every prior-only map except a separate reading 3."""
    return [k for k in sorted(p.ai) if k.startswith("prior_only__") and p.ai[k] is not None
            and not (p.reading3_separate and k == p.prior_spec)]


def _relabel(g: MapGraph, labels: list[str] | None) -> MapGraph:
    """``g`` with its node texts replaced by ``labels`` (None: ``g`` itself). Labels must align with the nodes."""
    if labels is None:
        return g
    if len(labels) != len(g.nodes):
        raise ValueError(f"{g.map_id}: {len(labels)} labels for {len(g.nodes)} nodes")
    return replace(g, nodes=[replace(n, text=t) for n, t in zip(g.nodes, labels)])


def scripts_by_role(p_list: list[PairInputs], support: int = 2, canonical: bool = False,
                    missing: list[str] | None = None) -> dict[str, MapGraph]:
    """``build_script`` per role from every non-failed prior-only map of that role, in ``p_list`` order
    and sorted spec-key order. With ``canonical`` the maps are first relabelled with ``canon_prior``; a sample
    without canonical labels keeps its raw text AND is appended to ``missing`` (when given) as
    "<pid>:<prior spec key>", so the caller knows the script is not purely canonical. The pipeline must build
    the scripts for its alignment calls the same way.

    Prior-only maps are drawn per role, so several participants of a role may carry the SAME sample: a sample
    (same spec key and ``map_id``) is used once, from the first participant that carries it."""
    by_role: dict[str, list[MapGraph]] = {}
    seen: set[tuple[str, str, str]] = set()
    for p in p_list:
        maps = by_role.setdefault(p.role, [])
        for k in script_keys(p):
            if (p.role, k, p.ai[k].map_id) in seen:
                continue
            seen.add((p.role, k, p.ai[k].map_id))
            labels = (p.canon_prior or {}).get(k) if canonical else None
            if canonical and labels is None and missing is not None:
                missing.append(f"{p.pid}:{k}")
            maps.append(_relabel(p.ai[k], labels))
    return {role: build_script(maps, support) for role, maps in by_role.items()}


# ------------------------------------------------------------------ base rate and the script-conditional test

LOW_POWER_N = 30                       # fewer ideals than this in either group: reported with a low-power flag


def base_rate(p_list: list[PairInputs], spec_key: str, setting: MatchSetting, *, min_words: int = 8, seed: int = 0,
              B: int = 2000) -> dict:
    """The base-rate claim ("the AI rarely sees absences"): over the endorsed in-transcript nodes that some LOCATED
    AI node matches, per map, the share the AI drew as wished (``ai_wished_share``: some matching located node is
    wished) against the share the endorsed map drew as wished (``endorsed_wished_share``). ``mean`` = AI minus
    endorsed, paired over maps, with a map-cluster bootstrap 95% CI. ``ai_below_endorsed`` is True iff the CI is
    below 0. It says nothing about the script (see ``script_conditional``) and does not gate anything else."""
    arm, ex, a, b = spec_arm(spec_key), _Ex(), [], []
    n_nodes = n_ai_w = n_end_w = 0
    for p in p_list:
        if p.ai.get(spec_key) is None:
            ex.add("ai_failed")
            continue
        st = _absence_structure(p, spec_key, arm, setting, min_words)
        matched = [(nid, ns) for nid, ns in st["ends"] if st["loc"].get(nid)]
        if not matched:
            ex.add("no_matched_in_transcript")
            continue
        ai_w = sum(1 for nid, _ in matched if any(st["status"][x] == "wished" for x in st["loc"][nid]))
        end_w = sum(1 for _, ns in matched if ns == "wished")
        a.append(ai_w / len(matched))
        b.append(end_w / len(matched))
        n_nodes += len(matched)
        n_ai_w += ai_w
        n_end_w += end_w
    out = _paired(a, b, ex, len(p_list), _seed(seed, "baserate"), B)
    out.update(ai_wished_share=out.pop("mean_a"), endorsed_wished_share=out.pop("mean_b"),
               n_matched_nodes=n_nodes, n_ai_wished=n_ai_w, n_endorsed_wished=n_end_w,
               ai_below_endorsed=bool(out["n"] and out["ci_hi"] < 0))
    return out


@dataclass(frozen=True)
class ScriptSource:
    """One script: concept nodes per role (``build_script`` / ``scripts_by_role`` output, or any map built from
    prior-only runs with no transcript). ``family`` is "own" (the reconstructor's own prior-only runs) or "other"
    (a different model family than the reconstructor). The alignments of the endorsed labels against it come from
    ``PairInputs.judged_endorsed_vs_sources[name]`` (the "own" source falls back to ``judged_endorsed_vs_script``)."""
    name: str
    family: str
    scripts: dict[str, MapGraph]
    reason: str | None = None       # why a configured source is "own" (a ``families.script_family`` reason code)

    def __post_init__(self):
        if self.family not in ("own", "other"):
            raise ValueError(f"family must be 'own' or 'other', got {self.family!r}")

    @property
    def label(self) -> str:
        """The reconstructor's own script says so; a configured source whose family could not be established says
        THAT (it is not the reconstructor's script, only unproven to be another family's). At most 40 characters:
        the row carries the source name and the reason code (``family_reason``) beside it."""
        if self.family == "other":
            return self.name
        return "own-script (sensitivity only)" if self.name == "own" else "family not established; sensitivity"


def _source_alignment(p: PairInputs, source: ScriptSource):
    by = p.judged_endorsed_vs_sources or {}
    if source.name in by:
        return by[source.name]
    return p.judged_endorsed_vs_script if source.family == "own" else None


def _script_maps(p_list, spec_key, setting, source, min_words):
    """Per map, the matched in-transcript endorsed IDEALS (wished): [(scripted, leaked)]. An ideal is matched when
    some located AI node matches it (``absence_category``: recovered as wished, or leaked as existing); it is
    scripted when it matches a concept of the role's script under the same matcher and thresholds."""
    arm, ex, maps = spec_arm(spec_key), _Ex(), []
    for p in p_list:
        script = source.scripts.get(p.role)
        if p.ai.get(spec_key) is None:
            ex.add("ai_failed")
            continue
        if script is None or not script.nodes:
            ex.add("no_script")
            continue
        aligned = _source_alignment(p, source)
        if aligned is None:
            ex.add("alignment_missing")
            continue
        st = _absence_structure(p, spec_key, arm, setting, min_words)
        flagged = match_nodes(p.endorsed, script, setting, aligned).left_matched
        units = []
        for nid, ns in st["ends"]:
            if ns != "wished":
                continue
            cat, _ = absence_category(st, st["status"], nid)
            if cat in ("recovered_as_wished", "leaked_as_existing"):
                units.append((nid in flagged, cat == "leaked_as_existing"))
        if units:
            maps.append(units)
        else:
            ex.add("no_matched_ideals")
    return maps, ex


def _pooled_diff(counts) -> float:
    """Pooled leak rate (scripted) minus pooled leak rate (unscripted) from per-map (ns, ls, nu, lu); nan if a group is empty."""
    ns = sum(c[0] for c in counts)
    nu = sum(c[2] for c in counts)
    if not ns or not nu:
        return _NAN
    return sum(c[1] for c in counts) / ns - sum(c[3] for c in counts) / nu


def _map_counts(units):
    sc = [lk for s, lk in units if s]
    un = [lk for s, lk in units if not s]
    return len(sc), sum(sc), len(un), sum(un)


def script_conditional_row(p_list: list[PairInputs], spec_key: str, setting: MatchSetting, source: ScriptSource, *,
                           n_perm: int = 10000, seed: int = 0, B: int = 2000, min_words: int = 8) -> dict:
    """The script-conditional test ("pull toward the script") for one script source.

    Population: the matched, in-transcript, endorsed IDEALS (wished) of the maps that have an AI map, a script
    for their role and an alignment. An ideal LEAKS if the AI renders it as existing (``leaked_as_existing``) and
    is SCRIPTED if it matches a concept of the source's script (same matcher and thresholds as the main
    matching). Statistic = pooled leak rate of the scripted ideals minus that of the unscripted ones.
    Null: the script flag permuted WITHIN each map (per-map counts kept), ``n_perm`` times with
    ``random.Random(seed)``; one-sided (greater), p = (1 + #{null >= observed}) / (1 + n_perm). The status of the AI
    nodes plays no part, so the null is zero whatever the AI's overall leak rate: it asks only whether the leak
    follows the script. CI: percentile bootstrap resampling MAPS (clusters) ``B`` times; a resample with an empty
    group is skipped (``n_boot_valid``). ``low_power`` is True when either group has fewer than 30 ideals.
    The row is eligible to carry the headline flag only for an "other"-family source (``headline_eligible``)."""
    maps, ex = _script_maps(p_list, spec_key, setting, source, min_words)
    out = {"source": source.name, "label": source.label, "family": source.family,
           **ex.report(len(p_list), len(maps)), "alternative": "greater", "n_perm": n_perm, "seed": seed, "B": B,
           "headline_eligible": source.family == "other"}
    if source.name != "own" and source.reason and source.family == "own":
        out["family_reason"] = source.reason
    counts = [_map_counts(u) for u in maps]
    ns, ls = sum(c[0] for c in counts), sum(c[1] for c in counts)
    nu, lu = sum(c[2] for c in counts), sum(c[3] for c in counts)
    est = _pooled_diff(counts)
    out.update(n_scripted=ns, n_unscripted=nu, n_leaked_scripted=ls, n_leaked_unscripted=lu,
               leak_rate_scripted=_ratio(ls, ns), leak_rate_unscripted=_ratio(lu, nu), estimate=est,
               low_power=bool(ns < LOW_POWER_N or nu < LOW_POWER_N))
    if math.isnan(est):
        out.update(p_perm=_NAN, ci_lo=_NAN, ci_hi=_NAN, n_boot_valid=0)
        return out
    # within-map permutation of the script flag: only maps with both kinds of ideal can change the statistic
    leaks_by_map = [[lk for _, lk in u] for u, c in zip(maps, counts) if 0 < c[0] < len(u)]
    ks = [c[0] for c in counts if c[0] and c[2]]
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        l_s = sum(sum(rng.sample(lks, k)) for lks, k in zip(leaks_by_map, ks))
        l_s += sum(c[1] for c in counts if not (c[0] and c[2]))         # maps with one kind only: fixed
        if l_s >= ls:
            ge += 1
    out["p_perm"] = (1 + ge) / (1 + n_perm)
    rng = random.Random(_seed(seed, "scriptboot"))
    m, boots = len(counts), []
    for _ in range(B):
        d = _pooled_diff([counts[rng.randrange(m)] for _ in range(m)])
        if not math.isnan(d):
            boots.append(d)
    out["n_boot_valid"] = len(boots)
    out["ci_lo"], out["ci_hi"] = _percentile_ci(boots, 0.05) if boots else (_NAN, _NAN)
    return out


def script_conditional(p_list: list[PairInputs], primary_spec: str, specs: list[str], settings: list[MatchSetting],
                       primary_setting: MatchSetting, sources: list[ScriptSource], *, n_perm: int = 10000,
                       seed: int = 0, B: int = 2000, min_words: int = 8, alpha: float = 0.05) -> dict:
    """One ``script_conditional_row`` per source, keyed by source name, for the primary spec and setting, plus a
    ``sign_stability`` of each estimate over settings x specs (reported, not gating).

    PRIMARY source = the first "other"-family source (a script from a model family that is not the reconstructor);
    the reconstructor's own script is a sensitivity row, labelled "own-script (sensitivity only)", and never sets
    the flag. ``claim_script_conditional_leakage`` = the primary source's permutation p < ``alpha`` AND its bootstrap
    CI lower bound > 0; False (``primary_source`` None) when there is no other-family source. ``low_power`` of the
    primary row is repeated next to the flag: a flag on a thin sample is a flag, and says so."""
    rows, order = {}, []
    for k, src in enumerate(sources):
        if src.name in rows:
            raise ValueError(f"two script sources named {src.name!r}")
        row = script_conditional_row(p_list, primary_spec, primary_setting, src, n_perm=n_perm,
                                     seed=_seed(seed, f"scriptperm{src.name}"), B=B, min_words=min_words)
        pkey = f"{primary_spec}|{primary_setting.key()}"

        def cell(s, st, src=src):
            mp, _ = _script_maps(p_list, s, st, src, min_words)
            c = [_map_counts(u) for u in mp]
            return _pooled_diff(c), len(c)
        row["sign_stability"] = _stab(specs, settings, cell, pkey)
        rows[src.name] = row
        order.append(src.name)
    primary = next((s.name for s in sources if s.family == "other"), None)
    prow = rows.get(primary) if primary else None
    flag = bool(prow and prow["p_perm"] < alpha and prow["ci_lo"] > 0)
    return {"primary_source": primary, "alpha": alpha, "n_perm": n_perm, "seed": seed, "rows": rows,
            "claim_script_conditional_leakage": flag, "primary_low_power": prow["low_power"] if prow else None}


def same_sign(a, b) -> bool:
    """True iff ``a`` and ``b`` are finite, neither is zero (|x| <= 1e-12 is zero) and they have the same sign. The
    rule behind ``symptom_of_script``: a difference AI - endorsed counts as a symptom of the pull toward the script
    only if prior-only - endorsed points the same way."""
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    if not (math.isfinite(a) and math.isfinite(b)) or abs(a) <= 1e-12 or abs(b) <= 1e-12:
        return False
    return (a > 0) == (b > 0)


def _assigned_priors(p_list: list[PairInputs]) -> list[tuple[MapGraph | None, list[str] | None, tuple | None]]:
    """One prior-only sample per participant, for the "prior-only minus endorsed" side of the symptom rule:
    (map, canonical labels or None, sample identity (role, spec key, map_id)) or (None, None, None). Prior-only
    samples are drawn per role and sit on every participant of the role, so they are handed out round robin by role
    (participant k of a role gets sample k mod n, samples in sorted spec-key order); a fixed rule, no tuning. Two
    participants that end up with the SAME sample share its identity (``homogenization`` leaves such pairs out)."""
    seen: Counter = Counter()
    out = []
    for p in p_list:
        keys = script_keys(p)
        if not keys:
            out.append((None, None, None))
            continue
        k = keys[seen[p.role] % len(keys)]
        seen[p.role] += 1
        g = p.ai[k]
        out.append((g, (p.canon_prior or {}).get(k), (p.role, k, g.map_id)))
    return out


# ------------------------------------------------------------------ homogenization / linearization

def _features(g: MapGraph, glued_only: bool) -> dict[str, float]:
    s = structure(g, glued_only)
    out = {k: float(v) for k, v in s.items() if k != "type_shares"}
    out.update({f"type_share:{t}": float(v) for t, v in s["type_shares"].items()})
    return out


def _mean_pairs(M, mask) -> float:
    n = len(M)
    return _mean(M[i][j] for i in range(n) for j in range(i + 1, n) if mask is None or mask[i][j])


def _pair_block(Ma, Mb, mask, seed: int, B: int, what: str) -> dict:
    """``what`` names the pairwise quantity: the means come out as ``mean_<what>_a`` / ``mean_<what>_b``."""
    n = len(Ma)
    n_pairs = sum(1 for i in range(n) for j in range(i + 1, n) if mask is None or mask[i][j])
    point, lo, hi = pairwise_bootstrap_ci(Ma, Mb, B=B, seed=seed, mask=mask)
    return {"n_pairs": n_pairs, f"mean_{what}_a": _mean_pairs(Ma, mask), f"mean_{what}_b": _mean_pairs(Mb, mask),
            "diff": point, "ci_lo": lo, "ci_hi": hi}


def homogenization(maps_a: list[MapGraph | None], maps_b: list[MapGraph | None],
                   canon_a: list[list[str]], canon_b: list[list[str]],
                   interviewers: list[str | None], seed: int = 0, *, B: int = 2000,
                   sample_ids: list | None = None) -> dict:
    """Are set A (AI maps) more alike one another than set B (endorsed maps)? Index i is participant i
    in all lists. A participant with a failed or empty map is excluded from both sets.

    concept: each map becomes the set of cluster ids of its canonical labels (``cluster_labels`` over all
    labels of both sets pooled); mean pairwise Jaccard within each set (higher = more homogeneous), ``diff``
    = A - B with a bootstrap over participants.
    structure: ``structure()`` vectors (glued edges, shares and counts, one entry per type), z-scored over
    both sets pooled (a constant feature is dropped); mean pairwise Euclidean distance within each set
    (lower = more homogeneous), ``diff`` = A - B.
    by_interviewer: the same on pairs of participants who share an interviewer ("same") and on pairs with
    two known, different interviewers ("different"); pairs with an unknown interviewer are in neither.
    ``sample_ids`` (one entry per participant, None = no id): two participants with the same non-None id carry
    the very same map in set A (a shared prior-only sample), so their pair is left out of every mean, in both sets."""
    n_total = len(maps_a)
    if not (len(maps_b) == len(canon_a) == len(canon_b) == len(interviewers) == n_total):
        raise ValueError("homogenization inputs must all have one entry per participant")
    if sample_ids is not None and len(sample_ids) != n_total:
        raise ValueError("sample_ids must have one entry per participant")
    ex = _Ex()
    keep: list[int] = []
    for i, (a, b) in enumerate(zip(maps_a, maps_b)):
        if a is None:
            ex.add("ai_failed")
        elif b is None:
            ex.add("endorsed_missing")
        elif not a.nodes or not b.nodes:
            ex.add("empty_map")
        else:
            keep.append(i)
    n = len(keep)
    out = ex.report(n_total, n)
    if n < 2:
        def nb(what):
            return {"n_pairs": 0, f"mean_{what}_a": _NAN, f"mean_{what}_b": _NAN, "diff": _NAN,
                    "ci_lo": _NAN, "ci_hi": _NAN}
        empty = {"n_pairs": 0, "concept": nb("jaccard"), "structure": nb("distance")}
        out.update(concept=nb("jaccard"), structure=nb("distance"),
                   by_interviewer={"same": dict(empty), "different": dict(empty), "n_pairs_unknown_excluded": 0})
        return out
    for i in keep:
        if len(canon_a[i]) != len(maps_a[i].nodes) or len(canon_b[i]) != len(maps_b[i].nodes):
            raise ValueError(f"participant {i}: canonical labels must align with the map's nodes")
    pooled = [lab for i in keep for lab in canon_a[i]] + [lab for i in keep for lab in canon_b[i]]
    ids = cluster_labels(pooled)
    pos = 0
    sets_a, sets_b = [], []
    for i in keep:
        sets_a.append(set(ids[pos:pos + len(canon_a[i])]))
        pos += len(canon_a[i])
    for i in keep:
        sets_b.append(set(ids[pos:pos + len(canon_b[i])]))
        pos += len(canon_b[i])

    def jac(x: set, y: set) -> float:
        return len(x & y) / len(x | y)
    Ja = [[jac(sets_a[i], sets_a[j]) for j in range(n)] for i in range(n)]
    Jb = [[jac(sets_b[i], sets_b[j]) for j in range(n)] for i in range(n)]

    fa = [_features(maps_a[i], True) for i in keep]
    fb = [_features(maps_b[i], True) for i in keep]
    names = sorted(fa[0])
    allv = fa + fb
    zs = {}
    for k in names:
        col = [v[k] for v in allv]
        mu = _mean(col)
        sd = math.sqrt(_mean((x - mu) ** 2 for x in col))
        zs[k] = (mu, sd)
    live = [k for k in names if zs[k][1] > 1e-12]

    def vec(f):
        return [(f[k] - zs[k][0]) / zs[k][1] for k in live]
    va, vb = [vec(f) for f in fa], [vec(f) for f in fb]

    def dist(u, v):
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(u, v)))
    Da = [[dist(va[i], va[j]) for j in range(n)] for i in range(n)]
    Db = [[dist(vb[i], vb[j]) for j in range(n)] for i in range(n)]

    iv = [interviewers[i] for i in keep]
    sid = [sample_ids[i] for i in keep] if sample_ids is not None else [None] * n
    ok = [[sid[i] is None or sid[i] != sid[j] for j in range(n)] for i in range(n)]
    all_ok = None if all(ok[i][j] for i in range(n) for j in range(n) if i != j) else ok
    same = [[iv[i] is not None and iv[i] == iv[j] and ok[i][j] for j in range(n)] for i in range(n)]
    diff = [[iv[i] is not None and iv[j] is not None and iv[i] != iv[j] and ok[i][j] for j in range(n)]
            for i in range(n)]
    out["concept"] = _pair_block(Ja, Jb, all_ok, _seed(seed, "concept"), B, "jaccard")
    out["structure"] = _pair_block(Da, Db, all_ok, _seed(seed, "structure"), B, "distance")
    out["n_structure_features"] = len(live)
    by = {}
    for name, mask in (("same", same), ("different", diff)):
        by[name] = {"n_pairs": sum(1 for i in range(n) for j in range(i + 1, n) if mask[i][j]),
                    "concept": _pair_block(Ja, Jb, mask, _seed(seed, "concept" + name), B, "jaccard"),
                    "structure": _pair_block(Da, Db, mask, _seed(seed, "structure" + name), B, "distance")}
    by["n_pairs_unknown_excluded"] = sum(1 for i in range(n) for j in range(i + 1, n)
                                         if (iv[i] is None or iv[j] is None) and ok[i][j])     # in neither group
    out["by_interviewer"] = by
    return out


def linearization(ai_maps: list[MapGraph | None], endorsed_maps: list[MapGraph | None], glued_only: bool,
                  seed: int = 0, *, B: int = 2000) -> dict:
    """Per structure measure, the paired difference (AI - endorsed) over participants: mean, bootstrap CI
    (pairs resampled) and exact sign test; always two-sided (no direction is assumed). Participants with
    a failed or empty map are excluded. Type shares appear as ``type_share:<type>``."""
    if len(ai_maps) != len(endorsed_maps):
        raise ValueError("one AI map and one endorsed map per participant")
    ex = _Ex()
    rows = []
    for a, e in zip(ai_maps, endorsed_maps):
        if a is None:
            ex.add("ai_failed")
        elif e is None:
            ex.add("endorsed_missing")
        elif not a.nodes or not e.nodes:
            ex.add("empty_map")
        else:
            rows.append((_features(a, glued_only), _features(e, glued_only)))
    out = {**ex.report(len(ai_maps), len(rows)), "alternative": "two-sided", "glued_only": glued_only,
           "measures": {}}
    names = sorted(rows[0][0]) if rows else sorted(_features(MapGraph("x", "ai"), glued_only))
    for k in names:
        a = [r[0][k] for r in rows]
        b = [r[1][k] for r in rows]
        point, lo, hi = paired_bootstrap_ci(a, b, B=B, seed=_seed(seed, "lin" + k))
        out["measures"][k] = {"mean_ai": _mean(a), "mean_endorsed": _mean(b), "mean_diff": point,
                              "ci_lo": lo, "ci_hi": hi, "sign": sign_test([x - y for x, y in zip(a, b)])}
    return out


# ------------------------------------------------------------------ leakage across arms

def _elements(p: PairInputs, spec: str):
    """AI elements of the map: every node, and every edge that has both a provenance and a modality entry."""
    g = p.ai[spec]
    pv, md = p.prov[spec], p.modality[spec]
    for n in g.nodes:
        yield n.node_id, n.status, pv[n.node_id], md[n.node_id]
    for e in g.edges:
        eid = f"e:{e.source}->{e.target}"
        if eid in pv and eid in md:
            yield eid, e.status, pv[eid], md[eid]


def _leak_inputs(p_list, setting, full_spec, removed_spec, min_words):
    rows, ex_all = [], _Ex()
    for p in p_list:
        if p.ai.get(full_spec) is None or p.ai.get(removed_spec) is None:
            ex_all.add("ai_failed")
            continue
        rows.append({
            "bf": buckets(p, full_spec, "full", setting, min_words=min_words),
            "br": buckets(p, removed_spec, "removed", setting, min_words=min_words),
            "af": absences(p, full_spec, "full", setting, min_words=min_words),
            "ar": absences(p, removed_spec, "removed", setting, min_words=min_words)})
    return rows, ex_all


def _recall_diff_rows(rows, ex_all):
    a, b, ex = [], [], _Ex()
    ex.c.update(ex_all.c)
    for r in rows:
        x, y = r["bf"]["node_recall"], r["br"]["node_recall"]
        if math.isnan(x) or math.isnan(y):
            ex.add("no_in_transcript_endorsed")
        else:
            a.append(x)
            b.append(y)
    return a, b, ex


def leakage(p_list: list[PairInputs], setting: MatchSetting, *, full_spec: str = FULL_SPEC,
            removed_spec: str | None = None, min_words: int = 8, seed: int = 0, B: int = 2000) -> dict:
    """Does the mapping talk leak into the AI map? Full arm vs the arm with interviewer and mapping-talk
    turns removed (default ``removed_spec``: the full spec with its arm swapped), per map:
      node_recall_diff   node_recall(full) - node_recall(removed)   (mean_a = full, mean_b = removed)
      leak_rate_diff     leak rate of the absences(full) - that of absences(removed)
      contaminated_share of the full arm's attested AI elements (nodes, and edges that have a provenance
                         and a modality entry), the share whose quote is interviewer-voiced or sits in
                         a mapping-talk turn: pooled over maps, and mean per map."""
    removed_spec = removed_spec or swap_arm(full_spec, "removed")
    rows, ex_all = _leak_inputs(p_list, setting, full_spec, removed_spec, min_words)
    n_total = len(p_list)
    a, b, ex = _recall_diff_rows(rows, ex_all)
    out = {"full_spec": full_spec, "removed_spec": removed_spec,
           "node_recall_diff": _paired(a, b, ex, n_total, _seed(seed, "leak_recall"), B)}
    la, lb, ex2 = [], [], _Ex()
    ex2.c.update(ex_all.c)
    for r in rows:
        x, y = r["af"]["leak_rate"], r["ar"]["leak_rate"]
        if math.isnan(x) or math.isnan(y):
            ex2.add("no_wished_in_transcript")
        else:
            la.append(x)
            lb.append(y)
    out["leak_rate_diff"] = _paired(la, lb, ex2, n_total, _seed(seed, "leak_rate"), B)
    ex3, shares, n_att, n_bad = _Ex(), [], 0, 0
    for p in p_list:
        if p.ai.get(full_spec) is None:
            ex3.add("ai_failed")
            continue
        nj = _nj(p, full_spec)
        att = [(pr.speaker == "interviewer" or pr.mapping_talk_turn)
               for eid, status, pr, mod in _elements(p, full_spec)
               if eid not in nj and eligible(pr, "full", min_words)[0] and attested(status, mod)]
        n_att += len(att)
        n_bad += sum(att)
        if att:
            shares.append(sum(att) / len(att))
        else:
            ex3.add("no_attested_elements")
    out["contaminated_share"] = {**ex3.report(n_total, len(shares)), "n_attested_elements": n_att,
                                 "n_contaminated": n_bad, "pooled_share": _ratio(n_bad, n_att),
                                 "mean_map_share": _mean(shares)}
    return out


# ------------------------------------------------------------------ controls

def _tok(texts: list[str]) -> list[frozenset]:
    return [_tok1(t) for t in texts]


def _raw_labels(p: PairInputs, spec: str) -> tuple[list[str], list[str]]:
    ai = p.ai[spec]
    ca = (p.canon_ai or {}).get(spec)
    ce = p.canon_endorsed
    return (ca if ca is not None else [n.text for n in ai.nodes],
            ce if ce is not None else [n.text for n in p.endorsed.nodes])


def _map_similarity(ai_raw, ai_can, en_raw, en_can) -> float:
    """Soft F: precision = mean over AI nodes of the best similarity to an endorsed node, recall = the
    same from the endorsed side, F = harmonic mean (0 if both 0). A node pair's similarity is the larger
    of its raw-text and its canonical-label token Jaccard."""
    sim = [[max(_jacc(ai_raw[i], en_raw[j]), _jacc(ai_can[i], en_can[j])) for j in range(len(en_raw))]
           for i in range(len(ai_raw))]
    prec = _mean(max(row) for row in sim)
    rec = _mean(max(sim[i][j] for i in range(len(sim))) for j in range(len(en_raw)))
    return 0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec)


def _shuffled_pairs_rows(p_list, spec, setting, perms_seed, n_derangements, min_words, cache):
    """(real recalls, shuffled recalls, exclusions, n_included) with LEXICAL-ONLY matching (no judge)."""
    arm = spec_arm(spec)
    ex, included = _Ex(), []
    for p in p_list:
        if p.ai.get(spec) is None:
            ex.add("ai_failed")
        else:
            included.append(p)
    n_total = len(p_list)
    if len(included) < 2:
        for _ in included:
            ex.add("too_few_participants")
        return [], [], ex, n_total
    perms = derangements(len(included), n_derangements, perms_seed)
    thr = setting.lex_threshold

    def recall(i: int, j: int) -> float:
        pi, pj = included[i], included[j]
        if (i, j) not in cache.setdefault(spec, {}):
            ta = _tok([n.text for n in pi.ai[spec].nodes])
            te = _tok([n.text for n in pj.endorsed.nodes])
            cache[spec][(i, j)] = [[_jacc(x, y) for y in te] for x in ta]
        sims = cache[spec][(i, j)]
        att = _attested_nodes(pi, spec, arm, min_words)
        ai_nodes = pi.ai[spec].nodes
        hit = {e for a, row in enumerate(sims) if ai_nodes[a].node_id in att
               for e, v in enumerate(row) if v >= thr}
        ends = pj.endorsed.nodes
        ids = [k for k, n in enumerate(ends) if pj.in_transcript[n.node_id]]
        return _ratio(sum(1 for k in ids if k in hit), len(ids))
    real, shuf = [], []
    for i in range(len(included)):
        r = recall(i, i)
        s = _nonnan([recall(i, perm[i]) for perm in perms])
        if math.isnan(r):
            ex.add("no_in_transcript_endorsed")
        elif not s:
            ex.add("no_in_transcript_endorsed_shuffled")
        else:
            real.append(r)
            shuf.append(_mean(s))
    return real, shuf, ex, n_total


def controls(p_list: list[PairInputs], setting: MatchSetting, *, spec_key: str = PRIMARY_SPEC,
             n_derangements: int = 20, seed: int = 0, min_words: int = 8, B: int = 2000) -> dict:
    """Three controls that should break the effect if it is real.

    shuffled_pairs   node recall of AI map i against the endorsed map of ANOTHER participant (``n_derangements``
                     seeded derangements, lexical-only matching at the setting's lexical threshold, attested AI
                     nodes only, endorsed in-transcript nodes of the partner), against the same measure for the
                     true pair. ``mean_a`` = real, ``mean_b`` = shuffled; ``mean`` = real - shuffled.
    permutation_rank rank of the own endorsed map when AI map i is compared with every endorsed map
                     (``_map_similarity`` over raw and canonical labels); ``canonical_used`` is the label half of
                     ``_canonical_audit`` (the audit behind the pull claim, here for this spec): canonical labels
                     of the endorsed maps, of every AI map that exists, and of every prior-only sample;
                     ``canonical_fallback_reasons`` lists what is missing. See ``stats.permutation_rank``.
    shuffled_quotes  modality-attested share of the AI nodes (modality backs the drawn status, no eligibility
                     rule) from the real judge vs the quote-shuffled re-judge (``modality_shuffled``);
                     ``mean_a`` = real, ``mean_b`` = shuffled."""
    n_total = len(p_list)
    real, shuf, ex, _ = _shuffled_pairs_rows(p_list, spec_key, setting, _seed(seed, "derange"), n_derangements,
                                             min_words, {})
    sp = _paired(real, shuf, ex, n_total, _seed(seed, "shuffled_pairs"), B)
    sp.update(real_recall_mean=sp["mean_a"], shuffled_recall_mean=sp["mean_b"], n_derangements=n_derangements)

    ex2, sims_idx = _Ex(), []
    for k, p in enumerate(p_list):
        g = p.ai.get(spec_key)
        if g is None:
            ex2.add("ai_failed")
        elif not g.nodes or not p.endorsed.nodes:
            ex2.add("empty_map")
        else:
            sims_idx.append(k)
    pr = {"mean_rank": _NAN, "expected_rank_null": _NAN, "share_rank1": _NAN, "p": _NAN, "n": len(sims_idx)}
    missing_prior: list[str] = []
    scripts_by_role(p_list, canonical=True, missing=missing_prior)
    audit = _canonical_audit(p_list, [spec_key], missing_prior)          # the same audit the pull claim uses
    canonical = audit["canonical_used"]
    if len(sims_idx) >= 2:
        toks = []
        for k in sims_idx:
            p = p_list[k]
            ai_can, en_can = _raw_labels(p, spec_key)
            toks.append((_tok([n.text for n in p.ai[spec_key].nodes]), _tok(ai_can),
                         _tok([n.text for n in p.endorsed.nodes]), _tok(en_can)))
        m = len(sims_idx)
        sim = [[_map_similarity(toks[i][0], toks[i][1], toks[j][2], toks[j][3]) for j in range(m)]
               for i in range(m)]
        pr = permutation_rank(sim, seed=_seed(seed, "perm_rank"))
    else:
        for _ in sims_idx:
            ex2.add("too_few_participants")
        pr["n"] = 0
    pr.update(ex2.report(n_total, pr["n"] if len(sims_idx) >= 2 else 0), canonical_used=canonical,
              canonical_fallback_reasons=[r for r in audit["fallback_reasons"] if not r.startswith("judged_")])

    ex3, ra, rb = _Ex(), [], []
    for p in p_list:
        g = p.ai.get(spec_key)
        if g is None:
            ex3.add("ai_failed")
        elif not g.nodes:
            ex3.add("empty_map")
        elif not p.modality_shuffled or spec_key not in p.modality_shuffled:
            ex3.add("no_shuffled_judge")
        elif not [n for n in g.nodes if n.node_id not in _nj(p, spec_key) | _nj_shuffled(p, spec_key)]:
            ex3.add("no_judged_nodes")
        else:
            skip = _nj(p, spec_key) | _nj_shuffled(p, spec_key)
            judged = [n for n in g.nodes if n.node_id not in skip]
            ra.append(_mean(attested(n.status, p.modality[spec_key][n.node_id]) for n in judged))
            rb.append(_mean(attested(n.status, p.modality_shuffled[spec_key][n.node_id]) for n in judged))
    sq = _paired(ra, rb, ex3, n_total, _seed(seed, "shuffled_quotes"), B)
    sq.update(real_share_mean=sq["mean_a"], shuffled_share_mean=sq["mean_b"])
    return {"spec": spec_key, "shuffled_pairs": sp, "permutation_rank": pr, "shuffled_quotes": sq}


# ------------------------------------------------------------------ modality sources

def with_keyword_modality(p: PairInputs) -> PairInputs:
    """A copy of ``p`` whose modality labels come from ``keyword_modality`` applied to each element's quote
    (the no-AI arm). Prior-only maps have no quotes and are left out; the original is not modified."""
    mod: dict[str, dict[str, str]] = {}
    for spec, g in p.ai.items():
        if g is None or spec_arm(spec) == "prior_only":
            continue
        m = {n.node_id: keyword_modality(n.quote) for n in g.nodes}
        m.update({f"e:{e.source}->{e.target}": keyword_modality(e.quote) for e in g.edges})
        mod[spec] = m
    return replace(p, modality=mod, not_judged=None)          # keyword modality is total: nothing is unjudged


def _as_judge2(p: PairInputs) -> PairInputs:
    """``p`` with the second judge's labels and not-judged ids in place of the first judge's."""
    return replace(p, modality=p.modality_judge2, not_judged=p.not_judged_judge2)


def _covers(p: PairInputs, mod: dict[str, dict[str, str]] | None, specs: list[str]) -> bool:
    if mod is None:
        return False
    for s in specs:
        g = p.ai.get(s)
        if g is not None and not (s in mod and all(n.node_id in mod[s] for n in g.nodes)):
            return False
    return True


# ------------------------------------------------------------------ headline

def _absence_rows(p_list, spec, setting, min_words):
    arm, ex, rows = spec_arm(spec), _Ex(), []
    for p in p_list:
        if p.ai.get(spec) is None:
            ex.add("ai_failed")
            continue
        a = absences(p, spec, arm, setting, min_words=min_words)
        if a["n_wished"] == 0:
            ex.add("no_wished_in_transcript")
        elif a["n_control"] == 0:
            ex.add("no_control_nodes")
        else:
            rows.append(a)
    return rows, ex


def _pooled_absences(p_list, spec, setting, min_words) -> dict:
    arm, ex, maps = spec_arm(spec), _Ex(), []
    for p in p_list:
        if p.ai.get(spec) is None:
            ex.add("ai_failed")
        else:
            maps.append(absences(p, spec, arm, setting, min_words=min_words))
    tot = {k: sum(a[k] for a in maps) for k in ABSENCE_COUNTS}
    return {**ex.report(len(p_list), len(maps)), **tot,
            "recovery_rate": _ratio(tot["recovered_as_wished"], tot["n_wished"]),
            "leak_rate": _ratio(tot["leaked_as_existing"], tot["n_wished"]),
            "leak_quote_wished_rate": _ratio(tot["leaked_quote_wished"], tot["n_wished"]),
            "counterfactual_leakage_rate": _ratio(tot["counterfactual_leakage"], tot["n_wished"]),
            "adequacy_flattening_rate": _ratio(tot["adequacy_flattening"], tot["n_wished"]),
            "leaked_unanchored_rate": _ratio(tot["leaked_unanchored"], tot["n_wished"]),
            "miss_rate": _ratio(tot["missed"], tot["n_wished"]),
            "flip_rate": _ratio(tot["flipped"], tot["n_control"])}


def _leak_split_block(p_list, spec, setting, pooled, min_words, seed, B) -> dict:
    """Pooled counts and rates of the leak split (``leak_split``), plus the mean per-map rate with a map-cluster
    bootstrap CI. ``share_of_leaks`` is the share among the ideals that leaked (nan when none did)."""
    arm = spec_arm(spec)
    n_leaked, nw = pooled["leaked_as_existing"], pooled["n_wished"]
    out = {"n_leaked": n_leaked, "n_wished": nw}
    for name in ("counterfactual_leakage", "adequacy_flattening", "leak_split_indeterminate"):
        rates = []
        for p in p_list:
            if p.ai.get(spec) is None:
                continue
            a = absences(p, spec, arm, setting, min_words=min_words)
            if a["n_wished"]:
                rates.append(a[name] / a["n_wished"])
        point, lo, hi = bootstrap_ci(rates, B=B, seed=_seed(seed, name))
        out[name] = {"count": pooled[name], "rate_of_wished": _ratio(pooled[name], nw),
                     "share_of_leaks": _ratio(pooled[name], n_leaked),
                     "map_mean_rate": point, "ci_lo": lo, "ci_hi": hi, "n_maps": len(rates)}
    return out


PULL_VARIANTS = ("canonical", "raw", "judge_only")


def _pull_rows(p_list, spec, setting, scripts, in_transcript_only, variant="canonical"):
    """Per map (script share of the AI map, of the endorsed map). Variants:
      canonical   (primary) all three sources are matched on canonical labels (``canon_ai``, ``canon_endorsed``,
                  and the script built from ``canon_prior``); raw text stands in where a label list is missing;
                  alignments come from the ``*_canon`` fields (none: lexical only).
      raw         raw labels, raw script, raw alignments.
      judge_only  as raw, but the lexical channel is switched off (threshold above 1): only the judge matches."""
    if variant not in PULL_VARIANTS:
        raise ValueError(f"variant must be one of {PULL_VARIANTS}")
    canon = variant == "canonical"
    if variant == "judge_only":
        setting = MatchSetting(1.01, setting.judge_accept)
    ex, a, b = _Ex(), [], []
    for p in p_list:
        g = p.ai.get(spec)
        script = scripts.get(p.role)
        if g is None:
            ex.add("ai_failed")
        elif script is None or not script.nodes:
            ex.add("no_script")
        elif not g.nodes:
            ex.add("ai_empty")
        elif not p.endorsed.nodes:
            ex.add("endorsed_empty")
        else:
            only = {n.node_id for n in p.endorsed.nodes if p.in_transcript[n.node_id]} if in_transcript_only else None
            if only is not None and not only:
                ex.add("no_in_transcript_endorsed")
                continue
            if canon:
                ga = _relabel(g, (p.canon_ai or {}).get(spec))
                ge = _relabel(p.endorsed, p.canon_endorsed)
                ja = (p.judged_ai_vs_script_canon or {}).get(spec)
                je = p.judged_endorsed_vs_script_canon
            else:
                ga, ge, ja, je = g, p.endorsed, p.judged_ai_vs_script.get(spec), p.judged_endorsed_vs_script
                if ja is None or je is None:
                    ex.add("alignment_missing")
                    continue
            a.append(script_share(ga, ja, script, setting))
            b.append(script_share(ge, je, script, setting, only=only))
    return a, b, ex


def _prior_rows(p_list, spec, setting, min_words, attested_ai=False):
    """Per map: (AI recall, prior-only recall) of the in-transcript endorsed nodes. Like-for-like by default:
    AI recall counts a match by ANY AI node and prior recall a match by ANY prior node (neither is checked
    against the transcript). With ``attested_ai`` the AI side is the attested ``node_recall`` of ``buckets``."""
    arm, ex, a, b = spec_arm(spec), _Ex(), [], []
    for p in p_list:
        g = p.ai.get(spec)
        pk = _prior_key(p)
        prior = p.ai.get(pk) if pk else None
        if g is None:
            ex.add("ai_failed")
            continue
        if prior is None:
            ex.add("no_prior_map")
            continue
        if p.judged_prior_vs_endorsed is None:
            ex.add("alignment_missing")
            continue
        ids = {n.node_id for n in p.endorsed.nodes if p.in_transcript[n.node_id]}
        if attested_ai:
            ai_recall = buckets(p, spec, arm, setting, min_words=min_words)["node_recall"]
        else:
            ai_recall = _ratio(len(ids & _matching(p, spec, setting).right_matched), len(ids))
        if math.isnan(ai_recall) or not ids:
            ex.add("no_in_transcript_endorsed")
            continue
        got = match_nodes(prior, p.endorsed, setting, p.judged_prior_vs_endorsed).right_matched
        a.append(ai_recall)
        b.append(len(ids & got) / len(ids))
    return a, b, ex


def _canonical_audit(p_list: list[PairInputs], specs: list[str], missing_prior: list[str]) -> dict:
    """Is the primary pull estimate really canonical? Checked over EVERY participant, EVERY spec of the
    stability grid (where that participant has an AI map) and EVERY prior-only sample behind the script.
    A None alignment is a missing input; an empty dict is a supplied "nothing matched"."""
    counts: Counter = Counter()
    supplied = False
    for p in p_list:
        supplied |= any(x is not None for x in (p.canon_endorsed, p.canon_ai, p.canon_prior,
                                                p.judged_ai_vs_script_canon, p.judged_endorsed_vs_script_canon))
        if p.canon_endorsed is None:
            counts["canon_endorsed_missing"] += 1
        if p.judged_endorsed_vs_script_canon is None:
            counts["judged_endorsed_vs_script_canon_missing"] += 1
        for sp in specs:
            if p.ai.get(sp) is None:
                continue
            if (p.canon_ai or {}).get(sp) is None:
                counts[f"canon_ai_missing:{sp}"] += 1
            if (p.judged_ai_vs_script_canon or {}).get(sp) is None:
                counts[f"judged_ai_vs_script_canon_missing:{sp}"] += 1
    if missing_prior:
        counts["canon_prior_missing"] = len(missing_prior)
    label_missing = {k for k in counts if not k.startswith("judged_")}
    judge_missing = {k for k in counts if k.startswith("judged_")}
    canonical_used, judge_ok = not label_missing, not judge_missing
    source = "canonical" if canonical_used and judge_ok else ("mixed" if supplied else "raw_fallback")
    return {"canonical_used": canonical_used, "judge_channel_canonical": judge_ok, "label_source": source,
            "fallback_reasons": sorted(counts), "fallback_counts": dict(sorted(counts.items())),
            "missing_prior_samples": sorted(missing_prior)}


def _rename_reason(reasons: dict, old: str, new: str) -> dict:
    return {(new if k == old else k): v for k, v in reasons.items()}


def _hom_label_source(p_list: list[PairInputs], spec: str) -> dict:
    """Which labels homogenization ran on, for the participants with an AI map: "canonical" (canonical labels for
    the AI and the endorsed map of every one), "raw_fallback" (none) or "mixed". Raw text brings back the
    shared-wording bias that canonical labels remove (the pull is computed on canonical labels only), so it is
    named here rather than left silent. The prior-only side is counted too."""
    have = [(p, pr) for p, pr in zip(p_list, _assigned_priors(p_list)) if p.ai.get(spec) is not None]
    n_ai = sum(1 for p, _ in have if (p.canon_ai or {}).get(spec) is None)
    n_end = sum(1 for p, _ in have if p.canon_endorsed is None)
    n_prior = sum(1 for _, (g, c, _i) in have if g is not None and c is None)
    possible = 2 * len(have) + sum(1 for _, (g, _c, _i) in have if g is not None)
    n_raw = n_ai + n_end + n_prior
    src = "canonical" if n_raw == 0 else ("raw_fallback" if n_raw == possible else "mixed")
    return {"label_source": src, "n_participants": len(have), "n_raw_ai": n_ai, "n_raw_endorsed": n_end,
            "n_raw_prior": n_prior}


def _not_judged_block(p_list: list[PairInputs], specs: list[str], setting: MatchSetting, min_words: int) -> dict:
    """Unjudged AI elements (nodes, and edges with a provenance and a modality entry) per spec of the headline, and
    on the endorsed side the in-transcript nodes left neither matched nor missed (``n_endorsed_not_judged``, at the
    primary setting). ``interpretation`` is computed over EVERY spec the headline uses: "reported_not_interpreted"
    as soon as any spec has more than ``NOT_JUDGED_LIMIT`` (10%) of its elements unjudged, else "interpreted"."""
    by_spec = {}
    for spec in specs:
        arm, n_el, n_nj, n_end_nj = spec_arm(spec), 0, 0, 0
        for p in p_list:
            if p.ai.get(spec) is None:
                continue
            nj = _nj(p, spec)
            ids = [eid for eid, *_ in _elements(p, spec)]
            n_el += len(ids)
            n_nj += sum(1 for eid in ids if eid in nj)
            n_end_nj += buckets(p, spec, arm, setting, min_words=min_words)["n_endorsed_not_judged"]
        by_spec[spec] = {"n_elements": n_el, "n_not_judged": n_nj, "share": _ratio(n_nj, n_el) if n_el else 0.0,
                         "n_endorsed_not_judged": n_end_nj}
    over = any(v["share"] > NOT_JUDGED_LIMIT + 1e-12 for v in by_spec.values())
    return {"limit": NOT_JUDGED_LIMIT, "by_spec": by_spec,
            "interpretation": "reported_not_interpreted" if over else "interpreted"}


def _stab(specs, settings, fn, primary_key: str) -> dict:
    """``sign_stability`` over the settings x specs grid; ``fn(spec, setting)`` returns (value, n maps)."""
    cells = {f"{s}|{st.key()}": fn(s, st) for s in specs for st in settings}
    return sign_stability({k: v for k, (v, _) in cells.items()}, {k: n for k, (_, n) in cells.items()},
                          primary_key)


def _mean_diff(a, b) -> float:
    return _mean(x - y for x, y in zip(a, b))


def _md(a, b) -> tuple[float, int]:
    return _mean_diff(a, b), len(a)


def _core(p_list, primary_spec, variation_specs, settings, primary_setting, min_words, grid, seed, B,
          n_derangements, n_perm, n_perm_script, script_sources, alpha_script) -> dict:
    spec_arm(primary_spec)
    if script_sources is None:                    # the reconstructor's own prior-only runs: a sensitivity row only
        script_sources = [ScriptSource("own", "own", scripts_by_role(p_list))]
    specs = [primary_spec] + [s for s in variation_specs if s != primary_spec]
    for s in specs:
        if spec_arm(s) == "prior_only":
            raise ValueError("headline specs must be full or removed arms")
    n_total = len(p_list)
    ps = primary_setting
    arm = spec_arm(primary_spec)
    out: dict = {"n_participants": n_total, "primary_spec": primary_spec, "variation_specs": specs[1:],
                 "primary_setting": ps.key(), "settings": [s.key() for s in settings], "min_words": min_words}

    # absences (lead finding)
    rows, ex = _absence_rows(p_list, primary_spec, ps, min_words)
    leak, flip = [r["leak_rate"] for r in rows], [r["flip_rate"] for r in rows]
    paired = _paired(leak, flip, ex, n_total, _seed(seed, "absences"), B)

    def abs_cell(s, st):
        r, _ = _absence_rows(p_list, s, st, min_words)
        return _md([x["leak_rate"] for x in r], [x["flip_rate"] for x in r])
    pkey = f"{primary_spec}|{ps.key()}"
    stab = _stab(specs, settings, abs_cell, pkey)
    qw = _paired([r["leak_quote_wished_rate"] for r in rows], flip, ex, n_total, _seed(seed, "absences_qw"), B)

    def qw_cell(s, st):
        r, _ = _absence_rows(p_list, s, st, min_words)
        return _md([x["leak_quote_wished_rate"] for x in r], [x["flip_rate"] for x in r])
    qw["sign_stability"] = _stab(specs, settings, qw_cell, pkey)
    # the old status-permutation null: a "worse than chance" check only, never a headline (see its docstring)
    null = status_permutation_null(p_list, primary_spec, ps, n_perm=n_perm, seed=_seed(seed, "statusnull"),
                                   min_words=min_words)
    worse = {"n": null["n"], "n_excluded": null["n_excluded"], "excluded_reasons": null["excluded_reasons"],
             "n_perm": null["n_perm"], "observed_mean_diff": null["observed_mean"],
             "null_mean_diff": null["null_mean"], "null_p95": null["null_p95"], "p": null["p"],
             "observed_exceeds_null_p95": null["observed_exceeds_null_p95"], "wished_rate_q": null["wished_rate_q"]}
    pooled = _pooled_absences(p_list, primary_spec, ps, min_words)
    out["absences"] = {"pooled": pooled, "paired_diff": paired,
                       "paired_diff_quote_wished": qw, "sign_stability": stab,
                       "base_rate": base_rate(p_list, primary_spec, ps, min_words=min_words,
                                              seed=_seed(seed, "base"), B=B),
                       "leak_split": _leak_split_block(p_list, primary_spec, ps, pooled, min_words,
                                                       _seed(seed, "leaksplit"), B),
                       "script_conditional": script_conditional(
                           p_list, primary_spec, specs, settings, ps, script_sources, n_perm=n_perm_script,
                           seed=seed, B=B, min_words=min_words, alpha=alpha_script),
                       "worse_than_chance_check": worse,
                       "contradiction_rate": contradiction_rate(p_list, primary_spec, min_words=min_words,
                                                                seed=_seed(seed, "contra"), B=B)}

    # pull toward the script (the thesis). PRIMARY = canonical labels for all three sources, so that wording
    # shared by the AI and the prior-only runs (same model) cannot favour the AI; raw labels and a judge-only
    # version are robustness lines.
    missing_prior: list[str] = []
    scripts = {"canonical": scripts_by_role(p_list, canonical=True, missing=missing_prior),
               "raw": scripts_by_role(p_list)}
    audit = _canonical_audit(p_list, specs, missing_prior)
    scripts["judge_only"] = scripts["raw"]

    def pull(variant: str, in_t: bool) -> dict:
        sc = scripts[variant]
        a, b, e = _pull_rows(p_list, primary_spec, ps, sc, in_t, variant)
        blk = _paired(a, b, e, n_total, _seed(seed, f"pull{variant}{in_t}"), B)

        def cell(s, st):
            x, y, _ = _pull_rows(p_list, s, st, sc, in_t, variant)
            return _md(x, y)
        blk["sign_stability"] = _stab(specs, settings, cell, pkey)
        blk["positive_and_stable"] = bool(blk["ci_lo"] > 0 and claim_stable(blk["sign_stability"]))
        return blk

    def gated(blk: dict) -> bool:
        """The claim needs the canonical path end to end: canonical labels AND canonical alignments."""
        return bool(blk.pop("positive_and_stable") and audit["canonical_used"] and audit["judge_channel_canonical"])
    prim = pull("canonical", False)
    prim["claim_pulled_toward_script"] = gated(prim)
    intr = pull("canonical", True)
    intr["claim_pulled_toward_script"] = gated(intr)
    out["pull_toward_script"] = {
        **prim, **audit,
        "in_transcript_only": intr,
        "robustness": {"raw_labels": pull("raw", False), "judge_only": pull("judge_only", False)}}

    # homogenization and linearization (independent of the matching setting: stability is over specs)
    def per_spec(spec):
        ai = [p.ai.get(spec) for p in p_list]
        end = [p.endorsed for p in p_list]
        ca, cb = [], []
        for p in p_list:
            can_a, can_b = _raw_labels(p, spec) if p.ai.get(spec) is not None else ([], [n.text for n in p.endorsed.nodes])
            ca.append(can_a)
            cb.append(can_b)
        return ai, end, ca, cb
    ai, end, ca, cb = per_spec(primary_spec)
    ivs = [p.interviewer for p in p_list]
    hom = homogenization(ai, end, ca, cb, ivs, seed=_seed(seed, "hom"), B=B)
    hom_c, hom_s, hom_n = {}, {}, {}
    for s in specs:
        h = homogenization(*per_spec(s), ivs, seed=_seed(seed, "hom" + s), B=min(B, 50))
        hom_c[s], hom_s[s], hom_n[s] = h["concept"]["diff"], h["structure"]["diff"], h["n"]
    hom["sign_stability"] = {"concept_diff_A_minus_B": sign_stability(hom_c, hom_n, primary_spec),
                             "structure_distance_diff_A_minus_B": sign_stability(hom_s, hom_n, primary_spec)}
    # the prior-only side of the symptom rule (01d): one prior sample per participant, only where the AI map exists
    pri = [(g if ai[k] is not None else None, c, i) for k, (g, c, i) in enumerate(_assigned_priors(p_list))]
    pri_maps = [g for g, _, _ in pri]
    pri_labels = [(c if c is not None else [n.text for n in g.nodes]) if g is not None else [] for g, c, _ in pri]
    hp = homogenization(pri_maps, end, pri_labels, cb, ivs, seed=_seed(seed, "hom_prior"), B=B,
                        sample_ids=[i for _, _, i in pri])
    hp["excluded_reasons"] = _rename_reason(hp["excluded_reasons"], "ai_failed", "prior_or_ai_missing")
    for k in ("concept", "structure"):
        hom[k]["prior_minus_endorsed"] = hp[k]["diff"]
        hom[k]["symptom_of_script"] = same_sign(hom[k]["diff"], hp[k]["diff"])
    hom["prior_only_contrast"] = {**{k: hp[k] for k in ("n", "n_excluded", "excluded_reasons", "concept", "structure")},
                                  "assignment": "round_robin_by_role"}
    hom["label_source"] = _hom_label_source(p_list, primary_spec)
    out["homogenization"] = hom
    lin: dict = {}
    for name, glued in (("glued", True), ("all", False)):
        lin[name] = linearization(ai, end, glued, seed=_seed(seed, "lin" + name), B=B)
        lp = linearization(pri_maps, end, glued, seed=_seed(seed, "linprior" + name), B=B)
        lin[name]["prior_only_contrast"] = {
            "n": lp["n"], "n_excluded": lp["n_excluded"],
            "excluded_reasons": _rename_reason(lp["excluded_reasons"], "ai_failed", "prior_or_ai_missing"),
            "assignment": "round_robin_by_role"}
        for k, row in lin[name]["measures"].items():
            row["prior_mean_diff"] = lp["measures"][k]["mean_diff"]
            row["symptom_of_script"] = same_sign(row["mean_diff"], lp["measures"][k]["mean_diff"])
    for name, glued in (("sign_stability", True), ("sign_stability_all_edges", False)):
        means: dict[str, dict[str, float]] = {}
        lin_n: dict[str, int] = {}
        for s in specs:
            r = linearization([p.ai.get(s) for p in p_list], end, glued, seed=_seed(seed, "linstab" + s), B=1)
            lin_n[s] = r["n"]
            for k, v in r["measures"].items():
                means.setdefault(k, {})[s] = v["mean_diff"]
        lin[name] = {k: sign_stability(v, lin_n, primary_spec) for k, v in means.items()}
    out["linearization"] = lin

    # leakage across arms
    full_spec = swap_arm(primary_spec, "full")
    removed_spec = swap_arm(primary_spec, "removed")
    lk = leakage(p_list, ps, full_spec=full_spec, removed_spec=removed_spec, min_words=min_words,
                 seed=_seed(seed, "leakage"), B=B)

    def leak_cell(s, st):
        r, e = _leak_inputs(p_list, st, swap_arm(s, "full"), swap_arm(s, "removed"), min_words)
        a, b, _ = _recall_diff_rows(r, e)
        return _md(a, b)
    lk["sign_stability"] = _stab(specs, settings, leak_cell, pkey)

    def leak_rate_cell(s, st):
        r, _ = _leak_inputs(p_list, st, swap_arm(s, "full"), swap_arm(s, "removed"), min_words)
        both = [(x["af"]["leak_rate"], x["ar"]["leak_rate"]) for x in r]
        ok = [(a, b) for a, b in both if not (math.isnan(a) or math.isnan(b))]
        return _mean(a - b for a, b in ok), len(ok)
    lk["leak_rate_diff"]["sign_stability"] = _stab(specs, settings, leak_rate_cell, pkey)
    out["leakage"] = lk

    # controls (shuffled pairs depend on the lexical threshold only; stability over thresholds x specs)
    cseed = _seed(seed, "controls")
    out["controls"] = controls(p_list, ps, spec_key=primary_spec, seed=cseed, n_derangements=n_derangements,
                               min_words=min_words, B=B)
    cache: dict = {}
    thr_cells: dict[str, float] = {}
    thr_n: dict[str, int] = {}
    for s in specs:
        for thr in sorted({st.lex_threshold for st in settings}):
            # same derangement count and seed derivation as the primary block inside ``controls``
            r, sh, _, _ = _shuffled_pairs_rows(p_list, s, MatchSetting(thr, "none"), _seed(cseed, "derange"),
                                               n_derangements, min_words, cache)
            thr_cells[f"{s}|lex{thr:g}"] = _mean_diff(r, sh)
            thr_n[f"{s}|lex{thr:g}"] = len(r)
    out["controls"]["shuffled_pairs"]["sign_stability"] = sign_stability(
        thr_cells, thr_n, f"{primary_spec}|lex{ps.lex_threshold:g}")

    # prior-only baseline: like-for-like (any AI node vs any prior node); the attested AI recall is a second line
    def prior_block(attested_ai: bool) -> dict:
        a, b, e = _prior_rows(p_list, primary_spec, ps, min_words, attested_ai)
        diff = _paired(a, b, e, n_total, _seed(seed, f"prior{attested_ai}"), B)

        def cell(s, st):
            x, y, _ = _prior_rows(p_list, s, st, min_words, attested_ai)
            return _md(x, y)
        return {"mean_ai_recall": diff["mean_a"], "mean_prior_recall": diff["mean_b"], "paired_diff": diff,
                "n": diff["n"], "n_excluded": diff["n_excluded"], "excluded_reasons": diff["excluded_reasons"],
                "sign_stability": _stab(specs, settings, cell, pkey)}
    out["prior_only_baseline"] = {**prior_block(False), "attested_line": prior_block(True)}

    # every spec the headline reads: the grid, and the full / removed twins its leakage block contrasts
    reads = specs + [t for s in specs for a in ("full", "removed")
                     if (t := swap_arm(s, a)) not in specs and any(t in p.ai for p in p_list)]
    out["not_judged"] = _not_judged_block(p_list, list(dict.fromkeys(reads)), ps, min_words)

    # quote-cutoff sensitivity
    sens = {}
    for mw in grid:
        bx, exb, recalls = Counter(), _Ex(), []
        for p in p_list:
            if p.ai.get(primary_spec) is None:
                exb.add("ai_failed")
                continue
            b1 = buckets(p, primary_spec, arm, ps, min_words=mw)
            for k in (*BUCKETS, "ai_attested_matched"):
                bx[k] += b1[k]
            recalls.append(b1["node_recall"])
        n_ok = n_total - sum(exb.c.values())
        bl = {**exb.report(n_total, n_ok), **{k: bx[k] for k in (*BUCKETS, "ai_attested_matched")},
              "node_recall_mean": _mean(_nonnan(recalls)), "n_recall": len(_nonnan(recalls))}
        r2, e2 = _absence_rows(p_list, primary_spec, ps, mw)
        al = {**_pooled_absences(p_list, primary_spec, ps, mw),
              "paired_diff": _paired([x["leak_rate"] for x in r2], [x["flip_rate"] for x in r2], e2, n_total,
                                     _seed(seed, f"sens{mw}"), B)}
        sens[str(mw)] = {"buckets": bl, "absences": al}
    out["quote_cutoff_sensitivity"] = sens
    return out


def headline(p_list: list[PairInputs], primary_spec: str, variation_specs: list[str],
             settings: list[MatchSetting], *, modality_source: str = "judge",
             primary_setting: MatchSetting = PRIMARY_SETTING, min_words: int = 8,
             min_words_grid: tuple[int, ...] = MIN_WORDS_GRID, seed: int = 0, B: int = 2000,
             n_derangements: int = 20, n_perm: int = 1000, n_perm_script: int = 10000,
             script_sources: list[ScriptSource] | None = None, alpha_script: float = 0.05) -> dict:
    """Assemble the headline numbers (JSON-safe).

    Blocks: ``absences``: ``pooled`` counts, ``paired_diff`` (descriptive leak rate minus control flip rate per map,
    with its sign stability; NOT a claim: an AI whose statuses are noise earns a positive value),
    ``base_rate`` (the AI's wished share against the endorsed wished share over matched in-transcript nodes: "the AI
    rarely sees absences"), ``script_conditional`` (THE lead test: among matched in-transcript endorsed ideals, the
    leak rate of those that match a script concept minus that of the rest, script flag permuted within map; one row
    per script source, headline flag ``claim_script_conditional_leakage`` only from an other-family source; see
    ``script_conditional``), ``leak_split`` (counterfactual_leakage vs adequacy_flattening, by the AI's own quote),
    ``worse_than_chance_check`` (the old within-map status-permutation null; never a headline) and
    ``contradiction_rate`` (among located AI nodes whose own quote the judge calls wished, the share drawn as
    experienced, and the mirror). ``n_perm_script`` / ``seed`` of the script test are recorded in its rows;
    ``script_sources`` is the list of ``ScriptSource`` (default: the reconstructor's own prior-only runs, a
    sensitivity row only), ``alpha_script`` the permutation level of the flag;
    ``pull_toward_script`` (primary estimate on
    CANONICAL labels of the AI, endorsed and prior-only maps; ``robustness`` holds the raw-label and the
    judge-only lines; ``in_transcript_only``; claim iff mean d > 0 with the CI above 0, full-coverage
    positive stability AND the canonical gate: ``canonical_used`` and ``judge_channel_canonical`` must both be
    true, i.e. every participant, every spec of the grid and every prior-only sample has canonical labels
    and canonical alignments (None = missing, {} = supplied). ``label_source`` reports what was actually
    used: "canonical", "mixed" or "raw_fallback", with ``fallback_reasons`` / ``fallback_counts`` /
    ``missing_prior_samples``), ``homogenization``, ``linearization`` (glued and all edges, stability for both),
    ``leakage``, ``controls``, ``prior_only_baseline`` (like-for-like, attested line second), and
    ``quote_cutoff_sensitivity`` (buckets and absences at every ``min_words`` in ``min_words_grid``).
    Point estimates use ``primary_spec`` and ``primary_setting`` (which must be in ``settings``);
    ``sign_stability`` runs over ``settings`` x ([primary] + ``variation_specs``) (over specs only for blocks
    that do not depend on the matching setting) and reports ``coverage``.

    ``modality_source``: "judge" (``PairInputs.modality``), "judge2" (the second judge; participants
    whose ``modality_judge2`` does not cover every element are left out and counted), or "keyword" (the
    no-AI ``keyword_modality`` classifier on the quotes). With "judge" and a second judge present,
    ``second_judge`` holds Cohen's kappa of the two judges' labels over the elements both labelled, the
    headline under judge 2 and, for a like-for-like comparison, under judge 1 on the same participants."""
    specs = [primary_spec] + [s for s in variation_specs if s != primary_spec]
    if primary_setting not in settings:
        raise ValueError("primary_setting must be one of settings (every claim is checked over the settings grid)")
    args = (primary_spec, variation_specs, settings, primary_setting, min_words, min_words_grid, seed, B,
            n_derangements, n_perm, n_perm_script, script_sources, alpha_script)
    if modality_source == "judge":
        out = _core(p_list, *args)
    elif modality_source == "keyword":
        out = _core([with_keyword_modality(p) for p in p_list], *args)
    elif modality_source == "judge2":
        have = [p for p in p_list if _covers(p, p.modality_judge2, specs)]
        out = _core([_as_judge2(p) for p in have], *args)
        out["n_participants_without_second_judge"] = len(p_list) - len(have)
    else:
        raise ValueError(f"modality_source must be judge, judge2 or keyword, got {modality_source!r}")
    out["modality_source"] = modality_source
    if modality_source == "judge":
        have = [p for p in p_list if _covers(p, p.modality_judge2, specs)]
        if have:
            la, lb = [], []
            for p in have:
                for s in specs:
                    g = p.ai.get(s)
                    if g is None:
                        continue
                    ids = [n.node_id for n in g.nodes] + [k for k in p.modality[s] if k.startswith("e:")
                                                          and k in p.modality_judge2[s]]
                    skip = _nj(p, s) | set((p.not_judged_judge2 or {}).get(s) or ())
                    for k in ids:
                        if k in skip:
                            continue
                        la.append(p.modality[s][k])
                        lb.append(p.modality_judge2[s][k])
            sub2 = _core([_as_judge2(p) for p in have], *args)
            sub1 = _core(have, *args)
            out["second_judge"] = {
                "cohen_kappa": cohen_kappa(la, lb), "n_labels": len(la),
                "agreement": _ratio(sum(1 for x, y in zip(la, lb) if x == y), len(la)),
                "n_participants": len(have), "n_participants_without_second_judge": len(p_list) - len(have),
                "headline_judge2": sub2, "headline_judge1_same_participants": sub1}
    return _clean(out)
