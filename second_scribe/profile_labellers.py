"""profile_labellers: the labeller numbers of the profile exporter (Task 4, split out of profile_floors by Ruling R17;
schema ``orgsci-profile/v2``). Zero model calls.

Ported (pure functions copied, never imported) from the analysis pack:
  a9_keyword_scope   relabel, contradiction_counts, contaminated_counts, map_row, build_numbers (the judge's labels and the
                     keyword labeller's, and, when the run holds a judge2_modality store, the second judge's)
  a10_band           fill_one (the judge2 fill) for the judge2 labeller

Public functions: ``keyword_labeller``, ``labeller_numbers`` (both re-exported by ``profile_floors``). Import layout
(Ruling R19): this module imports only from ``profile_common`` (LABS, _ci_defined, _finite, _hk, part_status), ``profile_f1``
(the wrong-transcript statistics: stat_real / stat_shuf / stat_diff, shuffled_parts, derange_seed) and ``profile_base``.
It never imports ``profile_floors``, which imports this module at its top level.
"""
from __future__ import annotations

import dataclasses

from . import judge, keywords, metrics, provenance
from . import profile_base as pb
from .profile_common import LABS, _ci_defined, _finite, _hk, part_status
from .profile_f1 import derange_seed, shuffled_parts, stat_diff, stat_real, stat_shuf

# ------------------------------------------------------------------ a9 / a10: the labellers
def relabel(p, specs):
    """A copy of ``p`` whose modality labels, for the AI maps of ``specs``, come from keywords.keyword_modality of each
    element's quote, ONLY for elements the judge labelled (an id in p.modality[spec] and not in p.not_judged[spec]).
    Everything else (not_judged, alignments, in_transcript, provenance, statuses) is left as the judge's (a9)."""
    mod = dict(p.modality)
    for spec in specs:
        g = p.ai.get(spec)
        if g is None or spec not in p.modality:
            continue
        nj = set((p.not_judged or {}).get(spec) or ())
        m = dict(p.modality[spec])
        for n in g.nodes:
            if n.node_id in m and n.node_id not in nj:
                m[n.node_id] = keywords.keyword_modality(n.quote)
        for e in g.edges:
            eid = f"e:{e.source}->{e.target}"
            if eid in m and eid not in nj:
                m[eid] = keywords.keyword_modality(e.quote)
        mod[spec] = m
    return dataclasses.replace(p, modality=mod)


def fill_one(md1, nj1, gem):
    """(labels, not_judged, n_filled, n_denominator) of one map and spec (a10_band's fill): ``md1`` = judge 1's labels,
    ``nj1`` its not-judged ids, ``gem`` = {"labels", "not_judged"} of the second judge or None. Element k is missing iff
    k is not a judge2 label or judge2 left it not judged. Missing and judged by judge 1: filled with judge 1's label.
    Judge 1 not judged: stays not judged."""
    lab = dict(gem["labels"]) if gem is not None else {}
    nj_g = set(gem["not_judged"]) if gem is not None else set(md1)
    new_nj, n_fill, n_den = set(), 0, 0
    for key in md1:
        judged1 = key not in nj1
        n_den += judged1
        if key not in lab or key in nj_g:
            if judged1:
                lab[key] = md1[key]
                n_fill += 1
            else:
                lab.setdefault(key, md1[key])
                new_nj.add(key)
        elif not judged1:
            new_nj.add(key)
    return lab, new_nj, n_fill, n_den


def _judge2_view(p, specs):
    """``p`` with the FILLED second judge's labels in place of the first judge's (the kit's _as_judge2)."""
    mod2, nj2 = {}, {}
    for spec in specs:
        g = p.ai.get(spec)
        if g is None or spec not in p.modality:
            continue
        nj1 = set((p.not_judged or {}).get(spec) or ())
        gem = None
        if p.modality_judge2 and spec in p.modality_judge2:
            gem = {"labels": p.modality_judge2[spec], "not_judged": list((p.not_judged_judge2 or {}).get(spec) or ())}
        lab, new_nj, _f, _d = fill_one(p.modality[spec], nj1, gem)
        mod2[spec], nj2[spec] = lab, new_nj
    return dataclasses.replace(p, modality=mod2, not_judged=nj2)


def has_judge2(ctx) -> bool:
    """True when the run holds a second judge's modality labels for at least one analysed map (the ``judge2_modality``
    store of the work dir)."""
    return any(p.modality_judge2 for p in ctx.analysed)


def contradiction_counts(p, spec, mw):
    """(n located nodes the labeller calls wished, how many of them drawn experienced, n located nodes called actual,
    how many of them drawn wished): the kit's contradiction_rate, one map."""
    g = p.ai[spec]
    pv, md = p.prov[spec], p.modality[spec]
    nj = set((p.not_judged or {}).get(spec) or ())
    arm = metrics.spec_arm(spec)
    located = [n for n in g.nodes if n.node_id not in nj and provenance.eligible(pv[n.node_id], arm, mw)[0]]
    wm = [n for n in located if md[n.node_id] == "wished"]
    am = [n for n in located if md[n.node_id] == "actual"]
    return (len(wm), sum(1 for n in wm if n.status == "experienced"),
            len(am), sum(1 for n in am if n.status == "wished"))


def contaminated_counts(p, full_spec, mw):
    """(n attested eligible AI elements of the full arm, how many are interviewer-voiced or in mapping talk): the
    kit's leakage().contaminated_share, one map. (0, 0) when the full-arm map does not exist."""
    if p.ai.get(full_spec) is None:
        return 0, 0
    nj = set((p.not_judged or {}).get(full_spec) or ())
    bad = [(pr.speaker == "interviewer" or pr.mapping_talk_turn)
           for eid, status, pr, mod in metrics._elements(p, full_spec)
           if eid not in nj and provenance.eligible(pr, "full", mw)[0] and judge.attested(status, mod)]
    return len(bad), sum(bad)


def map_row(p, spec, full_spec, ps, mw):
    arm = metrics.spec_arm(spec)
    b = metrics.buckets(p, spec, arm, ps, min_words=mw)
    a = metrics.absences(p, spec, arm, ps, min_words=mw)
    wm_n, wm_bad, am_n, am_bad = contradiction_counts(p, spec, mw)
    c_n, c_bad = contaminated_counts(p, full_spec, mw)
    return {"matched": b["matched"], "missed": b["missed"], "recall_den": b["matched"] + b["missed"] + b["n_endorsed_not_judged"],
            "attested_absent": b["attested_absent_from_map"], "unanchored": b["unanchored"],
            "n_ai": b["n_ai_nodes"], "n_not_judged": b["n_not_judged"],
            "n_wished": a["n_wished"], "leaked": a["leaked_as_existing"],
            "counterfactual": a["counterfactual_leakage"], "adequacy": a["adequacy_flattening"],
            "indeterminate": a["leak_split_indeterminate"],
            "wm_n": wm_n, "wm_bad": wm_bad, "am_n": am_n, "am_bad": am_bad, "c_n": c_n, "c_bad": c_bad}


def _label_maps(ctx, labs) -> dict:
    """{labeller: analysed maps under that labeller's modality labels}."""
    spec, full = ctx.spec, metrics.FULL_SPEC
    out = {}
    for lab in labs:
        if lab == "judge":
            out[lab] = list(ctx.analysed)
        elif lab == "keyword":
            out[lab] = [relabel(p, (spec, full)) for p in ctx.analysed]
        elif lab == "judge2":
            out[lab] = [_judge2_view(p, (spec, full)) for p in ctx.analysed]
        else:
            raise ValueError(lab)
    return out


def n_label_diffs(judge_maps, other_maps, spec) -> int:
    """Number of elements of ``spec`` whose label differs between two labellers, over the population."""
    return sum(1 for pj, pk in zip(judge_maps, other_maps) if pj.ai.get(spec) is not None
               for eid, m in pj.modality.get(spec, {}).items() if pk.modality.get(spec, {}).get(eid) != m)


# the numbers a9 reads under each labeller: key -> (metric, unit, kind, numerator field, denominator field)
_RATIOS = (("node_recall", "node_recall", "node", "estimate", "matched", "recall_den"),
           ("attested_absent", "attested_absent_from_map", "node", "count", "attested_absent", "n_ai"),
           ("unanchored", "unanchored", "node", "count", "unanchored", "n_ai"),
           ("counterfactual", "counterfactual_leakage", "ideal", "count", "counterfactual", "n_wished"),
           ("adequacy", "adequacy_flattening", "ideal", "count", "adequacy", "n_wished"),
           ("contradiction_wm", "contradiction_rate", "node", "estimate", "wm_bad", "wm_n"),
           ("contradiction_am", "contradiction_rate", "node", "estimate", "am_bad", "am_n"),
           ("contaminated", "contaminated_share", "element", "estimate", "c_bad", "c_n"))
_SHUF = (("shuf_real", "real_recall_pooled", stat_real, 1), ("shuf_shuffled", "shuffled_recall_pooled", stat_shuf, 3),
         ("shuf_diff", "shuffled_pairs", stat_diff, 1))


def build_numbers(ctx, labs=("judge", "keyword")) -> dict:
    """The a9 numbers for each labeller of ``labs``: {key: {"metric", "unit", "kind", "by": {lab: {"point", "ci_lo",
    "ci_hi", "ci_ok", "n", "count"} or None}}}. A count's point and bounds are on the COUNT scale (share x
    denominator); ``n`` is the denominator total and ``count`` the numerator total. ``by[lab]`` is None when the number is
    withheld under that labeller (the shuffled-pairs part fails the subset rule, or there are fewer than 2 maps).
    Also "label_diffs": {"primary": int, "full": int} = elements whose label differs between the judge and the other
    labellers' (the largest over the other labellers)."""
    spec, full, hk = ctx.spec, metrics.FULL_SPEC, _hk(ctx)
    mw, ps, n = hk["min_words"], pb.SETTINGS["primary"], len(ctx.analysed)
    by_maps = _label_maps(ctx, labs)
    rows_by = {lab: [map_row(p, spec, full, ps, mw) for p in by_maps[lab]] for lab in labs}
    out = {}
    for key, metric, unit, kind, num, den in _RATIOS:
        by = {}
        for lab in labs:
            rows = rows_by[lab]
            pt, lo, hi, used = pb.cluster_bootstrap_ratio([r[num] for r in rows], [r[den] for r in rows],
                                                          B=ctx.B, seed=ctx.seed)
            d, s = sum(r[den] for r in rows), sum(r[num] for r in rows)
            sc = d if kind == "count" else 1
            by[lab] = {"point": pt * sc if kind == "count" else pt, "ci_lo": lo * sc, "ci_hi": hi * sc,
                       "ci_ok": _ci_defined(lo, hi, used, ctx.B), "n": d, "count": s}
        out[key] = {"metric": metric, "unit": unit, "kind": kind, "by": by}
    shuf = {}
    if n >= 2:
        dseed = derange_seed(hk)
        for lab in labs:
            shuf[lab] = shuffled_parts(by_maps[lab], spec, ps, mw, hk["n_derangements"], dseed)
    for key, metric, stat, den_i in _SHUF:
        by = {}
        for lab in labs:
            if lab not in shuf or part_status(shuf[lab][2], n) == "suppressed":
                by[lab] = None
                continue
            pt, lo, hi, used = pb.cluster_bootstrap(shuf[lab][0], stat, B=ctx.B, seed=ctx.seed)
            by[lab] = {"point": pt, "ci_lo": lo, "ci_hi": hi, "ci_ok": _ci_defined(lo, hi, used, ctx.B),
                       "n": sum(x[den_i] for x in shuf[lab][0]), "count": 0}
        out[key] = {"metric": metric, "unit": "node", "kind": "estimate", "by": by}
    others = [lab for lab in labs if lab != "judge"]
    diffs = {"primary": 0, "full": 0}
    if "judge" in by_maps:
        diffs = {"primary": max([n_label_diffs(by_maps["judge"], by_maps[lab], spec) for lab in others] or [0]),
                 "full": max([n_label_diffs(by_maps["judge"], by_maps[lab], full) for lab in others] or [0])}
    out["label_diffs"] = diffs
    return out


def keyword_labeller(ctx) -> dict:
    """The a9 numbers under the judge's labels and under the keyword labeller (``keywords.keyword_modality`` applied to
    each quote, only to elements the judge labelled: a9's ``relabel``; the judge's not-judged set is kept, so the element
    set is the same under both). Consumed by Tasks 2 and 6 for the labeller-band members.

    Returns {number key: {"metric", "unit", "kind", "by": {"judge": N, "keyword": N}}, ..., "label_diffs": {...}} with
    N = {"point", "ci_lo", "ci_hi", "ci_ok", "n", "count"} (see ``build_numbers``) or None when withheld. Keys:
    node_recall, attested_absent, unanchored, counterfactual, adequacy, contradiction_wm (quote wished, marked
    experienced = the profile's matcher_free_rate), contradiction_am (quote actual, marked wished), contaminated,
    shuf_real, shuf_shuffled, shuf_diff (the wrong-transcript difference)."""
    return build_numbers(ctx, ("judge", "keyword"))


# the profile's metric names -> (a9 number key)
_LABELLER_METRICS = (("node_recall", "node_recall"), ("attested_absent_from_map", "attested_absent"),
                     ("unanchored", "unanchored"), ("matcher_free_rate", "contradiction_wm"),
                     ("contradiction_actual_as_wished", "contradiction_am"), ("contaminated_share", "contaminated"),
                     ("wrong_transcript_difference", "shuf_diff"))


def labeller_numbers(ctx) -> dict:
    """The point numbers of the labeller bands, one dict per labeller.

    Shape (ledger Ruling R1; consumed by Tasks 2 and 6):
        {"judge":   {metric: number or None, ...},
         "keyword": {metric: number or None, ...},
         "judge2":  {metric: number or None, ...}}        # the "judge2" key only when the run holds a judge2_modality store
    with metric in node_recall (matched / ALL in-transcript endorsed nodes, a share: a1's denominator, M-2; the kit's
    ``buckets`` leaves out the endorsed nodes only an unjudged AI node could match), attested_absent_from_map and unanchored (counts of
    AI nodes, at the primary setting), matcher_free_rate (the share of located nodes called wished that are drawn
    experienced; the profile's matcher-free rate), contradiction_actual_as_wished (the share of located nodes called actual
    that are drawn wished), contaminated_share (the share of attested AI elements of the full arm that are interviewer-
    voiced or in mapping talk) and wrong_transcript_difference (real minus shuffled recall). Every value is a finite
    int or float, or None when the metric cannot be computed under that labeller (an empty denominator, fewer than 2
    maps, or the shuffled-pairs part withheld by the subset rule). Every labeller dict always holds all 7 metric keys
    (Ruling R16), so a consumer reads ``d[metric]`` and tests ``is None``.
    judge2 is the FILLED second judge (a10_band's recipe: a missing or not-judged label is replaced by judge 1's)."""
    labs = ("judge", "judge2", "keyword") if has_judge2(ctx) else ("judge", "keyword")
    nums = build_numbers(ctx, labs)
    out = {}
    for lab in labs:
        d = {}
        for metric, key in _LABELLER_METRICS:
            v = nums[key]["by"].get(lab)
            d[metric] = v["point"] if v is not None and _finite(v["point"]) else None
        out[lab] = d
    return out


def _labeller_band(nums, key, *, labs):
    """The labeller band of one a9 number (Task 2's rule, Ruling R16): members = the labellers with a finite value (in LABS
    order); a band needs at least 2 of them, else None (the caller then emits a ``partial_not_run`` cell missing its
    band). ``narrow`` = the band is no wider than the judge's own 95% half-width (a10_band's band_narrow); False, never
    None, when the judge's interval is undefined."""
    members = [{"x": lab, "value": nums[key]["by"][lab]["point"]} for lab in LABS
               if lab in labs and nums[key]["by"].get(lab) is not None and _finite(nums[key]["by"][lab]["point"])]
    if len(members) < 2:
        return None
    j = nums[key]["by"].get("judge")
    narrow = False
    if j is not None and j["ci_ok"] and _finite(j["ci_lo"], j["ci_hi"]):
        vals = [m["value"] for m in members]
        narrow = bool(max(vals) - min(vals) <= (j["ci_hi"] - j["ci_lo"]) / 2)
    return pb.labeller_band(members, narrow=narrow)
