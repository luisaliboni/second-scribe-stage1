"""The ``tool`` block of a run config: settings that only the app writes (the tool-only switches).

A study config has no ``tool`` block, and ``RunConfig.sha256()`` leaves an empty block out of the hash, so every
study config keeps the hash it always had. A config with a non-empty block is a tool run: ``RunConfig.__post_init__``
checks the block here and the block enters the hash.

Keys (any other key is refused):

- ``app_log_head``: ``{"records": int >= 0, "head": 64-hex}``, the app log's length and chain head at run start.
- ``judge_rule``: ``"study"`` or ``"two_family"``.
- ``model_families``: ``{role: family}``.
- ``model_digests``: ``{role: 64-hex}`` (a leading ``sha256:`` of an Ollama digest is stripped before it lands here).
- ``model_check``: ``{"path": str, "sha256": 64-hex}`` and nothing else.
- ``rung1_user``: ``{"text": 1..20000 chars, "wrapper": str, "text_sha256", "wrapper_sha256"}``, both hashes equal
  to the sha256 of their text.
- ``remote_endpoints``: list of ``{"role", "host", "provider": "openai_compat"|"anthropic", "declaration_sha256"}``.
- ``endpoint_optins``: list of ``{"role", "host"}``.
- ``reference_type``: ``"A"``, ``"B"`` or ``"C"``.
- ``reading3_prompt_sha256``: 64-hex, the sha256 of the no-interview prompt as run, so pooled profiles carry it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

TOOL_KEYS = ("app_log_head", "judge_rule", "model_families", "model_digests", "model_check", "rung1_user",
             "remote_endpoints", "endpoint_optins", "reference_type", "reading3_prompt_sha256")
JUDGE_RULES = ("study", "two_family")
PROVIDERS = ("openai_compat", "anthropic")
REFERENCE_TYPES = ("A", "B", "C")
RUNG1_MAX_CHARS = 20000

# K9: the app's output-format block for a user-written rung-1 prompt. It lives outside ``prompts/`` on purpose: the
# study's prompt set (``prompts/MANIFEST.json`` and ``pipeline.prompt_set_sha256``) never sees it; the app copies its
# text into ``tool.rung1_user.wrapper``, so the config hash covers it.
RUNG1_WRAPPER_PATH = Path(__file__).resolve().parent / "app" / "prompts" / "rung1_wrapper.txt"
# V7: reading 3 (the no-interview map) is run from this kit prompt; a tool run records its sha256 at run start
READING3_PROMPT_FILE = "prior_only.p0.v2.txt"

_HEX64 = re.compile(r"[0-9a-f]{64}")


def _is_hex64(v) -> bool:
    return isinstance(v, str) and _HEX64.fullmatch(v) is not None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and v != ""


def _fail(key: str, why: str) -> None:
    raise ValueError(f"tool.{key}: {why}")


def _exact_keys(key: str, d, want: tuple) -> None:
    if not isinstance(d, dict):
        _fail(key, f"must be an object with keys {list(want)}")
    if set(d) != set(want):
        _fail(key, f"must have exactly the keys {list(want)}, got {sorted(d)}")


def _role_map(key: str, d, ok, what: str) -> None:
    if not isinstance(d, dict):
        _fail(key, "must be an object {role: ...}")
    for role, v in d.items():
        if not _nonempty_str(role) or not ok(v):
            _fail(key, f"each entry must map a role name to {what}")


def _list_of(key: str, items, want: tuple) -> None:
    if not isinstance(items, list):
        _fail(key, "must be a list")
    for i, d in enumerate(items):
        _exact_keys(f"{key}[{i}]", d, want)
        for k in ("role", "host"):
            if not _nonempty_str(d[k]):
                _fail(f"{key}[{i}].{k}", "must be a non-empty string")


def check_tool(tool: dict, corpus: dict) -> None:
    """Raise ``ValueError`` naming the key when the ``tool`` block is not well formed. ``corpus`` is the config's
    corpus block; no current check depends on it."""
    if not isinstance(tool, dict):
        raise ValueError("tool must be an object")
    for k in tool:
        if k not in TOOL_KEYS:
            raise ValueError(f"unknown tool key: {k}")
    if "app_log_head" in tool:
        v = tool["app_log_head"]
        _exact_keys("app_log_head", v, ("records", "head"))
        if not _is_int(v["records"]) or v["records"] < 0:
            _fail("app_log_head.records", "must be an integer >= 0")
        if not _is_hex64(v["head"]):
            _fail("app_log_head.head", "must be a 64-character lowercase hex sha256")
    if "judge_rule" in tool and tool["judge_rule"] not in JUDGE_RULES:
        _fail("judge_rule", f"must be one of {JUDGE_RULES}")
    if "model_families" in tool:
        _role_map("model_families", tool["model_families"], _nonempty_str, "a family name")
    if "model_digests" in tool:
        _role_map("model_digests", tool["model_digests"], _is_hex64, "a 64-character lowercase hex digest")
    if "model_check" in tool:
        v = tool["model_check"]
        _exact_keys("model_check", v, ("path", "sha256"))
        if not _nonempty_str(v["path"]):
            _fail("model_check.path", "must be a non-empty string")
        if not _is_hex64(v["sha256"]):
            _fail("model_check.sha256", "must be a 64-character lowercase hex sha256")
    if "rung1_user" in tool:
        v = tool["rung1_user"]
        _exact_keys("rung1_user", v, ("text", "wrapper", "text_sha256", "wrapper_sha256"))
        if not isinstance(v["text"], str) or not 1 <= len(v["text"]) <= RUNG1_MAX_CHARS:
            _fail("rung1_user.text", f"must be a string of 1 to {RUNG1_MAX_CHARS} characters")
        if not isinstance(v["wrapper"], str):
            _fail("rung1_user.wrapper", "must be a string")
        for part in ("text", "wrapper"):
            if v[f"{part}_sha256"] != hashlib.sha256(v[part].encode("utf-8")).hexdigest():
                _fail(f"rung1_user.{part}_sha256", f"does not match the sha256 of rung1_user.{part}")
    if "remote_endpoints" in tool:
        eps = tool["remote_endpoints"]
        _list_of("remote_endpoints", eps, ("role", "host", "provider", "declaration_sha256"))
        for i, d in enumerate(eps):
            if d["provider"] not in PROVIDERS:
                _fail(f"remote_endpoints[{i}].provider", f"must be one of {PROVIDERS}")
            if not _is_hex64(d["declaration_sha256"]):
                _fail(f"remote_endpoints[{i}].declaration_sha256", "must be a 64-character lowercase hex sha256")
    if "endpoint_optins" in tool:
        _list_of("endpoint_optins", tool["endpoint_optins"], ("role", "host"))
    if "reference_type" in tool and tool["reference_type"] not in REFERENCE_TYPES:
        _fail("reference_type", f"must be one of {REFERENCE_TYPES}")
    if "reading3_prompt_sha256" in tool and not _is_hex64(tool["reading3_prompt_sha256"]):
        _fail("reading3_prompt_sha256", "must be a 64-character lowercase hex sha256")


def is_tool(cfg) -> bool:
    """True for a run the app configured (a non-empty ``tool`` block), False for a study config."""
    return bool(cfg.tool)


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def rung1_user_block(text: str, wrapper: str | None = None) -> dict:
    """The ``tool.rung1_user`` block for the user's rung-1 prompt ``text``: the text, the wrapper (default: the app's
    ``rung1_wrapper.txt``) and the sha256 of each."""
    if wrapper is None:
        wrapper = RUNG1_WRAPPER_PATH.read_text(encoding="utf-8")
    return {"text": text, "text_sha256": _sha256_text(text), "wrapper": wrapper, "wrapper_sha256": _sha256_text(wrapper)}


def run_start_fields(tool: dict, prompts_dir) -> dict:
    """Additive ``run_start`` fields of a tool run, written before any call: ``reading3_prompt_sha256`` (V7, the sha256
    of the as-run no-interview prompt file) and, with a ``rung1_user`` block, ``rung1_user_prompt_sha256`` (K9, the
    sha256 of the user's text). ``{}`` for a study config (no ``tool`` block), so its records are unchanged.
    Audit K10: a ``tool.reading3_prompt_sha256`` the config carries must equal the live file's hash, else
    ``ValueError`` (raised before ``run_start`` is written, so the run does not start)."""
    if not tool:
        return {}
    live = hashlib.sha256((Path(prompts_dir) / READING3_PROMPT_FILE).read_bytes()).hexdigest()
    if "reading3_prompt_sha256" in tool and tool["reading3_prompt_sha256"] != live:
        _fail("reading3_prompt_sha256", f"is {tool['reading3_prompt_sha256']}, but the no-interview prompt "
                                        f"{READING3_PROMPT_FILE} as it is now hashes to {live}; refusing to run")
    out = {"reading3_prompt_sha256": live}
    if "rung1_user" in tool:
        out["rung1_user_prompt_sha256"] = tool["rung1_user"]["text_sha256"]
    return out
