from datetime import datetime, timedelta, timezone

from app.config import apply_timezone
from app.models import Segment, Task, TaskMeta
import app.scheduler as sched

UTC = timezone.utc


def cfg():
    return {
        "day_start": "07:00", "day_end": "23:00",
        "wake_time": "07:00", "sleep_start": "23:00",
        "meals": [], "max_deep_work_minutes": 240,
        "between_chunks_buffer": 10, "target_utilization": 0.9,
        "min_daily_slack_minutes": 0, "candidate_step_minutes": 10,
        "max_candidates_per_chunk": 80, "solver_time_limit_seconds": 3,
        "solver_workers": 2, "default_travel_buffer_minutes": 0,
        "weekly_capacity_minutes": {},
    }


def test_zero_remaining_prerequisite_is_satisfied():
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    prerequisite = Task("a", "p", "Prerequisite", priority=3)
    dependent = Task("b", "p", "Dependent", priority=3)
    meta = {
        "a": {"duration_minutes": 60, "remaining_minutes": 0, "confidence": "high"},
        "b": {"duration_minutes": 60, "confidence": "high", "dependencies": ["a"]},
    }
    segments, warnings, _ = sched.plan([prerequisite, dependent], meta, [], start, 1, cfg(), {})
    assert {s.task_id for s in segments} == {"b"}
    assert not any("waiting for unfinished" in w for w in warnings)


def test_utc_start_uses_local_calendar_day():
    apply_timezone("Asia/Singapore")
    try:
        # 20:00 UTC Jan 7 == 04:00 Singapore Jan 8. Scheduler should use Jan 8 awake window.
        start = datetime(2030, 1, 7, 20, 0, tzinfo=UTC)
        task = Task("w", "p", "Work", priority=3)
        segments, warnings, _ = sched.plan(
            [task], {"w": {"duration_minutes": 60, "confidence": "high"}},
            [], start, 1, cfg(), {}
        )
        assert segments, warnings
        assert segments[0].start.date().isoformat() == "2030-01-08"
        assert segments[0].start.hour == 7
        assert segments[0].start.utcoffset() == timedelta(hours=8)
    finally:
        apply_timezone("UTC")


def test_unknown_cpsat_falls_back_to_heuristic(monkeypatch):
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    task = Task("w", "p", "Work", priority=3)
    monkeypatch.setattr(
        sched, "_plan_cpsat",
        lambda *a, **k: ([], ["CP-SAT returned UNKNOWN; no schedule was committed"],
                         {"engine": "cp-sat", "status": "UNKNOWN"}),
    )
    segments, warnings, diagnostics = sched.plan(
        [task], {"w": {"duration_minutes": 60, "confidence": "high"}},
        [], start, 1, cfg(), {}
    )
    assert segments
    assert diagnostics.get("fallback_from") == "cp-sat:UNKNOWN"
    assert any("heuristic planner" in w for w in warnings)


def test_feasible_cpsat_is_not_replaced_by_more_low_value_minutes(monkeypatch):
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    urgent = Task("urgent", "p", "Urgent deadline work", priority=5)
    filler = Task("filler", "p", "Low priority filler", priority=0)
    cp = [Segment("urgent", "p", urgent.title, start, start + timedelta(minutes=60), 100.0, "cp", urgent)]
    heur = [Segment("filler", "p", filler.title, start, start + timedelta(minutes=120), 1.0, "heur", filler)]
    monkeypatch.setattr(
        sched, "_plan_cpsat",
        lambda *a, **k: (list(cp), [], {"engine": "cp-sat", "status": "FEASIBLE"}),
    )
    monkeypatch.setattr(
        sched, "_plan_heuristic",
        lambda *a, **k: (list(heur), [], {"engine": "heuristic-fallback", "ortools": False}),
    )
    segments, _, diagnostics = sched.plan(
        [urgent, filler],
        {
            "urgent": {"duration_minutes": 60, "confidence": "high", "must_finish": True},
            "filler": {"duration_minutes": 120, "confidence": "high"},
        },
        [], start, 1, cfg(), {}
    )
    assert {s.task_id for s in segments} == {"urgent"}
    assert "fallback_from" not in diagnostics


def test_duplicate_ids_are_collapsed_before_solving():
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    abandoned = Task("same", "p", "Stale copy", status=-1, priority=1)
    active = Task("same", "p", "Live copy", status=0, priority=5)
    segments, warnings, _ = sched.plan(
        [abandoned, active],
        {"same": {"duration_minutes": 60, "confidence": "high"}},
        [], start, 1, cfg(), {}
    )
    assert sum(int((s.end - s.start).total_seconds() // 60) for s in segments) == 60
    assert all(s.title == "Live copy" for s in segments)
    assert any("Duplicate task id" in w for w in warnings)


def test_max_chunk_remains_a_hard_ceiling():
    chunks = sched.choose_chunks(50, TaskMeta(task_id="x", min_chunk=30, max_chunk=45))
    assert max(chunks) <= 45
