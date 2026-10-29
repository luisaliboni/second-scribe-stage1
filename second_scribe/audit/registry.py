"""Hash-chained, append-only audit log (ported from the ISR harness registry).

One JSON record per line, linked by a SHA-256 chain: each record carries the hash
of the previous one, and the genesis record links to 64 zeros. Any later edit,
deletion or reordering is detectable with ``second_scribe.audit.verify``.

Secrets: ``redact`` runs on every payload, so an API key that strays into a
payload cannot reach the log. Keys are read from environment variables by name
elsewhere; this module never sees them on purpose.

The log holds no state in memory: every append takes an exclusive file lock,
re-reads the tail of the file, and extends the chain from there. Two writers on
the same file therefore cannot fork the chain.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, Callable

from . import lock

GENESIS_HASH = "0" * 64

# Field names that mark a value as secret: any name that CONTAINS one of these (case-insensitive), so
# ``access_token``, ``client_secret`` and ``x-api-key`` are caught. Backstop only: keys are env-var-only
# by design. A short allowlist of known non-secret inference settings is exempt (exact names, "-" read as
# "_"), so ``stop_token`` or ``max_tokens`` stay readable in the log.
SECRET_KEY_SUBSTRINGS = ("key", "token", "secret", "password", "passwd", "auth", "authorization", "bearer",
                         "cookie", "credential")
NON_SECRET_KEYS = frozenset({"stop_token", "stop_tokens", "max_tokens", "max_new_tokens", "top_k", "n_probs",
                             "top_logprobs", "logprobs", "cache_prompt", "keep_alive"})
REDACTED = "***REDACTED***"

# Record types written outside the model-call stream. ``verify`` hashes and chains whatever a record holds, so
# an unregistered type passes it too: RECORD_TYPES is documentation (the one place these types are named), not a
# whitelist that anything enforces.
#   declared_contrasts   a dated note, written before a real rung, of the contrasts, outcomes and draw counts the
#                        analysis will report (ladder_metrics.declare_contrasts). A record, not a lock: nothing
#                        reads it back to allow or refuse a run; an amendment is a new record with ``amends``.
DECLARED_CONTRASTS = "declared_contrasts"
RECORD_TYPES = frozenset({DECLARED_CONTRASTS})


def is_secret_key(name: str) -> bool:
    lower = name.lower()
    if lower.replace("-", "_") in NON_SECRET_KEYS:
        return False
    return any(sub in lower for sub in SECRET_KEY_SUBSTRINGS)


def redact(obj: Any) -> Any:
    """Deep-copy ``obj`` replacing any secret-looking STRING field with REDACTED.

    Only string values are scrubbed: ``max_tokens`` (an int) or ``api_key_present``
    (a bool) are audit metadata the trail must keep, not secrets.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_key(k) and isinstance(v, str):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


class CanonicalizationError(ValueError):
    """Raised when a payload cannot be canonicalised unambiguously."""


def _assert_canonicalizable(obj: Any) -> None:
    """Reject anything that would serialise ambiguously or invalidly.

    - non-string dict keys: numeric keys sort numerically before write but
      lexically after a JSON round-trip, which would forge a false tamper hit;
    - NaN/Infinity: json emits bare tokens a strict auditor rejects, yet Python
      round-trips them, so verification would pass an un-loadable trail.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalizationError(
                    f"non-string dict key {k!r} ({type(k).__name__}) - keys must be str"
                )
            _assert_canonicalizable(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_canonicalizable(v)
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise CanonicalizationError(f"non-finite float {obj!r} is not allowed in a payload")


def canonical(obj: Any) -> str:
    """Deterministic, strict JSON serialisation used as the hashing pre-image."""
    _assert_canonicalizable(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_hash(prev_hash: str, seq: int, run_id: str, record_type: str,
                 ts: str, payload: Any) -> str:
    """SHA-256 over the canonical form of the chained fields.

    ``ts`` and ``run_id`` are inside the pre-image so a record cannot be
    re-timestamped or re-homed to another run without breaking the chain.
    """
    preimage = canonical(
        {
            "prev_hash": prev_hash,
            "seq": seq,
            "run_id": run_id,
            "record_type": record_type,
            "ts": ts,
            "payload": payload,
        }
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _last_line(f) -> bytes | None:
    """Last non-empty line of a binary file object, reading backwards in blocks."""
    pos = f.seek(0, os.SEEK_END)
    buf = b""
    while pos > 0:
        step = min(65536, pos)
        pos -= step
        f.seek(pos)
        buf = f.read(step) + buf
        stripped = buf.rstrip(b"\r\n")
        if b"\n" in stripped or pos == 0:
            return stripped.rsplit(b"\n", 1)[-1] or None
    return None


def _tail_state(f) -> tuple[int, str]:
    """(record_count, head_hash) read from the tail of an open binary file."""
    line = _last_line(f)
    if line is None:
        return 0, GENESIS_HASH
    try:
        rec = json.loads(line.decode("utf-8"))
        return int(rec["seq"]) + 1, str(rec["hash"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("audit log tail is not a valid record; refusing to extend it") from exc


class AuditLog:
    """Append-only hash-chained JSONL writer for one run.

    If the file already has records (a resumed run) the internal chain must verify
    before anything is appended; otherwise ``RuntimeError`` is raised. Requests the
    previous process left unanswered are then closed with an "abandoned" call_error.
    """

    def __init__(self, path: str | os.PathLike, run_id: str,
                 clock: Callable[[], str] | None = None) -> None:
        self.path = os.fspath(path)
        self.run_id = run_id
        self._clock = clock or _utc_now
        if os.path.exists(self.path):
            from .verify import verify_internal_chain  # lazy: verify imports this module

            ok, detail = verify_internal_chain(self.path)
            if not ok:
                raise RuntimeError(f"refusing to resume a broken chain: {detail}")
            from .manifest import manifest_path_for, read_manifest

            if os.path.exists(manifest_path_for(self.path)):
                try:
                    old = read_manifest(manifest_path_for(self.path))
                except ValueError as exc:
                    raise RuntimeError(f"refusing to resume: manifest is invalid: {exc}") from None
                from .verify import manifest_prefix_problem

                if (why := manifest_prefix_problem(self.path, old)):
                    # a truncated or rewritten log must not be re-closed under a fresh manifest
                    raise RuntimeError(f"refusing to resume: {why}")
            self._close_abandoned_requests()

    def _close_abandoned_requests(self) -> None:
        """Crash recovery: a call_request the dead process never answered gets one
        ``call_error`` so the resumed chain verifies without ``allow_orphans``.

        A request that carries no request_id cannot be paired and is left alone
        (verification keeps failing on it, which is the honest outcome).
        """
        from .verify import scan_orphans

        seen: set[str] = set()
        for _seq, rid in scan_orphans(self.path):
            if rid is not None and rid not in seen:
                seen.add(rid)
                self.append("call_error", {"request_id": rid,
                                           "error": "abandoned: process restarted before response"})

    def append(self, record_type: str, payload: Any) -> dict[str, Any]:
        """Redact, hash, and append one record (flock + fsync). Returns the record."""
        safe_payload = redact(payload)
        ts = self._clock()
        with open(self.path, "a+b") as fh:
            with lock.exclusive(fh):
                seq, prev_hash = _tail_state(fh)
                h = compute_hash(prev_hash, seq, self.run_id, record_type, ts, safe_payload)
                record = {
                    "seq": seq,
                    "run_id": self.run_id,
                    "ts": ts,
                    "record_type": record_type,
                    "payload": safe_payload,
                    "prev_hash": prev_hash,
                    "hash": h,
                }
                line = (canonical(record) + "\n").encode("utf-8")  # raises before any write
                end = fh.seek(0, os.SEEK_END)
                if end:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        line = b"\n" + line
                fh.seek(0, os.SEEK_END)
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        return record

    def head(self) -> tuple[int, str]:
        """(record_count, head_hash); (0, GENESIS_HASH) for an empty log.

        ``record_count`` is also the seq the next record will get.
        """
        try:
            with open(self.path, "rb") as fh:
                return _tail_state(fh)
        except FileNotFoundError:
            return 0, GENESIS_HASH

    def close(self, prev_manifest_hash: str | None = None) -> tuple[str, str]:
        """Write the manifest next to the log. Returns (manifest_path, manifest_hash).

        The manifest hash is what you record outside this directory (the trust
        anchor). ``prev_manifest_hash`` defaults to the hash of the manifest this one replaces
        (None for the first close), so the manifests of all sessions form a chain.
        """
        from .manifest import compute_manifest_hash, manifest_path_for, read_manifest, write_manifest

        if prev_manifest_hash is None and os.path.exists(manifest_path_for(self.path)):
            try:       # chain this manifest to the one it replaces, so every session stays on record
                prev_manifest_hash = compute_manifest_hash(read_manifest(manifest_path_for(self.path)))
            except ValueError:
                pass
        count, head = self.head()
        return write_manifest(self.path, self.run_id, head, count,
                              prev_manifest_hash=prev_manifest_hash,
                              closed_at_iso=self._clock())
