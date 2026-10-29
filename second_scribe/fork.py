"""Fork a finished Q out_dir into a NEW out_dir for the judge phase, optionally for a few participants only.

``build_fork(config, out, pairs=None, script_samples=None, sample=None, seed=0)`` writes ``<out>/config.json`` and ``<out>/out/`` (the new
out_dir) and returns counts. The source out_dir is only read.

How the provenance is kept (the checks of ``verify`` pass on the fork without any of them being relaxed):
- ``<out>/out/logs/run.jsonl`` starts as a byte copy of the source log (and its manifest): every reconstructor call, every
  ``work_item`` announcement and every ``run_start`` of the Q phase stays on record with its original hashes.
- One ``run_fork`` record is appended. It names the source (record count, head hash, manifest hash, config and
  prompt-set hashes), the kept pairs, the counts, and DECLARES the hashes the forked run will run with (config,
  prompt set, quote rule). ``prereg.restart_problem`` and the run-validity check compare later sessions with those.
- The Q items of the kept pairs are byte copies (their logged sha256 still matches); every Q item of a pair that is not
  kept gets a ``work_item`` removal record, so the work store and the log agree.
- The new config is the source config with the participant tables cut to the kept pairs (``--script-samples`` may also
  lower the samples of the script sources, for a pilot only), the same ``run_id`` (it seeds the quote shuffle and the
  canonicalization order, so a pilot judges what the full run would judge), ``out_dir`` = ``<out>/out`` and
  ``base_dir`` = the source config's folder.

Refused (exit 2; nothing is written, except that a source log that grows while it is being copied leaves a partial
fork folder to delete) when: the source log does not verify; the source work store holds anything but the
Q stores (a judge item, a judge cache, an unknown file); the source log already holds a fork; the quote rule of the
code now differs from the source run's; a kept pair is not in the source config or lacks its mapping-talk item or a
done item for a main spec; ``out`` exists and is not empty, or lies inside the source out_dir (or the reverse).

``sample`` draws the pairs: n participants proportional to the roles of the config, seeded (``draw_pairs``); the ids
go only into the new config and the fork record, never to the screen.

Prints counts only: stores, items kept and dropped, pairs per role. Never a label, quote or transcript text.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import pipeline, prereg, textnorm
from .audit import verify as audit_verify
from .audit.manifest import compute_manifest_hash, manifest_path_for, read_manifest
from .audit.registry import AuditLog
from .paths import stage_dirname, stage_from_dirname

Q_STORES = ("mapping_talk_confirm", "recon", "prior_only")     # plus the ladder stores recon:<rung>
ROOT_FILES = ("models.json",)
PID_TABLES = ("pairs", "roles", "participant_labels", "interviewer_labels", "interviewers")


class ForkRefused(RuntimeError):
    """The fork cannot be made honestly; nothing was written."""


def _is_q_store(name: str) -> bool:
    return name in Q_STORES or prereg._ladder_rung(name) is not None


def _owner(store: str, name: str) -> tuple[str, str]:
    """("pid", <pid>) for a per-participant Q item, ("role", <role>) for a prior-only item."""
    if store == "prior_only":
        return "role", name.split("__", 1)[0]
    return "pid", name if store == "mapping_talk_confirm" else name.split("__", 1)[0]


def _inside(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def _source_checks(cfg: pipeline.RunConfig) -> tuple[Path, list[dict], dict]:
    out = Path(cfg.out_dir)
    log = out / "logs" / "run.jsonl"
    if not log.is_file():
        raise ForkRefused("the source out_dir has no logs/run.jsonl")
    ok, detail = audit_verify.verify_run(log)
    if not ok:
        raise ForkRefused(f"the source log does not verify ({detail.get('reason')})")
    work = out / "work"
    if (why := prereg.run_validity_problem(log, work) or audit_verify.work_integrity_problem(log, work)):
        raise ForkRefused(f"the source run is not valid ({why})")
    records = prereg._records(log) or []
    if any(r.get("record_type") == prereg.FORK_RECORD for r in records):
        raise ForkRefused("the source log is itself a fork; fork the original Q out_dir")
    return log, records, read_manifest(manifest_path_for(log))


def _source_items(work: Path) -> dict[str, list[str]]:
    """{store: [item names]} of the source work store; refuses anything that is not a finished Q store."""
    if not work.is_dir():
        raise ForkRefused("the source out_dir has no work store")
    stores: dict[str, list[str]] = {}
    for p in sorted(work.iterdir()):
        if p.is_dir():
            store = stage_from_dirname(p.name)       # the stage key (on Windows the folder has no colon)
            if not _is_q_store(store):
                raise ForkRefused(f"the source work store holds a {store!r} store: fork only a Q-phase out_dir "
                                  "(no judge item yet)")
            extra = [f for f in p.iterdir() if f.suffix != ".json"]
            if extra:
                raise ForkRefused(f"the source store {store} holds {len(extra)} file(s) that are not items")
            stores[store] = sorted(f.stem for f in p.glob("*.json"))
        elif p.name not in ROOT_FILES:
            raise ForkRefused(f"the source work store holds an unexpected file {p.name!r} (a judge cache or other)")
    return stores


def _new_config(raw: dict, cfg: pipeline.RunConfig, keep: list[str], out_dir: Path, script_samples) -> dict:
    d = json.loads(json.dumps(raw))
    corpus = d["corpus"]
    for key in PID_TABLES:
        table = corpus.get(key)
        if isinstance(table, dict):
            corpus[key] = {k: v for k, v in table.items() if k in keep or k == "*"}
    if script_samples is not None:
        for src in d.get("script_sources") or []:
            src["samples"] = int(script_samples)
    d["out_dir"] = str(out_dir)
    d["base_dir"] = str(Path(cfg.base_dir).resolve())
    return d


def _q_complete(work: pipeline.Work, cfg: pipeline.RunConfig, pid: str) -> bool:
    return (work.get("mapping_talk_confirm", pid) is not None
            and all(work.get("recon", f"{pid}__{spec.key()}") is not None for spec in cfg.recon_specs()))


def draw_pairs(people: dict, eligible: set[str], n: int, seed: int) -> list[str]:
    """A seeded draw of ``n`` participants, proportional to the roles of ALL participants of the config (largest
    remainder, ties to the role that sorts first), each role drawn with ``random.Random(seed)`` from its sorted
    eligible ids (roles in sorted order, one generator). Raises ForkRefused when a role has too few eligible ids."""
    import random
    if not 1 <= n <= len(people):
        raise ForkRefused(f"--sample must be between 1 and {len(people)}")
    by_role: dict[str, list[str]] = {}
    for pid, p in sorted(people.items()):
        by_role.setdefault(p.role, []).append(pid)
    quota = {r: n * len(ids) / len(people) for r, ids in sorted(by_role.items())}
    alloc = {r: int(q) for r, q in quota.items()}
    for r in sorted(quota, key=lambda r: (-(quota[r] - alloc[r]), r))[:n - sum(alloc.values())]:
        alloc[r] += 1
    rng, out = random.Random(seed), []
    for r in sorted(by_role):
        pool = [pid for pid in by_role[r] if pid in eligible]
        if len(pool) < alloc[r]:
            raise ForkRefused(f"role {r}: {alloc[r]} to draw but only {len(pool)} with a finished Q phase")
        out += rng.sample(pool, alloc[r])
    return sorted(out)


def build_fork(config, out, pairs: list[str] | None = None, script_samples: int | None = None,
               sample: int | None = None, seed: int = 0) -> dict:
    """Make the fork (see the module docstring). ``pairs`` (ids) or ``sample`` (a seeded draw proportional to the
    roles, ``draw_pairs``) choose the participants; neither: all of them. Returns the counts that ``main`` prints."""
    if pairs and sample is not None:
        raise ForkRefused("give --pairs or --sample, not both")
    config, out = Path(config).resolve(), Path(out).resolve()
    raw = json.loads(config.read_text(encoding="utf-8"))
    cfg = pipeline.RunConfig.load(config)
    src_out = Path(cfg.out_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise ForkRefused("the fork folder exists and is not empty")
    if _inside(out, src_out) or _inside(src_out, out):
        raise ForkRefused("the fork folder and the source out_dir must not contain each other")
    if script_samples is not None and (isinstance(script_samples, bool) or int(script_samples) < 1):
        raise ForkRefused("--script-samples must be at least 1")
    people = {p.pid: p for p in pipeline.load_participants(cfg)}
    if sample is not None:
        src_work = pipeline.Work(src_out)
        pairs = draw_pairs(people, {pid for pid in people if _q_complete(src_work, cfg, pid)}, int(sample), int(seed))
    keep = sorted(people) if not pairs else sorted(dict.fromkeys(pairs))
    missing = [p for p in keep if p not in people]
    if missing:
        raise ForkRefused(f"{len(missing)} of the pairs asked for are not in the source config")
    log, records, manifest = _source_checks(cfg)
    starts = prereg.session_starts(records)
    src_quote = {p.get("quote_rule_sha256") for p in starts} - {None}
    if src_quote and src_quote != {textnorm.quote_rule_sha256()}:
        raise ForkRefused("the quote rule of this code differs from the one the source run's Q phase used")
    work = src_out / "work"
    stores = _source_items(work)
    kept_roles = sorted({people[p].role for p in keep})
    src_work = pipeline.Work(src_out)
    gaps = sum(not _q_complete(src_work, cfg, pid) for pid in keep)
    if gaps:
        raise ForkRefused(f"{gaps} kept pair(s) lack a done mapping-talk or main-grid item in the source; finish the "
                          "Q phase first")

    new_out = out / "out"
    new_raw = _new_config(raw, cfg, keep, new_out, script_samples)
    out.mkdir(parents=True, exist_ok=True)
    new_config = out / "config.json"
    new_config.write_text(json.dumps(new_raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ncfg = pipeline.RunConfig.load(new_config)

    (new_out / "logs").mkdir(parents=True)
    new_log = new_out / "logs" / "run.jsonl"
    shutil.copyfile(log, new_log)
    shutil.copyfile(manifest_path_for(log), manifest_path_for(new_log))
    alog = AuditLog(new_log, cfg.run_id)            # verifies the copied chain against the copied manifest
    count, head = alog.head()
    if count != manifest["record_count"] or head != manifest["head_hash"]:
        raise ForkRefused("the source log changed while it was being copied; try again when nothing writes to it")

    kept: dict[str, int] = {}
    dropped: dict[str, int] = {}
    plan: list[tuple[str, str, bool]] = []
    for store, names in stores.items():
        for name in names:
            kind, owner = _owner(store, name)
            stay = owner in (keep if kind == "pid" else kept_roles)
            plan.append((store, name, stay))
            (kept if stay else dropped)[store] = (kept if stay else dropped).get(store, 0) + 1
    src_starts = {p.get("prompt_set_sha256") for p in starts} - {None}
    src_configs = {p.get("config_sha256") for p in starts} - {None}
    alog.append(prereg.FORK_RECORD, {
        "source_records": count, "source_head_hash": head,
        "source_manifest_sha256": compute_manifest_hash(manifest),
        "source_config_sha256": sorted(src_configs)[0] if len(src_configs) == 1 else None,
        "source_prompt_set_sha256": sorted(src_starts)[0] if len(src_starts) == 1 else None,
        "config_sha256": ncfg.sha256(), "prompt_set_sha256": pipeline.prompt_set_sha256(),
        "quote_rule_sha256": textnorm.quote_rule_sha256(),
        "pairs": keep, "n_pairs_source": len(people), "roles": kept_roles,
        "script_samples": script_samples, "kept": kept, "dropped": dropped,
        "sample": None if sample is None else {"n": int(sample), "seed": int(seed),
                                                "method": "per-role largest remainder; random.Random(seed).sample "
                                                          "of each role's sorted eligible ids, roles sorted"},
        "code_commit": prereg.git_head(prereg.KIT_ROOT)})
    new_work = new_out / "work"
    for store, name, stay in plan:
        if stay:
            dst = new_work / stage_dirname(store) / f"{name}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / stage_dirname(store) / f"{name}.json", dst)
        else:
            audit_verify.log_work_item(alog, store, name, None)
    for f in ROOT_FILES:
        if (work / f).is_file():
            new_work.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / f, new_work / f)
    if len(keep) == len(people):     # the Q audits describe exactly these pairs: the report keeps them
        q_audits = src_out / "audits" / "Q.json"
        if q_audits.is_file():
            (new_out / "audits").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(q_audits, new_out / "audits" / "Q.json")
    alog.close()

    ok, detail = audit_verify.verify_run(new_log)
    problem = None if ok else detail.get("reason")
    problem = problem or prereg.run_validity_problem(new_log, new_work) or audit_verify.work_integrity_problem(
        new_log, new_work)
    return {"config": str(new_config), "out_dir": str(new_out), "pairs": len(keep), "pairs_source": len(people),
            "roles": {r: sum(1 for p in keep if people[p].role == r) for r in kept_roles},
            "kept": kept, "dropped": dropped, "source_records": count,
            "script_samples": script_samples, "ladder_missing": pipeline.ladder_missing(ncfg),
            "verify": "PASS" if problem is None else f"FAIL: {problem}"}


def main(a) -> int:
    pairs = [p.strip() for p in a.pairs.split(",") if p.strip()] if a.pairs else None
    try:
        res = build_fork(a.config, a.out, pairs, a.script_samples, a.sample, a.seed)
    except ForkRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"fork: {res['pairs']} of {res['pairs_source']} pairs "
          f"({', '.join(f'{r} {n}' for r, n in res['roles'].items())}); source log {res['source_records']} records")
    for store in sorted(set(res["kept"]) | set(res["dropped"])):
        print(f"  {store:<24} kept {res['kept'].get(store, 0):>5}   dropped {res['dropped'].get(store, 0):>5}")
    if res["script_samples"] is not None:
        print(f"  script sources: samples set to {res['script_samples']} (pilot only)")
    if res["ladder_missing"]:
        print("  WARNING ladder cells without a current item: "
              + ", ".join(f"{k} {v}" for k, v in res["ladder_missing"].items()))
    print(f"  verify of the fork: {res['verify']}")
    print(f"  config: {res['config']}")
    return 0 if res["verify"] == "PASS" else 1


# ------------------------------------------------------------------ widen: the pilot's fork -> the full run

def _config_core(d: dict) -> dict:
    """A raw config without the participant tables and the places: what must not differ between pilot and full run."""
    core = json.loads(json.dumps(d))
    for k in ("out_dir", "base_dir"):
        core.pop(k, None)
    for key in PID_TABLES:
        (core.get("corpus") or {}).pop(key, None)
    return core


def _judge_now(cfg: pipeline.RunConfig):
    """The judge endpoint the full J session will use, from the environment (``SS_<JUDGE>_*``): a stand-in client with
    its ModelConfig, for ``prereg.resume_problem``. Never opens a connection."""
    from types import SimpleNamespace
    from .llm import ModelConfig
    try:
        return SimpleNamespace(cfg=ModelConfig.from_env(cfg.models["judge"]))
    except (KeyError, ValueError) as exc:
        raise ForkRefused(f"set the judge environment variables of the full J session first "
                          f"(SS_{str(cfg.models.get('judge')).upper()}_URL / _MODEL / _PARAMS / _KEY_ENV): "
                          f"{type(exc).__name__}") from None


def widen(pilot_config, q_config, out) -> dict:
    """The pilot (a fork made by ``build_fork``, judged) -> a NEW out_dir for the full run that keeps every item the
    pilot finished, so the full J session judges only what is missing. ``<out>/out/logs/run.jsonl`` = the pilot's log
    byte for byte + a second ``run_fork`` record (declaring the full config) + a ``work_item`` record for every Q item
    of the pairs the pilot dropped, byte-copied from the Q out_dir (their calls are in the Q prefix of the log).

    Refused (nothing usable is written) unless: the pilot and the Q run verify; the pilot's log starts with the Q log
    byte for byte; the pilot was forked from that Q run and made no fork since; the pilot config equals the full
    config apart from the participant tables (same run_id, specs, settings, ladder, script samples...); the prompt set
    and quote rule of this code equal those the pilot ran with; the judge model and params in the environment equal
    those of the pilot's judge calls; every pilot session after its fork recorded this very code commit with a clean
    tree, and the tree is clean now."""
    pilot_config, q_config, out = Path(pilot_config).resolve(), Path(q_config).resolve(), Path(out).resolve()
    pcfg, qcfg = pipeline.RunConfig.load(pilot_config), pipeline.RunConfig.load(q_config)
    p_out, q_out = Path(pcfg.out_dir).resolve(), Path(qcfg.out_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise ForkRefused("the widen folder exists and is not empty")
    if any(_inside(a, b) or _inside(b, a) for a, b in ((out, p_out), (out, q_out))):
        raise ForkRefused("the widen folder must not contain, or lie inside, the pilot or the Q out_dir")
    q_raw = json.loads(q_config.read_text(encoding="utf-8"))
    if _config_core(json.loads(pilot_config.read_text(encoding="utf-8"))) != _config_core(q_raw):
        raise ForkRefused("the pilot config differs from the full config beyond the participant tables "
                          "(a pilot made with --script-samples, or an edited config, cannot be widened)")
    p_log = p_out / "logs" / "run.jsonl"
    q_log, q_records, _qm = _source_checks(qcfg)
    if not p_log.is_file():
        raise ForkRefused("the pilot out_dir has no logs/run.jsonl")
    ok, detail = audit_verify.verify_run(p_log)
    if not ok:
        raise ForkRefused(f"the pilot log does not verify ({detail.get('reason')})")
    p_work = p_out / "work"
    if (why := prereg.run_validity_problem(p_log, p_work) or audit_verify.work_integrity_problem(p_log, p_work)):
        raise ForkRefused(f"the pilot run is not valid ({why})")
    q_bytes = q_log.read_bytes()
    with open(p_log, "rb") as fh:
        if fh.read(len(q_bytes)) != q_bytes:
            raise ForkRefused("the pilot log does not start with the Q log: the pilot was not forked from this Q run")
    p_records = prereg._records(p_log) or []
    forks = [i for i, r in enumerate(p_records) if r.get("record_type") == prereg.FORK_RECORD]
    if len(forks) != 1 or forks[0] != len(q_records):
        raise ForkRefused("the pilot log must hold exactly one fork, right after the Q log")
    after = p_records[forks[0] + 1:]
    starts = prereg.session_starts(p_records)
    if {s.get("prompt_set_sha256") for s in starts} != {pipeline.prompt_set_sha256()}:
        raise ForkRefused("the prompt set of this code differs from the one the pilot ran with")
    if {s.get("quote_rule_sha256") for s in starts} - {None} != {textnorm.quote_rule_sha256()}:
        raise ForkRefused("the quote rule of this code differs from the one the pilot ran with")
    sessions = [r.get("payload") or {} for r in after if r.get("record_type") == "run_start"]
    if not any(r.get("record_type") == "call_request" for r in after):
        raise ForkRefused("the pilot made no judge call after its fork: nothing to carry over")
    now = prereg.code_state()
    if not now["code_clean"] or now["code_commit"] is None:
        raise ForkRefused("the code tree has uncommitted changes under second_scribe/ or prompts/ (or is not a git "
                          "checkout): commit first, so the full run's commit is the pilot's")
    if not sessions or any(s.get("code_commit") != now["code_commit"] or s.get("code_clean") is not True
                           for s in sessions):
        raise ForkRefused("a pilot session ran on another code commit (or a dirty tree, or recorded none): "
                          "the full run must use the pilot's exact code")
    if (why := prereg.resume_problem(p_log, {"judge": _judge_now(qcfg)})):
        raise ForkRefused(f"{why}: the full J session must use the pilot's judge model and params")
    people = {p.pid: p for p in pipeline.load_participants(qcfg)}
    pilot_pids = {p.pid for p in pipeline.load_participants(pcfg)}
    pilot_roles = {people[p].role for p in pilot_pids if p in people}
    if not pilot_pids <= set(people):
        raise ForkRefused("the pilot holds pairs that the full config does not")
    q_stores = _source_items(q_out / "work")
    q_work = pipeline.Work(q_out)
    gaps = sum(not _q_complete(q_work, qcfg, pid) for pid in people)
    if gaps:
        raise ForkRefused(f"{gaps} pair(s) of the full config lack a done mapping-talk or main-grid item in the Q run")

    new_out = out / "out"
    out.mkdir(parents=True, exist_ok=True)
    new_raw = json.loads(json.dumps(q_raw))
    new_raw["out_dir"], new_raw["base_dir"] = str(new_out), str(Path(qcfg.base_dir).resolve())
    new_config = out / "config.json"
    new_config.write_text(json.dumps(new_raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ncfg = pipeline.RunConfig.load(new_config)
    (new_out / "logs").mkdir(parents=True)
    new_log = new_out / "logs" / "run.jsonl"
    shutil.copyfile(p_log, new_log)
    shutil.copyfile(manifest_path_for(p_log), manifest_path_for(new_log))
    p_manifest = read_manifest(manifest_path_for(p_log))
    alog = AuditLog(new_log, qcfg.run_id)
    count, head = alog.head()
    if count != p_manifest["record_count"] or head != p_manifest["head_hash"]:
        raise ForkRefused("the pilot log changed while it was being copied; try again when nothing writes to it")
    restored: dict[str, int] = {}
    plan = []
    for store, names in q_stores.items():
        for name in names:
            kind, owner = _owner(store, name)
            if (owner in pilot_pids) if kind == "pid" else (owner in pilot_roles):
                continue                                  # the pilot holds it already
            if (owner in people) if kind == "pid" else (owner in {p.role for p in people.values()}):
                plan.append((store, name))
                restored[store] = restored.get(store, 0) + 1
    carried: dict[str, int] = {}
    for d in sorted(p for p in p_work.iterdir() if p.is_dir()):
        carried[stage_from_dirname(d.name)] = len(list(d.glob("*.json")))
    alog.append(prereg.FORK_RECORD, {
        "kind": "widen", "source_records": count, "source_head_hash": head,
        "source_manifest_sha256": compute_manifest_hash(p_manifest),
        "source_config_sha256": pcfg.sha256(), "source_prompt_set_sha256": pipeline.prompt_set_sha256(),
        "config_sha256": ncfg.sha256(), "prompt_set_sha256": pipeline.prompt_set_sha256(),
        "quote_rule_sha256": textnorm.quote_rule_sha256(),
        "pairs": sorted(people), "n_pairs_source": len(pilot_pids), "roles": sorted({p.role for p in people.values()}),
        "carried": carried, "restored": restored, "code_commit": now["code_commit"]})
    new_work = new_out / "work"
    shutil.copytree(p_work, new_work)                    # every pilot item and cache, byte for byte (logged in the prefix)
    for store, name in plan:
        src = q_out / "work" / stage_dirname(store) / f"{name}.json"
        item = json.loads(src.read_text(encoding="utf-8"))
        audit_verify.log_work_item(alog, store, name, item)      # re-announced with the sha the Q log recorded
        dst = new_work / stage_dirname(store) / f"{name}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    q_audits = q_out / "audits" / "Q.json"
    if q_audits.is_file():
        (new_out / "audits").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(q_audits, new_out / "audits" / "Q.json")
    alog.close()
    ok, detail = audit_verify.verify_run(new_log)
    problem = None if ok else detail.get("reason")
    problem = problem or prereg.run_validity_problem(new_log, new_work) or audit_verify.work_integrity_problem(
        new_log, new_work)
    return {"config": str(new_config), "out_dir": str(new_out), "pairs": len(people), "pilot_pairs": len(pilot_pids),
            "carried": carried, "restored": restored, "verify": "PASS" if problem is None else f"FAIL: {problem}"}


def widen_main(a) -> int:
    try:
        res = widen(a.pilot_config, a.config, a.out)
    except ForkRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"widen: {res['pairs']} pairs, {res['pilot_pairs']} of them carried from the pilot with their judge items")
    for store in sorted(set(res["carried"]) | set(res["restored"])):
        print(f"  {store:<26} carried {res['carried'].get(store, 0):>5}   restored from Q {res['restored'].get(store, 0):>5}")
    print(f"  verify of the widened run: {res['verify']}")
    print(f"  config: {res['config']}")
    return 0 if res["verify"] == "PASS" else 1
