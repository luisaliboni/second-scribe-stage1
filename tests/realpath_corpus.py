"""A real-kind copy of the shipped synthetic corpus, for tests of the app's own-data path (Tasks 39 and 44).

``make_realpath_corpus(dst)`` copies the maps and transcripts of ``demo/corpus`` into ``dst`` with every pid and file
name renamed (``SYN-OW01`` -> ``CASE-A01``, ``SYN-OF01`` -> ``CASE-B01``, ...) and writes a matching participant id
map in the app's format (``importer.PID_MAP``: ``{"map": {pid: {"file_stem", "file_name"}}}``). The renamed files
give a corpus whose hash differs from the shipped one, so the kit treats it as a corpus of its own. The text is the
same, so ``tests/mock_models.MockModels([DEMO_CONFIG])`` answers its prompts (the oracle reads the prompt text only).

``realpath_config(dst, run_id, ...)`` writes ``<dst>/runs/<run_id>/config.json``: a real-kind config with the fields
of ``configs/real.template.json`` (settings, stats), the Standard preset's specs, prior-only draws and ladder, the
renamed corpus as explicit ``pairs`` (map first, then transcript), ``models = {"recon": "RECON", "judge": "JUDGE",
"judge2": null}`` and a ``tool`` block (``reference_type``, ``judge_rule: "two_family"``, families and digests of the
mock models). It stands in for Task 38's ``orchestrate.build_run_config`` so the runner is tested on its own.

No model is called here, and nothing here is real participant data.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from second_scribe.pipeline import KIT_ROOT

DEMO_CORPUS = KIT_ROOT / "demo" / "corpus"
DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
REAL_TEMPLATE = KIT_ROOT / "configs" / "real.template.json"
PREFIX = {"SYN-OW": "CASE-A", "SYN-OF": "CASE-B"}
STANDARD_SPEC = {"arm": "removed", "variant": "p0", "temperature": 0.0, "replicate": 0}
STANDARD_PRIOR = {"variants": ["p0"], "samples": 5, "temperature": 0.7}


def _new_pid(old: str) -> str:
    for a, b in PREFIX.items():
        if old.startswith(a):
            return b + old[len(a):]
    raise ValueError(f"unexpected demo pid {old!r}")


def make_realpath_corpus(dst: Path) -> dict:
    """Copy the demo corpus into ``dst`` (``maps/``, ``transcripts/``, ``pid_map.json``), renamed. Returns
    ``{old pid: new pid}``."""
    dst = Path(dst)
    (dst / "maps").mkdir(parents=True, exist_ok=True)
    (dst / "transcripts").mkdir(parents=True, exist_ok=True)
    meta = json.loads((DEMO_CORPUS / "meta.json").read_text(encoding="utf-8"))
    mapping = {p["pid"]: _new_pid(p["pid"]) for p in meta["participants"]}
    pid_map = {"map": {}}
    for old, new in sorted(mapping.items()):
        shutil.copyfile(DEMO_CORPUS / "maps" / f"{old}.vsdx", dst / "maps" / f"{new}.vsdx")
        shutil.copyfile(DEMO_CORPUS / "transcripts" / f"{old}.docx", dst / "transcripts" / f"{new}.docx")
        pid_map["map"][new] = {"file_stem": new, "file_name": f"{new}.docx"}
    (dst / "pid_map.json").write_text(json.dumps(pid_map, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return mapping


def realpath_config(dst: Path, run_id: str, *, group: str = "A", rungs=("rung2",), tool: dict | None = None,
                    corpus_dir: Path | None = None, out_dir: Path | None = None) -> Path:
    """Write ``<dst>/runs/<run_id>/config.json`` over the renamed corpus in ``corpus_dir`` (default ``dst/corpus``,
    made here when missing). ``tool`` entries are added to the default tool block. Returns the config path."""
    dst = Path(dst)
    corpus_dir = Path(corpus_dir) if corpus_dir is not None else dst / "corpus"
    if not (corpus_dir / "pid_map.json").is_file():
        make_realpath_corpus(corpus_dir)
    demo = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
    tmpl = json.loads(REAL_TEMPLATE.read_text(encoding="utf-8"))
    pids = sorted(json.loads((corpus_dir / "pid_map.json").read_text(encoding="utf-8"))["map"])
    roles = {_new_pid(old): role for old, role in demo["corpus"]["roles"].items()}
    folder = dst / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=True)
    picked = set(rungs) | {"rung2"}
    if "rung3" in picked:
        picked.add("placebo")
    from second_scribe.app.results import RUN_ORDER
    from tests.mock_models import default_models
    digests = {m: v["digest"] for m, v in default_models().items()}
    block = {"reference_type": group, "judge_rule": "two_family",
             "model_families": {"recon": "qwen", "judge": "gemma"},
             "model_digests": {"recon": digests["qwen-mock:7b"], "judge": digests["gemma-mock:9b"]}}
    block.update(tool or {})
    cfg = {
        "run_id": run_id,
        "corpus": {
            "kind": "real",
            "maps_glob": None, "transcripts_glob": None,
            "pairs": {pid: [str((corpus_dir / "maps" / f"{pid}.vsdx").resolve()),
                            str((corpus_dir / "transcripts" / f"{pid}.docx").resolve())] for pid in pids},
            "roles": {pid: roles[pid] for pid in pids},
            "participant_labels": {pid: ["pt"] for pid in pids},
            "interviewer_labels": {pid: ["iv"] for pid in pids},
            "participant_id_map": str((corpus_dir / "pid_map.json").resolve()),
        },
        "context": demo["context"],
        "specs": [dict(STANDARD_SPEC)],
        "prior_only": dict(STANDARD_PRIOR),
        "settings": tmpl["settings"],
        "models": {"recon": "RECON", "judge": "JUDGE", "judge2": None},
        "out_dir": str((Path(out_dir) if out_dir is not None else folder).resolve()),
        "base_dir": str(folder.resolve()),
        "ladder": {"rungs": [k for k in RUN_ORDER if k in picked], "draws": 1},
        "stats": tmpl["stats"],
        "tool": block,
    }
    path = folder / "config.json"
    path.write_text(json.dumps(cfg, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path
