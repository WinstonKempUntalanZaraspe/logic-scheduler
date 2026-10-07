"""Deterministic adversarial cases; these do not claim to benchmark a live LLM."""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app import project_intelligence_models as pm
from app import project_intelligence_runtime as runtime
from app import project_intelligence_sources as sources
from app.project_intelligence_provenance import validate_campaign_draft
from test_project_intelligence import draft


def future_draft():
    d = draft()
    d.deadline = (datetime.now(pm.settings.tz).date() + timedelta(days=60)).isoformat()
    d.presentation_date = None
    return d


def build(d=None, text='Prepare me for this hackathon'):
    return pm.build_campaign(d or future_draft(), text, [])


def test_past_event_does_not_generate_new_work():
    d = future_draft()
    d.deadline = (datetime.now(pm.settings.tz).date()-timedelta(days=1)).isoformat()
    with pytest.raises(HTTPException):
        build(d)


def test_today_deadline_does_not_claim_nonexistent_contingency():
    d = future_draft(); d.deadline = datetime.now(pm.settings.tz).date().isoformat()
    assert build(d)['deadline_buffer_days'] == 0


@pytest.mark.parametrize('problem', ['empty', 'duplicate', 'blank', 'cycle', 'missing'])
def test_invalid_learning_graph_fails_closed(problem):
    d = future_draft()
    if problem == 'empty': d.work_packages = []
    if problem == 'duplicate': d.work_packages[1].key = 'foundation'; d.work_packages[1].dependencies=[]
    if problem == 'blank': d.work_packages[0].definition_of_done = ' '
    if problem == 'cycle': d.work_packages[0].dependencies = ['build']
    if problem == 'missing': d.work_packages[1].dependencies = ['unknown']
    with pytest.raises(HTTPException): build(d)


def test_finished_foundations_unlock_next_work_immediately():
    c = build(); c['work_packages'][0]['status'] = 'done'
    assert c['work_packages'][1]['target_start'] > datetime.now(pm.settings.tz).date().isoformat()
    assert [p['key'] for p in pm.eligible_work_packages(c)] == ['build']


def test_paused_campaign_never_releases_work():
    c = build(); c['status'] = 'paused'
    assert pm.eligible_work_packages(c) == []


def test_fifteen_minute_work_is_schedulable():
    c=build(); p=c['work_packages'][0]; p['estimated_minutes']=p['remaining_minutes']=15
    assert runtime.scheduler_meta(c,p)['min_chunk'] <= 15


def test_inferred_intermediate_target_is_not_a_hard_deadline():
    c=build(); p=c['work_packages'][0]; p['target_finish']='2020-01-01'
    assert runtime.scheduler_meta(c,p)['deadline'][:10] == c['internal_deadline']



def test_low_priority_campaign_meta_is_background_fill():
    c = build()
    c['priority_class'] = 'low'
    p = c['work_packages'][0]
    assert runtime.scheduler_meta(c, p)['_background_fill'] is True

    c['priority_class'] = 'normal'
    assert runtime.scheduler_meta(c, p)['_background_fill'] is False


def test_low_priority_project_yields_to_ordinary_study_but_uses_leftover_capacity():
    from app.models import Task
    from app.scheduler import plan, ORTOOLS_AVAILABLE
    from test_scheduler import base_cfg

    assert ORTOOLS_AVAILABLE, 'Exercise the production CP-SAT solver'
    start = datetime.now(pm.settings.tz).replace(hour=7, minute=0, second=0, microsecond=0)

    project = Task('project-low', 'project', 'SPhL mechanics practice', priority=1, tags=['flexible'])
    ordinary = Task('ordinary-study', 'school', 'Study school physics', priority=3, tags=['flexible'])

    metas = {
        'project-low': {
            'duration_minutes': 60,
            'remaining_minutes': 60,
            'confidence': 'high',
            'energy': 'high',
            'splittable': True,
            'min_chunk': 30,
            'max_chunk': 30,
            'autoschedule': True,
            '_background_fill': True,
            # Even a near project target must not let explicitly low-priority
            # campaign work crowd out ordinary study.
            'deadline': (start + timedelta(hours=10)).isoformat(),
        },
        'ordinary-study': {
            'duration_minutes': 60,
            'remaining_minutes': 60,
            'confidence': 'high',
            'energy': 'high',
            'splittable': False,
            'autoschedule': True,
        },
    }
    cfg = base_cfg()
    cfg.update({
        # Deep-work capacity is a soft objective. Use a 105-minute window: 90
        # minutes of work plus the recovery interval after a demanding hour.
        'day_end': '08:45',
        'sleep_start': '08:45',
        'between_chunks_buffer': 0,
        'max_deep_work_minutes': 90,
        'solver_time_limit_seconds': 3,
        'solver_workers': 1,
        'candidate_step_minutes': 5,
    })

    segments, _, diagnostics = plan([project, ordinary], metas, [], start, 1, cfg, {})
    assert diagnostics['engine'] == 'cp-sat'

    ordinary_minutes = sum(
        int((s.end - s.start).total_seconds() // 60)
        for s in segments if s.task_id == 'ordinary-study'
    )
    project_minutes = sum(
        int((s.end - s.start).total_seconds() // 60)
        for s in segments if s.task_id == 'project-low'
    )

    assert ordinary_minutes == 60
    assert project_minutes == 30


def test_ticktick_task_keeps_learning_instructions_and_rubric():
    c=build(); p=c['work_packages'][0]
    content=runtime.task_content(c,p)
    assert p['description'] in content
    assert 'Technical quality' in content


@pytest.mark.parametrize('text,minutes', [('I can spend 45 minutes per day',45),('I have 5 hours per week',42),('I can spend 1.5 hours a day',90)])
def test_user_capacity_overrides_two_hour_guess(text, minutes):
    c=build(text='Prepare me for this hackathon. '+text)
    assert c['estimated_daily_project_capacity_minutes'] == minutes
    assert c['capacity_source'] == 'user_input'


@pytest.mark.parametrize('kind', ['hackathon','exam','research_project','portfolio_project','personal_goal'])
def test_large_projects_expose_scope_and_readiness_gaps(kind):
    d=future_draft(); d.campaign_type=kind
    for p in d.work_packages: p.estimated_minutes=12000
    c=build(d,text='Prepare me for this project. I have 15 minutes per day.')
    review=c['planning_review']
    assert review['capacity_status'] == 'overloaded'
    assert review['remaining_minutes'] == 24000
    assert any(x['code']=='capacity_overload' for x in review['issues'])


def test_zero_knowledge_build_only_roadmap_is_flagged():
    d=future_draft();d.knowledge_assumption='ZERO_KNOWLEDGE_BASELINE'; d.work_packages=d.work_packages[1:];d.work_packages[0].dependencies=[]
    c=build(d)
    assert any(i['code']=='missing_foundations' for i in c['planning_review']['issues'])


def test_fabricated_rubric_weight_is_rejected():
    d=future_draft();d.deadline=None;d.requirements=[]
    d.rubric[0].source='rules';d.rubric[0].source_type='website';d.rubric[0].weight=80
    docs=[{'source':'rules','source_type':'website','text':'Technical quality: 40%. Presentation: 60%.'}]
    with pytest.raises(HTTPException): validate_campaign_draft(d,'Prepare me',docs)


def test_supported_relative_deadline_is_accepted():
    d=future_draft();d.deadline=(datetime.now(pm.settings.tz).date()+timedelta(days=14)).isoformat();d.requirements=[];d.rubric=[]
    validate_campaign_draft(d,'Prepare me for this hackathon in 2 weeks',[])


def test_opaque_rubric_link_is_found_by_anchor_text(monkeypatch):
    visited=[]
    async def download(url):
        visited.append(url)
        if url.endswith('/event'):
            return url,'text/html',b'<title>Hackathon</title><a href="/page/123">Judging criteria</a>'
        return url,'text/html',b'Technical quality: 40%'
    monkeypatch.setattr(sources,'download',download)
    docs,warnings=asyncio.run(sources.fetch_source_bundle(['https://example.com/event']))
    assert len(docs)==2


def test_done_missing_tasks_do_not_starve_later_completions(monkeypatch):
    c=build(); seed=c['work_packages'][0]
    c['work_packages']=[dict(seed,key=str(i),ticktick_task_id=str(i),ticktick_project_id='p',status='actionable') for i in range(20)]
    class TT:
        async def get_task(self,project_id,task_id): return {'status':2 if task_id=='19' else 0}
    asyncio.run(runtime.reconcile_campaign(c,TT(),{}))
    asyncio.run(runtime.reconcile_campaign(c,TT(),{}))
    assert c['work_packages'][19]['status']=='done'


@pytest.mark.parametrize('minutes', [15,120,12000])
def test_materialized_project_work_respects_real_life(minutes):
    from app.models import Task, BusyBlock
    from app.scheduler import plan, ORTOOLS_AVAILABLE
    from test_scheduler import base_cfg
    assert ORTOOLS_AVAILABLE, 'Exercise the production solver, not just a fallback'
    start=datetime.now(pm.settings.tz).replace(hour=7,minute=0,second=0,microsecond=0)
    c=build();p=c['work_packages'][0];p['estimated_minutes']=p['remaining_minutes']=minutes
    task=Task('learning','p','Learn solver modelling',priority=3)
    busy=[BusyBlock(start.replace(hour=a),start.replace(hour=b),name) for a,b,name in [(8,16,'School and commute'),(17,19,'Swim, travel and shower'),(19,20,'Dinner')]]
    segments,_,diag=plan([task],{'learning':runtime.scheduler_meta(c,p)},busy,start,1,base_cfg(),{})
    assert segments, 'Use the legal remaining slots'
    assert diag['engine']=='cp-sat'
    for segment in segments:
        assert start <= segment.start < segment.end <= start.replace(hour=23)
        assert all(segment.end <= b.start or segment.start >= b.end for b in busy)
    ordered=sorted(segments,key=lambda s:s.start)
    assert all(a.end<=b.start for a,b in zip(ordered,ordered[1:]))


def test_note_destination_is_never_materialized(monkeypatch):
    c=build();c['ticktick_project_id']='notes'
    monkeypatch.setattr(runtime,'load_store',lambda:{c['id']:c})
    monkeypatch.setattr(runtime,'save_store',lambda _:None)
    class TT:
        connected=True
        async def all_active_tasks(self):return [],[{'id':'notes','kind':'NOTE'}]
        async def create_task(self,*args,**kwargs):raise AssertionError('NOTE list was written')
    result=asyncio.run(runtime._sync_unlocked(tt=TT()))
    assert result['created']==[]


def test_valid_rubric_weight_still_passes():
    d=future_draft();d.deadline=None;d.requirements=[]
    d.rubric[0].source='rules';d.rubric[0].source_type='website'
    validate_campaign_draft(d,'Prepare me', [{'source':'rules','source_type':'website','text':'Technical quality: 40%. Presentation: 60%.'}])


def test_model_transport_failure_creates_no_fake_blueprint(monkeypatch):
    monkeypatch.setattr(sources,'semantic_api_key',lambda:'')
    with pytest.raises(HTTPException) as err:asyncio.run(sources.reason_campaign('Prepare me',[]))
    assert err.value.status_code==503


def test_progress_endpoint_marks_finished_task_unschedulable(monkeypatch):
    import app.final_entrypoint as entry
    c=build();p=c['work_packages'][0];p['ticktick_task_id']='t1'
    meta={}
    monkeypatch.setattr(runtime,'get_campaign',lambda _:c)
    monkeypatch.setattr(runtime,'save_campaign',lambda _:None)
    monkeypatch.setattr(runtime.db,'get_meta',lambda _:{})
    monkeypatch.setattr(runtime.db,'set_meta',lambda _,value:meta.update(value))
    async def sync(**kwargs):return {}
    monkeypatch.setattr(runtime,'sync_campaigns',sync)
    runtime._LOCKS.clear()
    endpoint=next(r.endpoint for r in entry.app.routes if r.path=='/api/project-intelligence/{campaign_id}/progress')
    asyncio.run(endpoint(c['id'],pm.ProgressUpdate(work_package_key=p['key'],progress_percent=100,note='Exercise solved')))
    assert meta['autoschedule'] is False
    assert meta['remaining_minutes']==0
    assert c['planning_review']['remaining_minutes']==240



def test_specific_brief_does_not_crawl_unrelated_global_schedule_and_faq(monkeypatch):
    visited=[]
    async def download(url):
        visited.append(url)
        if url.endswith('/hackathon2025'):
            return url,'text/html',b'<title>Hackathon</title><nav><a href="/conference-schedule">Schedule</a><a href="/faq">FAQ</a><a href="/page/123">Judging criteria</a></nav><p>Build a city dashboard; judging criteria are on the linked page.</p>'
        return url,'text/html',b'Technical quality: 40%'
    monkeypatch.setattr(sources,'download',download)
    docs,warnings=asyncio.run(sources.fetch_source_bundle(['https://example.com/hackathon2025']))
    assert visited==['https://example.com/hackathon2025','https://example.com/page/123']
    assert [d['source_role'] for d in docs]==['primary','supporting']
