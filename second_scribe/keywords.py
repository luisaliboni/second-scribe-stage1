"""No-AI modality arm: a deterministic regex cue classifier.

``keyword_modality`` labels a quote with one of ``MODALITIES`` using whole-word cues only
(no model, no learning). It exists to show how much of the headline numbers survives without
an AI judge: ``metrics.headline(..., modality_source="keyword")`` uses it in place of the
judge's labels.

Cues (case-insensitive, whole words; text is normalised first, so curly apostrophes work):

  other_person  my (cousin|friend|neighbo(u)r|brother|sister|mother|father|wife|husband|partner)(s),
                someone else, another (family|parent|owner)
  hypothetical  if (but not "if only"), would have, could have, might, maybe, suppose
  negated       never, didn't, did not, don't, doesn't, can't, cannot, couldn't, won't, wasn't, weren't,
                isn't, aren't, haven't, hasn't, no one, nobody, nothing, none, no, not, without
  wished        wish(es/ed), hope(d), hoping, should have, should be, would like, would be nice, it'd be
                nice, wanted there to be, I'd love, if only

Priority, first match wins:  other_person > hypothetical > negated > wished > actual.
Departures from a plain top-to-bottom reading:
  * a wished cue together with a negation cue returns wished ("there should have been a person, but
    there wasn't" is a wish, not a negated experience) ...
  * ... unless the wish itself is negated: a negation word directly before a wish, want or hope verb
    ("didn't wish", "never wanted", "did not want", "don't want", "wouldn't want", "never hoped", "not hoping"),
    or "no hope(s)" / "no wish(es)" ("there was no hope"), returns negated. This is decided per cue and per
    clause (the quote is split on . ; ! ? and "but"): a wish cue outside any denied wish, in any clause, makes
    the quote wished ("Not hoping is hard, I hope they call." is wished);
  * "if only" is a wished cue, so its "if" does not count as a hypothetical cue.
Everything else is "actual"; a missing or blank quote is "unsupported".
A wished cue next to a hypothetical cue ("I wish they might call") is therefore hypothetical:
the classifier is deliberately crude, and that is the point of a no-AI arm.
"""
from __future__ import annotations

import re

from .textnorm import normalize

_FAMILY = r"(?:cousin|friend|neighbou?r|brother|sister|mother|father|wife|husband|partner)s?"

_OTHER_PERSON = re.compile(rf"\b(?:my {_FAMILY}|someone else|another (?:family|parent|owner))\b")
_HYPOTHETICAL = re.compile(r"\b(?:if(?! only\b)|would have|could have|might|maybe|suppose)\b")
_NEGATED = re.compile(r"\b(?:never|didn't|did not|don't|doesn't|can't|cannot|couldn't|won't|wasn't|weren't|isn't"
                      r"|aren't|haven't|hasn't|no one|nobody|nothing|none|no|not|without)\b")
_WISHED = re.compile(r"\b(?:wish(?:es|ed)?|hope[sd]?|hoping|should have|should be|would like|would be nice|it'd be nice"
                     r"|wanted there to be|i'd love|if only)\b")
# a negation word right before a wish / want / hope verb: the wish itself is denied
_NEGATED_WISH = re.compile(
    r"\b(?:(?:never|didn't|did not|don't|do not|doesn't|does not|wouldn't|would not|not|no longer)\s+"
    r"(?:really\s+|ever\s+)?(?:wish(?:es|ed)?|want(?:s|ed)?|hope[sd]?|hoping)"
    r"|no\s+(?:hopes?|wish(?:es)?))\b")
_CLAUSE = re.compile(r"[.;!?]|\bbut\b")


def keyword_modality(quote: str | None) -> str:
    """One of ``schemas.MODALITIES`` for ``quote`` (see the module docstring for the rules)."""
    if quote is None or not quote.strip():
        return "unsupported"
    t = normalize(quote)
    if _OTHER_PERSON.search(t):
        return "other_person"
    if _HYPOTHETICAL.search(t):
        return "hypothetical"
    denied = False
    for clause in _CLAUSE.split(t):
        spans = [m.span() for m in _NEGATED_WISH.finditer(clause)]
        denied |= bool(spans)
        # a wish cue that does not sit inside a denied wish ("not hoping", "no hope") stands
        if any(not any(a <= m.start() < b for a, b in spans) for m in _WISHED.finditer(clause)):
            return "wished"
    if denied or _NEGATED.search(t):
        return "negated"
    return "actual"
