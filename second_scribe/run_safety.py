"""Local output containment and exclusive ownership of a CLI run folder."""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
from pathlib import Path


class RunRefused(ValueError):
    """A run cannot safely write to this output folder."""


OUTPUT_NAMES = ("logs", "work", "aggregates", "audits", "stale",
                "report.md", "report.STALE", "aggregates.json", "traps.md")


def clear_demo_outputs(out_dir):
    """Reset generated demo artifacts while the caller holds ``run_lock``.

    The guard inode, lock metadata and unrelated user files stay in place.
    Symlinks are unlinked, never traversed while deleting.
    """
    root = check_output_paths(out_dir)
    for name in OUTPUT_NAMES:
        path = root / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def check_output_paths(out_dir) -> Path:
    """Refuse escaping output links before reading a log or creating run files."""
    root = Path(out_dir).resolve()

    def check(path):
        try:
            inside = path.resolve().is_relative_to(root)
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            raise RunRefused("an output path resolves outside the output folder; remove the symbolic link")

    for name in OUTPUT_NAMES + (".run.lock", ".run.guard"):
        path = root / name
        check(path)
        if path.is_dir():
            seen = set()
            for folder, dirs, files in os.walk(path, followlinks=True):
                real = Path(folder).resolve()
                if real in seen:
                    dirs[:] = []
                    continue
                seen.add(real)
                for child in dirs + files:
                    check(Path(folder) / child)
    return root


@contextlib.contextmanager
def run_lock(out_dir, say=None):
    """Own a CLI run until all writes and the manifest close have finished.

    The O_EXCL metadata file names the owner. A permanent, empty guard file holds
    an OS lock, released even on process death. It serializes stale-lock recovery
    too; deleting that guard would allow contenders to lock different inodes.
    """
    from .audit.lock import fcntl, _msvcrt

    root = check_output_paths(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".run.lock"
    with open(root / ".run.guard", "a+b") as guard:
        try:
            if _msvcrt is None:
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                guard.seek(0)
                _msvcrt.locking(guard.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError:
            try:
                pid = json.loads(path.read_text(encoding="utf-8")).get("pid")
            except (OSError, ValueError, AttributeError):
                pid = None
            owner = f" (pid {pid})" if type(pid) is int and pid > 0 else ""
            raise RunRefused(f"another run is using {root}{owner}") from None
        try:
            # No other owner can be alive with this guard held. Recovery and new
            # lock creation are serialized; two contenders cannot steal a lease.
            check_output_paths(root)
            if path.exists():
                path.unlink()
                if say:
                    say("note: recovered an abandoned run lock")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "started_at": datetime.datetime.now(
                        datetime.timezone.utc).isoformat()}, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                yield
            finally:
                path.unlink(missing_ok=True)
        finally:
            if _msvcrt is None:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
            else:
                guard.seek(0)
                _msvcrt.locking(guard.fileno(), _msvcrt.LK_UNLCK, 1)
