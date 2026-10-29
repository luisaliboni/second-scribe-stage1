"""Command line: ``python -m second_scribe.cli <command>``.

  demo   --out DIR --client {replay,http,oracle} [--record FIXTURES]   synthetic corpus, every stage
  run    --config CFG --stages Q|J|M|all [--only STAGE] [--client http|oracle|replay|replay-live]
  report --config CFG                                                  regenerate the report from work/
  verify LOG [MANIFEST]                                                check the audit log
  audit-leaks --real-config CFG [--paths P ...] [--repo DIR]           scan files that would leave the machine
                                                                       (exit 0 clean, 1 hits, 2 error)
  prereg freeze|check --config CFG [--calibration F] [--anchor-dir D] [--letter-mode]  OPTIONAL: freeze a pre-registration / check it (no run needs it)
  bench  --mode generative|logprob --out DIR [...]                     judge benchmark on the synthetic traps
  fork   --config CFG --out DIR [--pairs ID,ID | --sample N [--seed S]] [--script-samples N]
                                                                       a finished Q out_dir -> a NEW out_dir for the
                                                                       judge phase (all pairs, or a pilot subset);
                                                                       the source is only read; counts only
  widen  --pilot-config P --config CFG --out DIR                     the judged pilot -> a NEW out_dir for the full
                                                                       run that keeps the pilot's finished items
  jcounts --config PILOT_CFG --full-config CFG [--json]               judge pilot: calls, seconds per call, outcomes,
                                                                       verify, projected hours for the full run
  sidejudge --config J_CFG --out SIDE_DIR [--parts 2,3,4,6,7,8] [--dry-count] [--client http|oracle]
            [--judge2-prefix P] [--only-role judge|judge2] [--seed S] [--sizes k=v,...]
                                                                       side study on a FINISHED J run (second judge,
                                                                       planted cases, label order, off-record and
                                                                       aligner calibration); counts only
  model-check --out DIR --stages Q|J [--families R=F,..] [--digests R=HEX,..] [--remote-endpoints FILE.json]
                                                                       Setup model check of one model pair
                                                                       (synthetic canary; exit 0 ok, 4 failed)
  certificate --run DIR [--run DIR ...] [--app-log FILE] --out FILE.json [--study]
                                                                       the run certificate (and FILE.md) from the logs:
                                                                       what was run and whether the logs verify
                                                                       (exit 0 all verify, 1 one does not)
  smoke-q --config CFG --out DIR [--plan]                              Qwen-only live smoke (group Q, synthetic corpus
                                                                       + one padded transcript); --plan lists the calls
  export-profile --config CFG --profile P.json [--out-dir D] [--B 2000] [--seed S] [--model-info M.json]
                 [--reference-type participant-endorsed] [--withhold-chain-heads]
                                                                       a finished run -> its distance profile
                                                                       (orgsci-profile/v2); exit 2 = refused (fewer
                                                                       than 10 people, a failed privacy scan)
  pool   --profiles A.json B.json ... --out POOL.json                  lab profiles side by side, never combined;
                                                                       writes POOL.json and POOL.md; exit 2 = refused
  phantom [--reps 20] [--B 2000] --out DIR                             known-distance check: plants doses in the synthetic
                                                                       demo (oracle, built into a temp dir) and writes
                                                                       DIR/phantom.json and DIR/phantom.md
  calibrate --config CFG --profile P.json [--out-dir D] [--reps 20] [--out DIR]
                                                                       the same check with the profile's B, seed and kit
                                                                       commit; P.json gets a detection_limits block
  validate-nomodel --config CFG --out DIR [--checks keyword,robustness,null,diffs] [--null-shifts K]
                   [--loo-b B] [--loo-perm N]                          no-model checks on a FINISHED J out_dir (only
                                                                       read): keyword arm, robustness / LOO,
                                                                       wrong-transcript null, approximate-quote diffs;
                                                                       aggregates only, into DIR

Endpoints and keys come from environment variables (``SS_<PREFIX>_URL``, ``SS_<PREFIX>_MODEL``,
``SS_<PREFIX>_KEY_ENV``, ``SS_<PREFIX>_PARAMS``; ``prereg freeze`` also reads ``SS_<PREFIX>_WEIGHTS``, a weight-file
sha256 or revision; prefixes from the config's ``models``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from . import audits
from . import judge, prereg
from .audit import verify as audit_verify
from .run_safety import RunRefused, check_output_paths, clear_demo_outputs, run_lock
from .audit.registry import AuditLog
from .audit.manifest import compute_manifest_hash, manifest_path_for, read_manifest
from .llm import (FixtureMissing, HTTPClient, ModelConfig, RealGuardRefused, RecordingClient, ReplayClient,
                  ReplayLiveClient, ThinkingDetected, make_client, redact_params)
from . import pipeline
from .pipeline import KIT_ROOT, STAGE_ORDER, RunConfig, expand_stages, is_synthetic, model_ids, run
from . import report
from .report import out_paths
from .certificate import cmd_certificate
from .maps.readers import MapRefused
from .transcripts import TranscriptRefused

DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
DEMO_FIXTURES = KIT_ROOT / "demo" / "fixtures.json"
ROLES = ("recon", "judge", "judge2")


def load_config(path, **overrides) -> RunConfig:
    """``RunConfig.load``, with every way a config file can be unusable turned into one ``refused:`` line (a
    ``RunRefused``, exit 2) that names the file, never a traceback. A config the kit refuses for its content
    (``corpus.kind`` and the like) keeps its own message."""
    try:
        return RunConfig.load(path, **overrides)
    except OSError as exc:
        raise RunRefused(f"the config file {path} cannot be opened ({exc.strerror or type(exc).__name__})") from None
    except UnicodeDecodeError:
        raise RunRefused(f"the config file {path} is not UTF-8 text (save the JSON file as UTF-8)") from None
    except RunRefused:
        raise
    except ValueError as exc:
        raise RunRefused(str(exc)) from None
    except (KeyError, TypeError, AttributeError, RecursionError) as exc:
        what = {KeyError: f"it has no {exc.args[0]!r} setting" if exc.args else "a setting is missing",
                TypeError: "a setting is missing or has the wrong type"}.get(
                    type(exc), "it is not a JSON object with the settings of a run")
        raise RunRefused(f"the config file {path} cannot be used: {what}") from None


def build_clients(kind: str, cfg: RunConfig, fixtures: str | None = None, roles=None) -> dict:
    """{"recon", "judge", "judge2"} -> client (judge2 only when the config names a prefix for it). ``roles`` (default:
    all of them) limits the clients to the endpoints a session will use: a Q-only session needs ``("recon",)`` and no
    judge environment variable, and nothing here talks to an endpoint that session does not use."""
    if roles is not None and (bad := [r for r in roles if r not in ROLES]):
        raise ValueError(f"unknown client role {bad[0]!r}; roles are {', '.join(ROLES)}")
    wanted = [r for r in ROLES if r != "judge2" or cfg.models.get("judge2")]
    if roles is not None:
        wanted = [r for r in wanted if r in roles]
    if kind == "oracle":
        from .demo_oracle import OracleClient
        c = cfg.corpus
        oracle = OracleClient(cfg.path(c["synthetic_dir"]), cfg.context["roles"],
                              participant_labels=c["participant_labels"]["*"],
                              interviewer_labels=c.get("interviewer_labels", {}).get("*", ()))
        oracle.model_label = "oracle"
        return {r: oracle for r in wanted}
    if kind == "replay":
        path = str(fixtures or DEMO_FIXTURES)
        meta = (json.loads(Path(path).read_text(encoding="utf-8-sig")).get("_meta") or {}).get("models", {})
        out = {}
        first = None
        for r in wanted:
            out[r] = ReplayClient(path, share_with=first, role=r)      # one set of counters, as one recording
            first = first or out[r]
            out[r].model_label = meta.get(r, "replay")
        return out
    if kind == "replay-live":
        if not is_synthetic(cfg):
            raise ValueError("--client replay-live is only for a synthetic corpus")
        if cfg.reading3 is None:
            raise ValueError("--client replay-live needs a reading3 block: it re-records only reading 3 and the other "
                             "family's script (llm.LIVE_RULE)")
        keys = tuple(s.key() for s in cfg.reading3_specs())         # the reading-3 drafts that go live (LIVE_RULE)
        path = str(fixtures or DEMO_FIXTURES)
        meta = (json.loads(Path(path).read_text(encoding="utf-8-sig")).get("_meta") or {}).get("models", {})
        log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"       # where ReplayLiveClient reads each request's stage
        out = {}
        first = None
        for r in wanted:
            live = make_client(ModelConfig.from_env(cfg.models[r]))
            if meta.get(r) and live.cfg.model_id != meta[r]:      # log_call would credit the recording to the live model
                raise ValueError(f"--client replay-live: the live {r} model is {live.cfg.model_id}, the recording's is "
                                 f"{meta[r]}; a mixed recording would credit one model's answers to another")
            out[r] = ReplayLiveClient(path, live, log_path, share_with=first, reading3_keys=keys,
                                      role=r)                     # tagged entries, live_reasks_by_role
            first = first or out[r]
            out[r].model_label = live.cfg.model_id
        return out
    if kind == "http":
        return {r: make_client(ModelConfig.from_env(cfg.models[r])) for r in wanted}     # by SS_<P>_PROVIDER (K12)
    raise ValueError(f"unknown client kind {kind!r}")


def _fixtures_usable(path) -> bool:
    """Is ``path`` a UTF-8 JSON object (what ``ReplayClient`` loads)? Tells a bad fixtures file from bad endpoint settings."""
    try:
        return isinstance(json.loads(Path(path).read_text(encoding="utf-8-sig")), dict)
    except (OSError, ValueError):
        return False


def build_clients_or_refuse(kind: str, cfg: RunConfig, fixtures: str | None = None, **kw) -> dict:
    """``build_clients`` for the commands a person types: an unusable fixtures file, or ``--client http`` without its
    environment variables, is one ``refused:`` line (a ``RunRefused``, exit 2) that names the file or the variable,
    never a traceback. ``build_clients`` itself still raises what it always raised, for the callers that catch it."""
    try:
        return build_clients(kind, cfg, fixtures, **kw)
    except RunRefused:
        raise
    except OSError as exc:
        if kind not in ("replay", "replay-live"):
            raise
        raise RunRefused(f"the fixtures file {fixtures or DEMO_FIXTURES} cannot be opened "
                         f"({exc.strerror or type(exc).__name__})") from None
    except (ValueError, AttributeError, TypeError, RecursionError) as exc:
        if kind == "replay-live" and str(exc).startswith(("--client replay-live is only",
                                                          "--client replay-live needs a reading3 block",
                                                          "--client replay-live: the live ")):
            raise RunRefused(str(exc)) from None
        # replay-live reads the fixtures and the endpoint settings: a ValueError is the settings' only when the file is fine
        if kind == "replay-live" and isinstance(exc, ValueError) and _fixtures_usable(fixtures or DEMO_FIXTURES):
            raise RunRefused(f"the endpoint settings in the environment (SS_<PREFIX>_*) are not usable: {exc}") from None
        if kind in ("replay", "replay-live"):      # not JSON, not UTF-8, or the wrong shape
            raise RunRefused(f"the fixtures file {fixtures or DEMO_FIXTURES} is not a recorded-responses file (a "
                             "JSON object saved as UTF-8 text is expected)") from None
        if kind == "http" and isinstance(exc, ValueError):      # SS_<PREFIX>_TIMEOUT_S, _PARAMS or _PROVIDER
            raise RunRefused(f"the endpoint settings in the environment (SS_<PREFIX>_*) are not usable: {exc}") from None
        raise
    except KeyError as exc:
        if kind not in ("http", "replay-live") or not exc.args or not str(exc.args[0]).startswith("SS_"):
            raise
        raise RunRefused(f"--client {kind} needs the environment variable {exc.args[0]}, which is not set. Set "
                         "SS_<PREFIX>_URL and SS_<PREFIX>_MODEL for each model role (see \"Running on your own data\" "
                         "in README.md), or use --client replay or oracle for an offline run") from None


def roles_for_stages(stages: list[str]) -> tuple | None:
    """The client roles a session needs. Every model stage of the Q group (reconstructor endpoint): ``("recon",)``.
    Every model stage of the J group (judge endpoint): ``("judge", "judge2")``; ``build_clients`` leaves out ``judge2``
    when the config names no second judge, and no reconstructor is built, so a judge session runs while the
    reconstructor's server is stopped (its preflight sends the planted items through the judge only, and its
    reconstruction canary is skipped, see ``audits._canary_result``). A session that mixes Q and J stages needs every
    role (None); so does one with no model stage."""
    model = [s for s in stages if s not in pipeline.STAGE_GROUPS["M"]]
    if model and all(s in pipeline.STAGE_GROUPS["Q"] for s in model):
        return ("recon",)
    if model and all(s in pipeline.STAGE_GROUPS["J"] for s in model):
        return ("judge", "judge2")
    return None


def _record(clients: dict, path: str, merge: bool) -> tuple[dict, dict]:
    """Wrap every client in one shared recording store. A fresh run records a fresh file; a resumed run
    (``merge``) keeps the fixtures already in it, since it only makes the calls that are still missing."""
    store: dict = {}
    wrapped = {}
    for r, c in clients.items():
        rc = RecordingClient(c, path, store=store, merge=merge, role=r)      # tags each record with its role
        rc.model_label = getattr(c, "model_label", None)
        wrapped[r] = rc
    return wrapped, store


def _check_inputs(cfg: RunConfig) -> "pipeline.InputCheck":
    """Every input file opened once (``pipeline.check_inputs``), before any client is built or any folder is made; a
    refusal writes nothing. Its warnings go to stderr."""
    check = pipeline.check_inputs(cfg)
    for w in check.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return check


SYNC_NAMES = {"icloud": "iCloud Drive", "cloudstorage": "cloud-storage (Library/CloudStorage)", "dropbox": "Dropbox",
              "onedrive": "OneDrive", "google_drive": "Google Drive"}


def sync_folder_problems(cfg: RunConfig, check: "pipeline.InputCheck") -> list[str]:
    """For a real (or unknown) corpus: which of the output folder and the folders the inputs are read from sit in a
    sync folder (``app.project.sync_folder_kind``, the app's own detector), one line each, naming kinds, never paths.
    A synthetic corpus: []."""
    if not pipeline.is_real_or_unknown(cfg):
        return []
    from .app.project import sync_folder_kind       # the app's detector (Ruling R5: imported, not moved)
    out: list[str] = []
    places = [("the output folder (out_dir)", [Path(cfg.out_dir)]),
              ("a map's folder", sorted({p.map_path.parent for p in check.pairing.participants})),
              ("a transcript's folder", sorted({p.transcript_path.parent for p in check.pairing.participants}))]
    for what, folders in places:
        kinds = sorted({k for f in folders if (k := sync_folder_kind(f))})
        if kinds:
            out.append(f"{what} is in a {' and a '.join(SYNC_NAMES.get(k, k) for k in kinds)} folder")
    return out


def _execute(cfg: RunConfig, stages: list[str], clients: dict, record: str | None,
             check: "pipeline.InputCheck | None" = None, allow_synced_folder: bool = False,
             demo_copies: bool = False, fresh: bool = False) -> tuple[dict, Path]:
    if record and not is_synthetic(cfg):
        # a fixtures file holds every prompt and reply verbatim: never for participant data
        raise ValueError("--record is only allowed for a synthetic corpus (corpus.kind == \"synthetic\")")
    output_root = check_output_paths(cfg.out_dir)
    log_path = Path(cfg.out_dir) / "logs" / "run.jsonl"
    if check is None:
        check = _check_inputs(cfg)
    if (synced := sync_folder_problems(cfg, check)):
        if not allow_synced_folder:
            raise pipeline.InputsRefused("a real corpus must stay out of sync folders", synced,
                                         tail="A sync client copies every file to its provider's servers. Move them, "
                                              "or pass --allow-synced-folder to go on anyway")
        print("warning: --allow-synced-folder: " + "; ".join(synced), file=sys.stderr)
    pipeline.refuse_before_writing(cfg, clients, log_path, resume=not fresh)
    with run_lock(output_root, say=lambda m: print(m, file=sys.stderr)):
        # Another run might have finished between the preflight and taking the lock.
        pipeline.refuse_before_writing(cfg, clients, log_path, resume=not fresh)
        if fresh:
            clear_demo_outputs(output_root)
        print(f"output folder: {output_root}", file=sys.stderr)
        return _execute_locked(cfg, stages, clients, record, log_path, demo_copies)


def _execute_locked(cfg, stages, clients, record, log_path, demo_copies=False):
    inner = clients
    meters = {role: _CallMeter(client) for role, client in clients.items()}
    clients = meters
    store = None
    if record:
        clients, store = _record(clients, record, merge=log_path.exists())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = AuditLog(log_path, cfg.run_id)
    stage_seconds = {}
    try:
        seeded = set()
        for client in inner.values():
            if isinstance(client, ReplayClient) and id(client.served) not in seeded:
                client.resume_from_log(log_path)
                seeded.add(id(client.served))
        result = run(cfg, stages, clients, log=log, inputs_checked=True, say=lambda m: print(m, file=sys.stderr),
                     stage_seconds=stage_seconds)
        if "report" in expand_stages(stages):
            (Path(cfg.out_dir) / "aggregates" / "report.STALE").unlink(missing_ok=True)
    except (audits.AuditBlocked, ThinkingDetected):
        for folder in (Path(cfg.out_dir), Path(cfg.out_dir) / "aggregates"):
            if (folder / "report.md").is_file():
                (folder / "report.STALE").write_text(
                    "This report predates a stopped run. Finish the run before using it.\n", encoding="utf-8")
        raise
    finally:
        log.close()
        if record and store is not None:
            # a session with fewer roles (judge only) keeps the model labels an earlier session recorded for the others
            earlier = ((store.get("_meta") or {}).get("models") or {}) if isinstance(store.get("_meta"), dict) else {}
            store["_meta"] = {"models": {**earlier, **model_ids(inner)},
                              "note": "recorded responses; ReplayClient serves them by messages_sha256"}
            next(iter(clients.values())).save()
    if demo_copies:
        for name in ("aggregates", "report", "traps"):
            path = out_paths(cfg)[name]
            if path.exists():
                shutil.copyfile(path, Path(cfg.out_dir) / path.name)
        (Path(cfg.out_dir) / "report.STALE").unlink(missing_ok=True)
    _run_summary(cfg, log_path, meters, stage_seconds)
    return result, log_path


class _CallMeter:
    """Count client calls, including auditors' re-asks, without changing replies."""
    def __init__(self, inner):
        self.inner = inner
        self.kind = ("replay-live" if isinstance(inner, ReplayLiveClient) else
                     "replay" if isinstance(inner, ReplayClient) else
                     "http" if getattr(getattr(inner, "cfg", None), "endpoint", None) else
                     "oracle" if getattr(inner, "model_label", None) == "oracle" else "offline")
        self.model_calls = self.replayed = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, *args, **kwargs):
        if self.kind == "replay-live":      # the inner client decides, per request, whether it goes out or replays
            live0, replayed0 = self.inner.live_calls, self.inner.replayed_calls
            try:
                return self.inner.complete(*args, **kwargs)
            finally:
                self.model_calls += self.inner.live_calls - live0
                self.replayed += self.inner.replayed_calls - replayed0
        if self.kind == "http":
            self.model_calls += 1
        response = self.inner.complete(*args, **kwargs)
        if self.kind == "replay":
            self.replayed += 1
        return response


def _run_summary(cfg, log_path, meters, stage_seconds):
    kinds = sorted({m.kind for m in meters.values()})
    print("client: " + (", ".join(kinds) if kinds else "none (stored work only)"))
    print(f"{sum(m.model_calls for m in meters.values())} model calls; "
          f"responses replayed: {sum(m.replayed for m in meters.values())}")
    paths = out_paths(cfg)
    for name in ("aggregates", "report", "traps"):
        suffix = "" if paths[name].is_file() else " (not written by these stages)"
        print(f"{name}: {paths[name].resolve()}{suffix}")
    manifest = read_manifest(manifest_path_for(log_path))
    print(f"audit log records: {manifest['record_count']}")
    print(f"manifest hash: {compute_manifest_hash(manifest)}; record this manifest hash outside this folder")
    for stage, elapsed in stage_seconds.items():
        shown = "no_interview_judge" if stage == "script_prior_only" else stage
        print(f"stage {shown}: {elapsed:.3f} s")


def _warn_outage(cfg: RunConfig) -> None:
    """Say on stderr when items are left ``timeout_excluded`` (the model could not be reached): the run is
    incomplete and the same command, rerun, redoes exactly those."""
    n = 0
    for f in sorted((Path(cfg.out_dir) / "work").glob("*/*.json")):
        try:
            n += json.loads(f.read_text(encoding="utf-8")).get("status") == "timeout_excluded"
        except (OSError, ValueError, AttributeError):
            continue
    if n:
        print(f"WARNING: {n} item(s) are timeout_excluded because the model could not be reached; "
              "rerun the same command when it is back (only those are redone)", file=sys.stderr)
    try:
        gaps = pipeline.ladder_missing(cfg)
    except (OSError, ValueError, KeyError):
        gaps = {}
    if gaps:
        print("WARNING: the instruction ladder has cells without a stored map "
              f"({', '.join(f'{k}: {v}' for k, v in gaps.items())}); rerun the same command until this is empty "
              "(only the missing cells are redone) before reading the ladder report", file=sys.stderr)


def _verify(log_path: Path) -> int:
    rc = audit_verify.main([str(log_path)])
    return rc or _validity(log_path)


def _validity(log_path: Path) -> int:
    """For a real corpus: the work store (``<out_dir>/work``, next to ``<out_dir>/logs``) must agree with the log,
    and one model and one set of params must have served each role. Not a pre-registration check."""
    work = Path(log_path).parent.parent / "work"
    problem = prereg.run_validity_problem(log_path, work) or audit_verify.work_integrity_problem(log_path, work)
    if problem:
        print(f"FAIL: {json.dumps({'reason': 'run_validity', 'detail': problem})}")
        return 1
    return 0


def cmd_demo(a) -> int:
    fresh = getattr(a, "fresh", False)
    if fresh and (Path(a.config).resolve() != DEMO_CONFIG.resolve() or a.client == "http"):
        raise RunRefused("--fresh is only for the built-in offline demo; use a new --out for a configured study")
    out = Path(a.out).resolve()
    cfg = load_config(a.config, out_dir=str(out))
    check = _check_inputs(cfg)
    _result, log_path = _execute(cfg, STAGE_ORDER, build_clients_or_refuse(a.client, cfg, a.fixtures), a.record, check,
                                 getattr(a, "allow_synced_folder", False), demo_copies=True, fresh=fresh)
    _warn_outage(cfg)
    paths = out_paths(cfg)
    if paths["traps"].exists():
        print(paths["traps"].read_text(encoding="utf-8"))
    return 0 if _verify(log_path) == 0 else 1


def cmd_run(a) -> int:
    try:
        cfg = load_config(a.config)
    except ValueError as exc:       # a config the kit refuses (L33): exit 2 with the reason, like its other refusals
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        stages = expand_stages(a.stages, a.only)
    except ValueError as exc:       # a mistyped stage name: say which ones exist
        print(f"refused: {exc}. --stages takes Q, J, M, all, or a comma list of: {', '.join(STAGE_ORDER)}",
              file=sys.stderr)
        return 2
    check = _check_inputs(cfg)
    needs_models = any(s not in ("metrics", "report") for s in stages)
    roles = roles_for_stages(stages)
    clients = (build_clients_or_refuse(a.client, cfg, a.fixtures, **({"roles": roles} if roles else {}))
               if needs_models else {})
    _result, log_path = _execute(cfg, stages, clients, a.record, check, a.allow_synced_folder)
    _warn_outage(cfg)
    return 0 if _verify(log_path) == 0 else 1


def cmd_report(a) -> int:
    cfg = load_config(a.config)
    _result, log_path = _execute(cfg, ["metrics", "report"], {}, None, allow_synced_folder=a.allow_synced_folder)
    return 0 if _verify(log_path) == 0 else 1


def cmd_verify(a) -> int:
    if a.expect_manifest_hash is not None and not a.expect_manifest_hash.strip():
        # an unset shell variable ("$SAVED") gives "": nothing would be compared, and the run would still say PASS
        print("refused: --expect-manifest-hash is empty; give the manifest hash you saved outside the run folder "
              "(or leave the option out to check the log against its own manifest only)", file=sys.stderr)
        return 2
    argv = [a.log] + ([a.manifest] if a.manifest else [])
    if a.expect_manifest_hash:
        argv += ["--manifest-hash", a.expect_manifest_hash]
    rc = audit_verify.main(argv)
    return rc or _validity(Path(a.log))


def cmd_export_profile(a) -> int:
    from .profile_export import run_export_profile
    return run_export_profile(a)


def cmd_audit_leaks(a) -> int:
    """Scan ``--paths`` (files or folders) against the real corpus of ``--real-config``; with ``--repo``, also what
    ``git push`` would publish (the index, HEAD, every unpushed commit's files and messages, every path; a working
    tree that differs from HEAD is itself a failure). Prints counts and ``<kind>:<path>@<byte offset>`` per hit, never
    the matched text. Exit 0 clean, 1 hits, 2 the scan could not be done."""
    cfg = RunConfig.load(a.real_config)
    paths = [str(p) for p in (a.paths or [])]
    strict = [str(f) for p in paths for f in ([Path(p)] if Path(p).is_file() else sorted(Path(p).rglob("*.json")))
              if f.suffix.lower() == ".json"]
    if not paths and not a.repo:
        paths = [str(Path(cfg.out_dir) / "aggregates")]
    index = audits.LeakIndex.from_config(cfg)
    results = []
    if paths:
        results.append(audits.audit_leaks(paths, index, strict_json=strict))
    if a.repo:
        results.append(audits.audit_repo(a.repo, index, since_upstream=a.since_upstream))
    result = audits.merge_results("leaks", *results)
    print(json.dumps({"passed": result.passed, "counts": result.counts, "failures": result.failures},
                     sort_keys=True, indent=1))
    return 0 if result.passed else 1
def cmd_smoke_q(a) -> int:
    """The Qwen-only smoke (see ``smoke_q``): the Q stages on the synthetic corpus, then on one synthetic transcript
    padded to real length, each run in its own folder under ``--out`` with its own hash-chained log. ``--plan`` prints
    the calls and the endpoint and opens no connection. Exit 0 when both logs verify."""
    from . import smoke_q
    out = Path(a.out).resolve()
    cfg, smoke = smoke_q.load_config(a.config, out_dir=str(out / "synthetic"))
    smoke_q.check_config(cfg)
    if a.plan:
        with tempfile.TemporaryDirectory(prefix="ss-smoke-plan-") as tmp:
            text, rc = smoke_q.plan_text(cfg, smoke, smoke_q.prepare_padded(cfg, smoke, tmp))
        print(text, end="")
        return rc
    clients = smoke_q.build_clients(cfg)             # the guards run here, before the first request
    padded = smoke_q.prepare_padded(cfg, smoke, out)
    stages = expand_stages("Q")
    rc = 0
    for label, run_cfg in (("synthetic", cfg), ("padded", padded)):
        print(f"smoke-q: {label} run ({', '.join(stages)}) ...", flush=True)
        _result, log_path = _execute(run_cfg, stages, clients, None)
        _warn_outage(run_cfg)
        rc |= _verify(log_path)
    budget = smoke_q.build_budget(cfg, smoke, padded, verified=rc == 0)
    smoke_q.write_budget(out / "budget.json", budget)
    print(f"smoke-q: wrote {out / 'budget.json'}", flush=True)
    return rc


def cmd_model_check(a) -> int:
    """The Setup model check (K10, ``model_check``): one session, ``--stages Q`` or ``--stages J`` (J then M, then the
    canary is scored), of one synthetic run in ``--out``. Clients come from the environment as for ``run``, for every
    role whose ``SS_<PREFIX>_URL`` is set (so the two-family guard sees the whole pair). Exit 0 when the check passed or
    the Q session is done, 4 when it failed, 2 when refused."""
    from . import model_check
    cfg = RunConfig.load(model_check.CHECK_CONFIG)
    roles = tuple(r for r in ROLES if cfg.models.get(r) and os.environ.get(f"SS_{cfg.models[r].upper()}_URL"))
    need = "recon" if a.stages == "Q" else "judge"
    if need not in roles:
        print(f"refused: the {need} endpoint is not set (SS_{str(cfg.models.get(need)).upper()}_URL)", file=sys.stderr)
        return 2
    try:
        families = model_check.parse_role_map(a.families, "--families")
        digests = model_check.parse_role_map(a.digests, "--digests")
        eps = model_check.load_remote_endpoints(a.remote_endpoints) if a.remote_endpoints else None
        ops = model_check.load_endpoint_optins(a.endpoint_optins) if a.endpoint_optins else None
        doc = model_check.run_model_check(Path(a.out).resolve(), build_clients("http", cfg, roles=roles), a.stages,
                                          families=families or None, digests=digests, remote_endpoints=eps,
                                          endpoint_optins=ops)
    except (ValueError, FileNotFoundError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if doc.get("state") == "q_done":
        print("model-check: Q session done; run --stages J next")
        return 0
    path = Path(a.out).resolve() / model_check.FILE_NAME
    rc = _verify(model_check.log_path(Path(a.out).resolve()))
    print(f"model-check: {'passed' if doc['passed'] else 'failed'} ({', '.join(doc['failures']) or 'no failures'})")
    print(f"sha256 {hashlib.sha256(path.read_bytes()).hexdigest()}  {path}")
    return 4 if not doc["passed"] else (1 if rc else 0)


def cmd_fork(a) -> int:
    from . import fork
    return fork.main(a)


def cmd_widen(a) -> int:
    from . import fork
    return fork.widen_main(a)


def cmd_jcounts(a) -> int:
    from . import jcounts
    return jcounts.main(a)


def cmd_sidejudge(a) -> int:
    from . import sidejudge
    return sidejudge.main(a)


def cmd_phantom(a) -> int:
    """Build the oracle demo into a temp dir, then plant doses in it (second_scribe.phantom). Zero model calls."""
    from . import phantom
    out = Path(a.out).resolve()
    try:
        with phantom.demo_context(B=a.B) as ctx:
            phantom.run_phantom(ctx, reps=a.reps, out_dir=out)
    except RuntimeError as exc:
        print(f"phantom: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out / 'phantom.json'} and {out / 'phantom.md'}")
    return 0


def cmd_calibrate(a) -> int:
    """The known-distance check on the public synthetic demo with a lab's B, seed and kit commit (from its profile), then
    the profile rewritten with a ``detection_limits`` block. Exit 0 and the sha256; exit 2 with the reasons on refusal."""
    from . import phantom
    from .profile_export import ProfileRefused, refusal_lines
    try:
        if not Path(a.profile).exists():           # village E4: a missing file is not "cannot be read as JSON"
            raise ProfileRefused(f"file not found: {a.profile} (was this lab's export refused?)")
        print(f"note: when it succeeds, calibrate rewrites {a.profile} in place; keep a copy")     # village E5
        digest = phantom.calibrate(a.config, a.profile, reps=a.reps, out_dir=Path(a.out).resolve() if a.out else None,
                                   lab_out_dir=a.out_dir)
    except phantom.CalibrateIncomplete as exc:       # fix round 2, N4: after the profile write; not a refusal
        print(f"calibrate: {exc}", file=sys.stderr)
        return 1
    except ProfileRefused as exc:
        for line in refusal_lines(exc.reasons):
            print(line, file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:      # final review A8
        print(f"refused: {type(exc).__name__} while reading the inputs", file=sys.stderr)
        return 2
    except RuntimeError as exc:                      # the demo did not finish, before the profile write: refused (N4)
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {a.profile}")
    print(f"sha256 {digest}")
    try:                                             # village E5: the result, one line per dose type
        from . import pool as poolmod
        limits = poolmod.detection_limits_of(json.loads(Path(a.profile).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        limits = None
    for dose in poolmod.DOSE_TYPES if limits else ():
        print(f"{poolmod.dose_name(dose)}: {poolmod.detection_text(limits[dose])}")
    return 0


def cmd_validate_nomodel(a) -> int:
    from . import nomodel
    return nomodel.main(a)


def cmd_prereg(a) -> int:
    cfg = RunConfig.load(a.config)
    path, log_path = prereg.prereg_path(cfg), Path(cfg.out_dir) / "logs" / "run.jsonl"
    if a.prereg_cmd == "check":
        ok, msg = prereg.check(path, log_path, prereg.PROMPTS_DIR, cfg=cfg, kit_root=prereg.KIT_ROOT,
                               work_dir=Path(cfg.out_dir) / "work", anchor=a.require_anchor)
        print("prereg ok" if ok else f"prereg check failed: {msg}")
        return 0 if ok else 1
    head = prereg.git_head(prereg.KIT_ROOT)
    problem = prereg.shadow_problem(prereg.KIT_ROOT) or (
        "cannot read the code commit (not a git checkout)" if head is None else prereg.check_code(head, prereg.KIT_ROOT))
    if problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    model_info = {}
    for role in ("recon", "judge", "judge2"):
        prefix = cfg.models.get(role)
        if not prefix:
            continue
        p = f"SS_{prefix.upper()}_"
        if not os.environ.get(p + "MODEL"):
            print(f"refused: set {p}MODEL", file=sys.stderr)
            return 2
        params = json.loads(os.environ.get(p + "PARAMS") or "{}")
        model_info[role] = {"model_id": os.environ[p + "MODEL"], "weights": os.environ.get(p + "WEIGHTS") or "pending",
                            "params": redact_params(params, os.environ.get(p + "KEY_ENV") or None)}
        if a.letter_mode and role in prereg.LETTER_ROLES:       # the judge will also be called in letter mode
            model_info[role]["letter_params"] = dict(judge.LETTER_PARAMS)
    calibration = json.loads(Path(a.calibration).read_text(encoding="utf-8")) if a.calibration else None
    anchor_dir = Path(a.anchor_dir) if a.anchor_dir else path.parent
    try:
        prereg.ensure_writable(anchor_dir)       # before the freeze: a freeze without its anchor is half a freeze
    except OSError as exc:
        print(f"refused: the anchor folder {anchor_dir} is not writable ({exc.strerror or type(exc).__name__}); "
              "nothing was frozen", file=sys.stderr)
        return 2
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = AuditLog(log_path, cfg.run_id)
    try:
        digest = prereg.freeze(prereg.build_prereg(cfg, prereg.PROMPTS_DIR, code_commit=head, model_info=model_info,
                                                   decision_rules=prereg.DECISION_RULES, calibration=calibration,
                                                   code_root=prereg.KIT_ROOT),
                               path, log, allow_existing_work=a.allow_existing_work)
    except (ValueError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if log.head()[0]:
            log.close()
    print(f"prereg frozen: sha256 {digest} ({path})")
    try:
        anchor = prereg.write_anchor(digest, cfg.run_id, anchor_dir)
    except OSError as exc:
        print(f"the freeze is done but the anchor could not be written ({exc.strerror or type(exc).__name__}). Anchor it by "
              f"hand: put the line \"{digest} {cfg.run_id}\" in a file named {prereg.ANCHOR_NAME} and commit it to the "
              "private runner repo.", file=sys.stderr)
        return 3
    print(f"anchor written to {anchor}: commit it NOW to the private runner repo (outside the output folder), then verify "
          "later with: prereg check --config CFG --require-anchor <that file>")
    return 0


def _read_profile_json(f):
    """A lab's profile file for pool / calibrate (village E4): a missing file, an unreadable one and one that is not
    JSON are three plain refusals (ValueError), never a raw ``[Errno N]``."""
    path = Path(f)
    if not path.exists():
        raise ValueError(f"file not found: {f} (was this lab's export refused?)")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{f} cannot be read as JSON (it is not UTF-8 text)") from None
    except OSError as exc:
        raise ValueError(f"cannot read {f}: {exc.strerror or type(exc).__name__}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{f} cannot be read as JSON (line {getattr(exc, 'lineno', '?')}, "
                         f"column {getattr(exc, 'colno', '?')})") from None


def cmd_pool(a) -> int:
    from . import pool as poolmod
    out = Path(a.out)
    try:
        if out.suffix.lower() == ".md":
            raise ValueError("--out must be the JSON file (the markdown is written beside it as .md)")
        profiles, labels = [], []
        for f in a.profiles:
            profiles.append(_read_profile_json(f))
            labels.append(Path(f).stem)
            if poolmod.is_study_profile(profiles[-1]):      # village A9: named by the file as given, before any check
                raise ValueError(poolmod.study_refusal(f))
        pooled = poolmod.pool(profiles, labels=labels)
    except ValueError as exc:                      # a missing file, bad JSON, the k floor, a bad --out
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:                         # village E4: never a raw [Errno N]
        print(f"refused: {exc.strerror or type(exc).__name__}"
              + (f": {exc.filename}" if getattr(exc, "filename", None) else ""), file=sys.stderr)
        return 2
    except (KeyError, TypeError, AttributeError) as exc:     # final review A8: a malformed profile; the type name only
        print(f"refused: a profile is malformed ({type(exc).__name__})", file=sys.stderr)
        return 2
    # final review A6: both texts are built before either file is written, then each is written atomically
    json_text = json.dumps(pooled, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    md_text = poolmod.pool_markdown(pooled)
    written = []
    backups = {}
    for target, text in ((out, json_text), (out.with_suffix(".md"), md_text)):
        try:                                       # village A1: an unwritable --out folder is a refusal, not a traceback
            if target.is_file() or target.is_symlink():
                fd, backup = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".bak")
                os.close(fd)
                backups[target] = Path(backup)
                if target.is_symlink():
                    Path(backup).unlink()
                shutil.copy2(target, backup, follow_symlinks=False)
            report.write_text_atomic(target, text)
        except OSError as exc:
            for done in written:                   # a refusal leaves nothing half-written
                if done in backups:
                    os.replace(backups.pop(done), done)
                else:
                    done.unlink(missing_ok=True)
            for backup in backups.values():
                backup.unlink(missing_ok=True)
            print(f"refused: cannot write {target}: {exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 2
        written.append(target)
    for backup in backups.values():
        backup.unlink(missing_ok=True)
    for msg in pooled.get("checks") or []:          # village E3: warnings, never a refusal
        print(f"warning: {msg}", file=sys.stderr)
    n_tables, n_alone = poolmod.table_counts(pooled)          # village E2
    print(f"{n_tables} table{'' if n_tables == 1 else 's'}, {n_alone} lab{'' if n_alone == 1 else 's'} listed on "
          f"{'its' if n_alone == 1 else 'their'} own -> {out}")
    return 0
def cmd_replay_check(a) -> int:
    from . import replay_check       # lazy: replay_check imports this module
    ok, reasons = replay_check.replay_check(Path(a.config), Path(a.fixtures), Path(a.recorded_log), Path(a.view_js))
    print("\n".join(reasons) if reasons else "replay-check: ok")
    return 0 if ok else 1


# ladder.run_rung's RuntimeError when the rung table does not pass validate_rungs
VALIDATE_RUNGS_PREFIX = "the rung table does not pass validate_rungs; no call was made:"
PINNED_REFUSAL = "reconstruct prompts are pinned (ladder.PINNED_SHA256); an edited prompt file does not match"


def _corpus_kind(a) -> str | None:
    """The ``corpus.kind`` of the command's config file, or None when there is none or it cannot be read."""
    path = getattr(a, "config", None) or getattr(a, "real_config", None)
    if not path:
        return None
    try:
        kind = (json.loads(Path(path).read_text(encoding="utf-8-sig")).get("corpus") or {}).get("kind")
    except (OSError, ValueError, AttributeError):
        return None
    return kind if isinstance(kind, str) else None


def _read_error_line(exc: BaseException, kind: str | None) -> str:
    """One line for a file that could not be read. An OSError names its path only for a synthetic corpus (a real or
    unknown corpus gets the system's reason alone); the readers' own refusals never quote a file."""
    if isinstance(exc, MapRefused):
        return f"a map file cannot be read: {exc.detail}"
    if isinstance(exc, TranscriptRefused):
        return f"a transcript file cannot be read: {exc}"
    if isinstance(exc, zipfile.BadZipFile):
        return "an input file is not a readable zip (.docx, .vsdx or .xlsx)"
    if isinstance(exc, ElementTree.ParseError):
        return "an input file holds XML that is not well-formed (a damaged file)"
    reason = exc.strerror or type(exc).__name__
    if kind == "synthetic" and exc.filename:
        return f"{exc.filename}: {reason}"
    return reason


def _blank_out_message(flag: str) -> str:
    return (f"refused: {flag} is empty, so it would mean the current folder; give a name. (An unset shell variable, "
            f"as in {flag} \"$OUT\", gives an empty value.)")


def _blank_out_flag(args: list[str]) -> str | None:
    """``--out`` or ``--out-dir`` given with an empty or blank value in a raw argument list (the bench's), else None."""
    for i, tok in enumerate(args):
        flag, eq, value = tok.partition("=")
        if flag in ("--out", "--out-dir"):
            if eq and not value.strip():
                return flag
            if not eq and i + 1 < len(args) and not args[i + 1].strip():
                return flag
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="second_scribe.cli", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run every stage on the synthetic demo corpus")
    d.add_argument("--out", required=True)
    d.add_argument("--client", choices=("replay", "http", "oracle"), default="replay")
    d.add_argument("--record", help="also write the responses to this fixtures file")
    d.add_argument("--fixtures", help="fixtures file for --client replay (default demo/fixtures.json)")
    d.add_argument("--config", default=str(DEMO_CONFIG))
    d.add_argument("--fresh", action="store_true",
                   help="restart the built-in offline demo's generated outputs, after taking its run lock")
    r = sub.add_parser("run", help="run stages of a configured study")
    r.add_argument("--config", required=True)
    r.add_argument("--stages", default="all", help="Q, J, M, all, or a comma list")
    r.add_argument("--only", help="run one stage only")
    r.add_argument("--client", choices=("replay", "http", "oracle", "replay-live"), default="http")
    r.add_argument("--fixtures")
    r.add_argument("--record")
    rep = sub.add_parser("report", help="recompute metrics and the report from work/")
    rep.add_argument("--config", required=True)
    for sp in (d, r, rep):
        sp.add_argument("--allow-synced-folder", action="store_true",
                        help="a real corpus: go on although out_dir or an input folder is in a sync folder "
                             "(iCloud, OneDrive, Dropbox, Google Drive)")
    v = sub.add_parser("verify", help="verify an audit log against its manifest")
    v.add_argument("log")
    v.add_argument("manifest", nargs="?")
    v.add_argument("--expect-manifest-hash", metavar="HEX",
                   help="compare with a manifest hash saved outside the run folder")
    al = sub.add_parser("audit-leaks", help="scan files that would leave the machine for real text and names")
    al.add_argument("--real-config", required=True, help="run config of the real corpus (e.g. path/to/your-config.json)")
    al.add_argument("--paths", nargs="*", help="files or folders (default: <out_dir>/aggregates)")
    al.add_argument("--repo", help="also scan what git push would publish from this folder (whole history of HEAD)")
    al.add_argument("--since-upstream", action="store_true",
                    help="with --repo: history only from @{upstream}..HEAD (the index and HEAD are always scanned)")
    pr = sub.add_parser("prereg", help="freeze or check the pre-registration")
    pr.add_argument("prereg_cmd", choices=("freeze", "check"))
    pr.add_argument("--config", required=True)
    pr.add_argument("--calibration", help="JSON file with the matcher / off-record calibration aggregates (freeze only)")
    pr.add_argument("--anchor-dir", help="where freeze writes prereg.sha256 (default: next to prereg.json)")
    pr.add_argument("--allow-existing-work", action="store_true",
                    help="freeze even though work/ or aggregates/ hold files; synthetic corpora only (tests)")
    pr.add_argument("--letter-mode", action="store_true",
                    help="freeze: the judge(s) will also be called in letter (logprob) mode; its params are frozen too")
    pr.add_argument("--require-anchor", metavar="FILE", help="check: the anchor file must match the prereg file")
    sq = sub.add_parser("smoke-q", help="Qwen-only live smoke (group Q) on the synthetic corpus; --plan lists the calls")
    sq.add_argument("--config", required=True, help="configs/smoke-q.json (endpoint and model from SS_RECON_* variables)")
    sq.add_argument("--out", required=True)
    sq.add_argument("--plan", action="store_true", help="print the calls and the endpoint; open no connection")
    mc = sub.add_parser("model-check", help="Setup model check: the synthetic canary for one model pair (Q, then J)")
    mc.add_argument("--out", required=True, help="the pair's model-check folder (one run, resumed by the J session)")
    mc.add_argument("--stages", required=True, choices=("Q", "J"), help="Q, or J (J then M, then the canary is scored)")
    mc.add_argument("--families", help="role=family,... for the two-family judge rule (default: from the model ids)")
    mc.add_argument("--digests", help="role=<64-hex>,... weight digests to record (matched against tool.model_digests)")
    mc.add_argument("--remote-endpoints", metavar="FILE.json",
                    help="the app's declared remote endpoints, as tool.remote_endpoints (default: none, every "
                         "non-local endpoint is refused)")
    mc.add_argument("--endpoint-optins", metavar="FILE.json",
                    help="the app's opt-ins for LAN or Tailscale hosts, as tool.endpoint_optins ([] is valid; default: "
                         "not passed, the study guard)")
    fk = sub.add_parser("fork", help="fork a finished Q out_dir into a new out_dir for the judge phase (counts only)")
    fk.add_argument("--config", required=True, help="the config of the finished Q run (its out_dir is only read)")
    fk.add_argument("--out", required=True, help="new, empty folder: gets config.json and out/")
    fk.add_argument("--pairs", help="comma list of participant ids to keep (default: all)")
    fk.add_argument("--script-samples", type=int, help="pilot only: samples per script source variant")
    fk.add_argument("--sample", type=int, help="keep N participants drawn per role in proportion to the config's roles")
    fk.add_argument("--seed", type=int, default=0, help="seed of the --sample draw (default 0)")
    wd = sub.add_parser("widen", help="the judged pilot -> the full run, keeping the pilot's items (counts only)")
    wd.add_argument("--pilot-config", required=True, help="the pilot's config (made by fork, judged)")
    wd.add_argument("--config", required=True, help="the config of the finished Q run (all pairs; only read)")
    wd.add_argument("--out", required=True, help="new, empty folder: gets config.json and out/")
    jc = sub.add_parser("jcounts", help="judge pilot: counts, seconds per call and a projection (counts only)")
    jc.add_argument("--config", required=True, help="the pilot's config (made by fork)")
    jc.add_argument("--full-config", required=True, help="the config of the full run (the Q out_dir it counts units in)")
    jc.add_argument("--json", action="store_true")
    sj = sub.add_parser("sidejudge", help="side study on a FINISHED J run: second judge, planted cases, label order, "
                                           "off-record and aligner calibration (counts only)")
    sj.add_argument("--config", required=True, help="the J run's config (its out_dir is only read)")
    sj.add_argument("--out", required=True, help="the side-study folder (outside the J out_dir)")
    sj.add_argument("--parts", help="comma list of 2,3,4,6,7,8 (default: all, in that order)")
    sj.add_argument("--dry-count", action="store_true", help="print the calls per part and role; call nothing")
    sj.add_argument("--client", choices=("http", "oracle"), default="http")
    sj.add_argument("--judge-prefix", help="SS_<PREFIX>_* of the judge (default: the config's judge prefix)")
    sj.add_argument("--judge2-prefix", help="SS_<PREFIX>_* of the second judge (default: config's judge2, else JUDGE2)")
    sj.add_argument("--seed", type=int, default=0)
    sj.add_argument("--only-role", choices=("judge", "judge2"), help="run only this role's calls (one server at a time)")
    sj.add_argument("--sizes", help="sample sizes as key=value,... (keys: " + ", ".join(sorted(
        ("n_swap", "n_per_class", "n_reversed", "n_align", "n_canon", "n_determinism", "n_plant", "n_offneg",
         "n_cal", "n_cal_neg"))) + ")")
    sj.add_argument("--json", action="store_true")
    vn = sub.add_parser("validate-nomodel", help="no-model checks on a finished J out_dir (aggregates only, into --out)")
    vn.add_argument("--config", required=True, help="the config of the finished J run (its out_dir is only read)")
    vn.add_argument("--out", required=True, help="output folder, outside the run's out_dir")
    vn.add_argument("--checks", help="comma list of keyword, robustness, null, diffs (default: all)")
    vn.add_argument("--null-shifts", type=int, default=1, help="null: wrong transcripts per quote (default 1)")
    vn.add_argument("--loo-b", type=int, help="robustness: bootstrap resamples per left-out run (default 50)")
    vn.add_argument("--loo-perm", type=int, help="robustness: permutations per left-out run (default 50)")
    vn.add_argument("--debug", action="store_true", help="synthetic corpus only: raise errors with their message")
    pl = sub.add_parser("pool", help="place lab profiles side by side (never combined); writes OUT and OUT.md")
    pl.add_argument("--profiles", nargs="+", required=True, help="profile JSON files, one per lab")
    pl.add_argument("--out", required=True)
    ph = sub.add_parser("phantom", help="known-distance check: plant doses in the synthetic demo (oracle, no model calls)")
    ph.add_argument("--reps", type=int, default=20, help="replicates per dose level (default 20)")
    ph.add_argument("--B", type=int, default=2000, help="bootstrap replicates (default 2000)")
    ph.add_argument("--out", required=True, help="folder for phantom.json and phantom.md")
    rck = sub.add_parser("replay-check", help="prove shipped Try-it data is the recorded run (replay only, no model)")
    rck.add_argument("--config", required=True, help="the run config (configs/demo.json for Try it)")
    rck.add_argument("--fixtures", required=True, help="the recorded fixtures (v2: {messages_sha256: {calls: [...]}})")
    rck.add_argument("--recorded-log", required=True, help="the recording's logs/run.jsonl (its manifest next to it)")
    rck.add_argument("--view-js", required=True, help="the shipped view file (view_to_js output, e.g. tryit.js)")
    ce = sub.add_parser("certificate", help="the run certificate: what was run and whether the logs verify")
    ce.add_argument("--run", action="append", help="a run folder (with logs/run.jsonl); repeat for each run")
    ce.add_argument("--app-log", help="the project's logs/app.jsonl")
    ce.add_argument("--out", required=True, help="output FILE.json; the markdown is written next to it as FILE.md")
    ce.add_argument("--study", action="store_true", help="the study page's limit wording (run by the authors)")
    ep = sub.add_parser("export-profile", help="export the run's gate-safe distance profile (orgsci-profile/v2); exit 2 = refused")
    ep.add_argument("--config", required=True, help="the finished run's config (its out_dir is only read)")
    ep.add_argument("--out-dir", help="the run's out_dir when it is not the config's own")
    ep.add_argument("--profile", required=True, help="where to write the profile JSON")
    ep.add_argument("--B", type=int, default=2000, help="bootstrap resamples (default 2000)")
    ep.add_argument("--seed", type=int, default=20261017, help="bootstrap seed (default 20261017)")
    ep.add_argument("--model-info", help="JSON {role: {quantisation, weights, family, thinking}} for the run block")
    ep.add_argument("--reference-type", default="participant-endorsed", help="the reference type, a code of at most 40 characters of a-z 0-9 _ - (default participant-endorsed)")
    ep.add_argument("--withhold-chain-heads", action="store_true",
                    help="write run.run_chain_heads as null (run_chain_heads_source: available_from_authors)")
    ca = sub.add_parser("calibrate", help="known-distance check with a lab's B, seed and kit commit; adds detection_limits to its profile")
    ca.add_argument("--config", required=True, help="the lab's finished run config (read for the privacy scan and the people count)")
    ca.add_argument("--out-dir", help="the run's out_dir when it is not the config's own")
    ca.add_argument("--profile", required=True, help="the lab's profile JSON (calibrate rewrites it in place; keep a copy)")
    ca.add_argument("--reps", type=int, default=20, help="replicates per dose level (default 20)")
    ca.add_argument("--out", help="optional folder for phantom.json and phantom.md")
    sub.add_parser("bench", add_help=False, help="judge benchmark on the synthetic traps (own options: bench --help)")
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["bench"]:                       # the bench has its own command line (judge_bench.main)
        if (flag := _blank_out_flag(args[1:])):
            print(_blank_out_message(flag), file=sys.stderr)
            return 2
        from . import judge_bench
        return judge_bench.main(args[1:])
    a = ap.parse_args(argv)
    for name, flag in (("out", "--out"), ("out_dir", "--out-dir")):
        if getattr(a, name, None) is not None and not str(getattr(a, name)).strip():
            print(_blank_out_message(flag), file=sys.stderr)
            return 2
    try:
        if getattr(a, "record", None) and a.cmd in ("demo", "run"):
            if not is_synthetic(load_config(a.config)):
                print("refused: --record is only allowed for a synthetic corpus", file=sys.stderr)
                return 2
        return {"demo": cmd_demo, "run": cmd_run, "report": cmd_report, "verify": cmd_verify,
                "audit-leaks": cmd_audit_leaks, "prereg": cmd_prereg, "pool": cmd_pool,
                "export-profile": cmd_export_profile,
                "smoke-q": cmd_smoke_q, "model-check": cmd_model_check, "fork": cmd_fork, "jcounts": cmd_jcounts, "widen": cmd_widen,
                "sidejudge": cmd_sidejudge, "phantom": cmd_phantom, "calibrate": cmd_calibrate,
                "replay-check": cmd_replay_check,
                "certificate": cmd_certificate,
                "validate-nomodel": cmd_validate_nomodel}[a.cmd](a)
    except audits.AuditBlocked as exc:     # counts only; the evidence is in <out_dir>/audits/
        moved = getattr(exc, "stale_outputs", 0)
        print(f"stopped: {exc}" + (f"; {moved} earlier output(s) moved to <out_dir>/stale/" if moved else ""),
              file=sys.stderr)
        if getattr(exc, "details_path", None):
            print(f"details: {exc.details_path}", file=sys.stderr)
        return 4
    except RealGuardRefused as exc:        # a real corpus about to go somewhere it must not (checked before any call)
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ThinkingDetected as exc:        # the reply came from a model with thinking on: stop, write no item
        print(f"stopped: {exc}", file=sys.stderr)
        return 4
    except RuntimeError as exc:
        if str(exc).startswith(VALIDATE_RUNGS_PREFIX):     # ladder.run_rung's gate: a pinned prompt file was edited
            problems = str(exc)[len(VALIDATE_RUNGS_PREFIX):].strip()
            if "PINNED_SHA256" in problems:
                print(f"refused: {PINNED_REFUSAL}; {problems}", file=sys.stderr)
            else:
                print(f"refused: {exc}", file=sys.stderr)
            return 2
        if str(exc).startswith(("refusing to resume", "refusing to run")):   # broken log chain / prompts changed
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        if not str(exc).startswith("prereg"):
            if a.cmd == "audit-leaks":      # 1 means hits, never "error"
                print(f"audit-leaks could not run: {type(exc).__name__}", file=sys.stderr)
                return 2
            raise
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except prereg.CorpusError as exc:
        print(f"refused: the corpus is incomplete: {exc}. Fix the corpus folder or the roles/pairs in the config.",
              file=sys.stderr)
        return 2
    except FixtureMissing as exc:
        print(f"replay stopped: no recorded response for request {exc.args[0]} (re-record the fixtures)",
              file=sys.stderr)
        return 3
    except (pipeline.InputsRefused, RunRefused) as exc:     # refusals before writing run files
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (MapRefused, TranscriptRefused, zipfile.BadZipFile, ElementTree.ParseError, OSError) as exc:
        # safety net: a file that could not be read past the preflight is one line, never a traceback
        if a.cmd == "audit-leaks":      # 1 means hits, never "error"
            print(f"audit-leaks could not run: {type(exc).__name__}", file=sys.stderr)
            return 2
        print(f"refused: {_read_error_line(exc, _corpus_kind(a))}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        if a.cmd != "audit-leaks":
            raise
        print(f"audit-leaks could not run: {type(exc).__name__}", file=sys.stderr)    # 1 means hits, never "error"
        return 2


if __name__ == "__main__":
    sys.exit(main())
