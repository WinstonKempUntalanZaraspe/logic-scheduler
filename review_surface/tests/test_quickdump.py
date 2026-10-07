import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.quickdump import parse_quick_dump
from app.models import Task
from app.scheduler import plan
from app.ticktick import TickTickClient

TZ = ZoneInfo('Asia/Singapore')


def base_cfg():
    return {
        'day_start':'07:00','day_end':'23:00','sleep_start':'23:00','wake_time':'07:00',
        'meals':[],'max_deep_work_minutes':240,'between_chunks_buffer':10,'high_energy_recovery_minutes':15,
        'target_utilization':0.9,'min_daily_slack_minutes':0,'candidate_step_minutes':10,
        'max_candidates_per_chunk':80,'solver_time_limit_seconds':3,'solver_workers':2,
        'default_travel_buffer_minutes':0,'weekly_capacity_minutes':{},
    }


def test_quick_dump_understands_state_and_task_lines():
    now = datetime(2026,10,2,16,0,tzinfo=TZ)
    rows = [{'id':'calc','project_id':'p','project':'Study','title':'Calculus integration practice','tags':[]}]
    result = parse_quick_dump(
        "I'm exhausted after school and don't feel like studying. I want to sleep by 10:30pm.\n"
        "Calculus integration practice 2h due tomorrow\n"
        "Email lecturer 15m\n"
        "Table tennis tomorrow 10am-2pm fixed",
        rows, base_cfg(), now,
    )
    ctx = result['context']
    assert ctx['energy_scale'] < 0.5
    assert ctx['avoid_deep_today'] is True
    assert ctx['sleep_start'] == '22:30'
    by_title = {x['title']: x for x in result['tasks']}
    assert by_title['Calculus integration practice']['action'] == 'update'
    assert by_title['Calculus integration practice']['meta_patch']['duration_minutes'] == 120
    assert by_title['Calculus integration practice']['priority'] == 5
    table = by_title['Table tennis']
    assert table['tags_add'] == ['fixed']
    assert table['meta_patch']['splittable'] is False
    assert table['fixed_start'].endswith('10:00:00+08:00')
    assert table['fixed_end'].endswith('14:00:00+08:00')


def test_woke_late_is_today_only_wake_override():
    now = datetime(2026,10,2,10,12,tzinfo=TZ)
    result = parse_quick_dump('I woke up late at 9:30am', [], base_cfg(), now)
    assert result['context']['date'] == '2026-10-02'
    assert result['context']['wake_time'] == '09:30'
    assert not result['tasks']


def test_fixed_school_dump_becomes_one_unsplittable_commitment():
    now = datetime(2026,10,2,8,0,tzinfo=TZ)
    result = parse_quick_dump('School tomorrow 8am-4pm fixed', [], base_cfg(), now)
    task = result['tasks'][0]
    assert task['tags_add'] == ['fixed']
    assert task['meta_patch']['duration_minutes'] == 480
    assert task['meta_patch']['autoschedule'] is False
    assert task['meta_patch']['splittable'] is False


def test_today_fatigue_pushes_deep_work_to_next_day_when_possible():
    now = datetime(2026,10,2,16,0,tzinfo=TZ)
    cfg = base_cfg() | {'_quick_context': {
        'date':'2026-10-02','energy_scale':0.5,'avoid_deep_today':True,
        'fatigue_from':'2026-10-02T16:00:00+08:00','fatigue_until':'2026-10-02T19:00:00+08:00',
    }}
    task = Task('a','p','Calculus',priority=3,tags=['deep-work'])
    segs,_,_ = plan([task], {'a':{'duration_minutes':60,'confidence':'high','energy':'high'}}, [], now, 2, cfg, {})
    assert segs
    assert min(x.start for x in segs).date().isoformat() == '2026-10-03'


def test_today_sleep_override_shortens_only_today_usable_window():
    now = datetime(2026,10,2,18,0,tzinfo=TZ)
    cfg = base_cfg() | {'_quick_context': {'date':'2026-10-02','sleep_start':'21:30'}}
    task = Task('a','p','Late task',priority=5)
    segs,_,_ = plan([task], {'a':{'duration_minutes':60,'confidence':'high','timing':'late'}}, [], now, 2, cfg, {})
    assert segs
    today = [x for x in segs if x.start.date().isoformat() == '2026-10-02']
    assert all(x.end.hour < 21 or (x.end.hour == 21 and x.end.minute <= 30) for x in today)


def test_ticktick_create_task_can_create_unscheduled_dump(monkeypatch):
    # Keep this test hermetic so the clean distributable does not need a pre-created DB.
    monkeypatch.setattr('app.ticktick.get_kv', lambda key, default=None: default)
    client = TickTickClient()
    captured = {}
    async def fake_req(method, path, **kwargs):
        if method == 'GET' and path == '/project':
            return [{'id': 'p', 'kind': 'TASK'}]
        captured.update(kwargs['json'])
        return {'id':'new'}
    monkeypatch.setattr(client, '_req', fake_req)
    result = asyncio.run(client.create_task('p','Email lecturer',None,None,tags=['quick-win'],priority=1))
    assert result['id'] == 'new'
    assert 'startDate' not in captured and 'dueDate' not in captured
    assert captured['tags'] == ['quick-win']

def test_quick_dump_preserves_manual_existing_duration_and_priority_when_not_explicit():
    now = datetime(2026,10,2,10,0,tzinfo=TZ)
    rows = [{
        'id':'calc','project_id':'p','project':'Study','title':'Calculus practice','tags':['deep-work'],
        'priority':5,'duration_minutes':None,
        'meta':{'duration_minutes':150,'confidence':'high','splittable':True,'min_chunk':50,'max_chunk':75},
    }]
    result = parse_quick_dump('Calculus practice', rows, base_cfg(), now)
    task = result['tasks'][0]
    assert task['priority'] == 5
    assert 'duration_minutes' not in task['meta_patch']
    assert 'confidence' not in task['meta_patch']
    assert 'splittable' not in task['meta_patch']

def test_untimed_fixed_task_warns_instead_of_silently_disappearing():
    now = datetime(2026,10,2,8,0,tzinfo=TZ)
    fixed = Task('f','p','School block',tags=['fixed'])
    segs,warnings,_ = plan([fixed], {}, [], now, 1, base_cfg(), {})
    assert not segs
    assert any('#fixed' in x and 'blocks no interval' in x for x in warnings)


def test_plain_wake_report_plus_replan_is_context_not_task():
    now = datetime(2026,10,2,8,2,tzinfo=TZ)
    result = parse_quick_dump('Woke up at 8:00am today, reschedule my morning', [], base_cfg(), now)
    assert result['tasks'] == []
    ctx = result['context']
    assert ctx['wake_time'] == '08:00'
    assert ctx['actual_wake_reported'] is True
    assert ctx['replan_requested'] is True
    assert ctx['replan_scope'] == 'morning'
    assert ctx['catch_up_missed'] is True
    assert ctx['replan_from'].startswith('2026-10-02T08:02')


def test_plain_wake_report_can_offer_cleanup_for_exact_old_parser_artifact():
    now = datetime(2026,10,2,8,2,tzinfo=TZ)
    rows = [{
        'id':'oops','project_id':'p','project':'Inbox',
        'title':'Woke up at 8:00am today, reschedule my morning',
        'tags':[],'priority':0,'duration_minutes':35,
        'meta':{'duration_minutes':30},
    }]
    result = parse_quick_dump('Woke up at 8:00am today, reschedule my morning', rows, base_cfg(), now)
    assert result['tasks'] == []
    assert result['cleanup_tasks'] == [{
        'task_id':'oops','project_id':'p',
        'title':'Woke up at 8:00am today, reschedule my morning',
        'reason':'Remove accidental task created by the old Quick Dump parser',
    }]


def test_replan_does_not_resurrect_expired_timed_block():
    now = datetime(2026,10,2,8,5,tzinfo=TZ)
    cfg = base_cfg() | {'_quick_context': {
        'date':'2026-10-02','wake_time':'08:00','actual_wake_reported':True,
        'replan_requested':True,'replan_scope':'morning','catch_up_missed':True,
        'replan_from':'2026-10-02T08:05:00+08:00',
    }}
    later = Task('later','p','General flexible task',priority=0)
    missed = Task(
        'missed','p','Eat breakfast',
        start=datetime(2026,10,2,7,20,tzinfo=TZ),
        end=datetime(2026,10,2,7,40,tzinfo=TZ),
        priority=0,
    )
    meta = {
        'later':{'duration_minutes':20,'confidence':'high'},
        'missed':{'duration_minutes':20,'confidence':'high'},
    }
    segs,_,_ = plan([later, missed], meta, [], now, 1, cfg, {})
    assert segs
    first = min(segs, key=lambda x: x.start)
    assert first.task_id == 'later'
    assert not any(s.task_id == 'missed' for s in segs)


def test_exact_time_range_makes_ordinary_chore_a_fixed_reservation():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    result = parse_quick_dump('I will sweep the floor from 10:30pm to 10:50pm', [], base_cfg(), now)
    assert len(result['tasks']) == 1
    task = result['tasks'][0]
    assert task['fixed_start'] == '2026-10-07T22:30:00+08:00'
    assert task['fixed_end'] == '2026-10-07T22:50:00+08:00'
    assert task['tags_add'] == ['fixed']
    assert task['meta_patch']['duration_minutes'] == 20
    assert task['meta_patch']['autoschedule'] is False
    assert task['meta_patch']['splittable'] is False
    assert task['reason'] == 'Fixed commitment'


def test_exact_time_range_makes_study_fixed_even_without_event_vocabulary():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    result = parse_quick_dump('I will study calculus from 8pm to 9pm', [], base_cfg(), now)
    task = result['tasks'][0]
    assert task['fixed_start'].endswith('20:00:00+08:00')
    assert task['fixed_end'].endswith('21:00:00+08:00')
    assert task['meta_patch']['autoschedule'] is False


def test_tentative_time_range_stays_flexible_instead_of_becoming_fixed():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    result = parse_quick_dump('Maybe sweep the floor from 10:30pm to 10:50pm', [], base_cfg(), now)
    task = result['tasks'][0]
    assert task['fixed_start'] is None
    assert task['fixed_end'] is None
    assert 'fixed' not in task['tags_add']
    assert task['meta_patch']['autoschedule'] is True


def test_timed_availability_does_not_gain_fixed_task_authority_in_low_level_parser():
    now = datetime(2026, 10, 7, 18, 58, tzinfo=TZ)
    result = parse_quick_dump('I am available from 8pm to 9pm', [], base_cfg(), now)
    task = result['tasks'][0]
    assert task['fixed_start'] is None
    assert task['fixed_end'] is None
    assert 'fixed' not in task['tags_add']
