from __future__ import annotations

"""Ground natural-language intake in the user's live schedule without giving the model write authority.

This is deliberately a final patch layer. Semantic inference may resolve references and
paraphrases using a compact read-only view of active tasks, current-day facts and planning
bounds. Deterministic code still validates every source excerpt/reference, extracts hard
return-home bounds, blocks contradictory timing, and performs the actual compilation.
"""

import json
import os
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from . import db
from . import intake_contract as contract
from . import language_intake as intake
from . import semantic_intake as semantic
from . import human_context_patch as human_context


_BASE_REVIEW = contract.review_intake
_BASE_APPLY = contract.apply_reviewed_intake
_BASE_CLASSIFY = intake.classify
_BASE_PLAN = intake.intent_aware_plan


ResolvedKind = Literal[
    'task', 'state', 'directive', 'goal', 'relationship', 'clock',
    'meal-completed', 'progress-state', 'history', 'replan', 'constraint',
    'output', 'ambiguous', 'task-action', 'reality'
]


class GroundedIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: ResolvedKind
    text: str
    payload: str
    references: list[str] = Field(default_factory=list)
    resolved_payload: str | None = None


class GroundedDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intents: list[GroundedIntent]
    assumptions: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


_GROUNDED_SYSTEM = semantic._SYSTEM + """

You also receive read-only live_context containing existing tasks, current-day facts and
planning bounds. Use it only to understand references and real-life context. Never invent
an existing task or claim that a task exists when its id is absent.

For an intention that uses a pronoun, shorthand, or paraphrase for an EXISTING task, put
that task id in references. If the deterministic compiler needs explicit task names, you
may set resolved_payload to a minimal equivalent command using only: words from the source
excerpt, titles of tasks listed in references, clock/date words already present, and simple
connectives such as before/after/then/today/tomorrow. Do not use resolved_payload for a new
task. Do not turn context into task creation.

Examples of context, not task creation: 'I'll be out after church', '8:30 is the absolute
latest I'll be home', 'we're eating outside after that', 'I won't be back until 9'. Keep
hard user bounds distinct from estimates. If two explicit facts cannot both be true, report
a concise conflict instead of choosing one. Assumptions must be visible and must never be
presented as user facts.

Use task-action for explicit delete/cancel/skip/complete/resume commands on existing work.
The deterministic compiler owns target identity and the final action.
'I don't need to do X anymore' means cancel; only 'delete X' means delete.
Use reality for imminent sleep, bath, nap, current location, or arrival home. These are
temporary state, not permanent task creation. Copy the whole source as payload for
task-action and reality. Never reinterpret a negative sentence as completion.
"""


_RETURN_BY_PATTERNS = (
    r"\b(?:be|get|come|arrive)?\s*(?:back\s+)?home\s+(?:by|before|no later than)\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    r"\b(?:be|get|come|arrive)\s+back\s+(?:by|before|no later than)\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    r"\b(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:is|will be|would be)\s+(?:the\s+)?(?:absolute\s+)?(?:latest|max(?:imum)?)\s+(?:time\s+)?(?:i(?:'ll| will)?\s+)?(?:be|get|come|arrive)?\s*(?:back\s+)?home\b",
    r"\b(?:absolute\s+)?(?:latest|max(?:imum)?)\s+(?:i(?:'ll| will)?\s+)?(?:be|get|come|arrive)?\s*(?:back\s+)?home\s+(?:is|would be|will be)?\s*(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
)
_RETURN_NOT_BEFORE_PATTERNS = (
    r"\b(?:won't|will not|cant|can't|cannot)\s+(?:be|get|come|arrive)?\s*(?:back\s+)?home\s+(?:until|before)\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    r"\b(?:won't|will not|cant|can't|cannot)\s+(?:be|get|come|arrive)\s+back\s+(?:until|before)\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
)


def _norm(value) -> str:
    return intake._norm(value).lower().strip(' .')


def _active_rows(rows):
    return [r for r in rows if r.get('id') is not None and int(r.get('status') or 0) == 0
            and str(r.get('kind') or 'TEXT').upper() != 'NOTE'
            and str(r.get('project_kind') or 'TASK').upper() != 'NOTE']


def _safe_context_packet(rows, config, prior_context, now):
    tasks = []
    for row in _active_rows(rows)[:120]:
        meta = row.get('meta') or {}
        tasks.append({
            'id': str(row.get('id')),
            'title': str(row.get('title') or '')[:180],
            'start': row.get('start'),
            'end': row.get('end'),
            'tags': [str(x)[:60] for x in (row.get('tags') or [])[:12]],
            'duration_minutes': meta.get('duration_minutes'),
            'remaining_minutes': meta.get('remaining_minutes'),
            'earliest': meta.get('earliest'),
            'latest_end': meta.get('latest_end'),
            'dependencies': [str(x) for x in (meta.get('dependencies') or [])[:20]],
        })
    ctx = prior_context or {}
    compact_ctx = {
        k: deepcopy(ctx.get(k)) for k in (
            'date', 'wake_time', 'actual_wake_reported', 'completed_meals',
            'temporary_blocks', 'replan_from', 'intent_today_ids',
            'before_main_study_ids', 'intent_date_goals', 'intent_exclusions',
            'return_home_not_after', 'return_home_not_before', 'return_home_estimate',
            'current_location', 'location_reported_at', 'current_activity', 'journey_state', 'meal_locations'
        ) if ctx.get(k) is not None
    }
    planning = {
        k: config.get(k) for k in (
            'wake_time', 'day_start', 'sleep_start', 'day_end',
            'meal_spacing_minutes', 'post_meal_buffer_minutes', 'post_meal_swim_buffer_minutes',
            'post_meal_exercise_buffer_minutes', 'post_meal_leave_rest_minutes',
            'home_label', 'timezone', 'travel_profiles', 'bedtime_wind_down_minutes', 'maximize_productive_time'
        ) if config.get(k) is not None
    }
    return {
        'local_time': now.isoformat(),
        'existing_tasks': tasks,
        'current_day_context': compact_ctx,
        'planning_bounds': planning,
    }


def _allowed_resolved_payload(source: str, resolved: str, references: list[str], by_id: dict[str, dict]) -> bool:
    if not resolved:
        return True
    allowed_words = set(re.findall(r"[a-z0-9:]+", _norm(source)))
    for rid in references:
        row = by_id.get(str(rid))
        if row:
            allowed_words.update(re.findall(r"[a-z0-9:]+", _norm(row.get('title'))))
    allowed_words.update({'before','after','then','today','tomorrow','tonight','morning','afternoon','evening','at','by','until','to','from','and','the','a','an','do','read','study','work','go','swim'})
    resolved_words = set(re.findall(r"[a-z0-9:]+", _norm(resolved)))
    return resolved_words <= allowed_words


async def grounded_semantic_document(text, now, rows, config, prior_context):
    local = intake.extract_intents(text, now)
    key = os.getenv('OPENAI_API_KEY', '').strip()
    model = os.getenv('AUTOSCHEDULER_INTAKE_MODEL', 'gpt-6-luna').strip()
    if not key or not model:
        return local, 'local', None, [], []
    packet = _safe_context_packet(rows, config, prior_context, now)
    by_id = {str(r.get('id')): r for r in _active_rows(rows)}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(
                'https://api.openai.com/v1/responses',
                headers={'Authorization': 'Bearer ' + key},
                json={
                    'model': model,
                    'store': False,
                    'instructions': _GROUNDED_SYSTEM,
                    'input': json.dumps({'request': text, 'live_context': packet}),
                    'text': {'format': {'type': 'json_schema', 'name': 'grounded_schedule_intents',
                                        'strict': True, 'schema': GroundedDocument.model_json_schema()}},
                    'max_output_tokens': 12000,
                },
            )
            response.raise_for_status()
            body = response.json()
        if body.get('status') != 'completed':
            raise ValueError('Incomplete semantic interpretation')
        chunks = [p['text'] for item in body.get('output', []) for p in item.get('content', []) if p.get('type') == 'output_text']
        grounded = GroundedDocument.model_validate_json(''.join(chunks))
        normalized = _norm(text)
        converted = []
        for item in grounded.intents:
            source = _norm(item.text)
            if not source or source not in normalized:
                raise ValueError('Interpretation invented source evidence')
            if any(str(rid) not in by_id for rid in item.references):
                raise ValueError('Interpretation referenced an unknown task')
            payload = item.resolved_payload or item.payload
            if item.resolved_payload:
                if item.kind == 'task' or not item.references:
                    raise ValueError('Unsafe contextual task resolution')
                if not _allowed_resolved_payload(item.text, item.resolved_payload, item.references, by_id):
                    raise ValueError('Resolved payload introduced unsupported content')
            elif _norm(item.payload).strip(' ."') not in source:
                raise ValueError('Interpretation invented payload content')
            safe_kind, _ = contextual_classify(item.text, now=now)
            if safe_kind in {'task-action', 'reality'} and item.kind != safe_kind:
                raise ValueError('Explicit personal action/state was changed by semantic inference')
            if item.kind == 'task' and safe_kind in {'constraint','output','clock','meal-completed','progress-state','history','replan','directive','goal','relationship','state','ambiguous'}:
                raise ValueError('Control or ambiguous prose misclassified as task')
            if item.kind in {'clock','meal-completed','progress-state','replan'} and item.kind != safe_kind:
                raise ValueError('Unsupported fact/control compilation')
            if item.kind in {'state','directive','relationship','goal'} and safe_kind in {'constraint','output','history','clock','meal-completed','progress-state','replan'} and not item.resolved_payload:
                raise ValueError('Control prose misclassified as mutable intent')
            converted.append(intake.Intent(kind=item.kind, text=item.text, payload=payload))
        joined = ' '.join(_norm(x.text) for x in grounded.intents)
        for item in local.intents:
            if _norm(item.text) not in joined:
                raise ValueError('Semantic interpretation omitted source clauses')
        return intake.IntentDocument(intents=converted), 'semantic-grounded', None, grounded.assumptions, grounded.conflicts
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ImportError):
        return local, 'local-fallback', 'Semantic interpretation unavailable or failed validation; using conservative local interpretation.', [], []


def contextual_classify(text, numbered=False, output_section=False, now=None):
    low = _norm(text)
    # Real-life whereabouts/return statements are context, not new permanent work.
    if re.search(r"\b(?:i(?:'ll| will| am|'m)|we(?:'ll| will| are|'re))\b.*\b(?:out|outside|away|home|back)\b", low):
        if any(re.search(p, low, re.I) for p in _RETURN_BY_PATTERNS + _RETURN_NOT_BEFORE_PATTERNS):
            return 'constraint', text
        return 'state', text
    if re.search(r"\b(?:absolute\s+)?(?:latest|max(?:imum)?)\b.*\b(?:home|back)\b", low):
        return 'constraint', text
    return _BASE_CLASSIFY(text, numbered, output_section, now)


def _clock_from_match(raw, now):
    parsed = human_context._parse_clock(raw, now)
    if parsed and parsed.date() != now.date() and parsed - now > timedelta(hours=18):
        return None
    return parsed


def _return_bounds(text, now):
    low = str(text or '').replace('’', "'")
    upper = None
    lower = None
    for pattern in _RETURN_BY_PATTERNS:
        m = re.search(pattern, low, re.I)
        if m:
            upper = _clock_from_match(m.group('t'), now)
            if upper:
                break
    for pattern in _RETURN_NOT_BEFORE_PATTERNS:
        m = re.search(pattern, low, re.I)
        if m:
            lower = _clock_from_match(m.group('t'), now)
            if lower:
                break
    return lower, upper


def _attach_real_life_bounds(parsed, text, rows, now):
    lower, upper = _return_bounds(text, now)
    if not lower and not upper:
        return parsed
    ctx = parsed.get('context') or {'date': now.date().isoformat(), 'source': 'quick-dump'}
    if lower:
        ctx['return_home_not_before'] = lower.isoformat()
        parsed.setdefault('notes', []).append('Hard real-life bound understood: not home before ' + lower.strftime('%H:%M') + '.')
    if upper:
        ctx['return_home_not_after'] = upper.isoformat()
        parsed.setdefault('notes', []).append('Hard real-life bound understood: home no later than ' + upper.strftime('%H:%M') + '; outing placement must finish by this bound.')
    parsed['context'] = ctx
    conflicts = list(parsed.get('blocking_conflicts') or [])
    if lower and upper and lower > upper:
        conflicts.append('Return-home timing conflicts: the earliest possible return is later than the stated latest return.')
    if upper and upper <= now:
        conflicts.append('Return-home latest time is already in the past relative to the current planning time.')
    parsed['blocking_conflicts'] = list(dict.fromkeys(conflicts))
    for conflict in parsed['blocking_conflicts']:
        item = {'text': text, 'reason': conflict + ' Resolve the timing before applying.'}
        if item not in parsed.setdefault('clarifications', []):
            parsed['clarifications'].append(item)
        warning = item['reason']
        if warning not in parsed.setdefault('warnings', []):
            parsed['warnings'].append(warning)
    return parsed


async def contextual_review_intake(text, rows, config, now=None):
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    try:
        from .service import get_quick_context
        prior_context = get_quick_context() or {}
    except Exception:
        prior_context = {}
    from .semantic_plan import LOCAL_REVIEW
    if LOCAL_REVIEW.get():
        document, mode, warning, assumptions, semantic_conflicts = intake.extract_intents(text, now), 'local', None, [], []
    else:
        document, mode, warning, assumptions, semantic_conflicts = await grounded_semantic_document(
            text, now, rows, config, prior_context
        )
    parsed = intake.parse_language(text, rows, config, now, document)
    intake.validate_task_creation(parsed)
    parsed['interpreter_mode'] = mode
    parsed['semantic_context_used'] = mode == 'semantic-grounded'
    if warning:
        parsed['warnings'].append(warning)
    for assumption in assumptions:
        note = 'Assumption (not a user fact): ' + assumption
        if note not in parsed['notes']:
            parsed['notes'].append(note)
    if semantic_conflicts:
        parsed['blocking_conflicts'] = list(dict.fromkeys([*parsed.get('blocking_conflicts', []), *semantic_conflicts]))
        for conflict in semantic_conflicts:
            parsed['clarifications'].append({'text': text, 'reason': 'Conflict: ' + conflict + ' Resolve it before applying.'})
            parsed['warnings'].append('Conflict: ' + conflict + ' Resolve it before applying.')
    parsed = _attach_real_life_bounds(parsed, text, rows, now)
    token = secrets.token_urlsafe(24)
    parsed['preview_id'] = token
    parsed['reviewed_at'] = now.isoformat()
    expires = now + timedelta(minutes=contract.TTL_MINUTES)
    parsed['expires_at'] = expires.isoformat()
    contract.set_kv('intake_review', json.dumps({
        'text_hash': contract._fingerprint(text),
        'snapshot': contract._snapshot(rows, config),
        'expires_at': expires.isoformat(),
        'parsed': parsed,
    }))
    return parsed


def contextual_apply_reviewed_intake(text, rows, config, preview_id, now=None):
    parsed = _BASE_APPLY(text, rows, config, preview_id, now)
    if parsed.get('blocking_conflicts'):
        raise HTTPException(409, 'The reviewed request contains conflicting real-life timing. Interpret again after resolving the conflict.')
    return parsed


def contextual_intent_aware_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
    cfg = deepcopy(config or {})
    metas = deepcopy(meta_map or {})
    ctx = cfg.get('_quick_context') or {}
    raw_upper = ctx.get('return_home_not_after') if ctx.get('date') == start.date().isoformat() else None
    if raw_upper:
        try:
            upper = datetime.fromisoformat(raw_upper).astimezone(settings.tz)
        except (TypeError, ValueError):
            upper = None
        if upper:
            from . import reality_patch as reality
            active_today = set(ctx.get('intent_today_ids') or [])
            primaries = [t for t in tasks if t.status == 0 and reality._is_primary_outing(t, metas.get(t.id, {}))]
            selected = [t for t in primaries if not active_today or t.id in active_today]
            for task in selected:
                tags = {str(x).lower() for x in task.tags}
                if 'fixed' in tags:
                    continue
                raw = metas.setdefault(task.id, {})
                try:
                    old = datetime.fromisoformat(str(raw.get('latest_end')).replace('Z', '+00:00')).astimezone(settings.tz) if raw.get('latest_end') else None
                except ValueError:
                    old = None
                raw['latest_end'] = min(upper, old or upper).isoformat()
                raw['must_finish'] = True
    return _BASE_PLAN(tasks, metas, busy, start, horizon_days, cfg, mastery_map)


def install_contextual_intake_patch():
    intake.classify = contextual_classify
    contract.review_intake = contextual_review_intake
    contract.apply_reviewed_intake = contextual_apply_reviewed_intake
    return contextual_intent_aware_plan
