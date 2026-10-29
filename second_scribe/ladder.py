"""The instruction ladder (council round 5): copy auditor and rung registry.

Rung 1 = the authors' first-draft prompt, rung 2 = the good-practice prompt (the main run),
rung 3 = the remedy (narrator frame + counterfactual demos), placebo = rung 2 + matched demos.
Only the instruction text changes between rungs.

The copy auditor checks that a model did not copy wording from the demonstrations: any 6-gram
of a demo that shows up in a node label or a quote of an output map is flagged. Demos are
never scored; this is the check that they did not leak into the scored maps.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, NamedTuple, Sequence

from .llm import derive_seed
from .ladder_metrics import PLACEBO, REMEDY, RUNG1, RUNG2, _read_records
from .metrics import PRIMARY_SPEC
from .prompt_manifest import verify_manifest
from .prompts import load_definitions, load_system
from .reconstruct import RECON_MAX_TOKENS, ReconSpec, build_messages, reconstruct
from .schemas import MapGraph, Transcript
from .synth.demos import DEMO_NGRAM_N, demo_label_tuples, demo_ngrams, render_demos
from .textnorm import tokens


# ----------------------------------------------------------------------------------------------
# Copy auditor
# ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CopyFlag:
    map_id: str
    item_id: str          # node id ("n3") or edge id ("n1->n2")
    field: str            # "label" | "quote"
    ngram: tuple          # the demo n-gram (or whole demo label) found, as normalised tokens
    kind: str = "ngram"   # "ngram": a demo n-gram; "label_equal": the node label equals a demo label


_DEFAULT_LABELS = object()


def _check_grams(grams, n: int) -> None:
    if any(len(g) != n for g in grams):
        raise ValueError(f"the n-gram set does not hold {n}-grams (pass n= to match it)")


def _scan(text, grams, n: int):
    """Demo n-grams that occur in ``text`` (normalised, inside the one text), in order of appearance."""
    if not isinstance(text, str):
        return []
    t = tokens(text)
    found, seen = [], set()
    for i in range(len(t) - n + 1):
        g = tuple(t[i:i + n])
        if g in grams and g not in seen:
            seen.add(g)
            found.append(g)
    return found


def _source_grams(source_text, n: int) -> frozenset:
    """n-grams of the output's own transcript, taken line by line (a turn per line), never across lines."""
    out = set()
    for line in (source_text or "").split("\n"):
        t = tokens(line)
        out.update(tuple(t[i:i + n]) for i in range(len(t) - n + 1))
    return frozenset(out)


def scan_map(graph: MapGraph, grams, n: int = DEMO_NGRAM_N, *, map_id: str | None = None,
             source_text: str | None = None, labels=_DEFAULT_LABELS) -> tuple[list[CopyFlag], int]:
    """(flags, n_in_source). n-grams that also occur in ``source_text`` (the transcript this output was made from)
    are not copies of a demo: they are dropped and counted in the second value. Node labels that equal a demo label
    (normalised) are flagged with kind "label_equal" (``labels``: a set of token tuples; default all demo labels,
    ``frozenset()`` switches the check off)."""
    _check_grams(grams, n)
    labels = demo_label_tuples() if labels is _DEFAULT_LABELS else labels
    mid = graph.map_id if map_id is None else map_id
    src = _source_grams(source_text, n) if source_text else frozenset()
    flags: list[CopyFlag] = []
    in_source = 0

    def add(item_id, field, text):
        nonlocal in_source
        for g in _scan(text, grams, n):
            if g in src:
                in_source += 1
            else:
                flags.append(CopyFlag(mid, item_id, field, g))

    for node in graph.nodes:
        add(node.node_id, "label", node.text)
        add(node.node_id, "quote", node.quote)
        lab = tuple(tokens(node.text)) if isinstance(node.text, str) else ()
        if lab and lab in labels:
            flags.append(CopyFlag(mid, node.node_id, "label", lab, "label_equal"))
    for e in graph.edges:
        add(f"{e.source}->{e.target}", "quote", e.quote)
    return flags, in_source


def audit_map(graph: MapGraph, grams, n: int = DEMO_NGRAM_N, *, map_id: str | None = None,
              source_text: str | None = None, labels=_DEFAULT_LABELS) -> list[CopyFlag]:
    """Every demo copy found in a node label, a node quote or an edge quote of ``graph`` (see ``scan_map``)."""
    return scan_map(graph, grams, n, map_id=map_id, source_text=source_text, labels=labels)[0]


def audit_outputs(outputs: Mapping[str, object], grams=None, n: int = DEMO_NGRAM_N, labels=_DEFAULT_LABELS) -> dict:
    """Summary for one rung. ``outputs`` maps an output id to a ``MapGraph`` or to ``(MapGraph, transcript_text)``
    (the transcript lets the auditor ignore wording that really is in the source); None = the call failed:
    counted in ``n_missing``, never as clean. Returns n_outputs, n_missing, n_flagged, flagged_ids (sorted output
    ids with at least one flag), n_in_source (demo n-grams dropped because the transcript has them too), the gram
    size and the flags as plain dicts."""
    grams = demo_ngrams(n) if grams is None else grams
    _check_grams(grams, n)
    flags: list[CopyFlag] = []
    n_missing = n_out = n_in_source = 0
    for key in sorted(outputs):
        v = outputs[key]
        if v is None:
            n_missing += 1
            continue
        g, source = v if isinstance(v, tuple) else (v, None)
        n_out += 1
        f, dropped = scan_map(g, grams, n, map_id=key, source_text=source, labels=labels)
        flags += f
        n_in_source += dropped
    flagged = sorted({f.map_id for f in flags})
    return {"n": n, "n_outputs": n_out, "n_missing": n_missing, "n_flagged": len(flagged),
            "flagged_ids": flagged, "n_in_source": n_in_source,
            "flags": [{**asdict(f), "ngram": " ".join(f.ngram)} for f in flags]}


def audit_rungs(outputs_by_rung: Mapping[str, Mapping[str, object]], grams=None, n: int = DEMO_NGRAM_N,
                labels=_DEFAULT_LABELS) -> dict[str, dict]:
    """``audit_outputs`` for each rung ({rung id: summary}); default grams = all demos of both arms."""
    grams = demo_ngrams(n) if grams is None else grams
    return {rung: audit_outputs(outs, grams, n, labels) for rung, outs in outputs_by_rung.items()}


# ----------------------------------------------------------------------------------------------
# Rung registry
# ----------------------------------------------------------------------------------------------

REAL_DATA_RULE = "primary setting only"      # on real data every rung runs the primary spec and nothing else
REAL_DRAWS = 1                               # draws per map on real data: the primary setting is T=0 (greedy), so extra draws are copies (DECISIONS C7)
LENGTH_TOLERANCE = 0.02                      # remedy vs placebo prompt file, characters
_PLACEHOLDER = re.compile(r"\{(role|domain|phases|schema|transcript|definitions)\}")
_REQUIRED_PLACEHOLDERS = frozenset({"role", "domain", "phases", "schema", "transcript"})
_V2_PROMPT = "reconstruct.p0.v2.txt"
# The wordings of rungs 1 and 2 (and what rung 2 shares with the arms) are pinned here as well as in MANIFEST.json,
# so regenerating the manifest after an edit cannot pass the pre-run gate (review M6).
# Rung 1 is the authors' own first draft (commit a92a922). Run 2 (council round 8) changed the validator and added
# two sentences to every OTHER prompt; rung 1 must stay byte-identical or the ladder's lower bound is gone. Its hash
# is a constant of its own, checked by validate_rungs besides the pin, so editing the pin table alone cannot hide it.
RUNG1_SHA256 = "4b17dd7827b2de459229e931a4195efc5cb2f256d1cd8120235a46035c43584a"
# reconstruct.p0.v2.txt: run 2 (15 Oct 2026) replaced the false "each line is one turn" sentence and added the node-type
# sentence; its previous sha256 was 24f61a31fbce16985abd141bd965a21a8e40e9600e930bd0e95bcbf44cd591f5 (run 1).
PINNED_SHA256 = {
    "reconstruct.p0.v1.txt": RUNG1_SHA256,
    "reconstruct.p0.v2.txt": "831b51122594ed313f81b5b818a2406153a5c1fb439348930cc61bf7804494a5",
    "system.reconstruct.v2.txt": "deb5362a70a396fcb35fcff06fa6b12170253bf50736673b5dbeaafd03ead166",
    "definitions.v2.txt": "306439dec9d09303ca3ccc877962868b60596741dac96d9b15a135821cdcbc34",
}


@dataclass(frozen=True)
class Rung:
    """One instruction condition of the ladder. ``variant`` is the prompt variant it exists for (only p0: the remedy
    and the placebo have no p1/p2 file, so a p1/p2 spec must never run under a rung). ``real_data_specs`` governs the
    LADDER ARMS on real data (primary spec only); the real main run of rung 2 over the full grid is the existing
    pipeline's job and is outside this registry's rule (review M7)."""
    key: str                       # stable id used in file names, logs and tables: rung1 | rung2 | rung3 | placebo
    name: str                      # first_draft | good_practice | remedy | placebo
    label: str                     # wording for reports and figures
    prompt_file: str               # in prompts/, listed in MANIFEST.json
    system_file: str | None        # None: no system message (the v1 prompt never had one)
    definitions_file: str | None   # None: the prompt has no {definitions} slot
    demo_arm: str | None           # "remedy" | "placebo": the demo block embedded literally in prompt_file
    version: str = "v2"            # which system/definitions set the prompt uses ("v1": none of them)
    variant: str = "p0"            # the only prompt variant this rung exists for
    real_data: str = REAL_DATA_RULE
    real_data_specs: tuple = (PRIMARY_SPEC,)   # ReconSpec keys a real-data run of this rung may use
    requires: tuple = ()           # rung keys that must run with it (rung 3 never runs without the placebo)
    note: str = ""


RUNGS: tuple[Rung, ...] = (
    Rung("rung1", "first_draft", "Rung 1: first-draft prompt (lower bound on everyday drift)",
         "reconstruct.p0.v1.txt", None, None, None, version="v1",
         note="the authors' own first draft (commit a92a922), unchanged; v1 also words 'wished' differently, "
              "so 2 minus 1 is a package contrast, descriptive only"),
    Rung("rung2", "good_practice", "Rung 2: good-practice prompt (main run)",
         _V2_PROMPT, "system.reconstruct.v2.txt", "definitions.v2.txt", None,
         note="the headline instruction condition"),
    Rung("rung3", "remedy", "Rung 3: remedy (narrator frame + counterfactual demos)",
         "reconstruct.p0.remedy.v1.txt", "system.reconstruct.v2.txt", "definitions.v2.txt", "remedy",
         requires=("placebo",), note="never run without the placebo"),
    Rung("placebo", "placebo", "Placebo: neutral frame + matched neutral demos",
         "reconstruct.p0.placebo.v1.txt", "system.reconstruct.v2.txt", "definitions.v2.txt", "placebo",
         note="same demo count, format, length (+-2%) and wished share as the remedy; no departure, no narrator frame"),
)


# ``ladder_metrics`` names its arms rung1 / rung2 / remedy / placebo; the registry keys the remedy "rung3".
ARM_OF_RUNG = {"rung1": RUNG1, "rung2": RUNG2, "rung3": REMEDY, "placebo": PLACEBO}


def metrics_arms(by_rung: Mapping[str, object]) -> dict:
    """``{registry rung key: x}`` -> ``{ladder_metrics arm name: x}``; build the ``arms`` of
    ``ladder_metrics.ladder_report`` through this (a registry key there skips the remedy contrast). KeyError for an
    unknown key."""
    return {ARM_OF_RUNG[k]: v for k, v in by_rung.items()}


def get_rung(key: str, rungs=RUNGS) -> Rung:
    for r in rungs:
        if r.key == key:
            return r
    raise KeyError(f"unknown rung {key!r} (want one of {[r.key for r in rungs]})")


def load_rung_prompt(prompts_dir, rung: Rung) -> str:
    """The registered prompt file's text (``reconstruct.load_prompt`` cannot name the remedy/placebo files)."""
    return (Path(prompts_dir) / rung.prompt_file).read_text(encoding="utf-8")


def check_rung_spec(rung: Rung, spec: ReconSpec, *, real_data: bool = False, max_draws: int = REAL_DRAWS) -> None:
    """ValueError unless ``spec`` may run under ``rung``: same prompt variant; on real data also exactly one of the
    rung's ``real_data_specs`` (the primary setting) and a replicate index below ``max_draws`` (the declared draw
    count, 3 by default). A draw is a replicate of the primary setting, so draws 0, 1, 2 pass; another arm,
    temperature or variant, or a replicate beyond the declared draws, does not. Call it before every reconstruction of
    a ladder arm."""
    if spec.variant != rung.variant:
        raise ValueError(f"{rung.key} exists for variant {rung.variant} only, got spec {spec.key()}")
    if spec.temperature == 0 and spec.replicate > 0:
        raise ValueError(f"{rung.key}: {spec.key()} is draw {spec.replicate} at temperature 0 (greedy); extra draws at "
                         "T=0 are copies of draw 0, not independent replicates. Run one draw (r0), or sample at T>0")
    if real_data and replace(spec, replicate=0).key() not in rung.real_data_specs:
        raise ValueError(f"{rung.key}: real data runs {rung.real_data}: {rung.real_data_specs}, got {spec.key()}")
    if real_data and not 0 <= spec.replicate < max_draws:
        raise ValueError(f"{rung.key}: real data runs draws r0..r{max_draws - 1} of the primary setting, "
                         f"got {spec.key()}")


# K9 (tool runs only): rung 1 may run the user's own first-draft prompt. ``template`` is the config's
# ``tool.rung1_user`` block ({"text", "wrapper", ...}); the user's text is sent verbatim, followed by the app's wrapper
# (the output-format block) filled by ``build_messages``. Never for any other rung; ``None`` = the kit's rung 1.
_WRAPPER_END = "\nTRANSCRIPT:\n{transcript}"


def _check_template(rung: Rung, template) -> None:
    if template is not None and rung.key != "rung1":
        raise ValueError(f"a user prompt template applies to rung 1 only, not {rung.key}")


def rung_messages(prompts_dir, rung: Rung, ctx, transcript_text: str, *, spec: ReconSpec | None = None,
                  template: Mapping | None = None) -> list[dict]:
    """The chat messages of ``rung`` for one transcript: ``[system, user]``, or ``[user]`` for rung 1.
    Same loaders and filling code as the pipeline (``load_system``, ``load_definitions``,
    ``reconstruct.build_messages``); only the instruction text differs. With ``spec``, ``check_rung_spec`` runs first.
    With ``template`` (rung 1 only, a ``tool.rung1_user`` block) the one user message is
    ``text + "\n\n" + <wrapper filled>``: the text is never slot-filled, and there is no system message and no
    definitions, as in the kit's rung 1."""
    _check_template(rung, template)
    if spec is not None:
        check_rung_spec(rung, spec)
    if template is not None:
        user = build_messages(template["wrapper"], ctx, transcript_text)[0]
        return [{"role": "user", "content": template["text"] + "\n\n" + user["content"]}]
    d = Path(prompts_dir)
    system = load_system(d, "reconstruct", rung.version) if rung.system_file else None
    defs = load_definitions(d, rung.version) if rung.definitions_file else None
    return build_messages(load_rung_prompt(d, rung), ctx, transcript_text, system=system, definitions=defs)


def rung_prompt_sha256(prompts_dir, rung: Rung, *, template: Mapping | None = None) -> str:
    """sha256 over the content of the rung's prompt, system and definitions files (names included). Stored in each
    ladder work item: ``MANIFEST.json`` is regenerated on every edit and ``PINNED_SHA256`` covers rungs 1 and 2 only,
    so only this identity tells a resume that an item was made with other instruction text. With ``template`` (rung 1
    only, see ``rung_messages``) it is the sha256 of ``user:<sha256(text)>\nwrapper:<sha256(wrapper)>`` instead."""
    _check_template(rung, template)
    if template is not None:
        parts = (hashlib.sha256(template[k].encode("utf-8")).hexdigest() for k in ("text", "wrapper"))
        return hashlib.sha256("user:{}\nwrapper:{}".format(*parts).encode("utf-8")).hexdigest()
    d = Path(prompts_dir)
    h = hashlib.sha256()
    for role, name in (("prompt", rung.prompt_file), ("system", rung.system_file), ("definitions", rung.definitions_file)):
        if name:
            h.update(f"{role}:{name}:{hashlib.sha256((d / name).read_bytes()).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


# ---- identity of a rung's calls: unique keys, common random numbers -------------------------------------------
# The suffix makes log / work-store keys unique per rung; it must NEVER reach the seed. Equal seeds for the same
# map and draw across rungs are what makes 3 - P, P - 2 and 2 - 1 paired contrasts on one sampling stream.

class Stages(NamedTuple):
    store: str    # work-store stage (resume key), one per rung
    log: str      # audit-log stage, one per rung and arm


def rung_item_id(item_id: str, rung: Rung) -> str:
    """``item_id`` made unique per rung for logs and work stores: ``<item_id>@<rung.key>``."""
    return f"{item_id}@{rung.key}"


def base_item_id(item_id: str, rungs=RUNGS) -> str:
    """``item_id`` without ONE trailing ``@<rung key>`` (only a known rung key is stripped)."""
    head, sep, tail = item_id.rpartition("@")
    return head if sep and tail in {r.key for r in rungs} else item_id


def rung_seed(item_id: str, spec_key: str) -> int:
    """The seed ``reconstruct`` derives today (``derive_seed(item_id, spec_key)``), computed on the item id WITHOUT
    the rung suffix, so every rung samples with the same seed for the same map and draw."""
    return derive_seed(base_item_id(item_id), spec_key)


def rung_stage(rung: Rung, spec: ReconSpec) -> Stages:
    """Work-store stage ``recon:<rung>`` and audit-log stage ``recon:<arm>:<rung>`` for one call (variant-checked)."""
    check_rung_spec(rung, spec)
    return Stages(store=f"recon:{rung.key}", log=f"recon:{spec.arm}:{rung.key}")


def rung1_template_problems(template: Mapping) -> list[str]:
    """Problems of a ``tool.rung1_user`` block as a rung-1 template: a non-empty text and a wrapper holding exactly
    the five slots {schema} {domain} {role} {phases} {transcript} (no {definitions}: rung 1 has none), ending with
    the line ``TRANSCRIPT:`` followed by ``{transcript}``."""
    problems: list[str] = []
    text, wrapper = template.get("text"), template.get("wrapper")
    if not isinstance(text, str) or not text.strip():
        problems.append("rung1_user: the user's prompt text is empty")
    if not isinstance(wrapper, str):
        return problems + ["rung1_user: the wrapper is not a text"]
    found = Counter(_PLACEHOLDER.findall(wrapper))
    if missing := sorted(_REQUIRED_PLACEHOLDERS - set(found)):
        problems.append(f"rung1_user: wrapper placeholder(s) missing: {missing}")
    if extra := sorted(set(found) - _REQUIRED_PLACEHOLDERS):
        problems.append(f"rung1_user: wrapper placeholder(s) not allowed in rung 1: {extra}")
    if twice := sorted(k for k, n in found.items() if n > 1):
        problems.append(f"rung1_user: wrapper placeholder(s) used more than once: {twice}")
    if not wrapper.rstrip("\n").endswith(_WRAPPER_END):
        problems.append("rung1_user: the wrapper must end with the line TRANSCRIPT: followed by {transcript}")
    return problems


def validate_rungs(prompts_dir, rungs=RUNGS, *, rung1_template: Mapping | None = None) -> list[str]:
    """Problems in a rung table checked against the prompt files; an empty list means it is consistent. With
    ``rung1_template`` (a tool run's ``tool.rung1_user`` block) rung 1's template is checked too
    (``rung1_template_problems``); every pinned prompt file is still checked."""
    d = Path(prompts_dir)
    rungs = list(rungs)
    problems: list[str] = []
    if rung1_template is not None:
        problems += rung1_template_problems(rung1_template)
    for attr in ("key", "name", "label"):
        vals = [getattr(r, attr) for r in rungs]
        problems += [f"duplicate {attr} {v!r}" for v in sorted({v for v in vals if vals.count(v) > 1})]
    keys = {r.key for r in rungs}
    for r in rungs:
        if r.key == "rung1":
            f1 = d / r.prompt_file
            if f1.is_file() and hashlib.sha256(f1.read_bytes()).hexdigest() != RUNG1_SHA256:
                problems.append(f"rung 1 prompt {r.prompt_file} is not byte-identical to the authors' first draft "
                                "(ladder.RUNG1_SHA256)")
    for name, sha in PINNED_SHA256.items():
        f = d / name
        if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() != sha:
            problems.append(f"{name}: differs from the pinned sha256 in ladder.PINNED_SHA256")
    files_used = {f for r in rungs for f in (r.prompt_file, r.system_file, r.definitions_file) if f}
    manifest = verify_manifest(d)
    problems += [m for m in manifest if ": " not in m or m.split(": ", 1)[0] in files_used]
    texts: dict[str, str] = {}
    for r in rungs:
        if r.real_data != REAL_DATA_RULE:
            problems.append(f"{r.key}: real_data must be {REAL_DATA_RULE!r}, got {r.real_data!r}")
        if tuple(r.real_data_specs) != (PRIMARY_SPEC,):
            problems.append(f"{r.key}: real_data_specs must be ({PRIMARY_SPEC!r},)")
        if f".{r.variant}." not in r.prompt_file:
            problems.append(f"{r.key}: variant {r.variant} does not match prompt file {r.prompt_file}")
        for sk in r.real_data_specs:
            if f"__{r.variant}__" not in sk:
                problems.append(f"{r.key}: real_data_specs entry {sk!r} is not variant {r.variant}")
        if r.system_file != (f"system.reconstruct.{r.version}.txt" if r.system_file else None) or \
                r.definitions_file != (f"definitions.{r.version}.txt" if r.definitions_file else None):
            problems.append(f"{r.key}: system/definitions file names do not match version {r.version}")
        for rk in r.requires:
            if rk not in keys:
                problems.append(f"{r.key}: requires {rk!r}, which is not in the table")
        if r.demo_arm == "remedy" and not any(get_rung(k, rungs).demo_arm == "placebo"
                                              for k in r.requires if k in keys):
            problems.append(f"{r.key}: a remedy rung must require a placebo rung (it never runs without one)")
        missing = [f for f in (r.prompt_file, r.system_file, r.definitions_file) if f and not (d / f).is_file()]
        for f in missing:
            problems.append(f"{r.key}: file {f} is missing from the prompts folder")
        if missing:
            continue
        t = texts[r.key] = load_rung_prompt(d, r)
        found = set(_PLACEHOLDER.findall(t))
        if not _REQUIRED_PLACEHOLDERS <= found:
            problems.append(f"{r.key}: placeholder(s) missing: {sorted(_REQUIRED_PLACEHOLDERS - found)}")
        if ("definitions" in found) != bool(r.definitions_file):
            problems.append(f"{r.key}: placeholder {{definitions}} and definitions_file disagree")
        blocks = {arm: t.count(render_demos(arm)) for arm in ("remedy", "placebo")}
        for arm, count in blocks.items():
            want = 1 if r.demo_arm == arm else 0
            if count != want:
                problems.append(f"{r.key}: the {arm} demo block appears {count} times in {r.prompt_file}, want {want}")
        if r.demo_arm and (d / _V2_PROMPT).is_file():
            base = Counter(_PLACEHOLDER.findall((d / _V2_PROMPT).read_text(encoding="utf-8")))
            if Counter(_PLACEHOLDER.findall(t)) != base:
                problems.append(f"{r.key}: placeholder set differs from {_V2_PROMPT}")
    by_arm = {r.demo_arm: r.key for r in rungs if r.demo_arm}
    if "remedy" in by_arm and "placebo" in by_arm and by_arm["remedy"] in texts and by_arm["placebo"] in texts:
        a, b = len(texts[by_arm["remedy"]]), len(texts[by_arm["placebo"]])
        if abs(a - b) / a > LENGTH_TOLERANCE:
            problems.append(f"remedy and placebo prompt lengths differ by more than 2% ({a} vs {b} characters)")
    return problems


# ----------------------------------------------------------------------------------------------
# Running one arm of the ladder (orchestration only; the pipeline owner calls this from the Qwen phase)
# ----------------------------------------------------------------------------------------------

ERROR_STATUS = "error"     # outside pipeline.DONE on purpose: Work.get never reports it as done


class ArmResult(NamedTuple):
    graph: MapGraph | None     # None for every status except "ok"
    status: str                # ok | failed | too_long_excluded | no_speakers_excluded (the pipeline's statuses), or
                               # "error": no attempt got a reply (endpoint down); never stored, redone on resume
    item_id: str               # the unsuffixed item id the call used: "<pid>__<spec key of the draw>"


def _arm_graph(d: dict | None) -> MapGraph | None:
    return MapGraph.from_json(json.dumps(d["map"])) if d and d.get("status") == "ok" and d.get("map") else None


def run_ladder_arm(rung: Rung, transcripts: Sequence[Transcript], spec: ReconSpec, draws: int | Iterable[int], *,
                   log, client, ctx, prompts_dir, real_data: bool, work=None, max_draws: int = REAL_DRAWS,
                   max_tokens: int = RECON_MAX_TOKENS, pid_of: Callable[[Transcript], str] = lambda t: t.transcript_id,
                   template: Mapping | None = None) -> dict[tuple[str, int], ArmResult]:
    """Run ``rung`` over ``transcripts`` for each draw and return ``{(map id, draw): ArmResult}``.

    Pure orchestration around ``reconstruct(..., rung=rung)``: nothing about prompts, validation, retries or
    sampling is decided here.
      * Gate first: ``validate_rungs(prompts_dir)`` must be empty, else RuntimeError listing the problems; no call
        is made. ``check_rung_spec(rung, <draw spec>, real_data=real_data)`` is also run for every draw BEFORE the
        first call (on real data: the primary setting only; the draw is the replicate index of ``spec`` and must be
        below ``max_draws``, the declared draw count, 3 by default).
      * ``real_data`` has no default: True on a real corpus, False on a synthetic one. Forgetting it is a TypeError
        before any call, so the primary-setting rule cannot be skipped by omission.
      * ``draws``: an int n (draws 0..n-1) or an iterable of draw indices. Draw ``d`` is ``spec`` with
        ``replicate=d``. Draw-major order (every map's draw 0, then every map's draw 1, ...), so a cut of the
        last draw is a clean cut.
      * The item id is ``"<pid>__<draw spec key>"`` (the pipeline's naming), so rung 2 draw 0 sends the very messages
        and seed of the existing main-run item. ``ctx``: a ``ReconContext`` or a function ``Transcript ->
        ReconContext`` (the pipeline's ``r.ctx(role)``). ``pid_of`` maps a transcript to the map id (default
        ``transcript_id``).
      * ``work`` (optional; the pipeline's ``Work``: ``get(stage, name)`` / ``put(stage, name, obj)``): items are
        stored under the store stage ``rung_stage(rung, spec).store`` (``recon:<rung key>``, one per rung) as
        ``{"status", "map", "prompt_sha256"}`` and a done item (any status) made with the same prompt files
        (``rung_prompt_sha256``) is not called again, so a killed run resumes; an item with another or no recorded
        identity is redone (so one arm never mixes two prompt versions).
      * A call whose every attempt ended in a transport error (no reply at all) comes back as status "error", is NOT
        stored (a resume redoes it) and is listed by ``error_items``; only replies that failed validation are the
        final "failed".
      * The removed arm on a transcript without speakers is excluded without a call (``no_speakers_excluded``), as
        in the pipeline.
      * ``template`` (rung 1 only; a tool run's ``tool.rung1_user`` block, K9): rung 1 sends the user's prompt and the
        app wrapper (``rung_messages``); the gate also checks the template and the stored identity is
        ``rung_prompt_sha256(..., template=template)``.
    Rung 3 must run together with the placebo (``rung.requires``); that pairing is the caller's loop, not this
    function's.
    """
    _check_template(rung, template)
    problems = validate_rungs(prompts_dir, rung1_template=template)
    if problems:
        raise RuntimeError("the rung table does not pass validate_rungs; no call was made: " + "; ".join(problems))
    n = list(range(draws)) if isinstance(draws, int) else [int(d) for d in draws]
    specs = {d: replace(spec, replicate=d) for d in n}
    for dspec in specs.values():
        check_rung_spec(rung, dspec, real_data=real_data, max_draws=max_draws)
    store = rung_stage(rung, spec).store
    identity = rung_prompt_sha256(prompts_dir, rung, template=template)
    out: dict[tuple[str, int], ArmResult] = {}
    for d in n:
        dspec = specs[d]
        for t in transcripts:
            pid = pid_of(t)
            name = f"{pid}__{dspec.key()}"
            done = work.get(store, name) if work is not None else None
            if done and done.get("prompt_sha256") != identity:
                done = None        # made with other instruction text (or none recorded): redo, never mix two prompts
            if done:
                out[(pid, d)] = ArmResult(_arm_graph(done), done["status"], name)
                continue
            if dspec.arm == "removed" and t.speaker_format == "none":
                g, status = None, "no_speakers_excluded"
            else:
                g, status = reconstruct(log, client, t, dspec, ctx(t) if callable(ctx) else ctx, prompts_dir,
                                        item_id=name, max_tokens=max_tokens, rung=rung, real_data=real_data,
                                        max_draws=max_draws, **({"rung_template": template} if template is not None else {}))
            if status == ERROR_STATUS:      # an outage, not a result: not stored, so a resume makes the call again
                tracker = getattr(work, "tracker", None)
                if tracker is not None:
                    tracker.take()          # drop the errored calls' ids so they do not stamp the next item
            elif work is not None:
                work.put(store, name, {"status": status, "map": asdict(g) if g else None, "prompt_sha256": identity,
                                       "accepted_attempt": None if g is None else (g.meta or {}).get("accepted_attempt")})
            out[(pid, d)] = ArmResult(g, status, name)
    return out


def preseed_rung2(work, prompts_dir, names: Iterable[str]) -> list[str]:
    """Take over the main run's draw-0 maps as rung 2 items (report 4.2): for each ``name`` (``<pid>__<spec key>``)
    with a done item in the ``recon`` store, write the same item into ``recon:rung2`` as an explicit REUSED kind, with
    ``reused_from`` = ``{"stage": "recon", "name": name}``, the source's status, map and request ids, and the identity of
    the rung-2 prompt files. ``prereg._work_problem`` holds such an item to its source (same status and request ids,
    calls the source owns) and does not count it as a second claim of those calls. Returns the names taken over; a name
    the main run has not done is skipped."""
    from .pipeline import DONE
    rung = get_rung("rung2")
    store = rung_stage(rung, ReconSpec("removed", "p0", 0.0)).store
    identity = rung_prompt_sha256(prompts_dir, rung)
    taken: list[str] = []
    for name in names:
        src = work.read_any("recon", name) if hasattr(work, "read_any") else work.get("recon", name)
        if not src or src.get("status") not in DONE:
            continue
        item = {"status": src["status"], "map": src.get("map"), "prompt_sha256": identity,
                "reused_from": {"stage": "recon", "name": name}}
        if "accepted_attempt" in src:        # the source's attempt travels with the map; an old source has none (unknown)
            item["accepted_attempt"] = src["accepted_attempt"]
        if hasattr(work, "path"):       # the real Work: write as is (put would stamp the tracker's pending ids instead)
            from .audit.verify import log_work_item
            from .pipeline import write_json_atomic
            p = work.path(store, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            full = {**item, "request_ids": list(src.get("request_ids") or [])}
            if getattr(work, "log", None) is not None:
                log_work_item(work.log, store, name, full)      # announce before the write, as Work.put does
            write_json_atomic(p, full)
        else:
            work.put(store, name, {**item, "request_ids": list(src.get("request_ids") or [])})
        taken.append(name)
    return taken


def error_items(results: Mapping[tuple[str, int], ArmResult]) -> list[tuple[str, int]]:
    """The (map id, draw) keys whose call never got a reply (status "error"). They are not parse failures and are not
    stored; run the arm again with the same work store until this list is empty, before analysing the arm."""
    return sorted(k for k, v in results.items() if v.status == ERROR_STATUS)


def anonymous_ids(pids: Iterable[str]) -> dict[str, str]:
    """``{pid: "#<position>"}`` over the sorted distinct ids (the form ``audits._Refs`` uses), for every output that
    reaches an aggregate of a real corpus: a participant id must never be written there (map ids in
    ``ladder_metrics`` per-map rows and keys, ``audit_inputs``, ``null_phase_share`` ...)."""
    return {p: f"#{i}" for i, p in enumerate(sorted(set(pids)))}


def audit_inputs(results: Mapping[tuple[str, int], ArmResult], texts: Mapping[str, str] | None = None, *,
                 anonymize: Mapping[str, str] | None = None) -> dict:
    """``run_ladder_arm`` results -> the ``outputs`` argument of ``audit_outputs``/``audit_rungs``/``null_phase_share``:
    ``{"<pid>#<draw>": (graph, transcript text) | graph | None}``. ``texts`` maps a map id to the text of the
    transcript the call saw (lets the auditor ignore wording that really is in the source); a missing graph stays
    None (counted as missing). ``anonymize`` (``anonymous_ids(...)``) replaces each pid in the keys, so the summaries
    built from this dict carry no participant id."""
    texts = texts or {}
    out: dict = {}
    for (pid, d), r in sorted(results.items()):
        out[f"{anonymize[pid] if anonymize else pid}#{d}"] = (
            None if r.graph is None else ((r.graph, texts[pid]) if pid in texts else r.graph))
    return out


def _call_counts(log_path, transport_only: bool = False) -> tuple[dict, dict]:
    """``({(stage, item id): corrective retries}, {(stage, item id): transport errors})`` from the log. A request chain
    is the run of ``call_request`` records of one (stage, item id) with attempt numbers 1, 2, 3...; a new attempt 1
    starts a new chain (a resume after a kill). An attempt is a CORRECTIVE retry when the previous attempt of its chain
    got a ``call_response`` whose ``validation_error`` is not null (the model was told what was wrong). A
    ``call_error`` (no reply at all) is counted separately and never as a corrective retry; with ``transport_only`` only
    the errors ``log_call`` flagged ``transport`` (an outage) are counted, not an HTTP 400 or a malformed body."""
    key_of: dict[str, tuple[str, str]] = {}
    prev_of: dict[str, str | None] = {}
    last: dict[tuple[str, str], tuple[int, str]] = {}
    invalid: dict[str, bool] = {}
    retries: Counter = Counter()
    errors: Counter = Counter()
    for rec in _read_records(log_path)[0]:
        p = rec["payload"]
        kind = rec["record_type"]
        if kind == "call_request":
            k = (str(p.get("stage")), str(p.get("item_id")))
            rid, attempt = str(p.get("request_id")), int(p.get("attempt", 1))
            key_of[rid] = k
            before = last.get(k)
            prev_of[rid] = before[1] if before and attempt == before[0] + 1 else None
            last[k] = (attempt, rid)
            retries.setdefault(k, 0)
        elif kind == "call_response":
            invalid[str(p.get("request_id"))] = p.get("validation_error") is not None
        elif kind == "call_error" and str(p.get("request_id")) in key_of and (p.get("transport") or not transport_only):
            errors[key_of[str(p.get("request_id"))]] += 1
    for rid, prev in prev_of.items():
        if prev is not None and invalid.get(prev):
            retries[key_of[rid]] += 1
    return dict(retries), dict(errors)


def retries_by_call(log_path) -> dict[tuple[str, str], int]:
    """``{(log stage, logged item id): corrective retries}`` (see ``_call_counts``; summed over resumed chains). A
    missing log gives {}."""
    return _call_counts(log_path)[0]


def call_errors_by_call(log_path, *, transport_only: bool = False) -> dict[tuple[str, str], int]:
    """``{(log stage, logged item id): attempts that got no reply}``; reported next to the retries, never inside them.
    ``transport_only``: only the attempts that ended in a transport error (the outages), which is what the report's
    "transport errors" column says."""
    return _call_counts(log_path, transport_only)[1]


def retries_by_item(log_path) -> dict[str, int]:
    """``{logged item id: corrective retries}`` from the hash-chained log (``retries_by_call`` without the stage; the
    largest over stages). A ladder call is logged as ``<name>@<rung key>`` (``rung_item_id``). Transport errors are not
    retries (``call_errors_by_call``). A missing log gives {}."""
    out: dict[str, int] = {}
    for (_stage, item), n in retries_by_call(log_path).items():
        out[item] = max(out.get(item, 0), n)
    return out


def draw_retries(calls: Mapping[tuple[str, str], int], item_id: str, rung: Rung) -> int:
    """Corrective retries of one ladder draw: ``calls`` is ``retries_by_call(log)``, ``item_id`` the unsuffixed
    ``<pid>__<spec key>``. The call is looked up as it was logged by the ladder, stage ``recon:<arm>:<rung>`` and id
    ``<item_id>@<rung>``. Rung 2 draw 0 taken over from the main run (``preseed_rung2``) was logged by the main run, as
    ``<item_id>`` in stage ``recon:<arm>``: rung 2 falls back to that call when the suffixed one is not in the log."""
    arm = item_id.split("__")[-4] if item_id.count("__") >= 4 else None
    own = calls.get((f"recon:{arm}:{rung.key}", rung_item_id(item_id, rung)))
    if own is None and rung.key == "rung2":
        own = calls.get((f"recon:{arm}", item_id))
    return own or 0


def arm_retries(log_path, results: Mapping[tuple[str, int], ArmResult], rung: Rung) -> dict[tuple[str, int], int]:
    """Corrective retries of each (pid, draw) of an arm (what ``ladder_metrics.draw_from_pair_inputs(retries=...)``
    takes), read from the log (``draw_retries``). An item resumed from the work store and called in an earlier run is
    still found as long as the same log is passed, and so is a rung 2 draw taken over from the main run."""
    calls = retries_by_call(log_path)
    return {k: draw_retries(calls, v.item_id, rung) for k, v in results.items()}


def _map_signature(g: MapGraph) -> str:
    """The content of a map as JSON, without the draw-specific bookkeeping (map id, meta)."""
    d = asdict(g)
    return json.dumps({k: d[k] for k in ("origin", "nodes", "edges", "emotion_word") if k in d}, sort_keys=True)


def draw_identity(results: Mapping[tuple[str, int], ArmResult]) -> dict:
    """How often draw d (d >= 1) of a map is byte-identical (map content, ids and quotes included) to draw 0 of the
    same map, among the pairs where both are ``ok`` (review I1). The primary setting is temperature 0, so the draws
    differ only in the seed and may come out (nearly) the same; the report's draw count and its power wording should
    rest on this share, not on the number of draws run. Returns n_maps (with a draw 0 and at least one compared draw),
    n_maps_all_identical, n_pairs, n_identical, share_identical (None when nothing was compared) and by_draw
    ({d: {n_pairs, n_identical}}). It carries no map id."""
    sig: dict[tuple[str, int], str] = {k: _map_signature(v.graph) for k, v in results.items()
                                       if v.status == "ok" and v.graph is not None}
    by_draw: dict[int, dict] = {}
    per_map: dict[str, list[bool]] = {}
    for (pid, d), s in sorted(sig.items()):
        if d == 0 or (pid, 0) not in sig:
            continue
        same = s == sig[(pid, 0)]
        row = by_draw.setdefault(d, {"n_pairs": 0, "n_identical": 0})
        row["n_pairs"] += 1
        row["n_identical"] += same
        per_map.setdefault(pid, []).append(same)
    n_pairs = sum(r["n_pairs"] for r in by_draw.values())
    n_same = sum(r["n_identical"] for r in by_draw.values())
    return {"n_maps": len(per_map), "n_maps_all_identical": sum(all(v) for v in per_map.values()),
            "n_pairs": n_pairs, "n_identical": n_same, "share_identical": n_same / n_pairs if n_pairs else None,
            "by_draw": by_draw}


def draw_identity_by_rung(results_by_rung: Mapping[str, Mapping[tuple[str, int], ArmResult]]) -> dict[str, dict]:
    """``draw_identity`` for each rung ({rung id: summary})."""
    return {rung: draw_identity(res) for rung, res in results_by_rung.items()}


def null_phase_share(outputs: Mapping[str, object]) -> dict:
    """Share of nodes with no phase (``phase`` None or empty) in one rung's outputs (review M5): the demos have null
    phases, and a model may imitate them, differently in the remedy and the placebo. Descriptive, reported per rung;
    scoring does not depend on phase. ``outputs`` has the shape ``audit_outputs`` takes (``audit_inputs`` builds
    it). Returns n_maps, n_missing (None outputs), n_nodes, n_null_phase, share (None when there is no node),
    n_maps_with_null and by_map (map id -> its null share)."""
    n_nodes = n_null = n_maps = n_missing = with_null = 0
    by_map: dict[str, float] = {}
    for key in sorted(outputs):
        v = outputs[key]
        if v is None:
            n_missing += 1
            continue
        g = v[0] if isinstance(v, tuple) else v
        n_maps += 1
        k = sum(1 for n in g.nodes if not n.phase)
        n_nodes += len(g.nodes)
        n_null += k
        with_null += k > 0
        if g.nodes:
            by_map[key] = k / len(g.nodes)
    return {"n_maps": n_maps, "n_missing": n_missing, "n_nodes": n_nodes, "n_null_phase": n_null,
            "share": n_null / n_nodes if n_nodes else None, "n_maps_with_null": with_null, "by_map": by_map}


def null_phase_by_rung(outputs_by_rung: Mapping[str, Mapping[str, object]]) -> dict[str, dict]:
    """``null_phase_share`` for each rung ({rung id: summary})."""
    return {rung: null_phase_share(outs) for rung, outs in outputs_by_rung.items()}
