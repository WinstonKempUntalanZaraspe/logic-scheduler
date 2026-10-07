import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import contextual_intake_patch as ctx
import app.semantic_compat_patch as compat

TZ = ZoneInfo('Asia/Singapore')
NOW = datetime(2026, 10, 3, 14, 30, tzinfo=TZ)
CFG = {'wake_time':'07:00','day_start':'07:00','sleep_start':'23:00','day_end':'23:00'}


def _response(status, body):
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request('POST','https://api.openai.com/v1/responses'),
    )


def _semantic_body(text='Replan today.'):
    doc = {
        'intents':[{
            'kind':'replan','text':text,'payload':text,
            'references':[],'resolved_payload':None,
        }],
        'assumptions':[],
        'conflicts':[],
    }
    return {
        'status':'completed',
        'output':[{'content':[{'type':'output_text','text':json.dumps(doc)}]}],
    }


async def _primary_fallback(text, now, rows, config, prior):
    return ctx.intake.extract_intents(text, now), 'local-fallback', 'primary strict request failed', [], []


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_MODEL','configured-model')
    monkeypatch.delenv('AUTOSCHEDULER_INTAKE_FALLBACK_MODEL', raising=False)
    monkeypatch.setattr(compat, '_BASE_GROUNDED', _primary_fallback)


def test_same_configured_model_recovers_in_plain_json_mode(monkeypatch):
    seen=[]
    async def fake_post(self, url, **kwargs):
        seen.append(kwargs['json']['model'])
        return _response(200, _semantic_body())
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    result = asyncio.run(compat.resilient_grounded_semantic_document('Replan today.', NOW, [], CFG, {}))
    document, mode, warning, assumptions, conflicts = result
    assert mode == 'semantic-grounded'
    assert seen == ['configured-model']
    assert 'plain-JSON compatibility mode' in warning
    assert document.intents[0].kind == 'replan'


def test_http400_configured_model_recovers_with_fallback_model(monkeypatch):
    monkeypatch.setenv('AUTOSCHEDULER_INTAKE_FALLBACK_MODEL', 'test-fallback-model')
    seen=[]
    async def fake_post(self, url, **kwargs):
        model=kwargs['json']['model']; seen.append(model)
        if model == 'configured-model':
            return _response(400, {'error':{'message':'bad model/request'}})
        return _response(200, _semantic_body())
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    result = asyncio.run(compat.resilient_grounded_semantic_document('Replan today.', NOW, [], CFG, {}))
    assert result[1] == 'semantic-grounded'
    assert seen == ['configured-model', 'test-fallback-model']
    assert 'compatibility model `test-fallback-model`' in result[2]


def test_credentials_failure_does_not_model_hop(monkeypatch):
    seen=[]
    async def fake_post(self, url, **kwargs):
        seen.append(kwargs['json']['model'])
        return _response(401, {'error':{'message':'invalid key'}})
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    result = asyncio.run(compat.resilient_grounded_semantic_document('Replan today.', NOW, [], CFG, {}))
    assert result[1] == 'local-fallback'
    assert seen == ['configured-model']


def test_unknown_task_reference_is_rejected_even_in_compatibility_mode(monkeypatch):
    bad = {
        'status':'completed',
        'output':[{'content':[{'type':'output_text','text':json.dumps({
            'intents':[{
                'kind':'relationship','text':'Do that after Read physics.','payload':'Do that after Read physics.',
                'references':['missing'],'resolved_payload':'Do Math after Read physics',
            }],
            'assumptions':[], 'conflicts':[],
        })}]}],
    }
    async def fake_post(self, url, **kwargs):
        return _response(200, bad)
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    result = asyncio.run(compat.resilient_grounded_semantic_document(
        'Do that after Read physics.', NOW,
        [{'id':'read','title':'Read physics','status':0,'tags':[],'meta':{}}], CFG, {}
    ))
    assert result[1] == 'local-fallback'


def test_model_may_omit_clause_and_safe_local_clause_is_merged(monkeypatch):
    # Semantic model resolves the replan line but omits the output/control line;
    # compatibility mode keeps the safe local clause instead of rejecting everything.
    text='Replan today.\nShow the cleaned schedule.'
    body = _semantic_body('Replan today.')
    async def fake_post(self, url, **kwargs):
        return _response(200, body)
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    result = asyncio.run(compat.resilient_grounded_semantic_document(text, NOW, [], CFG, {}))
    assert result[1] == 'semantic-grounded'
    assert any(i.kind == 'replan' for i in result[0].intents)
    assert len(result[0].intents) >= 2


def test_semantic_model_cannot_promote_ambiguous_prose_to_task(monkeypatch):
    bad = {
        'status':'completed',
        'output':[{'content':[{'type':'output_text','text':json.dumps({
            'intents':[{
                'kind':'task','text':'How do I look?','payload':'How do I look?',
                'references':[],'resolved_payload':None,
            }],
            'assumptions':[], 'conflicts':[],
        })}]}],
    }
    async def fake_post(self, url, **kwargs):
        return _response(200, bad)
    monkeypatch.setattr(httpx.AsyncClient, 'post', fake_post)
    document, mode, warning, _, _ = asyncio.run(
        ctx.grounded_semantic_document('How do I look?', NOW, [], CFG, {})
    )
    assert mode == 'local-fallback'
    assert document.intents[0].kind == 'ambiguous'
    assert 'failed validation' in warning.lower()
