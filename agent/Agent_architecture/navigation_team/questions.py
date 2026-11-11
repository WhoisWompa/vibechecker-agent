"""Phrasing the questions the navigation team asks memory.

Memory holds questions about a *kind* of situation ("what should be done about
a big group booked in this cafe?"), and the memory agent calls a candidate
"same" only when it asks the same thing under the same conditions. A question
that carries today's particulars ("big group of 12 at 3pm") is conditioned
differently and misses. So every question leaves here in memory's own form:

- about the kind of event, not this instance: no counts, clock times or dates
- one relationship per question, no "and ... and"
- plain words, starts with a question word, ends with "?"

``note_question`` turns an owner's note into that form by recognising the kind
of note; anything it does not recognise gets the general shape with the
particulars stripped. ``is_well_formed`` is the check the tests apply to every
question the team banks.
"""

from __future__ import annotations

import re
from typing import Tuple

# (pattern over the note's words, the question memory would hold)
KINDS = [
    (r"\b(group|party|booking|booked|reservation|reserved|table for|birthday|celebration)\b",
     "what should be done about a big group booked in this cafe?"),
    (r"\b(rain|rains|rainy|shower|showers|thunder|storm|wet)\b",
     "what do people want from this room when it rains?"),
    (r"\b(hot|heat|heatwave|scorching|humid)\b",
     "what works in this room on a very hot day?"),
    (r"\b(clos(e|es|ing) early|last order|early close|half day)\b",
     "what should be done when the cafe closes early?"),
    (r"\b(open(s|ing)? (late|early)|late open)\b",
     "what should be done when the cafe opens at a different hour?"),
    (r"\b(live music|performance|gig|band|talk|workshop|launch|event|screening|quiz)\b",
     "what should be done when there is an event in this cafe?"),
    (r"\b(deliver(y|ies)|maintenance|repair|servicing|cleaning|contractor|aircon service|renovation)\b",
     "what should be done during maintenance or a delivery in this cafe?"),
    (r"\b(kids|children|school|students|exam)\b",
     "what should be done when children or students fill this cafe?"),
    (r"\b(quiet\w*|slow|empty|few guests|low turnout)\b",
     "what settings suit this room on a quiet day?"),
    (r"\b(promo|promotion|discount|sale|happy hour|offer)\b",
     "what should be done on a promotion day in this cafe?"),
]

_TIME = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b")
_NUMBER = re.compile(r"\b\d+(\.\d+)?\b")
_WINDOW_WORDS = re.compile(r"\b(at|from|until|till|to|after|before|around|between)\s*$", re.I)
_LEVER = re.compile(r"\b(light|temperature|temp|volume|fan)\s*=\s*\S+", re.I)
QUESTION_WORDS = ("what", "when", "how", "does", "do", "why", "which", "is", "are", "should", "can")


def strip_particulars(text: str) -> str:
    """Drop the instance details a memory question must not carry: times, dates, counts, lever=value pairs."""
    s = str(text)
    s = _LEVER.sub("", s)
    s = _TIME.sub("", s)
    s = _DATE.sub("", s)
    s = re.sub(r"\b(of|for|with)\s+\d+\b", "", s)  # "group of 12" -> "group"
    s = _NUMBER.sub("", s)
    s = re.sub(r"\s*[-–]\s*(?=[,.;]|$)", "", s)  # a dangling dash left by "3pm-5pm"
    s = _WINDOW_WORDS.sub("", s.strip())
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.;:-")
    return s


def note_question(text: str) -> str:
    """An owner's note (or message) -> the question memory would hold about that kind of event."""
    low = str(text).lower()
    if low.startswith("override"):
        low = low[len("override"):]
    for pattern, question in KINDS:
        if re.search(pattern, low):
            return question
    general = strip_particulars(low)
    if not general:
        return "what should be done about an unusual day in this cafe?"
    return f"what should be done in this cafe when the owner says {general}?"


def is_well_formed(question: str) -> Tuple[bool, str]:
    """Is this a question memory can be asked? Returns (ok, why not)."""
    q = str(question or "").strip()
    if not q.endswith("?"):
        return False, "does not end with a question mark"
    words = q[:-1].split()
    if not words or words[0].lower() not in QUESTION_WORDS:
        return False, "does not start with a question word"
    if not 4 <= len(words) <= 28:
        return False, f"{len(words)} words; wants 4 to 28"
    if _NUMBER.search(q):
        return False, "carries a number (a count, time or date belongs to the situation, not the question)"
    if ":" in q:
        return False, "carries a colon (an instance pasted into a template)"
    if len(re.findall(r"\band\b", q.lower())) > 1:
        return False, "asks more than one thing"
    return True, ""


def shape(question: str) -> str:
    """Tidy a question written by the model or a template: trimmed, one space, ends with '?'."""
    q = re.sub(r"\s+", " ", str(question or "")).strip().rstrip(".!")
    if q and not q.endswith("?"):
        q += "?"
    if q and q.split()[0].lower() in QUESTION_WORDS:  # memory's questions start lower-case
        q = q[:1].lower() + q[1:]
    return q
