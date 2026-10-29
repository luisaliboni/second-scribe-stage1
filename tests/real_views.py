"""Real exported views for the JS tests (``tests/js/real_views.test.js``): ``export_view.build_view`` over oracle demo
runs, written as the ``.js`` data files the browser loads (``view_to_js``). No model is called.

    python3 -m tests.real_views OUT_DIR   ->   pattern.js (the shippable test pattern), modec.js, aimapsonly.js

``pattern``: ``testpattern.make_test_pattern(drift=False)``; ``modec``: a run whose references are empty (reference type
C); ``aimapsonly``: a Q-only run with no judge (``--ai-maps-only``).
"""
import sys
import tempfile
from pathlib import Path

from second_scribe import export_view as ev
from second_scribe.app import testpattern
from second_scribe.pipeline import RunConfig

from tests.test_export_view import DEMO_CONFIG, _empty_refs, _run


def build(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    views = {"pattern": testpattern.make_test_pattern(drift=False)}
    with tempfile.TemporaryDirectory(prefix="ss-tests-realviews-") as tmp:
        root = Path(tmp)
        base = RunConfig.load(DEMO_CONFIG, out_dir=str(root / "x")).corpus
        cfg = _run(root / "c", tool={"reference_type": "C"}, corpus=_empty_refs(base, root / "refs" / "corpus"))
        views["modec"] = ev.build_view(cfg, source="synthetic", reference_type="C")
        cfg = _run(root / "q", stages="Q", roles=("recon",))
        views["aimapsonly"] = ev.build_view(cfg, source="own", reference_type=None, ai_maps_only=True)
    paths = {}
    for name, view in views.items():
        errors = ev.validate_view(view)
        if errors:
            raise ValueError(f"{name}: {errors}")
        paths[name] = out / f"{name}.js"
        paths[name].write_text(ev.view_to_js(view, name), encoding="utf-8")
    return paths


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m tests.real_views OUT_DIR")
    for name, path in build(Path(sys.argv[1])).items():
        print(name, path)
