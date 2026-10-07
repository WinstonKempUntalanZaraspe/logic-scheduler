from __future__ import annotations

"""Typed, clause-local intake before any legacy task inference or routing.

Control prose never reaches the task parser. Legacy handlers are used only for
supported task/state/directive clauses, one at a time. Uncertain prose is visible
as a clarification, not converted into a default-duration task.
"""

import re
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict

from .config import settings
from . import quickdump as qd
from . import human_context_patch as human
from . import schedule_instruction_patch as instructions

_LEGACY = human.human_context_parse
Kind = Literal['task', 'state', 'directive', 'goal', 'relationship', 'clock', 'meal-completed', 'progress-state', 'history', 'replan', 'constraint', 'output', 'ambiguous', 'task-action', 'reality']


class Intent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Kind
    text: str
    payload: str


class IntentDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intents: list[Intent]
_ACTION = re.compile(r"^(?:read|do|study|revise|practice|practise|finish|write|work on|solve|review|learn|watch|buy|pay|call|email|reply|send|submit|print|upload|book|clean|tidy|organize|organise|move|pack|repair|fix|build|code|run|swim|shower|change|travel|go|take|prepare|make|use|keep)\b", re.I)
_CONTROL = re.compile(r"^(?:do not|don't|dont|never|preserve|respect|avoid|treat|reuse|look ahead|account for|use (?:deadlines|priorities|future|dependencies)|clean up (?:both|today|tomorrow|scheduler|the schedule)|remove (?:stale|duplicate|only)|repair (?:broken|split)|keep (?:physical|outing|the chain)|if (?:a|only|a task)|after replanning|check (?:transitions|overlaps|travel)|minimize|carry (?:legitimate|unfinished)|also schedule (?:lunch|dinner|meals)|because|when (?:planning|scheduling)|meals (?:must|should)|move flexible|schedule (?:lunch|dinner|meals) realistically|do not assume)\b", re.I)
_OUTPUT = re.compile(r"^(?:how (?:you|the)|what (?:existing|was|new|blocks|you)|the (?:resulting|cleaned)|any (?:remaining|unfinished|unscheduled)|show(?: me)?\b|after replanning|output\b|report\b)", re.I)
_CREATION = re.compile(r"^(?:(?:please\s+)?(?:add|create)\s+(?:(?:a|new)\s+)?task\s*:?|new task\s*:|task\s*:|remind me to\s+|(?:please\s+)?(?:add|create)\s+)", re.I)


def _norm(text):
    text = str(text or '').replace('’', "'").replace('“', '"').replace('”', '"')
    text = re.sub(r"\bi've\b", 'I have', text, flags=re.I)
    text = re.sub(r'\btmr\b', 'tomorrow', text, flags=re.I)
    text = re.sub(r'\btdy\b', 'today', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip()


def clauses(text):
    # Numbered output lists retain their section role; decimals and clocks survive.
    # Strip list markers before sentence splitting so "1. How ..." never yields
    # a bogus standalone "1" task/clarification.
    cleaned = re.sub(r'(?m)^\s*(?:[-*•]\s+|\d+[.)]\s+)', '', str(text or ''))
    for raw in re.split(r'[\r\n;]+|(?<=[.!?])\s+(?=[A-Za-z])', cleaned):
        raw = raw.strip()
        if not raw:
            continue
        numbered = bool(re.match(r'^\d+[.)]\s+', raw))
        value = re.sub(r'^(?:[-*•]\s+|\d+[.)]\s+)', '', raw).strip(' .')
        # Independent statements joined by conjunctions must not swallow new work.
        for part in re.split(r',\s*(?=after\s+\d+\s*(?:minutes?|mins?|hours?|hrs?)\b)|,?\s+(?:and|but)\s+(?=(?:add|create|delete|cancel|skip|complete|resume|mark|remind me|replan|reschedule|I (?:need|have|want)|i(?:\x27m| am))\b)', value, flags=re.I):
            # Keep ordinary task chains intact for the relationship compiler. Split
            # narrated personal recovery so each duration belongs to its own step.
            from .personal_intents import split_personal_sequence
            for step in split_personal_sequence(part):
                yield _norm(step), numbered


def classify(text, numbered=False, output_section=False, now=None):
    low = _norm(text).lower()
    explicit = _CREATION.match(text)
    if explicit:
        title = text[explicit.end():].strip(' :"')
        # "add buffers" is a planner instruction; "add task: Buy milk" is work.
        if re.match(r'^(?:buffers?|breaks?|logistics|lunch and dinner|meals)\b', title, re.I) and 'task' not in explicit.group().lower():
            return 'constraint', text
        return ('task', title) if title else ('ambiguous', text)
    from .planning_instructions import is_planning_instruction, is_replan_command
    if is_replan_command(text):
        return 'replan', text
    if is_planning_instruction(text):
        return 'constraint', text
    if _OUTPUT.match(low) or (numbered and output_section):
        return 'output', text
    if re.match(r'^(?:suppose|for example|example|e\.g\.)\b', low):
        return 'history', text
    if re.match(r'^i (?:need|have) to (?:rest|recover)\b', low):
        return 'constraint', text
    if re.search(r'^(?:it is|it\x27s|its|now is|the time is)\s+(?:now\s+)?\d{1,2}[:\d\s]*(?:am|pm)?\b', low):
        return 'clock', text
    completion = r'^(?:i\s+)?(?:(?:have|just|already)\s+)*(?:finished(?: eating)?|done with|completed)\b'
    if re.match(completion + r'\s+(?:my\s+)?(?:breakfast|lunch|dinner)\b', low) or re.match(r'^(?:my )?(?:breakfast|lunch|dinner) (?:is|was) (?:already )?(?:done|finished|completed)\b', low):
        return 'meal-completed', text
    if re.search(r'\b(?:have not|haven\x27t|did not|didn\x27t|not completed|none of|nothing else|haven.?t done)\b', low):
        return 'progress-state', text
    if re.match(r'^(?:i )?(?:spent|was working|worked|have been working)\b', low):
        return 'history', text
    if re.match(completion, low) or re.search(r'\b(?:is|was|has been) (?:already )?(?:done|finished|completed)\b', low):
        return 'history', text
    # Reported speech is information about somebody else's request, not direct
    # authority from the user to create work.
    if re.match(
        r"^(?:(?:my\s+)?(?:friend|coach|teacher|lecturer|boss|dad|father|mom|mum|mother|parent|brother|sister)"
        r"|the\s+[a-z][a-z -]{1,30}|[a-z][a-z'-]{1,30})\s+"
        r"(?:said|says|told\s+me|asked\s+me|wants?\s+me|suggested|recommended)\b",
        low,
    ):
        return 'history', text
    # Meta-negation about the scheduler must never be inverted into the very task
    # the user is forbidding.
    if re.match(r"^i\s+(?:don't|dont|do not)\s+want\s+(?:(?:you|the scheduler|this app|it)\s+)?(?:to\s+)?(?:add|create|make|schedule)\b", low):
        return 'constraint', text
    # A departure/home deadline is a boundary on the day, not a chore named
    # "leave by 6".
    if re.match(r"^i\s+(?:have|need)\s+to\s+(?:leave|depart|be\s+home|get\s+home|return\s+home)\s+by\s+\d", low):
        return 'constraint', text
    # "Changed my mind about X" communicates reversal but not enough authority to
    # infer whether X should be skipped once, cancelled, or deleted.
    if re.match(r"^i\s+(?:just\s+)?changed\s+my\s+mind\b", low):
        return 'ambiguous', text
    # Declarative date assignment for existing work, e.g. "Physics is for tomorrow".
    dated_assignment = re.match(r"^(.+?)\s+(?:is|are)\s+for\s+(today|tomorrow)\s*(?:instead)?$", low)
    if dated_assignment:
        return 'goal', text
    # Uncertainty, negative belief and hypothetical capability are context/questions,
    # never authority to create a new task. Examples: "I don't think I can swim
    # tonight", "I'm not sure I can study later", "I doubt we'll go gym".
    if re.match(
        r"^(?:(?:i|we)\s+(?:(?:don't|dont|do not|didn't|didnt|did not)\s+(?:think|know|believe|expect)|doubt)\b"
        r"|i(?:'m| am)\s+not\s+sure\b|we(?:'re| are)\s+not\s+sure\b)",
        low,
    ):
        return 'ambiguous', text
    if low.endswith('?'):
        return 'ambiguous', text
    if not _CONTROL.match(low) and re.match(r"^(?:i|we)\s+(?:won't|will not|wouldn't|cannot|can't|might|may|could)\b|^(?:if|unless|what if)\b", low):
        return 'ambiguous', text
    if re.match(r'^(?:for (?:today|tomorrow)|clean up (?:both|today|tomorrow)|replan)\b', low):
        if re.search(r'\b(?:replan|clean up|schedule|today|tomorrow)\b', low):
            return 'replan', text
    if re.search(r'\b(?:replan|reschedule|rebuild|optimize|optimise)\b', low) and re.search(r'\b(?:day|today|schedule|tomorrow|everything|morning)\b', low) and not instructions._schedule_grammar(text):
        return 'replan', text
    if re.match(r'^(?:audit|clean up)\b', low) and re.search(r'\b(?:tomorrow|today|schedule)\b', low):
        return 'replan', text
    # A named exclusion has scheduler semantics; a generic safety sentence does not.
    if _CONTROL.match(low):
        if instructions._schedule_grammar(text) and instructions._extract_target(text) and not re.search(r'\b(?:immediately|after eating|overlaps|fixed|manual|displaced|unrelated|another part)\b', low):
            return 'directive', text
        return 'constraint', text
    if re.match(r'^(?:make (?:it|the|my|sure)|be mindful|ensure\b|include (?:all|enough|travel|buffers|logistics)|keep\b.*\b(?:appointments?|commitments?|events?|schedule|deadlines?)\b|move (?:lunch|dinner|meals) dynamically|a (?:missing|expired|deleted)\b)', low):
        return 'constraint', text
    if re.search(r'\b(?:must|should|needs? to)\b', low) and not re.match(r'^(?:i (?:need|have) to|(?:i )?must|need to)\b', low):
        return 'constraint', text
    # Actual state handlers only see this clause, never a whole multi-topic prompt.
    if qd._state_line(text) or human._availability_block(text, now or datetime.now(settings.tz)) or re.search(r'\b(?:got interrupted|already (?:did|done|completed)|minutes? (?:left|remaining))\b', low):
        return 'state', text
    if instructions._schedule_grammar(text):
        return 'directive', text
    goal = re.match(r'^(?:i (?:really )?(?:want|need|have) to|(?:please )?schedule)\s+(.+)', text, re.I)
    if goal and re.search(r'\b(?:today|tonight|tomorrow|morning|before|after)\b', low) and not re.match(r'^i (?:need|have) to\b', low):
        return 'goal', goal.group(1)
    # "after Physics" can express a dependency; "after 10 minutes" is a
    # submission-clock start offset and must stay attached to the activity.
    from .relative_start import strip_relative_start
    relationship_scan = strip_relative_start(low)
    if re.search(r'\b(?:depends on|requires|after|before|then)\b', relationship_scan):
        return 'relationship', text
    title = re.sub(r'^(?:i need to|need to|i have to|have to|gotta|i gotta|i must|must|i plan to|plan to|please)\s+', '', text, flags=re.I)
    if _ACTION.match(title) and len(title.split()) <= 25 and not re.search(r'\b(?:scheduler|optimizer|replanning|schedule|blocks|should|would|could|must not)\b', low):
        return 'task', title
    # Short task-list noun phrases are supported; prose does not get a 30m default.
    if len(title.split()) <= 10 and (qd._extract_duration(title) is not None or qd._contains_any(title, qd.EVENT_WORDS | qd.DEEP_WORDS)) and not re.search(r'\b(?:should|would|could|because|means|whether|suppose|example|actually|schedule|tasks|buffers|logistics|everything|none)\b', low):
        return 'task', title
    return 'ambiguous', text


def _task_inference(payload, rows, config, now):
    # Discourse corrections such as "Do Math tomorrow instead" refer to the same
    # work; "instead" is not part of the task title.
    original_payload = re.sub(r"\s+(?:instead|rather)\s*$", "", str(payload or ""), flags=re.I).strip()
    from .relative_start import strip_start_language
    payload = strip_start_language(original_payload)

    def restore_source(parsed):
        for change in parsed.get('tasks') or []:
            change['line'] = original_payload
        return parsed

    candidate = restore_source(qd.parse_task_candidate(payload, [], config, now))
    if not candidate.get('tasks'):
        return candidate
    title = candidate['tasks'][0]['title']
    normalized = instructions._norm(title)
    active = [r for r in rows if instructions._row_schedulable(r)]
    exact = [r for r in active if instructions._norm(r.get('title')) == normalized]
    if not exact:
        # Imperative verbs are syntax, not part of the referenced task identity.
        bare = re.sub(r"^(?:do|study|revise|practice|practise|review|work\s+on|finish|start)\s+", "", normalized, flags=re.I).strip()
        if bare and bare != normalized:
            exact = [r for r in active if instructions._norm(r.get('title')) == bare]
    if len(exact) > 1:
        return {'tasks': [], 'warnings': ['More than one existing task has this title; specify which one you mean. No new task created.']}
    if exact:
        return restore_source(qd.parse_task_candidate(payload, exact, config, now))
    scored = []
    for r in active:
        _, score = qd._match_existing(title, [r])
        # Different work verbs distinguish siblings such as Read/Do Physics.
        a = normalized.split()[:1]
        b = instructions._norm(r.get('title')).split()[:1]
        if a and b and a != b and (_ACTION.match(a[0]) or _ACTION.match(b[0])):
            continue
        if score >= .88:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0]-scored[1][0] < .08:
        return {'tasks': [], 'warnings': ['Task reference is ambiguous; no update or duplicate creation was performed.']}
    if scored:
        return restore_source(qd.parse_task_candidate(payload, [scored[0][1]], config, now))
    return candidate


def _merge(result, parsed, allow_create=False):
    for change in parsed.get('tasks') or []:
        if change.get('action') == 'create' and not allow_create:
            continue
        key = ('update', str(change.get('task_id'))) if change.get('action') == 'update' else ('create', qd._norm(change.get('title')))
        existing = next((x for x in result['tasks'] if (('update', str(x.get('task_id'))) if x.get('action') == 'update' else ('create', qd._norm(x.get('title')))) == key), None)
        if existing:
            patch = existing.get('meta_patch', {}) | change.get('meta_patch', {})
            existing.update(change)
            existing['meta_patch'] = patch
        else:
            result['tasks'].append(deepcopy(change))
    if parsed.get('context'):
        ctx = result.get('context') or {}
        blocks = list(ctx.get('temporary_blocks') or [])
        ctx.update(parsed['context'])
        for block in parsed['context'].get('temporary_blocks') or []:
            if block not in blocks:
                blocks.append(block)
        if blocks:
            ctx['temporary_blocks'] = blocks
        result['context'] = ctx
    for name in ('notes', 'warnings'):
        result[name].extend(parsed.get(name) or [])
    # Never inherit title-based deletion guesses. Ownership is not established by
    # a title matching a command; cleanup belongs to the generated-session audit.


def _set_context(result, now, **values):
    ctx = result.setdefault('context', None) or {'date': now.date().isoformat(), 'source': 'quick-dump'}
    ctx.update(values)
    result['context'] = ctx
    return ctx


def merge_context(current, incoming):
    """Facts accumulate; a fresh replan replaces older planning intentions."""
    if not current or current.get('date') != incoming.get('date'):
        return deepcopy(incoming)
    merged = deepcopy(current) | deepcopy(incoming)
    if incoming.get('intake_version') and incoming.get('replan_requested'):
        for key in ('intent_only_tonight_ids', 'intent_only_tonight_titles',
                    'intent_swim_tomorrow_ids', 'intent_swim_tomorrow_start',
                    'intent_today_ids', 'before_main_study_ids', 'intent_exact_order',
                    'intent_dinner_start', 'intent_dinner_end', 'intent_sleep_end'):
            merged[key] = deepcopy(incoming.get(key, [] if key.endswith('_ids') or key.endswith('_titles') or key=='intent_exact_order' else None))
        merged['defer_discretionary'] = incoming.get('defer_discretionary', False)
        merged['intent_date_goals'] = deepcopy(incoming.get('intent_date_goals') or {})
        merged['intent_exclusions'] = deepcopy(incoming.get('intent_exclusions') or [])
        merged['requested_project_campaign_ids'] = deepcopy(incoming.get('requested_project_campaign_ids') or [])
        merged['requested_project_work_packages'] = deepcopy(incoming.get('requested_project_work_packages') or {})
    meals = dict(current.get('completed_meals') or {}) | dict(incoming.get('completed_meals') or {})
    if meals:
        merged['completed_meals'] = meals
    return merged


def _select_goal(target, rows, now):
    # Full task names first; theme-only references are limited to today's occurrence
    # or undated work, with ambiguity reported instead of choosing a future lookalike.
    active = [r for r in rows if instructions._row_schedulable(r) and 'fixed' not in {str(t).lower() for t in r.get('tags', [])}]
    exact = [r for r in active if instructions._norm(r.get('title')) == instructions._norm(target)]
    if len(exact) == 1:
        return exact, False
    phrase = instructions._norm(target)
    direct = [r for r in active if phrase and phrase in instructions._norm(r.get('title'))]
    if len(direct) > 1:
        return [], True
    scoped = []
    for row in active:
        start = row.get('start')
        try:
            parsed = datetime.fromisoformat(str(start).replace('Z', '+00:00')) if start else None
            day = (parsed.replace(tzinfo=settings.tz) if parsed and parsed.tzinfo is None else parsed).astimezone(settings.tz).date() if parsed else None
        except ValueError:
            day = None
        if day is None or day <= now.date():
            scoped.append(row)
    direct = [r for r in scoped if phrase and phrase in instructions._norm(r.get('title'))]
    if len(direct) == 1:
        return direct, False
    matches, _, ambiguous = instructions._match_rows(scoped, target, False)
    return matches, ambiguous or len(matches) > 1


def extract_intents(text, now):
    intents = []
    output_section = False
    for clause, numbered in clauses(text):
        role, payload = classify(clause, numbered, output_section, now)
        output_section = output_section or bool(re.match(r'^(?:after replanning|show|output)', clause, re.I))
        intents.append(Intent(kind=role, text=clause, payload=payload))
    return IntentDocument(intents=intents)


def _constraint_status(clause):
    low = clause.lower()
    # Existing general safeguards do not implement arbitrary user-supplied limits.
    if re.search(r'\b\d+\s*(?:m|min|minutes?|h|hours?)\b|\b(?:at least|at most|no more than)\b', low):
        return []
    from .planning_instructions import supported_instruction_rules
    supported = supported_instruction_rules(clause)
    if supported:
        return supported
    rules = {
        'protect_fixed': r'\b(?:fixed|manual)\b',
        'no_overlap': r'\b(?:overlaps?|overlap)\b',
        'protect_sleep': r'\b(?:sleep|bedtime|wind[\s-]+down)\b',
        'preserve_effort': r'\b(?:unfinished|completed|remaining|expired|missing)\b',
        'owned_cleanup': r'\b(?:stale|duplicates?|garbage|scheduler-owned|scheduler owned)\b',
        'split_integrity': r'\b(?:split|numbering|parts?|sessions?|unrelated tasks|similar names)\b',
        'outing_integrity': r'\b(?:travel|changing|outing|physical|chain|transitions|real life|swimming|eating)\b',
        'meal_protection': r'\b(?:lunch|dinner|meal|eating|eat)\b',
        'capacity_and_priority': r'\b(?:workload|capacity|deadlines?|priorities|priority|dependencies|energy|buffers|future|available|late|move flexible|cram|impossible amounts)\b',
        'minimal_repair': r'\b(?:reuse|movement|recreat|already-good)\b',
    }
    matched = [name for name, pattern in rules.items() if re.search(pattern, low)]
    return matched


def parse_language(text, rows, config, now=None, document=None):
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    result = {'context': None, 'tasks': [], 'cleanup_tasks': [], 'notes': [], 'warnings': [], 'intents': [], 'clarifications': [], 'parser_version': 'typed-intake-1'}
    document = document or extract_intents(text, now)
    for intent in document.intents:
        clause, role, payload = intent.text, intent.kind, intent.payload
        compiled = {'kind': role, 'text': clause, 'status': 'interpreted'}
        result['intents'].append(compiled)
        if role in {'task', 'goal'}:
            from .conversational_activity import compile_day_activity
            dated_existing_assignment = role == 'goal' and bool(
                re.match(r"^.+?\s+(?:is|are)\s+for\s+(?:today|tomorrow)\s*(?:instead)?$", _norm(clause), re.I)
            )
            if not dated_existing_assignment and compile_day_activity(clause, payload, result, compiled, rows, config, now):
                continue
        if role in {'task-action', 'reality'}:
            from .personal_intents import compile_task_action, compile_reality
            compiler = compile_task_action if role == 'task-action' else compile_reality
            compiler(clause, result, compiled, rows, config, now)
            continue
        if role in {'task', 'state', 'directive'}:
            if role == 'directive':
                spec = instructions._instruction_spec(payload, rows, now)
                if spec and spec['kind'] == 'exclude':
                    if spec['ambiguous'] or not spec['rows'] or not spec['day']:
                        compiled['status'] = 'needs-input'
                        result['clarifications'].append({'text': clause, 'reason': 'Exclusion needs a clear task and day; nothing was created or moved.'})
                    else:
                        ctx = _set_context(result, now, replan_requested=True, replan_scope='today', replan_from=now.isoformat())
                        ctx.setdefault('intent_exclusions', []).append({'task_ids':[str(r['id']) for r in spec['rows']], 'date':spec['day'].isoformat()})
                        ctx['minimum_horizon_days'] = max(int(ctx.get('minimum_horizon_days') or 1), min(14, (spec['day']-now.date()).days+1))
                        compiled['status'] = 'compiled'
                    continue
            parsed = _task_inference(payload, rows, config, now) if role == 'task' else _LEGACY(payload, rows, config, now)
            _merge(result, parsed, allow_create=role == 'task')
            if role == 'task':
                for change in result['tasks']:
                    if change.get('action') == 'create':
                        change['intake_kind'] = 'task'
            compiled['status'] = 'compiled' if parsed.get('tasks') or parsed.get('context') else 'needs-input'
            if role == 'task':
                day, _ = instructions._day_from_clause(payload, now)
                if day and day > now.date() and not qd._extract_deadline(payload, now):
                    awake, _ = instructions._awake_bounds(day, config)
                    for change in result['tasks']:
                        if change.get('line') == payload:
                            change.setdefault('meta_patch', {})['earliest'] = awake.isoformat()
            continue
        if role == 'relationship':
            # Explicit relationships may change matched IDs, never create prose tasks.
            from .intelligence_patch import _explicit_relations
            relationships = {'tasks': [], 'notes': [], 'warnings': []}
            _explicit_relations(payload, relationships, rows)
            _merge(result, relationships)
            continue
        if role == 'goal':
            target = re.split(r'\b(?:before|after|today|tonight|tomorrow|morning|evening)\b', payload, maxsplit=1, flags=re.I)[0].strip()
            matched, ambiguous = _select_goal(target, rows, now)
            if not matched or ambiguous:
                result['clarifications'].append({'text': clause, 'reason': 'Name one existing task clearly; no task was created.'})
                continue
            ctx = _set_context(result, now, replan_requested=True, replan_from=now.isoformat(), replan_scope='today')
            ids = [str(r['id']) for r in matched]
            target_day, _ = instructions._day_from_clause(payload, now)
            target_day = target_day or now.date()
            ctx.setdefault('intent_date_goals', {}).update({tid:target_day.isoformat() for tid in ids})
            ctx['minimum_horizon_days'] = max(int(ctx.get('minimum_horizon_days') or 1), min(14, (target_day-now.date()).days+1))
            if target_day == now.date():
                ctx['intent_today_ids'] = list(dict.fromkeys([*ctx.get('intent_today_ids', []), *ids]))
            if re.search(r'\bbefore\b.*\b(?:study|studying|work)\b', payload, re.I):
                ctx['before_main_study_ids'] = ids
            result['notes'].append('Date preference ' + target_day.isoformat() + ': ' + ', '.join(str(r['title']) for r in matched))
            continue
        if role == 'clock':
            # Submission/server time is authoritative for placement. A stale copied
            # clock is retained as reported context, never moves the plan backwards.
            clock = qd._extract_explicit_clock(clause)
            _set_context(result, now, reported_clock=clock, planning_now=now.isoformat())
            continue
        if role == 'meal-completed':
            meal = re.search(r'\b(breakfast|lunch|dinner)\b', clause, re.I).group(1).lower()
            clock = qd._extract_explicit_clock(clause)
            end = now.replace(second=0, microsecond=0)
            if clock:
                h, m = map(int, clock.split(':'))
                reported = end.replace(hour=h, minute=m)
                if reported <= now:
                    end = reported
                else:
                    result['warnings'].append('Reported meal completion is in the future; using the current time. Please check the clock.')
            ctx = _set_context(result, now, replan_requested=True, replan_scope='today', replan_from=now.isoformat())
            meals = dict(ctx.get('completed_meals') or {})
            meals[meal] = end.isoformat()
            ctx['completed_meals'] = meals
            # Historical meal interval provides recovery information without creating
            # either a permanent task or a second meal in the future schedule.
            ctx.setdefault('temporary_blocks', []).append({'label': meal.title(), 'meal': meal, 'start': (end-timedelta(minutes=30)).isoformat(), 'end': end.isoformat(), 'nominal_end': end.isoformat(), 'source': 'human-meal-context', 'certainty': 'high', 'completed': True})
            continue
        if role == 'progress-state':
            _set_context(result, now, preserve_unfinished=True, catch_up_missed=True)
            result['notes'].append('Unconfirmed/missed work remains unfinished; no tasks marked complete.')
            continue
        if role == 'replan':
            ctx = _set_context(result, now, replan_requested=True, replan_scope='today', replan_from=now.isoformat(), catch_up_missed=True, preserve_unfinished=True)
            # A literal "today" request is a hard one-day scope. Generic "replan my
            # schedule" still respects the user's selected 2/7/14-day horizon.
            ctx['explicit_today_scope'] = bool(re.search(r'\btoday\b|\brest\s+of\s+(?:the\s+)?day\b', clause, re.I))
            if re.search(r'\btomorrow\b', clause, re.I):
                ctx['explicit_today_scope'] = False
                ctx['minimum_horizon_days'] = max(2, int(ctx.get('minimum_horizon_days') or 1))
                ctx['cleanup_dates'] = [now.date().isoformat(), (now+timedelta(days=1)).date().isoformat()]
            continue
        if role == 'constraint':
            ctx = _set_context(result, now)
            ctx.setdefault('requested_constraints', []).append(clause)
            rules = _constraint_status(clause)
            compiled.update(status='existing-rule' if rules else 'needs-input', rules=rules)
            if not rules:
                result['clarifications'].append({'text': clause, 'reason': 'This constraint has no supported planner rule yet; it was not silently enforced.'})
            if 'meal_protection' in rules:
                ctx['protect_meals'] = True
            if 'protect_sleep' in rules:
                ctx['protect_sleep'] = True
            if 'submission_clock' in rules:
                ctx.update(replan_requested=True, replan_from=now.isoformat(), planning_now=now.isoformat(),
                           timezone=settings.timezone)
                result['notes'].append(f'Planning uses the submission time {now:%Y-%m-%d %H:%M:%S} ({settings.timezone}); elapsed time is not available for new work.')
            if 'productive_time' in rules:
                ctx['maximize_productive_time'] = True
                result['notes'].append('Use available time for useful work in this plan, while preserving meals, recovery, travel and sleep.')
            continue
        if role == 'ambiguous':
            result['clarifications'].append({'text': clause, 'reason': 'Unclear whether this is work or a planning instruction; no task created.'})
    for item in result['clarifications']:
        result['warnings'].append(item['reason'] + ' “' + item['text'] + '”')
    if result.get('context'):
        result['context']['intake_version'] = result['parser_version']
    result['line_count'] = len(result['intents'])
    result['minimum_horizon_days'] = (result.get('context') or {}).get('minimum_horizon_days', 1)
    from .personal_scheduler import finalize_intake
    finalize_intake(result, text, rows, config, now)
    _validate_updates(result, rows)
    for key in ('warnings', 'notes'):
        result[key] = list(dict.fromkeys(result[key]))
    return result


def _validate_updates(result, rows):
    from .scheduler import dependency_cycle
    meta = {str(r['id']): dict(r.get('meta') or {}) for r in rows if r.get('id')}
    known = set(meta)
    for change in result['tasks']:
        if change.get('action') != 'update':
            continue
        tid = str(change.get('task_id'))
        if tid not in known:
            raise ValueError('Interpretation references an unknown task.')
        patch = change.get('meta_patch') or {}
        deps = patch.get('dependencies')
        if deps is not None:
            if not set(deps) <= known or dependency_cycle(tid, deps, meta):
                patch.pop('dependencies', None)
                result['warnings'].append('Dependency rejected: unknown task or cycle involving ' + str(change.get('title')))
            else:
                meta[tid] = meta[tid] | patch


def validate_task_creation(parsed):
    """Fail closed at preview/routing/write boundaries, even if a parser regresses."""
    invalid = [x for x in parsed.get('tasks', []) if x.get('action') == 'create' and x.get('intake_kind') != 'task']
    if invalid:
        raise ValueError('Unclassified prose reached task creation. Reinterpret the request before applying.')


def intent_aware_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
    from .human_adjuster_patch import human_adjusted_plan
    from . import reality_patch as reality
    cfg = deepcopy(config or {})
    metas = deepcopy(meta_map or {})
    ctx = cfg.get('_quick_context') or {}
    if ctx.get('protect_meals'):
        cfg['meal_protection'] = True
    if ctx.get('protect_sleep'):
        cfg['protect_sleep'] = True
    current_context = ctx.get('date') == start.date().isoformat()
    active_ids = {t.id for t in tasks if t.status == 0 and t.is_actionable}
    today_ids = set(ctx.get('intent_today_ids') or []) & active_ids if ctx.get('date') == start.date().isoformat() else set()
    before_ids = set(ctx.get('before_main_study_ids') or []) & today_ids
    primaries = [t for t in tasks if t.status == 0 and reality._is_primary_outing(t, metas.get(t.id, {}))]
    def containing_outing(tid):
        task = next((t for t in tasks if t.id == tid), None)
        return reality._choose_primary(task, primaries, metas) if task and reality._is_support(task) else None
    for tid in list(today_ids):
        primary = containing_outing(tid)
        if primary and 'fixed' not in {str(tag).lower() for tag in primary.tags}:
            today_ids.add(primary.id)
            if tid in before_ids:
                before_ids.discard(tid)
                before_ids.add(primary.id)
    _, sleep = instructions._awake_bounds(start.date(), cfg)
    def restrict_date(tid, lower, upper):
        from .scheduler import _ctx_dt
        raw = metas.setdefault(tid, {})
        old_lower = _ctx_dt(raw.get('earliest'))
        old_upper = _ctx_dt(raw.get('latest_end'))
        raw.update(earliest=max(start, lower, old_lower or lower).isoformat(), latest_end=min(upper, old_upper or upper).isoformat(), timing='asap')
        # A date is an availability/priority request, not an instruction to finish
        # an entire subject. Preserve explicit atomic work; ordinary work may progress.
        raw.setdefault('must_finish', False)
        if raw.get('intent_optional'):
            raw['must_finish'] = False
    for tid, day in ((ctx.get('intent_date_goals') or {}) if current_context else {}).items():
        if tid not in active_ids:
            continue
        target_day = datetime.fromisoformat(day).date()
        lower, upper = instructions._awake_bounds(target_day, cfg)
        restrict_date(tid, lower, upper)
        primary = containing_outing(tid)
        if primary and 'fixed' not in {str(tag).lower() for tag in primary.tags}:
            restrict_date(primary.id, lower, upper)
    for exclusion in (ctx.get('intent_exclusions') or []) if current_context else []:
        target_day = datetime.fromisoformat(exclusion['date']).date()
        lower, upper = instructions._awake_bounds(target_day, cfg)
        excluded_ids = set(exclusion['task_ids'])
        for support in tasks:
            if support.id in excluded_ids and reality._is_support(support):
                primary = reality._choose_primary(support, primaries, metas)
                if primary:
                    excluded_ids.add(primary.id)
        for tid in excluded_ids:
            cfg.setdefault('_task_exclusion_windows', {}).setdefault(tid, []).append({'start':lower.isoformat(),'end':upper.isoformat()})
    for tid in today_ids:
        raw = metas.setdefault(tid, {})
        primary = next(t for t in tasks if t.id == tid)
        if reality._is_fixed(primary):
            continue
        restrict_date(tid, start, sleep)
        if reality._is_primary_outing(primary, raw) and ctx.get('requested_constraints') and any(re.search(r'\b(?:travel|logistics|change)\b', c, re.I) for c in ctx['requested_constraints']):
            supports = [t for t in tasks if t.status == 0 and reality._is_support(t) and reality._choose_primary(t, [primary], metas)]
            if not supports:
                # No guessed travel durations or fake task creation. Surface missing
                # event logistics rather than returning an impossible naked swim.
                raw['autoschedule'] = False
                cfg.setdefault('_missing_outing_logistics', []).append(primary.title)
    for task in tasks:
        raw = metas.setdefault(task.id, {})
        tags = {str(x).lower() for x in task.tags}
        if task.status != 0 or 'fixed' in tags or 'autoscheduler-session' in tags or task.id in before_ids or reality._is_support(task):
            continue
        deep = 'deep-work' in tags or raw.get('energy') == 'high' or qd._contains_any(task.title, qd.DEEP_WORDS)
        if deep and before_ids:
            raw['dependencies'] = list(dict.fromkeys([*raw.get('dependencies', []), *sorted(before_ids)]))
    segments, warnings, diagnostics = human_adjusted_plan(tasks, metas, busy, start, horizon_days, cfg, mastery_map)
    optional_ids = {str(x) for field in ('optional_today_ids', 'optional_date_goal_ids', 'contingency_optional_ids') for x in ctx.get(field) or []}
    fixed_ids = {t.id for t in tasks if reality._is_fixed(t)}
    missing = today_ids - fixed_ids - optional_ids - {s.task_id for s in segments if s.start.date() == start.date()}
    for tid in sorted(missing):
        title = next(t.title for t in tasks if t.id == tid)
        warnings.append(f'{title}: requested today but no usable session fit its windows, prerequisites, meals, logistics and sleep. Unfinished work remains; it was not silently moved to tomorrow.')
    for title in cfg.get('_missing_outing_logistics', []):
        warnings.append(title + ': existing travel/preparation/return steps were not found. Add those event details before applying an outing plan.')
    diagnostics['explicit_today_unplaced'] = sorted(missing)
    diagnostics['intake_version'] = ctx.get('intake_version')
    return segments, warnings, diagnostics
