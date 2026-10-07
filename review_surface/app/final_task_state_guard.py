from __future__ import annotations

"""Final task-state + gap-fill guard.

This is deliberately deterministic. TickTick is the source of truth:
- status 0 = active/unfinished
- status -1 = abandoned / "Won't Do"
- status 2 = completed

The planner must never use an abandoned task as work, and it must distinguish:
1. no active tasks at all;
2. active tasks exist but are not schedulable (no duration, autoschedule off, fixed, etc.);
3. active unfinished work exists and should be packed into legal capacity;
4. all schedulable unfinished work is already allocated.
"""

from datetime import datetime, timedelta

from .models import Task
from .config import settings

ABANDONED_STATUS = -1
ACTIVE_STATUS = 0
COMPLETED_STATUS = 2

_INSTALLED = False


def _project_command_artifact(task) -> bool:
    tags = {str(x).strip().casefold() for x in (getattr(task, "tags", None) or [])}
    if "project-intelligence" in tags or "project-intelligence-virtual" in tags:
        return False
    try:
        from .project_intelligence_models import looks_like_project_blueprint_request
        return bool(looks_like_project_blueprint_request(str(getattr(task, "title", "") or "")))
    except Exception:
        return False


def _active_source_tasks(tasks):
    return [
        t for t in (tasks or [])
        if t.is_actionable
        and int(t.status) == ACTIVE_STATUS
        and not _project_command_artifact(t)
        and "autoscheduler-session" not in {str(x).strip().casefold() for x in (t.tags or [])}
    ]


def _task_state(tasks, meta_map, segments, scheduler):
    active = _active_source_tasks(tasks)
    scheduled_ids = {str(s.task_id) for s in (segments or [])}

    schedulable = []
    not_schedulable = []
    for task in active:
        tags = {str(x).strip().casefold() for x in (task.tags or [])}
        raw = dict((meta_map or {}).get(task.id) or {})
        meta = scheduler.build_meta(task, raw)

        if "fixed" in tags:
            not_schedulable.append({"task_id": task.id, "title": task.title, "reason": "fixed"})
            continue
        if not meta.autoschedule:
            not_schedulable.append({"task_id": task.id, "title": task.title, "reason": "autoschedule_off"})
            continue
        duration = scheduler.duration_for(task, meta)
        if duration is None and str(task.id) not in scheduled_ids:
            not_schedulable.append({"task_id": task.id, "title": task.title, "reason": "no_duration"})
            continue
        if duration is not None and duration <= 0:
            not_schedulable.append({"task_id": task.id, "title": task.title, "reason": "zero_remaining_work"})
            continue
        schedulable.append(task)

    unfinished = scheduler._remaining_work(tasks or [], meta_map or {}, segments or [])
    unfinished_ids = {str(row["task_id"]) for row in unfinished}

    if not active:
        state = "NO_ACTIVE_TASKS"
        message = "No active TickTick tasks exist. This is genuinely empty capacity; do not invent work."
    elif unfinished:
        state = "UNFINISHED_WORK"
        message = "Active unfinished work exists and was considered for legal placement."
    elif schedulable and scheduled_ids.intersection({t.id for t in schedulable}):
        state = "ALL_SCHEDULABLE_WORK_ALLOCATED"
        message = "All schedulable unfinished work is already allocated; remaining space is open capacity."
    else:
        state = "ACTIVE_BUT_NOT_SCHEDULABLE"
        message = "Active TickTick tasks exist, but none currently has a legal schedulable work block."

    return {
        "state": state,
        "message": message,
        "active_task_count": len(active),
        "schedulable_task_count": len(schedulable),
        "unfinished_work_count": len(unfinished),
        "not_schedulable": not_schedulable,
        "unfinished_task_ids": sorted(unfinished_ids),
    }


def _minutes_text(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h" + (f" {remainder}m" if remainder else "")


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _local_date(value):
    parsed = _dt(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.tz)
    return parsed.astimezone(settings.tz).date()


def _planning_scope(start, horizon_days, config):
    """Return the calendar dates this request is actually allowed to plan.

    A stale multi-day minimum must never make a fresh "today" replan pull tomorrow's
    work/events into today. Explicit today/tomorrow/date scopes beat the numeric
    horizon; otherwise the selected horizon remains authoritative.
    """
    ctx = (config or {}).get("_quick_context") or {}
    scope = str(ctx.get("replan_scope") or "").strip().lower()
    today = start.astimezone(settings.tz).date() if start.tzinfo else start.replace(tzinfo=settings.tz).date()
    if scope in {"today", "morning"} and (int(horizon_days or 1) <= 1 or bool(ctx.get("explicit_today_scope"))):
        return today, today, 1
    if scope == "tomorrow":
        target = today + timedelta(days=1)
        return target, target, max(2, int(horizon_days or 1))
    try:
        target = datetime.fromisoformat(scope).date() if scope else None
    except ValueError:
        target = None
    if target:
        return target, target, max(1, (target - today).days + 1)
    days = max(1, int(horizon_days or 1))
    return today, today + timedelta(days=days - 1), days


def _task_plan_date(task, config, start):
    """Explicit plan-local date goals override the source task's current date."""
    ctx = (config or {}).get("_quick_context") or {}
    tid = str(task.id)
    planning_day = start.astimezone(settings.tz).date() if start.tzinfo else start.replace(tzinfo=settings.tz).date()
    context_is_current = str(ctx.get("date") or "") == planning_day.isoformat()
    goals = (ctx.get("intent_date_goals") or {}) if context_is_current else {}
    raw_goal = goals.get(tid)
    if raw_goal:
        try:
            return datetime.fromisoformat(str(raw_goal)).date()
        except ValueError:
            try:
                return datetime.strptime(str(raw_goal), "%Y-%m-%d").date()
            except ValueError:
                pass
    if context_is_current and tid in {str(x) for x in (ctx.get("intent_today_ids") or [])}:
        return planning_day
    return _local_date(getattr(task, "start", None))


def _apply_calendar_date_boundary(tasks, meta_map, start, horizon_days, config, scheduler):
    """Keep existing dated work on its calendar date unless the user explicitly moves it."""
    scope_start, scope_end, effective_horizon = _planning_scope(start, horizon_days, config)
    scoped_meta = {str(k): dict(v or {}) for k, v in (meta_map or {}).items()}
    kept = []
    excluded = []

    for task in tasks or []:
        target_day = _task_plan_date(task, config, start)
        if target_day and not (scope_start <= target_day <= scope_end):
            excluded.append({
                "task_id": str(task.id),
                "title": task.title,
                "date": target_day.isoformat(),
                "reason": f"dated {target_day.isoformat()}, outside requested planning scope {scope_start.isoformat()}",
            })
            continue

        kept.append(task)
        tags = {str(x).strip().casefold() for x in (task.tags or [])}
        if not target_day or "fixed" in tags or task.is_all_day:
            continue

        # A flexible timed task can move within its assigned date, but never backward
        # into an earlier calendar day. Explicit intent_date_goals can reassign the day.
        day_start, day_end = scheduler._usable_bounds(target_day, config)
        raw = scoped_meta.setdefault(str(task.id), dict((meta_map or {}).get(task.id) or {}))
        current_earliest = _dt(raw.get("earliest"))
        current_latest = _dt(raw.get("latest_end"))
        if current_earliest is None or current_earliest < day_start:
            raw["earliest"] = day_start.isoformat()
        if current_latest is None or current_latest > day_end:
            raw["latest_end"] = day_end.isoformat()

    return kept, scoped_meta, excluded, effective_horizon, scope_start, scope_end


def _unscheduled_activity_conflicts(tasks, meta_map, busy, start, horizon_days, config, segments, diagnostics, scheduler):
    """Explain only *proven* infeasibility for active work omitted from the plan.

    This is intentionally conservative: an activity is called constraint-blocked only
    when its minimum atomic requirement is larger than every legal continuous opening.
    A low-priority task that merely lost an optimizer trade-off is not mislabeled.
    """
    active = {str(t.id): t for t in _active_source_tasks(tasks)}
    scheduled = {str(s.task_id) for s in (segments or [])}
    unfinished = {
        str(row.get("task_id")): row
        for row in (diagnostics.get("unfinished_work") or [])
        if str(row.get("task_id") or "") in active
    }
    logistics_by_task = {}
    for item in diagnostics.get("logistics_estimates") or []:
        tid = str(item.get("task_id") or "")
        minutes = item.get("minutes")
        if tid and minutes is not None:
            try:
                logistics_by_task[tid] = logistics_by_task.get(tid, 0) + max(0, int(minutes))
            except (TypeError, ValueError):
                pass

    availability_by_task = {
        str(item.get("task_id")): item
        for item in diagnostics.get("venue_availability_constraints") or []
        if item.get("task_id") is not None
    }
    arrival_by_task = {
        str(item.get("task_id")): item
        for item in diagnostics.get("venue_arrival_constraints") or []
        if item.get("task_id") is not None
    }

    hard_busy = list(scheduler._hard_busy(tasks, busy, start, horizon_days, config))
    hard_busy.extend(
        scheduler.BusyBlock(s.start, s.end, s.title, "planned")
        for s in (segments or [])
    )
    wind_down = max(0, int(config.get("bedtime_wind_down_minutes") or 0))
    conflicts = []

    for tid, row in unfinished.items():
        if tid in scheduled:
            continue
        task = active.get(tid)
        if not task:
            continue
        raw = dict((meta_map or {}).get(task.id) or {})
        meta = scheduler.build_meta(task, raw)
        # For outings, unfinished_work may reflect planner-expanded bundle geometry.
        # The user-facing "requested activity" must come from the source task's raw
        # activity estimate/remaining effort, otherwise logistics can be counted twice.
        raw_requested = raw.get("remaining_minutes")
        if raw_requested is None:
            raw_requested = raw.get("duration_minutes")
        if raw_requested is None:
            raw_requested = task.duration_minutes
        requested = int(raw_requested if raw_requested is not None else (row.get("remaining_minutes") or row.get("estimated_minutes") or 0))
        if requested <= 0:
            continue

        logistics = int(logistics_by_task.get(tid, 0))
        is_outing = logistics > 0 or tid in availability_by_task or tid in arrival_by_task
        # Outing phases are one contiguous real-world chain even though the scheduler
        # represents activity and logistics as separate internal parts.
        required = requested + logistics if is_outing else (
            requested if meta.must_finish or not meta.splittable
            else int(row.get("min_session_minutes") or meta.min_chunk or requested)
        )

        lower = max(start, meta.earliest or start)
        day = lower.date()
        _, awake_end = scheduler._usable_bounds(day, config)
        sleep_cutoff = awake_end - timedelta(minutes=wind_down)
        upper = sleep_cutoff

        availability = availability_by_task.get(tid) or {}
        venue_open = _dt(availability.get("bundle_earliest"))
        venue_end = _dt(availability.get("bundle_latest_end"))
        if venue_open:
            lower = max(lower, venue_open)
        if venue_end:
            upper = min(upper, venue_end)

        arrival = arrival_by_task.get(tid) or {}
        arrival_end = _dt(arrival.get("bundle_latest_end"))
        if arrival_end:
            upper = min(upper, arrival_end)

        if meta.latest_end:
            upper = min(upper, meta.latest_end)
        if meta.hard_stop:
            upper = min(upper, meta.hard_stop)

        windows = scheduler.free_windows(lower, upper, hard_busy) if upper > lower else []
        largest = max(
            [int((b - a).total_seconds() // 60) for a, b in windows]
            or [0]
        )

        # Prefer already-compiled legal windows when they prove an even tighter
        # task-specific restriction (dependencies, weekdays, recovery, budgets).
        legal = []
        for item in row.get("legal_windows") or []:
            a, b = _dt(item.get("start")), _dt(item.get("end"))
            if a and b and b > a:
                legal.append(int((b - a).total_seconds() // 60))
        if legal and not is_outing:
            largest = min(largest, max(legal)) if largest else max(legal)

        if largest >= required:
            continue

        reasons = []
        closing = _dt(availability.get("closes"))
        opening = _dt(availability.get("opens"))
        if closing:
            reasons.append(f"{availability.get('subject') or 'venue'} closes at {closing:%H:%M}")
        if opening and opening > start:
            reasons.append(f"{availability.get('subject') or 'venue'} opens at {opening:%H:%M}")
        arrive_by = _dt(arrival.get("arrive_by"))
        if arrive_by:
            reasons.append(f"you must arrive by {arrive_by:%H:%M}")
        if sleep_cutoff > start and upper == sleep_cutoff:
            if wind_down:
                reasons.append(f"protected wind-down starts at {sleep_cutoff:%H:%M} before sleep at {awake_end:%H:%M}")
            else:
                reasons.append(f"protected sleep starts at {awake_end:%H:%M}")
        elif awake_end > start:
            # Even when another bound is tighter, sleep is still useful context when
            # the remaining day is short.
            minutes_to_sleep = int((sleep_cutoff - start).total_seconds() // 60)
            if 0 <= minutes_to_sleep < required:
                reasons.append(
                    f"only {_minutes_text(minutes_to_sleep)} remains before "
                    + (f"wind-down at {sleep_cutoff:%H:%M}" if wind_down else f"sleep at {awake_end:%H:%M}")
                )

        if not reasons:
            overlapping = [
                block.label for block in hard_busy
                if lower < block.end and block.start < upper
                and block.label not in {"Protected sleep", "Wind down before sleep"}
            ]
            if overlapping:
                reasons.append("fixed/protected time blocks the window: " + ", ".join(list(dict.fromkeys(overlapping))[:2]))
            elif not legal:
                reasons.append("no legal continuous window remains after timing, dependency, recovery or fixed-time rules")

        if is_outing:
            requirement = (
                f"requested {requested} min of activity; the total outing needs {required} min "
                f"including {logistics} min of travel/change/recovery"
            )
        else:
            requirement = (
                f"needs {required} min continuously"
                if required == requested
                else f"needs at least {required} min for a legal session ({requested} min remains)"
            )
        reason_text = "; ".join(reasons[:3])
        message = (
            f"Constraints don't allow {task.title} in this schedule. "
            f"Available legal continuous time: {_minutes_text(largest)}; {requirement}. "
            f"{reason_text.capitalize()}. "
            "The activity was left unscheduled and its requested duration was not shortened."
        )
        conflicts.append({
            "task_id": tid,
            "title": task.title,
            "requested_minutes": requested,
            "logistics_minutes": logistics,
            "required_continuous_minutes": required,
            "largest_legal_window_minutes": largest,
            "reasons": reasons,
            "message": message,
        })
    return conflicts


def _largest_legal_gap(tasks, meta_map, busy, start, horizon_days, config, segments, scheduler):
    hard = scheduler._hard_busy(tasks, busy, start, horizon_days, config)
    best = None
    last_day = (start + timedelta(days=max(0, horizon_days - 1))).date()
    for dd in range(horizon_days):
        day = (start + timedelta(days=dd)).date()
        lower, upper = scheduler._usable_bounds(day, config)
        lower = max(lower, start) if dd == 0 else lower
        for a, b in scheduler.free_windows(lower, upper, hard + [
            scheduler.BusyBlock(s.start, s.end, s.title, "planned") for s in (segments or [])
        ]):
            minutes = int((b - a).total_seconds() // 60)
            if best is None or minutes > best["minutes"]:
                best = {"start": a, "end": b, "minutes": minutes}
    return best


def install_final_task_state_guard(service_module):
    """Install the final planner boundary after all existing wrappers are installed."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import scheduler

    base_plan = service_module.plan

    def guarded_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        # Never let a stale/incorrect upstream wrapper hand abandoned tasks into the
        # final optimizer. Active status is the only schedulable TickTick state.
        command_artifacts = [
            t for t in (tasks or [])
            if t.is_actionable and _project_command_artifact(t)
        ]
        lifecycle_tasks = [
            t for t in (tasks or [])
            if t.is_actionable and not t.is_expired(start) and not _project_command_artifact(t)
        ]
        clean_tasks, scoped_meta_map, future_scope_excluded, effective_horizon_days, scope_start, scope_end = _apply_calendar_date_boundary(
            lifecycle_tasks, meta_map, start, horizon_days, config, scheduler
        )

        result = base_plan(
            clean_tasks, scoped_meta_map, busy, start, effective_horizon_days, config, mastery_map or {}
        )
        segments, warnings, diagnostics = result
        diagnostics = dict(diagnostics or {})

        # Productive filling already runs inside the solver with compiled real-life
        # constraints. Never fill again here using the original, uncompiled metadata:
        # that resurrected held dependents and optional work, duplicated outings, and
        # inserted work AFTER the final overlap/reality validation boundary.

        diagnostics["task_state"] = _task_state(
            clean_tasks, scoped_meta_map, segments, scheduler
        )
        diagnostics["calendar_scope"] = {
            "start": scope_start.isoformat(),
            "end": scope_end.isoformat(),
            "effective_horizon_days": effective_horizon_days,
        }
        diagnostics["future_dated_tasks_excluded"] = future_scope_excluded
        diagnostics["abandoned_task_ids_excluded"] = sorted(
            str(t.id) for t in (tasks or []) if int(t.status) == ABANDONED_STATUS
        )
        diagnostics["completed_task_ids_excluded"] = sorted(
            str(t.id) for t in (tasks or []) if int(t.status) == COMPLETED_STATUS
        )
        diagnostics["project_command_artifacts_excluded"] = [
            {"task_id": str(t.id), "title": t.title, "reason": "Project Intelligence command text is not executable study work."}
            for t in command_artifacts
        ]
        if command_artifacts:
            warnings = [
                *(warnings or []),
                "Ignored an accidental Project Intelligence command-task artifact; it was not scheduled or deleted. Remove it from TickTick manually if it was created by an earlier build."
            ]

        # Explicitly distinguish "no work exists" from "work exists but cannot fit".
        state = diagnostics["task_state"]["state"]
        if state == "NO_ACTIVE_TASKS":
            diagnostics["planning_empty_reason"] = "no_active_ticktick_tasks"
        elif state == "ACTIVE_BUT_NOT_SCHEDULABLE":
            diagnostics["planning_empty_reason"] = "active_tasks_not_schedulable"
        elif state == "UNFINISHED_WORK":
            diagnostics["planning_empty_reason"] = None
        else:
            diagnostics["planning_empty_reason"] = "all_schedulable_work_allocated"

        conflicts = _unscheduled_activity_conflicts(
            clean_tasks, scoped_meta_map, busy, start, effective_horizon_days, config,
            segments or [], diagnostics, scheduler
        )
        diagnostics["unscheduled_activity_conflicts"] = conflicts
        if conflicts:
            warnings = [
                *(warnings or []),
                *(item["message"] for item in conflicts),
            ]

        from .planning_gaps import rebuild_final_gaps
        diagnostics = rebuild_final_gaps(
            segments or [], diagnostics, clean_tasks, busy, start, effective_horizon_days, config, scoped_meta_map
        )
        # rebuild_final_gaps preserves arbitrary diagnostics keys, including the
        # structured conflict cards above.
        return list(segments or []), list(dict.fromkeys(warnings or [])), diagnostics

    guarded_plan._final_task_state_guard = True
    service_module.plan = guarded_plan

    # Commit-time protection: a task can be marked Won't Do after preview. Re-read
    # TickTick immediately before applying so an old preview can never reschedule it.
    base_commit = service_module.commit_plan

    async def guarded_commit(payload):
        tt = service_module.TickTickClient()
        from .runtime_resilience_patch import REQUIRE_FRESH_TASKS
        from .performance_patch import invalidate_ticktick_cache
        invalidate_ticktick_cache(projects=False)
        token = REQUIRE_FRESH_TASKS.set(True)
        try:
            current_tasks, _ = await tt.all_active_items()
        finally:
            REQUIRE_FRESH_TASKS.reset(token)
        current_by_id = {str(t.id): t for t in current_tasks if int(t.status) == ACTIVE_STATUS}

        blocked = []
        kept_segments = []
        for seg in payload.get("segments") or []:
            tid = str(seg.get("task_id") or "")
            if tid and tid not in current_by_id:
                blocked.append(tid)
            else:
                kept_segments.append(seg)

        if blocked:
            payload = dict(payload)
            payload["segments"] = kept_segments
            diagnostics = dict(payload.get("diagnostics") or {})
            diagnostics["commit_time_excluded_task_ids"] = sorted(set(blocked))
            diagnostics["commit_time_exclusion_reason"] = "Task is no longer active in TickTick; it may have been marked Won't Do/completed/deleted after preview."
            payload["diagnostics"] = diagnostics

        return await base_commit(payload)

    guarded_commit._final_task_state_guard = True
    service_module.commit_plan = guarded_commit
