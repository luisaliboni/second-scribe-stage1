"""phantom: the known-distance check of the profile exporter. It plants doses in synthetic text and asks whether the
gauge detects them (synthetic only, zero model calls).

    python3 -m second_scribe.cli phantom [--reps 20] --out DIR

Ported from the analysis pack's ``kd_known_distance.py`` (pure functions copied, never imported). The input is a
finished run of the kit's synthetic corpus as replayed by the demo oracle (``run_phantom`` takes its
``profile_base.ProfileContext``; the ``phantom`` command builds the oracle demo into a temp dir itself). The oracle's AI
maps are the gold maps (minus what the removed arm cannot see), so dose 0 is the zero-distance point. Every dose
degrades COPIES of the oracle's PairInputs for the primary spec (removed__p0__t0__r0); every reading is then computed by
the kit's own functions on those copies (metrics.buckets, metrics.absences, metrics._pull_rows), the code path a lab
would run. Nothing is re-implemented except the pooling (ratio of sums, as pipeline._pooled), which tests/test_phantom.py
checks against pipeline._pooled.

Doses, each drawn from the POOLED set of elements of all analysed maps (k = round(share x pool), sampled without
replacement; the realised share k / pool is printed beside each dose, e.g. D4 25% = 2/7 = .286), seed
"SEED:dose:level:rep":
  D1 drop steps   10/20/40/60% of AI nodes, with their edges.
  D2 drop links   25/50/75% of AI edges.
  D3 merge steps  collapse 10/25/50% of AI edges: endpoints merged (union-find; the earlier node in map order survives
                  and keeps its quote, status, type and phase; label = member labels joined by " and "; self-loops
                  dropped, parallel edges kept once).
  D4 flip         25/50/100% of the AI partners of matched ideals (located AI nodes drawn wished that recover an endorsed
                  wished ideal at the primary setting) redrawn as experienced. Quote and modality label are kept, so a
                  flip is a counterfactual leak by construction.
  D5 lean         5/10/20% of AI nodes replaced, in place (same node id and edges), by nodes of the model's own
                  no-transcript maps (the prior_only__* script drafts in the participant's own PairInputs, all samples;
                  the kit draws them per role, so every participant of a role carries the same ones).
                  A second D3 run (primary setting, same draws) gives the merged box the surviving node's canonical
                  label instead of its raw text, to see whether the D3 pull fall is the construction's artefact.

Planted elements without a judge record (stated in the output):
  - an alignment pair (judged_ai_vs_endorsed, judged_ai_vs_script, judged_ai_vs_script_canon) is carried over only for
    a node whose label is unchanged (dropped, flipped and untouched nodes); a D3 merged node and a D5 inserted node
    get NO pair, so only the lexical half of the matcher can match them, whatever the setting;
  - modality: a D3 merged node (new label, no judge answer) is handled as the kit handles any element the judge never
    answered for: added to not_judged with the "unsupported" placeholder (so it is never attested; an endorsed node
    only it would match leaves the recall denominator, metrics.buckets). Re-pointed D3 edges and edges touching a D5
    node are marked not_judged the same way (no reading uses edge modality). A D5 node has no quote, so it is
    "unsupported" by the kit's rule for quote-less nodes (pipeline.py) and is not in not_judged;
  - provenance: a function of (quote, transcript) only (provenance.locate_quote), so an element whose quote is
    unchanged keeps its Provenance (re-keyed); a D5 node's empty quote is located by the kit's own locate_quote
    (never found);
  - canonical labels (canonical pull): kept for unchanged nodes; a D5 node takes its own canon_prior label; a D3
    merged node has none, so its raw label stands in (the kit's own fallback, here per node).
Two settings: the primary MatchSetting(0.6, "same") (planted elements lexical-only as above) and the lexical half alone,
MatchSetting(0.6, "none"), for every element.

Readings: node recall, direct edge recall, size ratio (AI boxes per endorsed box), the absence classes as shares of
the endorsed in-transcript ideals (kept as wished, flattened, counterfactual, missed) and the canonical pull.
Pass rules (declared in ROLE below, from the construction, before any run):
  1. each TARGET reading's median responds monotonically to its own dose (weakly, in the declared direction, and the
     top dose moves it);
  2. each STABLE reading's median stays within its dose-0 band (the map-cluster bootstrap 95% CI at dose 0);
  3. per dose type, the smallest dose whose replicate interval (2.5-97.5 percentile) excludes the dose-0 value;
  4. whether a planted pull of about .07 is detected (D5).
COUPLED readings move by construction through the kit's own definitions (reasons in WHY_COUPLED); they are reported,
not scored.

``detection_limits(result)`` condenses a result into the profile's ``orgsci-detection-limits/v1`` block
(PROFILE-SCHEMA.md section 12).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import math
import random
import re
import statistics
import tempfile
import types
from dataclasses import replace
from pathlib import Path

from . import metrics, pipeline, prereg, provenance, schemas, stats
from . import profile_base as pb

# The kit modules the port reads, as one namespace: the dose builders and readings take it as ``k`` (as in the pack).
K = types.SimpleNamespace(metrics=metrics, stats=stats, provenance=provenance, schemas=schemas, pipeline=pipeline)

SPEC = "removed__p0__t0__r0"
ARM = metrics.spec_arm(SPEC)
MW = 8
SEED = pb.SEED
REPS = 20
EPS = 1e-12
PULL_TARGET = 0.07
LOW_REPS = 5                          # fewer replicates than this: the output says so (low_reps)
SCHEMA = "orgsci-detection-limits/v1"
LIMIT = ("Tests the gauge's arithmetic on planted doses in synthetic text; it does not show that real-data matching "
         "is right.")

DOSES = {"D1": (0.10, 0.20, 0.40, 0.60), "D2": (0.25, 0.50, 0.75), "D3": (0.10, 0.25, 0.50),
         "D4": (0.25, 0.50, 1.00), "D5": (0.05, 0.10, 0.20)}
DOSE_NAMES = {"D1": "drop steps (share of AI nodes, with their edges)", "D2": "drop links (share of AI edges)",
              "D3": "merge steps (share of AI edges collapsed)",
              "D4": "flip wished to received (share of AI partners of matched ideals)",
              "D5": "lean toward the no-transcript map (share of AI nodes replaced)"}
READINGS = ("node_recall", "edge_recall", "size_ratio", "kept_as_wished", "flattened", "counterfactual", "missed",
            "pull")
AUX = ("n_ai_nodes", "n_ai_edges", "n_not_judged", "n_endorsed_not_judged", "leak_split_indeterminate", "n_wished",
       "pull_n_maps")

# Declared from the construction before any run: direction of every target / coupled reading (+1 up, -1 down).
ROLE = {
    "D1": {"target": {"node_recall": -1, "edge_recall": -1, "size_ratio": -1},
           "coupled": {"kept_as_wished": -1, "missed": +1},
           "stable": ("flattened", "counterfactual", "pull")},
    "D2": {"target": {"edge_recall": -1}, "coupled": {},
           "stable": ("node_recall", "size_ratio", "kept_as_wished", "flattened", "counterfactual", "missed", "pull")},
    "D3": {"target": {"size_ratio": -1, "node_recall": -1, "edge_recall": -1},
           "coupled": {"kept_as_wished": -1, "missed": +1, "pull": -1},
           "stable": ("flattened", "counterfactual")},
    "D4": {"target": {"counterfactual": +1, "kept_as_wished": -1},
           "coupled": {"node_recall": -1, "edge_recall": -1},
           "stable": ("size_ratio", "flattened", "missed", "pull")},
    "D5": {"target": {"pull": +1},
           "coupled": {"node_recall": -1, "edge_recall": -1, "kept_as_wished": -1, "missed": +1},
           "stable": ("size_ratio", "flattened", "counterfactual")},
}
WHY_COUPLED = {
    "D1": "a dropped node that was an ideal's only partner leaves that ideal missed",
    "D3": "a merged box absorbs an ideal's partner (kept falls, missed rises); a merged box has no canonical label, "
          "so its raw text stands in and cannot match the no-transcript map's canonical label (pull falls: an artefact of the "
          "construction, not a reading of the gauge)",
    "D4": "the kit counts only ATTESTED matches for recall (judge.attested: experienced needs an 'actual' quote), so a "
          "flipped node, still quoted as wished, stops counting for node and edge recall",
    "D5": "a replaced node is a lost step (node and edge recall fall; a replaced ideal partner leaves the ideal "
          "missed)",
}
COUPLED_REASON = {
    ("D1", "kept_as_wished"): "construction: a dropped node that was an ideal's only partner leaves the ideal unmatched",
    ("D1", "missed"): "construction: a dropped node that was an ideal's only partner leaves the ideal unmatched",
    ("D3", "kept_as_wished"): "construction: a merged box gets no judge pair and is not judged, so an ideal it "
                              "absorbs is no longer kept",
    ("D3", "missed"): "construction: a merged box gets no judge pair and is not judged, so an ideal it absorbs is "
                      "missed",
    ("D3", "pull"): "the test's own artefact: the merged box's raw text stands in for a canonical label "
                    "(merge_edges), so it cannot match the no-transcript map's canonical label; a real run would canonicalise it",
    ("D4", "node_recall"): "kit rule: only attested matches count for recall (judge.attested, via "
                           "metrics._attested_nodes); a flipped node is drawn experienced but quoted "
                           "as wished, so it is not attested",
    ("D4", "edge_recall"): "kit rule: an edge is recovered only between attested AI nodes (metrics.buckets, over the "
                           "attested matching); the same attested-only rule",
    ("D5", "node_recall"): "construction: a replaced node is a lost step; it has no quote, so it is unsupported by the "
                           "kit's rule (pipeline.py) and never attested",
    ("D5", "edge_recall"): "construction: edges touching a replaced node lose an attested end (metrics.buckets)",
    ("D5", "kept_as_wished"): "construction: a replaced ideal partner leaves the ideal unmatched",
    ("D5", "missed"): "construction: a replaced ideal partner leaves the ideal unmatched",
}


# ------------------------------------------------------------------ copies of the oracle's inputs
def _eid(e) -> str:
    return f"e:{e.source}->{e.target}"


def rebuild(p, g2, src, *, prov, modality, not_judged, canon):
    """A copy of PairInputs ``p`` whose primary-spec AI map is ``g2``. ``src[i2]`` is the index in the OLD map of the
    node now at position i2 whose judge alignments carry over, or None for a planted node (no pair: only the lexical
    half of the matcher can match it). Every index-based alignment of the AI side is re-indexed."""
    inv: dict = {}
    for i2, i in enumerate(src):
        if i is not None:
            inv.setdefault(i, []).append(i2)

    def remap(pairs):
        if pairs is None:
            return None
        return {(i2, j): v for (i, j), v in pairs.items() for i2 in inv.get(i, ())}

    def per_spec(field):
        if field is None or SPEC not in field:
            return field
        return {**field, SPEC: remap(field[SPEC])}
    canon_ai = p.canon_ai
    if canon_ai is not None and SPEC in canon_ai:
        canon_ai = {**canon_ai, SPEC: canon}
    return replace(p, ai={**p.ai, SPEC: g2}, prov={**p.prov, SPEC: prov}, modality={**p.modality, SPEC: modality},
                   judged_ai_vs_endorsed=per_spec(p.judged_ai_vs_endorsed),
                   judged_ai_vs_script=per_spec(p.judged_ai_vs_script),
                   judged_ai_vs_script_canon=per_spec(p.judged_ai_vs_script_canon),
                   canon_ai=canon_ai, not_judged={**(p.not_judged or {}), SPEC: set(not_judged)},
                   # fields keyed by the OLD element ids that no reading here uses: cleared, so a later reuse cannot
                   # read stale ids
                   modality_judge2=None, modality_shuffled=None, not_judged_judge2=None, not_judged_shuffled=None)


def _canon(p) -> list | None:
    return (p.canon_ai or {}).get(SPEC)


def _nj(p) -> set:
    return set((p.not_judged or {}).get(SPEC) or ())


def drop_nodes(p, ids: set):
    """D1: the AI map without the nodes ``ids`` and every edge touching them."""
    g, c, nj = p.ai[SPEC], _canon(p), _nj(p)
    keep = [i for i, n in enumerate(g.nodes) if n.node_id not in ids]
    nodes = [g.nodes[i] for i in keep]
    edges = [e for e in g.edges if e.source not in ids and e.target not in ids]
    alive = {n.node_id for n in nodes} | {_eid(e) for e in edges}
    return rebuild(p, replace(g, nodes=nodes, edges=edges), keep,
                   prov={x: v for x, v in p.prov[SPEC].items() if x in alive},
                   modality={x: v for x, v in p.modality[SPEC].items() if x in alive},
                   not_judged=nj & alive, canon=None if c is None else [c[i] for i in keep])


def drop_edges(p, idx: set):
    """D2: the AI map without the edges at positions ``idx`` (nodes untouched)."""
    g = p.ai[SPEC]
    edges = [e for i, e in enumerate(g.edges) if i not in idx]
    gone = {_eid(e) for i, e in enumerate(g.edges) if i in idx} - {_eid(e) for e in edges}
    return rebuild(p, replace(g, edges=edges), list(range(len(g.nodes))),
                   prov={x: v for x, v in p.prov[SPEC].items() if x not in gone},
                   modality={x: v for x, v in p.modality[SPEC].items() if x not in gone},
                   not_judged=_nj(p) - gone, canon=_canon(p))


def merge_edges(p, idx: set, canon_mode: str = "raw"):
    """D3: collapse the edges at positions ``idx`` (endpoints merged; see the module docstring). Returns
    (PairInputs, number of nodes absorbed). ``canon_mode`` "raw": the merged box's canonical label is its raw text
    (primary run); "survivor": the surviving node's canonical label (the variant run)."""
    if canon_mode not in ("raw", "survivor"):
        raise ValueError("canon_mode")
    g, c, nj0 = p.ai[SPEC], _canon(p), _nj(p)
    pos = {n.node_id: i for i, n in enumerate(g.nodes)}
    parent = {n.node_id: n.node_id for n in g.nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i in sorted(idx):
        a, b = find(g.edges[i].source), find(g.edges[i].target)
        if a != b:
            keep, gone = (a, b) if pos[a] < pos[b] else (b, a)
            parent[gone] = keep
    members: dict = {}
    for n in g.nodes:
        members.setdefault(find(n.node_id), []).append(n)
    pv0, md0 = p.prov[SPEC], p.modality[SPEC]
    nodes, src, prov, mod, nj, canon = [], [], {}, {}, set(), []
    for i, n in enumerate(g.nodes):
        if find(n.node_id) != n.node_id:
            continue
        group = members[n.node_id]
        prov[n.node_id] = pv0[n.node_id]                 # same quote, same provenance
        if len(group) == 1:
            nodes.append(n)
            src.append(i)
            mod[n.node_id] = md0[n.node_id]
            if n.node_id in nj0:
                nj.add(n.node_id)
            canon.append(None if c is None else c[i])
        else:
            text = " and ".join(m.text for m in group)
            nodes.append(replace(n, text=text))
            src.append(None)                             # new label: no alignment pair
            mod[n.node_id] = "unsupported"               # new element, no judge answer: the kit's not-judged handling
            nj.add(n.node_id)
            # no canonical label: raw text stands in (primary), or the survivor's label (variant)
            canon.append(text if canon_mode == "raw" else None if c is None else c[i])
    edges, seen = [], set()
    for e in g.edges:
        s, t = find(e.source), find(e.target)
        if s == t or (s, t) in seen:
            continue
        seen.add((s, t))
        old = _eid(e)
        if (s, t) == (e.source, e.target) and len(members[s]) == 1 and len(members[t]) == 1:
            edges.append(e)
            prov[old], mod[old] = pv0[old], md0[old]
            if old in nj0:
                nj.add(old)
        else:
            e2 = replace(e, source=s, target=t)
            new = _eid(e2)
            edges.append(e2)
            prov[new] = replace(pv0[old], element_id=new)
            mod[new] = "unsupported"
            nj.add(new)
    absorbed = len(g.nodes) - len(nodes)
    return rebuild(p, replace(g, nodes=nodes, edges=edges), src, prov=prov, modality=mod, not_judged=nj,
                   canon=None if c is None else canon), absorbed


def flip_nodes(p, ids: set):
    """D4: the nodes ``ids`` redrawn as experienced; quote, provenance, modality label and alignments kept."""
    g = p.ai[SPEC]
    nodes = [replace(n, status="experienced") if n.node_id in ids else n for n in g.nodes]
    return rebuild(p, replace(g, nodes=nodes), list(range(len(nodes))), prov=dict(p.prov[SPEC]),
                   modality=dict(p.modality[SPEC]), not_judged=_nj(p), canon=_canon(p))


def prior_pool(p) -> list:
    """Every node of the participant's no-transcript script drafts: [(prior spec key, node index)], sorted (a separate
    reading-3 map is not a script draft: ``metrics.script_keys``)."""
    return [(key, j) for key in metrics.script_keys(p) for j in range(len(p.ai[key].nodes))]


def insert_prior(p, picks: dict, k):
    """D5: node id -> (prior spec key, node index): each such AI node is replaced in place by that no-transcript
    node (same node id, so its edges stay; text, type, phase, status and quote are the prior node's)."""
    g, c, nj = p.ai[SPEC], _canon(p), _nj(p)
    prov, mod = dict(p.prov[SPEC]), dict(p.modality[SPEC])
    nodes, src, canon = [], [], []
    for i, n in enumerate(g.nodes):
        if n.node_id not in picks:
            nodes.append(n)
            src.append(i)
            canon.append(None if c is None else c[i])
            continue
        key, j = picks[n.node_id]
        q = p.ai[key].nodes[j]
        if q.quote:
            raise ValueError("a no-transcript node with a quote")    # never in the kit: prior-only maps have none
        nodes.append(replace(q, node_id=n.node_id))
        src.append(None)
        prov[n.node_id] = k.provenance.locate_quote(q.quote, None, n.node_id, "node")   # empty quote: never found
        mod[n.node_id] = "unsupported"                   # the kit's rule for a node without a quote
        nj.discard(n.node_id)
        cp = (p.canon_prior or {}).get(key)
        canon.append(cp[j] if cp is not None else q.text)
    for e in g.edges:
        if e.source in picks or e.target in picks:
            mod[_eid(e)] = "unsupported"
            nj.add(_eid(e))
    return rebuild(p, replace(g, nodes=nodes), src, prov=prov, modality=mod, not_judged=nj,
                   canon=None if c is None else canon)


def ideal_partners(p, k, setting) -> list:
    """AI node ids that recover an endorsed wished ideal (located, drawn wished; metrics.absence_category)."""
    m = k.metrics
    st = m._absence_structure(p, SPEC, ARM, setting, MW)
    out = set()
    for nid, status in st["ends"]:
        if status == "wished" and m.absence_category(st, st["status"], nid)[0] == "recovered_as_wished":
            out |= {a for a in st["loc"].get(nid, []) if st["status"][a] == "wished"}
    return sorted(out)


def _pick(rng, pool: list, share: float) -> list:
    n = int(share * len(pool) + 0.5)
    return sorted(rng.sample(pool, n))


def apply_dose(dose: str, share: float, pop: list, rng, k, d3_canon: str = "raw") -> tuple:
    """(degraded copy of ``pop``, realised dose info). Elements are drawn from the pool over all maps."""
    ps = pb.SETTINGS["primary"]
    out, info = list(pop), {}
    if dose in ("D1", "D5"):
        pool = [(i, n.node_id) for i, p in enumerate(pop) for n in p.ai[SPEC].nodes]
    elif dose in ("D2", "D3"):
        pool = [(i, j) for i, p in enumerate(pop) for j in range(len(p.ai[SPEC].edges))]
    elif dose == "D4":
        pool = [(i, a) for i, p in enumerate(pop) for a in ideal_partners(p, k, ps)]
    else:
        raise ValueError("dose")
    chosen = _pick(rng, pool, share)
    by_map: dict = {}
    for i, x in chosen:
        by_map.setdefault(i, set()).add(x)
    absorbed = 0
    for i, xs in sorted(by_map.items()):
        p = pop[i]
        if dose == "D1":
            out[i] = drop_nodes(p, xs)
        elif dose == "D2":
            out[i] = drop_edges(p, xs)
        elif dose == "D3":
            out[i], a = merge_edges(p, xs, d3_canon)
            absorbed += a
        elif dose == "D4":
            out[i] = flip_nodes(p, xs)
        else:
            src = prior_pool(p)
            if len(src) < len(xs):
                raise ValueError("no-transcript pool too small")
            draws = rng.sample(src, len(xs))
            out[i] = insert_prior(p, dict(zip(sorted(xs), draws)), k)
    info.update(n_pool=len(pool), n_chosen=len(chosen), realised_share=len(chosen) / len(pool) if pool else math.nan)
    if dose == "D3":
        info["nodes_absorbed"] = absorbed
    return out, info


# ------------------------------------------------------------------ readings (the kit's own metric code)
def map_rows(pop, setting, k) -> list:
    rows = []
    for p in pop:
        b = k.metrics.buckets(p, SPEC, ARM, setting, min_words=MW)
        a = k.metrics.absences(p, SPEC, ARM, setting, min_words=MW)
        rows.append({"matched": b["matched"], "nr_den": b["matched"] + b["missed"],
                     "er_num": b["n_edges_recovered"], "er_den": b["n_edges_eligible"],
                     "n_ai_nodes": b["n_ai_nodes"], "n_endorsed_nodes": b["n_endorsed_nodes"],
                     "n_ai_edges": len(p.ai[SPEC].edges), "n_not_judged": b["n_not_judged"],
                     "n_endorsed_not_judged": b["n_endorsed_not_judged"],
                     "kept_as_wished": a["recovered_as_wished"], "flattened": a["adequacy_flattening"],
                     "counterfactual": a["counterfactual_leakage"], "missed": a["missed"],
                     "leak_split_indeterminate": a["leak_split_indeterminate"], "n_wished": a["n_wished"]})
    return rows


RATIO = {"node_recall": ("matched", "nr_den"), "edge_recall": ("er_num", "er_den"),
         "size_ratio": ("n_ai_nodes", "n_endorsed_nodes"), "kept_as_wished": ("kept_as_wished", "n_wished"),
         "flattened": ("flattened", "n_wished"), "counterfactual": ("counterfactual", "n_wished"),
         "missed": ("missed", "n_wished")}


def readings(pop, setting, scripts, k) -> dict:
    """Every reading of one population: pooled ratios of sums over maps (as pipeline._pooled), the canonical pull
    (metrics._pull_rows; mean of per-map differences, as the kit's headline). Also the raw per-map inputs."""
    rows = map_rows(pop, setting, k)
    val = {name: pb.pooled_ratio([r[n] for r in rows], [r[d] for r in rows]) for name, (n, d) in RATIO.items()}
    pa, pb_, _ex = k.metrics._pull_rows(pop, SPEC, setting, scripts, False, "canonical")
    val["pull"] = k.metrics._mean_diff(pa, pb_) if pa else math.nan
    for x in AUX[:-1]:
        val[x] = sum(r[x] for r in rows)
    val["pull_n_maps"] = len(pa)
    return {"values": val, "rows": rows, "pull_a": pa, "pull_b": pb_}


def dose0_band(rd: dict, k, B: int, seed: int = SEED) -> dict:
    """Map-cluster bootstrap 95% CI of every reading at dose 0 (the gauge's own resolution on these maps)."""
    rows, band = rd["rows"], {}
    for name, (n, d) in RATIO.items():
        _pt, lo, hi, _n = pb.cluster_bootstrap_ratio([r[n] for r in rows], [r[d] for r in rows], B=B, seed=seed)
        band[name] = [lo, hi]
    _pt, lo, hi = k.stats.paired_bootstrap_ci(rd["pull_a"], rd["pull_b"], B=B, seed=seed)
    band["pull"] = [lo, hi]
    return band


def summary(xs: list) -> dict:
    xs = [x for x in xs if not math.isnan(x)]
    if not xs:
        return {"median": math.nan, "rep_low": math.nan, "rep_high": math.nan, "n": 0}
    lo, hi = pb._percentile(xs, 0.05)                     # the kit's 2.5 / 97.5 percentile rule over replicates
    return {"median": statistics.median(xs), "rep_low": lo, "rep_high": hi, "n": len(xs)}


# ------------------------------------------------------------------ pass rules
def monotone(meds: list, sign: int) -> bool:
    """Medians (dose 0 leading) weakly monotone in direction ``sign``, and the top dose moved the reading."""
    if any(math.isnan(m) for m in meds):
        return False
    steps = all(sign * (b - a) >= -EPS for a, b in zip(meds, meds[1:]))
    return steps and sign * (meds[-1] - meds[0]) > EPS


def within(x: float, band) -> bool:
    return not math.isnan(x) and band[0] - EPS <= x <= band[1] + EPS


def smallest_detected(levels: list, base: float, intervals: list):
    """The smallest dose whose replicate interval excludes the dose-0 value (None if none does, and None when the dose-0
    value is undefined: final review B L-2)."""
    if base is None or math.isnan(base):
        return None
    for lv, (lo, hi) in zip(levels, intervals):
        if not math.isnan(lo) and not (lo - EPS <= base <= hi + EPS):
            return lv
    return None


def pull_detection(levels: list, base: float, meds: list, detected: list, target: float = PULL_TARGET,
                   tol: float = 0.01) -> dict:
    """Is a planted pull of about ``target`` detected? Planted pull = median pull at a dose minus the dose-0 pull. If
    some dose plants target +/- tol, read it there; otherwise interpolate linearly between the bracketing doses (dose
    0 included) and SAY so: detected if the lower bracketing dose is detected (the doses are monotone), not
    established if only the upper one is."""
    planted = [m - base for m in meds]
    near = min(range(len(levels)), key=lambda i: abs(planted[i] - target))
    if abs(planted[near] - target) <= tol:
        return {"interpolated": False, "dose": levels[near], "planted": planted[near], "detected": detected[near]}
    xs, ys, det = [0.0, *levels], [0.0, *planted], [False, *detected]
    for i in range(len(xs) - 1):
        if ys[i] < target <= ys[i + 1]:
            dose = xs[i] + (target - ys[i]) * (xs[i + 1] - xs[i]) / (ys[i + 1] - ys[i])
            return {"interpolated": True, "dose": dose, "planted": target, "bracket": [xs[i], xs[i + 1]],
                    "detected": True if det[i] else (None if det[i + 1] else False)}
    return {"interpolated": False, "dose": None, "planted": max(planted), "detected": None,
            "note": "no dose planted a pull of about .07"}


def at_floor(name: str, sign: int, base: dict) -> bool:
    """A target reading that cannot move in its declared direction at dose 0: a share already at 0 (to fall) or 1 (to
    rise), or, for the counterfactual share, no ideal kept as wished for a flip to turn."""
    if name in RATIO and name != "size_ratio" and abs(base[name] - (0.0 if sign < 0 else 1.0)) < EPS:
        return True
    return name == "counterfactual" and base["kept_as_wished"] < EPS


def rules(dose: str, levels: list, base: dict, band: dict, summ: list, h2: list | None) -> dict:
    role = ROLE[dose]
    out = {"monotone": {}, "specificity": {}, "smallest_detected": {}, "coupled": {}, "at_floor": []}
    for name, sign in role["target"].items():
        meds = [base[name]] + [s[name]["median"] for s in summ]
        out["monotone"][name] = monotone(meds, sign)
        if at_floor(name, sign, base):
            out["at_floor"].append(name)
        out["smallest_detected"][name] = smallest_detected(levels, base[name],
                                                           [(s[name]["rep_low"], s[name]["rep_high"]) for s in summ])
    for name in role["stable"]:
        meds = [s[name]["median"] for s in summ]
        out["specificity"][name] = {"pass": all(within(m, band[name]) for m in meds),
                                    "replicates_inside": all(within(s[name]["rep_low"], band[name])
                                                             and within(s[name]["rep_high"], band[name]) for s in summ)}
    for name, sign in role["coupled"].items():
        meds = [base[name]] + [s[name]["median"] for s in summ]
        out["coupled"][name] = {"declared_direction": sign, "monotone": monotone(meds, sign)}
    found = [(lv, nm) for nm, lv in out["smallest_detected"].items() if lv is not None]
    out["smallest_detected_dose"] = min(found)[0] if found else None
    out["smallest_detected_by"] = sorted(nm for lv, nm in found if lv == min(found)[0]) if found else []
    out["pass"] = (all(out["monotone"].values()) and all(v["pass"] for v in out["specificity"].values()))
    if dose == "D5":
        det = [s["pull"]["rep_low"] > base["pull"] + EPS or s["pull"]["rep_high"] < base["pull"] - EPS for s in summ]
        out["pull_07"] = pull_detection(levels, base["pull"], [s["pull"]["median"] for s in summ], det)
        out["pull_07"]["kit_ci_lo_above_0_share"] = h2
    return out


# ------------------------------------------------------------------ the run
def settings(k=None) -> dict:
    return {"primary": pb.SETTINGS["primary"], "lexical_only": pb.SETTINGS["lexical_only_06"]}


def run(pop, scripts, k, *, reps: int = REPS, B: int = pb.B, seed: int = SEED, doses: dict = DOSES) -> dict:
    sets = settings(k)
    base = {s: readings(pop, st, scripts, k) for s, st in sets.items()}
    res = {s: {"dose0": base[s]["values"], "dose0_band": dose0_band(base[s], k, B, seed), "doses": {}} for s in sets}
    for dose, levels in doses.items():
        acc = {s: [] for s in sets}
        infos, h2 = [], {s: [] for s in sets}
        for lv in levels:
            vals = {s: {x: [] for x in (*READINGS, *AUX)} for s in sets}
            ci_hits = {s: 0 for s in sets}
            real = []
            for rep in range(reps):
                rng = random.Random(f"{seed}:{dose}:{lv}:{rep}")
                newpop, info = apply_dose(dose, lv, pop, rng, k)
                real.append(info)
                for s, st in sets.items():
                    rd = readings(newpop, st, scripts, k)
                    for x in (*READINGS, *AUX):
                        vals[s][x].append(rd["values"][x])
                    if dose == "D5":
                        _pt, lo, _hi = k.stats.paired_bootstrap_ci(rd["pull_a"], rd["pull_b"], B=B, seed=seed)
                        ci_hits[s] += lo > 0
            infos.append({"dose": lv, "n_pool": real[0]["n_pool"], "n_chosen": real[0]["n_chosen"],
                          "realised_share": real[0]["realised_share"],
                          **({"nodes_absorbed": summary([i["nodes_absorbed"] for i in real])} if dose == "D3" else {})})
            for s in sets:
                acc[s].append({x: summary(vals[s][x]) for x in (*READINGS, *AUX)})
                h2[s].append(ci_hits[s] / reps)
        for s in sets:
            res[s]["doses"][dose] = {
                "levels": list(levels), "realised": infos, "summary": acc[s],
                "rules": rules(dose, list(levels), res[s]["dose0"], res[s]["dose0_band"], acc[s],
                               h2[s] if dose == "D5" else None)}
    if "D3" in doses:                                    # variant: merged box takes the survivor's canonical label
        var = []
        for lv in doses["D3"]:
            vals = {x: [] for x in READINGS}
            for rep in range(reps):
                rng = random.Random(f"{seed}:D3:{lv}:{rep}")          # the primary D3 seeds: the same draws
                newpop, _info = apply_dose("D3", lv, pop, rng, k, d3_canon="survivor")
                rd = readings(newpop, sets["primary"], scripts, k)
                for x in READINGS:
                    vals[x].append(rd["values"][x])
            var.append({x: summary(vals[x]) for x in READINGS})
        res["primary"]["d3_survivor_canon"] = {"levels": list(doses["D3"]), "summary": var}
    return res


def kit_commit() -> str:
    """The kit's git HEAD, or "unknown" when the kit is not a git checkout."""
    return prereg.git_head(prereg.KIT_ROOT) or "unknown"


def meta(pop, reps: int, B: int, seed: int, commit: str) -> dict:
    return {"test": "known-distance check (planted doses)", "tool": "second_scribe.phantom",
            "corpus": "kit synthetic demo corpus, demo-oracle replay (zero model calls)",
            "kit_commit": commit, "spec": SPEC, "min_words": MW, "n_maps": len(pop), "replicates": reps,
            "bootstrap_B": B, "seed_base": seed, "seed": f"random.Random('{seed}:<dose>:<level>:<replicate>')",
            "settings": {"primary": "lex0.6_same; planted elements carry no judge pair (lexical half only)",
                         "lexical_only": "lex0.6, judge_accept none, every element"},
            "replicate_interval": "rep_low / rep_high: the kit's 2.5 / 97.5 percentile rule (stats._percentile_ci) "
                                  "over replicates; with fewer than 40 replicates this is the replicate range",
            "dose0_band": "map-cluster percentile bootstrap 95% CI at dose 0 (pull: stats.paired_bootstrap_ci)",
            "doses": {d: {"levels": list(v), "what": DOSE_NAMES[d]} for d, v in DOSES.items()},
            "roles": {d: {"target": r["target"], "coupled": r["coupled"], "stable": list(r["stable"])}
                      for d, r in ROLE.items()},
            "why_coupled": WHY_COUPLED, "coupled_reason": {f"{d}:{r}": v for (d, r), v in COUPLED_REASON.items()},
            "limit": LIMIT}


def detection_limits(result: dict) -> dict:
    """The profile's top-level ``detection_limits`` block (schema ``orgsci-detection-limits/v1``) from a result of
    ``run_phantom`` (or the same dict read back from phantom.json). Per dose type, on the primary setting:
    ``smallest_detected`` (the smallest dose whose replicate interval excludes the dose-0 value, None if none),
    ``lowest_tried`` and ``at_or_below`` (the smallest detected dose is the lowest tried: read it as "at or below",
    never as a threshold). Each entry names its ``rule``: "rep_interval" (D1-D4) or, for D5, "rep_interval+kit_ci": the
    smallest dose where the kit's CI is above 0 in every replicate AND the replicate interval is off the dose-0 point;
    null (and ``at_or_below`` false) when no D5 dose is safe, with no fallback to the plain rule. ``pull_rule`` is the
    rule behind ``pull_detected`` ("rep_interval"). ``low_reps`` (true) appears when there are fewer than 5 replicates. ``pull_detected`` is True only when
    the planted pull of .07 is DETECTED (not established and not detected are both False)."""
    prim, m = result["results"]["primary"], result["meta"]
    per_dose = {}
    for dose in DOSES:
        blk = prim["doses"][dose]
        sd, lowest = blk["rules"]["smallest_detected_dose"], blk["levels"][0]
        if dose == "D5":                                 # as the report leads: the safe dose, null when none is safe
            sd = _d5_safe(prim)
        per_dose[dose] = {"smallest_detected": sd, "lowest_tried": lowest,
                          "at_or_below": sd is not None and sd == lowest,
                          "rule": "rep_interval+kit_ci" if dose == "D5" else "rep_interval"}
    out = {"schema": SCHEMA, "per_dose": per_dose, "pull_planted": PULL_TARGET,
           "pull_detected": prim["doses"]["D5"]["rules"]["pull_07"]["detected"] is True, "pull_rule": "rep_interval",
           "dose0_bands": prim["dose0_band"],
           "settings": {"primary": "lex0.6_same", "lexical_only": "lex0.6_none", "reps": m["replicates"],
                        "B": m["bootstrap_B"], "seed": m["seed_base"], "min_words": m["min_words"], "spec": m["spec"]},
           "kit_commit": m["kit_commit"], "limit": LIMIT}
    if m["replicates"] < LOW_REPS:
        out["low_reps"] = True
    return _clean(out)


def _require_synthetic(ctx) -> None:
    if not pipeline.is_synthetic(ctx.cfg):
        raise RuntimeError("phantom: synthetic runs only")


def run_phantom(ctx: pb.ProfileContext, reps: int = REPS, out_dir: Path | None = None,
                commit: str | None = None) -> dict:
    """The known-distance check on the open synthetic run ``ctx`` (the demo oracle replay; B and seed from ``ctx``).
    Returns {"meta", "results", "detection_limits"}; with ``out_dir`` also writes ``phantom.json`` and ``phantom.md``
    there (sorted keys, 6 decimals: two runs on the same inputs give the same bytes). Refuses a non-synthetic run
    (RuntimeError) and a run whose primary spec or min_words differ from the ones this port is written for (ValueError),
    before drawing anything. ``commit`` is the kit commit to record (a lab's, from its profile); the default is this
    kit's own HEAD."""
    _require_synthetic(ctx)
    if ctx.spec != SPEC:
        raise ValueError(f"phantom is written for the primary spec {SPEC}, not {ctx.spec}")
    if pipeline.headline_kwargs(ctx.cfg)["min_words"] != MW:
        raise ValueError(f"phantom is written for min_words {MW}")
    if reps < 1:
        raise ValueError("reps must be at least 1")
    pop = ctx.analysed
    scripts = metrics.scripts_by_role(ctx.p_list, canonical=True)
    res = run(pop, scripts, K, reps=reps, B=ctx.B, seed=ctx.seed)
    m = meta(pop, reps, ctx.B, ctx.seed, commit if commit is not None else kit_commit())
    result = {"meta": m, "results": res}
    result["detection_limits"] = detection_limits(result)
    if out_dir is not None:
        write_outputs(result, out_dir)
    return result


def write_outputs(result: dict, out_dir) -> None:
    """phantom.json and phantom.md of a ``run_phantom`` result: both texts are built before either is written, and each
    is written atomically (``report.write_text_atomic``)."""
    from . import report
    out = Path(out_dir)
    json_text = json.dumps(_clean(result), indent=1, sort_keys=True) + "\n"
    md_text = markdown(result["results"], result["meta"])
    report.write_text_atomic(out / "phantom.json", json_text)
    report.write_text_atomic(out / "phantom.md", md_text)


# ------------------------------------------------------------------ outputs
def _clean(x):
    if isinstance(x, float):
        return None if math.isnan(x) else round(x, 6)
    if isinstance(x, dict):
        return {str(k_): _clean(v) for k_, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _f(x, nd=3) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def _pct(x) -> str:
    return "none" if x is None else f"{round(100 * x)}%"


def _cell(s: dict, name: str) -> str:
    return f"{_f(s[name]['median'])} [{_f(s[name]['rep_low'])}, {_f(s[name]['rep_high'])}]"


def _head(info: dict) -> str:
    return f"{_pct(info['dose'])} ({info['n_chosen']}/{info['n_pool']} = {_f(info['realised_share'])})"


def _outside(d: dict, name: str, band) -> bool:
    return any(not within(s[name]["median"], band) for s in d["summary"])


def _span(lo, hi) -> str:
    return f"[{_f(lo)}, {_f(hi)}]"


def _d3_variant_verdict(v: dict, band) -> str:
    """Direction-aware: does the D3 pull still fall, rise out of its band, or stay inside, when the merged box takes
    the surviving node's canonical label?"""
    meds = [s["pull"]["median"] for s in v["summary"]]
    below = [_pct(x) for x, m_ in zip(v["levels"], meds) if m_ < band[0] - EPS]
    above = [_pct(x) for x, m_ in zip(v["levels"], meds) if m_ > band[1] + EPS]
    if below:
        return f"still falls below its dose-0 band (at {', '.join(below)})"
    if above:
        return (f"no longer falls; it rises, and its median leaves the dose-0 band above at {', '.join(above)} "
                "(merging removes boxes and so changes the AI side's share of no-transcript-map nodes; not investigated further)")
    return "no longer falls and stays within its dose-0 band"


def _d5_safe(p: dict):
    """The smallest D5 dose where the kit's paired-bootstrap CI is above 0 in every replicate AND the replicate
    interval excludes the dose-0 pull. None if no dose does (there is no fallback to the plain rule), or when the
    result lacks those parts."""
    d = p["doses"]["D5"]
    h2 = (d["rules"].get("pull_07") or {}).get("kit_ci_lo_above_0_share")
    if h2 is None or "summary" not in d or "dose0" not in p:
        return None
    base = p["dose0"]["pull"]
    return next((x for x, s, h in zip(d["levels"], d["summary"], h2)
                 if h >= 1.0 and s["pull"]["rep_low"] > base + EPS), None)


def _d5_lead(p: dict) -> str:
    """D5 led by the safe dose; the lower dose shown only by its replicate interval, with the band overlap. With no
    safe dose the lead says so."""
    safe = _d5_safe(p)
    sd = p["doses"]["D5"]["rules"]["smallest_detected_dose"]
    if safe is None:
        return ("none safe" if sd is None else
                f"none safe (only the replicate interval excludes the dose-0 point, at {_pct(sd)})")
    if sd is None or sd >= safe:
        return f"{_pct(safe)} (kit CI above 0 in 100% of replicates)"
    return (f"{_pct(safe)} (kit CI above 0 in 100% of replicates); at {_pct(sd)} only the replicate interval excludes "
            "the dose-0 point, and it overlaps the dose-0 band")


def markdown(res: dict, m: dict) -> str:
    p = res["primary"]
    nw = p["dose0"]["n_wished"]
    nrep = m["replicates"]
    lowest, at_lowest = [], []
    for dose in DOSES:
        ru, lv = p["doses"][dose]["rules"], p["doses"][dose]["levels"]
        sd = ru["smallest_detected_dose"]
        at_lowest.append((_d5_safe(p) if dose == "D5" else sd) == lv[0])
        lowest.append(f"D5 {_d5_lead(p)}" if dose == "D5" else
                      f"{dose} {'at or below ' if sd == lv[0] else ''}{_pct(sd)}")
    all_lowest = all(at_lowest)
    L = ["# Known-distance check: planted doses in synthetic text, zero model calls", "",
         f"Limit: {LIMIT}", "",
         f"Kit {str(m['kit_commit'])[:7]}, {m['n_maps']} synthetic maps (demo oracle replay), spec `{m['spec']}`, "
         f"{nrep} replicates per dose, fixed seeds, B = {m['bootstrap_B']}. Dose 0 = the oracle (AI map = gold).",
         "Cells: median [rep_low, rep_high] over replicates (the kit's 2.5 / 97.5 percentile rule; with fewer than 40 "
         "replicates this is the replicate range). Column heads: nominal dose (elements changed / pool = realised "
         "share). Dose-0 band = map-cluster bootstrap 95% CI. Absence classes are shares of the endorsed "
         f"in-transcript ideals (n = {nw}).",
         "Planted elements without a judge record (D3 merged boxes, D5 inserted no-transcript boxes) carry no alignment "
         "pair, so only the lexical half of the matcher can match them; a merged box is also handled as not judged "
         "(the kit's not-judged handling). The lexical-only setting MatchSetting(0.6, \"none\") is run beside the "
         "primary one.",
         "**D3 and D5 recall drops are upper bounds:** merged and replaced boxes get no judge pair, while a lab's judge "
         "would still pair \"A and B\" with A or B, or a no-transcript box with a similar endorsed step.",
         *([f"**Low replicate count:** {nrep} replicates (fewer than {LOW_REPS}); read the intervals as a rough guide."]
           if nrep < LOW_REPS else []),
         (f"**Every dose type detects at the lowest dose tried**, so the test resolves only: detected "
          f"{', '.join(lowest)} (primary setting). It gives no threshold below those doses." if all_lowest else
          f"Smallest detected dose per dose type (primary setting): {', '.join(lowest)}."), ""]
    for dose in DOSES:
        d = p["doses"][dose]
        role, lv, ru, band = ROLE[dose], d["levels"], d["rules"], p["dose0_band"]
        L += [f"## {dose}: {DOSE_NAMES[dose]}", "",
              "| reading | role | dose 0 [band] | " + " | ".join(_head(i) for i in d["realised"]) + " |",
              "|---|---|---|" + "---|" * len(lv)]
        for name in READINGS:
            r_ = ("target" if name in role["target"] else "coupled" if name in role["coupled"] else
                  "stable" if name in role["stable"] else "-")
            L.append(f"| {name} | {r_} | {_f(p['dose0'][name])} {_span(*band[name])} | "
                     + " | ".join(_cell(s, name) for s in d["summary"]) + " |")
        L.append("")
        for name, ok in ru["monotone"].items():
            L.append(f"- Monotone ({name}, own dose): {'PASS' if ok else 'FAIL'}.")
        for name, v in ru["specificity"].items():
            L.append(f"- Specificity ({name} stays in its dose-0 band): {'PASS' if v['pass'] else 'FAIL'}"
                     f"{'' if v['replicates_inside'] else ' (median inside; some replicate outside)' if v['pass'] else ''}.")
        sd = ", ".join(f"{nm} {_pct(x)}" for nm, x in ru["smallest_detected"].items())
        L.append(f"- Smallest detected dose: {_d5_lead(p)}." if dose == "D5" else
                 f"- Smallest detected dose (replicate interval excludes the dose-0 value): "
                 f"{_pct(ru['smallest_detected_dose'])} ({sd})."
                 + (f" This is the lowest dose tried: detection at or below {_pct(lv[0])}, not a threshold."
                    if ru["smallest_detected_dose"] == lv[0] else ""))
        moved = [nm for nm in role["coupled"] if _outside(d, nm, band[nm])]
        for nm in moved:
            L.append(f"- Moved outside its dose-0 band but classed coupled: {nm} ({COUPLED_REASON[(dose, nm)]}).")
        inside = [nm for nm in role["coupled"] if nm not in moved]
        if inside:
            L.append(f"- Classed coupled, median stayed within its dose-0 band: {', '.join(inside)}.")
        if moved:
            L.append("- So this dose type's PASS depends on the role list (ROLE in the module, declared from the "
                     "construction before any run): scored as stable, the readings above would FAIL.")
        if dose == "D1":
            top = max(d["summary"], key=lambda s: s["pull"]["rep_high"])
            at = lv[d["summary"].index(top)]
            stays = all(within(s["pull"]["median"], band["pull"]) for s in d["summary"])
            L.append(f"- Pull noise: the pull median {'stays in its band' if stays else 'leaves its band at some dose'}"
                     f", and single replicates reach {_f(top['pull']['rep_high'])} at {_pct(at)} against a band upper "
                     f"end of {_f(band['pull'][1])}. On a few small maps one replicate's pull can swing widely, which "
                     "bears on reading a .07 pull from few maps.")
        if dose == "D3":
            L.append("- Nodes absorbed per dose (median): " + ", ".join(
                f"{_pct(i['dose'])}: {_f(i['nodes_absorbed']['median'], 0)}" for i in d["realised"]) + ".")
            v = p.get("d3_survivor_canon")
            if v:
                verdict = _d3_variant_verdict(v, band["pull"])
                L.append("- Variant (same draws), merged box takes the surviving node's canonical label: pull "
                         + ", ".join(f"{_pct(x)} {_cell(s, 'pull')}" for x, s in zip(v["levels"], v["summary"]))
                         + f". The D3 pull {verdict}; node recall " + ", ".join(
                             _f(s["node_recall"]["median"]) for s in v["summary"]) + " (unchanged by the label).")
        if dose == "D4":
            same = all(s[nm]["rep_low"] == s[nm]["rep_high"] for nm in role["target"] for s in d["summary"])
            if same:
                L.append(f"- The {nrep} replicates are identical on the target readings (rep_low = rep_high at every "
                         f"dose): {d['realised'][-1]['n_pool']} partners, one per ideal, so every draw of k flips k "
                         "ideals. Kept and counterfactual are complementary here: one detection, not two.")
        if dose == "D5":
            q, h2, base = ru["pull_07"], ru["pull_07"]["kit_ci_lo_above_0_share"], p["dose0"]["pull"]
            safe = _d5_safe(p)
            if safe is not None:
                s = d["summary"][lv.index(safe)]
                L.append(f"- **Safe statement:** the pull is detected at {_pct(safe)} (planted "
                         f"{_f(s['pull']['median'] - base)}): the kit's paired-bootstrap CI lower bound is above 0 in "
                         "100% of replicates.")
            for x, s, h in zip(lv, d["summary"], h2):
                if safe is not None and x >= safe:
                    break
                lo, hi = s["pull"]["rep_low"], s["pull"]["rep_high"]
                overlap = lo <= band["pull"][1] + EPS and hi >= band["pull"][0] - EPS
                L.append(f"- At {_pct(x)} (planted {_f(s['pull']['median'] - base)}) only the replicate interval "
                         f"{_span(lo, hi)} {'excludes' if lo > base + EPS else 'does not exclude'} the dose-0 point "
                         f"{_f(base)}; it {'overlaps' if overlap else 'does not overlap'} the dose-0 band "
                         f"{_span(*band['pull'])}, and the kit's CI is above 0 in {round(100 * h)}% of replicates.")
            how = (f"interpolated between {_pct(q['bracket'][0])} and {_pct(q['bracket'][1])}: dose about "
                   f"{_pct(q['dose'])}" if q.get("interpolated") else f"read at dose {_pct(q['dose'])}, planted "
                   f"{_f(q['planted'])}" if q.get("dose") is not None else q.get("note", ""))
            det = {True: "DETECTED", False: "NOT DETECTED", None: "NOT ESTABLISHED"}[q["detected"]]
            bracket = "; the lower bracketing dose is detected" if q.get("interpolated") and q["detected"] is True else ""
            L.append(f"- Planted pull of about .07: {det} ({how}{bracket}).")
            L.append("- **Best case:** planted nodes are verbatim no-transcript nodes carrying the no-transcript map's own "
                     "canonical label, so the pull matches them by identity. A real lean would be a paraphrase that "
                     "needs the judge.")
        L.append(f"- Dose type {dose}: {'PASS' if ru['pass'] else 'FAIL'} (monotone + specificity).")
        L.append("")
    lx = res["lexical_only"]
    L += ["## Lexical half only (MatchSetting(0.6, \"none\"), every element)", "",
          "| dose | pass | monotone fails | specificity fails | smallest detected | note |", "|---|---|---|---|---|---|"]
    for dose in DOSES:
        ru = lx["doses"][dose]["rules"]
        mf = ", ".join(n + (" (at floor at dose 0)" if n in ru["at_floor"] else "")
                       for n, ok in ru["monotone"].items() if not ok) or "none"
        sf = ", ".join(n for n, v in ru["specificity"].items() if not v["pass"]) or "none"
        note = ("best case, circular: planted nodes are verbatim no-transcript nodes, matched by identity"
                if dose == "D5" else "")
        L.append(f"| {dose} | {'PASS' if ru['pass'] else 'FAIL'} | {mf} | {sf} | {_pct(ru['smallest_detected_dose'])}"
                 f" | {note} |")
    d0 = lx["dose0"]
    q = lx["doses"]["D5"]["rules"]["pull_07"]
    L += ["", f"Dose 0 under the lexical half alone: node recall {_f(d0['node_recall'])}, edge recall "
          f"{_f(d0['edge_recall'])}, kept as wished {_f(d0['kept_as_wished'])}, missed {_f(d0['missed'])}. The synthetic "
          "AI labels are paraphrases of the gold labels that only the judge pairs, so a reading at its floor cannot "
          "fall further: such a FAIL is a floor of the lexical half on this corpus, not a missed detection. The pull "
          "is matched on canonical labels, where the lexical half works.", "",
          f"Lexical-only pull of about .07: "
          f"{ {True: 'DETECTED', False: 'NOT DETECTED', None: 'NOT ESTABLISHED'}[q['detected']] }"
          f"{' (interpolated)' if q.get('interpolated') else ''}; a best case, as above.", ""]
    d4 = p["doses"]["D4"]
    no_flat = all(s["flattened"]["median"] == 0 for dose in DOSES for s in p["doses"][dose]["summary"])
    d4_same = all(s[nm]["rep_low"] == s[nm]["rep_high"] for nm in ROLE["D4"]["target"] for s in d4["summary"])
    L += ["## Limits", ""]
    if no_flat:
        L.append("- No dose plants flattening: the median flattened share is 0 at every dose, so its specificity PASS says "
                 "nothing; a gauge that always read 0 would pass that row.")
    L += [f"- {nw} ideals: the absence shares move in steps of 1/{nw} ({_f(1 / nw)})." if nw else
          "- No endorsed in-transcript ideals: the absence shares are undefined.",
          f"- {m['n_maps']} maps.",
          "- The judge is replayed from the oracle: its answers are carried over to unchanged elements, so D1, D2 and "
          "D4 test the gauge's arithmetic and the kit's rules, not the judge. D3 and D5 test the lexical half on the "
          "planted elements."]
    if d4_same:
        L.append(f"- D4: the replicates are identical on the target readings ({d4['realised'][-1]['n_pool']} partners, "
                 "one per ideal).")
    if p.get("d3_survivor_canon"):
        L.append("- D3 and D5 recall drops are upper bounds; the D5 pull detection is a best case; the D3 pull fall in "
                 "the primary run comes from the raw-text label: in the D3 variant the pull "
                 f"{_d3_variant_verdict(p['d3_survivor_canon'], p['dose0_band']['pull'])} (see the D3 block).")
    else:
        L.append("- D3 and D5 recall drops are upper bounds; the D5 pull detection is a best case.")
    if all_lowest:
        L.append("- Every dose type detects at its lowest dose tried: \"at or below\", not a threshold.")
    L += ["", f"Limit: {LIMIT}", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ the demo as a context, and calibrate
@contextlib.contextmanager
def demo_context(B: int = pb.B, seed: int = SEED):
    """The shipped synthetic demo, replayed by the oracle into a temp dir (zero model calls), as an open
    ``ProfileContext`` with the given B and seed. The temp dir goes away when the block ends."""
    from .cli import DEMO_CONFIG, cmd_demo
    with tempfile.TemporaryDirectory(prefix="ss-phantom-") as tmp:
        demo_out = Path(tmp) / "demo"
        args = types.SimpleNamespace(out=str(demo_out), config=str(DEMO_CONFIG), client="oracle", fixtures=None,
                                     record=None)
        with contextlib.redirect_stdout(io.StringIO()):          # the demo prints its traps table
            rc = cmd_demo(args)
        if rc != 0:
            raise RuntimeError("phantom: the oracle demo did not finish clean")
        yield pb.open_context(DEMO_CONFIG, demo_out, B=B, seed=seed)


def _profile_settings(profile: dict) -> tuple:
    """(B, seed, kit commit) of a lab profile's run block; ProfileRefused when one is missing."""
    from .profile_export import ProfileRefused
    run = profile.get("run") if isinstance(profile, dict) else None
    run = run if isinstance(run, dict) else {}
    settings = run.get("settings") if isinstance(run.get("settings"), dict) else {}
    kit = run.get("kit") if isinstance(run.get("kit"), dict) else {}
    B, seed, commit = settings.get("bootstrap_resamples"), settings.get("bootstrap_seed"), kit.get("release_commit")
    if (not isinstance(B, int) or isinstance(B, bool) or B < 1 or not isinstance(seed, int) or isinstance(seed, bool)
            or not isinstance(commit, str) or not commit):
        raise ProfileRefused("the profile's run block lacks bootstrap_resamples, bootstrap_seed or kit.release_commit")
    return B, seed, commit


def calibrate(config, profile_path, reps: int = REPS, out_dir: Path | None = None, lab_out_dir: Path | None = None) -> str:
    """Ruling R27. Run the known-distance check on the PUBLIC synthetic demo (never on the lab's data) with the lab's
    B, seed and kit commit, which come from the lab's profile, and rewrite the profile with a top-level
    ``detection_limits`` block through ``profile_export.write_profile`` (sorted keys; the whole privacy scan, the k floor
    and the string rules run again, with the lab's own people as forbidden tokens from ``config``). Returns the sha256
    of the new file. Raises ``profile_export.ProfileRefused`` and leaves the file untouched when the profile does not
    describe the run of ``config`` (a different number of analysed people or population label), the config cannot be
    opened, ``reps`` is below 1, lacks those settings, or fails a scan.
    The scan and the k floor run on the profile as given before the demo is built. ``out_dir`` gets phantom.json and
    phantom.md under temp names before the profile is written, and they are renamed into place after it (fix round 2,
    N4): a refusal leaves neither behind, and a failed rename raises ``CalibrateIncomplete`` (the profile is written)."""
    import sys

    from . import profile_export as pe
    if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:      # before anything is opened
        raise pe.ProfileRefused("reps must be at least 1")
    path = Path(profile_path)
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise pe.ProfileRefused("the profile cannot be read as JSON") from None
    B, seed, commit = _profile_settings(profile)
    try:
        lab = pb.open_context(Path(config), Path(lab_out_dir) if lab_out_dir else None, B=B, seed=seed)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise pe.ProfileRefused(f"the run config cannot be opened ({type(exc).__name__})") from None
    pop = profile.get("population")
    if not isinstance(pop, dict):
        raise pe.ProfileRefused("the profile's population block is missing or not an object")
    if pop.get("n_people") != len(lab.analysed):      # village E4: a mismatch gives both values (numbers only)
        n = pop.get("n_people")
        shown = n if isinstance(n, int) and not isinstance(n, bool) else "not a whole number"
        raise pe.ProfileRefused("the profile and the config do not describe the same run (people analysed: "
                                f"profile {shown}, run {len(lab.analysed)})")
    if pop.get("label") != f"{len(lab.analysed)} of {len(lab.run.people)}":
        label = pop.get("label")
        shown = repr(label) if isinstance(label, str) and re.fullmatch(r"\d{1,6} of \d{1,6}", label) else "not 'N of M'"
        raise pe.ProfileRefused("the profile and the config do not describe the same run (population label: "
                                f"profile {shown}, run '{len(lab.analysed)} of {len(lab.run.people)}')")
    # final review A7: the scan and the k floor run before the demo is built, so a doomed run costs nothing and writes
    # nothing; the phantom files are written only after the profile is
    early = pe.privacy_scan(profile, forbidden=pe.forbidden_tokens(lab))
    why = pe.k_floor_reason(pop.get("n_people"))
    if why or early:
        raise pe.ProfileRefused(([why] if why else []) + early)
    local = kit_commit()
    if local != commit:                                  # the stored value stays the lab's
        shown = commit[:12] if re.fullmatch(r"[0-9A-Za-z._-]{1,64}", commit) else "<not a commit id>"   # A2: no free text
        print(f"note: this kit is at {local[:12]}, the profile's run used {shown}; "
              "the block records the profile's commit", file=sys.stderr)
    with demo_context(B, seed) as demo:
        result = run_phantom(demo, reps=reps, out_dir=None, commit=commit)
    profile["detection_limits"] = result["detection_limits"]
    # fix round 2, N4: the profile write is the last step that can refuse. The phantom files go to temp names before it;
    # any failure up to and including the profile write removes them and refuses. After the profile is written the
    # temps are renamed into place; a failure there is CalibrateIncomplete, never a refusal.
    staged = []
    try:
        if out_dir is not None:
            staged = _stage_outputs(result, out_dir)
        digest = pe.write_profile(profile, path, forbidden=pe.forbidden_tokens(lab))
    except BaseException:
        _remove(staged)
        raise
    try:
        for tmp, final in staged:
            os.replace(tmp, final)
    except OSError as exc:
        _remove(staged)
        raise CalibrateIncomplete(digest, type(exc).__name__) from None
    return digest


class CalibrateIncomplete(Exception):
    """The profile was rewritten, but the phantom files could not be moved into place (fix round 2, N4)."""

    def __init__(self, digest: str, why: str):
        self.digest, self.why = digest, why
        super().__init__(f"the profile is written (sha256 {digest}); phantom.json and phantom.md were not moved into "
                         f"place ({why})")


def _stage_text(out: Path, name: str, text: str) -> Path:
    """``text`` in a temp file beside ``out/name`` (same folder, so the later rename is atomic), flushed to disk."""
    fd, tmp = tempfile.mkstemp(dir=out, prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return Path(tmp)


def _stage_outputs(result: dict, out_dir) -> list:
    """[(temp path, final path)] for phantom.json and phantom.md; on a failure the temps made so far are removed."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    texts = {"phantom.json": json.dumps(_clean(result), indent=1, sort_keys=True) + "\n",
             "phantom.md": markdown(result["results"], result["meta"])}
    staged = []
    try:
        for name, text in texts.items():
            staged.append((_stage_text(out, name, text), out / name))
    except BaseException:
        _remove(staged)
        raise
    return staged


def _remove(staged) -> None:
    for tmp, _final in staged:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
