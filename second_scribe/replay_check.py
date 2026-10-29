"""replay-check (spec §13, F24; app T20): proves that shipped Try-it data is the recorded run. Replay only: the
``replay`` client serves the recorded responses, no model is called and nothing leaves the machine.

``replay_check(config, fixtures, recorded_log, view_js) -> (ok, reasons)`` replays ``config`` with the fixtures into a
temporary out_dir and checks:

1. **Requests.** ``recorded_log`` verifies against its manifest (else ``recorded_log_unverified``). The replayed
   ``call_request`` sequence equals the recorded one, index by index: the same ``messages_sha256``
   (``request_mismatch:<n>``, the first differing index; a shorter or longer replay differs at the shorter length) and
   the same model id, which must also be ``fixtures["_meta"]["models"][role]`` (``model_mismatch:<n>``). A call's model
   id is its logged ``model_id``, else the model the session's ``run_start`` names for the call's role (a replay
   client logs no ``model_id``; its label is the fixtures' model). A replay that runs out of recorded responses stops
   (``replay_stopped:<messages_sha256>``) and only check 1 runs on what it logged.
2. **Every recording is used.** For each fixtures key, the replay client served every recorded call: all ``calls`` of
   a list, or the one record of a single value (``unused_fixture:<key>:<i>``). The count is taken at the client, so it
   includes the determinism re-asks, which the client serves but ``run.jsonl`` does not log.
3. **Work integrity**, forced for a synthetic run (``audit.verify.work_integrity_problem(force=True)``,
   ``work_integrity:<detail>``), and the replay's own chain verifies (``replay_unverified:<reason>``).
4. **The view.** ``export_view.build_view`` + ``view_to_js`` on the replayed run equals ``view_js`` (``view_differs``;
   ``view_unreadable`` when it is not a ``view_to_js`` file). ``view_js`` must be exactly the exporter's bytes for its
   own content, and the content must equal the rebuilt view except for the four fields no second replay can
   reproduce: each ``request_id``'s random suffix (``uuid4``), each ``record_sha256`` and the ``chain_head`` (they hash
   records that carry those ids and timestamps), and ``checked_at``. Every other byte, ``record_index`` included, must
   match.
5. **Privacy scan** of the fixtures, the recorded log (and its manifest) and ``view_js``: no IPv4 literal outside
   loopback (private, shared-address or public ranges alike), no home path (``/Users/``, ``/home/``, ``C:\\Users``),
   no ``endpoint_host`` other than ``127.0.0.1:<port>`` or ``localhost:<port>``, no URL to another host, and nothing
   shaped like an API key (``privacy:<file name>:<what>``, never the text itself).
"""
from __future__ import annotations

import copy
import contextlib
import io
import ipaddress
import json
import re
import tempfile
from pathlib import Path

from . import cli, export_view
from .audit import verify as audit_verify
from .llm import FixtureMissing, messages_sha256
from .pipeline import STAGE_ORDER, RunConfig
from .prereg import STAGE_ROLES

VOLATILE_KEYS = ("chain_head", "checked_at", "record_sha256")     # masked in both views before the comparison
_MASK = "<per-replay>"
_RID_SUFFIX = re.compile(r":[0-9a-f]{8}$")                        # log_call's uuid4().hex[:8]
_VIEW_JS = re.compile(r'window\.SS_DATA = window\.SS_DATA \|\| \{\}; window\.SS_DATA\["([A-Za-z_][A-Za-z0-9_]*)"\] = '
                      r"(.*);", re.S)

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
_HOME = re.compile(r"/Users/|/home/|\b[A-Za-z]:\\{1,2}Users\\", re.I)
_ENDPOINT = re.compile(r'"endpoint_host"\s*:\s*"([^"]*)"')
_LOOPBACK_ENDPOINT = re.compile(r"(?:127\.0\.0\.1|localhost):\d{1,5}")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s\"'<>\\?#]+)", re.I)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|\bBearer\s+[A-Za-z0-9._~+/-]{20,}|\bAKIA[0-9A-Z]{16}\b"
                     r"|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


# ------------------------------------------------------------------------------------------------ 5. privacy
def _scan_text(text: str) -> set[str]:
    found: set[str] = set()
    for m in _IPV4.finditer(text):
        try:
            addr = ipaddress.IPv4Address(m.group(1))
        except ValueError:            # not an address (an octet over 255, a leading zero)
            continue
        if not addr.is_loopback:
            found.add("ipv4")
            break
    if _HOME.search(text):
        found.add("home_path")
    if any(not _LOOPBACK_ENDPOINT.fullmatch(m.group(1)) for m in _ENDPOINT.finditer(text)):
        found.add("endpoint_host")
    for m in _URL.finditer(text):
        host = m.group(1).rsplit("@", 1)[-1]                       # no userinfo
        host = host.split("]", 1)[0] + "]" if host.startswith("[") else host.split(":", 1)[0]     # no port
        if host.lower() not in _LOOPBACK_HOSTS:
            found.add("url_host")
            break
    if _SECRET.search(text):
        found.add("secret")
    return found


def privacy_scan(paths) -> list[str]:
    """``privacy:<file name>:<what>`` for each file and kind of hit, sorted; ``<what>`` is ``ipv4``, ``home_path``,
    ``endpoint_host``, ``url_host``, ``secret`` or ``unreadable``. Never the matched text."""
    reasons: set[str] = set()
    for p in paths:
        p = Path(p)
        try:
            text = p.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            reasons.add(f"privacy:{p.name}:unreadable")
            continue
        reasons.update(f"privacy:{p.name}:{what}" for what in _scan_text(text))
    return sorted(reasons)


# ------------------------------------------------------------------------------------------------ 1. requests
def _role(stage: str) -> str | None:
    for prefix, role in STAGE_ROLES:
        if stage.startswith(prefix):
            return role
    return None


def _calls(records) -> list[tuple]:
    """(messages_sha256, model id, role) per ``call_request``, in log order. The model id is the logged one, else
    the one the session's ``run_start`` names for the call's role."""
    models: dict = {}
    out = []
    for r in records:
        p = r.get("payload") or {}
        if r.get("record_type") == "run_start":
            models = p.get("models") if isinstance(p.get("models"), dict) else {}
        elif r.get("record_type") == "call_request":
            role = _role(str(p.get("stage", "")))
            out.append((p.get("messages_sha256"), p.get("model_id") or models.get(role), role))
    return out


def compare_requests(recorded: list[dict], replayed: list[dict], meta_models: dict) -> list[str]:
    """``request_mismatch:<n>`` and ``model_mismatch:<n>``, each at its first index, or []."""
    a, b = _calls(recorded), _calls(replayed)
    reasons = []
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x[0] != y[0]), None)
    if n is None and len(a) != len(b):
        n = min(len(a), len(b))
    if n is not None:
        reasons.append(f"request_mismatch:{n}")
    for i, ((_, rec_model, role), (_, rep_model, _)) in enumerate(zip(a, b)):
        expected = meta_models.get(role) if role is not None else rec_model
        if rec_model is None or rec_model != rep_model or rec_model != expected:
            reasons.append(f"model_mismatch:{i}")
            break
    return reasons


def _read_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ------------------------------------------------------------------------------------------------ 2. replay
def _replay(cfg: RunConfig, fixtures: Path) -> dict[str, int]:
    """Replay every stage of ``cfg`` with the ``replay`` client (as ``demo --client replay`` does) and return how
    many calls the client served per ``messages_sha256``, determinism re-asks included. Raises what the run raises
    (``FixtureMissing`` when the recording has no response for a request)."""
    clients = cli.build_clients("replay", cfg, str(fixtures))
    served: dict[str, int] = {}
    for c in {id(c): c for c in clients.values()}.values():
        inner = c.complete

        def counted(messages, *, _inner=inner, **kw):
            resp = _inner(messages, **kw)
            key = messages_sha256(messages)
            served[key] = served.get(key, 0) + 1
            return resp
        c.complete = counted
    # This internal replay feeds a result-only command and the site builder.
    # The run/demo summary belongs to those commands' public CLI, not this check.
    with contextlib.redirect_stdout(io.StringIO()):
        cli._execute(cfg, STAGE_ORDER, clients, None)
    return served


def unused_fixtures(fixtures: dict, served: dict[str, int]) -> list[str]:
    """``unused_fixture:<key>:<i>`` for every recorded call the replay did not consume (``_meta`` excluded)."""
    reasons = []
    for key in sorted(k for k in fixtures if k != "_meta"):
        v = fixtures[key]
        n = len(v["calls"]) if isinstance(v, dict) and isinstance(v.get("calls"), list) else 1
        reasons += [f"unused_fixture:{key}:{i}" for i in range(min(served.get(key, 0), n), n)]
    return reasons


# ------------------------------------------------------------------------------------------------ 4. view
def normalize_view(view: dict) -> dict:
    """A copy with the per-replay fields masked: ``chain_head``, ``checked_at``, ``record_sha256`` and the random
    suffix of every ``request_id``."""
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in VOLATILE_KEYS and isinstance(v, str):
                    x[k] = _MASK
                elif k == "request_id" and isinstance(v, str):
                    x[k] = _RID_SUFFIX.sub("", v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    out = copy.deepcopy(view)
    walk(out)
    return out


def parse_view_js(text: str) -> tuple[str, dict]:
    """(var, view) of a ``view_to_js`` file; ValueError if it is not one."""
    m = _VIEW_JS.fullmatch(text.strip())
    if not m:
        raise ValueError("not a view_to_js file")
    doc = json.loads(m.group(2))
    if not isinstance(doc, dict):
        raise ValueError("payload is not an object")
    return m.group(1), doc


def view_problem(rebuilt: dict, view_js_text: str) -> str | None:
    try:
        var, shipped = parse_view_js(view_js_text)
    except ValueError:
        return "view_unreadable"
    if export_view.view_to_js(shipped, var) != view_js_text:
        return "view_differs"               # not the exporter's own bytes for its content
    if export_view.view_to_js(normalize_view(rebuilt), var) != export_view.view_to_js(normalize_view(shipped), var):
        return "view_differs"
    return None


# ------------------------------------------------------------------------------------------------ the check
def replay_check(config: Path, fixtures: Path, recorded_log: Path, view_js: Path) -> tuple[bool, list[str]]:
    """(ok, reasons); see the module docstring. Writes only into a temporary folder it removes."""
    config, fixtures, recorded_log, view_js = Path(config), Path(fixtures), Path(recorded_log), Path(view_js)
    scanned = [fixtures, recorded_log]
    manifest = Path(audit_verify.manifest_path_for(recorded_log))
    if manifest.exists():
        scanned.append(manifest)
    reasons: list[str] = privacy_scan(scanned + [view_js])

    recorded: list[dict] | None = None
    try:
        rec_ok, _detail = audit_verify.verify_run(recorded_log)
        recorded = _read_records(recorded_log)
    except (OSError, ValueError):
        rec_ok = False
    if not rec_ok:
        reasons.append("recorded_log_unverified")

    try:
        fx = json.loads(fixtures.read_text(encoding="utf-8"))
        if not isinstance(fx, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        reasons.append("fixtures_unreadable")
        return False, reasons
    meta_models = ((fx.get("_meta") or {}).get("models") or {}) if isinstance(fx.get("_meta"), dict) else {}

    with tempfile.TemporaryDirectory(prefix="ss-replay-check-") as tmp:
        cfg = RunConfig.load(str(config), out_dir=str(Path(tmp) / "out"))
        log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"
        served = None
        try:
            served = _replay(cfg, fixtures)
        except FixtureMissing as exc:
            reasons.append(f"replay_stopped:{exc.args[0]}")
        except Exception as exc:  # noqa: BLE001 - any failed replay is a failed check, named by its type
            reasons.append(f"replay_failed:{type(exc).__name__}")
        replayed = _read_records(log_path) if log_path.exists() else []
        if recorded is not None:
            reasons += compare_requests(recorded, replayed, meta_models)
        if served is not None:
            reasons += unused_fixtures(fx, served)
            work = Path(cfg.out_dir) / "work"
            problem = audit_verify.work_integrity_problem(log_path, work, force=True)
            if problem:
                reasons.append(f"work_integrity:{problem}")
            ok, detail = audit_verify.verify_run(log_path)
            if not ok:
                reasons.append(f"replay_unverified:{detail.get('reason')}")
            try:
                text = view_js.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                reasons.append("view_unreadable")
            else:
                rebuilt = export_view.build_view(cfg, source="synthetic", reference_type=None)
                if (problem := view_problem(rebuilt, text)):
                    reasons.append(problem)
    return not reasons, reasons
