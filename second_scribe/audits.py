"""Independent auditors: deterministic code (no model) that checks every model stage and everything that leaves
the machine.

Nothing here trusts a model. The reconstructor's maps are re-parsed and their quotes re-checked by a second,
independent matcher; the judge's labels are checked for completeness and vocabulary; a sample of T=0 requests
is re-sent to see whether the server repeats itself; the synthetic corpus is run through the same stages, in
the same session, as a canary; and every file about to leave the machine is scanned by ``audit_leaks`` for
transcript text, participant names and contact details. No model de-identifies anything.

Results carry counts and ids only: for real data a failure names an element id, a spec key, a file path and a
byte offset, never the offending text. The forbidden sets of ``audit_leaks`` are built in memory from the real
files and are never written anywhere.

Maps are "interviewer-drawn, participant-endorsed maps". Recovering planted traps is a unit test of the code,
not evidence that the method is valid.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import shutil
import os
import subprocess
import time
import unicodedata
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from html import unescape
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from . import judge_bench
from .audit.verify import log_cache_changes, log_work_item
from .paths import stage_dirname, stage_from_dirname
from .schemas import MODALITIES, NODE_TYPES, STATUSES
from .leakscan import (_SVG_DATA_SIBLINGS, IndexUnavailable, LeakIndex, Unscannable, cat_blobs, git,  # noqa: F401
                       scan_named_bytes, tokens_with_offsets)
from .stats import cohen_kappa
from .textnorm import locate_quote_in_turns

# ------------------------------------------------------------------ constants

MAX_NODES = 120                      # the reconstructor's own limits, restated: the audit does not import them
MAX_LABEL_CHARS = 120
MAX_UNFOUND_SHARE = 0.50             # L16 (was 0.10): reconstruct tolerates up to this share of quotes not found
ONE_CLASS_SHARE = 0.95               # a spec whose judged labels are more than this share one class is flagged
ONE_CLASS_MIN_ITEMS = 20
CANARY_KINDS = ("wished", "negated")
CANARY_MIN_PASS = judge_bench.PASS_CLASS_RECALL   # the bench's per-class recall floor: one source of truth
MAX_STRING = 40                      # the aggregate-only length rule, restated for the JSON check of audit_leaks
NGRAM = 5
MAX_HITS_PER_FILE = 200              # locations kept per file; the counts always cover every hit
DETERMINISM_K = 20
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:>\-]{1,40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ result type

@dataclass
class AuditResult:
    name: str
    blocking: bool
    passed: bool
    counts: dict
    failures: list[str]                # ids only, never text: "<code>:<ids>"
    warnings: list[str] = field(default_factory=list)   # non-fatal flags, same format

    def summary(self) -> dict:
        """What the aggregates carry: counts and code tallies only (no ids, no paths)."""
        def codes(items):
            out: dict[str, int] = {}
            for x in items:
                out[x.split(":", 1)[0]] = out.get(x.split(":", 1)[0], 0) + 1
            return dict(sorted(out.items()))
        return {"name": self.name, "blocking": self.blocking, "passed": self.passed, "counts": self.counts,
                "failure_codes": codes(self.failures), "warning_codes": codes(self.warnings)}


class AuditBlocked(RuntimeError):
    """A blocking auditor failed: the pipeline must not continue. The message names counts only."""

    def __init__(self, results: list[AuditResult]):
        self.results = [r for r in results if r.blocking and not r.passed]
        names = ", ".join(f"{r.name} ({len(r.failures)} failures)" for r in self.results)
        super().__init__(f"blocked by audit: {names}")


class LeakBlocked(AuditBlocked):
    """``audit_leaks`` found something in text that was about to be written."""


# ------------------------------------------------------------------ small shared helpers

def _loads_strict(text: str) -> tuple[object, int]:
    """(parsed JSON, number of repeated keys inside one object). Raises ValueError on invalid JSON."""
    dup = 0

    def hook(pairs):
        nonlocal dup
        d = {}
        for k, v in pairs:
            if k in d:
                dup += 1
            d[k] = v
        return d
    return json.loads(text, object_pairs_hook=hook), dup


def _read_item(path: Path):
    """(object | None, repeated keys, problem code | None)."""
    try:
        obj, dup = _loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, 0, "unreadable_item"
    if not isinstance(obj, dict):
        return None, dup, "unreadable_item"
    return obj, dup, None


def _safe(s) -> str:
    """An element id or spec key as it may appear in a failure: only short, plain ids. A model can put anything in
    an id, so anything else is replaced."""
    return s if isinstance(s, str) and _SAFE_ID.fullmatch(s) else "<bad-id>"


def _split_name(name: str) -> tuple[str, str]:
    """"<pid>__<arm>__<variant>__t<temp>__r<rep>" -> (pid, spec key)."""
    parts = name.split("__")
    if len(parts) < 5:
        return name, ""
    return "__".join(parts[:-4]), "__".join(parts[-4:])


class _Refs:
    """Participant references for failures: the id itself for a synthetic corpus, otherwise an anonymous
    position (``#3``) so that a participant id never reaches an audit file."""

    def __init__(self, pids: Iterable[str], anonymize: bool):
        self.map = {p: (f"#{i}" if anonymize else _safe(p)) for i, p in enumerate(sorted(set(pids)))}

    def __call__(self, pid: str) -> str:
        return self.map.get(pid, "#?")


def _stage_names(work: Path, stage: str) -> list[str]:
    d = work / stage_dirname(stage)
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def _ladder_stores(work: Path, base: str) -> list[str]:
    """The instruction ladder's work stores of ``base`` (``recon`` -> ``recon:rung1``, ``recon:rung2``, ...): one per rung,
    holding the same kind of item as ``base`` under the same item names."""
    return sorted(stage_from_dirname(p.name) for p in work.glob(stage_dirname(f"{base}:") + "*")
                  if p.is_dir()) if work.exists() else []


# ------------------------------------------------------------------ audit_recon_outputs

_APOS = "'‘’ʼ`"
_WORD = re.compile(rf"[^\W_]+(?:[{_APOS}][^\W_]+)*")
_ELLIPSIS = re.compile(r"\.{3,}|…")
_SPLIT_APOS = re.compile(f"[{_APOS}]")
_pattern_cache: dict[str, "re.Pattern | None"] = {}


def _part_regex(words: list[str]) -> re.Pattern:
    def word(w: str) -> str:
        return f"[{_APOS}]".join(re.escape(piece) for piece in _SPLIT_APOS.split(w))
    body = r"[\W_]+".join(word(w) for w in words)
    # whole words only: not inside a longer word, and not before or after "'" + more letters (a contraction)
    return re.compile(rf"(?<![^\W_])(?<![^\W_][{_APOS}])(?:{body})(?![^\W_])(?![{_APOS}][^\W_])",
                      re.IGNORECASE)


def regex_contains_quote(hay: str, quote: str) -> bool:
    """An independent containment check (no use of ``textnorm``): every "..."-separated part of ``quote`` occurs in
    ``hay`` as whole words in order, case, punctuation and whitespace ignored, parts in order and not
    overlapping. A quote with no words is never contained."""
    q = unicodedata.normalize("NFC", quote or "")
    parts = [w for w in (_WORD.findall(p) for p in _ELLIPSIS.split(q)) if w]
    if not parts:
        return False
    h = unicodedata.normalize("NFC", hay)
    pos = 0
    for words in parts:
        key = "\x1f".join(words).casefold()
        pat = _pattern_cache.get(key)
        if pat is None:
            pat = _pattern_cache[key] = _part_regex(words)
        m = pat.search(h, pos)
        if m is None:
            return False
        pos = m.end()
    return True


def _check_map(d, arm: str, phases, anon: bool = False) -> list[tuple[str, str]]:
    """Schema problems of one saved map as (code, element reference). ``arm`` is full, removed or prior_only.
    With ``anon`` (a real corpus) the reference is the element's position (``n#3``, ``e#7``), never an id the model
    wrote."""
    out: list[tuple[str, str]] = []
    if not isinstance(d, dict):
        return [("map_not_object", "-")]
    want_origin = "prior_only" if arm == "prior_only" else "ai"
    if d.get("origin") != want_origin:
        out.append(("bad_origin", "-"))
    nodes, edges = d.get("nodes"), d.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return out + [("map_shape", "-")]
    if not 1 <= len(nodes) <= MAX_NODES:
        out.append(("no_nodes" if not nodes else "too_many_nodes", "-"))
    ids: set = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            out.append(("node_not_object", "-"))
            continue
        nid = n.get("node_id")
        ref = f"n#{i}" if anon else _safe(nid)
        if not isinstance(nid, str) or not nid:
            out.append(("bad_node_id", ref))
            continue
        if nid in ids:
            out.append(("duplicate_node_id", ref))
        ids.add(nid)
        if n.get("node_type") not in NODE_TYPES:
            out.append(("bad_node_type", ref))
        if n.get("status") not in STATUSES:
            out.append(("bad_status", ref))
        text = n.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_LABEL_CHARS:
            out.append(("bad_label", ref))
        if phases is not None and n.get("phase") is not None and n.get("phase") not in phases:
            out.append(("bad_phase", ref))
        _check_quote(out, "node", n.get("quote"), n.get("status"), arm, ref)
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            out.append(("edge_not_object", "-"))
            continue
        s, t = e.get("source"), e.get("target")
        ref = f"e#{i}" if anon else f"e:{_safe(s)}->{_safe(t)}"[:40]
        if not isinstance(s, str) or not isinstance(t, str):
            out.append(("edge_bad_endpoint", "-"))
            continue
        if s not in ids or t not in ids:
            out.append(("edge_unknown_node", ref))
        elif s == t:
            out.append(("self_loop", ref))
        if e.get("status") not in STATUSES:
            out.append(("bad_status", ref))
        _check_quote(out, "edge", e.get("quote"), e.get("status"), arm, ref)
    return out


def _check_quote(out, kind: str, quote, status, arm: str, ref: str) -> None:
    if arm == "prior_only":
        if quote is not None:
            out.append(("prior_quote", ref))
        if status == "wished":
            out.append(("prior_wished", ref))
    elif not isinstance(quote, str) or not quote.strip():
        out.append(("missing_quote", ref))


def audit_recon_outputs(work_dir, transcripts: dict, *, phases=None, anonymize: bool = False,
                        script_sources: bool = True) -> AuditResult:
    """Re-parse every saved AI map (``work/recon``, ``work/prior_only`` and, unless ``script_sources`` is False, the
    script sources' ``work/script_prior_only``) independently of reconstruct.py and re-check
    every quote with a second matcher. ``transcripts``: {participant id: Transcript} (the arm's text is what the
    quotes must come from). Blocking: any saved map that violates the schema, or whose quotes are more than
    50% (``MAX_UNFOUND_SHARE``, L16; was 10%) not found by the independent matcher although the pipeline accepted it. Disagreements between
    ``textnorm.contains_quote`` and the regex matcher are counted both ways, never decided.

    Run 2: each quote's tier comes from ``textnorm.locate_quote_tiered``, the function the validator used. The
    independent regex matcher is an EXACT matcher, so it cross-checks the exact-tier quotes. "Exact" is the turn-aware
    one (one paragraph by one speaker, run-2 review B1), the same as the validator's and Task 6's; the blocking
    share counts unfound-tier quotes plus exact-tier quotes the regex matcher does not find; approximate-tier
    quotes are counted apart (``quotes_exact`` / ``quotes_approximate``) and are not held against the regex matcher,
    which by construction cannot find them."""
    work = Path(work_dir)
    phases = tuple(phases) if phases is not None else None
    stores = ("recon", *_ladder_stores(work, "recon"), "prior_only")     # the ladder's maps too
    if script_sources:
        stores += ("script_prior_only",)
    names = {s: _stage_names(work, s) for s in stores}
    refs = _Refs((_split_name(n)[0] for ns in names.values() for n in ns), anonymize)
    c = {"maps_audited": 0, "items_by_status": {}, "quotes_checked": 0, "quotes_unfound_regex": 0,
         "quotes_unfound_textnorm": 0, "quotes_exact": 0, "quotes_approximate": 0,
         "disagree_textnorm_yes_regex_no": 0,
         "disagree_textnorm_no_regex_yes": 0, "maps_without_transcript": 0, "duplicate_keys": 0,
         "edges_repeated": 0}
    failures: list[str] = []
    for stage, ns in names.items():
        for name in ns:
            pid, spec = _split_name(name)
            ref = f"{refs(pid)}:{_safe(spec)}" + (stage[len("recon"):] if stage.startswith("recon:") else "")
            d, dup, problem = _read_item(work / stage_dirname(stage) / f"{name}.json")
            if problem:
                failures.append(f"{problem}:{ref}")
                continue
            if dup:
                c["duplicate_keys"] += dup
                failures.append(f"duplicate_key:{ref}")
            status = d.get("status")
            key = status if isinstance(status, str) else "<none>"
            c["items_by_status"][key] = c["items_by_status"].get(key, 0) + 1
            if status != "ok":
                continue
            arm = "prior_only" if stage in ("prior_only", "script_prior_only") else spec.split("__")[0]
            if arm not in ("full", "removed", "prior_only"):
                failures.append(f"bad_arm:{ref}")
                continue
            c["maps_audited"] += 1
            m = d.get("map")
            problems = _check_map(m, arm, phases, anonymize)
            failures += [f"{code}:{ref}:{eid}" for code, eid in problems]
            if problems or arm == "prior_only":
                continue
            tr = transcripts.get(pid)
            if tr is None:
                c["maps_without_transcript"] += 1
                failures.append(f"no_transcript:{ref}")
                continue
            hay, units = tr.text(arm), tr.validation_units(arm)
            quotes = [n["quote"] for n in m["nodes"]] + [e["quote"] for e in m["edges"]]
            unfound = 0
            seen_edges: set = set()
            for e in m["edges"]:
                k = (e["source"], e["target"])
                c["edges_repeated"] += k in seen_edges
                seen_edges.add(k)
            for q in quotes:
                tier = locate_quote_in_turns(units, q)[0]
                tn, rx = tier == "exact", regex_contains_quote(hay, q)
                c["quotes_checked"] += 1
                c["quotes_exact"] += tn
                c["quotes_approximate"] += tier == "approximate"
                c["quotes_unfound_textnorm"] += not tn
                c["quotes_unfound_regex"] += not rx
                unfound += tier == "unfound" or (tier == "exact" and not rx)
                c["disagree_textnorm_yes_regex_no"] += tn and not rx
                c["disagree_textnorm_no_regex_yes"] += rx and not tn
            if quotes and unfound / len(quotes) > MAX_UNFOUND_SHARE:
                failures.append(f"quote_share_regex:{ref}")
    c["items_by_status"] = dict(sorted(c["items_by_status"].items()))
    return AuditResult("recon_outputs", True, not failures, c, failures)


# ------------------------------------------------------------------ audit_judge_outputs / audit_second_judge

JUDGE_ITEM_STAGES = ("judge_modality", "judge_shuffled", "judge2_modality")
HELPER_STAGES = ("mapping_talk_confirm", "in_transcript", "canonicalize", "align")


def _elem_ref(m: dict, eid, anon: bool) -> str:
    """An element id for a failure: plain ids as they are; for a real corpus its position in the map."""
    if not anon:
        return _safe(eid)
    nodes = [n.get("node_id") for n in m.get("nodes", []) if isinstance(n, dict)]
    if eid in nodes:
        return f"n#{nodes.index(eid)}"
    edges = [f"e:{e.get('source')}->{e.get('target')}" for e in m.get("edges", []) if isinstance(e, dict)]
    return f"e#{edges.index(eid)}" if eid in edges else "x#?"


def _map_elements(m: dict, nodes_only: bool = False) -> tuple[set[str], int]:
    """(element ids the judge must label, number of judged items) for a saved map."""
    nodes = [n for n in m.get("nodes", []) if isinstance(n, dict)]
    ids = {n["node_id"] for n in nodes if isinstance(n.get("node_id"), str)}
    quoted_nodes = sum(1 for n in nodes if isinstance(n.get("quote"), str) and n["quote"].strip())
    if nodes_only:
        return ids, quoted_nodes
    edge_ids = {f"e:{e['source']}->{e['target']}" for e in m.get("edges", [])
                if isinstance(e, dict) and e.get("quote") and isinstance(e.get("source"), str)
                and isinstance(e.get("target"), str)}
    n_edge_items = sum(1 for e in m.get("edges", []) if isinstance(e, dict) and e.get("quote"))
    return ids | edge_ids, sum(1 for n in nodes if n.get("quote")) + n_edge_items


def _cache_keys(m: dict, labels: dict, not_judged: set, tr, fingerprint: str) -> dict[str, str]:
    """{judge cache key: label} for the items of one saved map, rebuilt the way the judge builds them (speaker and
    reply included). Items the judge never answered are left out."""
    from .judge import JudgeCache, modality_items
    from .provenance import provenance_for
    from .schemas import MapGraph
    g = MapGraph.from_json(json.dumps(m))
    out = {}
    for it in modality_items(g, provenance=provenance_for(g, tr), transcript=tr):
        eid = it["element_id"]
        if eid in labels and eid not in not_judged and labels[eid] in MODALITIES:
            out[JudgeCache.key(it["element"], it["quote"], fingerprint, it.get("speaker"),
                               it.get("reply"))] = labels[eid]
    return out


def audit_judge_outputs(work_dir, *, anonymize: bool = False, transcripts: dict | None = None,
                        prompts_dir=None) -> AuditResult:
    """Every judged item of ``judge_modality``, ``judge_shuffled`` and ``judge2_modality`` has exactly one label from
    MODALITIES; the label files and the judge caches hold no repeated key; ``n_items`` matches the map. Counts the
    items that were not judged, the failed batches and the helper items that fell back. Warns (does not fail)
    when a spec's labels are more than 95% one class. With ``transcripts`` ({pid: Transcript}) and ``prompts_dir``
    the judge's cache keys are rebuilt for every item, and a key that carries two different labels fails.
    Blocking on structural problems."""
    work = Path(work_dir)
    fingerprint = None
    if transcripts is not None and prompts_dir is not None:
        from .judge import prompt_fingerprint
        fingerprint = prompt_fingerprint(prompts_dir)
    key_labels: dict[tuple, dict] = {}
    names = {s: _stage_names(work, s) for s in (*JUDGE_ITEM_STAGES, *_ladder_stores(work, "judge_modality"))}
    refs = _Refs((_split_name(n)[0] for ns in names.values() for n in ns), anonymize)
    c = {"items_audited": 0, "items_labelled": 0, "items_not_judged": 0, "failed_batches": 0, "stale_maps": 0,
         "duplicate_keys": 0, "fallback_items": {}, "label_conflicts_same_text_quote": 0,
         "cache_keys_checked": 0, "cache_key_label_conflicts": 0}
    failures: list[str] = []
    warnings: list[str] = []
    by_spec: dict[str, dict[str, int]] = {}
    seen_label: dict[tuple, set] = {}
    for stage, ns in names.items():
        for name in ns:
            pid, spec = _split_name(name)
            ref = f"{refs(pid)}:{_safe(spec)}"
            d, dup, problem = _read_item(work / stage_dirname(stage) / f"{name}.json")
            if problem:
                failures.append(f"{problem}:{stage}:{ref}")
                continue
            if dup:
                c["duplicate_keys"] += dup
                failures.append(f"duplicate_key:{stage}:{ref}")
            if d.get("status") != "ok":
                continue
            c["items_audited"] += 1
            recon = "recon" + stage[stage.index(":"):] if ":" in stage else "recon"      # a rung's judge items: its maps
            rm, _dup, rproblem = _read_item(work / stage_dirname(recon) / f"{name}.json")
            m = rm.get("map") if rm else None
            if not isinstance(m, dict):
                failures.append(f"orphan_judge_item:{stage}:{ref}")
                continue
            shuffled = stage == "judge_shuffled"
            want, n_items = _map_elements(m, nodes_only=shuffled)
            labels = d.get("labels")
            if labels is None and shuffled and d.get("n_items") == 0:
                continue                                  # nothing could be shuffled for this map
            if not isinstance(labels, dict):
                failures.append(f"labels_not_object:{stage}:{ref}")
                continue
            if d.get("map_sha256") != hashlib.sha256(json.dumps(m, sort_keys=True, ensure_ascii=False)
                                                     .encode("utf-8")).hexdigest():
                c["stale_maps"] += 1
            for eid in sorted(want - set(labels)):
                failures.append(f"missing_label:{stage}:{ref}:{_elem_ref(m, eid, anonymize)}")
            for eid in sorted(set(labels) - want):
                failures.append(f"unknown_element:{stage}:{ref}:{_elem_ref(m, eid, anonymize)}")
            for eid, lab in sorted(labels.items()):
                if lab not in MODALITIES:
                    failures.append(f"bad_label:{stage}:{ref}:{_elem_ref(m, eid, anonymize)}")
            if d.get("n_items") != n_items:
                failures.append(f"n_items_mismatch:{stage}:{ref}")
            nj = d.get("not_judged") or []
            if not isinstance(nj, list) or not set(nj) <= set(labels):
                failures.append(f"bad_not_judged:{stage}:{ref}")
                nj = []
            c["items_labelled"] += sum(1 for v in labels.values() if v in MODALITIES)
            c["items_not_judged"] += len(nj)
            st = d.get("stats") or {}
            c["failed_batches"] += int(st.get("failed_batches", 0)) if isinstance(st, dict) else 0
            if fingerprint is not None and stage != "judge_shuffled" and pid in transcripts:
                for key, lab in _cache_keys(m, labels, set(nj), transcripts[pid], fingerprint).items():
                    seen = key_labels.setdefault((stage, key), {})
                    if lab not in seen.values():
                        seen[f"{ref}"] = lab
            if stage != "judge_shuffled":
                node_by_id = {n["node_id"]: n for n in m.get("nodes", []) if isinstance(n, dict)}
                for eid, lab in labels.items():
                    if lab not in MODALITIES or eid in nj:
                        continue
                    by_spec.setdefault(f"{stage}|{spec}", {}).setdefault(lab, 0)
                    by_spec[f"{stage}|{spec}"][lab] += 1
                    n = node_by_id.get(eid)
                    if n and isinstance(n.get("quote"), str):
                        seen_label.setdefault((stage, " ".join(n.get("text", "").split()),
                                               " ".join(n["quote"].split())), set()).add(lab)
    for labs in seen_label.values():
        c["label_conflicts_same_text_quote"] += len(labs) > 1
    c["cache_keys_checked"] = len(key_labels)
    for (stage, _key), by_ref in sorted(key_labels.items(), key=lambda kv: kv[0][0]):
        if len(set(by_ref.values())) > 1:
            c["cache_key_label_conflicts"] += 1
            failures.append(f"cache_key_conflict:{stage}:{sorted(by_ref)[0]}")
    for cache_name in ("judge_cache.json", "judge2_cache.json"):
        p = work / cache_name
        if not p.exists():
            continue
        try:
            obj, dup = _loads_strict(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            failures.append(f"unreadable_item:{cache_name}")
            continue
        if dup:
            c["duplicate_keys"] += dup
            failures.append(f"duplicate_key:{cache_name}")
        if not isinstance(obj, dict):
            failures.append(f"unreadable_item:{cache_name}")
        else:
            # an entry is a label or, when it names the call behind it (Task 10), {"modality", "request_id"}
            failures += [f"bad_label:{cache_name}" for v in obj.values()
                         if (v.get("modality") if isinstance(v, dict) else v) not in MODALITIES][:1]
    for stage in HELPER_STAGES:
        n = sum(1 for nm in _stage_names(work, stage)
                if (_read_item(work / stage / f"{nm}.json")[0] or {}).get("status") == "fallback")
        if n:
            c["fallback_items"][stage] = n
    flagged = 0
    for key, counts in sorted(by_spec.items()):
        total = sum(counts.values())
        if total >= ONE_CLASS_MIN_ITEMS and max(counts.values()) / total > ONE_CLASS_SHARE:
            flagged += 1
            warnings.append(f"one_class:{key.split('|', 1)[0]}:{_safe(key.split('|', 1)[1])}")
    c["specs_over_95_percent_one_class"] = flagged
    if c["items_not_judged"]:
        warnings.append(f"not_judged:{c['items_not_judged']}")
    return AuditResult("judge_outputs", True, not failures, c, failures, warnings)


def audit_second_judge(work_dir, sample_share: float = 1.0) -> AuditResult:
    """Cohen's kappa between judge 1 and judge 2 over the items both labelled (items either judge did not judge
    are left out). ``sample_share`` keeps a deterministic share of them (ordered by a hash of the item key).
    Non-blocking: agreement is not accuracy, it is reported."""
    work = Path(work_dir)
    a_names, b_names = _stage_names(work, "judge_modality"), set(_stage_names(work, "judge2_modality"))
    pairs: list[tuple[str, str, str]] = []
    for name in a_names:
        if name not in b_names:
            continue
        a, _d1, p1 = _read_item(work / "judge_modality" / f"{name}.json")
        b, _d2, p2 = _read_item(work / "judge2_modality" / f"{name}.json")
        if p1 or p2 or a.get("status") != "ok" or b.get("status") != "ok":
            continue
        la, lb = a.get("labels"), b.get("labels")
        if not isinstance(la, dict) or not isinstance(lb, dict):
            continue
        skip = set(a.get("not_judged") or []) | set(b.get("not_judged") or [])
        for eid in sorted(set(la) & set(lb) - skip):
            if la[eid] in MODALITIES and lb[eid] in MODALITIES:
                pairs.append((f"{name}|{eid}", la[eid], lb[eid]))
    pairs.sort(key=lambda t: sha256_text(t[0]))
    if sample_share < 1.0:
        pairs = pairs[:max(0, math.ceil(sample_share * len(pairs)))]
    c: dict = {"n_overlap": len(pairs), "skipped": not b_names, "sample_share": sample_share,
               "agreement": None, "kappa": None}
    warnings: list[str] = []
    if pairs:
        c["agreement"] = round(sum(x == y for _, x, y in pairs) / len(pairs), 4)
        k = cohen_kappa([x for _, x, _ in pairs], [y for _, _, y in pairs])
        if math.isnan(k):
            warnings.append("kappa_undefined:one_label")
        else:
            c["kappa"] = round(k, 4)
            if k < 0.6:
                warnings.append(f"kappa_low:{k:.2f}")
    return AuditResult("second_judge", False, True, c, [], warnings)


# ------------------------------------------------------------------ audit_determinism

_STAGE_ROLE = {"q": "recon", "recon": "recon", "prior_only": "recon", "judge": "judge", "judge2": "judge2"}


def sample_from_log(log_path, k: int, seed: int, roles: Iterable[str] | None = None) -> list[dict]:
    """``k`` seeded T=0 requests of a run log, each with an accepted reply, as
    ``{messages, temperature, seed, max_tokens, expected_sha256, role, params?}``. ``roles`` limits the stages by the
    client that served them (recon, judge, judge2)."""
    roles = set(roles) if roles is not None else None
    path = Path(log_path)
    requests: dict[str, tuple[int, str]] = {}
    ok: dict[str, str] = {}
    with open(path, "rb") as fh:
        while True:
            off = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pl = rec.get("payload") or {}
            if rec.get("record_type") == "call_request" and pl.get("temperature") == 0:
                role = _STAGE_ROLE.get(str(pl.get("stage", "")).split(":")[0])
                if role and (roles is None or role in roles):
                    requests[pl["request_id"]] = (off, role)
            elif rec.get("record_type") == "call_response" and pl.get("validation_error") is None \
                    and isinstance(pl.get("text"), str):
                ok[pl["request_id"]] = sha256_text(pl["text"])
    cands = sorted(rid for rid in requests if rid in ok)
    chosen = random.Random(seed).sample(cands, min(k, len(cands)))
    out = []
    with open(path, "rb") as fh:
        for rid in chosen:
            fh.seek(requests[rid][0])
            pl = json.loads(fh.readline())["payload"]
            out.append({"messages": pl["messages"], "temperature": pl["temperature"], "seed": pl.get("seed"),
                        "max_tokens": pl.get("max_tokens", 1000), "expected_sha256": ok[rid],
                        "role": requests[rid][1], **({"params": pl["params"]} if "params" in pl else {})})
    return out


def _determinism_params(sample: dict, client) -> dict:
    """Replay the recorded effective parameters, never a redacted placeholder or an inferred secret.

    Older requests without params are usable only with a client that has no configured extras.
    A client with additional current defaults would silently add fields absent from the original request.
    """
    from .llm import redact_params

    current = getattr(getattr(client, "cfg", None), "params", None) or {}
    params = sample.get("params", {})
    if not isinstance(params, dict) or not isinstance(current, dict) or set(current) - set(params):
        raise ValueError("request parameters unavailable")
    # JSON round-trip also isolates nested values from a client that mutates its arguments.
    copied = json.loads(json.dumps(params, allow_nan=False))

    def masked(value):
        if isinstance(value, str):
            return "[REDACTED]" in value
        if isinstance(value, dict):
            return any(masked(k) or masked(v) for k, v in value.items())
        if isinstance(value, list):
            return any(masked(v) for v in value)
        return False

    if masked(copied) or redact_params(copied) != copied:
        raise ValueError("request parameters unavailable")
    return {"params": copied} if copied else {}


def audit_determinism(log, client, sample: list[dict] | None, k: int = DETERMINISM_K, *, seed: int = 0,
                      remote: bool = False) -> AuditResult:
    """Re-send ``k`` seeded T=0 requests (``sample``, or taken from ``log`` when None) and compare the hash of each
    reply with the hash of the logged one. The re-sends go straight to the client, so they add nothing to the run
    log. Fail-closed: any error, any mismatch, or nothing to compare FAILS the audit (shown as FAIL in the report). It
    stays non-blocking, so a batching server that drifts a little does not stop a run; the share is in the counts.
    Recorded effective ``params`` accompany each re-send. Redacted/malformed parameters or extra current defaults
    fail the check without sending that request (``determinism_params_unavailable``), rather than guessing values.

    ``remote`` (K13: the role's endpoint is a declared ``tool.remote_endpoints`` entry): a cloud model is not repeatable
    at temperature 0, so a mismatch is reported as the warning ``remote_not_identical:<n>`` instead of a failure, and
    the counts carry ``"remote": true``. Errors and an empty check still fail. False (every study config): as before."""
    if sample is None:
        path = getattr(log, "path", log)
        sample = sample_from_log(path, k, seed) if path else []
    pool = list(sample)
    picked = random.Random(seed).sample(pool, min(k, len(pool)))
    ident = errors = params_unavailable = 0
    for s in picked:
        try:
            extra = _determinism_params(s, client)
        except (TypeError, ValueError):
            errors += 1
            params_unavailable += 1
            continue
        try:
            resp = client.complete(json.loads(json.dumps(s["messages"])), temperature=s.get("temperature", 0.0),
                                   seed=s.get("seed"), max_tokens=s.get("max_tokens", 1000), **extra)
        except Exception:  # noqa: BLE001 - counted, and the audit fails: an error is not agreement
            errors += 1
            continue
        ident += sha256_text(resp.text or "") == s["expected_sha256"]
    n = len(picked)
    c = {"n_checked": n, "n_identical": ident, "n_errors": errors,
         "identical_share": round(ident / n, 4) if n else None}
    failures = []
    if n == 0:
        failures.append("determinism_nothing_checked:0")      # a check that compared nothing is not a pass
    if errors:
        failures.append(f"determinism_error:{errors}")
    if params_unavailable:
        failures.append(f"determinism_params_unavailable:{params_unavailable}")
    warnings = []
    if n and ident + errors < n:
        (warnings if remote else failures).append(f"{'remote_' if remote else ''}not_identical:{n - ident - errors}")
    if remote:
        c["remote"] = True
    return AuditResult("determinism", False, not failures, c, failures, warnings)


# ------------------------------------------------------------------ audit_canary

def audit_canary(pipeline_result_synthetic) -> AuditResult:
    """The synthetic corpus, run through the same stages, prompts and models in the same session, must still pass the
    trap rule: the wished and negated traps pass at least ``CANARY_MIN_PASS`` of the time. Catches a broken
    server, a wrong model or a bad quantisation before real data is trusted. Blocking. A canary that did not
    produce trap rows for both kinds also blocks (it checked nothing)."""
    res = pipeline_result_synthetic
    if isinstance(res, dict) and "aggregates" in res:
        res = res["aggregates"]
    rows = res.get("traps") if isinstance(res, dict) else res
    rows = rows if isinstance(rows, list) else []
    c: dict = {"min_pass_rate": CANARY_MIN_PASS, "n_rows": len(rows)}
    failures: list[str] = []
    for kind in CANARY_KINDS:
        mine = [r for r in rows if isinstance(r, dict) and r.get("kind") == kind]
        n_pass = sum(r.get("result") == "PASS" for r in mine)
        rate = round(n_pass / len(mine), 4) if mine else None
        c[kind] = {"n": len(mine), "pass": n_pass, "rate": rate}
        if rate is None:
            failures.append(f"canary_no_rows:{kind}")
        elif rate < CANARY_MIN_PASS:
            failures.append(f"canary_below_rule:{kind}")
    return AuditResult("canary", True, not failures, c, failures)


def audit_canary_recon(work_dir) -> AuditResult:
    """The canary of a session with the reconstructor client alone: the synthetic corpus run through the Q stages, its
    reconstructions (``<work_dir>/recon``) must come back valid at least ``CANARY_MIN_PASS`` of the time. The trap rule
    needs the judge, so a session that has none checks what it can reach: a broken server, a wrong model or a bad
    quantisation shows here too. No reconstruction at all also blocks (it checked nothing)."""
    stats: dict[str, int] = {}
    for f in sorted((Path(work_dir) / "recon").glob("*.json")):
        try:
            st = json.loads(f.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError, AttributeError):
            st = "unreadable"
        stats[str(st)] = stats.get(str(st), 0) + 1
    n, ok = sum(stats.values()), stats.get("ok", 0)
    rate = round(ok / n, 4) if n else None
    failures = ["canary_no_rows:recon"] if rate is None else (["canary_below_rule:recon"] if rate < CANARY_MIN_PASS else [])
    return AuditResult("canary", True, not failures,
                       {"min_pass_rate": CANARY_MIN_PASS, "recon": {"n": n, "pass": ok, "rate": rate}}, failures)


# ------------------------------------------------------------------ audit_leaks

_KINDS = ("ngram", "name", "long_string", "email", "phone", "postal", "ip", "invalid_json")


def _as_index(source, names, n: int) -> LeakIndex:
    if isinstance(source, LeakIndex):
        return source
    texts = source() if callable(source) else source
    nm = names() if callable(names) else (names or ())
    return LeakIndex.build(texts, nm, n)


def _walk(paths: Iterable) -> tuple[list[tuple[Path, str]], list[str]]:
    """([(file, name relative to the folder it was found under)], missing paths)."""
    files: list[tuple[Path, str]] = []
    missing: list[str] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files += [(f, str(f.relative_to(pp))) for f in sorted(pp.rglob("*"))
                      if f.is_file() and ".git" not in f.relative_to(pp).parts]
        elif pp.is_file():
            files.append((pp, pp.name))
        else:
            missing.append(str(pp))
    return files, missing


def _is_aggregate_json(path: Path, strict: set[str]) -> bool:
    return path.suffix.lower() == ".json" and (str(path) in strict or "aggregates" in path.parts)


def _new_counts(index: LeakIndex) -> dict:
    return {"files_scanned": 0, "files_missing": 0, "files_unscannable": 0, "unscannable_reasons": {},
            **{f"{k}_hits": 0 for k in _KINDS}, "unverifiable_svg_hits": 0, "hits_not_listed": 0,
            "index_texts": index.stats["n_texts"], "index_ngrams": index.stats["n_ngrams"],
            "index_names": index.stats["n_names"], "index_id_map_entries": index.stats.get("id_map_entries", 0),
            "index_names_public_exempt": index.stats.get("names_public_exempt", 0)}


def _count_hit(c: dict, kind: str) -> None:
    c[f"{kind}_hits"] = c.get(f"{kind}_hits", 0) + 1


def audit_leaks(paths: list, forbidden_ngrams_source, names_source=None, *, n: int = NGRAM,
                strict_json: Iterable[str] | None = None) -> AuditResult:
    """Scan every file that is about to leave the machine. A hit is: (a) any ``n``-word run (default 5; runs of mostly
    stopwords excepted) that occurs in a real transcript, map label or work file, compared after folding accents,
    invisible characters, look-alike letters and line-break hyphens; (b) a participant id, initials or interviewer
    code (initials only in capitals, ids in any case and separator), also in a file's name; (c) a string over 40
    characters in a JSON aggregate (every .json under an ``aggregates`` folder, and the files in ``strict_json``);
    (d) an email, phone number, Canadian postal code or IPv4 address (loopback excepted). Every file is read in
    its decoded views (entities, JSON strings, markup without tags, archives opened); a missing path or a file that
    cannot be read as text fails closed. See ``leakscan`` for the folding and the accepted limits.

    ``forbidden_ngrams_source``: a ``LeakIndex``, an iterable of real texts, or a callable returning one;
    ``names_source``: an iterable of names or a callable. The sets exist only in memory. The result holds counts and,
    per hit, ``<kind>:<path>@<byte offset>`` (offset 0 = found only in a decoded view): never the matched text.
    BLOCKING."""
    index = _as_index(forbidden_ngrams_source, names_source, n)
    strict = {str(Path(p)) for p in (strict_json or ())}
    files, missing = _walk(paths)
    c = _new_counts(index)
    c["files_missing"] = len(missing)
    failures = [f"missing_path:{m}" for m in missing] + [f"index_problem:{x}" for x in index.problems]
    scanned = {str(f) for f, _ in files}
    for k, (f, rel) in enumerate(files):
        try:
            data = f.read_bytes()
        except OSError:
            data = None
        hits, reason = ([], "unreadable") if data is None else scan_named_bytes(
            index, f.name, data, aggregate_json=_is_aggregate_json(f, strict))
        if reason:
            c["files_unscannable"] += 1
            c["unscannable_reasons"][reason] = c["unscannable_reasons"].get(reason, 0) + 1
            failures.append(f"unscannable:{f}")
            continue
        c["files_scanned"] += 1
        listed = 0
        for loc, kind, off in hits:
            if kind == "unverifiable_svg":
                if any(str(f.with_suffix(ext)) in scanned for ext in _SVG_DATA_SIBLINGS):
                    continue                              # its data file is scanned too
            _count_hit(c, kind)
            if listed < MAX_HITS_PER_FILE:
                listed += 1
                failures.append(f"{kind}:{f}{loc}@{off}")
            else:
                c["hits_not_listed"] += 1
        for kind, _off in index.scan(rel, suffix=""):               # the file's own name (and folder) can name someone
            _count_hit(c, kind)
            failures.append(f"{kind}:{f.parent}/<name of file #{k}>")
    return AuditResult("leaks", True, not failures, c, failures)


def repo_files(repo) -> list[str]:
    """``git ls-files`` of ``repo`` as absolute paths (the tracked working tree)."""
    out = git(repo, "ls-files", "-z")
    root = Path(repo).resolve()
    return [str(root / p) for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _tree_entries(repo, *args: str) -> list[tuple[str, str]]:
    """[(blob sha, path)] of ``git ls-files -s`` / ``git ls-tree -r`` output (submodules left out)."""
    try:
        raw = git(repo, *args, "-z")
    except subprocess.CalledProcessError:                 # no HEAD yet
        return []
    out = []
    for rec in raw.split(b"\0"):
        if not rec or b"\t" not in rec:
            continue
        meta, path = rec.split(b"\t", 1)
        fields = meta.split()
        if fields[0] == b"160000" or (len(fields) > 1 and fields[1] == b"commit"):
            continue
        sha = fields[1] if args[0] == "ls-files" else fields[2]
        out.append((sha.decode(), path.decode("utf-8", "surrogateescape")))
    return out


def _object_types(repo, shas: list[str]) -> dict[str, str]:
    if not shas:
        return {}
    raw = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", input=("\n".join(shas) + "\n").encode())
    return {ln.split()[0]: ln.split()[1] for ln in raw.decode().splitlines() if len(ln.split()) == 2}


def _tag_messages(repo) -> dict[str, str]:
    """{tag name hash: message} of the annotated tags (the names of lightweight tags are not content)."""
    out: dict[str, str] = {}
    raw = git(repo, "for-each-ref", "refs/tags", "--format=%(objecttype) %(objectname)").decode().splitlines()
    for ln in raw:
        kind, _, sha = ln.partition(" ")
        if kind == "tag":
            out[sha] = git(repo, "cat-file", "tag", sha).decode("utf-8", "surrogateescape")
    return out


_LFS = b"version https://git-lfs.github.com/spec/"


def _parse_tree(raw: bytes, hexlen: int) -> list[tuple[bytes, str, str]]:
    """[(mode, name, object hex)] of a raw tree object."""
    out, i, n, w = [], 0, len(raw), hexlen // 2
    while i < n:
        sp = raw.index(b" ", i)
        nul = raw.index(b"\0", sp)
        out.append((raw[i:sp], raw[sp + 1:nul].decode("utf-8", "surrogateescape"), raw[nul + 1:nul + 1 + w].hex()))
        i = nul + 1 + w
    return out


def _history_paths(repo, rng: str, trees: list[str], root_trees: list[str]) -> dict[str, str]:
    """{full path: object sha} of every path (file or directory) that any commit in ``rng`` has, whatever blob or tree it
    holds: the paths of a renamed-and-renamed-back file, of a renamed directory, or of a file that exists only in a merge
    are all in the trees, while ``rev-list --objects`` names a shared object by its first path only."""
    if not root_trees:
        return {}
    hexlen = len(root_trees[0])
    raw = cat_blobs(repo, trees)
    entries = {t: _parse_tree(raw.get(t, b""), hexlen) for t in trees}
    out: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    stack = [(t, "") for t in dict.fromkeys(root_trees)]
    while stack:
        tree, prefix = stack.pop()
        if (tree, prefix) in seen:
            continue
        seen.add((tree, prefix))
        for mode, name, sha in entries.get(tree, ()):
            if mode == b"160000":
                continue
            path = f"{prefix}{name}"
            out.setdefault(path, sha)
            if mode == b"40000":
                stack.append((sha, path + "/"))
    return out


def audit_repo(repo, forbidden_ngrams_source, names_source=None, *, n: int = NGRAM,
               since_upstream: bool = False) -> AuditResult:
    """What ``git push`` would publish. Refuses (a failure) when the working tree differs from HEAD or the index.
    Scans every blob of the index and of HEAD and every blob reachable from HEAD (``git rev-list --objects``: the
    blobs of merge commits and of every other commit, whatever the diff against a parent shows), every path, every
    commit message and every annotated tag message, and every full path (file or directory) of the trees of every commit,
    so a file that was renamed and renamed back, a renamed directory, or a path that exists only in a merge is found
    by name even when its bytes are shared with another file. ``since_upstream`` limits the history to ``@{upstream}..HEAD``
    (the index and HEAD are always scanned). A git-LFS pointer is unscannable (the real content is elsewhere). A leak
    that was committed and then removed, resolved in a merge, staged and then cleaned, or that sits only in a file
    name is therefore caught. BLOCKING."""
    index = _as_index(forbidden_ngrams_source, names_source, n)
    repo = Path(repo)
    c = _new_counts(index)
    c.update(commits_scanned=0, blobs_scanned=0, paths_scanned=0, dirty_entries=0, has_upstream=0, tags_scanned=0)
    failures = [f"index_problem:{x}" for x in index.problems]
    dirty = [e for e in git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no").split(b"\0") if e]
    if dirty:
        c["dirty_entries"] = len(dirty)
        failures.append(f"dirty_tree:{len(dirty)}")
    entries = _tree_entries(repo, "ls-files", "-s") + _tree_entries(repo, "ls-tree", "-r", "HEAD")
    try:
        up = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").decode().strip()
        c["has_upstream"] = 1
    except subprocess.CalledProcessError:
        up = None
    rng = f"{up}..HEAD" if (up and since_upstream) else "HEAD"
    try:
        commits = git(repo, "rev-list", rng).decode().split()
        objs = git(repo, "rev-list", "--objects", rng).decode("utf-8", "surrogateescape").splitlines()
    except subprocess.CalledProcessError:                 # no HEAD yet
        commits, objs = [], []
    listed = {ln.split(" ", 1)[0]: (ln.split(" ", 1)[1] if " " in ln else "") for ln in objs if ln}
    types = _object_types(repo, list(listed))
    entries += [(sha, path) for sha, path in listed.items() if types.get(sha) == "blob"]
    tree_shas = [sha for sha in listed if types.get(sha) == "tree"]
    root_trees = git(repo, "log", "--format=%T", rng).decode().split() if commits else []
    messages: dict[str, str] = {}
    for cm in commits:
        messages[cm] = git(repo, "show", "-s", "--format=%B", cm).decode("utf-8", "surrogateescape")
    tags = _tag_messages(repo)
    c["commits_scanned"], c["tags_scanned"] = len(commits), len(tags)
    first: dict[str, str] = {}
    paths: dict[str, str] = {}
    for sha, path in entries:
        first.setdefault(sha, path)
        if path:
            paths.setdefault(path, sha)
    for path, sha in _history_paths(repo, rng, tree_shas, root_trees).items():
        paths.setdefault(path, sha)
    blobs = cat_blobs(repo, list(first))
    for sha, path in first.items():
        data = blobs.get(sha, b"")
        if data.startswith(_LFS):
            hits, reason = [], "lfs_pointer"
        else:
            hits, reason = scan_named_bytes(index, PurePosixPath(path).name, data,
                                            aggregate_json="aggregates" in PurePosixPath(path).parts)
        if reason:
            c["files_unscannable"] += 1
            c["unscannable_reasons"][reason] = c["unscannable_reasons"].get(reason, 0) + 1
            failures.append(f"unscannable:{path}#{sha[:7]}")
            continue
        c["blobs_scanned"] += 1
        c["files_scanned"] += 1
        for loc, kind, off in hits:
            _count_hit(c, kind)
            failures.append(f"{kind}:{path}#{sha[:7]}{loc}@{off}")
    for path, sha in paths.items():
        c["paths_scanned"] += 1
        for kind, _off in index.scan(path):
            _count_hit(c, kind)
            failures.append(f"{kind}:path of object #{sha[:7]}")
    for label, group in (("commit message", messages), ("tag message", tags)):
        for key, text in group.items():
            for kind, off in index.scan(text):
                _count_hit(c, kind)
                failures.append(f"{kind}:{label} #{key[:7]}@{off}")
    return AuditResult("leaks", True, not failures, c, failures)


def merge_results(name: str, *rs: AuditResult) -> AuditResult:
    """One result from several (numbers added, nested counts merged, failures concatenated)."""
    def add(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {k: add(a.get(k, 0), b.get(k, 0)) if k in a and k in b else a.get(k, b.get(k)) for k in {*a, *b}}
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            return a + b
        return b
    counts: dict = {}
    for r in rs:
        counts = add(counts, r.counts) if counts else dict(r.counts)
    return AuditResult(name, True, all(r.passed for r in rs), dict(sorted(counts.items())),
                       [f for r in rs for f in r.failures], [w for r in rs for w in r.warnings])


class LeakGuard:
    """A pre-write check for text the pipeline is about to write: ``guard(path, text)`` raises ``LeakBlocked``
    (naming counts and offsets only) instead of letting the write happen."""

    def __init__(self, index: LeakIndex):
        self.index = index

    @classmethod
    def from_config(cls, cfg) -> "LeakGuard":
        return cls(LeakIndex.from_config(cfg))

    def __call__(self, path, text: str) -> None:
        p = Path(path)
        c: dict = {"files_scanned": 1, "index_id_map_entries": self.index.stats.get("id_map_entries", 0)}
        failures = [f"index_problem:{x}" for x in self.index.problems]
        for kind, off in self.index.scan(text, json_aggregate=_is_aggregate_json(p, set()), suffix=p.suffix):
            _count_hit(c, kind)
            failures.append(f"{kind}:{p.name}@{len(text[:off].encode('utf-8', 'surrogateescape'))}")
        if failures:
            raise LeakBlocked([AuditResult("leaks", True, False, c, failures)])


# ------------------------------------------------------------------ running the auditors inside the pipeline

def _setting(ctx, key: str, default):
    return (ctx.cfg.corpus or {}).get(key, default)


def run_canary(cfg, clients: dict, stages=None) -> dict:
    """Run the synthetic corpus through the SAME stages, prompts and clients, in this session, into
    ``<out_dir>/canary`` (wiped first: a canary served from an earlier session's work files would prove nothing).
    ``corpus.canary`` may name another synthetic run config; the default is the kit's demo config. Returns the
    pipeline result of the synthetic run. ``stages`` (default: every stage) limits it to the stage groups whose
    clients the session has."""
    from . import pipeline
    from .audit.registry import AuditLog
    path = _canary_path(cfg)
    out = Path(cfg.out_dir) / "canary"
    if out.exists():
        shutil.rmtree(out)
    ccfg = pipeline.RunConfig.load(path, out_dir=str(out))
    if not pipeline.is_synthetic(ccfg):
        raise ValueError("the canary config must be a synthetic corpus")
    (out / "logs").mkdir(parents=True)
    log = AuditLog(out / "logs" / "run.jsonl", ccfg.run_id)
    try:
        return pipeline.run(ccfg, list(stages) if stages else pipeline.STAGE_ORDER, clients, log=log)
    finally:
        log.close()


def _determinism_results(stage: str, ctx) -> list[AuditResult]:
    from .llm import derive_seed, remote_entry
    k = int(_setting(ctx, "audit_determinism_k", DETERMINISM_K))
    roles = ("recon",) if stage == "Q" else ("judge", "judge2")
    out = []
    for role in roles:
        client = (ctx.clients or {}).get(role)
        if client is None or k <= 0:
            continue
        sample = sample_from_log(ctx.log.path, k, derive_seed(ctx.cfg.run_id, "determinism", role), {role})
        if sample:
            remote = remote_entry(role, getattr(client, "cfg", None),
                                  (getattr(ctx.cfg, "tool", None) or {}).get("remote_endpoints")) is not None
            r = audit_determinism(None, client, sample, k, seed=derive_seed(ctx.cfg.run_id, "determinism", role),
                                  remote=remote)
            r.name = f"determinism_{role}"
            out.append(r)
    return out


def _canary_result(ctx) -> AuditResult | None:
    """The canary, once per ``run`` call, after the first model stage group that ran (real corpus only)."""
    if ctx.cfg.corpus.get("kind") == "synthetic" or not ctx.clients or getattr(ctx, "canary_done", False):
        return None
    ctx.canary_done = True
    if (getattr(ctx.cfg, "tool", None) or {}).get("model_check"):
        return _model_check_reuse(ctx)      # K10, tool configs only: the Setup model check stands in for the canary
    if "judge" in ctx.clients and "recon" not in ctx.clients:
        # A judge-only session (the reconstructor's server is stopped on purpose): the synthetic canary would run the Q
        # stages and cannot. Its job for the judge is done by the preflight (planted items through the judge) and the
        # end-of-J recheck (the same items again). Not a pass of the canary: a separate, non-blocking result says it was
        # skipped, and the reconstruction canary is covered by the Q session that made the maps.
        return AuditResult("canary_skipped", False, True, {"skipped_no_recon_client": 1}, [],
                           ["canary_skipped:no_recon_client"])
    try:
        if "judge" in ctx.clients:
            res = audit_canary(run_canary(ctx.cfg, ctx.clients))
        else:       # a Q-only session has no judge client: its canary is the reconstruction stages alone
            from . import pipeline
            run_canary(ctx.cfg, ctx.clients, stages=pipeline.STAGE_GROUPS["Q"])
            res = audit_canary_recon(Path(ctx.cfg.out_dir) / "canary" / "work")
    except Exception as exc:  # noqa: BLE001 - a canary that cannot run is a failed canary
        res = AuditResult("canary", True, False, {"error": type(exc).__name__}, [f"canary_error:{type(exc).__name__}"])
    return res


MODEL_CHECK_SCHEMA = "model-check/1"


def _model_check_reuse(ctx) -> AuditResult:
    """K10: the canary of a tool session is the pair's Setup model check, read from ``tool.model_check.path`` and
    trusted only when the file's sha256 is the one the (hashed) config names. ``passed``, ``models``, ``digests`` and
    ``prompt_set_sha256`` come from that file, never from the config: the check must have passed, every model of this
    session must be the one it checked (``model_ids(clients)``), its digests must equal ``tool.model_digests`` and its
    prompt set must be the live one. A match is a passed canary with no model call; anything else blocks with
    ``model_check_mismatch:<field>``."""
    from . import pipeline
    mc = ctx.cfg.tool["model_check"]

    def mismatch(*fields) -> AuditResult:
        return AuditResult("canary", True, False, {}, [f"model_check_mismatch:{f}" for f in fields])
    try:
        raw = Path(mc["path"]).read_bytes()
    except OSError:
        return mismatch("path")
    if hashlib.sha256(raw).hexdigest() != mc["sha256"]:
        return mismatch("sha256")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return mismatch("schema")
    if not isinstance(doc, dict) or doc.get("schema") != MODEL_CHECK_SCHEMA:
        return mismatch("schema")
    bad = []
    if doc.get("passed") is not True:
        bad.append("passed")
    checked = doc.get("models")
    session = pipeline.model_ids(ctx.clients)
    if not isinstance(checked, dict) or not session or any(checked.get(r) != m for r, m in session.items()):
        bad.append("models")
    if (doc.get("digests") or {}) != (ctx.cfg.tool.get("model_digests") or {}):
        bad.append("digests")
    if doc.get("prompt_set_sha256") != pipeline.prompt_set_sha256():
        bad.append("prompt_set_sha256")
    if bad:
        return mismatch(*bad)
    counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
    return AuditResult("canary", True, True, {"reused": True, "model_check": counts}, [])


_CACHE_FILES = ("judge_cache.json", "judge2_cache.json")


def begin_session(ctx) -> None:
    """Mark the start of a pipeline session (one ``run`` call): an id, the time, and the judge caches as they were, so a
    session whose canary fails can be taken back (``quarantine_session``)."""
    ctx.session_id = uuid.uuid4().hex[:12]
    ctx.session_start_ns = time.time_ns()
    root = Path(ctx.cfg.out_dir) / "work"
    ctx.cache_snapshot = {name: ((root / name).read_bytes() if (root / name).exists() else None)
                          for name in _CACHE_FILES}


def quarantine_session(ctx, only: tuple | list | None = None) -> int:
    """Take back everything this session produced: every work item written since the session began gets
    ``status: "quarantined"`` (the session id kept, the old status in ``original_status``), so a resume redoes it and no
    later stage reads it; the judge caches go back to what they were. ``only`` limits it to the work folders of those
    stages (a judge that fails at the end of J takes back the J items, not the reconstructions the post-Q canary
    passed). Returns the number of items."""
    from .pipeline import write_json_atomic
    root = Path(ctx.cfg.out_dir) / "work"
    n = 0
    for p in sorted(root.rglob("*.json")) if root.exists() else []:
        # a ladder rung's store is "<stage>:<rung key>" (judge_modality:rung3): it belongs to the stage before the colon
        if p.name in _CACHE_FILES or (only is not None
                                      and stage_from_dirname(p.relative_to(root).parts[0]).split(":")[0] not in only):
            continue
        try:
            if p.stat().st_mtime_ns < ctx.session_start_ns:
                continue
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("status"), str) and obj["status"] != "quarantined":
            taken_back = {**obj, "original_status": obj["status"], "status": "quarantined",
                          "quarantined_session": ctx.session_id}
            if (log := getattr(ctx, "log", None)) is not None:     # announced in the log, like every work-store write
                log_work_item(log, stage_from_dirname(p.parent.name), p.stem, taken_back)
            write_json_atomic(p, taken_back)
            n += 1
    for name, content in (getattr(ctx, "cache_snapshot", None) or {}).items():
        p = root / name
        if content is None:
            if p.exists():
                _log_cache_restore(ctx, p, None)
                p.unlink()
        elif not p.exists() or p.read_bytes() != content:
            _log_cache_restore(ctx, p, content)
            p.write_bytes(content)
    return n


def _log_cache_restore(ctx, path: Path, content: bytes | None) -> None:
    """Log every judge-cache entry the restore of ``path`` changes or removes (``content``: the snapshot, None = the
    cache did not exist), so the log keeps matching the file."""
    log = getattr(ctx, "log", None)
    if log is None:
        return

    def read(raw) -> dict:
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}
    now = read(path.read_bytes() if path.exists() else None)
    log_cache_changes(log, path.stem, now, read(content))


def mark_stale(cfg) -> int:
    """After a refused write or a failed output scan: move the outputs of earlier runs (aggregates, report, traps,
    audit summary) out of ``aggregates/`` into ``<out_dir>/stale/``, so nothing that no longer matches the run, or
    that failed the scan, can be picked up and sent. Returns how many were moved."""
    src = Path(cfg.out_dir) / "aggregates"
    dst = Path(cfg.out_dir) / "stale"
    n = 0
    for name in ("aggregates.json", "report.md", "traps.md", "audits.json"):
        if (src / name).exists():
            dst.mkdir(parents=True, exist_ok=True)
            os.replace(src / name, dst / name)
            n += 1
    return n


def _canary_path(cfg):
    from . import pipeline
    spec = (cfg.corpus or {}).get("canary", True)
    return spec if isinstance(spec, str) else pipeline.KIT_ROOT / "configs" / "demo.json"


def _planted_items(ccfg, rr) -> tuple[list, dict]:
    """Up to four modality items planted in the synthetic corpus (two wished, two negated) and what each must be."""
    from .synth.bank import CONCEPTS
    root = ccfg.path(ccfg.corpus["synthetic_dir"])
    bank = {x.cid: x for x in CONCEPTS}
    picks: dict[str, list] = {k: [] for k in CANARY_KINDS}
    for p in rr.people:
        for t in json.loads((root / "traps" / f"{p.pid}.json").read_text(encoding="utf-8")):
            m = t.get("expected_modality")
            if m in picks and len(picks[m]) < 2 and t.get("sentence") and t.get("cid") in bank:
                picks[m].append(t)
    items, expected = [], {}
    for kind, ts in picks.items():
        for i, t in enumerate(ts):
            items.append({"element_id": f"{kind}{i}", "element": bank[t["cid"]].forms[0],
                          "quote": t["sentence"], "speaker": t.get("speaker") or "participant"})
            expected[f"{kind}{i}"] = kind
    return items, expected


def _judge_planted(log, client, items, expected, counts: dict, tag: str) -> list[str]:
    """Judge the planted items; fill ``counts`` with the rate per kind and return the failure codes."""
    from . import pipeline
    from .judge import judge_modality_detailed
    labels, not_judged = judge_modality_detailed(log, client, items, pipeline.PROMPTS_DIR, item_id=tag,
                                                 stage=f"{tag}:judge")
    failures: list[str] = []
    for kind in CANARY_KINDS:
        ids = [e for e, k in expected.items() if k == kind]
        ok = sum(labels.get(e) == kind and e not in not_judged for e in ids)
        rate = round(ok / len(ids), 4) if ids else None
        counts[kind] = {"n": len(ids), "pass": ok, "rate": rate}
        if rate is None:
            failures.append(f"{tag}_no_items:{kind}")
        elif rate < CANARY_MIN_PASS:
            failures.append(f"{tag}_below_rule:{kind}")
    return failures


def _judge_recheck(ctx) -> AuditResult | None:
    """The judge again, at the end of the J stage group (real corpus only): the planted items of the preflight go through
    the same judge client once more. A judge that was right at the start and wrong now (the server swapped, a model
    reloaded, a drifting endpoint) fails here, and the session's J items are taken back."""
    from . import pipeline
    from .audit.registry import AuditLog
    if ctx.cfg.corpus.get("kind") == "synthetic" or (ctx.clients or {}).get("judge") is None:
        return None
    out = Path(ctx.cfg.out_dir) / "preflight_post_j"
    if out.exists():
        shutil.rmtree(out)
    (out / "logs").mkdir(parents=True)
    c: dict = {"min_pass_rate": CANARY_MIN_PASS}
    try:
        ccfg = pipeline.RunConfig.load(_canary_path(ctx.cfg), out_dir=str(out))
        log = AuditLog(out / "logs" / "run.jsonl", ccfg.run_id)
        try:
            planted = getattr(ctx, "planted_items", None)
            if planted is None:
                planted = _planted_items(ccfg, pipeline._Run(ccfg, ctx.clients, log))
            failures = _judge_planted(log, ctx.clients["judge"], *planted, c, "canary_judge")
        finally:
            log.close()
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check
        c["error"] = type(exc).__name__
        failures = [f"canary_judge_error:{type(exc).__name__}"]
    return AuditResult("canary_judge", True, not failures, c, failures)


def _run_preflight(ctx, skip: frozenset = frozenset()) -> AuditResult:
    """A small synthetic check before the first real model call: two synthetic transcripts through the reconstructor
    client (the map must be valid and its quotes verbatim) and up to four planted modality items (two wished, two negated)
    through the judge client; the wished and negated items must all come back right. ``skip`` (tool sessions only, see
    ``preflight``) names the roles this session does not call."""
    from . import pipeline
    from .audit.registry import AuditLog
    from .reconstruct import ReconSpec, reconstruct
    cfg, clients = ctx.cfg, ctx.clients
    out = Path(cfg.out_dir) / "preflight"
    if out.exists():
        shutil.rmtree(out)
    ccfg = pipeline.RunConfig.load(_canary_path(cfg), out_dir=str(out))
    if not pipeline.is_synthetic(ccfg):
        raise ValueError("the canary config must be a synthetic corpus")
    (out / "logs").mkdir(parents=True)
    log = AuditLog(out / "logs" / "run.jsonl", ccfg.run_id)
    c: dict = {"recon_n": 0, "recon_ok": 0, "min_pass_rate": CANARY_MIN_PASS}
    failures: list[str] = []
    try:
        rr = pipeline._Run(ccfg, clients, log)
        if clients.get("recon") is not None and "recon" not in skip:
            for p in rr.people[:2]:
                g, status = reconstruct(log, clients["recon"], rr.rule_flagged(p), ReconSpec("full", "p0", 0.0),
                                        rr.ctx(p.role), pipeline.PROMPTS_DIR, item_id=f"preflight:{p.pid}",
                                        stage="preflight:recon")
                c["recon_n"] += 1
                c["recon_ok"] += status == "ok" and not _check_map(asdict(g), "full", ccfg.context.get("phases"))
            if c["recon_ok"] < c["recon_n"]:
                failures.append("preflight_recon")
        if clients.get("judge") is not None and "judge" not in skip:
            items, expected = _planted_items(ccfg, rr)
            ctx.planted_items = (items, expected)
            failures += _judge_planted(log, clients["judge"], items, expected, c, "preflight")
    finally:
        log.close()
    return AuditResult("preflight", True, not failures, c, failures)


def preflight(ctx) -> None:
    """Before the first real model call of a session (real corpus only): the small synthetic check above. A failure
    raises ``AuditBlocked`` before any real item exists. A real corpus cannot switch the canary off."""
    cfg = ctx.cfg
    if cfg.corpus.get("kind") == "synthetic" or not ctx.clients:
        return
    if (cfg.corpus or {}).get("canary", True) is False:
        raise AuditBlocked([AuditResult("preflight", True, False, {}, ["canary_disabled:real_corpus"])])
    tool = bool(getattr(cfg, "tool", None))
    skip = _preflight_skip(ctx) if tool else frozenset()      # K10: study configs call every role, as before
    try:
        res = _run_preflight(ctx, skip)
        if "recon" in skip:      # a tool J session: the reconstructor's preflight is the Q session's, from the log
            res.counts["recon_reused"] = int(_q_preflight_logged(ctx))
            if not res.counts["recon_reused"]:
                res.warnings.append("preflight_recon_not_reused:0")
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check
        res = AuditResult("preflight", True, False, {"error": type(exc).__name__},
                          [f"preflight_error:{type(exc).__name__}"])
    if not res.passed:
        raise AuditBlocked([res])
    ctx.preflight_result = res


def _log_records(ctx, types: tuple[str, ...]) -> list[dict]:
    """The records of this run's log (``ctx.log.path``) whose type is in ``types``, in order; [] when unreadable."""
    path = getattr(getattr(ctx, "log", None), "path", None)
    needles = [f'"record_type":"{t}"'.encode() for t in types]
    out = []
    try:
        with open(path, "rb") as fh:
            for line in fh:
                if any(n in line for n in needles):
                    rec = json.loads(line)
                    if rec.get("record_type") in types:
                        out.append(rec)
    except (OSError, TypeError, ValueError):
        return []
    return out


def _preflight_skip(ctx) -> frozenset:
    """K10, tool configs only: the roles whose stage group this session does not run (from the session's own
    ``run_start`` record, the last one in the log), so a J session sends no reconstructor call and a Q session no judge
    call. A session whose stages cannot be read skips nothing (today's preflight)."""
    from . import pipeline
    starts = _log_records(ctx, ("run_start",))
    stages = starts[-1].get("payload", {}).get("stages") if starts else None
    try:
        names = set(pipeline.expand_stages(stages)) if isinstance(stages, list) and stages else None
    except ValueError:
        names = None
    if names is None:
        return frozenset()
    skip = set()
    if not names & set(pipeline.STAGE_GROUPS["Q"]):
        skip.add("recon")
    if not names & set(pipeline.STAGE_GROUPS["J"]):
        skip.add("judge")
    return frozenset(skip)


def _q_preflight_logged(ctx) -> bool:
    """True when this run's log holds an ``audit_summary`` with a passed ``preflight`` entry that checked the
    reconstructor (every one of at least one map valid), from a session whose ``run_start`` names the current
    reconstructor model id and this config's hash (the digests are in the hashed ``tool.model_digests``)."""
    from . import pipeline
    recon = pipeline.model_ids(ctx.clients or {}).get("recon")
    if recon is None and callable(getattr(ctx, "recorded_models", None)):
        recon = ctx.recorded_models().get("recon")
    if not recon:
        return False
    sha = ctx.cfg.sha256()
    start = None
    for rec in _log_records(ctx, ("run_start", "audit_summary")):
        pl = rec.get("payload") or {}
        if rec["record_type"] == "run_start":
            start = pl
            continue
        if start is None or (start.get("models") or {}).get("recon") != recon or start.get("config_sha256") != sha:
            continue
        for a in pl.get("audits") or []:
            c = a.get("counts") or {} if isinstance(a, dict) else {}
            if (isinstance(a, dict) and a.get("name") == "preflight" and a.get("passed") is True
                    and isinstance(c.get("recon_n"), int) and c["recon_n"] > 0 and c.get("recon_ok") == c["recon_n"]):
                return True
    return False


def _output_leak_audit(ctx) -> AuditResult:
    agg_dir = Path(ctx.cfg.out_dir) / "aggregates"
    guard = ctx.leak_guard()
    if not agg_dir.exists():
        return AuditResult("leaks", True, True, {"files_scanned": 0, "no_outputs": 1}, [])
    strict = [str(p) for p in agg_dir.rglob("*.json")]
    return audit_leaks([agg_dir], guard.index, strict_json=strict)


def run_all(stage: str, ctx) -> list[AuditResult]:
    """The auditors of one stage group (``Q``, ``J`` or ``M``), run on the pipeline context ``ctx``. Real corpora get
    anonymous participant references in every failure."""
    cfg = ctx.cfg
    synthetic = cfg.corpus.get("kind") == "synthetic"
    work = ctx.work.root
    out: list[AuditResult] = []
    if stage == "Q":
        trs = {p.pid: t for p in ctx.people if (t := ctx.transcript(p)) is not None}
        # the script sources' maps are drawn in J, so they are not in recon_outputs: a run that resumes with them on
        # disk audits what a Q-then-J run audited at Q (same report bytes)
        out.append(audit_recon_outputs(work, trs, phases=(cfg.context or {}).get("phases"), anonymize=not synthetic,
                                       script_sources=False))
    elif stage == "J":
        trs = {p.pid: t for p in ctx.people if (t := ctx.transcript(p)) is not None}
        from .pipeline import PROMPTS_DIR
        out.append(audit_judge_outputs(work, anonymize=not synthetic, transcripts=trs, prompts_dir=PROMPTS_DIR))
        out.append(audit_second_judge(work))
    elif stage == "M":
        out.append(AuditResult("leaks", True, True, {"skipped_synthetic": 1}, []) if synthetic
                   else _output_leak_audit(ctx))
    if stage in ("Q", "J"):
        pre = getattr(ctx, "preflight_result", None)
        if pre is not None:
            out.append(pre)
            ctx.preflight_result = None
        out += _determinism_results(stage, ctx)
        canary = _canary_result(ctx)
        if canary is not None:
            out.append(canary)
    if stage == "J":
        recheck = _judge_recheck(ctx)
        if recheck is not None:
            out.append(recheck)
    return out


def audits_dir(cfg) -> Path:
    return Path(cfg.out_dir) / "audits"


def _write(path: Path, obj) -> None:
    from .report import write_text_atomic
    write_text_atomic(path, json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n")


def write_stage_results(cfg, stage: str, results: list[AuditResult]) -> Path:
    """``<out_dir>/audits/<stage>.json``: the full results (counts, ids, offsets; never text)."""
    p = audits_dir(cfg) / f"{stage}.json"
    _write(p, {"stage": stage, "results": [asdict(r) for r in results]})
    return p


def load_summaries(cfg) -> dict:
    """{stage: [summary, ...]} from the stage files on disk."""
    out = {}
    for p in sorted(audits_dir(cfg).glob("*.json")) if audits_dir(cfg).exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if p.stem in ("Q", "J", "M") and isinstance(d, dict):
            out[p.stem] = [AuditResult(**r).summary() for r in d.get("results", [])]
    return out


def write_summary(cfg, guard=None) -> Path:
    """``aggregates/audits.json``: counts and code tallies of every auditor run so far (no ids, no paths), checked by
    the leak guard before it is written when there is one."""
    from .report import out_paths
    p = out_paths(cfg)["audits"]
    text = json.dumps({"stages": load_summaries(cfg)}, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if guard is not None:
        guard(p, text)
    from .report import write_text_atomic
    write_text_atomic(p, text)
    return p


def run_stage_audits(stage: str, ctx) -> list[AuditResult]:
    """Run the auditors of a stage group, write ``audits/<stage>.json`` and ``aggregates/audits.json``, and raise
    ``AuditBlocked`` if a blocking auditor failed (the pipeline stops there)."""
    ctx._guard = None                    # the real texts of the index include work/, which has just changed
    results = run_all(stage, ctx)
    failed_canary = next((r for r in results if r.name == "canary" and not r.passed), None)
    failed_recheck = next((r for r in results if r.name == "canary_judge" and not r.passed), None)
    if failed_canary is not None:        # the server or the model is not what it was: nothing of this session stands
        failed_canary.counts["quarantined_items"] = quarantine_session(ctx)
        ctx.log.append("quarantine", {"session": ctx.session_id, "items": failed_canary.counts["quarantined_items"]})
    elif failed_recheck is not None:     # the judge drifted during J: take back the session's J items only
        from .pipeline import STAGE_GROUPS
        failed_recheck.counts["quarantined_items"] = quarantine_session(ctx, STAGE_GROUPS["J"])
        ctx.log.append("quarantine", {"session": ctx.session_id, "items": failed_recheck.counts["quarantined_items"]})
    write_stage_results(ctx.cfg, stage, results)
    ctx.audit_results = [*getattr(ctx, "audit_results", []), *results]
    write_summary(ctx.cfg, ctx.leak_guard())
    if any(r.blocking and not r.passed for r in results):
        exc = AuditBlocked(results)
        exc.details_path = audits_dir(ctx.cfg) / f"{stage}.json"
        raise exc
    return results


def record_blocked(cfg, exc: AuditBlocked) -> Path:
    """Keep the evidence of a block that did not come from a stage group (a pre-write guard): ``audits/blocked.json``."""
    p = audits_dir(cfg) / "blocked.json"
    _write(p, {"results": [asdict(r) for r in exc.results]})
    exc.details_path = p
    return p
