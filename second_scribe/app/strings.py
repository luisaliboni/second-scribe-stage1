"""User-visible strings for the local app (flat keys ``area.name``, English only)."""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "strings"


def load_strings(lang: str = "en") -> dict[str, str]:
    """Return the flat key -> text mapping for ``lang`` (only ``en`` ships)."""
    if not lang.isalpha():
        raise ValueError(f"bad language code: {lang!r}")
    path = _DIR / f"{lang}.json"
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return {**data, **_kit_profile_strings(set(data))}


# The kit's sentences for profile codes (PROFILE-SCHEMA §13: profile.reason.*, profile.missing.*,
# profile.formula.*). Merged, never copied by hand; absent until the exporter (E2 M1) is on the branch.
KIT_PROFILE_STRINGS = Path(__file__).resolve().parents[1] / "profile_strings.json"
KIT_PREFIXES = ("profile.reason.", "profile.missing.", "profile.formula.", "profile.provider.", "profile.role.")


def _kit_profile_strings(own: set[str]) -> dict[str, str]:
    if not KIT_PROFILE_STRINGS.is_file():
        return {}
    with KIT_PROFILE_STRINGS.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("profile_strings.json must hold a JSON object")
    for key, text in data.items():
        if not key.startswith(KIT_PREFIXES) or not isinstance(text, str) or not text:
            raise ValueError(f"profile_strings.json: bad entry {key!r}")
        if key in own:
            raise ValueError(f"profile_strings.json: {key!r} also in the app strings")
    return data


_CACHE: dict[str, dict[str, str]] = {}


def t(key: str, **kw: object) -> str:
    """Look up ``key`` and fill ``{slots}`` from ``kw``. Unknown key raises ``KeyError(key)``."""
    table = _CACHE.get("en")
    if table is None:
        table = _CACHE["en"] = load_strings("en")
    try:
        text = table[key]
    except KeyError:
        raise KeyError(key) from None
    return text.format_map(kw) if kw else text
