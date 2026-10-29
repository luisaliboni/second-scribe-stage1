"""The export screen's outputs (Task 42; spec sections 9.1, 9.3, 9.5, 6.9; plan amendments V6, V13, V14).

Four outputs, in the order the screen offers them (V13: the report comes first, comparing with other labs after it):

1. **Report** (``export_report``): ``exports/report-<run_id>/index.html`` with ``css/``, ``js/`` and ``fonts/`` copied
   byte for byte from the viewer's static files, ``data/strings.js``, ``data/view.js`` (``export_view.view_to_js``,
   var ``report_view``), ``data/profile.js`` (var ``report_profile``) and the run record (``run-record.json`` and
   ``run-record.md``, T41). The page has the site's CSP meta and no inline script or style, so it opens offline from
   the folder. It carries the stamp (run, reference type, view, the sha256 of the two data files), the bands, the
   mode-B note for a B group, the declaration lines of remote roles, the profile panel and the lanes (``js/report.js``
   mounts the one viewer, then draws every map's lanes for print) and the run record summary. ``print.css`` keeps the
   shapes in black and white and repeats the run and reference on every printed page. PDF = the browser's Save as PDF.
2. **Profile for comparison** (V6, V14; code names keep ``profile``): the kit exporter (``profile_view.run_exporter``)
   writes the run's ``orgsci-profile/v2`` profile into ``exports/.staging/``; ``profile_preview`` stamps the k used in
   ``run.settings.gate_k_people``, runs ``publish.violations(doc, k, "orgsci-profile/v2", forbidden=...)`` with the
   project's own names (``forbidden_strings``), refuses a group under k ("group below k: not exportable"), warns when
   the people count differs from an earlier logged profile export by fewer than k (``differs_by_fewer_than_k``), and
   writes the exact bytes it would save (``publish.write_public``) so the screen shows every line. ``save_profile``
   saves only that previewed file (same sha256, same k), after ``assert_publishable`` passes again, to
   ``exports/profile-<run_id>.json`` + ``.csv``.
3. **Methods paragraph** (``export_methods``): an English draft, header "Draft for you to edit"; saved to
   ``exports/methods-<run_id>.txt``.
4. **Run record** (``save_certificate``): the T41 run record of the run folder and the app log, to
   ``exports/run-record-<run_id>.json`` + ``.md``.

Every save appends ``export{kind, sha256, run_id, path, ...}`` to the app log. The routes (``routes/export.py``)
register with ``coding.results_route``: refused while any mode-B coding is open, and the reveal is logged.

**Run lookup.** ``find_run`` is the only place that turns a run id into a folder (T40 builds its own lookup in
parallel; one function to swap). A run is a folder ``runs/<name>/`` holding ``config.json`` whose ``run_id`` is the
id; its reference group is the config's ``tool.reference_type``, else the folder name's first letter (T38 names
folders ``runs/<group>-<run_id>``). It is finished when ``logs/run.manifest.json`` and ``aggregates/aggregates.json``
are both there.

Nothing here calls a model: the view and the profile are read from the finished run folder by the kit's own
exporters, and the record from its logs.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import certificate, export_view, families, publish
from ..profile_export import K_FLOOR
from . import build_site, coding, profile_view, results, strings
from .api import ApiError
from .opener import OPENABLE
from .project import APP_LOG, Project, ProjectError

SCHEMA = publish.SCHEMAS[0]                       # "orgsci-profile/v2"
K_MIN = K_FLOOR                                   # the exporter's own floor (10 people); a stricter k is allowed
K_MAX = 1_000_000
GROUPS = ("A", "B", "C")
EXPORT_KINDS = ("report", "profile", "methods", "certificate")
# --reference-type for the exporter, per reference group (the profile's reference.type; A is the exporter's default)
REFERENCE_NAMES = {"A": "participant-endorsed", "B": "researcher-reading", "C": "none"}
RUNS = "runs"
STAGING = "exports/.staging"


def profile_rel(run_id: str) -> tuple[str, str]:
    """``(exports/profile-<run_id>.json, exports/profile-<run_id>.csv)``: one pair for each run, so saving a second
    run's profile never overwrites the first (the staging files are named the same way)."""
    return f"exports/profile-{run_id}.json", f"exports/profile-{run_id}.csv"


def run_record_rel(run_id: str) -> str:
    """``exports/run-record-<run_id>.json``; its markdown twin is ``.md``. (The kit module and the log kind keep the
    name ``certificate``; the file the user sees does not.)"""
    return f"exports/run-record-{run_id}.json"
VIEW_VAR, PROFILE_VAR = "report_view", "report_profile"

HEAD_CSS = "report-head.css"
REPORT_CSS = ("fonts.css", "tokens.css", "base.css", "viewer.css", "views.css", "ledger.css", "strip.css",
              "profile.css", "report.css", "print.css")
# the viewer's scripts in try.html's order, then the report's own
REPORT_JS = ("core.js", "layout.js", "encoding.js", "a11y.js", "lanes.js", "views.js", "state.js", "ledger.js",
             "strip.js", "profile.js", "drawer.js", "viewer.js", "report.js")
STATIC = Path(__file__).resolve().parent / "static"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_VIEW_KEYS = frozenset({"label", "quote", "pid", "text"})


def _err(status: int, code: str, **kw: Any) -> ApiError:
    return ApiError(status, code, strings.t(f"export.error.{code}", **kw))


# ------------------------------------------------------------------------------------------------ run lookup
@dataclass(frozen=True)
class RunRef:
    run_id: str
    group: str | None
    dir: Path
    config: Path
    finished: bool


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_ref(folder: Path) -> RunRef | None:
    cfg_path = folder / "config.json"
    cfg = _read_json(cfg_path) if cfg_path.is_file() else None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("run_id"), str):
        return None
    tool = cfg.get("tool") if isinstance(cfg.get("tool"), dict) else {}
    group = tool.get("reference_type") if tool.get("reference_type") in GROUPS else None
    if group is None and folder.name[:2] in ("A-", "B-", "C-"):
        group = folder.name[0]
    finished = (folder / "logs" / "run.manifest.json").is_file() and \
        (folder / "aggregates" / "aggregates.json").is_file()
    return RunRef(cfg["run_id"], group, folder, cfg_path, finished)


def _run_folders(project: Project) -> list[Path]:
    runs = project.path(RUNS)
    if not runs.is_dir():
        return []
    return [d for d in sorted(runs.iterdir()) if d.is_dir() and not d.is_symlink()]


def find_run(project: Project, run_id: Any) -> RunRef:
    """The run with this id (the one lookup of this module). 422 ``bad_run_id``, 404 ``run_not_found``."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise _err(422, "bad_run_id")
    for folder in _run_folders(project):
        ref = _run_ref(folder)
        if ref is not None and ref.run_id == run_id:
            return ref
    raise _err(404, "run_not_found")


def list_runs(project: Project) -> list[dict]:
    """``[{run_id, group, finished}]`` for every run folder, in folder order. Reads config files only."""
    out = []
    for folder in _run_folders(project):
        ref = _run_ref(folder)
        if ref is not None:
            out.append({"run_id": ref.run_id, "group": ref.group, "finished": ref.finished})
    return out


def export_target(project: Project, rel: Any, reveal: bool) -> Path:
    """The saved export at ``rel`` (a project-relative path such as ``exports/methods-<run_id>.txt``) for the "Open" and
    "Show in folder" buttons (scout F11). It must lie inside the project's ``exports`` folder, outside the preview's
    staging folder, after links are resolved, and exist; "Open" takes only a file of a kind in ``opener.OPENABLE``.
    422 ``bad_path`` / ``cannot_open``, 404 ``export_missing``."""
    if not isinstance(rel, str) or not rel or len(rel) > 512:
        raise _err(422, "bad_path")
    try:
        target = project.path(rel).resolve()
        exports = project.path("exports").resolve()
        staging = project.path(STAGING).resolve()
    except (ProjectError, OSError, RuntimeError):
        raise _err(422, "bad_path") from None
    if target == exports or not target.is_relative_to(exports) or target.is_relative_to(staging):
        raise _err(422, "bad_path")
    if not target.exists():
        raise _err(404, "export_missing")
    if not reveal and (not target.is_file() or target.suffix.lower() not in OPENABLE):
        raise _err(422, "cannot_open")
    return target


def require_finished(run: RunRef) -> RunRef:
    if not run.finished:
        raise _err(409, "run_unfinished")
    return run


# ------------------------------------------------------------------------------------------------ view and profile
def _run_config(run: RunRef):
    from ..pipeline import RunConfig
    return RunConfig.load(str(run.config), out_dir=str(run.dir))


def load_view(project: Project, run: RunRef) -> dict:
    """The run's ``view-data/1``: ``views/<run_id>.view.json`` when the runner left a valid one for this run, else
    built from the run folder by ``export_view.build_view`` (source ``synthetic`` for a synthetic corpus, else
    ``own``). 500 ``view_failed`` when neither works."""
    stored = project.path(f"views/{run.run_id}.view.json")
    if stored.is_file():
        view = _read_json(stored)
        if isinstance(view, dict) and not export_view.validate_view(view) and \
                (view.get("run") or {}).get("run_id") == run.run_id:
            return view
    try:
        cfg = _run_config(run)
        source = "synthetic" if (cfg.corpus or {}).get("kind") == "synthetic" else "own"
        try:
            view = export_view.build_view(cfg, source=source, reference_type=run.group)
        except ValueError:
            view = export_view.build_view(cfg, source=source, reference_type=None)
    except Exception:                                   # noqa: BLE001 - the details stay in the terminal
        raise _err(500, "view_failed") from None
    if export_view.validate_view(view):
        raise _err(500, "view_failed")
    return view


def _staging(project: Project) -> Path:
    d = project.path(STAGING)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_profile(project: Project, run: RunRef) -> tuple[dict, str]:
    """Run the kit exporter on the run into ``exports/.staging/profile-<run_id>.raw.json``; (doc, sha256 of the file).
    ``profile_view.ProfileRefused`` passes through (the screens word its reasons with ``results.refusal_messages``); any other failure is 500
    ``profile_failed``."""
    dest = _staging(project) / f"profile-{run.run_id}.raw.json"
    try:
        sha = profile_view.run_exporter(run.config, run.dir, dest, reference_type=REFERENCE_NAMES.get(run.group))
        doc = profile_view.load_profile(dest)
    except profile_view.ProfileRefused:
        raise
    except Exception:                                   # noqa: BLE001
        raise _err(500, "profile_failed") from None
    return doc, sha


# ------------------------------------------------------------------------------------------------ names and lines
def forbidden_strings(project: Project, view: dict) -> frozenset[str]:
    """Every string a lab profile must never carry, held in memory only: from the view, the step labels, quotes and
    pids; from the project, the stored files' names, stems and pids (the names as imported read from ``private/``),
    the reference pids and the mode-B codings' step names and marked text. ``publish`` compares them
    whitespace-collapsed and casefolded with every string."""
    out: set[str] = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in FORBIDDEN_VIEW_KEYS and isinstance(v, str):
                    out.add(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(view.get("maps") or [])
    for rel, meta in (project.data.get("files") or {}).items():
        out.add(Path(rel).name)
        out.add(Path(rel).stem)
        if isinstance(meta, dict):
            for key in ("name", "stem", "pid"):
                if isinstance(meta.get(key), str):
                    out.add(meta[key])
            if isinstance(meta.get("name"), str):
                out.add(Path(meta["name"]).stem)
    from .importer import original_names               # privacy ruling F: the names live only in private/
    for entry in original_names(project).values():
        for v in entry.values():
            if isinstance(v, str):
                out |= {v, Path(v).stem}
    pids = set(project.data.get("references") or {}) | set(project.data.get("maps") or {})
    out |= {p for p in pids if isinstance(p, str)}
    for pid in sorted(p for p in pids if isinstance(p, str)):
        try:
            marks = coding.load_coding(project, pid).get("marks") or []
        except Exception:                               # noqa: BLE001 - no coding for this pid
            continue
        for m in marks:
            for key in ("step", "text"):
                if isinstance(m, dict) and isinstance(m.get(key), str):
                    out.add(m[key])
    return frozenset(s for s in out if s.strip())


def declaration_lines(project: Project, run: RunRef) -> list[str]:
    """One sentence per remote role of the run (``tool.remote_endpoints`` of its config), exactly as
    ``certificate.remote_sentences`` words it (spec 8.1, audit K4): the report, the methods paragraph and the profile
    preview share this one source. ``[]`` when the run has no remote role."""
    cfg = _read_json(run.config)
    tool = cfg.get("tool") if isinstance(cfg, dict) and isinstance(cfg.get("tool"), dict) else {}
    if not tool.get("remote_endpoints"):
        return []
    head = tool.get("app_log_head") if isinstance(tool.get("app_log_head"), dict) else None
    return certificate.remote_sentences(project.log.records(), cfg, head)


def reference_name(group: str | None) -> str:
    return strings.t(f"export.reference.{(group or 'A').lower()}")


def view_group(view: dict, run: RunRef) -> str | None:
    """The reference type the results show: the view's own, else the run's group."""
    t = view.get("reference_type")
    return t if t in GROUPS else run.group


def run_record(project: Project, run: RunRef) -> dict:
    """The T41 certificate of this run folder and the project's app log (every log verified afresh)."""
    return certificate.build_certificate([run.dir], project.path(APP_LOG))


def _log_export(project: Project, kind: str, sha256: str, run: RunRef, path: str, **extra: Any) -> dict:
    return project.log.append("export", {"kind": kind, "sha256": sha256, "run_id": run.run_id, "path": path,
                                         **extra})


def export_records(project: Project, run_id: str | None = None) -> list[dict]:
    recs = [r["payload"] for r in project.log.records("export") if isinstance(r.get("payload"), dict)]
    return [p for p in recs if run_id is None or p.get("run_id") == run_id]


def outputs(project: Project, run_id: str) -> list[dict]:
    """The four outputs of a run, in screen order: ``{kind, saved, path, sha256}`` (the latest logged save)."""
    recs = export_records(project, run_id)
    out = []
    for kind in EXPORT_KINDS:
        last = [p for p in recs if p.get("kind") == kind]
        p = last[-1] if last else {}
        out.append({"kind": kind, "saved": bool(last), "path": p.get("path"), "sha256": p.get("sha256")})
    return out


# ------------------------------------------------------------------------------------------------ report
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def folder_sha256(folder: Path) -> str:
    """sha256 over ``<relative path> <sha256 of the file>`` lines of every file in the folder, sorted."""
    lines = []
    for p in sorted(Path(folder).rglob("*")):
        if p.is_file():
            lines.append(f"{p.relative_to(folder).as_posix()} {_sha(p.read_bytes())}\n")
    return _sha("".join(lines).encode("utf-8"))


def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _t(key: str, **kw: Any) -> str:
    return _e(strings.t(key, **kw))


def _record_html(cert: dict) -> str:
    L = [f'<section class="report-record" aria-labelledby="report-record-title">',
         f'<h2 id="report-record-title">{_t("report.record.title")}</h2>',
         f'<p class="report-attests">{_e(cert["attests"])}</p>']
    for run in cert["runs"]:
        v = run["verify"]
        if v["ok"]:
            check = (f'{_t("report.record.pass", records=format(v["records"], ","))} '
                     f'<code class="report-hash">{_e(v["head_hash"])}</code>')
        else:
            check = _t("report.record.fail", errors=", ".join(v["errors"]) or "unknown")
        L += [f'<h3>{_t("report.record.run", run_id=run["run_id"] or "")}</h3>', '<dl class="report-dl">',
              f'<dt>{_t("report.record.outcome")}</dt><dd>{_e(run["outcome"])}</dd>',
              f'<dt>{_t("report.record.log_check")}</dt><dd>{check}</dd>',
              f'<dt>{_t("report.record.checked")}</dt><dd>{_e(v.get("checked_at", cert["checked_at"]))}</dd>']
        for key, label in (("code_sha256", "report.record.code"), ("config_sha256", "report.record.config"),
                           ("prompt_set_sha256", "report.record.prompts")):
            if run.get(key):
                L.append(f'<dt>{_t(label)}</dt><dd><code class="report-hash">{_e(run[key])}</code></dd>')
        models = "; ".join(strings.t("report.record.model", role=r, id=m.get("id") or "an unrecorded model")
                           for r, m in sorted((run.get("models") or {}).items()))
        if models:
            L.append(f'<dt>{_t("report.record.models")}</dt><dd>{_e(models)}</dd>')
        for rem in run.get("remote") or []:
            L.append(f'<dt>{_t("report.record.remote")}</dt><dd>{_e(" · ".join(rem.get("notes") or []))}</dd>')
        L.append("</dl>")
    app = cert.get("app_log")
    if app:
        v = app["verify"]
        verdict = (strings.t("report.record.pass", records=format(v["records"], ",")) + " " + str(v["head_hash"])
                   if v["ok"] else strings.t("report.record.fail", errors=", ".join(v["errors"]) or "unknown"))
        L += ['<dl class="report-dl">', f'<dt>{_t("report.record.app_log")}</dt><dd>'
              f'{_t("report.record.app_log_value", verdict=verdict, locks=app["coding_locked"]["count"], reveals=app["reveal"]["count"])}'
              '</dd></dl>']
    L.append(f'<h3>{_t("report.record.limit_label")}</h3>')
    if cert.get("blindness_limit"):
        L.append(f'<p class="report-limit">{_e(cert["blindness_limit"])}</p>')
    L.append(f'<p class="report-limit">{_e(cert["limit"])}</p>')
    L.append(f'<p class="report-files">{_t("report.record.files", md="run-record.md", json="run-record.json")}</p>')
    L.append("</section>")
    return "\n".join(L)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day(stamp: Any) -> str:
    """An ISO time stamp as a reader writes the day ("23 Oct 2026", UTC); the first ten characters when unreadable."""
    try:
        d = datetime.fromisoformat(str(stamp)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return str(stamp)[:10]
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _facts_line(run: RunRef) -> str:
    """The run in words, as the Results list writes it (scout F10): start date (UTC), the instructions used (item 19's
    names), the people and the models, joined by " · ". Facts the run does not hold are left out."""
    ref = results.RunRef(run_id=run.run_id, group=run.group or "A", folder=run.dir, config_path=run.config,
                         out_dir=run.dir)
    f = results.run_facts(ref)
    out = []
    try:
        d = datetime.fromisoformat(str(f["started"])).astimezone(timezone.utc)
        out.append(f"{d.day} {_MONTHS[d.month - 1]} {d.year}, {d:%H:%M} UTC")
    except (TypeError, ValueError):
        pass
    names = [strings.t("results.run.instructions_own") if r == "rung1" and f["rung1_own"] else strings.t(f"ledger.rung.{r}")
             for r in ("rung1", "rung2", "rung3", "placebo") if r in f["rungs"]]
    if names:
        out.append("; ".join(names))
    n = f["n_people"]
    if isinstance(n, int):
        out.append(strings.t("results.run.people_one") if n == 1 else strings.t("results.run.people", n=n))
    m = f["models"]
    if m.get("recon"):
        out.append(strings.t("results.run.models", recon=m["recon"], judge=m["judge"]) if m.get("judge")
                   else strings.t("results.run.models_no_judge", recon=m["recon"]))
    return " · ".join(out)


def _report_html(*, run: RunRef, view: dict, group: str | None, view_sha: str, profile_sha: str | None,
                 refused: list[str], declarations: list[str], cert: dict, made: str) -> str:
    ref = reference_name(group)
    synthetic = view.get("source") == "synthetic"
    if synthetic:
        band = f'<p class="report-band report-band-synthetic">{_t("band.synthetic")}</p>'
        source = strings.t("band.synthetic")
    else:
        word = {"B": "researcher", "C": "none"}.get(group or "A", "participant")
        source = strings.t("band.own", reference=strings.t(f"band.reference.{word}"))
        band = f'<p class="report-band report-band-own">{_e(source)}</p>'
    css = "".join(f'<link rel="stylesheet" href="css/{c}">\n' for c in REPORT_CSS + (HEAD_CSS,))
    data = ["data/strings.js", "data/view.js"] + (["data/profile.js"] if profile_sha else [])
    scripts = "".join(f'<script src="{s}"></script>\n' for s in data + [f"js/{j}" for j in REPORT_JS])
    title = strings.t("report.title") + " · " + run.run_id + " · " + strings.t("app.name")
    stamp = [("report.stamp.run", f'<code class="report-id">{_e(run.run_id)}</code>'),
             ("report.stamp.reference", _e(ref)),
             ("report.stamp.view", _t("report.stamp.view_value")),
             ("report.stamp.source", _e(source)),
             ("report.stamp.made", _e(made)),
             ("report.stamp.view_file", f'<code class="report-hash">{view_sha}</code>')]
    if profile_sha:
        stamp.append(("report.stamp.profile_file", f'<code class="report-hash">{profile_sha}</code>'))
    B = [f'<p class="report-modeb">{_t("report.mode_b")}</p>',
         f'<p class="report-modeb-note">{_t("strip.mode_b_note")}</p>'] if group == "B" else []
    decl = []
    if declarations:
        decl = ['<section class="report-declaration" aria-labelledby="report-declaration-title">',
                f'<h2 id="report-declaration-title">{_t("report.declaration.title")}</h2>']
        decl += [f'<p class="report-declaration-line">{_e(line)}</p>' for line in declarations] + ["</section>"]
    refused_html = ""
    if refused:
        items = "".join(f"<li>{_e(r)}</li>" for r in refused)
        refused_html = (f'<div class="report-refused"><p>{_t("export.error.profile_failed")}</p>'
                        f'<ul>{items}</ul></div>\n')
    facts = _facts_line(run)
    body = "\n".join([
        f'<div class="report-running" aria-hidden="true">{_t("report.running", run_id=run.run_id, reference=ref)}</div>',
        '<header class="report-head">',
        f'<p class="report-app">{_t("app.name")}</p>',
        f'<h1>{_t("report.heading")}</h1>',
        f'<p class="report-lede">{_t("report.lede")}</p>',
        band,
        f'<button type="button" id="report-print" class="primary report-print">{_t("report.print")}</button>',
        '</header>',
        # Part 5a (scout F9): plain sentences from the run's own profile first (js/report.js fills #report-lead
        # with SS.profile.renderLead), then the maps; the stamp, its hashes, the profile panels and the run record
        # sit in one closed "Technical details", which report.js opens for printing.
        '<section class="report-lead-section" aria-labelledby="report-lead-title">',
        f'<h2 id="report-lead-title">{_t("report.lead.title")}</h2>',
        f'<p class="report-facts">{_e(facts)}</p>' if facts else "",
        refused_html + '<div id="report-lead" class="report-lead" data-part="lead"></div>',
        f'<noscript><p class="report-note">{_t("report.lead.noscript")}</p></noscript>',
        *B,
        '</section>',
        *decl,
        '<section class="report-results" aria-labelledby="report-results-title">',
        f'<h2 id="report-results-title">{_t("report.results.title")}</h2>',
        '<div id="report-viewer" class="report-viewer"></div>',
        '<div id="report-maps" class="report-maps"></div>',
        f'<noscript><p class="report-note">{_t("report.noscript")}</p></noscript>',
        '</section>',
        '<details class="report-details" id="report-technical" data-part="technical">',
        f'<summary class="report-details-summary">{_t("report.details.summary")}</summary>',
        '<div class="report-details-body">',
        '<section class="report-stamp" aria-labelledby="report-stamp-title">',
        f'<h2 id="report-stamp-title">{_t("report.stamp.title")}</h2>',
        '<dl class="report-dl">',
        *[f"<dt>{_t(k)}</dt><dd>{v}</dd>" for k, v in stamp],
        '</dl>',
        f'<p class="report-how">{_t("report.how_to_read")}</p>',
        '</section>',
        '<section class="report-profile-section" aria-labelledby="report-profile-title">' if profile_sha else "",
        f'<h2 id="report-profile-title">{_t("report.profile.title")}</h2>' if profile_sha else "",
        '<div id="report-profile" class="report-profile"></div>' if profile_sha else "",
        '</section>' if profile_sha else "",
        _record_html(cert),
        '</div>',
        '</details>',
    ])
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"{build_site.CSP_META}\n<title>{_e(title)}</title>\n{css}</head>\n"
            f"<body class=\"report\">\n<main class=\"report-main\">\n{body}\n</main>\n{scripts}</body>\n</html>\n")


def css_string(text: str) -> str:
    """``text`` as a quoted CSS string: backslash and quote escaped, a line break as ``\\a``, other control
    characters dropped."""
    out = []
    for ch in str(text):
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\a ")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            continue
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _head_css(running: str) -> str:
    """The run line in the top margin box of every printed page (the page's own content never sits under it)."""
    return ("/* Written by the export for this run (Task 42): the line at the top of every printed page. */\n"
            "@page {\n  @top-left {\n"
            f"    content: {css_string(running)};\n"
            '    font-family: "Schibsted Grotesk", system-ui, sans-serif;\n    font-size: 8pt;\n    color: #000000;\n'
            "    vertical-align: bottom;\n    padding-bottom: 2mm;\n    border-bottom: 0.5pt solid #000000;\n"
            "  }\n}\n")


def _report_files(view: dict, profile: dict | None, running: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for c in REPORT_CSS:
        files[f"css/{c}"] = (STATIC / "css" / c).read_bytes()
    files[f"css/{HEAD_CSS}"] = _head_css(running).encode("utf-8")
    for j in REPORT_JS:
        files[f"js/{j}"] = (STATIC / "js" / j).read_bytes()
    for f in sorted((STATIC / "fonts").iterdir()):
        if f.is_file() and not f.name.startswith("."):
            files[f"fonts/{f.name}"] = f.read_bytes()
    files["data/strings.js"] = build_site._strings_js(strings.load_strings("en")).encode("utf-8")
    files["data/view.js"] = export_view.view_to_js(view, VIEW_VAR).encode("utf-8")
    if profile is not None:
        files["data/profile.js"] = build_site._ss_data_js(profile, PROFILE_VAR).encode("utf-8")
    return files


def _replace_folder(project: Project, final: Path, files: dict[str, bytes], extra) -> None:
    """Write ``files`` into a fresh sibling folder, let ``extra(folder)`` add to it, then swap it into ``final``."""
    exports = project.path("exports")
    tmp = exports / f".{final.name}.tmp"
    old = exports / f".{final.name}.old"
    for p in (tmp, old):
        if p.parent == exports and p.name.startswith(".report-") and p.exists():
            shutil.rmtree(p)
    tmp.mkdir(parents=True)
    for rel, data in files.items():
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    extra(tmp)
    if final.exists():
        os.replace(final, old)
    os.replace(tmp, final)
    if old.exists():
        shutil.rmtree(old)


def export_report(project: Project, run_id: str, state: Any = None) -> Path:
    """Write ``exports/report-<run_id>/`` (see the module docstring), log ``export{kind: "report"}``, return the path
    of its ``index.html``. ``state`` (the app state) is not needed: everything is read from the project."""
    run = require_finished(find_run(project, run_id))
    view = load_view(project, run)
    group = view_group(view, run)
    try:
        profile, _sha_raw = stage_profile(project, run)
        refused: list[str] = []
    except profile_view.ProfileRefused as e:
        results.log_refusal(run.run_id, e.reasons)               # the exporter's own words: the terminal log only
        profile, refused = None, [m["message"] for m in results.refusal_messages(e.reasons)]
    cert = run_record(project, run)
    files = _report_files(view, profile, strings.t("report.running", run_id=run.run_id,
                                                   reference=reference_name(group)))
    made = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page = _report_html(run=run, view=view, group=group, view_sha=_sha(files["data/view.js"]),
                        profile_sha=_sha(files["data/profile.js"]) if profile is not None else None,
                        refused=refused, declarations=declaration_lines(project, run), cert=cert, made=made)
    files["index.html"] = page.encode("utf-8")
    final = project.path(f"exports/report-{run.run_id}")
    _replace_folder(project, final, files, lambda folder: certificate.write_certificate(
        cert, folder / "run-record.json"))
    rel = project.rel(final / "index.html")
    _log_export(project, "report", folder_sha256(final), run, rel)
    return final / "index.html"


# ------------------------------------------------------------------------------------------------ profile
def check_k(k: Any) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or not K_MIN <= k <= K_MAX:
        raise _err(422, "bad_k")
    return k


def _refusal(code: str, where: str) -> dict:
    key = f"export.refusal.{code}"
    try:
        message = strings.t(key, where=where)
    except KeyError:
        message = strings.t("export.refusal.other", code=code, where=where)
    return {"code": code, "where": where, "message": message}


def _people(doc: dict) -> int | None:
    n = (doc.get("population") or {}).get("n_people") if isinstance(doc.get("population"), dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def _checks(project: Project, run: RunRef, doc: dict, k: int,
            forbidden: frozenset[str]) -> tuple[list[dict], list[dict]]:
    """(refusals, warnings) for ``doc`` at ``k``."""
    refusals: list[dict] = []
    n = _people(doc)
    if n is None or n < k:
        refusals.append({"code": "group_below_k", "where": "$.population.n_people",
                         "message": strings.t("export.refusal.group_below_k")})
    refusals += [_refusal(code, where) for code, where in publish.violations(doc, k, SCHEMA, forbidden=forbidden)]
    warnings: list[dict] = []
    seen = set()
    for rec in export_records(project):
        prev = rec.get("n_people")
        if rec.get("kind") != "profile" or not isinstance(prev, int) or prev in seen:
            continue
        seen.add(prev)
        if n is not None and publish.differs_by_fewer_than_k({"population": {"n_people": prev}}, doc, k):
            warnings.append({"code": "differencing", "message": strings.t(
                "export.warning.differencing", prev=prev, n=n, diff=abs(n - prev), k=k)})
    return refusals, warnings


def _stamp_k(doc: dict, k: int) -> dict:
    settings = (doc.get("run") or {}).get("settings") if isinstance(doc.get("run"), dict) else None
    if isinstance(settings, dict):
        settings["gate_k_people"] = k
    return doc


def _staged(project: Project, run_id: str) -> tuple[Path, Path, Path]:
    d = _staging(project)
    return d / f"profile-{run_id}.json", d / f"profile-{run_id}.csv", d / f"profile-{run_id}.meta.json"


def profile_preview(project: Project, run_id: str, k: Any) -> dict:
    """What ``save_profile`` would write, line by line, with the refusals and warnings (nothing is saved or logged).
    ``doc`` is the document; the route leaves it out and sends ``json_text`` instead."""
    k = check_k(k)
    run = require_finished(find_run(project, run_id))
    out: dict[str, Any] = {"run_id": run.run_id, "group": run.group, "k": k, "declaration":
                           declaration_lines(project, run), "warnings": []}
    js, cs, meta = _staged(project, run.run_id)
    for p in (js, cs, meta):
        if p.exists():
            p.unlink()
    try:
        doc, _raw = stage_profile(project, run)
    except profile_view.ProfileRefused as e:
        results.log_refusal(run.run_id, e.reasons)               # the exporter's own words: the terminal log only
        out.update(refusals=[{"code": m["code"], "where": "", "message": m["message"]}
                             for m in results.refusal_messages(e.reasons)],
                   exportable=False, preview=[], json_text="", sha256=None, n_people=None, state=None, checks=None,
                   doc=None)
        return out
    _stamp_k(doc, k)
    refusals, warnings = _checks(project, run, doc, k, forbidden_strings(project, load_view(project, run)))
    sha_json, sha_csv = publish.write_public(doc, js, cs)
    exportable = not refusals
    meta.write_text(json.dumps({"k": k, "sha256": sha_json, "sha256_csv": sha_csv, "exportable": exportable},
                               sort_keys=True) + "\n", encoding="utf-8")
    out.update(refusals=refusals, warnings=warnings, exportable=exportable, sha256=sha_json, sha256_csv=sha_csv,
               preview=cs.read_text(encoding="utf-8").splitlines(), json_text=js.read_text(encoding="utf-8"),
               n_people=_people(doc), state=doc.get("state"), checks=preview_checks(doc), doc=doc)
    return out


def preview_checks(doc: dict) -> dict:
    """The three plain checks above the raw lines of the preview (scout F12): ``smallest_group`` (the fewest people
    any group number of the file describes: the population and each row's ``n_people``; None when none is given),
    ``not_run`` (cells marked ``state: not_run``) and ``cells`` (all cells). Counts only; nothing from the cells."""
    sizes = [_people(doc)]
    cells = not_run = 0
    for row in doc.get("rows") or []:
        if not isinstance(row, dict):
            continue
        n = row.get("n_people")
        sizes.append(n if isinstance(n, int) and not isinstance(n, bool) else None)
        for c in row.get("cells") or []:
            if isinstance(c, dict):
                cells += 1
                not_run += c.get("state") == "not_run"
    sizes = [n for n in sizes if n is not None]
    return {"smallest_group": min(sizes) if sizes else None, "not_run": not_run, "cells": cells}


def save_profile(project: Project, run_id: str, k: Any, sha256: Any) -> dict:
    """Save the previewed profile (same k, same sha256) to ``exports/profile-<run_id>.json`` + ``.csv`` and log it.
    409 ``no_preview`` / ``preview_changed``; 422 ``not_exportable`` when the gate refuses (again)."""
    k = check_k(k)
    run = require_finished(find_run(project, run_id))
    js, cs, meta_path = _staged(project, run.run_id)
    meta = _read_json(meta_path) if meta_path.is_file() else None
    if not isinstance(meta, dict) or not js.is_file():
        raise _err(409, "no_preview")
    if meta.get("k") != k or meta.get("sha256") != sha256 or _sha(js.read_bytes()) != sha256:
        raise _err(409, "preview_changed")
    if not meta.get("exportable"):
        raise _err(422, "not_exportable")
    doc = json.loads(js.read_text(encoding="utf-8"))
    forbidden = forbidden_strings(project, load_view(project, run))
    refusals, warnings = _checks(project, run, doc, k, forbidden)
    try:
        if refusals:
            raise publish.PublishRefused(refusals[0]["code"], refusals[0]["where"])
        publish.assert_publishable(doc, k, SCHEMA, forbidden=forbidden)
    except publish.PublishRefused:
        raise _err(422, "not_exportable") from None
    rel_json, rel_csv = profile_rel(run.run_id)
    sha_json, sha_csv = publish.write_public(doc, project.path(rel_json), project.path(rel_csv))
    if sha_json != sha256:
        raise _err(409, "preview_changed")
    _log_export(project, "profile", sha_json, run, rel_json, sha256_csv=sha_csv, k=k, n_people=_people(doc))
    meta_path.unlink()
    return {"path": rel_json, "path_csv": rel_csv, "sha256": sha_json, "sha256_csv": sha_csv,
            "warnings": warnings}


# ------------------------------------------------------------------------------------------------ methods
def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def last_k(project: Project) -> int:
    """The k of the latest saved profile, else the floor (10)."""
    ks = [p.get("k") for p in export_records(project) if p.get("kind") == "profile" and isinstance(p.get("k"), int)]
    return ks[-1] if ks else K_MIN


_FAMILY_DISPLAY = {"qwen": "Qwen", "deepseek": "DeepSeek", "gemma": "Gemma", "llama": "Llama", "mistral": "Mistral",
                   "gpt-oss": "gpt-oss", "phi": "Phi", "glm": "GLM", "kimi": "Kimi", "minimax": "MiniMax",
                   "claude": "Claude"}


def _family_name(model_id: Any, declared: Any) -> str | None:
    """A model family as a reader writes it ("Qwen", never the config key "qwen2"): the family of the model id when
    the family table knows it, else the family of the settings' declared name; None when neither is known."""
    for name in (model_id, declared):
        fam = families.known_family(name if isinstance(name, str) else None)
        if fam:
            return _FAMILY_DISPLAY.get(fam, fam)
    return None


def export_methods(project: Project, run_id: str) -> str:
    """The methods paragraph (spec 9.5): models and families, reference type, rungs, controls, k, profile schema and
    the comparison rule, the record's hashes, and the declaration lines of remote roles. Header "Draft for you to
    edit"; built from en.json sentences only, so the wording lint covers every word but the filled-in values."""
    run = require_finished(find_run(project, run_id))
    view = load_view(project, run)
    group = view_group(view, run)
    cert = run_record(project, run)
    r0 = cert["runs"][0]
    cfg = _read_json(run.config) or {}
    tool = cfg.get("tool") if isinstance(cfg.get("tool"), dict) else {}
    models = (view.get("run") or {}).get("models") or {}
    recon, judge = models.get("recon"), models.get("judge")
    S = [strings.t("methods.models", n=len(view.get("maps") or []), recon=recon or "an unrecorded model", judge=judge or "an unrecorded model")
         if judge else strings.t("methods.no_judge")]
    fam = tool.get("model_families") if isinstance(tool.get("model_families"), dict) else {}
    if judge:
        f_recon, f_judge = _family_name(recon, fam.get("recon")), _family_name(judge, fam.get("judge"))
        if f_recon and f_judge:
            S.append(strings.t("methods.families", recon=f_recon, judge=f_judge) if f_recon != f_judge
                     else strings.t("methods.families_same", family=f_recon))
    note = strings.t("strip.mode_b_note")
    S.append(strings.t(f"methods.reference.{(group or 'A').lower()}", note=note))
    # Part 5a (scout F13): plain instruction names in the study's order; hashes only on the final Records line.
    rungs = [r for r in ("rung1", "rung2", "rung3", "placebo") if r in (view.get("rungs") or [])]
    own = r0.get("rung1_user_prompt_sha256")
    names = [strings.t("methods.rung.rung1_user" if r == "rung1" and own else f"methods.rung.{r}") for r in rungs]
    if len(names) > 1:
        S.append(strings.t("methods.rungs", rungs=_join(names)))
        ladder_cfg = cfg.get("ladder") if isinstance(cfg.get("ladder"), dict) else {}
        if ladder_cfg.get("draws", 1) == 1:
            S.append(strings.t("methods.rungs_once"))
    elif names:
        S.append(strings.t("methods.rungs_one", rungs=names[0]))
    S.append(strings.t("methods.control.no_interview"))
    if (group or "A") != "C":
        S.append(strings.t("methods.control.floors"))
    S.append(strings.t("methods.profile", k=last_k(project)))
    S.append(strings.t("methods.rule"))
    v = r0["verify"]
    S.append(strings.t("methods.record", date=_day(v.get("checked_at", cert["checked_at"]))))
    if not v.get("ok"):
        S.append(strings.t("methods.records_note", errors=", ".join(v.get("errors") or []) or "unknown"))
    S += declaration_lines(project, run)
    records = strings.t("methods.records", code=r0.get("code_sha256"), config=r0.get("config_sha256"),
                        prompts=r0.get("prompt_set_sha256"), head=v.get("head_hash"))
    if own:
        records += " " + strings.t("methods.records.rung1_user", sha256=own)
    return strings.t("methods.header") + "\n\n" + " ".join(S) + "\n\n" + records + "\n"


def save_methods(project: Project, run_id: str) -> dict:
    text = export_methods(project, run_id)
    run = find_run(project, run_id)
    rel = f"exports/methods-{run.run_id}.txt"
    from ..report import write_text_atomic
    write_text_atomic(project.path(rel), text)
    sha = _sha(text.encode("utf-8"))
    _log_export(project, "methods", sha, run, rel)
    return {"path": rel, "sha256": sha, "text": text}


# ------------------------------------------------------------------------------------------------ run record
def save_certificate(project: Project, run_id: str) -> dict:
    """The T41 record of the run and the app log, to ``exports/run-record-<run_id>.json`` + ``.md``; logged."""
    run = require_finished(find_run(project, run_id))
    cert = run_record(project, run)
    js, md = certificate.write_certificate(cert, project.path(run_record_rel(run.run_id)))
    sha = _sha(js.read_bytes())
    rel = project.rel(js)
    _log_export(project, "certificate", sha, run, rel, sha256_md=_sha(md.read_bytes()))
    return {"path": rel, "path_md": project.rel(md), "sha256": sha, "all_logs_verify": cert["all_logs_verify"]}
