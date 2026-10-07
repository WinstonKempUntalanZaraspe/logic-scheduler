import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app import scheduler
from app.models import Task
from app.temporal_intake_patch import enrich_temporal_review

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 7, 14, 20, tzinfo=TZ)
CFG = {
    "wake_time": "07:00",
    "sleep_start": "23:00",
    "day_start": "07:00",
    "day_end": "23:00",
    "bedtime_wind_down_minutes": 20,
    "candidate_step_minutes": 10,
    "between_chunks_buffer": 10,
    "meal_protection": False,
}


def parsed_task(line, *, duration=60, fixed_start=None, fixed_end=None):
    return {
        "tasks": [{
            "action": "create",
            "title": "Study",
            "line": line,
            "intake_kind": "task",
            "priority": 0,
            "tags_add": ["flexible"],
            "meta_patch": {"duration_minutes": duration, "confidence": "high"},
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
        }],
        "notes": [],
        "warnings": [],
        "clarifications": [],
        "blocking_conflicts": [],
    }


def enrich(payload, text):
    return asyncio.run(enrich_temporal_review(payload, text, [], CFG, NOW))


def test_exact_point_compiles_to_hard_exact_start_without_inventing_end():
    text = "Study tomorrow at 7pm for 60 minutes"
    out = enrich(parsed_task(text), text)
    change = out["tasks"][0]
    meta = change["meta_patch"]
    assert meta["exact_start"] == "2026-10-08T19:00:00+08:00"
    assert meta["earliest"] == meta["exact_start"]
    assert change["fixed_start"] is None
    assert change["fixed_end"] is None
    assert meta["duration_minutes"] == 60


def test_approximate_point_is_preference_not_exact_start():
    text = "Study tomorrow around 7pm for 60 minutes"
    out = enrich(parsed_task(text), text)
    meta = out["tasks"][0]["meta_patch"]
    assert "exact_start" not in meta
    assert meta["earliest"] == "2026-10-08T18:30:00+08:00"
    assert meta["latest_end"] == "2026-10-08T20:30:00+08:00"
    assert meta["preferred_window_start"] == "18:30"
    assert meta["preferred_window_end"] == "19:30"


def test_between_window_becomes_legal_bounds():
    text = "Study tomorrow sometime between 2pm and 5pm"
    out = enrich(parsed_task(text, duration=45), text)
    meta = out["tasks"][0]["meta_patch"]
    assert meta["earliest"] == "2026-10-08T14:00:00+08:00"
    assert meta["latest_end"] == "2026-10-08T17:00:00+08:00"
    assert "exact_start" not in meta


def test_by_clock_is_deadline_not_start():
    text = "Finish study by tomorrow at 9pm"
    out = enrich(parsed_task(text), text)
    meta = out["tasks"][0]["meta_patch"]
    assert meta["deadline"] == "2026-10-08T21:00:00+08:00"
    assert "exact_start" not in meta


def test_optional_exact_clock_stays_soft():
    text = "Maybe study tomorrow at 7pm"
    out = enrich(parsed_task(text), text)
    meta = out["tasks"][0]["meta_patch"]
    assert "exact_start" not in meta
    assert meta["preferred_window_start"] == "18:45"
    assert meta["preferred_window_end"] == "19:15"


def test_bare_past_clock_requires_date_instead_of_guessing_tomorrow():
    text = "Study at 7am"
    out = enrich(parsed_task(text), text)
    meta = out["tasks"][0]["meta_patch"]
    assert "exact_start" not in meta
    assert any("already passed today" in q["reason"] for q in out["clarifications"])


def test_existing_fixed_interval_remains_authoritative():
    text = "Study tomorrow from 7pm to 8pm"
    payload = parsed_task(
        text,
        duration=60,
        fixed_start="2026-10-08T19:00:00+08:00",
        fixed_end="2026-10-08T20:00:00+08:00",
    )
    out = enrich(payload, text)
    meta = out["tasks"][0]["meta_patch"]
    assert "exact_start" not in meta
    assert out["tasks"][0]["fixed_start"] == "2026-10-08T19:00:00+08:00"
    assert out["tasks"][0]["fixed_end"] == "2026-10-08T20:00:00+08:00"


def test_temporal_review_exposes_provenance_and_ir():
    text = "Study next Friday around 7pm"
    out = enrich(parsed_task(text), text)
    assert out["temporal_review"]["confidence"] in {"high", "medium"}
    assert out["temporal_review"]["timezone"] == "Asia/Singapore"
    assert any("DATE" in x and "next Friday" in x for x in out["temporal_review"]["summary"])
    assert out["temporal_ir"]["reference_time"] == NOW.isoformat()


def test_exact_start_is_enforced_by_cp_sat_and_heuristic():
    task = Task("study", "p", "Study")
    meta = {
        "study": {
            "duration_minutes": 40,
            "confidence": "high",
            "exact_start": "2026-10-08T19:03:00+08:00",
            "earliest": "2026-10-08T19:03:00+08:00",
            "splittable": False,
        }
    }
    for planner in (scheduler._plan_cpsat, scheduler._plan_heuristic):
        segments, warnings, _ = planner([task], meta, [], NOW, 2, CFG, {})
        assert segments, warnings
        assert segments[0].start.isoformat() == "2026-10-08T19:03:00+08:00"
        assert segments[0].end.isoformat() == "2026-10-08T19:43:00+08:00"


def test_exact_start_collision_is_rejected_not_shifted():
    task = Task("study", "p", "Study")
    meta = {
        "study": {
            "duration_minutes": 40,
            "confidence": "high",
            "exact_start": "2026-10-08T19:03:00+08:00",
            "earliest": "2026-10-08T19:03:00+08:00",
            "splittable": False,
        }
    }
    busy = [
        scheduler.BusyBlock(
            datetime(2026, 10, 8, 19, 0, tzinfo=TZ),
            datetime(2026, 10, 8, 20, 0, tzinfo=TZ),
            "Fixed event",
            "google",
        )
    ]
    for planner in (scheduler._plan_cpsat, scheduler._plan_heuristic):
        segments, _, _ = planner([task], meta, busy, NOW, 2, CFG, {})
        assert segments == []



def test_hard_after_until_compile_to_earliest_and_latest_end_not_deadline():
    text = "Study tomorrow after 7pm until 9pm"
    out = enrich(parsed_task(text, duration=45), text)
    meta = out["tasks"][0]["meta_patch"]
    assert meta["earliest"] == "2026-10-08T19:00:00+08:00"
    assert meta["latest_end"] == "2026-10-08T21:00:00+08:00"
    assert "deadline" not in meta


def test_no_later_than_compiles_to_hard_latest_end():
    text = "Finish study tomorrow no later than 8pm"
    out = enrich(parsed_task(text), text)
    meta = out["tasks"][0]["meta_patch"]
    assert meta["latest_end"] == "2026-10-08T20:00:00+08:00"
