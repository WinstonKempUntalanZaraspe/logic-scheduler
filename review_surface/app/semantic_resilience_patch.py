from __future__ import annotations

"""Resilience layer for semantic intake and conservative local fallback.

The model is allowed to interpret wording, never to write. This patch fixes two failure
modes that are especially damaging in real-life Quick Dump input:

1. A healthy but slower Responses API call must not be mislabeled as a network failure
   merely because a complex structured interpretation takes longer than 25 seconds.
2. If semantic interpretation really is unavailable, scheduler-policy prose must remain
   policy prose. Housekeeping/capacity/recovery instructions must never become invented
   TickTick tasks, dependencies, or fake conditional branches.

The deterministic compiler remains the authority for task identity, timing and writes.
"""

import asyncio
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import contingency_patch as contingency
from . import language_intake as intake
from . import semantic_plan as plan

_INSTALLED = False
_BASE_CLASSIFY = None
_BASE_CONSTRAINT_STATUS = None
_BASE_COMPILE = None
_BASE_BRANCH = None


PolicyName = Literal[
    'submission_clock', 'protect_sleep', 'productive_time', 'outing_integrity',
    'protect_fixed', 'preserve_effort', 'owned_cleanup', 'capacity_priority',
    'no_overlap', 'intake_resilience', 'meal_recovery',
]


class ResilientPlanStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str
    kind: Literal[
        'plan', 'activity', 'current_activity', 'location', 'rest', 'meal',
        'sleep', 'instruction', 'output', 'history', 'question'
    ]
    title: str | None
    references: list[str]
    duration_minutes: int | None = Field(ge=1, le=1440, strict=True)
    duration_evidence: str | None
    optional: bool
    location: str | None
    policy: PolicyName | None
    question: str | None


class ResilientPlanMeaning(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_date: str
    steps: list[ResilientPlanStep]
    assumptions: list[str]
    conflicts: list[str]


def _norm(value: str) -> str:
    return re.sub(r'\s+', ' ', str(value or '').replace('’', "'").strip().lower())


def _is_real_condition_branch(text: str) -> bool:
    """Conditions about the outside world remain genuine contingencies."""
    low = _norm(text)
    if not re.match(r'^(?:if|unless|what if)\b', low):
        return False
    condition = re.split(r'[,;]', low, maxsplit=1)[0]
    known_condition = bool(re.search(
        r'\b(?:rain(?:s|ing|y)?|haze|hazy|psi|tired|fatigued|exhausted|sleepy|'
        r'running late|behind schedule|late)\b', condition
    ))
    branch_verb = bool(re.search(r'\b(?:replace|swap|instead of|otherwise|else)\b', low))
    return known_condition and branch_verb


def _is_planner_policy(text: str) -> bool:
    """Recognize general scheduling policy without swallowing ordinary user work."""
    source = str(text or '').strip()
    low = _norm(source)
    if not low:
        return False

    # Explicit task creation/mutation always keeps its dedicated reviewed path.
    if getattr(intake, '_CREATION', None) is not None and intake._CREATION.match(source):
        return False
    try:
        from .personal_intents import task_command
        if task_command(source):
            return False
    except Exception:
        pass
    if _is_real_condition_branch(source):
        return False

    patterns = (
        # Scheduler-owned housekeeping is not a task named "clean up ...".
        r'\b(?:stale|duplicate|duplicated|scheduler[- ]owned|generated)\b.*\b(?:blocks?|schedule|tasks?)\b',
        r'^(?:clean up|tidy|remove)\b.*\b(?:stale|duplicate|scheduler|generated|blocks?)\b',
        # Capacity/priority instructions are policy even when phrased conversationally.
        r'\b(?:least important|lower[- ]priority|flexible work|flexible tasks?)\b.*\b(?:move|defer|reschedule|fit|capacity|cram|compress)\b',
        r'\b(?:move|defer|reschedule|compress|cram)\b.*\b(?:least important|lower[- ]priority|flexible work|flexible tasks?|capacity)\b',
        r'\b(?:compress|cram|overlap(?:ping|s)?)\b.*\b(?:everything|work|tasks?|schedule)\b',
        # Meal/recovery/logistics are geometry around work, not a dependency between prose tasks.
        r'\b(?:finish eating|after eating|meal|lunch|breakfast|dinner)\b.*\b(?:recover|recovery|physical|physically|strenuous|swim|exercise|workout)\b',
        r'\b(?:recover|recovery)\b.*\b(?:eating|meal|physical|physically|strenuous|swim|exercise)\b',
        r'\b(?:realistic travel|travel|changing|showering|preparation|buffers?|fatigue)\b.*\b(?:include|account|realistic|schedule|reschedule|around)\b',
        # Fixed-protection and ambiguity-handling are scheduler instructions.
        r'\b(?:fixed commitments?|fixed events?|fixed tasks?)\b.*\b(?:unchanged|preserve|keep|move)\b',
        r'\b(?:ambiguous|ambiguity)\b.*\b(?:infer|interpret|rewrite|fail|ask)\b',
        r'\b(?:infer|interpret)\b.*\b(?:sensible|real[- ]life|context)\b.*\b(?:rather than|instead of|ambiguous|failing|asking)\b',
    )
    return any(re.search(pattern, low, re.I) for pattern in patterns)


def _policy_rules(text: str) -> list[str]:
    low = _norm(text)
    rules: list[str] = []
    if re.search(r'\b(?:stale|duplicate|scheduler[- ]owned|generated)\b', low):
        rules.append('owned_cleanup')
    if re.search(r'\boverlap(?:ping|s)?\b', low):
        rules.append('no_overlap')
    if re.search(r'\bfixed\b.*\b(?:commitments?|events?|tasks?)\b|\b(?:commitments?|events?|tasks?)\b.*\bfixed\b', low):
        rules.append('protect_fixed')
    if re.search(r'\b(?:least important|priority|flexible|capacity|compress|cram|fit)\b', low):
        rules.append('capacity_and_priority')
    if re.search(r'\b(?:meal|eating|breakfast|lunch|dinner)\b', low):
        rules.append('meal_protection')
    if re.search(r'\b(?:recover|recovery|physical|physically|strenuous|swim|exercise|travel|changing|showering|preparation|buffer|fatigue)\b', low):
        rules.append('outing_integrity')
    if re.search(r'\b(?:ambiguous|ambiguity|infer|interpret|rewrite)\b', low):
        rules.append('intake_resilience')
    return list(dict.fromkeys(rules)) or ['intake_resilience']


def _current_meal(text: str) -> str | None:
    match = re.search(
        r"\b(?:i(?:'m| am)|we(?:'re| are))\s+(?:currently\s+)?(?:eating|having)\s+"
        r"(?:my\s+|our\s+)?(breakfast|lunch|dinner)\b",
        str(text or ''), re.I,
    )
    return match.group(1).lower() if match else None


def _current_home(text: str) -> bool:
    return bool(re.search(
        r"\b(?:i(?:'m| am)\s+(?:back\s+)?home|i\s+(?:just\s+)?(?:got|came|arrived)\s+"
        r"(?:back\s+)?home|back\s+at\s+my\s+place\s+now)\b",
        str(text or ''), re.I,
    ))


def _sanitize_current_reality(meaning: ResilientPlanMeaning, text: str) -> ResilientPlanMeaning:
    """Let deterministic reality handlers own an in-progress meal/arrival-home report."""
    meal = _current_meal(text)
    home = _current_home(text)
    if not meal and not home:
        return meaning
    data = meaning.model_dump()
    for step in data.get('steps', []):
        source = str(step.get('source') or '')
        if (meal and re.search(rf'\b{re.escape(meal)}\b', source, re.I) and _current_meal(source)) or (home and _current_home(source)):
            step['kind'] = 'history'
            step['policy'] = None
            step['question'] = None
    return ResilientPlanMeaning.model_validate(data)


def _compile_with_reality(meaning, text, rows, now):
    assert _BASE_COMPILE is not None
    safe = _sanitize_current_reality(meaning, text)
    normalized, questions, evidence = _BASE_COMPILE(safe, text, rows, now)

    # Re-inject only canonical *current state* that the mature deterministic reality
    # parser already understands. No task, duration or completion is invented here.
    facts: list[str] = []
    if _current_home(text):
        facts.append("I'm at home now")
    meal = _current_meal(text)
    if meal:
        facts.append(f"I'm eating {meal} now")
    if facts:
        marker = f'Plan {safe.target_date}. '
        if normalized.startswith(marker):
            normalized = marker + '. '.join(facts) + '. ' + normalized[len(marker):]
    return normalized, questions, evidence


async def _post_responses(request: dict, key: str, model: str) -> httpx.Response:
    """One long-enough model attempt, with a quick retry only for connection-level faults."""
    timeout = httpx.Timeout(connect=10.0, read=65.0, write=20.0, pool=10.0)
    retryable = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    'https://api.openai.com/v1/responses',
                    headers={'Authorization': 'Bearer ' + key},
                    json=request,
                )
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(0.35)
                continue
            return response
        except retryable as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.35)
                continue
            raise
    if last_error:
        raise last_error
    raise httpx.RequestError('Semantic request did not produce a response')


async def _resilient_interpret_plan(text, rows, config, now):
    from .contextual_intake_patch import _safe_context_packet
    from .service import get_quick_context

    key = __import__('os').environ.get('OPENAI_API_KEY', '').strip()
    model = __import__('os').environ.get('AUTOSCHEDULER_INTAKE_MODEL', 'gpt-6-luna').strip()
    plan.LAST_ATTEMPT.set(None)
    if not key or not model:
        return None

    try:
        packet = _safe_context_packet(rows, config, config.get('_quick_context') or get_quick_context() or {}, now)
        request = {
            'model': model,
            'store': False,
            'instructions': plan._SYSTEM,
            'input': json.dumps({'request': text, 'live_context': packet}),
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': 'plan_meaning',
                    'strict': True,
                    'schema': ResilientPlanMeaning.model_json_schema(),
                }
            },
            # Normal planning requests are far smaller than this. Keeping the bound
            # sane reduces runaway latency while leaving ample room for complex days.
            'max_output_tokens': 6000,
        }
        response = await _post_responses(request, key, model)

        # Compatibility retry is same-model and only for Structured Outputs rejection.
        if response.status_code == 400 and any(label in response.text.lower() for label in (
            'json_schema', 'structured output', 'response_format', 'text.format'
        )):
            compat = dict(request)
            compat.pop('text', None)
            compat['instructions'] = (
                plan._SYSTEM + '\nReturn only JSON matching this schema, without commentary:\n' +
                json.dumps(ResilientPlanMeaning.model_json_schema())
            )
            response = await _post_responses(compat, key, model)

        if response.is_error:
            plan._provider_failure(response, model)
        response.raise_for_status()
        payload = response.json()
        plan.record_provider_state('reachable', model=model, http_status=response.status_code)
        if payload.get('status') != 'completed':
            raise ValueError('Incomplete interpretation')

        from .semantic_compat_patch import _strip_json_fence
        content = _strip_json_fence(''.join(
            chunk['text']
            for item in payload.get('output', [])
            for chunk in item.get('content', [])
            if chunk.get('type') == 'output_text'
        ))
        meaning = ResilientPlanMeaning.model_validate_json(content)
        compiled = plan.compile_meaning(meaning, text, rows, now)
        plan.LAST_ATTEMPT.set({
            'code': 'semantic-active',
            'summary': 'Context-grounded plan interpretation succeeded.',
        })
        return (*compiled, meaning)
    except httpx.HTTPStatusError:
        return None
    except httpx.RequestError:
        plan.record_provider_state('unavailable', model=model)
        plan._failed_attempt(
            'semantic-network-unavailable',
            'Model interpretation could not finish over the network. This review uses local interpretation.',
        )
        return None
    except (ValueError, KeyError, TypeError):
        plan._failed_attempt(
            'semantic-validation-fallback',
            'Model interpretation could not safely resolve this wording. This review uses local interpretation and retains any questions.',
        )
        return None


def install_semantic_resilience() -> None:
    global _INSTALLED, _BASE_CLASSIFY, _BASE_CONSTRAINT_STATUS, _BASE_COMPILE, _BASE_BRANCH
    if _INSTALLED:
        return
    _INSTALLED = True

    # Preserve the fully composed classifier installed so far, then put a narrow
    # scheduler-policy gate in front of it. Later wrappers can safely compose on top.
    _BASE_CLASSIFY = intake.classify
    _BASE_CONSTRAINT_STATUS = intake._constraint_status

    def classify(text, numbered=False, output_section=False, now=None):
        if _is_planner_policy(text):
            return 'constraint', text
        return _BASE_CLASSIFY(text, numbered, output_section, now)

    def constraint_status(text):
        existing = list(_BASE_CONSTRAINT_STATUS(text) or [])
        if _is_planner_policy(text):
            existing.extend(_policy_rules(text))
        return list(dict.fromkeys(existing))

    intake.classify = classify
    intake._constraint_status = constraint_status

    # A sentence such as "if something no longer fits, move flexible work instead of
    # compressing everything" is a capacity policy, not an if/else task branch.
    _BASE_BRANCH = contingency._compile_branch

    def compile_branch(match, rows, now, parsed, *, alt_key, base_key, source):
        if _is_planner_policy(source):
            return None
        return _BASE_BRANCH(match, rows, now, parsed, alt_key=alt_key, base_key=base_key, source=source)

    contingency._compile_branch = compile_branch

    # Extend the small semantic plan language without widening write authority.
    plan.PlanStep = ResilientPlanStep
    plan.PlanMeaning = ResilientPlanMeaning
    plan._POLICIES.update({
        'owned_cleanup': 'Clean up stale or duplicate scheduler blocks',
        'capacity_priority': 'Use capacity and priorities; move flexible work instead of cramming',
        'no_overlap': 'Do not overlap tasks',
        'intake_resilience': 'Interpret ambiguous planning wording from context without inventing tasks',
        'meal_recovery': 'Keep meal recovery before physically demanding activity',
    })
    plan._SYSTEM += """

Additional generic instruction policies are supported: owned_cleanup for stale/duplicate
scheduler blocks; capacity_priority for moving lower-value flexible work instead of
cramming; no_overlap for overlap prevention; intake_resilience for requests to infer
ordinary planning meaning from context without inventing work; and meal_recovery for
recovery after eating before strenuous activity. These are scheduler policies, not
activities. A phrase like 'if something no longer fits, move the least important flexible
work instead of compressing everything' is capacity_priority, NOT a conditional activity
branch. Current-meal reports such as 'I'm eating lunch now' and arrival-home reports are
current reality; classify them as history here because the deterministic reality compiler
preserves them separately. Do not invent a remaining duration for an in-progress meal.
"""

    _BASE_COMPILE = plan.compile_meaning
    plan.compile_meaning = _compile_with_reality
    plan.interpret_plan = _resilient_interpret_plan


__all__ = [
    'install_semantic_resilience', '_is_planner_policy', '_policy_rules',
    '_post_responses', 'ResilientPlanMeaning', 'ResilientPlanStep',
]
