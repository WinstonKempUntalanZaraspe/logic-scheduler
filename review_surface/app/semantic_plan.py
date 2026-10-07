"""Ground unfamiliar language in a small plan representation before scheduling.

The model interprets meaning. The existing compiler owns timing, task identity,
travel, capacity and review. No model response writes external tasks directly.
"""
from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar
from datetime import date, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import settings

LOCAL_REVIEW = ContextVar('local_plan_review', default=False)
LAST_ATTEMPT = ContextVar('semantic_plan_attempt', default=None)
_INSTALLED = False


class PlanStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str
    kind: Literal['plan', 'activity', 'current_activity', 'location', 'rest', 'meal', 'sleep', 'instruction', 'output', 'history', 'question']
    title: str | None
    references: list[str]
    duration_minutes: int | None = Field(ge=1, le=1440, strict=True)
    duration_evidence: str | None
    optional: bool
    location: str | None
    policy: Literal['submission_clock', 'protect_sleep', 'productive_time', 'outing_integrity', 'protect_fixed', 'preserve_effort'] | None
    question: str | None


class PlanMeaning(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_date: str
    steps: list[PlanStep]
    assumptions: list[str]
    conflicts: list[str]


_SYSTEM = """Interpret an ordinary real-life planning request into atomic PlanSteps.
Do not schedule, execute, complete, delete or move saved events. Use the supplied
local time, saved places/travel, active tasks and latest context. Every source is
an exact, ordered, non-overlapping excerpt of the request. Cover ALL significant
words, including explanations, qualifications, instructions and questions.

Interpret meaning, slang, typos, unfamiliar verbs, references and implicit order;
do not require the user to write commands. Distinguish activity from current state,
history, recovery, meal, planning instructions and requested output. 'current_activity'
means already underway AS the user sends this; reserve REMAINING time from local_time.
Future work is activity. A location alone is not an indefinite busy reservation.
New updates replace stale intentions. Use home as location when the user clearly
means their current home; another named place stays its stated name.

Use references only for existing actionable task IDs in live_context, excluding
notes. New activity titles must retain the user's words, optionally adding a simple
work verb. Do not translate unfamiliar activity words into guessed different tasks.
Combined work can be one activity ('Math and Physics'); preserve AND versus OR.
Maybe/if time allows is optionality. A genuine external condition or unclear pronoun
needs a focused question when its meaning cannot be established from context.

Durations belong ONLY to their own step. duration_evidence quotes the exact duration
phrase. Do not invent a duration: null uses a disclosed saved/default estimate.
'Two hours swimming' means 120 actual swimming minutes; travel/change/shower remain
separate. Already-spent time is history, not remaining work. Preserve explicit
time ranges in source; the compiler checks them against saved fixed/recurring events.
Use the stated date; otherwise target_date is the date of local_time. Never assume
the entire day starts at morning when it is already afternoon.

Supported generic policy instructions are submission_clock, protect_sleep,
productive_time, outing_integrity, protect_fixed, preserve_effort. Map paraphrases
to these capabilities. Never discard a numeric/custom restriction by mapping it
to a generic policy; use question if it needs an unsupported rule. Terminal sleep
uses saved bedtime/wind-down. Preserve recovery when filling spare time.

Personal facts/history are not authority to create work. Questions, jokes posed as
questions, hypothetical examples and negatives are not positive task instructions.
If something is truly unresolved, include a concise focused question while retaining
the other understood steps. Expose semantic assumptions, especially inferred meals
or location aliases. Put contradictory facts in conflicts. An overfull wish list
is a scheduling/capacity issue, not a contradictory fact: retain its activities
and let the scheduler show what fits. Do not invent missing
tasks, source text, task IDs, clock times, addresses or completion. All fields must
be present; unused fields are null, false or []."""

_POLICIES = {
    'submission_clock': 'Plan from the current local time',
    'protect_sleep': 'Respect my usual bedtime and wind-down time',
    'productive_time': 'Use available gaps for useful work while keeping recovery',
    'outing_integrity': 'Keep physical outing chains and travel intact',
    'protect_fixed': 'Preserve fixed commitments',
    'preserve_effort': 'Treat unfinished work as unfinished',
}

_CREDIT_CODES = {'credit_balance_exhausted', 'insufficient_quota'}
_ACCOUNT_LIMIT_CODES = {'organization_spend_limit_exceeded', 'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'}


def record_provider_state(state, *, model=None, http_status=None, code=None):
    """Persist only safe availability facts; never provider text or credentials."""
    from .db import set_kv
    status = {'state': state, 'model': model or os.getenv('AUTOSCHEDULER_INTAKE_MODEL', 'gpt-6-luna').strip(),
              'checked_at': datetime.now(settings.tz).isoformat()}
    if http_status is not None:
        status['http_status'] = int(http_status)
    if code in _CREDIT_CODES | _ACCOUNT_LIMIT_CODES | {'rate_limit_exceeded', 'slow_down', 'model_not_found', 'invalid_api_key'}:
        status['reason'] = code
    set_kv('semantic_provider_state', json.dumps(status))


def _failed_attempt(code, summary):
    LAST_ATTEMPT.set({'code': code, 'summary': summary})


def _provider_failure(response, model):
    try:
        provider_code = (response.json().get('error') or {}).get('code')
    except (ValueError, TypeError, AttributeError):
        provider_code = None
    record_provider_state('unavailable', model=model, http_status=response.status_code, code=provider_code)
    if response.status_code == 429 and provider_code in _CREDIT_CODES:
        _failed_attempt('semantic-credits-unavailable',
            'Luna is unavailable because this API account has no usable credits. This review uses local interpretation; unfamiliar wording may need clarification.')
    elif response.status_code == 429 and provider_code in _ACCOUNT_LIMIT_CODES:
        _failed_attempt('semantic-usage-unavailable', 'Model interpretation is unavailable because the API account reached its usage or spending limit. This review uses local interpretation.')
    elif response.status_code == 429:
        _failed_attempt('semantic-rate-limited', 'Model interpretation is temporarily busy. This review uses local interpretation; try again shortly for unfamiliar wording.')
    elif response.status_code in {401, 403}:
        _failed_attempt('semantic-access-unavailable', 'Model interpretation could not access the configured API account. This review uses local interpretation.')
    else:
        _failed_attempt('semantic-request-unavailable', 'Model interpretation is temporarily unavailable. This review uses local interpretation and retains any questions.')


def _words(text):
    return set(re.findall(r'[a-z0-9]+', str(text).lower()))


def compile_meaning(meaning, text, rows, now):
    """Validate provenance, then express meaning in the existing planner grammar."""
    from . import general_day_plan_patch as general, quickdump as qd
    from .day_plan_activities import positive_stage, _OPTIONAL
    from .live_activity import remaining_minutes
    from .schedule_instruction_patch import _day_from_clause
    from .personal_intents import reality_kind, task_command
    by_id = {str(r['id']): r for r in rows if r.get('id') and int(r.get('status') or 0) == 0
             and str(r.get('kind') or 'TEXT').upper() != 'NOTE'
             and str(r.get('project_kind') or 'TASK').upper() != 'NOTE'}
    requested = general._target_day(text, now)
    hinted, explicit = _day_from_clause(text, now)
    expected_day = requested[0] if requested else hinted if explicit else now.date()
    target = date.fromisoformat(meaning.target_date)
    if target != expected_day or not meaning.steps or len(meaning.steps) > 60:
        raise ValueError('Date or stage coverage changed')
    cursor = 0
    body, policies, questions, evidence = [], [], [], []
    prefix_location, current = None, None
    for step in meaning.steps:
        pos = text.find(step.source, cursor)
        if not step.source.strip() or pos < 0 or re.search(r'\w', text[cursor:pos]):
            raise ValueError('Source coverage changed')
        cursor = pos + len(step.source)
        if any(rid not in by_id for rid in step.references):
            raise ValueError('Unknown or reference-only task')
        if task_command(step.source):
            raise ValueError('Task mutation must use its original compiler')
        if step.duration_minutes is not None:
            if not step.duration_evidence or step.duration_evidence not in step.source:
                raise ValueError('Duration has no source')
            stated = remaining_minutes(step.duration_evidence)
            if stated is not None and stated != step.duration_minutes:
                raise ValueError('Explicit duration changed')
            if stated is None and not meaning.assumptions:
                raise ValueError('Unverified duration needs a visible assumption')
        elif remaining_minutes(step.source) and step.kind in {'activity', 'current_activity', 'rest'}:
            raise ValueError('Explicit duration dropped')
        if step.location:
            if step.location != 'home' and step.location.lower() not in step.source.lower():
                raise ValueError('Location has no source')
            if step.location == 'home' and not reality_kind(step.source) == 'home' and 'home' not in step.source.lower() and not meaning.assumptions:
                raise ValueError('Home alias needs a visible assumption')
        evidence.append({'source': step.source, 'kind': step.kind, 'title': step.title,
                         'duration_minutes': step.duration_minutes, 'optional': step.optional, 'location': step.location})
        positive_source = _OPTIONAL.sub('', step.source).strip()
        if step.kind in {'activity', 'current_activity', 'rest'} and (not positive_stage(positive_source)
                or re.search(r"\b(?:if|unless|never|won't|don't|didn't|will\s+not|do\s+not)\b", positive_source, re.I)):
            raise ValueError('Non-positive text cannot authorize work')
        if step.kind == 'question':
            questions.append({'text': step.source, 'reason': step.question or 'What activity or timing do you mean here?'})
            continue
        if step.kind in {'plan', 'output', 'history'}:
            continue
        if step.kind == 'instruction':
            if not step.policy or remaining_minutes(step.source) is not None or re.search(r'\d|\b(?:at\s+least|at\s+most|only|no\s+more\s+than)\b', step.source, re.I):
                raise ValueError('Unsupported custom policy')
            policies.append(_POLICIES[step.policy])
            continue
        if step.kind == 'location':
            if prefix_location is not None or not step.location or not positive_stage(step.source) or re.search(r'\b(?:tomorrow|will|gonna|going\s+to|might|may)\b', step.source, re.I):
                raise ValueError('Unresolved current place')
            prefix_location = step.location
            continue
        if step.kind == 'sleep':
            body.append('sleep')
            continue
        if step.kind == 'meal':
            meal = str(step.title or '').lower()
            if meal not in {'breakfast', 'lunch', 'dinner'}:
                raise ValueError('Unresolved meal')
            if meal not in step.source.lower() and not meaning.assumptions:
                raise ValueError('Inferred meal needs a visible assumption')
            body.append('eat ' + meal + (' at ' + step.location if step.location else ''))
            continue
        if step.kind == 'rest':
            clocks = qd._extract_time_range(step.source, now)
            if re.search(r'\b(?:at|from|until|by)\s+\d', step.source, re.I) and not clocks:
                raise ValueError('Unresolved recovery clock')
            clock_text = f' from {clocks[0]:%H:%M}-{clocks[1]:%H:%M}' if clocks else ''
            body.append('Rest' + (f' for {step.duration_minutes} minutes' if step.duration_minutes else '') + clock_text)
            continue
        title = step.title or ''
        allowed = _words(step.source) | {'do', 'read', 'study', 'work', 'on', 'go', 'to', 'visit'}
        for rid in step.references:
            allowed |= _words(by_id[rid].get('title') or '')
        if not title.strip() or not _words(title) <= allowed or re.search(r'[\n;.]', title):
            raise ValueError('Task title introduced unsupported content')
        optional = step.optional or bool(_OPTIONAL.search(step.source))
        if re.search(r'\bor\b', step.source, re.I) and not re.search(r'\bor\b', title, re.I):
            raise ValueError('Choice was removed')
        duration = f' for {step.duration_minutes} minutes' if step.duration_minutes else ''
        clocks = qd._extract_time_range(step.source, now)
        clock_text = f' from {clocks[0]:%H:%M}-{clocks[1]:%H:%M}' if clocks else ''
        if step.kind == 'current_activity':
            if current or not step.duration_minutes or optional or target != now.date():
                raise ValueError('Unresolved current activity')
            if not re.search(r"\b(?:now|currently|already|still|here)\b|\b(?:i(?:'m| am)|we(?:'re| are))\s+\w+ing\b", step.source, re.I):
                raise ValueError('Activity was not reported underway')
            if re.search(r"\b(?:spent|ago|yesterday|finished|completed|have\s+been|i've\s+been)\b", step.source, re.I):
                raise ValueError('Elapsed work is not remaining time')
            current = (title, duration, step.location)
        else:
            body.append(('maybe ' if optional else '') + 'I will ' + title + duration + clock_text)
    if re.search(r'\w', text[cursor:]):
        raise ValueError('Source clauses omitted')
    prefix = ''
    if current:
        title, duration, location = current
        place = location or prefix_location
        prefix = (f"I'm at {place} now, I will {title}{duration}, then " if place
                  else f"I'm working on {title}{duration}, then ")
    elif prefix_location and target == now.date():
        prefix = f"I'm at {prefix_location} now, "
    # The target header precedes facts so future-day reports cannot become live
    # reservations today. A current activity always uses today's submission clock.
    normalized = f'Plan {target.isoformat()}. ' + prefix + ', then '.join(body)
    if policies:
        normalized += '. ' + '. '.join(policies)
    return normalized, questions, evidence


async def interpret_plan(text, rows, config, now):
    from .contextual_intake_patch import _safe_context_packet
    from .service import get_quick_context
    key = os.getenv('OPENAI_API_KEY', '').strip()
    model = os.getenv('AUTOSCHEDULER_INTAKE_MODEL', 'gpt-6-luna').strip()
    LAST_ATTEMPT.set(None)
    if not key or not model:
        return None
    try:
        packet = _safe_context_packet(rows, config, config.get('_quick_context') or get_quick_context() or {}, now)
        async with httpx.AsyncClient(timeout=25.0) as client:
            request = {'model': model, 'store': False, 'instructions': _SYSTEM,
                       'input': json.dumps({'request': text, 'live_context': packet}),
                       'text': {'format': {'type': 'json_schema', 'name': 'plan_meaning', 'strict': True,
                                           'schema': PlanMeaning.model_json_schema()}},
                       'max_output_tokens': 10000}
            response = await client.post('https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer ' + key}, json=request)
            # Only a rejected structured-output request merits one same-model
            # compatibility attempt. Authentication/quota/network failures do not.
            if response.status_code == 400 and any(label in response.text.lower() for label in ('json_schema', 'structured output', 'response_format', 'text.format')):
                request.pop('text')
                request['instructions'] += '\nReturn only JSON matching this schema, without commentary:\n' + json.dumps(PlanMeaning.model_json_schema())
                response = await client.post('https://api.openai.com/v1/responses',
                    headers={'Authorization': 'Bearer ' + key}, json=request)
            if response.is_error:
                _provider_failure(response, model)
            response.raise_for_status()
            payload = response.json()
            record_provider_state('reachable', model=model, http_status=response.status_code)
        if payload.get('status') != 'completed':
            raise ValueError('Incomplete interpretation')
        from .semantic_compat_patch import _strip_json_fence
        content = _strip_json_fence(''.join(c['text'] for item in payload.get('output', []) for c in item.get('content', []) if c.get('type') == 'output_text'))
        meaning = PlanMeaning.model_validate_json(content)
        compiled = compile_meaning(meaning, text, rows, now)
        LAST_ATTEMPT.set({'code': 'semantic-active', 'summary': 'Context-grounded plan interpretation succeeded.'})
        return (*compiled, meaning)
    except httpx.HTTPStatusError:
        return None
    except httpx.RequestError:
        record_provider_state('unavailable', model=model)
        _failed_attempt('semantic-network-unavailable', 'Model interpretation could not finish over the network. This review uses local interpretation.')
        return None
    except (ValueError, KeyError, TypeError):
        _failed_attempt('semantic-validation-fallback', 'Model interpretation could not safely resolve this wording. This review uses local interpretation and retains any questions.')
        return None


def install_semantic_plan():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    from . import intake_contract as contract
    from .day_plan_activities import fragments
    from .personal_intents import task_command
    base = contract.review_intake

    async def local_review(text, rows, config, now):
        token = LOCAL_REVIEW.set(True)
        try:
            return await base(text, rows, config, now)
        finally:
            LOCAL_REVIEW.reset(token)

    async def review(text, rows, config, now=None):
        now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        # Native metadata and explicit mutations retain their existing reviewed
        # compiler. Semantic normalization never grants mutation authority.
        from .language_intake import _CREATION, clauses
        clauses_now = list(clauses(text))
        if any(task_command(c) or _CREATION.match(c) for c, _ in clauses_now) or re.search(r'(?im)^\s*(?:description|due|reminder|repeat|checklist|subtask|note)\s*:', str(text)):
            return await base(text, rows, config, now)
        local = await local_review(text, rows, config, now)
        ctx = local.get('context') or {}
        # These established compilers represent rich journey/conditional/native
        # state that the smaller meaning schema intentionally does not replace.
        if not local.get('clarifications') and any(ctx.get(k) for k in ('journey_state', 'return_home_not_before', 'return_home_not_after', 'conditional_branches', 'fixed_overrides')):
            return local
        complex_request = len(fragments(str(text))) > 1
        if not local.get('clarifications') and not complex_request:
            return local
        interpreted = await interpret_plan(text, rows, config, now)
        if not interpreted:
            diagnostic = LAST_ATTEMPT.get()
            if diagnostic:
                local['semantic_diagnostic'] = diagnostic
                local['semantic_degraded'] = True
                local.setdefault('warnings', []).append(diagnostic['summary'])
                contract.set_kv('intake_review', json.dumps({'text_hash': contract._fingerprint(text),
                    'snapshot': contract._snapshot(rows, config), 'expires_at': local['expires_at'], 'parsed': local}))
            return local
        normalized, questions, evidence, meaning = interpreted
        parsed = await local_review(normalized, rows, config, now)
        # If normalization cannot compile, keep a successful deterministic
        # interpretation. Never downgrade working input because a model failed.
        if (parsed.get('clarifications') and not local.get('clarifications')) or any(c.get('action') not in {'create', 'update'} for c in parsed.get('tasks') or []):
            contract.set_kv('intake_review', json.dumps({'text_hash': contract._fingerprint(text),
                'snapshot': contract._snapshot(rows, config), 'expires_at': local['expires_at'], 'parsed': local}))
            return local
        parsed['clarifications'].extend(questions)
        # Preserve verified facts from the original local pass even when the
        # semantic representation groups them under history/explanation.
        ctx = parsed.setdefault('context', {}) or {}
        for name in ('completed_meals', 'wake_time', 'actual_wake_reported', 'reported_clock'):
            fact = (local.get('context') or {}).get(name)
            if fact is not None:
                ctx[name] = fact if name != 'completed_meals' else fact | ctx.get(name, {})
        for name in ('day_plan', 'tomorrow_plan'):
            original_plan = (local.get('context') or {}).get(name) or {}
            new_plan = ctx.get(name) or {}
            if original_plan.get('date') == new_plan.get('date'):
                for clock in ('wake_time', 'sleep_start'):
                    if original_plan.get(clock):
                        new_plan[clock] = original_plan[clock]
                ctx[name] = new_plan
        parsed['context'] = ctx
        for conflict in meaning.conflicts:
            parsed.setdefault('blocking_conflicts', []).append(conflict)
            parsed['clarifications'].append({'text': text, 'reason': conflict})
        parsed['interpreter_mode'] = 'semantic'
        parsed['semantic_context_used'] = True
        parsed['semantic_plan_steps'] = evidence
        parsed['semantic_diagnostic'] = {'code': 'semantic-active', 'summary': 'Context-grounded plan interpretation succeeded.'}
        parsed.pop('semantic_degraded', None)
        parsed['notes'] = [n for n in parsed['notes'] if not n.startswith('Deterministic intake resolved')]
        parsed['notes'].extend('Assumption: ' + a for a in meaning.assumptions)
        parsed['warnings'].extend(q['reason'] for q in questions)
        contract.validate_task_creation(parsed)
        contract.set_kv('intake_review', json.dumps({'text_hash': contract._fingerprint(text),
            'snapshot': contract._snapshot(rows, config), 'expires_at': parsed['expires_at'], 'parsed': parsed}))
        return parsed
    contract.review_intake = review
