"""Metrics for the prompt ladder (rung 1 first-draft prompt, rung 2 current prompt, P placebo, 3 remedy).

Design: paired contrasts between ladder arms with sign-flip permutation tests, Holm correction and bootstrap
intervals, the remedy verdict and the three-way split.  Everything here is a pure function of
pre-computed inputs (no model calls, no clock, no global randomness); every random draw takes a seed and is
recorded in the output.

Unit of data: ``MapDraw``, one reconstruction (one draw) of one map under one rung.  It carries one ``Item`` per
in-transcript endorsed node (the item's drawn state in the AI map), the AI node count, the counts behind the
false-wished rate, parse-failure and retry flags, and optionally the script-conditional units.  ``draw_from_pair_inputs``
builds a ``MapDraw`` from the existing ``metrics.PairInputs`` by reusing ``metrics`` helpers, so nothing about
matching, anchoring or the judge is re-implemented here.

Conventions
  * Three-way outcome over ALL endorsed ideals (endorsed status wished, in the transcript): ``kept`` = the AI has
    it as wished, ``leaked`` = the AI has it as experienced, ``missed`` = no AI node matches it.  No conditioning on
    a match, so a remedy that "drops" leaky ideals shows up as ``missed``, never as ``kept``.  The precedence among
    several matching AI nodes is ``metrics.absence_category``'s (located nodes decide first; if no matching node is
    located, unlocated ones decide and the item is ``unattested``: counted as neither kept, leaked, missed nor recalled).
  * Fixed denominator: every (map, draw) cell of the arm stays in.  A parse failure is a cell whose items are all
    ``missed`` (no AI map), reported separately (``parse_failures``, ``retries``).
  * Per-map quantities with an empty denominator are nan and drop out of the paired comparison (counted in
    ``n_excluded``); outputs are JSON-safe (nan -> None).
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, replace

from .audit.registry import DECLARED_CONTRASTS, AuditLog
from .match import MatchSetting
from .metrics import (LOW_POWER_N, PairInputs, ScriptSource, _absence_counts, _absence_structure, _clean, _map_counts, _pooled_diff,
                      _script_maps, _seed, absence_category, contradiction_rate, spec_arm)
from .stats import _percentile_ci, bootstrap_ci

ITEM_STATES = ("wished", "experienced", "missed", "unattested")
ENDORSED_STATUSES = ("wished", "experienced")
_NAN = math.nan

# absence_category -> item state. Only ATTESTED matches (an AI node whose quote is found in the transcript and
# eligible in the arm: ``metrics``' "located") decide wished / experienced, as everywhere else in the code base; a
# match through nodes that are not located is "unattested", reported on its own.
_CATEGORY_STATE = {
    "recovered_as_wished": "wished",
    "leaked_as_existing": "experienced",
    "unanchored_wished": "unattested",
    "leaked_unanchored": "unattested",
    "missed": "missed",
}


def _ratio(a: float, b: float) -> float:
    return a / b if b else _NAN


def _mean(xs) -> float:
    xs = [x for x in xs if not math.isnan(x)]
    return math.fsum(xs) / len(xs) if xs else _NAN


@dataclass(frozen=True)
class Item:
    """One in-transcript endorsed node and how the AI map drew it. ``state``: "wished" | "experienced" (matched
    by an attested AI node drawn that way) | "missed" (no AI node matches) | "unattested" (matched only by AI nodes
    whose quote is not found in the transcript)."""
    item_id: str
    endorsed_status: str
    state: str

    def __post_init__(self):
        if self.endorsed_status not in ENDORSED_STATUSES:
            raise ValueError(f"endorsed_status {self.endorsed_status!r} not in {ENDORSED_STATUSES}")
        if self.state not in ITEM_STATES:
            raise ValueError(f"state {self.state!r} not in {ITEM_STATES}")


@dataclass(frozen=True)
class MapDraw:
    """One draw of one map under one rung (see the module docstring)."""
    map_id: str
    draw: int
    items: tuple
    n_ai_nodes: int = 0
    n_actual_quote_nodes: int = 0            # located AI nodes whose quote the judge labels "actual"
    n_actual_quote_marked_wished: int = 0    # ... of which drawn as wished (metrics.contradiction_rate's mirror)
    n_control: int = 0                       # endorsed EXPERIENCED in-transcript nodes (metrics.absences ``n_control``)
    n_flipped: int = 0                       # ... matched only by located AI nodes ALL drawn as wished (``flipped``)
    parse_failed: bool = False
    retries: int = 0
    script_units: tuple | None = None        # per matched endorsed ideal: (scripted, leaked); None = not supplied

    @staticmethod
    def failed(map_id: str, draw: int, items, retries: int = 0) -> "MapDraw":
        """A parse failure: no AI map, every item missed."""
        items = tuple(replace(i, state="missed") for i in items)
        return MapDraw(map_id, draw, items, n_control=sum(i.endorsed_status == "experienced" for i in items),
                       parse_failed=True, retries=retries)


def _check_cells(draws: list[MapDraw]) -> None:
    if not draws:
        raise ValueError("no map draws")
    seen = set()
    for d in draws:
        k = (d.map_id, d.draw)
        if k in seen:
            raise ValueError(f"duplicate map draw {k}")
        seen.add(k)


def _by_map(draws: list[MapDraw]) -> dict[str, list[MapDraw]]:
    out: dict[str, list[MapDraw]] = {}
    for d in draws:
        out.setdefault(d.map_id, []).append(d)
    return out


# ------------------------------------------------------------------ item 1: the three-way outcome

def _ideal_counts(ds: list[MapDraw]) -> dict:
    c = {"n_ideals": 0, "kept": 0, "leaked": 0, "missed": 0, "unattested": 0}
    for d in ds:
        for i in d.items:
            if i.endorsed_status != "wished":
                continue
            c["n_ideals"] += 1
            c[{"wished": "kept", "experienced": "leaked"}.get(i.state, i.state)] += 1
    return c


def _rates(c: dict) -> dict:
    n = c["n_ideals"]
    return {"kept_rate": _ratio(c["kept"], n), "leaked_rate": _ratio(c["leaked"], n),
            "missed_rate": _ratio(c["missed"], n), "unattested_rate": _ratio(c["unattested"], n)}


def three_way(draws: list[MapDraw]) -> dict:
    """Kept / leaked / missed over ALL endorsed ideals of one rung, per map (draws pooled within the map) and
    pooled over every cell. ``map_mean`` averages the per-map rates (maps without ideals left out).
    ``n_cells`` = maps x draws (the fixed denominator); ``parse_failures`` / ``retries`` are per rung."""
    _check_cells(draws)
    per_map = []
    for mid, ds in _by_map(draws).items():
        c = _ideal_counts(ds)
        per_map.append({"map_id": mid, "n_draws": len(ds), **c, **_rates(c)})
    pooled = _ideal_counts(draws)
    pooled.update(_rates(pooled))
    with_ideals = [m for m in per_map if m["n_ideals"]]
    return {"n_cells": len(draws), "n_maps": len(per_map), "n_maps_with_ideals": len(with_ideals),
            "pooled": pooled, "per_map": per_map,
            "map_mean": {k: _mean(m[k] for m in with_ideals) for k in ("kept_rate", "leaked_rate", "missed_rate", "unattested_rate")},
            "parse_failures": sum(d.parse_failed for d in draws), "retries": sum(d.retries for d in draws)}


# ------------------------------------------------------------------ adapter from the existing pipeline inputs

def draw_from_pair_inputs(p: PairInputs, spec_key: str, draw: int, setting: MatchSetting, *,
                          min_words: int = 8, retries: int = 0, source: ScriptSource | None = None) -> MapDraw:
    """The ``MapDraw`` of one participant's map for one spec (= one draw of one rung), built with the
    ``metrics`` helpers: ``_absence_structure`` / ``absence_category`` for the per-item state. A missing AI map
    (``p.ai[spec_key] is None``) is a parse failure. ``retries`` (corrective retries the call needed) is not in
    ``PairInputs``; the pipeline passes it. ``source`` (a ``metrics.ScriptSource``), when given, fills
    ``script_units`` (per matched ideal: (scripted, leaked)) with ``metrics._script_maps``; left None when the map
    has no script, no alignment or no matched ideal (the map then drops out of the script-conditional gap)."""
    endorsed = [(n.node_id, n.status) for n in p.endorsed.nodes if p.in_transcript[n.node_id]]
    if p.ai.get(spec_key) is None:
        return MapDraw.failed(p.pid, draw, tuple(Item(nid, ns, "missed") for nid, ns in endorsed), retries)
    arm = spec_arm(spec_key)
    st = _absence_structure(p, spec_key, arm, setting, min_words)
    ac = _absence_counts(st, st["status"])           # the exact rule of metrics.absences: n_control and flipped
    items = []
    for nid, ns in endorsed:
        cat, _ = absence_category(st, st["status"], nid)
        items.append(Item(nid, ns, _CATEGORY_STATE[cat]))
    # the false-wished definition is metrics.contradiction_rate's "quote_actual_marked_wished" (located nodes whose
    # quote the judge labels actual that the AI drew as wished); one participant -> its own counts, B is irrelevant
    fw = contradiction_rate([p], spec_key, min_words=min_words, B=1)["separate_samples"]["quote_actual_marked_wished"]
    units = None
    if source is not None:
        maps, _ = _script_maps([p], spec_key, setting, source, min_words)
        units = tuple(maps[0]) if maps else None
    return MapDraw(p.pid, draw, tuple(items), n_ai_nodes=len(p.ai[spec_key].nodes),
                   n_actual_quote_nodes=fw["n_nodes"], n_actual_quote_marked_wished=fw["n_contradicting"],
                   n_control=ac["n_control"], n_flipped=ac["flipped"], retries=retries, script_units=units)


# ------------------------------------------------------------------ item 2: mirror metrics per rung

OUTCOME_METRICS = ("leaked_rate", "kept_rate", "missed_rate", "unattested_rate")
MIRROR_METRICS = ("false_wished_rate", "actual_recall", "ai_node_count", "flip_rate")
DESCRIPTIVE_METRICS = ("disagreement_vs_rung2", "disagreement_vs_rung2_experienced")     # never gate
ALL_METRICS = OUTCOME_METRICS + MIRROR_METRICS + DESCRIPTIVE_METRICS


def _metric_parts(d: MapDraw, ref: MapDraw | None = None) -> dict[str, tuple[float, float]]:
    """metric -> (numerator, denominator) of one draw. Definitions:
      leaked/kept/missed/unattested_rate   share of the draw's endorsed ideals (see ``three_way``);
      false_wished_rate        located AI nodes whose quote the judge labels actual, share drawn as wished
                               (``metrics.contradiction_rate``'s quote_actual_marked_wished); a failed draw has
                               no nodes, so it has no rate (nan), not a zero;
      actual_recall            endorsed in-transcript EXPERIENCED items that some ATTESTED AI node matches (any drawn
                               status; unattested matches do not count); a failed draw recalls nothing (0);
      ai_node_count            nodes in the AI map (0 for a failed draw);
      flip_rate                ``metrics.absences``' flip rate: endorsed EXPERIENCED in-transcript nodes matched only
                               by located AI nodes all drawn as wished, over the experienced nodes (the
                               over-correction mirror of leakage); a failed draw flips nothing (0 over its controls);
      disagreement_vs_rung2    DESCRIPTIVE, never a gate: share of items (all in-transcript endorsed nodes) whose
                               state differs from the same item in ``ref`` (rung 2's output for the same map and
                               draw index; draws are independent samples, so this includes benign churn);
      disagreement_vs_rung2_experienced   the same over endorsed EXPERIENCED items only."""
    ideals = [i for i in d.items if i.endorsed_status == "wished"]
    exper = [i for i in d.items if i.endorsed_status == "experienced"]
    n = len(ideals)
    parts = {"leaked_rate": (sum(i.state == "experienced" for i in ideals), n),
             "kept_rate": (sum(i.state == "wished" for i in ideals), n),
             "missed_rate": (sum(i.state == "missed" for i in ideals), n),
             "unattested_rate": (sum(i.state == "unattested" for i in ideals), n),
             "false_wished_rate": (d.n_actual_quote_marked_wished, d.n_actual_quote_nodes),
             "actual_recall": (sum(i.state in ("wished", "experienced") for i in exper), len(exper)),
             "ai_node_count": (d.n_ai_nodes, 1),
             "flip_rate": (d.n_flipped, d.n_control),
             "disagreement_vs_rung2": (0, 0), "disagreement_vs_rung2_experienced": (0, 0)}
    if ref is not None:
        r = {i.item_id: i for i in ref.items}
        if set(r) != {i.item_id for i in d.items}:
            raise ValueError(f"{d.map_id} draw {d.draw}: items differ from the reference draw's items")
        diff = [i.state != r[i.item_id].state for i in d.items]
        diff_x = [i.state != r[i.item_id].state for i in exper]
        parts["disagreement_vs_rung2"] = (sum(diff), len(diff))
        parts["disagreement_vs_rung2_experienced"] = (sum(diff_x), len(diff_x))
    return parts


def metric_values(d: MapDraw, ref: MapDraw | None = None) -> dict[str, float]:
    """Every metric of one draw (nan where the denominator is empty)."""
    return {k: _ratio(a, b) for k, (a, b) in _metric_parts(d, ref).items()}


def _reference_lookup(reference: list[MapDraw] | None) -> dict | None:
    if reference is None:
        return None
    _check_cells(reference)
    return {(d.map_id, d.draw): d for d in reference}


def metric_table(draws: list[MapDraw], reference: list[MapDraw] | None = None) -> dict:
    """metric -> {"per_map": {map_id: value or nan}, "pooled": value, "n_maps_used": int}. The per-map value is
    the mean of the draws' defined values (draws averaged within the map first); ``pooled`` is the ratio of the
    summed numerators and denominators over every cell. The disagreement metrics need ``reference`` (rung 2's
    draws): a draw without a reference draw has no value (counted in ``disagreement_reference_draws_missing``)."""
    _check_cells(draws)
    ref = _reference_lookup(reference)
    missing = 0
    cells = []                                     # (map_id, parts)
    for d in draws:
        r = ref.get((d.map_id, d.draw)) if ref is not None else None
        missing += ref is not None and r is None
        parts = _metric_parts(d, r)
        cells.append((d.map_id, parts, {k: _ratio(a, b) for k, (a, b) in parts.items()}))
    out: dict = {"disagreement_reference_draws_missing": missing, "metrics": {}}
    for k in ALL_METRICS:
        per_map: dict[str, float] = {}
        for mid in dict.fromkeys(c[0] for c in cells):
            per_map[mid] = _mean(c[2][k] for c in cells if c[0] == mid)
        num = sum(c[1][k][0] for c in cells)
        den = sum(c[1][k][1] for c in cells)
        out["metrics"][k] = {"per_map": per_map, "pooled": _ratio(num, den),
                             "n_maps_used": sum(not math.isnan(v) for v in per_map.values())}
    return out


def rung_summary(draws: list[MapDraw], *, reference: list[MapDraw] | None = None) -> dict:
    """Everything reported for one rung: the three-way outcome, the mirror metrics (per map, map mean, pooled),
    fixed-denominator cell count, parse failures and retries. ``reference`` = rung 2's draws, for the descriptive disagreement.
    JSON-safe (nan -> None)."""
    tw = three_way(draws)
    tab = metric_table(draws, reference)
    mirror, descriptive = {}, {}
    for group, names in ((mirror, MIRROR_METRICS), (descriptive, DESCRIPTIVE_METRICS)):
        for k in names:
            m = tab["metrics"][k]
            group[k] = {**m, "map_mean": _mean(m["per_map"].values())}
    n = len(draws)
    return _clean({"n_cells": n, "n_maps": tw["n_maps"], "three_way": tw, "mirror": mirror, "descriptive": descriptive,
                   "parse_failures": tw["parse_failures"], "parse_failure_rate": _ratio(tw["parse_failures"], n),
                   "retries": tw["retries"],
                   "disagreement_reference": "rung 2, same map and draw" if reference is not None else None,
                   "disagreement_reference_draws_missing": tab["disagreement_reference_draws_missing"]})


# ------------------------------------------------------------------ item 3: contrasts, tests, verdict

# Rung labels are words, not single letters/digits: a contrast name made of a letter, a hyphen and a digit reads
# as a participant id to the leak scan, and these names reach the published reports.
RUNG1, RUNG2, PLACEBO, REMEDY = "rung1", "rung2", "placebo", "remedy"
PRIMARY_CONTRASTS = ((REMEDY, PLACEBO), (PLACEBO, RUNG2), (RUNG2, RUNG1))   # remedy - placebo, placebo - rung 2, rung 2 - rung 1
REFERENCE_RUNG = RUNG2
MIN_MAPS = 3
# Short codes for what used to be sentences in the report: the aggregate-only guard allows no string over 40 characters
# in a real corpus's outputs, so the report carries codes and the report renderer (``report.ladder_markdown``) says them
# in words. CI_CODE: percentile bootstrap over maps (the clusters). TEST_CODE: two-sided sign-flip permutation, exact up
# to ``EXACT_MAX_MAPS`` non-zero differences, else Monte Carlo with the +1 correction. WORSE_CODE: the confidence
# interval excludes 0 on the bad side. NO_TEST_CODE: the script-conditional gap is reported per rung, with no test.
CI_CODE, TEST_CODE, WORSE_CODE, NO_TEST_CODE = "boot_maps", "signflip", "ci_excludes_zero_bad_side", "no_test"
# metric -> (which way is better, whether the remedy verdict gates on it)
#   flip_rate is metrics.absences' flip (experienced nodes drawn as wished). The disagreement metrics are descriptive.
#   ai_node_count: fewer nodes than the placebo is lost content, so a CI below 0 counts as worse.
POLARITY = {"leaked_rate": ("lower", False), "kept_rate": ("higher", False), "missed_rate": ("lower", True),
            "unattested_rate": ("lower", False),
            "false_wished_rate": ("lower", True), "actual_recall": ("higher", True),
            "ai_node_count": ("higher", True), "flip_rate": ("lower", True),
            "disagreement_vs_rung2": (None, False), "disagreement_vs_rung2_experienced": (None, False)}


EXACT_MAX_MAPS = 12      # up to this many non-zero paired differences the 2^n sign patterns are enumerated


def sign_flip_p(diffs: list[float], n_perm: int, seed: int) -> float:
    """Two-sided sign-flip permutation p of mean(diffs) = 0. With at most ``EXACT_MAX_MAPS`` non-zero differences
    all 2^n sign patterns are enumerated (exact p; ``n_perm`` and ``seed`` unused). Otherwise each difference
    gets a random sign, ``n_perm`` times with ``random.Random(seed)``, and
    p = (1 + #{|null sum| >= |observed|}) / (1 + n_perm), so it is never 0. All-zero differences give exactly 1."""
    n = len(diffs)
    if n == 0:
        return _NAN
    obs = abs(math.fsum(diffs))
    if obs == 0.0:
        return 1.0
    nz = [d for d in diffs if d != 0.0]
    if len(nz) <= EXACT_MAX_MAPS:
        sums = [0.0]
        for d in nz:
            sums = [s + d for s in sums] + [s - d for s in sums]
        return sum(1 for s in sums if abs(s) >= obs - 1e-12) / len(sums)
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        t = math.fsum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(t) >= obs - 1e-12:
            ge += 1
    return (1 + ge) / (1 + n_perm)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values (nan p-values are left out of the family and stay nan)."""
    valid = sorted(((p, k) for k, p in pvals.items() if not math.isnan(p)))
    m = len(valid)
    out = {k: _NAN for k in pvals}
    running = 0.0
    for i, (p, k) in enumerate(valid):
        running = max(running, (m - i) * p)
        out[k] = min(1.0, running)
    return out


def paired_contrast(a: dict[str, float], b: dict[str, float], *, name: str, n_perm: int, seed: int, B: int,
                    alpha: float = 0.05, min_maps: int = MIN_MAPS) -> dict:
    """a - b paired by map (``a`` / ``b``: map id -> value, the draws already averaged within the map). Maps with a
    nan on either side drop out (``n_excluded``). Sign-flip permutation p (two-sided) and a percentile bootstrap CI
    that resamples MAPS (clusters); fewer than ``min_maps`` usable maps gives no p and no CI (nan), never a
    degenerate interval. ``n_perm``, ``seed`` and ``B`` are recorded in the result."""
    if set(a) != set(b):
        raise ValueError(f"contrast {name}: the arms do not cover the same maps "
                         f"({sorted(set(a) ^ set(b))[:5]} differ)")
    ids = sorted(a)
    used = [i for i in ids if not (math.isnan(a[i]) or math.isnan(b[i]))]
    d = [a[i] - b[i] for i in used]
    out = {"n_maps": len(ids), "n_used": len(used), "n_excluded": len(ids) - len(used),
           "mean_a": _mean(a[i] for i in used), "mean_b": _mean(b[i] for i in used),
           "mean": math.fsum(d) / len(d) if d else _NAN, "alternative": "two-sided",
           "n_perm": n_perm, "seed": seed, "B": B, "alpha": alpha, "min_maps": min_maps}
    if len(used) < min_maps:
        out.update(ci_lo=_NAN, ci_hi=_NAN, p_perm=_NAN, ci_excludes_zero=False)
        return out
    _, lo, hi = bootstrap_ci(d, B=B, alpha=alpha, seed=_seed(seed, f"{name}|boot"))
    out.update(ci_lo=lo, ci_hi=hi, p_perm=sign_flip_p(d, n_perm, _seed(seed, f"{name}|perm")),
               p_method="exact" if sum(x != 0.0 for x in d) <= EXACT_MAX_MAPS else "monte_carlo",
               ci_excludes_zero=bool(lo > 0 or hi < 0))
    return out


def check_equal_draws(x: list[MapDraw], y: list[MapDraw], nx: str, ny: str) -> None:
    """The compared arms must cover the same maps with the same number of draws per map."""
    cx = {m: len(ds) for m, ds in _by_map(x).items()}
    cy = {m: len(ds) for m, ds in _by_map(y).items()}
    if set(cx) != set(cy):
        raise ValueError(f"arms {nx} and {ny} do not cover the same maps: {sorted(set(cx) ^ set(cy))[:5]} differ")
    bad = {m: (cx[m], cy[m]) for m in cx if cx[m] != cy[m]}
    if bad:
        raise ValueError(f"unequal draw counts between arms {nx} and {ny} (map: draws {nx}, {ny}): "
                         f"{dict(list(bad.items())[:5])}")


def ladder_contrasts(arms: dict[str, list[MapDraw]], *, reference: str = REFERENCE_RUNG,
                     contrasts=PRIMARY_CONTRASTS, n_perm: int = 10000, seed: int = 0, B: int = 2000,
                     alpha: float = 0.05, min_maps: int = MIN_MAPS) -> dict:
    """Every contrast (a, b) of ``contrasts`` (a - b) on every metric, paired by map. ``arms``: rung label ->
    its MapDraws; the labels are ``RUNG1``="rung1", ``RUNG2``="rung2", ``PLACEBO``="placebo", ``REMEDY``="remedy". Flip is measured against ``reference`` (rung 2).
    A contrast whose arm is absent is skipped and listed under ``skipped``; arms that ARE compared must have
    equal draw counts per map (``ValueError`` otherwise). Holm runs over the leak-rate p-values of the contrasts
    actually computed (``holm_leak``; the three primary ones when all arms are present)."""
    ref = arms.get(reference)
    tables = {k: metric_table(v, ref)["metrics"] for k, v in arms.items()}
    out: dict = {"parameters": {"n_perm": n_perm, "seed": seed, "B": B, "alpha": alpha, "min_maps": min_maps,
                                "reference": reference, "alternative": "two-sided", "ci": CI_CODE},
                 "contrasts": {}, "skipped": []}
    for x, y in contrasts:
        name = f"{x}-{y}"
        if x not in arms or y not in arms:
            out["skipped"].append({"contrast": name, "reason": f"arm {x if x not in arms else y} not run"})
            continue
        check_equal_draws(arms[x], arms[y], x, y)
        res = {}
        for k in ALL_METRICS:
            if k in DESCRIPTIVE_METRICS and reference in (x, y):
                # one side is the reference itself: its disagreement with itself is 0 by construction
                res[k] = {"status": "not_applicable", "reason": f"one arm is the reference ({reference})",
                          "better": POLARITY[k][0], "gating": POLARITY[k][1]}
                continue
            res[k] = paired_contrast(tables[x][k]["per_map"], tables[y][k]["per_map"], name=f"{name}|{k}",
                                     n_perm=n_perm, seed=seed, B=B, alpha=alpha, min_maps=min_maps)
            res[k]["better"], res[k]["gating"] = POLARITY[k]
        out["contrasts"][name] = {"a": x, "b": y, "metrics": res,
                                  "draws_per_map": sorted({len(v) for v in _by_map(arms[x]).values()})}
    adj = holm({n: c["metrics"]["leaked_rate"]["p_perm"] for n, c in out["contrasts"].items()})
    for n, c in out["contrasts"].items():
        c["metrics"]["leaked_rate"]["p_holm"] = adj[n]
    out["holm_leak"] = {"m": len(adj), "alpha": alpha, "adjusted": adj,
                        "significant": {n: bool(p < alpha) for n, p in adj.items()}}
    return out


def _script_rung(draws: list[MapDraw], seed: int, B: int) -> dict:
    """Pooled scripted-minus-unscripted leak rate of one rung with a bootstrap CI over MAPS. No permutation p: a
    map's draws are pooled, so one ideal appears up to once per draw with the same script flag, and a within-map
    permutation of those copies would treat them as exchangeable and understate the variance."""
    maps = []
    for mid, ds in _by_map(draws).items():
        units = [u for d in sorted(ds, key=lambda d: d.draw) if d.script_units is not None for u in d.script_units]
        if any(d.script_units is not None for d in ds) and units:
            maps.append(units)
    if not maps:
        return {"status": "no_script_units", "n_maps": 0}
    counts = [_map_counts(u) for u in maps]
    ns, ls = sum(c[0] for c in counts), sum(c[1] for c in counts)
    nu, lu = sum(c[2] for c in counts), sum(c[3] for c in counts)
    est = _pooled_diff(counts)
    out = {"status": "ok", "n_maps": len(maps), "n_scripted": ns, "n_unscripted": nu, "n_leaked_scripted": ls,
           "n_leaked_unscripted": lu, "leak_rate_scripted": _ratio(ls, ns), "leak_rate_unscripted": _ratio(lu, nu),
           "estimate": est, "low_power": bool(ns < LOW_POWER_N or nu < LOW_POWER_N), "seed": seed, "B": B}
    if math.isnan(est):
        out.update(ci_lo=_NAN, ci_hi=_NAN)
        return out
    rng = random.Random(_seed(seed, "boot"))
    m, boots = len(counts), []
    for _ in range(B):
        dd = _pooled_diff([counts[rng.randrange(m)] for _ in range(m)])
        if not math.isnan(dd):
            boots.append(dd)
    out["ci_lo"], out["ci_hi"] = _percentile_ci(boots, 0.05) if boots else (_NAN, _NAN)
    return out


def script_conditional_by_rung(arms: dict[str, list[MapDraw]], *, seed: int = 0, B: int = 2000) -> dict:
    """The script-conditional gap (pooled leak rate of scripted minus unscripted ideals, the definition of
    ``metrics.script_conditional_row``) for each rung, from ``MapDraw.script_units``, with a bootstrap CI over
    maps. DESCRIPTIVE ONLY: each rung's own estimate and CI are reported side by side in ``gaps``; there is no
    test, within or between rungs. Draws of a map are pooled into one cluster."""
    rungs = {k: _script_rung(v, _seed(seed, f"script|{k}"), B) for k, v in arms.items()}
    return _clean({"descriptive_only": True, "note": NO_TEST_CODE,
                   "rungs": rungs, "gaps": {k: r.get("estimate") for k, r in rungs.items()}})


def verdict_rules(*, require_kept_gain: bool = False, require_holm: bool = True, alpha: float = 0.05,
                  n_perm: int = 10000, seed: int = 0, B: int = 2000, min_maps: int = MIN_MAPS) -> dict:
    """The verdict rule as data: which metrics gate and which way is better, the two switches, and the test
    parameters. Stored in the ``declared_contrasts`` record, echoed in the verdict, compared by
    ``compare_to_declaration``."""
    return {"gating": {k: b for k, (b, g) in POLARITY.items() if g},
            "descriptive": [k for k, (_, g) in POLARITY.items() if not g],
            "remedy_contrast": f"{REMEDY}-{PLACEBO}", "require_kept_gain": bool(require_kept_gain),
            "require_holm": bool(require_holm), "alpha": float(alpha), "n_perm": int(n_perm), "seed": int(seed),
            "B": int(B), "min_maps": int(min_maps),
            "ci": CI_CODE, "test": TEST_CODE, "exact_max_nonzero": EXACT_MAX_MAPS, "worse": WORSE_CODE}


def analysis_config(*, contrasts=None, outcomes=None, draws: dict | None = None, n_perm: int = 10000, seed: int = 0,
                    B: int = 2000, alpha: float = 0.05, min_maps: int = MIN_MAPS, require_kept_gain: bool = False,
                    require_holm: bool = True) -> dict:
    """What an analysis does, in the shape stored by ``declare_contrasts``: contrasts, outcomes, draws per map by
    arm, test parameters and the verdict rules. Defaults: the three primary contrasts, every metric."""
    return {"contrasts": [f"{a}-{b}" for a, b in PRIMARY_CONTRASTS] if contrasts is None else [str(c) for c in contrasts],
            "outcomes": list(ALL_METRICS) if outcomes is None else [str(o) for o in outcomes],
            "draws": {str(k): int(v) for k, v in (draws or {}).items()},
            "parameters": {"n_perm": int(n_perm), "seed": int(seed), "B": int(B), "alpha": float(alpha),
                           "min_maps": int(min_maps), "reference": REFERENCE_RUNG, "alternative": "two-sided"},
            "verdict_rules": verdict_rules(require_kept_gain=require_kept_gain, require_holm=require_holm,
                                           alpha=alpha, n_perm=n_perm, seed=seed, B=B, min_maps=min_maps)}


def _num(x) -> float:
    """None (a JSON-cleaned nan) reads as nan."""
    return _NAN if x is None else x


def remedy_verdict(result: dict, *, remedy: str = f"{REMEDY}-{PLACEBO}", require_kept_gain: bool = False,
                   require_holm: bool = True) -> dict:
    """Does the remedy help? ``result`` = ``ladder_contrasts`` output (or a JSON-cleaned ``ladder_report``). "helps"
    only if
      (1) the leak-rate drop of the remedy-minus-placebo contrast has a CI that excludes 0 (upper bound < 0),
      (2) no GATING metric is worse with its CI excluding 0 in the bad direction: false-wished rate up, actual recall
          down, AI node count down (fewer nodes than the placebo is lost content; a remedy that only removes padding
          would be flagged, by design), flip rate up (experienced nodes drawn as wished), MISSED rate up (so a remedy
          that merely drops leaky ideals cannot pass: its leaks become missed, not kept), and
      (3) the Holm-adjusted sign-flip p of the leak contrast is below alpha (``require_holm``; not in the design
          text: the percentile bootstrap CI alone excludes 0 in ~7-10% of null samples at 12-46 maps, the
          permutation test is calibrated). The same test guards the opposite verdict "harms".
    ``require_kept_gain`` (default False) additionally demands that the kept rate rose (CI lower bound > 0).
    Verdicts: "helps"; "not_helps" (leak fell but (2) or ``require_kept_gain`` failed); "null" (leak CI includes 0:
    reports the CI and the largest leak drop it excludes); "harms" (leak CI above 0 and Holm p < alpha);
    "inconclusive" (a gating metric could not be assessed, or the CI excludes 0 but the Holm p does not);
    "not_run" (no such contrast). Each gating metric is a one-sided check at about 2.5% nominal, so the chance that
    at least one fires by luck is reported (``nominal_false_alarm_rate``; larger in practice, the bootstrap is
    liberal). Each verdict carries ``reasons`` (sentences, for a person reading it here) and ``reason_codes`` (short
    codes: what the report keeps, see ``ladder_report``)."""
    c = (result.get("contrasts") or {}).get(remedy)
    if c is None:
        return {"verdict": "not_run", "reasons": [f"contrast {remedy} was not computed"],
                "reason_codes": ["contrast_not_run"]}
    m = c["metrics"]
    alpha = (result.get("parameters") or {}).get("alpha", 0.05)
    lk = m["leaked_rate"]
    lo, hi, ph = _num(lk["ci_lo"]), _num(lk["ci_hi"]), _num(lk.get("p_holm", _NAN))
    n_gate = sum(1 for _, g in POLARITY.values() if g)
    prm = result.get("parameters") or {}
    rules = verdict_rules(require_kept_gain=require_kept_gain, require_holm=require_holm, alpha=alpha,
                          n_perm=prm.get("n_perm", 0), seed=prm.get("seed", 0), B=prm.get("B", 0),
                          min_maps=prm.get("min_maps", MIN_MAPS))
    base = {"rules": rules, "leak_mean": lk["mean"], "leak_ci_lo": lo, "leak_ci_hi": hi, "leak_p_perm": lk["p_perm"],
            "leak_p_holm": lk.get("p_holm"), "n_gating_checks": n_gate,
            "nominal_false_alarm_rate": 1 - (1 - 0.025) ** n_gate}
    holm_fail = require_holm and not math.isnan(ph) and not (ph < alpha)
    if math.isnan(lo):
        return {"verdict": "inconclusive", "reasons": ["leak contrast has too few usable maps"], **base,
                "reason_codes": ["leak_too_few_maps"], "mirror_worsened": [], "not_assessed": ["leaked_rate"]}
    if lo > 0:
        if holm_fail:
            return {"verdict": "inconclusive", **base, "mirror_worsened": [], "not_assessed": [],
                    "reason_codes": ["leak_rose_holm_fails"],
                    "reasons": [f"leak CI is above 0 but the Holm-adjusted permutation p is {ph:.3f} (>= {alpha})"]}
        return {"verdict": "harms", **base, "reasons": ["leak rose with the remedy (CI above 0)"],
                "reason_codes": ["leak_rose"], "mirror_worsened": [], "not_assessed": []}
    if hi >= 0:
        drop = max(0.0, -lo)
        return {"verdict": "null", **base, "excludes_leak_drop_larger_than": drop,
                "statement": (f"leak change {lk['mean']:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}]; "
                              f"the data exclude a leak-rate drop larger than {drop:.2f} (the pull resists this remedy "
                              f"to that extent)"),
                "reasons": ["leak CI includes 0"], "reason_codes": ["leak_ci_includes_zero"],
                "mirror_worsened": [], "not_assessed": []}
    reasons, worsened, not_assessed, checks = [], [], [], {}
    for k, (better, gating) in POLARITY.items():
        if not gating:
            continue
        s = m[k]
        slo, shi = _num(s["ci_lo"]), _num(s["ci_hi"])
        if math.isnan(slo):
            not_assessed.append(k)
            checks[k] = {"assessed": False}
            continue
        bad = (better == "lower" and slo > 0) or (better == "higher" and shi < 0)
        checks[k] = {"assessed": True, "mean": s["mean"], "ci_lo": slo, "ci_hi": shi, "worse": bad}
        if bad:
            worsened.append(k)
            reasons.append(f"{k} worsened beyond its CI ({s['mean']:+.3f}, [{slo:+.3f}, {shi:+.3f}])")
    kept = m["kept_rate"]
    klo = _num(kept["ci_lo"])
    kept_ok = (not require_kept_gain) or (not math.isnan(klo) and klo > 0)
    if not kept_ok:
        reasons.append(f"kept rate did not rise ({kept['mean']:+.3f}, [{klo:+.3f}, {_num(kept['ci_hi']):+.3f}]): "
                       f"the leaks did not become kept ideals")
    out = {**base, "mirror_worsened": worsened, "not_assessed": not_assessed, "mirror_checks": checks,
           "kept_mean": kept["mean"], "kept_ci_lo": klo, "kept_ci_hi": _num(kept["ci_hi"])}
    if worsened or not kept_ok:
        codes = [f"mirror_worse:{k}" for k in worsened] + ([] if kept_ok else ["kept_not_risen"])
        return {"verdict": "not_helps", "reasons": reasons, "reason_codes": codes, **out}
    if not_assessed:
        return {"verdict": "inconclusive", "reasons": [f"could not assess: {', '.join(not_assessed)}"],
                "reason_codes": [f"not_assessed:{k}" for k in not_assessed], **out}
    if holm_fail:
        return {"verdict": "inconclusive", **out, "reason_codes": ["leak_drop_holm_fails"],
                "reasons": [f"leak CI excludes 0 but the Holm-adjusted permutation p is {ph:.3f} (>= {alpha})"]}
    return {"verdict": "helps", "reasons": ["leak drop CI excludes 0; no gating metric worse"],
            "reason_codes": ["leak_drop_no_mirror_worse"], **out}


VERDICT_PROSE = ("reasons", "statement")     # the verdict's sentences: not in the report (see ``ladder_report``)


def ladder_report(arms: dict[str, list[MapDraw]], *, reference: str = REFERENCE_RUNG, contrasts=PRIMARY_CONTRASTS,
                  n_perm: int = 10000, seed: int = 0, B: int = 2000, alpha: float = 0.05,
                  min_maps: int = MIN_MAPS, require_kept_gain: bool = False, require_holm: bool = True,
                  declaration_log=None, declaration_rung: str | None = None) -> dict:
    """The whole ladder result (JSON-safe): per-rung summaries, contrasts (with Holm), the descriptive
    script-conditional gap per rung, and the remedy verdict (which echoes the rules it applied). It is safe to write
    into the aggregates of a real corpus: every string is a short code or enum (no string over the aggregate-only
    guard's 40 characters; the verdict has ``reason_codes`` and numbers, its sentences are left out). With
    ``declaration_log`` (path of the hash-chained log) the analysis config is compared to the latest
    ``declared_contrasts`` record (``declaration_rung`` selects the rung) and ``declaration_check`` carries the
    label: a label, never a block."""
    ref = arms.get(reference)
    res = ladder_contrasts(arms, reference=reference, contrasts=contrasts, n_perm=n_perm, seed=seed, B=B,
                           alpha=alpha, min_maps=min_maps)
    verdict = remedy_verdict(res, require_kept_gain=require_kept_gain, require_holm=require_holm)
    res["parameters"]["verdict_rules"] = verdict["rules"] if "rules" in verdict else None
    out = {"parameters": res["parameters"],
           "rungs": {k: rung_summary(v, reference=ref) for k, v in arms.items()},
           "contrasts": res["contrasts"], "skipped": res["skipped"], "holm_leak": res["holm_leak"],
           "script_conditional": script_conditional_by_rung(arms, seed=seed, B=B),
           # codes and numbers only: the sentences (``reasons``, ``statement``) are built by the report renderer
           "verdict": {k: v for k, v in verdict.items() if k not in VERDICT_PROSE}}
    if declaration_log is not None:
        draws = {k: max(len(v) for v in _by_map(ds).values()) for k, ds in arms.items()}
        cfg = analysis_config(contrasts=[f"{a}-{b}" for a, b in contrasts], draws=draws, n_perm=n_perm, seed=seed, B=B,
                              alpha=alpha, min_maps=min_maps, require_kept_gain=require_kept_gain,
                              require_holm=require_holm)
        out["declaration_check"] = compare_to_declaration(declaration_log, cfg, rung=declaration_rung)
    return _clean(out)


# ------------------------------------------------------------------ item 4: declared contrasts (a record, not a lock)

def _read_records(log_path) -> tuple[list[dict], int]:
    """(parsed records, number of unreadable lines). A missing log is ([], 0); nothing here raises on content."""
    recs, bad = [], 0
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(r, dict) and all(k in r for k in ("hash", "seq", "ts", "record_type", "payload")):
                    recs.append(r)
                else:
                    bad += 1
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    return recs, bad


def read_declarations(log_path) -> list[dict]:
    """Every ``declared_contrasts`` record of the log in order: {"id" (the record hash), "seq", "ts",
    "declared_on" (the date of the hashed ``ts``: the only date there is), "payload", "amends", "amends_found"
    (does ``amends`` name an earlier declared_contrasts record), "amended_by" (ids), "superseded" (some later
    record amends it)}. A missing log gives []; unreadable lines are skipped, never raised on."""
    recs, _ = _read_records(log_path)
    out = [{"id": r["hash"], "seq": r["seq"], "ts": r["ts"], "declared_on": str(r["ts"])[:10], "payload": r["payload"],
            "amends": r["payload"].get("amends"), "amended_by": []}
           for r in recs if r["record_type"] == DECLARED_CONTRASTS]
    ids = {d["id"]: d for d in out}
    for d in out:
        target = ids.get(d["amends"]) if d["amends"] else None
        d["amends_found"] = target is not None and target["seq"] < d["seq"] if d["amends"] else None
        if target is not None and target is not d:
            target["amended_by"].append(d["id"])
    for d in out:
        d["superseded"] = bool(d["amended_by"])
    return out


def declare_contrasts(log: AuditLog, *, rung: str, contrasts=None, outcomes=None, draws: dict | None = None,
                      n_perm: int = 10000, seed: int = 0, B: int = 2000, alpha: float = 0.05,
                      min_maps: int = MIN_MAPS, require_kept_gain: bool = False, require_holm: bool = True,
                      corpus_kind: str | None = None, note: str = "", amends: str | None = None) -> dict:
    """Append a dated ``declared_contrasts`` record to the hash-chained ``log`` and return it.

    Call it before a real rung: ``rung`` is the rung about to run, ``contrasts`` the contrast names
    ("remedy-placebo", ...; default the three primary ones), ``outcomes`` the metrics to be reported (default:
    all), ``draws`` the draws per map by arm, plus the test parameters and the verdict rules (gating metrics and
    directions, ``require_kept_gain``, ``require_holm``: see ``verdict_rules``). The date is the record's own
    hashed ``ts``. To change a declaration after the fact, call it again with ``amends=<the id (record hash) of
    the record being amended>``: that is a new record of the same type, chained after the original, which stays
    untouched; ``read_declarations`` then marks the original superseded. ``compare_to_declaration`` labels an
    analysis that differs from the latest record "exploratory_after_real_data".

    A record, not a lock (L6): nothing here checks that the declaration precedes the data, that it was followed,
    or that an ``amends`` id exists (an unknown id is recorded with ``amends_found: false``); no run reads these
    records to permit or refuse anything."""
    cfg = analysis_config(contrasts=contrasts, outcomes=outcomes, draws=draws, n_perm=n_perm, seed=seed, B=B,
                          alpha=alpha, min_maps=min_maps, require_kept_gain=require_kept_gain,
                          require_holm=require_holm)
    payload: dict = {"schema": "declared_contrasts/2", "kind": "amendment" if amends else "declaration",
                     "rung": str(rung), **cfg, "corpus_kind": corpus_kind, "note": str(note), "amends": amends}
    if amends:
        recs, bad = _read_records(log.path)
        payload["amends_found"] = any(r["hash"] == amends and r["record_type"] == DECLARED_CONTRASTS for r in recs)
        if bad:
            payload["log_unreadable_lines"] = bad
    return log.append(DECLARED_CONTRASTS, payload)


def _flat_differences(declared: dict, analysis: dict) -> list[dict]:
    diffs = []

    def add(field, d, a):
        if d != a:
            diffs.append({"field": field, "declared": d, "analysis": a})
    for k in ("contrasts", "outcomes"):
        add(k, sorted(declared.get(k) or []), sorted(analysis.get(k) or []))
    add("draws", declared.get("draws"), analysis.get("draws"))
    for group in ("parameters", "verdict_rules"):
        dd, aa = declared.get(group) or {}, analysis.get(group) or {}
        for k in sorted(set(dd) | set(aa)):
            add(f"{group}.{k}", dd.get(k), aa.get(k))
    return diffs


def compare_to_declaration(log_path, config: dict, *, rung: str | None = None) -> dict:
    """Compare an analysis config (``analysis_config``) with the LATEST non-superseded ``declared_contrasts`` record
    of the log (amendments included; ``rung`` restricts to that rung's records). Returns
    {"label", "declaration_id", "differences"}: "as_declared" (no difference), "exploratory_after_real_data"
    (something differs: list in ``differences``) or "undeclared" (no such record). A label for the report, never
    a block: it only reads the log and raises on nothing in it (L6)."""
    decl = [d for d in read_declarations(log_path) if rung is None or d["payload"].get("rung") == rung]
    live = [d for d in decl if not d["superseded"]]
    if not live:
        return {"label": "undeclared", "declaration_id": None, "differences": []}
    latest = live[-1]
    diffs = _flat_differences(latest["payload"], config)
    return {"label": "exploratory_after_real_data" if diffs else "as_declared", "declaration_id": latest["id"],
            "declared_on": latest["declared_on"], "differences": diffs}
