"""Run manifest: the external anchor for the hash chain.

A genesis-forward hash chain is only tamper-evident relative to a trusted head.
On its own it cannot detect end-truncation, an emptied file, or a full forward
rewrite. The manifest closes that gap: at close it records the chain's head hash
and record count, plus a link to the previous run's manifest. The manifest's own
SHA-256 is recorded somewhere outside the run directory (a lab notebook, an OSF
deposit); that external record is the trust anchor.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .registry import canonical, sha256_hex

# The exact fields the manifest hash is computed over (hashed via canonical(), which
# sorts keys). No self-hash field is stored inside.
MANIFEST_FIELDS = ("run_id", "head_hash", "record_count", "closed_at_iso",
                   "prev_manifest_hash")


def manifest_path_for(jsonl_path: str | os.PathLike) -> str:
    """Deterministic sibling manifest path: run.jsonl -> run.manifest.json."""
    return os.path.splitext(os.fspath(jsonl_path))[0] + ".manifest.json"


def compute_manifest_hash(manifest: dict[str, Any]) -> str:
    """SHA-256 over the canonical form of the manifest's anchored fields."""
    core = {k: manifest[k] for k in MANIFEST_FIELDS}
    return sha256_hex(canonical(core))


def write_manifest(jsonl_path: str | os.PathLike, run_id: str, head_hash: str,
                   record_count: int, prev_manifest_hash: str | None,
                   closed_at_iso: str) -> tuple[str, str]:
    """Write the manifest next to the JSONL (temp file, fsync, rename). Returns (manifest_path, manifest_hash)."""
    manifest = {
        "run_id": run_id,
        "head_hash": head_hash,
        "record_count": record_count,
        "closed_at_iso": closed_at_iso,
        "prev_manifest_hash": prev_manifest_hash,
    }
    path = manifest_path_for(jsonl_path)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(canonical(manifest) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)         # atomic: a crash leaves the old manifest or the new one, never a torn file
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:                              # make the rename itself durable
        dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    return path, compute_manifest_hash(manifest)


def read_manifest(manifest_path: str | os.PathLike) -> dict[str, Any]:
    """Read and validate a manifest. FileNotFoundError if absent; ``ValueError`` if it is
    not UTF-8, not JSON, not an object, lacks an anchored field, or cannot be hashed."""
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)          # JSONDecodeError and UnicodeDecodeError are ValueErrors
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not a JSON object")
    missing = [f for f in MANIFEST_FIELDS if f not in manifest]
    if missing:
        raise ValueError(f"manifest is missing fields: {', '.join(missing)}")
    compute_manifest_hash(manifest)       # e.g. non-finite numbers raise ValueError here
    return manifest
