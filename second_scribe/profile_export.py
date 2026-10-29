"""profile_export: the profile exporter's assembly (Task 6, MILESTONE 1; schema ``orgsci-profile/v2``). Zero model calls.

``build_profile(ctx)`` turns the per-row cell builders of Tasks 1-5 into one profile of a finished run: the lead and
ledger panels, rows 1-9, the reference block, the pooling rule and the run block. ``privacy_scan`` is the port of the
analysis build's scan (``analysis/public/build_profile.py`` 805-852, read, never imported) plus a check of the people's
own labels; ``write_profile`` refuses a profile with fewer than ``K_FLOOR`` analysed people (``n_analysed``, not the
number of files in the corpus) or a non-empty scan, and writes sorted-key JSON so that two exports of one run are the
same bytes. ``export-profile`` (``cli.py``) calls ``run_export_profile``.

Row-level fields (``rests_on``, ``qualifiers``, ``source``, ``band``) follow ``analysis/public/profile-v2.json``:
  * ``source``    the sorted, de-duplicated ``kit:<script>`` ids of the row's computed or partial cells (a not_run cell reads
                  nothing; a derived cell's ``a+b`` is split, as the analysis build's ``make_row`` does); row 4 takes
                  it from ``profile_links.row4_meta``;
  * ``rests_on``  the row's design codes (``ROW_META``, the dependency matrix) plus any code a cell of the row adds;
  * ``qualifiers`` the row's design codes (``ROW_META``);
  * ``band``      the headline cell's band (the analysis build's choice per row, ``_HEADLINE``), ``listening_added``
                  primary against lenient for the lead and row 7, min/max of the four ``ai_per_map`` cells for row 8.
A band is left out when its source cell has none (not run, or too few labellers).

State (PROFILE-SCHEMA §12): ``"synthetic"`` for a synthetic corpus, else ``"pending"``; never ``"reported"``.
``run.gate_chain``, ``run.declaration_log`` and ``run.reading3_prompt.agree`` are null: they belong to the study's lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from . import families, pipeline, prereg, report
from . import profile_base as pb
from .audit import verify as audit_verify
from .profile_coverage import ledger_cells, row1_cells, row2_cells, row3_cells, row9_floor_cells
from .profile_floors import labeller_numbers, lead_cells, row5_cells, row7_cells
from .profile_labellers import _labeller_band, build_numbers, has_judge2
from .profile_ladder import row8_cells, row9_rejudge_cells, rung1_size
from .profile_links import ROW4_LENIENT, row4_cells, row4_meta
from .profile_slant import row6_cells
from .toolcfg import PROVIDERS

SCHEMA = "orgsci-profile/v2"
K_FLOOR = 10                                   # people: no export below it (the gate's K of the study)
REFERENCE_TYPE = "participant-endorsed"        # M2 provenance (the interviewer-drawn map the participant endorsed)
UNKNOWN = "[SOURCE]"                           # a field no non-private source gives (PROFILE-SCHEMA §4)
READING3_PROMPT = "prompts/prior_only.p0.v2.txt"
ROW_ORDER = ("lead", "ledger", 1, 2, 3, 4, 5, 6, 7, 8, 9)
FORBIDDEN_KEY_FRAGMENTS = pb.FORBIDDEN_KEY_FRAGMENTS   # interview, participant, role, record_hash, entry, map_id, pair_id

# row -> (label_key, rests_on, qualifiers): copied (codes only, no number) from analysis/public/profile-v2.json. Row 4's
# come from profile_links.row4_meta.
ROW_META = {
    "lead": ("profile.lead", ["R", "AL", "LX", "IT", "P", "AS", "Q", "MO", "CN"],
             ["paired_by_map", "one_draw", "ci_excludes_draw_variance", "attested_line_one_sided", "size_beside_lead"]),
    "ledger": ("profile.ledger", ["R", "LX", "AL", "IT", "Q", "MO"],
               ["ceiling", "no_ratio", "labeller_dependent", "derived_arithmetic"]),
    1: ("profile.row1.coverage", ["R", "LX", "AL", "IT", "Q", "MO"], ["recall_is_a_band"]),
    2: ("profile.row2.size", ["R"], ["one_draw", "no_interval", "derived_arithmetic"]),
    3: ("profile.row3.support", ["R", "LX", "AL", "Q", "MO"], ["labeller_dependent"]),
    4: ("profile.row4.links", None, None),
    5: ("profile.row5.absences", ["R", "IT", "LX", "AL", "Q", "MO"], ["zero_count", "labeller_dependent"]),
    6: ("profile.row6.slant", ["R", "P", "CN", "AS", "LX", "Q", "MO", "AL", "JS", "IT"], ["partly_lexical", "descriptive"]),
    7: ("profile.row7.floors", ["R", "AL", "LX", "IT", "P", "AS", "Q", "MO", "CN"],
        ["paired_by_map", "one_draw", "ci_excludes_draw_variance"]),
    8: ("profile.row8.instruction", ["R", "Q", "LD", "LX", "IT", "MO"], ["one_draw", "descriptive", "package_contrast"]),
    9: ("profile.row9.instrument_error", ["R", "IT", "LX", "AL", "LD", "MO", "J2", "CN"], ["not_reproducible_to_the_digit"]),
}
# the cell whose band is the row's band (the analysis build's choice): row -> (cell name, x)
_HEADLINE = {"ledger": ("attested_absent_from_map", "primary"), 1: ("kept", None), 3: ("precision", "primary"),
             5: ("matcher_free_rate", "judge"), 6: ("pull", "canonical"), 9: ("attested_recall", "judge")}
_RUNGS = ("rung1", "rung2", "remedy", "placebo")
_MODEL_ROLES = (("recon", "reconstructor"), ("judge", "judge"), ("judge2", "second_judge_family"))   # (kit role, v2 name)
_MODEL_FIELDS = ("family", "quantisation", "weights", "thinking")
REMOTE_ROLES = ("recon", "judge", "judge2")      # PROFILE-SCHEMA section 14.8: the kit's own client names, no mapping
PROVIDER_MAX = 40
_HEX64 = re.compile(r"[0-9a-f]{64}")


class ProfileRefused(Exception):
    """The export is refused (fewer than K_FLOOR analysed people, a failed scan, a run log that does not verify, bad
    model info). ``reasons`` lists why; none of them carries a value from the data."""

    def __init__(self, reasons):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


SHOWN_REASONS = 3                    # village A9: a refusal prints at most 3 reasons, then "and N more"


def refusal_lines(reasons) -> list[str]:
    """The ``refused:`` lines for a list of reasons: at most ``SHOWN_REASONS``, then one "and N more" line."""
    reasons = list(reasons)
    lines = [f"refused: {r}" for r in reasons[:SHOWN_REASONS]]
    if len(reasons) > SHOWN_REASONS:
        lines.append(f"refused: and {len(reasons) - SHOWN_REASONS} more problem(s)")
    return lines


def finished_run_reason(config_path, out_dir=None) -> str | None:
    """Village A12: why there is no finished run to read (None when there is one). The run's folder is ``out_dir`` or
    the config's own; a finished run has ``aggregates/aggregates.json`` and ``logs/run.jsonl``. Raises what
    ``RunConfig.load`` raises for an unreadable config (the caller's "cannot be opened" refusal)."""
    overrides = {"out_dir": str(Path(out_dir).resolve())} if out_dir is not None else {}
    folder = Path(pipeline.RunConfig.load(Path(config_path), **overrides).out_dir)
    if not (folder / "aggregates" / "aggregates.json").is_file() or not (folder / "logs" / "run.jsonl").is_file():
        return f"no finished run found at {folder}; pass --out-dir <the run's folder>"
    return None


def k_floor_reason(n_analysed) -> str | None:
    """Village E4: the k floor in plain words (the substring "k floor" stays: `make pool` greps it)."""
    why = f"a profile needs at least {K_FLOOR} (the k floor) so no number can point to one person. Nothing was written."
    if isinstance(n_analysed, bool) or not isinstance(n_analysed, int):
        return f"the number of people this run analysed is missing or not a whole number; {why}"
    if n_analysed < K_FLOOR:
        return f"this run analysed {n_analysed} {'person' if n_analysed == 1 else 'people'}; {why}"
    return None


def file_state(cfg) -> str:
    """``"synthetic"`` for a synthetic corpus, else ``"pending"``. Never ``"reported"`` (PROFILE-SCHEMA §12)."""
    return "synthetic" if (cfg.corpus or {}).get("kind") == "synthetic" else "pending"


# ------------------------------------------------------------------ row 9
_LABELLER_CELLS = (   # (cell name, a9 number key, unit, kind)
    ("attested_recall", "node_recall", "node", "share"),
    ("attested_absent_from_map", "attested_absent", "ai_box", "count"),
    ("unanchored", "unanchored", "ai_box", "count"),
    ("matcher_free_rate", "contradiction_wm", "node", "rate"),
    ("quote_actual_drawn_as_wished", "contradiction_am", "node", "rate"),
    ("contaminated_share", "contaminated", "element", "share"),
)
_SIDEJUDGE_CELLS = (  # (name, x, unit, kind): needs the side study (second judge, label order, repeats)
    ("flip_rate", "modality", "label", "rate"), ("identical_requests", "modality", "request", "count"),
    ("flip_rate", "alignment", "label", "rate"), ("identical_requests", "alignment", "request", "count"),
    ("identical_requests", "canonical_names", "request", "count"), ("canonical_ari", "canonical_names", "label", "index"),
    ("label_order_changed_rate", "modality", "label", "rate"),
)
_LABELLER_SOURCE = "kit:a9_keyword_scope+kit:a10_band"


def _labeller_cells(ctx) -> list[dict]:
    """The six judge-1 cells of row 9, each banded over the labellers the run holds (judge, judge2 when it has a second
    judge's labels, keyword). The value is the judge's number; ``n`` its denominator."""
    labs = ("judge", "judge2", "keyword") if has_judge2(ctx) else ("judge", "keyword")
    nums = build_numbers(ctx, labs)
    cells = []
    for name, key, unit, kind in _LABELLER_CELLS:
        j = nums[key]["by"].get("judge")
        meta = dict(x="judge", unit=unit, kind=kind, source=_LABELLER_SOURCE)
        if j is None or not math.isfinite(j["point"]):
            cells.append(pb.not_run(name, [], reason_code="not_computable", **meta))
            continue
        value = j["point"]
        if kind == "count" and float(value).is_integer():
            value = int(value)
        kw = dict(meta, n=j["n"])
        if j["ci_ok"] and math.isfinite(j["ci_lo"]) and math.isfinite(j["ci_hi"]):
            kw["ci"] = (j["ci_lo"], j["ci_hi"])
        else:
            kw["note"] = "ci_undefined"
        band = _labeller_band(nums, key, labs=labs)
        cells.append(pb.cell(name, value, band=band, **kw) if band is not None
                     else pb.partial_not_run(name, value, missing=["band"], **kw))
    return cells


def _determinism_cell(ctx) -> dict:
    """identical_requests at x = run2_determinism_audit: the judge determinism audit's counts in
    ``aggregates/audits.json`` (stage entry ``determinism_judge``: n_checked requests, n_identical of them)."""
    meta = dict(x="run2_determinism_audit", unit="request", kind="count", source="kit:audits")
    counts = None
    try:
        audits = json.loads(report.out_paths(ctx.cfg)["audits"].read_text(encoding="utf-8"))
        for entries in (audits.get("stages") or {}).values():
            for e in entries:
                if e.get("name") == "determinism_judge":
                    counts = e.get("counts") or {}
    except (OSError, ValueError, AttributeError, TypeError):
        counts = None
    n, ident = (counts or {}).get("n_checked"), (counts or {}).get("n_identical")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (n, ident)) or n <= 0 or not 0 <= ident <= n:
        return pb.not_run("identical_requests", [], reason_code="no_determinism_audit", **meta)
    return pb.cell("identical_requests", ident, n=n, **meta)


def row9_cells(ctx) -> list[dict]:
    """Row 9 (instrument error) in the skeleton's order (Ruling R2): the two floors (Task 2), the rejudge cells (Task 5),
    the six labeller-band cells (Task 4's numbers), the seven side-study cells (``not_run``, needs ``sidejudge``) and
    ``identical_requests`` of the determinism audit."""
    cells = row9_floor_cells(ctx) + row9_rejudge_cells(ctx) + _labeller_cells(ctx)
    cells += [pb.not_run(name, ["sidejudge"], x=x, unit=unit, kind=kind, source="kit:sidejudge")
              for name, x, unit, kind in _SIDEJUDGE_CELLS]
    cells.append(_determinism_cell(ctx))
    return cells


# ------------------------------------------------------------------ rows: bands, the size consistency check
def _computed(c) -> bool:
    return c is not None and c.get("state") is None and isinstance(c.get("value"), (int, float))


def _find(cells, name, x=None):
    return next((c for c in cells if c["name"] == name and c.get("x") == x), None)


def row8_band(cells):
    """Row 8's band: min/max of the four ``ai_per_map`` cells (rung1, rung2, remedy, placebo), ``over: "variants"``, the
    four members named. None when any of the four is not computed."""
    four = [_find(cells, "ai_per_map", x) for x in _RUNGS]
    if not all(_computed(c) for c in four):
        return None
    vals = [c["value"] for c in four]
    return pb.band(min(vals), max(vals), "variants", members=[{"x": x, "value": v} for x, v in zip(_RUNGS, vals)])


def _listening_band(cells):
    """Primary against lenient ``listening_added`` (the lead's and row 7's band, ``over: "lenient"``)."""
    two = [_find(cells, "listening_added", x) for x in ("primary", "lenient")]
    if not all(_computed(c) for c in two):
        return None
    vals = [c["value"] for c in two]
    return pb.band(min(vals), max(vals), "lenient")


def row_band(rid, cells, meta4=None):
    if rid in ("lead", 7):
        return _listening_band(cells)
    if rid == 8:
        return row8_band(cells)
    if rid == 4:
        return (meta4 or {}).get("band")
    if rid in _HEADLINE:
        c = _find(cells, *_HEADLINE[rid])
        return c.get("band") if c is not None else None
    return None


def mark_unjudged_nodes(cells: list[dict]) -> list[dict]:
    """Row 2's consistency check (Task 2, minor 4): ``ai_per_map`` is judged boxes per map, ``ai_nodes_per_map`` all nodes
    per map. When they differ, some nodes were not judged: the ``ai_per_map`` cell gets the note ``unjudged_nodes``.
    Never stops the export. Edits ``cells`` in place and returns it."""
    judged, nodes = _find(cells, "ai_per_map"), _find(cells, "ai_nodes_per_map")
    if _computed(judged) and _computed(nodes) and abs(judged["value"] - nodes["value"]) > 5e-4 and "note" not in judged:
        judged["note"] = "unjudged_nodes"
    return cells


def _row(rid, cells, n, label, meta4=None) -> dict:
    label_key, rests_on, qualifiers = ROW_META[rid]
    if rid == 4:
        rests_on, qualifiers = meta4["rests_on"], meta4["qualifiers"]
    rests_on = list(rests_on) + [code for c in cells for code in c.get("rests_on", ()) if code not in rests_on]
    seen = []
    for code in rests_on:
        if code not in seen:
            seen.append(code)
    ran = [c for c in cells if c.get("state") != "not_run"]          # a not_run cell reads nothing
    if rid == 4:
        source = meta4["source"] if ran else []
    else:
        source = sorted({s for c in ran for s in c.get("source", "").split("+") if s})
    row = {"row": rid, "label_key": label_key, "population": label, "n_people": n, "cells": cells}
    band = row_band(rid, cells, meta4)
    if band is not None:
        row["band"] = copy.deepcopy(band)
    row.update(rests_on=seen, source=list(source), qualifiers=list(qualifiers))
    return row


# ------------------------------------------------------------------ the run block
def _log_records(log_path: Path) -> list[dict]:
    """The ``run_start`` and ``run_fork`` records of the log, in order (the others are not parsed)."""
    out = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            if '"run_start"' in line or prereg.FORK_RECORD in line:
                rec = json.loads(line)
                if rec.get("record_type") in ("run_start", prereg.FORK_RECORD):
                    out.append(rec)
    return out


def _prompt_sets(records) -> tuple:
    """(Q-phase, J/M-phase) prompt-set hashes: of the session that ran a Q stage (or the fork's declaration, which stands
    for the Q phase) and of the first session that ran a J stage."""
    q_stages, j_stages = set(pipeline.STAGE_GROUPS["Q"]), set(pipeline.STAGE_GROUPS["J"])
    q = j = None
    for p in prereg.session_starts(records):
        stages, h = set(p.get("stages") or ()), p.get("prompt_set_sha256")
        if not isinstance(h, str):
            continue
        if q is None and (not stages or stages & q_stages):
            q = h
        if j is None and stages & j_stages:
            j = h
    return q or UNKNOWN, j or UNKNOWN


def _models(records, model_info) -> list[dict]:
    ids = {}
    for rec in records:
        if rec.get("record_type") == "run_start":
            ids.update({k: v for k, v in ((rec.get("payload") or {}).get("models") or {}).items() if isinstance(v, str)})
    info = model_info or {}
    out = []
    for kit_role, name in _MODEL_ROLES:
        if kit_role not in ids:
            continue
        given = {**(info.get(kit_role) or {}), **(info.get(name) or {})}
        m = {"role": name, "model_id": ids[kit_role], "family": families.model_family(ids[kit_role]) or UNKNOWN,
             "quantisation": UNKNOWN, "weights": UNKNOWN}
        m.update({k: given[k] for k in _MODEL_FIELDS if k in given})
        m["source"] = "run_start of the run log; quantisation and weights from --model-info, else [SOURCE]"
        out.append(m)
    return out


MODEL_INFO_CODE = re.compile(r"(?=.{1,40}$)[A-Za-z0-9._:+-]+(/[A-Za-z0-9._:+-]+)?")   # R32: codes, never text; one "/" for org/model ids


def check_model_info(model_info):
    """The ``--model-info`` JSON: {role: {family?, quantisation?, weights?, thinking?}} with role a kit role (recon, judge,
    judge2) or a profile name (reconstructor, judge, second_judge_family) and text values."""
    if model_info is None:
        return None
    ok_roles = {r for pair in _MODEL_ROLES for r in pair}
    if not isinstance(model_info, dict):
        raise ProfileRefused("model info must be a JSON object {role: {quantisation, weights, ...}}")
    for role, d in model_info.items():
        if role not in ok_roles or not isinstance(d, dict):
            raise ProfileRefused("model info: unknown role or not an object")
        for k, v in d.items():
            if k not in _MODEL_FIELDS or not isinstance(v, str) or not MODEL_INFO_CODE.fullmatch(v):
                raise ProfileRefused(f"model info: a field is not one of {_MODEL_FIELDS}, or its value is not a code of "
                                     "at most 40 characters of A-Z, a-z, 0-9, '.', '_', ':', '+' and '-'")
    return model_info


def remote_endpoints(cfg) -> list[dict]:
    """``run.remote_endpoints`` (PROFILE-SCHEMA section 14.8): ``[{role, provider, declaration_sha256}]`` from the config's
    ``tool.remote_endpoints``, sorted by role then provider; ``[]`` when the config declares none (the caller then leaves
    the field out). The host and every other key of an entry are dropped. Checked again here although ``toolcfg`` checks
    at load, because a config can bypass it: an unknown role or provider, or a declaration that is not 64 lowercase hex,
    raises ``ProfileRefused``. The messages name the field, never the value."""
    tool = getattr(cfg, "tool", None)
    eps = tool.get("remote_endpoints") if isinstance(tool, dict) else None
    if not eps:
        return []
    if not isinstance(eps, list):
        raise ProfileRefused("tool.remote_endpoints must be a list")
    out, problems = [], []
    for i, ep in enumerate(eps):
        where = f"tool.remote_endpoints[{i}]"
        if not isinstance(ep, dict):
            problems.append(f"{where} must be an object")
            continue
        role, provider, decl = ep.get("role"), ep.get("provider"), ep.get("declaration_sha256")
        if not isinstance(role, str) or role not in REMOTE_ROLES:
            problems.append(f"{where}.role is not one of {REMOTE_ROLES}")
        if not isinstance(provider, str) or provider not in PROVIDERS or len(provider) > PROVIDER_MAX:
            problems.append(f"{where}.provider is not one of {PROVIDERS}")
        if not isinstance(decl, str) or not _HEX64.fullmatch(decl):
            problems.append(f"{where}.declaration_sha256 must be 64 lowercase hex characters")
        if not problems:
            out.append({"role": role, "provider": provider, "declaration_sha256": decl})
    if problems:
        raise ProfileRefused(problems)
    return sorted(out, key=lambda e: (e["role"], e["provider"], e["declaration_sha256"]))


REFERENCE_TYPE_RE = re.compile(r"[a-z0-9_-]{1,40}")


def check_reference_type(reference_type):
    """Final review A4: the reference type is a short code (at most 40 characters of [a-z0-9_-]). PROFILE-SCHEMA
    sections 5, 12 and 14 name one type, ``participant-endorsed``, and section 5 lists a lab of another type separately,
    so other codes are allowed but free text is not: it becomes a pool grouping key printed in pool.md."""
    if not isinstance(reference_type, str) or not REFERENCE_TYPE_RE.fullmatch(reference_type):
        raise ProfileRefused("--reference-type must be a code of at most 40 characters from a-z, 0-9, _ and - "
                             f"(for example {REFERENCE_TYPE})")
    return reference_type


def _settings(ctx) -> dict:
    cfg = ctx.cfg
    po = cfg.prior_only or {}
    # reading 3: the config's reading3 block (one map per role) when present, else the prior-only set it is taken from
    r3 = ({"temperature": cfg.reading3["temperature"], "samples": cfg.reading3["samples"]} if cfg.reading3 is not None
          else {"temperature": po.get("temperature", 0.7), "samples": po.get("samples", 0)})
    prim, len_ = pb.SETTINGS["primary"], pb.SETTINGS["lenient"]

    def m(s):
        return {"lexical_threshold": s.lex_threshold, "judge_label": s.judge_accept}
    return {
        "matcher_primary": m(prim), "matcher_lenient": m(len_), "matcher_row4_lenient": m(ROW4_LENIENT),
        "match_settings": {k: m(s) for k, s in pb.SETTINGS.items()},
        "reconstructor_temperature": cfg.recon_specs()[0].temperature,
        "reading3_temperature": float(r3["temperature"]), "reading3_draws": int(r3["samples"]),
        "bootstrap_resamples": ctx.B, "bootstrap_seed": ctx.seed, "interval_method": "cluster_bootstrap",
        "gate_k_people": K_FLOOR,
        "source": "RunConfig; bootstrap B and seed as given to the export",
    }


def run_code_sha256(records) -> str | None:
    """Village E6: ``run.kit.code_sha256``, the ``code_sha256`` (``codehash``, the kit's code named without git) that every
    ``run_start`` of the log recorded. Ruling: null when the run_starts disagree (the code changed between sessions) or
    when none recorded a 64-hex value, so the field is always one hash or null; ``run_commits`` keeps the per-session
    commits. A modified kit therefore shows a hash that differs from the release's."""
    codes = [(r.get("payload") or {}).get("code_sha256") for r in records if r.get("record_type") == "run_start"]
    if not codes or not all(isinstance(c, str) and _HEX64.fullmatch(c) for c in codes) or len(set(codes)) != 1:
        return None
    return codes[0]


def build_run_block(ctx, model_info=None, *, withhold_chain_heads: bool = False) -> dict:
    """The ``run`` block (chain heads and declared settings, never records). Refuses (``ProfileRefused``) when the run log
    does not verify against its manifest. ``withhold_chain_heads`` (Ruling R25): ``run_chain_heads`` is null and
    ``run_chain_heads_source`` is ``"available_from_authors"``; the log is verified all the same."""
    log_path = Path(ctx.out_dir) / "logs" / "run.jsonl"
    try:
        ok, detail = audit_verify.verify_run(log_path)
        records = _log_records(log_path) if ok else []
    except (OSError, ValueError) as exc:
        raise ProfileRefused(f"the run log cannot be read or does not verify ({type(exc).__name__})") from None
    if not ok:
        raise ProfileRefused(f"the run log does not verify against its manifest ({detail.get('reason')}): the run is not exported")
    q_hash, j_hash = _prompt_sets(records)
    commits = [(r.get("payload") or {}).get("code_commit") for r in records if r.get("record_type") == "run_start"]
    code_sha256 = run_code_sha256(records)                                       # village E6
    sha = UNKNOWN
    try:
        manifest = json.loads((pipeline.PROMPTS_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
        listed = (manifest.get("files") or {}).get(Path(READING3_PROMPT).name)
        if isinstance(listed, str) and manifest.get("set_sha256") == q_hash:       # the manifest on disk is the set that ran
            sha = listed
    except (OSError, ValueError):
        pass
    block = {
        "gate_chain": None,
        "declaration_log": None,
        "run_chain_heads": {"head": detail["head_hash"], "records": detail["records"], "manifest_hash": detail.get("manifest_hash"),
                            "source": "logs/run.manifest.json, checked by audit.verify.verify_run"},
        "kit": {"release_commit": prereg.git_head() or UNKNOWN, "run_commits": [c for c in commits if isinstance(c, str)],
                "code_sha256": code_sha256,
                "source": "release_commit = HEAD of the kit now; run_commits = code_commit of every run_start; "
                          "code_sha256 = the code hash every run_start logged (null when they differ or none is logged)"},
        "prompt_set": {"q_phase_sha256": q_hash, "j_m_phase_sha256": j_hash},          # run_start of the Q and J sessions
        "reading3_prompt": {"path": READING3_PROMPT, "sha256": sha, "agree": None,
                            "source": "prompts/MANIFEST.json, when its set hash is the Q session's"},
        "models": _models(records, check_model_info(model_info)),
        "settings": _settings(ctx),
    }
    remote = remote_endpoints(ctx.cfg)                      # section 14.8: absent, never an empty list
    if remote:
        block["remote_endpoints"] = remote
    if withhold_chain_heads:
        block["run_chain_heads"] = None
        block["run_chain_heads_source"] = "available_from_authors"
    return block


# ------------------------------------------------------------------ assembly
def _val(cells, name, x=None):
    c = _find(cells, name, x)
    return c["value"] if c is not None else None


def _reading3_note(cfg) -> str:
    """`` <reading-3 spec key>`` for the map-size source note when the config has a reading3 block, else ``""``."""
    return f" {cfg.reading3_specs()[0].key()}" if cfg.reading3 is not None else ""


def build_profile(ctx, model_info: dict | None = None, *, reference_type: str = REFERENCE_TYPE,
                  withhold_chain_heads: bool = False) -> dict:
    """The profile of the run ``ctx`` opens (schema ``orgsci-profile/v2``). It does not apply the k floor (``write_profile``
    does), but it refuses a profile that carries a pid, interviewer or role label, and a run log that does not verify."""
    check_reference_type(reference_type)
    n, n_total = len(ctx.analysed), len(ctx.run.people)
    label = f"{n} of {n_total}"
    labellers = labeller_numbers(ctx)
    r4 = row4_cells(ctx)
    cells = {"lead": lead_cells(ctx), "ledger": ledger_cells(ctx, labellers), 1: row1_cells(ctx),
             2: mark_unjudged_nodes(row2_cells(ctx, rung1_size(ctx))), 3: row3_cells(ctx, labellers), 4: r4,
             5: row5_cells(ctx), 6: row6_cells(ctx), 7: row7_cells(ctx), 8: row8_cells(ctx), 9: row9_cells(ctx)}
    meta4 = row4_meta(r4)
    rows = [_row(rid, cells[rid], n, label, meta4) for rid in ROW_ORDER]
    lead, row2 = cells["lead"], cells[2]
    profile = {
        "schema": SCHEMA,
        "state": file_state(ctx.cfg),
        "population": {"label": label, "n_people": n, "n_total": n_total},
        "reference": {"type": reference_type,
                      "map_size": {"ai_boxes": _val(row2, "ai_boxes"), "endorsed_boxes": _val(row2, "endorsed_boxes"),
                                   "ai_per_map": _val(row2, "ai_per_map"), "endorsed_per_map": _val(row2, "endorsed_per_map"),
                                   "ai_per_endorsed": _val(row2, "ai_per_endorsed"),
                                   "reading3_map_size": {"nodes_per_map": _val(lead, "no_interview_nodes_per_map"),
                                                         "edges_per_map": _val(lead, "no_interview_edges_per_map"),
                                                         "over_ai_nodes": _val(lead, "no_interview_over_ai_nodes"),
                                                         "note": "pooled_only", "source": "kit:r12_prior_size"},
                                   "source": "kit:a1_recall_matchers+kit:a7_additions_source; reading3_map_size from "
                                             f"work/prior_only{_reading3_note(ctx.cfg)} (kit:r12_prior_size)"}},
        "pooling": {"rule": "side_by_side", "within": ["reference_type", "prompt_set_pair", "state"], "meta_analysis": False},
        "rows": rows,
        "run": build_run_block(ctx, model_info, withhold_chain_heads=withhold_chain_heads),
    }
    problems = privacy_scan(profile, forbidden=forbidden_tokens(ctx), k_floor=False)
    if problems:
        raise ProfileRefused(problems)
    return profile


# ------------------------------------------------------------------ privacy
def forbidden_tokens(ctx) -> dict:
    """What names a person in this run, for the value check: {"pids": participant ids (6 characters or more matched
    as substrings, shorter ones as whole [A-Za-z0-9] tokens: Ruling R24), "codes": interviewer codes and role names (matched
    as whole [A-Za-z0-9] tokens, so ``owner_map`` hits and ``JS`` does not hit ``JSON``)}; strings of 2 characters or more.
    A bounded token of 3 characters or fewer in upper case matches case-sensitively (Ruling R31)."""
    pids, codes = set(), set()
    for p in ctx.run.people:
        pids.add(p.pid)
        codes.update(t for t in (p.role, p.interviewer) if isinstance(t, str))
    codes.update((ctx.cfg.corpus or {}).get("roles", {}).values())
    pids.update((ctx.cfg.corpus or {}).get("roles", {}))
    return {"pids": {t for t in pids if isinstance(t, str) and len(t) >= 2},
            "codes": {t for t in codes if isinstance(t, str) and len(t) >= 2}}


# Ruling R23: the kit's own closed vocabulary is not value-scanned (a role called "lead" or "judge", an interviewer coded
# "JS", would collide with row ids, x labels and rests_on codes). Every other string value and every dict key is scanned.
# label_key (profile.lead, profile.row1.coverage...) is the same kind of kit constant. ``reason`` and ``formula`` are not
# in the list (Ruling R20, Task 6I): neither key can exist in a lab export, and ``lab_string_problems`` refuses both.
CLOSED_VOCABULARY = frozenset({"row", "name", "x", "unit", "kind", "rests_on", "qualifiers", "needs", "missing", "reason_code",
                               "note", "formula_code", "derived", "source", "over", "label_key"})


def _key_label(k, hide=None) -> str:
    """``profile_base.key_label``: a key in a message only when it is a plain field name, else ``<key>`` (A2)."""
    return pb.key_label(k, hide)


def _nodes(o, path="", owner=None, hide=None):
    """(path, owner key, key-or-None, value): every leaf (owner = the dict key the leaf hangs under, also through lists) and
    every dict key (key set, value None). The path names positions, never content: keys pass through ``_key_label``."""
    if isinstance(o, dict):
        for k, v in o.items():
            step = f"{path}/{_key_label(k, hide)}"
            yield step, owner, k, None
            yield from _nodes(v, step, k, hide)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _nodes(v, f"{path}[{i}]", owner, hide)
    else:
        yield path, owner, None, o


def _row_label(r, i: int) -> str:
    return pb.row_label(r, i)


def _cell_label(c, j: int, hide=None) -> str:
    """A cell in a message: its position ``cell[j]``, never its name (a hand-edited file may hold anything there)."""
    return f"cell[{j}]"


PID_SUBSTRING_MIN = 6                       # Ruling R24: a pid this long is matched as a substring, a shorter one as a token
INITIALS_MAX = 3                            # Ruling R31: an all-upper-case token this short is matched case-sensitively


def _forbidden_patterns(forbidden, *, fold_initials: bool = False):
    """(pid pattern, code pattern, initials pattern), each None when empty. A plain iterable of strings counts as codes.
    Ruling R24: a pid of ``PID_SUBSTRING_MIN`` characters or more is matched anywhere inside a string, ignoring case; a
    shorter one (initials, a short code, which as a substring would hit ordinary words such as "absent") needs
    [A-Za-z0-9] boundaries, like the interviewer and role codes (so ``AB_map`` hits and ``absent`` does not). Ruling R31:
    a bounded token of at most ``INITIALS_MAX`` characters that is all upper case (interviewer initials such as ``OF``,
    ``BY``, ``IS``, ``IN``) matches case-sensitively, so the kit's own words ("12 of 12", "side_by_side", the
    detection-limits sentence) do not refuse a real lab; every other bounded token matches ignoring case."""
    if isinstance(forbidden, dict):
        pids, codes = forbidden.get("pids") or (), forbidden.get("codes") or ()
    else:
        pids, codes = (), forbidden or ()
    pids = {t for t in pids if t}
    long_pids = {t for t in pids if len(t) >= PID_SUBSTRING_MIN}
    bounded_all = {t for t in set(codes) | (pids - long_pids) if t}
    initials = set() if fold_initials else {t for t in bounded_all if len(t) <= INITIALS_MAX and t.isupper()}

    def alt(items, fmt, flags):
        items = sorted({t for t in items if t}, key=len, reverse=True)
        return re.compile("|".join(fmt(re.escape(t)) for t in items), flags) if items else None

    def bounded(t):
        return r"(?<![A-Za-z0-9])" + t + r"(?![A-Za-z0-9])"
    return (alt(long_pids, lambda t: t, re.I), alt(bounded_all - initials, bounded, re.I), alt(initials, bounded, 0))


# Final review A3 / fix round 2 N2: the run block is value-scanned too, except the fields the kit writes from the run
# log, the manifest and git (hashes, commits, model ids, the kit's role names, fixed method names). What is scanned:
# every --model-info value (family, quantisation, weights, thinking; ``family`` too, since --model-info can overwrite
# the kit-derived one) and any field a hand edit added, all ignoring case (Ruling R32). Run keys are kit setting names
# and are not scanned (a role called "judge" would collide with ``judge_only_same``).
LAB_ENTERED_PATHS = frozenset({"/reference/type"})           # R32: lab-typed fields outside run (--reference-type)
RUN_KIT_FIELDS = frozenset({"role", "model_id", "head", "manifest_hash", "release_commit", "run_commits", "code_sha256",
                            "q_phase_sha256", "j_m_phase_sha256", "sha256", "path", "interval_method", "judge_label",
                            "run_chain_heads_source"})


def value_scan(profile: dict, forbidden) -> list[str]:
    """Strings that name a person: participant ids anywhere inside a string or key, interviewer codes and role names as
    whole tokens (Rulings R23, R24, R31). Outside ``run`` the closed vocabulary (``CLOSED_VOCABULARY``) is skipped as a
    value and every key is scanned; inside ``run`` the values outside ``RUN_KIT_FIELDS`` and the closed vocabulary are
    scanned (A3)."""
    pats = [p for p in _forbidden_patterns(forbidden) if p is not None]
    if not pats:
        return []
    # Ruling R32: R31's case-sensitive initials hold only for the kit's own text; what a lab types (reference.type, the
    # --model-info values, any other free run field) is matched ignoring case for every token
    folded = [p for p in _forbidden_patterns(forbidden, fold_initials=True) if p is not None]
    out = []
    hide = lambda k: isinstance(k, str) and any(p.search(k) for p in folded)        # noqa: E731
    msg = "forbidden value (a participant, interviewer or role label) at {}"
    for path, owner, key, v in _nodes({k: x for k, x in profile.items() if k != "run"}, hide=hide):
        text = str(key) if key is not None else (v if isinstance(v, str) and owner not in CLOSED_VOCABULARY else None)
        use = folded if key is None and path in LAB_ENTERED_PATHS else pats
        if text is not None and any(p.search(text) for p in use):
            out.append(msg.format(path))
    run = profile.get("run")
    if isinstance(run, (dict, list)):
        for path, owner, key, v in _nodes(run, "/run", hide=hide):
            if key is None and isinstance(v, str) and owner not in CLOSED_VOCABULARY | RUN_KIT_FIELDS \
                    and any(p.search(v) for p in folded):
                out.append(msg.format(path))
    return out


# Cell fields that hold numbers only (or null): a string here is a sentence or a label, never a code (final review A5).
NUMBER_FIELDS = ("value", "ci_lo", "ci_hi", "ci_level", "p", "p_lt", "p_holm", "p_holm_lt", "n")


def _strings_in(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings_in(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _strings_in(v)


def privacy_scan(profile: dict, forbidden=(), *, k_floor: bool = True) -> list[str]:
    """Problems that stop an export (empty = clean); port of ``build_profile.py`` 805-852. Outside ``run``: no key with a
    forbidden fragment (interview, participant, role, record_hash, entry, map_id, pair_id), no 64-hex string, no map-level
    cell (unit ``map``) whose ``n`` differs from the population (``build_profile.py`` 927), no x label naming an
    interviewer, role or person, every ``n_people`` (population, rows, and any cell that carries one) at least K people
    (``k_floor``), no non-finite number, no cell number field (``NUMBER_FIELDS``) holding anything but a number or null,
    no cell string with white space (cells carry codes, never sentences); the string rules of PROFILE-SCHEMA section 13
    (``profile_base.lab_string_problems``); and, given ``forbidden`` (``forbidden_tokens``), the value check
    (``value_scan``). ``k_floor=False`` leaves out the K checks (``build_profile`` builds a small run's profile;
    ``write_profile`` refuses it). Messages name a position, never the offending key or value (final review A2)."""
    problems = []
    body = {k: v for k, v in profile.items() if k != "run"}
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        return ["rows is not a list"]
    n_people = (body.get("population") or {}).get("n_people") if isinstance(body.get("population"), dict) else None
    if k_floor:
        for path, owner, k, v in _nodes(body):
            if k is None and owner == "n_people" and (isinstance(v, bool) or not isinstance(v, int) or v < K_FLOOR):
                problems.append(f"n_people below K at {path}")
        for i, r in enumerate(rows):
            if isinstance(r, dict) and "n_people" not in r:
                problems.append(f"row {_row_label(r, i)} n_people below K (absent)")
    for path, _owner, k, v in _nodes(body):
        if k is not None and any(f in str(k).lower() for f in FORBIDDEN_KEY_FRAGMENTS):
            problems.append(f"forbidden key {path}")
        if isinstance(v, float) and not math.isfinite(v):
            problems.append(f"non-finite number at {path}")
    if re.search(r"\b[0-9a-f]{64}\b", json.dumps(body)):
        problems.append("64-hex string outside the run block")
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            problems.append(f"row [{i}] is not an object")
            continue
        for j, c in enumerate(r.get("cells") or []):
            if not isinstance(c, dict):
                problems.append(f"row {_row_label(r, i)} cell[{j}] is not an object")
                continue
            where = f"row {_row_label(r, i)} {_cell_label(c, j)}"
            if c.get("unit") == "map" and c.get("n") not in (None, n_people):
                problems.append(f"map-level cell with n != population: {where}")
            if re.search(r"(interviewer|role|pc\d|sp\d)", str(c.get("x") or ""), re.I):
                problems.append(f"suspicious x label in {where}")
            for f in NUMBER_FIELDS:
                v = c.get(f)
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    problems.append(f"{where}: {f} is not a number")
            if any(re.search(r"\s", t) for t in _strings_in(c)):
                problems.append(f"{where}: a string with white space (cells carry codes, not sentences)")
    problems += pb.lab_string_problems(body)          # Ruling R20: a profile that breaks section 13 is not exported
    if forbidden:
        problems += value_scan(profile, forbidden)
        pats = [p for p in _forbidden_patterns(forbidden, fold_initials=True) if p is not None]
        for p in pats:                                   # backstop (A2): no message carries a forbidden token
            problems = [p.sub("<redacted>", m) for m in problems]
    return problems


def write_profile(profile: dict, path, *, forbidden=None) -> str:
    """Write the profile (sorted keys, 1-space indent, ASCII) and return its sha256. Raises ``ProfileRefused``, and writes
    nothing, when ``n_analysed`` (``population.n_people``) is below K_FLOOR or the privacy scan finds anything. Given
    ``forbidden`` (``forbidden_tokens(ctx)``) the scan includes the value check; the export command always passes it."""
    reasons = privacy_scan(profile, forbidden=forbidden or ())
    why = k_floor_reason((profile.get("population") or {}).get("n_people"))
    if why:
        reasons.insert(0, why)
    if reasons:
        raise ProfileRefused(reasons)
    try:
        text = json.dumps(profile, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    except ValueError:
        raise ProfileRefused("the profile holds a non-finite number") from None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    report.write_text_atomic(path, text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ the command
def run_export_profile(a) -> int:
    """``export-profile``: exit 0 and the sha256 on success, exit 2 with the reasons on refusal."""
    import sys
    try:
        info = None
        if a.model_info:
            try:
                info = json.loads(Path(a.model_info).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ProfileRefused("--model-info cannot be read as JSON") from None
        check_model_info(info)
        check_reference_type(a.reference_type)
        try:                                             # final review A8: an unreadable config is a refusal, exit 2
            if (why := finished_run_reason(a.config, a.out_dir)) is not None:      # village A12: before the k floor
                raise ProfileRefused(why)
            ctx = pb.open_context(Path(a.config), Path(a.out_dir) if a.out_dir else None, B=a.B, seed=a.seed)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProfileRefused(f"the run config cannot be opened ({type(exc).__name__})") from None
        if (why := k_floor_reason(len(ctx.analysed))) is not None:       # before the heavy computation
            raise ProfileRefused(why)
        digest = write_profile(build_profile(ctx, info, reference_type=a.reference_type,
                                             withhold_chain_heads=bool(getattr(a, "withhold_chain_heads", False))), a.profile,
                               forbidden=forbidden_tokens(ctx))
    except ProfileRefused as exc:
        for line in refusal_lines(exc.reasons):
            print(line, file=sys.stderr)
        return 2
    print(f"wrote {a.profile}")
    print(f"sha256 {digest}")
    return 0
