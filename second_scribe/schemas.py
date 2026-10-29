"""Plain data shapes shared by every stage: maps, transcripts, and the closed vocabularies."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

NODE_TYPES = ("service", "information_source", "actor", "actor_information_source", "scenario", "other")
STATUSES = ("experienced", "wished")          # wished == drawn in red / "should exist but didn't"
MODALITIES = ("actual", "wished", "negated", "hypothetical", "other_person", "unsupported")
BUCKETS = ("matched", "attested_absent_from_map", "unanchored", "missed", "off_record")


@dataclass
class Node:
    node_id: str
    text: str
    node_type: str = "other"
    phase: str | None = None
    status: str = "experienced"
    quote: str | None = None            # verbatim supporting quote (AI maps only)
    x: float | None = None
    y: float | None = None


@dataclass
class Edge:
    source: str
    target: str
    status: str = "experienced"
    glued: bool = True                  # False when recovered by geometry fallback
    quote: str | None = None
    label: str | None = None            # connector text on the drawn map, normalised (case kept)


@dataclass
class MapGraph:
    map_id: str                          # e.g. "KC12" or "SYN-OW03"; never initials
    origin: str                          # "endorsed" | "ai" | "prior_only" | "synthetic_gold"
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    emotion_word: str | None = None
    interviewer: str | None = None       # pseudonymous interviewer code, e.g. "I1"
    unresolved_edges: int = 0
    meta: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_json(s: str) -> "MapGraph":
        d = json.loads(s)
        d["nodes"] = [Node(**n) for n in d.get("nodes", [])]
        d["edges"] = [Edge(**e) for e in d.get("edges", [])]
        return MapGraph(**d)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)


@dataclass
class Turn:
    idx: int
    speaker: str                         # "participant" | "interviewer" | "unknown"
    text: str
    mapping_talk: bool = False


_SPEAKER_PREFIX = {"participant": "[P]", "interviewer": "[I]", "unknown": "[?]"}


@dataclass
class Transcript:
    transcript_id: str
    turns: list[Turn]
    speaker_format: str                  # "bracket" | "first_token" | "synthetic" | "none"
    interviewer: str | None = None
    # How speakers were assigned: "label" (participant_labels matched / synthetic / no labels),
    # "word_share" (no label matched; participant = the wordier of the top-2 labels),
    # "ambiguous_labels" (one label covers >=90% of labelled paragraphs; every speaker "unknown").
    speaker_inference: str = "label"

    def text(self, arm: str = "full") -> str:
        """arm "full": all turns; arm "removed": drop turns with mapping_talk=True."""
        if arm not in ("full", "removed"):
            raise ValueError(f"unknown arm {arm!r} (want 'full' or 'removed')")
        turns = [t for t in self.turns if not (arm == "removed" and t.mapping_talk)]
        return "\n".join(f"{_SPEAKER_PREFIX.get(t.speaker, '[?]')} {t.text}" for t in turns)

    def units(self, arm: str = "full") -> list[tuple[str, str]]:
        """The same turns as ``text(arm)``, as [(marker, text)] with marker "I", "P" or "?" (what
        ``textnorm.locate_quote_in_turns`` takes): unit i is turn i of the arm, whatever the text holds."""
        if arm not in ("full", "removed"):
            raise ValueError(f"unknown arm {arm!r} (want 'full' or 'removed')")
        return [(_SPEAKER_PREFIX.get(t.speaker, "[?]")[1], t.text)
                for t in self.turns if not (arm == "removed" and t.mapping_talk)]

    def validation_units(self, arm: str = "full") -> list[tuple[str | None, str]]:
        """``units(arm)`` for the reconstruction validator and the audits: in arm "removed" each run of removed
        mapping-talk turns leaves ONE break unit ``(None, "")``, so the participant paragraphs on both sides of the
        seam are not merged into one turn. Task 6 searches the full transcript, where they are apart; with the break,
        a quote the validator accepts in the removed arm is one Task 6 can find too. Arm "full" is ``units("full")``."""
        if arm not in ("full", "removed"):
            raise ValueError(f"unknown arm {arm!r} (want 'full' or 'removed')")
        out: list[tuple[str | None, str]] = []
        for t in self.turns:
            if arm == "removed" and t.mapping_talk:
                if not out or out[-1][0] is not None:
                    out.append((None, ""))
            else:
                out.append((_SPEAKER_PREFIX.get(t.speaker, "[?]")[1], t.text))
        return out
