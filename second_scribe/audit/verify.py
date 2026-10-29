"""Verifier for a run's hash chain, anchored to its manifest (ported from the ISR harness).

Layers:

- ``verify_internal_chain(path)``: genesis-forward consistency of the JSONL alone
  (recomputes every hash, checks links and strictly increasing seq). A building
  block: on its own it cannot see end-truncation or a full forward rewrite.
- ``find_orphan_requests(path)``: seqs of ``call_request`` records that were never
  followed by a ``call_response`` or ``call_error`` with the same ``request_id``
  (a crash between sending a request and logging its outcome).
- ``verify_run(jsonl, manifest=None, expected_manifest_hash=None, allow_orphans=False)``:
  the guarantee. The manifest is REQUIRED. Fails when the manifest is missing, the
  chain is broken, the record count is zero, head or count diverge from the
  manifest, the externally recorded manifest hash differs, or (unless
  ``allow_orphans``) an unpaired call_request exists.

CLI:  python -m second_scribe.audit.verify RUN.jsonl [MANIFEST] [--manifest-hash HASH] [--allow-orphans]
      exit 0 = pass, 1 = fail, 2 = usage / file not found.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..paths import stage_from_dirname
from .manifest import compute_manifest_hash, manifest_path_for, read_manifest
from .registry import GENESIS_HASH, compute_hash


def verify_manifest_chain(manifest_paths: list[str],
                          genesis_prev: str | None = None) -> tuple[bool, dict[str, Any]]:
    """Walk a run-ordered list of manifests, checking each ``prev_manifest_hash`` link.

    Each link must equal the SHA-256 of the previous manifest, and the first must
    equal ``genesis_prev`` (default ``None``). Fail-closed: an empty list, a missing
    manifest, or a broken link returns False. Trust still roots in the externally
    recorded hash of the genesis or head manifest.
    """
    if not manifest_paths:
        return False, {"reason": "no_manifests"}
    prev_hash = genesis_prev
    for i, mp in enumerate(manifest_paths):
        try:
            m = read_manifest(mp)
        except FileNotFoundError:
            return False, {"reason": "manifest_missing", "index": i, "path": mp}
        except ValueError as exc:
            return False, {"reason": "manifest_invalid", "index": i, "path": mp, "error": str(exc)}
        if m.get("prev_manifest_hash") != prev_hash:
            return False, {"reason": "prev_manifest_mismatch", "index": i,
                           "expected": prev_hash, "found": m.get("prev_manifest_hash")}
        prev_hash = compute_manifest_hash(m)
    return True, {"count": len(manifest_paths), "head_manifest_hash": prev_hash}


def verify_internal_chain(path: str | os.PathLike) -> tuple[bool, dict[str, Any]]:
    """Genesis-forward consistency of the JSONL alone (building block).

    Never raises on file content: bytes that are not UTF-8, or a record that cannot be
    hashed, return ``(False, {"reason": "log_invalid", ...})``.
    """
    try:
        return _verify_internal_chain(path)
    except UnicodeDecodeError as exc:
        return False, {"reason": "log_invalid", "error": f"not valid UTF-8: {exc}"}


def _verify_internal_chain(path: str | os.PathLike) -> tuple[bool, dict[str, Any]]:
    prev_hash = GENESIS_HASH
    expected_seq = 0
    count = 0
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                return False, {"reason": "malformed_json", "line": lineno, "error": str(exc)}
            if not isinstance(rec, dict):
                return False, {"reason": "malformed_json", "line": lineno, "error": "not an object"}

            for field in ("seq", "run_id", "ts", "record_type", "payload", "prev_hash", "hash"):
                if field not in rec:
                    return False, {"reason": "missing_field", "line": lineno, "field": field}

            if rec["seq"] != expected_seq:
                return False, {"reason": "seq_gap", "line": lineno,
                               "expected": expected_seq, "found": rec["seq"]}
            if rec["prev_hash"] != prev_hash:
                return False, {"reason": "broken_link", "seq": rec["seq"],
                               "expected_prev": prev_hash, "found_prev": rec["prev_hash"]}

            try:
                recomputed = compute_hash(
                    rec["prev_hash"], rec["seq"], rec["run_id"],
                    rec["record_type"], rec["ts"], rec["payload"],
                )
            except (ValueError, TypeError) as exc:   # e.g. NaN in a payload
                return False, {"reason": "log_invalid", "seq": rec["seq"],
                               "error": f"record cannot be hashed: {exc}"}
            if recomputed != rec["hash"]:
                return False, {"reason": "hash_mismatch", "seq": rec["seq"],
                               "expected": recomputed, "found": rec["hash"]}

            prev_hash = rec["hash"]
            expected_seq += 1
            count += 1

    return True, {"records": count, "head_hash": prev_hash}


def manifest_prefix_problem(path: str | os.PathLike, manifest: dict[str, Any]) -> str | None:
    """Why an internally valid log cannot be the log ``manifest`` was written for, or None. The manifest names a
    record count and the hash of the last record of that prefix: a log with fewer records, or with a different record
    at that position, has been truncated or rewritten since. A log LONGER than the manifest is fine (a session that
    ended without closing). Call only after ``verify_internal_chain`` passed. Messages carry counts only."""
    n, head = manifest.get("record_count"), manifest.get("head_hash")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or not isinstance(head, str):
        return "the manifest's record_count / head_hash are not usable"
    if n == 0:
        return None
    count = 0
    found = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if count == n - 1:
                found = json.loads(line)["hash"]
            count += 1
    if count < n:
        return f"the log has been truncated: its manifest names {n} records and the log holds {count}"
    if found != head:
        return f"the log has been rewritten: record {n - 1} is not the one its manifest names"
    return None


def scan_orphans(path: str | os.PathLike) -> list[tuple[int, str | None]]:
    """(seq, request_id) of call_request records never answered, sorted by seq.

    ``request_id`` is None for a call_request that carries none (it can never be
    paired, so it always counts). Assumes the chain is already known to be well-formed.
    A request_id reused before its predecessor was answered orphans the earlier request.
    """
    open_reqs: dict[str, int] = {}
    orphans: list[tuple[int, str | None]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            payload = rec["payload"]
            rid = payload.get("request_id") if isinstance(payload, dict) else None
            if not isinstance(rid, str) or not rid:
                if rec["record_type"] == "call_request":
                    orphans.append((rec["seq"], None))
                continue
            if rec["record_type"] == "call_request":
                if rid in open_reqs:
                    orphans.append((open_reqs[rid], rid))
                open_reqs[rid] = rec["seq"]
            elif rec["record_type"] in ("call_response", "call_error"):
                open_reqs.pop(rid, None)
    return sorted(orphans + [(seq, rid) for rid, seq in open_reqs.items()])


def find_orphan_requests(path: str | os.PathLike) -> list[int]:
    """Seqs of call_request records with no later call_response/call_error for their
    request_id (including requests that carry no request_id). See ``scan_orphans``."""
    return [seq for seq, _ in scan_orphans(path)]


def verify_run(jsonl_path: str | os.PathLike, manifest_path: str | os.PathLike | None = None,
               expected_manifest_hash: str | None = None,
               allow_orphans: bool = False) -> tuple[bool, dict[str, Any]]:
    """Manifest-anchored verification. The manifest is REQUIRED.

    ``detail`` always has ``records``, ``head_hash``, ``orphan_requests`` and
    ``errors`` (list of reason strings, all failures found); on failure it also
    has ``reason`` (the first failure) and the fields that explain it.
    ``allow_orphans`` is for crash-recovery inspection only.
    """
    jsonl_path = os.fspath(jsonl_path)
    manifest_path = os.fspath(manifest_path) if manifest_path else manifest_path_for(jsonl_path)
    detail: dict[str, Any] = {"records": 0, "head_hash": None, "orphan_requests": [], "errors": []}

    def fail(reason: str, **extra: Any) -> None:
        detail["errors"].append(reason)
        detail.setdefault("reason", reason)
        for k, v in extra.items():
            detail.setdefault(k, v)

    try:
        manifest = read_manifest(manifest_path)
    except FileNotFoundError:
        manifest = None
        fail("manifest_missing", manifest_path=manifest_path)
    except ValueError as exc:
        manifest = None
        fail("manifest_invalid", manifest_path=manifest_path, error=str(exc))

    chain_ok, chain = verify_internal_chain(jsonl_path)
    if not chain_ok:
        fail(chain["reason"], **{k: v for k, v in chain.items() if k != "reason"})
        return False, detail
    count, head = chain["records"], chain["head_hash"]
    detail["records"], detail["head_hash"] = count, head
    detail["orphan_requests"] = find_orphan_requests(jsonl_path)

    if manifest is not None:
        if count == 0:
            fail("count_zero")
        if manifest.get("record_count") != count:
            fail("count_mismatch", expected=manifest.get("record_count"), found=count)
        if manifest.get("head_hash") != head:
            fail("head_mismatch", expected=manifest.get("head_hash"), found=head)
        manifest_hash = compute_manifest_hash(manifest)
        detail["manifest_hash"] = manifest_hash
        if expected_manifest_hash is not None and manifest_hash != expected_manifest_hash:
            fail("manifest_hash_mismatch", expected=expected_manifest_hash, found=manifest_hash)

    if detail["orphan_requests"] and not allow_orphans:
        fail("orphan_requests")

    return not detail["errors"], detail


# ---------------------------------------------------------------------------------------- work store integrity
# The work store (out_dir/work) holds the items every number comes from. Each write to it is announced in the hash
# chained log first, as a ``work_item`` record carrying the sha256 of the item's canonical JSON, so editing a file
# afterwards (with its request ids left valid) no longer matches the log. Forging the hash means breaking the chain.
# A hash stored inside the item would prove nothing: whoever edits the item recomputes it.

WORK_ITEM = "work_item"
CACHE_PREFIX = "cache:"          # stage of a judge cache entry: "cache:<file stem>", name = the entry's key


def canonical_sha256(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj`` (sorted keys, no whitespace): independent of how a file is formatted."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def log_work_item(log: Any, stage: str, name: str, obj: Any) -> None:
    """Append the ``work_item`` record for ``obj`` as it is about to be written (``None``: the item is removed).
    Called BEFORE the file is written, so a crash in between leaves a logged item whose file is absent or older, which
    the pipeline redoes on resume; never a file the log has not announced."""
    log.append(WORK_ITEM, {"stage": stage, "name": name, "sha256": None if obj is None else canonical_sha256(obj)})


def log_cache_changes(log: Any, stem: str, before: dict | None, after: dict | None) -> None:
    """``work_item`` records for every cache entry that differs between two states of a judge cache file."""
    before, after = before or {}, after or {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key) or (key in before) != (key in after):
            log_work_item(log, CACHE_PREFIX + stem, key, after.get(key) if key in after else None)


def _json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _UNREADABLE


_UNREADABLE = object()


def work_integrity_problem(log_path: str | os.PathLike, work_dir: str | os.PathLike,
                           force: bool = False) -> str | None:
    """None, or why the work store does not match the log. Only for a log whose ``run_start`` says the corpus is
    real (a synthetic or unknown log passes). Checks, with the last ``work_item`` record of each (stage, name) as
    the truth: every ``work/<stage>/*.json`` and every judge cache entry was logged and still has the logged hash;
    nothing logged as present is missing; the work store exists at all (D-2); and every call of a role stage records
    a model id (D-3). Messages name the item, never its content.

    ``force=True`` runs the work-store checks on any log, synthetic too (``replay_check`` on a synthetic replay,
    whose replay client logs no model id); D-3 stays a real-run rule. ``force=False`` is the behaviour above."""
    try:
        with open(log_path, encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return None                       # the chain check reports an unreadable log
    real = any(r.get("record_type") == "run_start" and (r.get("payload") or {}).get("corpus_kind") == "real"
               for r in recs)
    if not real and not force:
        return None
    from .. import prereg                 # lazy: prereg imports this module
    for r in recs:
        if real and r.get("record_type") == "call_request":
            req = r.get("payload") or {}
            stage = str(req.get("stage", ""))
            if "model_id" not in req and any(stage.startswith(pre) for pre, _ in prereg.STAGE_ROLES):
                return f"a call ({stage}) records no model id: a real run needs the instrument on record"
    root = Path(work_dir)
    if not root.is_dir():
        return f"work store not found at {root.name}/: provenance not checked"
    last: dict[tuple[str, str], str | None] = {}
    for r in recs:
        if r.get("record_type") == WORK_ITEM:
            pl = r.get("payload") or {}
            last[(str(pl.get("stage")), str(pl.get("name")))] = pl.get("sha256")
    seen: set[tuple[str, str]] = set()
    for sub in sorted(d for d in root.iterdir() if d.is_dir()):
        stage = stage_from_dirname(sub.name)         # the log names the stage key; on Windows the folder has no colon
        for f in sorted(sub.glob("*.json")):
            key = (stage, f.stem)
            seen.add(key)
            obj = _json_file(f)
            if obj is _UNREADABLE:
                return f"work item {stage}/{f.stem} is not readable"
            if key not in last:
                return f"work item {stage}/{f.stem} is not in the log (left over or added by hand)"
            if last[key] is None:
                return f"work item {stage}/{f.stem} was logged as removed but is present"
            if canonical_sha256(obj) != last[key]:
                return f"work item {stage}/{f.stem} changed after it was logged"
    for f in sorted(root.glob("*cache*.json")):
        data = _json_file(f)
        if not isinstance(data, dict):
            return f"judge cache {f.name} is not readable"
        stage = CACHE_PREFIX + f.stem
        for k, v in data.items():
            seen.add((stage, k))
            if (stage, k) not in last:
                return f"judge cache {f.name} holds an entry that is not in the log"
            if last[(stage, k)] is None or canonical_sha256(v) != last[(stage, k)]:
                return f"judge cache {f.name} holds an entry that changed after it was logged"
    for (stage, name), sha in sorted(last.items()):
        if sha is not None and (stage, name) not in seen:
            what = "judge cache entry" if stage.startswith(CACHE_PREFIX) else "work item"
            return f"{what} {stage.removeprefix(CACHE_PREFIX)}/{name[:16]} is in the log but missing from the work store"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    expected_manifest_hash = None
    allow_orphans = False
    if "--allow-orphans" in argv:
        argv.remove("--allow-orphans")
        allow_orphans = True
    if "--manifest-hash" in argv:
        i = argv.index("--manifest-hash")
        try:
            expected_manifest_hash = argv[i + 1]
        except IndexError:
            print("usage: --manifest-hash needs a value", file=sys.stderr)
            return 2
        del argv[i:i + 2]

    if not (1 <= len(argv) <= 2):
        print("usage: python -m second_scribe.audit.verify RUN.jsonl [MANIFEST] "
              "[--manifest-hash HASH] [--allow-orphans]", file=sys.stderr)
        return 2

    jsonl_path = argv[0]
    manifest_path = argv[1] if len(argv) == 2 else None
    if not os.path.isfile(jsonl_path):
        print(f"FAIL: {json.dumps({'reason': 'not_found', 'path': jsonl_path})}")
        return 2
    ok, detail = verify_run(jsonl_path, manifest_path, expected_manifest_hash, allow_orphans)
    print(f"{'PASS' if ok else 'FAIL'}: {json.dumps(detail)}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
