"""Counts and a time projection for a judge-phase pilot (``cli jcounts``). Prints counts and seconds only, never a label,
quote, transcript or model reply.

``jcounts --config PILOT_CFG --full-config FULL_CFG [--json]``

- Judge calls of the pilot's main log after its ``run_fork`` record (or of the whole log without one), per audit-log
  stage: requests, transport errors, replies that failed validation, and seconds per call (the client's ``latency_ms``;
  the gap between the request and its reply in the log when that is missing): median, p90, mean; median prompt and
  completion tokens when the server reports usage.
- Outcomes of the pilot's J work items: status counts per store (rung stores folded into their base), elements not
  judged, failed and unparsed judge batches, fallback helper items.
- Session overhead: wall time of each J session (run_start to run_end of the main log) and the calls of the preflight
  and end-of-J recheck logs. The determinism audit's re-asks (``audit_determinism_k``, 20 by default) are not logged
  and show up only in the wall time.
- ``verify`` of the pilot log (chain, manifest, run validity, work integrity).
- Projection for the full run: for each stage, calls per unit in the pilot x units of the full run x mean seconds. The
  units are counted the same way in both out_dirs from the Q work items (map JSON, counts only):
    in_transcript: participants; modality: quoted nodes and edges of the ok AI maps (main grid + ladder rungs);
    shuffled: quoted nodes of the ok primary-spec maps that have at least two; script_prior_only: script items the
    config asks for (sources x variants x samples x roles); canon: distinct normalized labels of the ok main-grid AI maps
    and prior-only maps (the endorsed maps are left out of both sides); align: ok AI maps (main grid + rungs).
  It is an estimate: the judge cache, the label overlap across participants and the pilot's few participants all move it.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime
from pathlib import Path

from . import pipeline, prereg
from .audit import verify as audit_verify
from .paths import stage_dirname, stage_from_dirname
from .textnorm import normalize

STAGE_UNITS = {
    "judge:in_transcript": "participants", "judge:modality": "quoted_elements", "judge:shuffled": "shuffled_nodes",
    "judge:script_prior_only": "script_items", "judge:canon": "labels", "judge:align": "ai_maps",
}
J_STORES = ("in_transcript", "judge_modality", "judge_shuffled", "script_prior_only", "canonicalize", "align",
            "judge2_modality")


def _stage_key(stage: str) -> str:
    """``judge:modality`` for ``judge:modality``, ``judge:canon`` for ``judge:canon``: the first two parts."""
    return ":".join(str(stage).split(":")[:2])


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    return ys[max(0, math.ceil(q * len(ys)) - 1)]


def _records(log: Path) -> list[dict]:
    return prereg._records(log) or []


def call_stats(records: list[dict]) -> dict:
    """{stage: counts and seconds} of the call records given."""
    req: dict[str, tuple[str, float]] = {}
    out: dict[str, dict] = {}
    for r in records:
        pl = r.get("payload") or {}
        rt = r.get("record_type")
        if rt == "call_request":
            st = _stage_key(pl.get("stage", ""))
            req[pl["request_id"]] = (st, _ts(r["ts"]))
            s = out.setdefault(st, {"calls": 0, "replies": 0, "errors": 0, "transport_errors": 0,
                                    "invalid_replies": 0, "_sec": [], "_pt": [], "_ct": []})
            s["calls"] += 1
        elif rt in ("call_response", "call_error") and pl.get("request_id") in req:
            st, t0 = req[pl["request_id"]]
            s = out[st]
            if rt == "call_error":
                s["errors"] += 1
                s["transport_errors"] += bool(pl.get("transport"))
                continue
            s["replies"] += 1
            s["invalid_replies"] += pl.get("validation_error") is not None
            lat = pl.get("latency_ms")
            s["_sec"].append(lat / 1000 if isinstance(lat, (int, float)) else _ts(r["ts"]) - t0)
            usage = pl.get("usage") or {}
            if isinstance(usage.get("prompt_tokens"), int):
                s["_pt"].append(usage["prompt_tokens"])
            if isinstance(usage.get("completion_tokens"), int):
                s["_ct"].append(usage["completion_tokens"])
    for s in out.values():
        sec, pt, ct = s.pop("_sec"), s.pop("_pt"), s.pop("_ct")
        s.update(seconds_total=round(sum(sec), 3), seconds_median=_round(statistics.median(sec) if sec else None),
                 seconds_p90=_round(_pct(sec, 0.9)), seconds_mean=_round(statistics.fmean(sec) if sec else None),
                 prompt_tokens_median=statistics.median(pt) if pt else None,
                 completion_tokens_median=statistics.median(ct) if ct else None)
    return dict(sorted(out.items()))


def _round(x):
    return None if x is None else round(x, 3)


def after_fork(records: list[dict]) -> list[dict]:
    idx = [i for i, r in enumerate(records) if r.get("record_type") == prereg.FORK_RECORD]
    return records[idx[-1] + 1:] if idx else records


def sessions(records: list[dict]) -> list[dict]:
    """Wall time and stages of each session (run_start .. run_end) of the records given."""
    out, cur = [], None
    for r in records:
        if r.get("record_type") == "run_start":
            cur = {"stages": (r.get("payload") or {}).get("stages"), "t0": _ts(r["ts"])}
        elif r.get("record_type") == "run_end" and cur is not None:
            out.append({"stages": cur["stages"], "wall_seconds": round(_ts(r["ts"]) - cur["t0"], 3)})
            cur = None
    if cur is not None:
        out.append({"stages": cur["stages"], "wall_seconds": None, "unfinished": True})
    return out


def outcomes(work_root: Path) -> dict:
    """Status counts of the J stores (rung stores folded into their base) and the judge's not-judged tallies."""
    out: dict[str, dict] = {}
    if not work_root.is_dir():
        return out
    for d in sorted(p for p in work_root.iterdir() if p.is_dir()):
        base = stage_from_dirname(d.name).split(":", 1)[0]
        if base not in J_STORES:
            continue
        o = out.setdefault(base, {"items": 0, "status": {}})
        for f in d.glob("*.json"):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                o["status"]["unreadable"] = o["status"].get("unreadable", 0) + 1
                continue
            o["items"] += 1
            st = str(item.get("status"))
            o["status"][st] = o["status"].get(st, 0) + 1
            if base in ("judge_modality", "judge_shuffled", "judge2_modality"):
                stats = item.get("stats") or {}
                o["elements"] = o.get("elements", 0) + int(item.get("n_items") or 0)
                o["not_judged"] = o.get("not_judged", 0) + len(item.get("not_judged") or [])
                o["failed_batches"] = o.get("failed_batches", 0) + int(stats.get("failed_batches") or 0)
                o["unparsed_batches"] = o.get("unparsed_batches", 0) + int(stats.get("unparsed_batches") or 0)
                o["batches"] = o.get("batches", 0) + int(stats.get("batches") or 0)
    for o in out.values():
        o["status"] = dict(sorted(o["status"].items()))
    return out


def _quoted(m: dict, edges: bool = True) -> int:
    n = sum(1 for x in m.get("nodes") or [] if (x.get("quote") or "").strip())
    return n + (sum(1 for e in m.get("edges") or [] if (e.get("quote") or "").strip()) if edges else 0)


def units(cfg: pipeline.RunConfig) -> dict:
    """The scaling units of a run (see the module docstring), counted from its Q work items."""
    work = pipeline.Work(cfg.out_dir)
    people = pipeline.load_participants(cfg)
    pids = {p.pid for p in people}
    roles = sorted({p.role for p in people})
    keys = [s.key() for s in cfg.recon_specs()]
    u = {"participants": len(people), "quoted_elements": 0, "shuffled_nodes": 0, "ai_maps": 0, "labels": 0,
         "script_items": sum(len(s["variants"]) * int(s["samples"]) * len(roles) for s in cfg.script_sources)}
    labels: set[str] = set()

    def ok_map(store: str, name: str) -> dict | None:
        d = work.get(store, name)
        return d["map"] if d and d.get("status") == "ok" and isinstance(d.get("map"), dict) else None

    for pid in sorted(pids):
        for k in keys:
            m = ok_map("recon", f"{pid}__{k}")
            if m is None:
                continue
            u["ai_maps"] += 1
            u["quoted_elements"] += _quoted(m)
            labels.update(normalize(x.get("text") or "") for x in m.get("nodes") or [])
            if k == keys[0] and (q := _quoted(m, edges=False)) >= 2:
                u["shuffled_nodes"] += q
    for store in sorted(stage_from_dirname(p.name) for p in work.root.glob(stage_dirname("recon:") + "*")
                        if p.is_dir()):
        for name in work.names(store):
            if name.split("__", 1)[0] in pids and (m := ok_map(store, name)) is not None:
                u["ai_maps"] += 1
                u["quoted_elements"] += _quoted(m)
    # the canonicalized labels: a separate reading-3 map (``reading3`` block) is not canonicalized, so it is left out
    reading3_only = {s.key() for s in cfg.reading3_specs()} - {s.key() for s in cfg.prior_specs()}
    for name in work.names("prior_only"):
        role, _, key = name.partition("__")
        if key in reading3_only:
            continue
        if role in roles and (m := ok_map("prior_only", name)) is not None:
            labels.update(normalize(x.get("text") or "") for x in m.get("nodes") or [])
    u["labels"] = len(labels)
    return u


def verify_text(log: Path, work: Path) -> str:
    ok, detail = audit_verify.verify_run(log)
    if not ok:
        return f"FAIL: {detail.get('reason')}"
    problem = prereg.run_validity_problem(log, work) or audit_verify.work_integrity_problem(log, work)
    return "PASS" if problem is None else f"FAIL: {problem}"


def report(pilot_config, full_config) -> dict:
    pcfg, fcfg = pipeline.RunConfig.load(pilot_config), pipeline.RunConfig.load(full_config)
    out = Path(pcfg.out_dir)
    log = out / "logs" / "run.jsonl"
    recs = after_fork(_records(log))
    calls = call_stats(recs)
    overhead = {}
    for sub in ("preflight", "preflight_post_j"):
        side = out / sub / "logs" / "run.jsonl"
        if side.is_file():
            overhead[sub] = call_stats(_records(side))
    pu, fu = units(pcfg), units(fcfg)
    proj, total = {}, 0.0
    for st, s in calls.items():
        unit = STAGE_UNITS.get(st)
        if unit is None or not pu.get(unit) or s["seconds_mean"] is None:
            proj[st] = {"unit": unit, "pilot_units": pu.get(unit), "full_units": fu.get(unit), "hours": None}
            continue
        calls_full = s["calls"] / pu[unit] * fu[unit]
        h = calls_full * s["seconds_mean"] / 3600
        total += h
        proj[st] = {"unit": unit, "pilot_units": pu[unit], "full_units": fu[unit], "calls": round(calls_full),
                    "hours": round(h, 2)}
    return {"pilot_participants": pu["participants"], "full_participants": fu["participants"],
            "calls": calls, "outcomes": outcomes(out / "work"), "sessions": sessions(recs), "overhead": overhead,
            "verify": verify_text(log, out / "work"), "projection": proj, "projected_hours_total": round(total, 2),
            "missing_stages": [s for s in STAGE_UNITS if s not in calls]}


def _fmt(x) -> str:
    return "-" if x is None else (f"{x:.1f}" if isinstance(x, float) else str(x))


def main(a) -> int:
    res = report(a.config, a.full_config)
    if a.json:
        print(json.dumps(res, indent=1, sort_keys=True))
        return 0 if res["verify"] == "PASS" else 1
    print(f"pilot: {res['pilot_participants']} participants; full run: {res['full_participants']}")
    print(f"verify: {res['verify']}")
    for s in res["sessions"]:
        print(f"session {','.join(s['stages'] or [])}: wall {_fmt(s['wall_seconds'])} s"
              + (" (unfinished)" if s.get("unfinished") else ""))
    print(f"{'stage':<26}{'calls':>6}{'err':>5}{'transp':>7}{'invalid':>8}{'med s':>8}{'p90 s':>8}{'mean s':>8}"
          f"{'in tok':>8}{'out tok':>8}")
    for st, s in res["calls"].items():
        print(f"{st:<26}{s['calls']:>6}{s['errors']:>5}{s['transport_errors']:>7}{s['invalid_replies']:>8}"
              f"{_fmt(s['seconds_median']):>8}{_fmt(s['seconds_p90']):>8}{_fmt(s['seconds_mean']):>8}"
              f"{_fmt(s['prompt_tokens_median']):>8}{_fmt(s['completion_tokens_median']):>8}")
    for sub, cs in res["overhead"].items():
        n = sum(s["calls"] for s in cs.values())
        t = sum(s["seconds_total"] for s in cs.values())
        print(f"overhead {sub}: {n} calls, {t:.1f} s")
    print("outcomes:")
    for store, o in res["outcomes"].items():
        extra = "".join(f", {k} {o[k]}" for k in ("elements", "not_judged", "batches", "failed_batches",
                                                    "unparsed_batches") if k in o)
        print(f"  {store:<20} items {o['items']}: " + ", ".join(f"{k} {v}" for k, v in o["status"].items()) + extra)
    print("projection for the full run (calls per unit in the pilot x full-run units x mean seconds):")
    for st, p in res["projection"].items():
        print(f"  {st:<26} unit {p['unit']}: pilot {_fmt(p['pilot_units'])}, full {_fmt(p['full_units'])}"
              + (f" -> ~{p['calls']} calls, ~{p['hours']} h" if p["hours"] is not None else " -> no estimate"))
    print(f"  TOTAL ~{res['projected_hours_total']} h of judge calls (plus per-session overhead: preflight, "
          "determinism re-asks, end-of-J recheck)")
    if res["missing_stages"]:
        print(f"  not measured in the pilot (no calls): {', '.join(res['missing_stages'])}")
    return 0 if res["verify"] == "PASS" else 1
