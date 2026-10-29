"""The run certificate (T41): what was run and whether the logs still verify, read from the logs alone.

The word "certificate" names this document and nothing more. It attests the integrity of the logs and what was run
(code, config, prompts, models); it does not attest that the results are right. ``LIMIT`` says so, and the rendered
markdown opens with ``ATTESTS``.

``build_certificate(run_dirs, app_log)`` returns a ``certificate/1`` dict:

- per run folder (every folder given, failed or interrupted ones included): the run id, the outcome, a fresh
  ``verify_run`` on ``logs/run.jsonl`` with ``checked_at``, the chain head, ``code_sha256``, ``code_commit``,
  ``config_sha256``, ``prompt_set_sha256`` and (tool runs) the rung-1 and no-interview prompt hashes, the model ids
  from ``run_start``, the model digests from the folder's ``config.json`` (``tool.model_digests``) checked by
  recomputing that config's hash against ``run_start.config_sha256``, the remote endpoints with their declaration
  hashes, and a remote-model line per remote role (plus a warning for an alias id);
- per remote role (audit K4/S1, K5/S4): the declaration check (``declaration_check``: the config's
  ``declaration_sha256`` must name an app-log ``declaration`` record for that role, host and provider, whose own text
  hashes to it, written before the run started, i.e. below the run's ``app_log_head``) and the stamped sentence
  "Transcript text was sent to <provider> (<host>) for <role>, under: <declaration>" (``remote_sentences``, also for
  the report);
- the app log, when given: a fresh verify, the opt-ins, declarations (with the typed basis), ``coding_locked`` and
  ``reveal`` records (their positions only: never a participant id), and, when anything was locked, the blindness
  limit sentence.

Nothing here reads transcripts, maps, work items or model replies, so no participant text can reach the output.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .audit.verify import verify_run

SCHEMA = "certificate/1"

ATTESTS = ("This document attests the integrity of the logs and what was run. "
           "It does not attest that the results are right.")

LIMIT_TEMPLATE = ("The chain can reveal edits when checked against a trusted head; it does not show what the "
                  "input was, nor that the models' answers are right. Full verification needs the logs, which hold "
                  "participant text, so it was run {who} on {date}.")

# SPEC section 6.7 "Stated limit" (Part 6b: the dash after "cryptographic" became ", so"). Task 37 shows the same sentence on the coding screen.
BLINDNESS_LIMIT = ("blindness is enforced by this interface and recorded in the log order; it is not cryptographic, "
                   "so someone could open the run folder by hand.")

REMOTE_LINE = "remote model: weights not inspectable; the provider may change the model behind this id"

RUN_START_FIELDS = ("code_sha256", "code_commit", "config_sha256", "prompt_set_sha256",
                    "rung1_user_prompt_sha256", "reading3_prompt_sha256")
LOG_REL = Path("logs") / "run.jsonl"
CONFIG_NAME = "config.json"

_HEX64 = re.compile(r"[0-9a-f]{64}")
# a date or a version after the name pins a snapshot: 20250929, 2025-09-29, 0613 (MMDD), "@2", ":v2"
_PINNED = re.compile(r"(?<!\d)(?:20\d{2}[-_]?(?:0[1-9]|1[0-2])[-_]?(?:0[1-9]|[12]\d|3[01])"
                     r"|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?!\d)|@\d|:v\d|-v\d")


# --- declarations (audit K4/S1, K5/S4) ---------------------------------------------------------------------------
# The app's ``declaration`` record (app/models.py): {schema, role, provider, host, basis, sha256}; sha256 is taken over
# the canonical JSON of the first five keys. The record lives in the app log only, never in a shared profile.
DECLARATION_KEYS = ("schema", "role", "provider", "host", "basis")
DECLARATION_MAX_CHARS = 2000            # the app caps the typed basis lower (models.MAX_BASIS); longer is not shown
REMOTE_ROLES = ("recon", "judge", "judge2")             # the kit's client roles (PROFILE-SCHEMA 14.8)
REMOTE_PROVIDERS = ("openai_compat", "anthropic")       # toolcfg.PROVIDERS
SENT_TEMPLATE = "Transcript text was sent to {provider} ({host}) for {role}, under: {basis}"
SENT_MISSING = "Transcript text was sent to {provider} ({host}) for {role}; no declaration with this hash is in the app log."
STRINGS_FILE = Path(__file__).resolve().parent / "profile_strings.json"
# the status of one remote role's declaration -> (ok, the certificate's line); {seq} is the app-log record number
DECLARATION_STATUS = {
    "found_before": (True, "declaration: found, before the run (app log record {seq})"),
    "found_after": (False, "declaration: FLAG, app log record {seq} was written after the run started"),
    "order_unchecked": (False, "declaration: FLAG, found (app log record {seq}), but the run records no app log "
                               "position, so it cannot be shown to come before the run"),
    "head_mismatch": (False, "declaration: FLAG, this app log is not the one the run was started from (it does not "
                             "hold the run's app log head)"),
    "text_mismatch": (False, "declaration: REFUSED, the text of app log record {seq} does not hash to the "
                             "declaration sha256 in the config"),
    "endpoint_mismatch": (False, "declaration: REFUSED, app log record {seq} declares another role, host or provider"),
    "not_found": (False, "declaration: NOT FOUND in the app log"),
    "unchecked": (None, "declaration: not checked (no app log given)"),
}


def declaration_record_sha256(payload: dict) -> str:
    """sha256 of a declaration's canonical content (``DECLARATION_KEYS`` only; sorted keys, no spaces, UTF-8): the
    hash ``app/models.declaration_sha256`` writes into the record and the run config carries."""
    doc = {k: payload.get(k) for k in DECLARATION_KEYS}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def _kit_strings() -> dict:
    try:
        d = json.loads(STRINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _lower_first(text: str) -> str:
    """``text`` with its first letter lower-cased for use inside a sentence, unless it starts an acronym ("AI")."""
    return text[:1].lower() + text[1:] if len(text) > 1 and text[1:2].islower() else text


def provider_display(code: Any) -> str:
    """The provider's name inside a sentence: the ``profile.provider.<code>`` string up to its first colon ("An
    OpenAI-compatible cloud endpoint: ..." -> "an OpenAI-compatible cloud endpoint"), else the code itself."""
    text = _kit_strings().get(f"profile.provider.{code}")
    if not isinstance(text, str) or not text.strip():
        return str(code)
    short = text.split(":", 1)[0].strip().rstrip(".")
    return _lower_first(short) if short.split(" ", 1)[0] in ("A", "An", "The") else short


def role_display(code: Any) -> str:
    """The role's name inside a sentence: the ``profile.role.<code>`` string ("Judge" -> "judge"), else the code."""
    text = _kit_strings().get(f"profile.role.{code}")
    return _lower_first(text.strip()) if isinstance(text, str) and text.strip() else str(code)


def _one_line(text: str) -> str:
    """The typed basis on one line (no control character, whitespace runs folded) so it cannot start a new line."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())


def _tool_of(run_config: Any) -> dict:
    tool = run_config.get("tool") if isinstance(run_config, dict) else getattr(run_config, "tool", None)
    return tool if isinstance(tool, dict) else {}


def _app_log_bound(sessions: list[dict]) -> dict | None:
    """The run's ``app_log_head`` ({records, head}) from its first ``run_start``, or None."""
    h = sessions[0].get("app_log_head") if sessions else None
    if isinstance(h, dict) and isinstance(h.get("records"), int) and not isinstance(h.get("records"), bool) \
            and _hex(h.get("head")):
        return {"records": h["records"], "head": h["head"]}
    return None


def declaration_check(app_log_records: list[dict] | None, ep: dict, app_log_head: dict | None = None,
                      require_order: bool = True) -> dict:
    """The declaration of one ``tool.remote_endpoints`` entry ``ep`` in the app log: ``{status, ok, seq, basis}``
    (``DECLARATION_STATUS``). ``app_log_records`` None: unchecked. A record counts only when its ``sha256`` is the
    entry's ``declaration_sha256``, its own text hashes to that value, and its role, host and provider are the
    entry's; it comes before the run when its seq is below ``app_log_head.records`` and the log holds that head.
    ``require_order`` False (the app, before a run exists): the order is not asked for."""
    out = {"status": "unchecked", "ok": None, "seq": None, "basis": None}
    if app_log_records is None:
        return out
    want = _hex(ep.get("declaration_sha256"))
    named = [r for r in app_log_records if r.get("record_type") == "declaration"
             and isinstance(r.get("payload"), dict) and want and r["payload"].get("sha256") == want]
    genuine = [r for r in named if declaration_record_sha256(r["payload"]) == want]
    same = [r for r in genuine if r["payload"].get("role") == ep.get("role")
            and str(r["payload"].get("host") or "").lower() == str(ep.get("host") or "").lower()
            and r["payload"].get("provider") == ep.get("provider")]
    if not named:
        return {**out, "status": "not_found", "ok": False}
    if not genuine:
        return {**out, "status": "text_mismatch", "ok": False, "seq": named[0].get("seq")}
    if not same:
        return {**out, "status": "endpoint_mismatch", "ok": False, "seq": genuine[0].get("seq")}
    rec = same[0]
    basis = rec["payload"].get("basis")
    basis = _one_line(basis) if isinstance(basis, str) and 0 < len(basis) <= DECLARATION_MAX_CHARS else None
    if basis is None:
        return {**out, "status": "text_mismatch", "ok": False, "seq": rec.get("seq")}
    found = {**out, "seq": rec.get("seq"), "basis": basis}
    if not require_order:
        return {**found, "status": "found_before", "ok": True}
    if app_log_head is None:
        return {**found, "status": "order_unchecked", "ok": False}
    n = app_log_head["records"]
    at = [r for r in app_log_records if r.get("seq") == n - 1]
    if n < 1 or not at or at[0].get("hash") != app_log_head["head"]:
        return {**found, "status": "head_mismatch", "ok": False}
    if not isinstance(rec.get("seq"), int) or rec["seq"] >= n:
        return {**found, "status": "found_after", "ok": False}
    return {**found, "status": "found_before", "ok": True}


def remote_sentence(ep: dict, check: dict) -> str:
    """The stamped sentence of one remote role (SPEC 8.1): with the typed basis when the declaration was found (even
    when its order is flagged; the flag is its own line), else the missing-declaration sentence."""
    names = {"provider": provider_display(ep.get("provider")), "host": ep.get("host"), "role": role_display(ep.get("role"))}
    if check.get("basis") is not None:
        return SENT_TEMPLATE.format(**names, basis=check["basis"])
    return SENT_MISSING.format(**names)


def remote_sentences(app_log_records: list[dict], run_config: Any, app_log_head: dict | None = None) -> list[str]:
    """One sentence per ``tool.remote_endpoints`` entry of ``run_config`` (a config dict or a ``RunConfig``), in the
    config's order: "Transcript text was sent to <provider> (<host>) for <role>, under: <declaration>", the
    declaration read from ``app_log_records`` (the app log's records, as read from ``logs/app.jsonl``). Pure: no
    file, no clock. ``[]`` for a config with no remote endpoint (every study config). The order before the run is
    checked by ``declaration_check`` (``app_log_head``), not here: the sentence carries the text either way."""
    out = []
    for ep in _tool_of(run_config).get("remote_endpoints") or []:
        if isinstance(ep, dict):
            out.append(remote_sentence(ep, declaration_check(app_log_records, ep, app_log_head, require_order=False)))
    return out


def limit_text(date: str, study: bool = False) -> str:
    """The chain-limit text, exact. ``study``: the study page's variant, run by the authors."""
    return LIMIT_TEMPLATE.format(who="by the authors" if study else "on this machine", date=date)


def is_alias_id(model_id: str | None) -> bool:
    """True for a model id that carries no date or version suffix (a name the provider may move to another model)."""
    if not model_id:
        return False
    low = str(model_id).lower()
    return low.endswith("latest") or _PINNED.search(low) is None


# ------------------------------------------------------------------------------------------------ reading logs
def _records(path: Path) -> list[dict]:
    """Every parseable record, in file order (a damaged line is skipped: the verify result reports the damage)."""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("payload"), dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _verify(path: Path, checked_at: str) -> dict:
    """A fresh ``verify_run`` on ``path``, reduced to what the certificate prints."""
    if not path.is_file():
        return {"ok": False, "records": 0, "head_hash": None, "manifest_hash": None, "errors": ["log_missing"],
                "reason": "log_missing", "orphan_requests": 0, "checked_at": checked_at}
    try:
        ok, d = verify_run(path)
    except Exception as exc:                      # never let one unreadable log stop the certificate
        return {"ok": False, "records": 0, "head_hash": None, "manifest_hash": None,
                "errors": [f"verify_error:{type(exc).__name__}"], "reason": f"verify_error:{type(exc).__name__}",
                "orphan_requests": 0, "checked_at": checked_at}
    return {"ok": bool(ok), "records": d.get("records", 0), "head_hash": d.get("head_hash"),
            "manifest_hash": d.get("manifest_hash"), "errors": list(d.get("errors", [])),
            "reason": d.get("reason"), "orphan_requests": len(d.get("orphan_requests", [])),
            "checked_at": checked_at}


def _hex(v: Any) -> str | None:
    return v if isinstance(v, str) and _HEX64.fullmatch(v) else None


def _short(v: Any, limit: int = 120) -> str | None:
    return v if isinstance(v, str) and 0 < len(v) <= limit else None


def _sessions(recs: list[dict]) -> list[dict]:
    """One entry per ``run_start``: its identity fields and how the session ended."""
    out: list[dict] = []
    for r in recs:
        t = r["record_type"]
        if t == "run_start":
            p = r["payload"]
            out.append({"ts": r.get("ts"), "corpus_kind": p.get("corpus_kind"),
                        "models": {k: _short(v, 200) for k, v in (p.get("models") or {}).items()
                                   if isinstance(p.get("models"), dict)},
                        "stages": [s for s in (p.get("stages") or []) if isinstance(s, str)],
                        **{k: p.get(k) for k in RUN_START_FIELDS}, "app_log_head": p.get("app_log_head"),
                        "ended": False, "blocked": False})
        elif out and t == "run_end":
            out[-1]["ended"] = True
        elif out and t == "audit_summary" and r["payload"].get("blocked"):
            out[-1]["blocked"] = True
    return out


def _outcome(verify: dict, sessions: list[dict]) -> str:
    if verify["errors"] == ["log_missing"]:
        return "no_log"
    if not verify["ok"]:
        return "interrupted" if verify["errors"] == ["orphan_requests"] else "chain_fail"
    if not sessions:
        return "no_run_start"
    last = sessions[-1]
    if last["ended"]:
        return "complete"
    return "stopped_by_audit" if last["blocked"] else "incomplete"


# ------------------------------------------------------------------------------------------------ one run folder
def _config(run_dir: Path) -> tuple[dict | None, str | None]:
    """(config.json as a dict or None, why not)."""
    p = run_dir / CONFIG_NAME
    if not p.is_file():
        return None, "config.json not found"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "config.json is not readable"
    return (d, None) if isinstance(d, dict) else (None, "config.json is not an object")


def _digest_check(run_dir: Path, cfg: dict | None, why: str | None, sessions: list[dict]) -> dict:
    tool = (cfg or {}).get("tool") if isinstance((cfg or {}).get("tool"), dict) else {}
    if cfg is None:
        return {"status": "unchecked", "detail": why}
    if not tool.get("model_digests"):
        return {"status": "none_recorded", "detail": "config.json records no model digests"}
    logged = [s.get("config_sha256") for s in sessions]
    if not logged or not all(_hex(h) for h in logged):
        return {"status": "unchecked", "detail": "the log records no config hash"}
    try:
        from .pipeline import RunConfig        # lazy: the pipeline is a heavy import
        now = RunConfig.load(run_dir / CONFIG_NAME).sha256()
    except Exception as exc:
        return {"status": "unchecked", "detail": f"config.json could not be loaded ({type(exc).__name__})"}
    if all(now == h for h in logged):
        return {"status": "match", "detail": "config.json hashes to the run's config_sha256, so its digests are "
                                             "the ones the run was started with"}
    return {"status": "mismatch", "detail": "config.json (with its input files) no longer hashes to the run's "
                                            "config_sha256: its digests are not confirmed"}


def _run_entry(run_dir: Path, checked_at: str) -> dict:
    run_dir = Path(run_dir)
    log = run_dir / LOG_REL
    recs = _records(log)
    sessions = _sessions(recs)
    verify = _verify(log, checked_at)
    cfg, why = _config(run_dir)
    tool = cfg.get("tool") if isinstance((cfg or {}).get("tool"), dict) else {}
    first = sessions[0] if sessions else {}
    last = sessions[-1] if sessions else {}
    differ = sorted(k for k in RUN_START_FIELDS if len({json.dumps(s.get(k)) for s in sessions}) > 1)
    digests = tool.get("model_digests") if isinstance(tool.get("model_digests"), dict) else {}
    models_logged = last.get("models") or {}
    roles = sorted(set(models_logged) | set(digests))
    models = {r: {"id": models_logged.get(r), "digest": _hex(digests.get(r))} for r in roles}

    remote = []
    for ep in tool.get("remote_endpoints") or []:
        if not isinstance(ep, dict):
            continue
        role = _short(ep.get("role"), 40)
        mid = (models_logged or {}).get(role)
        alias = is_alias_id(mid) if mid else False
        notes = [REMOTE_LINE]
        if mid is None:
            notes.append("warning: the log records no model id for this role")
        elif alias:
            notes.append(f"warning: model id {mid} has no date or version suffix; it may be an alias that the "
                         "provider moves to another model")
        remote.append({"role": role, "host": _short(ep.get("host")), "provider": _short(ep.get("provider"), 40),
                       "declaration_sha256": _hex(ep.get("declaration_sha256")), "model_id": mid,
                       "alias_warning": bool(alias or mid is None), "notes": notes})
    optins = [{"role": _short(o.get("role"), 40), "host": _short(o.get("host"))}
              for o in tool.get("endpoint_optins") or [] if isinstance(o, dict)]

    entry: dict[str, Any] = {
        "dir": run_dir.name,
        "run_id": _short(recs[0].get("run_id"), 200) if recs else None,
        "outcome": _outcome(verify, sessions),
        "verify": verify,
        "sessions": len(sessions),
        "corpus_kind": first.get("corpus_kind"),
        "stages": last.get("stages", []),
        **{k: first.get(k) for k in RUN_START_FIELDS if first.get(k) is not None or k in
           ("code_sha256", "code_commit", "config_sha256", "prompt_set_sha256")},
        "sessions_differ_in": differ,
        "models": models,
        "digest_check": _digest_check(run_dir, cfg, why, sessions),
        "reference_type": tool.get("reference_type"),
        "judge_rule": tool.get("judge_rule"),
        "model_families": tool.get("model_families") if isinstance(tool.get("model_families"), dict) else {},
        "remote": remote,
        "optins": optins,
    }
    entry["failed"] = entry["outcome"] != "complete"
    return entry


# ------------------------------------------------------------------------------------------------ the app log
def _declaration_entry(r: dict) -> dict:
    """An app-log ``declaration`` record as listed: role, provider, host, sha256, the typed basis (one line), its
    record number and whether its text hashes to its sha256."""
    p = r["payload"]
    basis = p.get("basis")
    return {"role": _short(p.get("role"), 200), "provider": _short(p.get("provider"), 200),
            "host": _short(p.get("host"), 200), "sha256": _hex(p.get("sha256")) or _short(p.get("sha256")),
            "basis": _one_line(basis) if isinstance(basis, str) and 0 < len(basis) <= DECLARATION_MAX_CHARS else None,
            "seq": r.get("seq"), "text_matches": _hex(p.get("sha256")) == declaration_record_sha256(p)}


def _app_entry(app_log: Path, checked_at: str) -> dict:
    app_log = Path(app_log)
    recs = _records(app_log)
    verify = _verify(app_log, checked_at)

    def pick(r: dict, keys: tuple[str, ...]) -> dict:
        p = r["payload"]
        return {k: (_hex(p.get(k)) or _short(p.get(k))) if k.endswith("sha256") else _short(p.get(k), 200)
                for k in keys}

    locks = [r.get("seq") for r in recs if r["record_type"] == "coding_locked"]
    reveals = [r.get("seq") for r in recs if r["record_type"] == "reveal"]
    return {"verify": verify,
            "optins": [pick(r, ("role", "host")) for r in recs if r["record_type"] == "optin"],
            "declarations": [_declaration_entry(r) for r in recs if r["record_type"] == "declaration"],
            "coding_locked": {"count": len(locks), "seqs": locks},
            "reveal": {"count": len(reveals), "seqs": reveals},
            "locks_before_reveal": (max(locks) < min(reveals)) if locks and reveals else None}


def build_certificate(run_dirs: list[Path], app_log: Path | None, *, study: bool = False,
                      now: Callable[[], datetime] | None = None) -> dict:
    """The ``certificate/1`` document for ``run_dirs`` (each a run folder with ``logs/run.jsonl``) and the app log.
    Every log is verified afresh now. ``study``: the study page's limit wording (run by the authors).
    ``now``: a clock returning an aware datetime (tests)."""
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    checked_at = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
    cert: dict[str, Any] = {
        "schema": SCHEMA,
        "checked_at": checked_at,
        "attests": ATTESTS,
        "limit": limit_text(checked_at[:10], study=study),
        "runs": [_run_entry(Path(d), checked_at) for d in run_dirs],
        "app_log": None,
        "blindness_limit": None,
    }
    if app_log is not None:
        cert["app_log"] = _app_entry(Path(app_log), checked_at)
        if cert["app_log"]["coding_locked"]["count"]:
            cert["blindness_limit"] = BLINDNESS_LIMIT
    app_recs = _records(Path(app_log)) if app_log is not None else None
    for run, d in zip(cert["runs"], run_dirs):
        bound = _app_log_bound(_sessions(_records(Path(d) / LOG_REL)))
        for ep in run["remote"]:
            check = declaration_check(app_recs, ep, bound)
            ep["declaration"] = {**check, "line": DECLARATION_STATUS[check["status"]][1].format(seq=check["seq"])}
            ep["sentence"] = remote_sentence(ep, check)
        run["remote_sentences"] = [ep["sentence"] for ep in run["remote"]]
    checks = [ep["declaration"]["ok"] for run in cert["runs"] for ep in run["remote"]]
    cert["all_declarations_ok"] = None if any(c is None for c in checks) else all(checks)
    cert["all_logs_verify"] = (all(r["verify"]["ok"] for r in cert["runs"])
                               and (cert["app_log"] is None or cert["app_log"]["verify"]["ok"]))
    return cert


# ------------------------------------------------------------------------------------------------ markdown
def _verdict(v: dict) -> str:
    if v["ok"]:
        return f"PASS, {v['records']} records, chain head {v['head_hash']}"
    why = ", ".join(v["errors"]) or "unknown"
    head = f", chain head {v['head_hash']}" if v.get("head_hash") else ""
    return f"FAIL ({why}){head}"


def render_certificate_md(cert: dict) -> str:
    """The certificate as markdown. Plain statements only; it says what it attests and what it does not."""
    L: list[str] = ["# Run certificate", "", cert["attests"], "", f"Checked at {cert['checked_at']}.", ""]
    for run in cert["runs"]:
        v = run["verify"]
        L += [f"## Run {run['run_id'] or '(no run id)'} (folder {run['dir']})", "",
              f"- Outcome: {run['outcome']}" + (" (this run did not finish cleanly)" if run["failed"] else ""),
              f"- Log check: {_verdict(v)}; checked at {v['checked_at']}"]
        if run["sessions"]:
            L.append(f"- Sessions in the log: {run['sessions']}; corpus kind: {run['corpus_kind']}")
        if run["sessions_differ_in"]:
            L.append("- Sessions differ in: " + ", ".join(run["sessions_differ_in"]) + " (the first session is shown)")
        for key, label in (("code_sha256", "Code sha256"), ("code_commit", "Code commit"),
                           ("config_sha256", "Config sha256"), ("prompt_set_sha256", "Prompt set sha256"),
                           ("reading3_prompt_sha256", "No-interview prompt sha256"),
                           ("rung1_user_prompt_sha256", "Rung-1 prompt sha256")):
            if key in run:
                L.append(f"- {label}: {run[key] if run[key] is not None else 'not recorded'}")
        if run["reference_type"]:
            L.append(f"- Reference type: {run['reference_type']}")
        for role, m in run["models"].items():
            dg = m["digest"] or "not recorded"
            L.append(f"- Model {role}: {m['id'] or 'not recorded'}; digest {dg}")
        dc = run["digest_check"]
        L.append(f"- Digest check: {dc['status']}; {dc['detail']}")
        for ep in run["remote"]:
            L.append(f"- Remote endpoint {ep['role']}: {ep['provider']} at {ep['host']}; model id "
                     f"{ep['model_id'] or 'not recorded'}; declaration sha256 {ep['declaration_sha256']}")
            L.append(f"  - {ep['sentence']}")
            L.append(f"  - {ep['declaration']['line']}")
            L += [f"  - {n}" for n in ep["notes"]]
        for o in run["optins"]:
            L.append(f"- Network opt-in: {o['role']} at {o['host']}")
        L.append("")
    app = cert["app_log"]
    if app is not None:
        L += ["## App log", "", f"- Log check: {_verdict(app['verify'])}; checked at {app['verify']['checked_at']}"]
        for o in app["optins"]:
            L.append(f"- Opt-in: {o['role']} at {o['host']}")
        for d in app["declarations"]:
            L.append(f"- Declaration (record {d['seq']}): {d['role']}, {d['provider']} at {d['host']}; sha256 "
                     f"{d['sha256']}" + ("" if d["text_matches"] else " (its text does NOT hash to this)"))
            if d["basis"] is not None:
                L.append(f"  - Basis as typed: {d['basis']}")
        n_l, n_r = app["coding_locked"]["count"], app["reveal"]["count"]
        L.append(f"- Codings locked: {n_l} (records {', '.join(map(str, app['coding_locked']['seqs'])) or 'none'})")
        L.append(f"- Reveal records: {n_r} (records {', '.join(map(str, app['reveal']['seqs'])) or 'none'})")
        if app["locks_before_reveal"] is not None:
            L.append("- Every lock precedes the reveal in the log: " + ("yes" if app["locks_before_reveal"] else "NO"))
        L.append("")
    L += ["## Limits", ""]
    if cert["blindness_limit"]:
        L += [f"Stated limit: {cert['blindness_limit']}", ""]
    L += [cert["limit"], ""]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------------ files and CLI
def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_certificate(cert: dict, out_json: Path) -> tuple[Path, Path]:
    """Write ``out_json`` (canonical JSON) and the markdown next to it (same name, ``.md``)."""
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md = out_json.with_suffix(".md")
    _atomic_write(out_json, json.dumps(cert, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    _atomic_write(out_md, render_certificate_md(cert))
    return out_json, out_md


def cmd_certificate(a) -> int:
    """``certificate --run DIR [--run DIR ...] [--app-log FILE] --out FILE.json [--study]``. Exit 0 when every log
    verifies, 1 when one does not (the files are written either way), 2 for a usage error."""
    if not a.run:
        print("refused: give at least one --run DIR", file=sys.stderr)
        return 2
    cert = build_certificate([Path(d) for d in a.run], Path(a.app_log) if a.app_log else None, study=a.study)
    out_json, out_md = write_certificate(cert, Path(a.out))
    bad = [r["dir"] for r in cert["runs"] if not r["verify"]["ok"]]
    if cert["app_log"] is not None and not cert["app_log"]["verify"]["ok"]:
        bad.append("app log")
    undeclared = [f"{r['dir']} ({ep['role']})" for r in cert["runs"] for ep in r["remote"]
                  if ep["declaration"]["ok"] is False]
    msg = "every log verifies" if not bad else "log check FAILED for: " + ", ".join(bad)
    if undeclared:
        msg += "; declaration check FAILED for: " + ", ".join(undeclared)
    print(f"wrote {out_json} and {out_md}; " + msg)
    return 0 if not bad and not undeclared else 1
