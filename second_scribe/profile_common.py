"""profile_common: the leaf of the profile exporter's import graph (Ruling R19). It holds the constants and the small helpers
every profile_* module needs and imports no other profile module except ``profile_base``: ``K_CELL`` (the subset rule),
``EPS``, ``LABS``, ``SRC`` (the cell source map), ``_finite``, ``_mean``, ``_ci_defined``, ``part_status``, the ``_nr`` /
``_est`` cell makers and ``_hk`` (the kit headline's keyword arguments). Copied unchanged from ``profile_floors``, which
re-exports every public name for its callers.
"""
from __future__ import annotations

import math

from . import pipeline
from . import profile_base as pb

# Reason codes (the part before any ':') that are built at run time, so no literal in the source holds them; the strings
# test reads this tuple next to the codes it finds in the source. ``note = f"leak_split_indeterminate:{k}"`` (profile_floors).
DYNAMIC_CODES = ("leak_split_indeterminate",)
# codes the study's public profile carries (built by the authors, not by this kit) that the app must still be able to show
PUBLIC_PROFILE_CODES = ("not_recorded_public",)
K_CELL = 10                          # the subset rule: fewer than 10 maps left out (or kept) are not released
EPS = 1e-12
LABS = ("judge", "judge2", "keyword")           # the labellers, in the order a band lists its members
# Ruling R11: a lab export has no gate and no cleared files, so a cell's source is "kit:<analysis script name>"
# (a derived cell joins the names with "+").
SRC = {"f1": "kit:f1_floors_checks", "a5": "kit:a5_one_pull", "a7": "kit:a7_additions_source",
       "a9": "kit:a9_keyword_scope", "r12": "kit:r12_prior_size", "a1": "kit:a1_recall_matchers", "a10": "kit:a10_band"}

# ------------------------------------------------------------------ small helpers


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def _mean(xs) -> float:
    xs = list(xs)
    return math.fsum(xs) / len(xs) if xs else math.nan


def _ci_defined(lo, hi, used, B) -> bool:
    """A CI exists only if the bootstrap kept all B replicates and both bounds are finite (amendment 4, G1)."""
    return bool(used >= B and _finite(lo, hi))


def part_status(k_left: int, n: int) -> str:
    """The subset rule: "full" (k = 0), "partial" (10 <= k <= n - 10), else "suppressed"."""
    if k_left == 0:
        return "full"
    return "partial" if K_CELL <= k_left <= n - K_CELL else "suppressed"


def _nr(name, meta, code, *, needs=(), source=None) -> dict:
    """A ``not_run`` cell: ``meta`` = (x, unit, kind), ``code`` its ``reason_code`` (the sentence is in the strings file
    under ``profile.reason.<code>``). Cells with no ``needs`` code always carry a ``reason_code``."""
    x, unit, kind = meta
    return pb.not_run(name, list(needs), x=x, unit=unit, kind=kind, reason_code=code, source=source)


def _est(ctx, name, meta, boot, *, source, extra_note=None, missing=None, **kw) -> dict:
    """A cell from ``boot`` = (point, lo, hi, n_used): a ``not_run`` (reason_code ``not_computable``) when the point is not finite,
    the point without an interval and with note ``ci_undefined`` when the bootstrap kept fewer than B replicates.
    ``missing`` (a list of names, for example ["band"]) makes a computed cell a ``partial_not_run`` naming them."""
    pt, lo, hi, used = boot
    x, unit, kind = meta
    if not _finite(pt):
        return _nr(name, meta, "not_computable", source=source)
    make = (lambda *a, **k: pb.partial_not_run(*a, missing=missing, **k)) if missing else pb.cell
    if _ci_defined(lo, hi, used, ctx.B):
        return make(name, pt, x=x, unit=unit, kind=kind, ci=(lo, hi), source=source, note=extra_note, **kw)
    return make(name, pt, x=x, unit=unit, kind=kind, source=source, note="ci_undefined", **kw)


def _hk(ctx) -> dict:
    """pipeline.headline_kwargs of the run's config (the kit headline's seed, n_derangements, min_words)."""
    return pipeline.headline_kwargs(ctx.cfg)
