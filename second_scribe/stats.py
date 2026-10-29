"""Small, pure, seeded statistics (standard library only).

Every function that draws random numbers takes a ``seed`` and uses its own
``random.Random(seed)``; the global ``random`` module is never touched, so a result is a
function of its inputs and its seed alone.  Bootstraps resample the unit that was observed
(participants, or participant pairs), never the elements inside it.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from typing import Callable, Sequence

_NAN3 = (math.nan, math.nan, math.nan)


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _percentile_ci(stats: list[float], alpha: float) -> tuple[float, float]:
    """Percentile method on ``stats`` (sorted here): the floor(alpha/2*B)-th and the
    (ceil((1-alpha/2)*B) - 1)-th order statistics."""
    stats.sort()
    b = len(stats)
    lo = stats[min(b - 1, int(alpha / 2 * b))]
    hi = stats[max(0, min(b - 1, math.ceil((1 - alpha / 2) * b) - 1))]
    return lo, hi


def bootstrap_ci(values: list[float], stat: Callable[[Sequence[float]], float] = statistics.mean,
                 B: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    """(point, lo, hi): percentile bootstrap of ``stat`` over ``values`` (each value is one unit,
    e.g. one participant).  Empty input gives (nan, nan, nan)."""
    n = len(values)
    if n == 0:
        return _NAN3
    f = _mean if stat is statistics.mean else stat      # same number, but exact-arithmetic mean is slow
    rng = random.Random(seed)
    boots = [f(rng.choices(values, k=n)) for _ in range(B)]
    lo, hi = _percentile_ci(boots, alpha)
    return f(values), lo, hi


def paired_bootstrap_ci(a: list[float], b: list[float], B: int = 2000, alpha: float = 0.05,
                        seed: int = 0) -> tuple[float, float, float]:
    """Bootstrap CI of mean(a - b). Resamples PAIRS (one pair per participant), never the elements of
    ``a`` and ``b`` separately."""
    if len(a) != len(b):
        raise ValueError(f"paired samples differ in length ({len(a)} vs {len(b)})")
    return bootstrap_ci([x - y for x, y in zip(a, b)], B=B, alpha=alpha, seed=seed)


def pairwise_bootstrap_ci(ma: Sequence[Sequence[float]], mb: Sequence[Sequence[float]], B: int = 2000,
                          alpha: float = 0.05, seed: int = 0,
                          mask: Sequence[Sequence[bool]] | None = None) -> tuple[float, float, float]:
    """Bootstrap CI of  mean over participant pairs of (ma - mb),  where ``ma[i][j]`` / ``mb[i][j]`` is a
    pairwise quantity (similarity or distance) between participants i and j in set A / set B.
    Resamples PARTICIPANTS with replacement; a pair is used only if its two draws are different
    participants (and ``mask[i][j]`` is true, when a mask is given), so a participant drawn twice never
    pairs with itself.  Resamples without a usable pair are skipped; no usable pair at all in the data gives (nan, nan, nan), and
    no usable resample gives the point estimate with nan bounds."""
    n = len(ma)
    if n < 2:
        return _NAN3
    d = [[ma[i][j] - mb[i][j] for j in range(n)] for i in range(n)]
    ok = [[(i != j) and (mask is None or bool(mask[i][j])) for j in range(n)] for i in range(n)]

    def pair_mean(idx: list[int]) -> float | None:
        vals = [d[a][b] for k, a in enumerate(idx) for b in idx[k + 1:] if ok[a][b]]
        return _mean(vals) if vals else None

    point = pair_mean(list(range(n)))
    if point is None:
        return _NAN3
    rng = random.Random(seed)
    boots = []
    for _ in range(B):
        v = pair_mean([rng.randrange(n) for _ in range(n)])
        if v is not None:
            boots.append(v)
    if not boots:
        return point, math.nan, math.nan
    lo, hi = _percentile_ci(boots, alpha)
    return point, lo, hi


def sign_test(diffs: list[float]) -> dict:
    """Exact two-sided sign test. Zeros are dropped; p = min(1, 2 * P(Binomial(n, 1/2) <= min(n_pos, n_neg)))."""
    n_pos = sum(1 for d in diffs if d > 0)
    n_neg = sum(1 for d in diffs if d < 0)
    n_zero = len(diffs) - n_pos - n_neg
    n = n_pos + n_neg
    if n == 0:
        p = 1.0
    else:
        k = min(n_pos, n_neg)
        p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"n_pos": n_pos, "n_neg": n_neg, "n_zero": n_zero, "p": p}


def permutation_rank(sim: list[list[float]], n_shuffles: int = 10000, seed: int = 0) -> dict:
    """Does each AI map resemble its OWN endorsed map more than the others'?

    ``sim[i][j]`` = similarity of AI map i to endorsed map j (square).  The rank of the own pair
    (1 = best) is taken per row; ties share their average rank.  Null: the own column is no
    better than any other, i.e. each row is shuffled.  Shuffling row i and reading position i
    is the same as reading a uniformly chosen column, so each shuffle draws one random column
    per row (same distribution, O(n) per shuffle instead of O(n^2)).
    p = (1 + #{null mean rank <= observed}) / (1 + n_shuffles): one-sided (low rank is the
    signal) and never exactly 0.  ``share_rank1`` counts rows where the own pair is strictly best."""
    n = len(sim)
    if any(len(row) != n for row in sim):
        raise ValueError("sim must be a square matrix")
    expected = (n + 1) / 2
    if n < 2:
        return {"mean_rank": math.nan, "expected_rank_null": expected, "share_rank1": math.nan,
                "p": math.nan, "n": n}
    ranks = []
    for row in sim:
        ranks.append([1 + sum(1 for v in row if v > x) + 0.5 * (sum(1 for v in row if v == x) - 1) for x in row])
    own = [ranks[i][i] for i in range(n)]
    observed = _mean(own)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_shuffles):
        if _mean([ranks[i][rng.randrange(n)] for i in range(n)]) <= observed + 1e-12:
            hits += 1
    return {"mean_rank": observed, "expected_rank_null": expected,
            "share_rank1": sum(1 for r in own if r == 1.0) / n,
            "p": (1 + hits) / (1 + n_shuffles), "n": n}


def cohen_kappa(a: list[str], b: list[str]) -> float:
    """Cohen's kappa of two label lists. nan when there are no items or when chance agreement is 1
    (both raters used one single label: kappa is undefined, not "perfect")."""
    if len(a) != len(b):
        raise ValueError(f"label lists differ in length ({len(a)} vs {len(b)})")
    n = len(a)
    if n == 0:
        return math.nan
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in ca) / (n * n)
    if pe >= 1.0:
        return math.nan
    return (po - pe) / (1 - pe)


def derangements(n: int, k: int, seed: int = 0) -> list[list[int]]:
    """``k`` seeded permutations of range(n) with no fixed point (rejection sampling; duplicates are
    possible when n is small). ``perm[i]`` is the partner of i."""
    if n < 2:
        raise ValueError("a derangement needs n >= 2")
    rng = random.Random(seed)
    out: list[list[int]] = []
    while len(out) < k:
        perm = list(range(n))
        rng.shuffle(perm)
        if all(perm[i] != i for i in range(n)):
            out.append(perm)
    return out
