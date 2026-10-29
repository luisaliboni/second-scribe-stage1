"""The researcher's project folder (Task 32; spec section 4, section 6.5 step 1; Review Focus 5).

One folder per project, which the app creates and owns::

    <project>/
      second-scribe-project.json   settings (``schema: project/1``); every path in it is relative to the folder
      inputs/transcripts/  inputs/maps/  reference/  coding/  runs/  views/  exports/
      logs/app.jsonl (+ app.manifest.json)   the app log (``audit.applog.AppLog``), re-closed after every append
      private/                     the file-name <-> participant-id map, the file names as imported (Task 33,
                                   privacy ruling F) and the speaker-label standard (item 27, its rules hold parts
                                   of file names);
                                   never exported. Nothing outside it holds a file's name.

Rules this module keeps for every later task:

- **Relative paths only.** ``Project.path(rel)`` is the one way from a stored or browser-sent path to the disk. It
  refuses anything that is not a plain relative POSIX path (no ``..``, ``.``, empty segment, backslash, drive,
  ``~``, control character) and anything that resolves outside the folder (a link out included), with
  ``ProjectError("path_outside")``. ``save()`` refuses a ``files`` key that would not pass, so a moved or copied
  folder reopens with every path intact (the app log holds no path at all).
- **Sync folders.** A folder inside iCloud Drive, OneDrive, Dropbox or Google Drive would send participant text
  out. ``sync_folder_kind`` names the kind; ``Project.blocked`` is that kind until the researcher acknowledges it,
  which ``ack_sync()`` logs as ``sync_folder_ack{kind}``. The logged record is the acknowledgement: reopening keeps
  it, moving into another kind of sync folder asks again. Route handlers call ``routes.project.require_project``.
- **Change detection.** ``record_file(rel, **meta)`` hashes an input as it is stored; ``changed_files()`` re-hashes
  every stored file and names those that differ or are gone ("this file changed since you imported it").
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from ..audit.applog import AppLog

SCHEMA = "project/1"
PROJECT_FILE = "second-scribe-project.json"
APP_LOG = "logs/app.jsonl"
LAYOUT = ("inputs/transcripts", "inputs/maps", "reference", "coding", "logs", "runs", "views", "exports",
          "private")
DEFAULTS: dict[str, Any] = {"context": {}, "files": {}, "maps": {}, "references": {}, "models": {}, "runs": []}
# Files a file manager drops into a folder on its own; a folder holding only these counts as empty.
FINDER_FILES = frozenset({".DS_Store", "desktop.ini", "Thumbs.db", ".localized"})
SYNC_KINDS = ("icloud", "cloudstorage", "dropbox", "onedrive", "google_drive", "box", "pcloud", "nextcloud", "mega",
              "sync_com", "sharepoint")
# Whole folder names (case-folded) the sync clients create. A name matches only as a complete path component, so
# "boxes", "Megan" or "my-sync-notes" do not. ``(`` / `` - `` prefixes cover "Box (Work)", "SharePoint - Lab".
_NAMED_KINDS = (
    ("box", frozenset({"box", "box sync"}), ("box (", "box - ")),
    ("pcloud", frozenset({"pcloud", "pcloud drive", "pclouddrive", "pcloudsync", "pcloud sync"}), ()),
    ("nextcloud", frozenset({"nextcloud"}), ("nextcloud (",)),
    ("mega", frozenset({"mega", "megasync", "mega sync"}), ()),
    ("sharepoint", frozenset({"sharepoint"}), ("sharepoint - ", "sharepoint (")),
)

MAX_PATH = 4096
MAX_NAME = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROJECT_ID = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ProjectError(Exception):
    """A refusal about the project folder. ``code`` is stable (the route's error code and the strings key
    ``project.error.<code>``); ``detail`` is for the terminal and tests, never shown in the browser."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- sync folders -----------------------------------------------------------------------------------------------

def _segments(path: str | os.PathLike) -> list[str]:
    """Case-folded path segments, splitting on both / and \\ (so Windows forms are read on any OS)."""
    return [s.casefold() for s in PureWindowsPath(os.fspath(path)).parts if s not in ("\\", "/")]


def _kind_of_segments(segs: list[str]) -> str | None:
    for i, s in enumerate(segs):
        nxt = segs[i + 1] if i + 1 < len(segs) else ""
        if s == "library" and nxt == "mobile documents":
            return "icloud"
        if s in ("com~apple~clouddocs", "iclouddrive", "icloud drive"):
            return "icloud"
        if s == "library" and nxt == "cloudstorage" and i + 2 < len(segs):
            return "cloudstorage"
    for s in segs:
        if s == "dropbox" or s.startswith("dropbox ("):
            return "dropbox"
        if s.startswith("onedrive"):
            return "onedrive"
        if s in ("google drive", "my drive", "googledrive"):
            return "google_drive"
        for kind, names, prefixes in _NAMED_KINDS:
            if s in names or s.startswith(prefixes):
                return kind
    # Sync.com puts a folder named exactly "Sync" directly in the home folder: /Users/<u>/Sync, /home/<u>/Sync,
    # C:\Users\<u>\Sync. A folder called "sync" anywhere else is an ordinary folder.
    for i in range(min(2, len(segs))):
        if segs[i] in ("users", "home") and i + 2 < len(segs) and segs[i + 2] == "sync":
            return "sync_com"
    return None


def _icloud_desktop_documents(path: Path, home: Path) -> bool:
    """macOS "Desktop & Documents Folders": ~/Desktop and ~/Documents sync to iCloud, which then holds folders of
    the same names. The path itself says nothing, so this is the one check that looks at the disk."""
    cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    for top in ("Desktop", "Documents"):
        base = home / top
        try:
            inside = path == base or path.is_relative_to(base)
        except ValueError:
            inside = False
        if inside and (cloud / top).is_dir():
            return True
    return False


def sync_folder_kind(path: str | os.PathLike, home: Path | None = None) -> str | None:
    """The kind of sync folder ``path`` is in, or None.

    - ``"icloud"``: under ``Library/Mobile Documents`` or a ``com~apple~CloudDocs`` / ``iCloudDrive`` segment, or
      under ``~/Desktop`` / ``~/Documents`` when iCloud's Desktop & Documents sync is on;
    - ``"cloudstorage"``: under ``Library/CloudStorage/<provider>`` (macOS: OneDrive, Google Drive, Dropbox, ...);
    - ``"dropbox"`` / ``"onedrive"`` / ``"google_drive"``: a segment ``Dropbox`` or ``Dropbox (...)``,
      ``OneDrive*``, ``Google Drive`` or ``My Drive``, on any OS (case-insensitive);
    - ``"box"`` / ``"pcloud"`` / ``"nextcloud"`` / ``"mega"`` / ``"sharepoint"``: a segment that is exactly one of the
      names those clients create (see ``_NAMED_KINDS``); ``"sync_com"``: a folder named exactly ``Sync`` directly in
      the home folder.

    This is a best-effort check on folder names, not a guarantee: a service it does not know, or a folder renamed
    by the user, is not found. The researcher confirms the folder is not synced.

    Both the path as given and, when it exists on this machine, its resolved form are checked, so a link into a
    sync folder is caught. ``home`` defaults to the user's home folder."""
    raw = os.fspath(path)
    kind = _kind_of_segments(_segments(raw))
    if kind:
        return kind
    if isinstance(path, PurePath) and not isinstance(path, Path):
        return None                                  # a foreign-OS path: nothing on this disk to look at
    if "\\" in raw and os.sep != "\\":
        return None
    local = Path(raw).expanduser()
    try:
        resolved = local.resolve()
    except (OSError, RuntimeError):
        resolved = local
    kind = _kind_of_segments(_segments(resolved))
    if kind:
        return kind
    home = Path(home) if home is not None else Path.home()
    try:
        home_resolved = home.resolve()
    except (OSError, RuntimeError):
        home_resolved = home
    for p, h in ((local, home), (resolved, home_resolved)):
        if _icloud_desktop_documents(p, h):
            return "icloud"
        try:
            rel = p.relative_to(h)
        except ValueError:
            continue
        if rel.parts and rel.parts[0].casefold() == "sync":
            return "sync_com"
    return None


# --- paths ------------------------------------------------------------------------------------------------------

def user_folder(raw: Any) -> Path:
    """A folder path typed by the user (create/open): a string, absolute after ``~`` expansion, one line, at most
    ``MAX_PATH`` characters. ``ProjectError("bad_path")`` otherwise. Links are kept as given (not resolved)."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PATH or _CONTROL.search(raw):
        raise ProjectError("bad_path", "a folder path is one line of text")
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        raise ProjectError("bad_path", "a folder path must be absolute")
    return Path(os.path.normpath(p))


def check_rel(rel: Any) -> str:
    """``rel`` when it is a plain relative POSIX path inside a folder, else ``ProjectError("path_outside")``.
    Purely textual; ``Project.path`` adds the on-disk check."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH or _CONTROL.search(rel):
        raise ProjectError("path_outside", "not a relative path")
    if "\\" in rel or rel.startswith(("/", "~")) or _DRIVE.match(rel):
        raise ProjectError("path_outside", "not a relative path")
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        raise ProjectError("path_outside", "a path segment is empty, . or ..")
    return rel


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or _CONTROL.search(name):
        raise ProjectError("bad_name", "a project name is one line of 1 to 200 characters")
    return name.strip()


# --- the project ------------------------------------------------------------------------------------------------

class Project:
    """An open project folder. ``data`` is the parsed ``second-scribe-project.json`` (change it, then ``save()``);
    ``log`` is the project's ``AppLog``; ``root`` is the folder as the user gave it."""

    def __init__(self, root: Path, data: dict) -> None:
        self.root = Path(root)
        self.data = data
        self.log = AppLog(self.root / APP_LOG, data["project_id"])
        self.sync_kind = sync_folder_kind(self.root)

    # -- create / open ----------------------------------------------------------------------------------------
    @classmethod
    def create(cls, root: Path, name: str) -> "Project":
        """Make the section 4 layout in ``root`` (a new folder in an existing one, or an empty folder), write the
        project file and log ``project_created``."""
        root = Path(root)
        name = _check_name(name)
        if not root.is_absolute():
            raise ProjectError("bad_path", "a folder path must be absolute")
        if (root / PROJECT_FILE).exists():
            raise ProjectError("project_exists")
        if root.exists() or root.is_symlink():
            if not root.is_dir():
                raise ProjectError("not_a_folder")
            if any(c.name not in FINDER_FILES for c in root.iterdir()):
                raise ProjectError("folder_not_empty")
        elif not root.parent.is_dir():
            raise ProjectError("parent_missing")
        data = {"schema": SCHEMA, "project_id": uuid.uuid4().hex, "name": name,
                **json.loads(json.dumps(DEFAULTS))}
        root.mkdir(exist_ok=True)
        for rel in LAYOUT:
            (root / rel).mkdir(parents=True, exist_ok=True)
        project = cls(root, data)
        project.save()
        project.log.append("project_created", {"schema": SCHEMA, "layout": list(LAYOUT)})
        return project

    @classmethod
    def open(cls, root: Path) -> "Project":
        """Open an existing project folder (``not_a_folder`` for a file, ``not_a_project`` / ``bad_project_file`` otherwise). Missing layout
        folders are made again. A project made before privacy ruling F gives its file names up to ``private/``
        (``importer.migrate_file_names``, which rewrites this file); nothing else is written."""
        root = Path(root)
        f = root / PROJECT_FILE
        if not f.is_file():
            if root.exists() and not root.is_dir():
                raise ProjectError("not_a_folder")          # the path is a file, not a folder
            raise ProjectError("not_a_project")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise ProjectError("bad_project_file", type(e).__name__) from None
        _validate(data)
        for rel in LAYOUT:
            (root / rel).mkdir(parents=True, exist_ok=True)
        project = cls(root, data)
        from .importer import migrate_file_names        # here: the importer imports this module
        migrate_file_names(project)
        return project

    @classmethod
    def open_or_create(cls, root: Path, name: str | None = None) -> "Project":
        """``open`` when ``root`` holds a project, else ``create`` (named after the folder unless ``name``)."""
        root = Path(root)
        if (root / PROJECT_FILE).exists():
            return cls.open(root)
        return cls.create(root, name if name is not None else root.name)

    # -- fields -----------------------------------------------------------------------------------------------
    @property
    def project_id(self) -> str:
        return self.data["project_id"]

    @property
    def name(self) -> str:
        return self.data["name"]

    # -- saving -----------------------------------------------------------------------------------------------
    def save(self) -> None:
        """Write the project file atomically (temp file in the folder, fsync, ``os.replace``)."""
        _validate(self.data)
        text = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.root / PROJECT_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- paths ------------------------------------------------------------------------------------------------
    def path(self, rel: Any) -> Path:
        """The file or folder at ``rel`` inside the project; ``ProjectError("path_outside")`` when ``rel`` is not
        a plain relative path or resolves outside the folder (through a link, say)."""
        rel = check_rel(rel)
        p = self.root.joinpath(*rel.split("/"))
        try:
            inside = p.resolve().is_relative_to(self.root.resolve())
        except (OSError, RuntimeError):
            inside = False
        if not inside:
            raise ProjectError("path_outside", "resolves outside the project folder")
        return p

    def rel(self, path: Path) -> str:
        """The relative POSIX path of ``path`` inside the project (``path_outside`` otherwise)."""
        try:
            rel = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            raise ProjectError("path_outside", "not inside the project folder") from None
        self.path(rel)
        return rel

    # -- files ------------------------------------------------------------------------------------------------
    def record_file(self, rel: str, **meta: Any) -> dict:
        """Hash the file at ``rel`` and store ``files[rel] = {"sha256", "bytes", **meta}`` (call ``save()``)."""
        p = self.path(rel)
        entry = {**meta, "sha256": file_sha256(p), "bytes": p.stat().st_size}
        self.data["files"][rel] = entry
        return entry

    def changed_files(self) -> list[str]:
        """Each stored file whose sha256 now differs from ``files[rel].sha256``, or that is gone; sorted."""
        changed = []
        for rel, meta in sorted(self.data["files"].items()):
            try:
                p = self.path(rel)
                same = p.is_file() and file_sha256(p) == meta.get("sha256")
            except (ProjectError, OSError):
                same = False
            if not same:
                changed.append(rel)
        return changed

    # -- sync folder ------------------------------------------------------------------------------------------
    @property
    def sync_acknowledged(self) -> bool:
        if not self.sync_kind:
            return False
        return any((r.get("payload") or {}).get("kind") == self.sync_kind
                   for r in self.log.records("sync_folder_ack"))

    @property
    def blocked(self) -> str | None:
        """The sync-folder kind while it is not acknowledged; None when the project may be used."""
        return self.sync_kind if self.sync_kind and not self.sync_acknowledged else None

    def ack_sync(self) -> dict:
        """Log ``sync_folder_ack{kind}``; ``no_sync_warning`` when there is nothing to acknowledge."""
        if not self.blocked:
            raise ProjectError("no_sync_warning")
        return self.log.append("sync_folder_ack", {"kind": self.sync_kind})

    # -- summary ----------------------------------------------------------------------------------------------
    def summary(self) -> dict:
        """What ``GET /api/project`` shows: names, hashes and counts, never file contents."""
        records, head = self.log.head()
        ok, _ = self.log.verify()
        d = self.data
        return {
            "schema": d["schema"], "project_id": d["project_id"], "name": d["name"], "root": str(self.root),
            "sync": {"kind": self.sync_kind, "acknowledged": self.sync_acknowledged},
            "blocked": self.blocked is not None,
            "files": {rel: {k: v for k, v in meta.items() if k in ("sha256", "bytes", "kind")}
                      for rel, meta in sorted(d["files"].items())},
            "changed_files": self.changed_files(),
            "counts": {"files": len(d["files"]), "maps": len(d["maps"]), "references": len(d["references"]),
                       "runs": len(d["runs"])},
            "log": {"records": records, "head": head, "ok": bool(ok)},
        }


def _validate(data: Any) -> None:
    """``ProjectError("bad_project_file")`` unless ``data`` is a ``project/1`` document whose file keys are
    relative paths with a 64-hex sha256."""
    def bad(why: str) -> ProjectError:
        return ProjectError("bad_project_file", why)

    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise bad("not a project/1 file")
    if not isinstance(data.get("project_id"), str) or not _PROJECT_ID.match(data["project_id"]):
        raise bad("project_id")
    if not isinstance(data.get("name"), str):
        raise bad("name")
    for key, empty in DEFAULTS.items():
        if not isinstance(data.get(key), type(empty)):
            raise bad(key)
    for rel, meta in data["files"].items():
        try:
            check_rel(rel)
        except ProjectError:
            raise ProjectError("path_outside", "a stored file path is not relative") from None
        if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str) or not _HEX64.match(meta["sha256"]):
            raise bad("files entry")
