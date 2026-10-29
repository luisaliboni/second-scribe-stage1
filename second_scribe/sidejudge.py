"""Side study on a FINISHED judge phase (J): AI-only checks of the judge steps, audit package items 2, 3, 4, 6, 7, 8.

  sidejudge --config J_CFG --out SIDE_DIR [--parts 2,3,4,6,7,8] [--dry-count] [--client http|oracle]
            [--judge-prefix P] [--judge2-prefix P] [--seed S] [--only-role judge|judge2] [--sizes k=v,...]

Parts (each resumable, seeded, T=0; every call through ``llm.log_call`` into the side study's own hash-chained log):
  2  the second judge (judge2, another model family) re-labels every quoted element of the main-grid specs with the
     J run's own logged messages, byte for byte (only the model differs); Cohen's kappa and raw agreement overall, per
     class, per spec, per role and over interviews; the headline under each judge (the kit's metrics code, through
     ``pipeline.compute_aggregates`` with judge 2's labels in place) and the band [min, max] of the two.
  3  planted cases built from the real material: (i) a real quote paired with an element of ANOTHER participant's map
     that shares no content word with the quote or with the participant's own map (expected: unsupported); (ii) real
     endorsed labels placed in sentence templates of known modality (the bench bank's domain-neutral templates; half of
     them in speech style). Both judges; accuracy per class with Wilson intervals; agreement; "both wrong" per class.
  4  the label order reversed (``judge.reverse_label_order``) on a seeded sample of the J run's own batches (same
     items, same order, only the rubric card's label order differs); both judges; agreement and position bias.
  6  the off-record step (``match.in_transcript``, the judge) on a COPY of each transcript into which labels of another
     participant were planted (verbatim, and as judge 2's paraphrase: expected "mentioned") next to other labels of
     another participant that share no content word with the transcript (expected "not mentioned"); and a calibration
     of the aligner (``match.align_labels``, the judge) on the participant's own endorsed labels against perturbed
     copies of some of them (word order reversed, one word deleted, one typo, a paraphrase written by judge 2) plus
     negatives (another participant's labels with no shared content word, unrelated decoys): precision and recall per
     perturbation type, with Wilson intervals.
  7  the second judge on a seeded, kind-stratified sample of the J run's alignment calls (its logged messages).
  8  canonicalization of a seeded label sample in another order (partition agreement with the J run's) and a larger
     determinism sample (logged J judge calls re-sent to the judge; identical replies).

Privacy: the J out_dir is only read (through a ``pipeline._Run`` built with no client and a log whose ``append``
raises). Outputs on real data are counts, rates, intervals and kappas, written to ``SIDE_DIR/results`` after the
kit's aggregate-only guard (every label, quote, planted sentence and paraphrase forbidden) and, for a real corpus, the
leak guard. The side log and ``SIDE_DIR/work`` hold the prompts and replies and stay on the machine, like the J log.
Agreement between two models is not accuracy; planted cases measure cue reading on real wording, not subtle modality.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import judge as judge_mod
from . import metrics, pipeline, prereg, textnorm
from .audit import verify as audit_verify
from .audit.registry import AuditLog
from .calibrate import _DECOYS, _graph, _typo
from .families import known_family, model_name
from .judge import _render_item, _squash, reverse_label_order
from .judge_bench import mcnemar_exact_p, says_it_happened, speechify, wilson
from .keywords import keyword_modality
from .kit_vocab import KIT_KEYS, KIT_VALUES
from .llm import (FORBIDDEN_JUDGE_FAMILIES, HTTPClient, ModelConfig, ModelResponse, RealGuardRefused, derive_seed,
                  guard_real_clients, log_call, messages_sha256, redact_params)
from .match import (MatchSetting, _numbered, _parse_concepts, _parse_pairs, align_labels, canonicalize,
                    in_transcript, lexical_sim, match_nodes)
from .pipeline import PROMPTS_DIR, RunConfig, write_json_atomic
from .prompts import DEFAULT_PROMPT_VERSION, JUDGE_PROMPT_VERSION, load_helper_prompt
from .provenance import provenance_for
from .report import KIT_NOTES, assert_aggregate_only, write_text_atomic
from .schemas import MODALITIES, Transcript, Turn
from .stats import cohen_kappa
from .synth.bench_bank import BENCH_TEMPLATES
from .textnorm import content_tokens, normalize

PARTS = ("2", "3", "4", "6", "7", "8")
PART_ROLES = {"2": ("judge2",), "3": ("judge", "judge2"), "4": ("judge", "judge2"), "6": ("judge2", "judge"),
              "7": ("judge2",), "8": ("judge",)}
LOG_NAME = "sidejudge.jsonl"
BATCH = 20                                  # modality batch size (the pipeline's default)
DEFAULTS = {"n_swap": 300, "n_per_class": 60, "n_reversed": 100, "n_align": 200, "n_canon": 1000,
            "n_determinism": 60, "n_plant": 8, "n_offneg": 8, "n_cal": 8, "n_cal_neg": 2}
KEPT_STAGES = ("judge:modality", "judge:shuffled", "judge:align", "judge:canon")   # J units kept in memory
J_SESSION_STAGES = frozenset(pipeline.STAGE_GROUPS["J"])
TEMPLATE_CLASSES = ("actual", "wished", "negated", "hypothetical", "other_person")
# bench-bank templates that name the synthetic domain are left out (the real corpus is another domain)
_DOMAIN_WORDS = re.compile(r"\b(city|truck|market|officer|stall|opened|open|paperwork|owner|rules)\b", re.I)
PLANT_SENTENCES = ("And there was {x} as well, yes.", "We also had {x} at one point.",
                   "Then there was {x}.", "That was when {x} came into it.")
PERTURB_TYPES = ("reorder", "deletion", "typo", "paraphrase")
NEG_TYPES = ("other_label", "decoy")
PARAPHRASE_PROMPT = (
    "Below is a numbered list of short labels taken from a journey map (steps, services, people and sources of "
    "information).\n"
    "For each label, write ONE new label that means exactly the same thing but says it in different words: replace "
    "the main words with synonyms or a different phrasing, keep it short (2 to 8 words), and do not add or drop any "
    "information.\n"
    "Return only JSON in exactly this format. Begin your answer with {, and write no text before or after the JSON:\n"
    '{"paraphrases": [{"n": <label number>, "text": "<new label>"}]}\n'
    "LABELS:\n{labels}\n"
    "REMINDER: one paraphrase for every label number, in different words, same meaning. Reply with only the JSON "
    '{"paraphrases": [...]}. Begin your answer with {')
PARAPHRASE_PROMPT_SHA256 = hashlib.sha256(PARAPHRASE_PROMPT.encode("utf-8")).hexdigest()

# the words this module puts into its results as keys or values (exempt from the label check by exact match only;
# the length rule still applies), on top of the kit's own (kit_vocab)
SIDE_KEYS: frozenset[str] = frozenset((
    "accuracy", "agreement", "all", "band", "both_wrong", "both_wrong_agree", "by_class", "by_kind", "by_role",
    "by_spec", "by_style", "by_stratum", "by_type", "calls", "ci", "cohen_kappa", "confusion", "coverage", "done",
    "earlier_in_reversed", "elements", "exact_name_agreement", "false_match", "first", "forward_vs_reversed",
    "headline", "identical", "interviews", "judge", "judge1", "judge2", "judge_only", "kappa", "kappa_binary",
    "later_in_reversed", "lenient", "lexical_baseline", "lexical_only", "marginals_forward", "marginals_reversed",
    "matched_other_own", "max", "mcnemar_p", "mean", "median", "min", "n", "n_agree", "n_both", "n_clusters_j",
    "n_clusters_side", "n_correct", "n_judged", "n_labels", "n_mentioned", "n_pairs", "n_participants", "n_units",
    "not_judged", "other_arms", "pair_agreement", "part", "positive_agreement", "precision", "primary",
    "primary_setting", "rand_index", "adjusted_rand_index", "rate", "recall", "remaining", "role", "same",
    "same_pairs", "second_judge", "share", "share_below_0_6", "share_of_agreements_wrong", "skipped", "strict",
    "uncovered", "units", "j_items", "j_items_skipped", "verbatim", "paraphrase", "lexically_absent",
    "lexically_present", "planted_paraphrase", "planted_verbatim", "swap", "prose", "speech", "real", "cells",
    "has_partner", "judge1_same_pairs", "judge2_same_pairs", "jaccard_same", "relation_agreement", "kind",
    "headline_judge1_same_participants", "headline_judge2", "mean_lexical_sim", "status", "note", "notes",
    "reorder", "deletion", "typo", "other_label", "decoy", "negatives", "positives", "j_valid", "replies",
    "n_items", "items", "sample", "labels", "batches", "stable", "changed", "n_judge1", "n_judge2",
    "not_judged_judge1", "not_judged_judge2", "agreement_ci", "n_participants_without_second_judge", "parts",
    "verify", "records", "reason", "ok", "seed", "options", "j_records", "corpus_kind", "formula", "by_part",
    "between_judges", "forward", "reversed", "template", *MODALITIES, "judge2_precision", "judge2_recall",
    "jaccard", "same_or_close_pairs", "canon", "determinism", "fallback", "by_setting", "aligncal", "offrecord",
    "by_stage", "not_mentioned", "mentioned", "expected", "lexically_absent", "n_clusters_side",
))
SIDE_VALUES: frozenset[str] = frozenset((*MODALITIES, *PARTS, "ok", "failed", "done", "pending", "skipped",
                                         "judge", "judge2", "not_run"))


class SideRefused(RuntimeError):
    """The side study cannot start honestly (the J run is not finished, a guard refused, ...); nothing was called."""


class _ReadOnlyLog:
    """The J run's log seen by ``pipeline._Run`` here: readable path, no appends (nothing may be written into it)."""

    def __init__(self, path):
        self.path = str(path)

    def append(self, record_type, payload):
        raise RuntimeError("sidejudge never writes to the J run's log")

    def note_request_ids(self, ids):
        return None


# ------------------------------------------------------------------ the J log, read once

@dataclass
class JUnit:
    """One ``log_call`` unit of the J run (stage + item_id): its attempt-1 request and the replies."""
    stage: str
    item_id: str
    seq: int
    first: dict                       # attempt-1 call_request payload
    first_text: str | None = None     # attempt-1 reply text (None: no reply)
    first_valid: bool = False
    accepted_text: str | None = None  # the reply log_call accepted (first one with no validation error)
    attempts: int = 0
    bodies: object = None             # modality units: the item bodies of the attempt-1 listing (False: not one)


@dataclass
class JIndex:
    units: dict = field(default_factory=dict)          # (stage, item_id) -> JUnit, for KEPT_STAGES
    starts: list = field(default_factory=list)         # run_start / run_fork records (minimal)
    open_session: bool = False
    judge_instruments: set = field(default_factory=set)
    judge_models: set = field(default_factory=set)
    recon_models: set = field(default_factory=set)
    records: int = 0
    head_hash: str | None = None
    manifest_hash: str | None = None

    def modality_units(self) -> list[JUnit]:
        return sorted((u for (s, _i), u in self.units.items() if s == "judge:modality" and "@" not in u.item_id),
                      key=lambda u: u.seq)


def read_j_log(log_path) -> JIndex:
    """One streaming pass over the J log (no record is kept but what the parts need)."""
    ix = JIndex()
    unit_of: dict[str, tuple] = {}
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            rt, pl = rec.get("record_type"), rec.get("payload") or {}
            ix.records += 1
            if rt in ("run_start", "run_fork"):
                ix.starts.append({"record_type": rt, "payload": pl, "seq": rec.get("seq"),
                                  "prev_hash": rec.get("prev_hash")})
                if rt == "run_start":
                    ix.open_session = True
            elif rt == "run_end":
                ix.open_session = False
            elif rt == "call_request":
                stage = str(pl.get("stage", ""))
                if stage.startswith("judge:"):
                    if "model_id" in pl:
                        ix.judge_models.add(pl["model_id"])
                        ix.judge_instruments.add(json.dumps([pl.get("model_id"), pl.get("params") or {}],
                                                            sort_keys=True))
                elif stage.startswith(("recon:", "q:", "prior_only")) and pl.get("model_id"):
                    ix.recon_models.add(pl["model_id"])
                if stage in KEPT_STAGES:
                    key = (stage, pl["item_id"])
                    unit_of[pl["request_id"]] = key
                    u = ix.units.get(key)
                    if u is None:
                        u = ix.units[key] = JUnit(stage, pl["item_id"], int(rec.get("seq", 0)), pl)
                    u.attempts += 1
            elif rt == "call_response" and pl.get("request_id") in unit_of:
                u = ix.units[unit_of[pl["request_id"]]]
                valid = pl.get("validation_error") is None
                if pl["request_id"] == u.first.get("request_id"):
                    u.first_text, u.first_valid = pl.get("text"), valid
                if valid and u.accepted_text is None:
                    u.accepted_text = pl.get("text")
    return ix


def j_finished_problem(log_path, ix: JIndex) -> str | None:
    """None when the J run is finished: its log verifies against its manifest (a running session's manifest lags behind
    the log), no session is open (every run_start has its run_end), and some session ran a J stage."""
    ok, detail = audit_verify.verify_run(log_path)
    if not ok:
        return f"the J log does not verify against its manifest ({detail.get('reason')}): is the J run still running?"
    ix.head_hash, ix.manifest_hash = detail.get("head_hash"), detail.get("manifest_hash")
    if ix.open_session:
        return "the last session of the J log has no run_end (a session is running or crashed)"
    if not any(set((s["payload"].get("stages") or [])) & J_SESSION_STAGES for s in ix.starts
               if s["record_type"] == "run_start"):
        return "no session of the J log ran a judge stage"
    return None


# ------------------------------------------------------------------ rendering helpers

def _forward_template() -> tuple[str, str | None]:
    return judge_mod._render_template(PROMPTS_DIR, JUDGE_PROMPT_VERSION)


def body_of(it: dict) -> str:
    """An item as the judge prompt renders it, without its number (identical rendering = identical judge input)."""
    return _render_item(1, it)[3:]


_ENTRY = re.compile(r"\n(?=\d+\. ELEMENT: )")


def listing_of(messages: list[dict], template: str) -> str | None:
    """The numbered item listing of a logged modality request rendered from ``template`` (None if it is not one)."""
    user = next((m["content"] for m in messages if m.get("role") == "user"), None)
    pre, _, post = template.partition("{items}")
    if user is None or not user.startswith(pre) or not user.endswith(post) or len(user) < len(pre) + len(post):
        return None
    return user[len(pre):len(user) - len(post)]


def bodies_of(listing: str) -> list[str] | None:
    """The item bodies of a listing, in order (None if the numbering is not 1..n)."""
    entries = _ENTRY.split(listing)
    out = []
    for k, e in enumerate(entries, 1):
        head = f"{k}. ELEMENT: "
        if not e.startswith(head):
            return None
        out.append(e[len(str(k)) + 2:])
    return out


def parse_labels(text: str | None, n: int) -> list[str] | None:
    if text is None:
        return None
    res = judge_mod._parse_labels(text, n)
    return None if isinstance(res, str) else [res[k] for k in range(1, n + 1)]


def _validator(n: int):
    def validate(text: str) -> str | None:
        res = judge_mod._parse_labels(text, n)
        return res if isinstance(res, str) else None
    return validate


def _uid(item_id: str) -> str:
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:20]


# ------------------------------------------------------------------ the side study's own store

class SideWork:
    """``SIDE_DIR/work/<part>/<name>.json``; every write is announced in the side log first (``work_item``)."""

    def __init__(self, root, log=None):
        self.root, self.log = Path(root), log

    def path(self, part: str, name: str) -> Path:
        return self.root / part / f"{name}.json"

    def get(self, part: str, name: str) -> dict | None:
        try:
            d = json.loads(self.path(part, name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("status") in ("ok", "failed") else None

    def put(self, part: str, name: str, obj: dict) -> None:
        p = self.path(part, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if self.log is not None:
            audit_verify.log_work_item(self.log, f"side:{part}", name, obj)
        write_json_atomic(p, obj)

    def names(self, part: str) -> list[str]:
        d = self.root / part
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


class OverlayWork(pipeline.Work):
    """The J work store, except that stage ``judge2_modality`` is read from the side study's store: the kit's metrics
    code then computes the second-judge block (kappa, headline under judge 2) from the side study's labels."""

    def __init__(self, j_work: pipeline.Work, side_root):
        super().__init__(Path(j_work.root).parent, j_work.tracker, j_work.log)
        self.side_root = Path(side_root)

    def path(self, stage, name):
        if stage == "judge2_modality":
            return self.side_root / stage / f"{name}.json"
        return super().path(stage, name)

    def names(self, stage):
        if stage == "judge2_modality":
            d = self.side_root / stage
            return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []
        return super().names(stage)

    def put(self, *a, **k):
        raise RuntimeError("the J work store is read-only here")

    def remove(self, *a, **k):
        raise RuntimeError("the J work store is read-only here")


# ------------------------------------------------------------------ statistics

def rate_block(k: int, n: int) -> dict:
    return {"n": n, "n_correct": k, "rate": (k / n) if n else None, "ci": wilson(k, n)}


def kappa_block(a: list[str], b: list[str]) -> dict:
    n = len(a)
    if not n:
        return {"n": 0, "agreement": None, "kappa": None}
    k = cohen_kappa(a, b)
    return {"n": n, "agreement": sum(x == y for x, y in zip(a, b)) / n,
            "agreement_ci": wilson(sum(x == y for x, y in zip(a, b)), n),
            "kappa": None if (isinstance(k, float) and math.isnan(k)) else k}


def per_class_agreement(a: list[str], b: list[str], classes=MODALITIES) -> dict:
    out = {}
    for c in classes:
        xa, xb = [x == c for x in a], [y == c for y in b]
        n1, n2, both = sum(xa), sum(xb), sum(1 for x, y in zip(xa, xb) if x and y)
        kb = cohen_kappa(["y" if x else "n" for x in xa], ["y" if y else "n" for y in xb]) if a else math.nan
        out[c] = {"n_judge1": n1, "n_judge2": n2, "n_both": both,
                  "positive_agreement": (2 * both / (n1 + n2)) if (n1 + n2) else None,
                  "kappa_binary": None if (isinstance(kb, float) and math.isnan(kb)) else kb}
    return out


def confusion(a: list[str], b: list[str]) -> dict:
    c = Counter(zip(a, b))
    return {x: {y: c.get((x, y), 0) for y in MODALITIES} for x in MODALITIES}


def kappa_counts(c: Counter) -> dict:
    """kappa_block from a {(a, b): count} table (for tables too large to list)."""
    n = sum(c.values())
    if not n:
        return {"n": 0, "agreement": None, "kappa": None}
    agree = sum(v for (x, y), v in c.items() if x == y)
    ma, mb = Counter(), Counter()
    for (x, y), v in c.items():
        ma[x] += v
        mb[y] += v
    pe = sum(ma[k] * mb[k] for k in ma) / (n * n)
    po = agree / n
    return {"n": n, "agreement": po, "agreement_ci": wilson(agree, n),
            "kappa": None if pe >= 1.0 else (po - pe) / (1 - pe)}


def adjusted_rand(a: list[str], b: list[str]) -> dict:
    """Rand index and adjusted Rand index of two partitions given as cluster names per item."""
    n = len(a)
    if n < 2:
        return {"n": n, "rand_index": None, "adjusted_rand_index": None}
    c2 = lambda k: k * (k - 1) / 2
    cont = Counter(zip(a, b))
    sa, sb = Counter(a), Counter(b)
    s_ij = sum(c2(v) for v in cont.values())
    s_a, s_b, tot = sum(c2(v) for v in sa.values()), sum(c2(v) for v in sb.values()), c2(n)
    rand = (tot + 2 * s_ij - s_a - s_b) / tot
    exp = s_a * s_b / tot
    mx = (s_a + s_b) / 2
    return {"n": n, "rand_index": rand, "adjusted_rand_index": None if mx == exp else (s_ij - exp) / (mx - exp)}


def summary_of(xs: list[float]) -> dict:
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not xs:
        return {"n": 0}
    m = len(xs)
    med = xs[m // 2] if m % 2 else (xs[m // 2 - 1] + xs[m // 2]) / 2
    return {"n": m, "median": med, "min": xs[0], "max": xs[-1], "share_below_0_6": sum(x < 0.6 for x in xs) / m}


def band_tree(a, b):
    """The two headlines side by side: every number present in both becomes {judge1, judge2, min, max}, every boolean
    {judge1, judge2, same}; anything else is left out."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = {}
        for k in sorted(set(a) & set(b)):
            v = band_tree(a[k], b[k])
            if v not in (None, {}):
                out[k] = v
        return out
    if isinstance(a, bool) and isinstance(b, bool):
        return {"judge1": a, "judge2": b, "same": a == b}
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        if any(isinstance(x, float) and not math.isfinite(x) for x in (a, b)):
            return None
        return {"judge1": a, "judge2": b, "min": min(a, b), "max": max(a, b)}
    return None


def _clean(x):
    """NaN/inf -> None (the audit log and JSON outputs refuse them)."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


# ------------------------------------------------------------------ the study

class SideStudy:
    def __init__(self, cfg: RunConfig, out, *, seed: int = 0, opts: dict | None = None, clients: dict | None = None,
                 only_role: str | None = None):
        self.cfg, self.out, self.seed = cfg, Path(out).resolve(), int(seed)
        self.opts = {**DEFAULTS, **(opts or {})}
        self.clients = clients or {}
        self.only_role = only_role
        self.j_out = Path(cfg.out_dir).resolve()
        self.j_log = self.j_out / "logs" / "run.jsonl"
        self.real = cfg.corpus.get("kind") == "real"
        self.log = None
        self.side = SideWork(self.out / "work")
        self.ix: JIndex | None = None
        self.r = None
        self.template, self.system = _forward_template()
        self.calls = Counter()
        self._private: set[str] = set()     # texts this study made from the real material (never in a result)

    # -------------------------------------------------------------- setup
    def check_places(self) -> None:
        if not self.j_log.is_file():
            raise SideRefused("the J out_dir has no logs/run.jsonl")
        for a, b in ((self.out, self.j_out), (self.j_out, self.out)):
            if a == b or b in a.parents:
                raise SideRefused("the side-study folder must not be, contain or lie inside the J out_dir")

    def load(self) -> None:
        self.check_places()
        self.ix = read_j_log(self.j_log)
        if (why := j_finished_problem(self.j_log, self.ix)):
            raise SideRefused(why)
        if self.real and (why := self._input_problem()):
            raise SideRefused(why)
        self.r = pipeline._Run(self.cfg, {}, _ReadOnlyLog(self.j_log))

    def _input_problem(self) -> str | None:
        """The prompt set, config and quote rule this code would judge with must be the J run's (the items rebuilt
        here and the prompts rendered here are then the J run's own)."""
        starts = prereg.session_starts(self.ix.starts)
        for fld, label, now in (("prompt_set_sha256", "prompt set", pipeline.prompt_set_sha256()),
                                ("config_sha256", "config", self.cfg.sha256()),
                                ("quote_rule_sha256", "quote rule", textnorm.quote_rule_sha256())):
            was = {p.get(fld) for p in starts} - {None}
            if was and (now not in was or len(was) > 1):
                return f"the {label} of this code/config differs from the J run's"
        if (why := pipeline.prompt_manifest_problem()):
            return f"the prompt files differ from prompts/MANIFEST.json ({why})"
        return None

    def guard_clients(self) -> None:
        """Before any call: for a real corpus the kit's guard (local endpoints, thinking off, no Qwen judge); judge 2
        of a known family other than the judge's and the reconstructor's; the judge = the J run's own instrument."""
        live = {r: c for r, c in self.clients.items() if hasattr(getattr(c, "cfg", None), "endpoint")}
        if self.real:
            guard_real_clients(self.clients)
        j2 = getattr(self.clients.get("judge2"), "cfg", None)
        j1 = getattr(self.clients.get("judge"), "cfg", None)
        judge_ids = set(self.ix.judge_models) | ({j1.model_id} if j1 is not None else set())
        if j2 is not None and getattr(j2, "model_id", None):
            fam2 = known_family(j2.model_id)
            if self.real and fam2 is None:
                raise RealGuardRefused("judge2's model family is not known (families.FAMILY_TABLE): a second judge "
                                       "must be shown to be another family")
            if any(f in model_name(j2.model_id) for f in FORBIDDEN_JUDGE_FAMILIES):
                raise RealGuardRefused("judge2 is Qwen-family (the reconstructor's family)")
            for other, what in [(m, "judge") for m in judge_ids] + [(m, "reconstructor") for m in self.ix.recon_models]:
                if model_name(other) == model_name(j2.model_id):
                    raise RealGuardRefused(f"judge2 is the {what}'s own model")
                fo = known_family(other)
                if fo is not None and fo == fam2:
                    raise RealGuardRefused(f"judge2 is of the {what}'s family ({fo}): a second judge must be another "
                                           "family")
                if self.real and fo is None and what == "judge":
                    raise RealGuardRefused("the judge's family is not known: judge2 cannot be shown to differ")
        if j1 is not None and "judge" in live and self.ix.judge_instruments:
            now = json.dumps([j1.model_id, redact_params(dict(j1.params or {}), j1.api_key_env)], sort_keys=True)
            if now not in self.ix.judge_instruments or len(self.ix.judge_instruments) > 1:
                raise RealGuardRefused("the judge client is not the instrument (model id and params) the J log "
                                       "records: the side study's judge calls must be the J run's judge")

    def open_log(self, parts: list[str]) -> None:
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        path = self.out / "logs" / LOG_NAME
        if path.exists():
            if (why := prereg.resume_problem(path, self.clients)):
                raise SideRefused(f"refusing to resume the side study: {why}")
            earlier = [r["payload"] for r in (prereg._records(path) or []) if r.get("record_type") == "sidejudge_start"]
            if any(e.get("j_head_hash") != self.ix.head_hash for e in earlier):
                raise SideRefused("the J log changed since an earlier side session (its head hash differs)")
            if any(e.get("seed") != self.seed or e.get("options") != self.opts for e in earlier):
                raise SideRefused("an earlier side session ran with another seed or other sample sizes: use a new "
                                  "side folder")
        self.log = AuditLog(path, f"side:{self.cfg.run_id}")
        self.side.log = self.log
        self.log.append("sidejudge_start", {
            "j_config_sha256": self.cfg.sha256(), "j_records": self.ix.records, "j_head_hash": self.ix.head_hash,
            "j_manifest_sha256": self.ix.manifest_hash, "prompt_set_sha256": pipeline.prompt_set_sha256(),
            "quote_rule_sha256": textnorm.quote_rule_sha256(), "paraphrase_prompt_sha256": PARAPHRASE_PROMPT_SHA256,
            "models": {r: getattr(getattr(c, "cfg", None), "model_id", None) or getattr(c, "model_label", None)
                       for r, c in sorted(self.clients.items())},
            "parts": parts, "seed": self.seed, "options": self.opts, "only_role": self.only_role,
            "corpus_kind": self.cfg.corpus.get("kind"), **prereg.code_state()})

    def close_log(self, parts_done: list[str]) -> dict:
        self.log.append("sidejudge_end", {"parts": parts_done, "calls": dict(self.calls)})
        self.log.close()
        ok, detail = audit_verify.verify_run(self.out / "logs" / LOG_NAME)
        return {"ok": ok, "reason": detail.get("reason"), "records": detail.get("records")}

    def rng(self, *parts) -> random.Random:
        return random.Random(derive_seed("sidejudge", self.seed, *parts))

    def role_on(self, role: str) -> bool:
        return (self.only_role is None or self.only_role == role) and role in self.clients

    def call(self, role: str, messages, *, stage: str, item_id: str, temperature=0.0, seed=None, max_tokens=1000,
             validate=None, max_attempts=3):
        self.calls[f"{role}:{stage}"] += 1
        return log_call(self.log, self.clients[role], messages, stage=f"{role}:{stage}", item_id=item_id,
                        temperature=temperature, seed=seed, max_tokens=max_tokens, validate=validate,
                        max_attempts=max_attempts)

    # -------------------------------------------------------------- J inputs
    def specs(self) -> list[str]:
        return [s.key() for s in self.r.specs]

    def main_items(self, p, spec: str):
        """(map, J judge item, the modality items as _judge_maps built them) or None."""
        g, t = self.r.ai_map(p.pid, spec), self.r.transcript(p)
        if g is None or t is None:
            return None
        j = self.r.judged("judge_modality", f"{p.pid}__{spec}", g)
        if j is None or j.get("status") != "ok":
            return None
        return g, j, judge_mod.modality_items(g, provenance=provenance_for(g, t), transcript=t)

    def forbidden(self) -> set[str]:
        """Every label and quote of every map of the run (rungs included) and every text this study made (planted
        sentences, transcript plants, paraphrases): none may appear in a result."""
        out: set[str] = set(self._private)
        views = [self.r] + [pipeline.rung_view(self.r, k) for k in pipeline.ladder_rungs(self.cfg)]
        for p in self.r.people:
            out.update(x.text for x in self.r.endorsed(p).nodes)
            for v in views:
                for s in v.specs:
                    g = v.ai_map(p.pid, s.key())
                    if g is None:
                        continue
                    for x in g.nodes:
                        out.add(x.text)
                        if x.quote:
                            out.add(x.quote)
                    out.update(e.quote for e in g.edges if e.quote)
        return out

    def write_result(self, name: str, obj: dict) -> Path:
        obj = _clean(obj)
        if self.real:
            assert_aggregate_only(obj, self.forbidden(), allowed=KIT_NOTES, keys=KIT_KEYS | SIDE_KEYS,
                                  values=KIT_VALUES | SIDE_VALUES)
        p = self.out / "results" / f"{name}.json"
        text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        if self.real:
            self.r.leak_guard()(p, text)
        write_text_atomic(p, text)
        return p

    # -------------------------------------------------------------- shared: replaying a logged J call
    def unit_bodies(self, u: JUnit) -> list[str] | None:
        """The item bodies of a J modality unit (None unless its attempt-1 messages hash to the logged digest and its
        user message is the forward card around a numbered listing)."""
        if u.bodies is None:
            msgs = u.first.get("messages") or []
            lst = listing_of(msgs, self.template) if messages_sha256(msgs) == u.first.get("messages_sha256") else None
            b = bodies_of(lst) if lst is not None else None
            u.bodies = b if b else False
        return u.bodies or None

    def replay(self, role: str, part: str, stage: str, u: JUnit, validate, parse) -> dict | None:
        """``u``'s attempt-1 messages, temperature, seed and token limit, sent to ``role`` (resumable by unit)."""
        name = _uid(f"{u.stage}|{u.item_id}")
        d = self.side.get(part, name)
        if d is not None or not self.role_on(role):
            return d
        f = u.first
        text, _ = self.call(role, [dict(m) for m in f["messages"]], stage=stage, item_id=u.item_id,
                            temperature=f.get("temperature", 0.0), seed=f.get("seed"),
                            max_tokens=int(f.get("max_tokens") or 1000), validate=validate)
        d = {"status": "ok" if text is not None else "failed", "j_stage": u.stage, "item_id": u.item_id,
             "parsed": parse(text) if text is not None else None}
        self.side.put(part, name, d)
        return d

    def _replay_done(self, part: str, u: JUnit) -> bool:
        return self.side.get(part, _uid(f"{u.stage}|{u.item_id}")) is not None

    # -------------------------------------------------------------- part 2
    def grid(self) -> list[tuple]:
        """[(participant, spec key, map, J judge item, modality items)] of the main grid."""
        if getattr(self, "_grid", None) is None:
            out = []
            for s in self.r.specs:
                for p in self.r.people:
                    m = self.main_items(p, s.key())
                    if m is not None:
                        out.append((p, s.key(), *m))
            self._grid = out
        return self._grid

    def relabel_plan(self) -> tuple[list[JUnit], int]:
        """The J modality units whose listings hold every quoted element of the main grid (main-grid units first, in
        log order; a label the J run served from its cache is covered by the unit that first judged it), and the
        number of element renderings no logged unit holds (0 expected)."""
        if getattr(self, "_plan2", None) is not None:
            return self._plan2
        need = {body_of(it) for (*_x, items) in self.grid() for it in items if _squash(it.get("quote"))}
        units = [u for u in self.ix.units.values() if u.stage in ("judge:modality", "judge:shuffled")]
        units.sort(key=lambda u: (u.stage != "judge:modality" or "@" in u.item_id, u.seq))
        chosen, covered = [], set()
        for u in units:
            b = self.unit_bodies(u)
            if b and (set(b) & need) - covered:
                chosen.append(u)
                covered |= set(b) & need
        self._plan2 = (chosen, len(need - covered))
        return self._plan2

    def run_relabel_units(self, units: list[JUnit]) -> None:
        for u in units:
            n = len(self.unit_bodies(u))
            self.replay("judge2", "relabel", "side_relabel", u, _validator(n), lambda t, n=n: parse_labels(t, n))

    def judge2_by_body(self) -> dict[str, str | None]:
        """{item body: judge 2's label (None: its batch failed)} from the side store."""
        out: dict[str, str | None] = {}
        for u in self.relabel_plan()[0]:
            d = self.side.get("relabel", _uid(f"{u.stage}|{u.item_id}"))
            if d is None:
                continue
            labs = d.get("parsed")
            for k, b in enumerate(self.unit_bodies(u)):
                if b not in out or out[b] is None:
                    out[b] = labs[k] if labs else None
        return out

    def part2(self) -> dict:
        chosen, uncovered = self.relabel_plan()
        self.run_relabel_units(chosen)
        lab2 = self.judge2_by_body()
        rows = []                           # (spec, role, pid, judge1, judge2)
        n_nj1 = n_nj2 = 0
        for p, spec, g, j, items in self.grid():
            labels, nj = {}, set()
            for it in items:
                if not _squash(it.get("quote")):
                    labels[it["element_id"]] = "unsupported"
                    continue
                v = lab2.get(body_of(it))
                if v is None:
                    labels[it["element_id"]] = "unsupported"
                    nj.add(it["element_id"])
                else:
                    labels[it["element_id"]] = v
            for x in g.nodes:
                labels.setdefault(x.node_id, "unsupported")
            self.side.put("judge2_modality", f"{p.pid}__{spec}", {
                "status": "ok", "labels": labels, "not_judged": sorted(nj), "n_items": len(items),
                "stats": {"replayed_from_j_log": True}, "map_sha256": j["map_sha256"]})
            nj1 = set(j.get("not_judged") or [])
            for it in items:
                k = it["element_id"]
                if not _squash(it.get("quote")):
                    continue
                if k in nj1:
                    n_nj1 += 1
                    continue
                if k in nj:
                    n_nj2 += 1
                    continue
                rows.append((spec, p.role, p.pid, j["labels"].get(k, "unsupported"), labels[k]))
        a, b = [r[3] for r in rows], [r[4] for r in rows]
        by = lambda idx: {key: kappa_block([r[3] for r in rows if r[idx] == key], [r[4] for r in rows if r[idx] == key])
                          for key in sorted({r[idx] for r in rows})}
        per_interview = []
        for pid in sorted({r[2] for r in rows}):
            sub = [r for r in rows if r[2] == pid]
            per_interview.append(kappa_block([r[3] for r in sub], [r[4] for r in sub]))
        res = {"part": "2", "units": {"n_units": len(chosen), "uncovered": uncovered,
                                       "done": sum(self._replay_done("relabel", u) for u in chosen)},
               "elements": {"n": len(rows), "not_judged_judge1": n_nj1, "not_judged_judge2": n_nj2},
               "all": kappa_block(a, b), "by_class": per_class_agreement(a, b), "confusion": confusion(a, b),
               "by_spec": by(0), "by_role": by(1),
               "interviews": {"agreement": summary_of([x["agreement"] for x in per_interview]),
                              "kappa": summary_of([x["kappa"] for x in per_interview])},
               "headline": self.headline_band()}
        self.write_result("part2", res)
        return res

    def headline_band(self) -> dict:
        """The kit's headline (``pipeline.compute_aggregates`` on the J run's work, with judge 2's labels read from the
        side store): per arm, the second-judge block and the band [min, max] over the two judges' headlines on the
        same participants."""
        view = copy.copy(self.r)
        view.work = OverlayWork(self.r.work, self.side.root)
        agg, _ = pipeline.compute_aggregates(view)
        out = {}
        arms = {metrics.spec_arm(agg.get("primary_spec") or self.specs()[0]): agg.get("headline") or {}}
        arms.update(agg.get("other_arms") or {})
        for arm, hl in arms.items():
            sj = (hl or {}).get("second_judge")
            if not sj:
                out[arm] = {"status": "not_run", "n_participants": 0}
                continue
            h1 = pipeline.compact_reasons(sj["headline_judge1_same_participants"])
            h2 = pipeline.compact_reasons(sj["headline_judge2"])
            out[arm] = {"cohen_kappa": sj.get("cohen_kappa"), "agreement": sj.get("agreement"),
                        "n_labels": sj.get("n_labels"), "n_participants": sj.get("n_participants"),
                        "n_participants_without_second_judge": sj.get("n_participants_without_second_judge"),
                        "band": band_tree(h1, h2)}
        return out

    # -------------------------------------------------------------- part 3
    def template_bank(self) -> dict[str, list[str]]:
        out = {}
        for c in TEMPLATE_CLASSES:
            ts = [t for t in BENCH_TEMPLATES[c] if not _DOMAIN_WORDS.search(t)]
            out[c] = ts if len(ts) >= 3 else list(BENCH_TEMPLATES[c])
        return out

    def planted_items(self) -> list[dict]:
        """The planted items, built from the real material with a seeded generator (no call): (i) ``swap``: a quote of
        the primary spec's map of participant p with an element label of another participant's map (same role when
        there is one) that shares no content word with the quote, its reply or anything on p's own maps; expected
        unsupported. (ii) ``template``: an endorsed label (not a person or a source of information, 1-8 words, no
        modality cue of its own) in a bench-bank sentence of known modality (templates naming the synthetic domain
        left out), half of them in speech style; kept only when the keyword rule reads the sentence as that class."""
        if getattr(self, "_planted", None) is not None:
            return self._planted
        rng = self.rng("part3")
        spec = self.specs()[0]
        people = list(self.r.people)
        ai_labels = {}
        for p in people:
            g = self.r.ai_map(p.pid, spec)
            if g is not None:
                ai_labels[p.pid] = sorted({" ".join(x.text.split()) for x in g.nodes})
        own = {}
        for p in people:
            toks: set[str] = set()
            for t in [x.text for x in self.r.endorsed(p).nodes] + ai_labels.get(p.pid, []):
                toks |= content_tokens(t)
            own[p.pid] = toks
        per: dict[str, list[dict]] = {}
        for p, s_key, _g, _j, items in self.grid():
            if s_key != spec:
                continue
            donors = [q for q in people if q.pid != p.pid and q.role == p.role and q.pid in ai_labels] or \
                     [q for q in people if q.pid != p.pid and q.pid in ai_labels]
            pool = sorted({l for q in donors for l in ai_labels[q.pid]})
            lst = []
            for it in items:
                if it["element_id"].startswith("e:") or not _squash(it.get("quote")):
                    continue
                ban = own[p.pid] | content_tokens(it["quote"]) | content_tokens(it.get("reply") or "")
                ok = [l for l in pool if content_tokens(l) and not (content_tokens(l) & ban)]
                if ok:
                    lst.append({"kind": "swap", "expected": "unsupported", "style": "real", "element": rng.choice(ok),
                                **{k: it[k] for k in ("quote", "speaker", "reply") if k in it}})
            rng.shuffle(lst)
            per[p.pid] = lst
        swap, k = [], 0
        while len(swap) < self.opts["n_swap"] and any(k < len(v) for v in per.values()):
            for pid in sorted(per):
                if k < len(per[pid]) and len(swap) < self.opts["n_swap"]:
                    swap.append(per[pid][k])
            k += 1
        labels = sorted({" ".join(x.text.split()) for p in people for x in self.r.endorsed(p).nodes
                         if x.node_type not in ("actor", "actor_information_source")
                         and 1 <= len(x.text.split()) <= 8 and keyword_modality(x.text) == "actual"})
        bank = self.template_bank()
        tmpl = []
        for c in TEMPLATE_CLASSES:
            pool = [l for l in labels if c == "actual" or not says_it_happened(l)]
            rng.shuffle(pool)
            made, i = 0, 0
            while made < self.opts["n_per_class"] and i < len(pool) * 3 and pool:
                label = pool[i % len(pool)]
                i += 1
                x = label if label[:2].isupper() else label[:1].lower() + label[1:]
                style = "speech" if made % 2 else "prose"
                for _try in range(4):
                    t = rng.choice(bank[c])
                    sent = t.replace("{x}", x)
                    sent = sent[:1].upper() + sent[1:]
                    if style == "speech":
                        sent = speechify(sent, protect=[x], key=f"side3:{c}:{made}")
                    if keyword_modality(sent) == c:
                        tmpl.append({"kind": "template", "expected": c, "style": style, "element": label,
                                     "quote": sent, "speaker": "participant"})
                        made += 1
                        break
        items = swap + tmpl
        rng.shuffle(items)
        for n, it in enumerate(items):
            it["element_id"] = f"pl{n}"
        self._private |= {it["quote"] for it in tmpl}
        self._planted = items
        return items

    def planted_batches(self) -> list[list[dict]]:
        items = self.planted_items()
        return [items[b:b + BATCH] for b in range(0, len(items), BATCH)]

    def judge_items(self, role: str, part: str, name: str, items: list[dict], *, stage: str, item_id: str,
                    label_order: str = "forward") -> dict | None:
        """``judge_modality_detailed`` (no cache) on ``items`` with ``role``'s client; resumable by ``name``."""
        d = self.side.get(part, name)
        if d is not None or not self.role_on(role):
            return d
        tap = pipeline._Tap(self.log)
        labels, nj = judge_mod.judge_modality_detailed(tap, self.clients[role], items, PROMPTS_DIR, item_id=item_id,
                                                       batch_size=BATCH, stage=f"{role}:{stage}", cache=None,
                                                       label_order=label_order)
        self.calls[f"{role}:{stage}"] += sum(1 for rt, _p in tap.records if rt == "call_request")
        d = {"status": "ok", "labels": labels, "not_judged": sorted(nj)}
        self.side.put(part, name, d)
        return d

    def part3(self) -> dict:
        batches = self.planted_batches()
        got: dict[str, dict[str, str | None]] = {"judge": {}, "judge2": {}}
        for b, batch in enumerate(batches):
            for role in ("judge", "judge2"):
                d = self.judge_items(role, "planted", f"{role}_{b:04d}", batch, stage="side_planted",
                                     item_id=f"planted:{b}")
                if d is None:
                    continue
                for it in batch:
                    k = it["element_id"]
                    got[role][k] = None if k in d["not_judged"] else d["labels"].get(k)
        items = self.planted_items()
        kinds = [("swap", "unsupported")] + [("template", c) for c in TEMPLATE_CLASSES]
        res = {"part": "3", "items": {"n": len(items), "batches": len(batches),
                                      "by_kind": {f"{k}:{c}" if k == "template" else k:
                                                  sum(1 for it in items if it["kind"] == k and it["expected"] == c)
                                                  for k, c in kinds}}}
        for role in ("judge", "judge2"):
            g = got[role]
            if not g:
                res[role] = {"status": "not_run"}
                continue
            blk = {"not_judged": sum(1 for it in items if it["element_id"] in g and g[it["element_id"]] is None)}
            judged = [it for it in items if g.get(it["element_id"]) is not None]
            blk["all"] = rate_block(sum(g[it["element_id"]] == it["expected"] for it in judged), len(judged))
            blk["by_kind"] = {}
            for k, c in kinds:
                sub = [it for it in judged if it["kind"] == k and it["expected"] == c]
                key = f"{k}:{c}" if k == "template" else k
                blk["by_kind"][key] = {**rate_block(sum(g[it["element_id"]] == c for it in sub), len(sub)),
                                       "confusion": dict(Counter(g[it["element_id"]] for it in sub))}
            blk["by_style"] = {st: rate_block(sum(g[it["element_id"]] == it["expected"] for it in sub), len(sub))
                               for st in ("prose", "speech")
                               if (sub := [it for it in judged if it["kind"] == "template" and it["style"] == st])}
            res[role] = blk
        both = [it for it in items if got["judge"].get(it["element_id"]) and got["judge2"].get(it["element_id"])]
        if both:
            a = [got["judge"][it["element_id"]] for it in both]
            b2 = [got["judge2"][it["element_id"]] for it in both]
            bw = {}
            for k, c in kinds:
                sub = [it for it in both if it["kind"] == k and it["expected"] == c]
                wrong = [it for it in sub if got["judge"][it["element_id"]] != c and got["judge2"][it["element_id"]] != c]
                bw[f"{k}:{c}" if k == "template" else k] = {
                    "n": len(sub), "both_wrong": len(wrong),
                    "both_wrong_agree": sum(1 for it in wrong
                                            if got["judge"][it["element_id"]] == got["judge2"][it["element_id"]]),
                    "rate": len(wrong) / len(sub) if sub else None, "ci": wilson(len(wrong), len(sub))}
            res["between_judges"] = {**kappa_block(a, b2), "both_wrong": bw}
        self.write_result("part3", res)
        return res

    def count3(self) -> dict:
        nb = len(self.planted_batches())
        return {role: (nb, sum(1 for b in range(nb) if self.side.get("planted", f"{role}_{b:04d}")))
                for role in ("judge", "judge2")}

    # -------------------------------------------------------------- part 4
    def reversed_sample(self) -> list[JUnit]:
        units = [u for u in self.ix.modality_units() if u.accepted_text is not None and self.unit_bodies(u)]
        k = min(self.opts["n_reversed"], len(units))
        return sorted(self.rng("part4").sample(units, k), key=lambda u: u.seq)

    def reversed_messages(self, u: JUnit) -> list[dict]:
        listing = listing_of(u.first["messages"], self.template)
        pre, _, post = reverse_label_order(self.template).partition("{items}")
        return [dict(m) if m.get("role") != "user" else {"role": "user", "content": pre + listing + post}
                for m in u.first["messages"]]

    def part4(self) -> dict:
        sample = self.reversed_sample()
        fwd: dict[str, dict] = {"judge": {}, "judge2": {}}
        rev: dict[str, dict] = {"judge": {}, "judge2": {}}
        for u in sample:
            n = len(self.unit_bodies(u))
            fwd["judge"][u.item_id] = parse_labels(u.accepted_text, n)
            d2 = self.replay("judge2", "relabel", "side_relabel", u, _validator(n), lambda t, n=n: parse_labels(t, n))
            if d2 is not None:
                fwd["judge2"][u.item_id] = d2.get("parsed")
            for role in ("judge", "judge2"):
                name = f"{role}_{_uid(u.item_id)}"
                d = self.side.get("reversed", name)
                if d is None and self.role_on(role):
                    f = u.first
                    text, _ = self.call(role, self.reversed_messages(u), stage="side_reversed", item_id=u.item_id,
                                        temperature=f.get("temperature", 0.0), seed=f.get("seed"),
                                        max_tokens=int(f.get("max_tokens") or 1000), validate=_validator(n))
                    d = {"status": "ok" if text is not None else "failed", "item_id": u.item_id,
                         "parsed": parse_labels(text, n) if text is not None else None}
                    self.side.put("reversed", name, d)
                if d is not None:
                    rev[role][u.item_id] = d.get("parsed")
        rev_pos = {m: i for i, m in enumerate(reversed(MODALITIES))}
        res = {"part": "4", "sample": {"n_units": len(sample),
                                       "n_items": sum(len(self.unit_bodies(u)) for u in sample)}}
        pairs_by_role = {}
        for role in ("judge", "judge2"):
            a, b = [], []
            for u in sample:
                x, y = fwd[role].get(u.item_id), rev[role].get(u.item_id)
                if x and y:
                    a += x
                    b += y
            pairs_by_role[role] = (a, b)
            if not a:
                res[role] = {"status": "not_run"}
                continue
            changed = [(x, y) for x, y in zip(a, b) if x != y]
            earlier = sum(1 for x, y in changed if rev_pos[y] < rev_pos[x])
            later = len(changed) - earlier
            res[role] = {"forward_vs_reversed": kappa_block(a, b), "confusion": confusion(a, b),
                         "marginals_forward": {m: a.count(m) for m in MODALITIES},
                         "marginals_reversed": {m: b.count(m) for m in MODALITIES},
                         "changed": len(changed), "earlier_in_reversed": earlier, "later_in_reversed": later,
                         "mcnemar_p": mcnemar_exact_p(earlier, later) if changed else None,
                         "failed": sum(1 for u in sample if rev[role].get(u.item_id) is None
                                       and u.item_id in rev[role])}
        both = {}
        for name, src in (("forward", fwd), ("reversed", rev)):
            a, b = [], []
            for u in sample:
                x, y = src["judge"].get(u.item_id), src["judge2"].get(u.item_id)
                if x and y:
                    a += x
                    b += y
            both[name] = kappa_block(a, b)
        res["between_judges"] = both
        self.write_result("part4", res)
        return res

    def count4(self) -> dict:
        sample = self.reversed_sample()
        out = {"judge": (len(sample), sum(1 for u in sample if self.side.get("reversed", f"judge_{_uid(u.item_id)}")))}
        # judge 2's forward labels are part 2's replays; only a sampled batch outside part 2's plan adds a call here
        plan2 = {id(u) for u in self.relabel_plan()[0]}
        extra = [u for u in sample if id(u) not in plan2]
        done2 = sum(1 for u in sample if self.side.get("reversed", f"judge2_{_uid(u.item_id)}")) + \
            sum(1 for u in extra if self._replay_done("relabel", u))
        out["judge2"] = (len(sample) + len(extra), done2)
        return out

    # -------------------------------------------------------------- part 6
    def people6(self) -> list:
        """Participants whose off-record check ran in J (a transcript too long for the judge stays out)."""
        out = []
        for p in self.r.people:
            d = self.r.work.get("in_transcript", p.pid)
            if d is not None and d.get("status") == "ok":
                out.append(p)
        return out

    def plan6(self, p) -> dict:
        """The seeded plan for one participant (no call): the labels planted into the transcript copy verbatim and as
        a paraphrase, the clean negatives, the aligner-calibration perturbations and negatives."""
        cache = self.__dict__.setdefault("_plan6", {})
        if p.pid in cache:
            return cache[p.pid]
        rng = self.rng("part6", p.pid)
        t = self.r.rule_flagged(p)
        t_tok = content_tokens(t.text("full"))
        own = []
        seen = set()
        for x in self.r.endorsed(p).nodes:
            k = normalize(x.text)
            if k and k not in seen:
                seen.add(k)
                own.append(" ".join(x.text.split()))
        own_tok = set().union(*[content_tokens(l) for l in own]) if own else set()
        donors = sorted({" ".join(x.text.split()) for q in self.r.people if q.pid != p.pid
                         for x in self.r.endorsed(q).nodes
                         if 2 <= len(x.text.split()) <= 8 and len(content_tokens(x.text)) >= 2})
        absent = [l for l in donors if not (content_tokens(l) & (t_tok | own_tok))]
        rng.shuffle(absent)
        npl, nneg = self.opts["n_plant"], self.opts["n_offneg"]
        used: set[str] = set()
        picked = []
        for l in absent:                  # labels that share no content word with one another either
            if not (content_tokens(l) & used):
                picked.append(l)
                used |= content_tokens(l)
            if len(picked) >= 2 * npl + nneg:
                break
        # dealt round-robin (verbatim, paraphrase, negative), so a small pool still feeds every stratum
        plant_v, plant_p, neg = [], [], []
        quota = ((plant_v, npl), (plant_p, npl), (neg, nneg))
        k = 0
        for l in picked:
            for _ in range(3):
                lst, q = quota[k % 3]
                k += 1
                if len(lst) < q:
                    lst.append(l)
                    break
        # aligner calibration: own labels perturbed round-robin over the four types
        order = list(range(len(own)))
        rng.shuffle(order)
        cal, ti = [], 0
        for i in order:
            if len(cal) >= self.opts["n_cal"]:
                break
            words = own[i].split()
            for _ in range(len(PERTURB_TYPES)):
                typ = PERTURB_TYPES[ti % len(PERTURB_TYPES)]
                ti += 1
                if typ == "reorder" and len(words) >= 2 and words[::-1] != words:
                    cal.append({"i": i, "type": typ, "text": " ".join(reversed(words))})
                elif typ == "deletion" and len(words) >= 3:
                    k = rng.randrange(len(words))
                    cal.append({"i": i, "type": typ, "text": " ".join(words[:k] + words[k + 1:])})
                elif typ == "typo" and (txt := _typo(own[i], rng)) != own[i]:
                    cal.append({"i": i, "type": typ, "text": txt})
                elif typ == "paraphrase":
                    cal.append({"i": i, "type": typ, "text": None})
                else:
                    continue
                break
        far = [l for l in donors if not (content_tokens(l) & own_tok) and l not in picked]
        rng.shuffle(far)
        decoys = [d for d in _DECOYS if all(lexical_sim(d, l) == 0.0 for l in own)]
        rng.shuffle(decoys)
        cache[p.pid] = plan = {"own": own, "plant_verbatim": plant_v, "plant_paraphrase": plant_p, "negatives": neg,
                               "cal": cal, "cal_other": far[:self.opts["n_cal_neg"]],
                               "cal_decoy": decoys[:self.opts["n_cal_neg"]],
                               "para_labels": plant_p + [own[c["i"]] for c in cal if c["type"] == "paraphrase"]}
        self._private |= set(own) | set(picked) | set(plan["cal_other"]) | {c["text"] for c in cal if c["text"]}
        return plan

    def paraphrases(self, p) -> dict[str, str] | None:
        """{label: judge 2's paraphrase} for the participant (None: not asked yet, or judge 2 failed)."""
        plan = self.plan6(p)
        labels = plan["para_labels"]
        if not labels:
            return {}
        d = self.side.get("paraphrase", p.pid)
        if d is None and self.role_on("judge2"):
            parsed: dict = {}

            def validate(text: str) -> str | None:
                try:
                    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                    rows = json.loads(body)["paraphrases"]
                    got = {int(r["n"]): " ".join(str(r["text"]).split()) for r in rows}
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    return f"not the requested JSON ({type(exc).__name__})"
                for n in range(1, len(labels) + 1):
                    if not got.get(n):
                        return f"missing paraphrase for label {n}"
                    if normalize(got[n]) == normalize(labels[n - 1]):
                        return f"paraphrase {n} repeats the label: use different words"
                    if len(got[n].split()) > 12:
                        return f"paraphrase {n} is longer than 12 words"
                parsed.clear()
                parsed.update(got)
                return None
            prompt = PARAPHRASE_PROMPT.replace("{labels}", _numbered(labels))
            text, _ = self.call("judge2", [{"role": "user", "content": prompt}], stage="side_paraphrase",
                                item_id=f"para:{p.pid}", seed=derive_seed("side_paraphrase", self.seed, p.pid),
                                max_tokens=200 + 40 * len(labels), validate=validate)
            d = {"status": "ok" if text is not None else "failed", "labels": labels,
                 "texts": [parsed[n] for n in range(1, len(labels) + 1)] if text is not None else None}
            self.side.put("paraphrase", p.pid, d)
        if d is None or d.get("texts") is None or d.get("labels") != labels:
            return None
        self._private |= set(d["texts"])
        return dict(zip(labels, d["texts"]))

    def planted_copy(self, p, para: dict[str, str]) -> tuple[Transcript, list[tuple[str, str, bool]]]:
        """(the transcript copy with the planted participant turns, [(label, stratum, expected mentioned)])."""
        plan = self.plan6(p)
        rng = self.rng("part6-plant", p.pid)
        t = self.r.rule_flagged(p)
        plants = [(l, l) for l in plan["plant_verbatim"]] + [(l, para[l]) for l in plan["plant_paraphrase"]
                                                              if l in para]
        sents = [PLANT_SENTENCES[rng.randrange(len(PLANT_SENTENCES))].replace("{x}", x[:1].lower() + x[1:]
                                                                              if not x[:2].isupper() else x)
                 for _l, x in plants]
        self._private |= set(sents)
        pos = sorted(rng.sample(range(len(t.turns) + 1), len(sents))) if len(t.turns) + 1 >= len(sents) else []
        turns, k = [], 0
        for i in range(len(t.turns) + 1):
            while k < len(pos) and pos[k] == i:
                turns.append(("participant", sents[k], False))
                k += 1
            if i < len(t.turns):
                x = t.turns[i]
                turns.append((x.speaker, x.text, x.mapping_talk))
        copy_t = Transcript(t.transcript_id, [Turn(n, sp, tx, mt) for n, (sp, tx, mt) in enumerate(turns)],
                            t.speaker_format, t.interviewer, t.speaker_inference)
        rows = [(l, "planted_verbatim", True) for l in plan["plant_verbatim"]]
        rows += [(l, "planted_paraphrase", True) for l in plan["plant_paraphrase"] if l in para]
        copy_tok = content_tokens(copy_t.text("full"))
        rows += [(l, "lexically_absent", False) for l in plan["negatives"] if not (content_tokens(l) & copy_tok)]
        rng.shuffle(rows)
        return copy_t, rows

    def offrecord(self, p) -> dict | None:
        d = self.side.get("offrecord", p.pid)
        if d is not None or not self.role_on("judge"):
            return d
        para = self.paraphrases(p)
        if para is None:
            return None                   # judge 2's paraphrases first (or failed): planted-paraphrase rows need them
        copy_t, rows = self.planted_copy(p, para)
        if not rows:
            d = {"status": "ok", "rows": []}
            self.side.put("offrecord", p.pid, d)
            return d
        labels = [l for l, _s, _e in rows]
        tap = pipeline._Tap(self.log)
        got = in_transcript(tap, self.clients["judge"], labels, copy_t, PROMPTS_DIR, item_id=f"off:{p.pid}",
                            stage="judge:side_offrecord")
        self.calls["judge:side_offrecord"] += sum(1 for rt, _p in tap.records if rt == "call_request")
        turn_tok = [content_tokens(x.text) for x in copy_t.turns]
        lexical = [bool(lt := content_tokens(l)) and any(len(lt & tt) / len(lt) >= 0.6 for tt in turn_tok)
                   for l in labels]
        d = {"status": pipeline._status(tap)["status"],
             "rows": [{"stratum": s_, "expected": e, "judge": bool(got.get(i, False)), "lexical": lexical[i]}
                      for i, (_l, s_, e) in enumerate(rows)]}
        self.side.put("offrecord", p.pid, d)
        return d

    def aligncal(self, p) -> dict | None:
        d = self.side.get("aligncal", p.pid)
        if d is not None or not self.role_on("judge"):
            return d
        plan = self.plan6(p)
        para = self.paraphrases(p)
        if para is None or not plan["own"]:
            return None
        rng = self.rng("part6-align", p.pid)
        right = [(c["text"] if c["type"] != "paraphrase" else para.get(plan["own"][c["i"]]), c["type"], c["i"])
                 for c in plan["cal"]]
        right = [r_ for r_ in right if r_[0]]
        right += [(l, "other_label", None) for l in plan["cal_other"]] + [(l, "decoy", None) for l in plan["cal_decoy"]]
        rng.shuffle(right)
        tap = pipeline._Tap(self.log)
        judged = align_labels(tap, self.clients["judge"], plan["own"], [r_[0] for r_ in right], PROMPTS_DIR,
                              item_id=f"cal:{p.pid}", stage="judge:side_align_cal")
        self.calls["judge:side_align_cal"] += sum(1 for rt, _p in tap.records if rt == "call_request")
        d = {"status": pipeline._status(tap)["status"], "right": [{"type": t_, "src": i} for _x, t_, i in right],
             "texts": [x for x, _t, _i in right], "pairs": sorted([i, j, rel] for (i, j), rel in judged.items())}
        self.side.put("aligncal", p.pid, d)
        return d

    def part6(self) -> dict:
        people = self.people6()
        for p in people:                  # judge 2 writes every paraphrase first, then the judge runs
            self.paraphrases(p)
        off, cal = {}, {}
        for p in people:
            if (d := self.offrecord(p)) is not None:
                off[p.pid] = d
            if (d := self.aligncal(p)) is not None:
                cal[p.pid] = d
        # paraphrases
        n_req = sum(1 for p in people if self.plan6(p)["para_labels"])
        sims, n_ok = [], 0
        for p in people:
            para = self.paraphrases(p) if self.side.get("paraphrase", p.pid) else None
            if para:
                n_ok += 1
                sims += [lexical_sim(a, b) for a, b in para.items()]
        res = {"part": "6", "n_participants": len(people),
               "paraphrase": {"n": n_req, "done": n_ok, "n_labels": len(sims),
                              "mean_lexical_sim": (sum(sims) / len(sims)) if sims else None,
                              "share_below_0_6": (sum(x < 0.6 for x in sims) / len(sims)) if sims else None}}
        rows = [r_ for d in off.values() for r_ in d["rows"]]
        res["offrecord"] = {"n_participants": len(off),
                            "fallback": sum(1 for d in off.values() if d["status"] != "ok"),
                            "by_stratum": {}}
        for st in ("planted_verbatim", "planted_paraphrase", "lexically_absent"):
            sub = [r_ for r_ in rows if r_["stratum"] == st]
            if not sub:
                continue
            res["offrecord"]["by_stratum"][st] = {
                "expected": "mentioned" if sub[0]["expected"] else "not_mentioned",
                "judge": rate_block(sum(r_["judge"] == r_["expected"] for r_ in sub), len(sub)),
                "lexical_baseline": rate_block(sum(r_["lexical"] == r_["expected"] for r_ in sub), len(sub))}
        res["aligncal"] = self.aligncal_metrics(cal)
        self.write_result("part6", res)
        return res

    def aligncal_metrics(self, cal: dict) -> dict:
        ps = self.cfg.match_settings()[0]
        settings = {"primary": ps, "judge_only": MatchSetting(2.0, "same"),
                    "lexical_only": MatchSetting(ps.lex_threshold, "none"),
                    "lenient": MatchSetting(ps.lex_threshold, "same_or_close")}
        out = {"n_participants": len(cal), "fallback": sum(1 for d in cal.values() if d["status"] != "ok"),
               "primary_setting": ps.key(), "by_setting": {}}
        plans = {p.pid: self.plan6(p) for p in self.r.people if p.pid in cal}
        for name, st in settings.items():
            tally = defaultdict(lambda: [0, 0, 0, 0, 0])   # type -> [n, found, pred pairs, true pred, other own]
            for pid, d in cal.items():
                own = plans[pid]["own"]
                judged = {(i, j): rel for i, j, rel in d["pairs"]}
                m = match_nodes(_graph("cal", "ai", "L", own), _graph("cal", "endorsed", "R", d["texts"]), st, judged)
                pred = {(int(a[1:]), int(b[1:])) for a, b in m.pairs}
                for j, r_ in enumerate(d["right"]):
                    t_ = tally[r_["type"]]
                    t_[0] += 1
                    mine = {i for i, jj in pred if jj == j}
                    t_[2] += len(mine)
                    if r_["src"] is not None:
                        t_[1] += r_["src"] in mine
                        t_[3] += r_["src"] in mine
                        t_[4] += len(mine - {r_["src"]})
                    else:
                        t_[1] += bool(mine)          # a negative matched to something: a false match
            blk = {}
            for typ in PERTURB_TYPES:
                if typ in tally:
                    n, found, npred, tp, other = tally[typ]
                    blk[typ] = {"recall": rate_block(found, n), "precision": rate_block(tp, npred),
                                "matched_other_own": other}
            for typ in NEG_TYPES:
                if typ in tally:
                    n, found, npred, _tp, _o = tally[typ]
                    blk[typ] = {"false_match": rate_block(found, n), "n_pairs": npred}
            pos = [tally[t_] for t_ in PERTURB_TYPES if t_ in tally]
            neg_pairs = sum(tally[t_][2] for t_ in NEG_TYPES if t_ in tally)
            tp = sum(x[3] for x in pos)
            blk["all"] = {"recall": rate_block(sum(x[1] for x in pos), sum(x[0] for x in pos)),
                          "precision": rate_block(tp, sum(x[2] for x in pos) + neg_pairs)}
            out["by_setting"][name] = blk
        return out

    def count6(self) -> dict:
        people = self.people6()
        c2 = [p for p in people if self.plan6(p)["para_labels"]]
        done2 = sum(1 for p in c2 if self.side.get("paraphrase", p.pid))
        calls1 = done1 = 0
        for p in people:
            plan = self.plan6(p)
            n_off = len(plan["plant_verbatim"]) + len(plan["plant_paraphrase"]) + len(plan["negatives"])
            k_off = -(-n_off // 60) if n_off else 0
            k_al = -(-len(plan["own"]) // 60) if plan["own"] else 0
            calls1 += k_off + k_al
            done1 += k_off * bool(self.side.get("offrecord", p.pid)) + k_al * bool(self.side.get("aligncal", p.pid))
        return {"judge2": (len(c2), done2), "judge": (calls1, done1)}

    # -------------------------------------------------------------- part 7
    def align_shape(self, u: JUnit) -> tuple[int, int, int] | None:
        """(lo, hi, n_right) of a logged alignment request (None unless it hashes to its digest and is the align
        card around two numbered lists)."""
        msgs = u.first.get("messages") or []
        if messages_sha256(msgs) != u.first.get("messages_sha256"):
            return None
        user = next((m["content"] for m in msgs if m.get("role") == "user"), "")
        tpl = load_helper_prompt(PROMPTS_DIR, "align", DEFAULT_PROMPT_VERSION)
        pre, _, rest = tpl.partition("{left}")
        mid, _, post = rest.partition("{right}")
        if not (user.startswith(pre) and user.endswith(post)) or mid not in user[len(pre):]:
            return None
        body = user[len(pre):len(user) - len(post)]
        left, _, right = body.partition(mid)
        nums = [int(m.group(1)) for line in left.split("\n") if (m := re.match(r"^(\d+)\. ", line))]
        n_right = sum(1 for line in right.split("\n") if re.match(r"^\d+\. ", line))
        if not nums or nums != list(range(nums[0], nums[0] + len(nums))) or not n_right:
            return None
        return nums[0], nums[-1], n_right

    @staticmethod
    def align_kind(item_id: str) -> str:
        rest = item_id.split("__", 1)[1] if "__" in item_id else item_id
        return rest.rsplit(":", 1)[0].split("__")[0]

    def align_sample(self) -> list[JUnit]:
        units = [u for (s_, _i), u in self.ix.units.items()
                 if s_ == "judge:align" and "@" not in u.item_id and u.accepted_text is not None and self.align_shape(u)]
        units.sort(key=lambda u: u.seq)
        rng = self.rng("part7")
        by = defaultdict(list)
        for u in units:
            by[self.align_kind(u.item_id)].append(u)
        for k in sorted(by):
            rng.shuffle(by[k])
        out, i = [], 0
        while len(out) < self.opts["n_align"] and any(i < len(v) for v in by.values()):
            for k in sorted(by):
                if i < len(by[k]) and len(out) < self.opts["n_align"]:
                    out.append(by[k][i])
            i += 1
        return sorted(out, key=lambda u: u.seq)

    def part7(self) -> dict:
        sample = self.align_sample()
        per_kind = defaultdict(lambda: {"cells": Counter(), "same1": 0, "same2": 0, "same_both": 0,
                                        "sc1": 0, "sc2": 0, "sc_both": 0, "units": 0, "failed": 0})
        for u in sample:
            lo, hi, nr = self.align_shape(u)
            val = lambda t, lo=lo, hi=hi, nr=nr: (lambda r_: r_ if isinstance(r_, str) else None)(_parse_pairs(t, lo, hi, nr))
            parse = lambda t, lo=lo, hi=hi, nr=nr: sorted([i, j, rel] for (i, j), rel in _parse_pairs(t, lo, hi, nr).items())
            d = self.replay("judge2", "align", "side_align", u, val, parse)
            if d is None:
                continue
            blk = per_kind[self.align_kind(u.item_id)]
            blk["units"] += 1
            if d.get("parsed") is None:
                blk["failed"] += 1
                continue
            p1 = _parse_pairs(u.accepted_text, lo, hi, nr)
            p2 = {(i, j): rel for i, j, rel in d["parsed"]}
            cells = (hi - lo + 1) * nr
            c = Counter()
            for key in set(p1) | set(p2):
                c[(p1.get(key, "none"), p2.get(key, "none"))] += 1
            c[("none", "none")] += cells - sum(c.values())
            blk["cells"].update(c)
            s1 = {k for k, v in p1.items() if v == "same"}
            s2 = {k for k, v in p2.items() if v == "same"}
            blk["same1"] += len(s1)
            blk["same2"] += len(s2)
            blk["same_both"] += len(s1 & s2)
            blk["sc1"] += len(p1)
            blk["sc2"] += len(p2)
            blk["sc_both"] += len(set(p1) & set(p2))
        res = {"part": "7", "sample": {"n_units": len(sample)}, "by_kind": {}}
        tot = {"cells": Counter(), "same1": 0, "same2": 0, "same_both": 0, "sc1": 0, "sc2": 0, "sc_both": 0,
               "units": 0, "failed": 0}
        for k, b in sorted(per_kind.items()):
            for f_ in tot:
                tot[f_] = tot[f_] + b[f_] if f_ != "cells" else tot[f_] + b[f_]
            res["by_kind"][k] = self._align_block(b)
        res["all"] = self._align_block(tot)
        self.write_result("part7", res)
        return res

    @staticmethod
    def _align_block(b: dict) -> dict:
        def pr(both, n1, n2):
            return {"judge2_precision": rate_block(both, n2), "judge2_recall": rate_block(both, n1),
                    "jaccard": both / (n1 + n2 - both) if (n1 + n2 - both) else None}
        return {"units": b["units"], "failed": b["failed"], "relation_agreement": kappa_counts(b["cells"]),
                "same_pairs": {"n_judge1": b["same1"], "n_judge2": b["same2"], "n_both": b["same_both"],
                               **pr(b["same_both"], b["same1"], b["same2"])},
                "same_or_close_pairs": {"n_judge1": b["sc1"], "n_judge2": b["sc2"], "n_both": b["sc_both"],
                                        **pr(b["sc_both"], b["sc1"], b["sc2"])}}

    def count7(self) -> dict:
        sample = self.align_sample()
        return {"judge2": (len(sample), sum(self._replay_done("align", u) for u in sample))}

    # -------------------------------------------------------------- part 8
    def canon_sample(self) -> tuple[list[str], dict[str, str]]:
        names = pipeline.canonical_names(self.r.work)
        labels = [l for l in pipeline._all_labels(self.r) if normalize(l) in names]
        rng = self.rng("part8-canon")
        sample = rng.sample(labels, min(self.opts["n_canon"], len(labels)))
        rng.shuffle(sample)
        return sample, names

    def determinism_sample(self) -> list[JUnit]:
        rng = self.rng("part8-det")
        by = defaultdict(list)
        for (s_, _i), u in sorted(self.ix.units.items(), key=lambda kv: kv[1].seq):
            if s_ in ("judge:modality", "judge:align", "judge:canon") and "@" not in u.item_id and u.first_valid \
                    and u.first_text is not None and messages_sha256(u.first.get("messages") or []) == \
                    u.first.get("messages_sha256"):
                by[s_].append(u)
        for k in sorted(by):
            rng.shuffle(by[k])
        out, i = [], 0
        while len(out) < self.opts["n_determinism"] and any(i < len(v) for v in by.values()):
            for k in sorted(by):
                if i < len(by[k]) and len(out) < self.opts["n_determinism"]:
                    out.append(by[k][i])
            i += 1
        return sorted(out, key=lambda u: u.seq)

    def _unit_parse(self, u: JUnit, text: str | None):
        if text is None:
            return None
        if u.stage == "judge:modality":
            return parse_labels(text, len(self.unit_bodies(u) or []))
        if u.stage == "judge:align":
            shape = self.align_shape(u)
            r_ = _parse_pairs(text, *shape) if shape else "?"
            return None if isinstance(r_, str) else sorted([i, j, rel] for (i, j), rel in r_.items())
        user = next(m["content"] for m in u.first["messages"] if m.get("role") == "user")
        n = sum(1 for line in user.split("LABELS:\n", 1)[-1].split("\n") if re.match(r"^\d+\. ", line))
        r_ = _parse_concepts(text, n)
        return None if isinstance(r_, str) else r_

    def part8(self) -> dict:
        res = {"part": "8"}
        # (a) canonicalization in another order
        sample, names = self.canon_sample()
        batches = [sample[b:b + pipeline.CANON_BATCH] for b in range(0, len(sample), pipeline.CANON_BATCH)]
        j_names, s_names, n_fb = [], [], 0
        for b, batch in enumerate(batches):
            name = f"batch_{b:04d}"
            d = self.side.get("canon", name)
            if d is None and self.role_on("judge"):
                tap = pipeline._Tap(self.log)
                concepts = canonicalize(tap, self.clients["judge"], batch, PROMPTS_DIR, item_id=f"canon_side:{b}",
                                        batch_size=pipeline.CANON_BATCH, stage="judge:side_canon")
                self.calls["judge:side_canon"] += sum(1 for rt, _p in tap.records if rt == "call_request")
                d = {**pipeline._status(tap), "labels": batch, "concepts": concepts}
                self.side.put("canon", name, d)
            if d is None:
                continue
            if d["status"] != "ok" or d["labels"] != batch:
                n_fb += 1
                continue
            for l, c in zip(batch, d["concepts"]):
                j_names.append(normalize(names[normalize(l)]))
                s_names.append(normalize(c))
        ar = adjusted_rand(j_names, s_names)
        res["canon"] = {"n_labels": len(j_names), "batches": len(batches), "fallback": n_fb,
                        "exact_name_agreement": rate_block(sum(a == b for a, b in zip(j_names, s_names)),
                                                           len(j_names)),
                        "n_clusters_j": len(set(j_names)), "n_clusters_side": len(set(s_names)),
                        "rand_index": ar["rand_index"], "adjusted_rand_index": ar["adjusted_rand_index"]}
        # (b) determinism of the judge on its own logged first attempts
        det = self.determinism_sample()
        by = defaultdict(lambda: [0, 0, 0, 0])          # stage -> [n, identical text, same parse, failed]
        for u in det:
            name = _uid(f"det|{u.stage}|{u.item_id}")
            d = self.side.get("determinism", name)
            if d is None and self.role_on("judge"):
                f = u.first
                text, _ = self.call("judge", [dict(m) for m in f["messages"]], stage="side_determinism",
                                    item_id=u.item_id, temperature=f.get("temperature", 0.0), seed=f.get("seed"),
                                    max_tokens=int(f.get("max_tokens") or 1000), max_attempts=1)
                d = {"status": "ok" if text is not None else "failed", "j_stage": u.stage,
                     "identical": text == u.first_text if text is not None else None,
                     "same_parse": (self._unit_parse(u, text) == self._unit_parse(u, u.first_text))
                     if text is not None else None}
                self.side.put("determinism", name, d)
            if d is None:
                continue
            t_ = by[u.stage.split(":", 1)[1]]
            t_[0] += 1
            if d["identical"] is None:
                t_[3] += 1
                continue
            t_[1] += bool(d["identical"])
            t_[2] += bool(d["same_parse"])
        def det_block(n, same, parse_, failed):
            return {"n": n, "failed": failed, "identical": rate_block(same, n - failed),
                    "same_parse": rate_block(parse_, n - failed)}
        res["determinism"] = {"n_units": len(det), "by_stage": {k: det_block(*v) for k, v in sorted(by.items())},
                              "all": det_block(*[sum(v[i] for v in by.values()) for i in range(4)])}
        self.write_result("part8", res)
        return res

    def count8(self) -> dict:
        sample, _ = self.canon_sample()
        nb = -(-len(sample) // pipeline.CANON_BATCH)
        det = self.determinism_sample()
        done = sum(1 for b in range(nb) if self.side.get("canon", f"batch_{b:04d}")) + \
            sum(1 for u in det if self.side.get("determinism", _uid(f"det|{u.stage}|{u.item_id}")))
        return {"judge": (nb + len(det), done)}

    # -------------------------------------------------------------- the readable summary
    def write_markdown(self) -> Path:
        res = {}
        for part in PARTS:
            f = self.out / "results" / f"part{part}.json"
            if f.exists():
                res[part] = json.loads(f.read_text(encoding="utf-8"))
        text = markdown(res, corpus_kind=self.cfg.corpus.get("kind"))
        p = self.out / "results" / "side_report.md"
        if self.real:
            self.r.leak_guard()(p, text)
        write_text_atomic(p, text)
        return p

    # -------------------------------------------------------------- planning (dry count)
    def count(self, part: str) -> dict:
        """{role: {"calls", "done", "remaining"}} for one part, from the J log and the side store alone."""
        fn = getattr(self, f"count{part}", None)
        if fn is None:
            return {"status": "not_run"}
        out = {}
        for role, (calls, done) in fn().items():
            out[role] = {"calls": calls, "done": done, "remaining": calls - done}
        return out

    def count2(self) -> dict:
        chosen, _ = self.relabel_plan()
        return {"judge2": (len(chosen), sum(self._replay_done("relabel", u) for u in chosen))}


# ------------------------------------------------------------------ the readable summary

def _f(x, nd=3) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _ci(c) -> str:
    return f" [{_f(c[0])}, {_f(c[1])}]" if c else ""


def _rate(b: dict | None) -> str:
    if not b:
        return "-"
    return f"{_f(b.get('rate'))}{_ci(b.get('ci'))} (n={b.get('n')})"


def _md_part2(r: dict) -> list[str]:
    a = r["all"]
    out = [f"- elements compared: {r['elements']['n']} (J units replayed: {r['units']['n_units']}, uncovered "
           f"renderings: {r['units']['uncovered']}; not judged: judge 1 {r['elements']['not_judged_judge1']}, judge 2 "
           f"{r['elements']['not_judged_judge2']})",
           f"- raw agreement {_f(a['agreement'])}{_ci(a.get('agreement_ci'))}, Cohen's kappa {_f(a['kappa'])}",
           "", "| class | judge 1 | judge 2 | both | positive agreement | binary kappa |", "|---|---|---|---|---|---|"]
    for c, b in r["by_class"].items():
        out.append(f"| {c} | {b['n_judge1']} | {b['n_judge2']} | {b['n_both']} | {_f(b['positive_agreement'])} | "
                   f"{_f(b['kappa_binary'])} |")
    out += ["", "| spec | n | agreement | kappa |", "|---|---|---|---|"]
    out += [f"| {k} | {b['n']} | {_f(b['agreement'])} | {_f(b['kappa'])} |" for k, b in r["by_spec"].items()]
    iv = r["interviews"]["agreement"]
    out.append(f"\n- per interview agreement: median {_f(iv.get('median'))}, min {_f(iv.get('min'))}, "
               f"share below 0.6 {_f(iv.get('share_below_0_6'))} (n={iv.get('n')})")
    for arm, h in r["headline"].items():
        if h.get("status") == "not_run":
            out.append(f"- headline band, arm {arm}: no second-judge block")
            continue
        flat = _band_rows(h["band"])
        out.append(f"- headline band, arm {arm}: kappa {_f(h['cohen_kappa'])} over {h['n_labels']} labels, "
                   f"{h['n_participants']} participants; {len(flat)} numbers, of which "
                   f"{sum(1 for _k, v in flat if v.get('min') != v.get('max') or v.get('same') is False)} differ "
                   "between the judges (full band in part2.json)")
        claims = [(k, v) for k, v in flat if k.rsplit(".", 1)[-1].startswith("claim_")]
        for k, v in claims:
            out.append(f"  - {k}: judge 1 {v['judge1']}, judge 2 {v['judge2']}")
    return out


def _band_rows(tree, prefix="") -> list[tuple[str, dict]]:
    out = []
    for k, v in (tree or {}).items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and ("judge1" in v and "judge2" in v and ("min" in v or "same" in v)):
            out.append((key, v))
        elif isinstance(v, dict):
            out += _band_rows(v, key)
    return out


def _md_part3(r: dict) -> list[str]:
    out = [f"- planted items: {r['items']['n']} in {r['items']['batches']} batches; by kind: "
           + ", ".join(f"{k} {n}" for k, n in r["items"]["by_kind"].items()), "",
           "| kind (expected) | judge 1 accuracy | judge 2 accuracy | both wrong |", "|---|---|---|---|"]
    bw = (r.get("between_judges") or {}).get("both_wrong") or {}
    for k in r["items"]["by_kind"]:
        a = (r.get("judge") or {}).get("by_kind", {}).get(k)
        b = (r.get("judge2") or {}).get("by_kind", {}).get(k)
        w = bw.get(k)
        out.append(f"| {k} | {_rate(a)} | {_rate(b)} | "
                   f"{(str(w['both_wrong']) + ' of ' + str(w['n'])) if w else '-'} |")
    for role in ("judge", "judge2"):
        blk = r.get(role) or {}
        if "all" in blk:
            out.append(f"\n- {role}: all {_rate(blk['all'])}; prose {_rate(blk['by_style'].get('prose'))}; speech "
                       f"{_rate(blk['by_style'].get('speech'))}; not judged {blk['not_judged']}")
    if r.get("between_judges"):
        out.append(f"- judge 1 vs judge 2 on the planted items: agreement {_f(r['between_judges']['agreement'])}, "
                   f"kappa {_f(r['between_judges']['kappa'])}")
    return out


def _md_part4(r: dict) -> list[str]:
    out = [f"- sample: {r['sample']['n_units']} J batches, {r['sample']['n_items']} items; the same listing under the "
           "reversed card (label lines and examples in reverse order)"]
    for role in ("judge", "judge2"):
        b = r.get(role) or {}
        if "forward_vs_reversed" not in b:
            out.append(f"- {role}: not run")
            continue
        fr = b["forward_vs_reversed"]
        out.append(f"- {role}: forward vs reversed agreement {_f(fr['agreement'])}{_ci(fr.get('agreement_ci'))}, "
                   f"kappa {_f(fr['kappa'])}; {b['changed']} labels changed ({b['earlier_in_reversed']} toward a "
                   f"label listed earlier in the reversed card, {b['later_in_reversed']} later; exact McNemar p "
                   f"{_f(b['mcnemar_p'])})")
    bj = r.get("between_judges") or {}
    for k in ("forward", "reversed"):
        if bj.get(k, {}).get("n"):
            out.append(f"- judge 1 vs judge 2, {k} card: agreement {_f(bj[k]['agreement'])}, kappa {_f(bj[k]['kappa'])}")
    return out


def _md_part7(r: dict) -> list[str]:
    out = [f"- sample: {r['sample']['n_units']} J alignment calls (stratified by kind), replayed to judge 2", "",
           "| kind | units | failed | cell agreement | cell kappa | same: judge 2 precision | same: judge 2 recall | "
           "same: Jaccard |", "|---|---|---|---|---|---|---|---|"]
    for k, b in [*r["by_kind"].items(), ("all", r["all"])]:
        ra, sp = b["relation_agreement"], b["same_pairs"]
        out.append(f"| {k} | {b['units']} | {b['failed']} | {_f(ra['agreement'])} | {_f(ra['kappa'])} | "
                   f"{_rate(sp['judge2_precision'])} | {_rate(sp['judge2_recall'])} | {_f(sp['jaccard'])} |")
    return out


def _md_part8(r: dict) -> list[str]:
    c, d = r["canon"], r["determinism"]
    out = [f"- canonicalization of {c['n_labels']} labels in another order ({c['batches']} batches, {c['fallback']} "
           f"fell back): exact concept name kept {_rate(c['exact_name_agreement'])}; partition Rand index "
           f"{_f(c['rand_index'])}, adjusted {_f(c['adjusted_rand_index'])}; clusters {c['n_clusters_j']} in J, "
           f"{c['n_clusters_side']} here",
           f"- determinism: {d['n_units']} logged first attempts re-sent to the judge; identical reply "
           f"{_rate(d['all']['identical'])}, same parsed answer {_rate(d['all']['same_parse'])}"]
    for k, b in d["by_stage"].items():
        out.append(f"  - {k}: identical {_rate(b['identical'])}, same parse {_rate(b['same_parse'])}, failed "
                   f"{b['failed']}")
    return out


def _md_part6(r: dict) -> list[str]:
    pa = r["paraphrase"]
    out = [f"- participants: {r['n_participants']}; judge 2 paraphrases: {pa['done']} of {pa['n']} participants, "
           f"{pa['n_labels']} labels, mean lexical similarity to the label {_f(pa['mean_lexical_sim'])} "
           f"(share below 0.6: {_f(pa['share_below_0_6'])})", "",
           "| off-record stratum (expected) | judge (main path) | lexical rule alone |", "|---|---|---|"]
    for st, b in r["offrecord"]["by_stratum"].items():
        out.append(f"| {st} ({b['expected']}) | {_rate(b['judge'])} | {_rate(b['lexical_baseline'])} |")
    al = r["aligncal"]
    out += ["", f"Aligner calibration on own endorsed labels ({al['n_participants']} participants, primary setting "
                f"{al['primary_setting']}; rate = recall for a perturbation, false-match rate for a negative):", "",
            "| type | primary | judge only | lexical only | lenient |", "|---|---|---|---|---|"]
    bs = al["by_setting"]
    for typ in (*PERTURB_TYPES, *NEG_TYPES):
        cells = []
        for name in ("primary", "judge_only", "lexical_only", "lenient"):
            b = (bs.get(name) or {}).get(typ)
            cells.append(_rate(b.get("recall") or b.get("false_match")) if b else "-")
        out.append(f"| {typ} | " + " | ".join(cells) + " |")
    pr = [(name, (bs.get(name) or {}).get("all", {}).get("precision")) for name in bs]
    out.append("\n- precision over all pairs: " + "; ".join(f"{n} {_rate(b)}" for n, b in pr))
    return out


MD_PARTS = {"2": ("Part 2: second judge on every quoted element of the main grid", _md_part2),
            "3": ("Part 3: planted cases built from the real material", _md_part3),
            "6": ("Part 6: off-record check on planted transcript copies, aligner calibration", _md_part6),
            "7": ("Part 7: second judge on a sample of the J run's alignments", _md_part7),
            "8": ("Part 8: canonicalization in another order, determinism sample", _md_part8),
            "4": ("Part 4: label order reversed", _md_part4)}


def markdown(res: dict, corpus_kind: str | None = None) -> str:
    lines = ["# Side study on the judge steps (sidejudge)", "",
             f"Corpus kind: {corpus_kind}. Counts, rates, Wilson 95% intervals and kappas only. Agreement between two "
             "judge models is not accuracy; planted cases test cue reading on real wording, not subtle modality.", ""]
    for part in PARTS:
        if part in res and part in MD_PARTS:
            title, fn = MD_PARTS[part]
            lines += [f"## {title}", ""] + fn(res[part]) + [""]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ clients

class ParaphraseOracle:
    """The synthetic corpus's oracle, plus the paraphrase prompt: another wording of the same bank concept (the bank's
    other form), else the words reversed. Offline, for the demo and the tests only."""

    def __init__(self, inner):
        self.inner, self.model_label = inner, "oracle"

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        user = next(m["content"] for m in messages if m.get("role") == "user")
        if '{"paraphrases"' not in user:
            return self.inner.complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens)
        block = user.split("LABELS:\n", 1)[1].split("\nREMINDER:", 1)[0]
        out = []
        for line in block.splitlines():
            m = re.match(r"^(\d+)\. (.*)$", line)
            if not m:
                continue
            label = m.group(2)
            cid = self.inner._cid(label)
            forms = [f for f in (self.inner.by_cid[cid].forms if cid else ()) if normalize(f) != normalize(label)]
            out.append({"n": int(m.group(1)), "text": forms[0] if forms else " ".join(reversed(label.split()))})
        text = json.dumps({"paraphrases": out})
        return ModelResponse(text, "stop", {}, 0, hashlib.sha256(text.encode("utf-8")).hexdigest())


def build_side_clients(kind: str, cfg: RunConfig, roles, *, judge_prefix: str | None = None,
                       judge2_prefix: str | None = None) -> dict:
    """{"judge", "judge2"} (only ``roles``). "http": the kit's HTTPClient from SS_<PREFIX>_* (the judge's prefix is the
    config's; judge 2's defaults to the config's judge2 prefix, else JUDGE2); "oracle": the synthetic oracle."""
    if kind == "oracle":
        from .demo_oracle import OracleClient
        c = cfg.corpus
        oracle = ParaphraseOracle(OracleClient(cfg.path(c["synthetic_dir"]), cfg.context["roles"],
                                               participant_labels=c["participant_labels"]["*"],
                                               interviewer_labels=c.get("interviewer_labels", {}).get("*", ())))
        return {r: oracle for r in roles}
    if kind == "http":
        prefixes = {"judge": judge_prefix or cfg.models.get("judge") or "JUDGE",
                    "judge2": judge2_prefix or cfg.models.get("judge2") or "JUDGE2"}
        return {r: HTTPClient(ModelConfig.from_env(prefixes[r])) for r in roles}
    raise ValueError(f"unknown client kind {kind!r}")


def parse_parts(text: str | None) -> list[str]:
    if not text:
        return list(PARTS)
    got = [x.strip() for x in text.split(",") if x.strip()]
    bad = [x for x in got if x not in PARTS]
    if bad:
        raise ValueError(f"unknown part {bad[0]!r}; parts are {', '.join(PARTS)}")
    return [x for x in PARTS if x in got]


def roles_for(parts: list[str], only_role: str | None) -> list[str]:
    roles = sorted({r for p in parts for r in PART_ROLES[p]})
    return [r for r in roles if only_role is None or r == only_role]


def dry_count(cfg: RunConfig, out, parts: list[str], *, seed: int = 0, opts: dict | None = None) -> dict:
    """Calls per part and role (done / remaining in ``out``); reads the J run and the side store, calls nothing and
    writes nothing."""
    st = SideStudy(cfg, out, seed=seed, opts=opts)
    st.load()
    return {"by_part": {p: st.count(p) for p in parts}, "seed": seed, "options": st.opts,
            "j_records": st.ix.records, "corpus_kind": cfg.corpus.get("kind")}


def run_side(cfg: RunConfig, out, parts: list[str], clients: dict, *, seed: int = 0, opts: dict | None = None,
             only_role: str | None = None) -> dict:
    st = SideStudy(cfg, out, seed=seed, opts=opts, clients=clients, only_role=only_role)
    st.load()
    st.guard_clients()
    st.open_log(parts)
    done, results = [], {}
    try:
        for part in parts:
            if not hasattr(st, f"part{part}"):
                continue
            results[part] = getattr(st, f"part{part}")()
            done.append(part)
    finally:
        verify = st.close_log(done)
    st.write_markdown()
    return {"parts": done, "calls": dict(st.calls), "verify": verify}


def parse_sizes(text: str | None) -> dict:
    out = {}
    for kv in (text or "").split(","):
        if not kv.strip():
            continue
        k, _, v = kv.partition("=")
        k = k.strip()
        if k not in DEFAULTS:
            raise ValueError(f"unknown size {k!r}; sizes are {', '.join(sorted(DEFAULTS))}")
        out[k] = int(v)
    return out


def main(a) -> int:
    cfg = RunConfig.load(a.config)
    parts, opts = parse_parts(a.parts), parse_sizes(a.sizes)
    try:
        if a.dry_count:
            res = dry_count(cfg, a.out, parts, seed=a.seed, opts=opts)
            if a.json:
                print(json.dumps(res, sort_keys=True, indent=2))
            else:
                print(f"sidejudge dry count (no call made): J log {res['j_records']} records, seed {a.seed}")
                total = Counter()
                for part, roles in res["by_part"].items():
                    for role, c in roles.items():
                        if isinstance(c, dict):
                            total[role] += c["remaining"]
                            print(f"  part {part} {role:7s} calls {c['calls']:6d}  done {c['done']:6d}  "
                                  f"remaining {c['remaining']:6d}")
                print("  remaining by role: " + ", ".join(f"{r} {n}" for r, n in sorted(total.items())))
            return 0
        clients = build_side_clients(a.client, cfg, roles_for(parts, a.only_role), judge_prefix=a.judge_prefix,
                                     judge2_prefix=a.judge2_prefix)
        res = run_side(cfg, a.out, parts, clients, seed=a.seed, opts=opts, only_role=a.only_role)
    except SideRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res, sort_keys=True) if a.json else
          f"sidejudge: parts {','.join(res['parts']) or '-'} done; calls {sum(res['calls'].values())}; side log "
          f"verify {'ok' if res['verify']['ok'] else 'FAILED'} ({res['verify']['records']} records)")
    return 0 if res["verify"]["ok"] else 1
