"""Source-grounded task actions and temporary personal state.

All compilation is read-only. Writers consume the reviewed actions exactly once.
Cancellation retains the task; only an explicit delete removes it.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, time

from fastapi import HTTPException

from .config import settings
from . import quickdump as qd

TASK_ACTIONS = {'create', 'update', 'cancel', 'skip', 'delete', 'complete', 'resume'}
_GUARD = re.compile(r"^(?:if\b|suppose\b|for example\b|example\b|what if\b|how\b|should\b|could\b|can\b|do not\b|don't (?:delete|cancel|complete|remove)\b|never\b)", re.I)
_CLOCK = r'\d{1,2}(?::\d{2})?\s*(?:am|pm)?'


def norm(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('’', "'").replace('“', '"').replace('”', '"')).strip()


def key(value):
    return re.sub(r'[^a-z0-9]+', ' ', norm(value).lower()).strip()


def split_personal_sequence(text):
    if not reality_kind(text):
        return [text]
    parts = re.split(r',?\s+then\s+(?=(?:nap|rest|bathe|shower|sleep|take (?:a |a nice )?(?:nap|sleep|bath|shower)|eat (?:breakfast|lunch|dinner))\b)', text, flags=re.I)
    return [part if index == 0 else 'then ' + part for index, part in enumerate(parts)]


def task_command(text):
    """Recognize explicit authority, never a title match or a hypothetical."""
    value = norm(text).strip(' .')
    value = re.sub(r'^(?:and\s+)?(?:then\s+)?(?:please\s+)?', '', value, flags=re.I)
    if _GUARD.match(value) or value.endswith('?'):
        return None
    patterns = [
        ('delete', r'^(?:delete|permanently delete|remove permanently)\s+(?:the\s+)?(?:task\s+)?(.+)$'),
        ('cancel', r"^(?:i\s+)?(?:don't|do not|dont)\s+(?:need|have|want)\s+to\s+(?:do\s+)?(.+?)(?:\s+anymore|\s+any more|\s+any longer)?$"),
        ('cancel', r'^(?:i\s+)?(?:no longer|never again)\s+(?:need|have|want)\s+to\s+(?:do\s+)?(.+)$'),
        ('cancel', r'^(?:cancel|cancel the task|drop the task|remove from (?:my |the )?schedule)\s+(.+)$'),
        ('skip', r'^(?:skip|skip the task)\s+(.+)$'),
        ('complete', r'^(?:mark\s+(.+?)\s+(?:as\s+)?(?:done|complete|completed))$'),
        ('complete', r'^(?:complete(?: the task)?|(?:i\s+)?(?:(?:have|just|already)\s+)*(?:finished|completed|done with))\s+(.+)$'),
        ('complete', r'^(.+?)\s+(?:is|has been)\s+(?:already\s+)?(?:done|complete|completed|finished)$'),
        ('resume', r'^(?:resume|uncancel|start scheduling|restore scheduling for)\s+(.+)$'),
    ]
    for action, pattern in patterns:
        match = re.match(pattern, value, re.I)
        if not match:
            continue
        target = match.group(1).strip(' ."')
        if action == 'complete' and re.fullmatch(r'(?:eating\s+)?(?:my\s+)?(?:breakfast|lunch|dinner)', target, re.I):
            return None
        scope = 'persistent'
        if action == 'skip' or re.search(r'\b(?:today|tonight|this occurrence|this time|tomorrow)\b', target, re.I):
            scope = 'occurrence'
        if re.search(r'\b(?:anymore|any longer|permanently|all occurrences|entire series)\b', value, re.I):
            scope = 'persistent'
        target = re.sub(r'\s+(?:for\s+)?(?:today(?: only)?|tonight|tomorrow|this occurrence|this time|anymore|any more|any longer|permanently|all occurrences|entire series)$', '', target, flags=re.I)
        target = re.sub(r'^the task\s+|^task\s+|^my\s+', '', target, flags=re.I)
        target = re.sub(r'\s+at\s+' + _CLOCK + r'$', '', target, flags=re.I)
        if target:
            return {'action': action, 'target': target.strip(' ."'), 'scope': scope, 'text': text}
    return None


def resolve_target(target, rows):
    active = [r for r in rows if r.get('id') and int(r.get('status') or 0) == 0
              and str(r.get('kind') or 'TEXT').upper() != 'NOTE'
              and str(r.get('project_kind') or 'TASK').upper() != 'NOTE'
              and 'autoscheduler-session' not in {str(t).lower() for t in r.get('tags') or []}]
    identity = key(target)
    exact = [r for r in active if key(r.get('id')) == identity or key(r.get('title')) == identity]
    if exact:
        return exact[0] if len(exact) == 1 else None
    # Completion can name the work in past tense, but destructive actions never use
    # similarity scores. A unique contained full phrase is still reviewable.
    identity = re.sub(r'^reading\b', 'read', identity)
    identity = re.sub(r'^doing\b', 'do', identity)
    exact = [r for r in active if key(r.get('title')) == identity]
    if len(exact) == 1:
        return exact[0]
    matches = [r for r in active if len(identity) >= 4 and re.search(r'\b' + re.escape(identity) + r'\b', key(r.get('title')))]
    return matches[0] if len(matches) == 1 else None


def context(result, now):
    ctx = result.get('context') or {'date': now.date().isoformat(), 'source': 'quick-dump'}
    ctx.update(replan_requested=True, replan_from=now.isoformat(), preserve_unfinished=True)
    result['context'] = ctx
    return ctx


def clarify(result, compiled, text, reason):
    compiled['status'] = 'needs-input'
    result.setdefault('clarifications', []).append({'text': text, 'reason': reason})
    result.setdefault('blocking_conflicts', []).append(reason)


def compile_task_action(text, result, compiled, rows, config, now):
    command = task_command(text)
    row = resolve_target(command['target'], rows) if command else None
    if not row:
        clarify(result, compiled, text, 'Name one existing task or its ID for this action; no task will be changed.')
        return
    action, scope = command['action'], command['scope']
    recurring = bool(row.get('repeat') or row.get('repeat_flag'))
    if action in {'cancel', 'skip'} and recurring and not re.search(r'\b(?:anymore|permanently|all occurrences|entire series|no longer)\b', norm(text), re.I):
        scope = 'occurrence'
    if action == 'delete' and recurring and not re.search(r'\b(?:permanently|all occurrences|entire series)\b', norm(text), re.I):
        clarify(result, compiled, text, 'This is a recurring task. Use “delete ' + str(row['title']) + ' entire series” to remove it, or “skip ' + str(row['title']) + ' today” for one occurrence.')
        return
    if action in {'delete', 'complete'} and scope == 'occurrence' and action == 'delete':
        clarify(result, compiled, text, 'Delete removes the whole task. Use “skip ' + str(row['title']) + ' today” to keep it for another day.')
        return
    day = qd._extract_day_hint(text, now)
    reasons = {
        'cancel': 'Keep this task and stop scheduling it' if scope == 'persistent' else 'Skip this occurrence; future occurrences stay intact',
        'skip': 'Exclude this task on ' + day.isoformat() + '; keep the task',
        'delete': 'Delete this task from TickTick' + (' and its recurring series' if recurring else ''),
        'complete': 'Mark the task complete in TickTick' + (' for this occurrence' if recurring else ''),
        'resume': 'Restore scheduling for this task',
    }
    change = {'action': action, 'task_id': str(row['id']), 'project_id': str(row.get('project_id') or ''),
              'title': row['title'], 'line': text, 'intake_kind': 'task-action', 'scope': scope,
              'date': day.isoformat(), 'recurring': recurring, 'priority': int(row.get('priority') or 0),
              'tags_add': [], 'meta_patch': {}, 'reason': reasons[action]}
    previous = next((c for c in result['tasks'] if c.get('task_id') == change['task_id']), None)
    if previous and previous.get('action') != action:
        clarify(result, compiled, text, 'Conflicting actions for ' + str(row['title']) + '; state one final action.')
        return
    if not previous:
        result['tasks'].append(change)
    ctx = context(result, now)
    if action in {'cancel', 'skip'} and scope == 'occurrence':
        ctx.setdefault('intent_exclusions', []).append({'task_ids': [change['task_id']], 'date': day.isoformat()})
        ctx.setdefault('skipped_occurrences', []).append({'task_id': change['task_id'], 'title': row['title'], 'date': day.isoformat()})
        if day == now.date():
            ctx.setdefault('suppressed_fixed_task_ids_today', []).append(change['task_id'])
            ctx.setdefault('suppressed_fixed_titles_today', []).append(row['title'])
            ctx['fresh_plan_revision'] = True
        ctx['minimum_horizon_days'] = max(int(ctx.get('minimum_horizon_days') or 1), min(14, (day-now.date()).days + 1))
    dependents = [str(r['title']) for r in rows if change['task_id'] in {str(x) for x in (r.get('meta') or {}).get('dependencies') or []}]
    if dependents and action in {'delete', 'cancel', 'skip'}:
        result.setdefault('notes', []).append('Dependent work stays held until its prerequisite is completed or changed: ' + ', '.join(dependents) + '.')
    compiled.update(status='compiled', bindings=[{'task_id': change['task_id'], 'title': row['title'], 'role': action}])


def reality_kind(text):
    from .conversational_activity import day_unavailability
    if day_unavailability(text):
        return 'day-unavailable'
    value = norm(text).lower().strip(' .')
    value = re.sub(r'\bim\b', "i'm", value)
    value = re.sub(r"\bi'm gonna\b", "i'm going to", value)
    if _GUARD.match(value) or value.endswith('?') or re.match(r'^(?:add|create|new task|task:|remind me)\b', value):
        return None
    # A dated chore is ordinary work; immediate narrated care is temporary reality.
    if re.search(r'\b(?:tomorrow|tmr|next week|next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b', value) and not re.search(r'\b(?:now|about to|after)\b', value):
        return None
    if re.search(r"\b(?:about to (?:sleep|go to bed)|(?:going to|gonna) (?:sleep|bed)(?: now)?|going to bed now|sleeping now|off to bed|heading to bed)\b", value):
        return 'sleep'
    prefix = r"^(?:(?:and\s+)?then\s+|now\s+)?(?:(?:i(?:'m| am)?\s+)?(?:going to\s+|want to\s+|need to\s+|will\s+|gonna\s+)?)?"
    if re.match(prefix + r'(?:bathe|shower|bath|take (?:a |my )?(?:bath|shower))\b', value):
        return 'bath'
    if re.match(prefix + r'(?:nap|rest|take (?:a |a nice )?(?:nap|sleep|rest))\b', value):
        return 'nap'
    if re.match(r'^(?:then\s+|now\s+|i(?:\x27m| am)? (?:going to|about to)\s+)(?:eat|have) (?:my )?(?:breakfast|lunch|dinner)\b', value):
        return 'meal'
    if re.search(r"^(?:(?:i|we)(?:'m|'re| am| are| have| just)?\s+)?(?:back home|home (?:now|early|already)|at home(?: now| early)?|back at home|(?:just )?(?:got|came|arrived|reached|returned)\s+(?:back\s+)?(?:at\s+)?home)\b", value):
        return 'home'
    if re.search(r'\b(?:take\s*away|take\s*out|takeout|dabao|pack(?:ing)?\s+(?:the\s+)?food)\b', value) and re.search(r'\b(?:eat(?:ing)?|have|having|meal|breakfast|lunch|dinner|home)\b', value) and not _GUARD.match(value):
        return 'takeaway'
    from .journey_context import is_journey_update
    if is_journey_update(value):
        return 'journey'
    if re.search(r"^(?:i(?:'m| am)?\s+)?(?:awake now|up now|just woke up)\b", value):
        return 'awake'
    if re.search(r"^(?:i(?:'m| am)?\s+)?(?:still |currently )?at\s+.+\buntil\s+" + _CLOCK, value):
        return 'location'
    if re.search(r"\b(?:haven't|have not|didn't|did not)\s+(?:eaten|had|eat|have)\s+(?:my\s+)?(?:breakfast|lunch|dinner)\b", value):
        return 'missed-meal'
    return None


def _wake_after(start, config):
    try:
        wake = time.fromisoformat(str(config.get('wake_time') or '07:00'))
    except ValueError:
        wake = time(7)
    end = datetime.combine(start.date(), wake, settings.tz)
    return end if end > start else end + timedelta(days=1)


def compile_reality(text, result, compiled, rows, config, now):
    kind = reality_kind(text)
    if not kind:
        clarify(result, compiled, text, 'This temporary state needs a clear activity or end time.')
        return
    if kind == 'day-unavailable':
        from .conversational_activity import compile_day_unavailability
        compile_day_unavailability(text, result, compiled, config, now)
        return
    ctx = context(result, now)
    if kind == 'journey':
        from .journey_context import compile_journey
        from .service import get_quick_context
        compile_journey(text, result, now, get_quick_context(), rows, config)
        compiled['status'] = 'compiled'
        return
    if kind == 'awake':
        ctx.update(activity_state='awake', sleep_until=None, fresh_plan_revision=True)
        ctx['temporary_blocks'] = [b for b in ctx.get('temporary_blocks') or [] if b.get('kind') != 'sleep']
        result['notes'].append('Awake now: release the temporary sleep reservation and rebuild unfinished work from now.')
        compiled['status'] = 'compiled'
        return
    if kind == 'home':
        ctx.update(current_location='home', home_base_active=True, location_reported_at=now.isoformat(), fresh_plan_revision=True)
        if re.search(r'\b(?:take\s*away|take\s*out|takeout|dabao)\b', text, re.I):
            named = re.search(r'\b(breakfast|lunch|dinner)\b', text, re.I)
            meal = named[1].lower() if named else ('lunch' if now.hour < 16 else 'dinner')
            ctx.setdefault('meal_locations', {})[meal] = 'home'
            ctx[meal + '_location'] = 'home'
        ctx['temporary_blocks'] = [b for b in ctx.get('temporary_blocks') or [] if b.get('source') not in {'human-reality-context', 'personal-away', 'personal-journey', 'personal-current-activity'}]
        ctx['current_activity'] = None
        for field in ('return_home_not_before', 'return_home_not_after', 'return_home_estimate', 'away_until', 'journey_state'):
            ctx[field] = None
        result['notes'].append('Home now: release the previous away/return-home assumptions and replan from now.')
        compiled['status'] = 'compiled'
        return
    if kind == 'takeaway':
        named = re.search(r'\b(breakfast|lunch|dinner)\b', text, re.I)
        meal = named.group(1).lower() if named else ('breakfast' if now.hour < 11 else 'lunch' if now.hour < 16 else 'dinner')
        ctx.setdefault('meal_locations', {})[meal] = 'home'
        ctx[meal + '_location'] = 'home'
        ctx.update(home_base_active=True, fresh_plan_revision=True)
        if re.search(r"\b(?:eating|having|eat(?:ing)? .{0,25}now)\b", text, re.I) and not re.search(r"\b(?:will|going to|later|after)\b", text, re.I):
            minutes = qd._extract_duration(text) or (30 if meal == 'breakfast' else 45)
            ctx.setdefault('completed_meals', {}).pop(meal, None)
            ctx.setdefault('temporary_blocks', []).append({'label': meal.title(), 'meal': meal,
                'start': now.isoformat(), 'end': (now + timedelta(minutes=minutes)).isoformat(),
                'nominal_end': (now + timedelta(minutes=minutes)).isoformat(),
                'source': 'human-meal-context', 'kind': 'meal', 'location': 'home', 'planning_estimate': True})
        # Buying food is not an extra restaurant stay. Release old dining-away
        # estimates, while keeping genuine appointment and travel facts intact.
        ctx['temporary_blocks'] = [b for b in ctx.get('temporary_blocks') or [] if not re.search(r'\b(?:restaurant|dinner out|lunch out|eat(?:ing)? out|family dinner)\b', str(b.get('label') or b.get('name') or ''), re.I)]
        for field in ('return_home_not_before', 'return_home_estimate', 'away_until', 'journey_state'):
            ctx[field] = None
        result['notes'].append(f'Takeaway {meal} is at home: reserve the meal after returning, without an additional restaurant stay. Report an early arrival to release unused travel estimates.')
        compiled['status'] = 'compiled'
        return
    start = now.replace(second=0, microsecond=0)
    # A sequential narrated recovery uses the previous temporary block's end.
    if re.match(r'^(?:and\s+)?then\b', norm(text), re.I):
        start = max(start, datetime.fromisoformat(ctx.get('personal_sequence_end') or start.isoformat()))
    clock = re.search(r'\b(?:at|from)\s+(' + _CLOCK + r')\b', text, re.I)
    if clock:
        minute = qd._clock_to_minutes(clock.group(1))
        if minute is not None:
            day = qd._extract_day_hint(text, now)
            start = datetime.combine(day, time(), settings.tz) + timedelta(minutes=minute)
            if start < now:
                clarify(result, compiled, text, 'This start time is in the past. Report it as happening now, or give a future time.')
                return
    minutes = qd._extract_duration(text)
    estimated = minutes is None
    labels = {'sleep': 'Sleep', 'nap': 'Nap / rest', 'bath': 'Bathe / shower', 'location': 'Away', 'missed-meal': 'Meal', 'meal': 'Meal'}
    if kind == 'sleep':
        end = start + timedelta(minutes=minutes) if minutes is not None else _wake_after(start, config)
        until = re.search(r'\buntil\s+(' + _CLOCK + r')', text, re.I)
        if until:
            minute = qd._clock_to_minutes(until.group(1))
            end = datetime.combine(start.date(), time(), settings.tz) + timedelta(minutes=minute or 0)
            if end <= start:
                end += timedelta(days=1)
            estimated = False
        ctx.update(activity_state='sleeping', minimum_horizon_days=max(2, int(ctx.get('minimum_horizon_days') or 1)),
                   sleep_until=end.isoformat(), fresh_plan_revision=True, catch_up_missed=False)
        result['notes'].append('Sleep is temporary reality from ' + start.strftime('%H:%M') + ' to ' + end.strftime('%a %H:%M') + '. Unfinished work moves after waking; your normal routine stays unchanged.' + (' Wake time uses your saved setting.' if estimated else ''))
    elif kind == 'location':
        match = re.search(r'\bat\s+(.+?)\s+until\s+(' + _CLOCK + r')', norm(text), re.I)
        minute = qd._clock_to_minutes(match.group(2))
        end = datetime.combine(start.date(), time(), settings.tz) + timedelta(minutes=minute or 0)
        if end <= start:
            clarify(result, compiled, text, 'The reported away-until time has elapsed; tell me whether you are home now or give a new end time.')
            return
        location = re.sub(r'^the\s+', '', match.group(1).strip(), flags=re.I)
        ctx.update(current_location=location, location_reported_at=now.isoformat(), away_until=end.isoformat())
        labels[kind] = 'At ' + location
        estimated = False
    elif kind in {'missed-meal', 'meal'}:
        meal = re.search(r'\b(breakfast|lunch|dinner)\b', text, re.I).group(1).lower()
        labels[kind] = meal.title()
        end = start + timedelta(minutes=minutes or (30 if meal == 'breakfast' else 45))
        ctx.setdefault('completed_meals', {}).pop(meal, None)
    else:
        end = start + timedelta(minutes=minutes if minutes is not None else (30 if kind == 'nap' else 20))
    if end <= start or end-start > timedelta(hours=24):
        clarify(result, compiled, text, 'Use a positive temporary duration of at most 24 hours.')
        return
    # "Shower after Swimming" is an anchored follow-up, not a block starting now.
    after = re.search(r'\bafter\s+(.+)$', text, re.I)
    if after and kind in {'bath', 'nap'}:
        target = re.sub(r'\s+for\s+\d+(?:\.\d+)?\s*(?:m|min(?:ute)?s?|h|hours?)\b.*$', '', after.group(1), flags=re.I)
        row = resolve_target(target, rows)
        if not row:
            clarify(result, compiled, text, 'Name one existing activity to anchor this follow-up.')
            return
        ctx.setdefault('personal_followups', []).append({'after_id': str(row['id']), 'label': labels[kind], 'minutes': int((end-start).total_seconds()//60), 'kind': kind, 'planning_estimate': estimated})
    else:
        block = {'label': labels[kind], 'start': start.isoformat(), 'end': end.isoformat(), 'source': 'personal-state',
                 'planning_estimate': estimated, 'certainty': 'estimate' if estimated else 'reported', 'kind': kind,
                 'location': ctx.get('current_location') or 'home'}
        if kind in {'missed-meal', 'meal'}:
            block.update(meal=meal, source='human-meal-context', nominal_end=end.isoformat())
        ctx.setdefault('temporary_blocks', []).append(block)
        ctx['personal_sequence_end'] = end.isoformat()
    if estimated and kind != 'sleep':
        result['notes'].append(labels[kind] + ': planning estimate ' + str(int((end-start).total_seconds()//60)) + ' minutes. Give a duration to replace it.')
    compiled['status'] = 'compiled'


def validate_action_batch(parsed, live):
    """Preflight the whole mixed batch before consuming its token or writing."""
    if parsed.get('blocking_conflicts'):
        raise HTTPException(409, 'Resolve the reviewed conflicts before applying: ' + ' '.join(parsed['blocking_conflicts']))
    seen = set()
    for change in parsed.get('tasks') or []:
        action = change.get('action')
        if action not in TASK_ACTIONS:
            raise HTTPException(400, 'Unsupported task action. Interpret again.')
        times = {}
        for field in ('fixed_start', 'fixed_end'):
            if change.get(field):
                try:
                    parsed_time = datetime.fromisoformat(str(change[field]).replace('Z', '+00:00'))
                    times[field] = parsed_time.replace(tzinfo=settings.tz) if parsed_time.tzinfo is None else parsed_time
                except (ValueError, TypeError):
                    raise HTTPException(400, 'Invalid reviewed task time. Interpret again.')
        if len(times) == 2 and times['fixed_end'] <= times['fixed_start']:
            raise HTTPException(400, 'Task end must be after its start.')
        if action == 'create':
            if not str(change.get('title') or '').strip():
                raise HTTPException(400, 'A new task needs a title.')
            continue
        tid = str(change.get('task_id') or '')
        task = live.get(tid)
        if not task or not task.is_actionable or task.status != 0:
            raise HTTPException(409, 'A referenced task changed. Refresh and interpret again.')
        if tid in seen:
            raise HTTPException(409, 'Multiple actions reference the same task. State one final action.')
        seen.add(tid)
        if action != 'update':
            source = task_command(change.get('line') or '')
            if not source or source['action'] != action:
                raise HTTPException(400, 'Task actions need matching explicit source instructions.')


async def apply_task_action(tt, change, live):
    """Execute reviewed existing-task actions; never fall through into create."""
    from . import db, performance_patch, service
    action = change.get('action')
    if action in {'create', 'update'}:
        return None
    task = live[str(change['task_id'])]
    meta = db.get_meta(task.id)
    if action in {'cancel', 'skip'} and change.get('scope') == 'occurrence':
        meta['skipped_dates'] = sorted(set([*meta.get('skipped_dates', []), change['date']]))
        db.set_meta(task.id, meta)
        performance_patch.invalidate_ticktick_cache(projects=False)
        return {'type': 'skipped', 'task_id': task.id, 'title': task.title, 'date': change['date']}
    if action == 'resume':
        meta.pop('cancelled', None)
        meta.pop('skipped_dates', None)
        meta['autoschedule'] = True
        tags = [tag for tag in task.tags if str(tag).lower() != 'cancelled']
        await tt.update_task(task, tags=tags)
        db.set_meta(task.id, meta)
    elif action == 'cancel':
        if not meta.get('duration_minutes') and task.duration_minutes:
            meta['duration_minutes'] = task.duration_minutes
        meta.update(cancelled=True, autoschedule=False)
        tags = list(dict.fromkeys([*[tag for tag in task.tags if str(tag).lower() != 'fixed'], 'cancelled']))
        if task.repeat_flag:
            await tt.update_task(task, tags=tags)
        else:
            await tt.clear_task_schedule(task, tags=tags)
        db.set_meta(task.id, meta)
    elif action == 'complete':
        await tt.complete_task(task.project_id, task.id)
        if not task.repeat_flag:
            meta.update(remaining_minutes=0, explicitly_completed=True)
            db.set_meta(task.id, meta)
        live.pop(task.id, None)
    elif action == 'delete':
        await tt.delete_task(task.project_id, task.id)
        # Deletion is not evidence that a prerequisite was accomplished.
        for other in live.values():
            current = db.get_meta(other.id)
            if task.id in {str(x) for x in current.get('dependencies') or []}:
                blocked = set(current.get('removed_prerequisites') or [])
                blocked.add(task.id)
                current['removed_prerequisites'] = sorted(blocked)
                db.set_meta(other.id, current)
        db.delete_meta(task.id)
        live.pop(task.id, None)
    else:
        raise HTTPException(400, 'Unsupported task action.')
    # Only sessions proven owned by the ledger AND source marker are cleaned up.
    ledger = {str(record['session_id']): str(record['source_id']) for record in db.active_generated_sessions()}
    for child in list(live.values()):
        if action not in {'cancel', 'delete', 'complete'} or not service._owned_generated_session(child):
            continue
        if ledger.get(child.id) == task.id and service._source_id_from_session(child) == task.id:
            await tt.delete_task(child.project_id, child.id)
            db.set_generated_session_state(child.id, 'deleted')
            live.pop(child.id, None)
    performance_patch.invalidate_ticktick_cache(projects=False)
    outcome = {'cancel': 'cancelled', 'delete': 'deleted', 'complete': 'completed', 'resume': 'resumed'}[action]
    return {'type': outcome, 'task_id': task.id, 'title': task.title}


def carry_active_reality(ctx, now):
    """Carry unexpired reality and a reviewed future plan; expire old day facts."""
    active = []
    for block in (ctx or {}).get('temporary_blocks') or []:
        try:
            end = datetime.fromisoformat(block['end'])
            if end.tzinfo is None:
                end = end.replace(tzinfo=settings.tz)
            if end > now and not block.get('completed'):
                active.append(deepcopy(block))
        except (ValueError, TypeError, KeyError):
            continue
    future_plan = (ctx or {}).get('tomorrow_plan') or {}
    future_plans = {
        str(k): deepcopy(v) for k, v in ((ctx or {}).get('future_day_plans') or {}).items()
        if str(k) >= now.date().isoformat() and isinstance(v, dict)
    }
    retain_plan = bool(
        (future_plan.get('date') and future_plan['date'] >= now.date().isoformat())
        or future_plans
    )
    if not active and not retain_plan:
        return None
    result = {'date': now.date().isoformat(), 'source': 'quick-dump', 'temporary_blocks': active,
              'replan_requested': True, 'personal_scheduler_version': '9.0'}
    current = next((b for b in active if b.get('source') == 'personal-current-activity'), None)
    if current:
        result['current_activity'] = deepcopy(current)
        if current.get('location'):
            result.update(current_location=current['location'], location_reported_at=current.get('reported_at'))
    if retain_plan:
        for field in ('tomorrow_plan', 'future_day_plans', 'intent_date_goals', 'plan_local_dependencies', 'plan_local_earliest',
                      'plan_local_latest_end', 'optional_date_goal_ids', 'after_meal_task_ids', 'personal_venue_overrides',
                      'day_plan', 'meal_after_task_ids', 'meal_not_before', 'after_meal_rest_minutes'):
            if field in ctx:
                result[field] = deepcopy(ctx[field])
        if future_plans:
            result['future_day_plans'] = future_plans
        nearest = min(
            [str(future_plan.get('date'))] if future_plan.get('date') else []
            + list(future_plans.keys())
        )
        result['replan_scope'] = 'tomorrow' if nearest > now.date().isoformat() else 'today'
    if any(b.get('kind') == 'sleep' for b in active):
        result.update(activity_state='sleeping', sleep_until=max(b['end'] for b in active if b.get('kind') == 'sleep'))
    return result


async def schedule_after_actions(create_plan, commit_plan, horizon, commit_requested):
    """Report saved task changes honestly if the subsequent plan cannot finish."""
    from . import db
    plan, committed, errors = None, [], {}
    try:
        plan = await create_plan(horizon)
    except Exception as exc:
        db.audit('quick_dump_replan_failed_after_actions', {'error_type': type(exc).__name__})
        errors['plan_error'] = 'Your task changes were saved. The schedule preview could not finish; build a fresh preview to continue.'
        return plan, committed, errors
    if commit_requested:
        try:
            committed = await commit_plan(plan)
            db.set_kv('last_plan', '')
        except Exception as exc:
            db.audit('quick_dump_schedule_write_failed_after_actions', {'error_type': type(exc).__name__})
            errors['commit_error'] = 'Your task changes were saved. Schedule application did not finish; refresh tasks and review a fresh preview.'
            # A partially applied schedule must never be offered as an intact plan.
            db.set_kv('last_plan', '')
            plan = None
    return plan, committed, errors
