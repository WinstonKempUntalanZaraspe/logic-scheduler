from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app.final_entrypoint  # installs production future-day wake/sleep overrides
from app import scheduler
from app.early_commitment import (
    enrich_early_future_commitment,
    looks_like_early_future_commitment,
    _travel_minutes,
    _wake_clock,
)

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 7, 21, 33, tzinfo=TZ)
CFG = {
    "wake_time": "07:00",
    "day_start": "07:00",
    "sleep_start": "23:00",
    "day_end": "23:00",
    "bedtime_wind_down_minutes": 20,
}


def base(text, tasks=None):
    return {
        "tasks": list(tasks or []),
        "context": {"date": NOW.date().isoformat(), "source": "quick-dump"},
        "notes": [],
        "warnings": [],
        "clarifications": [{"text": text, "reason": "Unclear whether this is work or a planning instruction; no task created."}],
        "intents": [{"text": text, "kind": "ambiguous", "status": "needs-input"}],
    }


def blocks(out, source):
    return [b for b in out["context"].get("temporary_blocks", []) if b.get("source") == source]


def test_exact_user_prompt_backplans_sleep_prep_and_travel_and_asks_only_for_job_end():
    text = "I actually have a job at 7am tmr, 1h 15 travel time, 5 am need wake up, shower etc."
    assert looks_like_early_future_commitment(text)
    assert _wake_clock(text) == "05:00"
    assert _travel_minutes(text) == 75

    accidental = {
        "action": "create",
        "title": text,
        "line": text,
        "meta_patch": {"duration_minutes": 30},
    }
    out = enrich_early_future_commitment(base(text, [accidental]), text, [], CFG, NOW)
    ctx = out["context"]
    future = ctx["tomorrow_plan"]
    early = future["early_commitment"]

    assert out["tasks"] == []
    assert ctx["replan_requested"] is True
    assert ctx["replan_scope"] == "today"
    assert ctx["minimum_horizon_days"] >= 2
    assert future["date"] == "2026-10-08"
    assert future["wake_time"] == "05:00"

    assert early["label"] == "Job"
    assert early["start"] == "2026-10-08T07:00:00+08:00"
    assert early["end"] is None
    assert early["travel_minutes"] == 75
    assert early["depart_home"] == "2026-10-08T05:45:00+08:00"
    assert early["prep_start"] == "2026-10-08T05:00:00+08:00"
    assert early["prep_end"] == "2026-10-08T05:45:00+08:00"

    prep = blocks(out, "early-commitment-prep")
    travel = blocks(out, "early-commitment-travel")
    assert len(prep) == 1
    assert prep[0]["start"] == "2026-10-08T05:00:00+08:00"
    assert prep[0]["end"] == "2026-10-08T05:45:00+08:00"
    assert len(travel) == 1
    assert travel[0]["start"] == "2026-10-08T05:45:00+08:00"
    assert travel[0]["end"] == "2026-10-08T07:00:00+08:00"

    # Normal routine is 23:00 -> 07:00 = 8h. Ideal one-off bedtime is therefore 21:00.
    # At 21:33 with a 20m wind-down, time travel is impossible, so sleep becomes 21:53.
    adjustment = ctx["early_wake_sleep_adjustment"]
    assert adjustment["ideal_sleep_start"] == "2026-10-07T21:00:00+08:00"
    assert adjustment["effective_sleep_start"] == "2026-10-07T21:53:00+08:00"
    assert adjustment["normal_sleep_minutes"] == 480
    assert adjustment["projected_sleep_minutes"] == 427
    assert adjustment["sleep_shortfall_minutes"] == 53
    assert ctx["sleep_start"] == "21:53"

    assert not out["clarifications"]
    held = blocks(out, "early-commitment-unknown-tail")
    assert len(held) == 1
    assert held[0]["start"] == "2026-10-08T07:00:00+08:00"
    assert held[0]["end"] == "2026-10-08T23:00:00+08:00"
    assert held[0]["uncertain_end"] is True
    assert any("end time is unknown" in w and "held as constrained" in w for w in out["warnings"])
    assert any("leave 05:45" in n and "arrive 07:00" in n for n in out["notes"])
    assert any("ideal sleep was 21:00" in n and "21:53" in n for n in out["notes"])

    # Persistent/global routine is untouched.
    assert CFG["wake_time"] == "07:00"
    assert CFG["sleep_start"] == "23:00"


def test_complete_job_range_has_no_missing_end_question_and_blocks_the_shift():
    text = (
        "Tomorrow I have a job from 7am to 3pm. Travel time is 1 hour 15 minutes. "
        "I need to wake at 5am, shower and get ready before leaving."
    )
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    early = out["context"]["tomorrow_plan"]["early_commitment"]
    assert early["start"] == "2026-10-08T07:00:00+08:00"
    assert early["end"] == "2026-10-08T15:00:00+08:00"
    assert early["depart_home"] == "2026-10-08T05:45:00+08:00"
    assert not out["clarifications"]

    fixed = blocks(out, "early-commitment-fixed")
    assert len(fixed) == 1
    assert fixed[0]["label"] == "Job"
    assert fixed[0]["start"] == "2026-10-08T07:00:00+08:00"
    assert fixed[0]["end"] == "2026-10-08T15:00:00+08:00"


def test_existing_job_row_can_supply_missing_end_time():
    text = "I have a job at 7am tomorrow, travel is 75 minutes, wake up at 5am and get ready first."
    rows = [{
        "id": "job-1",
        "title": "Job",
        "start": "2026-10-08T07:00:00+08:00",
        "end": "2026-10-08T15:00:00+08:00",
        "tags": ["fixed"],
        "status": 0,
    }]
    out = enrich_early_future_commitment(base(text), text, rows, CFG, NOW)
    assert out["context"]["tomorrow_plan"]["early_commitment"]["end"] == "2026-10-08T15:00:00+08:00"
    assert not out["clarifications"]


def test_impossible_wake_vs_travel_is_reported_instead_of_overlapping():
    text = "Tomorrow I have a shift at 7am, travel takes 1 hour 15 minutes, wake up at 6am, shower and get ready."
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    reasons = [q["reason"] for q in out["clarifications"]]
    assert any("waking at 06:00 leaves less than the stated 75-minute travel time" in r for r in reasons)
    assert blocks(out, "early-commitment-unknown-tail")


def test_unrelated_tomorrow_request_is_not_touched():
    text = "Tomorrow I want to study physics for 2 hours."
    original = base(text)
    assert not looks_like_early_future_commitment(text)
    assert enrich_early_future_commitment(original, text, [], CFG, NOW) == original



def test_shower_clock_range_is_not_mistaken_for_job_range():
    text = (
        "Tomorrow I have a job at 7am, travel takes 1h 15m, wake at 5am, "
        "shower from 5:00am to 5:20am and get ready."
    )
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    early = out["context"]["tomorrow_plan"]["early_commitment"]
    assert early["start"] == "2026-10-08T07:00:00+08:00"
    assert early["end"] is None
    assert blocks(out, "early-commitment-unknown-tail")
    assert not blocks(out, "early-commitment-fixed")



def test_solver_has_no_legal_overnight_window_between_temporary_sleep_and_early_wake():
    text = "I actually have a job at 7am tmr, 1h 15 travel time, 5 am need wake up, shower etc."
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    cfg = dict(CFG) | {"_quick_context": out["context"]}

    today_start, today_end = scheduler._usable_bounds(NOW.date(), cfg)
    tomorrow_start, tomorrow_end = scheduler._usable_bounds(NOW.date() + timedelta(days=1), cfg)

    assert today_start.isoformat() == "2026-10-07T07:00:00+08:00"
    assert today_end.isoformat() == "2026-10-07T21:53:00+08:00"
    assert tomorrow_start.isoformat() == "2026-10-08T05:00:00+08:00"
    assert tomorrow_end.isoformat() == "2026-10-08T23:00:00+08:00"
    assert tomorrow_start > today_end



def test_early_commitment_generalizes_to_friday_after_next_without_changing_normal_routine():
    text = (
        "Friday after next I have a job at 7am, travel time is 1h 15m, "
        "wake at 5am, shower and get ready."
    )
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    ctx = out["context"]
    plans = ctx["future_day_plans"]
    assert plans["2026-10-16"]["wake_time"] == "05:00"
    assert plans["2026-10-15"]["sleep_start"] == "21:00"
    early = plans["2026-10-16"]["early_commitment"]
    assert early["start"] == "2026-10-16T07:00:00+08:00"
    assert early["depart_home"] == "2026-10-16T05:45:00+08:00"
    assert ctx.get("sleep_start") is None
    assert CFG["wake_time"] == "07:00"
    assert CFG["sleep_start"] == "23:00"


def test_multiple_clock_roles_do_not_steal_shower_range_as_job_range():
    text = (
        "Friday after next I have a job at 7am. Travel takes 1h 15m. "
        "Wake at 5am. Shower from 5:00am to 5:20am, then get ready."
    )
    out = enrich_early_future_commitment(base(text), text, [], CFG, NOW)
    early = out["context"]["future_day_plans"]["2026-10-16"]["early_commitment"]
    assert early["start"] == "2026-10-16T07:00:00+08:00"
    assert early["end"] is None
    assert blocks(out, "early-commitment-unknown-tail")
    assert not blocks(out, "early-commitment-fixed")
