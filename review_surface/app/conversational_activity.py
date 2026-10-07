"""Source-grounded conversational activities and waking-day availability.

An explicit personal intention can name an unfamiliar activity. It must not depend
on a verb dictionary, and its words must not be silently corrected into a location.
Day-long work is flexible effort within the waking day; unavailability is a
temporary reservation, never an invented TickTick task.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import settings

_DAY_SPAN = re.compile(
    r"\b(?:(?:for\s+)?(?:the\s+)?(?:whole|entire|full)\s+day|(?:for\s+)?all[ -]day|"
    r"(?:for\s+)?the\s+rest\s+of\s+(?:(?:the|my)\s+day|today))\b", re.I)
_UNAVAILABLE = re.compile(
    r"^(?:(?:today|tomorrow|tmr)\s*[, :]\s*)?"
    r"(?:i(?:'m|'ll| am| will)?|we(?:'re|'ll| are| will)?)\s+"
    r"(?:(?:going\s+to|gonna)\s+)?(?:be\s+)?"
    r"(?:unavailable|busy|not\s+(?:free|available)|(?:go|head)\s+out|out(?:side)?|away|gone)\b", re.I)
_NON_ACTIVITY = re.compile(
    r"^(?:be\b|feel\b|have\s+(?:to|been)\b|not\b|never\b|"
    r"(?:think|believe|expect|hope|guess|know|remember|say|mean)\s+(?:that|it|this)\b|"
    r"(?:might|may|could|would|should|can't|cannot|won't|don't)\b|"
    r"make\s+sure\b|(?:change|ignore|override|relax)\s+(?:the\s+)?(?:rules|schema|constraints)\b)", re.I)


def has_day_span(text):
    return bool(_DAY_SPAN.search(str(text or '')))


def day_unavailability(text):
    value = str(text or '').replace('’', "'")
    value = re.sub(r'\bim\b', "i'm", value, flags=re.I).strip(' .')
    return has_day_span(value) and not value.endswith('?') and bool(_UNAVAILABLE.match(value))


def is_explicit_activity(candidate):
    """A positive spoken intention supplies authority, but must still be one activity.

    Do not reject merely because the title is long.  Reject structurally multi-action
    narratives unless the caller has explicit single-task naming authority.
    """
    candidate = str(candidate or '').strip()
    if not (
        candidate
        and re.match(r'^[A-Za-z]', candidate)
        and not candidate.endswith('?')
        and not _NON_ACTIVITY.match(candidate)
        and not re.search(r'\b(?:if|unless|whether|because|for example)\b', candidate, re.I)
    ):
        return False
    from .task_title_guard import is_atomic_activity_phrase
    return is_atomic_activity_phrase(candidate)


def day_bounds(text, config, now):
    from . import scheduler, human_day_patch
    from .schedule_instruction_patch import _day_from_clause
    day, explicit = _day_from_clause(text, now)
    day = day or now.date()
    # "Rest of the day" can refer to the end of a waking day after midnight.
    if not explicit and re.search(r'\brest\s+of\b', text, re.I):
        previous = day - timedelta(days=1)
        a, b = human_day_patch._logical_awake_bounds(previous, config)
        if a <= now < b:
            day = previous
    merged = dict(config) | scheduler._override_for_day(day, config)
    start, end = human_day_patch._logical_awake_bounds(day, merged)
    return day, max(start, now.replace(second=0, microsecond=0)), end


def compile_day_unavailability(text, result, compiled, config, now):
    from .personal_intents import context, clarify
    day, start, end = day_bounds(text, config, now)
    if end <= start:
        clarify(result, compiled, text, 'That waking day has ended. Specify tomorrow or an end time.')
        return
    ctx = context(result, now)
    away = bool(re.search(r'\b(?:out|outside|away|gone)\b', text, re.I))
    ctx.setdefault('temporary_blocks', []).append({
        'label': 'Away' if away else 'Unavailable', 'start': start.isoformat(),
        'end': end.isoformat(), 'source': 'personal-away' if away else 'personal-availability',
        'kind': 'location' if away else 'availability', 'planning_only': True,
        'certainty': 'high', 'day_span': True,
    })
    ctx.update(replan_requested=True, replan_from=now.isoformat(), fresh_plan_revision=True,
               minimum_horizon_days=max(1, (day - now.date()).days + 1))
    result['notes'].append(f'Unavailable {start:%a %H:%M}–{end:%a %H:%M}: “all day” uses your waking day, ending at your saved bedtime. Fixed commitments stay protected; no permanent task is created.')
    compiled['status'] = 'compiled'


def compile_day_activity(text, payload, result, compiled, rows, config, now):
    """Return False when ordinary task/goal compilation should handle the clause."""
    if not has_day_span(text):
        return False
    from . import language_intake as intake, quickdump as qd
    from .personal_intents import clarify, context
    from .lifelong_intake_patch import _SPOKEN_ACTION_PREFIX
    candidate = str(payload or text).strip()
    authority = _SPOKEN_ACTION_PREFIX.match(str(text)) or intake._CREATION.match(str(text))
    if not authority and not intake._ACTION.match(candidate):
        clarify(result, compiled, text, 'Name the activity you want to schedule, or say you are unavailable all day.')
        return True
    prefix = _SPOKEN_ACTION_PREFIX.match(candidate) or intake._CREATION.match(candidate)
    if prefix:
        candidate = candidate[prefix.end():].strip()
    title = _DAY_SPAN.sub('', candidate)
    if re.match(r'^spend\b', candidate, re.I):
        title = re.sub(r'^spend\s+', '', title, flags=re.I)
    title = re.sub(r'\b(?:today|tomorrow|tmr)\b', '', title, flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip(' ,.;:-')
    if not title or not is_explicit_activity(title):
        clarify(result, compiled, text, 'Name the activity you want to spend the day on, or say you are unavailable all day.')
        return True
    day, start, end = day_bounds(text, config, now)
    if end <= start:
        clarify(result, compiled, text, 'That waking day has ended. Specify tomorrow or a shorter activity.')
        return True
    parsed = intake._task_inference(title, rows, config, now)
    if not parsed.get('tasks'):
        clarify(result, compiled, text, 'Name one activity clearly; multiple existing tasks may have the same name.')
        return True
    for change in parsed['tasks']:
        patch = change.setdefault('meta_patch', {})
        original = next((r for r in rows if str(r.get('id')) == str(change.get('task_id'))), {})
        original_meta = original.get('meta') or {}
        for key, boundary in (('earliest', start), ('latest_end', end)):
            raw = original_meta.get(key)
            if raw:
                try:
                    old = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                    old = old.replace(tzinfo=settings.tz) if old.tzinfo is None else old
                    boundary = max(boundary, old) if key == 'earliest' else min(boundary, old)
                except (ValueError, TypeError):
                    clarify(result, compiled, text, 'The existing task has an invalid time limit. Correct it before planning the day.')
                    return True
            patch[key] = boundary.isoformat()
        if datetime.fromisoformat(patch['latest_end']) <= datetime.fromisoformat(patch['earliest']):
            clarify(result, compiled, text, 'The requested day conflicts with the existing task’s time limits.')
            return True
        # Existing estimated work remains authoritative. New day-long activities
        # receive a disclosed capacity target, rather than a fabricated 30m duration.
        if change.get('action') == 'create':
            minutes = qd._extract_duration(text) or int((end - start).total_seconds() // 60)
            patch.update(duration_minutes=minutes, splittable=True,
                         min_chunk=min(25, minutes), max_chunk=min(90, minutes),
                         must_finish=False, confidence='medium' if qd._extract_duration(text) else 'low')
            change['intake_kind'] = 'task'
        change['line'] = text
    intake._merge(result, parsed, allow_create=True)
    ctx = context(result, now)
    ctx.update(replan_requested=True, replan_from=now.isoformat(),
               minimum_horizon_days=max(1, (day - now.date()).days + 1))
    for change in parsed['tasks']:
        if change.get('task_id'):
            ctx.setdefault('intent_date_goals', {})[str(change['task_id'])] = day.isoformat()
    result['notes'].append(f'“All day” is a flexible activity within {start:%a %H:%M}–{end:%a %H:%M}. Existing effort estimates stay saved; a new activity uses this window as its capacity target. Meals, travel, recovery, fixed commitments and sleep remain protected; the preview reports any effort that cannot fit.')
    compiled['status'] = 'compiled'
    return True
