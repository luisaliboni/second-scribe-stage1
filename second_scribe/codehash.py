"""The code a run ran, named without git (K6).

``code_sha256()`` is a sha256 over the sorted ``"<relpath> <sha256>"`` lines (newline-joined) of every ``*.py``,
``*.json`` and ``*.txt`` file under ``second_scribe/`` and ``prompts/``, ``__pycache__`` left out: the same
construction as ``prompt_manifest.set_hash``, over the package source instead of the prompt folder. Paths are
relative to the kit root and written with ``/``, so the same files give the same hash in any folder and on any OS.

``git_commit()`` is the commit of the kit's own checkout, or None (no git, not a checkout, a kit unpacked inside
another repository, a git that does not answer in 5 s). Every ``run_start`` records the hash; the commit, when there
is one, is recorded as ``code_commit`` (``prereg.code_state``)."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .prompt_manifest import set_hash

KIT_ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("second_scribe", "prompts")
CODE_SUFFIXES = (".py", ".json", ".txt")
GIT_TIMEOUT_S = 5


def code_files(root: Path | None = None) -> dict[str, str]:
    """{relpath: sha256 of the bytes} of the files ``code_sha256`` covers."""
    root = Path(root if root is not None else KIT_ROOT)
    out: dict[str, str] = {}
    for sub in CODE_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix in CODE_SUFFIXES and "__pycache__" not in p.relative_to(root).parts and p.is_file():
                out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def code_sha256(root: Path | None = None) -> str:
    """sha256 of the package source under ``root`` (default: this kit)."""
    return set_hash(code_files(root))


def git_commit(root: Path | None = None) -> str | None:
    """The full HEAD commit of the git checkout whose top level is ``root`` (default: this kit), else None."""
    root = Path(root if root is not None else KIT_ROOT)
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"], capture_output=True,
                           text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    lines = r.stdout.split("\n") if r.returncode == 0 else []
    if len(lines) < 2:
        return None
    top, head = lines[0].strip(), lines[1].strip()
    try:
        if not top or Path(top).resolve() != root.resolve():
            return None        # the kit sits inside someone else's repository: that commit does not name this code
    except OSError:
        return None
    return head if len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head) else None
