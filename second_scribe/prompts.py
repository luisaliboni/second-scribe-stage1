"""Loaders for the shared prompt files: version check, construct definitions, system messages.

Used by both ``reconstruct`` and ``judge`` so the two prompts draw on one source
(``prompts/definitions.<version>.txt``) and one rule for naming a version.
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_PROMPT_VERSION = "v2"
# Two prompts moved on to v3 on 16 Oct 2026 (judge-prompts-v3, measured on the synthetic bench): the judge's rubric
# card (``judge_modality.v3.txt``: first ask whether the quote is about the element itself) and the off-record prompt
# (``in_transcript.v3.txt``: a not-mentioned label keeps its "quote" key, shown with an example). Every other prompt
# stays at DEFAULT_PROMPT_VERSION.
JUDGE_PROMPT_VERSION = "v3"
IN_TRANSCRIPT_PROMPT_VERSION = "v3"
# A version that changes only some files reuses the rest: (file stem, version) -> the version of the file it reads.
# The v3 prompts keep the v2 construct definitions (one source shared with the reconstruction prompt) and the v2
# system messages, byte for byte.
REUSED_FILES = {("definitions", "v3"): "v2", ("system.judge", "v3"): "v2", ("system.helper", "v3"): "v2"}
HELPER_PROMPTS = ("align", "in_transcript", "canonicalize", "confirm_mapping_talk")
_VERSION = re.compile(r"v[0-9]+")


def check_version(version: str) -> str:
    """``version`` if it looks like v1, v2, ...; otherwise ValueError (no path tricks)."""
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError(f"bad prompt version {version!r} (want v1, v2, ...)")
    return version


def file_version(stem: str, version: str) -> str:
    """The version of ``<stem>.<version>.txt`` actually read: ``version`` itself unless ``REUSED_FILES`` says that this
    version reuses an earlier file (the v3 prompts read ``definitions.v2.txt`` and the v2 system messages)."""
    return REUSED_FILES.get((stem, check_version(version)), version)


def load_definitions(prompts_dir, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """The construct definitions shared by the reconstruction and judge prompts
    (``definitions.<version>.txt``), as one text, without the trailing newline."""
    return (Path(prompts_dir) / f"definitions.{file_version('definitions', version)}.txt").read_text(encoding="utf-8").rstrip("\n")


def load_system(prompts_dir, role: str = "reconstruct", version: str = DEFAULT_PROMPT_VERSION) -> str:
    """The system message for ``role`` ("reconstruct", "judge" or "helper"): ``system.<role>.<version>.txt``."""
    if role not in ("reconstruct", "judge", "helper"):
        raise ValueError(f"unknown system prompt role {role!r}")
    return (Path(prompts_dir) / f"system.{role}.{file_version('system.' + role, version)}.txt").read_text(encoding="utf-8").strip()


def load_helper_prompt(prompts_dir, name: str, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """The user-message template of a helper prompt (``<name>.<version>.txt``): ``align``,
    ``in_transcript``, ``canonicalize`` or ``confirm_mapping_talk``. Anything else raises ValueError."""
    if name not in HELPER_PROMPTS:
        raise ValueError(f"unknown helper prompt {name!r} (want one of {HELPER_PROMPTS})")
    return (Path(prompts_dir) / f"{name}.{check_version(version)}.txt").read_text(encoding="utf-8")


def helper_messages(prompts_dir, user: str, version: str = DEFAULT_PROMPT_VERSION) -> list[dict]:
    """``[system, user]`` for a helper call; v1 is reproduced as the bare ``[user]`` it always was.
    Every later version sends ``system.helper.<version>.txt`` first (never an empty system message)."""
    if check_version(version) == "v1":
        return [{"role": "user", "content": user}]
    return [{"role": "system", "content": load_system(prompts_dir, "helper", version)},
            {"role": "user", "content": user}]
