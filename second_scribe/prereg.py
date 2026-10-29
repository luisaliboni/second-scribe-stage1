"""Hashed pre-registration: freeze the analysis plan before any real-data model call, and check it later.

``build_prereg`` assembles ``prereg.json`` (prompt hashes, code commit, config hash, specs and settings, thresholds,
decision rules, the judge-label conventions, model ids and weight hashes, calibration aggregates). ``freeze`` writes it
as canonical JSON and appends one ``prereg_frozen`` ``{sha256, path}`` record to the run's hash-chained audit log.
``check`` proves four things: the file still has the logged hash; the prompt set, the judge-label conventions and
(when given) the config and the code are still the frozen ones; and the freeze precedes every model call in the log.
``require_frozen`` is the guard ``pipeline.run`` calls first when ``cfg.prereg_required`` is set (an optional opt-in;
a pre-registration is not required for a real run).

Nothing here reads participant data. The file holds hashes, constants and aggregates only; endpoints and keys never
enter it (model ids are cut to a basename).
"""
from __future__ import annotations

import hashlib
import json
import os
import glob as _glob
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import judge_bench, prompt_manifest
from .audit.registry import canonical
from .audit.verify import verify_internal_chain
from .llm import redact_params
from .match import PRIMARY_SETTING, SETTINGS_GRID
from .paths import stage_from_dirname

KIT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = KIT_ROOT / "prompts"
DOCS_DIR = KIT_ROOT / "docs"
JUDGE_LABELS_DOC = "prereg-judge-labels.md"
PREREG_NAME = "prereg.json"
RECORD_TYPE = "prereg_frozen"

PRIMARY_SPEC_KEY = "removed__p0__t0__r0"
# Constants frozen with the plan. tests/test_prereg.py compares each one with the default it mirrors in the code,
# so a change on either side shows up as a failing test instead of a silent drift.
MIN_WORDS = 8
MIN_WORDS_GRID = [5, 8, 12]
MAX_UNFOUND_SHARE = 0.50           # L16 (council round 10), was 0.10
SCRIPT_SUPPORT = 2
MIN_CELL_N_FLOOR = 3
MIN_CELL_N_SHARE = 0.5
NOT_JUDGED_LIMIT = 0.10
CANON_BATCH_SIZE = 50
HEADLINE_ORDER = ["absences", "pull_toward_script", "homogenization", "linearization"]
# Judge pass rule of the benchmark on the synthetic traps: one source of truth, judge_bench's PASS_* constants.
JUDGE_PASS_RULE = judge_bench.pass_rule_thresholds()
# Which model role serves a stage of the audit log. A stage this table does not know is compared with every role.
STAGE_ROLES = (("recon:", "recon"), ("q:", "recon"), ("prior_only", "recon"), ("judge2:", "judge2"), ("judge:", "judge"))
# A judge call in letter mode (``judge.judge_modality_logprob``) carries per-call params (``judge.LETTER_PARAMS``) on top of
# the model's own. ``freeze`` records them per judge role (``letter_params``, only when letter mode is on); the check
# holds a call whose stage (after the role prefix) starts with LETTER_STAGE to params + letter_params and every other call to params.
LETTER_STAGE = "modality_letter"
LETTER_ROLES = ("judge", "judge2")
WEIGHTS_FORMAT = re.compile(r"[0-9a-f]{64}|[A-Za-z0-9._-]{1,40}")   # a sha256 or a short revision, never a path
ANCHOR_NAME = "prereg.sha256"
CODE_DIRS = ("second_scribe", "prompts")
# a real-data freeze needs both calibrations (keys of ``calibrate.calibrate_synthetic``'s result)
REAL_CALIBRATION = (("matcher", "matcher"), ("in_transcript", "off-record"))
SEEDS = "derive_seed(item_id, spec_key) / derive_seed(item_id, batch_index)"

DECISION_RULES = "\n".join([
    "1. Primary estimate: spec `removed__p0__t0__r0`, setting `PRIMARY_SETTING` (as set by calibration).",
    "2. A directional claim is made only if the 95% paired bootstrap CI excludes 0 for the primary estimate **and** "
    "the sign is stable across all 8 match settings × variation specs (p1, p2, T=0.7).",
    "3. Maps with failed/excluded reconstructions are dropped from paired comparisons; counts reported.",
    "4. Transcripts with `speaker_format == \"none\"` are excluded from the removed arm and from speaker-gated "
    "metrics; counts reported.",
    "5. Linearization is two-sided. Homogenization is reported overall and by interviewer.",
    "6. Thresholds are never changed after freezing; any post-hoc analysis is labelled exploratory.",
    "7. Coverage: a sign-stability claim needs every cell of the grid (match settings × variation specs) to have a "
    "value (no nan), to be valid (at least max(3, 50% of the primary cell's n) maps) and to have the same sign. A "
    "failed spec or a thin cell blocks the claim; it is never skipped.",
    "8. Pull toward the script is claimed only on canonical labels: the canonical gate requires canonical labels "
    "and canonical alignments for every participant, every spec of the grid and every prior-only sample. Raw-label "
    "and judge-only lines are robustness checks, not claims.",
    "9. Script-conditional leakage (the lead test) is claimed only from an other-family script source: among "
    "matched in-transcript endorsed ideals, the leak rate of the scripted ideals minus that of the unscripted ones, "
    "with the script flag permuted within each map. The claim needs the permutation p below 0.05 and the bootstrap "
    "CI lower bound above 0. The reconstructor's own script is a sensitivity row and never sets the flag. The base "
    "rate (the AI's wished share against the endorsed wished share) and the worse_than_chance_check (the within-map "
    "status-permutation null) are reported, never headlines and never gates.",
    "10. A spec with more than 10% of its elements not judged (for the primary spec, the quote-shuffled re-judge "
    "included) is reported as `reported_not_interpreted`.",
])


# ----------------------------------------------------------------------------------------------- helpers

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _basename(model_id) -> str:
    return str(model_id).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def prereg_path(cfg) -> Path:
    """Where a config's pre-registration lives: ``<out_dir>/prereg.json``."""
    return Path(cfg.out_dir) / PREREG_NAME


def _records(log_path) -> list[dict] | None:
    """The log's records, or None when the file is absent. Callers verify the chain separately."""
    try:
        text = Path(log_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _frozen_records(log_path) -> list[dict]:
    return [r for r in (_records(log_path) or []) if r.get("record_type") == RECORD_TYPE]


def _model_info(cfg, model_info: dict) -> dict:
    """{role: {"model_id" (basename), "model_id_sha256" (of the full id), "weights" (sha256 or short revision),
    "params" (secrets redacted)}}."""
    roles = [r for r in ("recon", "judge", "judge2") if (cfg.models or {}).get(r)]
    out = {}
    for role in roles:
        info = (model_info or {}).get(role)
        if not isinstance(info, dict) or not info.get("model_id") or not info.get("weights"):
            raise ValueError(f"model_info[{role!r}] needs model_id and weights (a sha256, a revision, or 'pending')")
        weights = str(info["weights"])
        if not WEIGHTS_FORMAT.fullmatch(weights):
            raise ValueError(f"model_info[{role!r}] weights must be a 64-hex sha256 or a revision of at most 40 "
                             "letters, digits, '.', '_' or '-' (no paths)")
        out[role] = {"model_id": _basename(info["model_id"]),
                     "model_id_sha256": _sha256_bytes(str(info["model_id"]).encode("utf-8")), "weights": weights,
                     "params": redact_params(info.get("params") or {})}
        if info.get("letter_params"):
            if role not in LETTER_ROLES or not isinstance(info["letter_params"], dict):
                raise ValueError(f"model_info[{role!r}] letter_params: only the judge roles have a letter mode, as a dict")
            out[role]["letter_params"] = redact_params(info["letter_params"])
    return out


def _skip(name: str) -> bool:
    return name == "__pycache__" or name.endswith(".pyc") or name.startswith(".")


def _code_roots(root: Path) -> list[Path]:
    """What is hashed: second_scribe/, prompts/, every top-level package (a folder with __init__.py), and (as
    files) every ``*.py`` at the kit root, which Python would import from there."""
    dirs = set(CODE_DIRS)
    if root.is_dir():
        dirs |= {p.name for p in root.iterdir() if p.is_dir() and not _skip(p.name) and (p / "__init__.py").exists()}
    return [root / d for d in sorted(dirs)]


IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyo", ".so", ".pyd")


def _stdlib_stem(name: str) -> str | None:
    """The module name an importable file would provide when its stem (before the first '.') is a standard-library
    module: ``random.py``, ``random.pyc``, ``random.so``, ``random.cpython-312-darwin.so``, ``random.pyd``."""
    stem = name.split(".")[0]
    return stem if name.endswith(IMPORTABLE_SUFFIXES) and stem in sys.stdlib_module_names else None


def _shadow_files(root: Path) -> list[Path]:
    """Files that could shadow a standard-library module: importable files at the kit root and directly inside each
    top-level folder, whatever the suffix."""
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_file() and _stdlib_stem(p.name):
            out.append(p)
        elif p.is_dir() and p.name != "__pycache__":
            out += [q for q in sorted(p.iterdir()) if q.is_file() and not q.name.startswith(".") and _stdlib_stem(q.name)]
    return out


def code_hashes(kit_root) -> dict[str, str]:
    """{path relative to kit_root (posix): sha256 of the file on disk} for every file under second_scribe/, prompts/
    and every top-level package folder, plus every ``*.py`` at the kit root and every importable file (any suffix:
    ``.pyc``, ``.so``, ``.pyd``, ...) whose stem is a standard-library module name at the root or directly in a top-level
    folder (``__pycache__``, ``*.pyc`` elsewhere and dotfiles left out). Read from disk, not from git, so git index flags
    (skip-worktree, assume-unchanged, info/exclude) cannot hide a change, and a stray ``random.py`` or ``random.so`` shows
    up as a new file."""
    root = Path(kit_root)
    out = {}
    for base in _code_roots(root):
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(n for n in dirnames if not _skip(n))
            for n in sorted(filenames):
                if not _skip(n):
                    f = Path(dirpath) / n
                    out[f.relative_to(root).as_posix()] = _sha256_bytes(f.read_bytes())
    if root.is_dir():
        for f in sorted(root.glob("*.py")):
            if f.is_file() and not _skip(f.name):
                out[f.name] = _sha256_bytes(f.read_bytes())
        for f in _shadow_files(root):
            out[f.relative_to(root).as_posix()] = _sha256_bytes(f.read_bytes())
    return dict(sorted(out.items()))


_PYC_OK: set = set()      # (pyc, its stat, its source's stat) already verified in this process


_NOT_CODE_DIRS = frozenset({".venv", "venv", "out", ".superpowers", "node_modules", "site-packages"})


def _pycache_folders(root: Path) -> list[Path]:
    """The ``__pycache__`` folders of the kit's own code: those under second_scribe/, tests/ and every top-level package
    (dot-folders, virtual environments, ``out/`` and ``.superpowers/`` skipped), and the one at the kit root."""
    bases = {root / d for d in (*CODE_DIRS, "tests")} | set(_code_roots(root))
    found = {root / "__pycache__"}
    for base in bases:
        if not base.is_dir():
            continue
        for d, dirs, _f in os.walk(base):
            dirs[:] = [n for n in dirs if not n.startswith(".") and n not in _NOT_CODE_DIRS]
            if "__pycache__" in dirs:
                found.add(Path(d) / "__pycache__")
    return sorted(f for f in found if f.is_dir())


def _bytecode_problem(root: Path) -> str | None:
    """A ``.pyc`` this interpreter could load from the kit's code must be what the current source compiles to.
    Looked at: every ``__pycache__`` of ``_pycache_folders``. Ignored: a pyc whose tag is not this interpreter's
    (another Python version cannot be loaded here) and an ``.opt-N`` pyc unless ``sys.flags.optimize == N``. For the
    rest the source must exist and, unless the pyc is stale in the way Python itself rejects (timestamp or source hash
    differs, so it recompiles), its code must equal a fresh compile of the source (``optimize=0`` for a plain pyc,
    ``N`` for ``.opt-N``). Delete the ``__pycache__`` folder to clear a refusal."""
    import importlib.util
    import marshal
    tag = sys.implementation.cache_tag
    for cache in _pycache_folders(root):
        for pyc in sorted(cache.glob("*.pyc")):
            parts = pyc.name[:-len(".pyc")].split(".")
            if len(parts) >= 3 and parts[-1].startswith("opt-") and parts[-1][4:].isdigit():
                stem, ptag, opt = ".".join(parts[:-2]), parts[-2], int(parts[-1][4:])
            elif len(parts) >= 2:
                stem, ptag, opt = ".".join(parts[:-1]), parts[-1], 0
            else:
                continue                                              # a name this interpreter would never load
            if ptag != tag or (opt and opt != sys.flags.optimize):
                continue                                              # another interpreter's, or another -O level
            rel = pyc.relative_to(root).as_posix()
            src = cache.parent / f"{stem}.py"
            if not src.is_file():
                return f"{rel} has no source file; delete the __pycache__ folder"
            stamp = (str(pyc), pyc.stat().st_mtime_ns, pyc.stat().st_size, src.stat().st_mtime_ns, src.stat().st_size)
            if stamp in _PYC_OK:
                continue
            data = pyc.read_bytes()
            if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
                return f"{rel} is not a valid bytecode file for this interpreter; delete the __pycache__ folder"
            flags = int.from_bytes(data[4:8], "little")
            source = src.read_bytes()
            if flags & 1:                                               # hash-based pyc
                if flags & 2 and data[8:16] != importlib.util.source_hash(source):
                    continue                                            # checked and stale: Python recompiles it
            else:                                                       # timestamp-based pyc
                st = src.stat()
                if (int.from_bytes(data[8:12], "little") != int(st.st_mtime) & 0xFFFFFFFF
                        or int.from_bytes(data[12:16], "little") != st.st_size & 0xFFFFFFFF):
                    continue                                            # stale: Python recompiles it
            try:
                same = marshal.loads(data[16:]) == compile(source, str(src), "exec", dont_inherit=True, optimize=opt)
            except (ValueError, EOFError, TypeError, SyntaxError):
                same = False
            if not same:
                return f"{rel} does not match its source; delete the __pycache__ folder"
            _PYC_OK.add(stamp)
    return None


def shadow_problem(kit_root) -> str | None:
    """What could make Python run other code than the hashed code, None when nothing does: an importable file or package
    named like a standard-library module at the kit root or directly in a top-level folder (``random.py``, ``random.pyc``,
    ``random.so``, ``random.cpython-312-darwin.so``, ``random.pyd``, a package ``json/``), or a ``.pyc`` in a ``__pycache__``
    that does not match its source."""
    root = Path(kit_root)
    if not root.is_dir():
        return None
    stdlib = set(sys.stdlib_module_names)
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name in stdlib and any(q.is_file() and q.name.split(".")[0] == "__init__"
                                                   and q.name.endswith(IMPORTABLE_SUFFIXES) for q in p.iterdir()):
            return f"{p.name}/ at the kit root shadows the standard-library module {p.name!r}; remove it"
    for p in _shadow_files(root):
        return f"{p.relative_to(root).as_posix()} shadows the standard-library module {_stdlib_stem(p.name)!r}; remove it"
    return _bytecode_problem(root)


def code_file_problem(frozen: dict, kit_root) -> str | None:
    """None when every file under second_scribe/ and prompts/ is the frozen one, else which ones changed."""
    now = code_hashes(kit_root)
    diffs = [f"{n} ({'removed' if n not in now else 'new' if n not in frozen else 'edited'})"
             for n in sorted(set(frozen) | set(now)) if frozen.get(n) != now.get(n)]
    if not diffs:
        return None
    more = f" and {len(diffs) - 5} more" if len(diffs) > 5 else ""
    return "code changed since the freeze: " + ", ".join(diffs[:5]) + more


# ----------------------------------------------------------------------------------------------- build / freeze

def build_prereg(cfg, prompts_dir, *, code_commit: str, model_info: dict, decision_rules: str,
                 calibration: dict | None = None, docs_dir=None, code_root=None, created: str | None = None) -> dict:
    """The pre-registration as a dict. ``code_root`` (default: the folder holding ``prompts_dir``) is hashed file by
    file under second_scribe/ and prompts/. Raises ValueError when the prompt manifest does not match the prompt
    files, when the first spec is not the primary one, or when ``model_info`` misses a configured model."""
    problems = prompt_manifest.verify_manifest(prompts_dir)
    if problems:
        raise ValueError("prompt manifest does not match the prompt files: " + "; ".join(problems))
    manifest = json.loads((Path(prompts_dir) / prompt_manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
    specs = [s.key() for s in cfg.recon_specs()]
    if not specs or specs[0] != PRIMARY_SPEC_KEY:
        raise ValueError(f"the first spec must be the primary one, {PRIMARY_SPEC_KEY!r}; got {specs[:1]}")
    settings = cfg.match_settings()
    labels_path = Path(docs_dir if docs_dir is not None else Path(prompts_dir).parent / "docs") / JUDGE_LABELS_DOC
    labels_bytes = labels_path.read_bytes()
    st = cfg.stats or {}
    return {
        "run_id": cfg.run_id,
        "created": created or datetime.now(timezone.utc).isoformat(),
        "corpus_kind": cfg.corpus.get("kind"),
        "code_commit": code_commit,
        "code_files": code_hashes(code_root if code_root is not None else Path(prompts_dir).parent),
        "config_sha256": cfg.sha256(),
        "prompt_set_sha256": manifest["set_sha256"],
        "prompts": manifest["files"],
        "judge_labels": {"sha256": _sha256_bytes(labels_bytes), "text": labels_bytes.decode("utf-8")},
        "specs": specs,
        "primary_spec": specs[0],
        "settings": [s.key() for s in settings],
        "primary_setting": settings[0].key(),
        "default_primary_setting": PRIMARY_SETTING.key(),
        "settings_grid": [s.key() for s in SETTINGS_GRID],
        "prior_only": cfg.prior_only,
        **({"reading3": cfg.reading3} if cfg.reading3 is not None else {}),     # a legacy record keeps its bytes
        "stats": st,
        "seeds": SEEDS,
        "model_info": _model_info(cfg, model_info),
        "thresholds": {"min_words": int(st.get("min_words", MIN_WORDS)), "min_words_grid": list(MIN_WORDS_GRID),
                       "max_unfound_share": MAX_UNFOUND_SHARE, "script_support": SCRIPT_SUPPORT,
                       "not_judged_limit": NOT_JUDGED_LIMIT},
        "coverage_rule": {"all_cells_same_sign": True, "n_nan": 0, "min_cell_n_floor": MIN_CELL_N_FLOOR,
                          "min_cell_n_share_of_primary": MIN_CELL_N_SHARE,
                          "cell_valid_iff": "n >= max(3, 50% of the primary cell's n)"},
        "canonical_gate": {"canonical_used": True, "judge_channel_canonical": True,
                           "scope": "every participant, every spec of the grid and every prior-only sample has "
                                    "canonical labels and canonical alignments"},
        "leakage_rule": {"claim": "script-conditional leak difference (leak rate of scripted minus unscripted "
                                  "endorsed ideals), other-family script source, permutation p < 0.05 and "
                                  "bootstrap CI lower bound above 0",
                         "own_script": "sensitivity only, never sets the flag",
                         "base_rate": "reported, never a headline, never a gate",
                         "worse_than_chance_check": "reported, never a headline, never a gate"},
        "not_judged_rule": {"limit": NOT_JUDGED_LIMIT, "above_limit": "reported_not_interpreted"},
        "canonicalization": {"batch_size": CANON_BATCH_SIZE,
                             "order": "unique normalized labels in sorted order, shuffled with "
                                      "random.Random(derive_seed(run_id, 'canon')), cut into batches in that order"},
        "judge_pass_rule": JUDGE_PASS_RULE,
        "decision_rules": decision_rules,
        "headline_order": list(HEADLINE_ORDER),
        "calibration": calibration if calibration is not None else {},
    }


def _existing_outputs(out_dir) -> list[str]:
    """Files already under ``out_dir/work`` or ``out_dir/aggregates`` (dotfiles aside)."""
    found = []
    for d in ("work", "aggregates"):
        base = Path(out_dir) / d
        if base.is_dir():
            found += [(Path(dp) / n).relative_to(out_dir).as_posix()
                      for dp, _dn, fns in os.walk(base) for n in fns if not n.startswith(".")]
    return sorted(found)


def freeze(prereg: dict, path, log, *, allow_existing_work: bool = False) -> str:
    """Write ``prereg`` as canonical JSON to ``path``, append a ``prereg_frozen`` record to ``log`` and return the
    file's sha256. Nothing is written or logged when it refuses:

    - the log already holds a freeze, or any ``call_request`` (a freeze must come first);
    - ``<folder of path>/work`` or ``/aggregates`` already holds files: a freeze after the run proves nothing.
      ``allow_existing_work`` waives this for a SYNTHETIC corpus only (tests); a real corpus never gets it;
    - a real corpus with weights still "pending" or without the matcher and off-record calibration aggregates
      (which must pass the aggregate-only string guard), or whose primary setting is not ``match.PRIMARY_SETTING``
      or whose settings do not cover ``match.SETTINGS_GRID``."""
    real = prereg.get("corpus_kind") == "real"
    if real:
        missing = [f"weights hash or revision of {r} (still 'pending')" for r, i in prereg.get("model_info", {}).items()
                   if str(i.get("weights", "")).strip().lower() in ("", "pending")]
        cal = prereg.get("calibration") or {}
        missing += [f"{label} calibration aggregates" for key, label in REAL_CALIBRATION if not cal.get(key)]
        if missing:
            raise ValueError("cannot freeze a real-data preregistration, missing: " + "; ".join(missing))
        if prereg.get("primary_setting") != prereg.get("default_primary_setting"):
            raise ValueError("cannot freeze a real-data preregistration: the primary setting "
                             f"{prereg.get('primary_setting')} is not match.PRIMARY_SETTING "
                             f"({prereg.get('default_primary_setting')}); set it by calibration in the code first")
        uncovered = sorted(set(prereg.get("settings_grid", [])) - set(prereg.get("settings", [])))
        if uncovered:
            raise ValueError(f"cannot freeze a real-data preregistration: settings miss the grid cells {uncovered}")
        from .report import KIT_NOTES, assert_aggregate_only     # late: report imports a lot
        assert_aggregate_only(cal, frozenset(), path="$.calibration", allowed=KIT_NOTES)
    records = _records(log.path) or []
    if any(r.get("record_type") == RECORD_TYPE for r in records):
        raise RuntimeError("prereg already frozen in this log")
    if any(r.get("record_type") == "call_request" for r in records):
        raise ValueError("cannot freeze: the log already holds model calls; a freeze must come before the first one")
    path = Path(path)
    existing = _existing_outputs(path.parent)
    if existing and (real or not allow_existing_work):
        raise ValueError(f"cannot freeze: {len(existing)} file(s) already under work/ or aggregates/ "
                         "(a freeze after the run proves nothing; use a fresh output folder)")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(prereg).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    digest = _sha256_bytes(data)
    log.append(RECORD_TYPE, {"sha256": digest, "path": str(path)})
    return digest


def ensure_writable(folder) -> None:
    """Create ``folder`` if needed and prove a file can be written there (OSError if not)."""
    d = Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".writable-", suffix=".tmp")
    os.close(fd)
    os.unlink(tmp)


def write_anchor(digest: str, run_id: str, anchor_dir) -> Path:
    """``<anchor_dir>/prereg.sha256``: one line, "<sha256> <run_id>". Meant to be committed OUTSIDE the output
    folder (the private runner repo), so deleting the output folder and freezing again leaves a trace."""
    d = Path(anchor_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ANCHOR_NAME
    p.write_text(f"{digest} {run_id}\n", encoding="utf-8")
    return p


def check_anchor(anchor, digest: str, run_id: str) -> str | None:
    """None when the anchor file says this sha256 for this run id, else the reason."""
    try:
        parts = Path(anchor).read_text(encoding="utf-8").split()
    except OSError:
        return f"anchor file not readable: {Path(anchor).name}"
    if len(parts) != 2:
        return "anchor file is not '<sha256> <run_id>'"
    if parts[0] != digest or parts[1] != run_id:
        return "anchor does not match the prereg file (a different freeze, or a re-freeze)"
    return None


class RequestTracker:
    """The run's audit log, seen through the work store: remembers the request_id of every ``call_request`` appended
    since the last ``take``, so ``Work.put`` can stamp an item with the calls that produced it. Everything else is
    passed through to the wrapped log."""

    def __init__(self, log):
        self._log = log
        self._ids: list[str] = []

    def append(self, record_type: str, payload):
        rec = self._log.append(record_type, payload)
        if record_type == "call_request":
            self._ids.append(payload["request_id"])
        return rec

    def note_request_ids(self, ids) -> None:
        """Ids of calls that produced a label this item REUSED (the judge cache): they count as the item's own."""
        self._ids.extend(ids)

    def take(self) -> list[str]:
        ids, self._ids = list(dict.fromkeys(self._ids)), []
        return ids

    def __getattr__(self, name):
        return getattr(self._log, name)


def _glob_dir(pattern) -> Path:
    parts = Path(str(pattern)).parts
    keep = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        keep.append(part)
    return Path(*keep) if keep else Path(".")


def corpus_input_patterns(corpus: dict) -> list[str]:
    """The glob patterns the pipeline expands (maps and transcripts), used only when there are no ``pairs``."""
    return [str(p) for p in (corpus.get("maps_glob"), corpus.get("transcripts_glob")) if p]


def _abs_path(p, base_dir) -> Path:
    q = Path(str(p))
    return q if q.is_absolute() else Path(base_dir) / q


def corpus_pair_paths(corpus: dict, base_dir) -> dict[str, Path]:
    """{"pairs/<pid>/map" or ".../transcript": the path as written} for the explicit ``pairs``. The pipeline opens these
    as LITERAL paths (a name such as ``P[1].vsdx`` is that file, not a glob), so they are never globbed here either."""
    out = {}
    for pid, pair in sorted((corpus.get("pairs") or {}).items()):
        for role, p in zip(("map", "transcript"), list(pair)[:2]):
            out[f"pairs/{pid}/{role}"] = _abs_path(p, base_dir)
    return out


def corpus_input_files(corpus: dict, base_dir) -> list[Path]:
    """The real paths of the files the pipeline would read: the ``pairs`` files (literal) when there are pairs, else
    what the two globs match; sorted. Used for the synthetic check."""
    found = set()
    if corpus.get("pairs"):
        found |= {os.path.realpath(p) for p in corpus_pair_paths(corpus, base_dir).values() if os.path.isfile(p)}
    else:
        for pat in corpus_input_patterns(corpus):
            for m in _glob.glob(str(_abs_path(pat, base_dir))):
                if os.path.isfile(m):
                    found.add(os.path.realpath(m))
    return [Path(m) for m in sorted(found)]


class CorpusError(ValueError):
    """A participant listed in roles or pairs has no map or no transcript file."""


def corpus_inputs(corpus: dict, base_dir) -> dict[str, Path]:
    """{logical name: real path} of every file that makes up the corpus, for hashing. The logical name is what the
    pipeline pairs by, not where the file lives: ``pairs/<pid>/map|transcript`` for explicit pairs (literal paths), else
    ``maps/<path relative to the glob's folder>`` and ``transcripts/<...>``; ``synthetic/<relative path>`` for each
    file under ``synthetic_dir``. Real paths serve only to read (and to follow symlinks). Raises ValueError when a
    pid listed in ``roles`` or ``pairs`` has no map or no transcript file."""
    out: dict[str, Path] = {}
    roles = set(corpus.get("roles") or {})
    if corpus.get("pairs"):
        pair_paths = corpus_pair_paths(corpus, base_dir)
        for pid in sorted(set(corpus["pairs"]) | roles):
            for role in ("map", "transcript"):
                f = pair_paths.get(f"pairs/{pid}/{role}")
                if f is None or not os.path.isfile(f):
                    raise CorpusError(f"participant {pid} has no {role} file")
                out[f"pairs/{pid}/{role}"] = Path(os.path.realpath(f))
    else:
        from .maps.readers import map_stem           # lazy: the readers module is not needed for pairs
        stems: dict[str, set[str]] = {"maps": set(), "transcripts": set()}
        for kind, key in (("maps", "maps_glob"), ("transcripts", "transcripts_glob")):
            if not corpus.get(key):
                continue
            pat = _abs_path(corpus[key], base_dir)
            folder = _glob_dir(pat)
            for m in sorted(_glob.glob(str(pat))):
                if os.path.isfile(m):
                    out[f"{kind}/{Path(os.path.relpath(m, folder)).as_posix()}"] = Path(os.path.realpath(m))
                    stems[kind].add(map_stem(m) if kind == "maps" else Path(m).stem)   # remove the full map suffix
        for pid in sorted(roles):
            for kind in ("maps", "transcripts"):
                if pid not in stems[kind]:
                    raise CorpusError(f"participant {pid} has no {kind[:-1]} file")
    if corpus.get("synthetic_dir"):
        root = _abs_path(corpus["synthetic_dir"], base_dir)
        seen: set[str] = set()                     # real paths of folders already walked: a symlink loop ends here
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            real = os.path.realpath(dirpath)
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames.sort()
            for n in sorted(filenames):
                f = Path(dirpath) / n
                if f.is_file():
                    out[f"synthetic/{f.relative_to(root).as_posix()}"] = Path(os.path.realpath(f))
    return out


def corpus_files_digest(corpus: dict, base_dir) -> str:
    """sha256 over the sorted list of (logical name, sha256 of the file) of ``corpus_inputs``. Moving the corpus folder
    keeps it; changing, adding, renaming, swapping or re-pointing (a symlink) any file changes it. Raises ValueError for a
    listed pid without files."""
    cache: dict[Path, str] = {}                   # real paths only dedupe the reads; the names stay in the hash
    rows = []
    for name, real in sorted(corpus_inputs(corpus, base_dir).items()):
        if real not in cache:
            cache[real] = _sha256_bytes(real.read_bytes())
        rows.append([name, cache[real]])
    return _sha256_bytes(canonical(rows).encode("utf-8"))


def corpus_without_paths(corpus: dict) -> dict:
    """The corpus section as the config hash sees it: no folder or glob paths (the digest of the inputs stands for
    them), participants of ``pairs`` by id only."""
    out = {k: v for k, v in corpus.items() if k not in ("maps_glob", "transcripts_glob", "synthetic_dir", "pairs")}
    if corpus.get("pairs"):
        out["pairs"] = sorted(corpus["pairs"])
    return out


def synthetic_corpus_problem(corpus: dict, base_dir) -> str | None:
    """None when a corpus labelled "synthetic" really is one, else the reason. A ``..`` at or after a glob's first
    wildcard is refused (an earlier one is resolved by ``realpath`` like every other part of the path).
    Every input must resolve (``realpath``, symlinks followed) to a place under ``<kit>/demo`` or under a folder holding
    a ``meta.json`` with a non-empty "domain" (what ``synth.build.build_corpus`` writes) that sits at most two levels
    above it. That is checked for EVERY file a glob matches, and for the folder each pattern starts in (so a pattern
    that matches nothing yet still points at a real-data folder is refused too), and for ``synthetic_dir``."""
    base = Path(base_dir)
    demo = (KIT_ROOT / "demo").resolve()

    def allowed(real: Path) -> bool:
        if real == demo or demo in real.parents:
            return True
        for cand in [real, *list(real.parents)[:2]]:
            try:
                meta = json.loads((cand / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and isinstance(meta.get("domain"), str) and meta["domain"].strip():
                return True
        return False

    def abs_(p) -> Path:
        q = Path(str(p))
        return q if q.is_absolute() else base / q

    patterns = corpus_input_patterns(corpus)
    literals = list(corpus_pair_paths(corpus, base_dir).values())
    for pat in patterns:
        parts = Path(pat).parts
        wild = next((k for k, part in enumerate(parts) if any(c in part for c in "*?[")), len(parts))
        if ".." in parts[wild:]:      # a '..' before the wildcard is resolved by realpath below (the demo config uses one)
            return f"corpus.kind is \"synthetic\" but a glob goes up with '..' after its wildcard: {Path(pat).name}"
    targets = [Path(os.path.realpath(_glob_dir(abs_(pat)))) for pat in patterns]
    targets += [Path(os.path.realpath(f)) for f in literals]          # literal paths, even a dangling one
    if corpus.get("synthetic_dir"):
        targets.append(Path(os.path.realpath(abs_(corpus["synthetic_dir"]))))
    targets += corpus_input_files(corpus, base_dir)
    for t in targets:
        if not allowed(t):
            return ("corpus.kind is \"synthetic\" but an input is neither under the kit's demo/ folder nor in a "
                    f"synthetic corpus folder (meta.json with a domain): {t.name}")
    return None


# ----------------------------------------------------------------------------------------------- code state

def _git(kit_root, *args) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(kit_root), *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def git_head(kit_root=KIT_ROOT) -> str | None:
    """The full commit hash of HEAD, or None when ``kit_root`` is not a git checkout."""
    r = _git(kit_root, "rev-parse", "HEAD")
    return r.stdout.strip() if r is not None and r.returncode == 0 and r.stdout.strip() else None


def code_state(kit_root=KIT_ROOT) -> dict:
    """{"code_commit": HEAD or None, "code_clean": no uncommitted change under second_scribe/ or prompts/ (None when
    unknown)}: recorded in every run_start so the sessions of a run can be tied to one commit (``fork.widen``)."""
    head = git_head(kit_root)
    return {"code_commit": head, "code_clean": None if head is None else check_code(head, kit_root) is None}


def check_code(commit: str, kit_root=KIT_ROOT) -> str | None:
    """None when the code under ``kit_root`` is the frozen code, else the reason. The code is the frozen code when
    the working tree has no uncommitted change under second_scribe/ or prompts/, and the frozen commit is HEAD or an
    ancestor of HEAD with no change under those two folders in between."""
    head = git_head(kit_root)
    if head is None:
        return f"cannot read the git state of {Path(kit_root).name}: the code cannot be tied to the frozen commit"
    known = _git(kit_root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if known is None or known.returncode != 0:
        return f"frozen code commit {commit[:12]} is not in this repository"
    status = _git(kit_root, "status", "--porcelain", "--untracked-files=all", "--", "second_scribe", "prompts")
    if status is None or status.returncode != 0:
        return "cannot read the git status"
    if status.stdout.strip():
        return "uncommitted changes under second_scribe/ or prompts/ since the freeze"
    if known.stdout.strip() == head:
        return None
    anc = _git(kit_root, "merge-base", "--is-ancestor", commit, head)
    if anc is None or anc.returncode != 0:
        return f"frozen code commit {commit[:12]} is not an ancestor of HEAD"
    diff = _git(kit_root, "diff", "--quiet", commit, head, "--", "second_scribe", "prompts")
    if diff is None or diff.returncode not in (0, 1):
        return "cannot diff the code against the frozen commit"
    if diff.returncode == 1:
        return f"code changed under second_scribe/ or prompts/ since the freeze commit {commit[:12]}"
    return None


# ----------------------------------------------------------------------------------------------- check / guard

def _stage_roles(stage: str, frozen_roles) -> list[str]:
    for prefix, role in STAGE_ROLES:
        if stage.startswith(prefix):
            return [role] if role in frozen_roles else []
    return list(frozen_roles)


def _is_letter_stage(stage: str) -> bool:
    """``judge:modality_letter``, ``judge2:modality_letter``: the stages of ``judge.judge_modality_logprob``."""
    return stage.split(":", 1)[-1].startswith(LETTER_STAGE)


def _model_problem(req: dict, pre: dict) -> str | None:
    """Why a call_request does not match the frozen model of its stage (model id basename, inference params)."""
    info = pre.get("model_info") or {}
    stage = str(req.get("stage", ""))
    if "model_id" not in req:
        # a client without a config (the oracle or a replay) is a test double: allowed unless the corpus is real
        return f"a call ({stage}) records no model id" if pre.get("corpus_kind") == "real" else None
    for role in _stage_roles(stage, info):
        i = info[role]
        expected = i.get("params") or {}
        if _is_letter_stage(stage):               # like with like: letter mode = the model's params + the frozen letter params
            expected = {**expected, **(i.get("letter_params") or {})}
        if (_basename(req["model_id"]) == i.get("model_id")
                and _sha256_bytes(str(req["model_id"]).encode("utf-8")) == i.get("model_id_sha256")
                and (req.get("params") or {}) == expected):
            return None
    return f"a call ({stage}) used a model or inference params that differ from the frozen ones"


# Work stages made by model calls, and the audit-log stage prefixes of those calls. An item names the calls that produced
# it (its ``request_ids``); the judge stages share one label cache, so their items may also name the calls behind cached
# labels (``stats.cache_request_ids``), which belong to other items.
OWN_STAGES = {          # audit-log stage prefixes of the calls a work stage makes itself
    "mapping_talk_confirm": ("q:mapping_talk",), "in_transcript": ("judge:in_transcript",), "recon": ("recon:",),
    "prior_only": ("prior_only",), "judge_modality": ("judge:modality",), "judge_shuffled": ("judge:shuffled",),
    "judge2_modality": ("judge2:modality",), "canonicalize": ("judge:canon",), "align": ("judge:align",),
    "script_prior_only": ("judge:script_prior_only", "judge2:script_prior_only")}
CACHE_STAGES = {        # the judge stages share one label cache: calls whose labels an item may have taken from it
    "judge_modality": ("judge:modality", "judge:shuffled"), "judge_shuffled": ("judge:modality", "judge:shuffled"),
    "judge2_modality": ("judge2:modality",)}
MODEL_WORK_STAGES = tuple(OWN_STAGES)
JUDGE_WORK_STAGES = tuple(CACHE_STAGES)
NO_CALL_STATUSES = ("too_long_excluded", "no_speakers_excluded")     # excluded by rule: made no call by construction
MODE_B_MARK = "mode_b"      # pipeline._stage_in_transcript: a mode-B item, every node present by construction (L33)


def logged_reference_type(records) -> str | None:
    """The reference type (``"A"``/``"B"``/``"C"``) every ``run_start`` of the log records, or None: a study log (no
    ``reference_type`` field), no run_start, or sessions that disagree."""
    types = {(r.get("payload") or {}).get("reference_type") for r in records if r.get("record_type") == "run_start"}
    return types.pop() if len(types) == 1 else None


def _no_call_by_construction(stage: str, item: dict, reference_type: str | None = None) -> bool:
    """An "ok" model-stage item the pipeline writes without any call: no speaker labels, nothing flagged, no labels to
    ask about, an empty side to align, or a judge item whose every element was decided by rule (no quote words, so
    "unsupported"; no batch sent, nothing served from the cache). A judge item served from the cache is NOT here: it
    names the calls behind the cached labels. An in_transcript item of a mode-B run (``reference_type`` "B", the
    run's own type as its log records it) with every node marked present is here too, but only with the mode-B marker
    and the reference file's own type "B" (audit K9 agreed before the skip wrote it); a study run never has one."""
    if stage == "mapping_talk_confirm":
        st = item.get("stats") or {}        # the model screens every interviewer turn: no call only if there are none
        return item.get("speaker_format") == "none" or st.get("n_interviewer_turns", st.get("rule_flagged")) == 0
    if stage == "in_transcript":
        marks = item.get("in_transcript")
        if marks == {}:
            return True
        return (reference_type == "B" and item.get("by_construction") == MODE_B_MARK
                and item.get("reference_file_type") == "B"
                and isinstance(marks, dict) and all(v is True for v in marks.values()))
    if stage in JUDGE_WORK_STAGES:
        if item.get("n_items") == 0 or item.get("labels") is None:
            return True
        st = item.get("stats") or {}
        return (st.get("batches") == 0 and st.get("cache_hits") == 0
                and set((item.get("labels") or {}).values()) <= {"unsupported"})
    if stage == "align":
        return not item.get("left") or not item.get("right")
    return False


LADDER_STORE = "recon:"     # work stores recon:<rung key> hold the instruction-ladder items (ladder.rung_stage)


def _ladder_rung(store: str) -> str | None:
    """The rung key of a ladder work store (``recon:rung3`` -> ``rung3``), None for any other store."""
    return store[len(LADDER_STORE):] if store.startswith(LADDER_STORE) and len(store) > len(LADDER_STORE) else None


def _own_prefixes(store: str) -> tuple | None:
    """Audit-log stage prefixes of the calls the work store makes itself, None for a store that makes no calls here.
    A ladder store ``recon:<rung>`` makes calls of the stages ``recon:<arm>:<rung>``."""
    if store in OWN_STAGES:
        return OWN_STAGES[store]
    return ("recon:",) if _ladder_rung(store) else None


def _stage_ok(store: str, stage: str) -> bool:
    rung = _ladder_rung(store)
    return stage.startswith("recon:") and stage.endswith(":" + rung) if rung else True


def _owns(stage: str, name: str, call_item_id: str) -> bool:
    """Does the call ``call_item_id`` (the ``item_id`` it was logged with) belong to work item ``name``?"""
    if (rung := _ladder_rung(stage)) is not None:      # a ladder call is logged as <name>@<rung> (ladder.rung_item_id)
        own = f"{name}@{rung}"
        return call_item_id == own or call_item_id.startswith(own + ":")
    if stage == "canonicalize":
        try:
            own = f"canon:{int(name.removeprefix('batch_'))}"
        except ValueError:
            return False
    else:
        own = name
    return call_item_id == own or call_item_id.startswith(own + ":")


REUSE_STORE = "recon:rung2"     # the only ladder store whose items may be taken over from the main run
REUSE_SOURCE = "recon"


def _reuse_problem(root: Path, store: str, name: str, item: dict, reused, calls) -> str | None:
    """Why a ``reused_from`` item is not what it says (None if it is): only rung 2 reuses a main-run draw, from the
    ``recon`` item of the same name, with the same status and the same request ids, every one of them a call of that
    source item. It claims nothing: the source item holds the claim."""
    if store != REUSE_STORE:
        return f"reuses a main-run item but only {REUSE_STORE} may"
    if reused != {"stage": REUSE_SOURCE, "name": name}:
        return "has a reused_from that does not name the main-run item of the same name"
    try:
        src = json.loads((root / REUSE_SOURCE / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "reuses a main-run item that is missing or unreadable"
    if not isinstance(src, dict) or src.get("status") != item.get("status") or src.get("request_ids") != item.get("request_ids"):
        return "does not match the main-run item it reuses (status or request ids differ)"
    for rid in item.get("request_ids") or []:
        stage, item_id = calls[rid]
        if not stage.startswith("recon:") or not _owns(REUSE_SOURCE, name, item_id):
            return f"names a call that does not belong to the main-run item ({item_id})"
    return None


def _work_problem(work_dir, calls: dict[str, tuple[str, str]], reference_type: str | None = None) -> str | None:
    """Check the work store against the calls logged after the freeze (``calls``: request_id -> (stage, item_id)).

    - Every item names only such calls, and every call is of its stage and belongs to it (the id's item_id is the
      item's name), except that a judge item may also name the calls behind labels it took from the cache (listed in
      its ``stats.cache_request_ids``, of the judge stages). No call is claimed as its own by two items.
    - A model-stage item with a status other than too_long_excluded / no_speakers_excluded must name at least one call,
      unless it is an "ok" item that makes no call by construction (``reference_type``: the run's, from its log).
      ``failed`` and ``fallback`` carry ids. A
      ``quarantined`` item (taken back by a failed canary) is held to its ``original_status``.
    - Every entry of a judge cache file at the work root names a call logged after the freeze.
    """
    root = Path(work_dir)
    if not root.is_dir():
        return None
    for f in sorted(root.glob("*cache*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"judge cache {f.name} is not readable"
        for key, v in (data.items() if isinstance(data, dict) else []):
            rid = v.get("request_id") if isinstance(v, dict) else None
            if rid not in calls or not calls[rid][0].startswith("judge"):
                return (f"judge cache {f.name} holds a label not produced after the freeze "
                        "(its request id is missing or is not a judge call logged after it)")
    claimed: dict[str, str] = {}
    for sub in sorted(d for d in root.iterdir() if d.is_dir()):
        store = stage_from_dirname(sub.name)     # the stage key (on Windows the folder has no colon)
        for f in sorted(sub.glob("*.json")):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(item, dict) or "status" not in item:
                continue
            where = f"{store}/{f.stem}"
            ids = item.get("request_ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or not set(ids) <= set(calls):
                return (f"work item {where} was not produced after the freeze "
                        "(its request ids are missing or are not calls logged after it)")
            own_prefixes = _own_prefixes(store)
            if own_prefixes is None:
                continue
            reused = item.get("reused_from")
            if reused is not None:      # a ladder draw taken over from the main run: its calls are the source item's
                problem = _reuse_problem(root, store, f.stem, item, reused, calls)
                if problem:
                    return f"work item {where} {problem}"
                continue
            cached = set((item.get("stats") or {}).get("cache_request_ids") or []) if store in CACHE_STAGES else set()
            for rid in ids:
                stage, item_id = calls[rid]
                if not stage.startswith(own_prefixes + CACHE_STAGES.get(store, ())) or not _stage_ok(store, stage):
                    return f"work item {where} names a call of another stage ({stage})"
                if _owns(store, f.stem, item_id) and stage.startswith(own_prefixes):
                    if claimed.setdefault(rid, where) != where:
                        return f"work item {where} names a call already claimed by {claimed[rid]}"
                elif rid not in cached:      # not its own call: only a listed cache hit may name it
                    if _owns(store, f.stem, item_id):         # (a sibling stage can log a call under this item's name)
                        return f"work item {where} names a call of another stage ({stage})"
                    return f"work item {where} names a call that belongs to another item ({item_id})"
            status = item["status"]
            if status == "quarantined":     # taken back by a failed canary (audits.quarantine_session): judged as it was
                status = item.get("original_status")
            if (status not in NO_CALL_STATUSES and not ids
                    and not (status == "ok" and _no_call_by_construction(store, item, reference_type))):
                return (f"work item {where} was not produced after the freeze "
                        "(a model-stage item names no model call)")
    return None


# ------------------------------------------------------------------ run validity (no pre-registration involved)
# These two checks used to run only as part of the prereg check. They are not a lock (no frozen rules, no freeze):
# they say the work store and the log describe the same run, made by one instrument per role. A real corpus always
# gets them, with or without a prereg file.

def _instrument_key(req: dict) -> tuple | None:
    """(role, letter mode) of a call_request whose stage names a role and which records a model id, else None."""
    stage = str(req.get("stage", ""))
    for prefix, role in STAGE_ROLES:
        if stage.startswith(prefix):
            return (role, _is_letter_stage(stage)) if "model_id" in req else None
    return None


def _instrument(req: dict) -> str:
    return json.dumps([req.get("model_id"), req.get("params") or {}], sort_keys=True)


_START_FIELDS = (("prompt_set_sha256", "prompt set"), ("config_sha256", "config"),
                 ("quote_rule_sha256", "quote rule"))


FORK_RECORD = "run_fork"
FORK_FIELDS = ("prompt_set_sha256", "config_sha256", "quote_rule_sha256")


def fork_problem(records) -> str | None:
    """None, or why a ``run_fork`` record of a log (``fork.build_fork``, ``fork.widen``) is not one this kit wrote: each
    sits right after the copied prefix of its source log (its seq is the source's record count and its prev_hash the
    source's head hash) and declares the hashes the run continues with. A chain (a pilot fork, then the widened full
    run) is allowed: the run is described by the LAST fork and the sessions after it. A log without a fork passes."""
    for f in records:
        if f.get("record_type") != FORK_RECORD:
            continue
        pl = f.get("payload") or {}
        if pl.get("source_records") != f.get("seq") or pl.get("source_head_hash") != f.get("prev_hash"):
            return "a run_fork record does not sit right after the source log it names"
        if any(not isinstance(pl.get(k), str) for k in FORK_FIELDS):
            return "a run_fork record does not declare the prompt-set, config and quote-rule hashes"
    return None


def session_starts(records) -> list[dict]:
    """The ``run_start`` payloads that describe the run the log is extending now. Without a ``run_fork`` record: every
    run_start, as always. After a fork: the fork's declared hashes, then the run_starts written after it (the records
    before the fork are the source run's, kept verbatim, with the source run's own hashes)."""
    idx = [i for i, r in enumerate(records) if r.get("record_type") == FORK_RECORD]
    if not idx:
        return [r.get("payload") or {} for r in records if r.get("record_type") == "run_start"]
    fork = records[idx[-1]].get("payload") or {}
    return [{k: fork.get(k) for k in FORK_FIELDS}] + [r.get("payload") or {} for r in records[idx[-1] + 1:]
                                                       if r.get("record_type") == "run_start"]


def _start_disagreement(records) -> str | None:
    """Why the ``run_start`` records of a log do not describe ONE run: they carry more than one prompt-set hash or
    more than one config hash (a missing value is ignored). After a fork only the fork's declared hashes and the later
    run_starts count (``session_starts``)."""
    if (why := fork_problem(records)) is not None:
        return why
    starts = session_starts(records)
    for field, label in _START_FIELDS:
        seen = {p.get(field) for p in starts}
        seen.discard(None)
        if len(seen) > 1:
            return (f"the run_start records of this log carry {len(seen)} different {label} hashes: the "
                    f"sessions of one run must use one {label}")
    return None


def restart_problem(log_path, prompt_set_sha256, config_sha256, quote_rule_sha256=None) -> str | None:
    """None, or why this session may not continue the run the log already records: the prompt set, the config or
    (run 2) the quote rule it would run with differs from the one an earlier ``run_start`` recorded. Called before a
    real run resumes. ``quote_rule_sha256`` None skips that comparison; a log that never recorded one is not compared.
    A log made by ``fork`` is compared with the hashes its ``run_fork`` record declared and the sessions after it."""
    all_records = _records(log_path) or []
    if (why := fork_problem(all_records)) is not None:
        return why
    starts = session_starts(all_records)          # after a fork: the fork's declared hashes and the later sessions
    checks = [("prompt_set_sha256", "prompt set", prompt_set_sha256), ("config_sha256", "config", config_sha256)]
    if quote_rule_sha256 is not None:
        checks.append(("quote_rule_sha256", "quote rule", quote_rule_sha256))
    for field, label, now in checks:
        was = {p.get(field) for p in starts} - {None}
        if (was and now not in was) or len(was) > 1:
            return f"the {label} hash differs from the one an earlier session of this log recorded"
    return None


def run_validity_problem(log_path, work_dir) -> str | None:
    """None, or why the run is not internally consistent. Only for a log that records a real corpus (a ``run_start``
    with ``corpus_kind`` real); a synthetic or unknown log passes. Checks (1) every work item names the logged calls
    that produced it, none claimed twice (``_work_problem``), and (2) within the log every role used one model id and
    one set of inference params (letter-mode judge calls are compared among themselves)."""
    records = _records(log_path)
    if not records or not any(r.get("record_type") == "run_start" and (r.get("payload") or {}).get("corpus_kind") == "real"
                              for r in records):
        return None
    if (why := _start_disagreement(records)) is not None:
        return why
    calls: dict[str, tuple[str, str]] = {}
    seen: dict[tuple, set[str]] = {}
    for r in records:
        if r.get("record_type") != "call_request":
            continue
        req = r["payload"]
        calls[req["request_id"]] = (str(req.get("stage", "")), str(req.get("item_id", "")))
        if (key := _instrument_key(req)) is not None:
            seen.setdefault(key, set()).add(_instrument(req))
    for (role, letter), variants in sorted(seen.items()):
        if len(variants) > 1:
            return (f"the {role} role used {len(variants)} different model or inference-param settings"
                    f"{' (letter mode)' if letter else ''} in one run")
    return _work_problem(work_dir, calls, logged_reference_type(records))


def resume_problem(log_path, clients: dict) -> str | None:
    """None, or why ``clients`` are not the instruments the log already records (model id, and for non-letter calls the
    inference params). Called before a real run resumes; a client without a ModelConfig (offline) is not compared."""
    records = _records(log_path) or []
    for role, c in sorted(clients.items()):
        cfg = getattr(c, "cfg", None)
        if cfg is None or getattr(cfg, "model_id", None) is None:
            continue
        now = json.dumps([cfg.model_id, redact_params(dict(getattr(cfg, "params", None) or {}),
                                                      getattr(cfg, "api_key_env", None))], sort_keys=True)
        for r in records:
            if r.get("record_type") != "call_request":
                continue
            req = r["payload"]
            if (key := _instrument_key(req)) is None or key[0] != role:
                continue
            if key[1]:       # letter mode adds per-call params: compare the model id only
                was, here = req.get("model_id"), cfg.model_id
                if was != here:
                    return f"the {role} model id differs from the one in the log"
                continue
            if _instrument(req) != now:
                what = "model id" if req.get("model_id") != cfg.model_id else "inference params"
                return f"the {role} {what} differs from the one in the log"
    return None


def check(path, log_path, prompts_dir, *, cfg=None, kit_root=None, docs_dir=None, work_dir=None,
          anchor=None) -> tuple[bool, str]:
    """(True, "ok") iff the pre-registration is intact and was frozen before any model call.

    Always checked: the log's chain verifies; it holds exactly one ``prereg_frozen`` record; the file at ``path`` has
    that record's sha256; every prompt file (and the prompt set hash) equals the frozen one; the judge-label document
    equals the frozen one; NO ``call_request`` precedes the freeze (deny by default: calibration and benchmark runs
    belong in their own log); every call after it used the frozen model id and inference params of its stage.
    With ``cfg`` the config hash must equal the frozen one. With ``work_dir`` every work item must name only calls
    logged after the freeze. With ``kit_root`` every file under second_scribe/ and prompts/ must hash as frozen
    (read from disk) and the code commit must pass ``check_code``. With ``anchor`` (a ``prereg.sha256`` file) it
    must match the file's hash and the run id. A missing freeze is the message "prereg not frozen"."""
    if not Path(log_path).exists():
        return False, "prereg not frozen"
    ok, detail = verify_internal_chain(log_path)
    if not ok:
        return False, f"log chain broken: {detail}"
    records = _records(log_path) or []
    frozen = [r for r in records if r.get("record_type") == RECORD_TYPE]
    if not frozen:
        return False, "prereg not frozen"
    if len(frozen) > 1:
        return False, "more than one prereg_frozen record in the log"
    path = Path(path)
    if not path.exists():
        return False, f"prereg file missing: {path.name}"
    data = path.read_bytes()
    digest = _sha256_bytes(data)
    if digest != frozen[0]["payload"].get("sha256"):
        return False, "prereg file changed since the freeze (hash differs from the logged one)"
    pre = json.loads(data.decode("utf-8"))

    now = prompt_manifest.file_hashes(prompts_dir)
    was = pre.get("prompts", {})
    diffs = [f"{n} ({'removed' if n not in now else 'new' if n not in was else 'edited'})"
             for n in sorted(set(was) | set(now)) if was.get(n) != now.get(n)]
    if diffs:
        return False, "prompt changed: " + ", ".join(diffs)
    if prompt_manifest.set_hash(now) != pre.get("prompt_set_sha256"):
        return False, "prompt changed: the prompt set hash differs from the frozen one"

    labels = Path(docs_dir if docs_dir is not None else Path(prompts_dir).parent / "docs") / JUDGE_LABELS_DOC
    try:
        labels_sha = _sha256_bytes(labels.read_bytes())
    except OSError:
        return False, "judge label conventions missing"
    if labels_sha != (pre.get("judge_labels") or {}).get("sha256"):
        return False, "judge label conventions changed since the freeze"

    if cfg is not None:
        try:
            config_hash = cfg.sha256()
        except (ValueError, OSError) as exc:
            return False, f"config changed since the freeze: its corpus cannot be read ({exc})"
        if config_hash != pre.get("config_sha256"):
            return False, "config changed since the freeze"

    freeze_seq = frozen[0]["seq"]
    post_calls: dict[str, tuple[str, str]] = {}
    for r in records:
        if r.get("record_type") != "call_request":
            continue
        if r["seq"] < freeze_seq:
            return False, f"a model call ({r['payload'].get('stage')}) was logged before the freeze"
        problem = _model_problem(r["payload"], pre)
        if problem:
            return False, problem
        post_calls[r["payload"]["request_id"]] = (str(r["payload"].get("stage", "")), str(r["payload"].get("item_id", "")))

    if work_dir is not None:
        rtype = logged_reference_type(records)
        if cfg is not None and (getattr(cfg, "tool", None) or {}).get("reference_type") != rtype:
            rtype = None                # the config and the log must both name the type (a study config names none)
        problem = _work_problem(work_dir, post_calls, rtype)
        if problem:
            return False, problem

    if kit_root is not None:
        problem = (shadow_problem(kit_root) or code_file_problem(pre.get("code_files") or {}, kit_root)
                   or check_code(pre.get("code_commit", ""), kit_root))
        if problem:
            return False, problem

    if anchor is not None:
        problem = check_anchor(anchor, digest, pre.get("run_id", ""))
        if problem:
            return False, problem
    return True, "ok"


def require_frozen(cfg, log) -> None:
    """The guard at the top of ``pipeline.run``: when ``cfg.prereg_required``, raise RuntimeError unless
    ``check`` passes for this config's prereg file and this run's log (prompts, config, code, models, ordering, and
    the provenance of every work item). The message is exactly "prereg not frozen" when no freeze exists, else
    "prereg check failed: <reason>"."""
    if not cfg.prereg_required:
        return
    ok, msg = check(prereg_path(cfg), log.path, PROMPTS_DIR, cfg=cfg, kit_root=KIT_ROOT,
                    work_dir=Path(cfg.out_dir) / "work")
    if not ok:
        raise RuntimeError(msg if msg == "prereg not frozen" else f"prereg check failed: {msg}")
