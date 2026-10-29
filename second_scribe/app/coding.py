"""Reference choice, the mode-B coding store, the lock, the results gate and the conversion to a reference (Task 36;
spec section 6.6, 6.7, section 4 per-map reference; the kit's reference modes are ``second_scribe.reference``).

**Reference per participant.** ``project.data["references"][pid] = {"type": "A"|"B"|"C"}``, each choice logged as
``reference_set{pid, type}``. The file ``reference/<pid>.mapgraph.json`` always follows the choice:

- A: the normalised map Task 33 wrote when the map was confirmed (``meta.reference_type = "A"``). Allowed only for a
  pid with a ``map_confirmed`` record. Switching away moves it aside to ``reference/<pid>.A.mapgraph.json`` (a pid
  never holds a dot, Task 33's ``pid_from_stem``), and switching back restores it.
- B: written from the coding at lock time (``to_reference``); no file while the coding is open.
- C: ``reference.empty_reference(pid)``.

**Blindness (F8).** While any B coding in the project is unlocked, no route serves results, the no-interview map
or an AI map, for ANY group. ``results_gate(project)`` is true only when every B pid is locked;
``require_results(project)`` refuses with 409 ``coding_unlocked`` otherwise and, the first time it lets a result
through, appends ``reveal{scope: "project"}`` (once per project). Routes that serve such content register with
``results_route`` (route + gate). Once the reveal is logged, a pid may become B again only when its coding was
locked before the reveal: a new coding after the researcher has seen results would not be blind (refused with 409
``revealed``; Task 41 reads "every lock precedes the reveal" from the same log).

**Locked means logged.** ``lock`` computes the coding's sha256 (``coding_sha256``: canonical JSON with
``locked`` false), appends ``coding_locked{pid, sha256, n_marks, coding_seconds}``, writes the B reference, and only
then sets ``locked`` in a separate file write. A coding is locked exactly when its record is in the app log: a crash
after the record is settled on the next load, and a ``locked`` flag without a record counts for nothing.

**Imported data (Task 33).** The only readers of the importer's keys are ``transcript_source``, ``setup_phases``
and ``map_confirmed``; their docstrings name the keys. Coding needs the speaker labels of the transcript confirmed
and unchanged (``require_labels``: 409 ``labels_unconfirmed``, or ``file_changed`` when the file on disk is not the
one confirmed). Task 33's ``POST /api/maps/confirm`` always writes the reference as type A; the route then calls
``apply_reference_choice`` so the file follows the choice again.

This interface is the only blindness there is: the stated limit (``certificate.BLINDNESS_LIMIT``, spec section
6.7) is served with the coding and shown on the coding screen.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from .. import provenance, textnorm
from ..certificate import BLINDNESS_LIMIT
from ..maps.table import MISSING_KINDS
from ..reference import empty_reference
from ..schemas import MapGraph, Node
from ..transcripts import parse_paragraphs, read_transcript_paras
from . import importer, strings
from .api import ApiError, route
from .project import Project, ProjectError, file_sha256

SCHEMA = "coding/1"
REFERENCE_TYPES = ("A", "B", "C")
STATUSES = ("happened", "missing")
# the kit's quote rule: a span shorter than this many words is not located (provenance.eligible's default)
MIN_WORDS: int = inspect.signature(provenance.eligible).parameters["min_words"].default
MAX_STEP = 200
MAX_TICK = 3600                 # seconds one tick may add (the screen ticks far more often)
MAX_SUGGEST = 20
_PID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_reveal_lock = threading.RLock()


def _err(status: int, code: str, **kw: Any) -> ApiError:
    return ApiError(status, code, strings.t(f"coding.error.{code}", **kw))


# --- what Task 33 leaves in the project (the one place its keys are read) ----------------------------------------

def transcript_source(project: Project, pid: str) -> dict | None:
    """The imported transcript of ``pid``: ``{"rel", "participant_labels", "interviewer_labels"}``, or None.

    Task 33 keys: ``files[<rel>] = {"kind": "transcript", "pid", "sha256", "bytes", "labels"}`` (no file name) with
    ``<rel>`` under ``inputs/transcripts/``, and ``labels[<pid>] = {"participant", "interviewer", "role", "format",
    "turns", "transcript_sha256"}`` once the labels are confirmed (``POST /api/labels/confirm``). The labels come
    from ``labels[<pid>]`` and only while ``importer.labels_confirmed`` holds; otherwise both lists are empty and
    ``require_labels`` refuses coding."""
    for rel, meta in sorted(project.data.get("files", {}).items()):
        if isinstance(meta, dict) and meta.get("kind") == "transcript" and meta.get("pid") == pid:
            part = inter = None
            if importer.labels_confirmed(project, pid):
                entry = project.data["labels"][pid]
                part, inter = entry.get("participant"), entry.get("interviewer")
            return {"rel": rel,
                    "participant_labels": [part] if isinstance(part, str) and part else [],
                    "interviewer_labels": [inter] if isinstance(inter, str) and inter else []}
    return None


def require_labels(project: Project, pid: str) -> dict:
    """``transcript_source`` of a pid that may be coded: 404 ``unknown_pid``; 409 ``labels_unconfirmed`` unless the
    speaker labels are confirmed and still those of the transcript imported (``importer.labels_confirmed``); 409
    ``file_changed`` when the transcript on disk is no longer the one the labels were confirmed on."""
    src = transcript_source(project, pid)
    if src is None:
        raise _err(404, "unknown_pid")
    if not importer.labels_confirmed(project, pid):
        raise _err(409, "labels_unconfirmed")
    try:
        now = file_sha256(project.path(src["rel"]))
    except (OSError, ProjectError):
        now = None
    if now != project.data["labels"][pid].get("transcript_sha256"):
        raise _err(409, "file_changed")
    return src


def known_pids(project: Project) -> list[str]:
    """Every pid with an imported transcript, sorted."""
    return sorted({m["pid"] for m in project.data.get("files", {}).values()
                   if isinstance(m, dict) and m.get("kind") == "transcript" and isinstance(m.get("pid"), str)})


def setup_phases(project: Project) -> list[str]:
    """The phases from Setup (``context["phases"]``, set by ``POST /api/context``; Task 33 keeps
    ``context = {"domain", "phases", "roles"}``)."""
    return [p for p in importer.setup_phases(project) if isinstance(p, str) and p]


def map_confirmed(project: Project, pid: str) -> bool:
    """True while the pid's map is confirmed and unchanged (``importer.map_confirmed``: it goes stale when the map
    file, its format's rule, the phases set on its boxes or the Setup phases change)."""
    return importer.map_confirmed(project, pid)


def turns(project: Project, pid: str) -> list:
    """The transcript turns of ``pid``, parsed exactly as the kit parses them for the run (``pipeline.rule_flagged``:
    ``parse_paragraphs`` with the confirmed labels), so a mark's turn index and offsets are the kit's."""
    src = require_labels(project, pid)
    try:
        paras = read_transcript_paras(project.path(src["rel"]))
    except (OSError, ValueError, ProjectError):
        raise _err(409, "transcript_unreadable") from None
    t = parse_paragraphs(paras, set(src["participant_labels"]), pid,
                         interviewer_labels=set(src["interviewer_labels"]) or None)
    return t.turns


# --- files ---------------------------------------------------------------------------------------------------------

def check_pid(pid: Any) -> str:
    if not isinstance(pid, str) or not _PID.match(pid):
        raise _err(404, "unknown_pid")
    return pid


def coding_path(project: Project, pid: str) -> Path:
    return project.path(f"coding/{check_pid(pid)}.coding.json")


def reference_path(project: Project, pid: str) -> Path:
    return project.path(f"reference/{check_pid(pid)}.mapgraph.json")


def _stash_path(project: Project, pid: str) -> Path:
    return project.path(f"reference/{check_pid(pid)}.A.mapgraph.json")


def _write_text(path: Path, text: str) -> None:
    """Atomic write: temp file in the same folder, fsync, ``os.replace``."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
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


def _write_json(path: Path, doc: dict) -> None:
    _write_text(path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_graph(path: Path, g: MapGraph) -> None:
    _write_text(path, g.to_json() + "\n")


def _read_graph(path: Path) -> MapGraph | None:
    try:
        return MapGraph.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return None


# --- the coding store ----------------------------------------------------------------------------------------------

def new_coding(pid: str) -> dict:
    return {"schema": SCHEMA, "pid": pid, "marks": [], "seconds": 0, "locked": False}


def canonical(doc: dict) -> bytes:
    """The bytes the lock hashes: the coding as canonical JSON with ``locked`` false (the state it is locked in)."""
    body = {**doc, "locked": False}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def coding_sha256(doc: dict) -> str:
    return hashlib.sha256(canonical(doc)).hexdigest()


def lock_record(project: Project, pid: str) -> dict | None:
    """The payload of ``pid``'s ``coding_locked`` record, or None. A coding is locked only once (read-only after)."""
    for r in project.log.records("coding_locked"):
        if (r.get("payload") or {}).get("pid") == pid:
            return r["payload"]
    return None


def is_locked(project: Project, pid: str) -> bool:
    """Locked means logged: True exactly when the app log holds ``pid``'s ``coding_locked`` record."""
    return lock_record(project, pid) is not None


def _read_coding(project: Project, pid: str) -> dict:
    f = coding_path(project, pid)
    if not f.exists():
        return new_coding(pid)
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise _err(409, "bad_coding_file") from None
    if (not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("pid") != pid
            or not isinstance(doc.get("marks"), list) or not isinstance(doc.get("seconds"), int)):
        raise _err(409, "bad_coding_file")
    return doc


def load_coding(project: Project, pid: str) -> dict:
    """``coding/<pid>.coding.json`` (a new, empty coding when there is none), with ``locked`` taken from the log.

    A lock that a crash interrupted after its record is completed here: the flag is set and, while the pid is B
    and the coding still hashes to the logged sha256, the B reference is written."""
    check_pid(pid)
    doc = _read_coding(project, pid)
    rec = lock_record(project, pid)
    doc["locked"] = rec is not None
    if rec is not None:
        f = coding_path(project, pid)
        stored = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        intact = coding_sha256(doc) == rec.get("sha256")
        if intact and isinstance(stored, dict) and stored.get("locked") is not True:
            _write_json(f, doc)
        if intact and reference_type_of(project, pid) == "B":
            g = _read_graph(reference_path(project, pid))
            if g is None or g.meta.get("reference_type") != "B" or g.meta.get("coding_sha256") != rec["sha256"]:
                _write_graph(reference_path(project, pid), to_reference(doc))
    return doc


def changed_after_lock(project: Project, pid: str, doc: dict | None = None) -> bool:
    """True when a locked coding no longer hashes to its logged sha256 (edited by hand after the lock)."""
    rec = lock_record(project, pid)
    if rec is None:
        return False
    doc = doc if doc is not None else _read_coding(project, pid)
    return coding_sha256({**doc, "locked": False}) != rec.get("sha256")


def _open_coding(project: Project, pid: str) -> dict:
    """The coding of a B pid that may still change: 409 ``not_mode_b`` / ``locked`` otherwise."""
    require_labels(project, check_pid(pid))
    if reference_type_of(project, pid) != "B":
        raise _err(409, "not_mode_b")
    doc = load_coding(project, pid)
    if doc["locked"]:
        raise _err(409, "locked")
    return doc


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def add_mark(project: Project, pid: str, body: dict) -> dict:
    """Validate and store one mark; returns it. The span must be the exact slice ``[start:end]`` of the parsed turn
    and hold at least ``MIN_WORDS`` words (``textnorm.word_count``, as the kit counts quote words), unless it is the
    whole turn (the kit's ``whole_turn`` exception)."""
    doc = _open_coding(project, pid)
    all_turns = turns(project, pid)
    turn, start, end, text = _int(body.get("turn")), _int(body.get("start")), _int(body.get("end")), body.get("text")
    if turn is None or start is None or end is None or not isinstance(text, str):
        raise _err(422, "bad_span")
    if not 0 <= turn < len(all_turns) or not 0 <= start < end <= len(all_turns[turn].text):
        raise _err(422, "bad_span")
    if all_turns[turn].text[start:end] != text:
        raise _err(422, "span_mismatch")
    # The kit's rule (provenance.eligible): fewer than MIN_WORDS words only when the span is the whole turn, compared
    # as word sequences exactly as provenance.locate_quote does (``whole_turn``). A span with no words never counts.
    words = textnorm.word_count(text)
    whole_turn = words > 0 and textnorm.token_seq(text) == textnorm.token_seq(all_turns[turn].text)
    if words < MIN_WORDS and not whole_turn:
        raise _err(422, "span_too_short", min_words=MIN_WORDS)
    status = body.get("status")
    if status not in STATUSES:
        raise _err(422, "bad_status")
    kind = body.get("missing_kind")
    if kind in ("", None):
        kind = None
    elif status != "missing" or kind not in MISSING_KINDS:
        raise _err(422, "bad_missing_kind")
    step = body.get("step")
    if not isinstance(step, str) or not step.strip() or len(step) > MAX_STEP or _CONTROL.search(step):
        raise _err(422, "bad_step")
    phase = body.get("phase")
    if phase not in setup_phases(project):
        raise _err(422, "bad_phase")
    used = {m.get("mark_id") for m in doc["marks"]}
    n = len(doc["marks"]) + 1
    while f"mark-{n:04d}" in used:
        n += 1
    mark = {"mark_id": f"mark-{n:04d}", "turn": turn, "start": start, "end": end, "text": text, "status": status,
            "missing_kind": kind, "step": " ".join(step.split()), "phase": phase}
    doc["marks"].append(mark)
    _write_json(coding_path(project, pid), doc)
    return mark


def remove_mark(project: Project, pid: str, mark_id: Any) -> int:
    """Remove one mark; returns the number left. 404 ``unknown_mark``."""
    doc = _open_coding(project, pid)
    keep = [m for m in doc["marks"] if m.get("mark_id") != mark_id]
    if len(keep) == len(doc["marks"]):
        raise _err(404, "unknown_mark")
    doc["marks"] = keep
    _write_json(coding_path(project, pid), doc)
    return len(keep)


def tick(project: Project, pid: str, seconds: Any) -> int:
    """Add visible coding time (1 to ``MAX_TICK`` whole seconds); returns the total."""
    s = _int(seconds)
    if s is None or not 1 <= s <= MAX_TICK:
        raise _err(422, "bad_seconds", max=MAX_TICK)
    doc = _open_coding(project, pid)
    doc["seconds"] += s
    _write_json(coding_path(project, pid), doc)
    return doc["seconds"]


def suggest(project: Project, prefix: Any) -> list[str]:
    """Step names this researcher has already used in this project's codings that start with ``prefix``
    (case-insensitive), sorted, at most ``MAX_SUGGEST``. Never a name from an AI map or the no-interview map: the
    only source read is ``coding/*.coding.json``."""
    pre = textnorm.normalize(prefix) if isinstance(prefix, str) else ""
    seen: dict[str, str] = {}
    folder = project.path("coding")
    for f in sorted(folder.glob("*.coding.json")):
        pid = f.name[: -len(".coding.json")]
        if not _PID.match(pid):
            continue
        try:
            doc = _read_coding(project, pid)
        except ApiError:
            continue
        for m in doc["marks"]:
            step = m.get("step")
            if isinstance(step, str) and textnorm.normalize(step).startswith(pre):
                seen.setdefault(textnorm.normalize(step), step)
    return sorted(seen.values(), key=lambda s: (textnorm.normalize(s), s))[:MAX_SUGGEST]


# --- conversion to a reference (F7) ----------------------------------------------------------------------------

def to_reference(coding: dict) -> MapGraph:
    """The coding as a mode-B reference: one node per distinct (step, phase), the step name compared after
    ``textnorm.normalize`` (the first spelling, in reading order, is kept), so one step in two phases is two nodes.
    ``status`` experienced for happened, wished for missing; ``quote`` = the first span in reading order; the
    missing kinds in ``meta.missing_kinds = {node_id: kind}`` (the kit's ``Node`` has no meta); no edges. A step
    marked both happened and missing in one phase is ``ApiError(422, "conflicting_marks")`` naming the step."""
    pid = coding["pid"]
    marks = sorted(coding.get("marks") or [], key=lambda m: (m["turn"], m["start"], m["mark_id"]))
    nodes: dict[tuple[str, str], Node] = {}
    kinds: dict[str, str] = {}
    for m in marks:
        key = (textnorm.normalize(m["step"]), m["phase"])
        status = "experienced" if m["status"] == "happened" else "wished"
        node = nodes.get(key)
        if node is None:
            node = Node(node_id=f"b{len(nodes) + 1:03d}", text=m["step"], node_type="other", phase=m["phase"],
                        status=status, quote=m["text"])
            nodes[key] = node
        elif node.status != status:
            raise _err(422, "conflicting_marks", step=node.text, phase=m["phase"])
        if status == "wished" and m.get("missing_kind") and node.node_id not in kinds:
            kinds[node.node_id] = m["missing_kind"]
    return MapGraph(map_id=pid, origin="endorsed", nodes=list(nodes.values()),
                    meta={"reference_type": "B", "missing_kinds": kinds, "coding_sha256": coding_sha256(coding)})


# --- the lock --------------------------------------------------------------------------------------------------

def lock(project: Project, pid: str) -> dict:
    """Finish and lock: (1) the sha256 of the canonical coding, (2) ``coding_locked{pid, sha256, n_marks,
    coding_seconds}`` appended to the app log, (3) the B reference written, (4) only then ``locked`` set in a
    separate file write. A conflict (``to_reference``) refuses before anything is logged."""
    doc = _open_coding(project, pid)
    if not doc["marks"]:
        raise _err(422, "no_marks")
    g = to_reference(doc)
    sha = coding_sha256(doc)
    project.log.append("coding_locked", {"pid": pid, "sha256": sha, "n_marks": len(doc["marks"]),
                                         "coding_seconds": doc["seconds"]})
    _write_graph(reference_path(project, pid), g)
    _write_json(coding_path(project, pid), {**doc, "locked": True})
    return {"sha256": sha, "n_marks": len(doc["marks"])}


# --- the results gate --------------------------------------------------------------------------------------------

def reference_type_of(project: Project, pid: str) -> str | None:
    entry = (project.data.get("references") or {}).get(pid)
    t = entry.get("type") if isinstance(entry, dict) else None
    return t if t in REFERENCE_TYPES else None


def b_pids(project: Project) -> list[str]:
    return sorted(pid for pid in (project.data.get("references") or {}) if reference_type_of(project, pid) == "B")


def waiting(project: Project) -> list[str]:
    """The B pids whose coding is not locked yet."""
    return [pid for pid in b_pids(project) if not is_locked(project, pid)]


def results_gate(project: Project) -> bool:
    """True only when every B-reference pid is locked (vacuously true with no B pid)."""
    return not waiting(project)


def revealed(project: Project) -> bool:
    return bool(project.log.records("reveal"))


def require_results(project: Project, *, log_reveal: bool = True) -> None:
    """Call before serving results, the no-interview map or any AI map. 409 ``coding_unlocked`` while a B coding is
    open; otherwise, unless ``log_reveal`` is false, ``note_reveal`` (the caller then serves the content)."""
    with _reveal_lock:
        if not results_gate(project):
            raise _err(409, "coding_unlocked")
        if log_reveal:
            note_reveal(project)


def note_reveal(project: Project) -> None:
    """Append ``reveal{scope: "project"}`` unless the log already holds it (once per project). Only ever called with
    the gate open."""
    with _reveal_lock:
        if not results_gate(project):                # never logged while a coding is open
            raise _err(409, "coding_unlocked")
        if not revealed(project):
            project.log.append("reveal", {"scope": "project"})


def results_route(method: str, path: str):
    """``api.route`` for a handler that serves results, the no-interview map or AI map content: the open project
    (``require_project``) and the gate are checked, under ``state.lock``, before the handler runs, and the reveal is
    logged once the handler has returned its answer (a refusal from the handler reveals nothing)."""
    from .routes.project import require_project

    def register(handler):
        def gated(state, body, query):
            with state.lock:
                project = require_project(state)
                require_results(project, log_reveal=False)
                result = handler(state, body, query)
                note_reveal(project)
                return result

        gated.__name__ = getattr(handler, "__name__", "gated")
        gated.__doc__ = handler.__doc__
        route(method, path)(gated)
        return handler

    return register


# --- reference choice --------------------------------------------------------------------------------------------

def _move_a_aside(project: Project, pid: str) -> None:
    ref = reference_path(project, pid)
    g = _read_graph(ref) if ref.exists() else None
    if g is not None and g.meta.get("reference_type") == "A":
        os.replace(ref, _stash_path(project, pid))


def _place_reference(project: Project, pid: str, rtype: str) -> None:
    """Make ``reference/<pid>.mapgraph.json`` follow the choice (module docstring)."""
    ref = reference_path(project, pid)
    if rtype == "A":
        g = _read_graph(ref) if ref.exists() else None
        stash = _stash_path(project, pid)
        if g is not None and g.meta.get("reference_type") == "A":
            if stash.exists():
                stash.unlink()                       # a newer confirmation replaced the one put aside
            return
        sg = _read_graph(stash) if stash.exists() else None
        if sg is None or sg.meta.get("reference_type") != "A":
            raise _err(409, "map_not_confirmed")
        os.replace(stash, ref)
        return
    g = None
    if rtype == "B" and is_locked(project, pid):     # B: a locked coding's reference; none while it is open
        doc = load_coding(project, pid)
        if changed_after_lock(project, pid, doc):
            raise _err(409, "coding_changed")        # refused before anything on disk moves
        g = to_reference(doc)
    _move_a_aside(project, pid)
    if rtype == "C":
        g = empty_reference(pid)
    if g is not None:
        _write_graph(ref, g)
    elif ref.exists():
        ref.unlink()


def apply_reference_choice(project: Project, pid: str) -> str | None:
    """Make ``reference/<pid>.mapgraph.json`` match the type chosen for ``pid`` (module docstring); returns that
    type, or None (and touches nothing) when no type was chosen. Task 33's ``POST /api/maps/confirm`` always writes
    the file as type A, so it calls this after every (re)confirmation: a B pid goes back to no file while its
    coding is open or to its locked coding's reference, a C pid back to the empty reference, and an A pid keeps the
    new file (the one put aside is dropped)."""
    rtype = reference_type_of(project, check_pid(pid))
    if rtype is not None:
        _place_reference(project, pid, rtype)
    return rtype


def set_reference(project: Project, pid: Any, rtype: Any) -> dict:
    """Choose ``pid``'s reference type; logs ``reference_set{pid, type}`` (nothing when it does not change)."""
    pid = check_pid(pid)
    if transcript_source(project, pid) is None:
        raise _err(404, "unknown_pid")
    if rtype not in REFERENCE_TYPES:
        raise _err(422, "bad_reference_type")
    if rtype == "A" and not map_confirmed(project, pid):
        raise _err(409, "map_not_confirmed")
    if rtype == "B" and revealed(project) and not is_locked(project, pid):
        raise _err(409, "revealed")
    if reference_type_of(project, pid) == rtype:
        return {"pid": pid, "type": rtype, "changed": False}
    _place_reference(project, pid, rtype)
    project.data.setdefault("references", {})[pid] = {"type": rtype}
    project.save()
    project.log.append("reference_set", {"pid": pid, "type": rtype})
    return {"pid": pid, "type": rtype, "changed": True}


def set_confirmed_maps_to_a(project: Project) -> list[str]:
    """Bulk: every pid with a confirmed map becomes A; returns the pids that changed."""
    changed = []
    for pid in known_pids(project):
        if map_confirmed(project, pid) and set_reference(project, pid, "A")["changed"]:
            changed.append(pid)
    return changed


def references_summary(project: Project) -> dict:
    """What the reference screen shows: per pid its type, whether its map is confirmed and its coding's state;
    and the gate. Nothing AI-derived."""
    rows = []
    for pid in known_pids(project):
        f = coding_path(project, pid)
        n = 0
        if f.exists():
            try:
                n = len(_read_coding(project, pid)["marks"])
            except ApiError:
                n = 0
        rows.append({"pid": pid, "type": reference_type_of(project, pid), "map_confirmed": map_confirmed(project, pid),
                     "coding": {"n_marks": n, "locked": is_locked(project, pid)}})
    return {"references": rows, "gate": gate_summary(project)}


def gate_summary(project: Project) -> dict:
    return {"open": results_gate(project), "waiting": waiting(project), "revealed": revealed(project)}


def coding_view(project: Project, pid: Any) -> dict:
    """What the coding screen shows: the transcript turns (speaker and text only), the marks, the time, the phases,
    the missing kinds, the span rule and the stated limit. Never anything AI-derived."""
    pid = check_pid(pid)
    all_turns = turns(project, pid)
    doc = load_coding(project, pid)
    return {"pid": pid, "type": reference_type_of(project, pid),
            "turns": [{"idx": t.idx, "speaker": t.speaker, "text": t.text} for t in all_turns],
            "marks": doc["marks"], "seconds": doc["seconds"], "locked": doc["locked"],
            "changed_after_lock": changed_after_lock(project, pid, doc),
            "phases": setup_phases(project), "missing_kinds": list(MISSING_KINDS), "min_words": MIN_WORDS,
            "limit": BLINDNESS_LIMIT}
