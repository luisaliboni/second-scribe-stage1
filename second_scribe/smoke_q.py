"""``smoke-q``: the Qwen-only live smoke the author runs before the real run (group Q on the SYNTHETIC corpus).

What it does: the Q-phase work only (mapping-talk screen, reconstruction for the main grid and every ladder rung,
prior-only maps) on the synthetic demo corpus, plus one synthetic transcript padded to real length (about 12k
words) in a second run folder. Both runs go through the same egress guard, thinking-off check and hash-chained log
as a real run. The judge is never contacted, a real corpus is refused, and ``--plan`` lists every call and the
endpoint without opening a connection.

This module holds the config loader, the padded corpus, the refusal checks and the call plan (this file); the
budget follows in ``budget`` below. ``cli.cmd_smoke_q`` strings them together.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import shutil
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import ladder
from .llm import (HTTPClient, ModelConfig, RealGuardRefused, endpoint_host, endpoint_problem, guard_real_clients,
                  safe_endpoint, thinking_off)
from .pipeline import RunConfig, _per_pid, ladder_rungs, load_participants
from .synth.docx_writer import write_docx
from .transcripts import _BATCH, parse_paragraphs, read_transcript_paras

SMOKE_KEY = "smoke_q"
RECON_ROLE = "recon"
Q_STAGES = ("mapping_talk_confirm", "recon", "prior_only")      # pipeline.STAGE_GROUPS["Q"]
PADDED_PREFIX = "SYN-PAD"


class SmokeRefused(RealGuardRefused):
    """The smoke would go somewhere or do something it must not; raised before any request (the CLI prints it as
    ``refused:`` and exits 2)."""


def load_config(path, **overrides) -> tuple[RunConfig, dict]:
    """(RunConfig, smoke block). The ``smoke_q`` block holds this command's own settings (padding, the real run's
    size, the options, the judge estimates); ``RunConfig`` would refuse the unknown key, so it is set aside first."""
    p = Path(path)
    d = json.loads(p.read_text(encoding="utf-8"))
    smoke = d.pop(SMOKE_KEY, None)
    if not isinstance(smoke, dict):
        raise ValueError(f"{p.name}: no \"{SMOKE_KEY}\" block; copy configs/smoke-q.json")
    d.setdefault("base_dir", str(p.resolve().parent))
    d.update(overrides)
    if not Path(d["out_dir"]).is_absolute():
        d["out_dir"] = str((Path(d["base_dir"]) / d["out_dir"]).resolve())
    return RunConfig(**d), smoke


# ------------------------------------------------------------------ refusals

def check_config(cfg: RunConfig) -> None:
    """``SmokeRefused`` unless the config is one this command may run: a synthetic corpus, the reconstructor as the
    only model role (a judge named in the config is refused, so a pasted real config cannot reach a judge)."""
    if cfg.corpus.get("kind") != "synthetic":
        raise SmokeRefused("smoke-q runs on the synthetic corpus only (corpus.kind must be \"synthetic\")")
    if not cfg.models.get(RECON_ROLE):
        raise SmokeRefused("smoke-q needs models.recon (the env prefix of the Qwen server, e.g. \"RECON\")")
    named = [r for r in ("judge", "judge2") if cfg.models.get(r)]
    if named:
        raise SmokeRefused(f"smoke-q never contacts a judge: set models.{named[0]} to null in the config")


def model_config(cfg: RunConfig) -> ModelConfig:
    """The Qwen server's ``ModelConfig`` from ``SS_<PREFIX>_*``; ``SmokeRefused`` names the missing variable."""
    prefix = cfg.models[RECON_ROLE]
    p = f"SS_{prefix.upper()}_"
    for suffix in ("URL", "MODEL", "PARAMS"):
        if not os.environ.get(p + suffix):
            raise SmokeRefused(f"set {p}{suffix} (see smoke_q.env_usage in the config)")
    try:
        return ModelConfig.from_env(prefix)
    except ValueError as exc:                    # PARAMS is not JSON, or sets a reserved field
        raise SmokeRefused(f"{p}PARAMS is not usable: {exc}") from None


def guard_clients(clients: dict) -> None:
    """The real-run guards, applied to a synthetic corpus on purpose: the endpoint must be loopback, private or
    Tailscale (``guard_real_clients``), thinking must be configured off, and the model must be a Qwen (this smoke
    exists to time Qwen; any other id means the wrong server)."""
    guard_real_clients(clients)
    cfg = getattr(clients.get(RECON_ROLE), "cfg", None)
    name = (getattr(cfg, "model_id", None) or "").lower()
    if "qwen" not in name:
        raise SmokeRefused("the model id does not name a Qwen model; smoke-q is the Qwen-only smoke "
                           "(check SS_RECON_MODEL against GET /v1/models)")


def build_clients(cfg: RunConfig) -> dict:
    """{"recon": HTTPClient}, guarded; no judge client exists."""
    clients = {RECON_ROLE: HTTPClient(model_config(cfg))}
    guard_clients(clients)
    return clients


def env_report(cfg: RunConfig) -> dict:
    """What the guards say about the environment, with no request and no DNS lookup: the endpoint as it would be
    contacted, the model id, whether thinking is configured off, and why the run would be refused (or None)."""
    p = f"SS_{cfg.models.get(RECON_ROLE, 'RECON').upper()}_"
    url, model = os.environ.get(p + "URL"), os.environ.get(p + "MODEL")
    problem = None
    params = None
    try:
        params = json.loads(os.environ.get(p + "PARAMS") or "null")
    except ValueError:
        problem = f"{p}PARAMS is not JSON"

    def no_lookup(host, port):                    # --plan opens no connection and asks no resolver
        raise OSError("not resolved in --plan")
    if problem is None and not url:
        problem = f"{p}URL is not set"
    if problem is None:
        why = endpoint_problem(url, no_lookup)
        if why and "cannot be resolved" in why:
            why = None                            # a name: checked against the resolver at run time
        problem = why
    if problem is None and not model:
        problem = f"{p}MODEL is not set"
    if problem is None and "qwen" not in model.lower():
        problem = "the model id does not name a Qwen model"
    if problem is None and not thinking_off(params):
        problem = f"{p}PARAMS does not set chat_template_kwargs.enable_thinking to false"
    return {"endpoint": safe_endpoint(url) if url else None, "endpoint_host": endpoint_host(url),
            "model_id": model, "thinking_off": thinking_off(params), "problem": problem}


# ------------------------------------------------------------------ the padded transcript

def _words(paragraphs: list[str]) -> int:
    return sum(len(x.split()) for x in paragraphs)


def padded_paragraphs(cfg: RunConfig, target_words: int, start: int = 0) -> list[str]:
    """Paragraphs of ONE synthetic transcript of at least ``target_words`` words (cut at a paragraph boundary): the
    synthetic participants' transcripts that share the most common speaker format, run through again from the
    ``start``-th participant on until the length is reached. Repeated content is fine: this transcript times the
    model, nothing is scored on it."""
    c = cfg.corpus
    by_format: dict[str, list[list[str]]] = {}
    for p in load_participants(cfg):
        paras = read_transcript_paras(p.transcript_path)
        t = parse_paragraphs(paras, _per_pid(c.get("participant_labels"), p.pid), p.pid, interviewer=p.interviewer,
                             interviewer_labels=_per_pid(c.get("interviewer_labels"), p.pid) or None)
        by_format.setdefault(t.speaker_format, []).append(paras)
    by_format.pop("none", None)
    if not by_format:
        raise SmokeRefused("the synthetic corpus has no transcript with speaker labels to pad")
    pool = max(by_format.values(), key=len)
    pool = pool[start % len(pool):] + pool[:start % len(pool)]
    flat = [x for paras in pool for x in paras]
    if _words(flat) == 0:
        raise SmokeRefused("the synthetic transcripts are empty")
    out: list[str] = []
    n = 0
    while n < target_words:
        for x in flat:
            out.append(x)
            n += len(x.split())
            if n >= target_words:
                break
    return out


def padded_pids(smoke: dict) -> list[str]:
    return [f"{PADDED_PREFIX}{k + 1:02d}" for k in range(int(smoke["padded"].get("n_transcripts", 1)))]


def prepare_padded(cfg: RunConfig, smoke: dict, root, run_id_suffix: str = "padded") -> RunConfig:
    """Write the padded synthetic corpus under ``root/padded-corpus`` (a ``meta.json`` with a domain marks it
    synthetic; the map is a copy of a synthetic one, which the Q phase never reads) and return the config of the
    second run (``root/padded``): the same specs, context and ladder rungs, one participant per padded transcript,
    no prior-only draws (they send no transcript, so the first run times them) and ``smoke_q.padded.draws``
    ladder draws. The files are byte-deterministic, so a rerun finds them unchanged."""
    root = Path(root)
    corpus = root / "padded-corpus"
    (corpus / "maps").mkdir(parents=True, exist_ok=True)
    (corpus / "transcripts").mkdir(parents=True, exist_ok=True)
    people = load_participants(cfg)
    (corpus / "meta.json").write_text(json.dumps(
        {"domain": cfg.context["domain"], "note": "synthetic transcript padded to real length for timing the Q phase"},
        indent=1) + "\n", encoding="utf-8")
    target = int(smoke["padded"].get("target_words", 12000))
    roles, interviewers = {}, {}
    for k, pid in enumerate(padded_pids(smoke)):
        write_docx(corpus / "transcripts" / f"{pid}.docx", padded_paragraphs(cfg, target, start=k))
        shutil.copyfile(people[0].map_path, corpus / "maps" / f"{pid}.vsdx")
        roles[pid], interviewers[pid] = people[0].role, people[0].interviewer
    d = dataclasses.asdict(cfg)
    d.update(run_id=f"{cfg.run_id}-{run_id_suffix}", out_dir=str(root / "padded"), base_dir=str(root))
    d["corpus"] = {**cfg.corpus, "maps_glob": str(corpus / "maps" / "*.vsdx"),
                   "transcripts_glob": str(corpus / "transcripts" / "*.docx"), "pairs": None,
                   "roles": roles, "interviewers": interviewers}
    d["corpus"].pop("synthetic_dir", None)
    d["prior_only"] = {**cfg.prior_only, "samples": 0}
    d["reading3"] = None                  # nor the reading-3 map: the padded run draws no prior-only map
    # a config without a ladder (primary setting only) keeps none: {"draws": n} alone is a ladder of no rungs
    d["ladder"] = {**cfg.ladder, "draws": int(smoke["padded"].get("draws", 1))} if cfg.ladder else {}
    return RunConfig(**d)


# ------------------------------------------------------------------ the call plan

@dataclass(frozen=True)
class PlannedCall:
    run: str          # "synthetic" | "padded"
    stage: str        # the audit-log stage of the call
    item_id: str      # the audit-log item id
    long_input: bool  # a whole transcript goes in (reconstruction); False for a batch of turns or a prior-only prompt


def plan_calls(cfg: RunConfig, run: str) -> list[PlannedCall]:
    """Every ``log_call`` of the Q stages for ``cfg``, in the order the pipeline makes them, one entry per call (a
    retry after an invalid reply is another request of the same call). It mirrors ``pipeline._stage_mapping_talk_
    confirm``, ``_stage_recon`` (main grid, then ``_ladder_recon``) and ``_stage_prior_only``; a test holds the
    plan equal to the calls of a real run of the code."""
    c = cfg.corpus
    people = load_participants(cfg)
    trs = {}
    for p in people:
        trs[p.pid] = parse_paragraphs(read_transcript_paras(p.transcript_path),
                                      _per_pid(c.get("participant_labels"), p.pid), p.pid, interviewer=p.interviewer,
                                      interviewer_labels=_per_pid(c.get("interviewer_labels"), p.pid) or None)
    out: list[PlannedCall] = []
    for p in people:                                                    # mapping_talk_confirm
        t = trs[p.pid]
        if t.speaker_format == "none":
            continue
        n_iv = sum(1 for x in t.turns if x.speaker == "interviewer")
        out += [PlannedCall(run, "q:mapping_talk", f"{p.pid}:{b}", False) for b in range(math.ceil(n_iv / _BATCH))]
    specs = cfg.recon_specs()
    for spec in specs:                                                  # recon: the main grid
        for p in people:
            if spec.arm == "removed" and trs[p.pid].speaker_format == "none":
                continue
            out.append(PlannedCall(run, f"recon:{spec.arm}", f"{p.pid}__{spec.key()}", True))
    keys = ladder_rungs(cfg)                                            # recon: the ladder
    if keys:
        spec, draws = specs[0], int(cfg.ladder["draws"])

        def arm(key: str, ds):
            for d in ds:
                dspec = dataclasses.replace(spec, replicate=d)
                for p in people:
                    if dspec.arm == "removed" and trs[p.pid].speaker_format == "none":
                        continue
                    rung = ladder.get_rung(key)
                    out.append(PlannedCall(run, ladder.rung_stage(rung, dspec).log,
                                           ladder.rung_item_id(f"{p.pid}__{dspec.key()}", rung), True))
        if "rung2" in keys:
            arm("rung2", range(1, draws))                  # draw 0 is the main run's own primary map (preseed_rung2)
        pair = [k for k in ("rung3", "placebo") if k in keys]
        for d in range(draws if pair else 0):
            for k in pair:
                arm(k, [d])
        if "rung1" in keys:
            arm("rung1", range(draws))
    roles = sorted({p.role for p in people})
    script_keys = {s.key() for s in cfg.prior_specs()}                  # prior_only: the script set, then reading 3
    for pspec in cfg.prior_specs() + [s for s in cfg.reading3_specs() if s.key() not in script_keys]:
        for role in roles:
            out.append(PlannedCall(run, "prior_only", f"{role}__{pspec.key()}", False))
    return out


def summarize(calls: list[PlannedCall]) -> list[tuple[str, str, int]]:
    """[(run, stage, n calls)] in first-seen order."""
    n = Counter((x.run, x.stage) for x in calls)
    seen: list[tuple[str, str]] = []
    for x in calls:
        if (x.run, x.stage) not in seen:
            seen.append((x.run, x.stage))
    return [(r, s, n[(r, s)]) for r, s in seen]


def determinism_k(cfg: RunConfig) -> int:
    """How many logged T=0 requests the determinism audit re-sends at the end of the Q group (``audits._setting``)."""
    return int((cfg.corpus or {}).get("audit_determinism_k", audits_default_k()))


def audits_default_k() -> int:
    from .audits import DETERMINISM_K
    return DETERMINISM_K


def plan_text(cfg: RunConfig, smoke: dict, padded: RunConfig) -> tuple[str, int]:
    """(the plan as text, exit code). Exit 0 when the guards would let the run start, 2 when they would refuse it (the
    plan is printed either way). Reads the environment and the local synthetic files only: no connection, no lookup."""
    env = env_report(cfg)
    lines = ["smoke-q plan: no network call is made by --plan",
             f"endpoint it would contact: {env['endpoint'] or '(SS_RECON_URL is not set)'}",
             f"model id it would send: {env['model_id'] or '(SS_RECON_MODEL is not set)'}",
             f"thinking configured off: {'yes' if env['thinking_off'] else 'NO'}",
             "judge: never contacted (the config names none)",
             "guard verdict: " + ("the run would start" if env["problem"] is None
                                  else f"the run WOULD BE REFUSED: {env['problem']}")]
    calls = plan_calls(cfg, "synthetic") + plan_calls(padded, "padded")
    n_pad = len(padded_pids(smoke))
    lines += ["", f"runs: synthetic corpus ({len(load_participants(cfg))} participants) and {n_pad} synthetic transcript(s) "
                  f"padded to about {int(smoke['padded'].get('target_words', 12000))} words ("
                  + (f"draws {padded.ladder['draws']})" if padded.ladder else "no ladder)"),
              f"calls: {len(calls)} logged (one entry per call; a reply that fails validation is retried, at most 3 "
              f"requests per call) and up to {determinism_k(cfg)} unlogged re-sends per run by the determinism audit",
              "", "calls by stage:"]
    lines += [f"  {r:<10} {s:<26} {n}" for r, s, n in summarize(calls)]
    lines += ["", "exact list, in run order (run, stage, item id; * = a whole transcript goes in):"]
    lines += [f"  {x.run:<9} {x.stage:<24} {x.item_id}{' *' if x.long_input else ''}" for x in calls]
    return "\n".join(lines) + "\n", 0 if env["problem"] is None else 2


# ------------------------------------------------------------------ the budget

BUDGET_SCHEMA = "smoke-q-budget/1"
JUDGE_LABEL = "estimate, unmeasured until the judge window"


def read_attempts(log_path) -> list[dict]:
    """One dict per request that got a reply, from a run log: stage, item id, attempt, temperature, latency (ms), prompt
    and completion tokens (None when the server sent no usage). Requests that got no reply (call_error) have no
    latency and are left out."""
    reqs: dict[str, dict] = {}
    out: list[dict] = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        pl = rec.get("payload") or {}
        if rec.get("record_type") == "call_request":
            reqs[pl["request_id"]] = pl
        elif rec.get("record_type") == "call_response" and pl.get("request_id") in reqs:
            rq, usage = reqs[pl["request_id"]], pl.get("usage") or {}
            out.append({"stage": rq["stage"], "item_id": rq["item_id"], "attempt": rq["attempt"],
                        "temperature": rq.get("temperature"), "model_id": rq.get("model_id"),
                        "endpoint_host": rq.get("endpoint_host"), "latency_ms": pl.get("latency_ms"),
                        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                        "ts": rec.get("ts")})
    return out


def classify(stage: str, item_id: str) -> tuple[str, str]:
    """(group, arm) of a call. Groups: mapping_talk, main_grid (arm = the spec key), ladder (arm = the rung key) and
    prior_only (arm = the prompt variant). A real run's number of calls is counted per (group, arm)."""
    if stage == "q:mapping_talk":
        return "mapping_talk", "batch"
    if stage == "prior_only":
        return "prior_only", item_id.split("__")[-3]
    if stage.startswith("recon:"):
        parts = stage.split(":")
        if len(parts) == 3:
            return "ladder", parts[2]
        return "main_grid", "__".join(item_id.split("@")[0].split("__")[-4:])
    return "other", stage


def _q(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..1); None for no values."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def measured_rows(run: str, attempts: list[dict]) -> list[dict]:
    """Per (stage, arm) of one run: n calls, n attempts, latency median / p90 / mean (ms), tokens per second (median
    over replies that report completion tokens), median prompt and completion tokens. ``p90_meaningful`` is false
    below 10 replies: the p90 of a handful is just their maximum."""
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for a in attempts:
        g, arm = classify(a["stage"], a["item_id"])
        groups.setdefault((g, a["stage"], arm), []).append(a)
    rows = []
    for (g, stage, arm), xs in groups.items():
        lat = [x["latency_ms"] for x in xs if isinstance(x["latency_ms"], (int, float))]
        tps = [x["completion_tokens"] / (x["latency_ms"] / 1000) for x in xs
               if x["completion_tokens"] and x["latency_ms"]]
        calls = {(x["stage"], x["item_id"]) for x in xs}
        pt = [x["prompt_tokens"] for x in xs if isinstance(x["prompt_tokens"], (int, float))]
        ct = [x["completion_tokens"] for x in xs if isinstance(x["completion_tokens"], (int, float))]
        rows.append({
            "run": run, "group": g, "stage": stage, "arm": arm, "temperature": xs[0]["temperature"],
            "n_calls": len(calls), "n_attempts": len(xs), "attempts_per_call": round(len(xs) / len(calls), 3),
            "latency_ms": {"median": _median(lat), "p90": _q(lat, 0.9), "mean": round(sum(lat) / len(lat), 1) if lat else None},
            "p90_meaningful": len(lat) >= 10,
            "tokens_per_s_median": round(_median(tps), 2) if tps else None,
            "prompt_tokens_median": _median(pt), "completion_tokens_median": _median(ct)})
    return rows


def _pick(rows: list[dict], run: str, group: str, arm: str | None = None) -> dict | None:
    xs = [r for r in rows if r["run"] == run and r["group"] == group and (arm is None or r["arm"] == arm)
          and r["latency_ms"]["median"] is not None]
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    med = sorted(xs, key=lambda r: r["latency_ms"]["median"])[len(xs) // 2]       # a group pooled over its arms
    return {**med, "arm": f"{group} (pooled over {len(xs)} arms)"}


def _component(name: str, calls: int, row: dict | None, basis: str, assumed_completion: float | None = None,
               temperature: float | None = None) -> dict:
    """One line of the projection: ``calls`` real calls at the measured per-call time of ``row`` (retries included
    through attempts_per_call)."""
    c = {"component": name, "calls_real": calls, "basis": basis, "temperature": temperature}
    if row is None:
        return {**c, "basis": "unmeasured", "per_call_s_median": None, "per_call_s_p90": None,
                "hours_median": None, "hours_p90": None, "hours_upper_bound": None}
    k = row["attempts_per_call"] / 1000
    med, p90 = row["latency_ms"]["median"] * k, (row["latency_ms"]["p90"] or row["latency_ms"]["median"]) * k
    c.update(per_call_s_median=round(med, 2), per_call_s_p90=round(p90, 2), n_measured=row["n_calls"],
             hours_median=round(calls * med / 3600, 4), hours_p90=round(calls * p90 / 3600, 4))
    ct = row.get("completion_tokens_median")
    scale = max(1.0, assumed_completion / ct) if assumed_completion and ct else 1.0
    c["hours_upper_bound"] = round(c["hours_p90"] * scale, 4)
    c["output_scale"] = round(scale, 2)
    return c


def _sum(comps: list[dict], key: str) -> float | None:
    vals = [c[key] for c in comps if c[key] is not None]
    return round(sum(vals), 4) if len(vals) == len(comps) else None


def qwen_projection(rows: list[dict], cfg: RunConfig, smoke: dict, opt: dict) -> dict:
    """Wall-clock of the REAL Qwen phase for one option: the 46-map main grid (every spec), the mapping-talk screen, the
    prior-only maps and the option's ladder arms, each as real call count x measured per-call time (padded transcript
    for the calls that carry a transcript; the synthetic pass for the prior-only maps, which carry none), plus the
    determinism audit's re-sends. Three totals: median, p90 and an upper bound that stretches the long-input calls to
    the planned real output length when the padded transcript made shorter maps."""
    real = smoke["real"]
    n, ac = int(real["n_maps"]), float(real.get("recon_completion_tokens") or 0) or None
    comps: list[dict] = []
    row = _pick(rows, "padded", "mapping_talk")
    batches = math.ceil(int(real["interviewer_turns_per_transcript"]) / _BATCH)
    comps.append(_component("mapping_talk", n * batches, row or _pick(rows, "synthetic", "mapping_talk"),
                            f"padded transcript, {batches} batches of {_BATCH} turns per transcript (assumed turns)"
                            if row else "synthetic pass (padded not measured)", temperature=0.0))
    for spec in cfg.recon_specs():
        exact = _pick(rows, "padded", "main_grid", spec.key())
        row = exact or _pick(rows, "padded", "main_grid")
        comps.append(_component(f"main_grid {spec.key()}", n, row,
                                "padded transcript" if exact else "padded main-grid median (this spec not measured)",
                                ac, spec.temperature))
    pv = cfg.prior_only
    n_prior = int(real["n_roles"]) * len(pv["variants"]) * int(real["prior_only_samples"])
    comps.append(_component("prior_only", n_prior, _pick(rows, "synthetic", "prior_only"),
                            "synthetic pass (no transcript goes in)", temperature=float(pv.get("temperature", 0.7))))
    if cfg.reading3 is not None and cfg.reading3_specs()[0].key() not in {s.key() for s in cfg.prior_specs()}:
        comps.append(_component("prior_only reading3", int(real["n_roles"]), _pick(rows, "synthetic", "prior_only"),
                                "synthetic pass (no transcript goes in), one map per role",
                                temperature=float(cfg.reading3["temperature"])))
    primary = cfg.recon_specs()[0]
    draws, temp = int(opt["draws"]), float(opt.get("temperature", 0.0))
    for rung in opt["rungs"]:
        if rung == "rung2" and temp == 0.0:
            continue                    # draw 0 at the primary setting IS the main-grid map (preseed_rung2): no new call
        arm_row = _pick(rows, "padded", "ladder", rung)
        if rung == "rung2":             # T>0 draws of the good-practice prompt = the T=0.7 main-grid call
            t07 = _pick(rows, "padded", "main_grid", f"{primary.arm}__{primary.variant}__t{temp:g}__r0")
            comps.append(_component(f"ladder rung2 x{draws} at T={temp:g}", n * draws, t07,
                                    "padded transcript, the T=0.7 main-grid call", ac, temp))
        else:
            comps.append(_component(f"ladder {rung} x{draws}" + (f" at T={temp:g}" if temp else ""), n * draws, arm_row,
                                    "padded transcript" + ("; assumes T=0.7 costs what T=0 does" if temp else ""),
                                    ac, temp))
    t0 = [c for c in comps if c["temperature"] == 0 and c["hours_median"] is not None and c["calls_real"]]
    mean_s = (sum(c["per_call_s_median"] * c["calls_real"] for c in t0) / sum(c["calls_real"] for c in t0)) if t0 else None
    k = determinism_k(cfg)
    det = round(k * mean_s / 3600, 4) if mean_s is not None else None
    out = {"components": comps, "determinism_audit_h": det,
           "determinism_audit_note": f"up to {k} logged T=0 requests are re-sent once, unlogged, at the end of the Q group; "
                                     "costed at the call-weighted mean of the T=0 calls"}
    for key, name in (("hours_median", "hours_median"), ("hours_p90", "hours_p90"), ("hours_upper_bound", "hours_upper_bound")):
        s = _sum(comps, key)
        out[name] = None if s is None or det is None else round(s + det, 4)
    out["complete"] = out["hours_median"] is not None
    return out


def judge_projection(smoke: dict, opt: dict) -> dict:
    """Judge phase for one option from the plan's estimates (``smoke_q.judge_estimates_h``). Never measured here: the
    judge is not contacted by this command."""
    est = smoke["judge_estimates_h"]
    temp = float(opt.get("temperature", 0.0))
    arm_draws = 0
    for rung in opt["rungs"]:
        if rung == "rung2" and temp == 0.0:
            continue                    # its draw-0 map is the main grid's, judged with it
        arm_draws += int(opt["draws"])
    lo, hi = est["main_grid"][0] + arm_draws * est["per_ladder_arm_draw"][0], \
        est["main_grid"][1] + arm_draws * est["per_ladder_arm_draw"][1]
    return {"label": est.get("label", JUDGE_LABEL), "main_grid_h": est["main_grid"], "ladder_arm_draws": arm_draws,
            "per_ladder_arm_draw_h": est["per_ladder_arm_draw"], "hours_low": round(lo, 2), "hours_high": round(hi, 2)}


def project(rows: list[dict], cfg: RunConfig, smoke: dict) -> dict:
    """The real run's wall-clock per phase for each option (and each extra), Qwen from the measurements and judge from
    the plan's estimates. ``total`` adds the Qwen median to the judge low and the Qwen p90 to the judge high, and is
    marked partly estimated."""
    def one(opt: dict) -> dict:
        q, j = qwen_projection(rows, cfg, smoke, opt), judge_projection(smoke, opt)
        both = q["hours_median"] is not None
        return {"definition": {k: opt[k] for k in ("rungs", "draws") if k in opt}
                | ({"temperature": opt["temperature"]} if "temperature" in opt else {}),
                "qwen_phase": q, "judge_phase": j,
                "total_h": {"low": round(q["hours_median"] + j["hours_low"], 2) if both else None,
                            "high": round(q["hours_p90"] + j["hours_high"], 2) if both else None,
                            "note": "Qwen median + judge low, Qwen p90 + judge high; the judge part is an estimate"}}
    opts = {name: one(o) for name, o in smoke["options"].items()}
    names = list(opts)
    for i, name in enumerate(names):
        same = [m for m in names[:i] if opts[m]["definition"] == opts[name]["definition"]]
        if same:
            opts[name]["same_as"] = same[0]
    # an extra is a cost estimate for a decision: the real-data guard (ladder.check_rung_spec) admits only the T=0
    # primary setting, so no config can run it on real data yet; saying so keeps "approved" from meaning "runnable"
    extras = {name: {**one(o), "runnable": False,
                     "runnable_note": "estimate only: the real-data guard admits the T=0 primary setting alone, "
                                      "so this cannot run on real data until a guarded path for it is added"}
              for name, o in smoke.get("extras", {}).items()}
    return {"qwen_phase_label": "projected from this smoke's measurements (per-call time x the real run's call counts)",
            "judge_phase_label": JUDGE_LABEL, "options": opts, "extras": extras}


def build_budget(cfg: RunConfig, smoke: dict, padded: RunConfig, *, verified: bool) -> dict:
    """budget.json for the two runs of the smoke (their logs are read from the run folders)."""
    syn, pad = read_attempts(Path(cfg.out_dir) / "logs" / "run.jsonl"), read_attempts(Path(padded.out_dir) / "logs" / "run.jsonl")
    rows = measured_rows("synthetic", syn) + measured_rows("padded", pad)
    ids = {a["model_id"] for a in syn + pad if a["model_id"]}
    hosts = {a["endpoint_host"] for a in syn + pad if a["endpoint_host"]}
    warnings = []
    if len(ids) > 1 or len(hosts) > 1:
        warnings.append("more than one model id or endpoint served this smoke (a rerun against another server): the "
                        "measurements mix instruments; start a new --out to time one server")
    if any(a["latency_ms"] is None for a in syn + pad):
        warnings.append("some replies carry no latency; their rows are incomplete")
    return {
        "schema": BUDGET_SCHEMA, "run_id": cfg.run_id, "runs_verified": verified, "warnings": warnings,
        "model_ids": sorted(ids), "endpoint_hosts": sorted(hosts),
        "decisions": ["DECISIONS C7: one draw per ladder arm at the primary setting (T=0); rung 2 draw 0 is the main-grid map",
                      "DECISIONS L12/L13: this smoke is synthetic; the real run needs its own go"],
        "measured": {"padded_words": int(smoke["padded"].get("target_words", 12000)),
                     "n_replies": {"synthetic": len(syn), "padded": len(pad)},
                     "sum_latency_s": {"synthetic": round(sum(a["latency_ms"] or 0 for a in syn) / 1000, 1),
                                       "padded": round(sum(a["latency_ms"] or 0 for a in pad) / 1000, 1)},
                     "rows": rows},
        "real_run_assumptions": smoke["real"] | {"n_specs": len(cfg.specs)},
        "projection": project(rows, cfg, smoke),
        "notes": ["Qwen figures are per-call time measured on a synthetic transcript padded to real length, multiplied by the "
                  "real call counts; real transcripts may produce longer or shorter maps (hours_upper_bound stretches the "
                  "latency to the planned output length when the padded maps were shorter).",
                  "p90 of fewer than 10 replies is just their maximum (p90_meaningful is false).",
                  "Not included: the real run's preflight and canary (they re-run the synthetic demo corpus through the same "
                  "clients), and any rerun after an outage.",
                  "The judge phase is the plan's estimate and is not measured by this command."]}


def write_budget(path, budget: dict) -> None:
    """Atomic write of budget.json."""
    p = Path(path)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(budget, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
