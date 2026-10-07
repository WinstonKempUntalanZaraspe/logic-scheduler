from __future__ import annotations

"""Small language-normalization refinements for schedule_instruction_patch.

Kept separate so the core classifier remains readable. These wrappers widen natural
phrasing without changing task creation semantics.
"""

import re
from datetime import datetime, timedelta

from . import schedule_instruction_patch as _sip


_ORIG_SCHEDULE_GRAMMAR = _sip._schedule_grammar
_ORIG_INSTRUCTION_KIND = _sip._instruction_kind
_ORIG_EXTRACT_TARGET = _sip._extract_target
_ORIG_APPLY_TIME_PREFERENCE = _sip._apply_time_preference


def _natural_norm(value: str | None) -> str:
    # _sip._norm intentionally strips punctuation, so contractions such as
    # "don't" become "don t". Collapse the common scheduling negations back to
    # one token before intent grammar runs.
    text = _sip._norm(value)
    text = re.sub(r"\bdon\s+t\b", "dont", text)
    text = re.sub(r"\bcan\s+t\b", "cant", text)
    text = re.sub(r"\bshouldn\s+t\b", "shouldnt", text)
    text = re.sub(r"\bwon\s+t\b", "wont", text)
    return text


def _clean_target(value: str) -> str:
    target = re.sub(r"\b(?:all|the|my|any|existing|current)\b", " ", value)
    target = re.sub(r"\brelated\s+tasks?\b|\btasks?\b", " ", target)
    return re.sub(r"\s+", " ", target).strip()


def schedule_grammar_natural(clause: str) -> bool:
    if _ORIG_SCHEDULE_GRAMMAR(clause):
        return True
    low = _natural_norm(clause)
    # Natural negative instructions are still scheduler commands even when the
    # user writes them conversationally: "don't schedule coding tonight".
    if re.search(r"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\b", low):
        return True
    return False


def instruction_kind_natural(clause: str) -> str:
    low = _natural_norm(clause)
    if re.search(r"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\b", low):
        return "exclude"
    return _ORIG_INSTRUCTION_KIND(clause)


def extract_target_natural(clause: str) -> str:
    target = _ORIG_EXTRACT_TARGET(clause)
    if target:
        return target

    low = _natural_norm(clause)
    temporal = r"(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"

    # Natural negative forms often omit the preposition:
    #   "don't schedule coding tonight"
    #   "don't move maths tomorrow"
    m = re.search(
        rf"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\s+(?:any\s+)?(.+?)\s+{temporal}\b",
        low,
    )
    if m:
        return _clean_target(m.group(1))

    # Also accept "X tasks to tomorrow" when the movement verb was implied earlier
    # in a conversational clause, but only when the noun 'task(s)' makes scheduler
    # semantics explicit.
    m = re.search(rf"\b(?:all\s+)?(.+?)\s+tasks?\s+(?:to|for)\s+{temporal}\b", low)
    if m and re.search(r"\b(?:move|shift|push|reschedule|postpone|defer|make)\b", low):
        return _clean_target(m.group(1))
    return ""


def apply_time_preference_natural(clause: str, start: datetime, end: datetime, patch: dict, now: datetime) -> None:
    _ORIG_APPLY_TIME_PREFERENCE(clause, start, end, patch, now)
    low = _natural_norm(clause)

    # "early morning", "early in the morning", and "first thing in the morning"
    # all mean the first ~3 hours of the awake window, not the entire period to noon.
    early_morning = bool(
        re.search(r"\bearly(?:\s+in)?(?:\s+the)?\s+morning\b", low)
        or re.search(r"\bfirst\s+thing(?:\s+in)?(?:\s+the)?\s+morning\b", low)
    )
    if not early_morning:
        return

    preferred_end = min(end, start + timedelta(hours=3))
    patch["preferred_window_start"] = start.strftime("%H:%M")
    patch["preferred_window_end"] = preferred_end.strftime("%H:%M")
    patch["timing"] = "asap"


_sip._schedule_grammar = schedule_grammar_natural
_sip._instruction_kind = instruction_kind_natural
_sip._extract_target = extract_target_natural
_sip._apply_time_preference = apply_time_preference_natural

__all__ = [
    "schedule_grammar_natural",
    "instruction_kind_natural",
    "extract_target_natural",
    "apply_time_preference_natural",
]
