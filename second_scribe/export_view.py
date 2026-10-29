"""K8: the view-data exporter (``view-data/1``), the one file the viewer reads for a run group.

``build_view`` opens a finished ``out_dir`` read-only (no client, a log that writes nothing) and assembles, from kit
functions only:

- ``maps``: per participant, the three lanes (the no-interview map of the role, the AI's map of each rung, the
  reference) and the reference ``items`` with their fate per rung. Fates come from the kit's own per-item states,
  ``ladder_metrics.draw_from_pair_inputs(...).items`` over ``pipeline.build_pair_inputs(pipeline.rung_view(r, rung))``,
  for exactly the participants and draw the ladder report counts, so the fates add up to
  ``aggregates.ladder.report.rungs.<arm>.three_way.pooled``.
  Each no-interview node carries the item it matches (``item_id``, the kit's prior-only vs endorsed matching) or
  ``gutter: true``, as the AI nodes do (``_Exporter.script_items``).
- ``evidence``: the AI node ``metrics._absence_structure`` matched to an item, its own quote, the judge's label and the
  line of the hash-chained log that holds the judge call which labelled it (``item_evidence``). A label the judge cache
  served (answered once for an identical element and quote, possibly in another map, and reused) names that earlier
  call and carries ``judge_cached: true`` (``judge_call``).
- ``numbers`` and ``states``: copied from ``aggregates.json`` (the per-rung dot's range is the kit's
  ``stats.bootstrap_ci`` over maps with the ladder's B and seed). A key the aggregates lack stays absent ("not
  computed"), never 0. The profile (``app/profile_view.py``) is the results headline; ``numbers`` feeds the drawer and
  the absent-steps table.
- ``run``: ids, hashes, models and a fresh ``verify_run`` of ``logs/run.jsonl``.

Nothing here calls a model or writes into ``out_dir``; the CLI writes only its ``--out`` file.

Fates (``Item(endorsed_status, state)``): wished+wished ``kept``, wished+experienced ``leaked``, experienced+experienced
``recovered``, experienced+wished ``flipped``, state missed ``missed``, matched only by AI nodes whose quote is not
located ``unattested``. With more than one draw per rung the lanes and fates show draw 0 (``draws`` says how many).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import math
import re
import sys
from pathlib import Path

from . import ladder, ladder_metrics, metrics, pipeline, reference, stats
from .audit.verify import verify_run
from .judge import _render_item, modality_items

SCHEMA = "view-data/1"
SOURCES = ("synthetic", "own", "study-public")
BUILD_SOURCES = ("synthetic", "own")              # the exporter never writes a study-public view (the profile is that)
REFERENCE_TYPES = ("A", "B", "C")
RUNG_DISPLAY_ORDER = ("rung1", "rung2", "placebo", "rung3")
FATES = ("kept", "leaked", "recovered", "flipped", "missed", "unattested")
REFERENCE_STATUSES = ("happened", "missing")
CONTRASTS = ("remedy-placebo", "placebo-rung2", "rung2-rung1")
INTERPRETATIONS = ("incomplete_run", "reported_not_interpreted", "interpreted")
VERIFY_STATUSES = ("not_run", "passed", "failed", "ok")
SCRIPT_SAMPLE = ("p0", 0)                         # the prior-only sample drawn as the no-interview lane: variant, replicate
LOW_POWER_N = metrics.LOW_POWER_N                 # 30
# the app's own low-power rule (the kit's low_power exists only on the script-conditional rows)
LOW_POWER_RULE = {"source": "app", "rung": "rung2", "count": "n_ideals", "below": LOW_POWER_N}
MIRROR_CELLS = (("flip", "flip_rate"), ("false_wished", "false_wished_rate"), ("actual_recall", "actual_recall"),
                ("ai_node_count", "ai_node_count"))
CONTRAST_KEYS = ("n_maps", "n_used", "mean", "ci_lo", "ci_hi", "p_perm", "p_holm")
PULL_KEYS = ("n", "mean", "ci_lo", "ci_hi", "claim_pulled_toward_script", "claim_blocked")
_FATE = {("wished", "wished"): "kept", ("wished", "experienced"): "leaked",
         ("experienced", "experienced"): "recovered", ("experienced", "wished"): "flipped"}
_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def fate_of(endorsed_status: str, state: str) -> str:
    """The fate of one reference item at one rung from the kit's ``Item(endorsed_status, state)``."""
    if state in ("missed", "unattested") and endorsed_status in ("wished", "experienced"):
        return state
    try:
        return _FATE[(endorsed_status, state)]
    except KeyError:
        raise ValueError(f"no fate for endorsed status {endorsed_status!r} and state {state!r}") from None


# ------------------------------------------------------------------------------------------------ opening a run
class _ReadOnlyLog:
    """Stands in for the run's audit log: ``path`` points at it, ``append`` writes nothing."""

    def __init__(self, path: Path):
        self.path = str(path)

    def append(self, record_type, payload):
        return None


def _open_run(cfg: pipeline.RunConfig) -> "pipeline._Run":
    """A ``pipeline._Run`` over ``cfg.out_dir`` that only reads: no client, a log that writes nothing."""
    return pipeline._Run(cfg, {}, _ReadOnlyLog(Path(cfg.out_dir) / "logs" / "run.jsonl"))


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _status_block(x) -> bool:
    """A kit status block such as ``{"status": "no_reference"}`` (a masked number)."""
    return isinstance(x, dict) and set(x) == {"status"}


def _get(d, *path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


# ------------------------------------------------------------------------------------------------ numbers
def leaked_cell(three_way: dict, *, B: int, seed: int) -> dict:
    """``numbers.per_rung.<r>.leaked`` from a rung's ``three_way``: the dot is the kit's per-map mean (so it matches its
    range), the range is ``stats.bootstrap_ci`` over the maps WITH ideals (``bootstrap_ci`` does not drop NaN), None
    below ``ladder_metrics.MIN_MAPS`` such maps; ``x`` of ``n`` is the pooled count."""
    pooled = three_way.get("pooled") or {}
    rates = [m.get("leaked_rate") for m in three_way.get("per_map") or []
             if isinstance(m, dict) and (m.get("n_ideals") or 0) > 0 and _finite(m.get("leaked_rate"))]
    ci = None
    if len(rates) >= ladder_metrics.MIN_MAPS:
        _point, lo, hi = stats.bootstrap_ci(rates, B=B, seed=seed)
        ci = [lo, hi] if _finite(lo) and _finite(hi) else None
    rate = _get(three_way, "map_mean", "leaked_rate")
    return {"x": pooled.get("leaked"), "n": pooled.get("n_ideals"), "n_people": three_way.get("n_maps_with_ideals"),
            "rate": rate if _finite(rate) else None, "ci": ci}


def _rung_cells(summary: dict, *, B: int, seed: int) -> dict:
    out: dict = {}
    tw = summary.get("three_way")
    if _status_block(tw):
        for k in ("leaked", "kept", "missed"):
            out[k] = dict(tw)
    elif isinstance(tw, dict):
        out["leaked"] = leaked_cell(tw, B=B, seed=seed)
        pooled = tw.get("pooled") or {}
        for k in ("kept", "missed"):
            out[k] = {"x": pooled.get(k), "n": pooled.get("n_ideals"), "n_people": tw.get("n_maps_with_ideals")}
    mirror = summary.get("mirror")
    if _status_block(mirror):
        for key, _m in MIRROR_CELLS:
            out[key] = dict(mirror)
    elif isinstance(mirror, dict):
        for key, metric in MIRROR_CELLS:
            m = mirror.get(metric)
            if _status_block(m):
                out[key] = dict(m)
            elif isinstance(m, dict):
                out[key] = {"value": m.get("pooled"), "n_people": m.get("n_maps_used")}
    return out


def _low_power(report: dict) -> bool | None:
    n = _get(report, "rungs", ladder.ARM_OF_RUNG["rung2"], "three_way", "pooled", "n_ideals")
    return bool(n < LOW_POWER_N) if isinstance(n, int) and not isinstance(n, bool) else None


def _numbers(agg: dict, rungs: list[str], *, B: int, seed: int) -> dict:
    out: dict = {}
    lad = agg.get("ladder") if isinstance(agg.get("ladder"), dict) else None
    report = lad.get("report") if lad and isinstance(lad.get("report"), dict) else None
    if report is not None and isinstance(report.get("rungs"), dict):
        per_rung = {}
        for r in rungs:
            summary = report["rungs"].get(ladder.ARM_OF_RUNG[r])
            if isinstance(summary, dict):
                per_rung[r] = _rung_cells(summary, B=B, seed=seed)
        out["per_rung"] = per_rung
    if report is not None and isinstance(report.get("contrasts"), dict):
        contrasts = {}
        for name in CONTRASTS:
            lr = _get(report, "contrasts", name, "metrics", "leaked_rate")
            if _status_block(lr):
                contrasts[name] = dict(lr)
            elif isinstance(lr, dict):
                contrasts[name] = {**{k: lr.get(k) for k in CONTRAST_KEYS}, "n_people": lr.get("n_used")}
        if contrasts:
            out["contrasts"] = contrasts
    if report is not None and isinstance(report.get("verdict"), dict):
        v = report["verdict"]
        verdict = {"code": v.get("verdict"), "reason_codes": list(v.get("reason_codes") or [])}
        if "excludes_leak_drop_larger_than" in v:       # the kit writes it for a null verdict only
            verdict["excludes_leak_drop_larger_than"] = v["excludes_leak_drop_larger_than"]
        low = _low_power(report)
        if low is not None:
            verdict["low_power"] = low
            verdict["low_power_rule"] = dict(LOW_POWER_RULE)
        out["verdict"] = verdict
    if lad and lad.get("interpretation") is not None:
        out["interpretation"] = lad["interpretation"]
    pull = _get(agg, "headline", "pull_toward_script")
    if _status_block(pull):
        out["pull"] = dict(pull)
    elif isinstance(pull, dict):
        out["pull"] = {**{k: pull.get(k) for k in PULL_KEYS}, "n_people": pull.get("n")}
    split = _get(agg, "headline", "absences", "leak_split")
    if isinstance(split, dict):
        out["leak_split"] = json.loads(json.dumps(split))
    return out


def _states(agg: dict) -> dict:
    out: dict = {}
    lad = agg.get("ladder") if isinstance(agg.get("ladder"), dict) else None
    if lad and lad.get("interpretation") is not None:
        out["interpretation"] = lad["interpretation"]
        out["incomplete"] = lad["interpretation"] == "incomplete_run"
    j = _get(agg, "judging", "primary")
    if isinstance(j, dict) and isinstance(j.get("n_items"), int):
        out["not_judged_share"] = j["n_not_judged"] / j["n_items"] if j["n_items"] else None
    low = _low_power(lad.get("report")) if lad and isinstance(lad.get("report"), dict) else None
    if low is not None:
        out["low_power"] = low
    out["needs_judge"] = False
    return out


# ------------------------------------------------------------------------------------------------ the log
class _LogIndex:
    """The run's ``call_request`` records: request_id -> (line index, record hash, item_id, stage, message text; the
    text of ``judge:modality`` calls only), the request ids per judge item, and the ``run_start`` payloads."""

    def __init__(self, path: Path):
        self.by_id: dict[str, tuple] = {}
        self.by_item: dict[str, list[str]] = {}
        self.run_starts: list[dict] = []
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            return
        with fh:
            for idx, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
                    continue
                p = rec["payload"]
                if rec.get("record_type") == "run_start":
                    self.run_starts.append(p)
                if rec.get("record_type") != "call_request" or not isinstance(p.get("request_id"), str):
                    continue
                stage = str(p.get("stage") or "")
                # only the judge's prompts are kept in memory (a reconstruction prompt holds a whole transcript)
                text = ("\n".join(str(m.get("content", "")) for m in p.get("messages") or [] if isinstance(m, dict))
                        if stage.startswith("judge:modality") else "")
                self.by_id[p["request_id"]] = (idx, rec.get("hash"), p.get("item_id"), p.get("stage"), text)
                self.by_item.setdefault(str(p.get("item_id")).rsplit(":", 1)[0], []).append(p["request_id"])


def _item_ends(rest: str) -> bool:
    """The rendered item ends where the match ends: the prompt goes on with a new line that is not a reply line
    (a rendered item carries its own reply, so a reply line after it belongs to a longer item)."""
    return not rest or (rest.startswith("\n") and not rest.startswith("\n   REPLY"))


def find_judge_call(index: _LogIndex, request_ids, it: dict) -> dict:
    """``{request_id, record_index, record_sha256}`` of the judge call that labelled judge item ``it`` (as
    ``judge.modality_items`` builds it): among ``request_ids``, the LAST ``judge:modality`` call_request in the log
    whose prompt lists exactly this item (``judge._render_item``: element, quote with its speaker tag, reply), so a
    corrective retry wins over the attempt it corrected. {} when none is found."""
    tail = _render_item(1, it)[len("1. "):]
    needle = ". " + tail
    best = None
    for rid in dict.fromkeys(request_ids):
        rec = index.by_id.get(rid)
        if rec is None or not str(rec[3] or "").startswith("judge:modality"):
            continue
        text = rec[4]
        at = text.find(needle)
        while at >= 0 and not _item_ends(text[at + len(needle):]):
            at = text.find(needle, at + 1)
        if at >= 0 and (best is None or rec[0] > best[0]):
            best = (rec[0], rec[1], rid)
    if best is None:
        return {}
    return {"request_id": best[2], "record_index": best[0], "record_sha256": best[1]}


def judge_call(index: _LogIndex, own_ids, cached_ids, it: dict) -> dict:
    """The judge call that labelled judge item ``it`` for one map: first among the map's OWN calls (its judge item's
    batches), else among the calls whose labels the judge cache served to it (``stats.cache_request_ids``, which
    ``judge.judge_modality_detailed`` fills on a cache hit: the label was answered once for an identical element,
    quote, speaker and reply, possibly in another map, and reused). A call found among the cache sources carries
    ``judge_cached: true``; an id that is in both lists counts as a cache source. {} when none is found."""
    cached = list(dict.fromkeys(cached_ids))
    got = find_judge_call(index, [i for i in own_ids if i not in set(cached)], it)
    if got:
        return got
    got = find_judge_call(index, cached, it)
    if got:
        got["judge_cached"] = True
    return got


def x_slots(nodes) -> dict[str, int]:
    """{node_id: x_slot}: the rank of each node's x within its phase, ties (and a missing x, ranked last) broken by
    node id. It depends on the reference map only, so an item keeps its place in every lane and rung."""
    by_phase: dict = {}
    for n in nodes:
        by_phase.setdefault(n.phase, []).append(n)
    out = {}
    for group in by_phase.values():
        for k, n in enumerate(sorted(group, key=lambda n: (n.x is None, n.x if n.x is not None else 0.0, n.node_id))):
            out[n.node_id] = k
    return out


# ------------------------------------------------------------------------------------------------ the exporter
class _Exporter:
    """Everything one view reads, computed once per run: the per-rung cells (draw 0 of each participant the ladder
    report counts), the own script source and the log index."""

    def __init__(self, r: "pipeline._Run"):
        self.r, self.cfg = r, r.cfg
        hk = pipeline.headline_kwargs(r.cfg)
        self.setting, self.min_words = hk["primary_setting"], hk["min_words"]
        self.keys = pipeline.ladder_rungs(r.cfg)
        self._cells: dict | None = None
        self._main: dict | None = None
        self._log: _LogIndex | None = None
        self._people = {p.pid: p for p in r.people}

    @property
    def log(self) -> _LogIndex:
        if self._log is None:
            self._log = _LogIndex(Path(self.cfg.out_dir) / "logs" / "run.jsonl")
        return self._log

    def spec0(self) -> str:
        return dataclasses.replace(self.r.specs[0], replicate=0).key()

    def ai_map(self, key: str, pid: str):
        """(draw-0 AI map or None, its recon item status) of one rung."""
        v = pipeline.rung_view(self.r, key)
        item = v.work.get("recon", f"{pid}__{self.spec0()}")
        return v.ai_map(pid, self.spec0()), (item["status"] if item else "not_run")

    def main_inputs(self) -> dict:
        """{pid: PairInputs} of the run itself (``pipeline.build_pair_inputs(r)``), computed once."""
        if self._main is None:
            main, _ex, _j = pipeline.build_pair_inputs(self.r)
            self._main = {p.pid: p for p in main}
        return self._main

    def cells(self) -> dict:
        """{rung: {pid: cell}} for the participants ``pipeline._ladder_section`` puts in the arms (a participant with
        an unmeasurable cell in ANY rung leaves every arm), draw 0. cell = {"p", "spec", "draw", "st"}."""
        if self._cells is not None:
            return self._cells
        r = self.r
        main = list(self.main_inputs().values())
        own = metrics.ScriptSource("own", "own", metrics.scripts_by_role(main)) if main else None
        views, unfinished = {}, set()
        for key in self.keys:
            v = pipeline.rung_view(r, key)
            pl, _ex, _j = pipeline.build_pair_inputs(v)
            by_pid = {p.pid: p for p in pl}
            views[key] = (v, by_pid)
            for s in v.specs:
                for p in r.people:
                    item = v.work.get("recon", f"{p.pid}__{s.key()}")
                    pi = by_pid.get(p.pid)
                    if pi is None:
                        continue
                    if item is None or (item["status"] == "ok" and pi.ai.get(s.key()) is None):
                        unfinished.add(p.pid)
        out: dict = {}
        for key, (v, by_pid) in views.items():
            spec = v.specs[0].key()
            out[key] = {}
            for pid, p in by_pid.items():
                if pid in unfinished:
                    continue
                draw = ladder_metrics.draw_from_pair_inputs(p, spec, 0, self.setting, min_words=self.min_words,
                                                            source=own)
                st = (metrics._absence_structure(p, spec, metrics.spec_arm(spec), self.setting, self.min_words)
                      if p.ai.get(spec) is not None else None)
                out[key][pid] = {"p": p, "spec": spec, "draw": draw, "st": st}
        self._cells = out
        return out

    # ---- evidence
    def _judge_call(self, key: str, pid: str, spec: str, it: dict) -> dict:
        """The log line of the judge call that labelled judge item ``it`` (as ``modality_items`` builds it), by
        ``judge_call``: this map's own calls (the request ids its judge item was stamped with, its batches in the log)
        first, then the calls its cached labels came from (marked ``judge_cached``). {} when none is found."""
        v = pipeline.rung_view(self.r, key)
        name = f"{pid}__{spec}"
        jm = v.work.get("judge_modality", name) or {}
        own = list(jm.get("request_ids") or []) + self.log.by_item.get(f"{name}{v.item_suffix}", [])
        return judge_call(self.log, own, list((jm.get("stats") or {}).get("cache_request_ids") or []), it)

    def evidence(self, key: str, pid: str, item_id: str) -> dict | None:
        cell = self.cells().get(key, {}).get(pid)
        if cell is None or cell["st"] is None:
            return None
        item = next((i for i in cell["draw"].items if i.item_id == item_id), None)
        if item is None or item.state == "missed":
            return None
        st, p, spec = cell["st"], cell["p"], cell["spec"]
        near, far = st["loc"].get(item_id, []), st["unloc"].get(item_id, [])
        if item.state == "wished":
            aid = next((a for a in near if st["status"][a] == "wished"), None)
        elif item.state == "experienced":
            aid = near[0] if near else None
        else:                                    # unattested: matched only by AI nodes whose quote is not located
            aid = next((a for a in far if st["status"][a] == "wished"), far[0] if far else None)
        if aid is None:
            return None
        g = p.ai[spec]
        node = g.node(aid)
        out: dict = {"ai_node_id": aid}
        if node.quote:
            out["quote"] = node.quote
        prov = p.prov.get(spec, {}).get(aid)
        if prov is not None and prov.found and prov.speaker:
            out["speaker"] = prov.speaker
        if aid in st["nj"]:
            out["not_judged"] = True
        elif st["mod"].get(aid):
            out["judge_label"] = st["mod"][aid]
        if fate_of(item.endorsed_status, item.state) == "leaked":
            out["leak_split"] = metrics.leak_split(st, item_id)
        if node.quote and "judge_label" in out:
            t = self.r.transcript(self._people[pid])
            its = [x for x in modality_items(g, provenance=p.prov.get(spec), transcript=t) if x["element_id"] == aid]
            if its:
                out.update(self._judge_call(key, pid, spec, its[0]))
        return out

    # ---- the run block
    def run_block(self, agg: dict, *, ai_maps_only: bool) -> dict:
        cfg = self.cfg
        start = self.log.run_starts[-1] if self.log.run_starts else {}
        out: dict = {"run_id": cfg.run_id}
        code = agg.get("code_sha256") or start.get("code_sha256")      # K6 (Task 15); absent until it is recorded
        if isinstance(code, str) and _HEX64.fullmatch(code):
            out["code_sha256"] = code
        for k in ("config_sha256", "prompt_set_sha256"):
            val = agg.get(k) or start.get(k)
            if val is not None:
                out[k] = val
        models = agg.get("models") if isinstance(agg.get("models"), dict) else self.r.recorded_models()
        out["models"] = {k: models[k] for k in ("recon", "judge") if models.get(k)
                         and not (ai_maps_only and k == "judge")}
        checked_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"
        if not log_path.exists():
            out["verify"] = {"status": "not_run", "records": 0, "checked_at": checked_at, "errors": ["log_missing"]}
            return out
        ok, detail = verify_run(log_path)
        if detail.get("head_hash"):
            out["chain_head"] = detail["head_hash"]
        out["verify"] = {"status": "passed" if ok else "failed", "records": detail.get("records", 0),
                         "checked_at": checked_at, "errors": [str(e) for e in detail.get("errors", [])]}
        return out

    # ---- maps
    def _nodes(self, g) -> list[dict]:
        return [{"node_id": n.node_id, "label": n.text, "phase": n.phase} for n in g.nodes] if g is not None else []

    def _script_key(self) -> str | None:
        if (k := self.r.reading3_key()) is not None:      # a reading3 block: the lane is that map (the lead row's)
            return k
        return next((s.key() for s in self.r.prior_specs if (s.variant, s.replicate) == SCRIPT_SAMPLE), None)

    def script_lane(self, role: str) -> dict:
        want = self._script_key()
        if self.r.reading3_key() is not None:
            g = self.r.reading3_map(role)
        else:
            g = self.r.prior_maps(role).get(want) if want else None
        return {"nodes": self._nodes(g)} if g is not None else {"nodes": [], "status": "not_run"}

    def script_items(self, pid: str, item_ids) -> dict[str, str] | None:
        """{no-interview node id: item id}: the reference item each node of the drawn prior-only sample matches, by the
        kit's own matcher (``metrics.match_nodes`` over that sample and the endorsed map, with the run's
        ``judged_prior_vs_endorsed`` alignment and the primary setting); a node matching several items takes the first
        in endorsed-map order. None when the alignment is missing or belongs to another sample (nothing is guessed)."""
        p = self.main_inputs().get(pid)
        want = self._script_key()
        if p is None or want is None or p.prior_spec != want or p.judged_prior_vs_endorsed is None \
                or p.ai.get(want) is None:
            return None
        ids = set(item_ids)
        order = {n.node_id: k for k, n in enumerate(p.endorsed.nodes)}
        m = metrics.match_nodes(p.ai[want], p.endorsed, self.setting, p.judged_prior_vs_endorsed)
        out: dict[str, str] = {}
        for a, b in sorted(m.pairs, key=lambda ab: (order.get(ab[1], len(order)), ab)):
            if b in ids:
                out.setdefault(a, b)
        return out

    def map_entry(self, p, rungs: list[str], *, match: bool) -> dict:
        e = self.r.endorsed(p)
        lanes: dict = {"script": self.script_lane(p.role), "reference": {"nodes": self._nodes(e)}, "ai": {}}
        it = self.r.work.get("in_transcript", p.pid) if match else None
        seen = (it or {}).get("in_transcript") if (it or {}).get("status") == "ok" else None
        items: list = []
        off: list[dict] = []
        kinds = (e.meta or {}).get("missing_kinds") or {}
        if isinstance(seen, dict):
            for n in e.nodes:
                row = {"item_id": n.node_id, "label": n.text, "phase": n.phase,
                       "reference_status": "missing" if n.status == "wished" else "happened"}
                if seen.get(n.node_id):
                    items.append((n, row))
                else:
                    off.append({"node_id": n.node_id, "label": n.text, "phase": n.phase,
                                "reference_status": row["reference_status"]})
        # x_slot: rank of the item's x within its phase (ties by node id), the same in every lane and rung
        rank = x_slots([n for n, _row in items])
        cells = self.cells() if match and items else {}
        flags: dict[str, set] = {}
        rows = []
        for n, row in items:
            row["x_slot"] = rank[n.node_id]
            kind = kinds.get(n.node_id) if isinstance(kinds, dict) else None
            if kind is not None:
                row["missing_kind"] = kind
            row["fate"], row["evidence"] = {}, {}
            rows.append(row)
        by_id = {row["item_id"]: row for row in rows}
        if rows and lanes["script"]["nodes"]:
            on = self.script_items(p.pid, by_id)
            if on is not None:              # each no-interview node sits on the item it matches, or in the gutter
                for nd in lanes["script"]["nodes"]:
                    if nd["node_id"] in on:
                        nd["item_id"] = on[nd["node_id"]]
                    else:
                        nd["gutter"] = True
        for key in rungs:
            g, status = self.ai_map(key, p.pid)
            cell = cells.get(key, {}).get(p.pid)
            node_item: dict[str, str] = {}
            if cell is not None:
                draw, st = cell["draw"], cell["st"]
                for i in draw.items:
                    if i.item_id in by_id:
                        by_id[i.item_id]["fate"][key] = fate_of(i.endorsed_status, i.state)
                        ev = self.evidence(key, p.pid, i.item_id)
                        if ev:
                            by_id[i.item_id]["evidence"][key] = ev
                if st is not None:
                    for eid in (x for x, _s in st["ends"] if x in by_id):
                        for a in st["loc"].get(eid, []) + st["unloc"].get(eid, []):
                            node_item.setdefault(a, eid)
                if draw.script_units:
                    matched = [i.item_id for i in draw.items
                               if i.endorsed_status == "wished" and i.state in ("wished", "experienced")]
                    for iid, (scripted, _leaked) in zip(matched, draw.script_units):
                        flags.setdefault(iid, set()).add(bool(scripted))
            nodes = self._nodes(g)
            if cell is not None and cell["st"] is not None:
                for nd in nodes:
                    if nd["node_id"] in node_item:
                        nd["item_id"] = node_item[nd["node_id"]]
                    else:
                        nd["gutter"] = True
            lanes["ai"][key] = {"nodes": nodes} if g is not None else {"nodes": [], "status": status}
        for row in rows:
            f = flags.get(row["item_id"])
            row["script_has"] = next(iter(f)) if f and len(f) == 1 else None
            if not row["evidence"]:
                del row["evidence"]
        return {"pid": p.pid, "role": p.role, "lanes": lanes, "items": rows, "off_record": off}


def _exporter_for(r) -> _Exporter:
    ex = r.__dict__.get("_export_view")
    if ex is None:
        ex = r.__dict__["_export_view"] = _Exporter(r)
    return ex


def item_evidence(r: "pipeline._Run", rung: str, pid: str, item_id: str) -> dict | None:
    """The AI node ``metrics._absence_structure`` matched to reference item ``item_id`` of ``pid`` at ``rung`` (draw 0):
    ``ai_node_id``, its ``quote``, ``speaker`` and ``judge_label`` (``not_judged`` instead when the judge gave none),
    ``leak_split`` for a leaked item, and the log line of the judge call that labelled it (``request_id``,
    ``record_index`` = 0-based line of ``logs/run.jsonl``, ``record_sha256`` = that record's hash; ``judge_cached: true``
    when the label was reused from that earlier identical call through the judge cache). None when the item
    is missed at that rung, unknown, or not counted by the ladder report."""
    return _exporter_for(r).evidence(rung, pid, item_id)


def _resolve_reference_type(cfg, reference_type):
    got = reference.reference_type(cfg)
    if reference_type is not None and reference_type not in REFERENCE_TYPES:
        raise ValueError(f"reference_type must be one of {REFERENCE_TYPES} or None, got {reference_type!r}")
    if reference_type is None:
        return got
    if got is None and reference_type != "A":
        raise ValueError(f"reference type {reference_type!r} needs a tool run with tool.reference_type set")
    if got is not None and got != reference_type:
        raise ValueError(f"reference type {reference_type!r} differs from the run's tool.reference_type {got!r}")
    return reference_type


def build_view(cfg: pipeline.RunConfig, *, source: str, reference_type: str | None,
               ai_maps_only: bool = False) -> dict:
    """The ``view-data/1`` document of the run in ``cfg.out_dir`` (see the module docstring). ``source`` is
    ``"synthetic"`` or ``"own"``. ``ai_maps_only`` (one model family, no judge): the AI's maps per rung from the
    recon stores, no items, ``numbers`` = ``per_rung.<r>.ai_node_count`` counted from those maps, no judge model,
    ``states.needs_judge``. Makes no model call and writes nothing."""
    if source not in BUILD_SOURCES:
        raise ValueError(f"source must be one of {BUILD_SOURCES}, got {source!r}")
    ref_type = _resolve_reference_type(cfg, reference_type)
    r = _open_run(cfg)
    ex = _exporter_for(r)
    agg_path = Path(cfg.out_dir) / "aggregates" / "aggregates.json"
    agg: dict = {}
    if not ai_maps_only and agg_path.exists():
        agg = json.loads(agg_path.read_text(encoding="utf-8"))
    report_rungs = _get(agg, "ladder", "report", "rungs")
    rungs = [k for k in RUNG_DISPLAY_ORDER if k in ex.keys
             and (not isinstance(report_rungs, dict) or ladder.ARM_OF_RUNG[k] in report_rungs)]
    view: dict = {"schema": SCHEMA, "source": source, "reference_type": ref_type,
                  "run": ex.run_block(agg, ai_maps_only=ai_maps_only),
                  "phases": list(cfg.context.get("phases") or []),
                  "rungs": rungs, "arm_of_rung": {k: ladder.ARM_OF_RUNG[k] for k in rungs}}
    if cfg.ladder:
        view["draws"] = int(cfg.ladder["draws"])
    match = not ai_maps_only and ref_type != "C"
    view["maps"] = [ex.map_entry(p, rungs, match=match) for p in r.people]
    if ai_maps_only:
        per_rung = {}
        for k in rungs:
            counts = [len(m["lanes"]["ai"][k]["nodes"]) for m in view["maps"] if "status" not in m["lanes"]["ai"][k]]
            per_rung[k] = {"ai_node_count": {"value": sum(counts) / len(counts) if counts else None,
                                             "x": sum(counts), "n": len(counts), "n_people": len(counts)}}
        view["numbers"] = {"per_rung": per_rung}
        view["states"] = {"needs_judge": True}
    else:
        prm = pipeline.ladder_params(cfg)
        view["numbers"] = _numbers(agg, rungs, B=prm["B"], seed=prm["seed"])
        view["states"] = _states(agg)
    return view


# ------------------------------------------------------------------------------------------------ validation
def _check_nodes(where: str, lane, item_ids: set, errors: list) -> None:
    if not isinstance(lane, dict) or not isinstance(lane.get("nodes"), list):
        errors.append(f"{where}: lane must be an object with a nodes list")
        return
    for k, n in enumerate(lane["nodes"]):
        w = f"{where}.nodes[{k}]"
        if not isinstance(n, dict) or not isinstance(n.get("node_id"), str):
            errors.append(f"{w}: node must be an object with a string node_id")
            continue
        if "item_id" in n:
            if n["item_id"] not in item_ids:
                errors.append(f"{w}: item_id {n['item_id']!r} is not an item of this map")
            if n.get("gutter"):
                errors.append(f"{w}: a node with an item_id is not in the gutter")
        if "gutter" in n and n["gutter"] is not True:
            errors.append(f"{w}: gutter must be true when present")


def _check_map(i: int, m, rungs: list, errors: list) -> None:
    w = f"maps[{i}]"
    if not isinstance(m, dict):
        errors.append(f"{w}: must be an object")
        return
    if not isinstance(m.get("pid"), str):
        errors.append(f"{w}.pid: must be a string")
    items = m.get("items")
    if not isinstance(items, list):
        errors.append(f"{w}.items: must be a list")
        items = []
    ids: set = set()
    slots: set = set()
    for k, it in enumerate(items):
        wi = f"{w}.items[{k}]"
        if not isinstance(it, dict) or not isinstance(it.get("item_id"), str):
            errors.append(f"{wi}: item must be an object with a string item_id")
            continue
        if it["item_id"] in ids:
            errors.append(f"{wi}: duplicate item_id {it['item_id']!r}")
        ids.add(it["item_id"])
        if it.get("reference_status") not in REFERENCE_STATUSES:
            errors.append(f"{wi}.reference_status: must be one of {REFERENCE_STATUSES}")
        if "script_has" in it and it["script_has"] not in (True, False, None):
            errors.append(f"{wi}.script_has: must be true, false or null")
        xs = it.get("x_slot")
        if not isinstance(xs, int) or isinstance(xs, bool) or xs < 0:
            errors.append(f"{wi}.x_slot: must be an integer >= 0")
        elif (it.get("phase"), xs) in slots:
            errors.append(f"{wi}.x_slot: another item of phase {it.get('phase')!r} has slot {xs}")
        else:
            slots.add((it.get("phase"), xs))
        if "missing_kind" in it and not isinstance(it["missing_kind"], str):
            errors.append(f"{wi}.missing_kind: must be a string")
        fate = it.get("fate", {})
        if not isinstance(fate, dict):
            errors.append(f"{wi}.fate: must be an object")
            fate = {}
        for r, f in fate.items():
            if r not in rungs:
                errors.append(f"{wi}.fate: {r!r} is not a rung of this view")
            if f not in FATES:
                errors.append(f"{wi}.fate.{r}: {f!r} is not one of {FATES}")
        evd = it.get("evidence", {})
        if not isinstance(evd, dict):
            errors.append(f"{wi}.evidence: must be an object")
            evd = {}
        for r, e in evd.items():
            if r not in rungs:
                errors.append(f"{wi}.evidence: {r!r} is not a rung of this view")
            if not isinstance(e, dict) or not isinstance(e.get("ai_node_id"), str):
                errors.append(f"{wi}.evidence.{r}: must be an object with a string ai_node_id")
            elif "record_index" in e and (not isinstance(e["record_index"], int) or e["record_index"] < 0):
                errors.append(f"{wi}.evidence.{r}.record_index: must be an integer >= 0")
            elif "judge_cached" in e and (e["judge_cached"] is not True
                                          or not all(k in e for k in ("request_id", "record_index", "record_sha256"))):
                errors.append(f"{wi}.evidence.{r}.judge_cached: when present it is true and the entry names the "
                              f"reused call (request_id, record_index, record_sha256)")
    lanes = m.get("lanes")
    if not isinstance(lanes, dict):
        errors.append(f"{w}.lanes: must be an object")
        return
    for name in ("script", "reference"):
        if name in lanes:
            _check_nodes(f"{w}.lanes.{name}", lanes[name], ids | {None}, errors)
    ai = lanes.get("ai", {})
    if not isinstance(ai, dict):
        errors.append(f"{w}.lanes.ai: must be an object")
        return
    for r, lane in ai.items():
        if r not in rungs:
            errors.append(f"{w}.lanes.ai: {r!r} is not a rung of this view")
        _check_nodes(f"{w}.lanes.ai.{r}", lane, ids, errors)
    if not isinstance(m.get("off_record", []), list):
        errors.append(f"{w}.off_record: must be a list")


def validate_view(view: dict) -> list[str]:
    """Problems with a ``view-data/1`` document (empty = valid): the schema, the enums (source, reference type, rungs
    and their arms, fates, reference statuses, interpretations, verify status), one place per item (unique item ids,
    one ``x_slot`` per phase slot, every lane node that names an item names one of the map's items, so an item has the
    same x in every lane and rung), and no ``maps`` in a ``study-public`` view. ``numbers`` and ``states`` may be
    absent (not computed); unknown extra keys are allowed."""
    errors: list[str] = []
    if not isinstance(view, dict):
        return ["view: must be an object"]
    if view.get("schema") != SCHEMA:
        errors.append(f"schema: must be {SCHEMA!r}")
    if view.get("source") not in SOURCES:
        errors.append(f"source: must be one of {SOURCES}")
    if view.get("reference_type") not in REFERENCE_TYPES + (None,):
        errors.append(f"reference_type: must be one of {REFERENCE_TYPES} or null")
    if "test_pattern" in view and not isinstance(view["test_pattern"], bool):
        errors.append("test_pattern: must be a boolean")
    rungs = view.get("rungs")
    if not isinstance(rungs, list) or any(r not in RUNG_DISPLAY_ORDER for r in rungs) or len(set(rungs)) != len(rungs):
        errors.append(f"rungs: must be a list of distinct rung keys from {RUNG_DISPLAY_ORDER}")
        rungs = [r for r in rungs if r in RUNG_DISPLAY_ORDER] if isinstance(rungs, list) else []
    arms = view.get("arm_of_rung")
    if not isinstance(arms, dict) or set(arms) != set(rungs) or any(arms[k] != ladder.ARM_OF_RUNG[k] for k in arms
                                                                     if k in ladder.ARM_OF_RUNG):
        errors.append("arm_of_rung: must map each rung to the kit's arm (ladder.ARM_OF_RUNG)")
    if not isinstance(view.get("phases"), list) or not all(isinstance(p, str) for p in view["phases"]):
        errors.append("phases: must be a list of strings")
    run_ = view.get("run")
    if run_ is not None:
        if not isinstance(run_, dict):
            errors.append("run: must be an object")
        else:
            st = _get(run_, "verify", "status")
            if st is not None and st not in VERIFY_STATUSES:
                errors.append(f"run.verify.status: must be one of {VERIFY_STATUSES}")
    if view.get("source") == "study-public":
        if "maps" in view:
            errors.append("maps: a study-public view carries numbers and states only")
    else:
        maps = view.get("maps")
        if not isinstance(maps, list):
            errors.append("maps: must be a list")
        else:
            for i, m in enumerate(maps):
                _check_map(i, m, rungs, errors)
    numbers = view.get("numbers")
    if numbers is not None:
        if not isinstance(numbers, dict):
            errors.append("numbers: must be an object")
        else:
            pr = numbers.get("per_rung", {})
            if not isinstance(pr, dict) or any(r not in rungs for r in pr):
                errors.append("numbers.per_rung: keys must be rungs of this view")
            cs = numbers.get("contrasts", {})
            if not isinstance(cs, dict) or any(c not in CONTRASTS for c in cs):
                errors.append(f"numbers.contrasts: keys must be among {CONTRASTS}")
            if "interpretation" in numbers and numbers["interpretation"] not in INTERPRETATIONS:
                errors.append(f"numbers.interpretation: must be one of {INTERPRETATIONS}")
            if "verdict" in numbers and not isinstance(numbers["verdict"], dict):
                errors.append("numbers.verdict: must be an object")
    states = view.get("states")
    if states is not None:
        if not isinstance(states, dict):
            errors.append("states: must be an object")
        else:
            if "interpretation" in states and states["interpretation"] not in INTERPRETATIONS:
                errors.append(f"states.interpretation: must be one of {INTERPRETATIONS}")
            for k in ("incomplete", "low_power", "needs_judge"):
                if k in states and not isinstance(states[k], bool):
                    errors.append(f"states.{k}: must be a boolean")
    return errors


# ------------------------------------------------------------------------------------------------ output
def view_to_js(view: dict, var: str) -> str:
    """``window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["<var>"] = <json>;`` on one line, safe inside a
    ``<script>`` element: ``</`` becomes ``<\\/``, ``<!--`` becomes ``\\u003c!--`` and U+2028 / U+2029 are escaped."""
    if not isinstance(var, str) or not _VAR_RE.fullmatch(var):
        raise ValueError("var must match ^[A-Za-z_][A-Za-z0-9_]*$")
    body = json.dumps(view, ensure_ascii=False, separators=(", ", ": "), allow_nan=False)
    body = body.replace("<!--", "\\u003c!--").replace("</", "<\\/")
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return f'window.SS_DATA = window.SS_DATA || {{}}; window.SS_DATA["{var}"] = {body};'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m second_scribe.export_view",
                                 description="Write the view-data/1 file of a finished run (no model call).")
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--source", required=True, choices=BUILD_SOURCES)
    ap.add_argument("--reference-type", choices=REFERENCE_TYPES, default=None)
    ap.add_argument("--ai-maps-only", action="store_true")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    cfg = pipeline.RunConfig.load(a.config)
    try:
        view = build_view(cfg, source=a.source, reference_type=a.reference_type, ai_maps_only=a.ai_maps_only)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    problems = validate_view(view)
    if problems:
        for p in problems:
            print(f"invalid: {p}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    pipeline.write_json_atomic(a.out, view, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
