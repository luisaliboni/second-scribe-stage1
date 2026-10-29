"""``GET /api/runs/last``: the latest run of each reference group and whether it finished (review R8).

The Compare instructions screen shows it beside each "Start the run" button, so a researcher can tell that a run
already exists for a group. Read-only; ids, group and a yes/no only, nothing AI-derived, so it is not a reveal and
does not go through ``coding.results_route``. "Latest" is the run whose ``config.json`` was written last (the run's
start), the run id breaking a tie. ``finished`` is the same test ``GET /api/results/runs`` uses (the kit's log
manifest exists).
"""
from __future__ import annotations

import os

from .. import results
from ..api import route
from .project import require_project


def _written(ref: results.RunRef) -> int:
    try:
        return os.stat(ref.config_path).st_mtime_ns
    except OSError:
        return 0


@route("GET", "/api/runs/last")
def last_runs(state, body, query):
    with state.lock:
        project = require_project(state)
        latest: dict = {}
        for ref in results.list_runs(project):
            best = latest.get(ref.group)
            if best is None or (_written(ref), ref.run_id) > (_written(best), best.run_id):
                latest[ref.group] = ref
        return {"last": [{"group": g, "run_id": latest[g].run_id, "finished": latest[g].finished}
                         for g in sorted(latest)]}
