"""Task 21: the labelled test pattern, ``view-data/1`` built from an oracle demo run (no model, no real data).

``make_test_pattern(drift=False)`` runs the kit's synthetic demo corpus with the oracle client (gold labels, the
reconstructor reads only the transcript, so every rung gives the same map and nothing leaks or flips) and passes the
finished out_dir through ``export_view.build_view``. Every ``numbers`` cell is kept as computed; the document is only
marked: ``"test_pattern": true`` (the viewer prints "Test pattern · not a recorded result"). It is the Try-it data the
site may carry until the recorded run lands.

``make_test_pattern(drift=True)`` is DEV-ONLY. It wraps the oracle in ``DriftingOracle``, a deterministic test client
that gives each rung's instruction a fixed seeded share of drift (wished nodes drawn as experienced, and one
experienced node per map drawn as wished), so the viewer can be looked at with leaked and flipped steps, rung
differences and a verdict. The run still goes through the kit, so the numbers are internally consistent. The document
also carries ``"dev_only": true`` and the model label ``test-pattern``. It lives only in
``static/dev/data/tryit.js`` and is never copied into the site: public drift comes only from a real model's recorded
replies (spec R20, 2.3). ``DriftingOracle`` is not a client choice anywhere.

CLI: ``python3 -m second_scribe.app.testpattern [--drift] --out FILE.js`` writes ``view_to_js(view, "tryit")``.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path

from .. import export_view
from ..audit.registry import AuditLog
from ..cli import DEMO_CONFIG, build_clients
from ..pipeline import RunConfig, expand_stages, run

LABEL = "test-pattern"
VAR = "tryit"
DRIFT_SEED = 20261018
# share of a reply's wished nodes drawn as experienced, per rung (ladder order: first draft, good practice, placebo, remedy)
DRIFT_SHARES = {"rung1": 0.40, "rung2": 0.30, "placebo": 0.30, "rung3": 0.15}
_RAISED_PER_MAP = 1                      # experienced nodes drawn as wished, per map and rung


def rung_of_prompt(user: str) -> str | None:
    """Which rung's reconstruction prompt this user message is, from the instruction text (before the transcript);
    None for any other call (judge, alignment, prior-only, the p1/p2 prompt variants)."""
    head = user.split("TRANSCRIPT:", 1)[0]
    if "Two worked examples" in head:
        if "according to the participant" in head:
            return "rung3"
        if "from the first turn to the last" in head:
            return "placebo"
        return None
    if 'Mark a node or edge "wished" when the person says' in head:
        return "rung1"
    if 'Mark every node and edge "experienced" or "wished"' in head and "Read the whole transcript and draw this person" in head:
        return "rung2"
    return None


def _unit(*parts: object) -> float:
    """A fixed pseudo-random number in [0, 1) from the parts (sha256, so the same on every machine)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


class DriftingOracle:
    """Test client, dev only: the oracle's reply with a fixed seeded share of its wished nodes drawn as experienced
    (``DRIFT_SHARES`` of the rung that the prompt belongs to) and ``_RAISED_PER_MAP`` experienced node drawn as wished.
    Everything else (text, quotes, ids, edges, every non-reconstruction call) passes through unchanged. Deterministic:
    the same prompt always gets the same reply. Its model label is ``test-pattern``."""
    model_label = LABEL

    def __init__(self, inner, seed: int = DRIFT_SEED):
        self.inner = inner
        self.seed = seed

    def complete(self, messages, **kw):
        resp = self.inner.complete(messages, **kw)
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        rung = rung_of_prompt(user)
        if rung is None:
            return resp
        doc = json.loads(resp.text)
        nodes = doc.get("nodes") or []
        key = hashlib.sha256(user.encode("utf-8")).hexdigest()
        share = DRIFT_SHARES[rung]
        wished = [n for n in nodes if n.get("status") == "wished"]
        experienced = [n for n in nodes if n.get("status") == "experienced"]
        for n in wished:
            if _unit(self.seed, "down", rung, key, n["id"]) < share:
                n["status"] = "experienced"
        for n in sorted(experienced, key=lambda n: _unit(self.seed, "up", rung, key, n["id"]))[:_RAISED_PER_MAP]:
            n["status"] = "wished"
        return dataclasses.replace(resp, text=json.dumps(doc))


class _Relabelled:
    """A client that forwards every call and only carries another model label."""

    def __init__(self, inner, label: str):
        self.inner = inner
        self.model_label = label

    def complete(self, messages, **kw):
        return self.inner.complete(messages, **kw)


def _run_demo(out: Path, *, drift: bool) -> RunConfig:
    cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
    clients = build_clients("oracle", cfg)
    if drift:
        clients = {role: (DriftingOracle(c) if role == "recon" else _Relabelled(c, LABEL)) for role, c in clients.items()}
    (out / "logs").mkdir(parents=True)
    log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run(cfg, expand_stages("all"), clients, log=log)
    finally:
        log.close()
    return cfg


def make_test_pattern(drift: bool = False) -> dict:
    """A ``view-data/1`` document, ``source == "synthetic"``, ``"test_pattern": true`` (and ``"dev_only": true`` when
    ``drift``), built by ``export_view.build_view`` over an oracle demo run. No model is called."""
    with tempfile.TemporaryDirectory(prefix="ss-testpattern-") as tmp:
        cfg = _run_demo(Path(tmp) / "out", drift=drift)
        view = export_view.build_view(cfg, source="synthetic", reference_type=None)
    view["test_pattern"] = True
    if drift:
        view["dev_only"] = True
    errors = export_view.validate_view(view)
    if errors:
        raise ValueError("the test pattern is not a valid view-data/1 document: " + "; ".join(errors))
    return view


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m second_scribe.app.testpattern",
                                 description="Write the labelled test-pattern view data as a JS data file.")
    ap.add_argument("--drift", action="store_true",
                    help="DEV ONLY: the drifting test client (leaked and flipped steps); marked dev_only, never shipped")
    ap.add_argument("--out", required=True, metavar="FILE.js", help="the data file to write")
    args = ap.parse_args(argv)
    view = make_test_pattern(drift=args.drift)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(export_view.view_to_js(view, VAR), encoding="utf-8")
    print(f"wrote {out} ({'drift, dev only' if args.drift else 'no drift'}; test pattern, not a recorded result)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
