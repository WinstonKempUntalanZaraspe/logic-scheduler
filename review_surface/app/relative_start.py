from __future__ import annotations

"""Relative/immediate start-time semantics for natural Quick Dump language.

Submission time is the reference clock.  "Right now" on a prospective action gets a tiny
lead so the generated preview is still physically usable; an activity explicitly reported
as already underway remains anchored to the actual submission timestamp.

Explicit relative wording always wins over the default immediate lead.
"""

import re
from datetime import datetime, timedelta

_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
    "a": 1, "an": 1, "half": .5,
}
_NUMBER = r"\d+(?:\.\d+)?|(?:" + "|".join(sorted((re.escape(x) for x in _NUMBERS), key=len, reverse=True)) + r")"
_UNIT = r"hours?|hrs?|hr|h|minutes?|mins?|min|m"
_RELATIVE_PATTERNS = (
    re.compile(
        rf"\b(?:in|after)\s+(?:about\s+|around\s+|roughly\s+|approximately\s+)?"
        rf"(?P<n>{_NUMBER})\s*(?P<u>{_UNIT})\b",
        re.I,
    ),
    re.compile(
        rf"\b(?P<n>{_NUMBER})\s*(?P<u>{_UNIT})\s+(?:from\s+now|later)\b",
        re.I,
    ),
    re.compile(
        rf"\b(?:give\s+me|wait)\s+(?:about\s+|around\s+)?(?P<n>{_NUMBER})\s*(?P<u>{_UNIT})\b",
        re.I,
    ),
)
_IMMEDIATE = re.compile(
    r"\b(?:right\s+now|now|straight\s+away|immediately|asap|as\s+soon\s+as\s+possible)\b",
    re.I,
)
_ALREADY_UNDERWAY = re.compile(
    r"^(?:(?:right\s+now|currently|at\s+the\s+moment)\s*,?\s*)?"
    r"(?:(?:i|we)(?:'m|'re|\s+am|\s+are)\s+)"
    r"(?:(?:already|currently|still)\s+)?"
    r"(?:[a-z]+ing\b|at\b|in\b|on\b)",
    re.I,
)
_FUTURE_PREFIX = re.compile(
    r"^\s*(?:(?:i|we)\s+(?:will|'ll|want\s+to|wanna|need\s+to|have\s+to|gotta|"
    r"plan\s+to|intend\s+to|aim\s+to|should)|"
    r"i(?:'m|\s+am)\s+(?:going\s+to|gonna|about\s+to|planning\s+to|trying\s+to)|"
    r"we(?:'re|\s+are)\s+(?:going\s+to|gonna|about\s+to|planning\s+to)|"
    r"i(?:'d|\s+would)\s+(?:like|love|prefer)\s+to|"
    r"we(?:'d|\s+would)\s+(?:like|love|prefer)\s+to|"
    r"(?:let\s+me|please))\b",
    re.I,
)
_BARE_IMPERATIVE = re.compile(
    r"^\s*(?:eat|have|do|study|revise|review|read|work|finish|start|continue|resume|"
    r"swim|run|walk|jog|cycle|go|head|visit|meet|grab|fetch|collect|buy|shop|cook|"
    r"write|code|debug|test|clean|pack|pray|rest|nap|shower|gym|work\s*out)\b",
    re.I,
)


def _number(value: str) -> float | None:
    raw = str(value or "").casefold().strip()
    if raw in _NUMBERS:
        return float(_NUMBERS[raw])
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def relative_start_minutes(text: str) -> tuple[int | None, str | None]:
    """Return explicit relative delay from the submission clock, when present."""
    # Temporal Engine owns the normalized interpretation. Keep the mature patterns
    # below as a compatibility fallback while callers migrate.
    try:
        from .temporal_engine import parse_temporal
        from .config import settings
        stamp = datetime.now(settings.tz)
        doc = parse_temporal(text, stamp)
        rows = [
            x for x in doc.constraints
            if x.kind == "submission_offset" and x.offset_minutes is not None
            and not x.negated and not x.hypothetical
        ]
        if rows:
            rows.sort(key=lambda x: x.evidence.start)
            minutes = int(rows[0].offset_minutes)
            if 0 <= minutes <= 1440:
                return minutes, "explicit-relative"
    except Exception:
        pass
    value = str(text or "").replace("’", "'")
    for pattern in _RELATIVE_PATTERNS:
        matches = [m for m in pattern.finditer(value)
                   if not re.search(r"\bfor\s*$", value[:m.start()], re.I)]
        if not matches:
            continue
        match = matches[0]
        n = _number(match.group("n"))
        if n is None:
            continue
        unit = match.group("u").casefold()
        minutes = round(n * (60 if unit.startswith("h") else 1))
        if 0 <= minutes <= 1440:
            return minutes, "explicit-relative"
    return None, None


def is_already_underway(text: str) -> bool:
    """True for a factual present-state report, not a near-future intention."""
    value = str(text or "").replace("’", "'").strip()
    # "I'm going to eat" is future despite the -ing form "going".
    if re.match(r"^(?:i|we)(?:'m|'re|\s+am|\s+are)\s+(?:going\s+to|gonna|about\s+to)\b", value, re.I):
        return False
    return bool(_ALREADY_UNDERWAY.search(value))


def prospective_immediate(text: str) -> bool:
    value = str(text or "").replace("’", "'").strip()
    if not _IMMEDIATE.search(value) or is_already_underway(value):
        return False
    return bool(_FUTURE_PREFIX.search(value) or _BARE_IMPERATIVE.search(value))


def start_delay_minutes(text: str, default_immediate_minutes: int = 2) -> tuple[int | None, str | None]:
    """Explicit relative delay wins; otherwise prospective 'now' gets a 2m default."""
    explicit, source = relative_start_minutes(text)
    if explicit is not None:
        return explicit, source
    if prospective_immediate(text):
        return max(0, int(default_immediate_minutes)), "immediate-lead"
    return None, None


def start_at(text: str, submitted_at: datetime, default_immediate_minutes: int = 2):
    minutes, source = start_delay_minutes(text, default_immediate_minutes)
    return (
        submitted_at + timedelta(minutes=minutes) if minutes is not None else None,
        source,
        minutes,
    )


def strip_start_language(text: str) -> str:
    """Remove start-timing modifiers from a task title, not from source evidence."""
    value = strip_relative_start(text)
    value = re.sub(
        r"\b(?:right\s+now|straight\s+away|immediately|asap|as\s+soon\s+as\s+possible)\b",
        "",
        value,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", value).strip(" ,.;:-")


def strip_relative_start(text: str) -> str:
    value = str(text or "")
    for pattern in _RELATIVE_PATTERNS:
        original = value
        value = pattern.sub(lambda m: m.group(0) if re.search(r"\bfor\s*$", original[:m.start()], re.I) else "", value)
    return re.sub(r"\s+", " ", value).strip(" ,.;:-")


__all__ = [
    "is_already_underway", "prospective_immediate", "relative_start_minutes",
    "start_at", "start_delay_minutes", "strip_relative_start", "strip_start_language",
]
