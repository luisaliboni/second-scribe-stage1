"""profile_ladder: the ladder cells of the profile exporter (schema ``orgsci-profile/v2``).

  * ``row8_cells(ctx)``          row 8, the instruction ladder: size-adjusted recall per rung and its contrasts, the
                                 remedy-minus-placebo side effects, the leak rate and its minimum detectable effect;
  * ``row9_rejudge_cells(ctx)``  the five ``rejudge_*`` cells of row 9 (how often a second judging of the same map
                                 moves an endorsed step in or out of "located");
  * ``rung1_size(ctx)``          the AI-map size of rung 1, which row 2 divides by the endorsed boxes;
  * ``has_ladder(ctx)``          whether the run's config has a ladder block.

Ported (copied, never imported) from the analysis pack: a12_ladder.py (``max_matching``, ``one_to_one``, ``map_parts``,
``rung_rows``, ``ratio`` / ``measure`` / ``per_rung``, ``contrast``, ``holm_finite``, ``filled_rows``,
``located_changes``, with Amendment 7's rung-2 fill rule) and a8_nulls.py (``leak_rung_rows``, ``leak_ratio``,
``leak_per_rung``, ``leak_contrast``, ``mde80``, ``testable_at_floor``). The gate glue (records, cell_under_10, the G7
sensitivities, claims, the kit's own contrasts, EXPECTED asserts) is dropped: a cell is built from a statistic.

Estimator: pooled ratio of sums over the analysed maps, percentile cluster bootstrap over maps (``ctx.B``, ``ctx.seed``,
a fresh ``random.Random(seed)`` per statistic) and a within-map permutation test of the rung labels (N_PERM = 10000).
Everything is read from the run's stores (``pipeline.rung_view`` + ``build_pair_inputs``) at the primary setting; zero
model calls.

Amendment 7: rung 2 is the main map copied as draw 0 and re-judged in the ladder phase. A map whose rung-2 copy is
missing, or is not the main map (another digest), is FILLED with the main run's row, so no rung-2 draw is ever failed.
The rejudge counts cover the maps that were not filled; when any map was filled they are not released (they would cover
a subset of the maps) and the rejudge cells are ``not_run`` with ``reason_code: "rung2_filled_from_main"``.

A run without a ladder block: every row 8 cell and every rejudge cell is ``not_run`` with ``needs: ["ladder"]``.
A ladder block that lists only some of the four rungs: a cell is ``not_run`` (needs ``["ladder"]``, reason_code
``rung_not_configured``) when a rung it reads is missing. A statistic that is undefined on this run (a rate over no
ideals, a missing interval for the mde80) is ``not_run`` with no ``needs`` and a ``reason_code``.

Fail closed (Ruling R14): a configured rung is UNFINISHED when an analysed participant has no recon work item in it
(never attempted), or when it is 100% failed (no participant has a usable rung map), or, for rung 2, 100% filled from the
main run. Every cell that reads an unfinished rung is ``not_run`` with ``needs: ["ladder"]`` and
``reason_code: "ladder_unfinished"``, and ``rung1_size`` returns None for an unfinished rung 1. Nothing raises, so the export
still produces its other rows. Partial failures (some maps failed, at least one rung map present) keep a12's scoring of
failed draws as zeros; a rung-2 copy that is missing for some maps is filled (Amendment 7), not unfinished.
"""
from __future__ import annotations

import math

from . import ladder, ladder_metrics, metrics, pipeline
from . import profile_base as pb

RUNG_KEYS = ("rung1", "rung2", "rung3", "placebo")                  # registry keys; "rung3" is the remedy
ARM_OF = ladder.ARM_OF_RUNG                                        # registry key -> ladder_metrics arm name
ARMS = ("rung1", "rung2", "remedy", "placebo")
MIRROR = ("actual_recall", "ai_node_count", "false_wished_rate", "flip_rate")
C21_MEASURES = ("size_adjusted_recall", "differential_recall")      # the Holm family of rung 2 minus rung 1
MDE_FACTOR = 2.8 / 3.92                                             # MDE80 = 2.8 * SE, SE = (hi - lo) / 3.92
SRC14, SRC15 = "kit:a12_ladder", "kit:a8_nulls"                      # cell ``source`` (Ruling R11): kit:<analysis script>


# ------------------------------------------------------------------ per map (a12)
def max_matching(adj: dict) -> int:
    """Size of a maximum bipartite matching (augmenting paths). ``adj``: left node -> iterable of right nodes."""
    match_right: dict = {}

    def augment(u, seen) -> bool:
        for v in adj[u]:
            if v in seen:
                continue
            seen.add(v)
            if v not in match_right or augment(match_right[v], seen):
                match_right[v] = u
                return True
        return False
    return sum(augment(u, set()) for u in sorted(adj))


def one_to_one(p, spec_key, setting, min_words) -> int:
    """M1 of one map: maximum matching between in-transcript endorsed nodes and their LOCATED AI partners."""
    st = metrics._absence_structure(p, spec_key, metrics.spec_arm(spec_key), setting, min_words)
    adj = {nid: sorted(st["loc"].get(nid, ())) for nid, _ in st["ends"]}
    return max_matching(adj)


def map_parts(draw, m1: int) -> dict:
    """measure -> (numerator, denominator) of one map's draw; plus "failed"."""
    kp = ladder_metrics._metric_parts(draw)
    ideals = [i for i in draw.items if i.endorsed_status == "wished"]
    exper = [i for i in draw.items if i.endorsed_status == "experienced"]
    hit = ("wished", "experienced")
    out = {"size_adjusted_recall": (m1, min(draw.n_ai_nodes, len(draw.items))),
           "ideal": (sum(i.state in hit for i in ideals), len(ideals)),
           "exp": (sum(i.state in hit for i in exper), len(exper)),
           "failed": bool(draw.parse_failed)}
    for m in MIRROR:
        out[m] = kp[m]
    return out


def _failed_draw(pm):
    items = tuple(ladder_metrics.Item(n.node_id, n.status, "missed") for n in pm.endorsed.nodes if pm.in_transcript[n.node_id])
    return ladder_metrics.MapDraw.failed(pm.pid, 0, items)


def rung_rows(rung_pl, maps, spec_key, setting, min_words) -> list:
    """Per P0 map (in ``maps`` order) the parts of its draw 0 in one rung. A P0 map absent from the rung's inputs, or
    without its AI map, is a failed draw: every item missed, 0 AI nodes."""
    by = {p.pid: p for p in rung_pl}
    rows = []
    for pm in maps:
        p = by.get(pm.pid)
        if p is None:
            draw, m1 = _failed_draw(pm), 0
        else:
            draw = ladder_metrics.draw_from_pair_inputs(p, spec_key, 0, setting, min_words=min_words)
            m1 = 0 if p.ai.get(spec_key) is None else one_to_one(p, spec_key, setting, min_words)
        rows.append(map_parts(draw, m1))
    return rows


def filled_rows(rung, main, use) -> list:
    """Rung 2's rows: the rung-2 row where ``use`` (its copy is the main map), else the main run's row (Amendment 7)."""
    return [r if u else m for r, m, u in zip(rung, main, use)]


def located_changes(pairs) -> dict:
    """(group, direction) -> nodes whose located status (non-empty ``loc``) differs, node by node, between the main
    judging and the rung-2 judging. ``pairs``: (main, rung-2) ``_absence_structure`` of each map NOT filled."""
    out = {(g, d): 0 for g in ("ideal", "experienced") for d in ("gained", "lost")}
    for st_m, st_r in pairs:
        if st_m["ends"] != st_r["ends"]:
            raise ValueError("the main judging and the rung-2 judging read different endorsed nodes")
        for nid, ns in st_m["ends"]:
            grp = "ideal" if ns == "wished" else "experienced"
            was, now = bool(st_m["loc"].get(nid)), bool(st_r["loc"].get(nid))
            if now and not was:
                out[(grp, "gained")] += 1
            elif was and not now:
                out[(grp, "lost")] += 1
    return out


# ------------------------------------------------------------------ statistics (a12)
def ratio(rows, key) -> float:
    return pb.pooled_ratio([r[key][0] for r in rows], [r[key][1] for r in rows])


def measure(rows, key) -> float:
    """A pooled measure over map rows (nan when a denominator is 0)."""
    if key == "differential_recall":
        return ratio(rows, "ideal") - ratio(rows, "exp")
    return ratio(rows, key)


def _ci_or_nan(lo, hi, n_used, B):
    return (math.nan, math.nan) if n_used < B else (lo, hi)         # an interval needs all B replicates (Amendment 4)


def per_rung(rows, key, *, B=pb.B, seed=pb.SEED) -> dict:
    point, lo, hi, n_used = pb.cluster_bootstrap(rows, lambda rs: measure(rs, key), B=B, seed=seed)
    lo, hi = _ci_or_nan(lo, hi, n_used, B)
    return {"value": point, "lo": lo, "hi": hi, "n_used": n_used}


def contrast(rows_a, rows_b, key, *, B=pb.B, seed=pb.SEED, n_perm=pb.N_PERM, with_p=True) -> dict:
    """a - b on a pooled measure, paired by map: map bootstrap CI and (``with_p``) the within-map permutation of the
    rung labels, two-sided."""
    units = list(zip(rows_a, rows_b))
    stat = lambda us: measure([u[0] for u in us], key) - measure([u[1] for u in us], key)  # noqa: E731
    point, lo, hi, n_used = pb.cluster_bootstrap(units, stat, B=B, seed=seed)
    lo, hi = _ci_or_nan(lo, hi, n_used, B)
    p = None
    if with_p:
        def perm_stat(groups):
            a = [outs[labs.index("a")] for labs, outs in groups]
            b = [outs[labs.index("b")] for labs, outs in groups]
            return measure(a, key) - measure(b, key)
        _obs, p = pb.permutation_within_map([(["a", "b"], [ra, rb]) for ra, rb in units], perm_stat,
                                            n_perm=n_perm, seed=seed)
    return {"value": point, "lo": lo, "hi": hi, "n_used": n_used, "p": p}


def holm_finite(pvals: list) -> list:
    """Holm over the finite p-values only (a nan p stays nan)."""
    idx = [i for i, p in enumerate(pvals) if p is not None and math.isfinite(p)]
    adj = pb.holm([pvals[i] for i in idx]) if idx else []
    out = [math.nan] * len(pvals)
    for i, a in zip(idx, adj):
        out[i] = a
    return out


def c21_contrasts(rows_a, rows_b, *, B=pb.B, seed=pb.SEED, n_perm=pb.N_PERM) -> dict:
    """Rung 2 minus rung 1 on the two Holm-family measures: {measure: contrast dict + "p_holm" + "holm_family_lt_2"}.
    ``rows_a`` = rung 2. When either p is nan the family has fewer than 2 members: ``p_holm`` is nan for both and
    ``holm_family_lt_2`` is True (no Holm-adjusted p is written on the cell)."""
    got = {key: contrast(rows_a, rows_b, key, B=B, seed=seed, n_perm=n_perm) for key in C21_MEASURES}
    ps = [got[k]["p"] for k in C21_MEASURES]
    family = all(_finite(p) for p in ps)                 # a nan p shrinks the family below 2: no Holm (a12 stops there)
    for key, h in zip(C21_MEASURES, holm_finite(ps) if family else [math.nan] * len(ps)):
        got[key]["p_holm"] = h
        got[key]["holm_family_lt_2"] = not family
    return got


# ------------------------------------------------------------------ the leak outcome (a8, part 3)
def leak_rung_rows(rung_pl, maps, spec_key, setting, min_words) -> list:
    """Per P0 map the leak parts of its draw 0 in one rung: {"leaked": (n_leaked, n_ideals), "failed": bool}. A P0 map
    absent from the rung's inputs is a failed draw (every item missed)."""
    by = {p.pid: p for p in rung_pl}
    rows = []
    for pm in maps:
        p = by.get(pm.pid)
        draw = _failed_draw(pm) if p is None else ladder_metrics.draw_from_pair_inputs(p, spec_key, 0, setting,
                                                                                         min_words=min_words)
        rows.append({"leaked": ladder_metrics._metric_parts(draw)["leaked_rate"], "failed": bool(draw.parse_failed)})
    return rows


def leak_ratio(rows) -> float:
    """Pooled leak rate of map rows (nan when there is no ideal)."""
    return pb.pooled_ratio([r["leaked"][0] for r in rows], [r["leaked"][1] for r in rows])


def leak_per_rung(rows, *, B=pb.B, seed=pb.SEED) -> dict:
    point, lo, hi, n_used = pb.cluster_bootstrap(rows, leak_ratio, B=B, seed=seed)
    lo, hi = _ci_or_nan(lo, hi, n_used, B)
    return {"value": point, "lo": lo, "hi": hi}


def leak_contrast(rows_a, rows_b, *, B=pb.B, seed=pb.SEED, n_perm=pb.N_PERM) -> dict:
    """a - b on the pooled leak rate, paired by map: map bootstrap at 95% and 90% and the within-map permutation of the
    rung labels (two-sided)."""
    units = list(zip(rows_a, rows_b))
    stat = lambda us: leak_ratio([u[0] for u in us]) - leak_ratio([u[1] for u in us])  # noqa: E731
    point, lo, hi, n_used = pb.cluster_bootstrap(units, stat, B=B, seed=seed)
    lo, hi = _ci_or_nan(lo, hi, n_used, B)
    # the 90% and 95% bootstraps share seed and B, so they draw the same replicates and n_used is identical
    _p90, lo90, hi90, n90 = pb.cluster_bootstrap(units, stat, B=B, seed=seed, alpha=0.10)
    lo90, hi90 = _ci_or_nan(lo90, hi90, n90, B)

    def perm_stat(groups):
        a = [outs[labs.index("a")] for labs, outs in groups]
        b = [outs[labs.index("b")] for labs, outs in groups]
        return leak_ratio(a) - leak_ratio(b)
    _obs, p = pb.permutation_within_map([(["a", "b"], [ra, rb]) for ra, rb in units], perm_stat, n_perm=n_perm, seed=seed)
    return {"value": point, "lo": lo, "hi": hi, "lo90": lo90, "hi90": hi90, "p": p}


def interval_defined(lo: float, hi: float) -> bool:
    return math.isfinite(lo) and math.isfinite(hi)


def mde80(lo: float, hi: float) -> float:
    """MDE80 = 2.8 * SE with SE = (hi - lo) / 3.92, from the contrast's two-sided 95% interval."""
    return MDE_FACTOR * (hi - lo)


def testable_at_floor(placebo_rate: float, lo: float, hi: float) -> bool:
    """The placebo's pooled leak rate is at least MDE80 (unrounded). No interval -> no MDE80 -> false. A placebo rate of
    0 is false whatever MDE80 is (a zero-width interval would otherwise give 0 >= 0)."""
    return bool(interval_defined(lo, hi) and placebo_rate > 0 and placebo_rate >= mde80(lo, hi))


# ------------------------------------------------------------------ reading the run
def has_ladder(ctx) -> bool:
    """True when the run's config has a ladder block with at least one rung."""
    return bool(pipeline.ladder_rungs(ctx.cfg))


def _min_words(ctx) -> int:
    return int((ctx.cfg.stats or {}).get("min_words", 8))


def _read_rungs(ctx, keys) -> dict:
    """registry key -> {"pl": the rung's PairInputs list, "unfinished": bool} for the configured rungs in ``keys``.

    Ruling R14 (fail closed) is a ruling on top of a12, not a12's behaviour: a12:476-486 only marks a pid unfinished,
    which blocks the kit contrasts, and does not stop. Here a rung is UNFINISHED when an analysed participant has no recon
    work item in it (never attempted) or when no analysed participant has a usable rung map at all (100% failed).
    Rung 2 is exempt from the first test: its copy of the main map is filled from the main run when absent (Amendment 7);
    it is unfinished when it is 100% failed or (checked in ``_ladder_rows``) 100% filled."""
    out = {}
    for key in keys:
        v = pipeline.rung_view(ctx.run, key)
        skey = v.specs[0].key()
        if skey != ctx.spec:
            raise ValueError(f"rung {key}: draw 0 reads spec {skey!r}, not the primary spec {ctx.spec!r}")
        rung_pl, _ex, _judging = pipeline.build_pair_inputs(v)
        by = {p.pid: p for p in rung_pl}
        never = key != "rung2" and any(v.work.get("recon", f"{p.pid}__{skey}") is None for p in ctx.analysed)
        usable = any(by.get(p.pid) is not None and by[p.pid].ai.get(skey) is not None for p in ctx.analysed)
        out[key] = {"pl": rung_pl, "unfinished": bool(never or not usable)}
    return out


def _main_totals(ctx, mw) -> tuple:
    """(wished, experienced) in-transcript endorsed nodes of the analysed maps, from the main run's inputs."""
    arm = metrics.spec_arm(ctx.spec)
    sts = [metrics._absence_structure(p, ctx.spec, arm, pb.SETTINGS["primary"], mw) for p in ctx.analysed]
    ideal = sum(1 for st in sts for _, ns in st["ends"] if ns == "wished")
    exp = sum(1 for st in sts for _, ns in st["ends"] if ns != "wished")
    return ideal, exp


def _ladder_rows(ctx) -> dict:
    """Everything the cells read, per arm: {"configured": registry keys, "rows": arm -> per-map parts (the analysed maps'
    order), "leak": arm -> leak rows (remedy, placebo), "filled": [bool] per map (rung 2 only; [] without rung 2),
    "changes": located_changes over the maps not filled (None without rung 2), "unfinished": the arms (ladder_metrics
    names) that are unfinished, Ruling R14: they have no entry in rows / leak and ``changes`` is None for rung 2}."""
    configured = pipeline.ladder_rungs(ctx.cfg)
    maps, spec, ps, mw = ctx.analysed, ctx.spec, pb.SETTINGS["primary"], _min_words(ctx)
    info = _read_rungs(ctx, configured)
    unfinished = {ARM_OF[k] for k in configured if info[k]["unfinished"]}
    live = [k for k in configured if not info[k]["unfinished"]]
    rows = {ARM_OF[k]: rung_rows(info[k]["pl"], maps, spec, ps, mw) for k in live}
    leak = {ARM_OF[k]: leak_rung_rows(info[k]["pl"], maps, spec, ps, mw) for k in live if k in ("rung3", "placebo")}
    for a, rs in rows.items():                         # a12:490-491, kept as errors
        if any(rw["exp"] != rw["actual_recall"] for rw in rs):
            raise ValueError(f"{a}: lived-step recall parts differ from the experienced parts")
        if any(rw["size_adjusted_recall"][0] > rw["size_adjusted_recall"][1] for rw in rs):
            raise ValueError(f"{a}: a map has more one-to-one matches than its size-adjusted denominator")
    main_ideals, main_exp = _main_totals(ctx, mw)
    filled, changes = [], None
    if "rung2" in live:
        by2 = {p.pid: p for p in info["rung2"]["pl"]}
        main_rows = rung_rows(maps, maps, spec, ps, mw)
        present = [by2.get(pm.pid) is not None and by2[pm.pid].ai.get(spec) is not None for pm in maps]
        same = [ok and pipeline.map_digest(by2[pm.pid].ai[spec]) == pipeline.map_digest(pm.ai[spec])
                for pm, ok in zip(maps, present)]
        if not any(same):                              # 100% filled: nothing of rung 2 was re-judged (R14)
            unfinished.add(ARM_OF["rung2"])
            del rows["rung2"]
        else:
            filled = [not u for u in same]
            rows["rung2"] = filled_rows(rows["rung2"], main_rows, same)
            arm = metrics.spec_arm(spec)
            pairs = [(metrics._absence_structure(pm, spec, arm, ps, mw),
                      metrics._absence_structure(by2[pm.pid], spec, arm, ps, mw)) for pm, u in zip(maps, same) if u]
            changes = located_changes(pairs)
            if (sum(r["ideal"][1] for r in rows["rung2"]), sum(r["exp"][1] for r in rows["rung2"])) != (main_ideals, main_exp):
                raise ValueError("rung 2 and the main run disagree on the number of endorsed nodes")
    for a, lr in leak.items():
        if sum(x["leaked"][1] for x in lr) != main_ideals:
            raise ValueError(f"{a}: the ideal total differs from the main run's")
    return {"configured": configured, "rows": rows, "leak": leak, "filled": filled, "changes": changes,
            "unfinished": unfinished}


# ------------------------------------------------------------------ cells
def _nr(name, x, unit, kind, needs, code=None, source=None) -> dict:
    return pb.not_run(name, needs, x=x, unit=unit, kind=kind, reason_code=code, source=source)


def _missing(name, x, unit, kind, source, note="rung_not_configured") -> dict:
    return _nr(name, x, unit, kind, ["ladder"], code=note, source=source)


def _why(d: dict, *arms) -> str:
    """The note of a cell that reads rungs that have no rows: unfinished (R14) or not in the config."""
    return "ladder_unfinished" if any(a in d["unfinished"] for a in arms) else "rung_not_configured"


def _undefined(name, x, unit, kind, source, note="undefined_value") -> dict:
    return _nr(name, x, unit, kind, [], code=note, source=source)


def _ci(s: dict, lo="lo", hi="hi"):
    return (s[lo], s[hi]) if interval_defined(s[lo], s[hi]) else None


def _ci_note(s: dict, lo="lo", hi="hi"):
    """``note`` of a cell whose interval is undefined (fewer than B usable bootstrap replicates), else None."""
    return None if interval_defined(s[lo], s[hi]) else "ci_undefined"


def _finite(*xs) -> bool:
    return all(x is not None and math.isfinite(x) for x in xs)


_R8_RESTS = ["R", "Q", "LD", "LX", "IT"]


def row8_cells(ctx) -> list[dict]:
    """The 18 cells of row 8, in the skeleton's order: ``ai_per_map`` (x = rung1, rung2, remedy, placebo),
    ``size_adjusted_recall`` (the same four), ``size_adjusted_recall_2_minus_1`` (with ``p`` and ``p_holm``, Holm over
    size-adjusted and differential recall), ``lived_recall_`` / ``ai_nodes_`` / ``false_wished_`` / ``flip_remedy_minus_placebo``,
    ``leak_rate`` (remedy, placebo), ``leak_rate_remedy_minus_placebo`` (``p``), its 90% twin and ``mde80_leak_contrast``.
    Without a ladder every cell is ``not_run`` with ``needs: ["ladder"]``. Cell n: ``size_adjusted_recall`` the summed
    min(AI nodes, endorsed nodes); the leak cells the remedy arm's ideals."""
    skeleton = ([("ai_per_map", a, "ai_box", "count") for a in ARMS]
                + [("size_adjusted_recall", a, "node", "share") for a in ARMS]
                + [("size_adjusted_recall_2_minus_1", "rung2-rung1", "node", "difference"),
                   ("lived_recall_remedy_minus_placebo", "remedy-placebo", "node", "difference"),
                   ("ai_nodes_remedy_minus_placebo", "remedy-placebo", "ai_box", "difference"),
                   ("false_wished_remedy_minus_placebo", "remedy-placebo", "node", "difference"),
                   ("flip_remedy_minus_placebo", "remedy-placebo", "node", "difference"),
                   ("leak_rate", "remedy", "ideal", "rate"), ("leak_rate", "placebo", "ideal", "rate"),
                   ("leak_rate_remedy_minus_placebo", "remedy-placebo", "ideal", "difference"),
                   ("leak_rate_remedy_minus_placebo_90", "remedy-placebo", "ideal", "difference"),
                   ("mde80_leak_contrast", "remedy-placebo", "ideal", "difference")])
    src = lambda name: SRC15 if name.startswith(("leak", "mde80")) else SRC14      # noqa: E731
    if not has_ladder(ctx):
        return [_nr(n, x, u, k, ["ladder"], source=src(n)) for n, x, u, k in skeleton]
    d = _ladder_rows(ctx)
    rows, leak = d["rows"], d["leak"]
    B, seed = ctx.B, ctx.seed
    out = []
    # ai_per_map and size_adjusted_recall per rung
    for name, key, unit, kind, rests in (("ai_per_map", "ai_node_count", "ai_box", "count", ["R"]),
                                         ("size_adjusted_recall", "size_adjusted_recall", "node", "share", _R8_RESTS)):
        for a in ARMS:
            if a not in rows:
                out.append(_missing(name, a, unit, kind, SRC14, _why(d, a)))
                continue
            s = per_rung(rows[a], key, B=B, seed=seed)
            if not _finite(s["value"]):
                out.append(_undefined(name, a, unit, kind, SRC14))
                continue
            n = sum(r[key][1] for r in rows[a]) if key == "size_adjusted_recall" else None
            out.append(pb.cell(name, s["value"], x=a, n=n, unit=unit, kind=kind, ci=_ci(s), note=_ci_note(s), rests_on=rests,
                               qualifiers=["one_draw"], source=SRC14))
    # rung 2 minus rung 1
    name, x = "size_adjusted_recall_2_minus_1", "rung2-rung1"
    if "rung1" not in rows or "rung2" not in rows:
        out.append(_missing(name, x, "node", "difference", SRC14, _why(d, "rung1", "rung2")))
    else:
        c = c21_contrasts(rows["rung2"], rows["rung1"], B=B, seed=seed)["size_adjusted_recall"]
        if not _finite(c["value"]):
            out.append(_undefined(name, x, "node", "difference", SRC14))
        else:
            notes = [n for n in (_ci_note(c), "holm_family_lt_2" if c["holm_family_lt_2"] else None) if n]
            out.append(pb.cell(name, c["value"], x=x, unit="node", kind="difference", ci=_ci(c), note=";".join(notes) or None,
                               p=c["p"] if _finite(c["p"]) else None, p_holm=c["p_holm"] if _finite(c["p_holm"]) else None,
                               rests_on=_R8_RESTS, qualifiers=["one_draw", "package_contrast", "descriptive"], source=SRC14))
    # remedy minus placebo, the four side effects
    for name, key, unit, rests in (("lived_recall_remedy_minus_placebo", "actual_recall", "node", _R8_RESTS),
                                   ("ai_nodes_remedy_minus_placebo", "ai_node_count", "ai_box", ["R"]),
                                   ("false_wished_remedy_minus_placebo", "false_wished_rate", "node", _R8_RESTS + ["MO"]),
                                   ("flip_remedy_minus_placebo", "flip_rate", "node", _R8_RESTS)):
        if "remedy" not in rows or "placebo" not in rows:
            out.append(_missing(name, "remedy-placebo", unit, "difference", SRC14, _why(d, "remedy", "placebo")))
            continue
        c = contrast(rows["remedy"], rows["placebo"], key, B=B, seed=seed, with_p=False)
        if not _finite(c["value"]):
            out.append(_undefined(name, "remedy-placebo", unit, "difference", SRC14))
            continue
        out.append(pb.cell(name, c["value"], x="remedy-placebo", unit=unit, kind="difference", ci=_ci(c), note=_ci_note(c),
                           rests_on=rests, source=SRC14))
    # the leak outcome
    have = "remedy" in leak and "placebo" in leak
    stats = {a: leak_per_rung(leak[a], B=B, seed=seed) for a in leak}
    n_ideals = sum(r["leaked"][1] for r in leak["remedy"]) if "remedy" in leak else None
    for a in ("remedy", "placebo"):
        if a not in leak:
            out.append(_missing("leak_rate", a, "ideal", "rate", SRC15, _why(d, a)))
        elif not _finite(stats[a]["value"]):
            out.append(_undefined("leak_rate", a, "ideal", "rate", SRC15))
        else:
            out.append(pb.cell("leak_rate", stats[a]["value"], x=a, n=sum(r["leaked"][1] for r in leak[a]),
                               unit="ideal", kind="rate", ci=_ci(stats[a]), note=_ci_note(stats[a]), rests_on=_R8_RESTS, qualifiers=["one_draw"],
                               source=SRC15))
    if not have:
        for name in ("leak_rate_remedy_minus_placebo", "leak_rate_remedy_minus_placebo_90", "mde80_leak_contrast"):
            out.append(_missing(name, "remedy-placebo", "ideal", "difference", SRC15, _why(d, "remedy", "placebo")))
        return out
    c = leak_contrast(leak["remedy"], leak["placebo"], B=B, seed=seed)
    if not _finite(c["value"]):
        for name in ("leak_rate_remedy_minus_placebo", "leak_rate_remedy_minus_placebo_90", "mde80_leak_contrast"):
            out.append(_undefined(name, "remedy-placebo", "ideal", "difference", SRC15))
        return out
    out.append(pb.cell("leak_rate_remedy_minus_placebo", c["value"], x="remedy-placebo", n=n_ideals, unit="ideal",
                       kind="difference", ci=_ci(c), note=_ci_note(c), p=c["p"] if _finite(c["p"]) else None, rests_on=_R8_RESTS,
                       qualifiers=["one_draw"], source=SRC15))
    ci90 = _ci(c, "lo90", "hi90")
    if ci90 is None:
        out.append(_undefined("leak_rate_remedy_minus_placebo_90", "remedy-placebo", "ideal", "difference", SRC15,
                              note="ci_undefined"))
    else:
        out.append(pb.cell("leak_rate_remedy_minus_placebo_90", c["value"], x="remedy-placebo", n=n_ideals, unit="ideal",
                           kind="difference", ci=ci90, ci_level=0.9, rests_on=_R8_RESTS, qualifiers=["one_draw"],
                           source=SRC15))
    if _ci(c) is None:
        out.append(_undefined("mde80_leak_contrast", "remedy-placebo", "ideal", "difference", SRC15, note="ci_undefined"))
    else:
        out.append(pb.cell("mde80_leak_contrast", mde80(c["lo"], c["hi"]), x="remedy-placebo", n=n_ideals, unit="ideal",
                           kind="difference", source=SRC15))
    return out


def row9_rejudge_cells(ctx) -> list[dict]:
    """The five ``rejudge_*`` cells of row 9, in the skeleton's order: ``rejudge_changed`` / ``rejudge_changed_share`` /
    ``rejudge_net`` at x = "lived_steps" (endorsed experienced steps, n = their count in rung 2) and ``rejudge_changed`` /
    ``rejudge_net`` at x = "ideals" (endorsed wished steps). "Changed" = gained + lost: the endorsed steps whose
    "has a located AI partner" status differs between the main judging and the rung-2 re-judging of the same map; "net"
    = gained - lost. Not released (``not_run``, needs ``["ladder"]``, reason_code ``rung2_filled_from_main``) when any map was
    filled from the main run (Amendment 7), and ``not_run`` with needs ``["ladder"]`` when the run has no ladder."""
    skeleton = [("rejudge_changed", "lived_steps", "node", "count"), ("rejudge_changed_share", "lived_steps", "node", "share"),
                ("rejudge_net", "lived_steps", "node", "count"), ("rejudge_changed", "ideals", "ideal", "count"),
                ("rejudge_net", "ideals", "ideal", "count")]
    if not has_ladder(ctx):
        return [_nr(n, x, u, k, ["ladder"], source=SRC14) for n, x, u, k in skeleton]
    d = _ladder_rows(ctx)
    if d["changes"] is None:
        return [_missing(n, x, u, k, SRC14, _why(d, "rung2")) for n, x, u, k in skeleton]
    if any(d["filled"]):
        return [_nr(n, x, u, k, ["ladder"], code="rung2_filled_from_main", source=SRC14) for n, x, u, k in skeleton]
    ch, r2 = d["changes"], d["rows"]["rung2"]
    n_l, n_i = sum(r["exp"][1] for r in r2), sum(r["ideal"][1] for r in r2)
    g_l, l_l, g_i, l_i = (ch[("experienced", "gained")], ch[("experienced", "lost")], ch[("ideal", "gained")],
                          ch[("ideal", "lost")])
    rest = ["AL", "LD"]
    cells = {("rejudge_changed", "lived_steps"): g_l + l_l, ("rejudge_net", "lived_steps"): g_l - l_l,
             ("rejudge_changed", "ideals"): g_i + l_i, ("rejudge_net", "ideals"): g_i - l_i}
    out = []
    for name, x, unit, kind in skeleton:
        n = n_l if x == "lived_steps" else n_i
        if name == "rejudge_changed_share":
            if not n:
                out.append(_undefined(name, x, unit, kind, SRC14, note="no_denominator"))
            else:
                out.append(pb.cell(name, round((g_l + l_l) / n, 3), x=x, n=n, unit=unit, kind=kind,
                                   derived="ratio_of_cleared_counts", rests_on=rest, source=SRC14))
            continue
        derived = "net" if name == "rejudge_net" else "sum_of_cleared_counts"
        out.append(pb.cell(name, cells[(name, x)], x=x, n=n, unit=unit, kind=kind, derived=derived, rests_on=rest,
                           source=SRC14))
    return out


def rung1_size(ctx) -> dict | None:
    """The AI-map size at rung 1, for row 2's ``ai_per_endorsed`` at x = "rung1" (n_ai_nodes / the endorsed boxes).
    None when the run has no ladder, the ladder has no rung 1, or rung 1 is unfinished (Ruling R14: an analysed
    participant has no rung-1 recon item, or no participant has a rung-1 map). Otherwise a dict:

      ``n_ai_nodes``   int    AI nodes (all, judged or not) summed over the analysed maps of rung 1's draw 0; a map
                              whose rung-1 map is missing counts 0 (a failed draw);
      ``n_maps``       int    the analysed maps (the population of the profile);
      ``n_failed``     int    maps whose rung-1 draw is failed (their AI map is missing);
      ``ai_per_map``   float  n_ai_nodes / n_maps;
      ``source``       str    "kit:a12_ladder" (the analysis script that makes the number, Ruling R11).

    Row 2 divides ``n_ai_nodes`` by its endorsed-boxes count and marks the cell ``one_draw`` / ``no_interval``."""
    if "rung1" not in pipeline.ladder_rungs(ctx.cfg):
        return None
    got = _read_rungs(ctx, ["rung1"])["rung1"]
    if got["unfinished"]:
        return None
    by = {p.pid: p for p in got["pl"]}
    n_ai, failed = 0, 0
    for pm in ctx.analysed:
        p = by.get(pm.pid)
        if p is None or p.ai.get(ctx.spec) is None:
            failed += 1
        else:
            n_ai += len(p.ai[ctx.spec].nodes)
    n = len(ctx.analysed)
    return {"n_ai_nodes": n_ai, "n_maps": n, "n_failed": failed, "ai_per_map": (n_ai / n) if n else math.nan,
            "source": SRC14}
