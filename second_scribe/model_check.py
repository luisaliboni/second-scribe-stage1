"""The Setup model check (K10): the kit's synthetic canary, run once per model pair before any real data, as a kit run
of its own lean synthetic config, ``configs/model-check.json`` (``CHECK_CONFIG``), in ``<out_dir>/lean/run``.

The lean config is the demo config cut to what the study runs: rung 2 only (the primary spec, mapping talk removed,
p0, T 0; no ladder, no other spec), the one no-interview map per role at the study's reading-3 setting (p0, one
sample, T 0; no prior-only script set, no judge script), on 6 of the 12 synthetic cases: SYN-OF01, SYN-OF03, SYN-OF05
(officers) and SYN-OW01, SYN-OW03, SYN-OW06 (owners). These are the six cases that carry both a wished and a negated
trap, the two kinds the trap rule gates on, so each gated kind keeps six rows (one miss of six still passes the 0.80
rule, two do not); together they carry all eleven planted kinds and both speaker-label formats and all three
interviewers. The judge check's planted items are the same four as with the twelve cases (the first two wished and
negated traps in pid order: SYN-OF01 and SYN-OF03). The determinism audit re-sends ``corpus.audit_determinism_k`` = 6
logged T 0 requests per role (20 in a study run). The study's demo config and the real-run canary are unchanged.

A one-machine user loads the reconstructor for the Q session, then swaps in the judge for the J session, so the check
is two sessions (two processes) of ONE run: ``stages="Q"`` runs the Q stage group; ``stages="J"`` resumes the same run
item by item (nothing is ever wiped) with the J and M groups, because the trap rows the canary scores are written in
the M stage. After J+M the canary is scored (the trap rule, plus the preflight's planted modality items through the
judge, ``judge_check``); after Q, when the pair has no judge, the reconstruction canary is. Then
``<out_dir>/model_check.json`` is written:

    {"schema": "model-check/1", "passed": bool, "counts": <AuditResult.counts>, "failures": [codes],
     "models": {role: model id}, "digests": {role: 64-hex}, "prompt_set_sha256": hex, "code_sha256": hex,
     "log_head": [records, head hash]}

A tool config names that file and its sha256 in ``tool.model_check``; ``audits._canary_result`` then reuses it instead
of running the canary in every session (matched on model ids, digests and the prompt-set hash). The demo corpus is
synthetic, so ``pipeline.run`` skips its real-corpus guard: the check calls ``guard_real_clients`` itself, with the
tool's two-family judge rule, before any call."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import audits, codehash, pipeline
from .audit.registry import AuditLog
from .llm import guard_real_clients

SCHEMA = "model-check/1"
STAGES = ("Q", "J")
FILE_NAME = "model_check.json"
_HEX64 = re.compile(r"[0-9a-f]{64}")
CHECK_CONFIG = pipeline.KIT_ROOT / "configs" / "model-check.json"
# The check's own folder inside ``<out_dir>``: a folder left by the earlier check (the whole demo config, in
# ``<out_dir>/run``) is never resumed with the lean config.
WORK_DIR = "lean"


def stage_list(stages: str) -> list[str]:
    """The pipeline stages of a model-check session: Q, or J then M (the canary's trap rows come from M)."""
    if stages == "Q":
        return list(pipeline.STAGE_GROUPS["Q"])
    if stages == "J":
        return list(pipeline.STAGE_GROUPS["J"]) + list(pipeline.STAGE_GROUPS["M"])
    raise ValueError(f"model check stages must be one of {STAGES}, got {stages!r}")


def run_dir(out_dir: Path) -> Path:
    return Path(out_dir) / WORK_DIR / "run"


def judge_check_log(out_dir: Path) -> Path:
    """The judge check's own log (appended to on a rerun)."""
    return Path(out_dir) / WORK_DIR / "judge_check" / "logs" / "run.jsonl"


def log_path(out_dir: Path) -> Path:
    return run_dir(out_dir) / "logs" / "run.jsonl"


def run_model_check(out_dir: Path, clients: dict, stages: str, *, families: dict | None = None,
                    digests: dict | None = None, remote_endpoints: list[dict] | None = None,
                    endpoint_optins: list[dict] | None = None) -> dict:
    """Run one session of the model check (see the module doc) and return its result. When the session ends the check
    (J, or Q with no judge client) the result is the ``model_check.json`` document, also written there; a Q session of
    a pair that has a judge returns ``{"schema", "state": "q_done", "passed": None}`` and writes nothing.

    ``families`` ({role: family}, optional) feeds the two-family judge rule; ``digests`` ({role: 64-hex}, optional) are
    the weight digests the app read from the model server, recorded so a run config's ``tool.model_digests`` can be
    matched against them. ``remote_endpoints`` (optional, the app's K13 allow-list ``[{role, host, provider,
    declaration_sha256}]``, as ``tool.remote_endpoints``) lets a declared remote endpoint through the guard; None (the
    default, and every study path) refuses every non-local endpoint as before. When given it is recorded in the
    document. ``endpoint_optins`` (optional, audit K7, as ``tool.endpoint_optins``: ``[{role, host}]``; ``[]`` when the
    app has none) makes the guard refuse a LAN or Tailscale host that has no opt-in for its role; None (the default,
    and every study path) keeps today's guard. ``RealGuardRefused`` is raised before any call when the pair breaks the guard."""
    names = stage_list(stages)
    digests = dict(digests or {})
    if bad := [r for r, d in digests.items() if not (isinstance(d, str) and _HEX64.fullmatch(d))]:
        raise ValueError(f"the {bad[0]} digest must be a 64-character lowercase hex sha256")
    if remote_endpoints is not None:
        check_remote_endpoints(remote_endpoints)
    if endpoint_optins is not None:
        check_endpoint_optins(endpoint_optins)
    guard_real_clients(clients, judge_rule="two_family", families=families, remote_endpoints=remote_endpoints,
                       endpoint_optins=endpoint_optins)
    out_dir = Path(out_dir)
    cfg = pipeline.RunConfig.load(CHECK_CONFIG, out_dir=str(run_dir(out_dir)))
    if not pipeline.is_synthetic(cfg):
        raise ValueError("the model check config must be a synthetic corpus")
    lp = log_path(out_dir)
    if stages == "J" and not lp.exists():
        raise FileNotFoundError("the model check's J session needs its Q session first (no run log)")
    lp.parent.mkdir(parents=True, exist_ok=True)
    log = AuditLog(lp, cfg.run_id)
    blocked: audits.AuditBlocked | None = None
    result: dict = {}
    try:
        result = pipeline.run(cfg, names, clients, log=log)
    except audits.AuditBlocked as exc:          # a stage group's own auditor stopped the check: the pair fails
        blocked = exc
    finally:
        n, head = log.head()
        log.close()
    if stages == "Q" and "judge" in clients and blocked is None:
        return {"schema": SCHEMA, "state": "q_done", "passed": None}
    if blocked is not None:
        res = audits.AuditResult("canary", True, False, {"blocked": [r.name for r in blocked.results]},
                                 [f for r in blocked.results for f in r.failures] or ["model_check_blocked:0"])
    elif stages == "J":
        res = audits.audit_canary(result)
        if clients.get("judge") is not None:        # the trap rule reads the maps; the judge's labels are checked here
            jc_counts, jc_failures = judge_check(out_dir, cfg, clients["judge"])
            res = audits.AuditResult("canary", True, res.passed and not jc_failures,
                                     {**res.counts, "judge_check": jc_counts}, [*res.failures, *jc_failures])
    else:                                       # a pair without a judge: the reconstruction canary is all it can show
        res = audits.audit_canary_recon(run_dir(out_dir) / "work")
    doc = {"schema": SCHEMA, "passed": bool(res.passed), "counts": res.counts, "failures": list(res.failures),
           "models": pipeline.model_ids(clients), "digests": digests,
           "prompt_set_sha256": pipeline.prompt_set_sha256(), "code_sha256": codehash.code_sha256(),
           "log_head": [n, head]}
    if remote_endpoints is not None:
        doc["remote_endpoints"] = [dict(e) for e in remote_endpoints]
    from .report import write_text_atomic
    write_text_atomic(out_dir / FILE_NAME, json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    return doc


def judge_check(out_dir: Path, cfg, client) -> tuple[dict, list[str]]:
    """The judge part of the kit's preflight, run on the pair's judge in the J session: the planted modality items of
    the synthetic corpus (two wished, two negated) must come back with their own label at least ``CANARY_MIN_PASS`` of
    the time. The trap rule scores the reconstructed maps, so a judge that labels every element "actual" passes it;
    this check does not. Its calls go to their own log, ``judge_check_log(out_dir)`` (as the preflight's do),
    appended to on a rerun. Returns (counts, failure codes ``model_check_judge_*``)."""
    from types import SimpleNamespace
    lp = judge_check_log(out_dir)
    lp.parent.mkdir(parents=True, exist_ok=True)
    counts: dict = {"min_pass_rate": audits.CANARY_MIN_PASS}
    log = AuditLog(lp, cfg.run_id)
    try:
        planted = audits._planted_items(cfg, SimpleNamespace(people=pipeline.load_participants(cfg)))
        failures = audits._judge_planted(log, client, *planted, counts, "model_check_judge")
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check
        counts["error"] = type(exc).__name__
        failures = [f"model_check_judge_error:{type(exc).__name__}"]
    finally:
        log.close()
    return counts, failures


def parse_role_map(text: str | None, what: str) -> dict:
    """``"recon=a,judge=b"`` -> {"recon": "a", "judge": "b"}; None or "" -> {}. ValueError on a malformed entry."""
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        role, sep, value = part.partition("=")
        if not sep or not role.strip() or not value.strip():
            raise ValueError(f"{what}: expected role=value entries separated by commas")
        out[role.strip()] = value.strip()
    return out


def check_remote_endpoints(eps) -> list[dict]:
    """``eps`` as ``tool.remote_endpoints`` must be (``toolcfg.check_tool``'s rule); ``ValueError`` naming the key."""
    from .toolcfg import check_tool
    check_tool({"remote_endpoints": eps}, {})
    return eps


def load_remote_endpoints(path) -> list[dict]:
    """The allow-list file of ``model-check --remote-endpoints``: a JSON list of ``{role, host, provider,
    declaration_sha256}`` (no key, no secret). ``ValueError`` when it cannot be read or is not well formed."""
    try:
        eps = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"--remote-endpoints: cannot read the file ({type(exc).__name__})") from None
    try:
        return check_remote_endpoints(eps)
    except ValueError as exc:
        raise ValueError(f"--remote-endpoints: {exc}") from None


def check_endpoint_optins(ops) -> list[dict]:
    """``ops`` as ``tool.endpoint_optins`` must be (``toolcfg.check_tool``'s rule); ``ValueError`` naming the key."""
    from .toolcfg import check_tool
    check_tool({"endpoint_optins": ops}, {})
    return ops


def load_endpoint_optins(path) -> list[dict]:
    """The opt-in file of ``model-check --endpoint-optins``: a JSON list of ``{role, host}`` (``[]`` is valid: no
    opt-in). ``ValueError`` when it cannot be read or is not well formed."""
    try:
        ops = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"--endpoint-optins: cannot read the file ({type(exc).__name__})") from None
    try:
        return check_endpoint_optins(ops)
    except ValueError as exc:
        raise ValueError(f"--endpoint-optins: {exc}") from None
