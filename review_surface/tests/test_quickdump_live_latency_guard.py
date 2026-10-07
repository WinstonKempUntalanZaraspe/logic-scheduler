from __future__ import annotations

import asyncio
from datetime import datetime

from app.config import settings
from app import semantic_cost_gate as gate
from app import quickdump_latency_patch as latency
from app.semantic_plan import LOCAL_REVIEW

NOW = datetime(2026, 10, 4, 20, 0, tzinfo=settings.tz)


def test_simple_quickdump_is_forced_local_without_semantic_call():
    calls = []

    async def review(text, rows, config, now):
        calls.append(bool(LOCAL_REVIEW.get()))
        assert LOCAL_REVIEW.get() is True
        return {'tasks': [], 'warnings': [], 'interpreter_mode': 'local'}

    parsed = asyncio.run(latency._quickdump_review(
        review, 'Do Math then Physics.', [], {}, NOW
    ))

    assert calls == [True]
    assert parsed['interpreter_mode'] == 'local'
    assert parsed['preview_local_first'] is True


def test_referential_prompt_keeps_semantic_available():
    assert not gate.is_simple_local_prompt('Do that after Read physics.')
    assert gate.is_simple_local_prompt('Do Math after that, then Physics.')


def test_complex_quickdump_timeout_retries_in_forced_local_mode(monkeypatch):
    calls = []

    async def review(text, rows, config, now):
        calls.append(bool(LOCAL_REVIEW.get()))
        if LOCAL_REVIEW.get():
            return {'tasks': [], 'warnings': [], 'interpreter_mode': 'local'}
        await asyncio.sleep(0.05)
        return {'tasks': [], 'warnings': [], 'interpreter_mode': 'semantic'}

    monkeypatch.setattr(latency, 'semantic_timeout_seconds', lambda _cfg=None: 0.01)
    parsed = asyncio.run(latency._quickdump_review(
        review, 'If it rains, replace swimming with Physics.', [], {}, NOW
    ))

    assert calls == [False, True]
    assert parsed['interpreter_mode'] == 'local'
    assert parsed['preview_timeout_recovered'] is True
    assert any('recovered' in str(x).lower() for x in parsed['warnings'])


def test_forced_local_review_restores_context_after_success():
    async def review(text, rows, config, now):
        assert LOCAL_REVIEW.get() is True
        return {'tasks': [], 'warnings': [], 'interpreter_mode': 'local'}

    async def scenario():
        LOCAL_REVIEW.set(False)
        result = await latency._forced_local_review(review, 'Replan today.', [], {}, NOW)
        return result, LOCAL_REVIEW.get()

    parsed, after = asyncio.run(scenario())
    assert parsed['interpreter_mode'] == 'local'
    assert after is False


def test_semantic_timeout_is_bounded_for_interactive_use(monkeypatch):
    monkeypatch.setenv('AUTOSCHEDULER_SEMANTIC_TIMEOUT_SECONDS', '999')
    assert gate.semantic_timeout_seconds({}) == 8.0
    monkeypatch.setenv('AUTOSCHEDULER_SEMANTIC_TIMEOUT_SECONDS', '0.1')
    assert gate.semantic_timeout_seconds({}) == 2.5



def test_early_job_sleep_chain_is_forced_local_without_semantic_call():
    calls = []

    async def review(text, rows, config, now):
        calls.append(bool(LOCAL_REVIEW.get()))
        assert LOCAL_REVIEW.get() is True
        return {'tasks': [], 'warnings': [], 'interpreter_mode': 'local'}

    prompt = 'I actually have a job at 7am tmr, 1h 15 travel time, 5 am need wake up, shower etc.'
    parsed = asyncio.run(latency._quickdump_review(
        review, prompt, [], {}, datetime(2026, 10, 7, 21, 33, tzinfo=settings.tz)
    ))

    assert calls == [True]
    assert parsed['preview_local_first'] is True
