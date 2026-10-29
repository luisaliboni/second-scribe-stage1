"""Import, speaker labels, missing rule and parsed-map confirmation (Task 33; spec section 6.5 steps 2-5, section 4
pairing, section 3.1 "Speaker labels (F5)", section 7; Review Focus 1-2; plan V12).

Every function takes the open ``Project`` and refuses with ``ApiError`` (code + a message from ``strings``); the
routes (``routes/importer.py``) only take the lock and call in. The data this module keeps in the project file
(``second-scribe-project.json``), for the later tasks:

- ``context``: ``{"domain": str, "phases": [str], "roles": {name: description}}`` (spec section 6.5 step 2).
- ``files[<rel>]``: ``{"kind": "transcript"|"map", "pid", "sha256", "bytes"}``; the copy lives at
  ``inputs/transcripts/<pid><suffix>`` or ``inputs/maps/<pid><suffix>`` (``.docx``, ``.vsdx``, ``.mapgraph.json``...).
  The file's name as imported is never stored here (privacy ruling F, 19 Oct): it goes to ``private/file_names.json``
  (``NAMES``), ``{"pids": {pid: {"stem", "transcript": <name>|None, "map": <name>|None}}}``, which the screens read
  to show the file as the user named it and which nothing logs or exports. Change detection uses ``sha256``.
- ``pairing``: ``{"table_sha256", "pids": {pid: {"transcript": <rel>, "map": <rel>|None}}}``, written when the
  user confirms the pairing table. Only pids with a transcript are listed; a map with no transcript is shown and
  left out. ``private/pid_map.json`` (``PID_MAP``) is written at the same moment.
- ``labels[pid]``: ``{"participant", "interviewer", "role", "format", "turns": {participant, interviewer, unknown},
  "transcript_sha256"}``: the confirmed speaker labels (the kit's ``participant_labels`` / ``interviewer_labels``
  for this pid) and the participant's role (the kit's ``roles[pid]``). The two labels are also copied to
  ``files[<transcript rel>]["labels"] = {"participant", "interviewer"}`` (read by T36's coding store).
- ``missing_rules[<suffix>]``: ``{"kind", "colors", "class_names", "inherit"}``, the shape of the kit's
  ``corpus.missing_rules`` (``readers.rule_for``); a suffix with no entry uses ``readers.DEFAULT_RULES``.
  ``colors_found`` reads the colours of the imported maps' boxes for the Setup page; it stores and logs nothing.
- ``maps[pid]``: ``{"node_phase": {node_id: phase}}`` (phases the user set) and, once confirmed,
  ``"confirmed": {"sha256", "rule", "node_phase", "phases", "counts", "reference", "reference_sha256"}``.
  The confirmation holds only while the map file, its rule, the user's phases and the Setup phases are all
  unchanged (``map_confirmed``). ``reference/<pid>.mapgraph.json`` is what the kit reads; the original stays in
  ``inputs/maps/``.

- ``private/label_standard.json`` (``STANDARD``, item 27): the speaker-label standard, ``{"schema", "participant",
  "interviewer", "rules": [{"contains", "role"}]}``. It pre-fills the Speaker labels table for every transcript,
  later imports included; nothing is confirmed by it. It sits in ``private/`` because a rule holds part of a file
  name, and it is not logged (each confirmation it leads to is, as ``labels_confirmed``).

App log records (``audit.applog.APP_RECORD_TYPES``): ``context_set``, ``import``, ``removed``, ``labels_confirmed``,
``missing_rule``, ``map_confirmed``. They name pids and hashes but never a file name, and never quote a transcript, a
label or a map. ``import`` is ``{"kind", "pid", "sha256", "bytes", "replaced": <rel of the copy replaced>|None}``.
``removed`` (item 26) is ``{"kind", "pid", "sha256", "bytes", "reset": {"pairing", "labels", "map"}}``: the hash
recorded at import of the copy deleted, and which confirmations the removal cleared. History is never edited.
The pairing and the user's phase choices have no record type of their own: pids follow from the logged imports, and
``map_confirmed`` carries the hash of the phases each box got (``phases_sha256``) and how many the user set.

Participant ids (``assign_pid``): a file stem written as a code (capital letters and digits, at least one digit,
with ``-`` or ``_`` between them, such as ``CASE-01``) is the id as written; any other stem, which may hold a person's name, gets the next
free neutral id ``ID-01``, ``ID-02``... A transcript and its map share a stem, so they share the id.

Privacy: transcripts and maps stay in the project folder. The browser gets file names (from ``private/``, to show),
pids, label tokens with their counts, and the parsed map's boxes and arrows (the preview the user confirms); never a
turn's words, never the pid map. A refusal may name a file on screen; refusals are not logged.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .. import transcripts as tx
from ..maps import readers
from ..maps.readers import DEFAULT_RULES, MAP_SUFFIXES, REFUSED_SUFFIXES, MapRefused, MissingRule
from ..schemas import MapGraph
from . import strings
from .api import ApiError
from .project import ProjectError, file_sha256

MAX_TRANSCRIPT_BYTES = tx.MAX_TRANSCRIPT_BYTES        # 20 MB
MAX_MAP_BYTES = readers.MAX_MAP_BYTES                  # 50 MB
PID_MAP = "private/pid_map.json"
NAMES = "private/file_names.json"
NAMES_SCHEMA = "file-names/1"
STANDARD = "private/label_standard.json"
STANDARD_SCHEMA = "label-standard/1"
MAX_RULE = 40
NEUTRAL_PREFIX = "ID-"
EXAMPLE_URL = "/static/examples/labelled-transcript.txt"
KINDS = ("transcript", "map")
FOLDERS = {"transcript": "inputs/transcripts", "map": "inputs/maps"}
# Missing-rule kinds the user may pick per format (spec section 7; default first). .mapgraph.json carries statuses.
RULE_OPTIONS: dict[str, tuple[str, ...]] = {
    ".vsdx": ("red_line_or_text", "red_fill", "dashed"),
    ".drawio": ("red_line_or_text", "red_fill", "dashed"),
    ".excalidraw": ("red_line_or_text", "red_fill", "dashed"),
    ".mmd": ("mermaid_class", "classdef_style"),
    ".csv": ("status_column",),
    ".xlsx": ("status_column",),
}
INHERIT_SUFFIXES = (".vsdx",)        # V12: "red, including red set by a master shape or style"
# A label followed by a number and a bracketed time, as in "Participant 3 (00:04:12):" (Review Focus 1). The kit's
# _TIMESTAMP removes only a leading "0:05", so this part stays at the start of the turn's text.
TS_PREFIX = re.compile(r"^\d+\s*\(\d{1,2}:\d{2}(:\d{2})?\)\s*:?")
MAX_NAME = 200
MAX_LABEL = 40
MAX_LINE = 300
MAX_PHASES = 12
MAX_ROLES = 20
MAX_COLORS = 8
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COLOR = re.compile(r"^#[0-9a-f]{6}$")
_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,39}$")
_PID_BAD = re.compile(r"[^A-Za-z0-9_-]")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Z0-9]+(?:[-_][A-Z0-9]+)*$")


def refuse(status: int, code: str, **kw: Any) -> ApiError:
    """``ApiError`` with the message ``importer.error.<code>`` (slots from ``kw``)."""
    return ApiError(status, code, strings.t(f"importer.error.{code}", **kw))


def _one_line(v: Any, limit: int) -> bool:
    return isinstance(v, str) and bool(v.strip()) and len(v) <= limit and not _CONTROL.search(v)


def _canon_sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _store(project) -> dict:
    """The project's data, with this module's keys present."""
    d = project.data
    for key, empty in (("pairing", {}), ("labels", {}), ("missing_rules", {}), ("maps", {})):
        if not isinstance(d.get(key), dict):
            d[key] = empty
    return d


# --- context ----------------------------------------------------------------------------------------------------

def check_context(body: dict) -> dict:
    """The study context from ``POST /api/context``; 422 ``bad_context`` unless: a one-line domain, 1-12 distinct
    one-line phases, 1-20 roles each with a one-line description."""
    domain, phases, roles = body.get("domain"), body.get("phases"), body.get("roles")
    bad = refuse(422, "bad_context")
    if not _one_line(domain, MAX_LINE):
        raise bad
    if not isinstance(phases, list) or not 1 <= len(phases) <= MAX_PHASES:
        raise bad
    if not all(_one_line(p, 60) for p in phases):
        raise bad
    phases = [p.strip() for p in phases]
    if len({p.casefold() for p in phases}) != len(phases):
        raise bad
    if not isinstance(roles, dict) or not 1 <= len(roles) <= MAX_ROLES:
        raise bad
    out_roles = {}
    for name, desc in roles.items():
        if not _one_line(name, 60) or not _one_line(desc, MAX_LINE):
            raise bad
        out_roles[name.strip()] = desc.strip()
    if len({r.casefold() for r in out_roles}) != len(out_roles):
        raise bad
    return {"domain": domain.strip(), "phases": phases, "roles": out_roles}


def set_context(project, body: dict) -> dict:
    ctx = check_context(body)
    project.log.append("context_set", {"sha256": _canon_sha(ctx), "n_phases": len(ctx["phases"]),
                                       "n_roles": len(ctx["roles"])})
    project.data["context"] = ctx
    project.save()
    return ctx


def setup_phases(project) -> list[str]:
    ph = (project.data.get("context") or {}).get("phases")
    return list(ph) if isinstance(ph, list) else []


def setup_roles(project) -> dict:
    r = (project.data.get("context") or {}).get("roles")
    return dict(r) if isinstance(r, dict) else {}


# --- import -----------------------------------------------------------------------------------------------------

def pid_from_stem(stem: str) -> str:
    """A participant id from a file stem: ``[A-Za-z0-9_-]`` kept, anything else ``_``, runs of ``_`` folded into
    one (the kit splits item names on ``__``), at most 64 characters."""
    pid = re.sub(r"_{2,}", "_", _PID_BAD.sub("_", stem))[:64]
    return pid or "_"


def file_stem(name: str, kind: str) -> str:
    """The pairing stem: the name without its format suffix (``.mapgraph.json`` counts as one)."""
    if kind == "map":
        suffix = readers.map_suffix(name)
        return name[: len(name) - len(suffix)] if suffix and name.lower().endswith(suffix) else Path(name).stem
    return Path(name).stem


def is_code(stem: str) -> bool:
    """True for a stem written as a code (``CASE-01``, ``MAP-A1``, ``SYN_OW03``): capital letters and digits with at
    least one digit, ``-`` or ``_`` between them, at most 64 characters (analysis ruling: ``SILVA`` is not a code). Such a stem is the participant id as written; any other stem may hold
    a person's name and gets a neutral id."""
    return len(stem) <= 64 and bool(_CODE.match(stem)) and "__" not in stem and any(c.isdigit() for c in stem)


# --- the file names, kept only in private/ ----------------------------------------------------------------------

def load_names(project) -> dict:
    """``private/file_names.json`` as ``{"schema", "pids": {pid: {"stem", "transcript", "map"}}}`` (empty when
    missing or unreadable)."""
    try:
        doc = json.loads(project.path(NAMES).read_text(encoding="utf-8"))
    except (OSError, ValueError, ProjectError):
        doc = None
    pids = doc.get("pids") if isinstance(doc, dict) else None
    out = {}
    for pid, e in (pids or {}).items():
        if isinstance(pid, str) and isinstance(e, dict):
            out[pid] = {"stem": e.get("stem") if isinstance(e.get("stem"), str) else None,
                        **{k: e.get(k) if isinstance(e.get(k), str) else None for k in KINDS}}
    return {"schema": NAMES_SCHEMA, "pids": out}


def _save_names(project, doc: dict) -> None:
    target = project.path(NAMES)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def original_names(project) -> dict:
    """``{pid: {"stem", "transcript", "map"}}``: each participant's files as the user named them (from ``private/``;
    for the screens and for the Profile's in-memory forbidden strings only)."""
    return load_names(project)["pids"]


def file_label(project, rel: str) -> str:
    """The name to show for the stored copy ``rel``: the file's name as imported, else the copy's own name."""
    meta = project.data["files"].get(rel) or {}
    entry = original_names(project).get(meta.get("pid")) or {}
    name = entry.get(meta.get("kind")) if meta.get("kind") in KINDS else None
    return name if isinstance(name, str) and name else rel.rsplit("/", 1)[-1]


def _pids_taken(project, names: dict) -> dict:
    """``{casefolded pid: pid}`` of every pid in use (the names store and the stored files)."""
    taken = {p.casefold(): p for p in names["pids"]}
    for m in _imported(project).values():
        if isinstance(m.get("pid"), str):
            taken.setdefault(m["pid"].casefold(), m["pid"])
    return taken


def assign_pid(project, names: dict, stem: str, name: str) -> str:
    """The participant id of a file with this stem: the one the stem already has; else the stem itself when it is a
    code (``is_code``; 409 ``pid_collision`` when that id, in any case, is another stem's); else the next free
    ``ID-<nn>``."""
    for pid, e in names["pids"].items():
        if e.get("stem") == stem:
            return pid
    taken = _pids_taken(project, names)
    if is_code(stem):
        other = taken.get(stem.casefold())
        if other is not None:
            e = names["pids"].get(other) or {}
            raise refuse(409, "pid_collision", name=name, other=e.get("transcript") or e.get("map") or other)
        return stem
    i = 1
    while f"{NEUTRAL_PREFIX}{i:02d}".casefold() in taken:
        i += 1
    return f"{NEUTRAL_PREFIX}{i:02d}"


def _check_name(name: Any) -> str:
    if not _one_line(name, MAX_NAME) or name != name.strip() or name.startswith(".") or "/" in name \
            or "\\" in name or ":" in name or name in (".", ".."):
        raise refuse(422, "bad_file_name")
    return name


def _suffix(name: str, kind: str) -> str:
    """The format suffix, refused (422) when it is an image or PDF or not a format of this kind."""
    plain = Path(name).suffix.lower()
    if plain in REFUSED_SUFFIXES:
        raise refuse(422, "refused_format")
    if kind == "transcript":
        if plain not in tx.TRANSCRIPT_SUFFIXES:
            raise refuse(422, "unsupported_format", kind=strings.t("importer.kind.transcript"),
                         formats=", ".join(tx.TRANSCRIPT_SUFFIXES))
        return plain
    suffix = readers.map_suffix(name)
    if suffix not in MAP_SUFFIXES or file_stem(name, "map") in ("", "."):
        raise refuse(422, "unsupported_format", kind=strings.t("importer.kind.map"), formats=", ".join(MAP_SUFFIXES))
    return suffix


def _decode(data_b64: Any, limit: int) -> bytes:
    if not isinstance(data_b64, str):
        raise refuse(422, "bad_data")
    if (len(data_b64) // 4) * 3 > limit + 3:
        raise refuse(413, "too_large", mb=str(limit // (1024 * 1024) or 1))
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise refuse(422, "bad_data") from None
    if len(data) > limit:
        raise refuse(413, "too_large", mb=str(limit // (1024 * 1024) or 1))
    if not data:
        raise refuse(422, "empty_file")
    return data


def _check_readable(path: Path, kind: str, pid: str, project) -> None:
    """Read the staged copy as the kit will; 422 when it cannot be read (the reason never quotes the file)."""
    if kind == "transcript":
        try:
            tx.read_transcript_paras(path)
        except Exception:  # noqa: BLE001 - a broken .docx raises zip/XML errors of many kinds
            raise refuse(422, "unreadable_transcript") from None
        return
    try:
        readers.read_map(path, pid, None, rule_of(project, readers.map_suffix(path)))
    except MapRefused as e:
        raise refuse(422, "map_refused", detail=e.detail) from None
    except Exception:  # noqa: BLE001
        raise refuse(422, "map_refused", detail=strings.t("importer.error.map_unreadable_detail")) from None


def _imported(project, kind: str | None = None) -> dict:
    """``{rel: meta}`` of the imported files (of ``kind``)."""
    return {rel: m for rel, m in project.data["files"].items()
            if m.get("kind") in KINDS and (kind is None or m.get("kind") == kind)}


def identical_transcripts(project, pid: str | None = None) -> dict[str, list[str]]:
    """``{pid: [names of the other transcripts with the same bytes]}`` from the sha256 recorded at import. Only pids
    that share content with another transcript appear (with ``pid``: only that one). A warning, nothing more: no
    file is excluded and nothing stored changes. The names are the files' names as imported (``file_label``)."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for rel, meta in sorted(_imported(project, "transcript").items()):
        sha, p = meta.get("sha256"), meta.get("pid")
        if isinstance(sha, str) and sha and isinstance(p, str):
            groups.setdefault(sha, []).append((p, file_label(project, rel)))
    out: dict[str, list[str]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        for p, _label in members:
            if pid is None or p == pid:
                out[p] = [lab for q, lab in members if q != p]
    return out


def import_file(project, kind: Any, name: Any, data_b64: Any) -> dict:
    """``POST /api/import``: check, stage, read, then copy into ``inputs/…``, hash and log ``import``. A file with
    the same stem of the same kind is replaced (a re-import after a change, or another format)."""
    if kind not in KINDS:
        raise refuse(422, "bad_kind")
    name = _check_name(name)
    suffix = _suffix(name, kind)
    data = _decode(data_b64, MAX_TRANSCRIPT_BYTES if kind == "transcript" else MAX_MAP_BYTES)
    stem = file_stem(name, kind)
    names = load_names(project)
    pid = assign_pid(project, names, stem, name)
    replaced = next((rel for rel, meta in _imported(project, kind).items() if meta.get("pid") == pid), None)
    folder = project.path(FOLDERS[kind])
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / f".import-{uuid.uuid4().hex}{suffix}"         # never the file's name: a crash may leave it
    rel = f"{FOLDERS[kind]}/{pid}{suffix}"
    try:
        _write_atomic(staged, data)
        _check_readable(staged, kind, pid, project)
        target = project.path(rel)
        os.replace(staged, target)
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        raise
    entry = names["pids"].setdefault(pid, {"stem": stem, "transcript": None, "map": None})
    old_name = entry.get(kind) if replaced is not None else None
    entry[kind] = name
    _save_names(project, names)
    if replaced is not None:
        if replaced != rel:
            try:
                project.path(replaced).unlink()
            except (OSError, ProjectError):
                pass
        del project.data["files"][replaced]
    entry = project.record_file(rel, kind=kind, pid=pid)
    project.log.append("import", {"kind": kind, "pid": pid, "sha256": entry["sha256"], "bytes": entry["bytes"],
                                  "replaced": replaced})
    project.save()
    reply = {"file": {"kind": kind, "name": name, "rel": rel, "pid": pid, "sha256": entry["sha256"],
                      "bytes": entry["bytes"]}, "replaced": old_name}
    if kind == "transcript":
        same = identical_transcripts(project, pid).get(pid)
        if same:
            reply["identical_to"] = same          # a warning for the screen; the file is stored as any other
    return reply


# --- pairing ----------------------------------------------------------------------------------------------------

def _table(project) -> list[dict]:
    """One row per pid; ``stem``, ``transcript`` and ``map`` are the names as imported (from ``private/``)."""
    rows: dict[str, dict] = {}
    names = original_names(project)
    for rel, meta in sorted(_imported(project).items()):
        pid = meta.get("pid")
        if not isinstance(pid, str):
            continue
        row = rows.setdefault(pid, {"pid": pid, "stem": (names.get(pid) or {}).get("stem") or pid,
                                    "transcript": None, "map": None, "transcript_rel": None, "map_rel": None})
        row[meta["kind"]] = file_label(project, rel)
        row[f"{meta['kind']}_rel"] = rel
    return [rows[p] for p in sorted(rows)]


def _table_sha(rows: list[dict]) -> str:
    return _canon_sha([[r["pid"], r["transcript_rel"], r["map_rel"]] for r in rows])


def pairing_current(project) -> bool:
    """True when the pairing table was confirmed and has not changed since."""
    pairing = _store(project)["pairing"]
    return bool(pairing.get("table_sha256")) and pairing.get("table_sha256") == _table_sha(_table(project))


def pairing(project) -> dict:
    """``GET /api/pairing``: one row per pid with the file names, whether it is paired, and the reference types it
    may take (a transcript without a map: B or C only; a map without a transcript: none). ``identical_to`` lists the
    other transcripts with the very same bytes (a warning only; ``table_sha256`` and the stored pairing ignore it)."""
    rows = _table(project)
    same = identical_transcripts(project)
    out = []
    for r in rows:
        paired = bool(r["transcript"] and r["map"])
        opts = ["A", "B", "C"] if paired else (["B", "C"] if r["transcript"] else [])
        out.append({"pid": r["pid"], "transcript": r["transcript"], "map": r["map"], "paired": paired,
                    "reference_options": opts, "identical_to": same.get(r["pid"], [])})
    return {"rows": out, "table_sha256": _table_sha(rows), "confirmed": pairing_current(project)}


def confirm_pairing(project, table_sha256: Any) -> dict:
    """``POST /api/pairing/confirm``: fixes the pids and writes ``private/pid_map.json`` (no log record: the pids
    follow from the logged imports)."""
    rows = _table(project)
    sha = _table_sha(rows)
    if table_sha256 != sha:
        raise refuse(409, "stale_pairing")
    people = [r for r in rows if r["transcript"]]
    if not people:
        raise refuse(409, "no_transcripts")
    pid_map = {"map": {r["pid"]: {"file_stem": r["stem"], "file_name": r["transcript"]} for r in people}}
    target = project.path(PID_MAP)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, (json.dumps(pid_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    _store(project)["pairing"] = {"table_sha256": sha, "pids": {r["pid"]: {"transcript": r["transcript_rel"],
                                                                           "map": r["map_rel"]} for r in people}}
    project.save()
    return pairing(project)


def confirmed_pids(project) -> list[str]:
    """The pids of the confirmed pairing (each has a transcript), or [] while the pairing needs confirming."""
    if not pairing_current(project):
        return []
    return sorted(_store(project)["pairing"].get("pids") or {})


def _row(project, pid: Any) -> dict:
    if not isinstance(pid, str) or not pid:
        raise refuse(404, "unknown_pid")
    for r in _table(project):
        if r["pid"] == pid:
            return r
    raise refuse(404, "unknown_pid")


def _require_pairing(project, pid: str) -> None:
    if not pairing_current(project) or pid not in (_store(project)["pairing"].get("pids") or {}):
        raise refuse(409, "pairing_unconfirmed")


def _require_unchanged(project, rel: str) -> str:
    """The file's sha256 now; 409 ``file_changed`` when it differs from the sha256 recorded at import."""
    meta = project.data["files"].get(rel) or {}
    try:
        now = file_sha256(project.path(rel))
    except (OSError, ProjectError):
        now = None
    if now is None or now != meta.get("sha256"):
        raise refuse(409, "file_changed", name=file_label(project, rel))
    return now


# --- speaker labels ---------------------------------------------------------------------------------------------

def _paras(project, rel: str) -> list[str]:
    try:
        return tx.read_transcript_paras(project.path(rel))
    except Exception:  # noqa: BLE001
        raise refuse(422, "unreadable_transcript") from None


def _rest_after_label(para: str, label: str) -> str | None:
    """The text after ``label`` when the paragraph opens with it (bracket or first token), else None."""
    p = para.strip()
    m = tx._BRACKET.match(p)
    if m:
        return p[m.end():] if tx._norm_label(m.group(1)) == label else None
    parts = p.split(None, 1)
    if not parts or re.sub(r"[\W_]+", "", parts[0], flags=re.UNICODE) != label:
        return None
    return parts[1] if len(parts) > 1 else ""


def label_preview(project, pid: Any) -> dict:
    """``GET /api/labels?pid=``: the detected format and the label candidates with their turn counts. A label
    followed by ``3 (00:04:12):`` shows that part as part of the label and warns that it stays in the turn text."""
    row = _row(project, pid)
    if not row["transcript_rel"]:
        raise refuse(404, "no_transcript")
    paras = _paras(project, row["transcript_rel"])
    fmt = tx.parse_paragraphs(paras, set(), row["pid"]).speaker_format
    candidates, warnings = [], []
    for label, n in tx.label_candidates(paras):
        shown, prefix = None, None
        for p in paras:
            rest = _rest_after_label(p, label)
            if rest is None:
                continue
            m = TS_PREFIX.match(rest.lstrip())
            if m:
                prefix = m.group(0).strip()
            head = p.strip()[: len(p.strip()) - len(rest)].rstrip()
            shown = f"{head} {prefix}" if prefix else head
            break
        candidates.append({"label": label, "turns": n, "shown": shown, "prefix": prefix})
        if prefix:
            warnings.append({"code": "timestamp_prefix", "label": label, "prefix": prefix,
                             "message": strings.t("importer.warning.timestamp_prefix", label=label, prefix=prefix)})
    return {"pid": row["pid"], "format": fmt, "candidates": candidates, "warnings": warnings,
            "example_url": EXAMPLE_URL, "confirmed": _labels_view(project, row["pid"])}


def _labels_view(project, pid: str) -> dict | None:
    entry = _store(project)["labels"].get(pid)
    if not entry or not labels_confirmed(project, pid):
        return None
    return {k: entry[k] for k in ("participant", "interviewer", "role", "format", "turns")}


def confirm_labels(project, body: dict) -> dict:
    """``POST /api/labels/confirm {pid, participant, interviewer, role}``: re-parse with the confirmed labels as the
    kit will (``parse_paragraphs`` with both label sets); refuse 422 ``no_speaker_labels`` when the format is still
    ``none`` or either speaker has no turn; log ``labels_confirmed`` (counts only, never the labels)."""
    row = _row(project, body.get("pid"))
    pid = row["pid"]
    if not row["transcript_rel"]:
        raise refuse(404, "no_transcript")
    _require_pairing(project, pid)
    part, iv, role = body.get("participant"), body.get("interviewer"), body.get("role")
    if not _one_line(part, MAX_LABEL) or not _one_line(iv, MAX_LABEL):
        raise refuse(422, "bad_label")
    part, iv = part.strip(), iv.strip()
    if not tx._norm_label(part) or not tx._norm_label(iv):
        raise refuse(422, "bad_label")
    if tx._norm_label(part) == tx._norm_label(iv):
        raise refuse(422, "same_label")
    if not isinstance(role, str) or role not in setup_roles(project):
        raise refuse(422, "bad_role")
    sha = _require_unchanged(project, row["transcript_rel"])
    t = tx.parse_paragraphs(_paras(project, row["transcript_rel"]), {part}, pid, interviewer_labels={iv})
    turns = {s: sum(1 for x in t.turns if x.speaker == s) for s in ("participant", "interviewer", "unknown")}
    if t.speaker_format == "none" or not turns["participant"] or not turns["interviewer"]:
        raise refuse(422, "no_speaker_labels", example=EXAMPLE_URL)
    if t.speaker_inference != "label":
        raise refuse(422, "label_not_found")
    project.log.append("labels_confirmed", {"pid": pid, "format": t.speaker_format, "turns": turns,
                                            "transcript_sha256": sha})
    _store(project)["labels"][pid] = {"participant": part, "interviewer": iv, "role": role,
                                      "format": t.speaker_format, "turns": turns, "transcript_sha256": sha}
    # T36 (coding.transcript_source) reads the labels beside the file; a re-import replaces this entry.
    project.data["files"][row["transcript_rel"]]["labels"] = {"participant": part, "interviewer": iv}
    project.save()
    return {"pid": pid, "format": t.speaker_format, "turns": turns, "role": role}


def labels_confirmed(project, pid: str) -> bool:
    """The labels hold while the transcript is the one confirmed and the role is still a Setup role."""
    entry = _store(project)["labels"].get(pid)
    rel = ((_store(project)["pairing"].get("pids") or {}).get(pid) or {}).get("transcript")
    if not entry or not rel:
        return False
    meta = project.data["files"].get(rel) or {}
    return meta.get("sha256") == entry.get("transcript_sha256") and entry.get("role") in setup_roles(project)


# --- remove an imported file (item 26) ---------------------------------------------------------------------------

def remove_file(project, kind: Any, pid: Any) -> dict:
    """``POST /api/import/remove {kind, pid}``: delete the project's copy of that participant's transcript or map, drop
    it from ``files``, clear what depended on it, and log ``removed``. The browser never sends a path: the copy is the
    one ``files`` registers for this kind and pid, and it must sit directly in ``inputs/transcripts`` or
    ``inputs/maps`` (422 ``bad_remove`` otherwise; 404 ``not_imported`` when nothing is registered). Cleared: the
    pairing confirmation (always; the table changed), the speaker labels of the pid (a transcript), the map
    confirmation and phase choices of the pid (a map). The file name as imported is dropped from ``private/``; the pid
    stays reserved to its stem, so importing the file again gives the same id. The copy is moved aside before the log
    record is written and put back if the log refuses, so a refusal changes nothing. Routes refuse while a run is in
    progress (``routes.importer``)."""
    if kind not in KINDS:
        raise refuse(422, "bad_kind")
    if not isinstance(pid, str) or not pid:
        raise refuse(404, "not_imported")
    hits = sorted(rel for rel, meta in _imported(project, kind).items() if meta.get("pid") == pid)
    if not hits:
        raise refuse(404, "not_imported")
    rel = hits[0]
    meta = project.data["files"][rel]
    folder = FOLDERS[kind]
    leaf = rel[len(folder) + 1:] if rel.startswith(folder + "/") else ""
    if not leaf or "/" in leaf or leaf.startswith("."):
        raise refuse(422, "bad_remove")
    try:
        target = project.path(rel)
        base = project.path(folder).resolve()
    except ProjectError:
        raise refuse(422, "bad_remove") from None
    if target.parent.resolve() != base:
        raise refuse(422, "bad_remove")
    if target.exists() and not target.is_file():
        raise refuse(422, "bad_remove")
    store = _store(project)
    reset = {"pairing": bool(store["pairing"]),
             "labels": kind == "transcript" and pid in store["labels"],
             "map": kind == "map" and pid in store["maps"]}
    aside = None
    if target.is_file():
        aside = target.parent / f".remove-{uuid.uuid4().hex}"
        os.replace(target, aside)
    try:
        project.log.append("removed", {"kind": kind, "pid": pid, "sha256": meta.get("sha256"),
                                       "bytes": meta.get("bytes"), "reset": reset})
    except BaseException:
        if aside is not None:
            os.replace(aside, target)
        raise
    if aside is not None:
        try:
            aside.unlink()
        except OSError:
            pass
    shown = file_label(project, rel)
    del project.data["files"][rel]
    names = load_names(project)
    entry = names["pids"].get(pid)
    if entry is not None:
        entry[kind] = None
        _save_names(project, names)
    store["pairing"] = {}
    if kind == "transcript":
        store["labels"].pop(pid, None)
    else:
        store["maps"].pop(pid, None)
    project.save()
    return {"removed": {"kind": kind, "pid": pid, "name": shown}, "reset": reset}


# --- the speaker-label standard (item 27) -----------------------------------------------------------------------

def load_standard(project) -> dict | None:
    """``private/label_standard.json`` as ``{"participant", "interviewer", "rules"}``, or None when there is none."""
    try:
        doc = json.loads(project.path(STANDARD).read_text(encoding="utf-8"))
    except (OSError, ValueError, ProjectError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("participant"), str) \
            or not isinstance(doc.get("interviewer"), str) or not isinstance(doc.get("rules"), list):
        return None
    rules = [{"contains": r["contains"], "role": r["role"]} for r in doc["rules"]
             if isinstance(r, dict) and isinstance(r.get("contains"), str) and isinstance(r.get("role"), str)]
    return {"participant": doc["participant"], "interviewer": doc["interviewer"], "rules": rules}


def check_standard(project, body: dict) -> dict:
    """The standard from ``POST /api/labels/standard``; 422 ``bad_standard`` unless: two one-line labels that differ
    once punctuation and case are dropped, and 0-20 rules, each a one-line piece of a file name and a Setup role."""
    bad = refuse(422, "bad_standard")
    part, iv, rules = body.get("participant"), body.get("interviewer"), body.get("rules")
    if not _one_line(part, MAX_LABEL) or not _one_line(iv, MAX_LABEL):
        raise bad
    part, iv = part.strip(), iv.strip()
    if not tx._norm_label(part) or not tx._norm_label(iv) or tx._norm_label(part) == tx._norm_label(iv):
        raise bad
    if not isinstance(rules, list) or len(rules) > MAX_ROLES:
        raise bad
    roles = setup_roles(project)
    out = []
    for r in rules:
        if not isinstance(r, dict) or not _one_line(r.get("contains"), MAX_RULE) or r.get("role") not in roles:
            raise bad
        out.append({"contains": r["contains"].strip(), "role": r["role"]})
    return {"participant": part, "interviewer": iv, "rules": out}


def set_standard(project, body: dict) -> dict:
    """``POST /api/labels/standard``: save the standard in ``private/`` and answer as ``standard_view``."""
    std = check_standard(project, body)
    target = project.path(STANDARD)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, (json.dumps({"schema": STANDARD_SCHEMA, **std}, ensure_ascii=False, indent=2,
                                      sort_keys=True) + "\n").encode("utf-8"))
    return standard_view(project)


def roles_for_name(std: dict, name: str, roles: dict) -> list[str]:
    """The Setup roles whose rule's piece appears in ``name`` (case ignored), sorted."""
    low = name.casefold()
    return sorted({r["role"] for r in std["rules"] if r["role"] in roles and r["contains"].casefold() in low})


def propose(project, std: dict, row: dict, roles: dict) -> dict:
    """What the standard fills for one transcript: the detected label that matches each standard label (as the
    server compares labels), the role whose rule matches the file name, the turn counts the confirmation would log,
    and ``problems`` (codes; a row with any is left for the person to choose): ``participant_missing``,
    ``interviewer_missing``, ``not_told_apart``, ``no_rule``, ``many_rules``, ``unreadable``."""
    out: dict[str, Any] = {"pid": row["pid"], "participant": None, "interviewer": None, "role": None,
                           "turns": None, "problems": []}
    try:
        paras = _paras(project, row["transcript_rel"])
    except ApiError:
        out["problems"].append("unreadable")
        return out
    cands = [lab for lab, _n in tx.label_candidates(paras, top=1000)]

    def find(label: str) -> str | None:
        key = tx._norm_label(label)
        return next((c for c in cands if c == label), None) or next(
            (c for c in cands if tx._norm_label(c) == key), None)

    part, iv = find(std["participant"]), find(std["interviewer"])
    out["participant"], out["interviewer"] = part, iv
    if part is None:
        out["problems"].append("participant_missing")
    if iv is None:
        out["problems"].append("interviewer_missing")
    if part is not None and iv is not None:
        t = tx.parse_paragraphs(paras, {part}, row["pid"], interviewer_labels={iv})
        turns = {s: sum(1 for x in t.turns if x.speaker == s) for s in ("participant", "interviewer", "unknown")}
        out["turns"] = turns
        if t.speaker_format == "none" or not turns["participant"] or not turns["interviewer"] \
                or t.speaker_inference != "label":
            out["problems"].append("not_told_apart")
    name = row["transcript"] or row["pid"]
    hits = roles_for_name(std, name, roles)
    if len(hits) == 1:
        out["role"] = hits[0]
    else:
        out["problems"].append("no_rule" if not hits else "many_rules")
    return out


def standard_view(project) -> dict:
    """``GET /api/labels/standard``: the saved standard (or None) and, with one, what it fills for every imported
    transcript (``propose``). Labels and counts only, as ``label_preview``; never a turn's words."""
    std = load_standard(project)
    rows = []
    if std is not None:
        roles = setup_roles(project)
        rows = [propose(project, std, r, roles) for r in _table(project) if r["transcript_rel"]]
    return {"standard": std, "rows": rows}


# --- missing rule -----------------------------------------------------------------------------------------------

def rule_dict(rule: MissingRule) -> dict:
    return {"kind": rule.kind, "colors": list(rule.colors), "class_names": list(rule.class_names),
            "inherit": rule.inherit}


def rule_of(project, suffix: str) -> MissingRule | None:
    """The project's rule for ``suffix`` (``readers.rule_for`` on its table), else the format default."""
    stored = _store(project)["missing_rules"]
    if suffix in stored:
        return readers.rule_for({"missing_rules": stored}, f"x{suffix}")
    return DEFAULT_RULES.get(suffix)


def rules_view(project) -> dict:
    """``GET /api/missing-rule``: the rule in force per format and the kinds each format allows."""
    rules = {}
    for suffix in RULE_OPTIONS:
        r = rule_of(project, suffix)
        rules[suffix] = {**rule_dict(r), "default": r == DEFAULT_RULES[suffix]}
    return {"rules": rules, "options": {s: list(k) for s, k in RULE_OPTIONS.items()},
            "inherit_suffixes": list(INHERIT_SUFFIXES)}


COLOR_SUFFIXES = (".vsdx", ".drawio", ".excalidraw")      # formats whose missing rule can be a colour
MAX_FOUND = 40


def colors_found(project) -> dict:
    """``GET /api/missing-rule/colors``: per colour-rule format with imported maps, the distinct outline, text and
    fill colours of their boxes (``readers.box_colors``, the format's own reader), each with the number of boxes that
    use it anywhere and per place, most used first (at most ``MAX_FOUND``; ``more`` counts the rest). A map that
    cannot be read is counted in ``unreadable`` and skipped. Colours and counts only, never a file name or a box's
    text; nothing is logged or saved."""
    out: dict = {}
    for row in _table(project):
        rel = row["map_rel"]
        suffix = readers.map_suffix(rel) if rel else None
        if suffix not in COLOR_SUFFIXES:
            continue
        f = out.setdefault(suffix, {"maps": 0, "boxes": 0, "unreadable": 0, "tally": {}})
        f["maps"] += 1
        try:
            boxes = readers.box_colors(project.path(rel))
        except Exception:  # noqa: BLE001  (a refused or broken map: counted, never quoted)
            f["unreadable"] += 1
            continue
        f["boxes"] += len(boxes)
        for places in boxes.values():
            for color in set().union(*places.values()):
                t = f["tally"].setdefault(color, {"color": color, "boxes": 0, "line": 0, "text": 0, "fill": 0})
                t["boxes"] += 1
                for place in ("line", "text", "fill"):
                    t[place] += color in places.get(place, ())
    for f in out.values():
        ranked = sorted(f.pop("tally").values(), key=lambda t: (-t["boxes"], t["color"]))
        f["colors"], f["more"] = ranked[:MAX_FOUND], max(0, len(ranked) - MAX_FOUND)
    return {"formats": out, "max_colors": MAX_COLORS}


def set_missing_rule(project, body: dict) -> dict:
    """``POST /api/missing-rule {suffix, kind, colors?, class_names?, inherit?}``; 422 ``bad_rule`` for a kind the
    format does not allow, a colour that is not ``#rrggbb``, a class name that is not a plain name, or ``inherit``
    anywhere but a Visio red-line rule. Logs ``missing_rule``."""
    suffix, kind = body.get("suffix"), body.get("kind")
    bad = refuse(422, "bad_rule")
    if not isinstance(suffix, str) or suffix not in RULE_OPTIONS or kind not in RULE_OPTIONS[suffix]:
        raise bad
    kw: dict = {"kind": kind}
    colors = body.get("colors")
    if colors is not None:
        if not isinstance(colors, list) or not 1 <= len(colors) <= MAX_COLORS:
            raise bad
        colors = [c.strip().lower() if isinstance(c, str) else c for c in colors]
        if not all(isinstance(c, str) and _COLOR.match(c) for c in colors):
            raise bad
        kw["colors"] = tuple(dict.fromkeys(colors))
    names = body.get("class_names")
    if names is not None:
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_COLORS:
            raise bad
        if not all(isinstance(c, str) and _CLASS.match(c) for c in names):
            raise bad
        kw["class_names"] = tuple(dict.fromkeys(names))
    inherit = body.get("inherit", False)
    if not isinstance(inherit, bool) or (inherit and (suffix not in INHERIT_SUFFIXES or kind != "red_line_or_text")):
        raise bad
    kw["inherit"] = inherit
    rule = MissingRule(**kw)
    d = rule_dict(rule)
    project.log.append("missing_rule", {"suffix": suffix, **d})
    _store(project)["missing_rules"][suffix] = d
    project.save()
    return {"suffix": suffix, "rule": {**d, "default": rule == DEFAULT_RULES[suffix]}}


# --- parsed-map preview and confirmation ------------------------------------------------------------------------

def phase_by_position(xs: list, phases: list[str]) -> list:
    """Setup phases as equal-width bands between the smallest and largest ``x``: each ``x`` gets its band's phase,
    a missing ``x`` the earliest phase; no Setup phases gives None everywhere."""
    if not phases:
        return [None] * len(xs)
    known = [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]
    lo, hi = (min(known), max(known)) if known else (0.0, 0.0)
    out = []
    for x in xs:
        if x not in known or hi <= lo:
            out.append(phases[0])
            continue
        i = int((x - lo) / (hi - lo) * len(phases))
        out.append(phases[min(max(i, 0), len(phases) - 1)])
    return out


def _map_row(project, pid: Any) -> dict:
    row = _row(project, pid)
    if not row["map_rel"]:
        raise refuse(404, "no_map")
    return row


def _read(project, row: dict) -> tuple[MapGraph, MissingRule | None, str]:
    rel = row["map_rel"]
    suffix = readers.map_suffix(rel)
    rule = rule_of(project, suffix)
    path = project.path(rel)
    try:
        g = readers.read_map(path, row["pid"], None, rule)
    except MapRefused as e:
        raise refuse(422, "map_refused", detail=e.detail) from None
    except Exception:  # noqa: BLE001
        raise refuse(422, "map_refused", detail=strings.t("importer.error.map_unreadable_detail")) from None
    return g, rule, suffix


def _effective_phases(project, pid: str, g: MapGraph) -> list[tuple[str | None, str]]:
    """(phase, from) per node: the user's choice ("you"), else the reader's ("map"), else by position."""
    phases = setup_phases(project)
    chosen = ((_store(project)["maps"].get(pid) or {}).get("node_phase") or {})
    by_x = phase_by_position([n.x for n in g.nodes], phases)
    out = []
    for n, px in zip(g.nodes, by_x):
        if n.node_id in chosen:
            out.append((chosen[n.node_id], "you"))
        elif n.phase:
            out.append((n.phase, "map"))
        else:
            out.append((px, "position"))
    return out


def _counts(g: MapGraph) -> dict:
    return {"boxes": len(g.nodes), "missing": sum(1 for n in g.nodes if n.status == "wished"),
            "missing_arrows": sum(1 for e in g.edges if e.status == "wished"), "arrows": len(g.edges),
            "unresolved": int(g.unresolved_edges or 0)}


def _norm_text(text: Any) -> str:
    return " ".join(text.split()).casefold() if isinstance(text, str) else ""


def phase_heading_flags(texts: list, phases: list[str]) -> list[bool]:
    """Per box text: True when it equals a Setup phase name, ignoring case and runs of whitespace (a lane heading
    drawn as a box). Preview only: nothing is dropped from the map, the counts or the reference the run reads."""
    names = {_norm_text(p) for p in phases} - {""}
    return [bool(t) and _norm_text(t) in names for t in texts]


def preview_map(project, pid: Any) -> dict:
    """``GET /api/maps/preview?pid=``: the parsed map as the kit will read it, with the phases, the counts, the
    warnings (``zero_missing`` and, for Visio, the offer of inherited red) and whether it is confirmed."""
    row = _map_row(project, pid)
    pid = row["pid"]
    g, rule, suffix = _read(project, row)
    eff = _effective_phases(project, pid, g)
    phases = setup_phases(project)
    counts = _counts(g)
    meta = project.data["files"].get(row["map_rel"]) or {}
    sha = file_sha256(project.path(row["map_rel"]))
    warnings = []
    if counts["missing"] == 0:
        warnings.append({"code": "zero_missing", "message": strings.t("importer.warning.zero_missing")})
    off = sum(1 for ph, _ in eff if ph is not None and phases and ph not in phases)
    if off:
        warnings.append({"code": "phase_not_in_setup", "n": off,
                         "message": strings.t("importer.warning.phase_not_in_setup", n=str(off))})
    if not phases:
        warnings.append({"code": "no_phases", "message": strings.t("importer.warning.no_phases")})
    heading = phase_heading_flags([n.text for n in g.nodes], phases)
    nodes = [{"id": n.node_id, "text": n.text, "status": "missing" if n.status == "wished" else "happened",
              "phase": ph, "phase_from": src, "x": n.x, "phase_heading": hd}
             for n, (ph, src), hd in zip(g.nodes, eff, heading)]
    n_heading = sum(heading)
    if n_heading:
        counts_text = strings.t("setup.maps.counts_heading_one" if n_heading == 1 else "setup.maps.counts_headings",
                                boxes=str(counts["boxes"] - n_heading), headings=str(n_heading),
                                missing=str(counts["missing"]), arrows=str(counts["arrows"]))
    else:
        counts_text = strings.t("importer.counts", boxes=str(counts["boxes"]), missing=str(counts["missing"]),
                                arrows=str(counts["arrows"]))
    edges = [{"source": e.source, "target": e.target, "status": "missing" if e.status == "wished" else "happened",
              "label": e.label} for e in g.edges]
    offer = suffix in INHERIT_SUFFIXES and counts["missing"] == 0 and not (rule and rule.inherit)
    return {"pid": pid, "map": row["map"], "suffix": suffix, "sha256": sha, "changed": sha != meta.get("sha256"),
            "rule": {**rule_dict(rule), "default": rule == DEFAULT_RULES.get(suffix)} if rule else None,
            "phases": phases, "nodes": nodes, "edges": edges, "counts": counts,
            "counts_text": counts_text, "phase_headings": n_heading,
            "warnings": warnings, "offer_inherit": offer, "confirmed": map_confirmed(project, pid)}


def set_phases(project, body: dict) -> dict:
    """``POST /api/maps/phases {pid, node_phase: {node_id: phase}}``: the user's phase for some boxes (each a Setup
    phase, each a box of this map); merged into ``maps[pid].node_phase`` (logged with the map's confirmation)."""
    row = _map_row(project, body.get("pid"))
    choice = body.get("node_phase")
    phases = setup_phases(project)
    if not isinstance(choice, dict) or not choice or not all(isinstance(v, str) and v in phases
                                                             for v in choice.values()):
        raise refuse(422, "bad_phase")
    g, _, _ = _read(project, row)
    ids = {n.node_id for n in g.nodes}
    if not all(isinstance(k, str) and k in ids for k in choice):
        raise refuse(422, "unknown_node")
    entry = _store(project)["maps"].setdefault(row["pid"], {})
    entry["node_phase"] = {**(entry.get("node_phase") or {}), **choice}
    project.save()
    return preview_map(project, row["pid"])


def confirm_map(project, body: dict) -> dict:
    """``POST /api/maps/confirm {pid, sha256}``: refuse a stale sha256 (409 ``stale_map``) or a file edited since
    import (409 ``file_changed``); write ``reference/<pid>.mapgraph.json`` (reader output + the phases shown,
    ``meta.reference_type = "A"``, ``meta.source_sha256``); log ``map_confirmed``."""
    row = _map_row(project, body.get("pid"))
    pid = row["pid"]
    _require_pairing(project, pid)
    if not setup_phases(project):
        raise refuse(409, "no_context")
    sha = _require_unchanged(project, row["map_rel"])
    if body.get("sha256") != sha:
        raise refuse(409, "stale_map")
    g, rule, _ = _read(project, row)
    eff = _effective_phases(project, pid, g)
    for n, (ph, _src) in zip(g.nodes, eff):
        n.phase = ph
    g.map_id, g.origin = pid, "endorsed"
    g.meta = {**(g.meta or {}), "reference_type": "A", "source_sha256": sha}
    ref = f"reference/{pid}.mapgraph.json"
    target = project.path(ref)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, (g.to_json() + "\n").encode("utf-8"))
    ref_sha = file_sha256(target)
    counts = _counts(g)
    rd = rule_dict(rule) if rule else None
    project.log.append("map_confirmed", {"pid": pid, "sha256": sha, "counts": counts, "rule": rd,
                                         "phases_sha256": _canon_sha({n.node_id: n.phase for n in g.nodes}),
                                         "n_phases_set": sum(1 for _, src in eff if src == "you"),
                                         "reference_sha256": ref_sha})
    entry = _store(project)["maps"].setdefault(pid, {})
    entry["confirmed"] = {"sha256": sha, "rule": rd, "node_phase": dict(entry.get("node_phase") or {}),
                          "phases": setup_phases(project), "counts": counts, "reference": ref,
                          "reference_sha256": ref_sha}
    project.save()
    return {"pid": pid, "reference": ref, "counts": counts, "confirmed": True}


def map_confirmed(project, pid: str) -> bool:
    """True while the confirmed map file, its format's rule, the user's phases and the Setup phases are unchanged."""
    entry = _store(project)["maps"].get(pid) or {}
    conf = entry.get("confirmed")
    rel = ((_store(project)["pairing"].get("pids") or {}).get(pid) or {}).get("map")
    if not conf or not rel:
        return False
    meta = project.data["files"].get(rel) or {}
    rule = rule_of(project, readers.map_suffix(rel))
    return (meta.get("sha256") == conf.get("sha256")
            and (rule_dict(rule) if rule else None) == conf.get("rule")
            and dict(entry.get("node_phase") or {}) == conf.get("node_phase")
            and setup_phases(project) == conf.get("phases"))


def unconfirmed(project) -> dict:
    """What still blocks a run (Task 38): ``pairing`` True while the pairing table needs confirming, and the pids
    of the last confirmed pairing whose labels or (paired) map are not confirmed."""
    pids = _store(project)["pairing"].get("pids") or {}
    return {"pairing": not pairing_current(project),
            "labels": sorted(p for p in pids if not labels_confirmed(project, p)),
            "maps": sorted(p for p, r in pids.items() if r.get("map") and not map_confirmed(project, p))}


# --- projects made before the privacy ruling ---------------------------------------------------------------------

def _stored_suffix(rel: str, kind: str) -> str:
    return readers.map_suffix(rel) if kind == "map" else Path(rel).suffix.lower()


def migrate_file_names(project) -> bool:
    """Run on open (``Project.open``). A project made before privacy ruling F kept each file's name in its ``files``
    entry (``name``, ``stem``) and in its key (``inputs/<kind>s/<name>``). Move the names to ``private/file_names.json``
    (written first, so no name is lost), rename each copy to ``<pid><suffix>``, re-key ``files`` and the pairing (its
    confirmation is kept when it held before), drop the ``name`` / ``stem`` fields, and delete leftover staged copies
    (``.import-*``), whose names held the file name. The pids stay as they were (they key the labels, maps, codings
    and runs). The app log is hash-chained and is not rewritten: its older ``import`` records keep the names they
    hold. Returns True when ``second-scribe-project.json`` was rewritten."""
    for folder in FOLDERS.values():
        try:
            d = project.path(folder)
            leftovers = [c for c in d.iterdir() if c.name.startswith((".import-", ".remove-")) and c.is_file()] \
                if d.is_dir() else []
        except (OSError, ProjectError):
            leftovers = []
        for c in leftovers:
            try:
                c.unlink()
            except OSError:
                pass
    files = project.data.get("files") or {}
    moves: dict[str, str] = {}
    for rel, meta in files.items():
        if not isinstance(meta, dict) or meta.get("kind") not in KINDS or not isinstance(meta.get("pid"), str):
            continue
        new_rel = f"{FOLDERS[meta['kind']]}/{meta['pid']}{_stored_suffix(rel, meta['kind'])}"
        if new_rel != rel or "name" in meta or "stem" in meta:
            moves[rel] = new_rel
    if not moves:
        return False
    was_current = pairing_current(project)
    names = load_names(project)
    for rel in moves:
        meta = files[rel]
        name = meta.get("name") if isinstance(meta.get("name"), str) else rel.rsplit("/", 1)[-1]
        stem = meta.get("stem") if isinstance(meta.get("stem"), str) else file_stem(name, meta["kind"])
        entry = names["pids"].setdefault(meta["pid"], {"stem": stem, "transcript": None, "map": None})
        entry["stem"] = entry.get("stem") or stem
        entry[meta["kind"]] = entry.get(meta["kind"]) or name
    _save_names(project, names)
    out: dict[str, dict] = {}
    for rel, meta in files.items():
        new_rel = moves.get(rel, rel)
        if new_rel != rel:
            try:
                src, dst = project.path(rel), project.path(new_rel)
                if src.is_file() and not dst.exists():
                    os.replace(src, dst)
            except (OSError, ProjectError):
                pass                     # a copy that cannot move shows as changed; the user imports it again
        out[new_rel] = {k: v for k, v in meta.items() if k not in ("name", "stem")} if isinstance(meta, dict) else meta
    project.data["files"] = out
    pairing = _store(project)["pairing"]
    for entry in (pairing.get("pids") or {}).values():
        if isinstance(entry, dict):
            for kind in KINDS:
                if entry.get(kind) in moves:
                    entry[kind] = moves[entry[kind]]
    if was_current:
        pairing["table_sha256"] = _table_sha(_table(project))
    project.save()
    return True
