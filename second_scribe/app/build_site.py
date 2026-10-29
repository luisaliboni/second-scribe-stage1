"""``make app``: build the offline site (Explain · The study · Try it · Install) from the app's static files (Task 28).

Contract with Tasks 29 and 30: ``app/build/site-contract.md``.

``build_site(out, *, tryit, tryit_profile, study=None, recorded=None, config=DEMO_CONFIG, pages_dir=PAGES) -> list``

* **Pages.** ``pages/<name>.html`` are body fragments. Their first lines are header comments: ``title_key`` (required),
  ``scripts`` and ``styles`` (paths relative to the site, space separated). Each fragment is wrapped in
  ``pages/shell.html``: doctype, the exact CSP meta (``CSP_META``), ``fonts.css tokens.css base.css site.css``, the
  nav with the current page marked, the theme toggle; then the listed styles and scripts, in order. ``{{t:key}}`` in
  a fragment is the ``en.json`` text of ``key``, HTML-escaped (an unknown key raises ``KeyError``). A fragment that
  holds an inline script, a ``<style>`` element, a ``style=`` attribute, an ``on*=`` handler or a ``javascript:`` URL
  is refused, as is a script or style the build does not ship. ``explain.html`` becomes ``index.html``; a missing
  fragment becomes a plain placeholder page.
* **Static files**, from an allow-list (``static_files``): ``fonts/*``, ``css/*.css``, ``js/*.js`` without
  ``js/app-*.js`` and ``js/api.js``. Never ``static/dev/``, never ``app.html``.
* **Data** in ``out/data/``: ``strings.js`` (``window.SS_STRINGS``), ``tryit.js`` (the input's bytes),
  ``tryit-profile.js`` (``SS_DATA["tryit_profile"]``, a synthetic profile), ``study.js`` (``SS_DATA["study"]``: the
  ``--study`` profile, or the stub ``{"state": "not_yet_run"}``), ``explain.js`` when T29's ``explain_data`` module
  is present, and ``SOURCES.json``, which accounts for every other file there.
* **Refusals** (``ValueError``; nothing is written): a ``dev_only`` view (``"dev_only view data cannot be shipped"``),
  a file that is not a ``view_to_js`` view, a view whose source is not ``synthetic``, a Try-it profile whose state is
  not ``synthetic``, a study profile whose state is ``synthetic`` (real and synthetic never share a page), and, with
  ``recorded``, a failed ``replay-check`` (``"replay-check failed: <reasons>"``). ``out`` must be absent, empty or an
  earlier build (it holds ``data/SOURCES.json``); files an earlier build left and this one does not write are removed.

``recorded`` is the recording folder (``demo/recorded``: ``demo-live-fixtures.v2.json``, ``run.jsonl`` and its
manifest). With it, ``build_site`` runs ``replay_check`` itself on ``tryit`` and marks the data as recorded only when
it passes, so ``SOURCES.json`` ``tryit.recorded`` is true exactly when the check passed in this build.

CLI: ``python3 -m second_scribe.app.build_site --out site --tryit T.js --tryit-profile P.json [--study S.json]
[--recorded demo/recorded]`` (exit 0, or 2 with the reason on stderr), and
``python3 -m second_scribe.app.build_site tryit-js --run RUN_DIR --out T.js``: the exporter's ``view_to_js`` file of a
replayed demo run (``export_view.build_view``, source ``synthetic``), and
``python3 -m second_scribe.app.build_site tryit-profile --run RUN_DIR --out P.json [--config C]``: the Try it profile,
the kit's ``export-profile`` run on that same finished run (``profile_view.build_tryit_profile``), so the profile and
the lanes come from one run. No model is called by any of them.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import re
import sys
from pathlib import Path

from . import profile_view, strings

APP_DIR = Path(__file__).resolve().parent
KIT_ROOT = APP_DIR.parents[1]
PAGES = APP_DIR / "pages"
STATIC = APP_DIR / "static"
DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"

CSP = ("default-src 'self'; connect-src 'none'; img-src 'self' data:; font-src 'self'; style-src 'self'; "
       "script-src 'self'")
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'

# (fragment name, output file, nav string key), in nav order
SITE_PAGES = (("explain", "index.html", "nav.explain"), ("study", "study.html", "nav.study"),
              ("try", "try.html", "nav.try"), ("install", "install.html", "nav.install"))
STUDY_STUB = {"state": "not_yet_run"}
RECORDED_FILES = ("demo-live-fixtures.v2.json", "run.jsonl", "run.manifest.json")

_HEADER = re.compile(r"\A(?:[ \t]*<!--\s*([a-z_]+):\s*(.*?)\s*-->[ \t]*\n)")
_T_TOKEN = re.compile(r"\{\{t:([a-z0-9_]+(?:\.[a-z0-9_]+)+)\}\}")
_SLOT = re.compile(r"\{\{slot:([a-z_]+)\}\}")
_SHELL_SLOT = re.compile(r"\{\{([a-z]+)\}\}")
_INLINE = ((re.compile(r"<script\b", re.I), "script element (list scripts in the header)"),
           (re.compile(r"<style\b", re.I), "style element"),
           (re.compile(r"\sstyle\s*=", re.I), "style attribute"),
           (re.compile(r"\son[a-z]+\s*=", re.I), "on* handler attribute"),
           (re.compile(r"javascript\s*:", re.I), "javascript: URL"),
           (re.compile(r"https?://", re.I), "remote URL"))


# ------------------------------------------------------------------------------------------------ helpers
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ss_data_js(doc, var: str) -> str:
    """``window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["<var>"] = <json>;`` with ``profile_to_js``'s JSON
    and escaping (safe inside a ``<script>`` element)."""
    js = profile_view.profile_to_js(doc, var)
    body = js[len(f"window.{var} = "):-1]
    return f'window.SS_DATA = window.SS_DATA || {{}}; window.SS_DATA["{var}"] = {body};'


def _strings_js(table: dict) -> str:
    js = profile_view.profile_to_js(table, "SS_STRINGS")
    return js


def static_files(static_dir: Path = STATIC) -> list[str]:
    """The static files the site ships, as sorted paths relative to ``static_dir``."""
    out = []
    for p in sorted(static_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(static_dir).as_posix()
        parts = rel.split("/")
        if len(parts) != 2:
            continue
        folder, name = parts
        if folder == "fonts" or (folder == "css" and name.endswith(".css")):
            out.append(rel)
        elif folder == "js" and name.endswith(".js") and not name.startswith("app-") and name != "api.js":
            out.append(rel)
    return sorted(out)


def _read_view(path: Path) -> tuple[bytes, dict]:
    from ..replay_check import parse_view_js     # lazy: replay_check imports the whole kit
    raw = Path(path).read_bytes()
    try:
        var, view = parse_view_js(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError(f"{Path(path).name}: not a view data file (view_to_js output)") from None
    if view.get("dev_only") is True:
        raise ValueError("dev_only view data cannot be shipped")
    if var != "tryit":
        raise ValueError(f"{Path(path).name}: the Try it data must set SS_DATA[\"tryit\"], not {var!r}")
    if view.get("source") != "synthetic":
        raise ValueError("Try it shows the synthetic corpus only: the view's source must be 'synthetic'")
    return raw, view


def _read_profile(path: Path, *, synthetic: bool) -> tuple[bytes, dict]:
    raw = Path(path).read_bytes()
    try:
        doc = profile_view.load_profile(Path(path))
    except profile_view.ProfileInvalid as exc:
        raise ValueError(f"{Path(path).name}: not a valid profile: {exc}") from None
    state = doc.get("state")
    if synthetic and state != "synthetic":
        raise ValueError(f"{Path(path).name}: the Try it profile must be state 'synthetic', not {state!r}")
    if not synthetic and state == "synthetic":
        raise ValueError(f"{Path(path).name}: a synthetic profile cannot be shown on The study page")
    return raw, doc


def _recorded_run(recorded: Path, tryit: Path, config: Path, view: dict) -> tuple[dict, dict]:
    """``(shipped, recording)``: the replay whose data is shipped (run id, records and chain head, read from the Try-it
    view, so they equal ``tryit.js`` and the profile exported from the same run) and the recording it was checked
    against (``chain_head`` and ``records`` from the recording's manifest)."""
    from .. import replay_check
    for name in RECORDED_FILES:
        if not (recorded / name).is_file():
            raise ValueError(f"replay-check failed: recording file missing: {name}")
    ok, reasons = replay_check.replay_check(config, recorded / "demo-live-fixtures.v2.json", recorded / "run.jsonl",
                                            tryit)
    if not ok:
        raise ValueError("replay-check failed: " + " ".join(reasons))
    manifest = json.loads((recorded / "run.manifest.json").read_text(encoding="utf-8"))
    run = view.get("run") or {}
    shipped = {"run_id": run["run_id"], "chain_head": run["chain_head"], "records": run["verify"]["records"]}
    return shipped, {"chain_head": manifest["head_hash"], "records": manifest["record_count"]}


# ------------------------------------------------------------------------------------------------ pages
def _parse_fragment(text: str, name: str) -> tuple[dict, str]:
    header: dict[str, str] = {}
    while True:
        m = _HEADER.match(text)
        if not m:
            break
        key, value = m.group(1), m.group(2)
        if key not in ("title_key", "scripts", "styles"):
            raise ValueError(f"pages/{name}.html: unknown header {key!r}")
        header[key] = value
        text = text[m.end():]
    if "title_key" not in header:
        raise ValueError(f"pages/{name}.html: the first line must be <!-- title_key: ... -->")
    for pat, what in _INLINE:
        if pat.search(text):
            raise ValueError(f"pages/{name}.html: {what} (the CSP forbids it)")
    return header, text


def _fill_strings(text: str, table: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in table:
            raise KeyError(key)
        return html.escape(table[key], quote=True)
    return _T_TOKEN.sub(sub, text)


def _fill_slots(text: str, slots: dict[str, str], name: str) -> str:
    def sub(m):
        if m.group(1) not in slots:
            raise ValueError(f"pages/{name}.html: unknown slot {m.group(1)!r}")
        return slots[m.group(1)]
    return _SLOT.sub(sub, text)


def _try_slots(view: dict, runs: list[dict], recording: dict | None, table: dict) -> dict[str, str]:
    def t(key, **kw):
        text = table[key]
        return html.escape(text.format_map(kw) if kw else text, quote=True)
    oracle = ""
    if (view.get("run") or {}).get("models", {}).get("recon") == "oracle":
        oracle = f'<p class="site-state">{t("try.oracle_state")}</p>\n'
    if runs:
        row_key = "try.runs.row_replay" if recording else "try.runs.row"
        rows = "".join(f'<li>{t(row_key, run_id=r["run_id"], records=format(r["records"], ","))} '
                       f'<code class="site-hash">{html.escape(r["chain_head"])}</code></li>\n' for r in runs)
        body = f'<ul class="try-runs-list">\n{rows}</ul>\n'
        if recording:
            # the recording is a second object with its own count and head; the sittings note says why they differ
            body += (f'<p class="site-note">{t("try.runs.checked", records=format(recording["records"], ","), head=recording["chain_head"][:8] + "…")}</p>\n')
            body += f'<p class="site-note">{t("try.runs.recorded", run_id=runs[0]["run_id"])}</p>\n'
    else:
        body = f'<p class="site-note">{t("try.runs.none")}</p>\n'
    return {"oracle": oracle, "runs": body}


def _render_page(shell: str, *, name: str, out_name: str, header: dict, body: str, table: dict,
                 shipped: set[str]) -> str:
    def refs(kind: str) -> list[str]:
        items = header.get(kind, "").split()
        for ref in items:
            if ref not in shipped:
                raise ValueError(f"pages/{name}.html: {kind} lists {ref!r}, which the site does not ship")
        return items
    styles = "".join(f'<link rel="stylesheet" href="{html.escape(s)}">\n' for s in refs("styles"))
    scripts = "".join(f'<script src="{html.escape(s)}"></script>\n' for s in refs("scripts"))
    nav = ""
    for _frag, target, key in SITE_PAGES:
        current = ' aria-current="page"' if target == out_name else ""
        nav += f'<li><a href="{target}"{current}>{html.escape(table[key])}</a></li>\n'
    title_key = header["title_key"]
    if title_key not in table:
        raise KeyError(title_key)
    slots = {"csp": CSP_META, "title": html.escape(f"{table[title_key]} · {table['app.name']}"),
             "styles": styles, "page": name, "nav": nav, "body": body, "scripts": scripts}

    def sub(m):
        return slots[m.group(1)] if m.group(1) in slots else m.group(0)
    return _fill_strings(_SHELL_SLOT.sub(sub, shell), table)


def _placeholder(key: str, others: tuple[tuple[str, str], ...] = ()) -> str:
    """A missing fragment: the page's name, how to build it, and links to the pages that do exist."""
    body = f"<h1>{{{{t:{key}}}}}</h1>\n<p class=\"site-note\">{{{{t:site.placeholder}}}}</p>\n"
    if others:
        links = "".join(f'<li><a href="{target}">{{{{t:{k}}}}}</a></li>\n' for target, k in others)
        body += (f'<p class="site-note">{{{{t:site.placeholder_pages}}}}</p>\n'
                 f'<ul class="site-placeholder-pages">\n{links}</ul>\n')
    return body


# ------------------------------------------------------------------------------------------------ build
def _check_out(out: Path) -> None:
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"{out}: not a folder")
        if any(out.iterdir()) and not (out / "data" / "SOURCES.json").is_file():
            raise ValueError(f"{out}: not empty and not an earlier site build; refusing to write into it")


def _explain_js(kit_root: Path):
    if importlib.util.find_spec(f"{__package__}.explain_data") is None:
        return None                       # T29 not merged yet: no explain.js, no SOURCES other.explain
    from . import explain_data
    case = explain_data.build_case(kit_root)
    return explain_data.case_to_js(case)


def build_site(out: Path, *, tryit: Path, tryit_profile: Path, study: Path | None = None,
               recorded: Path | None = None, config: Path = DEMO_CONFIG, pages_dir: Path = PAGES) -> list[Path]:
    """Build the site into ``out`` (see the module docstring) and return the written files, sorted."""
    out, tryit, pages_dir = Path(out), Path(tryit), Path(pages_dir)
    # 1. read and check every input before anything is written
    tryit_raw, view = _read_view(tryit)
    profile_raw, profile_doc = _read_profile(Path(tryit_profile), synthetic=True)
    study_raw, study_doc = (None, STUDY_STUB) if study is None else _read_profile(Path(study), synthetic=False)
    runs: list[dict] = []
    recording: dict | None = None
    if recorded is not None:
        shipped, recording = _recorded_run(Path(recorded), tryit, Path(config), view)
        runs = [shipped]
    _check_out(out)
    table = strings.load_strings("en")

    files: dict[str, bytes] = {}
    for rel in static_files(STATIC):
        files[rel] = (STATIC / rel).read_bytes()

    data: dict[str, bytes] = {
        "data/strings.js": _strings_js(table).encode("utf-8"),
        "data/tryit.js": tryit_raw,
        "data/tryit-profile.js": _ss_data_js(profile_doc, "tryit_profile").encode("utf-8"),
        "data/study.js": _ss_data_js(study_doc, "study").encode("utf-8"),
    }
    explain = _explain_js(KIT_ROOT)
    if explain is not None:
        data["data/explain.js"] = explain.encode("utf-8")
    other = {"strings": {"sha256": _sha(data["data/strings.js"]), "kind": "strings"},
             "study": {"sha256": _sha(data["data/study.js"]), "kind": "stub" if study is None else "study_public"},
             "tryit_profile": {"sha256": _sha(profile_raw), "kind": "profile_synthetic"}}
    if explain is not None:
        other["explain"] = {"sha256": _sha(data["data/explain.js"]), "kind": "explain"}
    sources = {"tryit": {"sha256": _sha(tryit_raw), "recorded": recorded is not None, "runs": runs,
                         "recorded_log_head": recording["chain_head"] if recording else None,
                         "recorded_records": recording["records"] if recording else None},
               "study": None if study_raw is None else {"sha256": _sha(study_raw)},
               "other": other}
    data["data/SOURCES.json"] = (json.dumps(sources, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files.update(data)

    shell = (pages_dir / "shell.html").read_text(encoding="utf-8")
    shipped = set(files)
    for name, out_name, key in SITE_PAGES:
        frag = pages_dir / f"{name}.html"
        others = tuple((t_out, t_key) for t_name, t_out, t_key in SITE_PAGES
                       if t_name != name and (pages_dir / f"{t_name}.html").is_file())
        text = (frag.read_text(encoding="utf-8") if frag.is_file()
                else f"<!-- title_key: {key} -->\n" + _placeholder(key, others))
        header, body = _parse_fragment(text, name)
        slots = _try_slots(view, runs, recording, table) if name == "try" else {}
        body = _fill_slots(body, slots, name)
        page = _render_page(shell, name=name, out_name=out_name, header=header, body=body, table=table,
                            shipped=shipped)
        files[out_name] = page.encode("utf-8")

    # 2. write, then remove what an earlier build left behind
    written = []
    for rel in sorted(files):
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.read_bytes() != files[rel]:
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_bytes(files[rel])
            tmp.replace(dest)
        written.append(dest)
    keep = {out / rel for rel in files}
    for p in sorted(out.rglob("*"), reverse=True):
        if p.is_file() and p not in keep:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    return sorted(written)


def write_tryit_js(run_dir: Path, dest: Path, config: Path = DEMO_CONFIG) -> Path:
    """The ``view_to_js`` file (var ``tryit``) of a finished demo run in ``run_dir``; no model call."""
    from .. import export_view
    from ..pipeline import RunConfig
    run_dir = Path(run_dir).resolve()       # RunConfig resolves a relative out_dir against the config's folder
    if not (run_dir / "logs" / "run.jsonl").is_file():
        raise ValueError(f"{run_dir}: no finished run here (logs/run.jsonl is missing)")
    cfg = RunConfig.load(str(config), out_dir=str(run_dir))
    view = export_view.build_view(cfg, source="synthetic", reference_type=None)
    problems = export_view.validate_view(view)
    if problems:
        raise ValueError("invalid view: " + "; ".join(problems))
    status = ((view.get("run") or {}).get("verify") or {}).get("status")
    if status != "passed":
        raise ValueError(f"the run's chain did not verify (verify status {status!r})")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(export_view.view_to_js(view, "tryit"), encoding="utf-8")
    return dest


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["tryit-js"]:
        ap = argparse.ArgumentParser(prog="python3 -m second_scribe.app.build_site tryit-js",
                                     description="Write the Try it view data of a finished demo run.")
        ap.add_argument("--run", required=True, type=Path, help="the demo run's out folder")
        ap.add_argument("--out", required=True, type=Path, help="the .js file to write")
        ap.add_argument("--config", type=Path, default=DEMO_CONFIG)
        a = ap.parse_args(args[1:])
        try:
            write_tryit_js(a.run, a.out, a.config)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if args[:1] == ["tryit-profile"]:
        ap = argparse.ArgumentParser(prog="python3 -m second_scribe.app.build_site tryit-profile",
                                     description="Export the Try it profile from a finished (replayed) demo run.")
        ap.add_argument("--run", required=True, type=Path, help="the finished demo run's out folder")
        ap.add_argument("--out", required=True, type=Path, help="the profile .json file to write")
        ap.add_argument("--config", type=Path, default=DEMO_CONFIG, help="the config the run used")
        a = ap.parse_args(args[1:])
        try:
            profile_view.build_tryit_profile(a.run, a.config, a.out)
        except (profile_view.ProfileRefused, profile_view.ProfileInvalid, RuntimeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    ap = argparse.ArgumentParser(prog="python3 -m second_scribe.app.build_site",
                                 description="Build the offline site (make app). No model is called.")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tryit", required=True, type=Path, help="the Try it view data (view_to_js file)")
    ap.add_argument("--tryit-profile", required=True, type=Path, help="the synthetic profile shown on Try it")
    ap.add_argument("--study", type=Path, help="the study's public profile (orgsci-profile/v2)")
    ap.add_argument("--recorded", type=Path, help="the recording folder; runs replay-check on --tryit")
    ap.add_argument("--config", type=Path, default=DEMO_CONFIG, help="the Try it run config (for replay-check)")
    a = ap.parse_args(args)
    try:
        written = build_site(a.out, tryit=a.tryit, tryit_profile=a.tryit_profile, study=a.study,
                             recorded=a.recorded, config=a.config)
    except (ValueError, KeyError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"site: {len(written)} files in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
