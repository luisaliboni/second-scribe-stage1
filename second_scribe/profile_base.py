"""profile_base: the foundation of the profile exporter (schema ``orgsci-profile/v2``).

Four things, none of which calls a model or opens a connection:

  * ``open_context``   a read-only view of one finished run: the run config, the kit's per-participant inputs
                       (``pipeline.build_pair_inputs``) and the analysed population, plus the bootstrap settings;
  * ``SETTINGS``       the 8 match settings of the profile, under the profile's own labels;
  * the statistics     the pooled ratio of sums and its percentile cluster bootstrap over maps, the permutation test
                       within maps, the Wilson and exact (Clopper-Pearson) intervals and Holm's step-down, ported
                       from the analysis pack's ``common.py`` (same formulas and the same random streams, so the
                       same numbers for the same inputs);
  * the constructors   ``cell``, ``not_run``, ``partial_not_run``, ``band`` and ``labeller_band``, which build the
                       schema's cell and band dicts with a fixed rounding (3 decimals), so two exports of the same run
                       give the same bytes;
  * the cell strings   (schema section 13) lab exports carry codes, never sentences: ``reason_code``, ``formula_code``,
                       ``missing`` and ``note`` codes whose English sentences live in ``profile_strings.json``
                       (``load_profile_strings``); ``lab_string_problems`` checks a whole profile against the limits.

Estimator: every cell is the pooled ratio of sums over maps with a percentile cluster bootstrap (clusters = maps),
B = 2000 and seed 20261017 by default; each call draws from a fresh ``random.Random(seed)``.
"""
from __future__ import annotations

import functools
import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

from . import nomodel, pipeline
from .match import MatchSetting
from .metrics import PairInputs

B = 2000                             # bootstrap replicates (percentile, clusters = maps)
SEED = 20261017                      # default seed; a fresh random.Random(seed) per call
N_PERM = 10000                       # permutation draws of permutation_within_map

# ------------------------------------------------------------------ the 8 match settings
# (profile label, lexical threshold, judge_accept). The labels are profile-v1's (the cells' ``x``): "primary" =
# lexical .6 + "same", "lenient" = lexical .4 + "same_or_close". The judge-only settings use a lexical threshold that
# no similarity can reach.
_SETTING_ROWS = (
    ("lexical_only_04", 0.4, "none"), ("lexical_only_05", 0.5, "none"),
    ("lexical_only_06", 0.6, "none"), ("lexical_only_07", 0.7, "none"),
    ("judge_only_same", 1.01, "same"), ("judge_only_same_or_close", 1.01, "same_or_close"),
    ("primary", 0.6, "same"), ("lenient", 0.4, "same_or_close"),
)
SETTINGS: dict[str, MatchSetting] = {label: MatchSetting(lex, acc) for label, lex, acc in _SETTING_ROWS}
JUDGE_USING = frozenset({"judge_only_same", "judge_only_same_or_close", "primary", "lenient"})   # the other four: lexical only


# ------------------------------------------------------------------ the run context
@dataclass(frozen=True)
class ProfileContext:
    cfg: pipeline.RunConfig
    out_dir: Path
    run: "pipeline._Run"                     # the kit's read-only run (no client, a log that writes nothing)
    p_list: list[PairInputs]                 # one per participant, in the kit's order (sorted pid)
    spec: str                                # the primary spec: the config's first spec key
    analysed: list[PairInputs]               # the participants that have the primary spec's AI map
    B: int
    seed: int
    exclusions: dict = field(default_factory=dict)    # build_pair_inputs: participants left out, by reason
    judging: dict = field(default_factory=dict)       # build_pair_inputs: judge-call counts per spec


def open_context(config_path: Path, out_dir: Path | None = None, B: int = B, seed: int = SEED) -> ProfileContext:
    """Open a finished run read-only. ``config_path`` is the run's RunConfig file (the config is not stored inside the
    out_dir); ``out_dir`` overrides the config's own ``out_dir``. Zero model calls: ``nomodel.open_run`` and
    ``pipeline.build_pair_inputs`` only read the work store, the endorsed maps and the transcripts."""
    overrides = {"out_dir": str(Path(out_dir).resolve())} if out_dir is not None else {}
    cfg = pipeline.RunConfig.load(config_path, **overrides)
    run = nomodel.open_run(cfg)
    p_list, exclusions, judging = pipeline.build_pair_inputs(run)
    spec = cfg.recon_specs()[0].key()
    analysed = [p for p in p_list if p.ai.get(spec) is not None]
    return ProfileContext(cfg=cfg, out_dir=Path(cfg.out_dir), run=run, p_list=p_list, spec=spec,
                          analysed=analysed, B=int(B), seed=int(seed), exclusions=exclusions, judging=judging)


# ------------------------------------------------------------------ statistics (ported from analysis/scripts/common.py)
def pooled_ratio(nums, dens) -> float:
    """Ratio of sums over maps: sum(nums) / sum(dens) (nan when the denominator is 0)."""
    d = math.fsum(dens)
    return math.fsum(nums) / d if d else math.nan


def _percentile(stats: list, alpha: float) -> tuple:
    """The kit's percentile rule (``stats._percentile_ci``): order statistics floor(alpha/2*B) and
    ceil((1-alpha/2)*B)-1 of the sorted replicates."""
    s = sorted(stats)
    b = len(s)
    lo = s[min(b - 1, int(alpha / 2 * b))]
    hi = s[max(0, min(b - 1, math.ceil((1 - alpha / 2) * b) - 1))]
    return lo, hi


def cluster_bootstrap_ratio(nums, dens, *, B: int = B, seed: int = SEED, alpha: float = 0.05) -> tuple:
    """(point, lo, hi, n_used): percentile bootstrap over MAPS (each map = one (num, den) pair, resampled with
    replacement) recomputing the pooled ratio of sums in every replicate. A replicate whose denominator is 0 is
    dropped (n_used counts the kept ones); none kept -> nan bounds."""
    nums, dens = list(nums), list(dens)
    if len(nums) != len(dens):
        raise ValueError("length mismatch")
    n = len(nums)
    point = pooled_ratio(nums, dens)
    if n == 0:
        return math.nan, math.nan, math.nan, 0
    rng = random.Random(seed)
    reps = []
    for _ in range(B):
        idx = rng.choices(range(n), k=n)
        d = math.fsum(dens[i] for i in idx)
        if d:
            reps.append(math.fsum(nums[i] for i in idx) / d)
    if not reps:
        return point, math.nan, math.nan, 0
    lo, hi = _percentile(reps, alpha)
    return point, lo, hi, len(reps)


def cluster_bootstrap(maps, statistic, *, B: int = B, seed: int = SEED, alpha: float = 0.05) -> tuple:
    """(point, lo, hi, n_used) for any statistic of a list of per-map units (resampled with replacement); a
    replicate whose statistic is nan is dropped."""
    maps = list(maps)
    point = statistic(maps)
    if not maps:
        return point, math.nan, math.nan, 0
    rng = random.Random(seed)
    reps = [v for _ in range(B) if not math.isnan(v := statistic(rng.choices(maps, k=len(maps))))]
    if not reps:
        return point, math.nan, math.nan, 0
    lo, hi = _percentile(reps, alpha)
    return point, lo, hi, len(reps)


def permutation_within_map(groups, statistic, *, n_perm: int = N_PERM, seed: int = SEED,
                           alternative: str = "two_sided") -> tuple:
    """(observed, p): label permutation WITHIN each map. ``groups`` is a list of (labels, outcomes) per map, two
    equal-length lists; ``statistic(groups)`` -> float. Each permutation shuffles every map's labels among that
    map's own units (outcomes fixed). p = (1 + #{T* at least as extreme}) / (1 + n_perm); alternative
    "two_sided" (|T*| >= |T|), "greater" (T* >= T) or "less" (T* <= T). A nan statistic never counts as extreme."""
    groups = [(list(a), list(b)) for a, b in groups]
    if any(len(a) != len(b) for a, b in groups):
        raise ValueError("length mismatch")
    obs = statistic(groups)
    if math.isnan(obs):
        return obs, math.nan
    rng = random.Random(seed)
    eps = 1e-12
    hits = 0
    for _ in range(n_perm):
        perm = []
        for labels, outcomes in groups:
            lab = labels[:]
            rng.shuffle(lab)
            perm.append((lab, outcomes))
        t = statistic(perm)
        if math.isnan(t):
            continue
        if alternative == "two_sided":
            hits += abs(t) >= abs(obs) - eps
        elif alternative == "greater":
            hits += t >= obs - eps
        elif alternative == "less":
            hits += t <= obs + eps
        else:
            raise ValueError("alternative")
    return obs, (1 + hits) / (1 + n_perm)


def wilson(k: int, n: int, level: float = 0.95) -> tuple:
    """Wilson score interval (lo, hi) for k successes in n trials (nan, nan when n == 0)."""
    if n == 0:
        return math.nan, math.nan
    if not 0 <= k <= n:
        raise ValueError("k out of range")
    z = NormalDist().inv_cdf(0.5 + level / 2)
    ph = k / n
    den = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple:
    """Exact (Clopper-Pearson) binomial interval (lo, hi), by bisection on the binomial tail (stdlib only).
    One-sided bound: level = 1 - 2*alpha, e.g. clopper_pearson(0, 90, 0.90)[1] = 1 - .05**(1/90) = .033."""
    if n == 0:
        return math.nan, math.nan
    if not 0 <= k <= n:
        raise ValueError("k out of range")
    a = (1 - level) / 2

    def cdf(x: int, q: float) -> float:
        return math.fsum(math.comb(n, i) * q ** i * (1 - q) ** (n - i) for i in range(x + 1))

    def bisect(pred) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if pred(mid) else (mid, hi)
        return (lo + hi) / 2
    lower = 0.0 if k == 0 else bisect(lambda q: 1 - cdf(k - 1, q) >= a)
    upper = 1.0 if k == n else bisect(lambda q: cdf(k, q) <= a)
    return lower, upper


def holm(pvals) -> list:
    """Holm step-down adjusted p-values, in the input order (monotone, capped at 1)."""
    p = list(pvals)
    m = len(p)
    order = sorted(range(m), key=lambda i: p[i])
    adj = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return adj


def r3(x) -> float:
    """A rate / share / CI bound rounded to 3 decimals (finite only)."""
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("non-finite")
    return round(x, 3)


# ------------------------------------------------------------------ cell and band constructors (schema section 3)
NEEDS = ("J2", "sidejudge", "ladder", "judge_script")            # the codes of a not_run cell's ``needs``
BAND_OVER = ("8_settings", "lenient", "labellers", "ci95", "variants")
P_LT = 0.001                                                      # the kit prints p to 3 decimals: 0.000 -> "p < .001"
MISSING_CODES = ("band", "n", "ci")                               # the codes of a partial_not_run cell's ``missing``
MAX_CODE = 40                                                     # a code (reason_code, formula_code) is at most 40 characters
MAX_STR = 40                                                      # any string in a lab-export cell
MAX_LONG = 200                                                    # ``source`` and any key ending ``_state``


def _num(x):
    """A finite number for the file: an int stays an int, a float is rounded to 3 decimals; anything else (bool,
    None, nan, inf, text) is refused."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("not a number")
    return x if isinstance(x, int) else r3(x)


def _put(d: dict, **kw) -> dict:
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def _code(value, what: str) -> str:
    """A code is a non-empty str of at most MAX_CODE characters (schema section 13)."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_CODE:
        raise ValueError(f"{what} must be a str of 1 to {MAX_CODE} characters")
    return value


def cell(name, value, *, x=None, n=None, unit=None, kind=None, ci=None, ci_level=None, p=None, p_holm=None,
         band=None, rests_on=None, qualifiers=None, derived=None, source=None, formula_code=None, note=None) -> dict:
    """A computed cell. ``value`` and the interval ``ci`` = (lo, hi) are rounded to 3 decimals; ``ci_level`` is 0.95
    when an interval is given and ``ci_level`` is not, else None (written as null). A ``p`` that the kit would print
    as 0.000 is written ``p_lt: 0.001`` and never ``p: 0``. ``p_holm`` is the Holm-adjusted p (3 decimals;
    one that prints as 0.000 is written ``p_holm_lt: 0.001``). ``source``, ``formula_code`` and ``note`` are copied when
    given. A derived cell carries ``formula_code`` (a str of at most 40 characters, whose text is in the strings file
    under ``profile.formula.<code>``), never the formula text (schema section 13)."""
    c = _put({"name": name}, x=x, n=n, unit=unit, kind=kind)
    c["value"] = _num(value)
    if ci is not None:
        lo, hi = ci
        c["ci_lo"], c["ci_hi"] = _num(lo), _num(hi)
        c["ci_level"] = 0.95 if ci_level is None else ci_level
    else:
        c["ci_level"] = ci_level
    if p is not None:
        pv = r3(p)
        if pv == 0.0:
            c["p_lt"] = P_LT
        else:
            c["p"] = pv
    if p_holm is not None:
        ph = r3(p_holm)
        if ph == 0.0:
            c["p_holm_lt"] = P_LT
        else:
            c["p_holm"] = ph
    if band is not None:
        c["band"] = band
    if rests_on is not None:
        c["rests_on"] = list(rests_on)
    if qualifiers is not None:
        c["qualifiers"] = list(qualifiers)
    if derived is not None:
        c["derived"] = derived
    if formula_code is not None:
        _code(formula_code, "formula_code")
    return _put(c, formula_code=formula_code, note=note, source=source)


def not_run(name, needs: list[str], *, x=None, unit=None, kind=None, reason_code=None, source=None) -> dict:
    """A cell that cannot be computed on this run: ``value`` None, ``state`` "not_run", ``needs`` (codes from NEEDS)
    and/or ``reason_code`` (a str of 1 to 40 characters; its sentence is in the strings file under
    ``profile.reason.<code before any ':'>``). It carries ``needs`` or ``reason_code`` or both, never neither, and never a
    free-sentence ``reason`` (schema section 13). ``source`` is the id of the analysis the cell comes from."""
    if not isinstance(needs, (list, tuple)) or any(code not in NEEDS for code in needs):
        raise ValueError(f"needs must be a list of {NEEDS}")
    if reason_code is not None:
        _code(reason_code, "reason_code")
    if not needs and reason_code is None:
        raise ValueError("a not_run cell must carry needs or a reason_code (every not_run cell has a reason)")
    c = _put({"name": name}, x=x, unit=unit, kind=kind)
    c.update(value=None, state="not_run", needs=list(needs))
    return _put(c, reason_code=reason_code, source=source)


def partial_not_run(name, value, *, missing: list[str], reason_code=None, **cell_kwargs) -> dict:
    """A cell whose value IS computed but whose band, denominator ``n`` or interval is missing: a ``cell`` with
    ``state`` "partial_not_run" and ``missing``, a non-empty list drawn only from MISSING_CODES ("band", "n", "ci"), for
    example ["band"] or ["n", "band"]. ``reason_code`` (optional) is a code as in ``not_run``."""
    if (not isinstance(missing, (list, tuple)) or not missing
            or any(not isinstance(m, str) or m not in MISSING_CODES for m in missing)):
        raise ValueError(f"missing must be a non-empty list of codes from {MISSING_CODES}")
    if reason_code is not None:
        _code(reason_code, "reason_code")
    c = cell(name, value, **cell_kwargs)
    c.update(state="partial_not_run", missing=list(missing))
    return _put(c, reason_code=reason_code)


def band(lo, hi, over, *, narrow=None, members=None, subbands=None) -> dict:
    """A band {lo, hi, over, narrow?, members?, subbands?}. ``members`` is [{"x", "value", "value_hi"?}, ...],
    ``subbands`` is [{"name", "lo", "hi"}, ...]; both are copied."""
    if over not in BAND_OVER:
        raise ValueError(f"band.over must be one of {BAND_OVER}")
    lo, hi = _num(lo), _num(hi)
    if lo > hi:
        raise ValueError("band lo > hi")
    b = {"lo": lo, "hi": hi, "over": over}
    if narrow is not None:
        b["narrow"] = narrow
    if members:
        b["members"] = [{k: (_num(v) if k.startswith("value") else v) for k, v in m.items()} for m in members]
    if subbands:
        b["subbands"] = [{**s, "lo": _num(s["lo"]), "hi": _num(s["hi"])} for s in subbands]
    return b


def labeller_band(members: list[dict], *, narrow=None) -> dict:
    """The band over labellers (``over: "labellers"``): the range of the members' values, one member
    {"x": labeller, "value": v} per labeller used, so a pool shows which labellers each lab had."""
    if not members:
        raise ValueError("a labeller band needs at least one member")
    names = [m.get("x") for m in members]
    if None in names or len(set(names)) != len(names):
        raise ValueError("every member needs its own labeller name")
    values = [_num(m.get("value")) for m in members]
    return band(min(values), max(values), "labellers", narrow=narrow, members=members)


# ------------------------------------------------------------------ cell strings in lab exports (schema section 13)
STRINGS_FILE = Path(__file__).resolve().parent / "profile_strings.json"
SENTENCE_KEYS = ("reason", "formula")          # free text, never in a lab-export cell (pack diagnostics only)


@functools.lru_cache(maxsize=1)
def _strings() -> tuple:
    d = json.loads(STRINGS_FILE.read_text(encoding="utf-8"))
    if not isinstance(d, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in d.items()):
        raise ValueError("profile_strings.json must be a flat object of strings")
    return tuple(d.items())


def load_profile_strings() -> dict[str, str]:
    """The strings file next to the package: a flat map from ``profile.reason.<code>``, ``profile.missing.<code>`` and
    ``profile.formula.<code>`` to an English sentence (a reason code counts by the part before any ':'; a formula code
    counts whole). Stdlib only; a fresh dict per call."""
    return dict(_strings())


# Final review A2: a message names fields and positions, never a key or value from the input.
FORBIDDEN_KEY_FRAGMENTS = ("interview", "participant", "role", "record_hash", "entry", "map_id", "pair_id")
_PLAIN_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
KIT_ROW_IDS = ("lead", "ledger", 1, 2, 3, 4, 5, 6, 7, 8, 9)


def key_label(k, hide=None) -> str:
    """A dict key as a path step in a message: the key only when it has the shape of a schema field name (plain lower
    case, at most 40 characters), holds no forbidden fragment and no forbidden token (``hide(k)``), else ``<key>``."""
    if (isinstance(k, str) and _PLAIN_KEY.fullmatch(k) and not any(f in k for f in FORBIDDEN_KEY_FRAGMENTS)
            and not (hide and hide(k))):
        return k
    return "<key>"


def row_label(row, i: int) -> str:
    """A row in a message: its id when it is one of the kit's row ids (lead, ledger, 1-9), else its position ``[i]``."""
    rid = row.get("row") if isinstance(row, dict) else None
    if not isinstance(rid, bool) and rid in KIT_ROW_IDS:
        return str(rid)
    return f"[{i}]"


def _walk_strings(value, path: str, limit: int, out: list, where: str) -> None:
    """Every string inside a cell: at most ``limit`` characters; ``source`` and keys ending ``_state`` get MAX_LONG; the
    keys ``reason`` and ``formula`` are refused wherever they appear."""
    if isinstance(value, str):
        if len(value) > limit:
            out.append(f"{where}: string at {path} is {len(value)} characters (limit {limit})")
    elif isinstance(value, dict):
        for k, v in value.items():
            kid = key_label(k)
            if k in SENTENCE_KEYS:
                out.append(f"{where}: key '{k}' at {path} is not allowed (lab exports carry codes, not sentences)")
                continue
            lim = MAX_LONG if isinstance(k, str) and (k == "source" or k.endswith("_state")) else MAX_STR
            if isinstance(k, str) and len(k) > MAX_STR:
                out.append(f"{where}: a key at {path} is {len(k)} characters (limit {MAX_STR})")
            _walk_strings(v, f"{path}.{kid}", lim, out, where)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _walk_strings(v, f"{path}[{i}]", limit, out, where)


def _is_codes(v) -> bool:
    return isinstance(v, (list, tuple)) and bool(v) and all(isinstance(c, str) for c in v)


def _cell_problems(c, where: str, strings: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(c, dict):
        return [f"{where}: the cell is not an object"]
    for k, v in c.items():
        if k in SENTENCE_KEYS:
            out.append(f"{where}: key '{k}' is not allowed (lab exports carry codes, not sentences)")
            continue
        lim = MAX_LONG if k == "source" or (isinstance(k, str) and k.endswith("_state")) else MAX_STR
        _walk_strings(v, key_label(k), lim, out, where)

    def entry(kind: str, code: str, what: str) -> None:
        if f"profile.{kind}.{code}" not in strings:
            out.append(f"{where}: {what} has no entry in the strings file (profile.{kind}.<code>)")

    rc = c.get("reason_code")
    if "reason_code" in c:
        if not isinstance(rc, str) or not 1 <= len(rc) <= MAX_CODE:
            out.append(f"{where}: reason_code must be a str of 1 to {MAX_CODE} characters")
        else:
            entry("reason", rc.split(":", 1)[0], "reason_code")
    state = c.get("state")
    if state == "not_run":
        needs = c.get("needs")
        if needs is not None and (not isinstance(needs, (list, tuple)) or any(n not in NEEDS for n in needs)):
            out.append(f"{where}: needs must be a list of {NEEDS}")
        has_needs = isinstance(needs, (list, tuple)) and bool(needs)
        if not has_needs and not (isinstance(rc, str) and rc):
            out.append(f"{where}: a not_run cell must carry needs or a reason_code")
    elif "needs" in c and c["needs"] and (not isinstance(c["needs"], (list, tuple)) or any(n not in NEEDS for n in c["needs"])):
        out.append(f"{where}: needs must be a list of {NEEDS}")
    if state == "partial_not_run" and not _is_codes(c.get("missing")):
        out.append(f"{where}: a partial_not_run cell must carry a non-empty missing list")
    if "missing" in c:
        m = c["missing"]
        if not _is_codes(m):
            out.append(f"{where}: missing must be a non-empty list of codes")
        else:
            for code in m:
                if code not in MISSING_CODES:
                    out.append(f"{where}: a missing code is not one of {MISSING_CODES}")
                else:
                    entry("missing", code, f"missing code '{code}'")
    if "formula_code" in c:
        fc = c["formula_code"]
        if not isinstance(fc, str) or not 1 <= len(fc) <= MAX_CODE:
            out.append(f"{where}: formula_code must be a str of 1 to {MAX_CODE} characters")
        else:
            entry("formula", fc, "formula_code")
    elif c.get("derived") is True:
        out.append(f"{where}: a derived cell (derived: true) must carry formula_code")
    if "note" in c:
        note = c["note"]
        if not isinstance(note, str) or not note:
            out.append(f"{where}: note must be a non-empty str")
        else:
            for part in note.split(";"):
                code = part.split(":", 1)[0]
                if not code:
                    out.append(f"{where}: note has an empty code")
                else:
                    entry("reason", code, "a note code")
    return out


def lab_string_problems(profile: dict) -> list[str]:
    """The violations of schema section 13 in a lab export's rows, one message per violation, each naming the row, the
    cell name and ``x`` and never echoing the offending value. ``profile`` is a dict with a ``rows`` list, each row with
    ``cells``. Checked: no key ``reason`` or ``formula`` anywhere in a row or cell; every string in a cell (lists, band,
    band members included) at most 40 characters, ``source`` and keys ending ``_state`` at most 200; a ``not_run`` cell
    carries ``needs`` or ``reason_code``; ``missing`` is a non-empty list of "band" / "n" / "ci"; a derived cell carries
    ``formula_code``; and every ``reason_code``, ``missing`` code, ``formula_code`` and ``note`` code has an entry in
    the strings file. Row-level strings are limited too (Task 6I): every string of a row outside its cells (``label_key``,
    ``population``, ``rests_on`` and ``qualifiers`` items, the band) is at most 40 characters, a ``source`` item 200. The
    top level and the ``run`` block are not checked (they hold hashes and kit commit ids). An empty list means the profile
    passes."""
    strings = load_profile_strings()
    out: list[str] = []
    for i, row in enumerate(profile.get("rows", [])):
        rid = row_label(row, i)
        if not isinstance(row, dict):
            out.append("a row is not an object")
            continue
        for k, v in row.items():
            if k == "cells":
                continue
            if k in SENTENCE_KEYS:
                out.append(f"row {rid}: key '{k}' is not allowed (lab exports carry codes, not sentences)")
                continue
            # row-level strings (label_key, population, rests_on, qualifiers, band, ...): 40; a source item: 200
            _walk_strings(v, key_label(k), MAX_LONG if k == "source" else MAX_STR, out, f"row {rid}")
        for j, c in enumerate(row.get("cells", [])):
            out.extend(_cell_problems(c, f"row {rid} cell[{j}]", strings))
    return out
