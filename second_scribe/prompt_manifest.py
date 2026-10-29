"""Frozen prompt set: ``prompts/MANIFEST.json`` = sha256 of every prompt file plus one hash
over the whole set, so an edit to any wording is visible (digest 3.3.2: write the wordings
BEFORE seeing results, freeze them, hash them together).

Regenerate after a deliberate change with ``python3 -m second_scribe.prompt_manifest``.
A change to a frozen file is a new prompt version, not an edit.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "MANIFEST.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hashes(prompts_dir) -> dict[str, str]:
    """{file name: sha256} for every regular file in ``prompts_dir`` except the manifest and dotfiles
    (``.DS_Store`` and the like), sorted by name."""
    d = Path(prompts_dir)
    return {p.name: _sha256_bytes(p.read_bytes()) for p in sorted(d.iterdir())
            if p.is_file() and p.name != MANIFEST_NAME and not p.name.startswith(".")}


def set_hash(files: dict[str, str]) -> str:
    """sha256 over the sorted ``"<name> <sha256>"`` lines (one per file, newline-joined)."""
    return _sha256_bytes("\n".join(f"{n} {files[n]}" for n in sorted(files)).encode("utf-8"))


def build_manifest(prompts_dir) -> dict:
    files = file_hashes(prompts_dir)
    return {"files": files, "set_sha256": set_hash(files)}


def write_manifest(prompts_dir) -> dict:
    m = build_manifest(prompts_dir)
    (Path(prompts_dir) / MANIFEST_NAME).write_text(json.dumps(m, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return m


def verify_manifest(prompts_dir) -> list[str]:
    """Problems between MANIFEST.json and the files on disk; an empty list means they match."""
    path = Path(prompts_dir) / MANIFEST_NAME
    if not path.exists():
        return [f"{MANIFEST_NAME} is missing"]
    recorded = json.loads(path.read_text(encoding="utf-8"))
    now = file_hashes(prompts_dir)
    problems = []
    rec_files = recorded.get("files", {})
    for n in sorted(set(rec_files) | set(now)):
        if n not in now:
            problems.append(f"{n}: listed in the manifest but missing on disk")
        elif n not in rec_files:
            problems.append(f"{n}: on disk but not in the manifest")
        elif rec_files[n] != now[n]:
            problems.append(f"{n}: content changed since the manifest was written")
    if recorded.get("set_sha256") != set_hash(rec_files):
        problems.append("set_sha256 does not match the listed file hashes")
    return problems


def main(argv=None) -> int:
    """``python3 -m second_scribe.prompt_manifest [PROMPTS_DIR]``: ``--help`` prints the usage and writes nothing; a
    folder that starts with ``-`` or is not a folder is refused (argparse error, exit 2, nothing written)."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m second_scribe.prompt_manifest",
        description="Rewrite MANIFEST.json for a prompts folder and print its set_sha256.")
    ap.add_argument("prompts_dir", metavar="PROMPTS_DIR", nargs="?",
                    help="the prompts folder (default: the kit's own prompts/; must not start with '-')")
    args = ap.parse_args(argv)
    if args.prompts_dir is None:
        target = Path(__file__).resolve().parent.parent / "prompts"
    else:
        if args.prompts_dir.startswith("-"):
            ap.error(f"PROMPTS_DIR must not start with '-' (got {args.prompts_dir!r}); use ./{args.prompts_dir} if you mean that folder")
        target = Path(args.prompts_dir)
        if not target.is_dir():
            ap.error(f"PROMPTS_DIR is not a folder: {args.prompts_dir}")
    print(json.dumps(write_manifest(target)["set_sha256"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
