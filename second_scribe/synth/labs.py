"""Three synthetic labs for the pool demo, built on the shipped synthetic corpus builder. Zero model calls.

  labA  the 12-person corpus of ``synth.build.build_corpus(dir, seed)``;
  labB  the same corpus, but the AI side (the oracle's gold map) loses 30% of the nodes of each map, and their edges,
        so its recall is lower;
  lab9  labA with ``corpus.roles`` limited to the 9 lowest pids in sorted order: only 9 people are analysed, so the
        exporter and the pool refuse it.

Every config has absolute paths and an ``out_dir`` under ``dest``. Each adds the ``judgescript`` script source (the
same entry as the analysis pack's parity-synth/run-grid-config.json) and nothing else: no stability-grid spec
(Ruling R15).
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from second_scribe.synth.build import _sample, _write_json, build_corpus

DROP_SHARE = 0.30
N_LAB9 = 9
# copied from analysis/public/parity-synth/run-grid-config.json (script_sources)
JUDGESCRIPT = {"name": "judgescript", "role": "judge", "variants": ["p0", "p1", "p2"], "samples": 2, "temperature": 0.7}


def _demo_config() -> dict:
    from second_scribe.cli import DEMO_CONFIG
    return json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))


def _thin_gold(corpus: Path, seed: int) -> None:
    """Remove ``round(0.3 * n)`` nodes per map from gold/<pid>.json, with the edges that touch them."""
    for path in sorted((corpus / "gold").glob("SYN-*.json")):
        if path.name.endswith(".concepts.json"):
            continue
        pid = path.stem
        gold = json.loads(path.read_text(encoding="utf-8"))
        ids = sorted(n["node_id"] for n in gold["nodes"])
        rng = random.Random(f"labB:{seed}:{pid}")
        dropped = set(_sample(rng, ids, round(DROP_SHARE * len(ids))))      # stdlib-version-stable draws
        gold["nodes"] = [n for n in gold["nodes"] if n["node_id"] not in dropped]
        gold["edges"] = [e for e in gold["edges"] if e["source"] not in dropped and e["target"] not in dropped]
        _write_json(path, gold)


def _config(lab: str, dest: Path, corpus: Path, roles: dict | None = None) -> dict:
    cfg = _demo_config()
    cfg["run_id"] = lab
    cfg["base_dir"] = str(dest / lab)
    cfg["out_dir"] = str(dest / lab / "out")
    cfg["corpus"].update(maps_glob=str(corpus / "maps" / "*.vsdx"),
                         transcripts_glob=str(corpus / "transcripts" / "*.docx"), synthetic_dir=str(corpus))
    if roles is not None:
        cfg["corpus"]["roles"] = roles
    cfg["script_sources"] = [dict(JUDGESCRIPT)]
    return cfg


def build_labs(dest, seed: int = 0) -> dict[str, Path]:
    """Build the three labs under ``dest`` and return ``{"labA": cfg_path, "labB": ..., "lab9": ...}``."""
    dest = Path(dest).resolve()
    corpus_a, corpus_b = dest / "labA" / "corpus", dest / "labB" / "corpus"
    build_corpus(corpus_a, seed)
    build_corpus(corpus_b, seed)
    _thin_gold(corpus_b, seed)
    roles_a = _config("labA", dest, corpus_a)["corpus"]["roles"]
    first9 = {pid: roles_a[pid] for pid in sorted(roles_a)[:N_LAB9]}
    cfgs = {"labA": _config("labA", dest, corpus_a), "labB": _config("labB", dest, corpus_b),
            "lab9": _config("lab9", dest, corpus_a, roles=first9)}
    out = {}
    for lab, cfg in cfgs.items():
        path = dest / lab / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out[lab] = path
    return out


def main(argv=None) -> int:
    """``python3 -m second_scribe.synth.labs DEST``: ``--help`` prints the usage and writes nothing; a DEST that
    starts with ``-`` is refused (argparse error, exit 2, nothing written)."""
    import argparse
    ap = argparse.ArgumentParser(
        prog="python3 -m second_scribe.synth.labs",
        description="Build three synthetic labs (labA, labB, lab9) under DEST for the pool demo. Zero model calls.")
    ap.add_argument("dest", metavar="DEST", help="folder to build the labs in (must not start with '-')")
    args = ap.parse_args(argv)
    if args.dest.startswith("-"):
        ap.error(f"DEST must not start with '-' (got {args.dest!r}); use ./{args.dest} if you mean that folder")
    for name, path in build_labs(args.dest).items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":          # python3 -m second_scribe.synth.labs DEST
    raise SystemExit(main())
