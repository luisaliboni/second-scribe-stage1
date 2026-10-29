"""Builds the synthetic corpus: 12 participants, each with a transcript (.docx), an
interviewer-drawn, participant-endorsed map (.vsdx), a gold graph, and a list of planted traps.

Synthetic recovery is a unit test of the code, not evidence of validity. Same seed -> byte-identical files.
Only ``random.Random.random()`` is used (through small helpers), so the committed corpus does not
depend on the Python version's ``choice``/``shuffle`` implementations.
"""
from __future__ import annotations

import json
import random
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from second_scribe.maps.vsdx_writer import write_vsdx
from second_scribe.schemas import Edge, MapGraph, Node
from second_scribe.synth.bank import (ACKS, CLOSING, CONCEPTS, FILLERS, OPENING, ORDINARY_PROMPTS,
                                      ORDINARY_TURNS, SAFE_FILL_CIDS, SPOKEN_KINDS, SYN_PHASES, TRAP_KINDS,
                                      TRAP_TEMPLATES, FILL_KINDS)
from second_scribe.synth.docx_writer import write_docx

DOMAIN = "food-truck licensing"
ROLES = {"owner": "food-truck owner who applied for and holds the licences",
         "officer": "municipal licensing or inspection officer who processes the applications"}
EMOTIONS = ("Exhausted", "Hopeful", "Lost", "Relieved")
_BY_CID = {c.cid: c for c in CONCEPTS}
_OWN_DIRS = ("transcripts", "maps", "gold", "traps")

# One master (stencil), legend label and fill per node type: the extractor recovers types from the legend.
_TYPES = ("service", "information_source", "actor", "actor_information_source", "scenario")
_MASTER_ID = dict(zip(_TYPES, "12345"))
_MASTERS = {"1": "Process", "2": "Data", "3": "Actor", "4": "ActorData", "5": "Scenario"}
_LEGEND = {"service": "Services", "information_source": "Information & Source", "actor": "Actor",
           "actor_information_source": "Actor + Information Source", "scenario": "Scenario"}
_FILL = {"service": "#cfe2f3", "information_source": "#d9ead3", "actor": "#fff2cc",
         "actor_information_source": "#fce5cd", "scenario": "#d9d2e9"}
_RED = "#ff0000"

# kind -> (expected modality, full-arm bucket, removed-arm bucket)
_EXPECT = {
    "wished": ("wished", "matched", "matched"),
    "negated": ("negated", "unanchored", "unanchored"),
    "other_person": ("other_person", "unanchored", "unanchored"),
    "hypothetical": ("hypothetical", "unanchored", "unanchored"),
    "interviewer_voiced": ("actual", "matched", "missed"),
    "mapping_talk": ("actual", "matched", "missed"),
    "workaround": ("actual", "matched", "matched"),
    "loop": ("actual", "matched", "matched"),
    "dead_end": ("actual", "matched", "matched"),
    "unstated_edge": ("unsupported", "off_record", "off_record"),
    "off_record": ("unsupported", "off_record", "off_record"),
}
_NOT_IN_MAP = ("negated", "other_person", "hypothetical")      # spoken, but never drawn
_INTERVIEWER_SPEAKS = ("interviewer_voiced", "mapping_talk")


@dataclass
class Participant:
    pid: str          # "SYN-OW01".."SYN-OW07" (owners), "SYN-OF01".."SYN-OF05" (officers)
    role: str         # "owner" | "officer"
    interviewer: str  # "I1" | "I2" | "I3"
    fmt: str          # "bracket" | "first_token"


def _participants() -> list[Participant]:
    pids = [("owner", f"SYN-OW{i:02d}") for i in range(1, 8)] + [("officer", f"SYN-OF{i:02d}") for i in range(1, 6)]
    return [Participant(pid, role, f"I{i % 3 + 1}", "bracket" if i % 2 == 0 else "first_token")
            for i, (role, pid) in enumerate(pids)]


def _pick(rng: random.Random, seq):
    return seq[int(rng.random() * len(seq))]


def _sample(rng: random.Random, seq, k: int) -> list:
    """k distinct items, in random order (partial Fisher-Yates)."""
    items = list(seq)
    for i in range(k):
        j = i + int(rng.random() * (len(items) - i))
        items[i], items[j] = items[j], items[i]
    return items[:k]


def _sentence(template: str, cid: str, other: str | None = None) -> str:
    s = template.format(x=_BY_CID[cid].forms[1], y=_BY_CID[other].forms[1] if other else "")
    return s[0].upper() + s[1:]


def _plan(rng: random.Random, p: Participant, kinds: list[str]) -> dict:
    """Decide everything about one participant: concepts, order, trap roles, edges."""
    pool = [c for c in CONCEPTS if p.role in c.roles]
    m = 10 + int(rng.random() * 5)
    spoken = [k for k in TRAP_KINDS if k in kinds and k in SPOKEN_KINDS]
    # kinds whose sentence must not say the thing happened to the speaker take their concept first, from the safe set
    need = [k for k in spoken if k in FILL_KINDS]
    fixed = dict(zip(need, _sample(rng, [c for c in pool if c.cid in SAFE_FILL_CIDS], len(need))))
    rest = _sample(rng, [c for c in pool if c not in fixed.values()], m + 1 - len(fixed))
    drawn = list(fixed.values()) + rest
    keyed = sorted(((SYN_PHASES.index(c.phase), rng.random(), c.cid) for c in drawn[:m]))
    seq = [cid for _, _, cid in keyed]
    off_cid = drawn[m].cid if "off_record" in kinds else None
    role_of = {cid: "ordinary" for cid in seq}
    for k, c in fixed.items():
        role_of[c.cid] = k
    free = [cid for cid in seq if role_of[cid] == "ordinary"]
    others = [k for k in spoken if k not in fixed]
    for k, cid in zip(others, _sample(rng, free, len(others))):
        role_of[cid] = k
    in_map = [cid for cid in seq if role_of[cid] not in _NOT_IN_MAP]
    dead = next((cid for cid in seq if role_of[cid] == "dead_end"), None)
    chain = [cid for cid in in_map if cid != dead]
    edges = list(zip(chain, chain[1:]))
    extra: dict[str, list] = {}
    loop = next((cid for cid in seq if role_of[cid] == "loop"), None)
    if loop is not None:
        i = chain.index(loop)
        # the cycle partner is named in the loop sentence, so it must be an ordinary concept
        # (an interviewer-voiced or mapping-talk concept must stay out of participant speech)
        partners = [c for c in chain if role_of[c] == "ordinary"]
        b = min(partners, key=lambda c: (abs(chain.index(c) - i), chain.index(c)))
        edges += [e for e in ((loop, b), (b, loop)) if e not in edges]
        extra["loop"] = [loop, b]
    if dead is not None:
        before = [cid for cid in chain if seq.index(cid) < seq.index(dead)]
        edges.append(((before[-1] if before else chain[0]), dead))
    gold_edges = list(edges)
    if "unstated_edge" in kinds:
        free = [i for i in range(len(chain) - 2)
                if (chain[i], chain[i + 2]) not in edges and (chain[i + 2], chain[i]) not in edges]
        i = free[int(rng.random() * len(free))]
        pair = (chain[i], chain[i + 2])
        edges.append(pair)
        extra["unstated_edge"] = list(pair)
    return {"seq": seq, "role_of": role_of, "in_map": in_map, "off": off_cid, "edges": edges,
            "gold_edges": gold_edges, "extra": extra, "dead": dead}


def _turns(rng: random.Random, plan: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """[(speaker, text)] and {cid: planted sentence}."""
    turns = [("IV", _pick(rng, OPENING))]
    sentences: dict[str, str] = {}
    for n, cid in enumerate(plan["seq"]):
        kind = plan["role_of"][cid]
        if kind in _INTERVIEWER_SPEAKS:
            sentences[cid] = _sentence(_pick(rng, TRAP_TEMPLATES[kind]), cid)
            turns += [("IV", sentences[cid]), ("PT", _pick(rng, ACKS[kind]))]
            continue
        if n:
            turns.append(("IV", _pick(rng, ORDINARY_PROMPTS)))
        templates = ORDINARY_TURNS if kind == "ordinary" else TRAP_TEMPLATES[kind]
        other = plan["extra"]["loop"][1] if kind == "loop" else None
        sentences[cid] = _sentence(_pick(rng, templates), cid, other)
        text = sentences[cid]
        if rng.random() < 0.4:
            text += " " + _pick(rng, FILLERS)
        turns.append(("PT", text))
    turns.append(("IV", _pick(rng, CLOSING)))
    return turns, sentences


def _paragraphs(rng: random.Random, fmt: str, turns: list[tuple[str, str]]) -> list[str]:
    out, t = [], 0
    for who, text in turns:
        if fmt == "bracket":
            out.append(f"[{who}] {text}")
        else:
            out.append(f"{who} {t // 60}:{t % 60:02d} {text}")
            t += 5 + int(rng.random() * 35)
    return out


def _effective_type(plan: dict, cid: str) -> str:
    return "scenario" if plan["role_of"].get(cid) == "workaround" else _BY_CID[cid].node_type


def _write_map(path: Path, plan: dict, emotion: str) -> dict[str, str]:
    """Draws the endorsed map; returns {extractor node id: cid}."""
    lane_x = {ph: 2.0 + 3.2 * i for i, ph in enumerate(SYN_PHASES)}
    shapes, nid = [], iter(range(1, 10_000))
    for ph in SYN_PHASES:
        shapes.append({"id": next(nid), "text": ph, "pin": (lane_x[ph], 9.5), "w": 2.6, "h": 0.6})
    cids = list(plan["in_map"]) + ([plan["off"]] if plan["off"] else [])
    used = [t for t in _TYPES if t in {_effective_type(plan, c) for c in cids}]
    for k, t in enumerate(used):
        shapes.append({"id": next(nid), "text": _LEGEND[t], "pin": (15.5, 8.5 - 0.9 * k), "w": 2.8, "h": 0.6,
                       "master": _MASTER_ID[t], "fill": _FILL[t]})
    shapes.append({"id": next(nid), "text": "Anything in red is the ideal", "pin": (15.5, 3.0), "w": 3.2,
                   "h": 0.6, "char": _RED})
    shapes.append({"id": next(nid), "text": f"Word: {emotion}", "pin": (15.5, 9.5), "w": 2.4, "h": 0.6})
    rows = {ph: 0 for ph in SYN_PHASES}
    sid_of, pos, endorsed = {}, {}, {}
    for cid in cids:                                  # sequence order, then the off-record node
        c = _BY_CID[cid]
        eff = _effective_type(plan, cid)
        pos[cid] = (lane_x[c.phase], 8.4 - 0.9 * rows[c.phase])
        rows[c.phase] += 1
        sid_of[cid] = next(nid)
        shape = {"id": sid_of[cid], "text": c.forms[0], "pin": pos[cid], "w": 2.4, "h": 0.6,
                 "master": _MASTER_ID[eff], "fill": _FILL[eff]}
        if plan["role_of"].get(cid) == "wished":
            shape["line"] = _RED
        shapes.append(shape)
        endorsed[f"p1.{sid_of[cid]}"] = cid
    connects = []
    for a, b in plan["edges"]:
        cid_shape = next(nid)
        shapes.append({"id": cid_shape, "connector": True, "pin": pos[a], "w": 1, "h": 0,
                       "begin": pos[a], "end": pos[b]})
        connects += [{"from_sheet": cid_shape, "from_cell": "BeginX", "to_sheet": sid_of[a]},
                     {"from_sheet": cid_shape, "from_cell": "EndX", "to_sheet": sid_of[b]}]
    write_vsdx(path, shapes, connects, masters=_MASTERS)
    return endorsed


def _gold(p: Participant, plan: dict) -> tuple[MapGraph, dict[str, str]]:
    """What the transcript supports: every drawn concept except the off-record node and the unstated edge."""
    gid = {cid: f"g{i}" for i, cid in enumerate(plan["in_map"], 1)}
    nodes = [Node(node_id=gid[cid], text=_BY_CID[cid].forms[2], node_type=_effective_type(plan, cid),
                  phase=_BY_CID[cid].phase,
                  status="wished" if plan["role_of"][cid] == "wished" else "experienced")
             for cid in plan["in_map"]]
    edges = [Edge(source=gid[a], target=gid[b]) for a, b in plan["gold_edges"]]
    role_of = plan["role_of"]
    meta = {"interviewer_voiced": [gid[c] for c in plan["in_map"] if role_of[c] == "interviewer_voiced"],
            "mapping_talk_only": [gid[c] for c in plan["in_map"] if role_of[c] == "mapping_talk"]}
    g = MapGraph(map_id=p.pid, origin="synthetic_gold", nodes=nodes, edges=edges, interviewer=p.interviewer, meta=meta)
    return g, {v: k for k, v in gid.items()}


def _traps(rng: random.Random, plan: dict, sentences: dict[str, str]) -> list[dict]:
    out = []

    def entry(kind: str, cid: str, sentence: str, speaker: str) -> dict:
        modality, full, removed = _EXPECT[kind]
        e = {"kind": kind, "cid": cid, "sentence": sentence, "speaker": speaker, "expected_modality": modality,
             "expected_bucket": full, "arm_expectation": {"full": full, "removed": removed}}
        if kind in plan["extra"]:
            e["cids"] = plan["extra"][kind]
        return e
    for cid in plan["seq"]:
        kind = plan["role_of"][cid]
        if kind != "ordinary":
            out.append(entry(kind, cid, sentences[cid], "interviewer" if kind in _INTERVIEWER_SPEAKS else "participant"))
    if "unstated_edge" in plan["extra"]:
        out.append(entry("unstated_edge", plan["extra"]["unstated_edge"][0], "", "none"))
    if plan["off"]:
        out.append(entry("off_record", plan["off"], "", "none"))
    return out


def _write_json(path: Path, obj) -> None:
    path.write_bytes((json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def build_corpus(out_dir, seed: int = 0) -> dict:
    """Writes transcripts/, maps/, gold/ and traps/ for 12 participants plus meta.json; returns the meta dict.
    First deletes those four subdirectories and meta.json in ``out_dir`` (nothing else there is touched).
    Every participant gets six trap kinds; across the corpus every kind appears at least 5 times."""
    rng = random.Random(seed)
    out = Path(out_dir)
    for sub in _OWN_DIRS:                       # remove only what this function writes, never anything else
        shutil.rmtree(out / sub, ignore_errors=True)
        (out / sub).mkdir(parents=True, exist_ok=True)
    (out / "meta.json").unlink(missing_ok=True)
    people = _participants()
    offset = int(rng.random() * len(TRAP_KINDS))
    for i, p in enumerate(people):
        # five consecutive kinds from a rolling window cover every kind across the 12 participants; one more at random
        kinds = [TRAP_KINDS[(offset + i * 5 + j) % len(TRAP_KINDS)] for j in range(5)]
        kinds.append(_pick(rng, [k for k in TRAP_KINDS if k not in kinds]))
        plan = _plan(rng, p, kinds)
        turns, sentences = _turns(rng, plan)
        write_docx(out / "transcripts" / f"{p.pid}.docx", _paragraphs(rng, p.fmt, turns))
        endorsed = _write_map(out / "maps" / f"{p.pid}.vsdx", plan, _pick(rng, EMOTIONS))
        gold, gold_cids = _gold(p, plan)
        _write_json(out / "gold" / f"{p.pid}.json", asdict(gold))
        _write_json(out / "gold" / f"{p.pid}.concepts.json", {"endorsed": endorsed, "gold": gold_cids})
        _write_json(out / "traps" / f"{p.pid}.json", _traps(rng, plan, sentences))
    meta = {"participants": [asdict(p) for p in people], "domain": DOMAIN, "phases": list(SYN_PHASES),
            "roles": dict(ROLES), "seed": seed}
    _write_json(out / "meta.json", meta)
    return meta
