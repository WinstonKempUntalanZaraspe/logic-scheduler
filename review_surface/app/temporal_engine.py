from __future__ import annotations

"""Public Temporal Engine facade with boundary hardening."""

import re
from datetime import datetime, time

from . import temporal_engine_core as _core

for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)

_original_find_date_refs = _core.find_date_refs
_original_find_clock_refs = _core.find_clock_refs
_original_resolve_range_minutes = _core._resolve_range_minutes
_original_range_constraints = _core._range_constraints
_original_deadline_constraints = _core._deadline_constraints


def duration_minutes(value: str | None) -> int | None:
    text = _core._normalise(value).lower()
    total = 0.0
    seen = False
    compound = re.search(
        r"\b(?P<h>\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)"
        r"(?:\s*(?:and\s+)?)?(?P<m>\d{1,2})?\s*(?:minutes?|mins?|min|m)?\b",
        text,
        re.I,
    )
    if compound:
        total += float(compound.group("h")) * 60 + int(compound.group("m") or 0)
        seen = True
        text = text[:compound.start()] + " " + text[compound.end():]
    pattern = re.compile(rf"\b(?P<n>{_core._NUM})\s*(?P<u>{_core._DURATION_UNIT})\b", re.I)
    for match in pattern.finditer(text):
        raw_number = match.group("n")
        if raw_number.casefold() in {"a", "an"} and match.start("u") == match.end("n"):
            continue
        n = _core._number(raw_number)
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
    return max(1, int(round(total))) if seen else None


def find_date_refs(text: str, now: datetime):
    """Filter numeric date candidates that are clearly shorthand clock ranges.

    19/10 remains a Singapore-style date. But in tomorrow 6-7pm, the 6-7
    substring belongs to a clock interval and must never reserve a July date span.
    """
    source = str(text or "")
    refs = _original_find_date_refs(source, now)
    out = []
    for ref in refs:
        raw = ref.evidence.source.strip()
        if re.fullmatch(r"\d{1,2}[-/]\d{1,2}", raw):
            after = source[ref.evidence.end:ref.evidence.end + 8]
            before = source[max(0, ref.evidence.start - 40):ref.evidence.start]
            ampm_tail = bool(re.match(r"\s*(?:a\.?m\.?|p\.?m\.?)\b", after, re.I))
            explicit_day_near = bool(re.search(
                r"\b(?:today|tonight|tomorrow|tmr|monday|tuesday|wednesday|thursday|"
                r"friday|saturday|sunday)\b[^.;\n]{0,32}$",
                before,
                re.I,
            ))
            if ampm_tail or explicit_day_near:
                continue
        out.append(ref)
    return out


def find_clock_refs(text: str):
    """Remove clock candidates that are actually tokens inside date expressions."""
    date_spans = [(ref.evidence.start, ref.evidence.end) for ref in _core.find_date_refs(text, datetime.now(_core.settings.tz))]
    return [
        row for row in _original_find_clock_refs(text)
        if not any(row[0] < end and start < row[1] for start, end in date_spans)
    ]


def _resolve_range_minutes(a_raw: str, b_raw: str, context: str):
    """Resolve shorthand ranges with reliable AM/PM inheritance.

    In compact forms like 6-7pm there is no word boundary before "pm", because the
    preceding 7 is also a word character. Detect meridiem as a suffix instead.
    """
    hint = _core._daypart_near(context, 0, len(context))
    a = _core.clock_to_minutes(a_raw, hint)
    b = _core.clock_to_minutes(b_raw, hint)
    if a is None or b is None:
        return None

    def suffix(value: str):
        match = re.search(r"(a\.?m\.?|p\.?m\.?)\s*$", str(value or ""), re.I)
        return match.group(1).lower().replace(".", "") if match else None

    a_ap = suffix(a_raw)
    b_ap = suffix(b_raw)
    if a_ap and not b_ap:
        inherited = _core.clock_to_minutes(str(b_raw).strip() + a_ap)
        if inherited is not None:
            b = inherited
    elif b_ap and not a_ap:
        inherited = _core.clock_to_minutes(str(a_raw).strip() + b_ap)
        if inherited is not None:
            a = inherited

    if b <= a:
        if b + 12 * 60 > a and b + 12 * 60 - a <= 12 * 60:
            b += 12 * 60
        else:
            b += 24 * 60
    if b - a > 16 * 60:
        return None
    return a, b


def _range_constraints(text: str, now: datetime):
    date_spans = [(ref.evidence.start, ref.evidence.end) for ref in _core.find_date_refs(text, now)]
    return [
        row for row in _original_range_constraints(text, now)
        if not any(row.evidence.start < end and start < row.evidence.end for start, end in date_spans)
    ]


def _looks_like_date_tail(text: str, row) -> bool:
    if row.clock is None:
        return False
    raw = _core._normalise(row.clock.text).casefold().replace(".", "")
    if not re.fullmatch(r"\d{1,2}", raw):
        return False
    after = text[row.evidence.end:min(len(text), row.evidence.end + 28)]
    return bool(
        re.match(rf"^\s+(?:{_core._MONTH_PATTERN})\b", after, re.I)
        or re.match(r"^\s*[/-]\s*\d", after)
    )


def _dedupe_deadlines(rows):
    out, seen = [], set()
    for row in sorted(rows, key=lambda x: (x.evidence.start, x.evidence.end, x.kind)):
        key = (
            row.kind,
            row.relation,
            row.latest_at.isoformat() if row.latest_at else None,
        )
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _deadline_from_body(text: str, now: datetime, match: re.Match, *, direct_due: bool, hard_not_after: bool = False):
    body = match.group("body")
    ref = _core.resolve_date_reference(body, now)
    if not ref:
        return None
    body_offset = match.start("body")
    clocks = []
    for start, end, clock in _core.find_clock_refs(body):
        abs_start, abs_end = body_offset + start, body_offset + end
        ref_start, ref_end = body_offset + ref.evidence.start, body_offset + ref.evidence.end
        if abs_start < ref_end and ref_start < abs_end:
            continue
        clocks.append(clock)
    if clocks:
        clock = clocks[-1]
        target = _core._combine(ref.start_date, clock.minutes)
        certainty, clock_ref = clock.certainty, clock
    else:
        target = datetime.combine(ref.end_date or ref.start_date,
                                  time(23, 0 if direct_due else 59), _core.settings.tz)
        certainty, clock_ref = ref.evidence.certainty, None
    optional, negated, hypothetical = _core._modality(text, match.start(), match.end())
    return _core.TemporalConstraint(
        kind="not_after" if hard_not_after else "deadline",
        relation="before" if hard_not_after else "by",
        latest_at=target, date_start=target.date(), clock=clock_ref,
        optional=optional, negated=negated, hypothetical=hypothetical,
        evidence=_core._evidence(text, match.start(), match.end(), certainty),
    )


def _deadline_constraints(text: str, now: datetime):
    rows = [row for row in _original_deadline_constraints(text, now) if not _looks_like_date_tail(text, row)]
    direct = re.compile(r"\b(?:due|deadline(?:\s+is)?)\s+(?P<body>[^,;.]+)", re.I)
    for match in direct.finditer(text):
        row = _deadline_from_body(text, now, match, direct_due=True)
        if row is not None:
            rows.append(row)
    by_date = re.compile(
        r"\b(?:due|deadline(?:\s+is)?|finish(?:ed)?|complete(?:d)?|done)?\s*"
        r"(?P<rel>by|no\s+later\s+than)\s+(?P<body>[^,;.]+)", re.I,
    )
    for match in by_date.finditer(text):
        if _core.resolve_date_reference(match.group("body"), now) is None:
            continue
        row = _deadline_from_body(text, now, match, direct_due=False,
                                  hard_not_after=match.group("rel").casefold().startswith("no"))
        if row is not None:
            rows.append(row)
    return _dedupe_deadlines(rows)


_core.duration_minutes = duration_minutes
_core.find_date_refs = find_date_refs
_core.find_clock_refs = find_clock_refs
_core._resolve_range_minutes = _resolve_range_minutes
_core._range_constraints = _range_constraints
_core._deadline_constraints = _deadline_constraints

parse_temporal = _core.parse_temporal
extract_deadline = _core.extract_deadline
extract_time_range = _core.extract_time_range
relative_submission_start = _core.relative_submission_start
parse_recurrence = _core.parse_recurrence
recurrence_rrule = _core.recurrence_rrule
strip_temporal_phrases = _core.strip_temporal_phrases
temporal_summary = _core.temporal_summary

__all__ = list(_core.__all__)
