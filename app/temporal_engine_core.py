from __future__ import annotations

"""Universal temporal-language normalization for AutoScheduler.

The scheduler should never reason directly from English clock/date prose. This module
normalizes human temporal language into a small, provenance-preserving intermediate
representation (Temporal IR). Deterministic parsing owns concrete dates, clocks,
durations and recurrence. Semantic fallback may later add relationship/uncertainty
structure, but it must not invent concrete time values unsupported by source text.

Compatibility helpers at the bottom deliberately mirror the old quickdump helpers so
the existing scheduler can migrate incrementally without one giant rewrite.
"""

import calendar
import re
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import settings


# ---------------------------------------------------------------------------
# IR
# ---------------------------------------------------------------------------

Certainty = Literal["exact", "derived", "approximate", "ambiguous"]
ConstraintKind = Literal[
    "date", "point", "interval", "window", "deadline", "not_before", "not_after",
    "relative", "submission_offset", "daypart", "recurrence",
]
RelationKind = Literal["at", "after", "before", "until", "by", "between", "from_to"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    certainty: Certainty = "exact"


class DateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date | None = None
    label: str
    evidence: Evidence
    precision: Literal["day", "range"] = "day"


class ClockRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minutes: int = Field(ge=0, le=1439)
    text: str
    certainty: Certainty = "exact"
    tolerance_minutes: int = Field(default=0, ge=0, le=180)


class RecurrenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency: Literal["daily", "weekly", "monthly", "yearly"]
    interval: int = Field(default=1, ge=1, le=365)
    by_weekday: list[str] = Field(default_factory=list)
    by_month_day: list[int] = Field(default_factory=list)
    by_set_pos: int | None = Field(default=None, ge=-5, le=5)
    until: date | None = None
    count: int | None = Field(default=None, ge=1, le=10000)
    rrule: str
    evidence: Evidence


class TemporalConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ConstraintKind
    relation: RelationKind | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    earliest_at: datetime | None = None
    latest_at: datetime | None = None
    date_start: date | None = None
    date_end: date | None = None
    anchor: str | None = None
    offset_minutes: int | None = Field(default=None, ge=-10080, le=10080)
    clock: ClockRef | None = None
    optional: bool = False
    negated: bool = False
    hypothetical: bool = False
    evidence: Evidence


class TemporalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_time: datetime
    timezone: str
    dates: list[DateRef] = Field(default_factory=list)
    constraints: list[TemporalConstraint] = Field(default_factory=list)
    recurrence: list[RecurrenceSpec] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "high"

    def concrete_intervals(self) -> list[TemporalConstraint]:
        return [x for x in self.constraints if x.kind in {"interval", "window"} and x.start_at and x.end_at]

    def deadlines(self) -> list[TemporalConstraint]:
        return [x for x in self.constraints if x.kind in {"deadline", "not_after"} and x.latest_at]


# ---------------------------------------------------------------------------
# Lexical data
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY_CODE = {0: "MO", 1: "TU", 2: "WE", 3: "TH", 4: "FR", 5: "SA", 6: "SU"}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_NUMBERS = {
    "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "ninety": 90, "half": 0.5,
}
_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
}

_DAYPARTS = {
    "morning": (6 * 60, 12 * 60),
    "afternoon": (12 * 60, 17 * 60),
    "evening": (17 * 60, 22 * 60),
    "night": (20 * 60, 24 * 60),
    "tonight": (18 * 60, 24 * 60),
}

_APPROX = re.compile(r"\b(?:around|about|roughly|approximately|approx\.?|ish|or\s+so)\b", re.I)
_OPTIONAL = re.compile(
    r"\b(?:maybe|perhaps|possibly|might|may|could|optional(?:ly)?|if\s+(?:i\s+)?(?:have\s+)?time(?:\s+allows?)?|if\s+possible)\b",
    re.I,
)
_NEGATIVE = re.compile(r"\b(?:do\s+not|don't|dont|never|won't|will\s+not|cannot|can't|cant|not\s+going\s+to)\b", re.I)
_HYPOTHETICAL = re.compile(r"\b(?:what\s+if|suppose|imagine|hypothetically|if\s+i\s+(?:were|had|did|could))\b", re.I)

_TEMPORAL_CUE = re.compile(
    r"\b(?:today|tonight|tomorrow|tmr|yesterday|weekday|weekend|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|morning|afternoon|evening|night|noon|midnight|"
    r"before|after|until|by|later|earlier|from\s+now|every|each|daily|weekly|monthly|yearly|"
    r"whenever|sometime|anytime|around|about|next\s+week|this\s+week|end\s+of\s+(?:the\s+)?month)\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.I,
)

_WORD_NUMBER = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|ninety|a|an|half)"
_NUM = rf"(?:\d+(?:\.\d+)?|{_WORD_NUMBER})"
_DURATION_UNIT = r"(?:seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|weeks?)"

_CLOCK_TOKEN = (
    r"(?:"
    r"midnight|noon|"
    r"(?:quarter|half)\s+(?:past|to)\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s+(?:a\.?m\.?|p\.?m\.?))?|"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|"
    r"\d{3,4}"
    r")(?:\s*-?ish)?"
)

_RANGE_RE = re.compile(
    rf"(?P<prefix>\b(?:from|between|anytime\s+between|sometime\s+between)?\s*)"
    rf"(?P<a>{_CLOCK_TOKEN})\s*(?P<sep>-|–|—|\bto\b|\btill\b|\buntil\b|\band\b)\s*(?P<b>{_CLOCK_TOKEN})",
    re.I,
)

_WEEKDAY_PATTERN = "|".join(sorted((re.escape(x) for x in _WEEKDAYS), key=len, reverse=True))
_MONTH_PATTERN = "|".join(sorted((re.escape(x) for x in _MONTHS), key=len, reverse=True))


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("’", "'").replace("–", "-").replace("—", "-")).strip()


def _evidence(text: str, start: int, end: int, certainty: Certainty = "exact") -> Evidence:
    return Evidence(source=text[start:end], start=max(0, start), end=max(start, end), certainty=certainty)


def _number(value: str | None) -> float | None:
    raw = str(value or "").casefold().strip().replace("-", " ")
    if not raw:
        return None
    if raw in _NUMBERS:
        return float(_NUMBERS[raw])
    try:
        return float(raw)
    except ValueError:
        pass
    parts = raw.split()
    if all(p in _NUMBERS for p in parts):
        vals = [_NUMBERS[p] for p in parts]
        # "twenty five", "thirty two".
        if len(vals) == 2 and vals[0] >= 20 and vals[1] < 10:
            return float(vals[0] + vals[1])
        return float(sum(vals))
    return None


def duration_minutes(value: str | None) -> int | None:
    """Parse one duration phrase without stealing submission-clock offsets."""
    text = _normalise(value).lower()
    total = 0.0
    seen = False
    # 1h15 / 1h 15m / 1 hour 15 minutes
    compound = re.search(
        r"\b(?P<h>\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)"
        r"(?:\s*(?:and\s+)?)?(?P<m>\d{1,2})?\s*(?:minutes?|mins?|min|m)?\b",
        text,
        re.I,
    )
    if compound:
        h = float(compound.group("h"))
        m = int(compound.group("m") or 0)
        total += h * 60 + m
        seen = True
        # Avoid double-counting the same phrase in the generic pass.
        text = text[:compound.start()] + " " + text[compound.end():]

    pattern = re.compile(rf"\b(?P<n>{_NUM})\s*(?P<u>{_DURATION_UNIT})\b", re.I)
    for match in pattern.finditer(text):
        n = _number(match.group("n"))
        if n is None:
            continue
        unit = match.group("u").lower()
        if unit.startswith(("sec", "s")):
            minutes = n / 60
        elif unit.startswith(("hour", "hr", "h")):
            minutes = n * 60
        elif unit.startswith("day"):
            minutes = n * 1440
        elif unit.startswith("week"):
            minutes = n * 10080
        else:
            minutes = n
        total += minutes
        seen = True
    if not seen:
        return None
    return max(1, int(round(total)))


def _sentence_window(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start), text.rfind("\n", 0, start))
    right_candidates = [x for x in (text.find(".", end), text.find(";", end), text.find("\n", end)) if x >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1:right]


def _modality(text: str, start: int, end: int) -> tuple[bool, bool, bool]:
    clause = _sentence_window(text, start, end)
    return bool(_OPTIONAL.search(clause)), bool(_NEGATIVE.search(clause)), bool(_HYPOTHETICAL.search(clause))


# ---------------------------------------------------------------------------
# Clock parsing
# ---------------------------------------------------------------------------

def clock_to_minutes(raw: str | None, daypart_hint: str | None = None) -> int | None:
    if raw is None:
        return None
    original = _normalise(raw)
    value = original.casefold().replace(".", "")
    value = re.sub(r"\s*-?ish\s*$", "", value)
    value = _APPROX.sub("", value).strip()

    if value == "noon":
        return 12 * 60
    if value == "midnight":
        return 0

    phrase = re.fullmatch(
        r"(?P<f>quarter|half)\s+(?P<dir>past|to)\s+"
        r"(?P<h>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)",
        value,
        re.I,
    )
    if phrase:
        hour = int(_NUMBERS[phrase.group("h")])
        amount = 15 if phrase.group("f") == "quarter" else 30
        if phrase.group("dir") == "past":
            minute = hour * 60 + amount
        else:
            minute = ((hour - 1) % 12) * 60 + (60 - amount)
        hint = str(daypart_hint or "").lower()
        if hint in {"afternoon", "evening", "night", "tonight"} and minute < 12 * 60:
            minute += 12 * 60
        return minute % 1440

    word_clock = re.fullmatch(
        r"(?P<h>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"(?:\s*(?P<ap>am|pm))?",
        value,
        re.I,
    )
    if word_clock:
        hour = int(_NUMBERS[word_clock.group("h")])
        ap = word_clock.group("ap")
        if ap:
            hour %= 12
            if ap == "pm":
                hour += 12
        elif str(daypart_hint or "").lower() in {"afternoon", "evening", "night", "tonight"}:
            hour = (hour % 12) + 12
        return hour * 60

    military = re.fullmatch(r"(?P<t>\d{3,4})", value)
    if military:
        digits = military.group("t")
        h, m = int(digits[:-2]), int(digits[-2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
        return None

    match = re.fullmatch(r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?", value)
    if not match:
        return None
    hour, minute, ap = int(match.group("h")), int(match.group("m") or 0), match.group("ap")
    if minute > 59:
        return None
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour %= 12
        if ap == "pm":
            hour += 12
    elif hour > 23:
        return None
    else:
        hint = str(daypart_hint or "").lower()
        if hint in {"afternoon", "evening", "night", "tonight"} and 1 <= hour <= 12:
            hour = (hour % 12) + 12
        elif hint == "morning" and hour == 12:
            hour = 0
    return hour * 60 + minute


def clock_string(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock_certainty(raw: str) -> tuple[Certainty, int]:
    if _APPROX.search(raw) or re.search(r"-?ish\b", raw, re.I):
        return "approximate", 30
    return "exact", 0


def _daypart_near(text: str, start: int, end: int) -> str | None:
    around = text[max(0, start - 28):min(len(text), end + 28)].lower()
    for label in _DAYPARTS:
        if re.search(rf"\b{label}\b", around):
            return label
    return None


def find_clock_refs(text: str) -> list[tuple[int, int, ClockRef]]:
    """Find standalone clock expressions without assigning an activity role."""
    source = str(text or "")
    refs: list[tuple[int, int, ClockRef]] = []
    for m in re.finditer(rf"(?<![A-Za-z0-9])(?P<clock>{_CLOCK_TOKEN})(?![A-Za-z0-9])", source, re.I):
        raw = m.group("clock")
        # Avoid interpreting ordinary bare numbers as clocks unless clock syntax or
        # nearby temporal words make that reading plausible.
        cleaned = raw.lower().replace(".", "").strip()

        # Numeric tokens embedded in dates/durations are not clocks. This prevents
        # "Oct 19 at 8pm" from also yielding 19:00 and "10 minutes after lunch"
        # from yielding a phantom 10:00 point.
        before_near = source[max(0, m.start() - 18):m.start()]
        after_near = source[m.end():min(len(source), m.end() + 24)]
        adjacent_date_sep = (
            source[max(0, m.start() - 1):m.start()] in "-/"
            or source[m.end():m.end() + 1] in "-/"
        )
        month_adjacent = bool(
            re.search(rf"(?:{_MONTH_PATTERN})\s+$", before_near, re.I)
            or re.match(rf"^\s+(?:{_MONTH_PATTERN})\b", after_near, re.I)
        )
        duration_follows = bool(re.match(
            r"^\s*(?:seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr|days?|weeks?)\b",
            after_near,
            re.I,
        ))
        if cleaned.isdigit() and (adjacent_date_sep or month_adjacent or duration_follows):
            continue

        explicit_clock = bool(
            re.search(r"(?:am|pm|:|noon|midnight|quarter|half|-?ish)", cleaned)
            or re.fullmatch(r"\d{3,4}", cleaned)
        )
        around = source[max(0, m.start() - 20):min(len(source), m.end() + 20)]
        # A four-digit year inside a date is not military time. Military clocks such
        # as "at 1900" remain valid because they have an explicit nearby clock role.
        if re.fullmatch(r"\d{4}", cleaned) and 1900 <= int(cleaned) <= 2099:
            before = source[max(0, m.start() - 18):m.start()]
            clock_role = bool(re.search(
                r"\b(?:at|by|before|after|from|to|around|about|starts?|begin|meet|leave|depart|arrive|reach)\s*$",
                before,
                re.I,
            ))
            if adjacent_date_sep or not clock_role:
                continue
        temporal_context = bool(re.search(
            r"\b(?:at|by|before|after|until|from|to|between|around|about|morning|afternoon|evening|night|wake|sleep|start|arrive|reach)\b",
            around,
            re.I,
        ))
        if not explicit_clock and not temporal_context:
            continue
        hint = _daypart_near(source, m.start(), m.end())
        minute = clock_to_minutes(raw, hint)
        if minute is None:
            continue
        certainty, tolerance = _clock_certainty(raw)
        refs.append((m.start(), m.end(), ClockRef(
            minutes=minute, text=raw, certainty=certainty, tolerance_minutes=tolerance
        )))
    return refs


def extract_clock(text: str) -> ClockRef | None:
    refs = find_clock_refs(text)
    return refs[-1][2] if refs else None


def clock_near(text: str, keyword_pattern: str, max_distance: int = 42) -> ClockRef | None:
    """Return the closest clock to a role keyword such as wake/sleep/job.

    This binds a normalized clock to a semantic role while keeping clock parsing itself
    centralized. Callers still decide what the role means.
    """
    source = str(text or "")
    keys = list(re.finditer(keyword_pattern, source, re.I))
    clocks = find_clock_refs(source)
    best = None
    best_distance = None
    for key in keys:
        for start, end, clock in clocks:
            distance = min(abs(start - key.end()), abs(key.start() - end))
            if distance <= max_distance and (best_distance is None or distance < best_distance):
                best, best_distance = clock, distance
    return best


def _resolve_range_minutes(a_raw: str, b_raw: str, context: str) -> tuple[int, int] | None:
    hint = _daypart_near(context, 0, len(context))
    a = clock_to_minutes(a_raw, hint)
    b = clock_to_minutes(b_raw, hint)
    if a is None or b is None:
        return None

    a_has_ap = bool(re.search(r"\b(?:am|pm)\b", a_raw.replace(".", ""), re.I))
    b_has_ap = bool(re.search(r"\b(?:am|pm)\b", b_raw.replace(".", ""), re.I))

    # Inherit explicit meridiem to the other side when sensible.
    if a_has_ap and not b_has_ap:
        ap = "pm" if "pm" in a_raw.lower().replace(".", "") else "am"
        inherited = clock_to_minutes(re.sub(r"\s*$", ap, b_raw))
        if inherited is not None:
            b = inherited
    elif b_has_ap and not a_has_ap:
        ap = "pm" if "pm" in b_raw.lower().replace(".", "") else "am"
        inherited = clock_to_minutes(re.sub(r"\s*$", ap, a_raw))
        if inherited is not None:
            a = inherited

    if b <= a:
        # Common shorthand "10-2" / "1-4" means the nearest positive interval.
        if b + 12 * 60 > a and b + 12 * 60 - a <= 12 * 60:
            b += 12 * 60
        else:
            b += 24 * 60
    if b - a > 16 * 60:
        return None
    return a, b


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_weekday(base: date, weekday: int, occurrence: int = 1, include_today: bool = False) -> date:
    delta = (weekday - base.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    first = base + timedelta(days=delta)
    return first + timedelta(days=7 * max(0, occurrence - 1))


def _month_for_phrase(month_text: str, now: datetime, explicit_year: int | None = None) -> tuple[int, int]:
    month = _MONTHS[month_text.lower()]
    year = explicit_year or now.year
    return year, month


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    if ordinal == -1:
        last = calendar.monthrange(year, month)[1]
        d = date(year, month, last)
        return d - timedelta(days=(d.weekday() - weekday) % 7)
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + 7 * (ordinal - 1)
    return _safe_date(year, month, day)


def find_date_refs(text: str, now: datetime) -> list[DateRef]:
    source = str(text or "")
    low = source.lower()
    found: list[tuple[int, DateRef]] = []
    occupied: list[tuple[int, int]] = []

    def add(start: int, end: int, d0: date, label: str, d1: date | None = None, certainty: Certainty = "exact"):
        ev = _evidence(source, start, end, certainty)
        found.append((start, DateRef(
            start_date=d0, end_date=d1, label=label, evidence=ev,
            precision="range" if d1 and d1 != d0 else "day",
        )))
        occupied.append((start, end))

    def free(start: int, end: int) -> bool:
        return not any(start < b and a < end for a, b in occupied)

    # ISO dates.
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", source):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add(m.start(), m.end(), d, m.group(0))

    # Singapore-style numeric dates: DD/MM[/YYYY]. Two-part form rolls forward.
    for m in re.finditer(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)", source):
        if not free(m.start(), m.end()):
            continue
        day_n, month_n = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year = now.year if not year_raw else int(year_raw) + (2000 if len(year_raw) == 2 else 0)
        d = _safe_date(year, month_n, day_n)
        if d and not year_raw and d < now.date():
            d = _safe_date(year + 1, month_n, day_n)
        if d:
            add(m.start(), m.end(), d, m.group(0))
            occupied.append((m.start(), m.end()))

    # Month-name forms.
    month_name = _MONTH_PATTERN
    patterns = [
        re.compile(rf"\b(?P<month>{month_name})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?\b", re.I),
        re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{month_name})(?:\s+(?P<year>20\d{{2}}))?\b", re.I),
    ]
    for pattern in patterns:
        for m in pattern.finditer(source):
            if not free(m.start(), m.end()):
                continue
            year = int(m.group("year") or now.year)
            d = _safe_date(year, _MONTHS[m.group("month").lower()], int(m.group("day")))
            if d and not m.group("year") and d < now.date():
                d = _safe_date(year + 1, d.month, d.day)
            if d:
                add(m.start(), m.end(), d, m.group(0))

    # First/second/... weekday of a month.
    ordinal_re = "|".join(sorted((re.escape(x) for x in _ORDINALS), key=len, reverse=True))
    for m in re.finditer(
        rf"\b(?P<ord>{ordinal_re})\s+(?P<weekday>{_WEEKDAY_PATTERN})\s+of\s+"
        rf"(?:(?P<each>each|every)\s+month|(?P<month>{_MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}))?)\b",
        source,
        re.I,
    ):
        if m.group("each"):
            continue  # recurrence parser owns this.
        year = int(m.group("year") or now.year)
        month = _MONTHS[m.group("month").lower()]
        d = _nth_weekday_of_month(year, month, _WEEKDAYS[m.group("weekday").lower()], _ORDINALS[m.group("ord").lower()])
        if d and not m.group("year") and d < now.date():
            d = _nth_weekday_of_month(year + 1, month, _WEEKDAYS[m.group("weekday").lower()], _ORDINALS[m.group("ord").lower()])
        if d:
            add(m.start(), m.end(), d, m.group(0))

    # Relative fixed phrases, longest first.
    relative_phrases = [
        (r"\bday\s+after\s+tomorrow\b", 2),
        (r"\btomorrow\b|\btmr\b", 1),
        (r"\btoday\b|\btonight\b", 0),
        (r"\byesterday\b", -1),
    ]
    for pattern, delta in relative_phrases:
        for m in re.finditer(pattern, source, re.I):
            if free(m.start(), m.end()):
                add(m.start(), m.end(), now.date() + timedelta(days=delta), m.group(0))

    # In N days/weeks; N days/weeks from now.
    for m in re.finditer(
        rf"\b(?:in\s+)?(?P<n>{_NUM})\s+(?P<u>days?|weeks?)"
        r"(?:\s+from\s+now)?\b",
        source,
        re.I,
    ):
        prefix = source[max(0, m.start() - 4):m.start()].lower()
        if not source[m.start():m.end()].lower().startswith("in ") and "from now" not in m.group(0).lower():
            # Bare "3 days" is usually duration, not a target date.
            continue
        n = _number(m.group("n"))
        if n is None or n < 0:
            continue
        days = int(round(n * (7 if m.group("u").lower().startswith("week") else 1)))
        add(m.start(), m.end(), now.date() + timedelta(days=days), m.group(0))

    # Nth weekday from now: "three Tuesdays from now".
    for m in re.finditer(
        rf"\b(?P<n>{_NUM})\s+(?P<weekday>{_WEEKDAY_PATTERN})s?\s+from\s+now\b",
        source,
        re.I,
    ):
        n = _number(m.group("n"))
        if n is None or n < 1:
            continue
        d = _next_weekday(now.date(), _WEEKDAYS[m.group("weekday").lower()], int(n), include_today=False)
        add(m.start(), m.end(), d, m.group(0))

    # Friday after next.
    for m in re.finditer(rf"\b(?P<weekday>{_WEEKDAY_PATTERN})\s+after\s+next\b", source, re.I):
        d = _next_weekday(now.date(), _WEEKDAYS[m.group("weekday").lower()], 2, include_today=False)
        add(m.start(), m.end(), d, m.group(0))

    # this/next/bare weekday.
    for m in re.finditer(rf"\b(?:(?P<mod>this|next)\s+)?(?P<weekday>{_WEEKDAY_PATTERN})\b", source, re.I):
        if not free(m.start(), m.end()):
            continue
        mod = (m.group("mod") or "").lower()
        wd = _WEEKDAYS[m.group("weekday").lower()]
        if mod == "this":
            d = _next_weekday(now.date(), wd, 1, include_today=True)
        else:
            d = _next_weekday(now.date(), wd, 1, include_today=False)
        add(m.start(), m.end(), d, m.group(0))

    # Week/weekend ranges.
    for m in re.finditer(r"\b(?P<mod>this|next)\s+week(?:end)?\b|\bthis\s+weekend\b|\bnext\s+weekend\b", source, re.I):
        phrase = m.group(0).lower()
        monday = now.date() - timedelta(days=now.weekday())
        if "weekend" in phrase:
            base_week = monday + (timedelta(days=7) if phrase.startswith("next") else timedelta())
            start = base_week + timedelta(days=5)
            if start < now.date() and phrase.startswith("this"):
                start += timedelta(days=7)
            add(m.start(), m.end(), start, m.group(0), start + timedelta(days=1), "approximate")
        else:
            start = monday + (timedelta(days=7) if phrase.startswith("next") else timedelta())
            add(m.start(), m.end(), start, m.group(0), start + timedelta(days=6), "approximate")

    # End of this/the month or end of next month.
    for m in re.finditer(r"\bend\s+of\s+(?:(?P<mod>this|next|the)\s+)?month\b", source, re.I):
        mod = (m.group("mod") or "this").lower()
        year, month = now.year, now.month
        if mod == "next":
            month += 1
            if month == 13:
                month, year = 1, year + 1
        last = calendar.monthrange(year, month)[1]
        d = date(year, month, last)
        if d < now.date():
            month += 1
            if month == 13:
                month, year = 1, year + 1
            d = date(year, month, calendar.monthrange(year, month)[1])
        add(m.start(), m.end(), d, m.group(0), certainty="approximate")

    found.sort(key=lambda item: (item[0], -(item[1].evidence.end - item[1].evidence.start)))
    # Remove exact duplicate spans/dates created by overlapping patterns.
    out, seen = [], set()
    for _, ref in found:
        key = (ref.evidence.start, ref.evidence.end, ref.start_date, ref.end_date)
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def resolve_date_reference(text: str, now: datetime) -> DateRef | None:
    refs = find_date_refs(text, now)
    if not refs:
        return None
    # Prefer the most explicit day precision. If there are several, preserve source order.
    exact_days = [x for x in refs if x.precision == "day"]
    return exact_days[0] if exact_days else refs[0]


def date_near(text: str, now: datetime, keyword_pattern: str, max_distance: int = 100) -> DateRef | None:
    """Bind the nearest explicit date phrase to a semantic role keyword."""
    source = str(text or "")
    keys = list(re.finditer(keyword_pattern, source, re.I))
    refs = [x for x in find_date_refs(source, now) if x.precision == "day"]
    best = None
    best_distance = None
    for key in keys:
        for ref in refs:
            distance = min(
                abs(ref.evidence.start - key.end()),
                abs(key.start() - ref.evidence.end),
            )
            if distance <= max_distance and (best_distance is None or distance < best_distance):
                best, best_distance = ref, distance
    return best


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------

def _until_from_text(text: str, now: datetime) -> date | None:
    match = re.search(r"\buntil\s+(.+?)(?:$|[,;.])", text, re.I)
    if not match:
        return None
    ref = resolve_date_reference(match.group(1), now)
    if ref:
        return ref.end_date or ref.start_date
    # "until December" means through the end of the named month.
    month_match = re.search(rf"\b({_MONTH_PATTERN})\b", match.group(1), re.I)
    if month_match:
        month = _MONTHS[month_match.group(1).lower()]
        year = now.year + (1 if month < now.month else 0)
        return date(year, month, calendar.monthrange(year, month)[1])
    return None


def _bounded_recurrence_until(text: str, now: datetime) -> date | None:
    explicit = _until_from_text(text, now)
    if explicit:
        return explicit
    match = re.search(
        rf"\bfor\s+(?:the\s+)?next\s+(?P<n>{_NUM})\s+(?P<u>days?|weeks?|months?)\b|"
        rf"\bfor\s+(?P<n2>{_NUM})\s+(?P<u2>days?|weeks?|months?)\b",
        text,
        re.I,
    )
    if not match:
        return None
    raw_n = match.group("n") or match.group("n2")
    raw_u = (match.group("u") or match.group("u2") or "").lower()
    n = _number(raw_n)
    if n is None or n <= 0:
        return None
    if raw_u.startswith("day"):
        return now.date() + timedelta(days=int(n))
    if raw_u.startswith("week"):
        return now.date() + timedelta(days=int(n * 7))
    # Month arithmetic without external dependencies.
    month_index = now.year * 12 + (now.month - 1) + int(n)
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _rrule(freq: str, interval: int = 1, byday: list[str] | None = None,
           bymonthday: list[int] | None = None, bysetpos: int | None = None,
           until: date | None = None, count: int | None = None) -> str:
    parts = [f"FREQ={freq}", f"INTERVAL={interval}"]
    if byday:
        tokens = list(byday)
        if bysetpos is not None and len(tokens) == 1:
            tokens = [f"{bysetpos}{tokens[0]}"]
        parts.append("BYDAY=" + ",".join(tokens))
    if bymonthday:
        parts.append("BYMONTHDAY=" + ",".join(str(x) for x in bymonthday))
    if until:
        parts.append("UNTIL=" + until.strftime("%Y%m%dT235959"))
    if count:
        parts.append("COUNT=" + str(count))
    return "RRULE:" + ";".join(parts)


def parse_recurrence(text: str, now: datetime) -> list[RecurrenceSpec]:
    source = str(text or "")
    low = source.lower()
    out: list[RecurrenceSpec] = []

    def add(match: re.Match, frequency: str, interval: int = 1, byday=None,
            bymonthday=None, bysetpos=None, count=None):
        until = _bounded_recurrence_until(source[match.start():], now)
        freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}[frequency]
        out.append(RecurrenceSpec(
            frequency=frequency,
            interval=interval,
            by_weekday=list(byday or []),
            by_month_day=list(bymonthday or []),
            by_set_pos=bysetpos,
            until=until,
            count=count,
            rrule=_rrule(freq, interval, list(byday or []), list(bymonthday or []), bysetpos, until, count),
            evidence=_evidence(source, match.start(), match.end()),
        ))

    # First Sunday of each month / last Friday every month.
    ord_pattern = "|".join(sorted((re.escape(x) for x in _ORDINALS), key=len, reverse=True))
    for m in re.finditer(
        rf"\b(?P<ord>{ord_pattern})\s+(?P<weekday>{_WEEKDAY_PATTERN})\s+"
        r"(?:of\s+)?(?:each|every)\s+month\b",
        source, re.I,
    ):
        wd = _WEEKDAY_CODE[_WEEKDAYS[m.group("weekday").lower()]]
        add(m, "monthly", 1, [wd], bysetpos=_ORDINALS[m.group("ord").lower()])

    # Weekdays, optionally excluding named days.
    for m in re.finditer(r"\b(?:every|each)\s+weekday\b|\bweekdays\b", source, re.I):
        days = ["MO", "TU", "WE", "TH", "FR"]
        tail = source[m.end():m.end() + 80]
        ex = re.search(rf"\bexcept\s+((?:{_WEEKDAY_PATTERN})(?:\s*(?:,|and|&)\s*(?:{_WEEKDAY_PATTERN}))*)", tail, re.I)
        if ex:
            excluded = {_WEEKDAY_CODE[_WEEKDAYS[x.lower()]] for x in re.findall(_WEEKDAY_PATTERN, ex.group(1), re.I)}
            days = [x for x in days if x not in excluded]
        add(m, "weekly", 1, days)

    # Every second Tuesday / every other Tuesday.
    for m in re.finditer(
        rf"\b(?:every\s+(?:(?P<n>second|2nd)|other)\s+|each\s+second\s+)"
        rf"(?P<weekday>{_WEEKDAY_PATTERN})\b",
        source, re.I,
    ):
        add(m, "weekly", 2, [_WEEKDAY_CODE[_WEEKDAYS[m.group("weekday").lower()]]])

    # Every Monday and Thursday.
    for m in re.finditer(
        rf"\b(?:every|each)\s+(?P<days>(?:{_WEEKDAY_PATTERN})(?:\s*(?:,|and|&)\s*(?:{_WEEKDAY_PATTERN}))*)\b",
        source, re.I,
    ):
        if re.search(r"\b(?:second|other)\s+", m.group(0), re.I):
            continue
        names = re.findall(_WEEKDAY_PATTERN, m.group("days"), re.I)
        codes = list(dict.fromkeys(_WEEKDAY_CODE[_WEEKDAYS[x.lower()]] for x in names))
        add(m, "weekly", 1, codes)

    # Every N units.
    for m in re.finditer(rf"\b(?:every|each)\s+(?P<n>{_NUM})\s+(?P<u>days?|weeks?|months?|years?)\b", source, re.I):
        n = _number(m.group("n"))
        if n is None or n < 1:
            continue
        unit = m.group("u").lower()
        frequency = "daily" if unit.startswith("day") else "weekly" if unit.startswith("week") else "monthly" if unit.startswith("month") else "yearly"
        add(m, frequency, int(n))

    simple = [
        (r"\b(?:every|each)\s+day\b|\bdaily\b", "daily"),
        (r"\b(?:every|each)\s+week\b|\bweekly\b", "weekly"),
        (r"\b(?:every|each)\s+month\b|\bmonthly\b", "monthly"),
        (r"\b(?:every|each)\s+year\b|\byearly\b|\bannually\b", "yearly"),
    ]
    for pattern, freq in simple:
        for m in re.finditer(pattern, source, re.I):
            if any(m.start() >= x.evidence.start and m.end() <= x.evidence.end for x in out):
                continue
            add(m, freq)

    # De-duplicate equivalent rules while preserving first source.
    unique, seen = [], set()
    for item in out:
        key = item.rrule
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Constraint extraction
# ---------------------------------------------------------------------------

def _combine(day: date, minute: int) -> datetime:
    return datetime.combine(day, time((minute % 1440) // 60, minute % 60), settings.tz)


def _date_for_span(text: str, now: datetime, start: int, end: int) -> date:
    # Prefer a date phrase in the same sentence, otherwise today.
    sentence = _sentence_window(text, start, end)
    ref = resolve_date_reference(sentence, now)
    return ref.start_date if ref else now.date()


def _range_constraints(text: str, now: datetime) -> list[TemporalConstraint]:
    out = []
    for m in _RANGE_RE.finditer(text):
        resolved = _resolve_range_minutes(m.group("a"), m.group("b"), _sentence_window(text, m.start(), m.end()))
        if not resolved:
            continue
        a, b = resolved
        day = _date_for_span(text, now, m.start(), m.end())
        start = _combine(day, a)
        end_day = day + timedelta(days=1 if b >= 1440 else 0)
        end = _combine(end_day, b % 1440)
        prefix = (m.group("prefix") or "").lower()
        windowish = "between" in prefix or re.search(r"\b(?:anytime|sometime|somewhere)\b", _sentence_window(text, m.start(), m.end()), re.I)
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        certainty: Certainty = "approximate" if _APPROX.search(m.group(0)) else "exact"
        out.append(TemporalConstraint(
            kind="window" if windowish else "interval",
            relation="between" if windowish else "from_to",
            start_at=start,
            end_at=end,
            date_start=start.date(),
            date_end=end.date(),
            optional=optional,
            negated=negated,
            hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end(), certainty),
        ))
    return out


def _daypart_constraints(text: str, now: datetime) -> list[TemporalConstraint]:
    out = []
    for label, (a, b) in _DAYPARTS.items():
        for m in re.finditer(rf"\b(?:this\s+)?{label}\b", text, re.I):
            day = _date_for_span(text, now, m.start(), m.end())
            if label == "tonight":
                day = now.date()
            start = _combine(day, a)
            end = _combine(day + (timedelta(days=1) if b >= 1440 else timedelta()), b % 1440)
            optional, negated, hypothetical = _modality(text, m.start(), m.end())
            out.append(TemporalConstraint(
                kind="daypart", relation="between", start_at=start, end_at=end,
                date_start=day, date_end=end.date(),
                optional=optional, negated=negated, hypothetical=hypothetical,
                evidence=_evidence(text, m.start(), m.end(), "approximate"),
            ))
    return out


def _deadline_constraints(text: str, now: datetime) -> list[TemporalConstraint]:
    out = []
    occupied: list[tuple[int, int]] = []

    def add_deadline(match, target: datetime, relation: str, certainty: Certainty = "exact"):
        optional, negated, hypothetical = _modality(text, match.start(), match.end())
        out.append(TemporalConstraint(
            kind="deadline" if relation == "by" else "not_after",
            relation="by" if relation == "by" else "before",
            latest_at=target,
            date_start=target.date(),
            optional=optional,
            negated=negated,
            hypothetical=hypothetical,
            evidence=_evidence(text, match.start(), match.end(), certainty),
        ))
        occupied.append((match.start(), match.end()))

    # Strongest form first: "by tomorrow at 9pm", "before Friday 17:30".
    combined = re.compile(
        rf"\b(?P<rel>no\s+later\s+than|by|before)\s+"
        rf"(?P<date>today|tonight|tomorrow|tmr|day\s+after\s+tomorrow|"
        rf"(?:this|next)\s+(?:{_WEEKDAY_PATTERN})|(?:{_WEEKDAY_PATTERN})\s+after\s+next|"
        rf"(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d{{2}})?|"
        rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_PATTERN})(?:\s+20\d{{2}})?|"
        rf"\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?|20\d{{2}}-\d{{2}}-\d{{2}})"
        rf"\s*(?:at|@)?\s*(?P<clock>{_CLOCK_TOKEN})\b",
        re.I,
    )
    for m in combined.finditer(text):
        ref = resolve_date_reference(m.group("date"), now)
        hint = _daypart_near(text, m.start(), m.end())
        minute = clock_to_minutes(m.group("clock"), hint)
        if not ref or minute is None:
            continue
        certainty, _ = _clock_certainty(m.group("clock"))
        target = _combine(ref.start_date, minute)
        add_deadline(m, target, m.group("rel").lower(), certainty)
        out[-1].clock = ClockRef(
            minutes=minute, text=m.group("clock"), certainty=certainty,
            tolerance_minutes=_clock_certainty(m.group("clock"))[1],
        )

    # Concrete clock target on the sentence's resolved date.
    pattern = re.compile(
        rf"\b(?P<rel>no\s+later\s+than|by|before)\s+(?P<clock>{_CLOCK_TOKEN})\b",
        re.I,
    )
    for m in pattern.finditer(text):
        if any(m.start() < b and a < m.end() for a, b in occupied):
            continue
        hint = _daypart_near(text, m.start(), m.end())
        minute = clock_to_minutes(m.group("clock"), hint)
        if minute is None:
            continue
        day = _date_for_span(text, now, m.start(), m.end())
        target = _combine(day, minute)
        certainty, tolerance = _clock_certainty(m.group("clock"))
        add_deadline(m, target, m.group("rel").lower(), certainty)
        out[-1].clock = ClockRef(
            minutes=minute, text=m.group("clock"), certainty=certainty,
            tolerance_minutes=tolerance,
        )

    # Direct due/deadline forms retained from the mature Quick Dump grammar:
    # "due tomorrow", "deadline Friday", "due 19 Oct at 5pm".
    due_re = re.compile(
        rf"\b(?P<label>due|deadline(?:\s+is)?)\s+"
        rf"(?P<date>today|tonight|tomorrow|tmr|day\s+after\s+tomorrow|"
        rf"(?:this|next)\s+(?:{_WEEKDAY_PATTERN})|(?:{_WEEKDAY_PATTERN})\s+after\s+next|"
        rf"(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+20\d{{2}})?|"
        rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_PATTERN})(?:\s+20\d{{2}})?|"
        rf"\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?|20\d{{2}}-\d{{2}}-\d{{2}})"
        rf"(?:\s+(?:at|by)\s*(?P<clock>{_CLOCK_TOKEN}))?\b",
        re.I,
    )
    for m in due_re.finditer(text):
        if any(m.start() < b and a < m.end() for a, b in occupied):
            continue
        ref = resolve_date_reference(m.group("date"), now)
        if not ref:
            continue
        clock_raw = m.group("clock")
        minute = clock_to_minutes(clock_raw, _daypart_near(text, m.start(), m.end())) if clock_raw else None
        target = _combine(ref.start_date, minute) if minute is not None else datetime.combine(ref.start_date, time(23, 0), settings.tz)
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        certainty: Certainty = _clock_certainty(clock_raw)[0] if clock_raw else ref.evidence.certainty
        out.append(TemporalConstraint(
            kind="deadline", relation="by", latest_at=target, date_start=ref.start_date,
            clock=(ClockRef(
                minutes=minute, text=clock_raw, certainty=certainty,
                tolerance_minutes=_clock_certainty(clock_raw)[1],
            ) if minute is not None and clock_raw else None),
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end(), certainty),
        ))
        occupied.append((m.start(), m.end()))

    # Date-only due/by/deadline. If the phrase also contains a clock, the combined
    # parser above owns it; never add a second fake 23:59 deadline.
    for m in re.finditer(
        r"\b(?:due|deadline(?:\s+is)?|finish(?:ed)?|complete(?:d)?|done)?\s*"
        r"(?:by|no\s+later\s+than)\s+([^,;.]+)",
        text,
        re.I,
    ):
        if any(m.start() < b and a < m.end() for a, b in occupied):
            continue
        phrase = m.group(1)
        if find_clock_refs(phrase):
            continue
        ref = resolve_date_reference(phrase, now)
        if not ref:
            continue
        d = ref.end_date or ref.start_date
        target = datetime.combine(d, time(23, 59), settings.tz)
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        out.append(TemporalConstraint(
            kind="deadline", relation="by", latest_at=target, date_start=d,
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end(), ref.evidence.certainty),
        ))
    return out

def _point_constraints(text: str, now: datetime, ranges: list[TemporalConstraint], deadlines: list[TemporalConstraint]) -> list[TemporalConstraint]:
    out = []
    occupied = [(x.evidence.start, x.evidence.end) for x in [*ranges, *deadlines]]

    point_re = re.compile(
        rf"\b(?P<lead>at|starts?\s+at|begin(?:s)?\s+at|arrive(?:s)?\s+at|reach(?:es)?\s+at|"
        rf"around|about|roughly|approximately)\s+(?P<clock>{_CLOCK_TOKEN})\b",
        re.I,
    )
    for m in point_re.finditer(text):
        if any(m.start() < b and a < m.end() for a, b in occupied):
            continue
        hint = _daypart_near(text, m.start(), m.end())
        minute = clock_to_minutes(m.group("clock"), hint)
        if minute is None:
            continue
        day = _date_for_span(text, now, m.start(), m.end())
        approximate = m.group("lead").lower() in {"around", "about", "roughly", "approximately"}
        certainty, tolerance = ("approximate", 30) if approximate else _clock_certainty(m.group("clock"))
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        out.append(TemporalConstraint(
            kind="point", relation="at", start_at=_combine(day, minute), date_start=day,
            clock=ClockRef(minutes=minute, text=m.group("clock"), certainty=certainty, tolerance_minutes=tolerance),
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end(), certainty),
        ))
        occupied.append((m.start(), m.end()))

    # "7-ish" needs no lead word. Likewise a bare clock attached to an explicit date
    # ("tomorrow 7am") is a real point. Never steal clocks already owned by ranges/deadlines.
    date_refs = find_date_refs(text, now)
    for start, end, clock in find_clock_refs(text):
        if any(start < b and a < end for a, b in occupied):
            continue
        sentence = _sentence_window(text, start, end)
        sentence_start = max(0, text.rfind(sentence, 0, start + 1)) if sentence else 0
        sentence_end = sentence_start + len(sentence)
        has_date = any(
            sentence_start <= ref.evidence.start < sentence_end
            for ref in date_refs
        )
        raw = text[start:end]
        approximate = clock.certainty == "approximate" or bool(_APPROX.search(text[max(0,start-14):min(len(text),end+14)]))
        if not (has_date or approximate):
            continue
        day = _date_for_span(text, now, start, end)
        certainty = "approximate" if approximate else clock.certainty
        tolerance = 30 if approximate else clock.tolerance_minutes
        optional, negated, hypothetical = _modality(text, start, end)
        out.append(TemporalConstraint(
            kind="point", relation="at", start_at=_combine(day, clock.minutes), date_start=day,
            clock=ClockRef(minutes=clock.minutes, text=raw, certainty=certainty, tolerance_minutes=tolerance),
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, start, end, certainty),
        ))
        occupied.append((start, end))
    return out

def _hard_clock_bounds(text: str, now: datetime) -> list[TemporalConstraint]:
    out = []
    specs = [
        (
            re.compile(
                rf"\b(?P<rel>not\s+before|no\s+earlier\s+than|after)\s+"
                rf"(?P<clock>{_CLOCK_TOKEN})\b"
                rf"(?!\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?))",
                re.I,
            ),
            "not_before",
        ),
        (
            re.compile(
                rf"\b(?P<rel>until|not\s+after)\s+(?P<clock>{_CLOCK_TOKEN})\b",
                re.I,
            ),
            "not_after",
        ),
    ]
    for pattern, kind in specs:
        for m in pattern.finditer(text):
            hint = _daypart_near(text, m.start(), m.end())
            minute = clock_to_minutes(m.group("clock"), hint)
            if minute is None:
                continue
            day = _date_for_span(text, now, m.start(), m.end())
            target = _combine(day, minute)
            optional, negated, hypothetical = _modality(text, m.start(), m.end())
            certainty, tolerance = _clock_certainty(m.group("clock"))
            kwargs = {"earliest_at": target} if kind == "not_before" else {"latest_at": target}
            out.append(TemporalConstraint(
                kind=kind,
                relation="after" if kind == "not_before" else "until",
                date_start=day,
                clock=ClockRef(
                    minutes=minute, text=m.group("clock"),
                    certainty=certainty, tolerance_minutes=tolerance,
                ),
                optional=optional,
                negated=negated,
                hypothetical=hypothetical,
                evidence=_evidence(text, m.start(), m.end(), certainty),
                **kwargs,
            ))
    return out


def _relative_constraints(text: str, now: datetime) -> list[TemporalConstraint]:
    out = []
    # Submission-clock offsets: "in 10 minutes", "10 minutes from now", "after 5 min"
    patterns = [
        re.compile(rf"\b(?:in|after)\s+(?P<dur>{_NUM}\s*{_DURATION_UNIT})\b", re.I),
        re.compile(rf"\b(?P<dur>{_NUM}\s*{_DURATION_UNIT})\s+(?:from\s+now|later)\b", re.I),
    ]
    for pattern in patterns:
        for m in pattern.finditer(text):
            before = text[max(0, m.start() - 12):m.start()]
            if re.search(r"\bfor\s*$", before, re.I):
                continue
            minutes = duration_minutes(m.group("dur"))
            if minutes is None or minutes > 10080:
                continue
            optional, negated, hypothetical = _modality(text, m.start(), m.end())
            out.append(TemporalConstraint(
                kind="submission_offset", relation="after",
                earliest_at=now + timedelta(minutes=minutes),
                offset_minutes=minutes, anchor="submission_time",
                optional=optional, negated=negated, hypothetical=hypothetical,
                evidence=_evidence(text, m.start(), m.end()),
            ))

    # Explicit offset around an anchor, e.g. "20 min after lunch", "2h before exam".
    anchor_re = re.compile(
        rf"\b(?P<dur>{_NUM}\s*{_DURATION_UNIT})\s+"
        r"(?P<rel>after|before)\s+"
        r"(?P<anchor>[A-Za-z][A-Za-z0-9' -]{1,70}?)(?=$|[,;.]|"
        r"\s+(?:then|and\s+then|and\s+(?:stop|do|study|review|go|eat|swim|work|leave|head|meet|start|finish|continue|resume))\b)",
        re.I,
    )
    for m in anchor_re.finditer(text):
        minutes = duration_minutes(m.group("dur"))
        if minutes is None:
            continue
        sign = 1 if m.group("rel").lower() == "after" else -1
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        out.append(TemporalConstraint(
            kind="relative", relation=m.group("rel").lower(),
            anchor=_normalise(m.group("anchor")).strip(" ,.;:-"),
            offset_minutes=sign * minutes,
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end()),
        ))

    # Relation-only anchors. Keep unresolved anchor text instead of inventing a clock.
    relation_re = re.compile(
        r"\b(?P<rel>after|before|until|once)\s+"
        r"(?P<anchor>(?!\d)[A-Za-z][A-Za-z0-9' -]{1,70}?)(?=$|[,;.]|"
        r"\s+(?:then|and\s+then|and\s+(?:stop|do|study|review|go|eat|swim|work|leave|head|meet|start|finish|continue|resume))\b)",
        re.I,
    )
    for m in relation_re.finditer(text):
        # Don't duplicate the inner tail of an offset relation.
        if any(x.evidence.start <= m.start() < x.evidence.end for x in out):
            continue
        rel = "after" if m.group("rel").lower() == "once" else m.group("rel").lower()
        optional, negated, hypothetical = _modality(text, m.start(), m.end())
        out.append(TemporalConstraint(
            kind="relative", relation=rel,
            anchor=_normalise(m.group("anchor")).strip(" ,.;:-"),
            offset_minutes=0,
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=_evidence(text, m.start(), m.end()),
        ))
    return out


def _date_constraints(text: str, now: datetime, refs: list[DateRef]) -> list[TemporalConstraint]:
    out = []
    for ref in refs:
        optional, negated, hypothetical = _modality(text, ref.evidence.start, ref.evidence.end)
        out.append(TemporalConstraint(
            kind="date", date_start=ref.start_date, date_end=ref.end_date or ref.start_date,
            optional=optional, negated=negated, hypothetical=hypothetical,
            evidence=ref.evidence,
        ))
    return out


def _recurrence_constraints(specs: list[RecurrenceSpec]) -> list[TemporalConstraint]:
    return [
        TemporalConstraint(
            kind="recurrence", relation=None, anchor=spec.rrule,
            evidence=spec.evidence,
        )
        for spec in specs
    ]


def _unresolved_temporal_phrases(text: str, constraints: list[TemporalConstraint], dates: list[DateRef]) -> list[str]:
    covered = [(x.evidence.start, x.evidence.end) for x in constraints] + [(x.evidence.start, x.evidence.end) for x in dates]
    unresolved = []
    for m in _TEMPORAL_CUE.finditer(text):
        if any(m.start() < b and a < m.end() for a, b in covered):
            continue
        phrase = _sentence_window(text, m.start(), m.end()).strip(" ,.;:-")
        if phrase and phrase not in unresolved:
            unresolved.append(phrase)
    # Relationship phrases that are intentionally represented by anchor text are resolved
    # even though the anchor may not yet have a clock; the solver can bind it later.
    represented_sources = " ".join(x.evidence.source.lower() for x in constraints if x.kind == "relative")
    unresolved = [x for x in unresolved if not any(k in represented_sources for k in ("after", "before", "until"))]
    return unresolved[:12]


def _conflicts(constraints: list[TemporalConstraint]) -> list[str]:
    out = []
    exact_intervals = [x for x in constraints if x.start_at and x.end_at and not x.optional and not x.negated and not x.hypothetical]
    for item in exact_intervals:
        if item.end_at <= item.start_at:
            out.append(f"Temporal interval is non-positive: {item.evidence.source}")
    # Multiple exact point starts in one clause can be legitimate for multiple activities;
    # conflict detection here is intentionally conservative. Scheduling conflicts are
    # resolved only after activity identity is known.
    return out


def parse_temporal(text: str, now: datetime | None = None) -> TemporalDocument:
    stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    source = str(text or "")
    dates = find_date_refs(source, stamp)
    ranges = _range_constraints(source, stamp)
    deadlines = _deadline_constraints(source, stamp)
    points = _point_constraints(source, stamp, ranges, deadlines)
    hard_bounds = _hard_clock_bounds(source, stamp)
    relatives = _relative_constraints(source, stamp)
    dayparts = _daypart_constraints(source, stamp)
    recurrence = parse_recurrence(source, stamp)
    constraints = [
        *_date_constraints(source, stamp, dates),
        *ranges,
        *deadlines,
        *points,
        *hard_bounds,
        *relatives,
        *dayparts,
        *_recurrence_constraints(recurrence),
    ]

    # De-duplicate constraints with same source/kind/value.
    unique, seen = [], set()
    for c in sorted(constraints, key=lambda x: (x.evidence.start, x.evidence.end, x.kind)):
        key = (
            c.kind, c.evidence.start, c.evidence.end,
            c.start_at.isoformat() if c.start_at else None,
            c.end_at.isoformat() if c.end_at else None,
            c.earliest_at.isoformat() if c.earliest_at else None,
            c.latest_at.isoformat() if c.latest_at else None,
            c.anchor, c.offset_minutes,
        )
        if key not in seen:
            seen.add(key)
            unique.append(c)

    unresolved = _unresolved_temporal_phrases(source, unique, dates)
    conflicts = _conflicts(unique)
    confidence = "high"
    if unresolved:
        confidence = "medium"
    if conflicts:
        confidence = "low"
    return TemporalDocument(
        reference_time=stamp,
        timezone=settings.timezone,
        dates=dates,
        constraints=unique,
        recurrence=recurrence,
        unresolved=unresolved,
        conflicts=conflicts,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Compatibility helpers for the existing scheduler/parser stack
# ---------------------------------------------------------------------------

def date_for_phrase(value: str, now: datetime) -> date | None:
    ref = resolve_date_reference(value, now)
    return ref.start_date if ref else None


def day_hint(text: str, now: datetime) -> date:
    ref = resolve_date_reference(text, now)
    return ref.start_date if ref else now.date()


def extract_time_range(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    doc = parse_temporal(text, now)
    ranges = [
        x for x in doc.constraints
        if x.kind == "interval" and x.start_at and x.end_at
        and not x.negated and not x.hypothetical
    ]
    if not ranges:
        return None
    # Prefer exact intervals over approximate windows and source order.
    ranges.sort(key=lambda x: (
        0 if x.kind == "interval" else 1,
        0 if x.evidence.certainty == "exact" else 1,
        x.evidence.start,
    ))
    return ranges[0].start_at, ranges[0].end_at


def extract_deadline(text: str, now: datetime) -> datetime | None:
    doc = parse_temporal(text, now)
    rows = [
        x for x in doc.constraints
        if x.kind in {"deadline", "not_after"} and x.latest_at
        and not x.negated and not x.hypothetical
    ]
    rows.sort(key=lambda x: x.evidence.start)
    return rows[0].latest_at if rows else None


def relative_submission_start(text: str, submitted_at: datetime) -> tuple[datetime | None, int | None, str | None]:
    doc = parse_temporal(text, submitted_at)
    rows = [
        x for x in doc.constraints
        if x.kind == "submission_offset" and x.earliest_at
        and not x.negated and not x.hypothetical
    ]
    if rows:
        rows.sort(key=lambda x: x.evidence.start)
        row = rows[0]
        return row.earliest_at, row.offset_minutes, row.evidence.source
    return None, None, None


def recurrence_rrule(text: str, now: datetime | None = None) -> str | None:
    doc = parse_temporal(text, now or datetime.now(settings.tz))
    return doc.recurrence[0].rrule if doc.recurrence else None


def strip_temporal_phrases(text: str, now: datetime | None = None) -> str:
    """Remove only concrete temporal modifiers from a candidate task title.

    Relationship anchors ("after Physics") are deliberately retained because another
    compiler needs them for dependencies.
    """
    stamp = now or datetime.now(settings.tz)
    doc = parse_temporal(text, stamp)
    spans = []
    for item in doc.constraints:
        if item.kind == "daypart":
            bare = item.evidence.source.strip().casefold()
            if bare in {"morning", "afternoon", "evening", "night"}:
                # Keep titles such as "Morning Review" intact. More explicit phrases
                # like "this evening" are true modifiers and may be stripped.
                continue
        if item.kind in {"interval", "window", "deadline", "not_after", "point", "date", "daypart", "submission_offset", "recurrence"}:
            spans.append((item.evidence.start, item.evidence.end))
    for ref in doc.dates:
        spans.append((ref.evidence.start, ref.evidence.end))
    if not spans:
        return str(text or "")
    merged = []
    for a, b in sorted(spans):
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    source = str(text or "")
    out, cursor = [], 0
    for a, b in merged:
        out.append(source[cursor:a])
        out.append(" ")
        cursor = b
    out.append(source[cursor:])
    return re.sub(r"\s+", " ", "".join(out)).strip(" ,.;:-")


def temporal_summary(doc: TemporalDocument) -> list[str]:
    """Human-readable provenance lines for review/debug UIs."""
    rows = []
    for ref in doc.dates:
        target = ref.start_date.isoformat()
        if ref.end_date and ref.end_date != ref.start_date:
            target += "…" + ref.end_date.isoformat()
        rows.append(f"DATE · {ref.evidence.source} → {target} · {ref.evidence.certainty}")
    for c in doc.constraints:
        if c.kind == "date":
            continue
        value = ""
        if c.start_at and c.end_at:
            value = f"{c.start_at.isoformat()} → {c.end_at.isoformat()}"
        elif c.start_at:
            value = c.start_at.isoformat()
        elif c.latest_at:
            value = "≤ " + c.latest_at.isoformat()
        elif c.earliest_at:
            value = "≥ " + c.earliest_at.isoformat()
        elif c.anchor:
            value = c.anchor + (f" ({c.offset_minutes:+d}m)" if c.offset_minutes is not None else "")
        rows.append(f"{c.kind.upper()} · {c.evidence.source} → {value or 'relationship'} · {c.evidence.certainty}")
    return rows


__all__ = [
    "ClockRef", "DateRef", "Evidence", "RecurrenceSpec", "TemporalConstraint", "TemporalDocument",
    "clock_near", "clock_string", "clock_to_minutes", "date_for_phrase", "date_near", "day_hint", "duration_minutes", "extract_clock", "find_clock_refs",
    "extract_deadline", "extract_time_range", "find_date_refs", "parse_recurrence", "parse_temporal",
    "recurrence_rrule", "relative_submission_start", "resolve_date_reference",
    "strip_temporal_phrases", "temporal_summary",
]
