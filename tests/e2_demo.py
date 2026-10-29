"""The kit exporter's real output on the recorded demo replay (E2 milestone M1), built once per test process.

``demo_export()`` runs ``second_scribe.cli demo --client replay`` (demo/fixtures.json, zero model calls) into a
``ss-tests-e2-wire-*`` dir (under the per-run dir that ``tests/__init__`` removes at exit), then the app's
``profile_view.run_exporter`` on it. tests.test_profile_view and tests.test_publish share the result.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from second_scribe.app import profile_view as pv
from second_scribe.cli import DEMO_CONFIG

REPO = Path(__file__).resolve().parent.parent
_CACHE: dict = {}


def replay_demo(out: Path) -> None:
    r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(out), "--client", "replay"],
                       cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError("demo replay failed:\n" + r.stdout[-2000:] + r.stderr[-2000:])


def demo_export() -> tuple[Path, Path, str]:
    """(run_dir, profile_path, sha256 that run_exporter returned) for the replayed demo."""
    if not _CACHE:
        tmp = Path(tempfile.mkdtemp(prefix="ss-tests-e2-wire-"))
        run = tmp / "run"
        replay_demo(run)
        prof = tmp / "profile.json"
        sha = pv.run_exporter(Path(DEMO_CONFIG), run, prof)
        _CACHE.update(run=run, profile=prof, sha=sha)
    return _CACHE["run"], _CACHE["profile"], _CACHE["sha"]
