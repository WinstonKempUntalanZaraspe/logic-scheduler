"""Explain actual free time after the complete real-life planner has run."""
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, time

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
    if meta.allowed_weekdays and day.weekday() not in meta.allowed_weekdays:
        # "Today only" plus a conflicting weekday is a real constraint conflict,
        # unlike ordinary work deliberately assigned to another weekday.
        today_only = (earliest and latest and day_start <= earliest < latest <= day_end)
        dated_today = source_start and day_start <= source_start < day_end
        if not today_only and not dated_today:
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


def _needed(row):
    # Must-finish means all CHUNKS or none, not "one continuous block".
    # Only an explicitly unsplittable task needs its entire remainder in one slot.
    return max(1, int(
        row.get('remaining_minutes') if not row.get('splittable', True)
        else row.get('min_session_minutes') or 0
    ))


def _refresh_unfinished_windows(rows, tasks, meta_map, segments, busy, start, horizon_days, config, blockers):
    """Recompute legal windows against the FINAL timeline (one blocker source).

    Earlier layers computed windows with `_hard_busy`, i.e. the CONFIGURED meal clocks. After a
    flexible meal moves (Lunch 12:30 -> 13:00) those windows still exclude 12:30-13:15, so a gap
    could be labelled CONSTRAINED by a reservation the user never sees. Windows are therefore
    derived without default meal clocks and intersected with the real final blockers. Every
    other field of a row is preserved.
    """
    by_id = {t.id: t for t in tasks}
    no_meals = dict(config or {}, meals=[])
    out = []
    for row in rows or []:
        task = by_id.get(row.get('task_id'))
        if task is None:
            out.append(row)
            continue
        merged = dict(row)
        local_meta = {**(meta_map or {}), task.id: dict((meta_map or {}).get(task.id) or {}, min_chunk=max(1, _needed(row) or 1))}
        windows = []
        for lower, upper in scheduler._free_task_windows(task, local_meta, tasks, busy, start, horizon_days, no_meals, segments):
            for a, b in scheduler.free_windows(lower, upper, blockers):
                if b > a:
                    windows.append({'start': a.isoformat(), 'end': b.isoformat()})
        merged['legal_windows'] = windows
        cap, dependent = scheduler._dependents_cap(task, meta_map or {}, tasks, segments, config)
        if cap is not None:
            merged['must_end_before'] = {'time': cap.isoformat(), 'dependent': dependent}
        out.append(merged)
    return out


def _fits(row, a, b):
    needed = _needed(row)
    for window in row.get('legal_windows') or []:
        lower = max(a, datetime.fromisoformat(window['start']))
        upper = min(b, datetime.fromisoformat(window['end']))
        if lower < upper and int((upper - lower).total_seconds() // 60) >= needed:
            return True
    return False


def _complete_multisession_witness(row, task, meta, gap_start, gap_end, segments, config):
    """Prove that *all* remaining must-finish chunks fit, including their breaks.

    CP-SAT defines must_finish as all selected chunks, not one uninterrupted
    session. Use an earliest-valid constructive schedule over final legal windows
    and require at least one of its sessions to land inside the reported gap.
    A bounded constructive check is conservative: it cannot authorize writes.
    """
    from .interactive_quality_speed_patch import _session_budget_remaining
    from .models import Segment

    remaining = max(0, int(row.get('remaining_minutes') or 0))
    if remaining <= 0:
        return None
    chunks = scheduler.choose_chunks(remaining, meta)
    if not chunks or any(
        chunk <= 0 or chunk > meta.max_chunk or chunk < meta.min_chunk
        for chunk in chunks
    ):
        return None

    windows = sorted(
        (
            (datetime.fromisoformat(w['start']), datetime.fromisoformat(w['end']))
            for w in row.get('legal_windows') or []
        ),
        key=lambda pair: pair[0],
    )
    # To prove a CASE A gap can be used, try each candidate window intersecting
    # this gap. All other chunks must still be schedulable in chronological order.
    grid = scheduler.GRID
    between = timedelta(minutes=max(0, int(config.get('between_chunks_buffer', 10))))
    for candidate_start, candidate_end in windows:
        first_start = scheduler.ceil_grid(max(candidate_start, gap_start), grid)
        if not (first_start < min(candidate_end, gap_end)):
            continue

        placed = []
        cursor = first_start
        witness = None
        for minutes in chunks:
            selected = None
            for a, b in windows:
                at = scheduler.ceil_grid(max(a, cursor), grid)
                end = at + timedelta(minutes=minutes)
                if end > b:
                    continue
                # No new chunk may exceed the per-day activity budget.
                allowance = _session_budget_remaining(
                    task.id, at.date(), [*segments, *placed], config
                )
                if allowance is not None and allowance < minutes:
                    continue
                if not scheduler._meal_activity_start_allowed(task.id, at, config, end):
                    continue
                selected = (at, end)
                break
            if selected is None:
                break
            at, end = selected
            if witness is None and gap_start <= at and end <= gap_end:
                witness = (at, minutes)
            placed.append(Segment(
                task.id, task.project_id, task.title, at, end,
                0.0, 'CASE A feasibility witness', task,
            ))
            cursor = end + between
        if len(placed) == len(chunks) and witness is not None:
            return witness
    return None


def _placement(row, task, meta, gap_start, gap_end, segments, config, day):
    """Construct a legal CASE A witness; never confuse atomicity with contiguity."""
    from .interactive_quality_speed_patch import _session_budget_remaining
    grid = scheduler.GRID
    needed = _needed(row)
    remaining = max(0, int(row.get('remaining_minutes') or 0))

    if bool(row.get('must_finish')) and meta.splittable:
        return _complete_multisession_witness(
            row, task, meta, gap_start, gap_end, segments, config
        )

    atomic = not row.get('splittable', True)
    for window in row.get('legal_windows') or []:
        lower = scheduler.ceil_grid(max(gap_start, datetime.fromisoformat(window['start'])), grid)
        upper = min(gap_end, datetime.fromisoformat(window['end']))
        room = int((upper - lower).total_seconds() // 60) // grid * grid
        if room < max(needed, 1):
            continue
        size = remaining if atomic else min(remaining, meta.max_chunk, room)
        budget = _session_budget_remaining(task.id, day, segments, config)
        if budget is not None:
            size = min(size, budget // grid * grid)
        if size < needed or size <= 0 or size > room:
            continue
        if atomic and size != remaining:
            continue
        if not scheduler._meal_activity_start_allowed(
            task.id, lower, config, lower + timedelta(minutes=size)
        ):
            continue
        return lower, size
    return None


def _verify_case_a(diagnostics, tasks, segments, config, meta_map):
    """CASE A only where a concrete legal session can be constructed (records it as `witness`)."""
    rows = {str(r['task_id']): r for r in diagnostics.get('unfinished_work') or []}
    by_id = {str(t.id): t for t in tasks}
    counts = diagnostics.setdefault('productivity_cases', {})
    for gap in diagnostics.get('planning_gaps') or []:
        if gap.get('productivity_case') != 'A':
            continue
        a, b = datetime.fromisoformat(gap['start']), datetime.fromisoformat(gap['end'])
        day = date.fromisoformat(gap.get('logical_day') or a.date().isoformat())
        ids = gap.get('relevant_unfinished_ids')
        witness = None
        for task_id in (ids if ids is not None else list(rows)):
            row, task = rows.get(str(task_id)), by_id.get(str(task_id))
            if not row or task is None:
                continue
            meta = scheduler.build_meta(task, (meta_map or {}).get(task.id, {}))
            found = _placement(row, task, meta, a, b, segments, config, day)
            if found:
                witness = {'task_id': task.id, 'title': task.title, 'start': found[0].isoformat(),
                           'end': (found[0] + timedelta(minutes=found[1])).isoformat(), 'minutes': found[1]}
                break
        if witness:
            gap['witness'] = witness
            continue
        minutes = int(gap.get('minutes') or 0)
        detail = 'No legal session can be constructed here (session allowance, activity timing or all-or-nothing work).'
        gap.update(productivity_case='CONSTRAINED', kind='constrained-work', fitting_task_titles=[])
        gap['constraint_details'] = list(dict.fromkeys([*(gap.get('constraint_details') or []), detail]))
        gap['reason'] = 'Cannot fit unfinished work here due to constraints. ' + detail
        counts['case_a_idle_minutes'] = max(0, int(counts.get('case_a_idle_minutes', 0)) - minutes)
        counts['case_a_count'] = max(0, int(counts.get('case_a_count', 0)) - 1)
        counts['constrained_minutes'] = int(counts.get('constrained_minutes', 0)) + minutes
        counts['case_b_open_minutes'] = counts['constrained_minutes']
        counts['constrained_count'] = int(counts.get('constrained_count', 0)) + 1
        counts['case_b_count'] = counts['constrained_count']
    return diagnostics


def rebuild_final_gaps(segments, diagnostics, tasks, busy, start, horizon_days, config, meta_map=None):
    """Describe every open awake interval, including empty days and trailing time.

    Run after the final overlap guard so labels refer to the timeline actually shown.
    Reuse compiled unfinished-work windows; do not infer new permission to place work.
    """
    from .final_productivity_contract_patch import classify_productivity_gaps
    from .interactive_quality_speed_patch import _session_budget_remaining
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
    diagnostics['unfinished_work'] = _refresh_unfinished_windows(
        diagnostics.get('unfinished_work') or [], tasks, meta_map, segments, busy, start, horizon_days, config, blockers
    )
    rows_by_id = {str(w['task_id']): w for w in diagnostics['unfinished_work']}
    gaps = []
    for day_index in range(horizon_days):
        day = (start + timedelta(days=day_index)).date()
        lower, upper = scheduler._usable_bounds(day, config)
        lower = max(lower, start)
        for a, b in scheduler.free_windows(lower, upper, blockers) if lower < upper else []:
            minutes = int((b-a).total_seconds() // 60)
            # Slivers under 15 minutes are noise UNLESS some unfinished work actually fits.
            if minutes >= 15 or (minutes >= 1 and any(_fits(r, a, b) for r in rows_by_id.values())):
                gaps.append({'start': a.isoformat(), 'end': b.isoformat(), 'minutes': minutes,
                             'logical_day': day.isoformat()})
    diagnostics = dict(diagnostics)
    remaining_ids = {str(w['task_id']) for w in diagnostics.get('unfinished_work') or []}
    task_by_id = {str(t.id): t for t in tasks}
    for gap in gaps:
        a,b = datetime.fromisoformat(gap['start']),datetime.fromisoformat(gap['end'])
        evidence=[]
        relevant_unfinished=[]
        relevant_non_schedulable=[]
        blocked_ids=[]
        day = date.fromisoformat(gap['logical_day'])
        for task in tasks:
            raw=(meta_map or {}).get(task.id,{})
            if not task.is_actionable or not _day_relevant(task, raw, day, config):
                continue
            meta=scheduler.build_meta(task,raw)
            if str(task.id) in remaining_ids and meta.autoschedule and 'fixed' not in {x.casefold() for x in task.tags}:
                relevant_unfinished.append(str(task.id))
                if meta.earliest and meta.earliest >= b:
                    evidence.append(f"{task.title}: earliest allowed start is {meta.earliest.strftime('%a %d %b %H:%M')}.")
                elif meta.latest_end and meta.latest_end <= a:
                    evidence.append(f"{task.title}: its allowed window ended at {meta.latest_end.strftime('%a %d %b %H:%M')}.")
                elif meta.allowed_weekdays and day.weekday() not in meta.allowed_weekdays:
                    evidence.append(f"{task.title}: assigned to this day, but its weekday rule does not allow {day.strftime('%A')}.")
                allowance = _session_budget_remaining(task.id, day, segments, config)
                work = next(w for w in diagnostics['unfinished_work'] if str(w['task_id']) == str(task.id))
                required = int(work.get('remaining_minutes') if work.get('must_finish') or not work.get('splittable', True)
                               else work.get('min_session_minutes') or meta.min_chunk)
                if allowance is not None and allowance < required:
                    blocked_ids.append(str(task.id))
                    evidence.append(f"{task.title}: daily study/work allowance has {allowance} minutes left; a legal session needs {required} minutes.")
                bucket = str(meta.weekly_bucket or meta.category or '').strip().casefold()
                budgets = {str(k).strip().casefold(): int(v) for k, v in (config.get('weekly_capacity_minutes') or {}).items()}
                if bucket in budgets:
                    used = 0
                    for segment in segments:
                        other = scheduler.build_meta(segment.source_task, (meta_map or {}).get(segment.task_id, {}))
                        if (segment.start.isocalendar()[:2] == day.isocalendar()[:2]
                                and str(other.weekly_bucket or other.category or '').strip().casefold() == bucket):
                            used += int((segment.end-segment.start).total_seconds() // 60)
                    left = max(0, budgets[bucket] - used)
                    if left < required:
                        blocked_ids.append(str(task.id))
                        evidence.append(f"{task.title}: weekly {bucket} allowance has {left} minutes left; a legal session needs {required} minutes.")
                for dep_id in meta.dependencies:
                    dep = task_by_id.get(str(dep_id))
                    if not dep or not dep.is_actionable:
                        continue
                    chosen = [s for s in segments if s.task_id == dep.id]
                    dep_meta = scheduler.build_meta(dep, (meta_map or {}).get(dep.id, {}))
                    total = scheduler.duration_for(dep, dep_meta)
                    planned = sum(int((s.end-s.start).total_seconds() // 60) for s in chosen)
                    session_dep = dep.id in (raw.get('_session_dependency_ids') or [])
                    if chosen and (session_dep or (total is not None and planned >= total)):
                        finish = max(s.end for s in chosen)
                    elif (not chosen and dep.end and dep.end > start and not dep.is_all_day
                          and (not dep_meta.autoschedule or 'fixed' in {x.casefold() for x in dep.tags} or total is None)):
                        finish = dep.end
                    else:
                        evidence.append(f"{task.title}: prerequisite {dep.title} still has unfinished work.")
                        continue
                    delay = max(0, int((raw.get('_dependency_gap_minutes') or {}).get(dep.id, config.get('between_chunks_buffer', 10))))
                    ready = finish + timedelta(minutes=delay)
                    if ready >= b:
                        evidence.append(f"{task.title}: prerequisite {dep.title} and its required interval finish at {ready.strftime('%a %d %b %H:%M')}.")
                bound = (rows_by_id.get(str(task.id)) or {}).get('must_end_before')
                if bound and datetime.fromisoformat(bound['time']) < b:
                    evidence.append(f"{task.title}: must finish before {bound['dependent']} starts "
                                    f"(remaining work cannot run after {datetime.fromisoformat(bound['time']).strftime('%a %d %b %H:%M')}).")
            if not meta.autoschedule or scheduler.duration_for(task, meta) is None:
                relevant_non_schedulable.append(str(task.id))
            # Dependency-held work is intentionally removed from the optimizer's
            # unfinished list. It still exists and must not look like an empty day.
            if (raw.get('removed_prerequisites') and 'fixed' not in {x.casefold() for x in task.tags}
                    and (scheduler.duration_for(task, meta) or 0) > 0):
                evidence.append(f"{task.title}: waiting for a removed prerequisite; its dependency must be resolved first.")
        gap['relevant_unfinished_ids']=relevant_unfinished
        gap['relevant_not_schedulable_ids']=relevant_non_schedulable
        gap['blocked_unfinished_ids']=blocked_ids
        gap['constraint_details']=list(dict.fromkeys(evidence))
    diagnostics['planning_gaps'] = gaps
    return _verify_case_a(classify_productivity_gaps(diagnostics), tasks, segments, config, meta_map)


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

