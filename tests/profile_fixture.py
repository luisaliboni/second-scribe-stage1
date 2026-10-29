"""Shared fixtures of the profile_floors / profile_labellers / profile_slant tests (Task 4).

Two synthetic runs, zero model calls (the oracle client): the shipped demo, and the demo plus the stability-grid spec
``removed/p0/T0.7`` and a ``judgescript`` script source (the same two additions as the analysis pack's
parity-synth/run-grid-config.json). Both are built once per process (the three test modules share them); the
temp dir lives under the per-run ``ss-tests-*`` dir that ``tests/__init__`` removes at exit.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from second_scribe import profile_base as pb
from second_scribe.cli import DEMO_CONFIG

REPO = Path(__file__).resolve().parent.parent
SKELETON = json.loads((REPO / "tests" / "data" / "profile_v2_skeleton.json").read_text(encoding="utf-8"))

_CACHE = {}


def _demo(out: Path, config: Path):
    r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(out), "--config", str(config),
                        "--client", "oracle"], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)


def contexts():
    """(DEMO, GRID): ProfileContext of the shipped demo / of the demo with the grid spec and a judgescript source."""
    if not _CACHE:
        tmp = Path(tempfile.mkdtemp(prefix="ss-tests-e2-t4-"))
        cfg = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        cfg["specs"].append({"arm": "removed", "variant": "p0", "temperature": 0.7, "replicate": 0})
        cfg["script_sources"] = [{"name": "judgescript", "role": "judge", "variants": ["p0", "p1", "p2"], "samples": 2,
                                  "temperature": 0.7}]
        cfg["base_dir"] = str(Path(DEMO_CONFIG).parent)
        grid_cfg = tmp / "grid-config.json"
        grid_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        _demo(tmp / "demo", Path(DEMO_CONFIG))
        _demo(tmp / "grid", grid_cfg)
        _CACHE["demo"] = pb.open_context(Path(DEMO_CONFIG), tmp / "demo")
        _CACHE["grid"] = pb.open_context(grid_cfg, tmp / "grid")
    return _CACHE["demo"], _CACHE["grid"]


def skeleton_names(row):
    rows = {r["row"]: r for r in SKELETON["rows"]}
    return [(c["name"], c.get("x")) for c in rows[row]["cells"]]


def names(cells):
    return [(c["name"], c.get("x")) for c in cells]


def by_name(cells, name, x=None):
    hits = [c for c in cells if c["name"] == name and c.get("x") == x]
    assert len(hits) == 1, (name, x, len(hits))
    return hits[0]
