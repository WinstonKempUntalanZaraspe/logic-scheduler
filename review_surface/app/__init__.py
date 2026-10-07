"""Package-wide parser invariants shared by every AutoScheduler entrypoint.

The public Temporal IR remains in temporal_engine.py. These hooks harden a few
role-binding edge cases at package import time without creating a second parser.
"""
from __future__ import annotations

import re
from datetime import datetime, time

from . import temporal_engine as _temporal

_ORIGINAL_DURATION_MINUTES = _temporal.duration_minutes
_ORIGINAL_RANGE_CONSTRAINTS = _temporal._range_constraints
_ORIGINAL_DEADLINE_CONSTRAINTS = _temporal._deadline_constraints
_ORIGINAL_POINT_CONSTRAINTS = _temporal._point_constraints
_ORIGINAL_FIND_CLOCK_REFS = _temporal.find_clock_refs


def _overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _nearest_date_for_span(text, now, start, end):
    """Bind a clock/range to the nearest date phrase in the same sentence."""
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start), text.rfind("\n", 0, start))
    right_candidates = [x for x in (text.find(".", end), text.find(";", end), text.find("\n", end)) if x >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    sentence_start = left + 1
    refs = _temporal.find_date_refs(text[sentence_start:right], now)
    if not refs:
        return now.date()
    local_start = max(0, start - sentence_start)
    local_end = max(local_start, end - sentence_start)
    ref = min(refs, key=lambda item: min(abs(item.evidence.start - local_end), abs(local_start - item.evidence.end)))
    return ref.start_date


def _clock_refs_outside_date_phrases(text):
    """A number/number-word inside a recognized date phrase is not a clock."""
    refs = _ORIGINAL_FIND_CLOCK_REFS(text)
    date_refs = _temporal.find_date_refs(text, datetime.now(_temporal.settings.tz))
    if not date_refs:
        return refs
    return [
        row for row in refs
        if not any(
            _overlap(row[0], row[1], ref.evidence.start, ref.evidence.end)
            for ref in date_refs
        )
    ]

def _duration_minutes_without_meridiem(value):
    """AM/PM markers are clocks, never the duration phrase "a m" = one minute."""
    source = str(value or "")
    source = re.sub(
        r"(?i)\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\b(?:a\.?m\.?|p\.?m\.?)\b",
        " ",
        source,
    )
    return _ORIGINAL_DURATION_MINUTES(source)


def _ranges_without_date_fragments(text, now):
    """Date punctuation such as 2026-10-19 must never become a clock range."""
    rows = _ORIGINAL_RANGE_CONSTRAINTS(text, now)
    dates = _temporal.find_date_refs(text, now)
    return [
        row for row in rows
        if not any(
            _overlap(row.evidence.start, row.evidence.end, ref.evidence.start, ref.evidence.end)
            for ref in dates
        )
    ]


def _deadline_constraints_hardened(text, now):
    """Separate day numbers in dates from real deadline clocks."""
    rows = _ORIGINAL_DEADLINE_CONSTRAINTS(text, now)
    refs = _temporal.find_date_refs(text, now)
    kept = []
    for row in rows:
        suspicious = False
        if row.clock is not None:
            clock_text = str(row.clock.text or "").strip().casefold().replace(".", "")
            for ref in refs:
                if not _overlap(row.evidence.start, row.evidence.end, ref.evidence.start, ref.evidence.end):
                    continue
                date_text = ref.evidence.source.casefold().replace(".", "")
                if clock_text and clock_text in date_text:
                    suspicious = True
                    break
        if not suspicious:
            kept.append(row)

    for ref in refs:
        if any(_overlap(row.evidence.start, row.evidence.end, ref.evidence.start, ref.evidence.end) for row in kept):
            continue

        sentence_left = max(text.rfind(".", 0, ref.evidence.start), text.rfind(";", 0, ref.evidence.start), text.rfind("\n", 0, ref.evidence.start)) + 1
        right_candidates = [x for x in (text.find(".", ref.evidence.end), text.find(";", ref.evidence.end), text.find("\n", ref.evidence.end)) if x >= 0]
        sentence_right = min(right_candidates) if right_candidates else len(text)
        prefix = text[sentence_left:ref.evidence.start]
        operator = re.search(r"(?i)\b(?P<op>deadline(?:\s+is)?|due|no\s+later\s+than|by)\s*$", prefix)
        if not operator:
            continue

        op = operator.group("op").casefold()
        target_day = ref.end_date or ref.start_date
        tail = text[ref.evidence.end:sentence_right]
        clock = None
        clock_end = ref.evidence.end
        if re.match(r"(?i)^\s*(?:at|by)\b", tail):
            clocks = _temporal.find_clock_refs(tail)
            if clocks:
                c0, c1, clock = clocks[0]
                clock_end = ref.evidence.end + c1

        if clock is not None:
            target = datetime.combine(target_day, time(clock.minutes // 60, clock.minutes % 60), _temporal.settings.tz)
        else:
            end_minute = 23 * 60 if op.startswith("due") or op.startswith("deadline") else 23 * 60 + 59
            target = datetime.combine(target_day, time(end_minute // 60, end_minute % 60), _temporal.settings.tz)

        optional, negated, hypothetical = _temporal._modality(text, ref.evidence.start, clock_end)
        hard = op.startswith("no later")
        ev_start = sentence_left + operator.start()
        kept.append(_temporal.TemporalConstraint(
            kind="not_after" if hard else "deadline",
            relation="before" if hard else "by",
            latest_at=target,
            date_start=target_day,
            clock=clock,
            optional=optional,
            negated=negated,
            hypothetical=hypothetical,
            evidence=_temporal._evidence(text, ev_start, clock_end, ref.evidence.certainty),
        ))

    kept.sort(key=lambda row: (row.evidence.start, row.evidence.end, row.kind))
    return kept


def _points_without_hard_bound_duplicates(text, now, ranges, deadlines):
    """after/until/not before are bounds, never simultaneous exact-start claims."""
    points = _ORIGINAL_POINT_CONSTRAINTS(text, now, ranges, deadlines)
    hard_bounds = _temporal._hard_clock_bounds(text, now)
    occupied = [(row.evidence.start, row.evidence.end) for row in hard_bounds]
    return [point for point in points if not any(_overlap(point.evidence.start, point.evidence.end, a, b) for a, b in occupied)]


_temporal.find_clock_refs = _clock_refs_outside_date_phrases
_temporal._date_for_span = _nearest_date_for_span
_temporal.duration_minutes = _duration_minutes_without_meridiem
_temporal._range_constraints = _ranges_without_date_fragments
_temporal._deadline_constraints = _deadline_constraints_hardened
_temporal._point_constraints = _points_without_hard_bound_duplicates
