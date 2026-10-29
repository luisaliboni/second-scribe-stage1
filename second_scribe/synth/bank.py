"""Concept bank and trap templates for the synthetic food-truck-licensing corpus.

All wording is everyday English about licensing a food truck; nothing here relates to real
participants. Every concept wording is a noun phrase, so one template fits any concept.
Speech outside the ``mapping_talk`` templates never uses mapping vocabulary (map, box, draw,
arrow, red, legend, screen, put/add/write/place/move ... here/in/on), so the rule-based
mapping-talk flag fires on the planted turns only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SYN_PHASES = ("Planning", "Permits", "Inspections", "Operating")


@dataclass(frozen=True)
class Concept:
    cid: str                      # "C01"
    forms: tuple[str, str, str]   # (map label, transcript wording, alt wording used by the gold graph)
    node_type: str                # one of schemas.NODE_TYPES
    phase: str                    # one of SYN_PHASES
    roles: tuple[str, ...]        # ("owner",), ("officer",) or both
    cluster: str = ""             # concepts of one cluster are about the same thing ("health", "fire", ...); ""=none


_B = ("owner", "officer")
_O = ("owner",)


# Domain clusters: a quote about one concept can be read as about another of the same cluster, so the benchmark
# never pairs them as "unrelated" (health permit / health inspection / health official; fire permit / fire
# inspection; parking permit / spots / fine / patrol; licence / clerk / yearly renewal).
_CLUSTERS = {"health": ("C11", "C17", "C20"), "fire": ("C12", "C18"), "parking": ("C15", "C25", "C27", "C28"),
             "licence": ("C09", "C16", "C26")}
_CLUSTER_OF = {cid: name for name, cids in _CLUSTERS.items() for cid in cids}


def _c(cid, forms, node_type, phase, roles=_B) -> Concept:
    return Concept(cid, forms, node_type, phase, roles, _CLUSTER_OF.get(cid, ""))


CONCEPTS: tuple[Concept, ...] = (
    # Planning
    _c("C01", ("Business plan", "the written plan for how the truck would make money", "startup plan"),
       "service", "Planning", _O),
    _c("C02", ("Licensing guide", "the how-to pages on the city website", "online permit handbook"),
       "information_source", "Planning"),
    _c("C03", ("Vendor association", "the club for street food sellers", "local vendor group"),
       "actor_information_source", "Planning"),
    _c("C04", ("Startup loan", "the money we borrowed from the credit union", "small business loan"),
       "service", "Planning", _O),
    _c("C05", ("Truck purchase", "the used truck we bought", "vehicle purchase"),
       "scenario", "Planning", _O),
    _c("C06", ("Menu costing", "the worksheet for pricing each dish", "recipe price sheet"),
       "information_source", "Planning", _O),
    _c("C07", ("Pre-application meeting", "the first sit-down with the licensing office before we applied",
               "intake chat with city staff"), "service", "Planning"),
    _c("C08", ("Business advisor", "the counsellor at the library who helps new businesses", "free startup counsellor"),
       "actor", "Planning"),
    # Permits
    _c("C09", ("Business licence", "the permit to run a business from city hall", "city business permit"),
       "service", "Permits"),
    _c("C10", ("Zoning clearance", "the letter saying the land could be used for vending", "land-use approval"),
       "service", "Permits"),
    _c("C11", ("Health permit", "the approval from the health unit", "public health authorization"),
       "service", "Permits"),
    _c("C12", ("Fire safety permit", "the sign-off from the fire department", "fire marshal approval"),
       "service", "Permits"),
    _c("C13", ("Commissary agreement", "the deal for renting kitchen space to prepare food", "shared kitchen contract"),
       "service", "Permits", _O),
    _c("C14", ("Insurance certificate", "the paper proving we had liability coverage", "proof of coverage"),
       "information_source", "Permits"),
    _c("C15", ("Parking permit", "the permission to park on the street", "curb-space authorization"),
       "service", "Permits"),
    _c("C16", ("Licensing clerk", "the person at the licensing counter", "front desk staff member"),
       "actor", "Permits"),
    # Inspections
    _c("C17", ("Health inspection", "the visit when the health unit checked the truck over", "sanitation check"),
       "scenario", "Inspections"),
    _c("C18", ("Fire inspection", "the visit from the fire department to check our propane", "propane and extinguisher check"),
       "scenario", "Inspections"),
    _c("C19", ("Vehicle safety check", "the garage test to see if the truck was fit for the road", "roadworthiness test"),
       "scenario", "Inspections", _O),
    _c("C20", ("Inspector", "the health official who came out to the truck", "sanitation officer"),
       "actor", "Inspections"),
    _c("C21", ("Inspection checklist", "the list of things they would look for", "pre-inspection list"),
       "information_source", "Inspections"),
    _c("C22", ("Re-inspection", "the second visit after we failed the first one", "follow-up check"),
       "scenario", "Inspections"),
    _c("C23", ("Water tank approval", "the sign-off on our fresh water supply", "plumbing sign-off"),
       "service", "Inspections", _O),
    _c("C24", ("Plan review", "the city engineer going over our truck layout", "design plans review"),
       "service", "Inspections"),
    # Operating
    _c("C25", ("Vending locations list", "the list of spots where we were allowed to sell", "permitted location roster"),
       "information_source", "Operating"),
    _c("C26", ("Annual renewal", "the paperwork we redo every year", "licence renewal cycle"),
       "service", "Operating"),
    _c("C27", ("Bylaw officer", "the city employee who patrols the streets", "enforcement officer"),
       "actor", "Operating"),
    _c("C28", ("Parking ticket", "the fine for parking in the wrong spot", "curb fine"),
       "scenario", "Operating", _O),
    _c("C29", ("Festival permit", "the one-off authorization to sell at the summer festival", "event vendor authorization"),
       "service", "Operating"),
    _c("C30", ("Sales tax registration", "the account we opened to collect sales tax", "provincial tax account"),
       "service", "Operating", _O),
    _c("C31", ("Owners group chat", "the messaging thread with other truck owners", "owner peer network"),
       "actor_information_source", "Operating", _O),
    _c("C32", ("Noise complaint", "the neighbour's objection to the noise", "noise grievance record"),
       "scenario", "Operating"),
)

# A concept wording "says it happened to the speaker" when it has a first-person word ("the money we borrowed",
# "to check our propane") or a past-tense event clause with no subject of its own ("the visit when the health unit
# checked the truck over", "the health official who came out to the truck"). Sentences that are NOT about the speaker's
# own experience (wished, negated, other_person, hypothetical) and questions the interviewer asks about a step take only
# wordings that do not, so the filled sentence never contradicts its gold label. This is the same rule as
# ``judge_bench.says_it_happened`` (copied: judge_bench imports this package, so it cannot be imported here); a lint
# test (tests/test_synth_gold.py) checks that the safe set is a subset of the bench's own.
_FIRST_PERSON = re.compile(r"\b(we|our)\b", re.I)
_EVENT_CLAUSE = re.compile(r"\b(who|when|where|which|that)\s+(?:\w+\s+){0,3}?"
                           r"(\w+ed|came|went|did|had|got|took|saw|made|ran|gave)\b", re.I)


def says_it_happened(wording: str) -> bool:
    return bool(_FIRST_PERSON.search(wording) or _EVENT_CLAUSE.search(wording))


SAFE_FILL_CIDS = frozenset(c.cid for c in CONCEPTS if not says_it_happened(c.forms[1]))
# Trap kinds whose concept must come from SAFE_FILL_CIDS (the speaker does not say it happened to them, or the
# interviewer's question is about the step itself).
FILL_KINDS = ("wished", "negated", "other_person", "hypothetical", "interviewer_voiced")

TRAP_KINDS = ("wished", "negated", "other_person", "hypothetical", "interviewer_voiced",
              "mapping_talk", "workaround", "loop", "dead_end", "unstated_edge", "off_record")
# Kinds that put a sentence into the transcript; unstated_edge and off_record live only in the maps.
SPOKEN_KINDS = tuple(k for k in TRAP_KINDS if k not in ("unstated_edge", "off_record"))

# "{x}" takes the concept's transcript wording (a noun phrase); loop templates also take "{y}", the second
# concept of the cycle. The last two kinds are never spoken;
# their templates only describe the trap in the traps file.
TRAP_TEMPLATES: dict[str, tuple[str, ...]] = {
    "wished": (
        "I wish there had been {x}.",
        "I really wish somebody had offered us {x}.",
        "What we needed was {x}, but nothing like it was there.",
    ),
    "negated": (
        "We never got {x}, nobody offered it.",
        "We never went through {x} at all.",
        "Nobody ever gave us {x}; it never happened.",
    ),
    # other_person: it happened to someone else and the sentence says nothing about the speaker (a "but we didn't" or
    # "not us" would make it negated, which outranks other_person)
    "other_person": (
        "My cousin got {x} last spring.",
        "A friend of mine ended up going through {x} twice.",
        "My neighbour had {x} sorted in a week.",
    ),
    # hypothetical: only a possibility; nothing assumes it happened to the speaker (no "if we had applied ...")
    "hypothetical": (
        "If we ever move to another city, {x} might work differently there.",
        "Someday we may find out more about {x}.",
        "It is possible that {x} could move online soon.",
    ),
    "interviewer_voiced": (
        "So {x} came next, right?",
        "And then {x} followed, is that right?",
        "Would {x} be the next thing?",
    ),
    "mapping_talk": (
        "I'll put a box for {x} here on the map.",
        "Let me draw an arrow over to {x} on the screen.",
        "I will write {x} down on the map next to the last one.",
    ),
    # workaround: the speaker says they got through x, with paid or informal help, so x happened to them
    "workaround": (
        "We paid a consultant out of pocket and he took us through {x}.",
        "In the end we hired a fixer ourselves, and he walked us through {x}.",
        "We got through {x} with the help of a friend who knew the system.",
    ),
    "loop": (          # two slots: {x} and {y} are the two concepts of the cycle
        "{x} sent us back to {y}, and {y} sent us back to {x}, three times.",
        "We got sent from {x} to {y}, then from {y} back to {x}, three times.",
        "{x} and {y} each pointed us at the other, three times in a row.",
    ),
    # dead_end: the speaker says x happened; what came after it went nowhere (x is not the thing they sought in vain)
    "dead_end": (
        "We went through {x}, but nothing ever came of it.",
        "We finished {x} and then it just went nowhere; nobody replied.",
        "We got through {x}, and after that the trail went cold.",
    ),
    "unstated_edge": (
        "The drawn link out of {x} is never connected to its target anywhere in the interview.",
        "Nobody in the interview ties {x} to the later step the link points at.",
        "The link leaving {x} is absent from everything that was said.",
    ),
    "off_record": (
        "{x} is drawn but never mentioned in the interview.",
        "The interview never touches {x}, yet it is drawn.",
        "Nobody spoke about {x}; it is only drawn.",
    ),
}

# Interviewer prompts that open each ordinary participant turn.
ORDINARY_PROMPTS = ("What happened next?", "And after that?", "How did that go?",
                    "Then what?", "What came after that?")
# Participant sentences for an ordinary (actually experienced) concept.
ORDINARY_TURNS = ("Then we dealt with {x}.", "After that came {x}.", "The next step was {x}.",
                  "Then there was {x}.")
# Participant asides appended to a turn now and then; they never name a concept.
FILLERS = ("It took a long time.", "Nobody explained the order of things.", "Honestly, it was confusing.",
           "That part was stressful.", "We just kept going.")
# Participant replies after an interviewer-only turn.
ACKS = {
    "interviewer_voiced": ("Yes.", "Yes, that's right.", "Right, yes."),
    # the reply says it happened: that, not the announcement of a box, is what makes the full-arm gold "actual"
    "mapping_talk": ("Yes, that is what happened to us.", "Right, we went through that.",
                     "Yes, that is how it happened for us."),
}
OPENING = ("Thanks for talking with me today. Could you start from the beginning?",
           "Thank you for your time. Where did it all begin?")
CLOSING = ("That covers what I wanted to ask. Thank you.", "Thanks, that is everything for today.")
