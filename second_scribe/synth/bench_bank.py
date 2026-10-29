"""Paraphrase bank for the judge benchmark only (never used to build the corpus or its transcripts).

The planted traps of ``bank.TRAP_TEMPLATES`` have three templates per class, so a judge could learn them from the
bench itself. This bank gives each modality class a dozen more sentence templates, in the same food-truck
licensing domain, with a ``{x}`` slot that takes a concept's transcript wording (a noun phrase with its own
article, so the slot sits mid-sentence or opens it).

Each template has ONE correct label under the shared v2 definitions and the judge's precedence (actual, then
wished, then negated, then other_person, then hypothetical), and carries a one-line comment saying which label and
why:

* actual: says it happened to them or existed for them. An attempt (applying, asking, a visit they went through)
  counts even if it failed; the thing they were trying to get counts only if they say they got it.
* wished: says they wanted or needed it, or that it should have existed, and does not say it happened to them.
  (A sentence that also says they never found it is still wished: wished outranks negated.)
* negated: says it did not happen or did not exist for them, and says nothing about wanting it or about anyone else.
* other_person: says it happened to someone else, and nothing about the speaker (a "not us" would also be a
  negation, which outranks other_person).
* hypothetical: talks about it only as a possibility, a condition or a guess about the future; it never assumes
  that it happened to the speaker and never says they want or need it.

Non-actual sentences are filled only with concept wordings that do not themselves say the thing happened to the
speaker (``FILL_CONCEPTS`` in ``judge_bench``: no "we" or "our" and no subjectless past-tense event clause in the wording), so the filled sentence cannot contradict its
label. About a third of the non-actual templates carry no cue word at all (no wish, never, if, my cousin ...), and
several actual templates carry a cue word on purpose ("did not enjoy ... but we went through it"), so a keyword rule
gets many of them wrong. The tests check this against ``keywords.keyword_modality``.
"""
from __future__ import annotations

BENCH_TEMPLATES: dict[str, tuple[str, ...]] = {
    "wished": (
        # wished: states a wish about what the city should have given them; nothing happened to them
        "Looking back, I wish the city had given us {x}.",
        # wished: states a need and does not say they got it
        "What we really needed was {x}.",
        # wished: would have loved = wanted, did not have
        "I would have loved to have {x}.",
        # wished: it should have existed for people like them
        "Someone should have set up {x} for people like us.",
        # wished: hoping to be pointed to it = wanted, no sign they got it
        "We kept hoping someone would point us to {x}.",
        # wished: wanted it and nobody could say where to get it
        "All I wanted was {x}, and nobody could tell us where to get it.",
        # wished: "if only there had been" = wanted it, it did not exist for them
        "If only there had been {x}.",
        # wished: a missing piece they were crying out for = needed, did not get
        "For us, {x} was the missing piece we were crying out for.",
        # wished: needed it and could not find it (wished outranks negated)
        "I needed {x} and could not find it.",
        # wished: badly needed it and it was nowhere to be found
        "We badly needed {x}, but it was nowhere to be found.",
        # wished: longed for = wanted
        "What I longed for was {x}.",
        # wished: wishes it had existed
        "Honestly, {x} is the one thing I wish had existed.",
    ),
    "negated": (
        # negated: never had it, nothing else said
        "In the end we never had {x} at all.",
        # negated: never part of it for them
        "{x} was never part of it for us.",
        # negated: nobody ever handed it to them; says nothing about wanting it
        "Nobody at the city ever handed us {x}.",
        # negated: they skipped it
        "We skipped {x} entirely.",
        # negated: it did not happen for them
        "{x} didn't happen for us.",
        # negated: the step was not there in their case
        "That step, {x}, simply wasn't there in our case.",
        # negated: managed without it
        "We got by without {x}.",
        # negated: it did not come up
        "In our case {x} did not come up at all.",
        # negated: nothing like it ever came their way
        "Nothing like {x} ever came our way.",
        # negated: exempt = it did not apply to them
        "We were exempt from {x}.",
        # negated: a plain denial that they ever had to deal with it
        "We never had to deal with {x}.",
        # negated: a plain denial that it applied to them
        "{x} never applied to us.",
        # negated: spared = it did not happen to them
        "We were spared {x}.",
        # negated: it passed them by, did not happen to them
        "{x} passed us by completely.",
    ),
    "hypothetical": (
        # Every hypothetical template is about what the speaker might face; none says it happened, none states a
        # wish or a denial, and none is a generic statement about the process (those read as "unsupported" too).
        # hypothetical: a conditional about a future expansion; does not say it happened or that they want it
        "If we ever expand, we might have to look at {x}.",
        # hypothetical: a vague future possibility ("maybe one day"), no cue word
        "Maybe one day we will look into {x}.",
        # hypothetical: a guess about the speaker's next year; no cue word
        "Perhaps we will have to deal with {x} next year.",
        # hypothetical: a possible future for the speaker, depending on who they get
        "We could end up facing {x} one day, depending on who we get.",
        # hypothetical: a possibility for the speaker, conditional on the officer
        "Depending on the officer, we may have to go through {x} next year.",
        # hypothetical: a guess that the speaker may end up with it
        "Who knows, we may well end up with {x} one day.",
        # hypothetical: guessing what the speaker would be asked for elsewhere
        "I'm guessing we would be asked for {x} in a different city.",
        # hypothetical: a possibility for the speaker once the rules change
        "Possibly we will get {x} when the city changes the rules.",
        # hypothetical: assuming a condition, what the speaker expects next time
        "Assuming the paperwork is right, we should get through {x} in about a week next time.",
        # hypothetical: a conditional about a rule change, for the speaker
        "Should the rules change, we could be looking at {x} within a year.",
        # hypothetical: a possibility for some day; no wish stated
        "Perhaps {x} is something we could look at some day.",
        # hypothetical: a guess about the road ahead for the speaker
        "Down the road, we might end up dealing with {x}.",
        # hypothetical: a "what if" question about the speaker, nothing asserted as having happened
        "What if we were asked for {x} next spring?",
    ),
    "other_person": (
        # other_person: it happened to the neighbour; nothing about the speaker
        "My neighbour had {x} last spring.",
        # other_person: it happened to a friend
        "A friend of mine went through {x} a couple of years ago.",
        # other_person: it happened to the cousin
        "My cousin dealt with {x} when she opened her own truck.",
        # other_person: another owner went through it, reported second hand
        "Another owner told me she had to go through {x}.",
        # other_person: it happened to the brother
        "My brother had {x} sorted in a week.",
        # other_person: it happened to the truck down the street
        "The truck down the street from ours had to go through {x}.",
        # other_person: it happened to a woman at the market
        "A woman I met at the market had to deal with {x}.",
        # other_person: it happened to the sister-in-law
        "My sister-in-law went through {x} in her first month.",
        # other_person: it happened to the partner's sister
        "My business partner's sister dealt with {x} last year.",
        # other_person: it happened to a man the speaker knows
        "A man I know from the market had {x} the week he opened.",
        # other_person: it happened to someone else
        "Someone else I know got {x} the week they opened.",
        # other_person: it happened to the mother
        "My mother ran a stall for years and she went through {x} twice.",
        # other_person: it happened to the uncle, reported second hand
        "My uncle told me he had to deal with {x} every spring.",
    ),
    "actual": (
        # actual: they got through it
        "We finally got through {x} on the third try.",
        # actual: they spent time on it
        "I spent most of a week on {x}.",
        # actual: they went through it
        "We went through {x} in the first month.",
        # actual: it took them three weeks
        "{x} took us about three weeks, start to finish.",
        # actual: carries a cue word on purpose ("did not"), but they went through it
        "We did not enjoy {x}, but we went through it.",
        # actual: they sat through it twice
        "I had to sit through {x} twice before they were happy.",
        # actual: an attempt that was refused, then got on the second try: they say they got it
        "We tried {x}, it was refused the first time, and we got it the second time.",
        # actual: carries a cue word on purpose ("might"), but they went through all of it
        "Even though {x} might sound easy, we went through all of it.",
        # actual: they went through it like everyone else
        "We went through {x} like everyone else; nobody gave us a shortcut.",
        # actual: it happened in March
        "{x} happened in March, right after we got the truck.",
        # actual: they got through it
        "I had never done anything like {x} before, and we got through it.",
        # actual: an attempt they made and got it after six weeks
        "We applied for {x} and got it after six weeks.",
        # actual: they waited for hours because of it
        "I remember waiting around for hours because of {x}.",
        # actual: it was finally done
        "It was a relief once {x} was finally done.",
    ),
}

# Interviewer questions for the "No." items of the bench. A bare "No." to one of these denies the EVENT (it did not
# happen, or did not exist, for them), which is what the label "negated" means. (The corpus builder's own
# interviewer-voiced questions ask about the order, "So {x} came next, right?", and a "No." to them only denies the
# sequence; they are kept for the no-reply items, where the right label is "unsupported".)
IV_NO_QUESTIONS: tuple[str, ...] = (
    "Did you have {x}?",
    "Was {x} part of it for you?",
    "Did {x} happen for you?",
)

CLASSES = tuple(BENCH_TEMPLATES)
PER_TEMPLATE = 3          # concepts each template is filled with, per corpus


def template_id(label: str, k: int) -> str:
    return f"bank:{label}:{k:02d}"


def render(template: str, wording: str) -> str:
    """The template with ``{x}`` replaced by the concept wording, first letter upper-case."""
    s = template.replace("{x}", wording)
    return s[0].upper() + s[1:]


def template_text(tid: str) -> str | None:
    """The template text for a ``bank:<label>:<k>`` id, else None."""
    parts = tid.split(":")
    if len(parts) == 3 and parts[0] == "bank" and parts[1] in BENCH_TEMPLATES:
        try:
            return BENCH_TEMPLATES[parts[1]][int(parts[2])]
        except (ValueError, IndexError):
            return None
    return None
