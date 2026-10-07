from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.language_intake import parse_language, intent_aware_plan, validate_task_creation
from app.models import Task, BusyBlock

TZ = ZoneInfo('Asia/Singapore')
NOW = datetime(2026, 10, 3, 12, 9, tzinfo=TZ)
CFG = {'wake_time': '07:00', 'day_start': '07:00', 'sleep_start': '23:00', 'day_end': '23:00', 'solver_seconds': 1, 'num_workers': 1}


def row(tid, title, **kw):
    return dict(id=tid, title=title, project_id='p', status=0, tags=[], meta={}) | kw


def test_full_real_world_prompt_through_production_parser():
    text = Path(__file__).with_name('stress_prompt.txt').read_text()
    result = parse_language(text, [row('swim', 'Swimming'), row('read', 'Read physics'), row('do', 'Do physics')], CFG, NOW)
    assert result['tasks'] == []
    assert result['cleanup_tasks'] == []
    assert result['clarifications'] == []
    assert result['warnings'] == []
    ctx = result['context']
    assert ctx['minimum_horizon_days'] == 2
    assert ctx['completed_meals']['breakfast'] == NOW.isoformat()
    assert len(ctx['temporary_blocks']) == 1
    assert ctx['intent_today_ids'] == ['swim']
    assert ctx['before_main_study_ids'] == ['swim']
    assert ctx['preserve_unfinished'] is True
    assert sum(i['kind'] == 'output' for i in result['intents']) == 9
    assert not any(i['kind'] == 'task' for i in result['intents'])


@pytest.mark.parametrize('text', [
    'It is now 12:09 PM on Saturday, October 3.', 'I just finished breakfast.',
    'Preserve all fixed/manual commitments.', 'Do not create overlaps.',
    'Respect sleep.', 'Treat unfinished work as unfinished.',
    'Do not schedule swimming immediately after eating.',
    'Use deadlines, priority and energy.', 'Keep physical outing chains contiguous.',
    'Look ahead at upcoming deadlines.', 'After replanning, show:',
    'What was moved?', 'The cleaned schedule for tomorrow.',
    'Never delete genuine manual events.', 'Add buffers and breaks.',
    'Make the schedule realistic.', 'If only one session remains, do not invent another part.',
    'Suppose Physics has two blocks tomorrow.', 'I spent yesterday building the website.',
    'A missing or expired AutoScheduler block is not proof of completion.',
    'Move lunch dynamically based on swimming and hunger.',
    'This should consider what actually matters to my real day.'
])
def test_control_and_explanation_never_create_or_route_tasks(text):
    result = parse_language(text, [row('s', 'Swimming')], CFG, NOW)
    assert not result['tasks']
    from app.smart_routing import _decorate_routing
    assert _decorate_routing(result, [], [], None) == []


@pytest.mark.parametrize('text,title,minutes', [
    ('Buy milk 15m', 'Buy milk', 15), ('Write expense report 2h', 'Write expense report', 120),
    ('Move boxes tomorrow 20m', 'Move boxes', 20), ('Shower tomorrow 20m', 'Shower', 20),
    ('Remind me to call Dad 10m', 'call Dad', 10), ('I need to buy milk 15m', 'buy milk', 15),
    ('Add task: Read chapter 4 60m', 'Read chapter 4', 60),
    ('Use drill to assemble shelf 45m', 'Use drill to assemble shelf', 45),
])
def test_actual_work_still_parses(text,title,minutes):
    result = parse_language(text, [], CFG, NOW)
    assert len(result['tasks']) == 1
    task = result['tasks'][0]
    assert task['title'].lower() == title.lower()
    assert task['meta_patch']['duration_minutes'] == minutes
    validate_task_creation(result)


def test_mixed_state_and_work_are_clause_local():
    r = parse_language("I'm eating breakfast now and add task: Buy milk 15m\nDo physics after Read physics", [row('do', 'Do physics'), row('read', 'Read physics')], CFG, NOW)
    assert [x['title'] for x in r['tasks'] if x['action']=='create'] == ['Buy milk']
    do = next(x for x in r['tasks'] if x.get('task_id')=='do')
    assert do['meta_patch']['dependencies'] == ['read']
    assert r['context']['temporary_blocks'][0]['meal'] == 'breakfast'


def test_task_tomorrow_date_is_not_lost():
    r = parse_language('Write report tomorrow 60m', [], CFG, NOW)
    assert r['tasks'][0]['meta_patch']['earliest'].startswith('2026-10-04T07:00')


def test_future_task_mentions_do_not_move_today_swim():
    r = parse_language('I want to swim before study today. Clean up tomorrow. Tomorrow has a Physics exam.', [row('swim', 'Swimming')], CFG, NOW)
    assert r['context']['intent_today_ids'] == ['swim']
    assert not r['context'].get('intent_swim_tomorrow_ids')


def test_vague_target_is_not_guessed():
    r = parse_language('I want to do physics today', [row('a','Do physics SHM'),row('b','Do physics Waves')], CFG, NOW)
    assert r['tasks'] == []
    assert r['clarifications']


def test_dependency_cycle_rejected():
    r = parse_language('Read physics after Do physics', [row('r','Read physics'),row('d','Do physics',meta={'dependencies':['r']})], CFG, NOW)
    assert not any(x.get('meta_patch',{}).get('dependencies') for x in r['tasks'])
    assert any('cycle' in x for x in r['warnings'])


def test_unclassified_creation_rejected_before_routing():
    from app.smart_routing import _decorate_routing
    with pytest.raises(ValueError):
        _decorate_routing({'tasks':[{'action':'create','title':'Preserve commitments'}]}, [], [], None)


def assert_production_plan_full_outing_meals_and_study():
    text = Path(__file__).with_name('stress_prompt.txt').read_text()
    rows = [row('swim','Swimming')]
    ctx = parse_language(text, rows, CFG, NOW)['context']
    tasks = [Task('swim','p','Swimming'),Task('out','p','Travel to pool'),Task('prep','p','Change at pool'),Task('shower','p','Shower and change'),Task('home','p','Go home'),Task('study','p','Read physics',tags=['deep-work'])]
    meta = {tid:{'duration_minutes':mins,'confidence':'high','splittable':False} for tid,mins in [('swim',60),('out',20),('prep',10),('shower',15),('home',20),('study',60)]}
    original = deepcopy(meta)
    segments, warnings, diagnostics = intent_aware_plan(tasks, meta, [], NOW, 2, CFG|{'_quick_context':ctx}, {})
    assert meta == original
    outing = sorted([s for s in segments if s.task_id!='study'], key=lambda s:s.start)
    assert {s.task_id for s in outing} == {'swim','out','prep','shower','home'}
    assert all(s.start.date()==NOW.date() for s in outing)
    assert all(a.end==b.start for a,b in zip(outing,outing[1:]))
    study = [s for s in segments if s.task_id=='study']
    assert study and min(s.start for s in study)>=outing[-1].end
    swim = next(s for s in outing if s.task_id=='swim')
    assert swim.start >= NOW+timedelta(minutes=15)
    for day in (NOW.date(), (NOW+timedelta(days=1)).date()):
        meals = [m for m in diagnostics['flexible_meals'] if datetime.fromisoformat(m['start']).date()==day]
        assert {m['name'] for m in meals} == {'Breakfast','Lunch','Dinner'}
        for meal in meals:
            a,b = map(datetime.fromisoformat,(meal['start'],meal['end']))
            assert all(not (s.start<b and a<s.end) for s in segments)
    lunch = next(m for m in diagnostics['flexible_meals'] if m['name']=='Lunch' and m['start'].startswith('2026-10-03'))
    assert datetime.fromisoformat(lunch['start'])>=NOW+timedelta(minutes=150)
    ordered = sorted(segments,key=lambda s:s.start)
    assert all(a.end<=b.start for a,b in zip(ordered,ordered[1:]))
    assert all(s.end.hour<=23 for s in segments)
    assert not diagnostics['explicit_today_unplaced']


def test_infeasible_today_goal_is_explained_without_deferral():
    r = parse_language('I want to swim today', [row('swim','Swimming')], CFG, NOW)
    tasks = [Task('swim','p','Swimming')]
    blocked = [BusyBlock(NOW,NOW.replace(hour=23),'Appointment')]
    segs,warnings,diag = intent_aware_plan(tasks, {'swim':{'duration_minutes':60}}, blocked, NOW, 2, CFG|{'_quick_context':r['context']},{})
    assert not segs
    assert diag['explicit_today_unplaced']==['swim']
    assert any('not silently moved' in w for w in warnings)


def test_same_day_split_is_soft_in_recovery_replan(monkeypatch):
    import app.human_day_patch as hdp
    captured={}
    def fake(tasks, metas, *args):
        captured.update(metas)
        return [],[],{}
    monkeypatch.setattr(hdp,'_BASE_PERSONAL_PLAN',fake)
    task=Task('read','p','Read physics',start=NOW-timedelta(hours=2),tags=['deep-work'])
    hdp.smart_plan([task],{'read':{'duration_minutes':300}},[],NOW,2,CFG|{'meal_protection':False,'_quick_context':{'preserve_unfinished':True,'replan_requested':True}}, {})
    assert not captured['read'].get('latest_end')


def test_final_entrypoint_in_fresh_production_process():
    # The production entrypoint installs global cache/DB/planner wrappers. Verify
    # it in its actual process model, isolated from monkeypatched unit-test state.
    import subprocess, sys
    completed=subprocess.run([sys.executable,'-c',
        "import app.final_entrypoint as p; import runpy; n=runpy.run_path('tests/test_language_intake.py'); "
        "assert p._main.parse_quick_dump is n['parse_language']; assert p._service.plan is p._contextual_plan; "
        "n['test_full_real_world_prompt_through_production_parser'](); n['assert_production_plan_full_outing_meals_and_study']()"],capture_output=True,text=True,timeout=30)
    assert completed.returncode==0,completed.stdout+completed.stderr


@pytest.mark.parametrize('text', ['I just finished Physics.', 'I completed swimming.', 'Finished reading Physics.', 'Travel must take 90 minutes.', 'Make sure everything fits.', 'Keep my appointment at 5 PM.', 'Ensure dinner happens.', "I've just finished Physics.", 'Physics is already done.', 'I have just finished breakfast.', 'Breakfast is done.'])
def test_completion_and_custom_limits_are_not_new_work(text):
    parsed = parse_language(text, [], CFG, NOW)
    assert parsed['tasks'] == []
    if '90' in text:
        assert parsed['clarifications']


def test_distinct_task_verbs_and_real_moving_work():
    parsed = parse_language('Do physics 60m', [row('read', 'Read physics')], CFG, NOW)
    assert parsed['tasks'][0]['action'] == 'create'
    parsed = parse_language('Move boxes today 20m', [], CFG, NOW)
    assert parsed['tasks'][0]['title'] == 'Move boxes'


def test_future_goal_is_context_and_does_not_create_duplicate():
    parsed = parse_language('I want to swim tomorrow', [row('swim', 'Swimming')], CFG, NOW)
    assert parsed['tasks'] == []
    assert parsed['context']['intent_date_goals'] == {'swim':'2026-10-04'}
    assert not parsed['context'].get('intent_today_ids')


def test_date_directives_preserve_manual_bounds_and_expire(monkeypatch):
    import app.human_adjuster_patch as adjuster
    captured = {}
    def fake(tasks, metas, busy, start, horizon, config, mastery):
        captured.update(meta=metas, cfg=config)
        return [], [], {}
    monkeypatch.setattr(adjuster, 'human_adjusted_plan', fake)
    tasks = [Task('swim', 'p', 'Swimming')]
    earliest = (NOW+timedelta(days=2)).isoformat()
    parsed = parse_language('I want to swim today', [row('swim','Swimming')], CFG, NOW)
    intent_aware_plan(tasks, {'swim':{'earliest':earliest}}, [], NOW, 2, CFG|{'_quick_context':parsed['context']}, {})
    assert captured['meta']['swim']['earliest'] == earliest
    intent_aware_plan(tasks, {}, [], NOW+timedelta(days=1), 2, CFG|{'_quick_context':parsed['context']}, {})
    assert not captured['meta']['swim'].get('latest_end')


@pytest.mark.parametrize('engine', ['cp-sat', 'heuristic'])
def test_excluding_tomorrow_does_not_exclude_today(engine):
    from app import scheduler
    parsed = parse_language("Don't schedule coding tomorrow", [row('code','Coding')], CFG, NOW)
    assert parsed['tasks'] == []
    ctx = parsed['context']
    assert ctx['intent_exclusions'] == [{'task_ids':['code'], 'date':'2026-10-04'}]
    lower = NOW.replace(hour=7,minute=0)+timedelta(days=1)
    upper = lower.replace(hour=23)
    cfg = CFG|{'meal_protection':False,'_task_exclusion_windows':{'code':[{'start':lower.isoformat(),'end':upper.isoformat()}]}}
    planner = scheduler._plan_cpsat if engine=='cp-sat' else scheduler._plan_heuristic
    segs, _, _ = planner([Task('code','p','Coding')], {'code':{'duration_minutes':60,'confidence':'high'}}, [], NOW, 2, cfg, {})
    assert segs and all(s.start.date()==NOW.date() for s in segs)
    assert not scheduler._meal_activity_start_allowed('code', lower-timedelta(minutes=20), cfg, lower+timedelta(minutes=20))


def test_support_date_exclusion_propagates_to_contiguous_outing(monkeypatch):
    import app.human_adjuster_patch as adjuster
    captured={}
    def fake(tasks,meta,busy,start,horizon,cfg,mastery):captured.update(cfg);return [],[],{}
    monkeypatch.setattr(adjuster,'human_adjusted_plan',fake)
    ctx={'date':NOW.date().isoformat(),'intent_exclusions':[{'task_ids':['travel'],'date':'2026-10-04'}]}
    tasks=[Task('swim','p','Swimming'),Task('travel','p','Travel to pool')]
    intent_aware_plan(tasks,{},[],NOW,2,CFG|{'_quick_context':ctx},{})
    assert captured['_task_exclusion_windows']['swim']==captured['_task_exclusion_windows']['travel']


def test_support_date_goal_constrains_the_containing_outing(monkeypatch):
    import app.human_adjuster_patch as adjuster
    captured={}
    def fake(tasks,meta,busy,start,horizon,cfg,mastery):captured.update(meta);return [],[],{}
    monkeypatch.setattr(adjuster,'human_adjusted_plan',fake)
    ctx={'date':NOW.date().isoformat(),'intent_date_goals':{'travel':'2026-10-04'}}
    tasks=[Task('swim','p','Swimming'),Task('travel','p','Travel to pool')]
    intent_aware_plan(tasks,{},[],NOW,2,CFG|{'_quick_context':ctx},{})
    assert captured['swim']['earliest'].startswith('2026-10-04')
    assert captured['swim']['latest_end']==captured['travel']['latest_end']
