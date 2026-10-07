from __future__ import annotations

"""Human triage layer above the physical reality planner.

Responsibilities:
- give flexible tasks a planning-only duration when none exists;
- respect explicit "these are the things I want today" focus language;
- move lower-value flexible work to tomorrow instead of crowding today's protected work;
- preserve fixed commitments, urgent deadlines, and prerequisites of today's focus.
"""

from copy import deepcopy
from datetime import datetime, timedelta, time

from . import scheduler as _sch
from .config import settings
from .duration_intelligence import estimate_task


_BASE_PLAN = _sch.plan


def _ctx(config: dict) -> dict:
    raw = (config or {}).get("_quick_context")
    return raw if isinstance(raw, dict) else {}


def _meta_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=settings.tz)
        return x.astimezone(settings.tz)
    except Exception:
        return None


def _tomorrow_start(start: datetime, config: dict) -> datetime:
    raw = str((config or {}).get("wake_time") or (config or {}).get("day_start") or "07:00")
    try:
        wake = time.fromisoformat(raw)
    except Exception:
        wake = time(7, 0)
    return datetime.combine(start.date() + timedelta(days=1), wake, settings.tz)


def _urgent(task, raw: dict, start: datetime) -> bool:
    # must_finish means all-or-nothing allocation, not an urgent deadline.
    if int(getattr(task, "priority", 0) or 0) >= 5:
        return True
    deadline = _meta_dt(raw.get("deadline"))
    return bool(deadline and deadline <= start + timedelta(hours=36))


def _fixed(task) -> bool:
    return "fixed" in {str(x).lower() for x in (getattr(task, "tags", []) or [])}


def _fits_today(task, metas: dict[str, dict], tasks, busy, start: datetime, config: dict) -> bool:
    """Return True only when a useful session is legally possible today.

    Focus language should change priority, not manufacture a tomorrow-only constraint.
    Reuse the mature scheduler legal-window engine instead of approximating feasibility
    from raw free minutes.
    """
    try:
        meta = _sch.build_meta(task, metas.get(task.id, {}))
        remaining = _sch._remaining_work(tasks, metas, [])
        row = next((r for r in remaining if str(r.get("task_id")) == str(task.id)), None)
        if not row:
            return False
        if meta.must_finish or not meta.splittable:
            needed = int(row.get("remaining_minutes") or 0)
        else:
            needed = int(row.get("min_session_minutes") or meta.min_chunk or 0)
        if needed <= 0:
            return False
        windows = _sch._free_task_windows(
            task, metas, tasks, busy or [], start, 1, config or {}, []
        )
        return any(
            int((upper - lower).total_seconds() // 60) >= needed
            for lower, upper in windows
        )
    except Exception:
        # If feasibility cannot be evaluated, do not manufacture a tomorrow-only
        # constraint; let the main planner decide.
        return True


def _dependency_closure(focus_ids: set[str], metas: dict[str, dict]) -> set[str]:
    keep = set(focus_ids)
    stack = list(focus_ids)
    while stack:
        tid = stack.pop()
        for dep in (metas.get(tid) or {}).get("dependencies") or []:
            dep = str(dep)
            if dep and dep not in keep:
                keep.add(dep)
                stack.append(dep)
    return keep


def _prepare_meta(tasks, meta_map: dict[str, dict], start: datetime, config: dict, busy=None) -> tuple[dict[str, dict], list[dict], list[str]]:
    metas = deepcopy(meta_map or {})
    estimates: list[dict] = []
    deferred: list[str] = []

    # 1) Planning-only duration inference. This removes the old dead-end where a task
    # with no estimate was simply unschedulable. Explicit user estimates always win.
    for task in tasks:
        if getattr(task, "status", 0) != 0 or _fixed(task):
            continue
        raw = dict(metas.get(task.id, {}))
        estimate = estimate_task(task, raw)
        if estimate:
            minutes = int(estimate["minutes"])
            raw["duration_minutes"] = minutes
            raw.setdefault("confidence", estimate["confidence"])
            raw.setdefault("category", estimate["category"])
            raw.setdefault("weekly_bucket", estimate["category"])
            raw.setdefault("energy", "high" if estimate["difficulty"] == "high" else "auto")
            raw.setdefault("splittable", minutes > 30)
            raw.setdefault("min_chunk", 25 if minutes >= 25 else minutes)
            raw.setdefault("max_chunk", min(90, max(30, minutes)))
            raw["auto_estimated_duration"] = True
            raw["auto_estimate_reason"] = estimate["reason"]
            metas[task.id] = raw
            estimates.append({"task_id": task.id, "title": task.title, **estimate})

    # 2) Explicit focus-day triage. "I want to do those two today" means protect
    # those items and their prerequisites; ordinary flexible work can move tomorrow.
    ctx = _ctx(config)
    focus_ids = {str(x) for x in (ctx.get("focus_today_ids") or []) if str(x)}
    if focus_ids:
        keep_ids = _dependency_closure(focus_ids, metas)
        tomorrow = _tomorrow_start(start, config)
        for task in tasks:
            if getattr(task, "status", 0) != 0 or _fixed(task) or task.id in keep_ids:
                continue
            raw = dict(metas.get(task.id, {}))
            if _urgent(task, raw, start):
                # A real deadline/high-priority commitment outranks casual focus language.
                continue
            # Do not defer a task that is itself a prerequisite of any urgent task.
            required_by_urgent = False
            for candidate in tasks:
                craw = metas.get(candidate.id, {})
                if _urgent(candidate, craw, start) and task.id in (craw.get("dependencies") or []):
                    required_by_urgent = True
                    break
            if required_by_urgent:
                continue
            # A focus request is not permission to throw away usable capacity.
            # Only impose tomorrow's earliest bound after proving that no useful
            # session for this task can legally fit today.
            if _fits_today(task, metas, tasks, busy, start, config):
                continue
            old = _meta_dt(raw.get("earliest"))
            if old is None or old < tomorrow:
                raw["earliest"] = tomorrow.isoformat()
            raw["timing"] = "late"
            metas[task.id] = raw
            deferred.append(task.title)

    return metas, estimates, deferred


def decision_plan(tasks, meta_map: dict[str, dict], busy, start: datetime, horizon_days: int,
                  config: dict, mastery_map: dict[str, float] | None = None):
    metas, estimates, deferred = _prepare_meta(tasks, meta_map, start, config or {}, busy=busy)
    segments, warnings, diagnostics = _BASE_PLAN(
        tasks, metas, busy, start, horizon_days, config, mastery_map or {}
    )
    diagnostics = dict(diagnostics or {})
    diagnostics["decision_intelligence"] = True
    diagnostics["auto_estimates"] = estimates
    diagnostics["focus_deferred"] = list(dict.fromkeys(deferred))

    warnings = list(warnings or [])
    if estimates:
        # One concise explanation rather than a warning for every previously-undated task.
        preview = ", ".join(f"{x['title']} ≈ {x['minutes']}m" for x in estimates[:5])
        warnings.append(
            "Used planning-only difficulty estimates for tasks without durations: "
            + preview + ("…" if len(estimates) > 5 else "")
            + ". Review them in Smart Review if you want to save the estimates."
        )
    if deferred:
        warnings.append(
            "Moved non-urgent flexible work out of today's explicit focus: "
            + ", ".join(list(dict.fromkeys(deferred))[:8])
            + ("…" if len(set(deferred)) > 8 else "")
        )
    return segments, list(dict.fromkeys(warnings)), diagnostics


_sch.plan = decision_plan

__all__ = ["decision_plan", "_prepare_meta", "_BASE_PLAN"]
