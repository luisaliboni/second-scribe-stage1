"""A finished synthetic run in a project's ``runs/`` folder, laid out as Task 38 will write it (Task 40's tests).

Tasks 38/39 are not built yet, so the app's Results are tested on the shipped demo run with the ``oracle`` client
(no model is called, no fixture file is needed):

    <project>/runs/<run_id>/config.json      the demo config with ``run_id``, ``out_dir`` (absolute) and ``base_dir``
    <project>/runs/<run_id>/logs, work, ...  the kit's out_dir (the run folder itself)
    <project>/views/<run_id>.view.json       ``python -m second_scribe.export_view`` (what Task 39 runs at the end)

The demo is run once per test process into a ``ss-tests-k40-*`` dir (under the per-run dir ``tests/__init__``
removes at exit) and copied into each project. Only group A is possible here: the demo is not a tool run, and the
kit's ``export_view`` gives a non-tool run reference type A only.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from second_scribe.cli import DEMO_CONFIG

REPO = Path(__file__).resolve().parent.parent
RUN_ID = "A-0a1b2c3d"
_CACHE: dict = {}


def _kit(*args: str) -> None:
    r = subprocess.run([sys.executable, *args], cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(" ".join(args[:3]) + " failed:\n" + r.stdout[-2000:] + r.stderr[-2000:])


def _config(run_id: str, out_dir: Path) -> dict:
    cfg = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
    cfg["run_id"] = run_id
    cfg["out_dir"] = str(out_dir)
    cfg["base_dir"] = str(Path(DEMO_CONFIG).resolve().parent)
    return cfg


def demo_run() -> Path:
    """The cached finished demo run folder (config.json inside)."""
    if "run" not in _CACHE:
        run = Path(tempfile.mkdtemp(prefix="ss-tests-k40-")) / RUN_ID
        run.mkdir()
        (run / "config.json").write_text(json.dumps(_config(RUN_ID, run), indent=1), encoding="utf-8")
        _kit("-m", "second_scribe.cli", "demo", "--out", str(run), "--client", "oracle",
             "--config", str(run / "config.json"))
        _CACHE["run"] = run
    return _CACHE["run"]


def place_run(project, run_id: str = RUN_ID, *, view: bool = True) -> Path:
    """Copy the demo run into ``project`` as ``runs/<run_id>``; with ``view`` also write ``views/<run_id>.view.json``
    the way Task 39 does. Returns the run folder."""
    folder = project.path("runs") / run_id
    shutil.copytree(demo_run(), folder)
    (folder / "config.json").write_text(json.dumps(_config(run_id, folder), indent=1), encoding="utf-8")
    if view:
        _kit("-m", "second_scribe.export_view", "--config", str(folder / "config.json"), "--source", "synthetic",
             "--reference-type", run_id[0], "--out", str(project.path("views") / f"{run_id}.view.json"))
    return folder
