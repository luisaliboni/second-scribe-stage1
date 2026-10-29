"""The Export screen's "Open" and "Show in folder" buttons (scout F11, Part 5a, 23 Oct).

A page in the browser cannot open a file on the computer, so the app, which runs on the same computer, hands a saved
export to the system's own opener: the file to its default program ("Open"), or its folder with the file picked
("Show in folder").

- ``command(path, reveal)``: the fixed argument list for this system, or ``None`` when there is no opener: macOS
  ``open`` (``open -R`` to show in the folder), Windows ``explorer`` (``/select,<path>`` to show in the folder), Linux
  ``xdg-open`` when on PATH and a display is set (showing in the folder opens the file's folder). The only text in the
  list besides the fixed words is the absolute path of a file the route has checked is inside the project's
  ``exports`` folder. No shell, no text from the request.
- ``open_path(path, reveal, popen=subprocess.Popen)``: starts the opener once and waits at most ``WAIT`` seconds for it
  to hand over (a desktop opener that is still running then is left alone). ``OpenError`` when there is no opener, or
  it could not start or answered with an error. Tests pass ``popen`` and never open anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

WAIT = 15                                     # seconds the opener may take to hand the file over
# The kinds of file "Open" hands to a default program: the report page, the profile, the methods text, the run record.
OPENABLE = frozenset({".html", ".json", ".csv", ".txt", ".md"})


class OpenError(Exception):
    """The opener could not open the file: ``code`` is ``unavailable`` or ``failed``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def command(path: str, reveal: bool, platform: Optional[str] = None,
            which: Callable[[str], Optional[str]] = shutil.which, env: Optional[dict] = None) -> Optional[list[str]]:
    """The argument list that opens ``path`` (an absolute path) or shows it in its folder, or ``None``."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if not os.path.isabs(path):
        return None
    if platform == "darwin":
        exe = which("open")
        return ([exe, "-R", path] if reveal else [exe, path]) if exe else None
    if platform.startswith("win"):
        exe = which("explorer")
        return ([exe, "/select," + path] if reveal else [exe, path]) if exe else None
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    exe = which("xdg-open")
    if not exe:
        return None
    return [exe, os.path.dirname(path) if reveal and not os.path.isdir(path) else path]


def available(**kw) -> bool:
    """True when this computer has an opener the app can start."""
    return command(os.path.abspath(os.sep), False, **kw) is not None


def open_path(path: str, reveal: bool, popen: Callable = subprocess.Popen, **kw) -> None:
    """Hand ``path`` to the system's opener once. ``OpenError`` when that fails."""
    argv = command(path, reveal, **kw)
    if argv is None:
        raise OpenError("unavailable", "no opener on this computer")
    try:
        proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     shell=False, close_fds=True)
    except OSError as e:
        raise OpenError("failed", type(e).__name__) from None
    try:
        code = proc.wait(timeout=WAIT)
    except subprocess.TimeoutExpired:
        return                                 # still handing over (a desktop that waits on the program): not an error
    # Windows Explorer answers 1 even when it opened the file or folder.
    if code != 0 and not (os.path.basename(argv[0]).lower().startswith("explorer") and code == 1):
        raise OpenError("failed", f"exit {code}")
