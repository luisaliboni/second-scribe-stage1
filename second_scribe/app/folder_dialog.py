"""The system's own folder window for the Setup screen's "Choose folder…" button (item 22, 23 Oct).

A browser never gives a page the full path of a folder, so the app, which runs on the same computer, opens the
operating system's folder window itself and hands back the path the person picked. The path only fills the folder
field; nothing is created or opened until the person presses Create or Open.

- ``command()``: the fixed argument list for this system, or ``None`` when there is no folder window to open:
  macOS ``osascript`` (choose folder), Windows PowerShell (FolderBrowserDialog), Linux ``zenity`` or ``kdialog`` when on
  PATH and a display is set. The window's title comes from the app's strings, passed as a program argument (macOS,
  Linux) or in an environment variable (Windows), never spliced into a script. No shell, no text from the request.
- ``choose(run=subprocess.run)``: opens the window once, waits at most ``TIMEOUT`` seconds, and returns the chosen
  folder as an absolute path, or ``None`` when the person cancelled. ``DialogError`` for a window that could not open,
  timed out, or answered something that is not an existing folder. Tests pass ``run`` and never open a real window.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

TIMEOUT = 120                      # seconds the window may stay open
MAX_OUT = 4096                     # a path longer than this is not a folder the app will use
PROMPT_ENV = "SS_FOLDER_PROMPT"    # Windows: the window's title, read by the fixed script

# macOS: the title is the script's first argument; "activate" brings the window in front of the browser.
_MAC = ["-e", "on run argv", "-e", "activate",
        "-e", "return POSIX path of (choose folder with prompt (item 1 of argv))", "-e", "end run"]
# Windows: a fixed script; the title arrives in the environment, the path leaves on stdout as UTF-8.
_WIN = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description = $env:" + PROMPT_ENV + "; $d.ShowNewFolderButton = $true; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }")


class DialogError(Exception):
    """The folder window could not give a folder: ``code`` is ``failed`` or ``timeout``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def command(prompt: str, platform: Optional[str] = None, which: Callable[[str], Optional[str]] = shutil.which,
            env: Optional[dict] = None) -> Optional[list[str]]:
    """The argument list that opens this system's folder window titled ``prompt``, or ``None`` if there is none."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform == "darwin":
        exe = which("osascript")
        return [exe, *_MAC, prompt] if exe else None
    if platform.startswith("win"):
        exe = which("powershell") or which("pwsh")
        return [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WIN] if exe else None
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return None
    exe = which("zenity")
    if exe:
        return [exe, "--file-selection", "--directory", "--title", prompt]
    exe = which("kdialog")
    if exe:
        return [exe, "--getexistingdirectory", os.path.expanduser("~"), "--title", prompt]
    return None


def available(prompt: str = "", **kw) -> bool:
    """True when this computer has a folder window the app can open."""
    return command(prompt or "x", **kw) is not None


def _cancelled(argv: list[str], code: int, err: str) -> bool:
    name = os.path.basename(argv[0]).lower()
    if name.startswith("osascript"):
        return "(-128)" in err                      # "User canceled. (-128)"
    if name.startswith(("zenity", "kdialog")):
        return code == 1                             # both answer 1 for Cancel
    return False


def choose(prompt: str, run: Callable = subprocess.run, **kw) -> Optional[str]:
    """Open the folder window once; the chosen folder's absolute path, or ``None`` when the person cancelled."""
    argv = command(prompt, **kw)
    if argv is None:
        raise DialogError("failed", "no folder window on this computer")
    env = dict(os.environ)
    env[PROMPT_ENV] = prompt
    try:
        done = run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, shell=False, env=env,
                   check=False)
    except subprocess.TimeoutExpired:
        raise DialogError("timeout") from None
    except OSError as e:
        raise DialogError("failed", type(e).__name__) from None
    out = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else str(done.stdout or "")
    err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else str(done.stderr or "")
    if done.returncode != 0:
        if _cancelled(argv, done.returncode, err):
            return None
        raise DialogError("failed", f"exit {done.returncode}")
    path = out.strip("\r\n")
    if not path:
        return None                                   # Windows: Cancel prints nothing
    if len(path) > MAX_OUT or "\n" in path or "\r" in path or "\x00" in path:
        raise DialogError("failed", "not one path")
    if len(path) > 1:
        path = path.rstrip("/\\") or path              # macOS ends a folder with "/"
        if len(path) == 2 and path[1] == ":":
            path += "\\"                               # keep "C:\" a folder
    if not os.path.isabs(path) or not os.path.isdir(path):
        raise DialogError("failed", "not an existing folder")
    return path
