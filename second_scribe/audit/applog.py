"""The app log (K7): a hash-chained record of what the researcher did in the app (imports, confirmations, opt-ins,
declarations, exports), kept per project in ``logs/app.jsonl``.

It is the kit's own ``AuditLog`` chain with two additions: only the app's record types are accepted
(``APP_RECORD_TYPES``), and the manifest is re-closed after every append, so ``logs/app.manifest.json`` always
names the current head and ``verify_run`` passes at any moment, not only at the end of a session. Each re-close
chains to the manifest it replaces (``prev_manifest_hash``).

A run the app starts carries the log's length and head at that moment in its ``run_start`` record
(``tool.app_log_head``), which ties the run to the app actions that set it up.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

from .manifest import compute_manifest_hash, manifest_path_for, read_manifest
from .registry import GENESIS_HASH, AuditLog, _tail_state
from .verify import manifest_prefix_problem, verify_internal_chain, verify_run

APP_RECORD_TYPES = ("project_created", "import", "removed", "labels_confirmed", "missing_rule", "map_confirmed", "context_set",
                    "reference_set", "sync_folder_ack", "optin", "declaration", "model_check", "coding_locked",
                    "reveal", "run_started", "run_linked", "export")


class AppLog:
    """Append-only, hash-chained log of the app's actions for one project; thread-safe within a process."""

    def __init__(self, path: str | os.PathLike, project_id: str,
                 clock: Callable[[], str] | None = None) -> None:
        self.path = os.fspath(path)
        self.project_id = project_id
        self._clock = clock
        self._lock = threading.Lock()
        self._log: AuditLog | None = None    # opened on the first append: verify() still works on a broken log

    def _audit_log(self) -> AuditLog:
        if self._log is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self._log = AuditLog(self.path, self.project_id, clock=self._clock)   # refuses a broken chain
        return self._log

    def append(self, record_type: str, payload: dict) -> dict[str, Any]:
        """Append one record and re-close the manifest. ``ValueError`` for a type not in ``APP_RECORD_TYPES``,
        ``TypeError`` for a payload that is not a dict, ``RuntimeError`` when the log on disk no longer verifies."""
        if record_type not in APP_RECORD_TYPES:
            raise ValueError(f"not an app log record type: {record_type!r}")
        if not isinstance(payload, dict):
            raise TypeError("an app log payload must be a dict")
        with self._lock:
            # checked before EVERY append, not only when the log is opened: re-closing the manifest over a log that
            # was edited, truncated or emptied in the meantime would anchor the damage under a fresh manifest
            why, manifest_hash = self._extend_problem()
            if why:
                raise RuntimeError(f"refusing to extend the app log: {why}")
            log = self._audit_log()
            record = log.append(record_type, payload)
            log.close(prev_manifest_hash=manifest_hash)
        return record

    def _extend_problem(self) -> tuple[str | None, str | None]:
        """(why the log on disk must not be extended or None, hash of the current manifest or None)."""
        mpath = manifest_path_for(self.path)
        manifest = None
        if os.path.exists(mpath):
            try:
                manifest = read_manifest(mpath)
            except ValueError:
                return "its manifest is invalid", None
        if not os.path.exists(self.path):
            return ("the log is missing but its manifest exists" if manifest is not None else None), None
        ok, detail = verify_internal_chain(self.path)
        if not ok:
            return f"the chain does not verify ({detail.get('reason')})", None
        if manifest is not None and (why := manifest_prefix_problem(self.path, manifest)):
            return why, None
        return None, None if manifest is None else compute_manifest_hash(manifest)

    def head(self) -> tuple[int, str]:
        """(record count, head hash); (0, 64 zeros) when nothing has been logged yet."""
        try:
            with open(self.path, "rb") as fh:
                return _tail_state(fh)
        except FileNotFoundError:
            return 0, GENESIS_HASH

    def verify(self) -> tuple[bool, dict[str, Any]]:
        """``verify_run`` on the log and its manifest; a log that does not exist yet is ``(False, log_missing)``."""
        if not os.path.exists(self.path):
            return False, {"records": 0, "head_hash": None, "orphan_requests": [], "errors": ["log_missing"],
                           "reason": "log_missing"}
        return verify_run(self.path)

    def records(self, record_type: str | None = None) -> list[dict[str, Any]]:
        """Every record in file order, or only those of ``record_type``."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        return recs if record_type is None else [r for r in recs if r.get("record_type") == record_type]
