"""pool: lab profiles side by side, never combined (PROFILE-SCHEMA.md section 5 and 12; ruling 4, 18 Oct).

Profiles (schema ``orgsci-profile/v2``) are grouped by (reference type, Q-phase prompt hash, J/M-phase prompt hash,
file state). A group with two or more labs becomes one table, one column per lab; a lab that is alone in its group is
listed under ``separate``. Cells are matched across labs on (row, cell name, x) and copied as they came, with their
state, needs and reason codes. Nothing is computed across labs: no sum, mean, interval, test or weight. No model
call, no network; the only file read is the kit's strings file (through the privacy scan).

Every input is a file from another lab, so each one passes the exporter's own privacy scan before anything is pooled
(``profile_export.privacy_scan`` with the k floor: every ``n_people`` at least 10, no forbidden key, no 64-hex string, no
sentence or text in a cell, the section 13 string rules). The pooler has no lab's participant list, so the value check
by name does not run here; each lab's export ran it on its own data.
"""
from __future__ import annotations

import json
import re

K_FLOOR = 10
HEX64 = re.compile(r"[0-9a-f]{64}")
FILE_STATES = frozenset({"synthetic", "pending", "reported"})        # PROFILE-SCHEMA sections 1 and 12

# Fix round 2, N1: everything pool copies into pool.json / pool.md from a lab's file must have a closed form (the pooler
# cannot know another lab's participant list, so only the shape can stop a name or a sentence). Row and cell fields are
# the ones the kit exporter writes (profile_export / profile_base.cell); the code lists are PROFILE-SCHEMA sections 6,
# 7 and 12.
from .profile_base import KIT_ROW_IDS  # noqa: E402  (lead, ledger, 1-9)
LABEL_KEY = re.compile(r"[a-z0-9_.]{1,40}")
ROW_FIELDS = frozenset({"row", "label_key", "population", "n_people", "cells", "band", "rests_on", "source", "qualifiers"})
CELL_FIELDS = frozenset({"name", "x", "n", "unit", "kind", "value", "ci_lo", "ci_hi", "ci_level", "p", "p_lt", "p_holm",
                         "p_holm_lt", "band", "state", "needs", "reason_code", "missing", "note", "qualifiers", "rests_on",
                         "source", "derived", "formula_code"})
BAND_FIELDS = frozenset({"lo", "hi", "over", "members", "narrow", "subbands"})
# N5: the cell `derived` values the exporter writes (True, or one of these codes)
DERIVED = frozenset({"ledger_band", "per_map_mean", "ratio_ai_per_endorsed", "ratio_of_cleared_counts",
                     "sum_of_cleared_counts", "net"})


def _is_num(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
CELL_STATES = frozenset({"not_run", "partial_not_run"})
NEEDS = frozenset({"J2", "sidejudge", "ladder", "judge_script"})
RESTS_ON = frozenset({"R", "P", "JS", "IT", "AL", "AS", "CN", "MO", "LD", "J2", "Q", "LX"})
QUALIFIERS = frozenset({
    "ceiling", "not_bounded_below", "descriptive", "one_draw", "package_contrast", "ci_excludes_draw_variance",
    "paired_by_map", "attested_line_one_sided", "derived_arithmetic", "no_interval", "zero_count", "labeller_dependent",
    "partly_lexical", "no_structural_claim", "seam_withheld_k", "recall_is_a_band", "no_ratio",
    "not_reproducible_to_the_digit", "size_beside_lead", "pooled_only", "not_a_test", "floor_not_estimate"})
LOWER_CODE = re.compile(r"[a-z0-9][a-z0-9_.:+-]{0,39}")             # name, x, unit, kind, band over / member x
SOURCE_CODE = re.compile(r"[a-z0-9][a-z0-9_.:+-]{0,199}")            # kit:<name>(+kit:<name>)*
CAVEAT = "Side by side: each column is one lab's own cell. No value is combined across labs."


SCHEMA_V2 = re.compile(r"orgsci-profile/v2(\.\d+)?")              # PROFILE-SCHEMA: readers take v2 and v2.<minor>
_SHOWABLE_SCHEMA = re.compile(r"[A-Za-z0-9._/:+-]{1,60}")          # a schema id is echoed only in this closed form


# the study's cell sources: "gate chain entry 013" (PROFILE-SCHEMA) and the older "cleared/013+cleared/020" (fix round 1, S1)
GATE_CHAIN_SOURCE = re.compile(r"gate chain entr(y|ies) \d{3}(\+\d{3})*|cleared/\d{3}(\+cleared/\d{3})*")
SHOWN_PROBLEMS = 3                                                 # village A9: a refusal lists at most 3 problems


def shown_problems(problems: list[str]) -> str:
    """At most ``SHOWN_PROBLEMS`` problems joined by "; ", then "and N more" (village A9)."""
    more = len(problems) - SHOWN_PROBLEMS
    return "; ".join(problems[:SHOWN_PROBLEMS]) + (f"; and {more} more" if more > 0 else "")


def is_study_profile(profile) -> bool:
    """Village A9 (Ruling R1, option 2): the study's published profile, which pool refuses early. It is recognised by a
    ``source`` (anywhere outside ``run``) naming gate-chain entries ("gate chain entry 013", "gate chain entries
    001+018", or the older "cleared/013+cleared/020"), or by a non-null ``run.gate_chain`` (the kit's exporter writes null there and "kit:..." sources)."""
    if not isinstance(profile, dict):
        return False
    run = profile.get("run")
    if isinstance(run, dict) and run.get("gate_chain") is not None:
        return True

    def walk(o, key=None):
        if isinstance(o, dict):
            return any(walk(v, k) for k, v in o.items())
        if isinstance(o, list):
            return any(walk(v, key) for v in o)
        return key == "source" and isinstance(o, str) and GATE_CHAIN_SOURCE.search(o) is not None
    return walk({k: v for k, v in profile.items() if k != "run"})


def study_refusal(name: str) -> str:
    """The refusal text for the study's published profile (village A9; ``name`` is the file as given)."""
    return (f"{name} is the study's published profile (the app's reading copy); it cannot be pooled. "
            "Pool the file written by export-profile from a finished run instead.")


def _schema(profile, lab: str) -> None:
    """Village A8: refuse a profile whose ``schema`` is not ``orgsci-profile/v2`` or ``v2.<minor>``. The value is
    echoed only when it has the closed form of a schema id (a message never carries free text from the file)."""
    schema = profile.get("schema") if isinstance(profile, dict) else None
    if schema is None:
        raise ValueError(f"{lab} has no schema; this kit reads orgsci-profile/v2 only")
    if isinstance(schema, str) and SCHEMA_V2.fullmatch(schema):
        return
    shown = repr(schema) if isinstance(schema, str) and _SHOWABLE_SCHEMA.fullmatch(schema) else "<not a schema id>"
    raise ValueError(f"{lab}: schema {shown} is not orgsci-profile/v2; this kit reads v2 only")


def _key(profile: dict, lab: str) -> dict:
    """The grouping key. Each of its four fields must be a non-empty string; a null or empty one is a malformed
    profile and is refused (final review A6), so pool_markdown never meets a null hash."""
    try:
        key = {"reference_type": profile["reference"]["type"],
               "q_phase_sha256": profile["run"]["prompt_set"]["q_phase_sha256"],
               "j_m_phase_sha256": profile["run"]["prompt_set"]["j_m_phase_sha256"],
               "state": profile["state"]}
    except (KeyError, TypeError):
        raise ValueError(f"{lab}: profile lacks reference.type, run.prompt_set or state (all are required)") from None
    for field, v in key.items():
        if not isinstance(v, str) or not v:
            raise ValueError(f"{lab}: {field} is null, empty or not text; a pool refuses a malformed profile")
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", key["reference_type"]):         # A4: a code, as the exporter writes it
        raise ValueError(f"{lab}: reference.type is not a code of at most 40 characters from a-z, 0-9, _ and -")
    for field in ("q_phase_sha256", "j_m_phase_sha256"):                       # fix round 2, N1: closed forms only
        if not HEX64.fullmatch(key[field]):
            raise ValueError(f"{lab}: run.prompt_set.{field} is not 64 lower-case hex characters")
    if key["state"] not in FILE_STATES:
        raise ValueError(f"{lab}: state is not one of {', '.join(sorted(FILE_STATES))}")
    return key


def _n_analysed(profile: dict, lab: str) -> int:
    if not isinstance(profile, dict) or not isinstance(profile.get("population"), dict):
        raise ValueError(f"{lab}: the profile is not a JSON object with a population block")
    n = profile["population"].get("n_people")
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"{lab}: population.n_people is missing or not an integer")
    if n < K_FLOOR:                                   # village E4: the exporter's plain wording
        from .profile_export import k_floor_reason
        raise ValueError(f"{lab}: {k_floor_reason(n)}")
    return n


def _privacy(profile: dict, lab: str) -> None:
    """Final review A5: refuse a profile that fails the exporter's privacy scan (k floor on every n_people). The message
    names the lab and the positions, never a key or value from the file."""
    from . import profile_export
    if not isinstance(profile, dict):
        raise ValueError(f"{lab}: the profile is not a JSON object")
    problems = profile_export.privacy_scan(profile, k_floor=True) + closed_form_problems(profile)
    if problems:
        raise ValueError(f"{lab}: the profile fails the privacy scan ({len(problems)} problem(s): {shown_problems(problems)}); "
                         "a pool refuses it")


def _codes_problems(where: str, v, allowed) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(t, str) and t in allowed for t in v):
        return [f"{where}: not a list of the schema's codes"]
    return []


def _band_problems(where: str, band) -> list[str]:
    if band is None:
        return []
    if not isinstance(band, dict) or not set(band) <= BAND_FIELDS:
        return [f"{where} band: not an object of the schema's band fields"]
    out = []
    if not (_is_num(band.get("lo")) and _is_num(band.get("hi"))):
        out.append(f"{where} band: lo or hi is not a number")
    if band.get("narrow") is not None and not isinstance(band["narrow"], bool):
        out.append(f"{where} band: narrow is not true or false")
    if band.get("over") is not None and not (isinstance(band["over"], str) and LOWER_CODE.fullmatch(band["over"])):
        out.append(f"{where} band: over is not a code")
    for m in band.get("members") or []:
        if not isinstance(m, dict) or not set(m) <= {"x", "value"} or not (
                isinstance(m.get("x"), str) and LOWER_CODE.fullmatch(m["x"])) or not _is_num(m.get("value")):
            out.append(f"{where} band: a member is not {{x: code, value}}")
            break
    for sb in band.get("subbands") or []:
        if not isinstance(sb, dict) or not set(sb) <= {"name", "lo", "hi"} or not (
                isinstance(sb.get("name"), str) and LOWER_CODE.fullmatch(sb["name"])) or not (
                _is_num(sb.get("lo")) and _is_num(sb.get("hi"))):
            out.append(f"{where} band: a subband is not {{name: code, lo, hi}}")
            break
    return out


def closed_form_problems(profile: dict) -> list[str]:
    """Fix round 2, N1: the row and cell fields that pool copies out, each in its closed form (positions only in the
    messages). Rows: ``row`` a kit row id, ``label_key`` a code of at most 40 characters of [a-z0-9_.], no field outside
    ``ROW_FIELDS``, the row's codes from the schema lists. Cells: no field outside ``CELL_FIELDS``; ``name``, ``x``,
    ``unit``, ``kind`` lower-case codes; ``state`` not_run / partial_not_run; ``needs``, ``rests_on``, ``qualifiers``
    from the schema lists; ``source`` a kit source code; the band's fields in their forms."""
    out = []
    for i, r in enumerate(profile.get("rows") or []):
        if not isinstance(r, dict):
            continue                                       # the privacy scan reports it
        rid = r.get("row")
        where = f"rows[{i}]"
        if isinstance(rid, bool) or rid not in KIT_ROW_IDS:
            out.append(f"{where}: row is not a kit row id (lead, ledger, 1-9)")
        if not (isinstance(r.get("label_key"), str) and LABEL_KEY.fullmatch(r["label_key"])):
            out.append(f"{where}: label_key is not a code of at most 40 characters of a-z, 0-9, _ and .")
        if not set(r) <= ROW_FIELDS:
            out.append(f"{where}: a field outside the schema's row fields")
        out += _codes_problems(f"{where} rests_on", r.get("rests_on"), RESTS_ON)
        out += _codes_problems(f"{where} qualifiers", r.get("qualifiers"), QUALIFIERS)
        out += _band_problems(where, r.get("band"))
        for j, c in enumerate(r.get("cells") or []):
            if not isinstance(c, dict):
                continue
            cw = f"{where}.cells[{j}]"
            if not set(c) <= CELL_FIELDS:
                out.append(f"{cw}: a field outside the schema's cell fields")
            for f in ("name", "x", "unit", "kind"):
                v = c.get(f)
                if (v is not None or f == "name") and not (isinstance(v, str) and LOWER_CODE.fullmatch(v)):
                    out.append(f"{cw}: {f} is not a lower-case code")
            d = c.get("derived")
            if d is not None and d is not True and d not in DERIVED:
                out.append(f"{cw}: derived is not true or a kit derivation code")
            if c.get("state") is not None and c["state"] not in CELL_STATES:
                out.append(f"{cw}: state is not not_run or partial_not_run")
            if c.get("source") is not None and not (isinstance(c["source"], str) and SOURCE_CODE.fullmatch(c["source"])):
                out.append(f"{cw}: source is not a kit source code")
            out += _codes_problems(f"{cw} needs", c.get("needs"), NEEDS)
            out += _codes_problems(f"{cw} rests_on", c.get("rests_on"), RESTS_ON)
            out += _codes_problems(f"{cw} qualifiers", c.get("qualifiers"), QUALIFIERS)
            out += _band_problems(cw, c.get("band"))
    return out


def _estimator(profile: dict) -> tuple:
    """(B, seed) as the lab recorded them in run.settings (the exporter's names, or plain B / seed)."""
    st = (profile.get("run") or {}).get("settings") or {}
    return (st.get("bootstrap_resamples", st.get("B")), st.get("bootstrap_seed", st.get("seed")))


def _code_sha256(profile: dict):
    """Village E6: ``run.kit.code_sha256`` (None when absent, as in the study's profile and older exports)."""
    kit = ((profile.get("run") or {}).get("kit") or {}) if isinstance(profile.get("run"), dict) else {}
    return kit.get("code_sha256") if isinstance(kit, dict) else None


def _cells(profile: dict, lab: str) -> list[tuple]:
    """[(row, label_key, name, x, cell)] in the profile's own order; a repeated (row, name, x) is refused."""
    out, seen = [], set()
    for i, row in enumerate(profile.get("rows") or []):
        for j, c in enumerate(row.get("cells") or []):
            ident = (row["row"], c["name"], c.get("x"))
            if ident in seen:                    # by position: a message never echoes a value from the file (A2)
                raise ValueError(f"{lab}: the cell at rows[{i}].cells[{j}] repeats an earlier (row, name, x)")
            seen.add(ident)
            out.append((row["row"], row.get("label_key"), c["name"], c.get("x"), c))
    return out


def _table(members: list[tuple]) -> list[dict]:
    """One line per (row, name, x), in order of appearance across the labs; one column per lab."""
    lines: dict[tuple, dict] = {}
    for lab, profile in members:
        for row, label_key, name, x, c in _cells(profile, lab):
            line = lines.get((row, name, x))
            if line is None:
                line = lines[(row, name, x)] = {"row": row, "label_key": label_key, "name": name, "columns": {}}
                if x is not None:
                    line["x"] = x
            line["columns"][lab] = c
    return list(lines.values())


DOSE_TYPES = ("D1", "D2", "D3", "D4", "D5")                      # phantom.DOSES (calibrate's known-distance check)


def _share(v) -> str:
    return f"{v * 100:g}%"


def detection_limits_of(profile: dict):
    """Village E5: the lab's ``detection_limits.per_dose`` reduced to {dose: {smallest_detected, lowest_tried,
    at_or_below}}, numbers and booleans only (closed form: nothing else from the file is copied). None when the
    profile has no block, or a malformed one."""
    dl = profile.get("detection_limits") if isinstance(profile, dict) else None
    per = dl.get("per_dose") if isinstance(dl, dict) else None
    if not isinstance(per, dict):
        return None
    out = {}
    for dose in DOSE_TYPES:
        e = per.get(dose)
        if not isinstance(e, dict):
            return None
        sd, lowest, at = e.get("smallest_detected"), e.get("lowest_tried"), e.get("at_or_below")
        if not (_is_num(sd) and _is_num(lowest) and lowest is not None and isinstance(at, bool)):
            return None
        out[dose] = {"smallest_detected": sd, "lowest_tried": lowest, "at_or_below": at}
    return out


def dose_name(dose: str) -> str:
    from .phantom import DOSE_NAMES
    return f"{dose} {DOSE_NAMES[dose]}"


def detection_text(entry: dict) -> str:
    """One dose type's result in plain words (calibrate's console line, pool.md's detection-limit cell)."""
    sd, lowest = entry["smallest_detected"], entry["lowest_tried"]
    if sd is None:
        return f"not detected at any dose tried (lowest dose tried {_share(lowest)})"
    text = f"smallest dose detected {_share(sd)}, lowest dose tried {_share(lowest)}"
    return text + (f" (read as: at or below {_share(sd)})" if entry["at_or_below"] else "")


_REPEAT_FIELDS = ("value", "ci_lo", "ci_hi", "ci_level", "p", "p_lt", "n", "state")    # E3: lead vs row 7
_EPS = 1e-9


def _chain_head(profile: dict):
    heads = (profile.get("run") or {}).get("run_chain_heads") if isinstance(profile.get("run"), dict) else None
    head = heads.get("head") if isinstance(heads, dict) else None
    return head if isinstance(head, str) and head else None


def _cell_where(row, c) -> str:
    """A cell by its codes (they passed the closed-form check): ``row 1 attested_recall (primary)``."""
    x = c.get("x")
    return f"row {row} {display_name(c['name'])}" + (f" ({x})" if x is not None else "")


def checks(members: list[tuple]) -> list[str]:
    """Village E3: cheap checks on the files as given (pool does not authenticate them). Warnings only, in input
    order: two files with identical bodies or the same run chain head; a value outside its own [ci_lo, ci_hi]; a cell
    of row 7 that repeats a lead cell and disagrees with it; a cell with no value that is not marked not_run.
    Messages name labs and cell codes, never a value from the file."""
    out = []
    bodies = {}
    heads = {}
    for lab, p in members:
        body = json.dumps(p, sort_keys=True)
        if body in bodies:
            out.append(f"{bodies[body]} and {lab} are identical files; {lab} may be a copy, not a second lab")
        else:
            bodies[body] = lab
            head = _chain_head(p)
            if head is not None and head in heads:
                out.append(f"{heads[head]} and {lab} have the same run chain head; they come from one run, not two labs")
            elif head is not None:
                heads[head] = lab
    for lab, p in members:
        lead = {}
        for row in p.get("rows") or []:
            for c in row.get("cells") or []:
                v, lo, hi = c.get("value"), c.get("ci_lo"), c.get("ci_hi")
                if v is None and c.get("state") != "not_run":
                    out.append(f"{lab}: {_cell_where(row['row'], c)} has no value and is not marked not_run (malformed)")
                if None not in (v, lo, hi) and not (lo - _EPS <= v <= hi + _EPS):
                    out.append(f"{lab}: {_cell_where(row['row'], c)}: the value is outside its own interval "
                               "[ci_lo, ci_hi]")
                if row["row"] == "lead":
                    lead[(c["name"], c.get("x"))] = c
                elif row["row"] == 7 and (c["name"], c.get("x")) in lead:
                    first = lead[(c["name"], c.get("x"))]
                    if any(first.get(f) != c.get(f) for f in _REPEAT_FIELDS):
                        out.append(f"{lab}: {_cell_where(7, c)} disagrees with the same cell in the lead block")
    return out


def pool(profiles: list[dict], labels: list[str] | None = None) -> dict:
    """Place lab profiles side by side. ``labels`` names the columns (default ``lab1``, ``lab2``, ...).
    Raises ValueError for an unnamed duplicate lab, a schema other than orgsci-profile/v2(.minor), a malformed profile,
    ``n_analysed`` under 10, or a profile that fails the privacy scan."""
    labels = list(labels) if labels is not None else [f"lab{i + 1}" for i in range(len(profiles))]
    if len(labels) != len(profiles):
        raise ValueError(f"one label per profile is required ({len(labels)} label(s), {len(profiles)} profile(s))")
    for i, lab in enumerate(labels):                    # village E3: name the clashing label
        if lab in labels[:i]:
            raise ValueError(f"two files are called {lab}; rename one")
    members: dict[tuple, list] = {}
    keys: dict[str, dict] = {}
    n_of: dict[str, int] = {}
    for lab, p in zip(labels, profiles):
        if is_study_profile(p):
            raise ValueError(study_refusal(lab))
        _schema(p, lab)
        n_of[lab] = _n_analysed(p, lab)
        _privacy(p, lab)
        keys[lab] = k = _key(p, lab)
        members.setdefault(tuple(k.values()), []).append((lab, p))
    groups, separate = [], []
    for ident in sorted(members, key=lambda t: tuple(map(str, t))):
        group = members[ident]
        if len(group) < 2:
            separate.append(group[0][0])
            continue
        code_differs = len({json.dumps(_code_sha256(p)) for _, p in group}) > 1     # E6: absent counts as a value
        groups.append({"key": keys[group[0][0]], "labs": [lab for lab, _ in group],
                       "n_analysed": {lab: n_of[lab] for lab, _ in group},
                       "settings_differ": len({_estimator(p) for _, p in group}) > 1 or code_differs,
                       "bootstrap_differs": len({_estimator(p) for _, p in group}) > 1,
                       "code_differs": code_differs,
                       "table": _table(group)})
    separate_set = set(separate)
    separate = [lab for lab in labels if lab in separate_set]             # input order
    profiles_by_label = dict(zip(labels, profiles))
    out = {"schema": "orgsci-pool/v1", "meta_analysis": False,
           "pooling": {"rule": "side_by_side", "within": ["reference_type", "prompt_set_pair", "state"],
                       "meta_analysis": False},
           "groups": groups, "separate": separate,
           "separate_keys": {lab: keys[lab] for lab in separate},
           # village E2: each lab listed on its own keeps its own one-column table (pool.md shows its headline)
           "separate_labs": {lab: {"n_analysed": n_of[lab], "table": _table([(lab, profiles_by_label[lab])])}
                             for lab in separate}}
    limits = {lab: dl for lab, p in zip(labels, profiles) if (dl := detection_limits_of(p)) is not None}
    if limits:                                                           # E5: only when a lab ran calibrate
        out["detection_limits"] = limits
    found = checks(list(zip(labels, profiles)))
    if found:                                                            # E3: only when something is found
        out["checks"] = found
    return out


def _fmt(c: dict | None) -> str:
    if c is None:
        return "-"
    if c.get("state") == "not_run" or c.get("value") is None:
        why = ", ".join(c.get("needs") or []) or c.get("reason_code") or c.get("reason") or "no value"
        return f"not run ({why})"
    v = c["value"]
    s = f"{v:g}" if isinstance(v, (int, float)) else str(v)
    if c.get("ci_lo") is not None and c.get("ci_hi") is not None:
        s += f" [{c['ci_lo']:g}, {c['ci_hi']:g}]"
    elif c.get("p_lt") is not None:
        s += f" (p<{c['p_lt']:g})"
    elif c.get("p") is not None:
        s += f" (p={c['p']:g})"
    if c.get("state") == "partial_not_run":
        s += " (partial)"
    return s


def display_name(name) -> str:
    """A cell name as pool.md shows it (final review A11). The strings file has no per-cell label, so the schema name is
    shown, with "scripted" read as "no-interview" (the three row-6 v2 names, which the profile keeps, R5). pool.json
    keeps the schema names."""
    return str(name).replace("scripted", "no-interview")


HOW_TO_READ = (                                                      # village E1: five lines, before any table
    "- Each column is one lab's own result; no number is combined across labs.",
    "- A number with brackets, such as 0.59 [0.52, 0.66], is the value and its confidence interval (95% unless the "
    "cell's ci_level in pool.json says otherwise).",
    "- primary and lenient are the two main matching rules (the Setting column names others too): primary is the "
    "main reading; lenient also accepts looser matches.",
    "- \"not run (X)\" means that lab's run did not compute the cell; X is what the cell needs (for example sidejudge "
    "or J2) or a reason code (for example group_empty); both are defined in PROFILE-SCHEMA.md.",
    "- Every row, cell and code is defined in PROFILE-SCHEMA.md.",
)
# (plain name, row, cell name, x): the headline table, in plain words (village E1)
HEADLINE = (
    ("Listening added (primary): AI recall minus no-interview recall", "lead", "listening_added", "primary"),
    ("AI recall (primary)", "lead", "ai_recall", "primary"),
    ("No-interview recall (primary)", "lead", "no_interview_recall", "primary"),
    ("AI map size: boxes per map", "lead", "ai_nodes_per_map", None),
    ("No-interview map size: boxes per map", "lead", "no_interview_nodes_per_map", None),
    ("Endorsed map size: boxes per map", "lead", "endorsed_nodes_per_map", None),
)


def _headline(labs: list[str], table: list[dict], n_analysed: dict) -> list[str]:
    """The headline table (people analysed, then ``HEADLINE``), one column per lab; "-" where a lab has no such cell."""
    lines = {(t["row"], t["name"], t.get("x")): t for t in table}
    out = ["| Headline | " + " | ".join(labs) + " |", "|---|" + "---|" * len(labs),
           "| People analysed | " + " | ".join(str(n_analysed.get(lab, "-")) for lab in labs) + " |"]
    for plain, row, name, x in HEADLINE:
        line = lines.get((row, name, x))
        cols = line["columns"] if line else {}
        out.append(f"| {plain} | " + " | ".join(_fmt(cols.get(lab)) for lab in labs) + " |")
    return out


def _all_cells(labs: list[str], table: list[dict]) -> list[str]:
    out = ["| Row | Cell | Setting | " + " | ".join(labs) + " |", "|---|---|---|" + "---|" * len(labs)]
    for line in table:
        out.append(f"| {line['row']} | {display_name(line['name'])} | {line.get('x', '')} | "
                   + " | ".join(_fmt(line["columns"].get(lab)) for lab in labs) + " |")
    return out


def _key_line(lab: str, k: dict, n) -> str:
    return (f"- {lab}: reference {k['reference_type']}, state {k['state']}, Q `{k['q_phase_sha256'][:12]}`, "
            f"J/M `{k['j_m_phase_sha256'][:12]}`, people analysed {n}")


def table_counts(pool_: dict) -> tuple[int, int]:
    """(tables, labs listed on their own): every group is one table and every lab on its own gets its own (E2)."""
    return len(pool_["groups"]) + len(pool_["separate"]), len(pool_["separate"])


def pool_markdown(pool_: dict) -> str:
    """The pool as markdown (village E1/E2): how to read it, any checks, then per group a headline table in plain words
    and all cells; a lone profile gets its own one-column tables; each lab listed on its own gets a headline table."""
    out = ["# Lab profiles, side by side", "", "## How to read this", "", *HOW_TO_READ, "", CAVEAT, ""]
    if pool_.get("checks"):                                              # village E3
        out += ["## Checks", "", "pool does not authenticate the files it is given. These checks found:", ""]
        out += [f"- {msg}" for msg in pool_["checks"]] + [""]
    separate_labs = pool_.get("separate_labs") or {}
    alone = not pool_["groups"] and len(pool_["separate"]) == 1
    if alone:                                                            # village E2
        lab = pool_["separate"][0]
        sep = separate_labs.get(lab) or {"n_analysed": "-", "table": []}
        out += ["## Only one profile was given", "",
                "There is no other lab to place beside it, so this is its own table.", "",
                _key_line(lab, pool_["separate_keys"][lab], sep["n_analysed"]), "",
                "### Headline", "", *_headline([lab], sep["table"], {lab: sep["n_analysed"]}), "",
                "### All cells", "", *_all_cells([lab], sep["table"]), ""]
    for g in pool_["groups"]:
        k = g["key"]
        out += [f"## Reference {k['reference_type']}, state {k['state']}", "",
                f"- Prompt-set pair: Q phase `{k['q_phase_sha256']}`, J/M phase `{k['j_m_phase_sha256']}`",
                "- People analysed: " + ", ".join(f"{lab} {n}" for lab, n in g["n_analysed"].items())]
        if g.get("settings_differ") and g.get("bootstrap_differs", not g.get("code_differs")):
            out.append("- The labs' bootstrap settings (B or seed) differ; the columns are still side by side and "
                       "are not merged.")
        if g.get("code_differs"):                                         # village E6
            out.append("- The labs ran different kit code (run.kit.code_sha256 differs, or is missing for some); "
                       "the columns are still side by side and are not merged.")
        out += ["", "### Headline", "", *_headline(g["labs"], g["table"], g["n_analysed"]), "",
                "### All cells", "", *_all_cells(g["labs"], g["table"]), ""]
    if pool_.get("detection_limits"):                                    # village E5
        labs_dl = list(pool_["detection_limits"])
        out += ["## Detection limits (calibrate's known-distance check)", "",
                "Per lab that ran `calibrate`: per dose type, the smallest dose the check detected and the lowest dose "
                "it tried. A lab without a column did not run it, or its block could not be read.", "",
                "| Dose type | " + " | ".join(labs_dl) + " |", "|---|" + "---|" * len(labs_dl)]
        for dose in DOSE_TYPES:
            out.append(f"| {dose_name(dose)} | "
                       + " | ".join(detection_text(pool_["detection_limits"][lab][dose]) for lab in labs_dl) + " |")
        out.append("")
    if pool_["separate"] and not alone:
        out += ["## Listed separately", "",
                "Each of these labs differs from every other lab given in reference type, prompt-set pair or state, so it "
                "is not placed beside them; each has its own headline table.", ""]
        for lab in pool_["separate"]:
            sep = separate_labs.get(lab)
            n = sep["n_analysed"] if sep else "-"
            out += [f"### {lab}", "", _key_line(lab, pool_["separate_keys"][lab], n), ""]
            if sep:
                out += [*_headline([lab], sep["table"], {lab: n}), ""]
    return "\n".join(out)
