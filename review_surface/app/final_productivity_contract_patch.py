from __future__ import annotations

"""Absolute final productivity contract for interactive day plans.

The mature scheduler remains authoritative for hard reality. This layer only closes
three contradictions that are unacceptable in a forward-looking plan:

1. A short unfinished task may not be stranded merely because the generic default
   minimum chunk is longer than the task's entire remaining duration.
2. For an explicit live ``from now/right now`` replan, the first flexible work block
   is pulled to the exact executable cutoff (submission time + 2 minutes) when the same
   block is already legal there.
3. Every displayed idle interval is classified explicitly:
      Case A = eligible unfinished work fits -> this is capacity the planner should use.
      CONSTRAINED = unfinished work exists but cannot use this interval.
      UNALLOCATED = free capacity without a verified task-specific restriction.

Nothing here relaxes fixed commitments, Google Calendar busy time, dependencies,
recovery, travel, meals, sleep/wind-down, explicit duration budgets, weekly caps,
or user-authored hard timing constraints.
"""

from datetime import datetime, timedelta

_INSTALLED = False


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def install_final_productivity_fill_contract() -> None:
    """Patch the already-safe last-mile filler without replacing hard planner logic."""
    global _INSTALLED
    if _INSTALLED:
        return

    from . import scheduler
    from . import interactive_quality_speed_patch as quality

    base_fill = quality._productive_fill
    if getattr(base_fill, "_final_productivity_contract", False):
        _INSTALLED = True
        return

    def contract_fill(_scheduler, tasks, meta_map, busy, start, horizon_days, config, mastery_map, result):
        before_segments = list((result or ([], [], {}))[0] or [])
        adjusted = {task_id: dict(raw or {}) for task_id, raw in (meta_map or {}).items()}
        task_by_id = {task.id: task for task in tasks}

        # `_remaining_work` already defines the truthful useful minimum as
        # min(remaining, configured min_chunk). Mirror that contract in the actual filler.
        # This rescues genuine 5/10/15/20-minute tasks and final remainders without
        # changing the source TickTick task or any hard duration budget.
        for row in scheduler._remaining_work(tasks, meta_map, before_segments):
            task_id = str(row.get("task_id") or "")
            task = task_by_id.get(task_id)
            if not task:
                continue
            remaining = max(0, int(row.get("remaining_minutes") or 0))
            useful_min = max(1, int(row.get("min_session_minutes") or remaining or 1))
            current = int(scheduler.build_meta(task, adjusted.get(task_id, {})).min_chunk)
            if remaining and useful_min < current:
                raw = adjusted.setdefault(task_id, {})
                raw["min_chunk"] = useful_min
                raw["_final_remainder_min_chunk"] = useful_min

        segments, warnings, diagnostics = base_fill(
            _scheduler, tasks, adjusted, busy, start, horizon_days, config,
            mastery_map or {}, result,
        )
        segments = list(segments or [])
        diagnostics = dict(diagnostics or {})

        # Live-now means executable from the actual submission clock, not from the next
        # aesthetically convenient 10/15-minute anchor. Move only the first flexible work
        # block and only when the mature legal-window engine proves that exact cutoff legal.
        ctx = ((config or {}).get("_quick_context") or {})
        cutoff = _dt(ctx.get("replan_from"))
        if (
            cutoff
            and ctx.get("replan_requested")
            and ctx.get("live_submission_cutoff")
            and str(ctx.get("date") or "") == cutoff.date().isoformat()
        ):
            movable = []
            for segment in segments:
                tags = {str(tag).strip().casefold() for tag in (segment.source_task.tags or [])}
                if "fixed" in tags or "autoscheduler-session" in tags or segment.end <= cutoff:
                    continue
                movable.append(segment)
            movable.sort(key=lambda segment: segment.start)
            if movable:
                first = movable[0]
                duration = first.end - first.start
                if first.start > cutoff and duration > timedelta(0):
                    others = [segment for segment in segments if segment is not first]
                    task = task_by_id.get(first.task_id)
                    if task is not None:
                        windows = scheduler._free_task_windows(
                            task, adjusted, tasks, busy, start, horizon_days, config, others
                        )
                        exact_legal = any(
                            lower <= cutoff and cutoff + duration <= upper
                            for lower, upper in windows
                        )
                        if exact_legal:
                            old_start = first.start
                            first.start = cutoff
                            first.end = cutoff + duration
                            first.reason = (
                                str(first.reason or "")
                                + " · Live-now front-load: first legal work starts at submission +2 minutes"
                            ).strip(" ·")
                            segments.sort(key=lambda segment: segment.start)
                            diagnostics["live_now_frontload"] = {
                                "applied": True,
                                "task_id": first.task_id,
                                "title": first.title,
                                "old_start": old_start.isoformat(),
                                "new_start": cutoff.isoformat(),
                            }
                        else:
                            diagnostics["live_now_frontload"] = {
                                "applied": False,
                                "task_id": first.task_id,
                                "title": first.title,
                                "requested_start": cutoff.isoformat(),
                                "reason": "The first task is not legally executable at the live cutoff.",
                            }

        # Recompute remaining-work truth after the rescue/front-load using the adjusted
        # plan-local minima so diagnostics and visible gaps agree with what can run.
        diagnostics["unfinished_work"] = scheduler._remaining_work(tasks, adjusted, segments)
        for row in diagnostics["unfinished_work"]:
            task = task_by_id.get(str(row.get("task_id") or ""))
            if not task:
                row["legal_windows"] = []
                continue
            row["legal_windows"] = [
                {"start": lower.isoformat(), "end": upper.isoformat()}
                for lower, upper in scheduler._free_task_windows(
                    task, adjusted, tasks, busy, start, horizon_days, config, segments
                )
            ]
        diagnostics["final_productivity_contract"] = {
            "active": True,
            "short_remainder_minimums": {
                task_id: raw.get("_final_remainder_min_chunk")
                for task_id, raw in adjusted.items()
                if raw.get("_final_remainder_min_chunk") is not None
            },
        }
        return segments, warnings, diagnostics

    contract_fill._final_productivity_contract = True
    quality._productive_fill = contract_fill
    _INSTALLED = True


def classify_productivity_gaps(diagnostics: dict) -> dict:
    """Classify every displayed gap using the final user-facing productivity contract.

    Case A remains an actionable planner miss. If unfinished work exists but cannot legally
    occupy the exact interval, the user sees a constraint-specific message. Unallocated means free capacity; global unscheduled work alone does not prove
    that a specific gap is constrained.
    """
    diagnostics = dict(diagnostics or {})
    unfinished = list(diagnostics.get("unfinished_work") or [])
    case_a_minutes = 0
    constrained_minutes = 0
    unallocated_minutes = 0

    for gap in diagnostics.get("planning_gaps") or []:
        gap_start = _dt(gap.get("start"))
        gap_end = _dt(gap.get("end"))
        if not gap_start or not gap_end or gap_end <= gap_start:
            continue
        minutes = max(0, int(gap.get("minutes") or (gap_end - gap_start).total_seconds() // 60))
        original_reason = str(gap.get("reason") or "").strip()

        # A remaining task in the *overall horizon* is not necessarily relevant to
        # this date. A Saturday-only task must never classify Friday as constrained.
        # When rebuilding final gaps, the real planner passes date-scoped IDs; direct
        # legacy callers without the field retain the previous all-work semantics.
        relevant_ids = gap.get("relevant_unfinished_ids")
        scoped_unfinished = (
            [work for work in unfinished if str(work.get("task_id")) in set(map(str, relevant_ids))]
            if relevant_ids is not None else unfinished
        )

        fitting = []
        for work in scoped_unfinished:
            needed = int(
                work.get("remaining_minutes")
                if work.get("must_finish") or not work.get("splittable", True)
                else work.get("min_session_minutes") or 0
            )
            for window in work.get("legal_windows") or []:
                lower_raw = _dt(window.get("start"))
                upper_raw = _dt(window.get("end"))
                if not lower_raw or not upper_raw:
                    continue
                lower = max(gap_start, lower_raw)
                upper = min(gap_end, upper_raw)
                if lower < upper and int((upper - lower).total_seconds() // 60) >= needed:
                    fitting.append(str(work.get("title") or work.get("task_id") or "Task"))
                    break

        if fitting:
            gap["productivity_case"] = "A"
            gap["kind"] = "eligible-work"
            gap["fitting_task_titles"] = list(dict.fromkeys(fitting))
            case_text = (
                "CASE A · Eligible unfinished work fits here: "
                + ", ".join(gap["fitting_task_titles"][:3])
                + ". This capacity should be used before the planner leaves it idle."
            )
            gap["reason"] = f"{original_reason} {case_text}".strip() if original_reason else case_text
            case_a_minutes += minutes
            continue

        # No eligible session fits this exact gap. If unfinished work exists,
        # explain *why* it cannot use the interval. UNALLOCATED is reserved for
        # genuinely open capacity after all schedulable work is allocated (or no
        # unfinished work exists), not for work that silently failed constraints.
        gap["fitting_task_titles"] = []
        restrictions = list(gap.get("constraint_details") or [])
        for work in scoped_unfinished:
            title = str(work.get("title") or work.get("task_id") or "Task")
            windows = [(_dt(w.get("start")), _dt(w.get("end"))) for w in work.get("legal_windows") or []]
            windows = [(a, b) for a, b in windows if a and b and b > a]
            needed = int(
                work.get("remaining_minutes")
                if work.get("must_finish") or not work.get("splittable", True)
                else work.get("min_session_minutes") or 0
            )
            if needed > minutes:
                restrictions.append(
                    f"{title}: needs at least {needed} minutes for a legal session; this gap has {minutes} minutes."
                )
                continue
            overlaps = [
                max(0, int((min(b, gap_end) - max(a, gap_start)).total_seconds() // 60))
                for a, b in windows if a < gap_end and b > gap_start
            ]
            if windows and not overlaps:
                next_window = next(((a, b) for a, b in sorted(windows) if a >= gap_end), None)
                if next_window:
                    a, b = next_window
                    restrictions.append(f"{title}: next legal window {a.strftime('%a %d %b %H:%M')}–{b.strftime('%H:%M')}.")
                else:
                    restrictions.append(f"{title}: its legal windows do not overlap this interval.")
            elif overlaps and max(overlaps) < needed:
                restrictions.append(
                    f"{title}: only {max(overlaps)} legal minutes overlap this gap, but at least {needed} are required."
                )
            elif not windows:
                restrictions.append(
                    f"{title}: no legal window remains here after timing, prerequisite, recovery, travel, meal, fixed-time, or sleep rules."
                )

        # Active work with no schedulable representation must also be explained.
        # Fixed commitments and zero-remaining items are not stranded flexible work,
        # so they do not turn otherwise-free capacity into a false constraint.
        task_state = diagnostics.get("task_state") or {}
        if not scoped_unfinished and task_state.get("state") == "ACTIVE_BUT_NOT_SCHEDULABLE":
            reason_labels = {
                "autoschedule_off": "Auto-schedule is turned off",
                "no_duration": "no usable duration/remaining-work estimate is available",
            }
            relevant_state_ids = gap.get("relevant_not_schedulable_ids")
            relevant_state_ids = (
                set(map(str, relevant_state_ids)) if relevant_state_ids is not None else None
            )
            for item in task_state.get("not_schedulable") or []:
                if relevant_state_ids is not None and str(item.get("task_id")) not in relevant_state_ids:
                    continue
                reason = reason_labels.get(str(item.get("reason") or ""))
                if reason:
                    restrictions.append(f"{item.get('title') or 'Task'}: {reason}.")

        gap["constraint_details"] = list(dict.fromkeys(restrictions))
        if gap["constraint_details"]:
            gap["productivity_case"] = "CONSTRAINED"
            gap["kind"] = "constrained-work"
            gap["reason"] = (
                "Cannot fit unfinished work here due to constraints. "
                + " ".join(gap["constraint_details"][:3])
            )
            constrained_minutes += minutes
        else:
            gap["productivity_case"] = "UNALLOCATED"
            gap["kind"] = "unallocated-time"
            gap["reason"] = "Unallocated time — no unfinished schedulable work is blocked from this interval; it is free to use or keep open."
            unallocated_minutes += minutes

    diagnostics["productivity_cases"] = {
        "case_a_idle_minutes": case_a_minutes,
        "constrained_minutes": constrained_minutes,
        "constrained_count": sum(
            1 for gap in diagnostics.get("planning_gaps") or []
            if gap.get("productivity_case") == "CONSTRAINED"
        ),
        # Deprecated compatibility counter retained for older diagnostics consumers.
        "case_b_open_minutes": constrained_minutes,
        "case_a_count": sum(
            1 for gap in diagnostics.get("planning_gaps") or []
            if gap.get("productivity_case") == "A"
        ),
        "case_b_count": sum(
            1 for gap in diagnostics.get("planning_gaps") or []
            if gap.get("productivity_case") == "CONSTRAINED"
        ),
        "unallocated_minutes": unallocated_minutes,
        "unallocated_count": sum(
            1 for gap in diagnostics.get("planning_gaps") or []
            if gap.get("productivity_case") == "UNALLOCATED"
        ),
    }
    return diagnostics


__all__ = ["install_final_productivity_fill_contract", "classify_productivity_gaps"]

