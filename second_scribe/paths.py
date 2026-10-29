"""Names on disk that must also be legal on Windows.

A stage key of the work store may hold a colon (``recon:rung1``, ``judge_modality:rung3``, ``align:placebo``), and a
colon is not allowed in a Windows file name. On macOS and Linux the folder is the key, byte for byte (the existing work
stores and the run logs' work-integrity checks are keyed by it); on Windows the colon becomes a ``~``, which no kit
stage key contains. The log always records the key, never the folder name.
"""
import os

WINDOWS: bool = os.name == "nt"      # tests set it to force the Windows branch
_COLON, _TILDE = ":", "~"


def stage_dirname(stage: str) -> str:
    """The work-store folder of stage key ``stage``."""
    return stage.replace(_COLON, _TILDE) if WINDOWS else stage


def stage_from_dirname(name: str) -> str:
    """The stage key of work-store folder ``name`` (the inverse of ``stage_dirname``)."""
    return name.replace(_TILDE, _COLON) if WINDOWS else name
