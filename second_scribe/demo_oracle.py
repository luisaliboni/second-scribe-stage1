"""The oracle client for the synthetic demo: a "perfect AI" that answers every prompt from the
corpus gold, the concept bank and the transcripts.

It exists to test the code path end to end (``demo --client oracle``) and to record replay
fixtures without a model. Recovering the planted traps with it is a unit test of the code, not
evidence that the method is valid.

It reads only the prompt text (as a real model would): the task is told apart by the JSON shape
the prompt asks for, the participant and arm by the transcript text it carries.
  reconstruct   the gold map for the full arm; for the removed arm the gold map minus the
                interviewer-voiced and mapping-talk-only nodes (when the two arm texts are the same,
                the full gold map); each quote is the whole transcript turn that names the concept
  prior_only    a fixed generic pathway per role (the role's concepts under their map labels)
  judge         the true modality of the quote for the element (trap sentences carry their own)
  align         the pairs that denote the same concept
  canonicalize  the concept id
  in_transcript the turn that names the concept, or null
  confirm       yes for the planted mapping-talk sentences only
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .llm import ModelResponse
from .schemas import MapGraph
from .synth.bank import CONCEPTS
from .textnorm import token_seq
from .transcripts import flag_mapping_talk, parse_paragraphs, read_docx_text

_NUM = re.compile(r"^(\d+)\. (.*)$", re.M)
_ITEM = re.compile(r'^(\d+)\. ELEMENT: (.*)\n\s+QUOTE(?: \((\w+)\))?: "(.*)"$', re.M)
_TAG = re.compile(r"^\[[A-Z?]\]\s*")
_PRIOR_SIZE = 10
# where the transcript starts in a reconstruction prompt: "TRANSCRIPT:" in the rung 1 / rung 2 prompts, "The participant
# says:" in the remedy and "The transcript follows:" in the placebo prompt of the instruction ladder (the demonstrations
# in those two carry other transcripts)
_TRANSCRIPT_MARKERS = ("TRANSCRIPT:\n", "The participant says:\n", "The transcript follows:\n")


def _transcript_of(user: str) -> str | None:
    """The transcript block of a reconstruction prompt, or None if the prompt has none."""
    marker = next((m for m in _TRANSCRIPT_MARKERS if m in user), None)
    return None if marker is None else _block(user, marker)


def _key(label: str) -> str:
    return " ".join(label.casefold().split())


def _block(text: str, start: str, ends=("\nREMINDER:",)) -> str:
    i = text.index(start) + len(start)
    stop = min((j for j in (text.find(e, i) for e in ends) if j >= 0), default=len(text))
    return text[i:stop].strip("\n")


def _numbered(block: str) -> dict[int, str]:
    return {int(n): s.strip() for n, s in _NUM.findall(block)}


class OracleClient:
    """Deterministic offline "model" for the synthetic corpus (see the module docstring)."""

    def __init__(self, corpus_dir, roles: dict[str, str], participant_labels=("pt",), interviewer_labels=("iv",)):
        root = Path(corpus_dir)
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        self.phases = tuple(meta["phases"])
        self.roles = roles                                        # role key -> role text used in prompts
        self.by_cid = {c.cid: c for c in CONCEPTS}
        self.cid_of: dict[str, str] = {}
        for c in CONCEPTS:
            for f in (c.cid, *c.forms):
                self.cid_of.setdefault(_key(f), c.cid)
        self.traps: list[tuple[str, set[str], str]] = []          # (token_seq sentence, cids, modality)
        self.mapping_sentences: set[str] = set()
        self.arm_text: dict[str, tuple[str, str]] = {}            # arm transcript text -> (pid, arm)
        self.gold: dict[str, MapGraph] = {}
        self.turns: dict[str, list] = {}
        for p in meta["participants"]:
            pid = p["pid"]
            traps = json.loads((root / "traps" / f"{pid}.json").read_text(encoding="utf-8"))
            for t in traps:
                if t["sentence"]:
                    self.traps.append((token_seq(t["sentence"]), set(t.get("cids") or [t["cid"]]),
                                       t["expected_modality"]))
                if t["kind"] == "mapping_talk":
                    self.mapping_sentences.add(token_seq(t["sentence"]))
        for p in meta["participants"]:
            pid = p["pid"]
            gold = MapGraph.from_json((root / "gold" / f"{pid}.json").read_text(encoding="utf-8"))
            concepts = json.loads((root / "gold" / f"{pid}.concepts.json").read_text(encoding="utf-8"))
            gold.meta["cid"] = concepts["gold"]
            self.gold[pid] = gold
            t = parse_paragraphs(read_docx_text(root / "transcripts" / f"{pid}.docx"), set(participant_labels),
                                 pid, interviewer_labels=set(interviewer_labels))
            t = flag_mapping_talk(t)
            for x in t.turns:   # the model's answer on every interviewer turn, as the pipeline's confirmation stage
                x.mapping_talk = x.speaker == "interviewer" and self._is_mapping(x.text)
            self.turns[pid] = t.turns
            self.arm_text.setdefault(t.text("full"), (pid, "full"))
            self.arm_text.setdefault(t.text("removed"), (pid, "removed"))

    # ------------------------------------------------------------------ helpers
    def _is_mapping(self, text: str) -> bool:
        ts = token_seq(text)
        return any(s in ts for s in self.mapping_sentences)

    def _cid(self, label: str) -> str | None:
        return self.cid_of.get(_key(label))

    def _mentions(self, cid: str, text: str) -> bool:
        return token_seq(self.by_cid[cid].forms[1]) in token_seq(text)

    def _quote_for(self, pid: str, cid: str) -> str | None:
        turns = [t for t in self.turns[pid] if self._mentions(cid, t.text)]
        best = next((t for t in turns if t.speaker == "participant"), turns[0] if turns else None)
        return best.text if best else None

    # ------------------------------------------------------------------ answers
    def _reconstruct(self, user: str) -> dict:
        pid, arm = self.arm_text[_transcript_of(user)]
        gold = self.gold[pid]
        drop = set(gold.meta.get("interviewer_voiced", [])) | set(gold.meta.get("mapping_talk_only", [])) \
            if arm == "removed" else set()
        quote = {n.node_id: self._quote_for(pid, gold.meta["cid"][n.node_id]) for n in gold.nodes}
        nodes = [{"id": n.node_id, "label": n.text, "type": n.node_type, "phase": n.phase, "status": n.status,
                  "quote": quote[n.node_id]} for n in gold.nodes if n.node_id not in drop]
        edges = [{"source": e.source, "target": e.target, "status": e.status, "quote": quote[e.source]}
                 for e in gold.edges if e.source not in drop and e.target not in drop]
        return {"nodes": nodes, "edges": edges, "emotion_word": None}

    def _prior_only(self, user: str) -> dict:
        role = max((r for r, text in self.roles.items() if text in user), key=lambda r: len(self.roles[r]))
        pool = [c for c in CONCEPTS if role in c.roles][:_PRIOR_SIZE]
        nodes = [{"id": f"n{k}", "label": c.forms[0], "type": c.node_type, "phase": c.phase,
                  "status": "experienced", "quote": None} for k, c in enumerate(pool, 1)]
        edges = [{"source": f"n{k}", "target": f"n{k + 1}", "status": "experienced", "quote": None}
                 for k in range(1, len(pool))]
        return {"nodes": nodes, "edges": edges, "emotion_word": None}

    def _modality(self, element: str, quote: str) -> str:
        if " → " in element:
            ends = [self._cid(x) for x in element.split(" → ", 1)]
            return "actual" if any(c and self._mentions(c, quote) for c in ends) else "unsupported"
        cid = self._cid(element)
        if cid is None:
            return "unsupported"
        tq = token_seq(quote)
        for sentence, cids, modality in self.traps:
            if cid in cids and (sentence in tq or tq in sentence):
                return modality
        return "actual" if self._mentions(cid, quote) else "unsupported"

    def _judge(self, user: str) -> dict:
        items = _ITEM.findall(_block(user, "ITEMS:\n"))
        return {"labels": [{"n": int(n), "modality": self._modality(el, q)} for n, el, _sp, q in items]}

    def _align(self, user: str) -> dict:
        left = _numbered(_block(user, "LIST A:\n", ("\nLIST B:",)))
        right = _numbered(_block(user, "LIST B:\n"))
        rc = {b: self._cid(t) for b, t in right.items()}
        return {"pairs": [{"a": a, "b": b, "rel": "same"} for a, t in sorted(left.items())
                          for b, c in sorted(rc.items()) if c is not None and c == self._cid(t)]}

    def _canonicalize(self, user: str) -> dict:
        labels = _numbered(_block(user, "LABELS:\n"))
        return {"concepts": [{"n": n, "concept": self._cid(t) or " ".join(t.lower().split()[:4])}
                             for n, t in sorted(labels.items())]}

    def _in_transcript(self, user: str) -> dict:
        lines = [_TAG.sub("", line) for line in _block(user, "TRANSCRIPT:\n", ("\nLABELS:",)).splitlines()]
        out = []
        for n, label in sorted(_numbered(_block(user, "LABELS:\n")).items()):
            cid = self._cid(label)
            q = next((line for line in lines if cid and self._mentions(cid, line)), None)
            out.append({"n": n, "quote": q})
        return {"answers": out}

    def _confirm(self, user: str) -> dict:
        turns = _numbered(_block(user, "TURNS:\n"))
        return {"answers": [{"n": n, "map_talk": "yes" if self._is_mapping(t) else "no"}
                            for n, t in sorted(turns.items())]}

    def complete(self, messages, *, temperature, seed, max_tokens) -> ModelResponse:
        user = next(m["content"] for m in messages if m.get("role") == "user")   # the task, not a retry note
        if '"map_talk"' in user:
            ans = self._confirm(user)
        elif '{"pairs"' in user:
            ans = self._align(user)
        elif '{"concepts"' in user:
            ans = self._canonicalize(user)
        elif '{"labels"' in user:
            ans = self._judge(user)
        elif '{"answers"' in user:
            ans = self._in_transcript(user)
        elif _transcript_of(user) is not None:
            ans = self._reconstruct(user)
        else:
            ans = self._prior_only(user)
        text = json.dumps(ans, ensure_ascii=False, sort_keys=True)
        return ModelResponse(text, "stop", {}, 0, hashlib.sha256(text.encode("utf-8")).hexdigest())
