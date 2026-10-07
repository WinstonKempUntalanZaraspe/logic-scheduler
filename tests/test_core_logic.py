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
