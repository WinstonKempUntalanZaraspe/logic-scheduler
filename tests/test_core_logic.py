from datetime import datetime, timedelta, timezone
from app.models import BusyBlock, Task
from app.scheduler import plan

UTC = timezone.utc

def cfg():
    return {
        "day_start":"07:00","day_end":"23:00","wake_time":"07:00","sleep_start":"23:00",
        "meals":[],"max_deep_work_minutes":240,"between_chunks_buffer":10,
        "target_utilization":0.9,"min_daily_slack_minutes":0,"candidate_step_minutes":10,
        "max_candidates_per_chunk":80,"solver_time_limit_seconds":3,"solver_workers":2,
        "default_travel_buffer_minutes":0,"weekly_capacity_minutes":{}
    }

def test_dependency_finishes_before_dependent_starts():
    start=datetime(2030,1,7,8,0,tzinfo=UTC)
    a=Task("a","p","Prerequisite",priority=2,tags=["deep-work"])
    b=Task("b","p","Dependent",priority=5,tags=["deep-work"])
    meta={"a":{"duration_minutes":60,"confidence":"high"},
          "b":{"duration_minutes":60,"confidence":"high","dependencies":["a"]}}
    segs,_,_=plan([a,b],meta,[],start,1,cfg(),{})
    by={s.task_id:s for s in segs}
    assert by["a"].end <= by["b"].start

def test_hard_busy_block_never_overlaps_work():
    start=datetime(2030,1,7,8,0,tzinfo=UTC)
    task=Task("work","p","Focused work",priority=5,tags=["deep-work"])
    block=BusyBlock(start+timedelta(hours=1),start+timedelta(hours=2),"Appointment")
    segs,_,_=plan([task],{"work":{"duration_minutes":120,"confidence":"high"}},[block],start,1,cfg(),{})
    assert segs
    assert all(s.end <= block.start or s.start >= block.end for s in segs)

def test_abandoned_and_note_items_never_schedule():
    start=datetime(2030,1,7,8,0,tzinfo=UTC)
    abandoned=Task("old","p","Abandoned",status=-1,priority=5)
    note=Task("note","p","Reference note",kind="NOTE",priority=5)
    live=Task("live","p","Real task",priority=1)
    meta={k:{"duration_minutes":30,"confidence":"high"} for k in ["old","note","live"]}
    segs,_,_=plan([abandoned,note,live],meta,[],start,1,cfg(),{})
    ids={s.task_id for s in segs}
    assert "old" not in ids and "note" not in ids and "live" in ids


# --- regression tests for the logic review fixes -------------------------------------

import pytest
from app.models import TaskMeta
from app.scheduler import choose_chunks, priority_score
from app.config import apply_timezone


def test_prerequisite_with_no_remaining_effort_does_not_block_dependent():
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    a = Task("a", "p", "Prerequisite", priority=3)
    b = Task("b", "p", "Dependent", priority=3)
    meta = {"a": {"duration_minutes": 60, "remaining_minutes": 0, "confidence": "high"},
            "b": {"duration_minutes": 60, "confidence": "high", "dependencies": ["a"]}}
    segs, warns, _ = plan([a, b], meta, [], start, 1, cfg(), {})
    assert {s.task_id for s in segs} == {"b"}
    assert not any("waiting for unfinished prerequisite" in w for w in warns)


@pytest.mark.parametrize("min_chunk,max_chunk", [(20, 45), (30, 45), (30, 60), (25, 90)])
def test_choose_chunks_never_leaves_fragment_below_min(min_chunk, max_chunk):
    for total in range(5, 361, 5):
        chunks = choose_chunks(total, TaskMeta(task_id="x", min_chunk=min_chunk, max_chunk=max_chunk))
        assert sum(chunks) == total
        if len(chunks) > 1:
            assert min(chunks) >= min_chunk, (total, chunks)


def test_overdue_task_outranks_imminent_deadline():
    now = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    t = Task("x", "p", "x", priority=3)
    imminent = priority_score(t, TaskMeta(task_id="x", deadline=now + timedelta(minutes=30)), now)
    overdue = priority_score(t, TaskMeta(task_id="x", deadline=now - timedelta(days=3)), now)
    assert overdue > imminent


def test_utc_start_is_planned_on_the_local_calendar_day():
    # 04:00 local (UTC+8) arrives as 20:00 UTC the previous day; the local day must still be used.
    apply_timezone("Asia/Singapore")
    try:
        start = datetime(2030, 1, 7, 20, 0, tzinfo=UTC)
        task = Task("w", "p", "Work", priority=3)
        segs, warns, _ = plan([task], {"w": {"duration_minutes": 60, "confidence": "high"}}, [], start, 1, cfg(), {})
        assert segs, warns
        assert segs[0].start.hour == 7 and segs[0].start.utcoffset() == timedelta(hours=8)
    finally:
        apply_timezone("UTC")


def test_naive_start_is_treated_as_local_instead_of_crashing():
    task = Task("w", "p", "Work", priority=3)
    segs, _, _ = plan([task], {"w": {"duration_minutes": 60, "confidence": "high"}},
                      [], datetime(2030, 1, 7, 8, 0), 1, cfg(), {})
    assert segs


def _stub_cpsat(monkeypatch, status, segments=()):
    import app.scheduler as sched

    def fake(*args, **kwargs):
        return list(segments), [f"CP-SAT returned {status}"], {"engine": "cp-sat", "status": status}

    monkeypatch.setattr(sched, "_plan_cpsat", fake)
    return sched


def test_solver_timeout_never_yields_an_empty_plan(monkeypatch):
    sched = _stub_cpsat(monkeypatch, "UNKNOWN")
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    task = Task("w", "p", "Work", priority=3)
    segs, warns, diag = sched.plan([task], {"w": {"duration_minutes": 60, "confidence": "high"}}, [], start, 1, cfg(), {})
    assert segs, "an empty plan should never be the result of a solver timeout"
    assert diag.get("fallback_from") == "cp-sat:UNKNOWN"
    assert any("heuristic planner" in w for w in warns)


def test_weak_feasible_solution_loses_to_much_better_baseline(monkeypatch):
    from app.models import Segment
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    tasks = [Task(f"t{i}", "p", f"T{i}", priority=3) for i in range(4)]
    meta = {t.id: {"duration_minutes": 60, "confidence": "high"} for t in tasks}
    weak = [Segment("t0", "p", "T0", start, start + timedelta(minutes=60), 1.0, "r", tasks[0])]
    sched = _stub_cpsat(monkeypatch, "FEASIBLE", weak)
    segs, warns, diag = sched.plan(tasks, meta, [], start, 1, cfg(), {})
    assert diag.get("fallback_from") == "cp-sat:FEASIBLE"
    assert len({s.task_id for s in segs}) == 4


def test_proven_optimal_solution_is_never_overridden(monkeypatch):
    sched = _stub_cpsat(monkeypatch, "OPTIMAL")  # optimal, nothing schedulable
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    task = Task("w", "p", "Work", priority=3)
    segs, _, diag = sched.plan([task], {"w": {"duration_minutes": 60, "confidence": "high"}}, [], start, 1, cfg(), {})
    assert not segs and "fallback_from" not in diag


def test_large_load_places_nearly_as_much_as_fast_baseline_and_keeps_invariants():
    import random
    import app.scheduler as sched
    random.seed(11)
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    tasks, meta = [], {}
    for i in range(30):
        tasks.append(Task(f"t{i}", "p", f"T{i}", priority=random.choice([1, 3, 5])))
        meta[f"t{i}"] = {"duration_minutes": random.choice([30, 60, 90, 120]), "confidence": "medium",
                         "min_chunk": 30, "max_chunk": 60}
        if i > 2 and random.random() < 0.5:
            meta[f"t{i}"]["dependencies"] = [f"t{random.randrange(i)}"]
    config = cfg() | {"maximize_productive_time": True, "solver_time_limit_seconds": 2}
    block = BusyBlock(start + timedelta(hours=3), start + timedelta(hours=5), "Busy")
    segs, _, _ = sched.plan(tasks, meta, [block], start, 5, config, {})
    base, _, _ = sched._plan_heuristic(tasks, meta, [block], start, 5, config, {})
    placed = lambda rows: sum(int((s.end - s.start).total_seconds() // 60) for s in rows)
    assert placed(segs) >= 0.9 * placed(base)
    ordered = sorted(segs, key=lambda s: s.start)
    assert all(a.end <= b.start for a, b in zip(ordered, ordered[1:]))
    assert all(s.end <= block.start or s.start >= block.end for s in segs)
    by = {}
    for s in segs:
        by.setdefault(s.task_id, []).append(s)
    for tid, m in meta.items():
        for dep in m.get("dependencies", []):
            if tid in by and dep in by:
                assert max(x.end for x in by[dep]) <= min(x.start for x in by[tid])


def test_duplicate_task_ids_never_produce_overlapping_segments():
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    first = Task("a", "p", "First copy", priority=5)
    second = Task("a", "p", "Second copy", priority=1)
    segs, warns, _ = plan([first, second], {"a": {"duration_minutes": 60, "confidence": "high"}}, [], start, 1, cfg(), {})
    ordered = sorted(segs, key=lambda s: s.start)
    assert all(a.end <= b.start for a, b in zip(ordered, ordered[1:]))
    assert sum(int((s.end - s.start).total_seconds() // 60) for s in segs) == 60
    assert any("Duplicate task id" in w for w in warns)


def test_invariant_enforcer_drops_engine_output_that_overlaps(monkeypatch):
    from app.models import Segment
    import app.scheduler as sched
    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    a, b = Task("a", "p", "A"), Task("b", "p", "B")
    block = BusyBlock(start + timedelta(hours=4), start + timedelta(hours=5), "Appointment")
    bad = [
        Segment("a", "p", "A", start, start + timedelta(minutes=60), 2.0, "r", a),
        Segment("b", "p", "B", start + timedelta(minutes=30), start + timedelta(minutes=90), 1.0, "r", b),  # overlaps A
        Segment("a", "p", "A", start + timedelta(hours=4, minutes=30), start + timedelta(hours=5, minutes=30), 1.0, "r", a),  # hits busy
    ]
    monkeypatch.setattr(sched, "_plan_cpsat", lambda *a_, **k: (list(bad), [], {"engine": "cp-sat", "status": "OPTIMAL"}))
    segs, warns, diag = sched.plan([a, b], {}, [block], start, 1, cfg(), {})
    assert [s.title for s in segs] == ["A"] and segs[0].start == start
    assert any("Dropped 2 segment" in w for w in warns)

# --- reviewer adversarial tests (not part of Claude's submission) --------------------

def test_max_chunk_remains_a_ceiling_when_constraints_conflict():
    chunks = choose_chunks(50, TaskMeta(task_id="x", min_chunk=30, max_chunk=45))
    assert max(chunks) <= 45, chunks


def test_feasible_cpsat_is_not_replaced_by_more_low_value_minutes(monkeypatch):
    from app.models import Segment
    import app.scheduler as sched

    start = datetime(2030, 1, 7, 8, 0, tzinfo=UTC)
    urgent = Task("urgent", "p", "Urgent deadline work", priority=5)
    filler = Task("filler", "p", "Low priority filler", priority=0)
    meta = {
        "urgent": {"duration_minutes": 60, "confidence": "high",
                   "deadline": (start + timedelta(hours=1)).isoformat(), "must_finish": True},
        "filler": {"duration_minutes": 120, "confidence": "high"},
    }
    cp = [Segment("urgent", "p", urgent.title, start, start + timedelta(minutes=60), 100.0, "cp", urgent)]
    heur = [Segment("filler", "p", filler.title, start, start + timedelta(minutes=120), 1.0, "heur", filler)]

    monkeypatch.setattr(sched, "_plan_cpsat",
                        lambda *a, **k: (list(cp), [], {"engine":"cp-sat","status":"FEASIBLE"}))
    monkeypatch.setattr(sched, "_plan_heuristic",
                        lambda *a, **k: (list(heur), [], {"engine":"heuristic-fallback","ortools":False}))

    segs, _, _ = sched.plan([urgent, filler], meta, [], start, 1, cfg(), {})
    assert {s.task_id for s in segs} == {"urgent"}
