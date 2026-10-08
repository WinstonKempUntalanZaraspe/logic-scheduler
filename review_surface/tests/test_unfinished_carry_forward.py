from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import Task
from app import scheduler
from app.temporal_engine_core import resolve_date_reference
from app.unfinished_carry_forward import compile_unfinished_carry_forward
from app.final_task_state_guard import (
    _apply_calendar_date_boundary,
    _persistent_future_window,
    _task_plan_window,
)

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 8, 18, 37, tzinfo=TZ)
CFG = {"wake_time": "07:00", "sleep_start": "23:00"}


def base():
    text = "I didn't finish all my tasks tdy, can you reschedule them tmr?"
    return {
        "tasks": [
            {
                "action": "create",
                "title": "I didn't finish all my tasks today can you reschedule them tomorrow",
                "line": text,
                "meta_patch": {},
            }
        ],
        "context": None,
        "notes": [],
        "warnings": [],
        "clarifications": [],
        "intents": [],
        "minimum_horizon_days": 1,
    }


def row(
    tid,
    title,
    *,
    start=None,
    end=None,
    status=0,
    tags=None,
    kind="TEXT",
    meta=None,
    repeat=None,
    all_day=False,
    priority=0,
):
    return {
        "id": tid,
        "project_id": "p",
        "title": title,
        "start": start.isoformat() if isinstance(start, datetime) else start,
        "end": end.isoformat() if isinstance(end, datetime) else end,
        "status": status,
        "tags": list(tags or []),
        "kind": kind,
        "project_kind": "TASK",
        "meta": dict(meta or {}),
        "repeat_flag": repeat,
        "repeat": repeat,
        "is_all_day": all_day,
        "priority": priority,
    }


def today_rows():
    return [
        row(
            "math",
            "Math practice",
            start=NOW.replace(hour=15, minute=0),
            end=NOW.replace(hour=16, minute=0),
            meta={"remaining_minutes": 75, "dependencies": ["read"]},
            priority=5,
        ),
        row(
            "physics",
            "Physics problems",
            start=NOW.replace(hour=16, minute=0),
            end=NOW.replace(hour=17, minute=0),
            meta={"remaining_minutes": 60},
            priority=3,
        ),
        row(
            "fixed",
            "Church",
            start=NOW.replace(hour=17, minute=0),
            end=NOW.replace(hour=18, minute=0),
            tags=["fixed"],
            meta={"remaining_minutes": 60},
        ),
        row(
            "note",
            "Physics notes",
            start=NOW.replace(hour=13, minute=0),
            end=NOW.replace(hour=14, minute=0),
            kind="NOTE",
            meta={"remaining_minutes": 45},
        ),
        row(
            "wont",
            "Won't Do task",
            start=NOW.replace(hour=12, minute=0),
            end=NOW.replace(hour=13, minute=0),
            status=-1,
            meta={"remaining_minutes": 30},
        ),
        row(
            "done",
            "Completed task",
            start=NOW.replace(hour=10, minute=0),
            end=NOW.replace(hour=11, minute=0),
            status=2,
            meta={"remaining_minutes": 30},
        ),
        row(
            "repeat",
            "Daily review",
            start=NOW.replace(hour=20, minute=0),
            end=NOW.replace(hour=20, minute=30),
            repeat="RRULE:FREQ=DAILY",
            meta={"remaining_minutes": 30},
        ),
        row(
            "all-day",
            "Due today",
            start=NOW.replace(hour=0, minute=0),
            end=(NOW + timedelta(days=1)).replace(hour=0, minute=0),
            all_day=True,
            meta={"remaining_minutes": 30},
        ),
        row(
            "future",
            "Tomorrow task",
            start=(NOW + timedelta(days=1)).replace(hour=9, minute=0),
            end=(NOW + timedelta(days=1)).replace(hour=10, minute=0),
            meta={"remaining_minutes": 60},
        ),
    ]


def updates(result):
    return {
        str(change.get("task_id")): change
        for change in result.get("tasks") or []
        if change.get("action") == "update"
    }


def test_tomorrow_carries_only_open_flexible_unfinished_today():
    result = compile_unfinished_carry_forward(base(), base()["tasks"][0]["line"], today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    assert not any(change.get("action") == "create" for change in result["tasks"])

    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    for tid in ("math", "physics"):
        patch = changed[tid]["meta_patch"]
        assert patch["earliest"].startswith(tomorrow + "T07:00")
        assert patch["latest_end"].startswith(tomorrow + "T23:00")
        assert result["context"]["intent_date_goals"][tid] == tomorrow
        assert result["context"]["intent_date_windows"][tid]["start"] == tomorrow
        assert result["context"]["intent_date_windows"][tid]["end"] == tomorrow

    # Carry-forward changes timing only; dependency semantics remain stored on source metadata.
    assert "dependencies" not in changed["math"]["meta_patch"]
    assert result["minimum_horizon_days"] >= 2


def test_cant_finish_some_tasks_next_week_keeps_today_then_spills_only_into_next_week():
    text = "I can't finish some of my tasks today, can you reschedule the unfinished ones next week?"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}

    # Physics is the lower-priority leaf and is guaranteed to move. Math stays
    # eligible today first and can spill to next week if today's capacity runs out.
    assert changed["physics"]["meta_patch"]["earliest"].startswith("2026-10-12T07:00")
    assert changed["physics"]["meta_patch"]["latest_end"].startswith("2026-10-18T23:00")
    assert changed["physics"]["meta_patch"]["carry_forward_mode"] == "forced"
    assert changed["math"]["meta_patch"]["earliest"].startswith("2026-10-08T18:37")
    assert changed["math"]["meta_patch"]["latest_end"].startswith("2026-10-18T23:00")
    assert changed["math"]["meta_patch"]["timing"] == "asap"
    assert changed["math"]["meta_patch"]["carry_forward_target_start"] == "2026-10-12"
    assert changed["math"]["meta_patch"]["carry_forward_target_end"] == "2026-10-18"
    assert changed["math"]["meta_patch"]["carry_forward_mode"] == "overflow"

    ctx = result["context"]
    assert ctx["intent_date_windows"]["physics"]["start"] == "2026-10-12"
    assert ctx["intent_date_windows"]["physics"]["end"] == "2026-10-18"
    assert ctx["intent_date_windows"]["math"]["start"] == "2026-10-08"
    assert ctx["intent_date_windows"]["math"]["target_start"] == "2026-10-12"
    assert ctx["intent_date_windows"]["math"]["end"] == "2026-10-18"
    assert ctx["unfinished_carry_forward"]["mode"] == "optimizer-overflow"
    assert ctx["unfinished_carry_forward"]["minimum_deferred_task_ids"] == ["physics"]
    assert ctx["unfinished_carry_forward"]["overflow_candidate_ids"] == ["math"]

    excluded = {
        item["date"]
        for item in ctx["intent_exclusions"]
        if "math" in item["task_ids"]
    }
    assert excluded == {"2026-10-09", "2026-10-10", "2026-10-11"}
    assert any("optimizer keeps useful unfinished work today" in note for note in result["notes"])


def test_next_month_is_real_calendar_range_and_keeps_interactive_horizon_bounded():
    text = "I didn't finish all my tasks today, move the unfinished work to next month"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    for change in changed.values():
        assert change["meta_patch"]["earliest"].startswith("2026-11-01T07:00")
        assert change["meta_patch"]["latest_end"].startswith("2026-11-30T23:00")
    ctx = result["context"]
    assert ctx["unfinished_carry_forward"]["full_window_beyond_interactive_horizon"] is True
    assert result["minimum_horizon_days"] == 14
    assert any("rolling optimizer" in note for note in result["notes"])


def test_named_weekday_is_exact_target_day():
    text = "I didn't finish my tasks today; push the remaining work to Tuesday"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    assert all(change["meta_patch"]["earliest"].startswith("2026-10-13T07:00") for change in changed.values())
    assert result["context"]["intent_date_goals"]["math"] == "2026-10-13"


def test_some_to_tomorrow_guarantees_one_move_but_keeps_other_work_today_first():
    text = "I didn't finish some of my tasks today, reschedule them tomorrow"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    assert changed["physics"]["meta_patch"]["earliest"].startswith("2026-10-09T07:00")
    assert changed["physics"]["meta_patch"]["carry_forward_mode"] == "forced"
    assert changed["math"]["meta_patch"]["earliest"].startswith("2026-10-08T18:37")
    assert changed["math"]["meta_patch"]["latest_end"].startswith("2026-10-09T23:00")
    assert changed["math"]["meta_patch"]["timing"] == "asap"
    assert result["context"]["intent_date_goals"]["physics"] == "2026-10-09"
    assert "math" not in result["context"]["intent_date_goals"]
    assert result["context"].get("intent_exclusions", []) == []


def test_split_cant_finish_clause_clears_only_resolved_needs_input():
    parsed = base()
    parsed["clarifications"] = [
        {"text": "I can't finish some of my tasks today", "reason": "Ambiguous capability statement"},
        {"text": "Unrelated question", "reason": "Keep this one"},
    ]
    parsed["intents"] = [
        {"kind": "ambiguous", "text": "I can't finish some of my tasks today", "status": "needs-input"},
        {"kind": "ambiguous", "text": "Unrelated question", "status": "needs-input"},
    ]
    text = "I can't finish some of my tasks today; can you reschedule them tomorrow?"
    result = compile_unfinished_carry_forward(parsed, text, today_rows(), CFG, NOW)
    assert [x["text"] for x in result["clarifications"]] == ["Unrelated question"]
    assert any(x.get("text") == "Unrelated question" and x.get("status") == "needs-input" for x in result["intents"])
    assert any(x.get("semantic") == "unfinished-carry-forward" and x.get("status") == "compiled" for x in result["intents"])


def test_monday_alias_is_exact_next_monday():
    text = "I didn't finish all my tasks today, reschedule them Monday"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    assert all(change["meta_patch"]["earliest"].startswith("2026-10-12T07:00") for change in changed.values())
    assert result["context"]["intent_date_goals"]["math"] == "2026-10-12"


def test_source_today_after_move_verb_does_not_beat_explicit_destination():
    text = "Can you reschedule the tasks I didn't finish today to tomorrow?"
    result = compile_unfinished_carry_forward(base(), text, today_rows(), CFG, NOW)
    changed = updates(result)
    assert set(changed) == {"math", "physics"}
    assert all(change["meta_patch"]["earliest"].startswith("2026-10-09T07:00") for change in changed.values())
    assert result["context"]["intent_date_goals"]["math"] == "2026-10-09"


def test_no_proven_today_scope_fails_closed_without_moving_backlog():
    rows = [
        row("undated", "General backlog", meta={"remaining_minutes": 90}),
        row(
            "future",
            "Future task",
            start=(NOW + timedelta(days=2)).replace(hour=9, minute=0),
            end=(NOW + timedelta(days=2)).replace(hour=10, minute=0),
            meta={"remaining_minutes": 60},
        ),
    ]
    text = "I didn't finish all my tasks today, reschedule them tomorrow"
    result = compile_unfinished_carry_forward(base(), text, rows, CFG, NOW)
    assert updates(result) == {}
    assert any("could not prove which active flexible tasks belonged to today's unfinished plan" in w for w in result["warnings"])


def test_temporal_engine_parses_plain_next_month_as_calendar_range():
    ref = resolve_date_reference("next month", NOW)
    assert ref is not None
    assert ref.start_date.isoformat() == "2026-11-01"
    assert ref.end_date.isoformat() == "2026-11-30"
    assert ref.precision == "range"


def test_persisted_future_window_survives_after_original_quick_context_expires():
    source_day = datetime(2026, 10, 8, 15, 0, tzinfo=TZ)
    task = Task(
        "math",
        "p",
        "Math practice",
        start=source_day,
        end=source_day + timedelta(hours=1),
    )
    planning_start = datetime(2026, 11, 1, 7, 0, tzinfo=TZ)
    meta = {
        "math": {
            "duration_minutes": 75,
            "remaining_minutes": 75,
            "earliest": "2026-11-01T07:00:00+08:00",
            "latest_end": "2026-11-30T23:00:00+08:00",
        }
    }
    assert _persistent_future_window(task, meta, planning_start) == (
        datetime(2026, 11, 1).date(),
        datetime(2026, 11, 30).date(),
    )
    kept, scoped, excluded, horizon, start_day, end_day = _apply_calendar_date_boundary(
        [task],
        meta,
        planning_start,
        14,
        CFG,
        scheduler,
    )
    assert kept == [task]
    assert excluded == []
    assert scoped["math"]["earliest"].startswith("2026-11-01T07:00")
    assert scoped["math"]["latest_end"].startswith("2026-11-30T23:00")
    assert start_day.isoformat() == "2026-11-01"
    assert end_day.isoformat() == "2026-11-14"


def test_final_calendar_guard_does_not_clip_carried_task_back_to_old_today_date():
    task = Task(
        "math",
        "p",
        "Math practice",
        start=NOW.replace(hour=15, minute=0),
        end=NOW.replace(hour=16, minute=0),
    )
    config = {
        **CFG,
        "_quick_context": {
            "date": NOW.date().isoformat(),
            "replan_scope": "today",
            "minimum_horizon_days": 11,
            "intent_date_windows": {
                "math": {
                    "start": "2026-10-12",
                    "end": "2026-10-18",
                    "source": "unfinished-carry-forward",
                }
            },
        },
    }
    assert _task_plan_window(task, config, NOW) == (
        datetime(2026, 10, 12).date(),
        datetime(2026, 10, 18).date(),
    )
    kept, scoped, excluded, horizon, start_day, end_day = _apply_calendar_date_boundary(
        [task],
        {
            "math": {
                "duration_minutes": 75,
                "remaining_minutes": 75,
                "earliest": "2026-10-12T07:00:00+08:00",
                "latest_end": "2026-10-18T23:00:00+08:00",
            }
        },
        NOW,
        11,
        config,
        scheduler,
    )
    assert kept == [task]
    assert excluded == []
    assert horizon == 11
    assert start_day.isoformat() == "2026-10-08"
    assert end_day.isoformat() == "2026-10-18"
    assert scoped["math"]["earliest"].startswith("2026-10-12T07:00")
    assert scoped["math"]["latest_end"].startswith("2026-10-18T23:00")



def test_persisted_overflow_destination_uses_future_target_not_broad_today_window():
    task = Task(
        "math",
        "p",
        "Math practice",
        start=NOW.replace(hour=15, minute=0),
        end=NOW.replace(hour=16, minute=0),
    )
    meta = {
        "math": {
            "remaining_minutes": 75,
            "earliest": "2026-10-08T18:37:00+08:00",
            "latest_end": "2026-11-30T23:00:00+08:00",
            "carry_forward_target_start": "2026-11-01",
            "carry_forward_target_end": "2026-11-30",
            "carry_forward_mode": "overflow",
        }
    }
    assert _persistent_future_window(
        task,
        meta,
        datetime(2026, 10, 20, 7, 0, tzinfo=TZ),
    ) == (
        datetime(2026, 11, 1).date(),
        datetime(2026, 11, 30).date(),
    )
    # The full last day remains valid; date-only persistence must not expire at 00:00.
    assert _persistent_future_window(
        task,
        meta,
        datetime(2026, 11, 30, 20, 0, tzinfo=TZ),
    ) == (
        datetime(2026, 11, 1).date(),
        datetime(2026, 11, 30).date(),
    )
