"""Judge benchmark on the synthetic planted traps (no real data, no live calls in the tests).

Three labelled sets, all built deterministically from synthetic corpora (``synth.build.build_corpus``; the
corpora of seeds 0-4 are built into temporary directories and POOLED, seed 0 being ``demo/corpus``). The pass rule
is evaluated on the pooled counts, every rate carries a Wilson 95% interval, ``per_seed`` shows the spread, and the
``mapping_talk`` items (no well-defined correct modality) are reported by kind but never scored:

* modality set: every planted trap sentence with the AI-wording label of its concept and the expected
  modality from ``traps/*.json``; 60 plain experienced statements (``actual``) from ordinary participant
  turns; 40 shuffled element/quote pairs (``unsupported``). Every item exists in two styles, "prose" (the
  sentence as written) and "speech" (``speechify``: fillers, a false start, a run-on), so the accuracy gap
  between styles can be reported (items are sent to the judge in separate style groups, never mixed);
* alignment set and off-record set: ``calibrate.calibrate_synthetic`` (reused, not duplicated).

Two judge modes: "generative" (``judge.judge_modality_detailed``, batches of 20) and "logprob"
(``judge.judge_modality_logprob``, one letter per call, calibration from the first-token logprobs).
``bench`` returns one results dict; ``pass_rule`` applies the pre-registered thresholds (constants below, fixed
before any result); ``both_wrong`` says how often two judges agree and are both wrong, per trap class.
Wording: the endorsed map is "interviewer-drawn, participant-endorsed"; synthetic recovery is a unit test of the
code, not evidence of validity.

Command line (the key is read from the environment variable named by SS_JUDGE_KEY_ENV and never printed)::

    python -m second_scribe.judge_bench --judge-prefix JUDGE --mode generative|logprob --out DIR \\
        [--client http|oracle|replay] [--fixtures FILE] [--seeds 0,1,2,3,4 | --corpus DIR] \\
        [--label-order forward|reversed|both]

The pipeline judges in generative mode, so the bench that selects the judge runs ``--mode generative`` (the letter mode
validates nothing about the pipeline judge). The output starts with the C3 floors, the ``in_transcript`` (off-record)
rows first. ``--label-order both`` scores the forward order and adds a reversed-order pass over the same items to
measure label-position bias (R58).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import random
import re
import statistics
import sys
import tempfile
from pathlib import Path

from .audit.registry import AuditLog
from .calibrate import _prf, calibrate_synthetic
from .judge import LETTER_MASS_MIN, _next_participant_reply, judge_modality_detailed, judge_modality_logprob
from .keywords import keyword_modality
from .llm import HTTPClient, ModelConfig, ModelResponse, RecordingClient, ReplayClient, derive_seed
from .match import PRIMARY_SETTING, MatchSetting
from .schemas import MODALITIES
from .synth.bank import CONCEPTS, ORDINARY_TURNS, TRAP_TEMPLATES
from .synth.bench_bank import BENCH_TEMPLATES, IV_NO_QUESTIONS, PER_TEMPLATE, render, template_id, template_text
from .synth.build import build_corpus
from .transcripts import parse_paragraphs, read_docx_text

_ROOT = Path(__file__).resolve().parent.parent
_BY_CID = {c.cid: c for c in CONCEPTS}
# Non-actual sentences (the bench bank, and the planted wished and negated traps) take only concepts whose transcript
# wording does not itself say the thing happened to the speaker: after "I wish we had had the money we borrowed" the
# label would contradict the sentence. A wording says so when it has a first-person word ("we", "our": "the money we
# borrowed", "to check our propane") or a past-tense event clause with no subject of its own ("the visit when the
# health unit checked the truck over", "the health official who came out to the truck").
_FIRST_PERSON = re.compile(r"\b(we|our)\b", re.I)
_EVENT_CLAUSE = re.compile(r"\b(who|when|where|which|that)\s+(?:\w+\s+){0,3}?"
                           r"(\w+ed|came|went|did|had|got|took|saw|made|ran|gave)\b", re.I)


def says_it_happened(wording: str) -> bool:
    return bool(_FIRST_PERSON.search(wording) or _EVENT_CLAUSE.search(wording))


FILL_CONCEPTS = tuple(c for c in CONCEPTS if not says_it_happened(c.forms[1]))
_FILL_CIDS = frozenset(c.cid for c in FILL_CONCEPTS)
FILL_REASON = "fill contradicts label"
STYLES = ("prose", "speech")
N_PLAIN = 60
N_SHUFFLED = 40
# The synthetic corpora of these seeds are pooled (every corpus has its own planted traps and sampled items).
DEFAULT_SEEDS = (0, 1, 2, 3, 4)
# Kinds with no well-defined correct modality: reported under ``by_kind`` but left out of accuracy, recall,
# calibration, the style gap and the pass rule.
#   mapping_talk: the interviewer announces what goes on the map and the participant says "Okay"; neither
#                 "actual" nor "unsupported" is clearly right.
#   workaround:   the element is the circumvented thing ("we paid a consultant to sort out X"); whether that says X
#                 happened to them is ambiguous.
#   other_person: the planted corpus sentences ("My cousin got X, but we didn't.") also deny the speaker's own
#                 experience, and under the precedence (negated before other_person) that makes the label
#                 ambiguous; the bench's own other_person templates (kind bank_other_person) avoid the denial and
#                 are scored.
#   dead_end:     the planted sentence says an attempt led nowhere; under the v2 definitions the attempt counts as
#                 experienced but the thing being sought does not, so "actual" is not clearly right for the element.
#   hypothetical: the planted corpus sentences ("If we had applied in winter, maybe X would have been faster.")
#                 assume X happened to the speaker, so "hypothetical" is not clearly right; the bench's own
#                 hypothetical templates (kind bank_hypothetical) are true possibilities and are scored.
# Besides whole kinds, single planted wished/negated items are excluded when their concept's wording says the thing
# happened to the speaker (``FILL_CONCEPTS`` below); the reason is ``FILL_REASON``.
EXCLUDED_KINDS = ("mapping_talk", "workaround", "other_person", "dead_end", "hypothetical")
EXCLUDED_REASONS = {
    "mapping_talk": "the interviewer announces what goes on the map; neither actual nor unsupported is clearly right",
    "workaround": "the element is the circumvented thing; whether the sentence says it happened to the speaker is ambiguous",
    "other_person": "the planted sentence also denies the speaker's own experience (negated outranks other_person)",
    "dead_end": "an attempt counts as experienced but the thing sought only if they say they got it; actual is not clearly the right label",
    "hypothetical": "the planted sentence assumes the thing happened to the speaker, so hypothetical is not clearly right",
}
N_BOOT = 1000          # template-cluster bootstrap resamples for the class-recall intervals

# The pass rule. These constants are the ONE source of truth for every threshold; ``pass_rule_thresholds()`` exports
# them and every consumer (audits, prereg, reports) derives from here. The bench is an automated check that reports,
# not a lock: a judge that fails a floor is replaced by the fallback judge.
PASS_ACCURACY = 0.90
PASS_CLASS_RECALL = 0.80
PASS_CLASSES = ("wished", "negated", "other_person", "hypothetical", "unsupported")
PASS_STYLE_GAP = 0.05
PASS_ALIGNMENT_F1 = 0.85
PASS_OFF_RECORD_RECALL = 0.85
PASS_NOT_JUDGED = 0.02
# Added after the audit (A6): the two errors that move the study's numbers.
#   actual recall:     experienced quotes the judge labels anything else; a judge that turns 29% of them into
#                      "wished" used to pass.
#   wished precision:  of the quotes the judge calls "wished", the share that are wished; wrong "wished" labels on
#                      experienced quotes inflate the evidence-based leakage measure.
PASS_ACTUAL_RECALL = 0.85
PASS_WISHED_PRECISION = 0.80
# Off-record is two-sided: recall of off-record labels (not flagged as mentioned) AND recall of mentioned labels
# (found in the transcript); a judge that never finds anything in the transcript used to pass. The pooled off-record
# set must have at least PASS_OFF_RECORD_MIN_N labels: ``bench`` adds corpora of further seeds until it does.
PASS_OFF_RECORD_MENTION_RECALL = 0.85
PASS_OFF_RECORD_MIN_N = 100
# Letter mode: items whose letters hold less than this share of the first token are not judged (judge.py owns the
# value, it is the extraction's own rule; the not-judged gate above counts them).
PASS_LETTER_MASS = LETTER_MASS_MIN
_EPS = 1e-9


def pass_rule_thresholds() -> dict:
    """Every threshold of the pass rule as one JSON-friendly dict, derived from the PASS_* constants."""
    return {"accuracy_min": PASS_ACCURACY, "recall_min": PASS_CLASS_RECALL, "recall_classes": list(PASS_CLASSES),
            "actual_recall_min": PASS_ACTUAL_RECALL, "wished_precision_min": PASS_WISHED_PRECISION,
            "style_gap_max": PASS_STYLE_GAP, "alignment_f1_strict_min": PASS_ALIGNMENT_F1,
            "off_record_recall_min": PASS_OFF_RECORD_RECALL,
            "off_record_mention_recall_min": PASS_OFF_RECORD_MENTION_RECALL,
            "off_record_min_n": PASS_OFF_RECORD_MIN_N, "not_judged_max": PASS_NOT_JUDGED,
            "letter_mass_min": PASS_LETTER_MASS}

_STRICT = PRIMARY_SETTING                                              # lex0.6 + judge "same"
_LENIENT = MatchSetting(PRIMARY_SETTING.lex_threshold, "same_or_close")
_AI_ONLY = MatchSetting(2.0, "same")           # lexical similarity never reaches 2.0: the judge alone decides
_LEXICAL_ONLY = MatchSetting(PRIMARY_SETTING.lex_threshold, "none")
_ALIGN_SETTINGS = (_STRICT, _LENIENT, _AI_ONLY, _LEXICAL_ONLY)


def _squash(s: str | None) -> str:
    return " ".join((s or "").split())


# ------------------------------------------------------------------ speech-ification

_FILLERS = ("um,", "you know,", "uh,")      # never "like,": it would turn "would, like, ..." into a wish cue
# A filler never follows one of these: "would um, like" style splices read as other cues to a keyword rule
# and break the verb phrase.
_MODALS = frozenset({"would", "should", "could", "might", "may", "must", "can"})


def _norm_word(w: str) -> str:
    return re.sub(r"^[^\w']+|[^\w']+$", "", w).strip("'").lower()


# Multi-word cues of ``keywords.keyword_modality``: speech never splits them (a filler inside "if only" or
# "my cousin" would hide the cue from a keyword rule, and the speech version would no longer be the same sentence
# said aloud).
_FAMILY = ("cousin", "friend", "neighbour", "neighbor", "brother", "sister", "mother", "father", "wife", "husband",
           "partner")
_CUE_PHRASES = tuple([f"my {f}{pl}" for f in _FAMILY for pl in ("", "s")]
                     + ["someone else", "another family", "another parent", "another owner", "would have",
                        "could have", "should have", "should be", "would like", "would be nice", "it'd be nice",
                        "wanted there to be", "i'd love", "if only", "no one", "did not"])


def _protected(words: list[str], wordings) -> set[int]:
    norm = [_norm_word(w) for w in words]
    out: set[int] = set()
    for wording in tuple(wordings) + _CUE_PHRASES:
        target = [_norm_word(w) for w in wording.split()]
        for i in range(len(words) - len(target) + 1):
            if target and norm[i:i + len(target)] == target:
                out.update(range(i, i + len(target)))
    return out


def _lower_first(word: str) -> str:
    return word if word == "I" or word.startswith("I'") else word[:1].lower() + word[1:]


def _modal_before(word: str) -> bool:
    w = _norm_word(word)
    return w in _MODALS or w.endswith("'d")


def speechify(sentence: str, protect=(), key: str = "") -> str:
    """Deterministic speech-like version of ``sentence``: a false start (the first 2-3 words said twice), two
    fillers ("um", "you know", "uh") and a run-on ("and then": it replaces a ";" or opens the sentence; a
    sentence that already opens with "Then" only gets "And").

    Words are only added, never removed or changed (apart from lower-casing the first word when it is no
    longer sentence-initial), and nothing is inserted into the spans of ``protect`` (the concept wordings), so
    those stay verbatim and the item remains answerable. A filler never follows a modal verb (would, 'd, should,
    could, might, may, must, can) or a comma, and never reads as a modality cue: the keyword rule gives the same
    label for the prose and the speech version of every sentence. Seeded by (``key``, ``sentence``)."""
    rng = random.Random(derive_seed("speech", key, sentence))
    words = sentence.split()
    prot = _protected(words, protect)
    toks = [[w, i in prot] for i, w in enumerate(words)]
    if not toks:
        return sentence
    # false start: "We never, we never got ..."
    k = 2 + int(rng.random() * 2)
    prefix = []
    for t in toks[:k]:
        if t[1]:
            break
        prefix.append(t)
    if len(prefix) >= 2:
        first_copy = [[w, False] for w, _ in prefix]
        first_copy[-1][0] = re.sub(r"[.,;:!?]+$", "", first_copy[-1][0]) + ","
        toks = first_copy + [[_lower_first(toks[0][0]) if not toks[0][1] else toks[0][0], toks[0][1]]] + toks[1:]
    # run-on: "and then" replaces a semicolon, or opens the sentence when there is none
    semi = next((i for i, t in enumerate(toks) if t[0].endswith(";")), None)
    if semi is not None:
        toks[semi][0] = toks[semi][0][:-1]
        nxt = _norm_word(toks[semi + 1][0]) if semi + 1 < len(toks) else ""
        glue = [["and", False]] + ([] if nxt == "then" else [["then", False]])
        toks_out = toks[:semi + 1] + glue + toks[semi + 1:]
    else:
        toks[0][0] = _lower_first(toks[0][0])           # no longer sentence-initial: "And then the ..."
        head = _norm_word(toks[0][0])
        glue = [["And", False]] + ([] if head == "then" else [["then", False]])
        toks_out = glue + toks
    # fillers at boundaries that do not cut a protected span, follow a comma or follow a modal verb
    allowed = [i for i in range(1, len(toks_out))
               if not (toks_out[i - 1][1] and toks_out[i][1]) and not toks_out[i - 1][0].endswith(",")
               and not _modal_before(toks_out[i - 1][0])]
    chosen = []
    pool = list(allowed)
    for _ in range(min(2, len(pool))):
        chosen.append(pool.pop(int(rng.random() * len(pool))))
    for i in sorted(chosen, reverse=True):
        toks_out.insert(i, [_FILLERS[int(rng.random() * len(_FILLERS))], False])
    return " ".join(w for w, _ in toks_out)


# ------------------------------------------------------------------ the modality set

def _pid_list(root: Path) -> list[str]:
    return [p["pid"] for p in json.loads((root / "meta.json").read_text(encoding="utf-8"))["participants"]]


def _fisher_yates(seq, rng: random.Random) -> list:
    items = list(seq)
    for i in range(len(items) - 1, 0, -1):
        j = int(rng.random() * (i + 1))
        items[i], items[j] = items[j], items[i]
    return items


def _ordinary_sentences() -> list[tuple[str, str, int]]:
    """[(cid, sentence, template index)] for every concept x ordinary template, as the corpus builder words them."""
    out = []
    for c in CONCEPTS:
        for k, tpl in enumerate(ORDINARY_TURNS):
            s = tpl.format(x=c.forms[1])
            out.append((c.cid, s[0].upper() + s[1:], k))
    return out


def _trap_template(kind: str, sentence: str, cids: list[str]) -> str:
    """``trap:<kind>:<index>``: which of the corpus builder's templates for ``kind`` made ``sentence``."""
    for idx, tpl in enumerate(TRAP_TEMPLATES[kind]):
        s = tpl.format(x=_BY_CID[cids[0]].forms[1], y=_BY_CID[cids[1]].forms[1] if len(cids) > 1 else "")
        if s[0].upper() + s[1:] == sentence:
            return f"trap:{kind}:{idx}"
    return f"trap:{kind}:?"


def template_label(tid: str) -> str | None:
    """The template text behind a template id (bank, trap or ordinary), for reports; None if unknown."""
    parts = tid.split(":")
    if parts[0] == "shuffled":
        return template_label(":".join(parts[1:]))
    if parts[0] == "bank":
        return template_text(tid)
    try:
        if parts[0] == "trap":
            return TRAP_TEMPLATES[parts[1]][int(parts[2])]
        if parts[0] == "plain":
            return ORDINARY_TURNS[int(parts[1])]
        if parts[0] == "iv_no_reply":
            return TRAP_TEMPLATES["interviewer_voiced"][int(parts[1])]
        if parts[0] == "iv_no":
            return IV_NO_QUESTIONS[int(parts[1])]
    except (KeyError, ValueError, IndexError):
        return None
    return None


def build_modality_set(corpus_dir, seed: int = 0, tag: str = "") -> list[dict]:
    """The labelled modality items, both styles, deterministic for a corpus and seed.

    Each item: ``element_id``, ``element`` (AI wording of the concept), ``quote``, ``speaker`` and, for an
    interviewer quote, ``reply`` (the participant's next turn) as the pipeline would pass them, plus the label
    ``expected``, ``kind`` (the trap kind, "plain" or "shuffled"), ``style``, ``pid``, ``cid`` and ``protect``
    (concept wordings that ``speechify`` leaves intact), ``seed``. Every interviewer-voiced question also appears
    twice more: with its reply removed (``iv_no_reply``, expected unsupported) and answered "No." (``iv_no``,
    expected negated). Per style: the planted trap sentences (58 in the demo corpus), 60 ordinary experienced
    turns, the paraphrase bank (``synth.bench_bank``: every template filled with ``PER_TEMPLATE`` concepts, kind
    ``bank_<class>``) and 40 unrelated element/quote pairs. ``template`` names the sentence template an item
    came from (the unit the report resamples), ``prose`` the sentence as written. ``tag`` prefixes the
    ``element_id``s so several corpora can be pooled without id clashes."""
    root = Path(corpus_dir)
    ordinary = _ordinary_sentences()
    base: list[dict] = []
    plain_pool: list[dict] = []
    for pid in _pid_list(root):
        transcript = parse_paragraphs(read_docx_text(root / "transcripts" / f"{pid}.docx"), {"pt"}, pid,
                                      interviewer_labels={"iv"})
        traps = json.loads((root / "traps" / f"{pid}.json").read_text(encoding="utf-8"))
        trap_sentences = set()
        for t in traps:
            if not t["sentence"]:
                continue           # unstated_edge and off_record live only in the maps
            trap_sentences.add(t["sentence"])
            turn = next((x for x in transcript.turns if t["sentence"] in x.text), None)
            if turn is None:
                raise ValueError(f"{pid}: trap sentence of kind {t['kind']} not found in the transcript")
            cids = t.get("cids", [t["cid"]])
            item = {"element": _BY_CID[t["cid"]].forms[2], "quote": t["sentence"], "speaker": t["speaker"],
                    "expected": t["expected_modality"], "kind": t["kind"], "pid": pid, "cid": t["cid"],
                    "protect": [_BY_CID[c].forms[1] for c in cids],
                    "template": _trap_template(t["kind"], t["sentence"], cids)}
            if t["kind"] in ("wished", "negated") and t["cid"] not in _FILL_CIDS:
                item["exclude"] = FILL_REASON         # the concept's wording says it happened to the speaker
            if t["speaker"] == "interviewer":
                reply = _next_participant_reply(transcript, turn.idx)
                if reply:
                    item["reply"] = reply
            base.append(item)
            if t["kind"] == "interviewer_voiced":
                # the same question, mentioned but never attested by the participant: without a reply the rubric
                # says unsupported; with a plain "No." it says negated
                bare = {k: v for k, v in item.items() if k != "reply"}
                idx = item["template"].rsplit(":", 1)[1]
                base.append({**bare, "kind": "iv_no_reply", "expected": "unsupported", "template": f"iv_no_reply:{idx}"})
                # the "No." items ask whether it happened (the corpus questions ask about the ORDER, and a "No."
                # to those only denies the sequence)
                q = int(idx) % len(IV_NO_QUESTIONS) if idx.isdigit() else 0
                no = {**bare, "kind": "iv_no", "expected": "negated", "reply": "No.",
                      "quote": render(IV_NO_QUESTIONS[q], _BY_CID[t["cid"]].forms[1]), "template": f"iv_no:{q}"}
                if t["cid"] not in _FILL_CIDS:
                    no["exclude"] = FILL_REASON      # "Did the account we opened happen for you?": the wording presupposes it
                base.append(no)
        for turn in transcript.turns:
            if turn.speaker != "participant" or any(s in turn.text for s in trap_sentences):
                continue
            hit = next(((cid, s, k) for cid, s, k in ordinary if turn.text.startswith(s)), None)
            if hit:
                plain_pool.append({"element": _BY_CID[hit[0]].forms[2], "quote": hit[1], "speaker": "participant",
                                   "expected": "actual", "kind": "plain", "pid": pid, "cid": hit[0],
                                   "protect": [_BY_CID[hit[0]].forms[1]], "template": f"plain:{hit[2]}"})
    if len(plain_pool) < N_PLAIN:
        raise ValueError(f"only {len(plain_pool)} ordinary statements in the corpus, need {N_PLAIN}")
    base += _fisher_yates(plain_pool, random.Random(seed))[:N_PLAIN]
    bank_items: list[dict] = []          # the paraphrase bank: each template filled with PER_TEMPLATE concepts
    for label, templates in BENCH_TEMPLATES.items():
        pool = CONCEPTS if label == "actual" else FILL_CONCEPTS
        for k, tpl in enumerate(templates):
            tid = template_id(label, k)
            for c in _fisher_yates(pool, random.Random(derive_seed("bench_bank", seed, tid)))[:PER_TEMPLATE]:
                bank_items.append({"element": c.forms[2], "quote": render(tpl, c.forms[1]), "speaker": "participant",
                                   "expected": label, "kind": f"bank_{label}", "pid": "bank", "cid": c.cid,
                                   "protect": [c.forms[1]], "template": tid})
    base += bank_items
    rng = random.Random(seed + 1)
    shuffled: list[dict] = []
    # quotes for the unrelated-pair items: ordinary turns and the bank's experienced sentences
    sources = _fisher_yates(plain_pool + [b for b in bank_items if b["expected"] == "actual"], rng)
    k = 0
    while len(shuffled) < N_SHUFFLED:
        src = sources[k % len(sources)]
        k += 1
        src_cluster = _BY_CID[src["cid"]].cluster
        cands = [c for c in CONCEPTS if c.cid != src["cid"]
                 and not (src_cluster and c.cluster == src_cluster)      # health/fire/... are not "unrelated"
                 and c.forms[1].lower() not in src["quote"].lower() and c.forms[0].lower() not in src["quote"].lower()]
        c = cands[int(rng.random() * len(cands))]
        shuffled.append({"element": c.forms[2], "quote": src["quote"], "speaker": "participant",
                         "expected": "unsupported", "kind": "shuffled", "pid": src["pid"], "cid": c.cid,
                         "protect": [src["protect"][0]], "template": f"shuffled:{src['template']}"})
        if k > 10 * len(sources) + N_SHUFFLED:
            raise ValueError("cannot build enough shuffled pairs")     # pragma: no cover
    base += shuffled
    items = []
    for n, b in enumerate(base):
        for style in STYLES:
            quote = b["quote"] if style == "prose" else speechify(b["quote"], b["protect"], f"{b['pid']}:{b['cid']}")
            base_id = f"{tag + ':' if tag else ''}{n:03d}:{b['pid']}:{b['kind']}"
            it = {**b, "quote": quote, "prose": b["quote"], "style": style, "seed": seed, "corpus": tag, "base": base_id,
                  "element_id": f"{base_id}:{style}"}
            items.append(it)
    items.sort(key=lambda it: (STYLES.index(it["style"]), it["element_id"]))
    return items


# ------------------------------------------------------------------ corpora and intervals

@contextlib.contextmanager
def corpora(corpus_dir=None, seeds=DEFAULT_SEEDS):
    """``[(tag, corpus path, seed)]``: the one corpus in ``corpus_dir`` if given (tag "c", seed 0), else the
    synthetic corpora of ``seeds``, each built by ``build_corpus`` into a temporary directory (deterministic)
    that is removed on exit."""
    if corpus_dir is not None:
        yield [("c", Path(corpus_dir), 0)]
        return
    with tempfile.TemporaryDirectory() as tmp:
        out = []
        for seed in seeds:
            d = Path(tmp) / f"seed{seed}"
            build_corpus(d, seed=seed)
            out.append((str(seed), d, seed))
        yield out


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """Wilson 95% interval [low, high] for k successes in n trials; None when n is 0."""
    if not n:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


# ------------------------------------------------------------------ metrics

def is_excluded(it: dict) -> bool:
    """Whole excluded kind, or a single item excluded for its own reason (``it["exclude"]``)."""
    return it["kind"] in EXCLUDED_KINDS or bool(it.get("exclude"))


def _sentence(it: dict) -> str:
    """The sentence an item is about, as written (its prose), whatever style it is in."""
    return it.get("prose") or it.get("quote") or it["element_id"]


def dedupe_items(items: list[dict]) -> list[dict]:
    """Keep the first of every item that repeats an earlier one: the same style, element, prose sentence and
    reply (the same sentence drawn again by another corpus seed). The pass rule counts distinct sentences."""
    seen: set = set()
    out = []
    for it in items:
        key = (it["style"], it["element"], _sentence(it), it.get("reply", ""))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def cluster_bootstrap_ci(clusters: list[tuple[int, int]], n_boot: int = N_BOOT, seed: int = 0,
                         level: float = 0.95) -> list[float] | None:
    """Percentile interval for a pooled rate when the items come in clusters (sentence templates): resample the
    clusters with replacement, ``clusters`` = [(hits, n)], and recompute hits/n each time. The unit is the
    template, because the items of one template are not independent (same wording, same cue). None for no
    clusters; a single cluster gives a point."""
    if not clusters:
        return None
    rng = random.Random(seed)
    m = len(clusters)
    rates = []
    for _ in range(n_boot):
        hits = n = 0
        for _ in range(m):
            h, k = clusters[int(rng.random() * m)]
            hits += h
            n += k
        rates.append(hits / n)
    rates.sort()
    return [rates[int((1 - level) / 2 * n_boot)], rates[min(n_boot - 1, int((1 + level) / 2 * n_boot) - 1)]]


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p for b and c discordant pairs (binomial test of b against b+c at 1/2)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def calibration(rows: list[tuple[dict[str, float], str]], bins: int = 10) -> dict:
    """ECE (equal-width bins on the top probability), Brier score of the top probability against being right,
    and the multiclass Brier score, over ``rows`` = [(distribution over the labels, expected label)]."""
    if not rows:
        return {"n": 0, "ece": None, "brier_top": None, "brier_multiclass": None}
    n = len(rows)
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    brier_top = brier_multi = 0.0
    for dist, expected in rows:
        label = max(MODALITIES, key=lambda m: dist.get(m, 0.0))
        conf = dist.get(label, 0.0)
        right = int(label == expected)
        buckets[min(int(conf * bins), bins - 1)].append((conf, right))
        brier_top += (conf - right) ** 2
        brier_multi += sum((dist.get(m, 0.0) - float(m == expected)) ** 2 for m in MODALITIES)
    ece = sum(len(b) / n * abs(sum(r for _, r in b) / len(b) - sum(c for c, _ in b) / len(b)) for b in buckets if b)
    return {"n": n, "ece": ece, "brier_top": brier_top / n, "brier_multiclass": brier_multi / n}


def score_modality(items: list[dict], labels: dict[str, str], not_judged: set[str],
                   dists: dict[str, dict[str, float]] | None = None, n_boot: int = 0) -> dict:
    """Accuracy, per-class recall, confusion matrix, per-style and per-kind accuracy, style gap and (when
    ``dists`` is given) calibration. Item-level Wilson 95% intervals (``accuracy_ci``, ``recall_ci_item``,
    ``by_style_ci``, ``by_kind_ci``) treat every item as independent; ``recall_ci`` is the template-cluster
    bootstrap interval of each class recall (``n_boot`` resamples of the sentence templates, None if 0), the one to
    read for a class. ``recall_n`` counts judged items per class, ``distinct_n`` the distinct sentences,
    ``n_templates`` the templates, and ``by_template`` gives each template's recall (templates of excluded kinds are
    listed with ``excluded`` true, the ``reason`` and recall None).

    Items of ``EXCLUDED_KINDS`` are left out of everything except ``by_kind`` (and the per-item rows, flagged
    ``excluded``). A not-judged item is left out of every accuracy and recall (and counted in
    ``n_not_judged``); ``accuracy_incl_not_judged`` scores it as wrong. ``n_all`` / ``n_not_judged_all`` count
    every item including the excluded kinds."""
    scored = [it for it in items if not is_excluded(it)]
    judged = [it for it in scored if it["element_id"] not in not_judged]
    judged_all = [it for it in items if it["element_id"] not in not_judged]
    ok = {it["element_id"]: labels[it["element_id"]] == it["expected"] for it in judged_all}

    def hits(sub):
        s = [it for it in sub if it["element_id"] in ok]
        return sum(ok[it["element_id"]] for it in s), len(s)

    def acc(sub):
        return _rate(*hits(sub))

    def ci(sub):
        return wilson(*hits(sub))
    confusion = {e: {p: 0 for p in MODALITIES} for e in MODALITIES}
    for it in judged:
        confusion[it["expected"]][labels[it["element_id"]]] += 1
    recall_n = {c: sum(confusion[c].values()) for c in MODALITIES}
    recall = {c: _rate(confusion[c][c], recall_n[c]) for c in MODALITIES}
    recall_ci_item = {c: wilson(confusion[c][c], recall_n[c]) for c in MODALITIES}
    # precision: of the scored, judged items the judge gave this label, the share that have it (column of the confusion)
    precision_n = {c: sum(confusion[e][c] for e in MODALITIES) for c in MODALITIES}
    precision = {c: _rate(confusion[c][c], precision_n[c]) for c in MODALITIES}
    precision_ci = {c: wilson(confusion[c][c], precision_n[c]) for c in MODALITIES}
    tmpl: dict[str, dict[str, list[int]]] = {c: {} for c in MODALITIES}      # class -> template -> [hits, n]
    for it in judged:
        cell = tmpl[it["expected"]].setdefault(it.get("template") or it["element_id"], [0, 0])
        cell[0] += ok[it["element_id"]]
        cell[1] += 1
    recall_ci = {c: (cluster_bootstrap_ci([tuple(v) for v in tmpl[c].values()], n_boot, derive_seed("boot", c))
                     if n_boot else None) for c in MODALITIES}
    distinct_n = {c: len({(it.get("element", it["element_id"]), _sentence(it), it.get("reply", ""))
                          for it in scored if it["expected"] == c}) for c in MODALITIES}
    by_template = {t: {"label": c, "n": v[1], "recall": v[0] / v[1], "text": template_label(t)}
                   for c in MODALITIES for t, v in sorted(tmpl[c].items())}
    for it in items:                  # excluded templates are listed, with the reason, but have no recall
        if it["kind"] in EXCLUDED_KINDS:      # (items excluded one by one stay with their scored template)
            t = it.get("template") or it["element_id"]
            cell = by_template.setdefault(t, {"label": it["expected"], "n": 0, "recall": None,
                                              "text": template_label(t), "excluded": True,
                                              "reason": EXCLUDED_REASONS[it["kind"]]})
            cell["n"] += 1
    by_style = {s: acc([it for it in scored if it["style"] == s]) for s in STYLES}
    gap = (abs(by_style["prose"] - by_style["speech"])
           if None not in by_style.values() else None)
    kinds = sorted({it["kind"] for it in items})
    pairs: dict[str, dict[str, bool]] = {}
    for it in judged:
        if it.get("base"):
            pairs.setdefault(it["base"], {})[it["style"]] = ok[it["element_id"]]
    both = [v for v in pairs.values() if len(v) == 2]
    b = sum(v["prose"] and not v["speech"] for v in both)       # right in prose, wrong in speech
    c = sum(v["speech"] and not v["prose"] for v in both)
    discordant = ({"prose_right_speech_wrong": b, "speech_right_prose_wrong": c, "n_pairs": len(both),
                   "mcnemar_exact_p": mcnemar_exact_p(b, c)} if both else None)
    out = {"n": len(scored), "n_judged": len(judged), "n_not_judged": len(scored) - len(judged),
           "n_all": len(items), "n_not_judged_all": len(items) - len(judged_all),
           "n_excluded": len(items) - len(scored), "excluded_kinds": list(EXCLUDED_KINDS),
           "excluded_items": {r: {"n": hits([it for it in items if it.get("exclude") == r])[1],
                                  "accuracy": acc([it for it in items if it.get("exclude") == r])}
                              for r in sorted({it["exclude"] for it in items if it.get("exclude")})},
           "accuracy": acc(scored), "accuracy_ci": ci(scored),
           "accuracy_incl_not_judged": _rate(sum(ok[it["element_id"]] for it in judged), len(scored)),
           "recall": recall, "recall_n": recall_n, "recall_ci": recall_ci, "recall_ci_item": recall_ci_item,
           "precision": precision, "precision_n": precision_n, "precision_ci": precision_ci,
           "distinct_n": distinct_n, "n_templates": {c: len(tmpl[c]) for c in MODALITIES},
           "by_template": by_template, "confusion": confusion,
           "by_style": by_style, "by_style_ci": {s: ci([it for it in scored if it["style"] == s]) for s in STYLES},
           "style_gap": gap, "style_discordant": discordant,
           "by_kind": {k: acc([it for it in items if it["kind"] == k]) for k in kinds},
           "by_kind_n": {k: hits([it for it in items if it["kind"] == k])[1] for k in kinds},
           "by_kind_ci": {k: ci([it for it in items if it["kind"] == k]) for k in kinds},
           "by_kind_excluded_n": {k: hits([it for it in items if it["kind"] == k and is_excluded(it)])[1]
                                  for k in kinds},
           "by_kind_scored": {k: acc([it for it in items if it["kind"] == k and not is_excluded(it)])
                              for k in kinds},
           "items": [{"id": it["element_id"], "expected": it["expected"], "kind": it["kind"], "style": it["style"],
                      "pred": labels[it["element_id"]], "judged": it["element_id"] not in not_judged,
                      "excluded": is_excluded(it)}
                     for it in items]}
    if dists is not None:
        out["calibration"] = calibration([(dists[it["element_id"]], it["expected"]) for it in judged
                                          if it["element_id"] in dists])
    return out


def pass_rule(results: dict, *, check_pool_size: bool = True) -> tuple[bool, list[str]]:
    """The pre-registered rule. ``results`` is the dict ``bench`` returns. Returns (passed, reasons for each
    failed criterion; a metric that could not be computed fails its criterion). The off-record pool floor is always
    ``PASS_OFF_RECORD_MIN_N``, never a value read from ``results``; only the per-seed rows of a pooled bench, which
    are one corpus each and not the pooled set, pass ``check_pool_size=False``."""
    reasons: list[str] = []

    def below(name, value, floor):
        if value is None:
            reasons.append(f"{name} could not be computed")
        elif value < floor - _EPS:
            reasons.append(f"{name} {value:.3f} < {floor:g}")
    m = results["modality"]
    below("modality accuracy", m["accuracy"], PASS_ACCURACY)
    below("recall on actual", m["recall"].get("actual"), PASS_ACTUAL_RECALL)
    for c in PASS_CLASSES:
        below(f"recall on {c}", m["recall"].get(c), PASS_CLASS_RECALL)
    below("precision on wished", (m.get("precision") or {}).get("wished"), PASS_WISHED_PRECISION)
    if m["style_gap"] is None:
        reasons.append("style gap could not be computed")
    elif m["style_gap"] > PASS_STYLE_GAP + _EPS:
        reasons.append(f"style gap {m['style_gap']:.3f} > {PASS_STYLE_GAP:g}")
    below("alignment F1 (strict)", results["alignment"]["strict"]["f1"], PASS_ALIGNMENT_F1)
    off = results["off_record"]
    below("off-record recall", off["recall"], PASS_OFF_RECORD_RECALL)
    below("off-record mention recall", off.get("mention_recall"), PASS_OFF_RECORD_MENTION_RECALL)
    n_off = off.get("n_off_record")
    if not check_pool_size:
        pass
    elif n_off is None:
        reasons.append("off-record set size could not be computed")
    elif n_off < PASS_OFF_RECORD_MIN_N:
        why = " (single-corpus run: it cannot select a judge)" if off.get("min_n") == 0 else ""
        reasons.append(f"off-record set has {n_off} labels, fewer than {PASS_OFF_RECORD_MIN_N}{why}")
    nj = results["not_judged_rate"]
    if nj is None:
        reasons.append("not-judged rate could not be computed")
    elif nj > PASS_NOT_JUDGED + _EPS:
        reasons.append(f"not-judged rate {nj:.3f} > {PASS_NOT_JUDGED:g}")
    return not reasons, reasons


def both_wrong(results_judge1: dict, results_judge2: dict) -> dict:
    """Per trap class (item ``kind``) and overall: how often two judges agree AND are both wrong. Reads
    ``results["modality"]["items"]`` of two ``bench`` runs, which must be on the same item set (ValueError if the
    id sets differ). Items judged by both only. Per class: ``n``, ``n_agree`` (same label), ``both_wrong_agree``
    (same wrong label), ``share`` (of n) and ``share_of_agreements_wrong`` (of the agreements; None if they
    never agree). ``all`` leaves out the excluded kinds (they are still listed under their own kind); items excluded
    one by one (``FILL_REASON``) are left out of their class row and of ``all``."""
    a_items, b_items = results_judge1["modality"]["items"], results_judge2["modality"]["items"]
    ids1, ids2 = {r["id"] for r in a_items}, {r["id"] for r in b_items}
    if ids1 != ids2:
        raise ValueError(f"the two results are not on the same items ({len(ids1 ^ ids2)} ids differ)")
    by_id = {r["id"]: r for r in b_items if r["judged"]}
    rows: dict[str, list[tuple[bool, bool]]] = {}
    for a in a_items:
        b = by_id.get(a["id"])
        if not a["judged"] or b is None:
            continue
        agree = a["pred"] == b["pred"]
        row = (agree, agree and a["pred"] != a["expected"])
        # an item excluded one by one (inside a scored kind) leaves its class row too; the excluded KINDS keep
        # their own rows, as the docstring says
        if not a.get("excluded") or a["kind"] in EXCLUDED_KINDS:
            rows.setdefault(a["kind"], []).append(row)
        if not a.get("excluded"):
            rows.setdefault("all", []).append(row)
    return {k: {"n": len(v), "n_agree": sum(g for g, _ in v), "both_wrong_agree": sum(w for _, w in v),
                "share": sum(w for _, w in v) / len(v),
                "share_of_agreements_wrong": _rate(sum(w for _, w in v), sum(g for g, _ in v))}
            for k, v in sorted(rows.items())}


# ------------------------------------------------------------------ clients that need no model

def _numbered(block: str) -> list[tuple[int, str]]:
    return [(int(m[1]), m[2]) for m in (re.match(r"(\d+)\. (.*)$", l) for l in block.splitlines()) if m]


_GOLD = {c.forms[2]: c.cid for c in CONCEPTS}
_ENDORSED = {c.forms[0]: c.cid for c in CONCEPTS}
_SAID = {c.cid: c.forms[1].lower() for c in CONCEPTS}
_ITEM_RE = re.compile(r'^(\d+)\. ELEMENT: (.*)\n   QUOTE(?: \((?:participant|interviewer)\))?: "(.*)"'
                      r'(?:\n   REPLY \(participant\): "(.*)")?$', re.M)
_LETTER_ITEM_RE = re.compile(r'^ELEMENT: (.*)\nQUOTE(?: \((?:participant|interviewer)\))?: "(.*)"'
                             r'(?:\nREPLY \(participant\): "(.*)")?$', re.M)


class NullClient:
    """A judge that proposes nothing: valid answers with no alignment pairs and no quotes, so only the lexical
    rules decide. Used for the lexical-only floor. It never answers a modality prompt."""

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        p = messages[-1]["content"]
        if "LIST A:" in p:
            text = '{"pairs": []}'
        elif "TRANSCRIPT:" in p:
            text = json.dumps({"answers": [{"n": n, "quote": None} for n, _ in _numbered(p.split("LABELS:\n")[1])]})
        else:
            raise KeyError("NullClient answers only helper prompts")
        return ModelResponse(text, "stop", {}, 0, "null")


class OracleClient:
    """Answers from the planted truth (the self-test of the bench: it must score 1.0 everywhere).

    Modality prompts (batched or one-letter) are answered from the benchmark items' expected labels; the
    alignment and off-record prompts from the concept bank. ``fixed_label`` makes it answer that label for every
    modality item instead (a judge that always says the same thing)."""

    def __init__(self, corpus_dir=None, seeds=DEFAULT_SEEDS, fixed_label: str | None = None,
                 with_logprobs: bool = True):
        """``corpus_dir`` (one corpus) or ``seeds`` (pooled corpora, the default): the item sets it knows."""
        self.truth: dict[tuple[str, str, str], str] = {}
        with corpora(corpus_dir, seeds) as cs:
            for tag, path, seed in cs:
                for it in build_modality_set(path, seed, tag):
                    reply = " ".join(it.get("reply", "").split()[:60]) if it["speaker"] == "interviewer" else ""
                    key = (_squash(it["element"]), _squash(it["quote"]), reply)
                    if self.truth.setdefault(key, it["expected"]) != it["expected"]:
                        raise ValueError(f"two items share {key!r} with different expected labels")
        self.fixed_label = fixed_label
        self.with_logprobs = with_logprobs
        self._null = NullClient()

    def _label(self, element: str, quote: str, reply: str = "") -> str:
        return self.fixed_label or self.truth[(_squash(element), _squash(quote), _squash(reply))]

    def complete(self, messages, *, temperature, seed, max_tokens, params=None) -> ModelResponse:
        p = messages[-1]["content"]
        if "ITEMS:\n" in p:
            body = p.split("ITEMS:\n", 1)[1].split("\nREMINDER:")[0]
            text = json.dumps({"labels": [{"n": int(n), "modality": self._label(e, q, r)}
                                          for n, e, q, r in _ITEM_RE.findall(body)]})
            return ModelResponse(text, "stop", {}, 0, "oracle")
        if "\nITEM:\n" in p:
            body = p.split("\nITEM:\n", 1)[1].split("\nREMINDER:")[0]
            e, q, r = _LETTER_ITEM_RE.search(body).groups()
            letter = "ABCDEF"[MODALITIES.index(self._label(e, q, r or ""))]
            other = "ABCDEF"[("ABCDEF".index(letter) + 1) % 6]
            top = [[letter, -0.01], [other, -5.0]] if self.with_logprobs else None
            return ModelResponse(letter, "length", {}, 0, "oracle", logprobs=top)
        if "LIST A:" in p:
            a = _numbered(p.split("LIST A:\n")[1].split("LIST B:\n")[0])
            b = _numbered(p.split("LIST B:\n")[1])
            return ModelResponse(json.dumps({"pairs": [{"a": i, "b": j, "rel": "same"} for i, x in a for j, y in b
                                                       if _GOLD[x] == _ENDORSED[y]]}), "stop", {}, 0, "oracle")
        if "TRANSCRIPT:" in p:
            lines = p.split("TRANSCRIPT:\n")[1].split("\nLABELS:\n")[0].splitlines()
            answers = []
            for n, label in _numbered(p.split("LABELS:\n")[1]):
                quote = next((t.split("] ", 1)[1] for t in lines if _SAID[_ENDORSED[label]] in t.lower()), None)
                answers.append({"n": n, "quote": quote})
            return ModelResponse(json.dumps({"answers": answers}), "stop", {}, 0, "oracle")
        raise KeyError("OracleClient does not recognise this prompt")


# ------------------------------------------------------------------ the benchmark

def _call_stats(log_path, start_seq: int, one_token: bool = False) -> dict:
    lat, comp, cost, n = [], 0, 0.0, 0
    have_tokens = have_cost = False
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec["seq"] < start_seq or rec["record_type"] != "call_response":
                continue
            pl = rec["payload"]
            n += 1
            lat.append(pl["latency_ms"])
            usage = pl.get("usage") or {}
            if isinstance(usage.get("completion_tokens"), (int, float)):
                comp += usage["completion_tokens"]
                have_tokens = True
            if isinstance(usage.get("cost"), (int, float)):
                cost += usage["cost"]
                have_cost = True
    secs = sum(lat) / 1000
    # one-token calls: tokens/s says nothing about speed (the answer is one token); calls/s is the figure
    return {"calls": n, "median_latency_ms": statistics.median(lat) if lat else None,
            "calls_per_s": n / secs if secs > 0 else None,
            "tokens_per_s": comp / secs if have_tokens and secs > 0 and not one_token else None,
            "cost_usd": cost if have_cost else None}


_SETTING_NAMES = (("strict", _STRICT), ("lenient", _LENIENT), ("ai_only", _AI_ONLY), ("lexical_only", _LEXICAL_ONLY))


def _pool(parts: list[dict]) -> dict:
    """Sum nested dicts of counts key by key."""
    out: dict = {}
    for part in parts:
        for k, v in part.items():
            out[k] = _pool([out.get(k, {}), v]) if isinstance(v, dict) else out.get(k, 0) + v
    return out


def _align_block(c: dict) -> dict:
    """Precision / recall / F1 of matched pairs from counts, Wilson intervals on precision and recall (F1 is not a
    proportion: it has no interval, see the per-seed spread)."""
    return {**_prf(c["tp"], c["n_pred"], c["n_truth"]), "tp": c["tp"], "n_truth": c["n_truth"],
            "precision_ci": wilson(c["tp"], c["n_pred"]), "recall_ci": wilson(c["tp"], c["n_truth"])}


def _off_block(c: dict) -> dict:
    """Off-record as the positive class: a label is off-record when the transcript does not mention it.
    recall = share of off-record labels flagged; precision = share of flagged labels that are off-record."""
    tn, fp, fn, tp = c["tn"], c["fp"], c["fn"], c["tp"]
    return {"precision": _rate(tn, tn + fn), "precision_ci": wilson(tn, tn + fn),
            "recall": _rate(tn, tn + fp), "recall_ci": wilson(tn, tn + fp), "n_off_record": tn + fp,
            "counts": dict(c), "mention_recall": _rate(tp, tp + fn), "mention_recall_ci": wilson(tp, tp + fn),
            "n_mentioned": tp + fn, "false_mention_rate": _rate(fp, fp + tn)}


def _assemble(items, labels, not_judged, dists, align_counts: dict, off_counts: dict, n_boot: int = 0) -> dict:
    """The part of a results dict that ``pass_rule`` reads, for one set of items and counts (pooled, one seed,
    or the no-AI floor). Only the pooled set gets the bootstrap intervals (``n_boot``)."""
    m = score_modality(items, labels, not_judged, dists, n_boot)
    n, nj = m["n_all"], m["n_not_judged_all"]
    return {"modality": m, "not_judged_rate": _rate(nj, n), "not_judged_rate_ci": wilson(nj, n),
            "alignment": {name: _align_block(c) for name, c in align_counts.items()},
            "off_record": _off_block(off_counts)}


def _floor(corpus_list, items: list[dict]) -> dict:
    """The no-AI arm: keyword-only modality, lexical-only matching, lexical-only off-record (a judge that
    proposes nothing), pooled over the corpora."""
    kw = {it["element_id"]: keyword_modality(it["quote"]) for it in items}
    align, off = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for tag, path, _seed in corpus_list:
            cal = calibrate_synthetic(path, AuditLog(Path(tmp) / f"floor{tag}.jsonl", "floor"), NullClient(),
                                      settings=(_LEXICAL_ONLY,))
            align.append({"strict": cal["matcher_counts"][_LEXICAL_ONLY.key()]})
            off.append(cal["in_transcript_counts"])
    return _assemble(items, kw, set(), None, _pool(align), _pool(off))


def _arms(ai: dict, floor: dict) -> dict:
    """Which pass-rule criteria hold under keyword-only / lexical-only (no AI), AI-only and combined (AI with the
    lexical floor). None = the arm does not exist for that criterion (modality has no combined arm; the
    off-record rule has no AI-only arm because its lexical fallback is part of the detector)."""
    def ok(v, floor_, upper=False):
        return None if v is None else (v <= floor_ + _EPS if upper else v >= floor_ - _EPS)
    fm, am = floor["modality"], ai["modality"]
    rows = {"modality accuracy": (fm["accuracy"], am["accuracy"], None, PASS_ACCURACY, False),
            "style gap": (fm["style_gap"], am["style_gap"], None, PASS_STYLE_GAP, True)}
    rows["recall on actual"] = (fm["recall"]["actual"], am["recall"]["actual"], None, PASS_ACTUAL_RECALL, False)
    for c in PASS_CLASSES:
        rows[f"recall on {c}"] = (fm["recall"][c], am["recall"][c], None, PASS_CLASS_RECALL, False)
    rows["precision on wished"] = (fm["precision"]["wished"], am["precision"]["wished"], None,
                                   PASS_WISHED_PRECISION, False)
    rows["alignment F1 (strict)"] = (floor["alignment"]["strict"]["f1"], ai["alignment"]["ai_only"]["f1"],
                                     ai["alignment"]["strict"]["f1"], PASS_ALIGNMENT_F1, False)
    rows["off-record recall"] = (floor["off_record"]["recall"], None, ai["off_record"]["recall"],
                                 PASS_OFF_RECORD_RECALL, False)
    rows["off-record mention recall"] = (floor["off_record"]["mention_recall"], None,
                                         ai["off_record"]["mention_recall"], PASS_OFF_RECORD_MENTION_RECALL, False)
    return {name: {"keyword_only": ok(k, thr, up), "ai_only": ok(a, thr, up), "combined": ok(c, thr, up),
                   "values": {"keyword_only": k, "ai_only": a, "combined": c}}
            for name, (k, a, c, thr, up) in rows.items()}


MAX_EXTRA_OFF_RECORD_CORPORA = 100      # safety cap on the corpora added to reach PASS_OFF_RECORD_MIN_N


def _grow_off_record(log, client, seeds, counts: dict, min_n: int) -> tuple[dict, list[int]]:
    """Off-record counts of extra corpora (seeds after the benched ones, one at a time) until the pooled off-record
    set (``counts`` plus the extras) has ``min_n`` labels. Only the off-record check is kept from these corpora:
    their modality items and alignment pairs are not part of the bench. Returns (extra counts, extra seeds)."""
    extra = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    used: list[int] = []
    nxt = max(seeds) + 1
    with tempfile.TemporaryDirectory() as tmp:
        while counts["tn"] + counts["fp"] + extra["tn"] + extra["fp"] < min_n and len(used) < MAX_EXTRA_OFF_RECORD_CORPORA:
            d = Path(tmp) / f"seed{nxt}"
            build_corpus(d, seed=nxt)
            cal = calibrate_synthetic(d, log, client, settings=(_LEXICAL_ONLY,))
            for k, v in cal["in_transcript_counts"].items():
                extra[k] += v
            used.append(nxt)
            nxt += 1
    return extra, used


def order_check(items: list[dict], fwd: tuple[dict, set], rev: tuple[dict, set]) -> dict:
    """Label-position bias (R58): the same items judged with the labels listed forward and reversed.
    ``fwd`` / ``rev`` are ``(labels, not_judged_ids)``. Reports, over the items judged in BOTH orders: the share
    that got the same label (``agreement``, Wilson 95%), accuracy in each order on the scored items (items of
    ``EXCLUDED_KINDS`` and single excluded items left out, as everywhere) with the McNemar exact p of the
    right-in-one-order-only pairs, the share of predictions equal to the label listed FIRST in that order (actual when
    forward, unsupported when reversed), and the agreement per expected class. A position-free judge has agreement
    near 1 and equal first-listed shares."""
    (lf, nf), (lr, nr) = fwd, rev
    both = [it for it in items if it["element_id"] not in nf and it["element_id"] not in nr]
    scored = [it for it in both if not is_excluded(it)]
    same = [lf[it["element_id"]] == lr[it["element_id"]] for it in both]
    ok_f = [lf[it["element_id"]] == it["expected"] for it in scored]
    ok_r = [lr[it["element_id"]] == it["expected"] for it in scored]
    b, c = sum(f and not r for f, r in zip(ok_f, ok_r)), sum(r and not f for f, r in zip(ok_f, ok_r))
    first = {"forward": MODALITIES[0], "reversed": MODALITIES[-1]}
    by_class = {}
    for cls in MODALITIES:
        sub = [lf[it["element_id"]] == lr[it["element_id"]] for it in scored if it["expected"] == cls]
        if sub:
            by_class[cls] = {"n": len(sub), "agreement": sum(sub) / len(sub)}
    return {"n_items": len(items), "n_judged_both": len(both),
            "n_not_judged_either": len(items) - len(both), "n_disagree": len(same) - sum(same),
            "agreement": _rate(sum(same), len(same)), "agreement_ci": wilson(sum(same), len(same)),
            "n_scored": len(scored), "accuracy_forward": _rate(sum(ok_f), len(scored)),
            "accuracy_reversed": _rate(sum(ok_r), len(scored)),
            "right_forward_only": b, "right_reversed_only": c, "mcnemar_exact_p": mcnemar_exact_p(b, c),
            "first_listed": first,
            "pred_first_listed_share": {
                "forward": _rate(sum(lf[it["element_id"]] == first["forward"] for it in scored), len(scored)),
                "reversed": _rate(sum(lr[it["element_id"]] == first["reversed"] for it in scored), len(scored))},
            "agreement_by_expected": by_class}


def bench(client, prompts_dir, corpus_dir, log, *, mode: str = "generative", seeds=DEFAULT_SEEDS,
          cache=None, label_order: str = "forward") -> dict:
    """Run the benchmark with ``client`` as the judge. ``mode``: "generative" or "logprob" (modality only; the
    alignment and off-record calls are the same in both).

    Corpora: with ``corpus_dir`` that one corpus is benched; with ``corpus_dir=None`` the synthetic corpora of
    ``seeds`` (default 0-4) are built into temporary directories and POOLED: the pass rule is evaluated on the
    pooled counts, every rate carries a Wilson 95% interval, and ``per_seed`` shows each corpus on its own.

    Everything is written to ``log`` (an AuditLog); the timing figures cover only this run's model calls.
    Returns the results dict: ``mode``, ``seeds``, ``n_items``, ``modality`` (items of ``EXCLUDED_KINDS`` are
    not scored), ``not_judged_rate``, ``alignment`` (strict, lenient, ai_only, lexical_only), ``off_record``,
    ``per_seed``, ``judge_stats``, ``timing``, ``keyword_floor``, ``arms``, ``pass`` ({"passed", "reasons"}).

    ``label_order`` (R58, generative mode only): "forward" (default, the study's prompt), "reversed" (the whole bench
    is scored with the labels and reference examples listed backwards) or "both" (the scored results are the forward
    ones; the same items are judged once more with the reversed card, after the forward timing is taken, and
    ``results["order_check"]`` reports the agreement between the two orders, see ``order_check``)."""
    if mode not in ("generative", "logprob"):
        raise ValueError(f"mode must be generative or logprob, got {mode!r}")
    if label_order not in ("forward", "reversed", "both"):
        raise ValueError(f"label_order must be forward, reversed or both, got {label_order!r}")
    if mode == "logprob" and label_order != "forward":
        raise ValueError("label_order applies to the generative judge (the pipeline's mode); the letter mode has "
                         "fixed letters")
    primary = "reversed" if label_order == "reversed" else "forward"
    start = log.head()[0]
    with corpora(corpus_dir, seeds) as cs:
        items_by: dict[str, list[dict]] = {}
        labels: dict[str, str] = {}
        dists: dict[str, dict[str, float]] = {}
        not_judged: set[str] = set()
        stats: dict = {}
        built = dedupe_items([it for tag, path, seed in cs for it in build_modality_set(path, seed, tag)])
        for tag, path, seed in cs:
            items_by[tag] = [it for it in built if it["corpus"] == tag]
            for style in STYLES:
                # shuffled before batching: items are generated in blocks of one expected label, and a batch of
                # one label would let the judge's answers lean on its neighbours
                group = _fisher_yates([it for it in items_by[tag] if it["style"] == style],
                                      random.Random(derive_seed(seed, style)))
                st: dict = {}
                if mode == "generative":
                    lab, nj = judge_modality_detailed(log, client, group, prompts_dir, item_id=f"bench:{tag}:{style}",
                                                      cache=cache, stats=st, label_order=primary)
                else:
                    lab, dist, nj = judge_modality_logprob(log, client, group, prompts_dir,
                                                           item_id=f"bench:{tag}:{style}", cache=cache, stats=st)
                    dists.update(dist)
                for k, v in st.items():
                    stats[k] = stats.get(k, [] if isinstance(v, list) else 0) + v
                labels.update(lab)
                not_judged |= nj
        one_token = mode == "logprob"
        timing = _call_stats(log.path, start, one_token)           # before the helper calls below add theirs
        rev_pass = None
        if label_order == "both":      # the same groups (same seeded shuffles), the labels listed backwards
            rl: dict[str, str] = {}
            rnj: set[str] = set()
            for tag, path, seed in cs:
                for style in STYLES:
                    group = _fisher_yates([it for it in items_by[tag] if it["style"] == style],
                                          random.Random(derive_seed(seed, style)))
                    lab, nj = judge_modality_detailed(log, client, group, prompts_dir,
                                                      item_id=f"bench:{tag}:{style}:reversed", cache=cache,
                                                      label_order="reversed")
                    rl.update(lab)
                    rnj |= nj
            rev_pass = (rl, rnj)
        align_by, off_by = {}, {}
        for tag, path, _seed in cs:
            cal = calibrate_synthetic(path, log, client, settings=_ALIGN_SETTINGS)
            align_by[tag] = {name: cal["matcher_counts"][s.key()] for name, s in _SETTING_NAMES}
            off_by[tag] = cal["in_transcript_counts"]
        pooled_off = _pool(list(off_by.values()))
        if corpus_dir is None:
            extra_off, extra_seeds = _grow_off_record(log, client, seeds, pooled_off, PASS_OFF_RECORD_MIN_N)
            min_n = PASS_OFF_RECORD_MIN_N
        else:                          # one corpus on purpose: its few off-record labels are not grown
            extra_off, extra_seeds, min_n = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}, [], 0
        pooled_off = _pool([pooled_off, extra_off])
        timing_all = _call_stats(log.path, start, one_token)
        all_items = [it for tag, _p, _s in cs for it in items_by[tag]]
        use_dists = dists if mode == "logprob" else None
        results = _assemble(all_items, labels, not_judged, use_dists, _pool(list(align_by.values())),
                            pooled_off, N_BOOT)
        results["off_record"].update({"min_n": min_n, "extra_seeds": extra_seeds, "extra_counts": extra_off})
        results["label_order"] = label_order
        if rev_pass is not None:
            results["order_check"] = order_check(all_items, (labels, not_judged), rev_pass)
        results.update({"mode": mode, "seeds": [t for t, _p, _s in cs], "n_items": len(all_items),
                        "judge_stats": {k: v for k, v in stats.items() if k != "unparsed_items"},
                        "timing": {"modality": timing, "all": timing_all}})
        if mode == "generative":
            results["unparsed_rate"] = len(stats.get("unparsed_items", [])) / len(all_items)
        else:
            n_mass = stats.get("letter_mass_n", 0)
            results["letter_mass"] = {"mean": stats.get("letter_mass_sum", 0.0) / n_mass if n_mass else None,
                                      "n_calls": n_mass, "floor": PASS_LETTER_MASS,
                                      "n_low_mass_items": stats.get("low_letter_mass", 0)}
        per_seed = {}
        for tag, _p, _s in cs:
            one = _assemble(items_by[tag], labels, not_judged, use_dists, align_by[tag], off_by[tag])
            one["modality"].pop("items")
            one["modality"].pop("by_template")
            one["pass"] = dict(zip(("passed", "reasons"),
                                   pass_rule(one, check_pool_size=False)))     # the pool size belongs to the pooled set
            per_seed[tag] = one
        results["per_seed"] = per_seed
        results["keyword_floor"] = _floor(cs, all_items)
    results["arms"] = _arms(results, results["keyword_floor"])
    passed, reasons = pass_rule(results)
    results["pass"] = {"passed": passed, "reasons": reasons}
    return results


# ------------------------------------------------------------------ report

def c3_floors(results: dict) -> list[dict]:
    """The C3 floors the pipeline's judge must clear, one row each, ``in_transcript`` (the off-record check) FIRST: it
    sets the denominator of every lead test (R57). Row: ``{"floor", "value", "rule", "ok", "shown"}``, ``ok`` None
    when the value could not be computed. The thresholds are the PASS_* constants; this only reads ``results``."""
    off, m = results.get("off_record") or {}, results.get("modality") or {}

    def row(name, value, floor, fmt=None):
        ok = None if value is None else value >= floor - _EPS
        return {"floor": name, "value": value, "rule": f">= {floor:g}", "ok": ok, "shown": (fmt or _f)(value)}
    return [row("in_transcript: off-record set size (labels, pooled)", off.get("n_off_record"),
                PASS_OFF_RECORD_MIN_N, lambda v: "n/a" if v is None else str(v)),
            row("in_transcript: off-record mention recall", off.get("mention_recall"), PASS_OFF_RECORD_MENTION_RECALL),
            row("in_transcript: off-record recall", off.get("recall"), PASS_OFF_RECORD_RECALL),
            row("modality: recall on actual", (m.get("recall") or {}).get("actual"), PASS_ACTUAL_RECALL),
            row("modality: precision on wished", (m.get("precision") or {}).get("wished"), PASS_WISHED_PRECISION)]


def _f(v, nd=3):
    return "n/a" if v is None else f"{v:.{nd}f}"


def _pf(v, ci, nd=3):
    """Value with its Wilson interval: ``0.912 [0.88, 0.94]``."""
    return _f(v, nd) if v is None or ci is None else f"{v:.{nd}f} [{ci[0]:.2f}, {ci[1]:.2f}]"


def _disc(m: dict) -> str:
    d = m.get("style_discordant")
    return "n/a" if not d else (f"{d['prose_right_speech_wrong']} / {d['speech_right_prose_wrong']} "
                                f"(p={d['mcnemar_exact_p']:.3f}, {d['n_pairs']} pairs)")


def _off_record_note(o: dict) -> str:
    if o.get("extra_seeds"):
        seeds = o["extra_seeds"]
        return (f"Off-record set: {o['n_off_record']} labels (needs {o['min_n']}): the benched corpora plus extra corpora "
                f"of seeds {seeds[0]}-{seeds[-1]} ({len(seeds)} corpora, used for the off-record check only, not for "
                f"modality or alignment).")
    if o.get("min_n"):
        return f"Off-record set: {o['n_off_record']} labels (needs {o['min_n']}; no extra corpora were needed or possible)."
    return (f"Off-record set: {o['n_off_record']} labels from a single corpus; the pool is not grown to "
            f"{PASS_OFF_RECORD_MIN_N} when one corpus is benched on purpose, so this run cannot select a judge.")


def render_markdown(results: dict, title: str = "Judge benchmark (synthetic traps)") -> str:
    m, a, o = results["modality"], results["alignment"], results["off_record"]
    t = results["timing"]["modality"]
    out = [f"# {title}", "", f"Mode: {results['mode']}. Corpora pooled: {', '.join(results['seeds'])}. "
           f"Items: {m['n_all']} ({m['n_excluded']} of kind {', '.join(m['excluded_kinds'])} are excluded from "
           f"accuracy, recall and the pass rule and listed by kind below; {m['n_judged']} of the other {m['n']} "
           f"judged). Class recalls carry a template-cluster bootstrap 95% interval (the sentence templates are resampled; "
           f"n is the distinct sentences, not the items); accuracy, style and kind rates carry item-level Wilson "
           f"intervals. "
           f"Synthetic recovery is a unit test of the code, not evidence of validity.", "",
           f"**Pass rule (pooled): {'PASS' if results['pass']['passed'] else 'FAIL'}**"]
    out += [f"- {r}" for r in results["pass"]["reasons"]]
    out += ["", f"## C3 floors (label order: {results.get('label_order', 'forward')}; the in_transcript rows come first, "
                f"they set the denominator of every lead test)", "",
            "| Floor | Value | Rule | |", "|---|---|---|---|"]
    out += [f"| {r['floor']} | {r['shown']} | {r['rule']} | "
            f"{'n/a' if r['ok'] is None else 'ok' if r['ok'] else 'BELOW'} |" for r in c3_floors(results)]
    out += ["", "| Measure | Value [95% CI] | Rule |", "|---|---|---|",
            f"| Modality accuracy (item-level Wilson) | {_pf(m['accuracy'], m['accuracy_ci'])} | >= {PASS_ACCURACY} |"]
    out += [f"| Recall, {c} (n={m['distinct_n'][c]} sentences, {m['n_templates'][c]} templates; cluster bootstrap) | "
            f"{_pf(m['recall'][c], m['recall_ci'][c])} | >= {PASS_CLASS_RECALL} |" for c in PASS_CLASSES]
    out += [f"| Recall, actual (n={m['distinct_n']['actual']} sentences, {m['n_templates']['actual']} templates; "
            f"cluster bootstrap) | {_pf(m['recall']['actual'], m['recall_ci']['actual'])} | >= {PASS_ACTUAL_RECALL} |",
            f"| Precision, wished (n={m['precision_n']['wished']} judged as wished; item-level Wilson) | "
            f"{_pf(m['precision']['wished'], m['precision_ci']['wished'])} | >= {PASS_WISHED_PRECISION} |",
            f"| Accuracy, prose | {_pf(m['by_style']['prose'], m['by_style_ci']['prose'])} | |",
            f"| Accuracy, speech | {_pf(m['by_style']['speech'], m['by_style_ci']['speech'])} | |",
            f"| Style gap | {_f(m['style_gap'])} | <= {PASS_STYLE_GAP} |",
            f"| Style pairs: right in prose only / right in speech only (McNemar exact p) | {_disc(m)} | reported |",
            f"| Alignment F1, strict (no interval) | {_f(a['strict']['f1'])} | >= {PASS_ALIGNMENT_F1} |",
            f"| Alignment precision, strict | {_pf(a['strict']['precision'], a['strict']['precision_ci'])} | reported |",
            f"| Alignment recall, strict | {_pf(a['strict']['recall'], a['strict']['recall_ci'])} | reported |",
            f"| Alignment P / R / F1, lenient | {_f(a['lenient']['precision'])} / {_f(a['lenient']['recall'])} / "
            f"{_f(a['lenient']['f1'])} | reported |",
            f"| Off-record recall (n={o['n_off_record']}) | {_pf(o['recall'], o['recall_ci'])} | "
            f">= {PASS_OFF_RECORD_RECALL} |",
            f"| Off-record mention recall (n={o['n_mentioned']} mentioned labels) | "
            f"{_pf(o['mention_recall'], o['mention_recall_ci'])} | >= {PASS_OFF_RECORD_MENTION_RECALL} |",
            f"| Off-record precision | {_pf(o['precision'], o['precision_ci'])} | reported |",
            f"| Not-judged rate (all items) | {_pf(results['not_judged_rate'], results['not_judged_rate_ci'])} "
            f"| <= {PASS_NOT_JUDGED} |"]
    if "unparsed_rate" in results:
        out.append(f"| JSON parse failure rate (unparsed items) | {_f(results['unparsed_rate'])} | reported |")
    lm = results.get("letter_mass")
    if lm:
        out.append(f"| Mean letter mass (A-F share of the first token, {lm['n_calls']} calls; items below "
                   f"{lm['floor']:g}: {lm['n_low_mass_items']}, counted as not judged) | {_f(lm['mean'])} | reported |")
    cal = m.get("calibration")
    if cal:
        out += [f"| ECE (10 bins) | {_f(cal['ece'])} | reported |",
                f"| Brier, top probability | {_f(cal['brier_top'])} | reported |"]
    out += [f"| Median latency (ms) | {_f(t['median_latency_ms'], 0)} | reported |",
            f"| Calls per second | {_f(t['calls_per_s'], 2)} | reported |",
            f"| Tokens per second (generative only) | {_f(t['tokens_per_s'], 1)} | reported |",
            f"| Cost (USD, if reported) | {_f(t['cost_usd'], 4)} | reported |", "",
            "", _off_record_note(o), ""]
    oc = results.get("order_check")
    if oc:
        pf = oc["pred_first_listed_share"]
        out += ["## Label-order check (the same items, labels listed forward and reversed)", "",
                f"Items judged in both orders: {oc['n_judged_both']} of {oc['n_items']}. A judge that does not lean on "
                f"the position of a label agrees with itself; the share of answers equal to the label listed first "
                f"(forward: {oc['first_listed']['forward']}, reversed: {oc['first_listed']['reversed']}) should not "
                f"jump with the order. Reported, not a pass-rule criterion.", "",
                "| Measure | Value |", "|---|---|",
                f"| Agreement between the two orders | {_pf(oc['agreement'], oc['agreement_ci'])} "
                f"({oc['n_disagree']} items differ) |",
                f"| Accuracy, forward / reversed (scored n={oc['n_scored']}) | {_f(oc['accuracy_forward'])} / "
                f"{_f(oc['accuracy_reversed'])} |",
                f"| Right forward only / right reversed only (McNemar exact p) | {oc['right_forward_only']} / "
                f"{oc['right_reversed_only']} (p={oc['mcnemar_exact_p']:.3f}) |",
                f"| Answers equal to the first-listed label, forward / reversed | {_f(pf['forward'])} / "
                f"{_f(pf['reversed'])} |"]
        out += [f"| Agreement, expected {c} (n={v['n']}) | {_f(v['agreement'])} |"
                for c, v in oc["agreement_by_expected"].items()]
        out.append("")
    out += ["## Per corpus (spread across seeds)", "",
            "Per-seed rows are not like-for-like: a sentence drawn by several seeds is kept once (for the first seed "
            "that drew it), so later seeds hold fewer items and a different template mix. The pass rule reads only "
            "the pooled set above; each row's verdict is for that corpus's items on their own, and the off-record "
            "set size is not judged per corpus.", "",
            "| seed | scored n | accuracy | style gap | alignment F1 strict | off-record recall (n) | not judged | pass |",
            "|---|---|---|---|---|---|---|---|"]
    for tag, r in results["per_seed"].items():
        pm, po = r["modality"], r["off_record"]
        out.append(f"| {tag} | {pm['n']} | {_f(pm['accuracy'])} | {_f(pm['style_gap'])} | "
                   f"{_f(r['alignment']['strict']['f1'])} | {_f(po['recall'])} ({po['n_off_record']}) | "
                   f"{_f(r['not_judged_rate'])} | {'yes' if r['pass']['passed'] else 'no'} |")
    weakest = sorted((v["recall"], t, v) for t, v in m["by_template"].items()
                      if not v.get("excluded") and v["recall"] < 1.0)[:12]
    out += ["", "## Weakest templates (recall below 1, worst first; the unit the class intervals resample)", ""]
    if weakest:
        out += ["| template | class | n | recall | text |", "|---|---|---|---|---|"]
        out += [f"| {t} | {v['label']} | {v['n']} | {v['recall']:.3f} | {v['text'] or ''} |" for _r, t, v in weakest]
    else:
        out.append("Every template has recall 1.")
    for r, v in m["excluded_items"].items():
        out += ["", f"Items excluded one by one, reason \"{r}\": {v['n']} judged, accuracy {_f(v['accuracy'])} "
                    f"(planted wished/negated sentences whose concept wording says it happened to the speaker)."]
    excl = sorted((t, v) for t, v in m["by_template"].items() if v.get("excluded"))
    if excl:
        out += ["", "## Templates excluded from scoring", "", "| template | class | n | reason | text |", "|---|---|---|---|---|"]
        out += [f"| {t} | {v['label']} | {v['n']} | {v['reason']} | {v['text'] or ''} |" for t, v in excl]
    out += ["", "## Confusion matrix (rows expected, columns predicted; pooled, scored items)", "",
            "| expected | " + " | ".join(MODALITIES) + " |", "|---|" + "---|" * len(MODALITIES)]
    out += [f"| {e} | " + " | ".join(str(m["confusion"][e][p]) for p in MODALITIES) + " |" for e in MODALITIES]
    out += ["", "## Accuracy by trap kind (all kinds, including the excluded ones)", "",
            "Items excluded one by one (the planted wished and negated sentences whose concept wording says it "
            "happened to the speaker) are judged and listed here but never scored: \"accuracy, scored only\" leaves "
            "them out, \"accuracy, all judged\" keeps them.", "",
            "| kind | n judged | of which not scored | accuracy, all judged [95% CI] | accuracy, scored only |",
            "|---|---|---|---|---|"]
    out += [f"| {k} | {m['by_kind_n'][k]} | {m['by_kind_excluded_n'][k]} | {_pf(v, m['by_kind_ci'][k])} | "
            f"{_f(m['by_kind_scored'][k]) if m['by_kind_excluded_n'][k] < m['by_kind_n'][k] else 'not scored'} |"
            for k, v in m["by_kind"].items()]
    out += ["", "## Which claims hold without the AI (keyword-only / lexical-only), with the AI alone, and combined", "",
            "Reference only: the bench is built to defeat keyword cues (cue-free templates in every class), so the "
            "keyword rule fails the modality rows by construction, and the lexical-only off-record arm proposes "
            "nothing, so it flags every label as off-record: off-record recall holds and mention recall fails "
            "whatever the judges do. This table does not compare judges; the no-AI arm that matters is run on the "
            "study data.", "",
            "| Criterion | keyword/lexical only | AI only | combined |", "|---|---|---|---|"]

    def mark(v):
        return "n/a" if v is None else ("holds" if v else "fails")
    out += [f"| {k} | {mark(v['keyword_only'])} | {mark(v['ai_only'])} | {mark(v['combined'])} |"
            for k, v in results["arms"].items()]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ command line

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="second_scribe.judge_bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--judge-prefix", default="JUDGE", help="env prefix: SS_<PREFIX>_URL, _MODEL, _KEY_ENV, _PARAMS")
    ap.add_argument("--mode", choices=("generative", "logprob"), required=True)
    ap.add_argument("--out", required=True, help="directory for bench.<mode>.json/.md and the audit log")
    ap.add_argument("--client", choices=("http", "oracle", "replay"), default="http")
    ap.add_argument("--fixtures", help="fixtures JSON for --client replay")
    ap.add_argument("--label-order", choices=("forward", "reversed", "both"), default="forward",
                    help="generative mode: list the labels forward (the study's prompt, default), reversed, or "
                         "both (forward is scored; the reversed pass is added and the agreement reported)")
    ap.add_argument("--seeds", default=",".join(map(str, DEFAULT_SEEDS)),
                    help="corpus seeds to build and pool (default %(default)s)")
    ap.add_argument("--corpus", default=None, help="bench this one corpus directory instead of pooling --seeds")
    ap.add_argument("--prompts", default=str(_ROOT / "prompts"))
    args = ap.parse_args(argv)
    try:
        seeds = tuple(int(x) for x in args.seeds.split(","))
    except ValueError:
        ap.error("--seeds must be comma-separated integers, e.g. 0,1,2,3,4")
    if args.client == "http":
        try:
            client = HTTPClient(ModelConfig.from_env(args.judge_prefix))
        except KeyError as exc:
            print(f"set SS_{args.judge_prefix.upper()}_URL and SS_{args.judge_prefix.upper()}_MODEL "
                  f"(missing {exc.args[0]})", file=sys.stderr)
            return 2
    elif args.client == "oracle":
        client = OracleClient(args.corpus, seeds)
    else:
        if not args.fixtures:
            print("--client replay needs --fixtures FILE", file=sys.stderr)
            return 2
        client = ReplayClient(args.fixtures)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / f"bench.{args.mode}.audit.jsonl"
    log = AuditLog(log_path, f"judge-bench-{args.mode}")
    recorder = None
    if args.client != "replay":         # keep every response (logprobs included) so the run can be replayed offline
        recorder = client = RecordingClient(client, str(out / f"fixtures.{args.mode}.json"), merge=False)
    if args.mode == "logprob" and args.label_order != "forward":
        print("--label-order applies to --mode generative (the pipeline's judge)", file=sys.stderr)
        return 2
    results = bench(client, args.prompts, args.corpus, log, mode=args.mode, seeds=seeds,
                    label_order=args.label_order)
    if recorder is not None:
        recorder.save()
    (out / f"bench.{args.mode}.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / f"bench.{args.mode}.md").write_text(render_markdown(results), encoding="utf-8")
    for r in c3_floors(results):      # in_transcript first: it sets the denominator of every lead test
        print(f"{r['floor']}: {r['shown']} (floor {r['rule']}) "
              f"{'n/a' if r['ok'] is None else 'ok' if r['ok'] else 'BELOW'}")
    oc = results.get("order_check")
    if oc:
        print(f"label order forward vs reversed: agreement {_f(oc['agreement'])} on {oc['n_judged_both']} items; "
              f"accuracy {_f(oc['accuracy_forward'])} / {_f(oc['accuracy_reversed'])}")
    print(f"{args.mode}: {'PASS' if results['pass']['passed'] else 'FAIL'}; wrote {out}/bench.{args.mode}.json and .md")
    for r in results["pass"]["reasons"]:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
