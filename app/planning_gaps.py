"""Explain actual free time after the complete real-life planner has run."""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, time

from . import scheduler
from .config import settings
from .session_titles import task_base_title


def _day_relevant(task, raw, day, config):
    """Does this unfinished task have any scheduling eligibility on this logical day?

    Work whose earliest allowed start is tomorrow (or a later week/month) must not
    make every gap *today* read as constrained. This is a date-level check, not
    a claim that a useful session fits a specific interval.
    """
    day_start, day_end = scheduler._usable_bounds(day, config)
    meta = scheduler.build_meta(task, raw or {})

    def local(when):
        if when is None:
            return None
        return when.replace(tzinfo=settings.tz) if when.tzinfo is None else when.astimezone(settings.tz)

    earliest, latest, hard_stop = local(meta.earliest), local(meta.latest_end), local(meta.hard_stop)
    if meta.allowed_weekdays and day.weekday() not in meta.allowed_weekdays:
        return False
    if earliest and earliest >= day_end:
        return False
    if latest and latest <= day_start:
        return False
    if hard_stop and hard_stop <= day_start:
        return False
    # A future-dated source without an earlier explicit eligibility rule belongs
    # to its own future day. The final state guard may also inject meta.earliest.
    source_start = local(getattr(task, 'start', None))
    if source_start and source_start >= day_end and not earliest:
        return False
    return True


def _actual_meal_aware_hard_busy(tasks, busy, start, horizon_days, config, diagnostics):
    """Do not subtract a stale clock reservation AND its moved flexible meal.

    `_hard_busy` merges overlapping blocks, losing each block's source/label.
    Build the non-meal blockers first, then restore only default meal reservations
    that do not have an authoritative flexible-meal block for the same day/name.
    """
    chosen_meals = diagnostics.get('flexible_meals') or []
    if not chosen_meals:
        return list(scheduler._hard_busy(tasks, busy, start, horizon_days, config))

    actual = set()
    for meal in chosen_meals:
        try:
            when = datetime.fromisoformat(str(meal['start']))
            end = datetime.fromisoformat(str(meal['end']))
            name = str(meal.get('name') or meal.get('label') or '').strip().casefold()
            if name and end > when:
                actual.add((when.date(), name))
        except (TypeError, ValueError, KeyError):
            continue

    blockers = list(scheduler._hard_busy(
        tasks, busy, start, horizon_days, dict(config or {}, meals=[])
    ))
    for day_index in range(horizon_days):
        day = (start + timedelta(days=day_index)).date()
        lower, upper = scheduler._usable_bounds(day, config)
        for meal in config.get('meals') or []:
            try:
                name = str(meal['name'])
                if (day, name.strip().casefold()) in actual:
                    continue
                clock = time.fromisoformat(str(meal['start']))
                minutes = int(meal['minutes'])
                if minutes <= 0:
                    continue
                meal_start = datetime.combine(day, clock, settings.tz)
                if meal_start < lower:
                    meal_start += timedelta(days=1)
                if lower <= meal_start < upper:
                    blockers.append(scheduler.BusyBlock(
                        meal_start, meal_start + timedelta(minutes=minutes), name, 'rules'
                    ))
            except (KeyError, TypeError, ValueError):
                continue
    return blockers


def rebuild_final_gaps(segments, diagnostics, tasks, busy, start, horizon_days, config, meta_map=None):
    """Describe every open awake interval, including empty days and trailing time.

    Run after the final overlap guard so labels refer to the timeline actually shown.
    Reuse compiled unfinished-work windows; do not infer new permission to place work.
    """
    from .final_productivity_contract_patch import classify_productivity_gaps
    diagnostics = dict(diagnostics or {})
    calendar_end = (start + timedelta(days=max(1, horizon_days))).replace(hour=0, minute=0, second=0, microsecond=0)
    # A logical day may legally continue after midnight when the saved bedtime does.
    # Keep those already-enforced sleep/wind-down diagnostics visible without extending
    # the planner into another day's wake window or changing any scheduling geometry.
    last_day = (start + timedelta(days=max(0, horizon_days - 1))).date()
    _, logical_end = scheduler._usable_bounds(last_day, config)
    display_end = max(calendar_end, logical_end)
    diagnostics['planning_window_start'] = start.isoformat()
    diagnostics['planning_window_end'] = display_end.isoformat()
    for key in ('fixed_timeline', 'reality_timeline', 'flexible_meals', 'human_uncertainty_buffers'):
        kept=[]
        for row in diagnostics.get(key) or []:
            try:
                if datetime.fromisoformat(row['start']) < display_end and datetime.fromisoformat(row['end']) > start:
                    kept.append(row)
            except (ValueError, TypeError, KeyError):
                continue
        diagnostics[key]=kept
    blockers = _actual_meal_aware_hard_busy(
        tasks, busy, start, horizon_days, config, diagnostics
    )
    blockers.extend(scheduler.BusyBlock(s.start, s.end, s.title, 'planned') for s in segments)
    for key in ('fixed_timeline', 'reality_timeline', 'flexible_meals', 'human_uncertainty_buffers'):
        for row in diagnostics.get(key) or []:
            try:
                a = datetime.fromisoformat(row['start'])
                b = datetime.fromisoformat(row['end'])
                if b > a:
                    blockers.append(scheduler.BusyBlock(a, b, row.get('label') or row.get('name') or key, key))
            except (ValueError, TypeError, KeyError):
                continue
    gaps = []
    for day_index in range(horizon_days):
        day = (start + timedelta(days=day_index)).date()
        lower, upper = scheduler._usable_bounds(day, config)
        lower = max(lower, start)
        for a, b in scheduler.free_windows(lower, upper, blockers) if lower < upper else []:
            minutes = int((b-a).total_seconds() // 60)
            if minutes >= 15:
                gaps.append({'start': a.isoformat(), 'end': b.isoformat(), 'minutes': minutes})
    diagnostics = dict(diagnostics)
    remaining_ids = {str(w['task_id']) for w in diagnostics.get('unfinished_work') or []}
    for gap in gaps:
        a,b = datetime.fromisoformat(gap['start']),datetime.fromisoformat(gap['end'])
        evidence=[]
        relevant_unfinished=[]
        relevant_non_schedulable=[]
        for task in tasks:
            raw=(meta_map or {}).get(task.id,{})
            if not task.is_actionable or not _day_relevant(task, raw, a.date(), config):
                continue
            meta=scheduler.build_meta(task,raw)
            if str(task.id) in remaining_ids and meta.autoschedule and 'fixed' not in {x.casefold() for x in task.tags}:
                relevant_unfinished.append(str(task.id))
                if meta.earliest and meta.earliest >= b:
                    evidence.append(f"{task.title}: earliest allowed start is {meta.earliest.strftime('%a %d %b %H:%M')}.")
                elif meta.latest_end and meta.latest_end <= a:
                    evidence.append(f"{task.title}: its allowed window ended at {meta.latest_end.strftime('%a %d %b %H:%M')}.")
                elif raw.get('weekly_bucket') and 0 <= int((config.get('weekly_capacity_minutes') or {}).get(raw['weekly_bucket'], -1)) < meta.min_chunk:
                    evidence.append(f"{task.title}: its weekly time budget is smaller than its minimum session.")
            if not meta.autoschedule or scheduler.duration_for(task, meta) is None:
                relevant_non_schedulable.append(str(task.id))
            if str(task.id) in remaining_ids and raw.get('removed_prerequisites'):
                evidence.append(f"{task.title}: waiting for a removed prerequisite; its dependency must be resolved first.")
        gap['relevant_unfinished_ids']=relevant_unfinished
        gap['relevant_not_schedulable_ids']=relevant_non_schedulable
        gap['constraint_details']=list(dict.fromkeys(evidence))
    diagnostics['planning_gaps'] = gaps
    return classify_productivity_gaps(diagnostics)


def install_gap_explanations(base_plan):
    def explained_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        segments, warnings, diagnostics = base_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map)
        diagnostics = dict(diagnostics or {})
        usable_start, _ = scheduler._usable_bounds(start.date(), config)
        diagnostics['planning_start'] = max(start, usable_start).isoformat()
        groups = defaultdict(list)
        for segment in segments:
            groups[segment.task_id].append(segment)
        numbered = []
        for group in groups.values():
            for index, segment in enumerate(sorted(group, key=lambda s: s.start), 1):
                numbered.append(replace(segment, segment_index=index, segment_count=len(group)))
        segments = sorted(numbered, key=lambda s: s.start)
        protected = [r for r in diagnostics.get('human_uncertainty_buffers', []) if r.get('source') != 'study-recovery']
        heavy = [s for s in segments if scheduler.tag_energy(s.source_task, scheduler.build_meta(s.source_task, meta_map.get(s.task_id, {}))) == 'high']
        for segment in heavy:
            minutes = int((segment.end-segment.start).total_seconds()/60)
            recovery = scheduler._recovery_after_minutes(minutes, segment.end, config)
            _, close = scheduler._usable_bounds(segment.start.date(), config)
            end = min(close, segment.end+timedelta(minutes=recovery))
            if end > segment.end:
                protected.append({'label': 'Break before more study', 'start': segment.end.isoformat(),
                                  'end': end.isoformat(), 'source': 'study-recovery', 'advisory': True})
        wind_down = max(0, int(config.get('bedtime_wind_down_minutes', 0)))
        if wind_down:
            for dd in range(horizon_days):
                lower, close = scheduler._usable_bounds((start+timedelta(days=dd)).date(), config)
                a = max(start, lower, close-timedelta(minutes=wind_down))
                if a < close:
                    protected.append({'label': 'Wind down before sleep', 'start': a.isoformat(),
                                      'end': close.isoformat(), 'source': 'bedtime-wind-down'})
        diagnostics['human_uncertainty_buffers'] = protected
        rows = [{'start': s.start.isoformat(), 'end': s.end.isoformat(), 'label': task_base_title(s.source_task)} for s in segments]
        for key in ('fixed_timeline', 'reality_timeline', 'flexible_meals', 'human_uncertainty_buffers'):
            rows.extend(diagnostics.get(key) or [])
        rows.sort(key=lambda r: datetime.fromisoformat(r['start']))
        original_ids = {t.id for t in tasks if t.is_actionable}
        unfinished = [r for r in diagnostics.get('unfinished_work', []) if r['task_id'] in original_ids]
        gaps = []
        covered = datetime.fromisoformat(diagnostics['planning_start'])
        for row in rows:
            a, b = datetime.fromisoformat(row['start']), datetime.fromisoformat(row['end'])
            if covered is not None and a > covered:
                minutes = int((a-covered).total_seconds()/60)
                if minutes >= 15:
                    fitting = []
                    for work in unfinished:
                        for window in work.get('legal_windows') or []:
                            lower = max(covered, datetime.fromisoformat(window['start']))
                            upper = min(a, datetime.fromisoformat(window['end']))
                            needed = work['remaining_minutes'] if work['must_finish'] or not work['splittable'] else work['min_session_minutes']
                            if int((upper-lower).total_seconds()/60) >= needed:
                                fitting.append(work['title'])
                                break
                    if fitting:
                        reason = 'A useful session for unfinished work fits the basic time constraints here: ' + ', '.join(fitting[:2]) + '. It was not selected by the planner.'
                        kind = 'eligible-work'
                    elif unfinished:
                        minimum = min(r['remaining_minutes'] if r['must_finish'] or not r['splittable'] else r['min_session_minutes'] for r in unfinished)
                        reason = (f'Remaining work needs at least {minimum} minutes for a useful session; only {minutes} minutes are open.'
                                  if minutes < minimum else 'Remaining work cannot fit here within its timing, prerequisite, recovery or another hard constraint.')
                        kind = 'constrained-work'
                    else:
                        reason = 'All work with a usable estimate is allocated in this preview. You can use this time for another task.'
                        kind = 'no-estimated-work'
                    gaps.append({'start': covered.isoformat(), 'end': a.isoformat(), 'minutes': minutes,
                                 'reason': reason, 'kind': kind, 'fitting_task_titles': fitting})
            covered = max(covered or b, b)
        diagnostics['planning_gaps'] = gaps
        diagnostics['maximize_productive_time'] = bool(config.get('maximize_productive_time'))

        # Final user-facing truth: distinguish eligible capacity (Case A) from genuine
        # unallocated time. This classifier uses the legal windows already produced
        # by the mature planner and never invents eligibility.
        from .final_productivity_contract_patch import classify_productivity_gaps
        diagnostics = classify_productivity_gaps(diagnostics)
        return segments, warnings, diagnostics
    return explained_plan
