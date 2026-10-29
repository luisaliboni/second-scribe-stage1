"""Held-out demonstration set for the instruction ladder (rung 3 "remedy" and its placebo).

Four short synthetic interview excerpts, each with the correct JSON fragment in exactly the v2
reconstruction schema. They come from DIFFERENT domains than the synthetic corpus (which is
food-truck licensing, owner/officer): a car repair under warranty and a lost suitcase. Both are far
from the real study's domain (no health, disability, children, school, care, government services or
benefits), so the demos cannot prime in-domain content (tests/test_ladder_demos.py keeps a forbidden-word list).
Demos are prompt text only: they are never scored, never part of any scored item (synthetic or
real), and ``second_scribe.ladder`` audits model outputs for copies of their wording.

REMEDY demos (REM1, REM2): the participant's pathway departs from the usual one. They say a step the
institution normally provides was missing and wish it had existed, so the correct label is
"wished", explained in the third person ("The participant says ...").

PLACEBO demos (PLA1, PLA2): same number, same format, near-identical length, the same share of
"wished" labels, but no departure from the usual pathway: the step happened ("experienced") and
the one wish is a side matter that is no step of the pathway. No narrator frame in the note.
Each placebo is built as a minimal rewrite of its remedy twin so that only the active ingredient
(departure + narrator frame) differs.

``phase`` is null in every fragment, so a demo can never push a phase name that the run's own
template does not have.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..textnorm import tokens

DEMO_NGRAM_N = 6
ARMS = ("remedy", "placebo")

INTRO = ("Two worked examples from other interviews follow. They show how to fill in the format; "
         "do not reuse their wording.")


@dataclass(frozen=True)
class Demo:
    demo_id: str          # "REM1", "REM2" (remedy), "PLA1", "PLA2" (placebo)
    arm: str              # "remedy" | "placebo"
    setting: str          # one phrase naming the interview, shown in the example header
    excerpt: str          # interview turns, [I] / [P] tagged, one turn per line
    fragment: dict        # the correct JSON in the v2 schema
    note: str             # one sentence on why the wished label is right
    unrelated_wish: bool  # True when the wish is a side matter, no step of the pathway (placebo)


def _node(i, label, typ, status, quote):
    return {"id": i, "label": label, "type": typ, "phase": None, "status": status, "quote": quote}


def _edge(s, t, status, quote):
    return {"source": s, "target": t, "status": status, "quote": quote}


_REM1_T1 = "I dropped the car at the dealership on Monday morning, and the advisor wrote up the noise on a job sheet."
_REM1_T2 = ("No, I had to take the bus for nine days. "
            "I wish the dealer had lent me a car, because I drive to work every day.")
_REM1_T3 = ("Once the job sheet was approved by the manufacturer, a mechanic phoned me on the tenth day "
            "to say the car was ready.")
_PLA1_T2 = ("Yes, once the job sheet was signed, the dealer lent me a small car for the whole nine days "
            "and I only had to fill the tank.")
_PLA1_T3 = "I wish the dealership were closer to my house, since it took me an hour to get there each way, and the lot was always full by the time I got there."

_REM2_T1 = "My suitcase never came off the plane, so I filled in a report at the baggage desk before I left the airport."
_REM2_T2 = ("No, they only handed me a copy of the form with no number on it. "
            "I wish they had given me a tracking number, because I phoned the airline eleven times to ask.")
_REM2_T3 = ("Once the report was entered, a courier brought the suitcase to my door five days later "
            "without any warning.")
_PLA2_T2 = ("Yes, once the report was entered the clerk gave me a tracking number on a slip, "
            "and I looked it up online each morning.")
_PLA2_T3 = "I wish the arrivals hall had more seats, since I stood for an hour while the desk worked through the long line at the desk."

REMEDY_DEMOS: tuple[Demo, ...] = (
    Demo("REM1", "remedy", "a customer talking about a car repair under warranty",
         "[I] Walk me through how the repair went.\n"
         f"[P] {_REM1_T1}\n"
         "[I] Did they give you anything to drive while it was in the shop?\n"
         f"[P] {_REM1_T2}\n"
         "[I] And when it was ready?\n"
         f"[P] {_REM1_T3}",
         {"nodes": [_node("n1", "Repair slip on arrival", "information_source", "experienced", _REM1_T1),
                    _node("n2", "Loaner car from dealer", "service", "wished", _REM1_T2),
                    _node("n3", "Phone call that car was ready", "service", "experienced", _REM1_T3)],
          "edges": [_edge("n1", "n3", "experienced", _REM1_T3)],
          "emotion_word": None},
         "The participant says no car was lent and wishes the dealer had lent one, "
         "so that step is wished, even though a dealer under warranty normally provides it.",
         False),
    Demo("REM2", "remedy", "a traveller talking about a lost suitcase",
         "[I] What did you do when the suitcase did not appear?\n"
         f"[P] {_REM2_T1}\n"
         "[I] Did they give you anything to follow the search?\n"
         f"[P] {_REM2_T2}\n"
         "[I] How did you get the suitcase back?\n"
         f"[P] {_REM2_T3}",
         {"nodes": [_node("n1", "Lost bag report", "service", "experienced", _REM2_T1),
                    _node("n2", "Tracking number for the bag", "information_source", "wished", _REM2_T2),
                    _node("n3", "Courier delivery to my door", "service", "experienced", _REM2_T3)],
          "edges": [_edge("n1", "n3", "experienced", _REM2_T3)],
          "emotion_word": None},
         "The participant says no tracking number was given and wishes there had been one, "
         "so that step is wished, even though an airline normally issues one.",
         False),
)

PLACEBO_DEMOS: tuple[Demo, ...] = (
    Demo("PLA1", "placebo", "a customer talking about a car repair under warranty",
         "[I] Walk me through how the repair went.\n"
         f"[P] {_REM1_T1}\n"
         "[I] Did they give you anything to drive while it was in the shop?\n"
         f"[P] {_PLA1_T2}\n"
         "[I] Is there anything else you remember?\n"
         f"[P] {_PLA1_T3}",
         {"nodes": [_node("n1", "Repair slip on arrival", "information_source", "experienced", _REM1_T1),
                    _node("n2", "Loaner car from dealer", "service", "experienced", _PLA1_T2),
                    _node("n3", "Dealership closer to home", "other", "wished", _PLA1_T3)],
          "edges": [_edge("n1", "n2", "experienced", _PLA1_T2)],
          "emotion_word": None},
         "Both steps happened, so they are experienced; the closer dealership is only wanted, "
         "so it is wished, and it is no step of the repair at all.",
         True),
    Demo("PLA2", "placebo", "a traveller talking about a lost suitcase",
         "[I] What did you do when the suitcase did not appear?\n"
         f"[P] {_REM2_T1}\n"
         "[I] Did they give you anything to follow the search?\n"
         f"[P] {_PLA2_T2}\n"
         "[I] Is there anything else you remember?\n"
         f"[P] {_PLA2_T3}",
         {"nodes": [_node("n1", "Lost bag report", "service", "experienced", _REM2_T1),
                    _node("n2", "Tracking number on a slip", "information_source", "experienced", _PLA2_T2),
                    _node("n3", "Chairs in arrivals", "other", "wished", _PLA2_T3)],
          "edges": [_edge("n1", "n2", "experienced", _PLA2_T2)],
          "emotion_word": None},
         "Both steps happened, so they are experienced; the extra seats are only wanted, "
         "so they are wished, and they are no step of the claim at all.",
         True),
)


def demos_for(arm: str) -> tuple[Demo, ...]:
    if arm == "remedy":
        return REMEDY_DEMOS
    if arm == "placebo":
        return PLACEBO_DEMOS
    raise ValueError(f"unknown demo arm {arm!r} (want one of {ARMS})")


def render_fragment(fragment: dict) -> str:
    """The fragment laid out like the prompt's own {schema} text (nodes and edges one per line)."""
    nodes = ",\n  ".join(json.dumps(n, ensure_ascii=False) for n in fragment["nodes"])
    edges = ",\n  ".join(json.dumps(e, ensure_ascii=False) for e in fragment["edges"])
    return (f'{{"nodes": [{nodes}],\n "edges": [{edges}],\n '
            f'"emotion_word": {json.dumps(fragment["emotion_word"])}}}')


def render_demo(demo: Demo, number: int) -> str:
    return (f"Example {number} ({demo.setting}):\n{demo.excerpt}\n"
            f"Correct JSON for this excerpt:\n{render_fragment(demo.fragment)}\n"
            f"Note: {demo.note}")


def render_demos(arm: str) -> str:
    """The literal block embedded in the prompt file of ``arm``: intro line, then the demos."""
    return INTRO + "\n\n" + "\n\n".join(render_demo(d, i) for i, d in enumerate(demos_for(arm), 1))


def demo_texts(demo: Demo) -> list[str]:
    """Every piece of demo wording a model could copy: the excerpt turns, node labels, quotes and the note.
    JSON keys and schema words are not here (every output has them)."""
    parts = [t[4:] for t in demo.excerpt.split("\n")]
    parts += [n["label"] for n in demo.fragment["nodes"]]
    parts += [x["quote"] for x in demo.fragment["nodes"] + demo.fragment["edges"]]
    parts.append(demo.note)
    return parts


def demo_ngrams(n: int = DEMO_NGRAM_N, arms: tuple[str, ...] = ARMS) -> frozenset:
    """All n-grams (tuples of normalised tokens, ``textnorm.tokens``) of the demo wording of the given arms,
    taken inside each piece of text, never across two pieces."""
    grams = set()
    for arm in arms:
        for d in demos_for(arm):
            for text in demo_texts(d):
                t = tokens(text)
                grams.update(tuple(t[i:i + n]) for i in range(len(t) - n + 1))
    return frozenset(grams)


def demo_label_tuples(arms: tuple[str, ...] = ARMS) -> frozenset:
    """The node labels of the demos of the given arms as tuples of normalised tokens. They are 2-5 tokens long,
    so ``demo_ngrams`` (6-grams) can never see them; the copy auditor compares whole labels instead."""
    return frozenset(tuple(tokens(n["label"])) for arm in arms for d in demos_for(arm) for n in d.fragment["nodes"])
