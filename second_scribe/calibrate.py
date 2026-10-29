"""Calibration of the matcher and the off-record detector on cases whose answer is known.

Everything here is a pure function over pre-made judge outputs; the live judge calls happen elsewhere.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .maps.vsdx import ExtractConfig, extract_vsdx
from .match import SETTINGS_GRID, MatchSetting, align_labels, in_transcript, lexical_sim, match_nodes
from .schemas import MapGraph, Node
from .synth.bank import SYN_PHASES
from .textnorm import STOPWORDS
from .transcripts import parse_paragraphs, read_docx_text

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Plain labels unrelated to the journeys being calibrated; used as decoys (truth None).
_DECOYS = ("tax return filing", "dental cleaning appointment", "gym membership renewal", "passport photo",
           "library card", "bicycle repair", "winter tire change", "movie night", "swimming lessons",
           "garden hose", "piano tuning", "carpet cleaning")


def _drop_stopwords(label: str) -> str:
    kept = [w for w in label.split() if w.lower() not in STOPWORDS]
    return " ".join(kept) if kept else label


def _typo(label: str, rng: random.Random) -> str:
    """Delete one interior character of one word of 4+ letters (unchanged if there is none)."""
    words = label.split()
    cands = [k for k, w in enumerate(words) if len(w) >= 4]
    if not cands:
        return label
    k = rng.choice(cands)
    pos = rng.randrange(1, len(words[k]) - 1)
    words[k] = words[k][:pos] + words[k][pos + 1:]
    return " ".join(words)


def perturb(labels: list[str], rng: random.Random) -> tuple[list[str], dict[int, int | None]]:
    """Make a noisy second list from ``labels``, using only ``rng``.

    20% of the labels (rounded) are dropped, and an unrelated decoy is added in place of each dropped
    one; decoys have truth None (any match to a decoy is a false positive). Each survivor gets,
    independently, stopwords removed (p=0.25), word order reversed (p=0.25) and one typo (p=0.2); the
    list is then shuffled. Returns (perturbed labels, truth) where truth maps each perturbed index to the
    original index it came from, or None for a decoy.
    Raises ValueError if there are fewer usable decoys than dropped labels."""
    n = len(labels)
    dropped = set(rng.sample(range(n), round(0.2 * n)))
    pool = [d for d in _DECOYS if all(lexical_sim(d, l) == 0.0 for l in labels)]
    if len(pool) < len(dropped):
        raise ValueError(f"perturb needs {len(dropped)} decoys but only {len(pool)} of {len(_DECOYS)} share no "
                         f"content token with the labels; add decoys to _DECOYS or pass fewer labels")
    items: list[tuple[str, int | None]] = []
    for i, label in enumerate(labels):
        if i in dropped:
            continue
        out = label
        if rng.random() < 0.25:
            out = _drop_stopwords(out)
        if rng.random() < 0.25:
            out = " ".join(reversed(out.split()))
        if rng.random() < 0.2:
            out = _typo(out, rng)
        items.append((out, i))
    for _ in dropped:
        items.append((pool.pop(rng.randrange(len(pool))), None))
    order = list(range(len(items)))
    rng.shuffle(order)
    shuffled = [items[k] for k in order]
    return [t for t, _ in shuffled], {j: o for j, (_, o) in enumerate(shuffled)}


def _graph(map_id: str, origin: str, prefix: str, labels: list[str]) -> MapGraph:
    return MapGraph(map_id, origin, [Node(f"{prefix}{i}", t) for i, t in enumerate(labels)])


def _prf(tp: int, n_pred: int, n_truth: int) -> dict:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_truth if n_truth else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0, "n_pairs": n_pred}


def matcher_counts(cases: list[tuple[list[str], list[str], set[tuple[int, int]]]],
                   judged_by_case: list[dict], settings=SETTINGS_GRID) -> dict[str, dict[str, int]]:
    """Per setting key: ``tp``, ``n_pred`` (predicted pairs) and ``n_truth`` (true pairs), pooled over cases.
    The counts behind ``calibrate_matcher``'s rates, so several corpora can be pooled by adding them."""
    if len(cases) != len(judged_by_case):
        raise ValueError("cases and judged_by_case must have the same length")
    result: dict[str, dict[str, int]] = {}
    for s in settings:
        tp = n_pred = n_truth = 0
        for k, ((left, right, truth), judged) in enumerate(zip(cases, judged_by_case)):
            m = match_nodes(_graph(f"cal{k}", "ai", "L", left), _graph(f"cal{k}", "endorsed", "R", right), s, judged)
            predicted = {(int(a[1:]), int(b[1:])) for a, b in m.pairs}
            tp += len(predicted & truth)
            n_pred += len(predicted)
            n_truth += len(truth)
        result[s.key()] = {"tp": tp, "n_pred": n_pred, "n_truth": n_truth}
    return result


def calibrate_matcher(cases: list[tuple[list[str], list[str], set[tuple[int, int]]]],
                      judged_by_case: list[dict], settings=SETTINGS_GRID) -> dict[str, dict]:
    """Per setting key: precision, recall, f1 and n_pairs, pooled over cases. Each case is
    (left labels, right labels, true (left index, right index) pairs); ``judged_by_case[k]`` is the
    ``align_labels`` output for case k. Precision is 0.0 when nothing is predicted, recall 0.0 when
    there is no true pair."""
    return {key: _prf(c["tp"], c["n_pred"], c["n_truth"])
            for key, c in matcher_counts(cases, judged_by_case, settings).items()}


def in_transcript_counts(cases: list[tuple[list[bool], dict[int, bool]]]) -> dict[str, int]:
    """Confusion counts (positive = "the label is mentioned") pooled over cases, as in
    ``calibrate_in_transcript``. The off-record view reads them the other way round: tn = off-record labels
    correctly flagged, fp = off-record labels wrongly found mentioned."""
    tp = fp = fn = tn = 0
    for truth, pred in cases:
        for i, t in enumerate(truth):
            p = pred.get(i, False)
            tp += t and p
            fp += (not t) and p
            fn += t and not p
            tn += (not t) and not p
    return {"tp": int(tp), "fp": int(fp), "fn": int(fn), "tn": int(tn)}


def calibrate_in_transcript(cases: list[tuple[list[bool], dict[int, bool]]]) -> dict:
    """Truth (is the label really mentioned) against ``in_transcript`` predictions, pooled over cases.
    A label with no prediction counts as "not mentioned". Rates are 0.0 when their denominator is 0."""
    c = in_transcript_counts(cases)
    tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]
    return {"precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "false_mention_rate": fp / (fp + tn) if fp + tn else 0.0}


class _FallbackCount:
    """The calibration's audit log, seen through a counter of the ``judge_fallback`` records the helpers append when
    a judge call never got a valid answer (and the matcher fell back to lexical-only)."""

    def __init__(self, log):
        self.log = log
        self.by_stage = {"align": 0, "in_transcript": 0}

    def append(self, record_type, payload):
        if record_type == "judge_fallback":
            stage = str((payload or {}).get("stage", ""))
            key = "align" if stage.endswith("align") else "in_transcript"
            self.by_stage[key] += 1
        return self.log.append(record_type, payload)

    def __getattr__(self, name):
        return getattr(self.log, name)


def calibrate_synthetic(corpus_dir, log, client, settings=SETTINGS_GRID) -> dict:
    """Calibrate matching and off-record detection on the synthetic corpus built by ``synth.build``.

    Recovery on synthetic data is a unit test of the code, not evidence that the method is valid.

    Matching: for each participant, the gold graph's labels (alt wording, the stand-in for AI labels) are
    aligned once against the labels of the endorsed map as the extractor reads it from ``maps/<pid>.vsdx``.
    Truth is the pair of nodes standing for the same concept (``gold/<pid>.concepts.json``). One
    ``align_labels`` call per participant is reused for every setting in ``SETTINGS_GRID``.

    Off-record: ``in_transcript`` runs on the endorsed labels against the parsed transcript (participant
    label "pt", interviewer "iv"). A label is truly mentioned unless its concept is the participant's
    ``off_record`` trap (drawn on the map, never said).

    ``settings`` (default ``SETTINGS_GRID``) are the match settings scored; the align call is the same for all.
    Returns {"matcher": {setting key: {precision, recall, f1, n_pairs}}, "in_transcript": {precision,
    recall, false_mention_rate}, "matcher_counts": {setting key: {tp, n_pred, n_truth}},
    "in_transcript_counts": {tp, fp, fn, tn}, "n_participants": n, "judge_fallbacks": {align, in_transcript,
    total}}. ``judge_fallbacks`` counts the helper calls that never got a valid answer: the matcher then used the
    lexical rule alone for that participant, so a calibration with fallbacks measures the lexical floor, not the
    judge; a gate should read it.
    """
    log = _FallbackCount(log)
    root = Path(corpus_dir)
    meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    pids = [p["pid"] for p in meta["participants"]]
    cfg = ExtractConfig(phase_names=SYN_PHASES)
    match_cases, judged, off_cases = [], [], []
    for pid in pids:
        concepts = json.loads((root / "gold" / f"{pid}.concepts.json").read_text(encoding="utf-8"))
        endorsed = extract_vsdx(root / "maps" / f"{pid}.vsdx", pid, cfg)
        gold = MapGraph.from_json((root / "gold" / f"{pid}.json").read_text(encoding="utf-8"))
        right = [n.text for n in endorsed.nodes]
        left = [n.text for n in gold.nodes]
        right_cid = [concepts["endorsed"][n.node_id] for n in endorsed.nodes]
        left_cid = [concepts["gold"][n.node_id] for n in gold.nodes]
        truth = {(i, j) for i, a in enumerate(left_cid) for j, b in enumerate(right_cid) if a == b}
        match_cases.append((left, right, truth))
        judged.append(align_labels(log, client, left, right, _PROMPTS_DIR, item_id=pid))
        transcript = parse_paragraphs(read_docx_text(root / "transcripts" / f"{pid}.docx"), {"pt"}, pid,
                                      interviewer_labels={"iv"})
        traps = json.loads((root / "traps" / f"{pid}.json").read_text(encoding="utf-8"))
        off = {t["cid"] for t in traps if t["kind"] == "off_record"}
        predicted = in_transcript(log, client, right, transcript, _PROMPTS_DIR, item_id=pid)
        off_cases.append(([cid not in off for cid in right_cid], predicted))
    counts = matcher_counts(match_cases, judged, settings)
    return {"matcher": {k: _prf(c["tp"], c["n_pred"], c["n_truth"]) for k, c in counts.items()},
            "matcher_counts": counts,
            "in_transcript": calibrate_in_transcript(off_cases),
            "in_transcript_counts": in_transcript_counts(off_cases),
            "n_participants": len(pids),
            "judge_fallbacks": {**log.by_stage, "total": sum(log.by_stage.values())}}
