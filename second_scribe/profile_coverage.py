"""profile_coverage: the coverage cells of the profile exporter (schema ``orgsci-profile/v2``): the ledger, rows 1, 2
and 3, and row 9's two floors (``lexical_floor``, ``any_node_recall``).

Ported from the analysis pack's a1_recall_matchers (``recall_cell``, ``precision_cell``, ``three_way``, ``extract``),
a3_size_adjusted (the ledger's on-the-map count), r12_prior_size (the three per-map size means) and the row 2 / ledger
assembly of ``analysis/public/build_profile.py``. Nothing is imported from ``analysis/``. Zero model calls: the cells are
recomputed from the run's per-participant inputs (``ProfileContext.analysed``) with the profile estimator (pooled ratio
of sums, percentile cluster bootstrap over maps, ``ctx.B`` replicates, ``ctx.seed``).

Inputs from other tasks arrive as parameters:
  * ``labellers``  Task 4's ``labeller_numbers(ctx)``: ``{"judge": {...}, "keyword": {...}, ["judge2": {...}]}``, each a
                   map metric -> number (this module reads ``node_recall``, ``attested_absent_from_map`` and
                   ``unanchored``). The labeller bands are built from the labellers present, so a lab without a second
                   judge family gets a 2-member band. ``None`` (no labeller numbers at all): the cells that sit under a
                   labeller band are ``partial_not_run`` (the value is computed, ``missing`` is ["band"]).
  * ``ladder``     Task 5's ``rung1_size(ctx)``: a dict whose ``"n_ai_nodes"`` is the pooled number of AI boxes of
                   rung 1 (all nodes, located rule), or ``None`` when the run has no ladder.

A cell's ``source`` is "kit:<analysis script name>" (derived cells join the names with "+"): a lab has no gate
and no cleared files.

Cells needing the second judge family end to end (``r_star``) are ``not_run`` with ``needs: ["sidejudge"]``.
"""
from __future__ import annotations

import math

from . import graphs, judge, metrics, provenance
from . import profile_base as pb
from .profile_base import SETTINGS, JUDGE_USING

LABELLER_ORDER = ("judge", "judge2", "keyword")           # the order of the labeller members in profile-v2
_RESTS_RECALL = ["R", "LX", "AL", "IT"]


# ------------------------------------------------------------------ pure cell functions (ported from a1)
def recall_cell(e_in, pairs, rule_set, drop=frozenset()):
    """(numerator, denominator): in-transcript endorsed nodes (minus ``drop``) with a partner in ``rule_set`` / all."""
    keep = set(e_in) - set(drop)
    return len({e for a, e in pairs if a in rule_set and e in keep}), len(keep)


def precision_cell(pairs, rule_set, judged):
    """(numerator, denominator): judged AI nodes of ``rule_set`` with a partner (any endorsed node) / judged AI nodes."""
    return len({a for a, _e in pairs if a in rule_set and a in judged}), len(judged)


def three_way(e_in, pairs_primary, pairs_lenient):
    """(kept, coarsened, omitted) counts over the in-transcript endorsed nodes, any AI node: kept = partner under the
    primary setting; coarsened = partner under the lenient setting but not kept; omitted = neither."""
    kept = {e for _a, e in pairs_primary if e in e_in}
    len_ = {e for _a, e in pairs_lenient if e in e_in} - kept
    return len(kept), len(len_), len(set(e_in) - kept - len_)


def extract(ctx, p) -> dict:
    """Everything the coverage cells need of one map, as plain sets (no kit object leaves this function)."""
    spec = ctx.spec
    arm = metrics.spec_arm(spec)
    mw = int((ctx.cfg.stats or {}).get("min_words", 8))
    g = p.ai[spec]
    pv, md = p.prov[spec], p.modality[spec]
    ids = [n.node_id for n in g.nodes]
    status = {n.node_id: n.status for n in g.nodes}
    nj = set(ids) & set((p.not_judged or {}).get(spec) or ())
    loc = {a for a in ids if provenance.eligible(pv[a], arm, mw)[0]}
    att = {a for a in loc if a not in nj and judge.attested(status[a], md[a])}
    pairs = {name: metrics.match_nodes(g, p.endorsed, setting, p.judged_ai_vs_endorsed[spec]).pairs
             for name, setting in SETTINGS.items()}
    matched_att = {e for a, e in pairs["primary"] if a in att}
    nj_end = {e for a, e in pairs["primary"] if a in nj and a in loc and e not in matched_att}     # as metrics.buckets
    return {"e_in": {n.node_id for n in p.endorsed.nodes if p.in_transcript[n.node_id]}, "nj_end": nj_end,
            "n_endorsed": len(p.endorsed.nodes), "n_ai": len(ids), "n_ai_edges": graphs.structure(g, False)["n_edges"],
            "judged": set(ids) - nj, "att": att, "pairs": pairs, "any": set(ids)}


_MEMO: dict = {}


def _data(ctx) -> list[dict]:
    """The per-map extraction of ``ctx.analysed`` (kept for the last context, since five functions read it)."""
    hit = _MEMO.get("ctx")
    if hit is not None and hit[0] is ctx and hit[1] == [id(p) for p in ctx.analysed]:
        return hit[2]
    data = [extract(ctx, p) for p in ctx.analysed]
    _MEMO["ctx"] = (ctx, [id(p) for p in ctx.analysed], data)
    return data


# ------------------------------------------------------------------ small helpers
def _boot(ctx, nums, dens):
    """(point, lo, hi, ci_ok): ci_ok is False when fewer than B replicates were kept (the CI is then undefined)."""
    point, lo, hi, used = pb.cluster_bootstrap_ratio(nums, dens, B=ctx.B, seed=ctx.seed)
    return point, lo, hi, bool(used >= ctx.B and math.isfinite(lo) and math.isfinite(hi))


def _empty(name, *, rests_on=None, **meta):
    """A ``not_run`` cell for an empty denominator; it keeps ``rests_on`` and ``source`` (``pb.not_run`` takes only
    ``source``, so ``rests_on`` is attached here)."""
    c = pb.not_run(name, [], reason_code="empty_denominator", **meta)
    if rests_on is not None:
        c["rests_on"] = list(rests_on)
    return c


def _sub_bands(per: dict) -> list:
    out = []
    for name, keep in (("judge_using", lambda s: s in JUDGE_USING), ("lexical_only", lambda s: s not in JUDGE_USING)):
        vs = [v for s, v in per.items() if keep(s)]
        out.append({"name": name, "lo": min(vs), "hi": max(vs)})
    return out


def _setting_band(per: dict):
    """The band over the 8 match settings; ``per`` = {setting: value} in SETTINGS order. None when a value is nan
    (the caller then makes the cell ``partial_not_run``, missing ["band"])."""
    if any(not math.isfinite(v) for v in per.values()):
        return None
    vals = [per[s] for s in SETTINGS]
    return pb.band(min(vals), max(vals), "8_settings", members=[{"x": s, "value": per[s]} for s in SETTINGS],
                   subbands=_sub_bands(per))


def _recall_parts(data, setting, rule):
    cells = [recall_cell(d["e_in"], d["pairs"][setting], d["att"] if rule == "attested" else d["any"]) for d in data]
    return [c[0] for c in cells], [c[1] for c in cells]


def _recall_by_setting(data, rule) -> dict:
    return {s: pb.pooled_ratio(*_recall_parts(data, s, rule)) for s in SETTINGS}


def _precision_parts(data, setting):
    cells = [precision_cell(d["pairs"][setting], d["att"], d["judged"]) for d in data]
    return [c[0] for c in cells], [c[1] for c in cells]


def _members(labellers, metric) -> list:
    """The labeller members {"x": labeller, "value": number} for ``metric``: judge, judge2, keyword (the others,
    if any, after them). A labeller without a finite number for the metric is left out."""
    if not labellers:
        return []
    order = [k for k in LABELLER_ORDER if k in labellers] + sorted(k for k in labellers if k not in LABELLER_ORDER)
    out = []
    for lab in order:
        v = (labellers[lab] or {}).get(metric)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            continue
        out.append({"x": lab, "value": v})
    return out


def _labeller_band(members, half_width):
    """The band over labellers (at least 2 members, else None). ``narrow`` = the band is no wider than the half-width
    of the judge number's own 95% CI, on the unrounded values; False when that CI is undefined."""
    if len(members) < 2:
        return None
    vals = [m["value"] for m in members]
    narrow = half_width is not None and (max(vals) - min(vals)) <= half_width
    return pb.labeller_band(members, narrow=bool(narrow))


def _with_band(name, value, band_, **kw):
    """A cell with ``band_``; ``partial_not_run`` (missing ["band"]) when the band could not be built."""
    if band_ is None:
        return pb.partial_not_run(name, value, missing=["band"], **kw)
    return pb.cell(name, value, band=band_, **kw)


# ------------------------------------------------------------------ the ledger
def ledger_counts(ctx) -> dict:
    """The ledger of AI boxes at each of the 8 settings: ``{setting: {on_map, attested_absent_from_map, unanchored,
    ai_boxes_total}}``, pooled over the analysed maps. ``ai_boxes_total`` = AI boxes the judge answered for;
    ``unanchored`` = judged boxes without an attested quote (no matcher involved, so the same at every setting);
    ``on_map`` = attested boxes with a partner among the endorsed nodes; ``attested_absent_from_map`` = attested boxes
    without one, computed exactly as attested - on_map (the three always sum to ``ai_boxes_total``)."""
    data = _data(ctx)
    total = sum(len(d["judged"]) for d in data)
    attested = sum(len(d["att"] & d["judged"]) for d in data)
    out = {}
    for s in SETTINGS:
        on_map = sum(precision_cell(d["pairs"][s], d["att"], d["judged"])[0] for d in data)
        out[s] = {"on_map": on_map, "attested_absent_from_map": attested - on_map, "unanchored": total - attested,
                  "ai_boxes_total": total}
    return out


def _ledger_band(counts) -> dict:
    per = {s: counts[s]["attested_absent_from_map"] for s in SETTINGS}
    vals = list(per.values())
    return pb.band(min(vals), max(vals), "8_settings", members=[{"x": s, "value": per[s]} for s in SETTINGS],
                   subbands=_sub_bands(per))


def _endorsed_total(data) -> int:
    return sum(d["n_endorsed"] for d in data)


def _share_cells(ctx, data):
    """{kept, coarsened, omitted}: (point, lo, hi, ci_ok, total in-transcript nodes)."""
    sp = [three_way(d["e_in"], d["pairs"]["primary"], d["pairs"]["lenient"]) for d in data]
    dens = [len(d["e_in"]) for d in data]
    return {cls: _boot(ctx, [s[i] for s in sp], dens) + (sum(dens),)
            for i, cls in enumerate(("kept", "coarsened", "omitted"))}


def _share_cell(name, res, *, band_=None, want_band=False, **kw):
    """A share cell from ``res`` = (point, lo, hi, ci_ok, n). ``want_band``: the cell is meant to carry a band; when
    ``band_`` is None (a value of the band was nan) the cell is ``partial_not_run`` (missing ["band"]) instead of
    silently losing it. An empty denominator gives ``not_run`` that keeps ``x``, ``rests_on`` and ``source``."""
    point, lo, hi, ok, n = res
    if math.isnan(point):
        return _empty(name, unit="node", kind="share", **kw)
    kw = dict(kw, n=n, unit="node", kind="share")
    kw.update({"ci": (lo, hi)} if ok else {"note": "ci_undefined"})
    if band_ is not None:
        kw["band"] = band_
    elif want_band:
        return pb.partial_not_run(name, point, missing=["band"], **kw)
    return pb.cell(name, point, **kw)


def _judge_numbers(ctx, data):
    """The judge labeller's own bootstrap at the primary setting, for the ``narrow`` flag and the x = labellers cell's
    interval: {metric: (point, lo, hi, ci_ok, scale)} with count metrics on the count scale (ratio * judged boxes).
    ``node_recall`` is a1's number: attested matches / ALL in-transcript endorsed nodes (M-2, analysis ruling), the same
    value and denominator as the row 1 and row 3 ``attested_recall`` cells, so the cell, its CI and its labeller band
    describe one statistic. Nodes whose only possible partner is an AI node the judge never answered for stay in the
    denominator (they are not dropped as ``metrics.buckets`` does for its own ``node_recall``).
    Each statistic gets a fresh ``Random(seed)`` stream (``_boot`` -> ``ctx.B``, ``ctx.seed``).

    The labeller count-CI denominator is the judged boxes (Ruling R13). That differs from a10, which uses all AI
    nodes; the judged boxes are the ledger's own population (the three buckets sum to them)."""
    judged = [len(d["judged"]) for d in data]
    n = sum(judged)
    on = [precision_cell(d["pairs"]["primary"], d["att"], d["judged"])[0] for d in data]
    attested = [len(d["att"] & d["judged"]) for d in data]
    absent = [a - o for a, o in zip(attested, on)]
    unanch = [j - a for j, a in zip(judged, attested)]
    cells = [recall_cell(d["e_in"], d["pairs"]["primary"], d["att"]) for d in data]
    rec_n, rec_d = [c[0] for c in cells], [c[1] for c in cells]
    return {"node_recall": _boot(ctx, rec_n, rec_d) + (1,),
            "attested_absent_from_map": _boot(ctx, absent, judged) + (n,),
            "unanchored": _boot(ctx, unanch, judged) + (n,)}


def _half_width(res):
    point, lo, hi, ok, scale = res
    return (hi - lo) * scale / 2 if ok else None


def ledger_cells(ctx, labellers: dict | None = None) -> list[dict]:
    """The 7 ledger cells, in profile order: omitted_share; attested_absent_from_map and unanchored at the primary
    setting; off_record_endorsed; attested_absent_from_map under the labellers; ai_boxes_total;
    ai_boxes_attested_on_map. The primary count is exact, and its band is the exact count at each of the 8 settings
    (members carry ``value`` only)."""
    data = _data(ctx)
    counts = ledger_counts(ctx)
    prim = counts["primary"]
    n_ai = prim["ai_boxes_total"]
    endorsed = _endorsed_total(data)
    jn = _judge_numbers(ctx, data)
    cells = [_share_cell("omitted_share", _share_cells(ctx, data)["omitted"], rests_on=_RESTS_RECALL,
                         source="kit:a1_recall_matchers")]
    cells.append(pb.cell("attested_absent_from_map", prim["attested_absent_from_map"], x="primary", n=n_ai, unit="ai_box",
                         kind="count", band=_ledger_band(counts),
                         qualifiers=["ceiling", "not_bounded_below", "derived_arithmetic"],
                         rests_on=["R", "LX", "AL", "Q", "MO"], derived="ledger_band",
                         source="kit:s00_reproduce+kit:a1_recall_matchers+kit:a3_size_adjusted"))
    un_band = _labeller_band(_members(labellers, "unanchored"), _half_width(jn["unanchored"]))
    cells.append(_with_band("unanchored", prim["unanchored"], un_band, x="primary", n=n_ai, unit="ai_box",
                            kind="count", qualifiers=["labeller_dependent"], rests_on=["R", "Q", "MO"],
                            source="kit:s00_reproduce+kit:a10_band"))
    cells.append(pb.cell("off_record_endorsed", endorsed - sum(len(d["e_in"]) for d in data), x="primary", n=endorsed,
                         unit="node", kind="count", rests_on=["IT"], source="kit:s00_reproduce"))
    cells.append(_absent_labellers_cell(labellers, jn, prim, n_ai))
    cells.append(pb.cell("ai_boxes_total", n_ai, unit="ai_box", kind="count", source="kit:a1_recall_matchers"))
    cells.append(pb.cell("ai_boxes_attested_on_map", prim["on_map"], x="primary", n=n_ai, unit="ai_box", kind="count",
                         source="kit:a3_size_adjusted"))
    return cells


def _absent_labellers_cell(labellers, jn, prim, n_ai):
    """attested_absent_from_map, x = labellers: the judge's count with its interval, banded over the labellers."""
    point, lo, hi, ok, scale = jn["attested_absent_from_map"]
    kw = dict(x="labellers", n=n_ai, unit="ai_box", kind="count", rests_on=["R", "LX", "AL", "Q", "MO"],
              source="kit:a10_band")
    if ok:
        kw["ci"] = (lo * scale, hi * scale)
    else:
        kw["note"] = "ci_undefined"
    band_ = _labeller_band(_members(labellers, "attested_absent_from_map"), _half_width(jn["attested_absent_from_map"]))
    return _with_band("attested_absent_from_map", prim["attested_absent_from_map"], band_, **kw)


# ------------------------------------------------------------------ row 1
def row1_cells(ctx) -> list[dict]:
    """Row 1 (coverage): kept, coarsened, omitted (shares of the in-transcript endorsed nodes; ``kept`` carries the
    8-setting band of the any-node recall); attested_recall at primary (8-setting band) and lenient; r_star
    (``not_run``, needs sidejudge)."""
    data = _data(ctx)
    shares = _share_cells(ctx, data)
    any_band = _setting_band(_recall_by_setting(data, "any_node"))
    att_band = _setting_band(_recall_by_setting(data, "attested"))
    cells = []
    for cls in ("kept", "coarsened", "omitted"):
        kw = dict(band_=any_band, want_band=True) if cls == "kept" else {}
        cells.append(_share_cell(cls, shares[cls], rests_on=_RESTS_RECALL, source="kit:a1_recall_matchers", **kw))
    rests = ["R", "LX", "AL", "IT", "Q", "MO"]
    for setting in ("primary", "lenient"):
        nums, dens = _recall_parts(data, setting, "attested")
        res = _boot(ctx, nums, dens) + (sum(dens),)
        kw = dict(band_=att_band, want_band=True) if setting == "primary" else {}
        cells.append(_share_cell("attested_recall", res, x=setting, rests_on=rests, source="kit:a1_recall_matchers", **kw))
    cells.append(pb.not_run("r_star", ["sidejudge"], unit="node", kind="ratio", source="kit:a2_calibration"))
    return cells


# ------------------------------------------------------------------ row 2
def row2_cells(ctx, ladder: dict | None = None) -> list[dict]:
    """Row 2 (size), 9 cells in profile-v2 order: ai_boxes, endorsed_boxes (counted from the endorsed maps), the
    derived ai_per_endorsed / ai_per_map / endorsed_per_map, ai_per_endorsed at rung 1 (from ``ladder``, Task 5's
    ``rung1_size``: ``{"n_ai_nodes": ...}``; ``None`` -> ``not_run``, needs ladder), and the three per-map size means
    (point and percentile cluster bootstrap over maps)."""
    data = _data(ctx)
    n_maps = len(data)
    n_ai = sum(len(d["judged"]) for d in data)
    endorsed = _endorsed_total(data)
    cells = [pb.cell("ai_boxes", n_ai, unit="ai_box", kind="count", source="kit:a1_recall_matchers"),
             pb.cell("endorsed_boxes", endorsed, unit="node", kind="count", source="kit:a7_additions_source")]
    if endorsed:
        cells.append(pb.cell("ai_per_endorsed", n_ai / endorsed, kind="ratio", derived="ratio_ai_per_endorsed",
                             source="kit:a1_recall_matchers+kit:a7_additions_source"))
    else:
        cells.append(_empty("ai_per_endorsed", kind="ratio", source="kit:a1_recall_matchers+kit:a7_additions_source"))
    if n_maps:
        cells.append(pb.cell("ai_per_map", n_ai / n_maps, unit="ai_box", kind="count", derived="per_map_mean",
                             source="kit:a1_recall_matchers+kit:a12_ladder"))
        cells.append(pb.cell("endorsed_per_map", endorsed / n_maps, unit="node", kind="count", derived="per_map_mean",
                             source="kit:a7_additions_source"))
    else:                                                    # L-9: no analysed map -> no denominator
        cells.append(_empty("ai_per_map", unit="ai_box", kind="count", source="kit:a1_recall_matchers+kit:a12_ladder"))
        cells.append(_empty("endorsed_per_map", unit="node", kind="count", source="kit:a7_additions_source"))
    n1 = (ladder or {}).get("n_ai_nodes")
    if ladder is None or isinstance(n1, bool) or not isinstance(n1, (int, float)):
        cells.append(pb.not_run("ai_per_endorsed", ["ladder"], x="rung1", kind="ratio", source="kit:a12_ladder"))
    elif endorsed:
        cells.append(pb.cell("ai_per_endorsed", n1 / endorsed, x="rung1", kind="ratio", derived="ratio_ai_per_endorsed",
                             qualifiers=["one_draw", "no_interval"], source="kit:a7_additions_source+kit:a12_ladder"))
    else:
        cells.append(_empty("ai_per_endorsed", x="rung1", kind="ratio",
                            source="kit:a7_additions_source+kit:a12_ladder"))
    ones = [1] * n_maps
    for name, field in (("ai_nodes_per_map", "n_ai"), ("ai_edges_per_map", "n_ai_edges"),
                        ("endorsed_nodes_per_map", "n_endorsed")):
        point, lo, hi, ok = _boot(ctx, [d[field] for d in data], ones)
        if not math.isfinite(point):
            cells.append(_empty(name, unit="map", kind="mean", rests_on=["R"], source="kit:r12_prior_size"))
            continue
        ci = {"ci": (lo, hi)} if ok else {"note": "ci_undefined"}
        cells.append(pb.cell(name, point, n=n_maps, unit="map", kind="mean", rests_on=["R"], source="kit:r12_prior_size", **ci))
    return cells


# ------------------------------------------------------------------ row 3
def row3_cells(ctx, labellers: dict | None = None) -> list[dict]:
    """Row 3 (support): precision at primary (8-setting band), then attested_recall, attested_absent_from_map and
    unanchored at primary, each banded over the labellers present in ``labellers``."""
    data = _data(ctx)
    counts = ledger_counts(ctx)["primary"]
    n_ai = counts["ai_boxes_total"]
    jn = _judge_numbers(ctx, data)
    rests = ["R", "LX", "AL", "Q", "MO"]
    pn, pd = _precision_parts(data, "primary")
    point, lo, hi, ok = _boot(ctx, pn, pd)
    prec_band = _setting_band({s: pb.pooled_ratio(*_precision_parts(data, s)) for s in SETTINGS})
    if math.isnan(point):
        cells = [_empty("precision", x="primary", unit="ai_box", kind="share", rests_on=rests,
                        source="kit:a1_recall_matchers")]
    else:
        kw = {"ci": (lo, hi)} if ok else {"note": "ci_undefined"}
        cells = [_with_band("precision", point, prec_band, x="primary", n=n_ai, unit="ai_box", kind="share",
                            rests_on=rests, source="kit:a1_recall_matchers", **kw)]
    rn, rd = _recall_parts(data, "primary", "attested")
    rpoint, rlo, rhi, rok = _boot(ctx, rn, rd)
    rkw = dict(x="primary", n=sum(rd), unit="node", kind="share", rests_on=rests, source="kit:a1_recall_matchers+kit:a10_band")
    rkw.update({"ci": (rlo, rhi)} if rok else {"note": "ci_undefined"})
    rband = _labeller_band(_members(labellers, "node_recall"), _half_width(jn["node_recall"]))
    if math.isnan(rpoint):
        cells.append(_empty("attested_recall", x="primary", unit="node", kind="share", rests_on=rests,
                            source=rkw["source"]))
    else:
        cells.append(_with_band("attested_recall", rpoint, rband, **rkw))
    cells.append(_with_band("attested_absent_from_map", counts["attested_absent_from_map"],
                            _labeller_band(_members(labellers, "attested_absent_from_map"),
                                           _half_width(jn["attested_absent_from_map"])),
                            x="primary", n=n_ai, unit="ai_box", kind="count", qualifiers=["ceiling", "not_bounded_below"],
                            rests_on=rests, source="kit:s00_reproduce+kit:a10_band"))
    cells.append(_with_band("unanchored", counts["unanchored"],
                            _labeller_band(_members(labellers, "unanchored"), _half_width(jn["unanchored"])),
                            x="primary", n=n_ai, unit="ai_box", kind="count", qualifiers=["labeller_dependent"],
                            rests_on=["R", "Q", "MO"], source="kit:s00_reproduce+kit:a10_band"))
    return cells


# ------------------------------------------------------------------ row 9 floors
def row9_floor_cells(ctx) -> list[dict]:
    """The two floors of row 9: lexical_floor (any-node recall with lexical matching only, threshold .4) and
    any_node_recall at the primary setting."""
    data = _data(ctx)
    out = []
    for name, setting, rests in (("lexical_floor", "lexical_only_04", ["R", "IT", "LX"]),
                                 ("any_node_recall", "primary", ["R", "IT", "LX", "AL"])):
        nums, dens = _recall_parts(data, setting, "any_node")
        out.append(_share_cell(name, _boot(ctx, nums, dens) + (sum(dens),), x=setting, rests_on=rests,
                               source="kit:a1_recall_matchers"))
    return out
