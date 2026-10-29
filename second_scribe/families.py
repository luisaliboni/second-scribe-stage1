"""Model families, in ONE place. The label "other family" on a script source (the generic pathway a different
model writes from prior-only prompts) is never taken from config: it is derived here from the model ids the audit
log records for the script's calls and for the reconstructor's calls (M-F2-1)."""
from __future__ import annotations

# (family, substrings of the lower-case model name). First match wins; the Qwen row is the same set the real-run
# guard forbids for a judge (llm.FORBIDDEN_JUDGE_FAMILIES).
FAMILY_TABLE = (
    ("qwen", ("qwen", "qwq")),
    ("deepseek", ("deepseek",)),
    ("gemma", ("gemma",)),
    ("llama", ("llama",)),
    ("mistral", ("mistral", "mixtral", "ministral")),
    ("gpt-oss", ("gpt-oss",)),
    ("phi", ("phi-", "phi3", "phi4")),
    ("glm", ("glm",)),
    ("kimi", ("kimi",)),
    ("minimax", ("minimax",)),
    ("claude", ("claude",)),
)


def model_name(model_id: str | None) -> str:
    """Lower-case model name without a leading path (``org/Model-X`` -> ``model-x``)."""
    return (model_id or "").strip().lower().rsplit("/", 1)[-1]


def model_family(model_id: str | None) -> str | None:
    """The family of a model id for DISPLAY: a ``FAMILY_TABLE`` name, else the model's own name. None for no id.
    Never the basis of the "other family" label: that uses ``known_family`` (an unlisted id may be an alias)."""
    name = model_name(model_id)
    if not name:
        return None
    for family, needles in FAMILY_TABLE:
        if any(n in name for n in needles):
            return family
    return name


def known_family(model_id: str | None) -> str | None:
    """The ``FAMILY_TABLE`` family of a model id, or None when the table does not know it (an alias such as "judge",
    which a server that ignores the model field can serve under any real model)."""
    name = model_name(model_id)
    for family, needles in FAMILY_TABLE:
        if name and any(n in name for n in needles):
            return family
    return None


def script_family(script_model_ids, recon_model_ids) -> tuple[str, str]:
    """("other" | "own", reason code). "other" only when the script was written by exactly one logged model whose
    family is not Qwen and differs from the family of every logged reconstructor model (and it is not the same model
    id). Anything that cannot be established from the log is "own": the sensitivity row that never sets the headline
    flag. Reason codes (short: they go into the aggregates): different_family, script_model_not_on_record,
    several_script_models, recon_model_not_on_record, script_is_qwen_family, same_model_or_family, family_unknown
    (fail closed: "other" needs the script's family AND every reconstructor's family to be rows of ``FAMILY_TABLE``;
    an alias or an unlisted id proves nothing about the family)."""
    script = {model_name(m) for m in script_model_ids if model_name(m)}
    recon = {model_name(m) for m in recon_model_ids if model_name(m)}
    if not script:
        return "own", "script_model_not_on_record"
    if len(script) > 1:
        return "own", "several_script_models"
    if not recon:
        return "own", "recon_model_not_on_record"
    (s,) = script
    fs = known_family(s)
    if fs == "qwen":
        return "own", "script_is_qwen_family"
    if s in recon or (fs is not None and fs in {known_family(r) for r in recon}):
        return "own", "same_model_or_family"
    if fs is None or any(known_family(r) is None for r in recon):
        return "own", "family_unknown"
    return "other", "different_family"
