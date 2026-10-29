"""profile_slant: row 6 (the slant) of the profile exporter (Task 4, split out of profile_floors by Ruling R17; schema
``orgsci-profile/v2``). Zero model calls.

Ported (pure functions copied, never imported) from the analysis pack:
  a5_one_pull         pull_variant, recovery, recovery_stats (``recovery`` also feeds row 5's recovery_rate)
  a7_additions_source classify ... analyse, decide (the scripted share of the AI's unsupported additions)

Public function: ``row6_cells``. Import layout (Ruling R19): the estimator helpers (``_est``, ``_nr``, ``_finite``,
``SRC``, ...) come from ``profile_common``; this module never imports ``profile_floors``, which imports ``recovery`` from
here at its top level.
"""
from __future__ import annotations

import math

from . import judge, metrics, pipeline, provenance
from . import profile_base as pb
from .match import MatchSetting, match_nodes
from .profile_common import (SRC, _ci_defined, _est, _finite, _hk, _mean, _nr)

# ------------------------------------------------------------------ a5: recovery and the pull variants
VARIANTS = {"canonical": ("canonical", False), "in_transcript_only": ("canonical", True),
            "raw_wording": ("raw", False), "judge_only": ("judge_only", False)}


def pull_variant(maps, spec, ps, scripts: dict, name: str, B=pb.B, seed=pb.SEED) -> dict:
    """The kit's per-map rows of one variant over ``maps``; nan pairs dropped. ``scripts`` = {"canonical": ..., "raw": ...}
    (metrics.scripts_by_role). Returns n, the mean difference with its 95% and 90% percentile CIs, and the two shares."""
    variant, in_t = VARIANTS[name]
    a, b, _ex = metrics._pull_rows(maps, spec, ps, scripts["raw" if variant != "canonical" else "canonical"], in_t, variant)
    pairs = [(x, y) for x, y in zip(a, b) if not (math.isnan(x) or math.isnan(y))]
    d = [x - y for x, y in pairs]
    return {"n": len(d), "boot95": pb.cluster_bootstrap(d, _mean, B=B, seed=seed),
            "boot90": pb.cluster_bootstrap(d, _mean, B=B, seed=seed, alpha=0.10)}


def recovery_stats(rec: list, wsh: list, B=pb.B, seed=pb.SEED) -> dict:
    """Pooled recovery (ratio of sums, bootstrap over maps) from per-map counts."""
    point, lo, hi, used = pb.cluster_bootstrap_ratio(rec, wsh, B=B, seed=seed)
    return {"recovered": sum(rec), "n_wished": sum(wsh), "boot": (point, lo, hi, used)}


def recovery(ctx) -> dict:
    mw = _hk(ctx)["min_words"]
    ps, arm = pb.SETTINGS["primary"], metrics.spec_arm(ctx.spec)
    rows = [metrics.absences(p, ctx.spec, arm, ps, min_words=mw) for p in ctx.analysed]
    return recovery_stats([r["recovered_as_wished"] for r in rows], [r["n_wished"] for r in rows], ctx.B, ctx.seed)


# ------------------------------------------------------------------ a7: where do the AI's unsupported additions come from
PRIMARY_SCRIPT, SECONDARY_SCRIPT = "own_canonical", "judgescript_lexical"
JUDGESCRIPT = "judgescript"
SECONDARY_LEX = 0.6
P_MAX, MIN_MAPS = 0.05, 10
U_PARTS = ("u1", "u2", "u3")
SPLIT = {"U": ("u1", "u2", "u3"), "T": ("T",), "K": ("K",), "KT": ("K", "T"), "u1": ("u1",), "u2": ("u2",), "u3": ("u3",)}


def classify(p, spec, ps, mw) -> dict:
    """{ai node id: "u1"|"u2"|"u3"|"T"|"K"} for every JUDGED AI node of the spec (not-judged left out). u1 = quote not
    found, u2 = found but not eligible, u3 = eligible but the judged modality does not back the drawn status, T = attested
    and matched to no endorsed node, K = attested and matched."""
    arm = metrics.spec_arm(spec)
    g = p.ai[spec]
    pv, md = p.prov[spec], p.modality[spec]
    nj = set((p.not_judged or {}).get(spec) or ())
    matched = match_nodes(g, p.endorsed, ps, p.judged_ai_vs_endorsed[spec]).left_matched
    out = {}
    for n in g.nodes:
        if n.node_id in nj:
            continue
        ok, why = provenance.eligible(pv[n.node_id], arm, mw)
        if not ok:
            out[n.node_id] = "u1" if why == "not_found" else "u2"
        elif not judge.attested(n.status, md[n.node_id]):
            out[n.node_id] = "u3"
        else:
            out[n.node_id] = "K" if n.node_id in matched else "T"
    return out


def own_canonical_flags(p, spec, ps, script) -> tuple:
    """(scripted AI ids, scripted endorsed ids, AI side fell back to raw/lexical, endorsed side fell back): the pull's
    canonical reading (metrics._pull_rows, variant canonical)."""
    canon_ai = (p.canon_ai or {}).get(spec)
    ja = (p.judged_ai_vs_script_canon or {}).get(spec)
    je = p.judged_endorsed_vs_script_canon
    ga = metrics._relabel(p.ai[spec], canon_ai)
    ge = metrics._relabel(p.endorsed, p.canon_endorsed)
    sa = match_nodes(ga, script, ps, ja).left_matched
    se = match_nodes(ge, script, ps, je).left_matched
    return sa, se, canon_ai is None or ja is None, p.canon_endorsed is None or je is None


def judgescript_flags(p, spec, script) -> tuple:
    """(scripted AI ids, scripted endorsed ids): raw labels against the judgescript, lexical matcher only."""
    lex = MatchSetting(SECONDARY_LEX, "none")
    return (match_nodes(p.ai[spec], script, lex, None).left_matched,
            match_nodes(p.endorsed, script, lex, None).left_matched)


def map_units(groups: dict, scripted_ai, endorsed_ids, scripted_end) -> dict:
    """One map's units under one script: {"nodes": [(group, scripted 0/1)], "end": (scripted, n)}."""
    return {"nodes": [(grp, int(nid in scripted_ai)) for nid, grp in groups.items()],
            "end": (sum(1 for e in endorsed_ids if e in scripted_end), len(endorsed_ids))}


NO_UNITS = {"nodes": [], "end": (0, 0)}


def group_counts(m: dict, key: str) -> tuple:
    """(scripted, n) of one map for group key (U, T, K, KT, u1, u2, u3, E)."""
    if key == "E":
        return m["end"]
    parts = SPLIT[key]
    sel = [s for grp, s in m["nodes"] if grp in parts]
    return sum(sel), len(sel)


def share(maps, key: str) -> float:
    c = [group_counts(m, key) for m in maps]
    return pb.pooled_ratio([a for a, _ in c], [b for _, b in c])


def diff_stat(a: str, b: str):
    return lambda maps: share(maps, a) - share(maps, b)


def perm_groups(maps) -> list:
    """(labels, outcomes) per map over its U and T nodes only (K and not-judged left out)."""
    out = []
    for m in maps:
        ut = [("U" if grp in U_PARTS else "T", s) for grp, s in m["nodes"] if grp != "K"]
        out.append(([lab for lab, _ in ut], [s for _, s in ut]))
    return out


def ut_stat(groups) -> float:
    """Pooled S(U) - S(T) of (labels, outcomes) groups (nan if either group is empty)."""
    su = nu = st = nt = 0
    for labels, outcomes in groups:
        for lab, o in zip(labels, outcomes):
            if lab == "U":
                su += o
                nu += 1
            else:
                st += o
                nt += 1
    return su / nu - st / nt if nu and nt else math.nan


def n_contributing(maps) -> int:
    """Maps with at least one U and at least one T node (internal only: never written)."""
    return sum(1 for labels, _ in perm_groups(maps) if "U" in labels and "T" in labels)


def analyse(maps, *, B=pb.B, n_perm=pb.N_PERM, seed=pb.SEED) -> dict:
    """Every number of one script, unrounded: shares (point, lo, hi) of U, T, K, KT, u1, u2, u3, E; counts; delta = S(U) -
    S(T) and delta_kt = S(U) - S(K u T) (point, lo, hi); delta90 (the 90% interval of delta: its upper end is the one-sided
    95% bound); the within-map permutation p of delta (one-sided greater); ``ci_undefined`` = names whose bootstrap kept
    fewer than B replicates (their bounds are nan)."""
    res = {"shares": {}, "counts": {}, "ci_undefined": set()}

    def keep(name, point, lo, hi, n_used):
        if n_used < B:
            res["ci_undefined"].add(name)
            return point, math.nan, math.nan
        return point, lo, hi
    for key in ("U", "T", "K", "KT", "u1", "u2", "u3", "E"):
        c = [group_counts(m, key) for m in maps]
        res["shares"][key] = keep(key, *pb.cluster_bootstrap_ratio([a for a, _ in c], [b for _, b in c], B=B, seed=seed))
        res["counts"][key] = (sum(a for a, _ in c), sum(b for _, b in c))
    for name, (a, b) in (("delta", ("U", "T")), ("delta_kt", ("U", "KT"))):
        res[name] = keep(name, *pb.cluster_bootstrap(maps, diff_stat(a, b), B=B, seed=seed))
    res["delta90"] = keep("delta90", *pb.cluster_bootstrap(maps, diff_stat("U", "T"), B=B, seed=seed, alpha=0.10))
    res["n_contributing"] = n_contributing(maps)
    res["enough_maps"] = res["n_contributing"] >= MIN_MAPS
    obs, p = pb.permutation_within_map(perm_groups(maps), ut_stat, n_perm=n_perm, seed=seed, alternative="greater")
    res["perm_obs"], res["p"] = obs, p
    return res


def _sign(x: float) -> int:
    return 0 if x == 0 else (1 if x > 0 else -1)


def decide(res: dict, other) -> dict:
    """The declared rules for one script (``other`` = the other script's result; None when it releases nothing)."""
    d, lo, hi = res["delta"]
    od = other["delta"][0] if other is not None else math.nan
    defined = _finite(d, lo, hi)
    same_sign = defined and _finite(od) and _sign(od) == _sign(d)
    return {"enough_maps_contribute": bool(res["enough_maps"]),
            "additions_from_model_knowledge": bool(defined and res["enough_maps"] and lo > 0 and _finite(res["p"])
                                                   and res["p"] < P_MAX and same_sign),
            "no_sign_of_it": bool(defined and res["enough_maps"] and lo <= 0 <= hi)}


def _judgescript_source(ctx):
    return next((s for s in (ctx.cfg.script_sources or []) if s.get("name") == JUDGESCRIPT), None)


def _a7_units(ctx, own, judge_scripts) -> tuple:
    """(units by script, secondary covers every role, partly lexical) over the analysed maps. The primary script must
    cover every analysed map (else the caller marks the cells not_run)."""
    spec, hk = ctx.spec, _hk(ctx)
    mw, ps = hk["min_words"], pb.SETTINGS["primary"]
    units = {PRIMARY_SCRIPT: [], SECONDARY_SCRIPT: []}
    sec_covers, partly = True, False
    for p in ctx.analysed:
        groups = classify(p, spec, ps, mw)
        end_ids = [e.node_id for e in p.endorsed.nodes]
        sa, se, fb_ai, fb_end = own_canonical_flags(p, spec, ps, own[p.role])
        partly = partly or fb_ai or fb_end
        units[PRIMARY_SCRIPT].append(map_units(groups, sa, end_ids, se))
        js = (judge_scripts or {}).get(p.role)
        if judge_scripts is None or js is None or not js.nodes:
            sec_covers = False
            units[SECONDARY_SCRIPT].append(NO_UNITS)
        else:
            sa2, se2 = judgescript_flags(p, spec, js)
            units[SECONDARY_SCRIPT].append(map_units(groups, sa2, end_ids, se2))
    return units, sec_covers, partly


_ROW6_A7 = (("scripted_share_unanchored", "own_script", "share"), ("scripted_share_attested_absent", "own_script", "share"),
            ("scripted_share_difference", "own_script", "difference"),
            ("scripted_share_difference", "other_family_script", "difference"),
            ("largest_excess_ruled_out", "other_family_script", "upper_bound"))


def _a7_not_run(code=None, *, other_needs=(), only_other=False) -> list[dict]:
    """The five a7 cells as ``not_run``: ``reason_code`` ``code`` (its sentence is in the strings file), except the
    other-family cells that need a judgescript source, which carry ``needs`` only. ``only_other`` returns just the two
    other-family cells (then ``code`` is not needed)."""
    out = []
    for name, x, kind in _ROW6_A7:
        if only_other and x != "other_family_script":
            continue
        needs = list(other_needs) if x == "other_family_script" else []
        out.append(pb.not_run(name, needs, x=x, unit="ai_box", kind=kind, reason_code=None if needs else code,
                              source=SRC["a7"]))
    return out


def a7_cells(ctx) -> list[dict]:
    """The five scripted-share cells of row 6. They mirror a7: when the primary script's S(U), S(T) or delta is not finite
    (on the shipped demo no AI box is attested and absent, so S(T) is undefined and a7 stops by design) every cell is
    ``not_run`` with a reason_code, never a value; a primary script missing for a role of an analysed map is the same. The
    other-family cells need a ``judgescript`` source in the config (else ``not_run`` needs ["judge_script"])."""
    spec, maps = ctx.spec, ctx.analysed
    src = _judgescript_source(ctx)
    needs = [] if src is not None else ["judge_script"]
    own = metrics.scripts_by_role(ctx.p_list, canonical=True)
    if any(own.get(p.role) is None or not own[p.role].nodes for p in maps):
        return _a7_not_run("no_own_script", other_needs=needs)
    judge_scripts = pipeline.source_scripts(ctx.run, src) if src is not None else None
    units, sec_covers, partly = _a7_units(ctx, own, judge_scripts)
    prim = analyse(units[PRIMARY_SCRIPT], B=ctx.B, seed=ctx.seed)
    if not _finite(prim["shares"]["U"][0], prim["shares"]["T"][0], prim["delta"][0]):
        return _a7_not_run("group_empty", other_needs=needs)
    sec_uninformative = (not sec_covers) or sum(s for m in units[SECONDARY_SCRIPT] for g, s in m["nodes"] if g != "K") == 0
    sec = analyse(units[SECONDARY_SCRIPT], B=ctx.B, seed=ctx.seed) if src is not None and sec_covers else None
    q_own = ["partly_lexical"] if partly else None

    def share_cell(res, key, name, x, rests_on):
        pt, lo, hi = res["shares"][key]
        ok = key not in res["ci_undefined"] and _finite(lo, hi)
        return _est(ctx, name, (x, "ai_box", "share"), (pt, lo, hi, ctx.B if ok else 0), source=SRC["a7"],
                    n=res["counts"][key][1], rests_on=rests_on)

    def diff_cell(res, x, **kw):
        pt, lo, hi = res["delta"]
        nu = res["counts"]["U"][1] + res["counts"]["T"][1]
        ok = "delta" not in res["ci_undefined"] and "delta90" not in res["ci_undefined"] and _finite(lo, hi)
        note = "descriptive_only" if not res["enough_maps"] else None
        kw2 = {"p": res["p"]} if res["enough_maps"] and _finite(res["p"]) else {}
        c = _est(ctx, "scripted_share_difference", (x, "ai_box", "difference"), (pt, lo, hi, ctx.B if ok else 0),
                 source=SRC["a7"], n=nu, extra_note=note, **kw2, **kw)
        if c.get("note") == "ci_undefined" and note:        # L-12: _est keeps one note; both caveats hold
            c["note"] = "ci_undefined;" + note
        return c
    cells = [share_cell(prim, "U", "scripted_share_unanchored", "own_script", ["R", "Q", "MO", "P", "CN", "AS", "LX"]),
             share_cell(prim, "T", "scripted_share_attested_absent", "own_script",
                        ["R", "Q", "MO", "P", "CN", "AS", "LX", "AL"]),
             diff_cell(prim, "own_script", qualifiers=q_own, rests_on=["R", "Q", "MO", "AL", "P", "CN", "AS", "LX"])]
    if src is None:
        return cells + _a7_not_run(other_needs=["judge_script"], only_other=True)    # needs only, no reason_code
    if sec is None or sec_uninformative or not _finite(sec["shares"]["U"][0], sec["shares"]["T"][0], sec["delta"][0]):
        return cells + _a7_not_run("secondary_uninformative")[3:]
    cells.append(diff_cell(sec, "other_family_script", rests_on=["R", "Q", "MO", "AL", "JS", "LX"]))
    pt90, lo90, hi90 = sec["delta90"]
    if claims_secondary_no_sign(sec) and _finite(lo90, hi90):
        cells.append(pb.cell("largest_excess_ruled_out", hi90, x="other_family_script", n=sec["counts"]["U"][1] + sec["counts"]["T"][1],
                             unit="ai_box", kind="upper_bound", ci=(lo90, hi90), ci_level=0.90, source=SRC["a7"],
                             rests_on=["R", "Q", "MO", "AL", "JS", "LX"]))
    else:
        cells.append(pb.not_run("largest_excess_ruled_out", [], x="other_family_script", unit="ai_box", kind="upper_bound",
                                reason_code="bound_released_only_when_no_sign_of_it", source=SRC["a7"]))
    return cells


def claims_secondary_no_sign(sec) -> bool:
    d, lo, hi = sec["delta"]
    return bool(_finite(d, lo, hi) and sec["enough_maps"] and lo <= 0 <= hi)


# ------------------------------------------------------------------ row 6
def row6_cells(ctx) -> list[dict]:
    """Row 6, the slant (10 cells, the skeleton's order): pull (canonical, 95%, with the band over the variants), pull_90,
    pull_variant at raw_wording, judge_only and in_transcript_only (a variant is released only if every analysed map
    contributes a pair; else ``not_run`` with reason_code ``variant_does_not_cover_all_maps``), then the five scripted-share
    cells (see ``a7_cells``)."""
    maps, spec = ctx.analysed, ctx.spec
    ps = pb.SETTINGS["primary"]
    scripts = {"canonical": metrics.scripts_by_role(ctx.p_list, canonical=True), "raw": metrics.scripts_by_role(ctx.p_list)}
    pulls = {name: pull_variant(maps, spec, ps, scripts, name, B=ctx.B, seed=ctx.seed) for name in VARIANTS}
    src, n = SRC["a5"], len(maps)
    rests = {"canonical": ["R", "P", "CN", "AS", "LX"], "raw_wording": ["R", "P", "AS", "LX"],
             "judge_only": ["R", "P", "CN", "AS"], "in_transcript_only": ["R", "P", "CN", "AS", "LX", "IT"]}

    def covers(name):
        v = pulls[name]
        return v["n"] == n and _finite(v["boot95"][0])
    means = [pulls[nm]["boot95"][0] for nm in ("canonical", "raw_wording", "judge_only", "in_transcript_only") if covers(nm)]
    pull_band = pb.band(min(means), max(means), "variants") if len(means) >= 2 else None    # R16: a band needs 2 members

    def variant_cell(cell_name, x, name, level):
        meta = (x, "map", "difference")
        if not covers(name):
            return _nr(cell_name, meta, "variant_does_not_cover_all_maps", source=src)
        b = pulls[name]["boot95" if level == 95 else "boot90"]
        extra = {"rests_on": rests[name], "n": pulls[name]["n"]}
        if level == 90:
            pt, lo, hi, used = b
            if _ci_defined(lo, hi, used, ctx.B):
                return pb.cell(cell_name, pt, x=x, unit="map", kind="difference", ci=(lo, hi), ci_level=0.9, source=src,
                               **extra)
            return pb.cell(cell_name, pt, x=x, unit="map", kind="difference", note="ci_undefined", source=src, **extra)
        if cell_name == "pull":
            if pull_band is None:                  # fewer than 2 variants cover all maps: partial_not_run, missing ["band"]
                return _est(ctx, cell_name, meta, b, source=src, missing=["band"], **extra)
            extra["band"] = pull_band
        return _est(ctx, cell_name, meta, b, source=src, **extra)
    cells = [variant_cell("pull", "canonical", "canonical", 95), variant_cell("pull_90", "canonical", "canonical", 90),
             variant_cell("pull_variant", "raw_wording", "raw_wording", 95),
             variant_cell("pull_variant", "judge_only", "judge_only", 95),
             variant_cell("pull_variant", "in_transcript_only", "in_transcript_only", 95)]
    return cells + a7_cells(ctx)
