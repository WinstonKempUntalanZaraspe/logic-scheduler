"""PRODUCTION-ADAPTED copy for autoscheduler-pro (app/planning_gaps.py + app/scheduler.py candidate patch).

Gap classification as a VERIFIED scheduling invariant.

Each test pins a confirmed failure (reproduced against the original code) and the
general rule that replaces it. Nothing here encodes task names: the scenarios use
arbitrary titles and the assertions are about time geometry.

  1. Remaining effort of a prerequisite may never be placed after a dependent has started.
  2. Remaining effort may use time BEFORE the task's own later sessions.
  3. A moved flexible meal never shrinks legal windows (one blocker source of truth).
  4. A gap shorter than 15 minutes is still shown when real work fits in it.
  5. Relevance is judged on the LOGICAL day (bedtime after midnight).
  6. CASE A is only reported when a concrete legal session can be built (a witness).
  7. The final boundary repairs CASE A without breaking any hard invariant.
"""
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import scheduler as S
from app.config import apply_timezone, settings
from app.interactive_quality_speed_patch import _productive_fill
from app.models import BusyBlock, Segment, Task
from app.planning_gaps import rebuild_final_gaps

TZ = ZoneInfo("Asia/Singapore")
FRI = datetime(2026, 10, 9, 7, 0, tzinfo=TZ)
BASE = {
    "wake_time": "07:00", "sleep_start": "23:00", "meals": [], "between_chunks_buffer": 10,
    "max_deep_work_minutes": 600, "candidate_step_minutes": 10, "max_candidates_per_chunk": 60,
    "solver_time_limit_seconds": 2, "solver_workers": 2, "weekly_capacity_minutes": {},
    "maximize_productive_time": True, "target_utilization": 1.0, "min_daily_slack_minutes": 0,
}


@pytest.fixture(autouse=True)
def _singapore():
    previous = settings.timezone
    apply_timezone("Asia/Singapore")
    yield
    apply_timezone(previous)


def at(hour, minute=0, day=0):
    base = FRI + timedelta(days=day)
    extra = timedelta(days=1) if hour >= 24 else timedelta()
    return base.replace(hour=hour % 24, minute=minute) + extra


def seg(task, a, b):
    return Segment(task.id, task.project_id, task.title, a, b, 1.0, "test", task)


def windows(task, meta, tasks, segments, config=BASE, horizon=1):
    return S._free_task_windows(task, meta, tasks, [], FRI, horizon, config, segments)


def gaps(tasks, meta, segments, config=BASE, horizon=1, extra=None):
    # Production's final guard hands rebuild_final_gaps the planner's unfinished-work rows;
    # the candidate patch refreshes their legal windows instead of re-deriving the rows
    # (production intentionally drops dependency-held work from that list).
    rows = S._remaining_work(tasks, meta, segments)
    by_id = {t.id: t for t in tasks}
    for row in rows:  # exactly what the planner attaches (scheduler.plan / contract_fill)
        row["legal_windows"] = [{"start": a.isoformat(), "end": b.isoformat()} for a, b in
                                S._free_task_windows(by_id[row["task_id"]], meta, tasks, [], FRI, horizon, config, segments)]
    diagnostics = {"unfinished_work": rows, **(extra or {})}
    out = rebuild_final_gaps(segments, diagnostics, tasks, [], FRI, horizon, config, meta)
    return out["planning_gaps"]


def gap_at(rows, a, b):
    return next(g for g in rows if g["start"] == a.isoformat() and g["end"] == b.isoformat())


# 1 ---------------------------------------------------------------------------------------
def test_prerequisite_remainder_never_runs_after_a_scheduled_dependent():
    pre = Task("pre", "p", "Prerequisite work")
    dep = Task("dep", "p", "Dependent work")
    meta = {"pre": {"duration_minutes": 120, "confidence": "high", "min_chunk": 30, "max_chunk": 60},
            "dep": {"duration_minutes": 60, "confidence": "high", "dependencies": ["pre"]}}
    segments = [seg(pre, at(8), at(9)), seg(dep, at(9, 10), at(10, 10))]
    for a, b in windows(pre, meta, [pre, dep], segments):
        assert b <= at(9, 10) - timedelta(minutes=10), (a, b)
    rows = gaps([pre, dep], meta, segments)
    after = gap_at(rows, at(10, 10), at(23))
    assert after["productivity_case"] == "CONSTRAINED"
    assert "must finish before Dependent work" in after["reason"]


def test_session_level_dependency_does_not_cap_the_remainder():
    pre = Task("pre", "p", "Prerequisite work")
    dep = Task("dep", "p", "Needs only one session")
    meta = {"pre": {"duration_minutes": 120, "confidence": "high", "min_chunk": 30, "max_chunk": 60},
            "dep": {"duration_minutes": 60, "confidence": "high", "dependencies": ["pre"],
                    "_session_dependency_ids": ["pre"]}}
    segments = [seg(pre, at(8), at(9)), seg(dep, at(9, 10), at(10, 10))]
    assert any(b > at(10, 10) for _, b in windows(pre, meta, [pre, dep], segments))


# 2 ---------------------------------------------------------------------------------------
def test_remaining_effort_may_run_before_the_tasks_own_later_session():
    essay = Task("essay", "p", "Long draft")
    meta = {"essay": {"duration_minutes": 180, "confidence": "high", "min_chunk": 30, "max_chunk": 60}}
    segments = [seg(essay, at(16), at(17))]
    legal = windows(essay, meta, [essay], segments)
    assert any(a <= at(7) and b >= at(15, 45) for a, b in legal)
    for a, b in legal:  # own session keeps its between-session buffer on both sides
        assert b <= at(15, 50) or a >= at(17, 10)
    morning = gap_at(gaps([essay], meta, segments), at(7), at(16))
    assert morning["productivity_case"] == "A"


def test_first_session_constraints_still_force_remainder_after_own_sessions():
    task = Task("t", "p", "Work with a first-session deadline")
    meta = {"t": {"duration_minutes": 120, "confidence": "high", "min_chunk": 30, "max_chunk": 60,
                  "_initial_latest_end": at(18).isoformat()}}
    segments = [seg(task, at(16), at(17))]
    assert all(a >= at(17, 10) for a, _ in windows(task, meta, [task], segments))


# 3 ---------------------------------------------------------------------------------------
def test_moved_flexible_meal_does_not_shrink_legal_windows():
    config = dict(BASE, meals=[{"name": "Lunch", "start": "12:30", "minutes": 45}])
    report = Task("report", "p", "Unsplittable long block")
    later = Task("later", "p", "Afternoon commitment")
    meta = {"report": {"duration_minutes": 340, "confidence": "high", "splittable": False},
            "later": {"duration_minutes": 555, "confidence": "high", "splittable": False}}
    segments = [seg(later, at(13, 45), at(23))]
    flexible = {"flexible_meals": [{"name": "Lunch", "start": at(13).isoformat(), "end": at(13, 45).isoformat()}]}
    morning = gap_at(gaps([report, later], meta, segments, config, extra=flexible), at(7), at(13))
    assert morning["productivity_case"] == "A", morning["reason"]


def test_real_meal_reservation_still_protects_legal_windows():
    config = dict(BASE, meals=[{"name": "Lunch", "start": "12:30", "minutes": 45}])
    report = Task("report", "p", "Unsplittable long block")
    meta = {"report": {"duration_minutes": 340, "confidence": "high", "splittable": False}}
    rows = gaps([report], meta, [], config)
    for gap in rows:
        a, b = datetime.fromisoformat(gap["start"]), datetime.fromisoformat(gap["end"])
        assert b <= at(12, 30) or a >= at(13, 15)


# 4 ---------------------------------------------------------------------------------------
def test_short_gap_is_reported_when_a_short_task_fits():
    short = Task("short", "p", "Ten-minute item")
    block = Task("block", "p", "Long fixed-size block")
    rest = Task("rest", "p", "Afternoon block")
    meta = {"short": {"duration_minutes": 10, "confidence": "high", "min_chunk": 10},
            "block": {"duration_minutes": 300, "confidence": "high", "splittable": False},
            "rest": {"duration_minutes": 648, "confidence": "high", "splittable": False}}
    segments = [seg(block, at(7), at(12)), seg(rest, at(12, 12), at(23))]
    rows = gaps([short, block, rest], meta, segments)
    tiny = gap_at(rows, at(12), at(12, 12))
    assert tiny["productivity_case"] == "A"


def test_short_gap_with_nothing_that_fits_stays_hidden():
    block = Task("block", "p", "Long block")
    meta = {"block": {"duration_minutes": 300, "confidence": "high", "splittable": False}}
    segments = [seg(block, at(7), at(12)), seg(Task("x", "p", "Other"), at(12, 12), at(23))]
    assert not [g for g in gaps([block], meta, segments) if g["minutes"] < 15]


# 5 ---------------------------------------------------------------------------------------
def test_gap_after_midnight_belongs_to_the_previous_logical_day():
    config = dict(BASE, sleep_start="01:30")
    late = Task("late", "p", "Late-evening work")
    filler = Task("filler", "p", "Day block")
    evening = Task("evening", "p", "Evening block")
    meta = {"late": {"duration_minutes": 60, "confidence": "high", "earliest": at(21).isoformat(),
                     "latest_end": at(25, 30).isoformat()},
            "filler": {"duration_minutes": 840, "confidence": "high", "splittable": False},
            "evening": {"duration_minutes": 180, "confidence": "high", "splittable": False}}
    segments = [seg(filler, at(7), at(21)), seg(evening, at(21), at(24))]
    tail = gap_at(gaps([late, filler, evening], meta, segments, config), at(24), at(25, 30))
    assert tail["productivity_case"] == "A"


# 6 ---------------------------------------------------------------------------------------
def test_every_case_a_gap_carries_a_constructible_witness():
    task = Task("t", "p", "Splittable work")
    meta = {"t": {"duration_minutes": 120, "confidence": "high", "min_chunk": 30, "max_chunk": 60}}
    rows = gaps([task], meta, [])
    case_a = [g for g in rows if g["productivity_case"] == "A"]
    assert case_a
    for gap in case_a:
        w = gap["witness"]
        a, b = datetime.fromisoformat(gap["start"]), datetime.fromisoformat(gap["end"])
        assert a <= datetime.fromisoformat(w["start"]) < datetime.fromisoformat(w["end"]) <= b
        assert w["minutes"] <= 60


def test_multichunk_must_finish_has_constructive_full_completion_witness():
    # Must-finish is atomic across *all chunks*, not necessarily one 150-minute
    # continuous session. This task legitimately fits as 60+60+30 with buffers.
    atomic = Task("atomic", "p", "All-or-nothing multi-session work")
    meta = {"atomic": {"duration_minutes": 150, "confidence": "high", "must_finish": True,
                       "min_chunk": 30, "max_chunk": 60}}
    rows = gaps([atomic], meta, [])
    assert any(g["productivity_case"] == "A" for g in rows)
    assert any(g.get("witness", {}).get("minutes") == 60 for g in rows)


def test_multichunk_must_finish_cannot_claim_case_a_without_full_completion():
    atomic = Task("atomic", "p", "Must-finish task too large for today's remaining capacity")
    meta = {"atomic": {"duration_minutes": 150, "confidence": "high", "must_finish": True,
                       "min_chunk": 30, "max_chunk": 60}}
    # 80 free minutes total before a fixed reservation: one chunk may fit but
    # *all* chunks cannot, so claiming CASE A would be misleading.
    blocking = BusyBlock(at(8, 20), at(23), "Real commitment", "fixed")
    rows = rebuild_final_gaps(
        [], {"unfinished_work": S._remaining_work([atomic], meta, [])},
        [atomic], [blocking], FRI, 1, BASE, meta
    )["planning_gaps"]
    assert rows
    assert all(g["productivity_case"] != "A" for g in rows)




def test_production_filler_never_schedules_a_prerequisite_after_its_dependent():
    pre = Task("pre", "p", "Prerequisite work")
    dep = Task("dep", "p", "Dependent work")
    meta = {"pre": {"duration_minutes": 120, "confidence": "high", "min_chunk": 30, "max_chunk": 60},
            "dep": {"duration_minutes": 60, "confidence": "high", "dependencies": ["pre"]}}
    segments = [seg(pre, at(8), at(9)), seg(dep, at(9, 10), at(10, 10))]
    out, _, _ = _productive_fill(S, [pre, dep], meta, [], FRI, 1, BASE, {}, (segments, [], {}))
    assert max(s.end for s in out if s.task_id == "pre") <= min(s.start for s in out if s.task_id == "dep")


def test_productive_filler_uses_moved_meal_not_old_configured_meal():
    config = dict(BASE, meals=[{"name": "Lunch", "start": "12:30", "minutes": 45}])
    report = Task("report", "p", "Long uninterrupted report")
    afternoon = Task("afternoon", "p", "Long fixed afternoon block")
    meta = {
        "report": {"duration_minutes": 340, "confidence": "high", "splittable": False},
        "afternoon": {"duration_minutes": 555, "confidence": "high", "splittable": False},
    }
    scheduled = [seg(afternoon, at(13, 45), at(23))]
    moved = {"flexible_meals": [
        {"name": "Lunch", "start": at(13).isoformat(), "end": at(13, 45).isoformat()},
    ]}
    result = _productive_fill(
        S, [report, afternoon], meta, [], FRI, 1, config, {},
        (scheduled, [], moved),
    )
    result_segments = result[0]
    report_segments = [s for s in result_segments if s.task_id == "report"]
    assert report_segments, "Stale 12:30 meal clock wrongly blocked the 340m morning task."
    assert all(s.end <= at(13) for s in report_segments)
    assert all(s.end <= at(13) or s.start >= at(13, 45) for s in result_segments)
