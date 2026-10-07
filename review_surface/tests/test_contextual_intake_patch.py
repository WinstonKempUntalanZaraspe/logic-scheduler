import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import db
import app.contextual_intake_patch as patch
from app.contextual_intake_patch import (
    contextual_review_intake,
    contextual_apply_reviewed_intake,
    contextual_intent_aware_plan,
)
from app.contextual_intake_phrase_extension import extend_contextual_intake_phrases
from app.models import Task

TZ = ZoneInfo('Asia/Singapore')
NOW = datetime(2026, 10, 3, 14, 9, tzinfo=TZ)
CFG = {'wake_time':'07:00','day_start':'07:00','sleep_start':'23:00','day_end':'23:00'}


def row(tid, title, **kw):
    return {'id':tid,'project_id':'p','title':title,'status':0,'tags':[],'meta':{}} | kw


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'context.db')
    monkeypatch.setattr(db, 'USE_POSTGRES', False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('AUTOSCHEDULER_INTAKE_MODEL', raising=False)
    db.init_db()
    extend_contextual_intake_phrases()
    # This unit fixture installs an inner layer; restore the production wrappers
    # afterward so later integration tests still exercise the complete runtime.
    monkeypatch.setattr(patch.intake, 'classify', patch.intake.classify)
    monkeypatch.setattr(patch.contract, 'review_intake', patch.contract.review_intake)
    monkeypatch.setattr(patch.contract, 'apply_reviewed_intake', patch.contract.apply_reviewed_intake)
    patch.install_contextual_intake_patch()


@pytest.mark.parametrize('text', [
    "I need to be home by 8:30pm.",
    "I'll be back home no later than 8:30 pm.",
    "I should get back by 8:30pm at the latest.",
    "8:30pm is the absolute latest time I'll be home.",
    "The maximum time I'll arrive back home is 8:30pm.",
])
def test_return_home_paraphrases_compile_to_same_hard_upper_bound(text):
    parsed = asyncio.run(contextual_review_intake(text, [], CFG, NOW))
    assert parsed['tasks'] == []
    assert parsed['context']['return_home_not_after'].startswith('2026-10-03T20:30')
    assert not parsed.get('blocking_conflicts')
    assert any('home no later than 20:30' in note for note in parsed['notes'])


def test_exact_church_dinner_return_wording_is_context_not_task_creation():
    text = "I'm going to church then eat dinner, so I will be outside for a period of time. 8:30pm is the absolute max time I'll be back."
    rows = [row('church','Church',tags=['fixed']), row('swim','Swimming'), row('math','Do Math')]
    parsed = asyncio.run(contextual_review_intake(text, rows, CFG, NOW))
    assert parsed['tasks'] == []
    assert parsed['context']['return_home_not_after'].startswith('2026-10-03T20:30')
    assert not any(t.get('action') == 'create' for t in parsed['tasks'])


def test_conflicting_return_bounds_fail_closed():
    text = "I won't be back home until 9pm. I need to be home by 8:30pm."
    parsed = asyncio.run(contextual_review_intake(text, [], CFG, NOW))
    assert parsed['context']['return_home_not_before'].startswith('2026-10-03T21:00')
    assert parsed['context']['return_home_not_after'].startswith('2026-10-03T20:30')
    assert parsed['blocking_conflicts']
    with pytest.raises(Exception) as exc:
        contextual_apply_reviewed_intake(text, [], CFG, parsed['preview_id'], NOW)
    assert 'conflicting real-life timing' in str(exc.value)


def _semantic_response(document):
    return httpx.Response(
        200,
        json={'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(document)}]}]},
        request=httpx.Request('POST','https://api.openai.com/v1/responses'),
    )


def test_grounded_semantic_can_resolve_pronoun_only_to_known_task(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_MODEL','configured-model')
    text = 'Do that after Read physics.'
    rows = [row('math','Do Math'), row('read','Read physics')]
    async def fake(*args, **kwargs):
        packet = json.loads(kwargs['json']['input'])['live_context']
        assert {x['id'] for x in packet['existing_tasks']} == {'math','read'}
        return _semantic_response({
            'intents':[{
                'kind':'relationship','text':text,'payload':text,
                'references':['math','read'],'resolved_payload':'Do Math after Read physics'
            }],
            'assumptions':[], 'conflicts':[]
        })
    monkeypatch.setattr(httpx.AsyncClient,'post',fake)
    parsed = asyncio.run(contextual_review_intake(text, rows, CFG, NOW))
    assert parsed['interpreter_mode'] == 'semantic-grounded'
    math = next(x for x in parsed['tasks'] if x.get('task_id') == 'math')
    assert math['meta_patch']['dependencies'] == ['read']


def test_semantic_unknown_reference_is_rejected_and_falls_back(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_MODEL','configured-model')
    text = 'Do that after Read physics.'
    rows = [row('read','Read physics')]
    async def fake(*args, **kwargs):
        return _semantic_response({
            'intents':[{
                'kind':'relationship','text':text,'payload':text,
                'references':['missing','read'],'resolved_payload':'Do Math after Read physics'
            }],
            'assumptions':[], 'conflicts':[]
        })
    monkeypatch.setattr(httpx.AsyncClient,'post',fake)
    parsed = asyncio.run(contextual_review_intake(text, rows, CFG, NOW))
    assert parsed['interpreter_mode'] == 'local-fallback'
    assert not any(x.get('task_id') == 'missing' for x in parsed['tasks'])
    assert any('conservative local' in w for w in parsed['warnings'])


def test_semantic_receives_prior_day_facts_and_planning_bounds(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_MODEL','configured-model')
    import app.service as service
    monkeypatch.setattr(service, 'get_quick_context', lambda: {
        'date':'2026-10-03','completed_meals':{'breakfast':'2026-10-03T11:15:00+08:00'},
        'wake_time':'08:00','actual_wake_reported':True
    })
    text = 'Replan today.'
    async def fake(*args, **kwargs):
        payload = json.loads(kwargs['json']['input'])
        assert payload['live_context']['current_day_context']['wake_time'] == '08:00'
        assert payload['live_context']['current_day_context']['completed_meals']['breakfast'].startswith('2026-10-03T11:15')
        assert payload['live_context']['planning_bounds']['sleep_start'] == '23:00'
        return _semantic_response({
            'intents':[{'kind':'replan','text':text,'payload':text,'references':[],'resolved_payload':None}],
            'assumptions':[], 'conflicts':[]
        })
    monkeypatch.setattr(httpx.AsyncClient,'post',fake)
    parsed = asyncio.run(contextual_review_intake(text, [row('swim','Swimming')], CFG, NOW))
    assert parsed['semantic_context_used'] is True


def test_semantic_conflict_is_visible_and_blocks_apply(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_MODEL','configured-model')
    text = 'Replan today.'
    async def fake(*args, **kwargs):
        return _semantic_response({
            'intents':[{'kind':'replan','text':text,'payload':text,'references':[],'resolved_payload':None}],
            'assumptions':[], 'conflicts':['Two explicit time bounds cannot both be satisfied.']
        })
    monkeypatch.setattr(httpx.AsyncClient,'post',fake)
    parsed = asyncio.run(contextual_review_intake(text, [], CFG, NOW))
    assert parsed['blocking_conflicts']
    assert parsed['clarifications']
    with pytest.raises(Exception):
        contextual_apply_reviewed_intake(text, [], CFG, parsed['preview_id'], NOW)


def test_return_deadline_constrains_flexible_outing_bundle(monkeypatch):
    captured = {}
    def fake(tasks, metas, busy, start, horizon, cfg, mastery):
        captured.update(deepcopy(metas))
        return [], [], {}
    monkeypatch.setattr(patch, '_BASE_PLAN', fake)
    ctx = {
        'date':'2026-10-03',
        'intent_today_ids':['swim'],
        'return_home_not_after':'2026-10-03T20:30:00+08:00'
    }
    tasks = [Task('swim','p','Swimming')]
    contextual_intent_aware_plan(tasks, {'swim':{'duration_minutes':90}}, [], NOW, 1, CFG|{'_quick_context':ctx}, {})
    assert captured['swim']['latest_end'].startswith('2026-10-03T20:30')
    assert captured['swim']['must_finish'] is True


@pytest.mark.parametrize('text', [
    "I will be outside after church.",
    "We'll be out after dinner.",
    "I'll be away until later.",
    "I should be back home by 9pm.",
    "9pm is the latest I'll be home.",
    "I won't be home until 8pm.",
])
def test_whereabouts_language_never_becomes_new_task(text):
    parsed = asyncio.run(contextual_review_intake(text, [], CFG, NOW))
    assert not [x for x in parsed['tasks'] if x.get('action') == 'create']
