"""Staged, resumable pipeline: corpus -> Q (reconstructor endpoint) -> J (judge endpoint) -> M (metrics, report).

Every item of every stage is one JSON file ``out_dir/work/<stage>/<name>.json``, written atomically
(temp file + ``os.replace``). A file that parses and carries a known ``status`` is done and is skipped on
the next run (resume), failures included (``{"status": "failed" | "too_long_excluded" |
"no_speakers_excluded"}``). An item whose model could not be reached (connection refused, timeout, HTTP 5xx after
the retries) is ``timeout_excluded``: kept and counted, but not done, so a rerun redoes it. Items whose inputs can change (canonicalization batches, alignments) also store
those inputs and are redone when they no longer match. Every model call goes through ``llm.log_call``
(inside the stage modules) into the run's hash-chained audit log; ``run`` adds one ``run_start`` and one
``run_end`` record, plus a ``stage_end`` record only for a stage that did new work.

A helper item (mapping-talk confirmation, in_transcript, canonicalization batch, alignment) whose model call never
got a valid answer is stored as ``{"status": "fallback"}``: counted, never used as a result, redone on resume.

Audit-log stage names: q:mapping_talk, judge:in_transcript, recon:full, recon:removed, prior_only, judge:modality,
judge:shuffled, judge:script_prior_only (judge2:script_prior_only for a judge2 source), judge:canon, judge:align,
judge2:modality.

Maps are "interviewer-drawn, participant-endorsed maps" (endorsed) and AI reconstructions (ai).
"""
from __future__ import annotations

import copy
import difflib
import glob
import hashlib
import json
import os
import time
import random
import re
import tempfile
import zipfile
import zlib
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from . import audits, codehash, ladder, ladder_metrics, metrics, prereg, report, toolcfg
from .audit.registry import AuditLog
from .audit.verify import log_work_item, verify_internal_chain
from .judge import JudgeCache, judge_modality_detailed, modality_items
from .llm import derive_seed, guard_real_clients
from .maps.readers import MAP_SUFFIXES, map_stem, read_map, rule_for
from .maps.vsdx import ExtractConfig
from .match import MatchSetting, align_labels, canonicalize, in_transcript
from . import prompt_manifest, textnorm
from .families import model_family, script_family
from .paths import stage_dirname, stage_from_dirname
from .prompt_manifest import verify_manifest
from .provenance import provenance_for
from .reconstruct import DEFAULT_PROMPT_VERSION, QUOTE_TIERS, VARIANTS, ReconContext, ReconSpec, reconstruct
from .schemas import MapGraph, Transcript
from .stats import derangements
from .textnorm import normalize
from .transcripts import (TranscriptRefused, confirm_mapping_talk, flag_mapping_talk, label_lookalikes, norm_label,
                          parse_paragraphs, read_transcript_paras)

KIT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = KIT_ROOT / "prompts"

STAGE_GROUPS = {
    "Q": ["mapping_talk_confirm", "recon", "prior_only"],                       # reconstructor endpoint (long inputs)
    # judge endpoint. in_transcript (the off-record check) runs on the JUDGE model, the one it is calibrated on;
    # its input is the whole transcript, so the judge needs a context window that holds one.
    # canonicalize runs before align: the canonical script alignments need it.
    # Order: in_transcript, judge_modality, judge_shuffled, script_prior_only, canonicalize, align, judge2_modality.
    "J": ["in_transcript", "judge_modality", "judge_shuffled", "script_prior_only", "canonicalize", "align",
          "judge2_modality"],
    "M": ["metrics", "report"],                                                # no model calls
}
STAGE_ORDER = [s for g in ("Q", "J", "M") for s in STAGE_GROUPS[g]]
CORPUS_KINDS = ("synthetic", "real")
# "timeout_excluded" (the model could not be reached) is NOT done: a rerun redoes it. "failed" and the not-judged
# labels are the model's own unusable answers and stay final.
DONE = ("ok", "failed", "too_long_excluded", "no_speakers_excluded")
CANON_BATCH = 50
NOT_JUDGED_LIMIT = 0.10


SCRIPT_SOURCE_KEYS = ("name", "role", "variants", "samples", "temperature")
SCRIPT_SOURCE_ROLES = ("judge", "judge2")
SCRIPT_STAGE_NAME = "script_prior_only"
# audit-log stage of a source's script calls: "<role>:script_prior_only", so the calls count under the role (judge or
# judge2) whose model wrote them (prereg.STAGE_ROLES maps the prefix to the role)
SCRIPT_STAGES = tuple(f"{role}:{SCRIPT_STAGE_NAME}" for role in SCRIPT_SOURCE_ROLES)
_SOURCE_NAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")


def _check_script_sources(sources) -> None:
    if not isinstance(sources, list):
        raise ValueError("script_sources must be a list")
    names: set[str] = set()
    for s in sources:
        if not isinstance(s, dict):
            raise ValueError("each script source must be an object")
        if "family" in s:
            raise ValueError("a script source has no 'family': whether it is another model family is derived "
                             "from the model ids in the audit log, never from config")
        if set(s) - set(SCRIPT_SOURCE_KEYS) or set(SCRIPT_SOURCE_KEYS) - set(s):
            raise ValueError(f"a script source needs exactly the keys {list(SCRIPT_SOURCE_KEYS)}, got {sorted(s)}")
        name = s["name"]
        if (not isinstance(name, str) or not _SOURCE_NAME.fullmatch(name) or "__" in name or name == "own"
                or len(name) > 24):
            raise ValueError(f"script source name {name!r}: letters, digits, '-' or '_' (single), at most 24 "
                             "characters, and not 'own' (the reconstructor's own script)")
        if name in names:
            raise ValueError(f"two script sources are named {name!r}")
        names.add(name)
        if s["role"] not in SCRIPT_SOURCE_ROLES:
            raise ValueError(f"script source {name}: role must be one of {SCRIPT_SOURCE_ROLES}")
        if not (isinstance(s["variants"], list) and s["variants"] and all(isinstance(v, str) for v in s["variants"])):
            raise ValueError(f"script source {name}: variants must be a non-empty list of prompt variants")
        if not (isinstance(s["samples"], int) and not isinstance(s["samples"], bool) and s["samples"] >= 1):
            raise ValueError(f"script source {name}: samples must be an integer of at least 1")
        if not isinstance(s["temperature"], (int, float)) or isinstance(s["temperature"], bool):
            raise ValueError(f"script source {name}: temperature must be a number")


LADDER_KEYS = ("rungs", "draws", "n_perm", "B", "seed")
# the order the ladder runs in on a real corpus is fixed by the design (synthesis item 8), whatever order a config
# lists the rungs in: rung 2, then the remedy interleaved with the placebo, then rung 1
LADDER_RUN_ORDER = ("rung2", "rung3", "placebo", "rung1")
# Recorded in run_start and the report (run-2 review 3.2): the arms run one after another, each over all participants, so
# server drift during the window is confounded with condition. Interleaving per participant is NOT done: DECISIONS C6
# puts rung 1 last so that a cut removes it first, and a rung is declared (a hash-chained record) before its first call.
LADDER_CALL_ORDER = "condition-major"
LADDER_DEFAULTS = {"n_perm": 10000, "B": 2000, "seed": 0}


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _check_ladder(lad, specs, kind) -> None:
    """ValueError unless ``lad`` is a usable ladder: known keys, rungs that exist in the registry (a wrong key never
    gets silently skipped), a rung's required companions present (rung 3 never without the placebo), a positive integer
    draw count, and a primary spec the ladder may run (the first spec; the primary SETTING exactly on a real corpus)."""
    if not isinstance(lad, dict):
        raise ValueError("ladder must be an object")
    if set(lad) - set(LADDER_KEYS):
        raise ValueError(f"ladder: unknown key(s) {sorted(set(lad) - set(LADDER_KEYS))}; allowed {list(LADDER_KEYS)}")
    rungs = lad.get("rungs")
    if not isinstance(rungs, list) or not rungs or not all(isinstance(k, str) for k in rungs):
        raise ValueError("ladder.rungs must be a non-empty list of rung keys")
    known = [r.key for r in ladder.RUNGS]
    if bad := [k for k in rungs if k not in known]:
        raise ValueError(f"ladder.rungs: unknown rung(s) {bad}; the registry keys are {known}")
    if len(set(rungs)) != len(rungs):
        raise ValueError("ladder.rungs lists a rung twice")
    for k in rungs:
        if missing := [q for q in ladder.get_rung(k).requires if q not in rungs]:
            raise ValueError(f"ladder.rungs: {k} never runs without {missing}")
    if not _is_int(lad.get("draws")) or lad["draws"] < 1:
        raise ValueError("ladder.draws must be an integer of at least 1")
    for k in ("n_perm", "B"):
        if k in lad and (not _is_int(lad[k]) or lad[k] < 1):
            raise ValueError(f"ladder.{k} must be a positive integer")
    if "seed" in lad and not _is_int(lad["seed"]):
        raise ValueError("ladder.seed must be an integer")
    primary = ReconSpec(**specs[0]) if specs else None
    if primary is not None and primary.temperature == 0 and lad["draws"] > 1:
        raise ValueError(f"ladder.draws is {lad['draws']} but the primary spec runs at temperature 0 (greedy): extra "
                         "draws would be copies of draw 0, not independent replicates. Use \"draws\": 1 "
                         "(DECISIONS C7), or sample at T>0")
    if primary is None or primary.variant != "p0":
        raise ValueError("ladder: the first spec (the primary) must be a p0 spec, the variant the rungs exist for")
    if kind == "real" and primary.key() != metrics.PRIMARY_SPEC:
        raise ValueError(f"ladder on a real corpus: the first spec must be the primary setting {metrics.PRIMARY_SPEC}")


READING3_KEYS = ("variant", "samples", "temperature")


def _check_reading3(r3) -> None:
    """ValueError unless ``r3`` is a usable ``reading3`` block: exactly the keys ``READING3_KEYS``, a prior-only prompt
    variant the kit has, one sample (reading 3 is one map per role) and a temperature that is a number >= 0."""
    if not isinstance(r3, dict):
        raise ValueError("reading3 must be an object")
    if unknown := sorted(set(r3) - set(READING3_KEYS)):
        raise ValueError(f"reading3: unknown key(s) {unknown}; allowed {list(READING3_KEYS)}")
    if missing := [k for k in READING3_KEYS if k not in r3]:
        raise ValueError(f"reading3: missing key(s) {missing}; needs exactly {list(READING3_KEYS)}")
    v = r3["variant"]
    if not (isinstance(v, str) and v in VARIANTS
            and (PROMPTS_DIR / f"prior_only.{v}.{DEFAULT_PROMPT_VERSION}.txt").is_file()):
        raise ValueError(f"reading3.variant: {v!r} is not a prior-only prompt variant of the kit {list(VARIANTS)}")
    if not _is_int(r3["samples"]) or r3["samples"] != 1:
        raise ValueError("reading3.samples: only 1 is supported (reading 3 is one map per role)")
    t = r3["temperature"]
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not t >= 0:
        raise ValueError("reading3.temperature must be a number of at least 0")


@dataclass
class RunConfig:
    run_id: str
    corpus: dict          # {"kind": "synthetic"|"real", "maps_glob", "transcripts_glob", "pairs": {pid: [map, transcript]}|None,
                          #  "participant_labels": {pid|"*": [labels]}, "interviewer_labels": {pid|"*": [labels]} (optional),
                          #  "roles": {pid: role}, "interviewers": {pid: code}, "synthetic_dir": dir (synthetic only)}
    context: dict         # {"domain", "roles": {role: role text}, "phases": [...], "extract": {ExtractConfig fields},
                          #  "max_input_words" (optional)}
    specs: list[dict]     # ReconSpec dicts; first = primary
    prior_only: dict      # {"variants": ["p0","p1","p2"], "samples": 5, "temperature": 0.7}
    settings: list[dict]  # MatchSetting dicts; first = primary
    models: dict          # {"recon": "RECON", "judge": "JUDGE", "judge2": "JUDGE2"|None} -> env prefixes
    out_dir: str          # work/ (per-item JSON) + aggregates/ + logs/
    prereg_required: bool = False      # optional: true makes run() refuse until prereg.require_frozen passes (no run needs it)
    stats: dict = field(default_factory=dict)   # headline options: B, n_perm, n_derangements, seed, min_words
    base_dir: str = "."   # relative corpus paths resolve against this (the config file's directory)
    script_sources: list = field(default_factory=list)   # other-model scripts for the script-conditional test: see
                          # SCRIPT_SOURCE_KEYS. {"name", "role": "judge"|"judge2", "variants", "samples", "temperature"}.
                          # The family ("other"/"own") is NOT a key: it is derived from the logged model ids.
    ladder: dict = field(default_factory=dict)   # the instruction ladder (see LADDER_KEYS): {} = no ladder. {"rungs":
                          # [registry keys], "draws": n} plus optional "n_perm", "B", "seed" of the ladder analysis
    tool: dict = field(default_factory=dict)     # tool-only switches the app writes (see toolcfg.TOOL_KEYS): {} = a
                          # study config, left out of sha256() so every study config keeps its hash
    reading3: dict | None = None   # optional: {"variant": "p0", "samples": 1, "temperature": 0} = reading 3 (the lead
                          # row and the floors) drawn apart from the script set ``prior_only``; None = reading 3 is the
                          # first prior-only map in sorted key order, as before (see ``reading3_specs``)

    def __post_init__(self) -> None:
        _check_script_sources(self.script_sources)
        kind = (self.corpus or {}).get("kind")
        if kind not in CORPUS_KINDS:      # case-sensitive: anything else could slip past the real-data guard
            raise ValueError(f"corpus.kind must be one of {CORPUS_KINDS}, got {kind!r}")
        if kind == "synthetic" and (why := prereg.synthetic_corpus_problem(self.corpus, self.base_dir)):
            raise ValueError(why)
        if self.ladder:
            _check_ladder(self.ladder, self.specs, kind)
        if self.reading3 is not None:
            _check_reading3(self.reading3)
        if self.tool or not isinstance(self.tool, dict):     # {} = a study config; None, [] etc. are refused
            toolcfg.check_tool(self.tool, self.corpus)

    @staticmethod
    def load(path, **overrides) -> "RunConfig":
        d = json.loads(Path(path).read_text(encoding="utf-8-sig"))      # a BOM (Notepad, PowerShell) is dropped
        d.setdefault("base_dir", str(Path(path).resolve().parent))
        d.update(overrides)
        if not Path(d["out_dir"]).is_absolute():
            d["out_dir"] = str((Path(d["base_dir"]) / d["out_dir"]).resolve())
        return RunConfig(**d)

    def sha256(self) -> str:
        """Hash of the run settings plus the content of the corpus input files; ``out_dir`` and ``base_dir`` are
        left out (they name places, not settings)."""
        d = {k: v for k, v in asdict(self).items() if k not in ("out_dir", "base_dir")}
        if not d.get("script_sources"):
            d.pop("script_sources", None)       # a config without script sources keeps the hash it always had
        if not d.get("ladder"):
            d.pop("ladder", None)               # ... and so does one without a ladder
        if not d.get("tool"):
            d.pop("tool", None)                 # ... and one without a tool block (every study config)
        if d.get("reading3") is None:
            d.pop("reading3", None)             # ... and one without a reading3 block
        d["corpus"] = prereg.corpus_without_paths(self.corpus)       # the inputs' content stands for their place
        d["corpus_files_sha256"] = prereg.corpus_files_digest(self.corpus, self.base_dir)
        return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else Path(self.base_dir) / q

    def recon_specs(self) -> list[ReconSpec]:
        return [ReconSpec(**s) for s in self.specs]

    def prior_specs(self) -> list[ReconSpec]:
        po = self.prior_only or {}
        return [ReconSpec("prior_only", v, float(po.get("temperature", 0.7)), r)
                for v in po.get("variants", []) for r in range(int(po.get("samples", 0)))]

    def reading3_specs(self) -> list[ReconSpec]:
        """The draw of reading 3 (one no-interview map per role, arm ``prior_only``) when the config has a ``reading3``
        block, else []: without the block reading 3 is taken from the script set (``pipeline._prior_key``)."""
        r3 = self.reading3
        return [ReconSpec("prior_only", r3["variant"], float(r3["temperature"]), 0)] if r3 is not None else []

    def source_specs(self, source: dict) -> list[ReconSpec]:
        """The prior-only draws of one script source: its variants x samples, at its temperature."""
        return [ReconSpec("prior_only", v, float(source["temperature"]), k)
                for v in source["variants"] for k in range(int(source["samples"]))]

    def match_settings(self) -> list[MatchSetting]:
        return [MatchSetting(**s) for s in self.settings]


@dataclass
class Participant:
    pid: str
    role: str
    interviewer: str | None
    map_path: Path
    transcript_path: Path


def prompt_set_sha256(prompts_dir=None) -> str | None:
    """The prompt-set hash COMPUTED from the prompt files on disk now (``prompt_manifest.build_manifest``), not the
    value MANIFEST.json claims; None if the folder cannot be read."""
    try:
        return prompt_manifest.build_manifest(prompts_dir or PROMPTS_DIR)["set_sha256"]
    except OSError:
        return None


def prompt_manifest_problem(prompts_dir=None) -> str | None:
    """None when the prompt files are exactly what ``prompts/MANIFEST.json`` records (every file hash, and the set
    hash recorded there equal to the one computed from the files), else a short reason."""
    d = prompts_dir or PROMPTS_DIR
    try:
        problems = prompt_manifest.verify_manifest(d)
        recorded = json.loads((Path(d) / prompt_manifest.MANIFEST_NAME).read_text(encoding="utf-8")).get("set_sha256")
    except (OSError, ValueError):
        return "MANIFEST.json cannot be read"
    now = prompt_set_sha256(d)
    if recorded != now and not problems:     # (an edited file already explains a different set hash)
        problems.append("the set hash recorded in MANIFEST.json is not the one computed from the files")
    return "; ".join(problems) if problems else None


def _per_pid(table: dict | None, pid: str) -> set[str]:
    table = table or {}
    return set(table.get(pid, table.get("*", [])))


@dataclass
class CorpusPairing:
    """The participants of a corpus and where their files sit. ``positions``: {pid: (map #, transcript #)}, each
    1-based in the sorted list of that kind's files (the glob's matches, or the sorted ``pairs``): a real corpus's
    refusals and warnings name these positions, never a file name or a participant id."""
    participants: list
    positions: dict
    real: bool
    not_in_roles: list = field(default_factory=list)     # pids with a map and a transcript but no entry in roles


def is_real_or_unknown(cfg: RunConfig) -> bool:
    """True unless the corpus is labelled synthetic: what a refusal may name is decided on the safe side."""
    return cfg.corpus.get("kind") != "synthetic"


def pair_corpus(cfg: RunConfig) -> CorpusPairing:
    """Participants sorted by pid: explicit ``pairs`` or, without them, map and transcript files paired by stem."""
    c = cfg.corpus
    pairs = c.get("pairs")
    positions: dict[str, tuple[int, int]] = {}
    real = is_real_or_unknown(cfg)
    if not pairs:
        maps = _by_stem(sorted(glob.glob(str(cfg.path(c["maps_glob"])))), "map", "maps_glob", real, stem=map_stem)
        trs = _by_stem(sorted(glob.glob(str(cfg.path(c["transcripts_glob"])))), "transcript", "transcripts_glob", real)
        pairs = {pid: [maps[pid][1], trs[pid][1]] for pid in sorted(set(maps) & set(trs))}
        positions = {pid: (maps[pid][0], trs[pid][0]) for pid in pairs}
    else:
        positions = {pid: (i, i) for i, pid in enumerate(sorted(pairs), 1)}
    roles, ivs = c.get("roles", {}), c.get("interviewers", {})
    people = [Participant(pid, roles[pid], ivs.get(pid), cfg.path(str(m)), cfg.path(str(t)))
              for pid, (m, t) in sorted(pairs.items()) if pid in roles]
    return CorpusPairing(people, positions, real, sorted(pid for pid in pairs if pid not in roles))


def _by_stem(files: list[str], kind: str, key: str, real: bool, stem=lambda f: Path(f).stem
             ) -> dict[str, tuple[int, Path]]:
    """{stem: (position, path)} of one kind's sorted files (``stem``: maps use the readers' ``map_stem``, so
    ``SYN-PAIR01.mapgraph.json`` pairs as ``SYN-PAIR01``). Two files with one stem (.docx and .txt) are refused, all of them
    at once: which one a run would read must never depend on the order the disk lists them in."""
    groups: dict[str, list[tuple[int, Path]]] = {}
    for i, f in enumerate(files, 1):
        groups.setdefault(stem(f), []).append((i, Path(f)))
    clashes = [(stem, got) for stem, got in sorted(groups.items()) if len(got) > 1]
    if clashes:
        lines = [" and ".join(f"{kind} #{i}" for i, _ in got) if real else
                 f"{stem}: " + " and ".join(f.name for _, f in got) for stem, got in clashes]
        n = len(clashes)
        raise InputsRefused(f"{n} participant id{'s' if n != 1 else ''} match{'' if n != 1 else 'es'} more than one "
                            f"file in {key}", lines,
                            tail="Keep one file per participant, or list each participant's files in corpus.pairs")
    return {stem: got[0] for stem, got in groups.items()}


def load_participants(cfg: RunConfig) -> list[Participant]:
    """Participants sorted by pid: explicit ``pairs`` or, without them, map and transcript files paired by stem."""
    return pair_corpus(cfg).participants


class InputsRefused(ValueError):
    """Input files the run will not read, all found in one pass before any stage (``check_inputs``). The message
    lists at most ``SHOWN`` of them and says how many more there are."""

    SHOWN = 3

    def __init__(self, head: str, problems: list[str] = (), tail: str = ""):
        self.problems = list(problems)
        shown = "; ".join(self.problems[:self.SHOWN])
        more = len(self.problems) - self.SHOWN
        super().__init__(head + (f": {shown}" if shown else "") +
                         (f"; and {more} more" if more > 0 else "") + (f". {tail}" if tail else ""))


@dataclass
class InputCheck:
    """What ``check_inputs`` found on a corpus that passed: the pairing, and console warnings (worded already)."""
    pairing: CorpusPairing
    warnings: list = field(default_factory=list)


def _file_label(kind: str, path, position: int, real: bool) -> str:
    """How a refusal or warning names one input file: ``transcript #3`` for a real corpus, the file name otherwise."""
    return f"{kind} #{position}" if real else Path(path).name


def _reason(exc: BaseException) -> str:
    """A short reason for a file that could not be read, never quoting the file (a real corpus's path included)."""
    from .maps.readers import MapRefused
    if isinstance(exc, MapRefused):
        return exc.detail
    if isinstance(exc, TranscriptRefused):
        return str(exc)
    if isinstance(exc, OSError):
        return f"cannot be opened ({exc.strerror or type(exc).__name__})"
    if isinstance(exc, zipfile.BadZipFile):
        return "not a zip file"
    if isinstance(exc, ET.ParseError):
        return "the XML inside is not well-formed (a damaged file)"
    return f"cannot be read ({type(exc).__name__})"


def _check_every_role_has_files(cfg: RunConfig, pairing: CorpusPairing) -> None:
    """Every participant in ``corpus.roles`` has a map and a transcript, else ``prereg.CorpusError`` naming, for each
    missing file, the name it needed and (synthetic corpus) up to 3 nearest names the glob did match."""
    c = cfg.corpus
    have = {p.pid for p in pairing.participants}
    missing = [pid for pid in sorted(c.get("roles") or {}) if pid not in have]
    if not missing:
        return
    pos = {pid: i for i, pid in enumerate(sorted(c.get("roles") or {}), 1)}
    lines = []
    if c.get("pairs"):
        lines = [f"participant {f'#{pos[pid]} of corpus.roles' if pairing.real else pid} has no entry in corpus.pairs"
                 for pid in missing]
    else:
        found = {kind: sorted(glob.glob(str(cfg.path(c[key])))) for kind, key in (("map", "maps_glob"),
                                                                                  ("transcript", "transcripts_glob"))}
        stems = {"map": {map_stem(f) for f in found["map"]}, "transcript": {Path(f).stem for f in found["transcript"]}}
        for pid in missing:
            for kind, key in (("map", "maps_glob"), ("transcript", "transcripts_glob")):
                if pid in stems[kind]:
                    continue
                if pairing.real:
                    lines.append(f"participant #{pos[pid]} of corpus.roles has no {kind} file that {key} matched")
                    continue
                want = (f"{pid}.<map suffix: {' '.join(MAP_SUFFIXES)}>" if kind == "map" else
                        f"{pid}.docx or {pid}.txt")
                by_id: dict[str, list[str]] = {}              # "SYN-OF01.mapgraph.json" is compared as "SYN-OF01"
                for f in found[kind]:
                    by_id.setdefault(Path(f).name.split(".")[0], []).append(Path(f).name)
                near = [n for k in difflib.get_close_matches(pid, list(by_id), n=3, cutoff=0.5) for n in by_id[k]][:3]
                lines.append(f"participant {pid} has no {kind} file: no file named {want} matched {key} "
                             f"({c[key]})" + (f"; nearest names: {', '.join(near)}" if near else ""))
    shown = "; ".join(lines[:InputsRefused.SHOWN])
    more = len(lines) - InputsRefused.SHOWN
    raise prereg.CorpusError(shown + (f"; and {more} more" if more > 0 else ""))


def _labels_in_neither_set(corpus: dict, p: Participant, paras: list[str]) -> dict[str, int]:
    """{normalised label: turns it opened} for the speaker labels of ``p``'s transcript that are in neither its
    participant_labels nor its interviewer_labels (read as interviewer turns). Empty when the config gives no
    interviewer_labels for ``p`` (then every other label is the interviewer by design) or no speaker could be told."""
    plabels = _per_pid(corpus.get("participant_labels"), p.pid)
    ilabels = _per_pid(corpus.get("interviewer_labels"), p.pid)
    if not plabels or not ilabels:
        return {}
    counts: Counter = Counter()
    t = parse_paragraphs(paras, plabels, p.pid, interviewer=p.interviewer, interviewer_labels=ilabels,
                         label_counts=counts)
    if t.speaker_format == "none":
        return {}
    known = {norm_label(x) for x in plabels | ilabels}
    return {lab: n for lab, n in counts.items() if lab not in known}


def duplicate_transcripts(participants) -> list[list[str]]:
    """Groups (pids, sorted, groups in pid order) of participants whose transcript FILES are byte-identical: the same
    interview filed twice, so it would count as two people in n and in the bootstrap. An empty file or one that cannot
    be read is left out (the run refuses or excludes it on its own). A warning and a report line only; nothing is
    excluded."""
    by_digest: dict[str, list[str]] = {}
    for p in sorted(participants, key=lambda x: x.pid):
        try:
            data = Path(p.transcript_path).read_bytes()
        except OSError:
            continue
        if data:
            by_digest.setdefault(hashlib.sha256(data).hexdigest(), []).append(p.pid)
    return [pids for pids in by_digest.values() if len(pids) > 1]


READ_ERRORS = (ValueError, OSError, KeyError, EOFError, zipfile.BadZipFile, ET.ParseError, zlib.error,
               NotImplementedError, UnicodeError)


def check_inputs(cfg: RunConfig) -> InputCheck:
    """Open every map and transcript once, before any stage, and refuse once (``InputsRefused``) listing every file
    that cannot be read: an empty, truncated or non-zip .docx, a .docx without word/document.xml or with that part over
    ``MAX_DOCX_XML_BYTES`` uncompressed, a .txt that is not UTF-8, a map its reader refuses. Reads only; writes
    nothing."""
    pairing = pair_corpus(cfg)
    real = pairing.real
    c = cfg.corpus
    _check_every_role_has_files(cfg, pairing)
    extract = ExtractConfig(**{k: tuple(v) if isinstance(v, list) else v
                               for k, v in (cfg.context.get("extract") or {}).items()})
    problems: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []
    lookalike: list[str] = []
    for p in pairing.participants:
        m_pos, t_pos = pairing.positions.get(p.pid, (0, 0))
        try:
            read_map(p.map_path, p.pid, extract, rule_for(c, p.map_path))
        except READ_ERRORS as exc:
            problems.append(f"{_file_label('map', p.map_path, m_pos, real)}: {_reason(exc)}")
        try:
            paras = read_transcript_paras(p.transcript_path)
        except READ_ERRORS as exc:
            problems.append(f"{_file_label('transcript', p.transcript_path, t_pos, real)}: {_reason(exc)}")
            continue
        if (extra := _labels_in_neither_set(c, p, paras)):
            n_turns = sum(extra.values())
            what = f"{len(extra)} label{'s' if len(extra) != 1 else ''}" if real else ", ".join(sorted(extra))
            unknown.append(f"{_file_label('transcript', p.transcript_path, t_pos, real)}: {n_turns} "
                           f"turn{'s' if n_turns != 1 else ''} ({what})")
        if (like := label_lookalikes(paras, _per_pid(c.get("participant_labels"), p.pid), p.pid,
                                     _per_pid(c.get("interviewer_labels"), p.pid))):
            n_lines = sum(like.values())
            what = f"{len(like)} spelling{'s' if len(like) != 1 else ''}" if real else ", ".join(sorted(like))
            lookalike.append(f"{_file_label('transcript', p.transcript_path, t_pos, real)}: {n_lines} "
                             f"line{'s' if n_lines != 1 else ''} ({what})")
    if problems:
        n = len(problems)
        raise InputsRefused(f"{n} input file{'s' if n != 1 else ''} cannot be read", problems)
    if unknown:
        n, more = len(unknown), len(unknown) - InputsRefused.SHOWN
        warnings.append(f"{n} transcript{'s' if n != 1 else ''} ha{'ve' if n != 1 else 's'} turns opened by a speaker "
                        "label in neither participant_labels nor interviewer_labels; they are read as interviewer "
                        "turns (two-party interviews are assumed): " + "; ".join(unknown[:InputsRefused.SHOWN])
                        + (f"; and {more} more" if more > 0 else ""))
    if lookalike:
        n, more = len(lookalike), len(lookalike) - InputsRefused.SHOWN
        warnings.append(f"{n} transcript{'s' if n != 1 else ''} ha{'ve' if n != 1 else 's'} lines that look like a speaker "
                        "label but are not read as one (the label in another letter case, or with a space before the "
                        "colon); such a line is joined to the previous turn or keeps a stray colon, so check how the "
                        "labels are spelled in the file: " + "; ".join(lookalike[:InputsRefused.SHOWN])
                        + (f"; and {more} more" if more > 0 else ""))
    if (dups := duplicate_transcripts(pairing.participants)):
        by_pid = {p.pid: p for p in pairing.participants}

        def named(pid: str) -> str:
            return _file_label("transcript", by_pid[pid].transcript_path, pairing.positions.get(pid, (0, 0))[1], real)
        copies = sum(len(g) - 1 for g in dups)
        groups = [" = ".join(named(pid) for pid in g) for g in dups]
        more = len(groups) - InputsRefused.SHOWN
        warnings.append(f"{copies} transcript{'s' if copies != 1 else ''} {'are' if copies != 1 else 'is'} "
                        "a byte-identical copy of another one, so the same interview would count as more than one "
                        "person in n and in the intervals; nothing is excluded: " + "; ".join(groups[:InputsRefused.SHOWN])
                        + (f"; and {more} more" if more > 0 else ""))
    if pairing.not_in_roles:
        n = len(pairing.not_in_roles)
        who = [(f"map #{pairing.positions[pid][0]} and transcript #{pairing.positions[pid][1]}"
                if pairing.positions[pid][0] != pairing.positions[pid][1] or not c.get("pairs")
                else f"pair #{pairing.positions[pid][0]}") if real else pid for pid in pairing.not_in_roles]
        more = n - InputsRefused.SHOWN
        warnings.append(f"{n} paired file{'s' if n != 1 else ''} ha{'ve' if n != 1 else 's'} no entry in corpus.roles "
                        f"and {'are' if n != 1 else 'is'} not analysed: " + ", ".join(who[:InputsRefused.SHOWN])
                        + (f" and {more} more" if more > 0 else "") + " (counted as not_in_roles)")
    return InputCheck(pairing, warnings)


class Work:
    """The per-item store under ``out_dir/work``."""

    def __init__(self, out_dir, tracker=None, log=None):
        self.root = Path(out_dir) / "work"
        self.tracker = tracker      # prereg.RequestTracker: stamps each item with the calls that produced it
        self.log = log              # every write is announced here first, with the item's sha256 (work_item)

    def path(self, stage: str, name: str) -> Path:
        return self.root / stage_dirname(stage) / f"{name}.json"

    def get(self, stage: str, name: str) -> dict | None:
        """The item if it is done (valid JSON object with a known status), else None."""
        p = self.path(stage, name)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("status") in DONE else None

    def put(self, stage: str, name: str, obj: dict) -> None:
        p = self.path(stage, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        item = obj if self.tracker is None else {**obj, "request_ids": self.tracker.take()}
        if self.log is not None:
            log_work_item(self.log, stage, name, item)      # before the write: see log_work_item
        write_json_atomic(p, item)

    def remove(self, stage: str, name: str) -> None:
        """Delete an item (logged as removed first)."""
        if self.log is not None:
            log_work_item(self.log, stage, name, None)
        self.path(stage, name).unlink()

    def read_any(self, stage: str, name: str) -> dict | None:
        """The item whatever its status (a "fallback" item included), or None if absent or unreadable."""
        try:
            d = json.loads(self.path(stage, name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None

    def names(self, stage: str) -> list[str]:
        d = self.root / stage_dirname(stage)
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def statuses(self, stage: str) -> dict[str, int]:
        """Item counts by status, "fallback" (not done, redone on resume) included."""
        out: dict[str, int] = {}
        for name in self.names(stage):
            d = self.read_any(stage, name)
            if d is not None and "status" in d:
                out[d["status"]] = out.get(d["status"], 0) + 1
        return dict(sorted(out.items()))


class RungWork:
    """The run's ``Work`` seen through one ladder rung: the per-AI-map stages get ``:<rung key>`` appended (``recon``,
    ``judge_modality`` and the ``align`` items of AI maps, whose names contain ``__ai_``), every other stage (the
    confirmed mapping talk, in_transcript, the prior-only maps, the canonicalization batches, the shared alignments, the
    judge cache file at the root) stays the run's own. ``rung_view`` hands this to the stage functions, so they judge and
    align a rung's maps with the code that judges and aligns the main run's, and cannot write to a shared item."""

    SCOPED = ("recon", "judge_modality")

    def __init__(self, work: Work, key: str):
        self._w, self.key = work, key
        self.root, self.tracker, self.log = work.root, work.tracker, work.log

    def _stage(self, stage: str, name: str | None = None) -> str:
        if stage in self.SCOPED or (stage == "align" and name is not None and "__ai_" in name):
            return f"{stage}:{self.key}"
        return stage

    def path(self, stage, name): return self._w.path(self._stage(stage, name), name)
    def get(self, stage, name): return self._w.get(self._stage(stage, name), name)
    def put(self, stage, name, obj): return self._w.put(self._stage(stage, name), name, obj)
    def remove(self, stage, name): return self._w.remove(self._stage(stage, name), name)
    def read_any(self, stage, name): return self._w.read_any(self._stage(stage, name), name)
    def names(self, stage): return self._w.names(self._stage(stage))
    def statuses(self, stage): return self._w.statuses(self._stage(stage))


def write_json_atomic(path, obj, *, indent=None) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, sort_keys=True, ensure_ascii=False, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _graph(d: dict) -> MapGraph:
    return MapGraph.from_json(json.dumps(d))


def _pairs_out(judged: dict) -> list:
    return [[i, j, rel] for (i, j), rel in sorted(judged.items())]


def _pairs_in(rows: list) -> dict:
    return {(int(i), int(j)): rel for i, j, rel in rows}


# a model server's refusal of an input that is longer than its context window (llama.cpp: "the request exceeds the
# available context size"; OpenAI-style: "maximum context length"); only ever applied to HTTP 400 errors
_CONTEXT_400 = re.compile(r"HTTP 400.*(context|too long|too large|exceed|n_ctx|maximum.{0,20}(length|tokens))",
                          re.IGNORECASE | re.DOTALL)


class _Tap:
    """The run's audit log, seen through one item: every record appended while the item runs is kept, so the
    pipeline can tell a helper call that never got a valid answer (and fell back) from one that did."""

    def __init__(self, log):
        self.log = log
        self.records: list[tuple[str, dict]] = []

    def append(self, record_type: str, payload):
        rec = self.log.append(record_type, payload)
        self.records.append((record_type, payload))
        return rec

    def __getattr__(self, name):          # note_request_ids, path, ...: whatever the wrapped log offers
        return getattr(self.log, name)

    def transport_failed_calls(self) -> int:
        """Calls (``log_call`` item ids) that never got an answer from the model and whose LAST attempt ended in an
        outage (``call_error`` with ``transport``): the item is redone on a rerun. A call in which the model answered
        at ANY attempt (even unusably) is the model's own failure and is final: redoing it would give it fresh
        attempts and lower the parse-failure rates the ladder reports."""
        last: dict[str, str] = {}
        item_of: dict[str, str] = {}
        outage: set[str] = set()
        answered: set[str] = set()
        for rt, pl in self.records:
            if rt == "call_request":
                last[pl["item_id"]] = pl["request_id"]
                item_of[pl["request_id"]] = pl["item_id"]
            elif rt == "call_error" and pl.get("transport"):
                outage.add(pl["request_id"])
            elif rt == "call_response" and pl["request_id"] in item_of:
                answered.add(item_of[pl["request_id"]])
        return sum(1 for item, rid in last.items() if rid in outage and item not in answered)

    def context_length_errors(self) -> int:
        """Calls of this item the model server refused for their size (HTTP 400 naming the context window): the
        input does not fit the model, and asking again changes nothing."""
        item_of = {pl["request_id"]: pl["item_id"] for rt, pl in self.records if rt == "call_request"}
        return len({item_of.get(pl["request_id"]) for rt, pl in self.records
                    if rt == "call_error" and _CONTEXT_400.search(str(pl.get("error", "")))})

    def failed_calls(self) -> int:
        """Calls (``log_call`` item ids) of this item with no valid response: each one is a helper fallback."""
        item_of: dict[str, str] = {}
        ok: set[str] = set()
        for rt, pl in self.records:
            if rt == "call_request":
                item_of[pl["request_id"]] = pl["item_id"]
            elif rt == "call_response" and pl.get("validation_error") is None and pl["request_id"] in item_of:
                ok.add(item_of[pl["request_id"]])
        return len(set(item_of.values()) - ok)


def _status(tap: _Tap) -> dict:
    """{"status": "ok"} or, when any call of the item fell back, {"status": "fallback", "fallback_calls": n}, or
    "timeout_excluded" when one of those calls never reached the model: a fallback / timeout_excluded item is kept
    for inspection, counted, redone on resume and never used as a result."""
    n = tap.failed_calls()
    if not n:
        return {"status": "ok"}
    return {"status": "timeout_excluded" if tap.transport_failed_calls() else "fallback", "fallback_calls": n}


def _outage(status: str, tap: _Tap) -> str:
    """A "failed" model item whose last attempt hit an outage is "timeout_excluded" (redone on rerun); a failure
    that came from the model's own answers stays "failed"."""
    return "timeout_excluded" if status == "failed" and tap.transport_failed_calls() else status


def attempt_of(g: MapGraph | None) -> int | None:
    """The attempt at which a reconstruction was accepted (``reconstruct`` records it in the map's meta), for the work
    item's ``accepted_attempt``; None when there is no map (the item failed)."""
    return None if g is None else (g.meta or {}).get("accepted_attempt")


def map_digest(g: MapGraph) -> str:
    return hashlib.sha256(json.dumps(asdict(g), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class _Run:
    """State shared by the stages of one ``run`` call (inputs are read lazily and cached)."""

    item_suffix = ""        # appended to the audit-log item id of a judge or alignment call ("@rung3" in a rung view)

    def __init__(self, cfg: RunConfig, clients: dict, log: AuditLog):
        self.cfg, self.clients, self.log = cfg, clients, log
        # Items are stamped with the calls that produced them for a real corpus always (a validity check, not a
        # pre-registration lock) and for any config that opts in to the prereg check.
        stamp = cfg.prereg_required or cfg.corpus.get("kind") == "real"
        if stamp:
            self.log = prereg.RequestTracker(log)
        self.work = Work(cfg.out_dir, self.log if stamp else None, log=self.log)
        self.pairing = pair_corpus(cfg)
        self.people = self.pairing.participants
        self.specs = cfg.recon_specs()
        self.prior_specs = cfg.prior_specs()
        self.reading3_specs = cfg.reading3_specs()
        self.extract = ExtractConfig(**{k: tuple(v) if isinstance(v, list) else v
                                        for k, v in (cfg.context.get("extract") or {}).items()})
        self._endorsed: dict[str, MapGraph] = {}
        self._base: dict[str, Transcript] = {}
        self._guard = None
        self.audit_results: list = []
        self.canary_done = False
        self.preflight_result = None
        self._preflighted = False
        audits.begin_session(self)

    def leak_guard(self):
        """The pre-write leak guard of a real corpus (None for a synthetic one), built once from the real files
        in memory. If it cannot be built the run is blocked, never continued unguarded."""
        if is_synthetic(self.cfg):
            return None
        if self._guard is None:
            try:
                self._guard = audits.LeakGuard.from_config(self.cfg)
            except audits.IndexUnavailable as exc:
                raise audits.AuditBlocked([audits.AuditResult(
                    "leaks", True, False, {}, [f"index_unavailable:{exc.code}"])]) from None
        return self._guard

    # inputs
    def client(self, role: str):
        if not self._preflighted:           # the first real model call of the session comes after a small canary
            self._preflighted = True
            audits.preflight(self)
        c = self.clients.get(role)
        if c is None:
            raise RuntimeError(f"no {role!r} client configured")
        return c

    def endorsed(self, p: Participant) -> MapGraph:
        if p.pid not in self._endorsed:
            g = read_map(p.map_path, p.pid, self.extract, rule_for(self.cfg.corpus, p.map_path))
            g.interviewer = p.interviewer
            self._endorsed[p.pid] = g
        return self._endorsed[p.pid]

    def rule_flagged(self, p: Participant) -> Transcript:
        if p.pid not in self._base:
            c = self.cfg.corpus
            t = parse_paragraphs(read_transcript_paras(p.transcript_path),
                                 _per_pid(c.get("participant_labels"), p.pid), p.pid, interviewer=p.interviewer,
                                 interviewer_labels=_per_pid(c.get("interviewer_labels"), p.pid) or None)
            self._base[p.pid] = flag_mapping_talk(t)
        return self._base[p.pid]

    def transcript(self, p: Participant) -> Transcript | None:
        """The transcript with the confirmed mapping-talk flags, or None before confirmation ran."""
        d = self.work.get("mapping_talk_confirm", p.pid)
        if d is None:
            return None
        t = self.rule_flagged(p)
        keep = set(d.get("mapping_talk_turns", []))
        out = Transcript(t.transcript_id, [type(x)(x.idx, x.speaker, x.text, x.idx in keep) for x in t.turns],
                         t.speaker_format, t.interviewer, t.speaker_inference)
        return out

    def ctx(self, role: str) -> ReconContext:
        c = self.cfg.context
        return ReconContext(domain=c["domain"], role=c["roles"][role], phases=tuple(c["phases"]),
                            max_input_words=int(c.get("max_input_words", 45000)))

    def ai_map(self, pid: str, spec_key: str) -> MapGraph | None:
        d = self.work.get("recon", f"{pid}__{spec_key}")
        return _graph(d["map"]) if d and d["status"] == "ok" else None

    def judged(self, stage: str, name: str, g: MapGraph) -> dict | None:
        """The judge item for map ``g`` if it is done AND was judged on this very map (digest), else None."""
        d = self.work.get(stage, name)
        return d if d and d.get("map_sha256") == map_digest(g) else None

    def prior_maps(self, role: str) -> dict[str, MapGraph]:
        """The script set of ``role`` (the ``prior_only`` block's draws), never the separate reading-3 map."""
        out = {}
        for s in self.prior_specs:
            d = self.work.get("prior_only", f"{role}__{s.key()}")
            if d and d["status"] == "ok":
                out[s.key()] = _graph(d["map"])
        return out

    def reading3_key(self) -> str | None:
        """The spec key of reading 3 when the config has a ``reading3`` block, else None."""
        return self.reading3_specs[0].key() if self.reading3_specs else None

    def reading3_separate(self) -> bool:
        """Is reading 3 a map apart from the script set? False without the block and when its key is also a script
        key (one shared draft then serves both)."""
        k = self.reading3_key()
        return k is not None and k not in {s.key() for s in self.prior_specs}

    def reading3_map(self, role: str) -> MapGraph | None:
        """The reading-3 map of ``role`` (block present and its item ok), else None."""
        k = self.reading3_key()
        d = self.work.get("prior_only", f"{role}__{k}") if k else None
        return _graph(d["map"]) if d and d["status"] == "ok" else None

    def roles(self) -> list[str]:
        return sorted({p.role for p in self.people})

    def record_models(self, stages: list[str]) -> None:
        """Remember which model ids produced the work (``work/models.json``), so a metrics-only run, which has
        no clients, still reports them."""
        if not any(s not in STAGE_GROUPS["M"] for s in stages) or not self.clients:
            return
        p = self.work.root / "models.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(p, {**self.recorded_models(), **model_ids(self.clients)})

    def recorded_models(self) -> dict:
        try:
            return json.loads((self.work.root / "models.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


# ------------------------------------------------------------------ Q stages

def _stage_mapping_talk_confirm(r: _Run) -> int:
    n = 0
    for p in r.people:
        if r.work.get("mapping_talk_confirm", p.pid):
            continue
        t = r.rule_flagged(p)
        if t.speaker_format == "none":
            r.work.put("mapping_talk_confirm", p.pid, {"status": "ok", "speaker_format": "none",
                                                       "mapping_talk_turns": [], "stats": None})
        else:
            tap = _Tap(r.log)
            confirmed, st = confirm_mapping_talk(t, tap, r.client("recon"), item_id=p.pid, prompts_dir=PROMPTS_DIR,
                                                 stage="q:mapping_talk")
            r.work.put("mapping_talk_confirm", p.pid, {
                **_status(tap), "speaker_format": t.speaker_format, "stats": st,
                "mapping_talk_turns": [x.idx for x in confirmed.turns if x.mapping_talk]})
        n += 1
    return n


def _mode_b_mismatch(run_type: str | None, pid: str, g: MapGraph) -> str | None:
    """Audit K9: why the run's reference type and a reference file's ``meta.reference_type`` disagree about mode B,
    or None. The mode-B skip needs both to say "B"; a study config (no type) never has a mode-B file."""
    meta = (g.meta or {}).get("reference_type")
    if (run_type == "B") == (meta == "B"):
        return None
    return (f"refusing to run in_transcript: the run's reference type is {json.dumps(run_type)} but the reference "
            f"file of {pid} records reference type {json.dumps(meta)}; a mode-B (span-coded) reference needs a "
            "mode-B run, and a mode-B run needs mode-B reference files")


def _stage_in_transcript(r: _Run) -> int:
    from . import reference
    run_type = reference.reference_type(r.cfg)
    if run_type is not None:            # a tool run: every pending file is checked before any judge call or work item
        for p in r.people:
            if not r.work.get("in_transcript", p.pid) and (why := _mode_b_mismatch(run_type, p.pid, r.endorsed(p))):
                raise ValueError(why)
    n = 0
    for p in r.people:
        if r.work.get("in_transcript", p.pid):
            continue
        e = r.endorsed(p)
        if why := _mode_b_mismatch(run_type, p.pid, e):     # a study config: checked in its usual reading order
            raise ValueError(why)
        if run_type == "B":     # a researcher's span-coded reading (file meta "B" too): in the transcript by construction
            # L33: the marker and the file's own type (just agreed by K9) let the run-validity check accept the item
            r.work.put("in_transcript", p.pid, {"status": "ok", "in_transcript": {x.node_id: True for x in e.nodes},
                                                "by_construction": prereg.MODE_B_MARK,
                                                "reference_file_type": (e.meta or {}).get("reference_type")})
            n += 1
            continue
        tap = _Tap(r.log)
        got = in_transcript(tap, r.client("judge"), [x.text for x in e.nodes], r.rule_flagged(p), PROMPTS_DIR,
                            item_id=p.pid, stage="judge:in_transcript")
        st = _status(tap)
        if st["status"] == "fallback" and tap.context_length_errors():
            # the transcript does not fit the judge's context window: final (a rerun would fail the same way), and
            # reported as its own exclusion, not as a Q-phase label
            r.work.put("in_transcript", p.pid, {**st, "status": "too_long_excluded", "reason": "judge_context"})
        else:
            r.work.put("in_transcript", p.pid, {**st, "in_transcript": {x.node_id: bool(got.get(i, False))
                                                                       for i, x in enumerate(e.nodes)}})
        n += 1
    return n


def _stage_recon(r: _Run) -> int:
    n = 0
    for spec in r.specs:                      # primary first, then the order the config lists
        for p in r.people:
            name = f"{p.pid}__{spec.key()}"
            if r.work.get("recon", name):
                continue
            t = r.transcript(p)
            if t is None:
                continue                      # confirmation has not run for this participant
            if spec.arm == "removed" and t.speaker_format == "none":
                r.work.put("recon", name, {"status": "no_speakers_excluded"})
            else:
                tap = _Tap(r.log)
                g, status = reconstruct(tap, r.client("recon"), t, spec, r.ctx(p.role), PROMPTS_DIR, item_id=name)
                r.work.put("recon", name, {"status": _outage(status, tap), "map": asdict(g) if g else None,
                                           "accepted_attempt": attempt_of(g)})
            n += 1
    return n + _ladder_recon(r)


# ------------------------------------------------------------------ the instruction ladder (Q phase)

def ladder_rungs(cfg: RunConfig) -> list[str]:
    """The configured rung keys in the order the design runs them (``LADDER_RUN_ORDER``), whatever order the config
    lists them in; ``[]`` without a ladder."""
    keys = set((cfg.ladder or {}).get("rungs", ()))
    return [k for k in LADDER_RUN_ORDER if k in keys]


def ladder_params(cfg: RunConfig) -> dict:
    """{"n_perm", "B", "seed"} of the ladder analysis: the declaration (``_declare_once``) and the report
    (``_ladder_section``) both read them here, so the report matches what was declared."""
    lad = cfg.ladder or {}
    return {k: int(lad.get(k, LADDER_DEFAULTS[k])) for k in LADDER_DEFAULTS}


def _ladder_label(cfg: RunConfig, key: str) -> str:
    """The rung a declaration is filed under: the placebo is declared with the remedy ("rung3") when that runs."""
    return "rung3" if key == "placebo" and "rung3" in ladder_rungs(cfg) else key


def _declare_once(r: _Run, key: str) -> None:
    """Before the first call of a rung: a dated ``declared_contrasts`` record in the hash-chained log (contrasts,
    outcomes, draws per arm, test parameters, verdict rules, the prompt-set hash in the note). A record, not a lock
    (L6): nothing reads it to permit or refuse a call. Once per label, so a resumed run adds nothing; an amendment
    (``declare_contrasts(amends=...)``) counts as the declaration."""
    label = _ladder_label(r.cfg, key)
    live = [d for d in ladder_metrics.read_declarations(r.log.path) if not d["superseded"]]
    if any(d["payload"].get("rung") == label for d in live):
        return
    n = int(r.cfg.ladder["draws"])
    ladder_metrics.declare_contrasts(
        r.log, rung=label, draws={ladder.ARM_OF_RUNG[k]: n for k in ladder_rungs(r.cfg)}, **ladder_params(r.cfg),
        corpus_kind=r.cfg.corpus.get("kind"), note="prompt_set_sha256=" + (prompt_set_sha256() or ""))


def _preseed_rung2(r: _Run, spec: ReconSpec) -> None:
    """Rung 2 draw 0 IS the main run's primary-spec map: take over the done items not yet in the rung 2 store
    (``ladder.preseed_rung2``: an explicit reused item, never a byte copy or a second claim of the same call)."""
    store = ladder.rung_stage(ladder.get_rung("rung2"), spec).store
    names = [f"{p.pid}__{spec.key()}" for p in r.people]
    ladder.preseed_rung2(r.work, PROMPTS_DIR, [x for x in names if r.work.get(store, x) is None])


class _LazyClient:
    """A client looked up (``_Run.client``, which runs the session's preflight) when a call is about to be made, not
    before: a ladder with every cell already stored makes no call and so reaches no endpoint."""

    def __init__(self, get):
        self._get, self._client = get, None

    def _real(self):
        if self._client is None:
            self._client = self._get()
        return self._client

    def complete(self, *args, **kw):
        return self._real().complete(*args, **kw)

    def __getattr__(self, name):          # cfg, model_label, ...: whatever the client offers
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._real(), name)


def _ladder_recon(r: _Run) -> int:
    """The Q-phase calls of the instruction ladder (``cfg.ladder``): rung 2 (draw 0 taken over from the main run, the
    other draws called), then the remedy interleaved with the placebo draw by draw, then rung 1 (DECISIONS C6; a cut
    removes rung 1 first, then the last draw, then 3 + P together). The spec of every call is the primary spec with the
    draw as replicate; on a real corpus ``run_ladder_arm`` allows nothing else (``real_data`` comes from
    ``cfg.corpus["kind"]``), and the check runs here first so a refusal comes before any model call. A call that got no
    reply is not stored (the same command, rerun, makes it again). Returns the number of new work items."""
    keys = ladder_rungs(r.cfg)
    if not keys:
        return 0
    spec, draws = r.specs[0], int(r.cfg.ladder["draws"])
    real = r.cfg.corpus.get("kind") == "real"
    for k in keys:
        for d in range(draws):
            ladder.check_rung_spec(ladder.get_rung(k), replace(spec, replicate=d), real_data=real, max_draws=draws)
    role = {p.pid: p.role for p in r.people}
    ts = [t for p in r.people if (t := r.transcript(p)) is not None]
    common = dict(log=r.log, client=_LazyClient(lambda: r.client("recon")), ctx=lambda t: r.ctx(role[t.transcript_id]),
                  prompts_dir=PROMPTS_DIR, work=r.work, real_data=real, max_draws=draws)

    def count() -> int:
        return sum(len(r.work.names(f"recon:{k}")) for k in keys)

    before = count()
    if "rung2" in keys:
        _declare_once(r, "rung2")
        _preseed_rung2(r, spec)
        # draw 0 is the main run's item and only ever a copy of it (a main item that is not done leaves a gap that the
        # rerun fills after the main item is redone): never a second call whose map could differ from the headline's
        ladder.run_ladder_arm(ladder.get_rung("rung2"), ts, spec, range(1, draws), **common)
    pair = [k for k in ("rung3", "placebo") if k in keys]
    if pair:
        _declare_once(r, pair[0])
        for d in range(draws):                       # draw-major: cutting the last draw is a clean cut
            for k in pair:
                ladder.run_ladder_arm(ladder.get_rung(k), ts, spec, [d], **common)
    if "rung1" in keys:
        _declare_once(r, "rung1")
        # K9: a tool run may carry the user's own rung-1 prompt (and the app wrapper) in tool.rung1_user
        ladder.run_ladder_arm(ladder.get_rung("rung1"), ts, spec, draws, template=r.cfg.tool.get("rung1_user"), **common)
    return count() - before


def rung_view(r: _Run, key: str) -> _Run:
    """``r`` seen through ladder rung ``key``: its work store is a ``RungWork`` and its specs are the primary spec with each
    draw as replicate (``cfg.ladder["draws"]``), so ``build_pair_inputs``, ``_judge_maps`` and ``_stage_align`` read and
    write the rung's maps and nothing else of the run's. Judge and alignment calls are logged as ``<name>@<rung key>``."""
    v = copy.copy(r)
    v.work = RungWork(r.work, key)
    v.item_suffix = f"@{key}"
    v.specs = [replace(r.specs[0], replicate=d) for d in range(int(r.cfg.ladder["draws"]))]
    return v


def ladder_missing(cfg: RunConfig) -> dict[str, int]:
    """{rung key: configured cells without a done item} for a config with a ladder: the (participant, draw) pairs of
    ``cfg.ladder["draws"]`` whose item in ``work/recon:<rung>`` is done AND was made with the rung's current prompt files
    (the rule ``ladder.run_ladder_arm`` resumes by), only the rungs with a gap. Files of other draws, quarantined or
    not-done items and items made with other prompt files do not count. A cell that got no reply from the model is such
    a gap (it is not stored, so a rerun of the same command makes it again); a parse failure is a stored result and not
    a gap. {} without a ladder."""
    keys = ladder_rungs(cfg)
    if not keys:
        return {}
    people, draws, spec = load_participants(cfg), int(cfg.ladder["draws"]), cfg.recon_specs()[0]
    work = Work(cfg.out_dir)
    names = [f"{p.pid}__{replace(spec, replicate=d).key()}" for d in range(draws) for p in people]
    out = {}
    for k in keys:
        rung = ladder.get_rung(k)
        template = cfg.tool.get("rung1_user") if k == "rung1" else None      # K9: the identity rung 1 was run with
        store, identity = ladder.rung_stage(rung, spec).store, ladder.rung_prompt_sha256(PROMPTS_DIR, rung,
                                                                                         template=template)
        missing = sum(1 for n in names if (it := work.get(store, n)) is None or it.get("prompt_sha256") != identity)
        if missing:
            out[k] = missing
    return out


def _stage_prior_only(r: _Run) -> int:
    n = 0
    script_keys = {s.key() for s in r.prior_specs}
    # the script set first (unchanged order), then reading 3. A reading-3 key that is also a script key is drawn once
    # and that one draft is then in both roles (allowed: the legacy single set works that way).
    for spec in r.prior_specs + [s for s in r.reading3_specs if s.key() not in script_keys]:
        for role in r.roles():
            name = f"{role}__{spec.key()}"
            if r.work.get("prior_only", name):
                continue
            tap = _Tap(r.log)
            g, status = reconstruct(tap, r.client("recon"), None, spec, r.ctx(role), PROMPTS_DIR, item_id=name,
                                    stage="prior_only")
            r.work.put("prior_only", name, {"status": _outage(status, tap), "map": asdict(g) if g else None,
                                            "accepted_attempt": attempt_of(g)})
            n += 1
    return n


# ------------------------------------------------------------------ J stages

def _stage_script_prior_only(r: _Run) -> int:
    """The script sources of the config: the judge-family model draws the generic pathway from the prior-only prompts
    (no transcript, no participant text), once per role, variant and sample. Items are
    ``work/script_prior_only/<source>__<role>__<spec key>.json``, logged as ``judge:script_prior_only``."""
    n = 0
    for src in r.cfg.script_sources:
        for spec in r.cfg.source_specs(src):
            for role in r.roles():
                name = f"{src['name']}__{role}__{spec.key()}"
                if r.work.get("script_prior_only", name):
                    continue
                tap = _Tap(r.log)
                g, status = reconstruct(tap, r.client(src["role"]), None, spec, r.ctx(role), PROMPTS_DIR,
                                        item_id=name, stage=f"{src['role']}:{SCRIPT_STAGE_NAME}")
                r.work.put("script_prior_only", name,
                           {"status": _outage(status, tap), "map": asdict(g) if g else None})
                n += 1
    return n


def source_scripts(r: _Run, source: dict) -> dict[str, MapGraph]:
    """{role: script} of one script source, built as ``metrics.scripts_by_role`` builds the own script
    (``build_script``, support 2) from the source's done maps, in sorted item-name order. Only the draws the current
    config lists (``cfg.source_specs``) count: items of an earlier session's other samples, variants or temperature
    still on disk are left out. A role without a map has no script."""
    by_role: dict[str, list[MapGraph]] = {}
    names = sorted(f"{source['name']}__{role}__{spec.key()}" for role in r.roles()
                   for spec in r.cfg.source_specs(source))      # the draws the CURRENT config lists, nothing else
    for role in r.roles():
        for name in names:
            if not name.startswith(f"{source['name']}__{role}__"):
                continue
            d = r.work.get("script_prior_only", name)
            if d and d["status"] == "ok":
                by_role.setdefault(role, []).append(_graph(d["map"]))
    return {role: metrics.build_script(maps) for role, maps in by_role.items()}


def _judge_status(tap: _Tap) -> str:
    """A judge item is "ok" (its unanswered batches are the model's own failure: those labels are not judged and
    final) unless a batch never reached the judge, then "timeout_excluded": the rerun judges it again, and the
    labels already in the cache are not asked twice."""
    return "timeout_excluded" if tap.transport_failed_calls() else "ok"


def _judge_maps(r: _Run, stage: str, role: str, cache_name: str, judge_stage: str) -> int:
    n = 0
    cache = None
    for spec in r.specs:
        for p in r.people:
            name = f"{p.pid}__{spec.key()}"
            g, t = r.ai_map(p.pid, spec.key()), r.transcript(p)
            if g is None or t is None or r.judged(stage, name, g):
                continue
            cache = cache or JudgeCache(r.work.root / cache_name, log=r.log)
            items = modality_items(g, provenance=provenance_for(g, t), transcript=t)
            st: dict = {}
            tap = _Tap(r.log)
            labels, not_judged = judge_modality_detailed(tap, r.client(role), items, PROMPTS_DIR,
                                                         item_id=name + r.item_suffix, stage=judge_stage, cache=cache,
                                                         stats=st)
            for x in g.nodes:                 # a node without a quote is unsupported by rule
                labels.setdefault(x.node_id, "unsupported")
            r.work.put(stage, name, {"status": _judge_status(tap), "labels": labels, "not_judged": sorted(not_judged),
                                     "n_items": len(items), "stats": st, "map_sha256": map_digest(g)})
            n += 1
    return n


def _stage_judge_modality(r: _Run) -> int:
    n = _judge_maps(r, "judge_modality", "judge", "judge_cache.json", "judge:modality")
    for key in ladder_rungs(r.cfg):       # the maps of every ladder rung, judged by the same code, blind to the rung
        n += _judge_maps(rung_view(r, key), "judge_modality", "judge", "judge_cache.json", "judge:modality")
    return n


def _stage_judge2_modality(r: _Run) -> int:
    if r.clients.get("judge2") is None:
        return 0
    return _judge_maps(r, "judge2_modality", "judge2", "judge2_cache.json", "judge2:modality")


def shuffled_copy(g: MapGraph, seed: int) -> MapGraph | None:
    """``g`` with its node quotes moved by a seeded derangement among the quoted nodes (None if fewer than two
    nodes carry a quote). Edges and everything else stay as they are."""
    quoted = [k for k, x in enumerate(g.nodes) if x.quote and x.quote.strip()]
    if len(quoted) < 2:
        return None
    perm = derangements(len(quoted), 1, seed=seed)[0]
    nodes = list(g.nodes)
    for i, k in enumerate(quoted):
        nodes[k] = replace(nodes[k], quote=g.nodes[quoted[perm[i]]].quote)
    return replace(g, nodes=nodes)


def _stage_judge_shuffled(r: _Run) -> int:
    """Quote-shuffled re-judge of the primary spec: every quoted node is judged with the quote of another node of
    the same map (seeded derangement), with the items built by ``modality_items`` on the shuffled copy, so the
    speaker tag and the participant's reply follow the quote exactly as in the real judge. A control that should
    destroy attestation."""
    n, spec = 0, r.specs[0].key()
    cache = None
    for p in r.people:
        name = f"{p.pid}__{spec}"
        g, t = r.ai_map(p.pid, spec), r.transcript(p)
        if g is None or t is None or r.judged("judge_shuffled", name, g):
            continue
        sh = shuffled_copy(g, derive_seed(r.cfg.run_id, p.pid, "shuffle"))
        if sh is None:
            r.work.put("judge_shuffled", name, {"status": "ok", "labels": None, "not_judged": [], "n_items": 0,
                                                "map_sha256": map_digest(g)})
            n += 1
            continue
        node_ids = {x.node_id for x in sh.nodes}
        items = [it for it in modality_items(sh, provenance=provenance_for(sh, t), transcript=t)
                 if it["element_id"] in node_ids]
        cache = cache or JudgeCache(r.work.root / "judge_cache.json", log=r.log)
        st: dict = {}
        tap = _Tap(r.log)
        labels, not_judged = judge_modality_detailed(tap, r.client("judge"), items, PROMPTS_DIR, item_id=name,
                                                     stage="judge:shuffled", cache=cache, stats=st)
        for x in g.nodes:
            labels.setdefault(x.node_id, "unsupported")
        r.work.put("judge_shuffled", name, {"status": _judge_status(tap), "labels": labels, "not_judged": sorted(not_judged),
                                            "n_items": len(items), "stats": st, "map_sha256": map_digest(g)})
        n += 1
    return n


def _all_labels(r: _Run) -> list[str]:
    """Every label of every source (AI maps of every spec, endorsed maps, the script set's prior-only maps),
    deduplicated by normalized text, in normalized order, then shuffled with a seed derived from the run id. A separate
    reading-3 map (``reading3`` block) is not canonicalized: its canonical cells read as not computed."""
    seen: dict[str, str] = {}
    graphs: list[MapGraph] = []
    for p in r.people:
        graphs.append(r.endorsed(p))
        graphs += [g for s in r.specs if (g := r.ai_map(p.pid, s.key())) is not None]
    for role in r.roles():
        graphs += list(r.prior_maps(role).values())
    for g in graphs:
        for x in g.nodes:
            seen.setdefault(normalize(x.text), " ".join(x.text.split()))
    labels = [seen[k] for k in sorted(seen)]
    random.Random(derive_seed(r.cfg.run_id, "canon")).shuffle(labels)
    return labels


def _stage_canonicalize(r: _Run) -> int:
    labels = _all_labels(r)
    n = 0
    for k, b in enumerate(range(0, len(labels), CANON_BATCH)):
        batch = labels[b:b + CANON_BATCH]
        d = r.work.get("canonicalize", f"batch_{k:04d}")
        if d and d.get("labels") == batch:
            continue
        tap = _Tap(r.log)
        concepts = canonicalize(tap, r.client("judge"), batch, PROMPTS_DIR, item_id=f"canon:{k}",
                                batch_size=CANON_BATCH, stage="judge:canon")
        r.work.put("canonicalize", f"batch_{k:04d}", {**_status(tap), "labels": batch, "concepts": concepts})
        n += 1
    n_batches = -(-len(labels) // CANON_BATCH)
    for name in r.work.names("canonicalize"):      # batches of an earlier, longer label list are stale
        if name.startswith("batch_") and int(name[6:]) >= n_batches:
            r.work.remove("canonicalize", name)
    return n


def canonical_names(work: Work) -> dict[str, str]:
    """{normalized label: canonical concept name} from every canonicalization batch on disk."""
    out: dict[str, str] = {}
    root = work.root / "canonicalize"
    for p in sorted(root.glob("batch_*.json")) if root.exists() else []:
        d = work.get("canonicalize", p.stem)
        if d:
            out.update({normalize(a): c for a, c in zip(d["labels"], d["concepts"])})
    return out


def _canon_list(g: MapGraph, names: dict[str, str]) -> list[str] | None:
    """Canonical labels aligned with ``g``'s nodes; None if any label has none."""
    out = [names.get(normalize(x.text)) for x in g.nodes]
    return None if any(c is None for c in out) else out


def _stub(pid: str, role: str, prior: dict[str, MapGraph], canon_prior: dict | None) -> metrics.PairInputs:
    empty = MapGraph(pid, "endorsed")
    return metrics.PairInputs(pid=pid, role=role, interviewer=None, endorsed=empty, ai=dict(prior), prov={},
                              modality={}, in_transcript={}, judged_ai_vs_endorsed={}, judged_prior_vs_endorsed={},
                              judged_ai_vs_script={}, judged_endorsed_vs_script={}, canon_prior=canon_prior)


def _scripts(r: _Run, names: dict[str, str]) -> tuple[dict, dict]:
    """(raw scripts, canonical scripts) by role, built by ``metrics.scripts_by_role`` from the same inputs, in the
    same order, as the metrics stage builds them, so alignment indexes line up."""
    priors = {role: r.prior_maps(role) for role in r.roles()}
    stubs = []
    for p in r.people:
        pm = priors[p.role]
        cp = {k: c for k, g in pm.items() if (c := _canon_list(g, names)) is not None}
        stubs.append(_stub(p.pid, p.role, pm, cp))
    return metrics.scripts_by_role(stubs), metrics.scripts_by_role(stubs, canonical=True)


def _prior_key(r: _Run, role: str) -> str | None:
    """The key of reading 3 for ``role``: with a ``reading3`` block its own key when that map is ok (else None, never a
    script draft in its place); without the block the first script-set key in sorted order, as before."""
    if r.reading3_key() is not None:
        return r.reading3_key() if r.reading3_map(role) is not None else None
    pm = r.prior_maps(role)
    return sorted(pm)[0] if pm else None


def _stage_align(r: _Run, *, ai_only: bool = False) -> int:
    """The alignments the metrics read. ``ai_only`` (a rung view): only the AI-map items, the shared ones (endorsed map
    against the script, prior-only maps, script sources) belong to the run itself."""
    names = canonical_names(r.work)
    raw_scripts, canon_scripts = _scripts(r, names)
    src_scripts = {src["name"]: source_scripts(r, src) for src in r.cfg.script_sources}
    n = 0

    def item(pid: str, kind: str, left: list[str] | None, right: list[str] | None) -> None:
        nonlocal n
        if left is None or right is None:
            return
        name = f"{pid}__{kind}"
        d = r.work.get("align", name)
        if d and d.get("left") == left and d.get("right") == right:
            return
        tap = _Tap(r.log)
        judged = align_labels(tap, r.client("judge"), left, right, PROMPTS_DIR, item_id=name + r.item_suffix)
        # a fallback alignment is lexical-only: kept for inspection, never used as a valid alignment
        r.work.put("align", name, {**_status(tap), "left": left, "right": right, "pairs": _pairs_out(judged)})
        n += 1

    for p in r.people:
        e = r.endorsed(p)
        e_raw, e_can = [x.text for x in e.nodes], _canon_list(e, names)
        rs, cs = raw_scripts.get(p.role), canon_scripts.get(p.role)
        rs_l = [x.text for x in rs.nodes] if rs else None
        cs_l = [x.text for x in cs.nodes] if cs else None
        for spec in r.specs:
            g = r.ai_map(p.pid, spec.key())
            if g is None:
                continue
            a_raw = [x.text for x in g.nodes]
            item(p.pid, f"ai_endorsed__{spec.key()}", a_raw, e_raw)
            item(p.pid, f"ai_script__{spec.key()}", a_raw, rs_l)
            item(p.pid, f"ai_script_canon__{spec.key()}", _canon_list(g, names), cs_l)
        if ai_only:
            continue
        item(p.pid, "endorsed_script", e_raw, rs_l)
        item(p.pid, "endorsed_script_canon", e_can, cs_l)
        pk = _prior_key(r, p.role)
        if pk:
            pg = r.reading3_map(p.role) if r.reading3_key() else r.prior_maps(p.role)[pk]
            item(p.pid, "prior_endorsed", [x.text for x in pg.nodes], e_raw)
        for src in r.cfg.script_sources:     # the endorsed labels against the other-model script of the role
            sc = src_scripts[src["name"]].get(p.role)
            item(p.pid, f"endorsed_src__{src['name']}", e_raw, [x.text for x in sc.nodes] if sc else None)
    if not ai_only:
        for key in ladder_rungs(r.cfg):       # then the rung maps (their canonical-label alignments exist only where
            n += _stage_align(rung_view(r, key), ai_only=True)    # the main run canonicalized the very same labels)
    return n


# ------------------------------------------------------------------ M stages

def _aligned(r: _Run, pid: str, kind: str, left: list[str] | None, right: list[str] | None):
    """The alignment for these exact label lists, or None when it is missing or stale."""
    d = r.work.get("align", f"{pid}__{kind}")
    if d is None or left is None or right is None or d.get("left") != left or d.get("right") != right:
        return None
    return _pairs_in(d["pairs"])


def build_pair_inputs(r: _Run) -> tuple[list[metrics.PairInputs], dict, dict]:
    """(PairInputs per participant, exclusion counts, judging summary by spec)."""
    names = canonical_names(r.work)
    raw_scripts, canon_scripts = _scripts(r, names)
    src_scripts = {src["name"]: source_scripts(r, src) for src in r.cfg.script_sources}
    ex: dict[str, int] = {}
    judging: dict[str, dict] = {s.key(): {"n_items": 0, "n_not_judged": 0} for s in r.specs}
    shuffled = {"n_items": 0, "n_not_judged": 0}

    def count(reason: str) -> None:
        ex[reason] = ex.get(reason, 0) + 1
    out = []
    for p in r.people:
        t, e = r.transcript(p), r.endorsed(p)
        it = r.work.get("in_transcript", p.pid)
        if t is None:
            count("q_inputs_missing")
            continue
        if it is None:
            count("in_transcript_missing")          # the judge group has not (validly) run for this participant
            continue
        if it.get("status") == "too_long_excluded":
            count("in_transcript_too_long")         # the transcript is longer than the judge's context window
            continue
        if not t.turns:
            count("empty_transcript")               # nothing to read (a 0-byte .txt, a .docx with no text)
        elif t.speaker_format == "none":
            count("no_speaker_labels")
        rs, cs = raw_scripts.get(p.role), canon_scripts.get(p.role)
        rs_l = [x.text for x in rs.nodes] if rs else None
        cs_l = [x.text for x in cs.nodes] if cs else None
        e_raw = [x.text for x in e.nodes]
        ai, prov, mod, mod2, mod_sh, nj, nj2, nj_sh = {}, {}, {}, {}, {}, {}, {}, {}
        j_end, j_scr, j_scr_c, canon_ai = {}, {}, {}, {}
        for spec in r.specs:
            k = spec.key()
            g = r.ai_map(p.pid, k)
            if g is None:
                ai[k] = None
                continue
            jm = r.judged("judge_modality", f"{p.pid}__{k}", g)
            ae = _aligned(r, p.pid, f"ai_endorsed__{k}", [x.text for x in g.nodes], e_raw)
            if jm is None or ae is None:      # judge labels or the AI<->endorsed alignment missing, stale or fell back
                count(f"j_inputs_missing:{k}")
                ai[k] = None
                continue
            # no script for the role: {} (the pull leaves the participant out as no_script); otherwise a missing,
            # stale or fallback alignment is left out (None), so metrics counts it as alignment_missing
            asr = _aligned(r, p.pid, f"ai_script__{k}", [x.text for x in g.nodes], rs_l) if rs_l is not None else {}
            ai[k], prov[k], mod[k], nj[k] = g, provenance_for(g, t), jm["labels"], set(jm["not_judged"])
            judging[k]["n_items"] += jm["n_items"]
            judging[k]["n_not_judged"] += len(jm["not_judged"])
            j_end[k] = ae
            if asr is None:
                count(f"ai_script_alignment_missing:{k}")
            else:
                j_scr[k] = asr
            c = _canon_list(g, names)
            if c is not None:
                canon_ai[k] = c
            jc = _aligned(r, p.pid, f"ai_script_canon__{k}", c, cs_l)
            if jc is not None:
                j_scr_c[k] = jc
            j2 = r.judged("judge2_modality", f"{p.pid}__{k}", g)
            if j2:
                mod2[k], nj2[k] = j2["labels"], set(j2["not_judged"])
            sh = r.judged("judge_shuffled", f"{p.pid}__{k}", g)
            if sh and sh.get("labels"):
                mod_sh[k], nj_sh[k] = sh["labels"], set(sh.get("not_judged", []))
                shuffled["n_items"] += sh.get("n_items", 0)
                shuffled["n_not_judged"] += len(sh.get("not_judged", []))
        pm = r.prior_maps(p.role)
        ai.update(pm)
        pk = _prior_key(r, p.role)
        if pk and r.reading3_key():
            ai[pk] = r.reading3_map(p.role)       # reading 3 under its own key, beside the script set
        jpe = _aligned(r, p.pid, "prior_endorsed", [x.text for x in ai[pk].nodes], e_raw) if pk else None
        if pk and jpe is None:
            count("prior_endorsed_alignment_missing")
        jes = _aligned(r, p.pid, "endorsed_script", e_raw, rs_l) if rs_l is not None else {}
        if jes is None:
            count("endorsed_script_alignment_missing")
        j_src = {}
        for src in r.cfg.script_sources:
            sc = src_scripts[src["name"]].get(p.role)
            if sc is None:
                continue                  # no script for the role: metrics counts it as no_script
            j_src[src["name"]] = _aligned(r, p.pid, f"endorsed_src__{src['name']}", e_raw,
                                          [x.text for x in sc.nodes])
            if j_src[src["name"]] is None:
                count(f"endorsed_src_alignment_missing:{src['name']}")
        out.append(metrics.PairInputs(
            pid=p.pid, role=p.role, interviewer=p.interviewer, endorsed=e, ai=ai, prov=prov, modality=mod,
            in_transcript=it["in_transcript"], judged_ai_vs_endorsed=j_end, judged_prior_vs_endorsed=jpe,
            judged_ai_vs_script=j_scr, judged_endorsed_vs_script=jes,
            modality_judge2=mod2 or None, modality_shuffled=mod_sh or None,
            not_judged=nj, not_judged_judge2=nj2 or None, not_judged_shuffled=nj_sh or None,
            canon_endorsed=_canon_list(e, names), canon_ai=canon_ai, prior_spec=pk,
            reading3_separate=r.reading3_separate(),
            canon_prior={k: c for k, g in pm.items() if (c := _canon_list(g, names)) is not None},
            judged_ai_vs_script_canon=j_scr_c,
            judged_endorsed_vs_script_canon=_aligned(r, p.pid, "endorsed_script_canon", _canon_list(e, names), cs_l),
            judged_endorsed_vs_sources=j_src or None))
    def share(d: dict):
        return d["n_not_judged"] / d["n_items"] if d["n_items"] else None
    shuffled["not_judged_share"] = share(shuffled)
    primary = r.specs[0].key()
    for k, j in judging.items():
        j["not_judged_share"] = share(j)
        over = (j["not_judged_share"] or 0) > NOT_JUDGED_LIMIT
        if k == primary:              # the quote-shuffled re-judge of the primary spec counts toward its flag
            j["shuffled"] = shuffled
            over = over or (shuffled["not_judged_share"] or 0) > NOT_JUDGED_LIMIT
        j["interpretation"] = "reported_not_interpreted" if over else "interpreted"
    return out, ex, judging


def _num(x):
    return None if isinstance(x, float) and x != x else x


def _pooled(p_list, spec: str, setting: MatchSetting, min_words: int) -> tuple[dict, dict]:
    """(pooled buckets, pooled absences) of one spec over the participants that have its AI map."""
    arm = metrics.spec_arm(spec)
    bsum: dict[str, float] = {}
    asum: dict[str, float] = {}
    recalls, n, failed = [], 0, 0
    for p in p_list:
        if p.ai.get(spec) is None:
            failed += 1
            continue
        n += 1
        b = metrics.buckets(p, spec, arm, setting, min_words=min_words)
        for k in (*metrics.BUCKETS, "ai_attested_matched", "n_not_judged", "n_edges_eligible", "n_edges_recovered"):
            bsum[k] = bsum.get(k, 0) + b[k]
        if b["node_recall"] == b["node_recall"]:
            recalls.append(b["node_recall"])
        a = metrics.absences(p, spec, arm, setting, min_words=min_words)
        for k in metrics.ABSENCE_COUNTS:
            asum[k] = asum.get(k, 0) + a[k]
    den = bsum.get("matched", 0) + bsum.get("missed", 0)
    buckets = {**bsum, "n": n, "n_excluded": failed, "excluded_reasons": {"ai_failed": failed} if failed else {},
               "node_recall": bsum["matched"] / den if den else None,
               "node_recall_mean": sum(recalls) / len(recalls) if recalls else None,
               "edge_recall": (bsum["n_edges_recovered"] / bsum["n_edges_eligible"]
                               if bsum.get("n_edges_eligible") else None)}
    nw, nc = asum.get("n_wished", 0), asum.get("n_control", 0)
    absences = {**asum, "n": n, "n_excluded": failed,
                "leak_rate": asum["leaked_as_existing"] / nw if nw else None,
                "recovery_rate": asum["recovered_as_wished"] / nw if nw else None,
                "flip_rate": asum["flipped"] / nc if nc else None}
    return buckets, absences


# run_start labels of clients that are not a model (cli.build_clients: the oracle; a replay whose recording's _meta names
# no model for the role): never a model id for the script-family label
CLIENT_KIND_LABELS = ("oracle", "replay")


def _logged_models(r: _Run) -> tuple[dict[str, set], set]:
    """({script source name: model ids of its calls}, reconstructor model ids), read from the run's audit log: the
    ids the calls were SENT with, which is all the family label may rest on. A ``call_request`` without a
    ``model_id`` takes the model that the session's latest ``run_start`` names for the call's role (the role mapping
    of ``replay_check._role``): a replay client logs no model_id; its label is the recording's model, as replay_check
    reads it. A logged ``model_id`` always wins. The labels ``cli.build_clients`` gives clients that are not a model
    (``CLIENT_KIND_LABELS``: the oracle, a replay whose recording names no model for the role) are not taken: such a
    call stays without a model on record, as before."""
    from .replay_check import _role     # lazy: replay_check imports this module
    scripts: dict[str, set] = {}
    recon: set = set()
    models: dict = {}
    try:
        with open(r.log.path, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("record_type") == "run_start":
                    p = rec.get("payload") or {}
                    models = p.get("models") if isinstance(p.get("models"), dict) else {}
                    continue
                if rec.get("record_type") != "call_request":
                    continue
                pl = rec["payload"]
                stage = str(pl.get("stage", ""))
                fallback = models.get(_role(stage))
                model_id = pl.get("model_id") or (None if fallback in CLIENT_KIND_LABELS else fallback)
                if stage in SCRIPT_STAGES:
                    scripts.setdefault(str(pl.get("item_id", "")).split("__", 1)[0], set()).add(model_id)
                elif stage.startswith(("recon:", "q:", "prior_only")):
                    recon.add(model_id)
    except (OSError, ValueError, AttributeError):
        pass
    return scripts, recon


def script_source_list(r: _Run) -> tuple[list | None, dict]:
    """(ScriptSource list for ``metrics.headline``, description by source name). Without configured sources: (None,
    {}), and the headline uses the reconstructor's own script alone, as before. The family of each source is DERIVED
    from the logged model ids (``families.script_family``); the reconstructor's own script comes last, as the
    sensitivity row."""
    if not r.cfg.script_sources:
        return None, {}
    by_source, recon_ids = _logged_models(r)
    sources, desc = [], {}
    for src in r.cfg.script_sources:
        ids = by_source.get(src["name"], set())
        family, why = script_family(ids, recon_ids)
        sources.append(metrics.ScriptSource(src["name"], family, source_scripts(r, src), why))
        shown = sorted({m for m in map(_basename, ids) if m})
        desc[src["name"]] = {"family": family, "reason": why, "role": src["role"],
                             "model": shown[0] if len(shown) == 1 else None,
                             "script_family": model_family(shown[0]) if len(shown) == 1 else None,
                             "reconstructor_family": next(iter(sorted(
                                 {f for f in map(model_family, recon_ids) if f})), None)}
    return sources, desc


_AUDIT_KEEP = ("n", "n_outputs", "n_missing", "n_flagged", "flagged_ids", "n_in_source")


def _copy_audit_summary(summary: dict) -> dict:
    """A copy-audit summary reduced to counts and (anonymous) output ids: the flags' item ids come from the model's own
    JSON and the demo n-grams are sentences, neither belongs in an aggregate; their tallies by field and kind do."""
    flags = summary.get("flags") or []
    out = {k: summary[k] for k in _AUDIT_KEEP}
    for key in ("field", "kind"):
        tally: dict[str, int] = {}
        for f in flags:
            tally[f[key]] = tally.get(f[key], 0) + 1
        out["by_" + key] = dict(sorted(tally.items()))
    return out


def primary_script_source(sources, own):
    """The script the ladder's script-conditional gaps rest on: the one ``metrics.script_conditional`` calls primary
    (the first source whose family is "other"), else the reconstructor's own script. NOT the first configured source:
    that one may be an alias whose family is not established."""
    return next((s for s in sources or [] if s.family == "other"), own)


def _ladder_section(r: _Run, setting: MatchSetting, min_words: int, source) -> dict:
    """``aggregates["ladder"]``: per rung, the maps judged and aligned like the main run's (``rung_view``) turned into
    ``ladder_metrics.MapDraw`` cells (a missing or failed map is a parse failure, never clean), the ladder report
    (contrasts, Holm, verdict as codes) with the declaration check, the copy audit, the null-phase share and how often a
    draw repeats draw 0. Participant ids are anonymised; the arms are built through ``ladder.metrics_arms`` (an unknown
    key raises instead of skipping a contrast). ``source``: the primary ``metrics.ScriptSource`` (or None)."""
    keys = ladder_rungs(r.cfg)
    draws = int(r.cfg.ladder["draws"])
    prm = ladder_params(r.cfg)
    anon = ladder.anonymous_ids(p.pid for p in r.people)
    calls = ladder.retries_by_call(r.log.path)
    errors = ladder.call_errors_by_call(r.log.path, transport_only=True)
    outs: dict[str, dict] = {}
    results: dict[str, dict] = {}
    per_rung: dict[str, dict] = {}
    views: dict[str, tuple] = {}           # rung key -> (rung_view, PairInputs list)
    unfinished: set[str] = set()           # participants with a cell not yet measurable in some rung
    for key in keys:
        rung = ladder.get_rung(key)
        v = rung_view(r, key)
        pl, ex, judging = build_pair_inputs(v)
        views[key] = (v, pl)
        by_pid = {p.pid: p for p in pl}
        res: dict[tuple[str, int], ladder.ArmResult] = {}
        outs[key] = {}
        status_counts: dict[str, int] = {}
        n_retries = n_errors = n_j_missing = 0
        for d, s in enumerate(v.specs):
            for p in r.people:
                name = f"{p.pid}__{s.key()}"
                item = v.work.get("recon", name)
                status = item["status"] if item else "not_run"
                g = v.ai_map(p.pid, s.key())
                res[(p.pid, d)] = ladder.ArmResult(g, status, name)
                t = r.transcript(p)
                outs[key][f"{anon[p.pid]}#{d}"] = None if g is None else (g, t.text(s.arm)) if t is not None else g
                status_counts[status] = status_counts.get(status, 0) + 1
                if item:
                    n_retries += ladder.draw_retries(calls, name, rung)
                    n_errors += errors.get((f"recon:{s.arm}:{key}", ladder.rung_item_id(name, rung)), 0)
                pi = by_pid.get(p.pid)
                if pi is None:
                    continue                  # left out of every rung alike (a shared input is missing)
                if item is None:
                    unfinished.add(p.pid)     # the rung's call has not (validly) been made
                elif status == "ok" and pi.ai.get(s.key()) is None:
                    # the map is there but its judge labels or alignment are missing, stale, timed out or fell back:
                    # a gap in the measurement, not a parse failure (only a reconstruction that failed is one)
                    n_j_missing += 1
                    unfinished.add(p.pid)
        results[key] = res
        n_items = sum(j["n_items"] for j in judging.values())
        per_rung[key] = {"expected": len(r.people) * draws, "by_status": dict(sorted(status_counts.items())),
                         "retries": n_retries, "transport_errors": n_errors, "j_inputs_missing": n_j_missing,
                         "not_judged_share": sum(j["n_not_judged"] for j in judging.values()) / n_items
                         if n_items else None,
                         "exclusions": _nest_reasons({k: n for k, n in ex.items()
                                                      if k.startswith(("j_inputs_missing", "ai_script_alignment"))})}
    # a participant with an unmeasurable cell in ANY rung leaves EVERY arm, so the arms still cover the same maps
    arms: dict[str, list] = {}
    for key in keys:
        v, pl = views[key]
        rung = ladder.get_rung(key)
        arms[key] = [replace(ladder_metrics.draw_from_pair_inputs(
            p, s.key(), d, setting, min_words=min_words, source=source,
            retries=ladder.draw_retries(calls, f"{p.pid}__{s.key()}", rung)), map_id=anon[p.pid])
            for d, s in enumerate(v.specs) for p in pl if p.pid not in unfinished]
    complete = not unfinished and all(not st["by_status"].get("not_run") for st in per_rung.values())
    declared = "rung3" if "rung3" in keys else keys[0]
    report_ = (ladder_metrics.ladder_report(ladder.metrics_arms(arms), n_perm=prm["n_perm"], B=prm["B"],
                                            seed=prm["seed"], declaration_log=r.log.path,
                                            declaration_rung=_ladder_label(r.cfg, declared))
               if any(arms.values()) else None)
    over = [k for k, st in per_rung.items() if (st["not_judged_share"] or 0) > NOT_JUDGED_LIMIT]
    interpretation = "incomplete_run" if not complete else "reported_not_interpreted" if over else "interpreted"
    if report_ and not complete:
        # no verdict while a cell is missing: the maps that are left are not the maps asked for
        report_["verdict"] = {"verdict": "incomplete", "reason_codes": ["incomplete"]}
    elif report_ and over:
        # the main headline's rule (NOT_JUDGED_LIMIT) for the rungs: a rung the judge left mostly unjudged differs
        # from the others in how it was measured as well as in its instruction, so the contrast is reported, not read
        report_["verdict"] = {"verdict": "reported_not_interpreted", "reason_codes": ["not_judged_over_limit"]}
    if report_ and report_.get("declaration_check"):
        # the declaration record's id hashes its timestamp: it differs between a run and its replay, so it stays in
        # the log (where the record is) and out of the aggregates, which must reproduce byte for byte
        report_["declaration_check"].pop("declaration_id", None)
    null_phase = ladder.null_phase_by_rung(outs)
    for summary in null_phase.values():
        summary.pop("by_map", None)
    return {"rungs": keys, "draws": draws, "arm_of_rung": {k: ladder.ARM_OF_RUNG[k] for k in keys}, **prm,
            "call_order": LADDER_CALL_ORDER,
            "prompt_set_sha256": prompt_set_sha256(),
            "complete": complete, "interpretation": interpretation,
            "script_source": {"name": getattr(source, "name", None), "family": getattr(source, "family", None)},
            "per_rung": per_rung, "report": report_,
            "copy_audit": {k: _copy_audit_summary(sm) for k, sm in ladder.audit_rungs(outs).items()},
            "null_phase": null_phase, "draw_identity": ladder.draw_identity_by_rung(results)}


def headline_kwargs(cfg: RunConfig) -> dict:
    """The keyword arguments M passes to ``metrics.headline``: the settings grid, its primary setting and the
    ``stats`` block of the config (one place, so a re-computation outside M uses exactly M's numbers)."""
    settings = cfg.match_settings()
    st = cfg.stats or {}
    return {"settings": settings, "primary_setting": settings[0], "min_words": int(st.get("min_words", 8)),
            "seed": int(st.get("seed", 0)), "B": int(st.get("B", 2000)),
            "n_derangements": int(st.get("n_derangements", 20)), "n_perm": int(st.get("n_perm", 1000)),
            "n_perm_script": int(st.get("n_perm_script", 10000)), "alpha_script": float(st.get("alpha_script", 0.05))}


def arm_headlines(p_list: list[metrics.PairInputs], keys: list[str], hk: dict, sources, judging: dict,
                  incomplete: bool, *, modality_source: str = "judge", other_arms: bool = True) -> tuple[dict, dict]:
    """(headline of the primary spec's arm, {other arm: its headline}) exactly as M builds them: the claim-stability
    grid varies the variant / temperature WITHIN the primary's arm; each other arm is a headline of its own (its
    first spec is its primary), never mixed into this grid. ``hk`` is ``headline_kwargs``; ``modality_source`` as in
    ``metrics.headline`` ("keyword" = the no-AI arm). Reason codes compacted, interpretation set."""
    primary = keys[0]
    arm = metrics.spec_arm(primary)

    def one(first: str, variations: list[str]) -> dict:
        return metrics.headline(p_list, first, variations, **hk, script_sources=sources,
                                modality_source=modality_source)
    head = one(primary, [k for k in keys[1:] if metrics.spec_arm(k) == arm])
    apply_stability_grid(head, arm, keys)
    others = {}
    for other in (dict.fromkeys(metrics.spec_arm(k) for k in keys if metrics.spec_arm(k) != arm)
                  if other_arms else ()):
        of = [k for k in keys if metrics.spec_arm(k) == other]
        hl = one(of[0], of[1:])
        apply_stability_grid(hl, other, keys)
        others[other] = compact_reasons(hl)
    head["interpretation"] = headline_interpretation(head, judging, incomplete)       # incomplete: items unreachable
    for blk in others.values():
        blk["interpretation"] = headline_interpretation(blk, judging, incomplete)
    return compact_reasons(head), others


def _not_analysed_reason(r: _Run, p: Participant, primary: str, judged: bool = True) -> str:
    """Why participant ``p`` has no usable map of the primary spec: the first missing input, in pipeline order.
    ``judged`` False stops after stage Q (the end-of-Q console line)."""
    t = r.transcript(p)
    if t is None:
        return "q_inputs_missing"
    if not t.turns:
        return "empty_transcript"
    if t.speaker_format == "none":
        return "no_speaker_labels"
    d = r.work.read_any("recon", f"{p.pid}__{primary}")
    if d is None or d.get("status") != "ok":
        return f"recon_{d.get('status') or 'missing'}" if d is not None else "recon_missing"
    if not judged:
        return "ok"
    it = r.work.get("in_transcript", p.pid)
    if it is None:
        return "in_transcript_missing"
    if it.get("status") == "too_long_excluded":
        return "in_transcript_too_long"
    return "j_inputs_missing"


def not_analysed_reasons(r: _Run, primary: str, pids: set, judged: bool = True) -> dict[str, int]:
    """{reason: n} over the participants ``pids`` (one reason each, ``_not_analysed_reason``), sorted by reason."""
    out: dict[str, int] = {}
    for p in r.people:
        if p.pid in pids:
            why = _not_analysed_reason(r, p, primary, judged)
            if why != "ok":
                out[why] = out.get(why, 0) + 1
    return dict(sorted(out.items()))


def compute_aggregates(r: _Run) -> tuple[dict, list[metrics.PairInputs]]:
    cfg = r.cfg
    p_list, ex, judging = build_pair_inputs(r)
    if getattr(r, "pairing", None) and r.pairing.not_in_roles:      # paired files the config does not list: not participants, but on the record
        ex = {**ex, "not_in_roles": len(r.pairing.not_in_roles)}
    hk = headline_kwargs(cfg)
    settings = hk["settings"]
    ps = hk["primary_setting"]
    min_words = hk["min_words"]
    keys = [s.key() for s in r.specs]
    by_b, by_a = {}, {}
    for k in keys:
        b, a = _pooled(p_list, k, ps, min_words) if p_list else ({}, {})
        b["interpretation"] = a["interpretation"] = judging[k]["interpretation"]
        by_b[k], by_a[k] = b, a
    primary = keys[0]
    source = None
    by_pid = {pi.pid: pi for pi in p_list}
    analysed = [p for p in r.people if p.pid in by_pid and by_pid[p.pid].ai.get(primary) is not None]
    agg = {
        "run_id": cfg.run_id, "corpus_kind": cfg.corpus.get("kind"), "config_sha256": cfg.sha256(),
        "prompt_set_sha256": prompt_set_sha256(), "prompt_manifest_ok": prompt_manifest_problem() is None,
        "models": r.recorded_models(),
        "primary_spec": primary, "specs": keys, "prior_specs": [s.key() for s in r.prior_specs],
        # n_analysed is the profile's: the people with the primary spec's AI map (profile_base.open_context)
        "primary_setting": ps.key(), "n_participants": len(r.people), "n_analysed": len(analysed),
        "exclusions": _nest_reasons(ex),
        "items": {s: r.work.statuses(s) for s in STAGE_ORDER if s not in STAGE_GROUPS["M"]},
        "fallbacks": _fallback_counts(r),
        "outage": _outage_counts(r),
        "mapping_talk": _mapping_talk_counts(r),
        "quote_type": quote_type_section(r),
        "judging": {"by_spec": judging, "primary": judging[primary]},
        "buckets": {"by_spec": by_b, "primary": by_b[primary]},
        "absences": {"by_spec": by_a, "primary": by_a[primary]},
    }
    # one reason per person not analysed (they sum to n_participants - n_analysed), and the items left out for being
    # too long, under keys of their own; both only when there is something to count
    not_analysed = not_analysed_reasons(r, primary, {pid for pid, pi in by_pid.items() if pi.ai.get(primary) is None}
                                        | {p.pid for p in r.people if p.pid not in by_pid})
    if not_analysed:
        agg["not_analysed"] = not_analysed
    too_long = {st: n["too_long_excluded"] for st, n in agg["items"].items() if n.get("too_long_excluded")}
    if too_long:
        agg["item_exclusions"] = {"too_long_excluded": too_long}
    if (dups := duplicate_transcripts(r.people)):        # only when it happens; the report says so, nothing is excluded
        agg["duplicate_transcripts"] = {"groups": len(dups), "copies": sum(len(g) - 1 for g in dups)}
    if p_list:
        sources, desc = script_source_list(r)
        own = metrics.ScriptSource("own", "own", metrics.scripts_by_role(p_list))
        source = primary_script_source(sources, own)
        if sources is not None:
            sources.append(own)
            agg["script_sources"] = desc
        incomplete = agg["outage"]["timeout_excluded_items"] > 0       # the model could not be reached for some items
        agg["headline"], other_arms = arm_headlines(p_list, keys, hk, sources, judging, incomplete)
        if other_arms:
            agg["other_arms"] = other_arms
    if ladder_rungs(cfg):
        agg["ladder"] = _ladder_section(r, ps, min_words, source)
    if toolcfg.is_tool(cfg):       # K6, tool runs only: the head changes with every run (records carry times), and
        agg["chain"] = chain_state(r)          # a study config's aggregates stay byte for byte reproducible
    from . import reference
    if ref_type := reference.reference_type(cfg):     # a tool run: blank what its reference mode cannot support
        return reference.mask_aggregates(metrics._clean(agg), ref_type), p_list
    return metrics._clean(agg), p_list


def chain_state(r) -> dict:
    """{"head", "records", "verified"} of the run log now (``verify_internal_chain``: every hash recomputed and
    every link checked). The manifest is written when the log closes, after the aggregates, so it is not read here;
    ``verify`` checks the closed log against it. No log file: head None, 0 records, not verified."""
    path = getattr(r.log, "path", None)
    if not path or not os.path.isfile(path):
        return {"head": None, "records": 0, "verified": False}
    ok, detail = verify_internal_chain(path)
    return {"head": detail.get("head_hash"), "records": detail.get("records", 0), "verified": bool(ok)}


# the claim-stability grid of one arm (rule 2): prompt variants p0, p1, p2 at T0 and p0 at T0.7, replicate 0
GRID_SPECS = (("p0", 0.0), ("p1", 0.0), ("p2", 0.0), ("p0", 0.7))


def apply_stability_grid(headline: dict, arm: str, keys: list[str]) -> None:
    """Write ``stability_grid`` = {expected, present, missing} (the arm's grid specs against the specs the config
    lists). A missing spec is not skipped silently: the sign-stability claims fail closed (rule 7), the pull claim is
    set false and marked ``claim_blocked: grid_incomplete``."""
    expected = [ReconSpec(arm, v, t, 0).key() for v, t in GRID_SPECS]
    present = [k for k in expected if k in keys]
    missing = [k for k in expected if k not in keys]
    headline["stability_grid"] = {"expected": expected, "present": present, "missing": missing}
    if missing:
        pull = headline.get("pull_toward_script") or {}
        for blk in (pull, pull.get("in_transcript_only") or {}):
            if "claim_pulled_toward_script" in blk:
                blk["claim_pulled_toward_script"] = False
        pull["claim_blocked"] = "grid_incomplete"


def headline_interpretation(headline: dict, judging: dict, incomplete: bool = False) -> str:
    """"incomplete_run" while any item is still ``timeout_excluded`` (the numbers rest on the items that happened to
    be reachable: rerun first). Else "reported_not_interpreted" when ANY of what the headline reads is more than ``NOT_JUDGED_LIMIT`` unjudged:
    metrics' own not_judged block (the grid and the full / removed twins its leakage block contrasts), and the
    judging summary of the primary (the quote-shuffled re-judge included) and of every spec in that block."""
    if incomplete:
        return "incomplete_run"
    block = headline.get("not_judged") or {}
    read = {headline.get("primary_spec"), *(headline.get("variation_specs") or []), *(block.get("by_spec") or {})}
    over = block.get("interpretation") == "reported_not_interpreted" or any(
        (judging.get(k) or {}).get("interpretation") == "reported_not_interpreted" for k in read)
    return "reported_not_interpreted" if over else "interpreted"


def _nest_reasons(counts: dict[str, int]) -> dict:
    """{"reason:spec": n, "reason": n} -> {"reason": {"spec": n}, "reason": n}: every key stays short (the
    aggregate-only guard allows no string over 40 characters)."""
    out: dict = {}
    for k in sorted(counts):
        code, _, spec = k.partition(":")
        if spec:
            out.setdefault(code, {})[spec] = counts[k]
        else:
            out[code] = counts[k]
    return out


def compact_reasons(obj):
    """The headline with its reason codes in a short form: ``fallback_counts`` and ``excluded_reasons`` keys
    "code:spec" nested as {code: {spec: n}}, ``fallback_reasons`` / ``canonical_fallback_reasons`` lists reduced
    to their codes, and ``missing_prior_samples`` (participant ids) reduced to a count."""
    if isinstance(obj, list):
        return [compact_reasons(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k == "missing_prior_samples" and isinstance(v, list):
            out[k] = len(v)
        elif k in ("fallback_reasons", "canonical_fallback_reasons") and isinstance(v, list):
            out[k] = sorted({str(x).partition(":")[0] for x in v})
        elif k in ("fallback_counts", "excluded_reasons") and isinstance(v, dict):
            out[k] = _nest_reasons(v)
        else:
            out[k] = compact_reasons(v)
    return out


def _fallback_counts(r: _Run) -> dict:
    """Items whose helper calls fell back (lexical rule or raw labels), by stage and, where the item belongs to a
    spec, by spec. Those items are not used as results and are redone on the next run."""
    keys = [s.key() for s in r.specs]
    out = {}
    for stage in ("mapping_talk_confirm", "in_transcript", "canonicalize", "align"):
        names = [nm for nm in r.work.names(stage) if (r.work.read_any(stage, nm) or {}).get("status") == "fallback"]
        by_spec = {k: sum(1 for nm in names if nm.endswith("__" + k)) for k in keys}
        out[stage] = {"items": len(names), "by_spec": {k: v for k, v in by_spec.items() if v}}
    return out


TYPE_SHARE_NOTE = "type_share_includes_coerced_other"      # a short code: the sentence is report.TYPE_SHARE_SENTENCE


def quote_type_counts(maps) -> dict:
    """Counts over saved AI maps (dicts as stored in a work item, None skipped): maps, nodes, nodes whose type was
    coerced to "other" (``meta["type_coercions"]``), maps with at least one, and the quote tiers recorded at parse time
    (``meta["quote_tier_counts"]``). A map saved without that meta counts as zero coercions and no tiers."""
    out = {"maps": 0, "nodes": 0, "type_coercions": 0, "maps_with_coercion": 0,
           "quote_tiers": {t: 0 for t in QUOTE_TIERS}}
    for m in maps:
        if not m:
            continue
        meta = m.get("meta") or {}
        out["maps"] += 1
        out["nodes"] += len(m.get("nodes") or [])
        out["type_coercions"] += int(meta.get("type_coercions") or 0)
        out["maps_with_coercion"] += bool(meta.get("type_coercions"))
        for t in QUOTE_TIERS:
            out["quote_tiers"][t] += int((meta.get("quote_tier_counts") or {}).get(t) or 0)
    return out


ATTEMPT_BUCKETS = ("1", "2", "3", "failed", "unknown")


def attempt_counts(items) -> dict:
    """Distribution of ``accepted_attempt`` over work items (dicts; None skipped): "1", "2", "3" for an ok map accepted at
    that attempt, "failed" for an item whose every attempt was rejected, "unknown" for an ok item saved before the field
    existed (or with a value outside 1-3). Items of any other status are not counted."""
    out = {b: 0 for b in ATTEMPT_BUCKETS}
    for it in items:
        if not it:
            continue
        if it.get("status") == "failed":
            out["failed"] += 1
        elif it.get("status") == "ok":
            a = it.get("accepted_attempt")
            out[str(a) if str(a) in ("1", "2", "3") else "unknown"] += 1
    return out


def quote_type_section(r: _Run) -> dict:
    """``aggregates["quote_type"]``: the counts of ``quote_type_counts`` per spec key, per arm and per ladder rung, over
    the ok maps in ``work/recon`` (and ``work/prior_only`` for the type counts) and each rung's own recon store, with the
    ``accepted_attempt`` distribution (``attempt_counts``) of the same items plus their failed ones. Counts only. ``type_share_affected`` and ``note`` say that the ``type_share:*`` features include coerced nodes."""
    def items(work, stage):
        return [(n.split("__", 1)[-1], it) for n in work.names(stage)
                if (it := work.read_any(stage, n)) and it.get("status") in ("ok", "failed")]

    def cell(its):
        """Counts of the ok maps plus the accepted_attempt distribution of the ok and failed items."""
        return {**quote_type_counts(i.get("map") for i in its if i.get("status") == "ok"),
                "accepted_attempt": attempt_counts(its)}

    by_spec: dict[str, list] = {}
    for stage in ("recon", "prior_only"):
        for spec_key, it in items(r.work, stage):
            by_spec.setdefault(spec_key, []).append(it)
    by_arm: dict[str, list] = {}
    for spec_key, its in by_spec.items():
        by_arm.setdefault(spec_key.split("__", 1)[0], []).extend(its)
    by_rung = {key: cell([it for _k, it in items(RungWork(r.work, key), "recon")]) for key in ladder_rungs(r.cfg)}
    return {"by_spec": {k: cell(v) for k, v in sorted(by_spec.items())},
            "by_arm": {k: cell(v) for k, v in sorted(by_arm.items())},
            "by_rung": by_rung, "type_share_affected": True, "note": TYPE_SHARE_NOTE}


def _outage_counts(r: _Run) -> dict:
    """Items left ``timeout_excluded`` because the model could not be reached, by stage. Not zero means the run is
    incomplete: rerun the same command once the model is back (those items are redone, the rest is kept). Every work
    directory is walked, the ladder's own (``recon:<rung>``, ``judge_modality:<rung>``, ``align:<rung>``) included:
    a judge that went down while a rung's maps were judged leaves those items timeout_excluded there."""
    stages = [s for s in STAGE_ORDER if s not in STAGE_GROUPS["M"]]
    try:
        extra = sorted(k for d in r.work.root.iterdir() if d.is_dir()
                       and (k := stage_from_dirname(d.name)) not in stages)
    except OSError:
        extra = []
    by_stage = {s: n for s in (*stages, *extra) if (n := r.work.statuses(s).get("timeout_excluded", 0))}
    return {"timeout_excluded_items": sum(by_stage.values()), "by_stage": by_stage}


def _mapping_talk_counts(r: _Run) -> dict:
    """Rule-only vs model counts for the mapping-talk screen, summed over participants. The model screens every
    interviewer turn; ``rule_only`` = flagged by the regex and rejected by the model, ``model_only`` = found by the
    model and missed by the regex, ``agreement`` = share of screened turns on which the two agree."""
    keys = ("rule_flagged", "model_flagged", "model_confirmed", "rule_only", "model_only", "disagreements",
            "n_interviewer_turns", "n_screened", "unscreened_turns")
    out = {**{k: 0 for k in keys}, "files_without_speakers": 0}
    for p in r.people:
        d = r.work.get("mapping_talk_confirm", p.pid)
        if not d:
            continue
        if d.get("speaker_format") == "none":
            out["files_without_speakers"] += 1
        for k in keys:
            out[k] += (d.get("stats") or {}).get(k, 0)
    out["agreement"] = (1 - out["disagreements"] / out["n_screened"]) if out["n_screened"] else None
    return out


def is_synthetic(cfg: RunConfig) -> bool:
    return cfg.corpus.get("kind") == "synthetic"


def _basename(model_id) -> str | None:
    """A model id without any path separator ("/Users/x/models/foo.gguf" -> "foo.gguf", "org/name" -> "name"),
    so a local path never reaches the log or the aggregates."""
    if model_id is None:
        return None
    return str(model_id).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def model_ids(clients: dict) -> dict:
    """Model ids per role (never endpoints): ``client.cfg.model_id``, else ``client.model_label``; a path is cut
    to its last part."""
    out = {}
    for role in ("recon", "judge", "judge2"):
        c = clients.get(role)
        if c is not None:
            out[role] = _basename(getattr(getattr(c, "cfg", None), "model_id", None) or getattr(c, "model_label", None))
    return out


def _stage_metrics(r: _Run) -> int:
    agg, p_list = compute_aggregates(r)
    if is_synthetic(r.cfg) and r.cfg.corpus.get("synthetic_dir"):
        agg["traps"] = report.evaluate_traps(p_list, r.cfg.path(r.cfg.corpus["synthetic_dir"]),
                                             [s.key() for s in r.specs], r.cfg.match_settings()[0],
                                             int((r.cfg.stats or {}).get("min_words", 8)))
    # the guard runs for every corpus that is not synthetic (fail closed)
    report.write_aggregates(r.cfg, agg, forbidden=None if is_synthetic(r.cfg) else _forbidden(r, p_list),
                            guard=r.leak_guard())
    r.last_aggregates = agg
    return 0


def _forbidden(r: _Run, p_list) -> set[str]:
    """Every label and quote of every map, collected in memory for the aggregate-only guard (never written)."""
    out: set[str] = set()
    for p in p_list:
        for g in [p.endorsed, *[g for g in p.ai.values() if g is not None]]:
            for x in g.nodes:
                out.add(x.text)
                if x.quote:
                    out.add(x.quote)
            out.update(e.quote for e in g.edges if e.quote)
    for key in ladder_rungs(r.cfg):          # the instruction ladder's maps are as private as the main run's
        v = rung_view(r, key)
        for p in r.people:
            for s in v.specs:
                if (g := v.ai_map(p.pid, s.key())) is not None:
                    for x in g.nodes:
                        out.add(x.text)
                        if x.quote:
                            out.add(x.quote)
                    out.update(e.quote for e in g.edges if e.quote)
    return out


def _stage_report(r: _Run) -> int:
    report.write_report(r.cfg, guard=r.leak_guard())
    return 0


STAGES = {
    "mapping_talk_confirm": _stage_mapping_talk_confirm, "in_transcript": _stage_in_transcript,
    "recon": _stage_recon, "prior_only": _stage_prior_only,
    "judge_modality": _stage_judge_modality, "judge_shuffled": _stage_judge_shuffled,
    "script_prior_only": _stage_script_prior_only, "canonicalize": _stage_canonicalize, "align": _stage_align, "judge2_modality": _stage_judge2_modality,
    "metrics": _stage_metrics, "report": _stage_report,
}


def expand_stages(stages: str | list[str], only: str | None = None) -> list[str]:
    """"Q", "J", "M", "all", a comma list of those, or stage names -> stage names in pipeline order."""
    if only:
        if only not in STAGES:
            raise ValueError(f"unknown stage {only!r}")
        return [only]
    parts = stages.split(",") if isinstance(stages, str) else list(stages)
    chosen: set[str] = set()
    for s in parts:
        if s == "all":
            chosen.update(STAGE_ORDER)
        elif s in STAGE_GROUPS:
            chosen.update(STAGE_GROUPS[s])
        elif s in STAGES:
            chosen.add(s)
        else:
            raise ValueError(f"unknown stage or group {s!r}")
    return [s for s in STAGE_ORDER if s in chosen]


def refuse_before_writing(cfg: RunConfig, clients: dict, log_path, *, resume=True) -> None:
    """The refusals that come before a run writes anything (``run`` calls it first; ``cli._execute`` calls it before
    it creates the out_dir, so a refused run leaves no new folder): a real corpus's clients, the frozen prompt files,
    an opt-in pre-registration, and a resumed real run's model, settings, prompts and config. ``log_path``: the
    run's log, which may not exist yet. Reads only."""
    if cfg.corpus.get("kind") == "real":
        guard_real_clients(clients, judge_rule=cfg.tool.get("judge_rule", "study"),     # local endpoints only,
                           families=cfg.tool.get("model_families"),   # thinking off, judge rule: before any call
                           remote_endpoints=cfg.tool.get("remote_endpoints"),     # K13: None for a study config
                           # audit K7: a tool run's LAN/Tailscale opt-ins ([] when none); a study call is unchanged
                           **({"endpoint_optins": cfg.tool.get("endpoint_optins") or []} if toolcfg.is_tool(cfg) else {}))
    live = any(hasattr(getattr(c, "cfg", None), "endpoint") for c in clients.values())   # sends something somewhere
    if (cfg.corpus.get("kind") == "real" or live) and (why := prompt_manifest_problem()):
        # the wordings sent to the model must be the frozen ones (a changed wording is a new prompt version); an
        # offline synthetic run (mock / replay / oracle) may go on while prompts are edited and says so in its report
        raise RuntimeError(f"refusing to run: the prompt files differ from prompts/MANIFEST.json ({why}). "
                           "Restore the frozen files, or write the change as a new prompt version and regenerate "
                           "the manifest (python3 -m second_scribe.prompt_manifest) before a run that calls a model")
    prereg.require_frozen(cfg, SimpleNamespace(path=log_path))
    if not resume:
        return
    if cfg.corpus.get("kind") == "real" and (why := prereg.resume_problem(log_path, clients)):
        # one instrument per role for the whole run (a validity check, not a pre-registration lock)
        raise RuntimeError(f"refusing to resume: {why}. A resumed real run must use the model and settings its "
                           "log already records; start a new out_dir (or restore the earlier model/settings) instead")
    if why := prereg.restart_problem(log_path, prompt_set_sha256(), cfg.sha256(), textnorm.quote_rule_sha256()):
        # regenerating the manifest after an edit makes the prompt check above pass, so the sessions of a run are
        # compared with each other: one prompt set and one config for the whole log
        if cfg.corpus.get("kind") == "synthetic":
            raise InputsRefused("inputs changed since this folder was written; use a new --out")
        raise RuntimeError(f"refusing to resume: {why}. Start a new out_dir to run with another prompt set, "
                           "config or quote rule (the earlier sessions stay as they are)")


def run(cfg: RunConfig, stages: list[str], clients: dict, *, log: AuditLog, inputs_checked: bool = False,
        say=None, stage_seconds=None) -> dict:
    """Run ``stages`` (names, in pipeline order) and return {"new_items": {stage: n}, "aggregates": dict | None}.

    ``clients``: {"recon", "judge", "judge2" (optional)} -> objects with ``complete``. No run needs a frozen
    pre-registration; only a config that sets ``prereg_required`` true is held to one (``prereg.require_frozen``).
    Every input file is opened once before any stage (``check_inputs``) unless the caller already did that
    (``inputs_checked``). ``say`` (optional, one string per call) gets the console line at the end of stage Q."""
    refuse_before_writing(cfg, clients, log.path)
    if not inputs_checked:
        check_inputs(cfg)
    r = _Run(cfg, clients, log)
    r.last_aggregates = None
    log.append("run_start", {"config_sha256": cfg.sha256(), "prompt_set_sha256": prompt_set_sha256(),
                             "quote_rule_sha256": textnorm.quote_rule_sha256(),
                             "quote_rule": textnorm.quote_rule_fingerprint(),
                             "prompt_manifest_problems": len(verify_manifest(PROMPTS_DIR)),
                             "models": model_ids(clients), "stages": list(stages),
                             "corpus_kind": cfg.corpus.get("kind"), "n_participants": len(r.people),
                             "ladder_order": LADDER_CALL_ORDER if cfg.ladder else None, **prereg.code_state(),
                             "code_sha256": codehash.code_sha256(),     # K6: the code, named without git
                             # K7: an app run names the app log's length and head (study records are unchanged)
                             **({"app_log_head": cfg.tool["app_log_head"]} if "app_log_head" in cfg.tool else {}),
                             # L33: a tool run names its reference type (the run-validity check reads it from here)
                             **({"reference_type": cfg.tool["reference_type"]} if "reference_type" in cfg.tool else {}),
                             # K9 / V7: a tool run names its rung-1 user prompt and the as-run no-interview prompt
                             **toolcfg.run_start_fields(cfg.tool, PROMPTS_DIR)})
    new: dict[str, int] = {}
    stages = expand_stages(stages)
    r.record_models(stages)
    try:
        for i, s in enumerate(stages):
            started = time.perf_counter()
            new[s] = STAGES[s](r)
            if new[s]:
                log.append("stage_end", {"stage": s, "new_items": new[s], "statuses": r.work.statuses(s)})
            group = _group_of(s)
            if i + 1 == len(stages) or _group_of(stages[i + 1]) != group:     # the end of a stage group
                audits.run_stage_audits(group, r)
                if group == "Q" and say is not None:
                    primary = r.specs[0].key()
                    say("stage Q: " + report.analysed_line(len(r.people), not_analysed_reasons(
                        r, primary, {p.pid for p in r.people}, judged=False)))
                if group == "M" and "report" in stages:      # the report now carries the final audit summary
                    report.write_report(cfg, guard=r.leak_guard())
            if stage_seconds is not None:
                stage_seconds[s] = time.perf_counter() - started
    except audits.AuditBlocked as exc:
        if any(x.name == "leaks" for x in exc.results):      # nothing from earlier runs may be picked up and sent
            exc.stale_outputs = audits.mark_stale(cfg)
        _log_audits(log, r, blocked=exc)
        raise
    _log_audits(log, r)
    agg_path = report.aggregates_path(cfg)
    log.append("run_end", {"new_items": new, "aggregates_sha256": hashlib.sha256(agg_path.read_bytes()).hexdigest()
                           if agg_path.exists() else None})
    return {"new_items": new, "aggregates": r.last_aggregates}


def _group_of(stage: str) -> str:
    return next(g for g, names in STAGE_GROUPS.items() if stage in names)


def _log_audits(log: AuditLog, r: _Run, blocked: "audits.AuditBlocked | None" = None) -> None:
    """One ``audit_summary`` record per run (counts and code tallies only), plus the evidence file of a block that
    came from a pre-write guard rather than a stage group."""
    results = list(r.audit_results)
    if blocked is not None:
        results += [x for x in blocked.results if x not in results]
        if not any(x.blocking and not x.passed for x in r.audit_results):
            audits.record_blocked(r.cfg, blocked)
    if results:
        log.append("audit_summary", {"audits": [x.summary() for x in results], "blocked": blocked is not None})
