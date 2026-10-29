"""One kit run config and one subprocess environment per reference group (Task 38; spec 3, 3.1 presets, 4, 6.5 step
6, 6.8, 11 "Endpoint guard"; plan V7).

**One run per reference group.** The participants of a group are the pids of the confirmed pairing whose reference
type (Task 36, ``coding.reference_type_of``) is that group. Types are never pooled: an A run, a B run and a C run
each get their own config, run folder and run id.

**Run folder.** ``runs/<run_id>/config.json``, where ``run_id`` = ``<group>-`` + 8 random lower-case hex
(``new_run_id``; undated, so the publish gate's dated-id rule never trips). The kit's ``out_dir`` is that folder
(Task 40's ``results._read_run`` reads exactly this layout). A config that already exists is a resumed run: it is
returned unchanged, never rewritten, and no second ``run_started`` is logged.

**Refusals** (``ApiError``; nothing is written or logged when one is raised), in this order:

- 422 ``bad_group`` / ``bad_rungs`` / ``bad_preset`` / ``bad_run_id`` (the request itself);
- 409 ``file_changed`` ("<name> changed since you imported it"; ``Project.changed_files``, the importer's message);
- 409 ``no_context``; 409 ``pairing_unconfirmed`` (the importer's);
- 409 ``id_map_missing``: ``private/pid_map.json`` missing, unreadable or with an empty ``map``;
- 409 ``group_empty``: no pid of the confirmed pairing has this reference type;
- 409 ``labels_unconfirmed`` (a pid of the group), ``map_unconfirmed`` (an A pid);
- group B only: 409 ``coding_unlocked`` while a B coding of the group is open (A and C may run meanwhile, spec
  6.7; the results gate stays ``coding.require_results``), ``coding_changed`` for a coding edited after its lock;
- 409 ``reference_missing`` (no ``reference/<pid>.mapgraph.json``), ``reference_changed`` (an A reference that
  is no longer the file the map confirmation wrote);
- the models: ``models.require_ready`` (its first problem: ``optin_missing``, ``declaration_missing``,
  ``remote_needs_https``, ``bad_url``, ``cloud_model``, ``connection_untested`` / ``connection_failed`` /
  ``thinking_on``, ``families_unconfirmed``; ``models.py`` owns these checks), then
  ``jobs.require_model_check`` (409 ``model_check_failed`` / ``model_check_missing``).

``key_missing`` needs the keys in memory, so ``run_env`` checks it (via ``models.problems(project, secrets)``).

**The config** names every input by absolute path: ``corpus = {kind: "real", pairs: {pid: [reference, transcript]},
roles, participant_labels, interviewer_labels, participant_id_map}`` (the kit's pair order is map first), the
Setup ``context`` (plus ``extract.phase_names``), the preset's ``specs`` / ``prior_only`` / ``reading3`` /
``settings`` / ``stats`` and ``ladder.draws`` (``presets/<name>.json``, inside the code hash), the ladder rungs
(``results.run_rungs``: rung 2 always, the placebo with rung 3), ``models`` = the env prefixes, the optional
``script_sources`` from Setup (``project.data["script_sources"]``; off unless set) and the ``tool`` block:
``app_log_head``, ``judge_rule``, ``model_families``, ``model_digests``, ``model_check``, ``rung1_user`` (rung 1
picked with the user's own prompt), ``remote_endpoints`` / ``endpoint_optins`` (when not empty, as
``models.tool_fields`` gives them), ``reference_type`` and ``reading3_prompt_sha256`` (V7). An AI-maps-only run
(one family) keeps only the reconstructor's family, digest and endpoint entries, as its model check did.
Then ``run_started{group, run_id, config_sha256}`` is appended.

**The env** (``run_env``) is ``jobs.child_env`` over ``models.role_env`` for each role the run uses: no stray
``SS_`` variable of the app's own shell, keys only in the environment (``SS_<P>_KEY_ENV`` naming the variable that
holds the key), never in a file.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .. import publish, toolcfg
from ..pipeline import PROMPTS_DIR, RunConfig
from . import coding, importer, jobs, models, results, strings
from .api import ApiError
from .project import Project, ProjectError, file_sha256

GROUPS = ("A", "B", "C")
PRESETS = ("standard", "full")
PRESETS_DIR = Path(__file__).resolve().parent / "presets"
PRESET_FIELDS = ("specs", "prior_only", "reading3", "settings", "stats")
MODEL_PREFIXES = {"recon": "RECON", "judge": "JUDGE", "judge2": None}       # the kit's env prefixes (cli.py)
RUNS_DIR = "runs"
ERROR_CODES = ("bad_group", "bad_rungs", "bad_preset", "bad_run_id", "no_context", "id_map_missing", "group_empty",
               "labels_unconfirmed", "map_unconfirmed", "coding_unlocked", "coding_changed", "reference_missing",
               "reference_changed")


def _err(status: int, code: str, **kw: Any) -> ApiError:
    return ApiError(status, code, strings.t(f"orchestrate.error.{code}", **kw))


def new_run_id(group: str) -> str:
    """``<group>-`` + 8 random lower-case hex characters, drawn again until the publish gate's dated-id pattern
    (``publish._DATED``) finds nothing in it: 8 hex characters are all decimal digits about 2% of the time, and
    ``20261019`` would read as a date."""
    if group not in GROUPS:
        raise _err(422, "bad_group")
    while True:
        rid = f"{group}-{_secrets.token_hex(4)}"
        if not publish._DATED.search(rid):
            return rid


def load_preset(name: str) -> dict:
    if name not in PRESETS:
        raise _err(422, "bad_preset")
    return json.loads((PRESETS_DIR / f"{name}.json").read_text(encoding="utf-8"))


def run_roles(project) -> tuple[str, ...]:
    """The model roles a run of the project uses: both for a two-family run, the reconstructor alone otherwise."""
    return ("recon", "judge") if models.pipeline_mode(project) == "two_family" else ("recon",)


# --- checks -------------------------------------------------------------------------------------------------------

def _check_request(group: Any, rungs: Any, preset: Any, run_id: Any) -> None:
    if group not in GROUPS:
        raise _err(422, "bad_group")
    if not isinstance(rungs, list) or not all(isinstance(r, str) and r in results.PICKABLE_RUNGS for r in rungs):
        raise _err(422, "bad_rungs")
    if preset not in PRESETS:
        raise _err(422, "bad_preset")
    if not isinstance(run_id, str) or not results.RUN_ID.fullmatch(run_id) or run_id[0] != group:
        raise _err(422, "bad_run_id")


def _check_files(project: Project) -> None:
    changed = project.changed_files()
    if changed:
        raise importer.refuse(409, "file_changed", name=importer.file_label(project, changed[0]))    # screen only


def _context(project: Project) -> dict:
    ctx = project.data.get("context") or {}
    domain, phases, roles = ctx.get("domain"), ctx.get("phases"), ctx.get("roles")
    if not (isinstance(domain, str) and domain.strip() and isinstance(phases, list) and phases
            and isinstance(roles, dict) and roles):
        raise _err(409, "no_context")
    return {"domain": domain, "roles": dict(roles), "phases": list(phases), "extract": {"phase_names": list(phases)}}


def _check_id_map(project: Project) -> Path:
    try:
        path = project.path(importer.PID_MAP)
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ProjectError):
        raise _err(409, "id_map_missing") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("map"), dict) or not doc["map"]:
        raise _err(409, "id_map_missing")
    return path


def group_pids(project: Project, group: str) -> list[str]:
    """The pids of the confirmed pairing whose reference type is ``group``, sorted."""
    return [pid for pid in importer.confirmed_pids(project) if coding.reference_type_of(project, pid) == group]


def _check_group(project: Project, group: str) -> list[str]:
    todo = importer.unconfirmed(project)
    if todo["pairing"]:
        raise importer.refuse(409, "pairing_unconfirmed")
    pids = group_pids(project, group)
    if not pids:
        raise _err(409, "group_empty", group=group)
    for pid in pids:
        if pid in todo["labels"]:
            raise _err(409, "labels_unconfirmed", pid=pid)
    if group == "A":
        for pid in pids:
            if pid in todo["maps"] or not importer.map_confirmed(project, pid):
                raise _err(409, "map_unconfirmed", pid=pid)
    if group == "B":
        for pid in pids:
            if not coding.is_locked(project, pid):
                raise _err(409, "coding_unlocked", pid=pid)
            doc = coding.load_coding(project, pid)        # also completes a lock a crash interrupted
            if coding.changed_after_lock(project, pid, doc):
                raise _err(409, "coding_changed", pid=pid)
    for pid in pids:
        ref = coding.reference_path(project, pid)
        if not ref.is_file():
            raise _err(409, "reference_missing", pid=pid)
        if group == "A":
            conf = ((project.data.get("maps") or {}).get(pid) or {}).get("confirmed") or {}
            if file_sha256(ref) != conf.get("reference_sha256"):
                raise _err(409, "reference_changed", pid=pid)
    return pids


def check_ready(project: Project, group: str, rungs: list, preset: str, run_id: str) -> list[str]:
    """Every refusal of ``build_run_config`` (module docstring), in order; returns the group's pids."""
    _check_request(group, rungs, preset, run_id)
    _check_files(project)
    _context(project)
    if importer.unconfirmed(project)["pairing"]:
        raise importer.refuse(409, "pairing_unconfirmed")
    _check_id_map(project)
    pids = _check_group(project, group)
    models.require_ready(project)
    jobs.require_model_check(project)
    return pids


# --- the config ---------------------------------------------------------------------------------------------------

def _corpus(project: Project, pids: list[str]) -> dict:
    pairing = project.data["pairing"]["pids"]
    labels = project.data["labels"]
    return {"kind": "real",
            "pairs": {pid: [str(coding.reference_path(project, pid).resolve()),
                            str(project.path(pairing[pid]["transcript"]).resolve())] for pid in pids},
            "roles": {pid: labels[pid]["role"] for pid in pids},
            "participant_labels": {pid: [labels[pid]["participant"]] for pid in pids},
            "interviewer_labels": {pid: [labels[pid]["interviewer"]] for pid in pids},
            "participant_id_map": str(project.path(importer.PID_MAP).resolve())}


def reading3_prompt_sha256() -> str:
    """sha256 of the no-interview prompt file the kit runs (V7; ``toolcfg.run_start_fields`` records the same)."""
    return hashlib.sha256((PROMPTS_DIR / toolcfg.READING3_PROMPT_FILE).read_bytes()).hexdigest()


def tool_block(project: Project, group: str, rungs: list[str]) -> dict:
    """The config's ``tool`` block (module docstring)."""
    n, head = project.log.head()
    roles = run_roles(project)
    fields = models.tool_fields(project)
    tool: dict[str, Any] = {
        "app_log_head": {"records": n, "head": head},
        "judge_rule": fields["judge_rule"],
        "model_families": {r: f for r, f in fields["model_families"].items() if r in roles},
        "model_digests": {r: d for r, d in fields["model_digests"].items() if r in roles},
        "model_check": jobs.require_model_check(project),
    }
    ins = results.instructions(project)
    if "rung1" in rungs and ins.get("rung1") == "own" and isinstance(ins.get("rung1_user"), dict):
        tool["rung1_user"] = ins["rung1_user"]
    for key in ("remote_endpoints", "endpoint_optins"):
        entries = [e for e in fields.get(key) or [] if e.get("role") in roles]
        if entries:
            tool[key] = entries
    tool["reference_type"] = group
    tool["reading3_prompt_sha256"] = reading3_prompt_sha256()
    return tool


def config_doc(project: Project, group: str, rungs: list[str], preset: str, run_id: str, pids: list[str]) -> dict:
    p = load_preset(preset)
    out_dir = project.path(f"{RUNS_DIR}/{run_id}").resolve()
    doc: dict[str, Any] = {"run_id": run_id, "corpus": _corpus(project, pids), "context": _context(project),
                           **{k: p[k] for k in PRESET_FIELDS},
                           "models": dict(MODEL_PREFIXES), "out_dir": str(out_dir),
                           "ladder": {"rungs": results.run_rungs(rungs), "draws": p["ladder"]["draws"]}}
    src = project.data.get("script_sources")
    if isinstance(src, list) and src:
        doc["script_sources"] = src
    doc["tool"] = tool_block(project, group, results.run_rungs(rungs))
    return doc


def _write_new(path: Path, text: str) -> None:
    """Write ``text`` to a new file atomically (temp file, fsync, rename)."""
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_run_config(project: Project, group: str, rungs: list[str], preset: str, run_id: str) -> Path:
    """Write ``runs/<run_id>/config.json`` for reference group ``group`` and log ``run_started``; returns the path.
    Refuses with ``ApiError`` (module docstring) before anything is written. An existing config (a resumed run) is
    returned unchanged."""
    pids = check_ready(project, group, rungs, preset, run_id)
    folder = project.path(f"{RUNS_DIR}/{run_id}")
    path = folder / "config.json"
    if path.is_file():
        return path
    doc = config_doc(project, group, rungs, preset, run_id, pids)
    # audit K5: every remote endpoint's declaration must be in the app log, below the head the tool block names
    models.require_declarations(project, doc["tool"])
    # the kit must accept it before anything is written: RunConfig checks the ladder, the tool block and the sources
    RunConfig(**{**doc, "base_dir": str(folder.resolve())})
    folder.mkdir(parents=True, exist_ok=True)
    _write_new(path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    sha = RunConfig.load(path).sha256()
    project.log.append("run_started", {"group": group, "run_id": run_id, "config_sha256": sha})
    return path


# --- the env ------------------------------------------------------------------------------------------------------

def run_env(project: Project, secrets, role_set: Iterable[str] | None = None) -> dict[str, str]:
    """The environment of a kit run subprocess: ``jobs.child_env`` over ``models.role_env`` of each role in
    ``role_set`` (default ``run_roles``: both roles for a two-family run). ``SS_<P>_URL/MODEL/PROVIDER/PARAMS``
    (thinking off on every address but a remote one, ``{}`` there; never ``temperature``) and, for a key,
    ``SS_<P>_KEY_ENV`` plus the variable it names. An AI-maps-only run writes no judge variable. ``ValueError`` for a
    role that is not ``recon`` or ``judge``; 409 ``key_missing`` (the models' message) for an API role without a key
    in memory. Pass the result to the subprocess only; never write it."""
    roles = tuple(role_set) if role_set is not None else run_roles(project)
    bad = [r for r in roles if r not in models.ROLES]
    if bad:
        raise ValueError(f"unknown model role {bad[0]!r}; roles are {', '.join(models.ROLES)}")
    used = run_roles(project)
    roles = tuple(r for r in models.ROLES if r in roles and r in used)
    if secrets is not None:
        for p in models.problems(project, secrets):
            if p["code"] == "key_missing" and p["role"] in roles:
                raise ApiError(409, "key_missing", strings.t("models.error.key_missing"))
    extra: dict[str, str] = {}
    for r in roles:
        extra.update(models.role_env(project, secrets, r, MODEL_PREFIXES[r]))
    return jobs.child_env(extra)
