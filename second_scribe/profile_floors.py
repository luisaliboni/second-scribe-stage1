"""profile_floors: the lead, rows 5 (absences) and 7 (floors) of the profile exporter (schema ``orgsci-profile/v2``). Zero
model calls: every number is computed from a finished run opened read-only (``profile_base.open_context``).

Import layout (Ruling R19; no module-level cycle, no lazy imports). ``profile_common`` is the leaf (constants and small
helpers); ``profile_f1`` holds the f1 statistics and imports only ``profile_common`` and ``profile_base``;
``profile_labellers`` (a9 / a10) and ``profile_slant`` (row 6: a5 and a7) import only from ``profile_common``,
``profile_f1`` and ``profile_base``. This module sits on top: it imports the labellers and the slant at module level and
builds the cells that need them. It re-exports the moved public names (``K_CELL``, ``part_status``, ``derange_seed``,
``stat_real``, ``keyword_labeller``, ``labeller_numbers`` and the rest) so ``profile_floors.<name>`` keeps working.

Ported (pure functions copied, never imported) from the analysis pack, with the exec loader, the gate glue (records,
vocab, ``cell_under_10`` gate records, EXPECTED dicts, asserts tied to the real study's 43 maps) and the markdown dropped:
  f1_floors_checks   ai_counts, prior_counts, prior_units, stat_*, floor1_numbers, derange_seed, shuffled_parts,
                     stat_real / stat_shuf / stat_diff, one_sided_upper_bound (now in ``profile_f1``); the located-rule
                     absence counts (here)
  r12_prior_size     the lead's size cells (AI / no-interview / endorsed nodes and edges per map)
Estimator (profile_base): pooled ratio of sums, percentile cluster bootstrap over maps with ``ctx.B`` and ``ctx.seed``
(a fresh ``random.Random(seed)`` per call). The kit's own functions do the matching, the buckets and the absence
classes; nothing is copied from ``aggregates.json``.

Public functions: ``lead_cells``, ``row5_cells``, ``row7_cells``, ``size_claims``, ``absence_counts`` (and, re-exported,
``keyword_labeller``, ``labeller_numbers``).

Rules this module applies (so a cell is never invented):
  * a value that is not finite, or whose group is empty, is a ``not_run`` cell carrying a ``reason_code`` (``needs`` is
    used only for the four documented inputs; the sentence for a code is in ``profile_strings.json``, schema section 13);
  * the subset rule of f1 / r12 (k = maps a part leaves out): k = 0 releases, 10 <= k <= n - 10 releases, anything else
    withholds the part's cells (``not_run`` with reason_code ``cell_under_10``); k is never written;
  * a bootstrap that keeps fewer than B replicates has no interval (the cell carries the note ``ci_undefined``);
  * the no-interview size cells are points only (note ``pooled_only``), for every lab: with two no-interview maps an
    interval would expose the per-role sizes (r12_prior_size);
  * derived cells carry ``formula_code`` (the text is in the strings file), never the formula;
  * the f1 cells of the lead and row 7 are always computed (Ruling R15): they read the work and no-interview maps
    only; f1's Part 3 (sign stability), the only part that reads the stability grid, feeds no profile cell, so the
    grid spec ``removed/p0/T0.7`` is not needed.
"""
from __future__ import annotations

import math

from . import metrics
from . import profile_base as pb
from .graphs import structure
# the moved names are re-exported from here (Ruling R19)
from .profile_common import (EPS, K_CELL, LABS, SRC, _ci_defined, _est, _finite, _hk, _mean, _nr, part_status)    # noqa: F401
from .profile_f1 import (_floor1, _floor2, _in_ids, _permutation, ai_counts, derange_seed, floor1_numbers,    # noqa: F401
                         n_left_out_part1, one_sided_upper_bound, prior_counts, prior_units, shuffled_parts,
                         stat_ai, stat_ai_minus_prior, stat_diff, stat_prior, stat_real, stat_shuf)
from .profile_labellers import (_labeller_band, build_numbers, has_judge2, keyword_labeller,                # noqa: F401
                                labeller_numbers)
from .profile_slant import recovery


# ------------------------------------------------------------------ f1 Part 4: the absence classes (located rule)
def absence_counts(ctx, setting_key: str) -> dict:
    """Pooled absence classes of the analysed maps at one setting ("primary" or "lenient") by the kit's
    ``metrics._pooled_absences`` (f1's route E recount equals it): n_wished, recovered_as_wished, leaked_as_existing,
    adequacy_flattening, counterfactual_leakage, leak_split_indeterminate, missed, leaked_unanchored, unanchored_wished
    and ``n_matched`` (= recovered + leaked_as_existing: ideals matched by a located partner)."""
    mw = _hk(ctx)["min_words"]
    pa = metrics._pooled_absences(ctx.analysed, ctx.spec, pb.SETTINGS[setting_key], mw)
    out = {k: pa[k] for k in ("n_wished", "recovered_as_wished", "leaked_as_existing", "adequacy_flattening",
                              "counterfactual_leakage", "leak_split_indeterminate", "missed", "leaked_unanchored",
                              "unanchored_wished")}
    out["n_matched"] = out["recovered_as_wished"] + out["leaked_as_existing"]
    return out


# ------------------------------------------------------------------ r12: the size of the maps
AI_N, PRIOR_N, AI_E, PRIOR_E, END_N, END_E, USED = range(7)
ZERO_UNIT = (0, 0, 0, 0, 0, 0, 0)


def map_unit(p, spec):
    """One map's sizes (AI nodes, no-interview nodes, AI edges, no-interview edges, endorsed nodes, endorsed edges, 1) by
    graphs.structure(g, False), or None when the map has no AI map or no no-interview map (left out of the part)."""
    g = p.ai.get(spec)
    pk = metrics._prior_key(p)
    prior = p.ai.get(pk) if pk else None
    if g is None or prior is None:
        return None
    sa, sp, se = structure(g, False), structure(prior, False), structure(p.endorsed, False)
    return sa["n_nodes"], sp["n_nodes"], sa["n_edges"], sp["n_edges"], se["n_nodes"], se["n_edges"], 1


def _holds_units(p, spec) -> bool:
    g = p.ai.get(spec)
    pk = metrics._prior_key(p)
    prior = p.ai.get(pk) if pk else None
    return bool((g is not None and g.nodes) or (prior is not None and prior.nodes) or p.endorsed.nodes)


def _per_map(field):
    def stat(us):
        return pb.pooled_ratio([u[field] for u in us], [u[USED] for u in us])
    return stat


def _per_map_diff(a, b):
    def stat(us):
        return pb.pooled_ratio([u[a] - u[b] for u in us], [u[USED] for u in us])
    return stat


def _prior_over_ai(us) -> float:
    return pb.pooled_ratio([u[PRIOR_N] for u in us], [u[AI_N] for u in us])


_BOOTED = (("ai_nodes_per_map", _per_map(AI_N)), ("ai_minus_prior_nodes_per_map", _per_map_diff(AI_N, PRIOR_N)),
           ("ai_edges_per_map", _per_map(AI_E)), ("ai_minus_prior_edges_per_map", _per_map_diff(AI_E, PRIOR_E)),
           ("endorsed_nodes_per_map", _per_map(END_N)))
_POINTS = (("prior_nodes_per_map", _per_map(PRIOR_N)), ("prior_edges_per_map", _per_map(PRIOR_E)),
           ("prior_over_ai_nodes", _prior_over_ai))


def _size(ctx) -> dict:
    """r12's numbers: {"status", "booted": {metric: (pt, lo, hi, used)}, "points": {metric: pt}}. The status word is the
    subset rule's (k = maps with units that the part leaves out)."""
    maps, spec = ctx.analysed, ctx.spec
    rows = [map_unit(p, spec) for p in maps]
    units = [r if r is not None else ZERO_UNIT for r in rows]
    k_left = sum(1 for p, r in zip(maps, rows) if r is None and _holds_units(p, spec))
    return {"status": part_status(k_left, len(maps)),
            "booted": {m: pb.cluster_bootstrap(units, st, B=ctx.B, seed=ctx.seed) for m, st in _BOOTED},
            "points": {m: st(units) for m, st in _POINTS}}


def size_claims(ctx) -> dict:
    """r12's claims, reported as computed and never raised on (the real study's own build stops when
    ``prior_map_smaller`` is false; the kit does not): ``prior_map_smaller`` = the 95% lower bound of AI minus no-interview
    nodes per map is above 0 (false when that interval is undefined or the part is withheld); ``covers_all_maps`` = no
    analysed map is left out of the part."""
    s = _size(ctx)
    pt, lo, hi, used = s["booted"]["ai_minus_prior_nodes_per_map"]
    ok = _ci_defined(lo, hi, used, ctx.B) and s["status"] != "suppressed"
    return {"prior_map_smaller": bool(ok and lo > 0), "covers_all_maps": s["status"] == "full"}


_SIZE_TABLE = (  # (r12 metric, cell name, kind, point only)
    ("ai_nodes_per_map", "ai_nodes_per_map", "mean", False),
    ("prior_nodes_per_map", "no_interview_nodes_per_map", "mean", True),
    ("ai_minus_prior_nodes_per_map", "ai_minus_no_interview_nodes_per_map", "difference", False),
    ("ai_edges_per_map", "ai_edges_per_map", "mean", False),
    ("prior_edges_per_map", "no_interview_edges_per_map", "mean", True),
    ("ai_minus_prior_edges_per_map", "ai_minus_no_interview_edges_per_map", "difference", False),
    ("endorsed_nodes_per_map", "endorsed_nodes_per_map", "mean", False),
    ("prior_over_ai_nodes", "no_interview_over_ai_nodes", "ratio", True))


def _size_cells(ctx, size) -> list[dict]:
    n = len(ctx.analysed)
    cells = []
    for metric, name, kind, point_only in _SIZE_TABLE:
        meta = (None, "map", kind)
        if size["status"] == "suppressed":
            cells.append(_nr(name, meta, "cell_under_10", source=SRC["r12"]))
            continue
        if point_only:
            pt = size["points"][metric]
            if not _finite(pt):
                cells.append(_nr(name, meta, "not_computable", source=SRC["r12"]))
                continue
            cells.append(pb.cell(name, pt, n=n, unit="map", kind=kind, note="pooled_only", source=SRC["r12"],
                                 qualifiers=["size_beside_lead", "pooled_only", "no_interval"]))
        else:
            cells.append(_est(ctx, name, meta, size["booted"][metric], source=SRC["r12"], n=n,
                              qualifiers=["size_beside_lead"]))
    return cells


# ------------------------------------------------------------------ the lead and row 7
def _intr(ctx) -> int:
    """In-transcript endorsed nodes over the analysed maps (the unit count behind the listening differences)."""
    return sum(len(_in_ids(p)) for p in ctx.analysed)


def _floor_cells(ctx) -> list[dict]:
    """The 13 cells of row 7 (and the first 13 of the lead), in the skeleton's order."""
    maps = ctx.analysed
    n, intr = len(maps), _intr(ctx)
    f1 = _floor1(ctx)
    cells = []

    def cells_for(sk):
        got = f1[("all", sk)]
        ai, pr, df = (got["nums"][k] for k in ("ai", "prior", "diff"))
        if got["status"] == "suppressed":
            return [_nr(nm, (sk, "node", kd), "cell_under_10", source=SRC["f1"])
                    for nm, kd in (("ai_recall", "share"), ("no_interview_recall", "share"),
                                   ("listening_added", "difference"))]
        return [_est(ctx, "ai_recall", (sk, "node", "share"), ai[:4], source=SRC["f1"], n=ai[4]),
                _est(ctx, "no_interview_recall", (sk, "node", "share"), pr[:4], source=SRC["f1"], n=pr[4]),
                _est(ctx, "listening_added", (sk, "node", "difference"), df[:4], source=SRC["f1"], n=intr)]
    cells += cells_for("primary") + cells_for("lenient")
    for sk in ("primary", "lenient"):
        got = f1[("attested_line", sk)]
        if got["status"] == "suppressed":
            cells.append(_nr("listening_added_attested_line", (sk, "node", "difference"), "cell_under_10",
                             source=SRC["f1"]))
        else:
            cells.append(_est(ctx, "listening_added_attested_line", (sk, "node", "difference"),
                              got["nums"]["diff"][:4], source=SRC["f1"], n=intr))
    f2 = _floor2(ctx)
    if f2 is None or f2["status"] == "suppressed":
        note = "cell_under_10" if f2 is not None else "too_few_maps"
        cells += [_nr(nm, ("primary", "node", kd), note, source=SRC["f1"])
                  for nm, kd in (("real_recall", "share"), ("shuffled_recall", "share"),
                                 ("wrong_transcript_difference", "difference"))]
    else:
        bt = f2["boot"]
        cells.append(_est(ctx, "real_recall", ("primary", "node", "share"), bt["real"][:4], source=SRC["f1"],
                          n=bt["real"][4]))
        cells.append(_est(ctx, "shuffled_recall", ("primary", "node", "share"), bt["shuf"][:4], source=SRC["f1"],
                          n=bt["shuf"][4]))
        labs = ("judge", "judge2", "keyword") if has_judge2(ctx) else ("judge", "keyword")
        nums = build_numbers(ctx, labs)
        band = _labeller_band(nums, "shuf_diff", labs=labs)
        kw = {"band": band} if band is not None else {"missing": ["band"]}
        cells.append(_est(ctx, "wrong_transcript_difference", ("primary", "node", "difference"), bt["diff"][:4],
                          source=SRC["f1"], n=bt["diff"][4], **kw))
    pm = _permutation(ctx)
    if pm is None or pm["status"] == "suppressed" or not all(
            _finite(pm["perm"][k]) for k in ("mean_rank", "expected_rank_null", "p")):
        note = "cell_under_10" if pm is not None and pm["status"] == "suppressed" else "not_computable"
        cells += [_nr("own_map_mean_rank", ("primary", "map", "rank"), note, source=SRC["f1"]),
                  _nr("rank_null_expectation", ("primary", "map", "rank"), note, source=SRC["f1"])]
    else:
        perm = pm["perm"]
        cells.append(pb.cell("own_map_mean_rank", perm["mean_rank"], x="primary", n=n, unit="map", kind="rank",
                             p=perm["p"], source=SRC["f1"]))
        cells.append(pb.cell("rank_null_expectation", perm["expected_rank_null"], x="primary", n=n, unit="map",
                             kind="rank", source=SRC["f1"]))
    return cells


def _derived_cells(ctx, floor, floor_note, size) -> list[dict]:
    """yield_per_box (ai, no_interview), equal_size_bound, listening_edge_equal_size: arithmetic on the numbers above
    (build_profile.derived_lines, unrounded inputs). Descriptive; not a test."""
    maps = ctx.analysed
    mw, ps = _hk(ctx)["min_words"], pb.SETTINGS["primary"]
    q = ["not_a_test", "derived_arithmetic", "descriptive", "size_beside_lead"]
    fl = q + ["floor_not_estimate"]
    metas = {("yield_per_box", "ai"): ("ai", "node", "ratio"), ("yield_per_box", "no_interview"): ("no_interview", "node", "ratio"),
             ("equal_size_bound", "no_interview"): ("no_interview", "node", "share"),
             ("listening_edge_equal_size", "no_interview"): ("no_interview", "node", "difference")}
    out = []
    # yield per AI box: any-node primary matched count / AI boxes judged
    n_judged = 0
    for p in maps:
        b = metrics.buckets(p, ctx.spec, metrics.spec_arm(ctx.spec), ps, min_words=mw)
        n_judged += b["n_ai_nodes"] - b["n_not_judged"]
    matched = sum(ai_counts(p, ctx.spec, ps, mw, False)[0] for p in maps)
    y_ai = matched / n_judged if n_judged else math.nan
    if _finite(y_ai):
        out.append(pb.cell("yield_per_box", y_ai, x="ai", unit="node", kind="ratio", derived=True, source=SRC["a1"],
                           formula_code="yield_per_box:ai",
                           qualifiers=q))
    else:
        out.append(_nr("yield_per_box", metas[("yield_per_box", "ai")], "not_computable", source=SRC["a1"]))

    def dependent(key, note):
        return _nr(key[0], metas[key], note, source="+".join((SRC["f1"], SRC["r12"])))
    keys = [("yield_per_box", "no_interview"), ("equal_size_bound", "no_interview"),
            ("listening_edge_equal_size", "no_interview")]
    if floor is None:
        return out + [dependent(k, floor_note) for k in keys]
    if size["status"] == "suppressed":
        return out + [dependent(k, "cell_under_10") for k in keys]
    ai_r, pr_r = floor["ai"], floor["prior"]
    ai_pm = size["booted"]["ai_nodes_per_map"][0]
    pr_pm = size["points"]["prior_nodes_per_map"]
    if not _finite(ai_r, pr_r, ai_pm, pr_pm) or not pr_pm:
        return out + [dependent(k, "not_computable") for k in keys]
    n = len(maps)
    y_pr = pr_r * _intr(ctx) / (pr_pm * n)
    bound = pr_r * ai_pm / pr_pm
    edge = ai_r - bound
    out.append(pb.cell("yield_per_box", y_pr, x="no_interview", unit="node", kind="ratio", derived=True,
                       source="+".join((SRC["a1"], SRC["f1"], SRC["r12"])), qualifiers=q,
                       formula_code="yield_per_box:no_interview"))
    out.append(pb.cell("equal_size_bound", bound, x="no_interview", unit="node", kind="share", derived=True,
                       source="+".join((SRC["f1"], SRC["r12"])), qualifiers=fl,
                       formula_code="equal_size_bound"))
    out.append(pb.cell("listening_edge_equal_size", edge, x="no_interview", unit="node", kind="difference",
                       derived=True, source="+".join((SRC["f1"], SRC["r12"])), qualifiers=fl,
                       formula_code="listening_edge_equal_size"))
    return out


def _primary_recalls(ctx):
    """{"ai", "prior"}: the pooled all-line recalls at the primary setting (None when the part is withheld)."""
    f1 = _floor1(ctx)[("all", "primary")]
    if f1["status"] == "suppressed":
        return None
    return {"ai": f1["nums"]["ai"][0], "prior": f1["nums"]["prior"][0]}


def row7_cells(ctx) -> list[dict]:
    """Row 7, the floors: the 13 cells of the lead's first block, in the skeleton's order: ai_recall, no_interview_recall,
    listening_added (primary, lenient), listening_added_attested_line (primary, lenient), real_recall, shuffled_recall,
    wrong_transcript_difference (with its band over the labellers present), own_map_mean_rank, rank_null_expectation.
    Always computed (Ruling R15): no stability-grid spec is needed."""
    return _floor_cells(ctx)


def lead_cells(ctx) -> list[dict]:
    """The lead (25 cells): row 7's 13 floor cells, then the size cells of r12_prior_size (ai_nodes_per_map,
    no_interview_nodes_per_map [point only], ai_minus_no_interview_nodes_per_map, ai_edges_per_map,
    no_interview_edges_per_map [point only], ai_minus_no_interview_edges_per_map, endorsed_nodes_per_map,
    no_interview_over_ai_nodes [point only]) and the four derived cells (yield_per_box at ai and no_interview,
    equal_size_bound, listening_edge_equal_size). All size and derived cells carry ``size_beside_lead``; the derived ones
    also ``not_a_test`` and ``derived: true`` with their ``formula_code``."""
    cells = _floor_cells(ctx)
    size = _size(ctx)
    floor, note = _primary_recalls(ctx), "cell_under_10"
    return cells + _size_cells(ctx, size) + _derived_cells(ctx, floor, note, size)


# ------------------------------------------------------------------ row 5: absences
def _zero(v) -> list:
    return ["zero_count"] if v == 0 else None


def row5_cells(ctx) -> list[dict]:
    """Row 5, the absences (20 cells, the skeleton's order): the ideals' classes at the primary setting (n_ideals,
    n_matched, recovered_as_wished, flattened, counterfactual, missed, unanchored) and, for the first five, at the lenient
    one; flattening_rate with its exact interval; counterfactual_upper_bound over all ideals / matched / matched lenient;
    recovery_rate; matcher_free_rate at judge, judge2 and keyword (judge2: ``not_run`` needs ["J2"] unless a judge2
    store exists), the judge's carrying the band over the labellers present."""
    src = SRC["f1"]
    cp, cl = absence_counts(ctx, "primary"), absence_counts(ctx, "lenient")
    nw = cp["n_wished"]
    cells = []
    note = f"leak_split_indeterminate:{cp['leak_split_indeterminate']}" if cp["leak_split_indeterminate"] else None
    ideal = lambda x, kind="count": (x, "ideal", kind)        # noqa: E731
    cells.append(pb.cell("n_ideals", nw, x="primary", unit="ideal", kind="count", source=src))
    cells.append(pb.cell("n_matched", cp["n_matched"], x="primary", n=nw, unit="ideal", kind="count", source=src,
                         note=note))
    for nm, key in (("recovered_as_wished", "recovered_as_wished"), ("flattened", "adequacy_flattening"),
                    ("counterfactual", "counterfactual_leakage"), ("missed", "missed")):
        cells.append(pb.cell(nm, cp[key], x="primary", n=nw, unit="ideal", kind="count", source=src,
                             qualifiers=_zero(cp[key])))
    un = cp["leaked_unanchored"] + cp["unanchored_wished"]
    cells.append(pb.cell("unanchored", un, x="primary", n=nw, unit="ideal", kind="count", derived="sum_of_cleared_counts",
                         source=src, qualifiers=_zero(un)))
    cells.append(pb.cell("n_matched", cl["n_matched"], x="lenient", n=nw, unit="ideal", kind="count", source=src))
    for nm, key in (("recovered_as_wished", "recovered_as_wished"), ("flattened", "adequacy_flattening"),
                    ("counterfactual", "counterfactual_leakage"), ("missed", "missed")):
        cells.append(pb.cell(nm, cl[key], x="lenient", n=nw, unit="ideal", kind="count", source=src,
                             qualifiers=_zero(cl[key])))
    if nw > 0:
        lo, hi = pb.clopper_pearson(cp["adequacy_flattening"], nw)
        cells.append(pb.cell("flattening_rate", cp["adequacy_flattening"] / nw, x="primary", n=nw, unit="ideal",
                             kind="rate", ci=(lo, hi), source=src, qualifiers=_zero(cp["adequacy_flattening"])))
    else:
        cells.append(_nr("flattening_rate", ideal("primary", "rate"), "no_ideals", source=src))
    for x, den, cnt in (("all_ideals", nw, cp["counterfactual_leakage"]), ("matched", cp["n_matched"], cp["counterfactual_leakage"]),
                        ("matched_lenient", cl["n_matched"], cl["counterfactual_leakage"])):
        meta = (x, "ideal", "upper_bound")
        if den > 0:
            cells.append(pb.cell("counterfactual_upper_bound", one_sided_upper_bound(cnt, den), x=x, n=den, unit="ideal",
                                 kind="upper_bound", ci_level=0.95, source=src, qualifiers=_zero(cnt)))
        else:
            cells.append(_nr("counterfactual_upper_bound", meta, "no_denominator", source=src))
    rec = recovery(ctx)
    cells.append(_est(ctx, "recovery_rate", ("primary", "ideal", "rate"), rec["boot"], source=SRC["a5"], n=rec["n_wished"]))
    # matcher-free rate by labeller
    labs = ("judge", "judge2", "keyword") if has_judge2(ctx) else ("judge", "keyword")
    nums = build_numbers(ctx, labs)
    band = _labeller_band(nums, "contradiction_wm", labs=labs)
    for lab in LABS:
        meta = (lab, "node", "rate")
        if lab not in labs:
            cells.append(pb.not_run("matcher_free_rate", ["J2"], x=lab, unit="node", kind="rate", source=SRC["a10"]))
            continue
        v = nums["contradiction_wm"]["by"][lab]
        src_lab = SRC["a10"] if lab == "judge2" else SRC["a9"]
        if v["n"] == 0 or not _finite(v["point"]):
            cells.append(_nr("matcher_free_rate", meta, "no_wished_under_labeller", source=src_lab))
            continue
        boot = (v["point"], v["ci_lo"], v["ci_hi"], ctx.B if v["ci_ok"] else 0)
        extra = {"rests_on": ["R", "Q", "MO"]}
        if lab == "judge":
            extra.update(qualifiers=["labeller_dependent"])
            if band is not None:
                extra.update(band=band)
            else:
                extra.update(missing=["band"])
        cells.append(_est(ctx, "matcher_free_rate", meta, boot, source=src_lab, n=v["n"], **extra))
    return cells
