from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.config import settings
from app import semantic_cost_gate as gate
from app import semantic_plan

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=settings.tz)


@pytest.mark.parametrize('text', [
    'Woke up at 8am. Replan today.',
    "I'm home now. Replan today.",
    'I got interrupted for 30 minutes. Reschedule my day.',
    'Do Math then Physics.',
    'Swim then do Math.',
    'Study calculus then read Bible.',
    "I'm eating lunch now. Replan today.",
    'I am tired. Reorganise the rest of today.',
])
def test_obviously_simple_prompts_route_local(text):
    assert gate.is_simple_local_prompt(text), text


@pytest.mark.parametrize('text', [
    'If it rains, do that instead of swimming.',
    'Maybe do Math or Physics depending on how I feel.',
    'Move it after the other one.',
    'Do whichever task makes the most sense after that, but only if I get home early.',
    'I have a complicated day: church, dinner outside, maybe swimming, and if my family stays out late replace it with study.',
    'What should I do after church?',
])
def test_ambiguous_or_complex_prompts_keep_semantic_available(text):
    assert not gate.is_simple_local_prompt(text), text


def test_simple_prompt_never_calls_paid_interpreter():
    calls = []

    async def paid(text, rows, config, now):
        calls.append(text)
        return ('normalized', [], [], object())

    wrapped = gate.make_cost_aware_interpreter(paid)
    result = asyncio.run(wrapped('Do Math then Physics.', [], {}, NOW))
    assert result is None
    assert calls == []


def test_complex_prompt_still_calls_paid_interpreter():
    calls = []

    async def paid(text, rows, config, now):
        calls.append(text)
        return ('normalized', [], [], object())

    wrapped = gate.make_cost_aware_interpreter(paid)
    text = 'If it rains, do that instead of swimming.'
    result = asyncio.run(wrapped(text, [], {}, NOW))
    assert result is not None
    assert calls == [text]


def test_semantic_timeout_falls_back_instead_of_hanging(monkeypatch):
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return ('late', [], [], object())

    monkeypatch.setattr(gate, 'semantic_timeout_seconds', lambda _cfg=None: 0.01)
    wrapped = gate.make_cost_aware_interpreter(slow)

    async def scenario():
        semantic_plan.LAST_ATTEMPT.set(None)
        result = await wrapped('If it rains, study Physics instead.', [], {}, NOW)
        return result, semantic_plan.LAST_ATTEMPT.get()

    result, attempt = asyncio.run(scenario())
    assert result is None
    assert attempt['code'] == 'semantic-time-budget-exceeded'


def test_timeout_environment_is_bounded(monkeypatch):
    monkeypatch.setenv('AUTOSCHEDULER_SEMANTIC_TIMEOUT_SECONDS', '999')
    assert gate.semantic_timeout_seconds({}) == 8.0
    monkeypatch.setenv('AUTOSCHEDULER_SEMANTIC_TIMEOUT_SECONDS', '0.1')
    assert gate.semantic_timeout_seconds({}) == 2.5
