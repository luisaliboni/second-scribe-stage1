"""Call counts and the time estimate (Task 35; spec 6.5 step 8).

``count_calls(cfg, n_people)`` counts the model calls of a run from the config's shape only (specs, rungs and draws per
person, prior-only samples per role, judge calls per map at the ratio measured on the demo), plus each session's
preflight (2 reconstructor calls, 4 judge calls), the determinism re-asks (``audits.DETERMINISM_K`` per role) and,
when the pair has no model check yet, the model check's own calls. ``stage_calls`` is the per-stage breakdown;
``test_count_calls_matches_oracle_log`` pins it to the ``call_request`` records of ``configs/demo.json`` run with the
oracle client.

``model_check_parts`` counts the Setup model check (``configs/model-check.json``, six synthetic cases) by part: the
reconstructor part (Q) and the judge part (J), each with its logged calls, its determinism re-asks (sent straight to
the model, not logged) and, for J, the judge check; ``model_check_time`` gives its time range from the recorded
per-call times of the study's two models on the same synthetic corpus. ``test_model_check_counts_match_the_stand_in``
pins the counts to every request the tests' stand-in model server receives during a whole check.

``estimate(project, rungs)`` times ONE reconstruction call, on the shipped synthetic transcript closest in word count
to the median of the user's transcripts, through the user's reconstructor (a kit child process, ``probe`` below), so no
interview text reaches a model before the user presses Start. That time is scaled by the word ratio (for the stages
whose prompt holds the transcript), by each stage's time relative to a reconstruction call in the shipped recorded
demo runs (``TIMING_LOGS``: the live per-call times of a real reconstructor and judge on the synthetic corpus),
and by the call counts. The range is x0.7 to x1.6. ``tokens`` (prompt + reply characters / 4, per role) lets the page
show an optional cost line for a remote role at a price the user types (kept in the page only).

``python -m second_scribe.app.timing probe --pid PID --out DIR [--remote-endpoints FILE.json]`` is the child process:
it reads the reconstructor from ``SS_RECON_*`` (keys only through the env), passes the kit's guard, makes the one call
(logged, hash-chained, in ``DIR/logs/run.jsonl``) and prints ``{"seconds", "prompt_chars", "reply_chars", "status"}``.
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import math
import statistics
import sys
import time
from pathlib import Path

from .. import audits, ladder
from .. import model_check as kit_model_check
from ..pipeline import KIT_ROOT, STAGE_GROUPS, RunConfig, ladder_rungs

DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
RECORDED_LOG = KIT_ROOT / "demo" / "recorded" / "run.jsonl"
TIMING_LOGS = (RECORDED_LOG, KIT_ROOT / "demo" / "recorded" / "run-2026-10-18.jsonl")
DEMO_PEOPLE = 12
RANGE = (0.7, 1.6)
PREFLIGHT_CALLS = {"recon": 2, "judge": 4}
# Judge calls per map, from configs/demo.json run with the oracle client (575 call_request records: 21 modality
# batches over 120 AI maps, 2 canonicalisation batches over 84 main-grid and prior-only maps). Pinned by
# test_count_calls_matches_oracle_log.
MODALITY_PER_MAP = 21 / 120
CANON_PER_MAP = 2 / 84
ALIGN_PER_MAP = 3            # ai_endorsed, ai_script, ai_script_canon (with prior-only maps; 1 without)
ALIGN_PER_PERSON = 3         # endorsed_script, endorsed_script_canon, prior_endorsed (with prior-only maps)
# Stages whose prompt holds the transcript: their time and tokens scale with its length.
TRANSCRIPT_STAGES = frozenset({"q:mapping_talk", "recon", "recon:rungs", "judge:in_transcript", "judge:modality",
                               "judge:shuffled", "judge2:modality"})
STANDARD_PRIOR = {"variants": ["p0"], "samples": 5, "temperature": 0.7}
STANDARD_READING3 = {"variant": "p0", "samples": 1, "temperature": 0}       # presets/standard.json's reading3 block
STANDARD_SPEC = {"arm": "removed", "variant": "p0", "temperature": 0.0, "replicate": 0}
PICKABLE_RUNGS = ("rung1", "rung2", "rung3")
CHECK_CONFIG = kit_model_check.CHECK_CONFIG
# The model check's judge calls at the size of the synthetic maps (first attempts; pinned to the stand-in server by
# test_model_check_counts_match_the_stand_in): per case one in-transcript call, one modality call and one
# wrong-transcript call (each map has fewer than 20 elements to judge, one batch), and two alignments (the AI map with
# the endorsed map, the role's no-interview map with the endorsed map); the canonical labels fit in two batches of 50.
# A model that draws bigger maps, or answers that need a retry, add a few calls: the progress total then grows.
CHECK_JUDGE_PER_CASE = {"judge:in_transcript": 1, "judge:modality": 1, "judge:shuffled": 1, "judge:align": 2}
CHECK_CANON_BATCHES = 2
CHECK_JUDGE_CHECK_CALLS = 1        # the four planted items of ``model_check.judge_check`` in one batch of up to 20
# The last stage with model calls in each part: after its ``stage_end`` record the session runs its audits, whose
# determinism re-asks are not logged.
CHECK_LAST_STAGE = {"Q": "prior_only", "J": "align"}
CHECK_ROLES = {"Q": "recon", "J": "judge"}
PROBE_TIMEOUT_S = 600.0


# --- counts ------------------------------------------------------------------------------------------------------

def group(stage: str) -> str:
    """"J" for a judge stage key, "Q" otherwise."""
    return "J" if stage.startswith(("judge:", "judge2:")) else "Q"


def _n_roles(cfg: RunConfig, n_people: int, n_roles: int | None) -> int:
    if n_roles is None:
        roles = (cfg.corpus or {}).get("roles")
        n_roles = len(set(roles.values())) if isinstance(roles, dict) and roles else len(
            (cfg.context or {}).get("roles") or {}) or 1
    return max(0, min(int(n_roles), int(n_people))) if n_people else 0


def stage_calls(cfg: RunConfig, n_people: int, *, n_roles: int | None = None) -> dict[str, int]:
    """First-attempt model calls per stage key of one run of ``cfg`` over ``n_people`` people (no preflight, no
    determinism re-asks, no retries). Keys: ``q:mapping_talk``, ``recon`` (the specs), ``recon:rungs``, ``prior_only``,
    ``judge:in_transcript``, ``judge:modality``, ``judge:shuffled``, ``judge:script_prior_only``, ``judge:canon``,
    ``judge:align`` and, with a second judge, ``judge2:modality``."""
    n = int(n_people)
    roles = _n_roles(cfg, n, n_roles)
    specs = len(cfg.specs)
    rungs = ladder_rungs(cfg)
    draws = int((cfg.ladder or {}).get("draws", 1)) if rungs else 0
    # rung 2 draw 0 is the main run's primary map (taken over, no call); its other draws and every other rung are called
    rung_calls = sum(draws - 1 if k == "rung2" else draws for k in rungs)
    po = cfg.prior_only or {}
    prior = len(po.get("variants") or []) * int(po.get("samples", 0) or 0)
    has_prior = prior > 0
    # a reading3 block draws one more no-interview map per role unless its key is a script key; it is not canonicalized
    reading3 = len({s.key() for s in cfg.reading3_specs()} - {s.key() for s in cfg.prior_specs()})
    ai_maps = n * (specs + len(rungs) * draws)
    out = {
        "q:mapping_talk": n,
        "recon": n * specs,
        "recon:rungs": n * rung_calls,
        "prior_only": (prior + reading3) * roles,
        "judge:in_transcript": n,
        "judge:modality": round(ai_maps * MODALITY_PER_MAP),
        "judge:shuffled": n,
        "judge:script_prior_only": sum(len(s["variants"]) * int(s["samples"]) for s in cfg.script_sources) * roles,
        "judge:canon": math.ceil((n * specs + prior * roles) * CANON_PER_MAP) if n else 0,
        "judge:align": ai_maps * (ALIGN_PER_MAP if has_prior else 1) + (n * ALIGN_PER_PERSON if has_prior else 0),
    }
    if (cfg.models or {}).get("judge2"):
        out["judge2:modality"] = out["judge:modality"]
    return out


def _sessions_overhead(cfg: RunConfig) -> dict[str, int]:
    judges = 1 + bool((cfg.models or {}).get("judge2"))
    return {"Q": PREFLIGHT_CALLS["recon"] + audits.DETERMINISM_K,
            "J": judges * (PREFLIGHT_CALLS["judge"] + audits.DETERMINISM_K)}


@functools.lru_cache(maxsize=1)
def check_config() -> RunConfig:
    return RunConfig.load(CHECK_CONFIG)


def model_check_stages(judge: bool = True) -> dict[str, int]:
    """First-attempt logged calls per stage key of the Setup model check (``configs/model-check.json``). The
    reconstructor part is exact (``stage_calls``); the judge part is ``CHECK_JUDGE_PER_CASE`` per case plus
    ``CHECK_CANON_BATCHES`` (left out when ``judge`` is False: a pair with no judge checks the reconstructor alone)."""
    cfg = check_config()
    n = len((cfg.corpus or {}).get("roles") or {})
    out = {k: v for k, v in stage_calls(cfg, n).items() if group(k) == "Q"}
    if judge:
        out.update({k: v * n for k, v in CHECK_JUDGE_PER_CASE.items()})
        out["judge:canon"] = CHECK_CANON_BATCHES
    return out


def model_check_parts(judge: bool = True) -> dict[str, dict]:
    """``{"Q": {...}, "J": {...}}`` (no "J" when ``judge`` is False): per part of the model check, ``role``,
    ``calls`` (logged), ``determinism`` (re-asks, not logged: ``corpus.audit_determinism_k`` of the check config, at
    most the part's calls, all at T 0), ``judge_check`` (J only) and ``total`` (all three)."""
    cfg = check_config()
    k = int((cfg.corpus or {}).get("audit_determinism_k", audits.DETERMINISM_K))
    by = model_check_stages(judge)
    out = {}
    for g in ("Q", "J") if judge else ("Q",):
        calls = sum(v for s, v in by.items() if group(s) == g)
        det = min(max(k, 0), calls)
        jc = CHECK_JUDGE_CHECK_CALLS if g == "J" else 0
        out[g] = {"role": CHECK_ROLES[g], "calls": calls, "determinism": det, "judge_check": jc,
                  "total": calls + det + jc}
    return out


def model_check_calls(judge: bool = True) -> dict[str, int]:
    """Every call of the Setup model check per part (``model_check_parts`` totals); J is 0 when ``judge`` is False."""
    parts = model_check_parts(judge)
    return {g: (parts[g]["total"] if g in parts else 0) for g in ("Q", "J")}


def model_check_time(judge: bool = True) -> dict | None:
    """``{"low_s", "high_s", "per_part": {part: seconds}, "range"}``: the model check's time from the median live
    time per call of each stage in the recorded runs of the study's two models on this synthetic corpus
    (``recorded_profile``), times the calls of ``model_check_parts`` (the re-asks at the part's mean time per call,
    the judge check at a modality call's), over ``RANGE``. None when a stage has no recorded time."""
    prof = recorded_profile()
    by = model_check_stages(judge)
    per_part: dict[str, float] = {}
    for g, p in model_check_parts(judge).items():
        main = [(n, (prof.get(k) or {}).get("seconds")) for k, n in by.items() if group(k) == g and n]
        if not main or any(s is None for _, s in main):
            return None
        t = sum(n * s for n, s in main)
        per = t / p["calls"]
        t += p["determinism"] * per
        if p["judge_check"]:
            t += p["judge_check"] * ((prof.get("judge:modality") or {}).get("seconds") or per)
        per_part[g] = t
    total = sum(per_part.values())
    return {"low_s": round(total * RANGE[0]), "high_s": round(total * RANGE[1]),
            "per_part": {g: round(v, 1) for g, v in per_part.items()}, "range": list(RANGE)}


def count_calls(cfg: RunConfig, n_people: int, *, n_roles: int | None = None, overhead: bool = True,
                model_check: bool = False, judge: bool = True) -> dict[str, int]:
    """``{"Q", "J", "M"}`` model calls (M makes none). ``overhead`` adds each session's preflight and determinism
    re-asks; ``model_check`` adds every call of the model check (``model_check_calls``: its re-asks and its judge
    check included); ``judge=False`` (AI maps only: no judge) leaves J at 0."""
    by = stage_calls(cfg, n_people, n_roles=n_roles)
    out = {"Q": sum(v for k, v in by.items() if group(k) == "Q"),
           "J": sum(v for k, v in by.items() if group(k) == "J"), "M": 0}
    if overhead:
        for g, v in _sessions_overhead(cfg).items():
            out[g] += v
    if model_check:
        mc = model_check_calls(judge)
        out["Q"] += mc["Q"]
        out["J"] += mc["J"]
    if not judge:
        out["J"] = 0
    return out


@functools.lru_cache(maxsize=1)
def demo_config() -> RunConfig:
    return RunConfig.load(DEMO_CONFIG)


def check_rungs(rungs) -> list[str]:
    """The rungs the user picked (``rung1``, ``rung2``, ``rung3``); ``ValueError`` otherwise."""
    if not isinstance(rungs, list) or not all(isinstance(r, str) for r in rungs):
        raise ValueError("rungs must be a list of rung names")
    bad = [r for r in rungs if r not in PICKABLE_RUNGS]
    if bad:
        raise ValueError(f"unknown rung {bad[0]!r}")
    return [r for r in PICKABLE_RUNGS if r in rungs]


def standard_config(rungs: list[str]) -> RunConfig:
    """The shape of a Standard run (Task 38's preset): the primary spec only, the picked rungs plus rung 2 (and the
    placebo with rung 3), one draw, 5 prior-only samples of p0 per role plus the reading-3 map. Only its shape is
    read."""
    picked = set(check_rungs(rungs)) | {"rung2"}
    if "rung3" in picked:
        picked.add("placebo")
    lad = {**demo_config().ladder, "rungs": sorted(picked), "draws": 1}
    return dataclasses.replace(demo_config(), specs=[dict(STANDARD_SPEC)], ladder=lad, prior_only=dict(STANDARD_PRIOR),
                               reading3=dict(STANDARD_READING3))


# --- the recorded demo run: per-stage time and size relative to a reconstruction call ----------------------------

def stage_key(log_stage: str) -> str:
    """The ``stage_calls`` key of an audit-log stage (``recon:removed:rung1`` -> ``recon:rungs``)."""
    parts = str(log_stage).split(":")
    if parts[0] == "recon":
        return "recon:rungs" if len(parts) > 2 else "recon"
    if parts[0] in ("judge", "judge2") and len(parts) > 1:
        return f"{parts[0]}:{parts[1]}"
    return ":".join(parts[:2]) if parts[0] == "q" else parts[0]


def _profile_of(sigs: tuple) -> dict[str, dict]:
    secs: dict[str, list[float]] = {}
    chars: dict[str, list[int]] = {}
    try:
        for path, _mtime, _size in sigs:
            req: dict[str, tuple[str, int]] = {}
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if '"call_request"' not in line and '"call_response"' not in line:
                        continue
                    r = json.loads(line)
                    p = r.get("payload") or {}
                    if r.get("record_type") == "call_request":
                        n = sum(len(str(m.get("content", ""))) for m in p.get("messages") or [] if isinstance(m, dict))
                        req[p.get("request_id")] = (stage_key(p.get("stage", "")), n)
                    elif r.get("record_type") == "call_response" and p.get("request_id") in req:
                        k, n = req[p["request_id"]]
                        lat = p.get("latency_ms")
                        if isinstance(lat, (int, float)) and not isinstance(lat, bool) and lat > 0:
                            secs.setdefault(k, []).append(lat / 1000)
                        chars.setdefault(k, []).append(n + len(str(p.get("text") or "")))
    except (OSError, ValueError):
        return {}
    return {k: {"seconds": statistics.median(secs[k]) if secs.get(k) else None,
                "chars": statistics.median(chars[k]) if chars.get(k) else None} for k in sorted(chars)}


@functools.lru_cache(maxsize=8)
def _profile_cached(sigs: tuple) -> dict[str, dict]:
    return _profile_of(sigs)


def recorded_profile(paths=TIMING_LOGS) -> dict[str, dict]:
    """``{stage key: {"seconds": median live latency, "chars": median prompt + reply characters}}`` of the recorded
    run logs (counts and sizes only). ``paths`` is one path (str or Path) or a sequence of them; every log that exists
    is read, and ``{}`` is returned when none exists or one cannot be read.

    ``seconds`` counts only responses with ``latency_ms > 0``. A replayed response is logged with ``latency_ms`` 0: the
    client did not wait for a model, so counting it would pull every stage towards 0 s and the time estimate with it.
    A stage with no live response has ``seconds`` None. The 18 Oct 2026 recording (``run-2026-10-18.jsonl``) ran every
    call live on the same models and the same machine as the current one, whose log replays most of its calls from
    that recording, so the live latencies of all the logs are pooled per stage key. ``chars`` is the median prompt +
    reply characters over every response of every log, replayed or not (a replayed response has the same size)."""
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    sigs = []
    for p in paths:
        try:
            st = Path(p).stat()
        except OSError:
            continue
        sigs.append((str(p), st.st_mtime_ns, st.st_size))
    return _profile_cached(tuple(sigs)) if sigs else {}


# --- words ---------------------------------------------------------------------------------------------------------

def transcript_words(path: Path) -> int:
    from ..transcripts import read_transcript_paras
    return sum(len(p.split()) for p in read_transcript_paras(path))


@functools.lru_cache(maxsize=1)
def demo_words() -> dict[str, int]:
    """Words of each shipped synthetic transcript, by pid."""
    from ..pipeline import load_participants
    return {p.pid: transcript_words(p.transcript_path) for p in load_participants(demo_config())}


def user_words(project) -> list[int]:
    """Words of each imported transcript (counts only; nothing leaves this process)."""
    from . import coding
    out = []
    for pid in coding.known_pids(project):
        src = coding.transcript_source(project, pid)
        if src is not None:
            out.append(transcript_words(project.path(src["rel"])))
    return out


def user_roles(project) -> int:
    """How many participant roles the project has (prior-only maps are drawn once per role): the roles Setup
    recorded (``context.roles``), else the distinct ``role`` of the imported transcripts, else 1."""
    roles = (project.data.get("context") or {}).get("roles")
    if isinstance(roles, (dict, list)) and roles:
        return len(roles)
    seen = {m.get("role") for m in project.data.get("files", {}).values()
            if isinstance(m, dict) and m.get("kind") == "transcript" and isinstance(m.get("role"), str)}
    return len(seen) or 1


def pick_demo(median_words: float) -> str:
    """The shipped pid whose transcript is closest in word count to ``median_words`` (ties: the first pid)."""
    words = demo_words()
    return min(sorted(words), key=lambda pid: abs(words[pid] - median_words))


# --- the timed call ------------------------------------------------------------------------------------------------

def probe(project, secrets, pid: str) -> dict:
    """Run the ``probe`` child for ``pid`` on the project's reconstructor; its printed result, or ``RuntimeError``
    with the exit code's name."""
    from . import jobs, models
    out = project.path(f"{jobs.check_rel(project)}/estimate")
    out.mkdir(parents=True, exist_ok=True)
    argv = ["-m", "second_scribe.app.timing", "probe", "--pid", pid, "--out", str(out)]
    eps = [e for e in models.remote_endpoints(project) if e["role"] == "recon"]
    if eps:
        f = out / "remote_endpoints.json"
        f.write_text(json.dumps(eps, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        argv += ["--remote-endpoints", str(f)]
    f = out / "endpoint_optins.json"                                       # audit K7: always passed, [] when none
    f.write_text(json.dumps([o for o in models.endpoint_optins(project) if o["role"] == "recon"], sort_keys=True,
                            indent=2) + "\n", encoding="utf-8")
    argv += ["--endpoint-optins", str(f)]
    env = jobs.child_env(models.role_env(project, secrets, "recon"))
    job = jobs.Job(argv, env, KIT_ROOT, out / "logs" / "run.jsonl", job_dir=out / "job").start()
    try:
        rc = job.wait(PROBE_TIMEOUT_S)
    except Exception:                    # noqa: BLE001 - a hung server: stop the child, report a timeout
        job.terminate()
        raise RuntimeError("timeout") from None
    if rc != 0:
        raise RuntimeError(jobs.EXIT_CODES.get(rc, "crashed"))
    lines = (job.job_dir / "stdout.txt").read_text(encoding="utf-8").strip().splitlines()
    doc = json.loads(lines[-1])
    if doc.get("status") != "ok" or not isinstance(doc.get("seconds"), (int, float)):
        raise RuntimeError("probe_failed")
    return doc


def estimate(project, rungs, *, secrets=None, prober=None) -> dict:
    """``{"low_s", "high_s", "per_stage": {"Q", "J"} (seconds), "calls": {"Q", "J", "M"}, "tokens": {role: n},
    "words": {"median", "demo"}, "demo_pid", "probe_s", "remote_roles", "range", "includes_check"}`` for a Standard
    run with ``rungs`` (``includes_check``: the pair's model check has not passed, so its calls are counted).
    ``prober(pid) -> {"seconds", ...}`` makes the one timed call (default: ``probe`` above). ``ValueError`` when no
    transcript is imported or a rung is unknown."""
    from . import jobs, models
    rungs = check_rungs(rungs)
    words = user_words(project)
    if not words:
        raise ValueError("no_transcripts")
    median = statistics.median(words)
    pid = pick_demo(median)
    demo_w = demo_words()[pid]
    ratio = median / demo_w if demo_w else 1.0
    judge = models.pipeline_mode(project) == "two_family"
    measured = (prober or (lambda p: probe(project, secrets, p)))(pid)
    t_unit = float(measured["seconds"])

    prof = recorded_profile()
    base = (prof.get("recon") or {}).get("seconds")

    def rel(k: str) -> float:
        s = (prof.get(k) or {}).get("seconds")
        return s / base if s and base else 1.0

    def chars(k: str) -> float:
        c = (prof.get(k) or {}).get("chars")
        return c if c else float(measured.get("prompt_chars", 0) + measured.get("reply_chars", 0))

    cfg = standard_config(rungs)
    n_people = len(words)
    n_roles = user_roles(project)
    by = stage_calls(cfg, n_people, n_roles=n_roles)
    secs = {"Q": 0.0, "J": 0.0}
    toks = {"Q": 0.0, "J": 0.0}
    for k, n in by.items():
        g = group(k)
        if g == "J" and not judge:
            continue
        w = ratio if k in TRANSCRIPT_STAGES else 1.0
        secs[g] += n * t_unit * rel(k) * w
        toks[g] += n * chars(k) * w / 4
    unchecked = jobs.model_check_state(project) != "passed"
    calls = count_calls(cfg, n_people, n_roles=n_roles, model_check=unchecked, judge=judge)
    for g in ("Q", "J"):
        main = sum(n for k, n in by.items() if group(k) == g)
        if g == "J" and not judge:
            continue
        per = secs[g] / main if main else t_unit
        extra = calls[g] - main
        secs[g] += extra * per
        toks[g] += extra * (toks[g] / main if main else 0)
    total = secs["Q"] + secs["J"]
    remote = [r for r in models.ROLES if (models.role(project, r) or {}).get("address_class") == "remote"
              and (r == "recon" or judge)]
    return {"low_s": round(total * RANGE[0], 1), "high_s": round(total * RANGE[1], 1),
            "per_stage": {g: round(v, 1) for g, v in secs.items()}, "calls": calls,
            "tokens": {"recon": int(toks["Q"]), **({"judge": int(toks["J"])} if judge else {})},
            "words": {"median": int(median), "demo": demo_w}, "demo_pid": pid, "probe_s": round(t_unit, 3),
            "remote_roles": remote, "range": list(RANGE), "includes_check": unchecked}


# --- the probe child -------------------------------------------------------------------------------------------------

def _probe_main(a) -> int:
    from ..audit.registry import AuditLog
    from ..cli import build_clients
    from ..llm import guard_real_clients
    from ..model_check import load_endpoint_optins, load_remote_endpoints
    from ..pipeline import PROMPTS_DIR, _Run, load_participants
    from ..reconstruct import ReconSpec, reconstruct
    out = Path(a.out).resolve()
    cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
    people = {p.pid: p for p in load_participants(cfg)}
    if a.pid not in people:
        print("refused: unknown synthetic pid", file=sys.stderr)
        return 2
    try:
        eps = load_remote_endpoints(a.remote_endpoints) if a.remote_endpoints else None
        ops = load_endpoint_optins(a.endpoint_optins) if a.endpoint_optins else None
        clients = build_clients("http", cfg, roles=("recon",))
        guard_real_clients(clients, judge_rule="two_family", remote_endpoints=eps, endpoint_optins=ops)
    except Exception as exc:  # noqa: BLE001 - a refusal names its reason on stderr only
        print(f"refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    (out / "logs").mkdir(parents=True, exist_ok=True)
    log = AuditLog(out / "logs" / "run.jsonl", "estimate")
    try:
        r = _Run(cfg, clients, log)
        p = people[a.pid]
        t0 = time.monotonic()
        _, status = reconstruct(log, clients["recon"], r.rule_flagged(p), ReconSpec("removed", "p0", 0.0, 0),
                                r.ctx(p.role), PROMPTS_DIR, item_id=f"{a.pid}__estimate", stage="estimate:recon")
        wall = time.monotonic() - t0
    finally:
        log.close()
    req, resp = None, None
    for line in (out / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("record_type") == "call_request":
            req = rec["payload"]
        elif rec.get("record_type") == "call_response" and req and rec["payload"].get("request_id") == req[
                "request_id"]:
            resp = rec["payload"]
            break                                   # the first call's answer: retries are not the call's time
    lat = (resp or {}).get("latency_ms")
    print(json.dumps({"status": "ok" if resp is not None else status,
                      "seconds": round(lat / 1000 if isinstance(lat, (int, float)) else wall, 3),
                      "prompt_chars": sum(len(m["content"]) for m in (req or {}).get("messages", [])),
                      "reply_chars": len(str((resp or {}).get("text") or ""))}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="second_scribe.app.timing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe", help="one timed reconstruction call on a shipped synthetic transcript")
    pr.add_argument("--pid", required=True)
    pr.add_argument("--out", required=True)
    pr.add_argument("--remote-endpoints", metavar="FILE.json")
    pr.add_argument("--endpoint-optins", metavar="FILE.json")
    a = ap.parse_args(argv)
    return _probe_main(a)


if __name__ == "__main__":
    sys.exit(main())
