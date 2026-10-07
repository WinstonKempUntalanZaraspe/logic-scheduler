from datetime import datetime
from zoneinfo import ZoneInfo

from app.production_reliability_patch import (
    compile_definite_timed_event,
    looks_like_definite_timed_event,
)
from app.forward_create_plan_patch import forward_only_payload
from app.fresh_day_runtime_patch import fresh_runtime_context
from app.timed_event_unicode_compat import install_timed_event_unicode_compat

# Production installs this once at startup. Install it here too so the direct compiler
# regressions exercise the exact punctuation behavior used by Quick Dump.
install_timed_event_unicode_compat()

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 5, 23, 0, tzinfo=TZ)
CFG = {"wake_time": "07:00", "sleep_start": "23:30"}


def base_parsed(text, *, tasks=None):
    return {
        "tasks": list(tasks or []),
        "context": {"date": NOW.date().isoformat(), "source": "quick-dump"},
        "clarifications": [{"text": text, "reason": "Unclear whether this is work or a planning instruction; no task created."}],
        "warnings": [],
        "notes": [],
        "intents": [{"text": text, "kind": "ambiguous", "status": "needs-input"}],
    }


def test_exact_hangout_prompt_becomes_one_fixed_commitment_without_clarification():
    text = "I'm hanging out with friends tomorrow from 6pm–9pm. Replan around it."
    assert looks_like_definite_timed_event(text, NOW)
    out = compile_definite_timed_event(base_parsed(text), text, [], CFG, NOW)
    assert out["clarifications"] == []
    assert len(out["tasks"]) == 1
    change = out["tasks"][0]
    assert change["action"] == "create"
    assert change["title"] == "Hang out with friends"
    assert change["tags_add"] == ["fixed"]
    assert change["fixed_start"].startswith("2026-10-06T18:00")
    assert change["fixed_end"].startswith("2026-10-06T21:00")
    assert out["context"]["replan_requested"] is True
    assert out["context"]["minimum_horizon_days"] >= 2


def test_party_prompt_keeps_leave_and_return_as_logistics_not_second_task():
    text = "Party tomorrow 7pm–11pm at Clarke Quay. I’ll leave home around 6pm and probably be back by midnight."
    parsed = base_parsed(text, tasks=[{
        "action": "create",
        "title": "leave home around 6pm and probably be back by midnight",
        "line": text,
    }])
    out = compile_definite_timed_event(parsed, text, [], CFG, NOW)
    assert len(out["tasks"]) == 1
    change = out["tasks"][0]
    assert change["title"] == "Party at Clarke Quay"
    assert change["fixed_start"].startswith("2026-10-06T19:00")
    assert change["fixed_end"].startswith("2026-10-06T23:00")
    blocks = out["context"].get("temporary_blocks") or []
    assert any(str(b["start"]).startswith("2026-10-06T18:00") and str(b["end"]).startswith("2026-10-06T19:00") for b in blocks)
    assert any(str(b["start"]).startswith("2026-10-06T23:00") and str(b["end"]).startswith("2026-10-07T00:00") for b in blocks)
    assert all(b.get("source") == "human-reality-explicit-event-logistics" for b in blocks)




def test_exact_timed_chore_is_a_fixed_commitment_not_flexible_work():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    text = "I will sweep the floor from 10:30pm to 10:50pm"
    assert looks_like_definite_timed_event(text, now)
    out = compile_definite_timed_event(base_parsed(text), text, [], CFG, now)
    assert out["clarifications"] == []
    assert len(out["tasks"]) == 1
    change = out["tasks"][0]
    assert change["action"] == "create"
    assert change["title"].lower() == "sweep the floor"
    assert change["tags_add"] == ["fixed"]
    assert change["fixed_start"] == "2026-10-07T22:30:00+08:00"
    assert change["fixed_end"] == "2026-10-07T22:50:00+08:00"
    assert change["meta_patch"]["autoschedule"] is False
    assert out["context"]["replan_requested"] is True


def test_exact_timed_atomic_study_is_fixed_but_tentative_study_is_not():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    assert looks_like_definite_timed_event("I will study calculus from 8pm to 9pm", now)
    assert not looks_like_definite_timed_event("Maybe study calculus from 8pm to 9pm", now)




def test_timed_availability_and_recovery_are_not_promoted_to_ticktick_tasks():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    assert not looks_like_definite_timed_event('I am available from 8pm to 9pm', now)
    assert not looks_like_definite_timed_event('I will nap from 8pm to 9pm', now)
    assert not looks_like_definite_timed_event('I will eat dinner from 8pm to 8:45pm', now)


def test_tentative_social_event_is_not_promoted_to_hard_commitment():
    text = "Maybe meet friends tomorrow from 6pm-9pm."
    original = base_parsed(text)
    assert not looks_like_definite_timed_event(text, NOW)
    assert compile_definite_timed_event(original, text, [], CFG, NOW) == original


def test_existing_event_is_reused_instead_of_duplicated():
    text = "Party tomorrow 7pm–11pm at Clarke Quay."
    rows = [{"id": "party-1", "title": "Party at Clarke Quay", "status": 0, "kind": "TEXT", "tags": []}]
    out = compile_definite_timed_event(base_parsed(text), text, rows, CFG, NOW)
    assert len(out["tasks"]) == 1
    assert out["tasks"][0]["action"] == "update"
    assert out["tasks"][0]["task_id"] == "party-1"
    assert "fixed" in out["tasks"][0]["tags_add"]


def test_two_day_preview_hides_elapsed_today_rows_and_drops_empty_today_capacity():
    payload = {
        "generated_at": "2026-10-05T23:00:00+08:00",
        "segments": [
            {"task_id": "old", "title": "Old work", "start": "2026-10-05T17:00:00+08:00", "end": "2026-10-05T18:00:00+08:00"},
            {"task_id": "tomorrow", "title": "Tomorrow work", "start": "2026-10-06T10:00:00+08:00", "end": "2026-10-06T11:00:00+08:00"},
        ],
        "diagnostics": {
            "fixed_timeline": [{"label": "Old event", "start": "2026-10-05T16:00:00+08:00", "end": "2026-10-05T17:00:00+08:00"}],
            "reality_timeline": [],
            "capacity": [
                {"date": "2026-10-05", "scheduled_minutes": 60, "free_minutes": 100},
                {"date": "2026-10-06", "scheduled_minutes": 60, "free_minutes": 500},
            ],
        },
    }
    out = forward_only_payload(payload)
    assert [s["task_id"] for s in out["segments"]] == ["tomorrow"]
    assert out["diagnostics"]["fixed_timeline"] == []
    assert [c["date"] for c in out["diagnostics"]["capacity"]] == ["2026-10-06"]
    assert out["diagnostics"]["forward_preview"]["hidden_elapsed_rows"] == 1


def test_new_day_discards_yesterdays_planning_story_and_future_temporary_blocks():
    old = {
        "date": "2026-10-05",
        "tomorrow_plan": {"date": "2026-10-06"},
        "intent_date_goals": {"swim": "2026-10-06"},
        "after_meal_task_ids": {"swim": "breakfast"},
        "plan_local_dependencies": {"math": ["swim"]},
        "energy_scale": 0.5,
        "temporary_blocks": [{
            "label": "Old planned outing",
            "start": "2026-10-06T18:00:00+08:00",
            "end": "2026-10-06T21:00:00+08:00",
            "source": "human-reality-planned",
        }],
    }
    assert fresh_runtime_context(old, datetime(2026, 10, 6, 7, 0, tzinfo=TZ)) is None


def test_only_reality_literally_active_across_midnight_survives_new_day():
    old = {
        "date": "2026-10-05",
        "tomorrow_plan": {"date": "2026-10-06"},
        "intent_date_goals": {"math": "2026-10-06"},
        "temporary_blocks": [{
            "label": "Sleep",
            "kind": "sleep",
            "start": "2026-10-05T23:30:00+08:00",
            "end": "2026-10-06T07:30:00+08:00",
            "source": "personal-current-activity",
            "location": "home",
        }],
    }
    out = fresh_runtime_context(old, datetime(2026, 10, 6, 7, 0, tzinfo=TZ))
    assert out is not None
    assert out["activity_state"] == "sleeping"
    assert out["current_location"] == "home"
    assert "tomorrow_plan" not in out
    assert "intent_date_goals" not in out
    assert len(out["temporary_blocks"]) == 1
