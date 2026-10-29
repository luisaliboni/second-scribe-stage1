"""The Explain page's case (plan Task 29, V5): one synthetic interview, SYN-OW03, from the shipped corpus.

``build_case(kit_root)`` reads only these shipped files under ``demo/corpus/``:
  meta.json                     the domain, the role text and the phases (what the no-interview map is given)
  transcripts/SYN-OW03.docx     the interview, turn by turn
  maps/SYN-OW03.vsdx            reading 1: the interviewer-drawn, participant-endorsed map (red = missing)
  gold/SYN-OW03.json            the AI's map in the illustration: the kit oracle's "perfect AI" answers with it
  gold/SYN-OW03.concepts.json   which concept each box denotes, so the three readings line up

The no-interview map is the kit oracle's stand-in for the role (``demo_oracle``: the role's concepts from the
synthetic concept bank under their map labels), so nothing here is invented and nothing is a recorded result
(the page is an animation drawn from the answer key, author's item 4). Each box's ``turn`` is the transcript turn
that names its concept, as the oracle quotes it (a participant turn when one does), or null when no turn names it.
Council 30 (MUST 1): the gold's ``meta.mapping_talk_only`` boxes (SYN-OW03 g4, named only in the interviewer's
mapping talk, turn 6) are left out of the AI's map, because mapping talk is removed before the AI reads
(``glossary.ai_map``); ``mapping_talk`` lists the turns that name them, which scene 3 shows struck out.
``case_to_js`` writes ``window.SS_DATA["explain"]`` for site/data/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..demo_oracle import _PRIOR_SIZE
from ..export_view import view_to_js
from ..maps.readers import read_map
from ..maps.vsdx import ExtractConfig
from ..synth.bank import CONCEPTS
from ..textnorm import token_seq
from ..transcripts import parse_paragraphs, read_docx_text
from .strings import load_strings

SCHEMA = "explain-case/1"
MAP_ID = "SYN-OW03"
ROLE = "owner"
CORPUS = Path("demo") / "corpus"
FILES = {
    "meta": CORPUS / "meta.json",
    "transcript": CORPUS / "transcripts" / f"{MAP_ID}.docx",
    "reference": CORPUS / "maps" / f"{MAP_ID}.vsdx",
    "ai": CORPUS / "gold" / f"{MAP_ID}.json",
    "concepts": CORPUS / "gold" / f"{MAP_ID}.concepts.json",
}
# The strings the Explain page reads (the dev harness ships only these).
STRING_PREFIXES = ("app.", "explain.", "lane.", "band.", "common.")


def _turn_naming(cid: str, turns: list[dict]) -> int | None:
    """The turn that names concept ``cid`` (its transcript phrase, whole words): a participant turn when one
    does, else the earliest turn; None when no turn names it. The same choice as the oracle's quotes."""
    phrase = token_seq(_BANK[cid].forms[1])
    hits = [i for i, t in enumerate(turns) if phrase in token_seq(t["text"])]
    best = next((i for i in hits if turns[i]["speaker"] == "participant"), hits[0] if hits else None)
    return best


_BANK = {c.cid: c for c in CONCEPTS}


def build_case(kit_root: Path) -> dict:
    root = Path(kit_root)
    meta = json.loads((root / FILES["meta"]).read_text(encoding="utf-8"))
    phases = list(meta["phases"])
    concepts = json.loads((root / FILES["concepts"]).read_text(encoding="utf-8"))

    t = parse_paragraphs(read_docx_text(root / FILES["transcript"]), {"pt"}, MAP_ID, interviewer_labels={"iv"})
    turns = [{"speaker": x.speaker, "text": x.text} for x in t.turns]

    g = read_map(root / FILES["reference"], MAP_ID, extract=ExtractConfig(phase_names=tuple(phases)))
    reference = []
    for n in g.nodes:
        if n.phase is None:          # the phase headers and the legend are not boxes
            continue
        cid = concepts["endorsed"][n.node_id]
        reference.append({"id": n.node_id, "label": n.text, "phase": n.phase, "missing": n.status == "wished",
                          "concept": cid, "turn": _turn_naming(cid, turns)})

    gold = json.loads((root / FILES["ai"]).read_text(encoding="utf-8"))
    talk_only = set((gold.get("meta") or {}).get("mapping_talk_only") or [])
    ai = []
    mapping_talk = []
    for n in gold["nodes"]:
        cid = concepts["gold"][n["node_id"]]
        if n["node_id"] in talk_only:      # named only in the interviewer's mapping talk, removed before the AI reads
            turn = _turn_naming(cid, turns)
            if turn is not None and turns[turn]["speaker"] == "interviewer" and turn not in mapping_talk:
                mapping_talk.append(turn)
            continue
        ai.append({"id": n["node_id"], "label": n["text"], "phase": n["phase"], "missing": n["status"] == "wished",
                   "concept": cid, "turn": _turn_naming(cid, turns)})

    pool = [c for c in CONCEPTS if ROLE in c.roles][:_PRIOR_SIZE]     # demo_oracle.OracleClient._prior_only
    no_interview = [{"id": f"n{k}", "label": c.forms[0], "phase": c.phase, "missing": False, "concept": c.cid,
                     "turn": _turn_naming(c.cid, turns)} for k, c in enumerate(pool, 1)]

    return {
        "schema": SCHEMA,
        "map_id": MAP_ID,
        "synthetic": True,
        "domain": meta["domain"],
        "role": meta["roles"][ROLE],
        "phases": phases,
        "transcript": turns,
        "reference": reference,
        "ai": ai,
        "mapping_talk": sorted(mapping_talk),
        "no_interview": no_interview,
        "sources": {k: v.as_posix() for k, v in FILES.items()} | {"no_interview": "second_scribe.demo_oracle"},
    }


def case_to_js(case: dict) -> str:
    """``window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["explain"] = <json>;`` (script-safe, one line)."""
    return view_to_js(case, "explain")


def strings_to_js(strings: dict[str, str]) -> str:
    """The dev harness's string table: only the keys the Explain page reads."""
    sub = {k: v for k, v in sorted(strings.items()) if k.startswith(STRING_PREFIXES)}
    body = json.dumps(sub, ensure_ascii=False, indent=1).replace("</", "<\\/")
    return f"window.SS_STRINGS = {body};\n"


_DEV_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Explain dev harness</title>
<!-- Dev only (never shipped: build_site skips static/dev/). Written by
     python3 -m second_scribe.app.explain_data --dev second_scribe/app/static/dev
     The body is pages/explain.html verbatim. Hash: #theme=dark|light, #motion=reduce, #scene=<id>, #w=390 (a real 390px frame). -->
<link rel="stylesheet" href="../css/fonts.css">
<link rel="stylesheet" href="../css/tokens.css">
<link rel="stylesheet" href="../css/base.css">
<link rel="stylesheet" href="../css/site-explain.css">
<script>
  (function () {
    var h = location.hash, html = document.documentElement;
    if (/(^|[#&])theme=dark($|&)/.test(h)) html.setAttribute("data-theme", "dark");
    if (/(^|[#&])theme=light($|&)/.test(h)) html.setAttribute("data-theme", "light");
    if (/(^|[#&])motion=reduce($|&)/.test(h)) html.setAttribute("data-motion", "reduce");
    // headless Chrome opens no window narrower than 500px, so #w=390 shows the page in an iframe that wide
    var w = /(?:^|[#&])w=(\\d+)/.exec(h);
    if (w && window.top === window) {
      document.addEventListener("DOMContentLoaded", function () {
        document.body.textContent = "";
        document.body.style.margin = "0";
        var f = document.createElement("iframe");
        f.src = location.pathname + h.replace(/(^#|&)w=\\d+/, "$1");
        f.setAttribute("title", "Explain at " + w[1] + "px");
        f.style.border = "0";
        f.style.display = "block";
        f.style.height = "100vh";
        f.style.width = w[1] + "px";
        document.body.appendChild(f);
      });
    }
  })();
</script>
</head>
<body>
"""
_DEV_TAIL = """
<script src="data/explain.strings.js"></script>
<script src="data/explain.js"></script>
<script src="../js/core.js"></script>
<script src="../js/site-explain.js"></script>
</body>
</html>
"""


def dev_harness(fragment: str) -> str:
    """The dev page: the fragment's body (its header comments dropped) inside a bare page."""
    body = "\n".join(fragment.splitlines()[3:]).strip()
    return _DEV_HEAD + body + "\n" + _DEV_TAIL


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m second_scribe.app.explain_data",
                                 description="Write the Explain page's case data (dev harness: --dev).")
    ap.add_argument("--kit-root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--out", type=Path, help="write the case as explain.js to this path")
    ap.add_argument("--dev", type=Path, help="write data/explain.js and data/explain.strings.js under this folder")
    a = ap.parse_args(argv)
    js = case_to_js(build_case(a.kit_root))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(js, encoding="utf-8")
    if a.dev:
        (a.dev / "data").mkdir(parents=True, exist_ok=True)
        (a.dev / "data" / "explain.js").write_text(js, encoding="utf-8")
        (a.dev / "data" / "explain.strings.js").write_text(strings_to_js(load_strings("en")), encoding="utf-8")
        page = (Path(__file__).resolve().parent / "pages" / "explain.html").read_text(encoding="utf-8")
        (a.dev / "explain.html").write_text(dev_harness(page), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
