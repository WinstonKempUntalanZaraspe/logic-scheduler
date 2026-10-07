"""Daily session-budget helpers needed by the core scheduler.

This logic-lab adapter preserves production budget/chunk semantics without importing
the production web/runtime day layer.
"""
from __future__ import annotations
from datetime import date

def session_budgets(config):
    result = []
    ctx = (config or {}).get("_quick_context") or {}
    requests = list(ctx.get("plan_local_duration_requests") or []) + list((config or {}).get("_project_daily_budgets") or [])
    for request in requests:
        try:
            day = date.fromisoformat(request["date"])
            minutes = int(request["minutes"])
            ids = frozenset(str(tid) for tid in request["task_ids"])
        except (KeyError, TypeError, ValueError):
            continue
        if ids and 0 <= minutes <= 1440:
            result.append((ids, minutes, day))
    return result

def requested_chunks(task, total, meta, config, choose_chunks):
    goal = (((config or {}).get("_quick_context") or {}).get("intent_date_goals") or {}).get(task.id)
    limits = [minutes for ids, minutes, day in session_budgets(config)
              if task.id in ids and day.isoformat() == goal]
    limits += [int(r["minutes"]) for r in (config or {}).get("_project_daily_budgets", [])
               if task.id in r.get("task_ids", []) and int(r.get("minutes", 0)) > 0]
    if not limits or not meta.splittable or meta.must_finish:
        return choose_chunks(total, meta)
    budget = min(limits)
    if budget < meta.min_chunk:
        return choose_chunks(total, meta)
    shared_project = any(task.id in r.get("task_ids", []) and len(r.get("task_ids", [])) > 1
                         for r in (config or {}).get("_project_daily_budgets", []))
    if shared_project:
        allowance = min(total, budget)
        if allowance < meta.min_chunk:
            return choose_chunks(total, meta)
        count, tail = divmod(allowance, meta.min_chunk)
        prefix = [meta.min_chunk] * max(0, count - 1)
        prefix += choose_chunks(meta.min_chunk + tail, meta)
        return prefix + (choose_chunks(total - allowance, meta) if total > allowance else [])
    if budget >= total:
        return choose_chunks(total, meta)
    return choose_chunks(budget, meta) + choose_chunks(total - budget, meta)

def budget_intervals(config):
    from . import scheduler
    result = []
    for ids, minutes, day in session_budgets(config):
        cfg = dict(config) | scheduler._override_for_day(day, config)
        start, end = scheduler._usable_bounds(day, cfg)
        result.append((ids, minutes, start, end))
    return result
