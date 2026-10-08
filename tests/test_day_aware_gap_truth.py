"""Final timeline truth: real free gaps, day-scoped constraints, no hidden meal ghosts.

These are deterministic regression cases for the Friday 9 Oct screenshot. They do
not need TickTick, external accounts, or network calls.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Task
from app.planning_gaps import rebuild_final_gaps
from app.final_productivity_contract_patch import classify_productivity_gaps


TZ = ZoneInfo("Asia/Singapore")
FRIDAY = datetime(2026, 10, 9, 7, 0, tzinfo=TZ)
SATURDAY = FRIDAY + timedelta(days=1)
CONFIG = {
    "wake_time": "07:00",
    "sleep_start": "23:00",
    "bedtime_wind_down_minutes": 20,
    "meals": [
        {"name": "Lunch", "start": "12:30", "minutes": 45},
        {"name": "Dinner", "start": "18:30", "minutes": 45},
    ],
}


def dt(hour, minute=0, *, day=FRIDAY):
    return day.replace(hour=hour, minute=minute).isoformat()


def meals():
    return [
        {"name": "Breakfast", "start": dt(7, 35), "end": dt(8, 5)},
        {"name": "Lunch", "start": dt(13), "end": dt(13, 45)},
        {"name": "Dinner", "start": dt(19), "end": dt(19, 45)},
    ]


def gaps(tasks=None, meta=None, diagnostics=None, horizon=1, config=None):
    return rebuild_final_gaps(
        [], {"flexible_meals": meals(), **(diagnostics or {})},
        tasks or [], [], FRIDAY, horizon, config or CONFIG, meta or {},
    )["planning_gaps"]


def span(rows, start, end):
    return next(
        (row for row in rows if row["start"] == start and row["end"] == end),
        None,
    )


def test_shifted_lunch_dinner_do_not_leave_invisible_15_or_30_minute_gaps():
    rows = gaps()
    # Old default Lunch at 12:30 and Dinner at 18:30 cannot carve invisible
    # gaps out of displayed 13:00 / 19:00 flexible meal appointments.
    morning = span(rows, dt(8, 5), dt(13))
    afternoon = span(rows, dt(13, 45), dt(19))
    assert morning is not None
    assert afternoon is not None
    assert afternoon["minutes"] == 315
    assert afternoon["productivity_case"] == "UNALLOCATED"
    assert morning["productivity_case"] == "UNALLOCATED"
    assert not span(rows, dt(18, 45), dt(19))
    assert all(g["productivity_case"] == "UNALLOCATED" for g in rows)


def test_future_only_rotational_physics_does_not_constrain_friday():
    task = Task(
        "rotation", "physics", "Learn rotational motion and connect it to linear motion",
        start=SATURDAY.replace(hour=7),
        end=SATURDAY.replace(hour=9),
    )
    meta = {
        "rotation": {
            "earliest": dt(7, day=SATURDAY),
            "latest_end": dt(23, day=SATURDAY),
            "duration_minutes": 180,
            "remaining_minutes": 180,
        }
    }
    unfinished = [{
        "task_id": "rotation",
        "title": task.title,
        "remaining_minutes": 180,
        "min_session_minutes": 25,
        "must_finish": False,
        "splittable": True,
        "legal_windows": [{"start": dt(10, 15, day=SATURDAY), "end": dt(13, day=SATURDAY)}],
    }]
    rows = gaps([task], meta, {"unfinished_work": unfinished})
    afternoon = span(rows, dt(13, 45), dt(19))
    assert afternoon is not None
    assert afternoon["productivity_case"] == "UNALLOCATED"
    assert afternoon["relevant_unfinished_ids"] == []
    assert all(g["productivity_case"] == "UNALLOCATED" for g in rows)
    assert all(task.title not in g["reason"] for g in rows)


def test_same_day_work_with_no_legal_opening_is_truly_constrained():
    task = Task("today", "p", "Tonight-only study", start=FRIDAY.replace(hour=19))
    meta = {"today": {"earliest": dt(20), "duration_minutes": 90}}
    unfinished = [{
        "task_id": "today",
        "title": task.title,
        "remaining_minutes": 90,
        "min_session_minutes": 60,
        "must_finish": False,
        "splittable": True,
        "legal_windows": [{"start": dt(20), "end": dt(22)}],
    }]
    rows = gaps([task], meta, {"unfinished_work": unfinished})
    afternoon = span(rows, dt(13, 45), dt(19))
    assert afternoon is not None
    assert afternoon["relevant_unfinished_ids"] == ["today"]
    assert afternoon["productivity_case"] == "CONSTRAINED"
    assert "earliest allowed start" in afternoon["reason"]


def test_same_day_eligible_work_remains_case_a():
    task = Task("work", "p", "Available project", start=FRIDAY.replace(hour=9))
    meta = {"work": {"duration_minutes": 60}}
    unfinished = [{
        "task_id": "work",
        "title": task.title,
        "remaining_minutes": 60,
        "min_session_minutes": 25,
        "splittable": True,
        "must_finish": False,
        "legal_windows": [{"start": dt(13, 45), "end": dt(19)}],
    }]
    rows = gaps([task], meta, {"unfinished_work": unfinished})
    afternoon = span(rows, dt(13, 45), dt(19))
    assert afternoon is not None
    assert afternoon["productivity_case"] == "A"
    assert "Available project" in afternoon["reason"]


def test_shifting_friday_meals_does_not_remove_saturday_default_protection():
    rows = gaps(horizon=2)
    # Only Friday has explicit flexible meals in this synthetic horizon. Saturday
    # still protects the configured meal clocks; nothing globally disables meals.
    assert span(rows, dt(7, day=SATURDAY), dt(12, 30, day=SATURDAY))
    assert span(rows, dt(13, 15, day=SATURDAY), dt(18, 30, day=SATURDAY))


def test_actual_protected_15_minute_buffer_is_not_merged_away():
    diagnostics = {
        "human_uncertainty_buffers": [{
            "label": "Real transition before dinner",
            "source": "personal-recovery",
            "start": dt(18, 30),
            "end": dt(18, 45),
        }],
    }
    rows = gaps(diagnostics=diagnostics)
    assert span(rows, dt(13, 45), dt(18, 30)) is not None
    assert span(rows, dt(18, 45), dt(19)) is not None
    assert not span(rows, dt(13, 45), dt(19))


def test_original_configured_meals_still_protect_time_without_flexible_meal_plan():
    diagnostics = rebuild_final_gaps(
        [], {"unfinished_work": []}, [], [], FRIDAY, 1, CONFIG, {},
    )
    rows = diagnostics["planning_gaps"]
    assert span(rows, dt(7), dt(12, 30))
    assert span(rows, dt(13, 15), dt(18, 30))
    assert not span(rows, dt(13, 15), dt(19))


def test_direct_classifier_honors_explicit_empty_same_day_work_scope():
    result = classify_productivity_gaps({
        "unfinished_work": [{
            "task_id": "tomorrow-only",
            "title": "Tomorrow's physics",
            "remaining_minutes": 90,
            "min_session_minutes": 25,
            "splittable": True,
            "legal_windows": [{"start": dt(7, day=SATURDAY), "end": dt(11, day=SATURDAY)}],
        }],
        "planning_gaps": [{
            "start": dt(13, 45),
            "end": dt(19),
            "minutes": 315,
            "relevant_unfinished_ids": [],
            "relevant_not_schedulable_ids": [],
        }],
    })
    gap = result["planning_gaps"][0]
    assert gap["productivity_case"] == "UNALLOCATED"
    assert "Tomorrow's physics" not in gap["reason"]
