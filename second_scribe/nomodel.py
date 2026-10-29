"""No-model validation checks on a FINISHED judge (J) out_dir: ``cli validate-nomodel --config CFG --out DIR``.

Nothing here calls a model or opens a connection. The J out_dir is only read (its work store, its endorsed maps and
transcripts through the config, its log for the recorded hashes); every output goes to ``DIR`` (which must lie outside
the J out_dir): ``DIR/aggregates/<check>.json`` plus ``DIR/logs/run.jsonl``, a hash-chained log of this session (the
config and quote-rule hashes, the J log's head, the sha256 of each output) closed with a manifest.

Checks (``--checks``, comma list, default all of them):

  keyword     The no-AI arm on the real items. Every AI element the judge labelled gets the deterministic cue-word label
              (``keywords.keyword_modality`` on its own quote). Reports judge-vs-keyword agreement (raw share and
              Cohen's kappa, overall and per class as one-vs-rest, the 6x6 confusion counts) over all main specs, by
              spec, arm, element kind, quote tier, cue / no cue and quote eligibility; and the headline of every arm
              recomputed under the judge's labels and under the keyword labels with M's own code and parameters
              (``pipeline.arm_headlines``), side by side (``comparison``: the headline numbers under each, and the
              difference keyword minus judge).
  robustness  Agreement per interview (distribution over interviews: quartiles, minimum, share under kappa 0.6; and
              its rank correlation with the interview's unfound-quote and not-judged shares), per role (agreement and
              the headline numbers under each label source), and leave-one-interview-out of the headline numbers
              under both label sources (point estimates only: min / max over the left-out runs, the largest change,
              and how many runs change a sign or a claim flag). The scripts stay those of the full run.
  null        Wrong-transcript null of the run's AI quotes under the current quote rule (``textnorm``, the rule the
              kit records as ``quote_rule_sha256``): each quote of each ok main-spec map is located in its own
              transcript and in another participant's (the next one in sorted-id order; ``--null-shifts K`` adds
              shifts 2..K). Counts by tier (exact / approximate / unfound), by spec and arm; the share located in the
              wrong transcript against the 2% bar of the run-2 gate.
  diffs       What differs in the approximate quotes: the quote's words against the transcript's own words of the
              matched span (``span_parts``), as word-level edit operations (substitution, insertion = a word the quote
              adds, deletion = a transcript word the quote drops). Counted by type: substitutions with no guard word,
              changes that touch a number, a person word, a guard word, a word of the participant's endorsed labels,
              changes within two words of a guard word (negation-adjacent), stopword-only changes, same-stem
              substitutions. Never the words themselves.

Every output is counts, shares, kappas and point estimates; no label, quote, transcript text, participant id or
model reply. For a real corpus the aggregate-only guard (``report.assert_aggregate_only``, every map label and quote
of every participant as the forbidden set) and the pre-write leak guard (``audits.LeakGuard``) see the exact text
before it is written; the command prints counts only and, on an error, the error type and line number only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from . import metrics, pipeline, prereg, report, textnorm
from .keywords import keyword_modality
from .kit_vocab import KIT_KEYS, KIT_VALUES
from .provenance import eligible
from .schemas import MODALITIES

CHECKS = ("keyword", "robustness", "null", "diffs")
KAPPA_FLOOR = 0.6                  # the kit's second-judge audit warns below this kappa (audits.py)
NULL_BAR = 0.02                    # run-2 gate (a): at most 2% of the quotes located in a wrong transcript
LOO_DEFAULTS = {"B": 50, "n_perm": 50, "n_perm_script": 50}     # point estimates only: CIs / p-values not reported

# the words this module writes as dict keys (exempt from the label check by exact match, as ``kit_vocab``)
NOMODEL_KEYS: frozenset[str] = frozenset((
    "agreement", "all", "approximate", "arm", "by_arm", "by_cue", "by_eligible", "by_kind", "by_role", "by_shift",
    "by_spec", "by_tier", "changes", "check", "checks", "classes", "cohen_kappa", "comparison", "confusion", "cue",
    "deletion", "diff", "edge", "eligible", "exact", "full", "headline_judge", "headline_keyword", "insertion",
    "interviews", "judge", "judge_given_keyword", "keyword", "keyword_given_judge", "kinds", "loo", "max",
    "max_abs_change", "median", "min", "n", "n_agree", "n_both", "n_changes", "n_flag_change", "n_judge",
    "n_keyword", "n_undefined", "n_loo", "n_failed", "n_missing", "n_participants", "n_quotes", "n_sign_change",
    "n_words", "no_cue", "node", "not_eligible", "null", "null_bar", "null_located", "null_located_share",
    "null_ok", "other_arms_judge", "other_arms_keyword", "own", "own_located_share", "parameters", "per_interview",
    "per_role", "primary_spec", "q1", "q3", "quote_rule_matches_run", "quote_rule_sha256", "quote_rule_version",
    "removed", "robustness", "run_quote_rule_sha256", "shares", "share_kappa_below_floor", "kappa_floor",
    "spearman_kappa_unfound_share", "spearman_kappa_not_judged_share", "spec", "specs", "substitution",
    "unfound", "values", "words", "words_per_quote", "n_quotes_with", "ops", "shift", "n_shifts",
    "n_quotes_used", "keyword_minus_judge", "headline", "values_kappa", "values_agreement", "all_specs",
    "n_interviews", "own_unfound_reasons", "below_threshold", "span", "guard", "empty_quote", "no_markers",
    "unknown", "n_quotes_all_minor", "not_eligible", "0", *MODALITIES,
    # diff-audit flags
    "guard_word_free_substitution", "number", "person_word", "guard_word", "endorsed_label_word",
    "negation_adjacent", "stopword_only", "same_stem", "contraction", "1", "2", "3", "4", "5+",
))


# ------------------------------------------------------------------ opening a finished out_dir read-only

class _ReadOnlyLog:
    """Stands in for the J run's audit log: ``path`` points at it (the script-source families are read from it),
    ``append`` writes nothing. Nothing of this session reaches the J log."""

    def __init__(self, path: Path):
        self.path = str(path)

    def append(self, record_type, payload):
        return None


def open_run(cfg: pipeline.RunConfig) -> "pipeline._Run":
    """A ``pipeline._Run`` over ``cfg.out_dir`` that only reads: no client, a log that writes nothing."""
    log = Path(cfg.out_dir) / "logs" / "run.jsonl"
    r = pipeline._Run(cfg, {}, _ReadOnlyLog(log))
    r.last_aggregates = None
    return r


def outside(out: Path, run_dir: Path) -> bool:
    out, run_dir = out.resolve(), run_dir.resolve()
    return out != run_dir and run_dir not in out.parents


# ------------------------------------------------------------------ small statistics

def _num(x):
    return None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x


def _share(a: int, b: int):
    return a / b if b else None


def _quantile(xs: list[float], q: float):
    """Linear-interpolation quantile of the sorted values (None when empty)."""
    if not xs:
        return None
    ys = sorted(xs)
    pos = q * (len(ys) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ys) - 1)
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs: list[float], ys: list[float]):
    """Spearman rank correlation (average ranks for ties); None with fewer than 3 pairs or a constant side."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None and x == x and y == y]
    if len(pairs) < 3:
        return None
    rx, ry = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def agreement(rows: list[dict]) -> dict:
    """Judge vs keyword over ``rows`` (each with "judge" and "keyword" labels): n, raw agreement, Cohen's kappa, per
    class (one-vs-rest: counts, raw agreement, kappa, the share of the judge's class the keyword gives too and the
    reverse) and the confusion counts {judge label: {keyword label: n}} (non-zero cells only)."""
    a = [x["judge"] for x in rows]
    b = [x["keyword"] for x in rows]
    n = len(rows)
    n_agree = sum(1 for x, y in zip(a, b) if x == y)
    classes = {}
    for c in MODALITIES:
        ja, kb = [x == c for x in a], [y == c for y in b]
        n_j, n_k = sum(ja), sum(kb)
        both = sum(1 for x, y in zip(ja, kb) if x and y)
        classes[c] = {"n_judge": n_j, "n_keyword": n_k, "n_both": both,
                      "agreement": _share(sum(1 for x, y in zip(ja, kb) if x == y), n),
                      "cohen_kappa": _num(metrics.cohen_kappa([str(x) for x in ja], [str(y) for y in kb])),
                      "keyword_given_judge": _share(both, n_j), "judge_given_keyword": _share(both, n_k)}
    conf: dict[str, dict[str, int]] = {}
    for x, y in zip(a, b):
        conf.setdefault(x, {}).setdefault(y, 0)
        conf[x][y] += 1
    return {"n": n, "n_agree": n_agree, "agreement": _share(n_agree, n),
            "cohen_kappa": _num(metrics.cohen_kappa(a, b)), "classes": classes,
            "confusion": {k: dict(sorted(v.items())) for k, v in sorted(conf.items())}}


def _grouped(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for x in rows:
        out.setdefault(str(x[key]), []).append(x)
    return {k: agreement(v) for k, v in sorted(out.items())}


# ------------------------------------------------------------------ the element rows (judge and keyword labels)

def element_rows(p_list: list[metrics.PairInputs], specs: list[str], min_words: int) -> list[dict]:
    """One row per AI element the judge answered for, in every main spec: participant index (position in
    ``p_list``, never the id), role, spec, arm, kind, quote tier, eligible in its arm, cue (the keyword classifier
    found a cue word: its label is neither actual nor unsupported), and the two labels."""
    rows = []
    for i, p in enumerate(p_list):
        kw = metrics.with_keyword_modality(p).modality
        for s in specs:
            g = p.ai.get(s)
            if g is None or s not in p.modality:
                continue
            arm = metrics.spec_arm(s)
            skip = metrics._nj(p, s)
            ids = [(n.node_id, "node") for n in g.nodes] + [(f"e:{e.source}->{e.target}", "edge") for e in g.edges]
            for eid, kind in ids:
                if eid in skip or eid not in p.modality[s]:
                    continue
                pv = p.prov.get(s, {}).get(eid)
                k = kw[s][eid]
                rows.append({"i": i, "role": p.role, "spec": s, "arm": arm, "kind": kind,
                             "tier": pv.tier if pv else "unfound",
                             "eligible": bool(pv and eligible(pv, arm, min_words)[0]),
                             "cue": k not in ("actual", "unsupported"),
                             "judge": p.modality[s][eid], "keyword": k})
    return rows


def agreement_block(rows: list[dict]) -> dict:
    return {"all": agreement(rows), "by_spec": _grouped(rows, "spec"), "by_arm": _grouped(rows, "arm"),
            "by_kind": _grouped(rows, "kind"), "by_tier": _grouped(rows, "tier"),
            "by_cue": {"cue": agreement([x for x in rows if x["cue"]]),
                       "no_cue": agreement([x for x in rows if not x["cue"]])},
            "by_eligible": {"eligible": agreement([x for x in rows if x["eligible"]]),
                            "not_eligible": agreement([x for x in rows if not x["eligible"]])}}


# ------------------------------------------------------------------ headline numbers

# (name, path) of the point estimates and claim flags read from a headline; a path that is absent reads None
HEADLINE_SCALARS = (
    ("base_rate_mean", "absences.base_rate.mean"),
    ("base_rate_ai_wished_share", "absences.base_rate.ai_wished_share"),
    ("base_rate_endorsed_wished_share", "absences.base_rate.endorsed_wished_share"),
    ("base_rate_ai_below_endorsed", "absences.base_rate.ai_below_endorsed"),
    ("leak_rate", "absences.pooled.leak_rate"),
    ("recovery_rate", "absences.pooled.recovery_rate"),
    ("flip_rate", "absences.pooled.flip_rate"),
    ("counterfactual_leakage_rate", "absences.pooled.counterfactual_leakage_rate"),
    ("adequacy_flattening_rate", "absences.pooled.adequacy_flattening_rate"),
    ("leaked_unanchored_rate", "absences.pooled.leaked_unanchored_rate"),
    ("paired_diff_mean", "absences.paired_diff.mean"),
    ("claim_script_conditional", "absences.script_conditional.claim_script_conditional_leakage"),
    ("contradiction_wished_as_exp", "absences.contradiction_rate.quote_wished_marked_experienced.pooled_share"),
    ("contradiction_actual_as_wished", "absences.contradiction_rate.quote_actual_marked_wished.pooled_share"),
    ("pull_mean", "pull_toward_script.mean"),
    ("claim_pull", "pull_toward_script.claim_pulled_toward_script"),
    ("pull_in_transcript_only_mean", "pull_toward_script.in_transcript_only.mean"),
    ("pull_raw_labels_mean", "pull_toward_script.robustness.raw_labels.mean"),
    ("pull_judge_only_mean", "pull_toward_script.robustness.judge_only.mean"),
    ("homogenization_concept_diff", "homogenization.concept.diff"),
    ("homogenization_structure_diff", "homogenization.structure.diff"),
    ("leakage_leak_rate_diff", "leakage.leak_rate_diff.mean"),
    ("leakage_node_recall_diff", "leakage.node_recall_diff.mean"),
    ("contaminated_share", "leakage.contaminated_share.pooled_share"),
    ("shuffled_quotes_mean", "controls.shuffled_quotes.mean"),
    ("shuffled_pairs_mean", "controls.shuffled_pairs.mean"),
    ("permutation_mean_rank", "controls.permutation_rank.mean_rank"),
    ("prior_only_paired_diff", "prior_only_baseline.paired_diff.mean"),
)


def _get(d, path: str):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d if isinstance(d, (int, float, bool)) or d is None else None


def headline_scalars(h: dict) -> dict:
    """The headline's point estimates and claim flags by name (``HEADLINE_SCALARS``), the script-conditional
    estimate of each script source (``sc:<source>``) and the mean difference AI minus endorsed of each linearization
    measure on glued edges (``lin:<measure>``). Keys stay under the guard's 40 characters."""
    out = {name: _get(h, path) for name, path in HEADLINE_SCALARS}
    rows = ((h.get("absences") or {}).get("script_conditional") or {}).get("rows") or {}
    for src in sorted(rows):
        out[f"sc:{src}"] = _get(rows[src], "estimate")
    measures = ((h.get("linearization") or {}).get("glued") or {}).get("measures") or {}
    for m in sorted(measures):
        out[f"lin:{m}"] = _get(measures[m], "mean_diff")
    return out


def compare_scalars(a: dict, b: dict) -> dict:
    """{name: {judge, keyword, keyword_minus_judge}} for the names of either side."""
    out = {}
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        num = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y))
        out[k] = {"judge": x, "keyword": y, "keyword_minus_judge": (y - x) if num else None}
    return out


# ------------------------------------------------------------------ the shared context

class Context:
    """The finished run, read once: participants' inputs, the judging summary, M's headline parameters and the script
    sources as M builds them (from the full participant list)."""

    def __init__(self, cfg: pipeline.RunConfig):
        self.cfg = cfg
        self.r = open_run(cfg)
        self.p_list, self.ex, self.judging = pipeline.build_pair_inputs(self.r)
        self.keys = [s.key() for s in self.r.specs]
        self.hk = pipeline.headline_kwargs(cfg)
        self.min_words = self.hk["min_words"]
        self.sources = None
        if self.p_list:
            sources, _desc = pipeline.script_source_list(self.r)
            own = metrics.ScriptSource("own", "own", metrics.scripts_by_role(self.p_list))
            if sources is not None:
                sources.append(own)
            self.sources = sources
        self.incomplete = pipeline._outage_counts(self.r)["timeout_excluded_items"] > 0
        self._rows = None

    @property
    def rows(self) -> list[dict]:
        if self._rows is None:
            self._rows = element_rows(self.p_list, self.keys, self.min_words)
        return self._rows

    def headlines(self, p_list, source: str, *, other_arms: bool = True, **override) -> tuple[dict, dict]:
        hk = {**self.hk, **override}
        return pipeline.arm_headlines(p_list, self.keys, hk, self.sources, self.judging, self.incomplete,
                                      modality_source=source, other_arms=other_arms)


# ------------------------------------------------------------------ check: keyword

def check_keyword(ctx: Context) -> dict:
    out = {"n_participants": len(ctx.p_list), "specs": ctx.keys, "primary_spec": ctx.keys[0] if ctx.keys else None,
           "agreement": agreement_block(ctx.rows)}
    if not ctx.p_list:
        return out
    hj, oj = ctx.headlines(ctx.p_list, "judge")
    hkw, okw = ctx.headlines(ctx.p_list, "keyword")
    out["headline_judge"], out["headline_keyword"] = hj, hkw
    out["other_arms_judge"], out["other_arms_keyword"] = oj, okw
    out["comparison"] = {"headline": compare_scalars(headline_scalars(hj), headline_scalars(hkw)),
                         **{arm: compare_scalars(headline_scalars(oj[arm]), headline_scalars(okw.get(arm) or {}))
                            for arm in oj}}
    return out


# ------------------------------------------------------------------ check: robustness

def distribution(values: list, floor: float | None = KAPPA_FLOOR) -> dict:
    """Quartiles, extremes and the sorted values (rounded to 4 places, anonymous: no id, no order of the corpus) of
    per-interview numbers; None values (an undefined kappa) are counted, not used."""
    xs = sorted(v for v in values if v is not None)
    out = {"n": len(values), "n_undefined": len(values) - len(xs),
           "min": xs[0] if xs else None, "q1": _quantile(xs, 0.25), "median": _quantile(xs, 0.5),
           "q3": _quantile(xs, 0.75), "max": xs[-1] if xs else None, "values": [round(x, 4) for x in xs]}
    if floor is not None:
        out["kappa_floor"] = floor
        out["share_kappa_below_floor"] = _share(sum(1 for x in xs if x < floor), len(xs))
    return out


def _interview_shares(p: metrics.PairInputs, specs: list[str]) -> tuple:
    """(unfound share of the interview's quoted AI elements, not-judged share of its AI elements), over ``specs``."""
    quoted = unfound = elems = nj = 0
    for s in specs:
        g = p.ai.get(s)
        if g is None:
            continue
        for x in [*g.nodes, *g.edges]:
            eid = x.node_id if hasattr(x, "node_id") else f"e:{x.source}->{x.target}"
            elems += 1
            nj += eid in metrics._nj(p, s)
            if x.quote and x.quote.strip():
                quoted += 1
                pv = p.prov.get(s, {}).get(eid)
                unfound += pv is None or pv.tier == "unfound"
    return _share(unfound, quoted), _share(nj, elems)


def per_interview(ctx: Context, rows: list[dict], specs: list[str]) -> dict:
    by_i: dict[int, list[dict]] = {}
    for x in rows:
        if x["spec"] in specs:
            by_i.setdefault(x["i"], []).append(x)
    idx = sorted(by_i)
    ag = {i: agreement(by_i[i]) for i in idx}
    kappas = [ag[i]["cohen_kappa"] for i in idx]
    shares = [_interview_shares(ctx.p_list[i], specs) for i in idx]
    roles: dict[str, list[int]] = {}
    for i in idx:
        roles.setdefault(ctx.p_list[i].role, []).append(i)
    return {"n_interviews": len(idx),
            "cohen_kappa": distribution(kappas),
            "agreement": distribution([ag[i]["agreement"] for i in idx], floor=None),
            "by_role": {role: {"cohen_kappa": distribution([ag[i]["cohen_kappa"] for i in ii]),
                               "agreement": distribution([ag[i]["agreement"] for i in ii], floor=None)}
                        for role, ii in sorted(roles.items())},
            "spearman_kappa_unfound_share": spearman(kappas, [s[0] for s in shares]),
            "spearman_kappa_not_judged_share": spearman(kappas, [s[1] for s in shares]),
            "shares": {"unfound": distribution([s[0] for s in shares], floor=None),
                       "not_judged": distribution([s[1] for s in shares], floor=None)}}


def _scalar_runs(ctx: Context, subsets: list[list[metrics.PairInputs]], source: str) -> list[dict | None]:
    """Headline scalars (primary arm) of each participant subset at the leave-one-out parameters; None where the
    headline could not be computed (counted by the caller)."""
    out = []
    for sub in subsets:
        try:
            h, _ = ctx.headlines(sub, source, other_arms=False, **ctx.loo)
            out.append(headline_scalars(h))
        except (ValueError, ZeroDivisionError):     # e.g. too few participants left for a derangement
            out.append(None)
    return out


def _sign(x) -> int:
    return (x > 0) - (x < 0)


def loo_summary(reference: dict, runs: list[dict | None]) -> dict:
    """Per headline number: the value on all interviews (``all``, same parameters), min / max over the left-out runs,
    the largest absolute change, and how many runs change its sign (numbers) or its value (claim flags)."""
    ok = [r for r in runs if r is not None]
    out = {}
    for k in sorted(reference):
        ref = reference[k]
        vals = [r.get(k) for r in ok]
        if isinstance(ref, bool) or any(isinstance(v, bool) for v in vals):
            out[k] = {"all": ref, "n_flag_change": sum(1 for v in vals if v != ref),
                      "n_missing": sum(1 for v in vals if v is None)}
            continue
        nums = [v for v in vals if isinstance(v, (int, float))]
        blk = {"all": ref, "min": min(nums) if nums else None, "max": max(nums) if nums else None,
               "n_missing": len(vals) - len(nums)}
        if isinstance(ref, (int, float)):
            blk["max_abs_change"] = max((abs(v - ref) for v in nums), default=None)
            blk["n_sign_change"] = sum(1 for v in nums if _sign(v) != _sign(ref))
        out[k] = blk
    return {"n_loo": len(runs), "n_failed": len(runs) - len(ok), "values": out}


def check_robustness(ctx: Context) -> dict:
    rows = ctx.rows
    primary = ctx.keys[0] if ctx.keys else None
    out = {"n_participants": len(ctx.p_list), "primary_spec": primary, "parameters": dict(ctx.loo),
           "per_interview": {"all_specs": per_interview(ctx, rows, ctx.keys),
                             "primary_spec": per_interview(ctx, rows, [primary] if primary else [])}}
    if not ctx.p_list:
        return out
    roles = sorted({p.role for p in ctx.p_list})
    per_role = {}
    for role in roles:
        sub = [p for p in ctx.p_list if p.role == role]
        idx = {i for i, p in enumerate(ctx.p_list) if p.role == role}
        j, k = (_scalar_runs(ctx, [sub], s)[0] for s in ("judge", "keyword"))
        per_role[role] = {"n_participants": len(sub), "agreement": agreement([x for x in rows if x["i"] in idx]),
                          "headline": compare_scalars(j or {}, k or {}),
                          "n_failed": int(j is None) + int(k is None)}
    out["per_role"] = per_role
    subsets = [ctx.p_list[:i] + ctx.p_list[i + 1:] for i in range(len(ctx.p_list))]
    loo = {}
    for source in ("judge", "keyword"):
        ref = _scalar_runs(ctx, [ctx.p_list], source)[0] or {}
        loo[source] = loo_summary(ref, _scalar_runs(ctx, subsets, source))
    kap = [agreement([x for x in rows if x["i"] != i])["cohen_kappa"] for i in range(len(ctx.p_list))]
    allk = agreement(rows)["cohen_kappa"]
    loo["agreement"] = {"all": allk, "min": min((v for v in kap if v is not None), default=None),
                        "max": max((v for v in kap if v is not None), default=None),
                        "max_abs_change": max((abs(v - allk) for v in kap if v is not None and allk is not None),
                                              default=None)}
    out["loo"] = loo
    return out


# ------------------------------------------------------------------ the run's quotes, located once

TIERS = ("exact", "approximate", "unfound")


def run_quote_rule(log_path: Path) -> list[str]:
    """The distinct ``quote_rule_sha256`` values the run's ``run_start`` records carry (sorted). Reads only that
    field of those records."""
    seen: set[str] = set()
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if '"run_start"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("record_type") == "run_start":
                    h = (rec.get("payload") or {}).get("quote_rule_sha256")
                    if isinstance(h, str):
                        seen.add(h)
    except OSError:
        return []
    return sorted(seen)


def quote_units(ctx: Context) -> list[dict]:
    """The quotes of every ok main-spec AI map, per participant in sorted-id order (participants whose transcript has
    no confirmed mapping-talk item are left out): [{"j": position, "person", "units" (full transcript turns),
    "transcript", "maps": [(spec, arm, [(element id, kind, quote)])]}]. Cached on ``ctx``."""
    if getattr(ctx, "_quote_units", None) is not None:
        return ctx._quote_units
    out = []
    for p in sorted(ctx.r.people, key=lambda x: x.pid):
        t = ctx.r.transcript(p)
        if t is None:
            continue
        maps = []
        for s in ctx.r.specs:
            g = ctx.r.ai_map(p.pid, s.key())
            if g is None:
                continue
            qs = [(n.node_id, "node", n.quote) for n in g.nodes if n.quote and n.quote.strip()]
            qs += [(f"e:{e.source}->{e.target}", "edge", e.quote) for e in g.edges if e.quote and e.quote.strip()]
            maps.append((s.key(), metrics.spec_arm(s.key()), qs))
        try:
            stems = {x for n in ctx.r.endorsed(p).nodes for x in textnorm.content_tokens(n.text)}
        except Exception:  # noqa: BLE001 - an unreadable map: no label words to compare (the guard still scans)
            stems = set()
        out.append({"j": len(out), "person": p, "transcript": t, "units": t.units("full"), "maps": maps,
                    "label_stems": stems})
    ctx._quote_units = out
    return out


def own_tier(row: dict, q: str) -> tuple[str, dict]:
    """``locate_quote_in_turns`` of quote ``q`` in the row's own transcript, cached on the row."""
    cache = row.setdefault("_own", {})
    if q not in cache:
        cache[q] = textnorm.locate_quote_in_turns(row["units"], q)
    return cache[q]


def _tier_counts() -> dict:
    return {t: 0 for t in TIERS}


def _null_block(own: Counter, null: Counter) -> dict:
    n_own, n_null = sum(own.values()), sum(null.values())
    located = null["exact"] + null["approximate"]
    return {"n_quotes": n_own, "own": {**_tier_counts(), **own},
            "own_located_share": _share(own["exact"] + own["approximate"], n_own),
            "null": {**_tier_counts(), **null}, "null_located": located,
            "null_located_share": _share(located, n_null)}


# ------------------------------------------------------------------ check: null

def check_null(ctx: Context) -> dict:
    """Wrong-transcript null under the current quote rule (see the module docstring)."""
    from .textnorm import locate_quote_in_turns
    people = quote_units(ctx)
    n = len(people)
    shifts = [k for k in range(1, max(1, ctx.null_shifts) + 1) if n > 1 and k % n]
    recorded = run_quote_rule(Path(ctx.cfg.out_dir) / "logs" / "run.jsonl")
    current = textnorm.quote_rule_sha256()
    own_all, own_spec, own_arm, own_kind = Counter(), {}, {}, {}
    reasons: Counter = Counter()
    null_all = {k: Counter() for k in shifts}
    null_spec: dict = {k: {} for k in shifts}
    null_arm: dict = {k: {} for k in shifts}
    null_kind: dict = {k: {} for k in shifts}
    per_person_null = []
    for row in people:
        pn = Counter()
        for spec, arm, qs in row["maps"]:
            for _eid, kind, q in qs:
                tier, info = own_tier(row, q)
                own_all[tier] += 1
                own_spec.setdefault(spec, Counter())[tier] += 1
                own_arm.setdefault(arm, Counter())[tier] += 1
                own_kind.setdefault(kind, Counter())[tier] += 1
                if tier == "unfound":
                    reasons[info.get("reason", "unknown")] += 1
                for k in shifts:
                    other = people[(row["j"] + k) % n]
                    nt, _ = locate_quote_in_turns(other["units"], q)
                    null_all[k][nt] += 1
                    null_spec[k].setdefault(spec, Counter())[nt] += 1
                    null_arm[k].setdefault(arm, Counter())[nt] += 1
                    null_kind[k].setdefault(kind, Counter())[nt] += 1
                    pn[nt] += 1
        tot = sum(pn.values())
        per_person_null.append(_share(pn["exact"] + pn["approximate"], tot))
    pooled_null = sum(null_all.values(), Counter())
    head = _null_block(own_all, pooled_null)
    head["null_ok"] = head["null_located_share"] is not None and head["null_located_share"] <= NULL_BAR
    return {"n_participants": n, "n_shifts": len(shifts), "null_bar": NULL_BAR,
            "quote_rule_version": textnorm.QUOTE_RULE_VERSION, "quote_rule_sha256": current,
            "run_quote_rule_sha256": recorded, "quote_rule_matches_run": bool(recorded) and recorded == [current],
            "all": head, "own_unfound_reasons": dict(sorted(reasons.items())),
            "by_spec": {s: _null_block(own_spec[s], sum((null_spec[k].get(s, Counter()) for k in shifts), Counter()))
                        for s in sorted(own_spec)},
            "by_arm": {a: _null_block(own_arm[a], sum((null_arm[k].get(a, Counter()) for k in shifts), Counter()))
                       for a in sorted(own_arm)},
            "by_kind": {x: _null_block(own_kind[x], sum((null_kind[k].get(x, Counter()) for k in shifts), Counter()))
                        for x in sorted(own_kind)},
            "by_shift": {str(k): {"null": {**_tier_counts(), **null_all[k]},
                                  "null_located_share": _share(null_all[k]["exact"] + null_all[k]["approximate"],
                                                               sum(null_all[k].values()))} for k in shifts},
            "per_interview": {"null_located_share": distribution(per_person_null, floor=None)}}


# ------------------------------------------------------------------ check: diffs (approximate quotes)

NUMBER_WORDS = frozenset("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                         "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty "
                         "ninety hundred thousand million first second third fourth fifth sixth seventh eighth ninth "
                         "tenth once twice half dozen couple".split())
PERSON_WORDS = frozenset("i me my mine myself we us our ours ourselves he him his himself she her hers herself they "
                         "them their theirs themselves you your yours yourself son sons daughter daughters child "
                         "children kid kids boy boys girl girls baby husband wife mom mum mommy mother dad daddy father "
                         "brother brothers sister sisters parent parents family grandma grandmother grandpa grandfather "
                         "partner someone somebody everyone everybody".split())
DIFF_TYPES = ("substitution", "insertion", "deletion")
DIFF_FLAGS = ("guard_word_free_substitution", "guard_word", "negation_adjacent", "number", "person_word",
              "endorsed_label_word", "stopword_only", "same_stem", "contraction")
_OP = {"replace": "substitution", "insert": "insertion", "delete": "deletion"}


def diff_ops(quote: str, span_parts: list[list[str]], label_stems: set[str]) -> list[dict]:
    """Word-level edit operations from the transcript's matched span (``span_parts``, one token list per "..." part) to
    the quote's own tokens: substitution, insertion (the quote adds words) or deletion (the quote drops transcript
    words), each with its size in words and its flags (``DIFF_FLAGS``). Never returns a word."""
    parts = [textnorm.tokens(x) for x in textnorm._ELLIPSIS.split(textnorm.normalize(quote))]
    parts = [x for x in parts if x]
    ops = []
    for qp, sp in zip(parts, span_parts):
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, sp, qp, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            a, b = sp[i1:i2], qp[j1:j2]
            changed = a + b
            typ = _OP[tag]
            guard = any(textnorm._is_guard(t) for t in changed)
            near = sp[max(0, i1 - 2):i2 + 2] + qp[max(0, j1 - 2):j2 + 2]
            flags = {
                "guard_word": guard,
                "guard_word_free_substitution": typ == "substitution" and not guard,
                "negation_adjacent": any(textnorm._is_guard(t) for t in near),
                "number": any(t in NUMBER_WORDS or any(c.isdigit() for c in t) for t in changed),
                "person_word": any(t in PERSON_WORDS for t in changed),
                "endorsed_label_word": any(t not in textnorm.STOPWORDS and textnorm.stem(t) in label_stems
                                           for t in changed),
                "stopword_only": all(t in textnorm.STOPWORDS for t in changed),
                "same_stem": typ == "substitution" and len(a) == len(b) and all(
                    textnorm.stem(x) == textnorm.stem(y) or x.replace("'", "") == y.replace("'", "")
                    for x, y in zip(a, b)),
                "contraction": any("'" in t for t in changed),
            }
            ops.append({"type": typ, "n_words": max(len(a), len(b)), "flags": flags})
    return ops


def _minor(op: dict) -> bool:
    f = op["flags"]
    return f["stopword_only"] or f["same_stem"] or (f["contraction"] and not f["guard_word"])


def diff_block(quotes: list[list[dict]]) -> dict:
    """Counts over approximate quotes, each given as its ``diff_ops`` list."""
    ops = {t: {"n_changes": 0, "n_words": 0} for t in DIFF_TYPES}
    flags = {f: {"n_changes": 0, "n_quotes_with": 0} for f in DIFF_FLAGS}
    hist = {"0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5+": 0}
    minor = 0
    for q in quotes:
        words = 0
        for op in q:
            ops[op["type"]]["n_changes"] += 1
            ops[op["type"]]["n_words"] += op["n_words"]
            words += op["n_words"]
            for f in DIFF_FLAGS:
                flags[f]["n_changes"] += op["flags"][f]
        for f in DIFF_FLAGS:
            flags[f]["n_quotes_with"] += any(op["flags"][f] for op in q)
        hist[str(words) if words < 5 else "5+"] += 1
        minor += bool(q) and all(_minor(op) for op in q)
    return {"n_quotes": len(quotes), "ops": ops, "flags": flags, "words_per_quote": hist,
            "n_quotes_all_minor": minor}


def check_diffs(ctx: Context) -> dict:
    """What differs in the approximate quotes (see the module docstring). Counts only."""
    from .provenance import locate_quote
    allq: list = []
    by: dict[str, dict[str, list]] = {"by_arm": {}, "by_spec": {}, "by_kind": {}, "by_eligible": {}}
    for row in quote_units(ctx):
        for spec, arm, qs in row["maps"]:
            for eid, kind, q in qs:
                tier, info = own_tier(row, q)
                if tier != "approximate":
                    continue
                ops = diff_ops(q, info["span_parts"], row.get("label_stems") or set())
                if row.get("transcript") is not None:
                    pv = locate_quote(q, row["transcript"], eid, kind)
                    el = "eligible" if eligible(pv, arm, ctx.min_words)[0] else "not_eligible"
                else:
                    el = "unknown"
                allq.append(ops)
                for key, val in (("by_arm", arm), ("by_spec", spec), ("by_kind", kind), ("by_eligible", el)):
                    by[key].setdefault(val, []).append(ops)
    return {"n_participants": len(quote_units(ctx)), "quote_rule_version": textnorm.QUOTE_RULE_VERSION,
            "quote_rule_sha256": textnorm.quote_rule_sha256(), "all": diff_block(allq),
            **{key: {k: diff_block(v) for k, v in sorted(d.items())} for key, d in by.items()}}


CHECK_FUNCS = {"keyword": check_keyword, "robustness": check_robustness, "null": check_null, "diffs": check_diffs}


# ------------------------------------------------------------------ writing (guards first)

def forbidden_strings(ctx: Context) -> set[str]:
    """Every label and quote of every map of every participant (endorsed, main grid, rungs) and every participant
    id: the aggregate-only guard's forbidden set, in memory only."""
    out = set(pipeline._forbidden(ctx.r, ctx.p_list))
    for p in ctx.r.people:
        out.add(p.pid)
        try:
            maps = [ctx.r.endorsed(p)]
        except Exception:  # noqa: BLE001 - an unreadable map has nothing to forbid; the leak guard still scans
            maps = []
        maps += [g for s in ctx.r.specs if (g := ctx.r.ai_map(p.pid, s.key())) is not None]
        for g in maps:
            for x in g.nodes:
                out.add(x.text)
                if x.quote:
                    out.add(x.quote)
            out.update(e.quote for e in g.edges if e.quote)
            out.update(e.label for e in g.edges if e.label)
    return out


def write_output(ctx: Context, out: Path, check: str, obj: dict, forbidden: set[str] | None) -> tuple[Path, str]:
    """``out/aggregates/<check>.json`` (sorted keys); the guards see the exact text first (real corpus)."""
    obj = metrics._clean(obj)
    if forbidden is not None:
        report.assert_aggregate_only(obj, forbidden, allowed=report.KIT_NOTES, keys=KIT_KEYS | NOMODEL_KEYS,
                                     values=KIT_VALUES)
    p = out / "aggregates" / f"{check}.json"
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    guard = ctx.r.leak_guard()
    if guard is not None:
        guard(p, text)
    report.write_text_atomic(p, text)
    return p, hashlib.sha256(text.encode("utf-8")).hexdigest()


def summary_lines(check: str, obj: dict) -> list[str]:
    """Counts and a few headline numbers for the console (no label, quote or id)."""
    def f(x):
        return "NA" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))
    lines = []
    if check == "keyword":
        a = obj["agreement"]["all"]
        lines.append(f"keyword: {a['n']} elements, agreement {f(a['agreement'])}, kappa {f(a['cohen_kappa'])}")
        for c, v in a["classes"].items():
            lines.append(f"  {c}: judge {v['n_judge']} keyword {v['n_keyword']} both {v['n_both']} "
                         f"kappa {f(v['cohen_kappa'])}")
        for k, v in (obj.get("comparison") or {}).get("headline", {}).items():
            if k.startswith("lin:"):
                continue
            lines.append(f"  headline {k}: judge {f(v['judge'])} keyword {f(v['keyword'])}")
    elif check == "null":
        a = obj["all"]
        lines.append(f"null: {obj['n_participants']} participants, {a['n_quotes']} quotes, shifts {obj['n_shifts']}, "
                     f"rule matches run {obj['quote_rule_matches_run']}")
        lines.append(f"  own: exact {a['own']['exact']} approximate {a['own']['approximate']} unfound "
                     f"{a['own']['unfound']}")
        lines.append(f"  wrong transcript: exact {a['null']['exact']} approximate {a['null']['approximate']} -> "
                     f"located {a['null_located']} share {f(a['null_located_share'])} (bar {obj['null_bar']}: "
                     f"{'PASS' if a['null_ok'] else 'FAIL'})")
    elif check == "diffs":
        a = obj["all"]
        lines.append(f"diffs: {a['n_quotes']} approximate quotes, all-minor {a['n_quotes_all_minor']}")
        for t, v in a["ops"].items():
            lines.append(f"  {t}: {v['n_changes']} changes, {v['n_words']} words")
        for fl, v in a["flags"].items():
            lines.append(f"  {fl}: {v['n_changes']} changes in {v['n_quotes_with']} quotes")
    elif check == "robustness":
        k = obj["per_interview"]["all_specs"]["cohen_kappa"]
        lines.append(f"robustness: per-interview kappa n {k['n']} (undefined {k['n_undefined']}) median "
                     f"{f(k['median'])} min {f(k['min'])} share<{k['kappa_floor']} {f(k['share_kappa_below_floor'])}")
        for role, v in (obj.get("per_role") or {}).items():
            lines.append(f"  role {role}: n {v['n_participants']} kappa {f(v['agreement']['cohen_kappa'])}")
        for src in ("judge", "keyword"):
            blk = (obj.get("loo") or {}).get(src)
            if not blk:
                continue
            for name, v in blk["values"].items():
                if name.startswith("lin:"):
                    continue
                if "n_flag_change" in v:
                    lines.append(f"  loo {src} {name}: all {v['all']} flag changes {v['n_flag_change']}")
                else:
                    lines.append(f"  loo {src} {name}: all {f(v['all'])} range {f(v.get('min'))}..{f(v.get('max'))} "
                                 f"sign changes {v.get('n_sign_change', 'NA')}")
    return lines


def run_checks(cfg: pipeline.RunConfig, out: Path, checks: list[str], *, null_shifts: int = 1,
               loo: dict | None = None, echo=print) -> dict:
    """Run ``checks`` on the finished out_dir of ``cfg``; write each to ``out/aggregates/<check>.json``; log the
    session in ``out/logs/run.jsonl``. Returns {check: sha256 of its output}."""
    from .audit.registry import AuditLog
    out = Path(out)
    if not outside(out, Path(cfg.out_dir)):
        raise ValueError("refusing: --out must lie outside the run's out_dir (the J run is only read)")
    ctx = Context(cfg)
    ctx.null_shifts, ctx.loo = null_shifts, {**LOO_DEFAULTS, **(loo or {})}
    forbidden = None if pipeline.is_synthetic(cfg) else forbidden_strings(ctx)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    log = AuditLog(out / "logs" / "run.jsonl", f"{cfg.run_id}-nomodel")
    jlog = Path(cfg.out_dir) / "logs" / "run.jsonl"
    head = list(_log_head(jlog))
    log.append("nomodel_start", {"config_sha256": cfg.sha256(), "checks": list(checks), "run_log_head": head,
                                 "quote_rule_sha256": textnorm.quote_rule_sha256(), "null_shifts": null_shifts,
                                 "loo": ctx.loo, "n_participants": len(ctx.r.people), **prereg.code_state()})
    done = {}
    try:
        for check in checks:
            obj = CHECK_FUNCS[check](ctx)
            path, digest = write_output(ctx, out, check, {"check": check, **obj}, forbidden)
            log.append("nomodel_output", {"check": check, "file": path.name, "sha256": digest})
            done[check] = digest
            for line in summary_lines(check, obj):
                echo(line)
    finally:
        log.append("nomodel_end", {"outputs": done})
        log.close()
    return done


def _log_head(path: Path) -> tuple[int, str | None]:
    """(records, hash of the last record) of the J log, read without opening a writer."""
    n, last = 0, None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n += 1
                    last = line
    except OSError:
        return 0, None
    try:
        return n, json.loads(last)["hash"] if last else None
    except (ValueError, KeyError, TypeError):
        return n, None


def main(a) -> int:
    """``cli validate-nomodel``. Exit 0 done, 2 refused (bad arguments, --out inside the run), 1 error (its type and
    line number only: a message could carry text of the corpus)."""
    checks = [c.strip() for c in (a.checks or ",".join(CHECK_FUNCS)).split(",") if c.strip()]
    bad = [c for c in checks if c not in CHECK_FUNCS]
    if bad:
        print(f"refused: unknown or unavailable check(s) {', '.join(bad)}; available: {', '.join(CHECK_FUNCS)}",
              file=sys.stderr)
        return 2
    cfg = pipeline.RunConfig.load(a.config)
    if not outside(Path(a.out), Path(cfg.out_dir)):
        print("refused: --out must lie outside the run's out_dir (the J run is only read)", file=sys.stderr)
        return 2
    loo = {k: v for k, v in (("B", a.loo_b), ("n_perm", a.loo_perm), ("n_perm_script", a.loo_perm)) if v}
    try:
        done = run_checks(cfg, Path(a.out), checks, null_shifts=a.null_shifts, loo=loo)
    except Exception as exc:  # noqa: BLE001 - privacy guard: never print a message that may quote the corpus
        if pipeline.is_synthetic(cfg) and getattr(a, "debug", False):
            raise
        tb = exc.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = f"{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}" if tb else "?"
        print(f"error: {type(exc).__name__} at {where}", file=sys.stderr)
        return 1
    for check, digest in done.items():
        print(f"wrote {check}.json sha256 {digest[:16]}")
    return 0
