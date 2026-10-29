"""profile_f1: the f1 statistics of the profile exporter (Ruling R19, split out of ``profile_floors``): the no-transcript
floor (Part 1), the wrong-transcript floor (Part 2) and the permutation rank, ported (pure functions copied, never
imported) from ``analysis/scripts/f1_floors_checks``. It imports only ``profile_common`` and ``profile_base``, so
``profile_labellers`` and ``profile_slant`` can use these statistics without importing ``profile_floors``. The cells
themselves (the lead, rows 5 and 7) are built in ``profile_floors``.
"""
from __future__ import annotations

import math

from . import metrics, stats
from . import profile_base as pb
from .match import match_nodes
from .profile_common import _hk, part_status

def _in_ids(p) -> set:
    return {n.node_id for n in p.endorsed.nodes if p.in_transcript[n.node_id]}


# ------------------------------------------------------------------ f1 Part 1: the no-transcript floor
def ai_counts(p, spec, setting, mw, attested_ai):
    """(hits, denominator) of the AI recall of one map: matched by ANY AI node over the in-transcript endorsed ids, or
    (attested_ai) the attested matched / (matched + missed) of buckets."""
    ids = _in_ids(p)
    if attested_ai:
        b = metrics.buckets(p, spec, metrics.spec_arm(spec), setting, min_words=mw)
        return b["matched"], b["matched"] + b["missed"]
    return len(ids & metrics._matching(p, spec, setting).right_matched), len(ids)


def prior_counts(p, spec, setting, mw, attested_ai):
    """(AI hits, AI denominator, prior-only hits, prior-only denominator) of one map (metrics._prior_rows, one map, as
    counts). None when the kit excludes the map (no AI map, no prior-only map, no prior alignment, nothing in the
    transcript, or no attested recall)."""
    g = p.ai.get(spec)
    pk = metrics._prior_key(p)
    prior = p.ai.get(pk) if pk else None
    if g is None or prior is None or p.judged_prior_vs_endorsed is None:
        return None
    ids = _in_ids(p)
    ai_num, ai_den = ai_counts(p, spec, setting, mw, attested_ai)
    if ai_den == 0 or not ids:
        return None
    got = match_nodes(prior, p.endorsed, setting, p.judged_prior_vs_endorsed).right_matched
    return ai_num, ai_den, len(ids & got), len(ids)


def prior_units(maps, spec, setting, mw, attested_ai):
    """(rows with None for an excluded map, units: the rows with (0, 0, 0, 0) for an excluded map, which adds 0 to the
    sums and stays in the population)."""
    rows = [prior_counts(p, spec, setting, mw, attested_ai) for p in maps]
    return rows, [r if r is not None else (0, 0, 0, 0) for r in rows]


def n_left_out_part1(rows, maps, spec, setting, mw, attested_ai) -> int:
    """k of the subset rule: every map the part leaves out that holds units on either side of the line."""
    n = 0
    for p, r in zip(maps, rows):
        if r is not None:
            continue
        if any(p.in_transcript[x.node_id] for x in p.endorsed.nodes) \
                or (p.ai.get(spec) is not None and ai_counts(p, spec, setting, mw, attested_ai)[1] > 0):
            n += 1
    return n


def stat_ai(us):
    return pb.pooled_ratio([u[0] for u in us], [u[1] for u in us])


def stat_prior(us):
    return pb.pooled_ratio([u[2] for u in us], [u[3] for u in us])


def stat_ai_minus_prior(us):
    return stat_ai(us) - stat_prior(us)


def floor1_numbers(units, B=pb.B, seed=pb.SEED):
    """{"ai", "prior", "diff"}: (point, lo, hi, n_used, denominator total), cluster bootstrap over maps (same seed, so the
    three share their resamples)."""
    out = {}
    for name, stat, den_i in (("ai", stat_ai, 1), ("prior", stat_prior, 3), ("diff", stat_ai_minus_prior, 1)):
        pt, lo, hi, used = pb.cluster_bootstrap(units, stat, B=B, seed=seed)
        out[name] = (pt, lo, hi, used, sum(u[den_i] for u in units))
    return out


def _floor1(ctx) -> dict:
    """{(line, setting key): {"nums": floor1_numbers, "status": subset-rule word}} for lines all / attested_line."""
    spec, mw, maps = ctx.spec, _hk(ctx)["min_words"], ctx.analysed
    out = {}
    for line in ("all", "attested_line"):
        for sk in ("primary", "lenient"):
            st = pb.SETTINGS[sk]
            rows, units = prior_units(maps, spec, st, mw, line == "attested_line")
            out[(line, sk)] = {"nums": floor1_numbers(units, ctx.B, ctx.seed),
                               "status": part_status(n_left_out_part1(rows, maps, spec, st, mw, line == "attested_line"),
                                                     len(maps))}
    return out


# ------------------------------------------------------------------ f1 Part 2: the wrong-transcript floor
def derange_seed(hk) -> int:
    """The seed of the derangements in the kit headline: controls() gets _seed(seed, "controls") and draws them with
    _seed(that, "derange"). ``hk`` = pipeline.headline_kwargs(run config)."""
    return metrics._seed(metrics._seed(hk["seed"], "controls"), "derange")


def shuffled_parts(maps, spec, ps, mw, n_der, dseed):
    """Per map i (same order as ``maps``): the kit's controls.shuffled_pairs rows as sums. Lexical-only matching at the
    setting's lexical threshold, attested AI nodes of map i against the in-transcript endorsed nodes of the partner.
    Returns (rows, kit_style, n_dropped): rows[i] = (real hits, real denominator, shuffled hits summed over the
    derangements, shuffled denominators summed); a map whose own recall or whose shuffled recalls are all undefined gets
    (0, 0, 0, 0). kit_style = (real recalls, mean shuffled recalls) of the kept maps (the kit's lists). n_dropped = the
    dropped maps that hold units on either side (the G7 k)."""
    m = len(maps)
    arm = metrics.spec_arm(spec)
    thr = ps.lex_threshold
    ta = [metrics._tok([n.text for n in p.ai[spec].nodes]) for p in maps]
    te = [metrics._tok([n.text for n in p.endorsed.nodes]) for p in maps]
    att = [metrics._attested_nodes(p, spec, arm, mw) for p in maps]
    ids = [[q for q, n in enumerate(p.endorsed.nodes) if p.in_transcript[n.node_id]] for p in maps]
    perms = stats.derangements(m, n_der, dseed)
    cache = {}

    def parts(i, j):
        if (i, j) not in cache:
            hit = set()
            for a, n in enumerate(maps[i].ai[spec].nodes):
                if n.node_id in att[i]:
                    hit.update(q for q, y in enumerate(te[j]) if metrics._jacc(ta[i][a], y) >= thr)
            cache[(i, j)] = (sum(1 for q in ids[j] if q in hit), len(ids[j]))
        return cache[(i, j)]
    rows, real_l, shuf_l = [], [], []
    n_dropped = 0
    for i in range(m):
        rh, rd = parts(i, i)
        sp = [parts(i, perm[i]) for perm in perms]
        sh, sd = sum(x[0] for x in sp), sum(x[1] for x in sp)
        if rd == 0 or sd == 0:
            if rd > 0 or sd > 0:
                n_dropped += 1
            rows.append((0, 0, 0, 0))
            continue
        rows.append((rh, rd, sh, sd))
        real_l.append(rh / rd)
        shuf_l.append(math.fsum(x[0] / x[1] for x in sp if x[1]) / sum(1 for x in sp if x[1]))
    return rows, (real_l, shuf_l), n_dropped


def stat_real(ms):
    return pb.pooled_ratio([x[0] for x in ms], [x[1] for x in ms])


def stat_shuf(ms):
    return pb.pooled_ratio([x[2] for x in ms], [x[3] for x in ms])


def stat_diff(ms):
    return stat_real(ms) - stat_shuf(ms)


def one_sided_upper_bound(k: int, n: int) -> float:
    """The exact one-sided 95% upper bound of k / n, the upper end of the two-sided 90% Clopper-Pearson interval
    (0 of 90 -> .0327, 0 of 32 -> .0894). nan when n is 0."""
    return pb.clopper_pearson(k, n, 0.90)[1]


def _floor2(ctx) -> dict | None:
    """Wrong-transcript floor on the judge's labels: {"boot": {real, shuf, diff}, "status", "kit_style"}; None with fewer
    than 2 analysed maps (a derangement needs two)."""
    maps = ctx.analysed
    if len(maps) < 2:
        return None
    hk = _hk(ctx)
    rows, kit_style, dropped = shuffled_parts(maps, ctx.spec, pb.SETTINGS["primary"], hk["min_words"],
                                              hk["n_derangements"], derange_seed(hk))
    boot = {}
    for key, stat, den_i in (("real", stat_real, 1), ("shuf", stat_shuf, 3), ("diff", stat_diff, 1)):
        pt, lo, hi, used = pb.cluster_bootstrap(rows, stat, B=ctx.B, seed=ctx.seed)
        boot[key] = (pt, lo, hi, used, sum(x[den_i] for x in rows))
    return {"boot": boot, "status": part_status(dropped, len(maps)), "kit_style": kit_style}


def _permutation(ctx) -> dict | None:
    """The kit's controls()["permutation_rank"] on the analysed maps (the same function and seed as the headline)
    with its subset-rule word; None when the kit cannot rank (fewer than 2 maps)."""
    maps, spec = ctx.analysed, ctx.spec
    if len(maps) < 2:
        return None
    hk = _hk(ctx)
    ctl = metrics.controls(maps, pb.SETTINGS["primary"], spec_key=spec, seed=metrics._seed(hk["seed"], "controls"),
                           n_derangements=hk["n_derangements"], min_words=hk["min_words"], B=50)
    perm = ctl["permutation_rank"]
    k_left = sum(1 for p in maps if p.endorsed.nodes and (p.ai.get(spec) is None or not p.ai[spec].nodes))
    return {"perm": perm, "status": part_status(k_left, len(maps))}
