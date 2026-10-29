"""profile_links: row 4 of the profile (links between steps), ported from the analysis pack's ``a4_edges``.

For an endorsed edge u->v, A(u) and A(v) are the attested AI partners of its two ends (the kit's attested matching).
Each edge of the kit's edge denominator (the "universe": glued endorsed edges with both ends in the transcript, neither
end an endorsed node whose only possible match is a not-judged AI node) falls in one class, in this order:
  endpoint_lost  A(u) or A(v) is empty;
  collapsed      every pair (a, b) in A(u) x A(v) has a == b (both ends sit on one AI box);
  eligible       some a in A(u), b in A(v) with a != b.
Rates are over the eligible edges (collapsed edges are not in the conditional set): ``direct`` (an AI edge a->b, the
kit's own ``match.edge_recovered``), ``undirected`` (a->b or b->a) and ``path_le2`` (a directed AI path a->b or
a->x->b, b != a). The null permutes AI node identities within each map's partner sets (N_NULL = 1000 permutations,
seeded from ``ctx.seed``); ``direct_over_null`` is observed / expected.

Settings. Row 4 names two settings: ``primary`` = lexical .6 + "same" (the profile's primary) and ``lenient``, which
here is lexical .6 + "same_or_close" (``ROW4_LENIENT``), NOT ``profile_base.SETTINGS["lenient"]`` (lexical .4, used by
rows 1-3 and 7). Profile-v1 prints both under the word "lenient"; the constant is local so they are not confused.

Estimator: pooled ratio of sums over maps with a percentile cluster bootstrap (clusters = maps, ``ctx.B``,
``ctx.seed``). A bootstrap that dropped a replicate (a replicate with a zero denominator) has no defined interval: the
cell carries the point, ``ci_level`` null and ``note: "ci_undefined"``.

Private kit functions used (pinned by tests/test_profile_links.py): ``metrics._attested_matching``, ``metrics._nj``,
``match.edge_recovered``, ``provenance.eligible``; ``metrics.buckets`` is the cross-check of the universe.
"""
from __future__ import annotations

import math
import random

from . import match, metrics, provenance
from . import profile_base as pb
from .match import MatchSetting

N_NULL = 1000                         # permutations of the null (a4's N_NULL)
KINDS = ("direct", "undirected", "path_le2")
CLASSES = ("endpoint_lost", "collapsed", "eligible")
SOURCE = "kit:a4_edges"               # the kit analysis row 4 comes from (R11: never a cleared/ id on a lab's cells)

ROW4_LENIENT = MatchSetting(0.6, "same_or_close")        # row 4's "lenient": lex .6, not SETTINGS["lenient"] (lex .4)
ROW4_SETTINGS: dict[str, MatchSetting] = {"primary": pb.SETTINGS["primary"], "lenient": ROW4_LENIENT}
ROW4_RESTS_ON = ["R", "IT", "LX", "AL", "Q", "MO"]
ROW4_QUALIFIERS = ["no_structural_claim", "seam_withheld_k"]


# ------------------------------------------------------------------ pure functions (known-answer tested)
def classify(au, av) -> str:
    if not au or not av:
        return "endpoint_lost"
    if any(a != b for a in au for b in av):
        return "eligible"
    return "collapsed"


def successors(ai_edges) -> dict:
    succ: dict = {}
    for s, t in ai_edges:
        succ.setdefault(s, set()).add(t)
    return succ


def rec_direct(au, av, succ) -> bool:
    return any(t in av for a in au for t in succ.get(a, ()))


def rec_undirected(au, av, succ) -> bool:
    return rec_direct(au, av, succ) or rec_direct(av, au, succ)


def rec_path_le2(au, av, succ) -> bool:
    for a in au:
        for x in succ.get(a, ()):
            if x in av and x != a:
                return True
            for b in succ.get(x, ()):
                if b in av and b != a:
                    return True
    return False


RECOVER = {"direct": rec_direct, "undirected": rec_undirected, "path_le2": rec_path_le2}


def map_counts(edges, succ) -> dict:
    """edges: list of (A(u), A(v)) of one map's universe edges. Counts of the classes and of eligible recoveries."""
    c = dict.fromkeys(("n_universe", *CLASSES, *KINDS), 0)
    for au, av in edges:
        c["n_universe"] += 1
        cl = classify(au, av)
        c[cl] += 1
        if cl == "eligible":
            for kind in KINDS:
                c[kind] += RECOVER[kind](au, av, succ)
    return c


def null_expected(units, *, n_perm: int = N_NULL, seed: int = pb.SEED) -> dict:
    """units: per map (edges, succ). Each permutation maps every map's partner-set AI nodes to a random permutation of
    themselves (a bijection, so every class is kept) with the AI edges fixed; returns the mean pooled rate per kind
    (nan when there is no eligible edge)."""
    prepared = []
    for edges, succ in units:
        elig = [(au, av) for au, av in edges if classify(au, av) == "eligible"]
        domain = sorted({a for au, av in edges for a in (*au, *av)})
        prepared.append((elig, succ, domain))
    den = sum(len(e) for e, _, _ in prepared)
    if den == 0:
        return {kind: math.nan for kind in KINDS}
    rng = random.Random(seed)
    tot = dict.fromkeys(KINDS, 0)
    for _ in range(n_perm):
        for elig, succ, domain in prepared:
            if not elig:
                continue
            img = domain[:]
            rng.shuffle(img)
            pi = dict(zip(domain, img))
            for au, av in elig:
                pu, pv = {pi[a] for a in au}, {pi[b] for b in av}
                for kind in KINDS:
                    tot[kind] += RECOVER[kind](pu, pv, succ)
    return {kind: tot[kind] / (n_perm * den) for kind in KINDS}


# ------------------------------------------------------------------ the kit's inputs per map
def map_units(p, spec: str, arm: str, setting: MatchSetting, min_words: int):
    """(universe edges as (A(u), A(v), kit edge_recovered), AI successor sets) for one map at one setting."""
    m, m_att, _att = metrics._attested_matching(p, spec, arm, setting, min_words)
    ai = p.ai[spec]
    nj = metrics._nj(p, spec)
    nj_end = set()
    if nj:
        pv = p.prov[spec]
        nj_end = {e for a, e in m.pairs if a in nj and e not in m_att.right_matched
                  and provenance.eligible(pv[a], arm, min_words)[0]}
    partners: dict = {}
    for a, e in m_att.pairs:
        partners.setdefault(e, set()).add(a)
    it = p.in_transcript
    out = []
    for ed in p.endorsed.edges:
        if not (ed.glued and it[ed.source] and it[ed.target]) or ed.source in nj_end or ed.target in nj_end:
            continue
        out.append((frozenset(partners.get(ed.source, ())), frozenset(partners.get(ed.target, ())),
                    match.edge_recovered(ed, ai, m_att)))
    return out, successors((e.source, e.target) for e in ai.edges)


def analyse(ctx, setting: MatchSetting) -> dict:
    """{"per_map": [counts per analysed map], "null": expected pooled rate per kind} at one setting. Fails closed when
    a map's universe or recoveries disagree with the kit's ``metrics.buckets``."""
    spec = ctx.spec
    arm = metrics.spec_arm(spec)
    mw = int((ctx.cfg.stats or {}).get("min_words", 8))
    per_map, units = [], []
    for p in ctx.analysed:
        edges, succ = map_units(p, spec, arm, setting, mw)
        kb = metrics.buckets(p, spec, arm, setting, min_words=mw)
        if len(edges) != kb["n_edges_eligible"] or sum(r for _, _, r in edges) != kb["n_edges_recovered"]:
            raise RuntimeError("row 4: the edge universe disagrees with metrics.buckets (kit changed?)")
        if any(rec_direct(au, av, succ) != r for au, av, r in edges):
            raise RuntimeError("row 4: rec_direct disagrees with match.edge_recovered (kit changed?)")
        pairs = [(au, av) for au, av, _ in edges]
        per_map.append(map_counts(pairs, succ))
        units.append((pairs, succ))
    return {"per_map": per_map, "null": null_expected(units, seed=ctx.seed)}


# ------------------------------------------------------------------ the cells
def _undefined(name, x, unit, kind, code):
    return pb.not_run(name, [], x=x, unit=unit, kind=kind, reason_code=code, source=SOURCE)


def _rate_cell(name, x, pm, num, den, n, kind, B, seed):
    """A pooled share: ``num`` / ``den`` (map keys or functions) over the maps, with its percentile bootstrap."""
    nums = [m[num] if isinstance(num, str) else num(m) for m in pm]
    dens = [m[den] if isinstance(den, str) else den(m) for m in pm]
    pt, lo, hi, n_used = pb.cluster_bootstrap_ratio(nums, dens, B=B, seed=seed)
    if math.isnan(pt):
        return _undefined(name, x, "edge", kind, "undefined_no_denominator")
    ok = n_used == B and math.isfinite(lo) and math.isfinite(hi)
    return pb.cell(name, pt, x=x, n=n, unit="edge", kind=kind, ci=(lo, hi) if ok else None,
                   note=None if ok else "ci_undefined", source=SOURCE)


def setting_cells(res: dict, x: str, *, B: int, seed: int) -> list[dict]:
    """The cells of one setting from its per-map counts and null: ``primary`` has 10, ``lenient`` 4 (endpoint_lost,
    eligible_share, eligible_edges, direct)."""
    pm = res["per_map"]
    tot = {key: sum(m[key] for m in pm) for key in ("n_universe", *CLASSES, *KINDS)}
    nu, ne = tot["n_universe"], tot["eligible"]
    cells = [_rate_cell("endpoint_lost", x, pm, "endpoint_lost", "n_universe", nu, "share", B, seed)]
    if x == "primary":
        cells.append(_rate_cell("collapsed", x, pm, "collapsed", "n_universe", nu, "share", B, seed))
    cells.append(_rate_cell("eligible_share", x, pm, "eligible", "n_universe", nu, "share", B, seed))
    cells.append(pb.cell("eligible_edges", ne, x=x, n=nu, unit="edge", kind="count", source=SOURCE))
    direct = _rate_cell("direct", x, pm, "direct", "eligible", ne, "share", B, seed)
    cells.append(direct)
    if x != "primary":
        return cells
    ex = res["null"]["direct"]
    if math.isnan(ex):
        cells.append(_undefined("direct_null_expected", x, "edge", "share", "undefined_no_denominator"))
    else:
        cells.append(pb.cell("direct_null_expected", ex, x=x, n=ne, unit="edge", kind="share", source=SOURCE))
    if math.isnan(ex) or ex <= 0 or direct.get("value") is None:
        cells.append(_undefined("direct_over_null", x, "edge", "ratio", "undefined_null_zero_or_empty"))
    else:
        cells.append(pb.cell("direct_over_null", (tot["direct"] / ne) / ex, x=x, n=ne, unit="edge", kind="ratio",
                             source=SOURCE))
    cells.append(_rate_cell("undirected", x, pm, "undirected", "eligible", ne, "share", B, seed))
    cells.append(_rate_cell("path_le2", x, pm, "path_le2", "eligible", ne, "share", B, seed))
    cells.append(_rate_cell("collapsed_share_of_matched", x, pm, "collapsed",
                            lambda m: m["collapsed"] + m["eligible"], tot["collapsed"] + ne, "share", B, seed))
    return cells


def row4_cells(ctx) -> list[dict]:
    """The 14 cells of row 4, in profile order: 10 at x = primary, then 4 at x = lenient (lex .6 / same_or_close).
    The primary ``direct`` carries the band over the two settings (``over: "lenient"``)."""
    cells = []
    for x, setting in ROW4_SETTINGS.items():
        cells += setting_cells(analyse(ctx, setting), x, B=ctx.B, seed=ctx.seed)
    d = {c["x"]: c for c in cells if c["name"] == "direct"}
    primary, lenient = d["primary"], d["lenient"]
    if primary.get("value") is not None:
        if lenient.get("value") is not None:
            vals = [primary["value"], lenient["value"]]
            primary["band"] = pb.band(min(vals), max(vals), "lenient")
        else:
            ci = (primary["ci_lo"], primary["ci_hi"]) if "ci_lo" in primary else None
            cells[cells.index(primary)] = pb.partial_not_run(
                "direct", primary["value"], missing=["band"], x=primary["x"], n=primary["n"], unit=primary["unit"],
                kind=primary["kind"], ci=ci, ci_level=primary["ci_level"], note=primary.get("note"), source=SOURCE)
    return cells


def row4_meta(cells: list[dict]) -> dict:
    """The row-level fields of row 4 for the assembler: ``rests_on``, ``qualifiers``, ``source`` (["kit:a4_edges"]) and the
    row ``band`` (the primary ``direct`` cell's band, or None)."""
    direct = [c for c in cells if c["name"] == "direct" and c.get("x") == "primary"]
    return {"rests_on": list(ROW4_RESTS_ON), "qualifiers": list(ROW4_QUALIFIERS), "source": [SOURCE],
            "band": direct[0].get("band") if direct else None}
