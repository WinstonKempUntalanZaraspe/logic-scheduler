from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import quickdump as qd
from app.temporal_engine import (
    clock_to_minutes,
    extract_deadline,
    extract_time_range,
    parse_recurrence,
    parse_temporal,
    resolve_date_reference,
)

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 7, 14, 20, tzinfo=TZ)  # Wednesday


@pytest.mark.parametrize(
    "text, expected",
    [
        ("today", date(2026, 10, 7)),
        ("tomorrow", date(2026, 10, 8)),
        ("tmr", date(2026, 10, 8)),
        ("day after tomorrow", date(2026, 10, 9)),
        ("in 3 days", date(2026, 10, 10)),
        ("in two weeks", date(2026, 10, 21)),
        ("this Friday", date(2026, 10, 9)),
        ("next Friday", date(2026, 10, 9)),
        ("Friday after next", date(2026, 10, 16)),
        ("three Tuesdays from now", date(2026, 10, 27)),
        ("19 Oct", date(2026, 10, 19)),
        ("Oct 19", date(2026, 10, 19)),
        ("19/10", date(2026, 10, 19)),
        ("2026-10-19", date(2026, 10, 19)),
        ("first Monday of November", date(2026, 11, 2)),
        ("end of the month", date(2026, 10, 31)),
    ],
)
def test_date_language_normalizes_to_one_calendar(text, expected):
    ref = resolve_date_reference(text, NOW)
    assert ref is not None
    assert ref.start_date == expected


def test_week_and_weekend_ranges_are_preserved_as_ranges():
    weekend = resolve_date_reference("this weekend", NOW)
    assert weekend and weekend.start_date == date(2026, 10, 10)
    assert weekend.end_date == date(2026, 10, 11)
    assert weekend.precision == "range"

    week = resolve_date_reference("next week", NOW)
    assert week and week.start_date == date(2026, 10, 12)
    assert week.end_date == date(2026, 10, 18)
    assert week.precision == "range"


@pytest.mark.parametrize(
    "clock, expected",
    [
        ("7pm", 19 * 60),
        ("7 PM", 19 * 60),
        ("19:00", 19 * 60),
        ("1900", 19 * 60),
        ("noon", 12 * 60),
        ("midnight", 0),
        ("quarter past seven", 7 * 60 + 15),
        ("half past six", 6 * 60 + 30),
        ("quarter to nine", 8 * 60 + 45),
        ("7-ish", 7 * 60),
    ],
)
def test_clock_language_normalizes(clock, expected):
    assert clock_to_minutes(clock) == expected


@pytest.mark.parametrize(
    "text, expected_start, expected_end",
    [
        (
            "I will sweep from 10:30 PM to 10:50 PM",
            "2026-10-07T22:30:00+08:00",
            "2026-10-07T22:50:00+08:00",
        ),
        (
            "study 7pm-9pm",
            "2026-10-07T19:00:00+08:00",
            "2026-10-07T21:00:00+08:00",
        ),
        (
            "work tomorrow 7am till 3pm",
            "2026-10-08T07:00:00+08:00",
            "2026-10-08T15:00:00+08:00",
        ),
        (
            "shift tomorrow, seven till three",
            "2026-10-08T07:00:00+08:00",
            "2026-10-08T15:00:00+08:00",
        ),
        (
            "sleep 11pm-1am",
            "2026-10-07T23:00:00+08:00",
            "2026-10-08T01:00:00+08:00",
        ),
    ],
)
def test_exact_intervals(text, expected_start, expected_end):
    interval = extract_time_range(text, NOW)
    assert interval is not None
    assert interval[0].isoformat() == expected_start
    assert interval[1].isoformat() == expected_end


def test_between_is_window_not_fixed_interval_semantics_in_ir():
    doc = parse_temporal("Study sometime between 2pm and 5pm tomorrow", NOW)
    rows = [x for x in doc.constraints if x.kind == "window"]
    assert len(rows) == 1
    assert rows[0].start_at.isoformat() == "2026-10-08T14:00:00+08:00"
    assert rows[0].end_at.isoformat() == "2026-10-08T17:00:00+08:00"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("finish this by 9pm", "2026-10-07T21:00:00+08:00"),
        ("finish no later than 8pm", "2026-10-07T20:00:00+08:00"),
        ("finish by tomorrow at 9:30pm", "2026-10-08T21:30:00+08:00"),
        ("finish by 19 Oct", "2026-10-19T23:59:00+08:00"),
    ],
)
def test_deadlines_distinguish_latest_completion_from_start(text, expected):
    deadline = extract_deadline(text, NOW)
    assert deadline is not None
    assert deadline.isoformat() == expected


def test_combined_date_clock_deadline_is_not_duplicated_as_2359():
    doc = parse_temporal("Finish assignment by tomorrow at 9:30pm", NOW)
    deadlines = [x for x in doc.constraints if x.kind in {"deadline", "not_after"}]
    assert len(deadlines) == 1
    assert deadlines[0].latest_at.isoformat() == "2026-10-08T21:30:00+08:00"


def test_exact_and_approximate_points_stay_distinct():
    exact = parse_temporal("Study tomorrow at 7pm", NOW)
    exact_point = next(x for x in exact.constraints if x.kind == "point")
    assert exact_point.start_at.isoformat() == "2026-10-08T19:00:00+08:00"
    assert exact_point.clock.certainty == "exact"
    assert exact_point.clock.tolerance_minutes == 0

    approx = parse_temporal("Study tomorrow around 7pm", NOW)
    approx_point = next(x for x in approx.constraints if x.kind == "point")
    assert approx_point.start_at.isoformat() == "2026-10-08T19:00:00+08:00"
    assert approx_point.clock.certainty == "approximate"
    assert approx_point.clock.tolerance_minutes == 30

    ish = parse_temporal("Study tomorrow 7-ish", NOW)
    ish_point = next(x for x in ish.constraints if x.kind == "point")
    assert ish_point.clock.certainty == "approximate"


def test_year_in_date_does_not_turn_into_2026_clock():
    doc = parse_temporal("Study on 2026-10-19", NOW)
    points = [x for x in doc.constraints if x.kind == "point"]
    assert points == []


def test_relative_submission_and_anchor_relationships_are_different():
    doc = parse_temporal("Start in 10 minutes, then review 20 minutes after lunch and stop before dinner.", NOW)
    offsets = [x for x in doc.constraints if x.kind == "submission_offset"]
    relations = [x for x in doc.constraints if x.kind == "relative"]
    assert offsets and offsets[0].offset_minutes == 10
    assert offsets[0].earliest_at.isoformat() == "2026-10-07T14:30:00+08:00"
    assert any(x.anchor.lower() == "lunch" and x.offset_minutes == 20 for x in relations)
    assert any(x.anchor.lower() == "dinner" and x.relation == "before" for x in relations)


@pytest.mark.parametrize(
    "text, rrule",
    [
        ("Repeat every second Tuesday", "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"),
        ("Repeat weekdays except Friday", "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH"),
        ("Repeat first Sunday of each month", "RRULE:FREQ=MONTHLY;INTERVAL=1;BYDAY=1SU"),
        ("Repeat every Monday and Thursday", "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TH"),
        ("Repeat every 2 weeks", "RRULE:FREQ=WEEKLY;INTERVAL=2"),
    ],
)
def test_recurrence_language(text, rrule):
    rows = parse_recurrence(text, NOW)
    assert rows
    assert rows[0].rrule == rrule


def test_recurrence_until_month_keeps_boundary():
    rows = parse_recurrence("Repeat every Monday until December", NOW)
    assert rows
    assert rows[0].until == date(2026, 12, 31)
    assert "UNTIL=20261231T235959" in rows[0].rrule


@pytest.mark.parametrize(
    "text, flag",
    [
        ("Maybe work tomorrow at 7am", "optional"),
        ("I don't work tomorrow at 7am", "negated"),
        ("What if I worked tomorrow at 7am?", "hypothetical"),
    ],
)
def test_modality_is_preserved_instead_of_promoted_to_hard_time(text, flag):
    doc = parse_temporal(text, NOW)
    point = next(x for x in doc.constraints if x.kind == "point")
    assert getattr(point, flag) is True


def test_provenance_preserves_exact_source_excerpt():
    text = "Please finish the report no later than 8:15 PM tomorrow."
    doc = parse_temporal(text, NOW)
    row = next(x for x in doc.constraints if x.kind in {"deadline", "not_after"})
    assert text[row.evidence.start:row.evidence.end] == row.evidence.source
    assert "8:15 PM" in row.evidence.source


def test_quickdump_compatibility_helpers_now_share_the_same_engine():
    assert qd._clock_to_minutes("10:30 PM") == 22 * 60 + 30
    assert qd._date_for_word("Friday after next", NOW) == date(2026, 10, 16)
    a, b = qd._extract_time_range("Tomorrow from 7am to 3pm", NOW)
    assert a.isoformat() == "2026-10-08T07:00:00+08:00"
    assert b.isoformat() == "2026-10-08T15:00:00+08:00"



def test_mature_due_syntax_survives_temporal_refactor():
    assert extract_deadline("Assignment due tomorrow", NOW).isoformat() == "2026-10-08T23:00:00+08:00"
    assert extract_deadline("Assignment deadline Friday at 5pm", NOW).isoformat() == "2026-10-09T17:00:00+08:00"


def test_hard_before_after_bounds_are_not_collapsed_into_soft_deadlines():
    doc = parse_temporal("Study tomorrow after 7pm until 9pm", NOW)
    lower = next(x for x in doc.constraints if x.kind == "not_before")
    upper = next(x for x in doc.constraints if x.kind == "not_after" and x.relation == "until")
    assert lower.earliest_at.isoformat() == "2026-10-08T19:00:00+08:00"
    assert upper.latest_at.isoformat() == "2026-10-08T21:00:00+08:00"


def test_no_later_than_is_a_hard_not_after_constraint():
    doc = parse_temporal("Finish tomorrow no later than 8pm", NOW)
    row = next(x for x in doc.constraints if x.kind == "not_after")
    assert row.latest_at.isoformat() == "2026-10-08T20:00:00+08:00"


def test_legacy_exact_range_adapter_does_not_promote_flexible_between_window():
    assert qd._extract_time_range("Study tomorrow sometime between 2pm and 5pm", NOW) is None


def test_recurrence_for_next_six_weeks_is_bounded():
    rows = parse_recurrence("Do physics every Monday for the next six weeks", NOW)
    assert rows
    assert rows[0].until == date(2026, 11, 18)
    assert "UNTIL=20261118T235959" in rows[0].rrule


def test_bare_daypart_word_is_preserved_in_task_title_stripping():
    assert qd._strip_task_modifiers("Morning Review") == "Morning Review"



def test_date_and_duration_numbers_do_not_become_phantom_clock_points():
    doc = parse_temporal("Study Oct 19 at 8pm", NOW)
    points = [x for x in doc.constraints if x.kind == "point"]
    assert len(points) == 1
    assert points[0].start_at.isoformat() == "2026-10-19T20:00:00+08:00"

    doc = parse_temporal("Tomorrow review 10 minutes after lunch", NOW)
    points = [x for x in doc.constraints if x.kind == "point"]
    assert points == []
    rel = next(x for x in doc.constraints if x.kind == "relative" and x.anchor.lower() == "lunch")
    assert rel.offset_minutes == 10
