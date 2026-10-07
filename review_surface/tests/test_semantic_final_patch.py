import asyncio

import httpx

from app import semantic_final_patch as final
from app.semantic_resilience_patch import ResilientPlanMeaning


def _meaning(steps, assumptions=None):
    return ResilientPlanMeaning.model_validate({
        'target_date': '2026-10-04',
        'steps': steps,
        'assumptions': assumptions or [],
        'conflicts': [],
    })


def _step(source, kind, *, title=None, duration=None, evidence=None, location=None, policy=None, optional=False, references=None, question=None):
    return {
        'source': source,
        'kind': kind,
        'title': title,
        'references': references or [],
        'duration_minutes': duration,
        'duration_evidence': evidence,
        'optional': optional,
        'location': location,
        'policy': policy,
        'question': question,
    }


def test_current_home_alias_and_inferred_step_locations_do_not_break_semantic_validation():
    text = 'Home already. Gimme 15 mins to recharge, then work on physics for 35m.'
    meaning = _meaning([
        _step('Home already.', 'location', title='Current home', location='Current home'),
        _step('Gimme 15 mins to recharge,', 'rest', title='recharge', duration=15, evidence='15 mins', location='Current home'),
        _step('work on physics for 35m.', 'activity', title='work on physics', duration=35, evidence='35m', location='Current home'),
    ], ['“Home already” refers to the saved Current home location.'])

    safe, facts = final._prepare_meaning(meaning, text)
    assert facts['home'] is True
    assert safe.steps[0].kind == 'history'
    assert safe.steps[0].location is None
    assert safe.steps[1].location is None
    assert safe.steps[2].location is None
    assert safe.steps[1].source.endswith(' then ')


def test_current_meal_can_use_semantic_canonical_title_even_with_casual_spelling():
    text = "I'm eating my lunc now, then study Math."
    meaning = _meaning([
        _step("I'm eating my lunc now,", 'meal', title='lunch'),
        _step('study Math.', 'activity', title='study Math'),
    ], ['“lunc” means lunch.'])

    safe, facts = final._prepare_meaning(meaning, text)
    assert facts['meal'] == 'lunch'
    assert safe.steps[0].kind == 'history'
    assert safe.steps[0].location is None
    assert safe.steps[0].source.endswith(' then ')


def test_only_connector_glue_is_allowed_to_fill_model_source_gaps():
    assert final._safe_connector_gap('; then ')
    assert final._safe_connector_gap(', and afterwards ')
    assert not final._safe_connector_gap(' because I changed my mind ')
    assert not final._safe_connector_gap(' before Church ')


def test_explicit_location_on_its_own_activity_is_preserved():
    text = 'Study Physics at the library for 30 minutes.'
    meaning = _meaning([
        _step('Study Physics at the library for 30 minutes.', 'activity', title='Study Physics', duration=30, evidence='30 minutes', location='library')
    ])
    safe, facts = final._prepare_meaning(meaning, text)
    assert facts == {'home': False, 'meal': None}
    assert safe.steps[0].location == 'library'


def test_logistics_instruction_fragments_never_become_support_tasks():
    text = 'Include travel, changing, showering and returning home separately, using my saved locations and travel times.'
    meaning = _meaning([
        _step('Include travel,', 'activity', title='Travel to the pool', location='Nearest ActiveSG stadium'),
        _step('changing,', 'activity', title='Changing', location='Nearest ActiveSG stadium'),
        _step('showering', 'activity', title='Showering', location='Nearest ActiveSG stadium'),
        _step('and returning home separately,', 'activity', title='Returning home', location='Current home'),
        _step('using my saved locations and travel times.', 'instruction'),
    ])
    safe, _ = final._prepare_meaning(meaning, text)
    assert safe.steps[0].kind == 'instruction'
    assert safe.steps[0].policy == 'outing_integrity'
    assert all(step.kind == 'history' for step in safe.steps[1:])
    assert all(step.title is None for step in safe.steps)


def test_relation_only_fragments_cannot_invent_missing_tasks():
    text = 'then go to Church from 5-6pm. After Church, have dinner.'
    meaning = _meaning([
        _step('then go to', 'activity', title='Travel to Church', location='Church'),
        _step('Church from 5-6pm.', 'activity', title='Church', references=['church']),
        _step('After Church,', 'activity', title='Return home', location='Current home'),
        _step('have dinner.', 'meal', title='Dinner'),
    ])
    safe, _ = final._prepare_meaning(meaning, text)
    assert safe.steps[0].kind == 'history'
    assert safe.steps[2].kind == 'history'
    assert safe.steps[1].kind == 'activity' and safe.steps[1].references == ['church']


def test_unmapped_conversational_instruction_becomes_context_not_failure():
    meaning = _meaning([_step('No rush', 'instruction', title='No rush')])
    safe, _ = final._prepare_meaning(meaning, 'No rush')
    assert safe.steps[0].kind == 'history'
    assert safe.steps[0].policy is None


def test_semantic_transport_uses_long_read_timeout_without_blind_retry(monkeypatch):
    calls = []
    seen = []

    class Client:
        def __init__(self, **kwargs):
            seen.append(kwargs['timeout'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls.append(1)
            raise httpx.ConnectError('temporary fault', request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))

    monkeypatch.setattr(final.httpx, 'AsyncClient', Client)
    try:
        asyncio.run(final._post_once({'model': 'test'}, 'secret', 'test'))
    except httpx.ConnectError:
        pass
    else:
        raise AssertionError('expected connection failure')

    assert len(calls) == 1
    assert len(seen) == 1
    assert seen[0].read == 70.0
