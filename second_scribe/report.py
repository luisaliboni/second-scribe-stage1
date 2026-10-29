"""Aggregate-only outputs: ``aggregates/aggregates.json``, ``aggregates/report.md`` and, for the synthetic
corpus, ``aggregates/traps.md``.

For a real corpus every output is counts, rates, intervals, n and exclusions only. ``assert_aggregate_only``
walks an object before it is written and refuses any string longer than 40 characters and any string equal
to a node label or quote (collected from the work files in memory, never written).

Maps are "interviewer-drawn, participant-endorsed maps". Recovering the planted traps of the synthetic corpus
tests the code and gives no evidence about the method (council 30: no "leaked", "leak rate" or "method is valid" in
the printed report; the field names stay).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .metrics import CONTRADICTION_NOTE
from .kit_vocab import KIT_KEYS, KIT_VALUES
from .textnorm import locate_quote_tiered, token_seq

MAX_STRING = 40
_DIGEST = re.compile(r"^[0-9a-f]{64}$")          # a sha256 hex digest (config, prompt set): cannot carry a label
_NOT_DRAWN_KINDS = ("negated", "other_person", "hypothetical")


# The kit's own fixed notes that the headline carries, listed by value: exempt from the length rule only by
# exact match, and still checked against labels and quotes. Add a note here when metrics adds one.
KIT_NOTES: frozenset[str] = frozenset({CONTRADICTION_NOTE})


def assert_aggregate_only(obj, forbidden: set[str] | frozenset = frozenset(), *, path: str = "$",
                          allowed: frozenset[str] = frozenset(), keys: frozenset[str] = frozenset(),
                          values: frozenset[str] = frozenset()) -> None:
    """Raise ValueError if any string (dict keys included) in ``obj`` is longer than 40 characters or equals
    (whitespace-collapsed, case-insensitive) a string in ``forbidden``. The message names the location only,
    never the offending text. The one exemption from the length rule is a whole lowercase sha256 hex digest
    (the config and prompt-set hashes the aggregates must carry). ``keys`` / ``values`` (``kit_vocab``) are the
    kit's own words: a dict key exactly in ``keys``, or a value exactly in ``values``, is not compared with the
    labels (a map node may be labelled "Family"; the kit's schema key ``family`` is not that label)."""
    bad = {" ".join(s.split()).casefold() for s in forbidden if s}

    def walk(x, where: str, kit: frozenset[str] = values) -> None:
        if isinstance(x, str):
            if len(x) > MAX_STRING and not _DIGEST.fullmatch(x) and x not in allowed:
                raise ValueError(f"aggregate-only guard: string longer than {MAX_STRING} characters at {where}")
            if x not in kit and " ".join(x.split()).casefold() in bad:
                raise ValueError(f"aggregate-only guard: a label or quote at {where}")
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k, f"{where}.<key>", keys)
                walk(v, f"{where}.{k}" if isinstance(k, str) and len(k) <= MAX_STRING else f"{where}.<key>")
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                walk(v, f"{where}[{i}]")
    walk(obj, path)


def out_paths(cfg) -> dict[str, Path]:
    d = Path(cfg.out_dir) / "aggregates"
    return {"aggregates": d / "aggregates.json", "report": d / "report.md", "traps": d / "traps.md",
            "audits": d / "audits.json"}


def aggregates_path(cfg) -> Path:
    return out_paths(cfg)["aggregates"]


def write_text_atomic(path, text: str) -> None:
    """Temp file in the same folder, fsync, then ``os.replace``: a crash never leaves a half-written output."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_aggregates(cfg, agg: dict, forbidden: set[str] | None = None, guard=None) -> Path:
    """Write ``agg`` (pretty, sorted keys). With ``forbidden`` (real corpus) the guard runs first and nothing is
    written if it fails. ``guard(path, text)`` (``audits.LeakGuard``, real corpus) sees the exact text first and
    raises to refuse the write."""
    if forbidden is not None:
        assert_aggregate_only(agg, forbidden, allowed=KIT_NOTES, keys=KIT_KEYS, values=KIT_VALUES)
    p = aggregates_path(cfg)
    text = json.dumps(agg, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if guard is not None:
        guard(p, text)
    write_text_atomic(p, text)
    return p


# ------------------------------------------------------------------ traps (synthetic only)

def evaluate_traps(p_list, synthetic_dir, spec_keys: list[str], setting, min_words: int = 8) -> list[dict]:
    """One row per planted trap and arm: {pid, kind, cid, arm, expected, observed, result}.

    The arm's spec is the primary spec's variant in that arm (``<arm>__p0__t0__r0`` for the demo). Observed:
    endorsed-node kinds use the bucket of the concept's endorsed node(s) (matched / missed / off_record); a wished
    concept uses the absence classification of ``metrics.absences`` (matched = recovered_as_wished, otherwise
    leaked_as_existing, leaked_unanchored, unanchored_wished or missed);
    unstated_edge is "off_record" unless the AI recovers the drawn edge ("recovered"); negated, other_person and
    hypothetical are never drawn on the endorsed map, so they pass when no ATTESTED AI node carries the trap
    sentence as its quote (observed "not_drawn" or "unanchored"), and fail as "attested"."""
    from . import metrics      # late: metrics imports nothing from here, but keep this module light to import

    root = Path(synthetic_dir)
    primary = spec_keys[0]
    rows = []
    for p in p_list:
        traps = json.loads((root / "traps" / f"{p.pid}.json").read_text(encoding="utf-8"))
        concepts = json.loads((root / "gold" / f"{p.pid}.concepts.json").read_text(encoding="utf-8"))
        nodes_of: dict[str, list[str]] = {}
        for nid, cid in concepts["endorsed"].items():
            nodes_of.setdefault(cid, []).append(nid)
        for arm in ("full", "removed"):
            spec = metrics.swap_arm(primary, arm)
            if spec not in spec_keys:
                continue
            g = p.ai.get(spec)
            for t in traps:
                expected = t["arm_expectation"][arm]
                row = {"pid": p.pid, "kind": t["kind"], "cid": t["cid"], "arm": arm, "expected": expected}
                if g is None:
                    row["observed"] = "ai_failed"
                else:
                    row["observed"] = _observe(metrics, p, spec, arm, setting, min_words, t, nodes_of)
                ok = (row["observed"] in ("not_drawn", "unanchored") if t["kind"] in _NOT_DRAWN_KINDS
                      else row["observed"] == expected)
                row["result"] = "PASS" if ok else "FAIL"
                rows.append(row)
    return rows


def quote_carries_sentence(quote: str, sentence: str) -> bool:
    """A node's quote carries a planted trap sentence if one contains the other (word for word), or, since run 2,
    if the quote is an approximate copy of the sentence by the tiered quote rule: Task 6 attests such a quote, so the
    trap check must see it as a carrier too."""
    a, b = token_seq(quote), token_seq(sentence)
    if a in b or b in a:
        return True
    return locate_quote_tiered(f"[P] {sentence}", quote)[0] != "unfound"


def _observe(metrics, p, spec, arm, setting, min_words, trap, nodes_of) -> str:
    m, m_att, att = metrics._attested_matching(p, spec, arm, setting, min_words)
    g = p.ai[spec]
    kind = trap["kind"]
    if kind in _NOT_DRAWN_KINDS:
        carriers = [x for x in g.nodes if x.quote and quote_carries_sentence(x.quote, trap["sentence"])]
        if any(x.node_id in att for x in carriers):
            return "attested"
        return "unanchored" if carriers else "not_drawn"
    if kind == "unstated_edge":
        a, b = trap["cids"]
        edges = [e for e in p.endorsed.edges if e.source in nodes_of.get(a, []) and e.target in nodes_of.get(b, [])]
        return "recovered" if any(metrics.edge_recovered(e, g, m_att) for e in edges) else "off_record"
    cids = trap.get("cids") or [trap["cid"]]
    found = []
    for cid in cids:
        for nid in nodes_of.get(cid, []):
            if not p.in_transcript[nid]:
                found.append("off_record")
            elif nid in m_att.right_matched:
                found.append("matched")
            else:
                found.append("missed")
    if not found:
        return "not_on_map"
    worst = next((b for b in ("off_record", "missed") if b in found), "matched")
    if kind == "wished" and worst != "off_record":
        # the same classification as metrics.absences: recovered_as_wished is "matched", anything else is named
        st = metrics._absence_structure(p, spec, arm, setting, min_words)
        cats = [metrics.absence_category(st, st["status"], nid)[0] for nid in nodes_of.get(trap["cid"], [])]
        if cats and all(c == "recovered_as_wished" for c in cats):
            return "matched"
        return next(c for c in cats if c != "recovered_as_wished") if cats else "not_on_map"
    return worst


def traps_markdown(rows: list[dict]) -> str:
    n_fail = sum(r["result"] == "FAIL" for r in rows)
    lines = ["# Planted traps (synthetic corpus)", "",
             "Recovering the planted traps tests the code and gives no evidence about the method.", "",
             f"{len(rows)} checks, {len(rows) - n_fail} pass, {n_fail} fail.", "",
             "| participant | kind | concept | arm | expected | observed | result |",
             "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['pid']} | {r['kind']} | {r['cid']} | {r['arm']} | {r['expected']} | {r['observed']} | "
              f"{r['result']} |" for r in rows]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ report.md

def _fmt(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def _ci(block: dict | None) -> str:
    if not block:
        return "n/a"
    return f"{_fmt(block.get('mean'))} [{_fmt(block.get('ci_lo'))}, {_fmt(block.get('ci_hi'))}] (n={_fmt(block.get('n'))})"


def audits_markdown(summary: dict) -> str:
    """The audit summary (``aggregates/audits.json``) as a report section: stage, auditor, blocking, result and
    counts. Reused by the technical appendix."""
    lines = ["## Audits", "",
             "Deterministic auditors (no model) run at the end of every stage group. Counts only; a failed blocking "
             "auditor stops the pipeline.", "",
             "| stage | auditor | blocking | result | counts | failures | warnings |", "|---|---|---|---|---|---|---|"]
    n = 0
    stages = summary.get("stages") or {}
    for stage in [s for s in ("Q", "J", "M") if s in stages] + sorted(set(stages) - {"Q", "J", "M"}):
        for r in stages[stage]:
            n += 1
            lines.append(f"| {stage} | {r['name']} | {'yes' if r['blocking'] else 'no'} | "
                         f"{'pass' if r['passed'] else 'FAIL'} | {json.dumps(r['counts'], sort_keys=True)} | "
                         f"{json.dumps(r['failure_codes'], sort_keys=True)} | "
                         f"{json.dumps(r['warning_codes'], sort_keys=True)} |")
    return "\n".join(lines + ([] if n else ["| - | none yet | | | | | |"])) + "\n"


_RULE_WORDS = {
    "boot_maps": "the interval is a percentile bootstrap that resamples whole maps (a participant's draws stay together)",
    "signflip": "the test is a two-sided sign-flip permutation over maps (exact when few maps differ, otherwise sampled)",
    "ci_excludes_zero_bad_side": "a measure counts as worse only when its whole interval lies on the bad side of 0",
    "no_test": "the script-conditional gap is shown per rung and is not tested",
}


def _reason_sentence(code: str, v: dict) -> str:
    name, _, arg = code.partition(":")
    leak = f"change {_fmt(v.get('leak_mean'))}, 95% CI [{_fmt(v.get('leak_ci_lo'))}, {_fmt(v.get('leak_ci_hi'))}]"
    return {
        "contrast_not_run": "the remedy-placebo contrast was not computed",
        "leak_too_few_maps": "too few maps could be compared to say anything about the share drawn as received",
        "leak_rose_holm_fails": f"the share drawn as received rose ({leak}) but the Holm-adjusted p is not below alpha",
        "leak_rose": f"the share drawn as received rose with the remedy ({leak})",
        "leak_ci_includes_zero": f"the share drawn as received did not measurably change ({leak})",
        "mirror_worse": f"{arg} got worse beyond its interval",
        "kept_not_risen": "the share drawn as wished for (kept ideals) did not rise",
        "not_assessed": f"{arg} could not be assessed",
        "leak_drop_holm_fails": f"the share drawn as received fell ({leak}) but the Holm-adjusted p is not below alpha",
        "leak_drop_no_mirror_worse": f"the share drawn as received fell ({leak}) and no gating measure got worse",
        "not_judged_over_limit": "the judge left more than the allowed share of a rung's items unjudged: the contrast "
                                 "is reported, not interpreted",
        "incomplete": "a rung has maps whose judge labels or alignments are missing: no verdict until the same "
                      "command is rerun",
    }.get(name, f"({code})")


def ladder_markdown(lad: dict, synthetic: bool = False) -> str:
    """The instruction-ladder section: the prose is built here from the codes and numbers in ``aggregates["ladder"]``
    (the aggregates carry no sentences, so the aggregate-only guard stays as strict as for the rest of the run)."""
    rep = lad.get("report") or {}
    v = rep.get("verdict") or {}
    prm = rep.get("parameters") or {}
    lines = ["## Instruction ladder", "",
             f"- rungs: {', '.join(lad['rungs'])}; draws per participant and rung: {lad['draws']}; "
             f"permutations {lad['n_perm']}, bootstrap {lad['B']}, seed {lad['seed']}",
             f"- arm of each rung: {json.dumps(lad['arm_of_rung'], sort_keys=True)}",
             f"- complete: {lad['complete']}" + ("" if lad["complete"] else
                                                 " (cells without a stored map: rerun the same command before "
                                                 "reading the contrasts)")]
    if lad.get("call_order"):
        lines.append(f"- order of the calls: {lad['call_order']} (rung 2, then rung 3 and the placebo draw by draw, then "
                     "rung 1; each arm over all participants before the next, so drift in the model server during the "
                     "run is confounded with condition; see the determinism audit)")
    if lad.get("interpretation"):
        ss = lad.get("script_source") or {}
        lines.append(f"- interpretation: {lad['interpretation']}; script of the script-conditional gaps: "
                     f"{ss.get('name')} (family {ss.get('family')})")
    if synthetic:
        lines.append("- synthetic corpus: the numbers below are in-distribution (they check the code path, not the "
                     "remedy, and not any real model)")
    lines += ["", "### Maps per rung", "",
              "| rung | expected | ok | failed | other | not run | retries | transport errors | copy-audit flagged "
              "| null-phase share |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k in lad["rungs"]:
        pr, by = lad["per_rung"][k], lad["per_rung"][k]["by_status"]
        other = sum(n for st, n in by.items() if st not in ("ok", "failed", "not_run"))
        lines.append(f"| {k} | {pr['expected']} | {by.get('ok', 0)} | {by.get('failed', 0)} | {other} | "
                     f"{by.get('not_run', 0)} | {pr['retries']} | {pr['transport_errors']} | "
                     f"{_fmt((lad['copy_audit'].get(k) or {}).get('n_flagged'))} | "
                     f"{_fmt((lad['null_phase'].get(k) or {}).get('share'))} |")
    lines += ["", "### Do repeated draws differ?", ""]
    for k in lad["rungs"]:
        di = lad["draw_identity"].get(k) or {}
        lines.append(f"- {k}: {_fmt(di.get('n_identical'))} of {_fmt(di.get('n_pairs'))} draw pairs identical to draw 0 "
                     f"(share {_fmt(di.get('share_identical'))})")
    if v:
        codes = v.get("reason_codes") or []
        lines += ["", "### Verdict on the remedy (remedy against placebo)", "", f"- verdict: **{v.get('verdict')}**"]
        lines += [f"- {_reason_sentence(c, v)}" for c in codes]
        if v.get("verdict") == "null" and v.get("excludes_leak_drop_larger_than") is not None:
            lines.append(f"- the data exclude a drop in the share drawn as received larger than {_fmt(v['excludes_leak_drop_larger_than'])}: "
                         "the pull toward the script resists this remedy to that extent")
        lines.append(f"- Holm-adjusted p for the contrast on the share drawn as received: {_fmt(v.get('leak_p_holm'))}; gating checks: "
                     f"{_fmt(v.get('n_gating_checks'))}; nominal false-alarm rate across them: "
                     f"{_fmt(v.get('nominal_false_alarm_rate'))}")
        rules = v.get("rules") or prm.get("verdict_rules") or {}
        words = [_RULE_WORDS[c] for c in (rules.get("ci"), rules.get("test"), rules.get("worse")) if c in _RULE_WORDS]
        if (rep.get("script_conditional") or {}).get("note") in _RULE_WORDS:
            words.append(_RULE_WORDS[rep["script_conditional"]["note"]])
        lines += [f"- rules: {'; '.join(words)}"] if words else []
    dc = rep.get("declaration_check") or {}
    if dc:
        lines.append(f"- declaration: the analysis is **{dc.get('label')}** against the contrasts declared "
                     f"{dc.get('declared_on', 'n/a')} (UTC date)" + (f" (differences: {', '.join(map(str, dc['differences']))})"
                                                           if dc.get("differences") else ""))
    if rep.get("skipped"):
        lines.append(f"- skipped contrasts: {', '.join(map(str, rep['skipped']))}")
    return "\n".join(lines) + "\n"


TYPE_SHARE_SENTENCE = ("type_share:* features (and so homogenization and linearization, which read them) count a node "
                       "the model typed wrongly as 'other'; the type-coercion counts above say how many nodes that is. "
                       "Descriptive only: the pre-registered features are unchanged.")


def quote_type_markdown(sec: dict) -> list[str]:
    """Lines for the quote-tier / type-coercion counts (``aggregates["quote_type"]``): one row per spec, arm and rung,
    then the one-line note that ``type_share:*`` features include coerced nodes."""
    lines = ["## Quote tiers and type coercions", "",
             "| group | maps | nodes | type coerced to other | maps with a coercion | quotes exact | approximate | unfound |",
             "|---|---|---|---|---|---|---|---|"]
    for part, label in (("by_spec", "spec"), ("by_arm", "arm"), ("by_rung", "rung")):
        for k, c in (sec.get(part) or {}).items():
            t = c.get("quote_tiers") or {}
            lines.append(f"| {label} {k} | {_fmt(c.get('maps'))} | {_fmt(c.get('nodes'))} | "
                         f"{_fmt(c.get('type_coercions'))} | {_fmt(c.get('maps_with_coercion'))} | "
                         f"{_fmt(t.get('exact'))} | {_fmt(t.get('approximate'))} | {_fmt(t.get('unfound'))} |")
    rows = [(label, k, c["accepted_attempt"]) for part, label in (("by_spec", "spec"), ("by_arm", "arm"),
                                                                  ("by_rung", "rung"))
            for k, c in (sec.get(part) or {}).items() if c.get("accepted_attempt")]
    if rows:
        lines += ["", "### Accepted at attempt", "",
                  "A map accepted at attempt 2 or 3 was written with a rejected draft and a correction in context.", "",
                  "| group | attempt 1 | attempt 2 | attempt 3 | failed | unknown |", "|---|---|---|---|---|---|"]
        lines += [f"| {label} {k} | {_fmt(a.get('1'))} | {_fmt(a.get('2'))} | {_fmt(a.get('3'))} | "
                  f"{_fmt(a.get('failed'))} | {_fmt(a.get('unknown'))} |" for label, k, a in rows]
    if sec.get("type_share_affected"):
        lines += ["", f"- note: {TYPE_SHARE_SENTENCE}"]
    return lines + [""]


def analysed_line(n_participants: int, not_analysed: dict, item_exclusions: dict | None = None) -> str:
    """``participants N, analysed M, excluded K (no speaker labels: 1, …)``, plus the items left out for length."""
    k = sum(not_analysed.values())
    line = f"participants {n_participants}, analysed {n_participants - k}, excluded {k}"
    if not_analysed:
        line += " (" + ", ".join(f"{why.replace('_', ' ')}: {n}" for why, n in sorted(not_analysed.items())) + ")"
    for code, by_stage in sorted((item_exclusions or {}).items()):
        line += f"; items {code.replace('_', ' ')}: " + ", ".join(f"{st} {n}" for st, n in sorted(by_stage.items()))
    return line


def _run_line(agg: dict) -> str:
    """The report's run line. With someone not analysed (or an item left out for its length) it says who and why:
    ``participants N, analysed M, excluded K (no speaker labels: 1)``; otherwise it is the line it always was."""
    not_analysed, items = agg.get("not_analysed") or {}, agg.get("item_exclusions") or {}
    head = f"- run: `{agg['run_id']}`, corpus: {agg['corpus_kind']}, "
    if not not_analysed and not items and agg["n_participants"] == agg["n_analysed"]:
        return head + f"participants: {agg['n_participants']} (analysed {agg['n_analysed']})"
    return head + analysed_line(agg["n_participants"], not_analysed, items)


def report_markdown(agg: dict, audits: dict | None = None) -> str:
    h = agg.get("headline") or {}
    ab, pull = h.get("absences") or {}, h.get("pull_toward_script") or {}
    lines = [
        "# Report: what did listening add?", "",
        "AI reconstructions compared with interviewer-drawn, participant-endorsed maps. Aggregates only.", "",
        _run_line(agg),
        f"- config sha256: `{agg['config_sha256']}`", f"- prompt set sha256: `{agg['prompt_set_sha256']}`",
        *([] if agg.get("prompt_manifest_ok", True) else
          ["- WARNING: the prompt files differ from MANIFEST.json (the run used edited prompts)"]),
        f"- models: {', '.join(f'{k}={v}' for k, v in sorted(agg['models'].items())) or 'n/a'}",
        f"- primary spec: `{agg['primary_spec']}`, primary match setting: `{agg['primary_setting']}`",
        f"- exclusions: {json.dumps(agg['exclusions'], sort_keys=True)}",
        *([f"- duplicate transcripts: {d['copies']} transcript file(s) are byte-identical copies of another "
           f"({d['groups']} group(s)); each copy counts as a separate person in n and in the intervals "
           "(nothing was excluded)"] if (d := agg.get("duplicate_transcripts")) else []),
        *([f"- run incomplete: {agg['outage']['timeout_excluded_items']} item(s) are timeout_excluded (the model could "
           f"not be reached; by stage {json.dumps(agg['outage'].get('by_stage') or {}, sort_keys=True)}); the numbers "
           "below rest on the items that were reachable, so rerun the same command before reading them"]
          if (agg.get("outage") or {}).get("timeout_excluded_items") else []), "",
        "## Node buckets by spec", "",
        "| spec | n | matched | missed | off_record | unanchored | attested_absent | node recall | edge recall | interpretation |",
        "|---|---|---|---|---|---|---|---|---|---|"]
    for k in agg["specs"]:
        b = (agg["buckets"].get("by_spec") or {}).get(k) or {}      # {} when a reference mode masks it
        lines.append(f"| {k} | {_fmt(b.get('n'))} | {_fmt(b.get('matched'))} | {_fmt(b.get('missed'))} | "
                     f"{_fmt(b.get('off_record'))} | {_fmt(b.get('unanchored'))} | "
                     f"{_fmt(b.get('attested_absent_from_map'))} | {_fmt(b.get('node_recall'))} | "
                     f"{_fmt(b.get('edge_recall'))} | {b.get('interpretation')} |")
    lines += ["", "## Absences (endorsed wished nodes in the transcript) by spec", "",
              "| spec | n_wished | recovered as wished | drawn as received | missed | flip rate (control) |",
              "|---|---|---|---|---|---|"]
    for k in agg["specs"]:
        a = (agg["absences"].get("by_spec") or {}).get(k) or {}
        lines.append(f"| {k} | {_fmt(a.get('n_wished'))} | {_fmt(a.get('recovered_as_wished'))} | "
                     f"{_fmt(a.get('leaked_as_existing'))} | {_fmt(a.get('missed'))} | {_fmt(a.get('flip_rate'))} |")
    sc = ab.get("script_conditional") or {}
    ls = ab.get("leak_split") or {}
    lines += ["", "## Headline claims (primary spec)", "",
              f"- script-conditional share drawn as received (lead): primary source {sc.get('primary_source')}; "
              f"claim_script_conditional_leakage: {sc.get('claim_script_conditional_leakage')}"]
    if agg.get("script_sources"):
        lines.append("  - the family label of a script source (other / own) rests on the model id each call was sent "
                     "with, not on the model the endpoint served (a server may ignore the model field)")
    for name, row in sorted((sc.get("rows") or {}).items()):
        lines.append(f"  - {row.get('label', name)}: scripted minus unscripted share drawn as received {_fmt(row.get('estimate'))} "
                     f"[{_fmt(row.get('ci_lo'))}, {_fmt(row.get('ci_hi'))}], perm p {_fmt(row.get('p_perm'))} "
                     f"(one-sided, {_fmt(row.get('n_perm'))} perms), n scripted/unscripted "
                     f"{_fmt(row.get('n_scripted'))}/{_fmt(row.get('n_unscripted'))}, low power: {row.get('low_power')}")
    lines += [f"- base rate, AI wished share minus endorsed wished share, per map: {_ci(ab.get('base_rate'))}",
              f"- wished-for services drawn as received ({_fmt(ls.get('n_leaked'))}): against their own quote "
              f"(counterfactual_leakage) {_fmt((ls.get('counterfactual_leakage') or {}).get('count'))}, flattened "
              f"(adequacy_flattening) {_fmt((ls.get('adequacy_flattening') or {}).get('count'))}",
              f"- share drawn as received minus control flip, per map (descriptive, not a claim): {_ci(ab.get('paired_diff'))}; "
              f"worse_than_chance_check p: {_fmt((ab.get('worse_than_chance_check') or {}).get('p'))}",
              f"- pull toward the script (canonical labels): {_ci(pull)}; "
              f"claim_pulled_toward_script: {pull.get('claim_pulled_toward_script')}; "
              f"label source: {pull.get('label_source')}",
              *([f"- stability grid incomplete, claims blocked: missing {', '.join(g['missing'])}"]
                if (g := h.get("stability_grid") or {}).get("missing") else []),
              f"- interpretation: {h.get('interpretation', 'n/a')}", "",
              "## Judging", "",
              "| spec | items | not judged | share | interpretation |", "|---|---|---|---|---|"]
    for k in agg["specs"]:
        j = agg["judging"]["by_spec"][k]
        lines.append(f"| {k} | {j['n_items']} | {j['n_not_judged']} | {_fmt(j['not_judged_share'])} | "
                     f"{j['interpretation']} |")
    lines += ["", "## Items by stage", ""]
    lines += [f"- {s}: {json.dumps(c, sort_keys=True)}" for s, c in agg["items"].items()]
    if agg.get("quote_type"):
        lines += [""] + quote_type_markdown(agg["quote_type"])
    if agg.get("traps") is not None:
        n_fail = sum(r["result"] == "FAIL" for r in agg["traps"])
        lines += ["", "## Planted traps", "", f"{len(agg['traps'])} checks, {n_fail} fail (see traps.md). "
                  "Recovering the planted traps tests the code and gives no evidence about the method."]
    text = "\n".join(lines) + "\n"
    if agg.get("ladder"):
        text += "\n" + ladder_markdown(agg["ladder"], synthetic=agg.get("corpus_kind") != "real")
    return text + ("\n" + audits_markdown(audits)) if audits is not None else text


def write_report(cfg, guard=None) -> dict[str, Path]:
    """report.md (and traps.md when the aggregates carry traps) from aggregates.json on disk, with the audit
    summary when ``aggregates/audits.json`` exists. ``guard(path, text)`` (the leak guard of a real corpus) sees
    each text before it is written and raises to refuse the write."""
    paths = out_paths(cfg)
    agg = json.loads(paths["aggregates"].read_text(encoding="utf-8"))
    try:
        audits = json.loads(paths["audits"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        audits = None
    texts = {"report": report_markdown(agg, audits)}
    if agg.get("traps") is not None:
        texts["traps"] = traps_markdown(agg["traps"])
    if guard is not None:
        for name, text in texts.items():
            guard(paths[name], text)
    for name, text in texts.items():
        write_text_atomic(paths[name], text)
    return {name: paths[name] for name in texts}
