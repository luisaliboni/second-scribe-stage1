"""``make test-fast``: the same discovery as ``make test`` minus the slow suites (stdlib only).

A SKIP list, not an allow list: a new tests/test_*.py is in the fast run unless it is listed here. SLOW_MODULES are the
modules that took 60 s or more each when timed alone on 2026-10-19 (end-to-end pipeline, leak-scan, replay and
export suites); ``make test`` still runs them. Run with ``python3 -m tests.fast`` from the repository root.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

SLOW_MODULES = frozenset({
    "test_audits", "test_build_site", "test_fork", "test_judge_bench", "test_judge_only_session", "test_leakscan",
    "test_m_kit_words", "test_mapping_talk_screen", "test_model_check", "test_nomodel", "test_outage", "test_phantom",
    "test_pipeline", "test_pipeline_ladder_gaps", "test_prereg", "test_profile_export", "test_profile_ladder",
    "test_real_run_validity", "test_replay_check", "test_script_sources", "test_sidejudge", "test_work_integrity",
})


def _flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _flatten(t)
        else:
            yield t


def fast_suite(slow=SLOW_MODULES) -> unittest.TestSuite:
    """Every discovered test whose module is not in ``slow`` (import failures are kept, so they still show)."""
    found = unittest.defaultTestLoader.discover(str(HERE), pattern="test*.py", top_level_dir=str(HERE.parent))
    out = unittest.TestSuite()
    for t in _flatten(found):
        if type(t).__module__.rpartition(".")[2] not in slow:
            out.addTest(t)
    return out


def main() -> int:
    stale = sorted(n for n in SLOW_MODULES if not (HERE / f"{n}.py").is_file())
    if stale:
        print(f"tests/fast.py: SLOW_MODULES names no such file: {', '.join(stale)}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=1).run(fast_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
