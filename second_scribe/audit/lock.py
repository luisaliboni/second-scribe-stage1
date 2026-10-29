"""One exclusive lock around an append to the audit log, on POSIX and on Windows.

POSIX: ``fcntl.flock`` on the log file, released on exit (the behaviour the registry always had).

Windows has no ``fcntl``. There the lock is a byte lock (``msvcrt.locking``) on byte 0 of a sidecar file, the log's name
plus ``.lock``, never on the log itself, so a reader (``head()``, verify) is not blocked. ``LK_NBLCK`` does not wait, so
the lock is retried every 50 ms for up to 120 s and then refused with a RuntimeError.
"""
import contextlib
import os
import time

try:
    import fcntl
except ImportError:      # Windows
    fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:      # POSIX; tests set this to a fake to take the Windows branch
    _msvcrt = None

RETRY_SECONDS = 0.05
TIMEOUT_SECONDS = 120.0
LOCKED_MESSAGE = "audit log is locked by another process"


@contextlib.contextmanager
def exclusive(fh):
    """Hold the audit log's exclusive lock for the ``with`` body. ``fh`` is the open log file."""
    if _msvcrt is None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return
    with open(os.fspath(fh.name) + ".lock", "a+b") as side:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while True:
            side.seek(0)
            try:
                _msvcrt.locking(side.fileno(), _msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(LOCKED_MESSAGE) from None
                time.sleep(RETRY_SECONDS)
        try:
            yield
        finally:
            side.seek(0)
            _msvcrt.locking(side.fileno(), _msvcrt.LK_UNLCK, 1)
