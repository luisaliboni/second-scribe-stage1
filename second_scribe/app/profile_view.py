"""Profile files for the app's results surfaces (plan amendment V2, task T19b).

Every results surface renders a profile of schema ``orgsci-profile/v2`` (analysis/public/PROFILE-SCHEMA.md,
sections 1 to 4 and 12). This module is the app's only door to those files:

* ``load_profile``   parse and validate the structure (it computes nothing and adds no metric);
* ``run_exporter``   run the kit's ``export-profile`` command and return the sha256 of the file it wrote;
* ``build_tryit_profile``  the Try-it synthetic profile: the exporter on the recorded demo replay (for the site build);
* ``profile_to_js``  turn a profile into a ``window.<var> = <json>;`` script for the offline site;
* ``cells``          the (row, name, x) triples of a profile, which the renderer tests use to check that every
                     cell is drawn exactly once.

Standard library only; no model calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]            # the folder that holds the second_scribe package
DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
DEFAULT_B = 2000                                           # the exporter's defaults (experiment session, 18 Oct)
DEFAULT_SEED = 20261017

SCHEMA_RE = re.compile(r"^orgsci-profile/v(\d+)(?:\.\d+)*$")
SUPPORTED_MAJOR = 2
FILE_STATES = ("reported", "synthetic", "pending")
CELL_STATES = ("absent", "not_run", "partial_not_run")
NEEDS = ("J2", "sidejudge", "ladder", "judge_script")
BAND_OVER = ("8_settings", "lenient", "labellers", "ci95", "variants")
ROW_ORDER = ("lead", "ledger", 1, 2, 3, 4, 5, 6, 7, 8, 9)
TOP_REQUIRED = ("schema", "state", "population", "reference", "pooling", "rows", "run")
PLACEHOLDER = "[SOURCE]"
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA_LINE = re.compile(r"^sha256 ([0-9a-f]{64})$")
_REFUSED = "refused: "
_REASON_CODE = re.compile(r"^[a-z0-9_.-]{1,40}$")        # a code that says why a value is null (§14, public form)
MODEL_TEXT_FIELDS = ("role", "family", "model_id", "weights", "quantisation", "thinking", "source")

# Run block and pooling (PROFILE-SCHEMA §4 for the study's v2 file, §14 for the kit exporter, E2 M1 6b8c398; the
# study's public file, profile-v3, writes the §14 form: run_chain_heads null with run_chain_heads_source, run_commits
# a list, release_commit null, a null model field with that model's source "not_recorded_public").
HEADS_PHASE = ("q", "j", "m")                              # v2: one head per phase of the real run
HEADS_KIT = ("head", "records", "manifest_hash")           # §14: the one kit chain
HEADS_PACK = "demo_single_log"                             # the analysis pack's diagnostic form (parity-synth only)
POOLING_WITHIN = (("reference_type", "prompt_set"),                   # v2 (the study's file)
                  ("reference_type", "prompt_set_pair", "state"))     # §12/§14 (kit exports)


class ProfileInvalid(Exception):
    """The file does not follow the profile schema. ``path`` locates the problem inside the document
    (for example ``rows[3].cells[0].value``) and ``reason`` says what is wrong. Neither carries the offending
    value, so the message is safe to show."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class ProfileRefused(Exception):
    """The exporter refused to write a profile (exit 2). ``reasons`` are its stderr lines, to be shown verbatim."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons) if reasons else "the exporter refused")
        self.reasons = list(reasons)


# ----------------------------------------------------------------------------------------------- small checks
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_or_placeholder(v) -> bool:
    return _is_num(v) or v == PLACEHOLDER


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(s, str) for s in v)


MISSING = ("band", "n", "ci")  # PROFILE-SCHEMA §13


def _fail(path: str, reason: str):
    raise ProfileInvalid(path, reason)


def _no_dup_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ProfileInvalid("$", "duplicate key in the file")
        out[k] = v
    return out


def _no_constants(name):
    raise ProfileInvalid("$", "NaN or Infinity is not valid JSON")


# ----------------------------------------------------------------------------------------------- validation
def _check_band(band, path: str) -> None:
    if not isinstance(band, dict):
        _fail(path, "band must be an object")
    over = band.get("over")
    if over not in BAND_OVER:
        _fail(path + ".over", f"must be one of {', '.join(BAND_OVER)}")
    if "state" in band:
        # a band that could not be computed: {state: "not_run", over, reason}
        if band["state"] != "not_run":
            _fail(path + ".state", "a band state can only be 'not_run'")
        return
    for k in ("lo", "hi"):
        if k not in band:
            _fail(f"{path}.{k}", "required")
        if not _num_or_placeholder(band[k]):
            _fail(f"{path}.{k}", "must be a number")
    members = band.get("members")
    if members is not None:
        if not isinstance(members, list):
            _fail(path + ".members", "must be a list")
        for i, m in enumerate(members):
            mp = f"{path}.members[{i}]"
            if not isinstance(m, dict):
                _fail(mp, "must be an object")
            if not isinstance(m.get("x"), str) or not m["x"]:
                _fail(mp + ".x", "must be a non-empty string")
            if "value" in m and not _num_or_placeholder(m["value"]):
                _fail(mp + ".value", "must be a number")
            if "value_hi" in m and not _num_or_placeholder(m["value_hi"]):
                _fail(mp + ".value_hi", "must be a number")
    subbands = band.get("subbands")
    if subbands is not None:
        if not isinstance(subbands, list):
            _fail(path + ".subbands", "must be a list")
        for i, s in enumerate(subbands):
            sp = f"{path}.subbands[{i}]"
            if not isinstance(s, dict):
                _fail(sp, "must be an object")
            if not isinstance(s.get("name"), str) or not s["name"]:
                _fail(sp + ".name", "must be a non-empty string")
            for k in ("lo", "hi"):
                if not _num_or_placeholder(s.get(k)):
                    _fail(f"{sp}.{k}", "must be a number")


def _check_cell(c, path: str) -> None:
    if not isinstance(c, dict):
        _fail(path, "cell must be an object")
    if not isinstance(c.get("name"), str) or not c["name"]:
        _fail(path + ".name", "required non-empty string")
    if "x" in c and c["x"] is not None and not isinstance(c["x"], str):
        _fail(path + ".x", "must be a string")
    if not isinstance(c.get("source"), str) or not c["source"]:
        _fail(path + ".source", "required non-empty string")
    for k in ("unit", "kind", "note", "reason", "reason_code"):
        if k in c and c[k] is not None and not isinstance(c[k], str):
            _fail(f"{path}.{k}", "must be a string")
    if "missing" in c and (not isinstance(c["missing"], list) or any(m not in MISSING for m in c["missing"])):
        _fail(path + ".missing", f"must be a list drawn from {', '.join(MISSING)}")
    if "n" in c and c["n"] is not None and not (_is_int(c["n"]) or c["n"] == PLACEHOLDER):
        _fail(path + ".n", "must be an integer")
    for k in ("ci_lo", "ci_hi", "p", "p_lt", "p_holm", "p_holm_lt"):
        if k in c and c[k] is not None and not _num_or_placeholder(c[k]):
            _fail(f"{path}.{k}", "must be a number")
    if "ci_level" in c and c["ci_level"] is not None and not _is_num(c["ci_level"]):
        _fail(path + ".ci_level", "must be a number or null")
    for k in ("rests_on", "qualifiers"):
        if k in c and not _is_str_list(c[k]):
            _fail(f"{path}.{k}", "must be a list of strings")
    if "band" in c:
        _check_band(c["band"], path + ".band")

    state = c.get("state")
    if state is not None and state not in CELL_STATES:
        _fail(path + ".state", f"must be one of {', '.join(CELL_STATES)} or absent")

    if state == "not_run":
        if c.get("value") is not None:
            _fail(path + ".value", "a not_run cell has no value")
        needs = c.get("needs")
        if needs is not None:
            if not isinstance(needs, list) or any(n not in NEEDS for n in needs):
                _fail(path + ".needs", f"must be a list drawn from {', '.join(NEEDS)}")
        if not needs:
            # §13: needs or a reason_code (exporter); a free reason only in pack diagnostics (parity-synth)
            why = c.get("reason_code") or c.get("note") or c.get("reason")
            if not (isinstance(why, str) and why):
                _fail(path + ".needs", "a not_run cell needs at least one code (or a stated reason)")
    else:
        if "needs" in c:
            _fail(path + ".needs", "only a not_run cell may carry needs")
        if "value" not in c:
            _fail(path + ".value", "required")
        if not _num_or_placeholder(c["value"]):
            _fail(path + ".value", "must be a number")

    if "derived" in c:
        d = c["derived"]
        if d is True:
            f = c.get("formula") or c.get("formula_code")      # §13: lab exports carry formula_code
            if not isinstance(f, str) or not f:
                _fail(path + ".formula", "derived: true needs a formula or formula_code")
        elif not (isinstance(d, str) and d):
            _fail(path + ".derived", "must be true or the name of a formula")
    if "formula" in c and not isinstance(c["formula"], str):
        _fail(path + ".formula", "must be a string")
    if "formula_code" in c and not isinstance(c["formula_code"], str):
        _fail(path + ".formula_code", "must be a string")


def _check_row(r, i: int) -> None:
    path = f"rows[{i}]"
    if not isinstance(r, dict):
        _fail(path, "row must be an object")
    want = ROW_ORDER[i]
    got = r.get("row")
    if got != want or isinstance(got, bool) or type(got) is not type(want):
        _fail(path + ".row", f"rows must run lead, ledger, 1 to 9; expected {want!r} here")
    if not isinstance(r.get("label_key"), str) or not r["label_key"]:
        _fail(path + ".label_key", "required non-empty string")
    if not isinstance(r.get("population"), str):
        _fail(path + ".population", "required string")
    if not _is_int(r.get("n_people")):
        _fail(path + ".n_people", "required integer")
    for k in ("rests_on", "source", "qualifiers"):
        if k not in r:
            _fail(f"{path}.{k}", "required")
        if not _is_str_list(r[k]):
            _fail(f"{path}.{k}", "must be a list of strings")
    if not isinstance(r.get("cells"), list):
        _fail(path + ".cells", "required list")
    if "band" in r:
        _check_band(r["band"], path + ".band")
    seen = set()
    for j, c in enumerate(r["cells"]):
        cp = f"{path}.cells[{j}]"
        _check_cell(c, cp)
        key = (c["name"], c.get("x"))
        if key in seen:
            _fail(cp, "duplicate cell (same name and x in this row)")
        seen.add(key)


def _hex_or_unknown(v) -> bool:
    return v is None or v == PLACEHOLDER or (isinstance(v, str) and bool(_HEX64.fullmatch(v)))


def _commit_or_unknown(v) -> bool:
    return v == PLACEHOLDER or (isinstance(v, str) and bool(_COMMIT.fullmatch(v)))


def _check_source(obj: dict, path: str) -> None:
    if "source" in obj and obj["source"] is not None and not isinstance(obj["source"], str):
        _fail(path + ".source", "must be a string")


def _check_run_chain_heads(h, state: str) -> None:
    path = "run.run_chain_heads"
    if h is None:
        return
    if not isinstance(h, dict):
        _fail(path, "must be an object or null")
    keys = set(h) - {"source"}
    _check_source(h, path)
    if keys and keys <= set(HEADS_PHASE):                  # v2: {q, j, m, source}
        for k in sorted(keys):
            if not _hex_or_unknown(h[k]):
                _fail(f"{path}.{k}", "must be a lowercase 64-hex chain head, \"[SOURCE]\" or null")
        return
    if "head" in keys or "manifest_hash" in keys or "records" in keys:      # §14: {head, records, manifest_hash}
        if keys != set(HEADS_KIT):
            _fail(path, "the kit shape is {head, records, manifest_hash, source}; mixed or partial shape")
        for k in ("head", "manifest_hash"):
            if not _hex_or_unknown(h[k]):
                _fail(f"{path}.{k}", "must be a lowercase 64-hex hash, \"[SOURCE]\" or null")
        r = h["records"]
        if not (r is None or r == PLACEHOLDER or (_is_int(r) and r >= 0)):
            _fail(path + ".records", "must be a count (int >= 0), \"[SOURCE]\" or null")
        return
    if keys == {HEADS_PACK} and state == "synthetic":      # parity-synth: {demo_single_log: {run: {...}}, source}
        logs = h[HEADS_PACK]
        lp = f"{path}.{HEADS_PACK}"
        if not isinstance(logs, dict) or not logs:
            _fail(lp, "must be a non-empty object")
        for name, entry in logs.items():
            ep = f"{lp}.{name}"
            if not isinstance(entry, dict) or set(entry) != {"head_hash", "record_count", "log"}:
                _fail(ep, "must be {head_hash, record_count, log}")
            if not (isinstance(entry["head_hash"], str) and _HEX64.fullmatch(entry["head_hash"])):
                _fail(ep + ".head_hash", "must be a lowercase 64-hex chain head")
            if not (_is_int(entry["record_count"]) and entry["record_count"] >= 0):
                _fail(ep + ".record_count", "must be a count (int >= 0)")
            if not isinstance(entry["log"], str):
                _fail(ep + ".log", "must be a string")
        return
    _fail(path, "unknown shape: expected {q, j, m, source} (v2) or {head, records, manifest_hash, source} (kit)")


def _check_kit(kit) -> None:
    path = "run.kit"
    if kit is None:
        return
    if not isinstance(kit, dict):
        _fail(path, "must be an object or null")
    _check_source(kit, path)
    if "release_commit" in kit and kit["release_commit"] is not None and not _commit_or_unknown(kit["release_commit"]):
        _fail(path + ".release_commit", "must be a 40-hex commit or \"[SOURCE]\"")
    rc = kit.get("run_commits")
    cp = path + ".run_commits"
    if rc is None:
        return
    if isinstance(rc, list):                                # §14: every run_start code_commit, in order
        for i, c in enumerate(rc):
            if not (isinstance(c, str) and _COMMIT.fullmatch(c)):
                _fail(f"{cp}[{i}]", "must be a 40-hex commit")
    elif isinstance(rc, dict):                              # v2: {q, j, m}
        if not rc or not set(rc) <= set(HEADS_PHASE):
            _fail(cp, "the v2 form holds only q, j and m")
        for k, c in rc.items():
            if not (c is None or _commit_or_unknown(c)):
                _fail(f"{cp}.{k}", "must be a 40-hex commit, \"[SOURCE]\" or null")
    else:
        _fail(cp, "must be a list of commits (kit) or an object {q, j, m} (v2)")


def _check_pooling(pooling) -> None:
    if "rule" in pooling and pooling["rule"] != "side_by_side":
        _fail("pooling.rule", "profiles are pooled side by side; this must be 'side_by_side'")
    if "within" in pooling:
        w = pooling["within"]
        if not isinstance(w, list) or tuple(w) not in POOLING_WITHIN:
            _fail("pooling.within", "must be [reference_type, prompt_set] (v2) or "
                                    "[reference_type, prompt_set_pair, state] (kit)")


def _check_chain_heads_source(run: dict) -> None:
    """``run_chain_heads_source`` is a code (<= 40 of [a-z0-9_.-]), never a sentence, and it explains a withheld
    ``run_chain_heads``: it is not written beside heads that are there."""
    path = "run.run_chain_heads_source"
    if "run_chain_heads_source" not in run:
        return
    v = run["run_chain_heads_source"]
    if v is None:
        return
    if not (isinstance(v, str) and _REASON_CODE.fullmatch(v)):
        _fail(path, "must be a code of at most 40 characters from a-z, 0-9, _ . -")
    if run.get("run_chain_heads") is not None:
        _fail(path, "is written only where run_chain_heads is null")


def _check_models(models) -> None:
    """A model is an object of strings; a field the public files do not record is null (its source then reads
    \"not_recorded_public\"). The file's own keys beyond the known ones are left alone."""
    path = "run.models"
    if models is None:
        return
    if not isinstance(models, list):
        _fail(path, "must be a list or null")
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            _fail(f"{path}[{i}]", "must be an object")
        for k in MODEL_TEXT_FIELDS:
            if k in m and m[k] is not None and not isinstance(m[k], str):
                _fail(f"{path}[{i}].{k}", "must be a string or null")


def _check_run(doc: dict) -> None:
    run = doc["run"]
    _check_run_chain_heads(run.get("run_chain_heads"), doc["state"])
    _check_chain_heads_source(run)
    _check_kit(run.get("kit"))
    _check_models(run.get("models"))


def validate_profile(doc) -> None:
    """Raise ProfileInvalid when ``doc`` does not follow PROFILE-SCHEMA.md sections 1 to 4 and 12."""
    if not isinstance(doc, dict):
        _fail("$", "a profile is a JSON object")
    schema = doc.get("schema")
    if not isinstance(schema, str):
        _fail("schema", "required string")
    m = SCHEMA_RE.match(schema)
    if not m or int(m.group(1)) != SUPPORTED_MAJOR:
        _fail("schema", f"unknown schema; this app reads orgsci-profile/v{SUPPORTED_MAJOR}")
    for k in TOP_REQUIRED:
        if k not in doc:
            _fail(k, "required")
    if doc["state"] not in FILE_STATES:
        _fail("state", f"must be one of {', '.join(FILE_STATES)}")
    pop = doc["population"]
    if not isinstance(pop, dict):
        _fail("population", "must be an object")
    if not _is_int(pop.get("n_people")):
        _fail("population.n_people", "required integer")
    if "label" in pop and not isinstance(pop["label"], str):
        _fail("population.label", "must be a string")
    if "n_total" in pop and not _is_int(pop["n_total"]):
        _fail("population.n_total", "must be an integer")
    for k in ("reference", "pooling", "run"):
        if not isinstance(doc[k], dict):
            _fail(k, "must be an object")
    if "meta_analysis" in doc["pooling"] and doc["pooling"]["meta_analysis"] is not False:
        _fail("pooling.meta_analysis", "profiles are pooled side by side; this must be false")
    _check_pooling(doc["pooling"])
    _check_run(doc)
    if "detection_limits" in doc and not isinstance(doc["detection_limits"], dict):
        _fail("detection_limits", "must be an object")
    rows = doc["rows"]
    if not isinstance(rows, list):
        _fail("rows", "must be a list")
    if len(rows) != len(ROW_ORDER):
        _fail("rows", f"expected {len(ROW_ORDER)} rows (lead, ledger, 1 to 9), found {len(rows)}")
    for i, r in enumerate(rows):
        _check_row(r, i)


def load_profile(path: Path) -> dict:
    """Read, parse and validate a profile file. Returns the document with key order as in the file."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text, object_pairs_hook=_no_dup_keys, parse_constant=_no_constants)
    except ProfileInvalid:
        raise
    except ValueError:
        raise ProfileInvalid("$", "not valid JSON") from None
    validate_profile(doc)
    return doc


def cells(doc: dict) -> list[tuple]:
    """Every cell as ``(row, name, x)`` in file order (``x`` is None when the cell has none). Raises ProfileInvalid
    when a triple repeats, which the schema forbids ("name is unique inside the row, with x")."""
    out: list[tuple] = []
    seen = set()
    for i, r in enumerate(doc["rows"]):
        for j, c in enumerate(r["cells"]):
            key = (r["row"], c["name"], c.get("x"))
            if key in seen:
                raise ProfileInvalid(f"rows[{i}].cells[{j}]", "duplicate cell (same row, name and x)")
            seen.add(key)
            out.append(key)
    return out


# ----------------------------------------------------------------------------------------------- script output
def profile_to_js(doc: dict, var: str) -> str:
    """``window.<var> = <json>;`` on one line, safe inside a ``<script>`` element and in older JS engines:
    ``</`` becomes ``<\\/``, ``<!--`` becomes ``\\u003c!--`` and U+2028 / U+2029 are escaped."""
    if not isinstance(var, str) or not _VAR_RE.fullmatch(var):
        raise ValueError("var must match ^[A-Za-z_][A-Za-z0-9_]*$")
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    body = body.replace("<!--", "\\u003c!--").replace("</", "<\\/")
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f"window.{var} = {body};"


# ----------------------------------------------------------------------------------------------- exporter
def _count(v, name: str, low: int) -> int:
    if not _is_int(v) or v < low:
        raise ValueError(f"{name} must be an int >= {low}")
    return v


def run_exporter(config_path: Path, out_dir: Path | None, profile_path: Path, *, B: int = DEFAULT_B,
                 seed: int = DEFAULT_SEED, model_info: Path | None = None, reference_type: str | None = None,
                 python: str = sys.executable) -> str:
    """Run the kit's exporter (E2 milestone M1) and return the sha256 of the profile it wrote::

        python -m second_scribe.cli export-profile --config C [--out-dir O] --profile P --B 2000 --seed 20261017
                                                   [--model-info M] [--reference-type T]

    List args, no shell, working directory = the kit root (every path is made absolute first). Exit 0 prints
    ``wrote P`` and ``sha256 <hex>``: exactly one such line is required, and it is checked against the file. Exit 2
    with ``refused: <reason>`` lines on stderr is a refusal: ``ProfileRefused`` with the reasons verbatim (prefix
    dropped). Anything else (another exit code, exit 2 with no refusal line, as argparse gives for a bad command
    line, or a missing or mismatched sha256) raises RuntimeError."""
    B = _count(B, "B", 1)
    seed = _count(seed, "seed", 0)
    absolute = lambda p: os.path.abspath(os.fspath(p))           # noqa: E731 (no symlink resolution)
    argv = [python, "-m", "second_scribe.cli", "export-profile", "--config", absolute(config_path)]
    if out_dir is not None:
        argv += ["--out-dir", absolute(out_dir)]
    argv += ["--profile", absolute(profile_path), "--B", str(B), "--seed", str(seed)]
    if model_info is not None:
        argv += ["--model-info", absolute(model_info)]
    if reference_type is not None:
        argv += ["--reference-type", str(reference_type)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, cwd=str(KIT_ROOT))
    except OSError as e:
        raise RuntimeError(f"could not start the exporter: {e.__class__.__name__}") from None
    err_lines = [ln.strip() for ln in proc.stderr.splitlines() if ln.strip()]
    tail = " | ".join(err_lines[-3:])
    if proc.returncode == 2:
        reasons = [ln[len(_REFUSED):].strip() for ln in err_lines if ln.startswith(_REFUSED)]
        if reasons:
            raise ProfileRefused(reasons)
        raise RuntimeError("the exporter exited with code 2 without a refusal" + (f": {tail}" if tail else ""))
    if proc.returncode != 0:
        raise RuntimeError(f"the exporter exited with code {proc.returncode}" + (f": {tail}" if tail else ""))
    shas = [m.group(1) for m in (_SHA_LINE.match(ln.strip()) for ln in proc.stdout.splitlines()) if m]
    if len(shas) != 1:
        raise RuntimeError("the exporter did not print exactly one 'sha256 <hex>' line")
    printed = shas[0]
    try:
        actual = hashlib.sha256(Path(absolute(profile_path)).read_bytes()).hexdigest()
    except OSError:
        raise RuntimeError("the exporter reported success but the profile file is not readable") from None
    if actual != printed:
        raise RuntimeError("the exporter's sha256 does not match the profile file it wrote")
    return printed


# ----------------------------------------------------------------------------------------------- Try it
def build_tryit_profile(run_dir: Path, config: Path | None, out: Path, *, python: str = sys.executable) -> str:
    """The Try-it synthetic profile (plan V2): the kit exporter on the recorded demo replay. Returns its sha256.

    ``config`` (default ``configs/demo.json``) must be a synthetic corpus. ``run_dir`` is either a finished run of
    it (``logs/run.manifest.json`` present: reused as it is) or absent / empty, and then the recorded replay
    (``second_scribe.cli demo --client replay``, ``demo/fixtures.json``, no model calls) is run into it first.
    The profile is written to ``out``, validated with ``load_profile`` and must be state ``synthetic``; on any
    failure RuntimeError (or ProfileRefused from the exporter) is raised."""
    config = Path(os.path.abspath(config if config is not None else DEMO_CONFIG))
    try:
        corpus = json.loads(config.read_text(encoding="utf-8")).get("corpus") or {}
    except (OSError, ValueError, AttributeError):
        raise RuntimeError("the Try-it config cannot be read") from None
    if not isinstance(corpus, dict) or corpus.get("kind") != "synthetic":
        raise RuntimeError("Try it runs on a synthetic corpus only")
    run_dir = Path(os.path.abspath(run_dir))
    if not (run_dir / "logs" / "run.manifest.json").is_file():
        if run_dir.exists() and (not run_dir.is_dir() or any(run_dir.iterdir())):
            raise RuntimeError("the run folder is neither a finished run nor empty")
        argv = [python, "-m", "second_scribe.cli", "demo", "--out", str(run_dir), "--client", "replay",
                "--config", str(config)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  stdin=subprocess.DEVNULL, cwd=str(KIT_ROOT))
        except OSError as e:
            raise RuntimeError(f"could not start the demo replay: {e.__class__.__name__}") from None
        if proc.returncode != 0:
            raise RuntimeError(f"the demo replay exited with code {proc.returncode}")
    sha = run_exporter(config, run_dir, out, python=python)
    doc = load_profile(Path(os.path.abspath(out)))
    if doc["state"] != "synthetic":
        raise RuntimeError("the Try-it profile must be state 'synthetic'")
    return sha
