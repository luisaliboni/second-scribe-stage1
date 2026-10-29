"""Instructions and Results routes (Task 40; spec 6.3, 6.7, 6.8). The logic lives in ``app/results.py``.

``GET  /api/prompts``                 the instruction texts as run (kit ``prompts/``, read-only) and the rung-1 wrapper
``GET  /api/instructions``            the rung choice, the user's rung-1 prompt block and the preset
``POST /api/instructions``            ``{rung1: "basic"|"own", rung1_text?, rungs: [rung1|rung2|rung3], preset}``
``GET  /api/results/runs``            the runs (id, group, finished, ready, state): nothing AI-derived, never a reveal
``GET  /api/view?run=<run_id>``       the run's ``view-data/1`` file                    } ``coding.results_route``:
``GET  /api/results/profile?run=``    the run's profile, or the exporter's refusal     } gate first, reveal after

The two results routes register with ``coding.results_route``, never ``route``: while any B coding is open they
answer 409 ``coding_unlocked``, and the first answer they give logs the project's reveal (Task 36). The Results screen
asks for them only when the user presses the button that opens a run.
"""
from __future__ import annotations

from .. import profile_view, results
from ..api import route
from ..coding import results_route
from .project import require_project
from .runs import _live


@route("GET", "/api/prompts")
def get_prompts(state, body, query):
    return results.prompts()


@route("GET", "/api/instructions")
def get_instructions(state, body, query):
    with state.lock:
        ins = results.instructions(require_project(state))
    return {"instructions": ins, "run_rungs": results.run_rungs(ins["rungs"])}


@route("POST", "/api/instructions")
def post_instructions(state, body, query):
    with state.lock:
        ins = results.set_instructions(require_project(state), body)
    return {"instructions": ins, "run_rungs": results.run_rungs(ins["rungs"])}


@route("GET", "/api/results/runs")
def list_runs(state, body, query):
    with state.lock:
        live = _live(state)
        if live is not None:
            live.poll()                      # a stage that has just failed must not read as running
        return {"runs": results.runs_summary(require_project(state), live)}


@results_route("GET", "/api/view")
def get_view(state, body, query):
    ref, doc = results.load_view(state.project, query.get("run"))
    return {"run_id": ref.run_id, "group": ref.group, "view": doc}


@results_route("GET", "/api/results/profile")
def get_profile(state, body, query):
    try:
        ref, doc = results.profile_for(state.project, query.get("run"))
    except profile_view.ProfileRefused as e:
        ref = results.find_run(state.project, query.get("run"))
        results.log_refusal(ref.run_id, e.reasons)
        return {"run_id": ref.run_id, "group": ref.group, "refused": list(e.reasons),
                "refusals": results.refusal_messages(e.reasons)}
    return {"run_id": ref.run_id, "group": ref.group, "profile": doc}
