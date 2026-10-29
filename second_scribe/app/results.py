"""The app's Results data and the Instructions choices (Task 40; spec 6.3, 6.7 gate, 6.8; plan V2, V13).

**Runs.** Task 38 writes one run per reference group as ``<project>/runs/<run_id>/config.json`` (``run_id`` =
``<group>-`` + 8 lower-case hex, the group being A, B or C), with the kit's out_dir in that folder; Task 39 writes
the view of a finished run to ``<project>/views/<run_id>.view.json`` (``python -m second_scribe.export_view``).
``list_runs`` and ``find_run`` are the only readers of that layout, so Tasks 38/39 adapt them in one place. A folder
counts as a run when its ``config.json`` names the same ``run_id``, its ``out_dir`` is that folder or a folder under
``runs/``, and any ``tool.reference_type`` equals the group of the id.

**Served files.** Only ``views/<run_id>.view.json`` and ``views/<run_id>.profile.json`` are read for the browser, for
a run id matching ``RUN_ID`` exactly, and never through a link (``_views_file``). Nothing here sends a model call.

**The profile** (plan V2) is the kit exporter's, ``profile_view.run_exporter(config, out_dir, profile, reference_type=
<the group's code, export.REFERENCE_NAMES>)``, written to ``views/<run_id>.profile.json`` and read back with ``load_profile``. It is made again when the
file is missing, unreadable or older than the run's log manifest. A refusal (``ProfileRefused``: fewer than 10 maps
analysed, the privacy scan, a chain that does not verify) is passed on with its reasons as the exporter wrote them.

**Instructions** (spec 6.8): ``project.data["instructions"] = {"rung1": "basic"|"own", "rung1_user": <tool.rung1_user
block> | None, "rungs": [picked of rung1, rung2, rung3; rung2 always], "preset": "standard"|"full"}``. Task 38 reads
``rung1_user`` (when ``rung1 == "own"`` and rung 1 is picked) and ``rungs``; ``run_rungs`` adds the placebo to rung 3,
as the kit's registry requires.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import ladder, toolcfg
from . import profile_view, strings
from .api import ApiError
from .project import Project, ProjectError

RUN_ID = re.compile(r"^[ABC]-[0-9a-f]{8}$")
GROUPS = ("A", "B", "C")
# The routes that serve results (routes/results.py registers them with coding.results_route).
RESULT_ROUTES = ("/api/view", "/api/results/profile")

PICKABLE_RUNGS = ("rung1", "rung2", "rung3")
RUN_ORDER = ("rung1", "rung2", "placebo", "rung3")         # the viewer's order: the placebo sits next to rung 3
RUNG1_MODES = ("basic", "own")
PRESETS = ("standard", "full")
DEFAULT_INSTRUCTIONS = {"rung1": "basic", "rung1_user": None, "rungs": ["rung2"], "preset": "standard"}
KIT_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def _err(status: int, code: str) -> ApiError:
    return ApiError(status, code, strings.t(f"results.error.{code}"))


# --- runs --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRef:
    run_id: str
    group: str
    folder: Path
    config_path: Path
    out_dir: Path

    @property
    def manifest(self) -> Path:
        return self.out_dir / "logs" / "run.manifest.json"

    @property
    def finished(self) -> bool:
        return self.manifest.is_file()


def check_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise _err(422, "bad_run")
    return run_id


def _runs_root(project: Project) -> Path:
    return project.path("runs").resolve()


def _read_run(project: Project, folder: Path) -> RunRef | None:
    """The run in ``folder`` (``runs/<run_id>``), or None when the folder is not a consistent run."""
    run_id = folder.name
    if not RUN_ID.fullmatch(run_id) or folder.is_symlink() or not folder.is_dir():
        return None
    cfg_path = folder / "config.json"
    if cfg_path.is_symlink() or not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get("run_id") != run_id:
        return None
    group = run_id[0]
    tool = cfg.get("tool")
    if isinstance(tool, dict) and tool.get("reference_type") not in (None, group):
        return None
    out = cfg.get("out_dir")
    if out is None:
        out_dir = folder
    elif isinstance(out, str) and out:
        out_dir = Path(out) if Path(out).is_absolute() else project.root / out
    else:
        return None
    try:
        out_dir = out_dir.resolve()
        root = _runs_root(project)
    except (OSError, RuntimeError, ProjectError):
        return None
    if out_dir != folder.resolve() and not out_dir.is_relative_to(root):
        return None
    if out_dir == root:
        return None
    return RunRef(run_id=run_id, group=group, folder=folder, config_path=cfg_path, out_dir=out_dir)


def list_runs(project: Project) -> list[RunRef]:
    """Every run of the project, by group then id (see the module docstring for what counts as a run)."""
    root = project.path("runs")
    if not root.is_dir():
        return []
    found = [_read_run(project, f) for f in sorted(root.iterdir())]
    return sorted((r for r in found if r is not None), key=lambda r: (r.group, r.run_id))


def find_run(project: Project, run_id: Any) -> RunRef:
    """The run with this id: 422 ``bad_run`` for an id that is not ``<group>-<8 hex>``, 404 ``unknown_run``."""
    run_id = check_run_id(run_id)
    ref = _read_run(project, project.path("runs") / run_id)
    if ref is None:
        raise _err(404, "unknown_run")
    return ref


def _views_file(project: Project, run_id: str, kind: str) -> Path:
    """``views/<run_id>.<kind>.json``; the path itself, never followed through a link."""
    return project.path("views") / f"{check_run_id(run_id)}.{kind}.json"


def _servable(project: Project, path: Path) -> bool:
    try:
        return (not path.is_symlink() and path.is_file()
                and path.resolve().parent == project.path("views").resolve())
    except (OSError, RuntimeError, ProjectError):
        return False


def has_view(project: Project, run_id: str) -> bool:
    return _servable(project, _views_file(project, run_id, "view"))


_MODEL_ROLES = ("recon", "judge")


def run_facts(ref: RunRef) -> dict:
    """What a row of the runs list says about a run before anything is opened (scout F10): when it started, the
    instructions it used, how many people and which models. Nothing AI-derived: the rungs and the people come from the
    run's ``config.json`` (``ladder.rungs``, ``tool.rung1_user``, ``corpus.pairs``); the start time, the model ids and
    (for a config without pairs) ``n_participants`` from the ``run_start`` records of its log; no other field is read. Missing
    facts are None: a run that never logged a start has no time or models."""
    try:
        cfg = json.loads(ref.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    ladder_cfg = cfg.get("ladder") if isinstance(cfg.get("ladder"), dict) else {}
    rungs = [r for r in RUN_ORDER if r in (ladder_cfg.get("rungs") or [])]
    tool = cfg.get("tool") if isinstance(cfg.get("tool"), dict) else {}
    corpus = cfg.get("corpus") if isinstance(cfg.get("corpus"), dict) else {}
    pairs = corpus.get("pairs")
    started, models, n_log = None, {}, None
    log = ref.out_dir / "logs" / "run.jsonl"
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"run_start"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("record_type") != "run_start":
                    continue
                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                if started is None and isinstance(rec.get("ts"), str):
                    started = rec["ts"]
                n = payload.get("n_participants")
                if n_log is None and isinstance(n, int) and not isinstance(n, bool):
                    n_log = n
                m = payload.get("models")
                for role in _MODEL_ROLES:
                    if isinstance(m, dict) and isinstance(m.get(role), str) and m[role] and role not in models:
                        models[role] = m[role][:200]
    except OSError:
        pass
    return {"started": started, "rungs": rungs, "rung1_own": isinstance(tool.get("rung1_user"), dict),
            "n_people": len(pairs) if isinstance(pairs, dict) else n_log, "models": models}


def runs_summary(project: Project, live: Any = None) -> list[dict]:
    """What the Results screen lists before anything is opened: ids, groups, whether a view is ready, the run's
    ``state`` and its facts (``run_facts``: start time, instructions, people, models). Nothing AI-derived. ``state`` is
    "finished" (the view is ready), "running" (``live``, the app's live run job, is working on this run: a stage, the
    export, a model switch) or "stopped" (it is neither: it failed, was paused or the app was restarted). The screen
    tells a stopped run apart from one still being made, and lists the newest run first."""
    out = []
    for r in list_runs(project):
        ready = has_view(project, r.run_id)
        active = live is not None and getattr(live, "run_id", None) == r.run_id and bool(getattr(live, "active", False))
        out.append({"run_id": r.run_id, "group": r.group, "finished": r.finished, "ready": ready,
                    "state": "finished" if ready else ("running" if active else "stopped"), **run_facts(r)})
    return out


def load_view(project: Project, run_id: Any) -> tuple[RunRef, dict]:
    """The run and its view-data file. 404 ``view_missing`` until Task 39 has written it; 500 ``view_unreadable``."""
    ref = find_run(project, run_id)
    path = _views_file(project, ref.run_id, "view")
    if not _servable(project, path):
        raise _err(404, "view_missing")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _err(500, "view_unreadable") from None
    if not isinstance(doc, dict):
        raise _err(500, "view_unreadable")
    return ref, doc


def _cached_profile(project: Project, ref: RunRef, path: Path) -> dict | None:
    if not _servable(project, path):
        return None
    try:
        if ref.finished and path.stat().st_mtime_ns < ref.manifest.stat().st_mtime_ns:
            return None
        return profile_view.load_profile(path)
    except (OSError, ValueError, profile_view.ProfileInvalid):
        return None


def profile_for(project: Project, run_id: Any) -> tuple[RunRef, dict]:
    """The run and its profile (module docstring). ``ProfileRefused`` passes through; any other failure of the
    exporter is 500 ``profile_failed`` (its details go to the terminal only)."""
    ref = find_run(project, run_id)
    path = _views_file(project, ref.run_id, "profile")
    doc = _cached_profile(project, ref, path)
    if doc is not None:
        return ref, doc
    if path.is_symlink():
        raise _err(500, "profile_failed")
    try:
        from .export import REFERENCE_NAMES       # the exporter takes a code (participant-endorsed...), not A/B/C
        profile_view.run_exporter(ref.config_path, ref.out_dir, path, reference_type=REFERENCE_NAMES.get(ref.group))
        doc = profile_view.load_profile(path)
    except profile_view.ProfileRefused:
        _drop(path)
        raise
    except (RuntimeError, OSError, ValueError, profile_view.ProfileInvalid) as e:
        _drop(path)
        print(f"profile for {ref.run_id} failed: {e.__class__.__name__}: {e}", flush=True)
        raise _err(500, "profile_failed") from None
    return ref, doc


# --- the exporter's refusals, in plain words (R12) ------------------------------------------------------------------
# The kit exporter writes its reasons as developer text ("n_analysed 4 is below the k floor of 10 people: ...", or since
# village E4 "this run analysed 4 people; a profile needs at least 10 (the k floor) ..."; both forms are read).
# The screens show one plain sentence for each kind of reason (strings ``results.refused.reason.<code>``); the
# exporter's own words go to the terminal log only. A reason this list does not know becomes "other", never raw text.
K_FLOOR_TEXT = re.compile(r"^n_analysed (?P<n>-?\d+) is below the k floor of (?P<k>\d+) people"
                          r"|^this run analysed (?P<n2>-?\d+) (?:person|people); a profile needs at least (?P<k2>\d+)"
                          r" \(the k floor\)")
REFUSAL_PATTERNS = (                                  # (code, regex on the reason as written), first match wins
    ("k_floor", re.compile(r"^n_analysed -?\d+ is below the k floor|^this run analysed -?\d+ (?:person|people); "
                           r"a profile needs at least|n_people below K")),
    ("k_floor_unknown", re.compile(r"^n_analysed is missing or not an integer"
                                   r"|^the number of people this run analysed is missing")),
    ("no_finished_run", re.compile(r"^no finished run found")),                              # village A12
    ("log_unreadable", re.compile(r"^the run log cannot be read")),
    ("log_unverified", re.compile(r"^the run log does not verify")),
    ("config_unreadable", re.compile(r"^the run config cannot be opened")),
    ("non_finite", re.compile(r"non-finite number")),
    ("other", re.compile(r"^(model info|--model-info|--reference-type|reference type)")),   # not reachable from the app
)
# Everything else the exporter can say comes from its privacy scan (the scan, the lab-string check, the value check):
# a key or string that could name someone, a number that is not a number, a cell that rests on the wrong people...
REFUSAL_CODES = ("k_floor", "k_floor_unknown", "no_finished_run", "log_unreadable", "log_unverified", "config_unreadable", "non_finite",
                 "privacy_check", "other")


def refusal_code(reason: Any) -> str:
    text = str(reason)
    for code, pat in REFUSAL_PATTERNS:
        if pat.search(text):
            return code
    return "privacy_check" if text.strip() else "other"


def refusal_messages(reasons: Any) -> list[dict]:
    """``[{"code", "message"}]``: one plain sentence for each kind of reason in ``reasons`` (the exporter's list), in
    the order each kind first appears. For the k floor the sentence that names the count wins over a bare one."""
    from ..profile_export import K_FLOOR
    found: dict[str, dict] = {}
    for reason in reasons if isinstance(reasons, (list, tuple)) else []:
        code = refusal_code(reason)
        vars_ = {"k": str(K_FLOOR)}
        hit = K_FLOOR_TEXT.match(str(reason))
        if code == "k_floor":
            code_key = "k_floor" if hit else "k_floor_bare"
            if hit:
                vars_ = {"n": hit.group("n") or hit.group("n2"), "k": hit.group("k") or hit.group("k2")}
        else:
            code_key = code
        if code in found and not (code == "k_floor" and hit and found[code]["bare"]):
            continue
        found[code] = {"code": code, "bare": code == "k_floor" and not hit,
                       "message": strings.t(f"results.refused.reason.{code_key}", **vars_)}
    return [{"code": v["code"], "message": v["message"]} for v in found.values()]


def log_refusal(run_id: str, reasons: Any) -> None:
    """The exporter's own words, to the terminal log only (the screens show ``refusal_messages``)."""
    for reason in reasons if isinstance(reasons, (list, tuple)) else []:
        print(f"profile for {run_id} refused: {reason}", flush=True)


def _drop(path: Path) -> None:
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        pass


# --- prompts (read-only) -----------------------------------------------------------------------------------------

def _prompt_file(name: str | None) -> dict | None:
    if name is None:
        return None
    data = (KIT_PROMPTS / name).read_bytes()
    return {"file": name, "text": data.decode("utf-8"), "sha256": hashlib.sha256(data).hexdigest()}


def prompts() -> dict:
    """The instruction texts as run, read from the kit's ``prompts/`` (never written) and the app's rung-1 wrapper."""
    rungs = []
    for r in ladder.RUNGS:
        main = _prompt_file(r.prompt_file)
        rungs.append({"rung": r.key, **main, "system": _prompt_file(r.system_file),
                      "definitions": _prompt_file(r.definitions_file), "requires": list(r.requires)})
    wrapper = toolcfg.RUNG1_WRAPPER_PATH.read_bytes()
    reading3 = (KIT_PROMPTS / toolcfg.READING3_PROMPT_FILE).read_bytes()
    return {"rungs": rungs,
            "wrapper": {"text": wrapper.decode("utf-8"), "sha256": hashlib.sha256(wrapper).hexdigest()},
            "reading3": {"file": toolcfg.READING3_PROMPT_FILE, "sha256": hashlib.sha256(reading3).hexdigest()}}


# --- instructions ------------------------------------------------------------------------------------------------

def _ierr(code: str) -> ApiError:
    return ApiError(422, code, strings.t(f"instructions.error.{code}"))


def instructions(project: Project) -> dict:
    saved = project.data.get("instructions")
    out = dict(DEFAULT_INSTRUCTIONS)
    if isinstance(saved, dict):
        out.update({k: saved[k] for k in DEFAULT_INSTRUCTIONS if k in saved})
    return out


def run_rungs(picked: list[str]) -> list[str]:
    """The rungs a run with this pick runs, in the viewer's order: rung 2 always, the placebo with rung 3."""
    keys = set(picked) | {"rung2"}
    if "rung3" in keys:
        keys |= set(ladder.get_rung("rung3").requires)
    return [k for k in RUN_ORDER if k in keys]


def set_instructions(project: Project, body: dict) -> dict:
    """Check and keep the Instructions choices (module docstring); 422 with a code on any bad field."""
    cur = instructions(project)
    mode = body.get("rung1", cur["rung1"])
    if mode not in RUNG1_MODES:
        raise _ierr("bad_rung1")
    rungs = body.get("rungs", cur["rungs"])
    if not isinstance(rungs, list) or not all(isinstance(r, str) and r in PICKABLE_RUNGS for r in rungs):
        raise _ierr("bad_rungs")
    preset = body.get("preset", cur["preset"])
    if preset not in PRESETS:
        raise _ierr("bad_preset")
    block = cur["rung1_user"]
    if "rung1_text" in body or (mode == "own" and block is None):
        text = body.get("rung1_text")
        if not isinstance(text, str) or not text.strip():
            raise _ierr("rung1_text_empty")
        if len(text) > toolcfg.RUNG1_MAX_CHARS:
            raise _ierr("rung1_text_long")
        block = toolcfg.rung1_user_block(text)
        if ladder.rung1_template_problems(block):
            raise _ierr("rung1_text_empty")
    picked = [r for r in PICKABLE_RUNGS if r in set(rungs) | {"rung2"}]
    out = {"rung1": mode, "rung1_user": block, "rungs": picked, "preset": preset}
    project.data["instructions"] = out
    project.save()
    return out
