from __future__ import annotations

"""Last-mile task semantics for human-facing schedules.

A scheduler that ignores obvious meaning in a task title is not ready for a real day.
This layer gives *plan-local* timing bounds to clearly temporal task names (morning
review, night review, wake up, before bed, etc.) without rewriting TickTick metadata.
It also stops the duration estimator from assigning fake work time to TickTick NOTE
items before the lower scheduler gets a chance to ignore them.

Explicit user metadata always wins. Fixed items and generated AutoScheduler sessions
are never retimed here.
"""

import re
from copy import deepcopy
from datetime import datetime, timedelta, time

from .config import settings
from . import decision_patch
from . import scheduler


_ORIGINAL_ESTIMATE = decision_patch.estimate_task


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _tags(task) -> set[str]:
    return {_norm(x) for x in (getattr(task, "tags", None) or [])}


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        out = value
    else:
        try:
            out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=settings.tz)
    return out.astimezone(settings.tz)


def _clock(value, fallback: str) -> time:
    try:
        return time.fromisoformat(str(value or fallback))
    except Exception:
        return time.fromisoformat(fallback)


def _logical_awake(day, config: dict) -> tuple[datetime, datetime]:
    ctx = (config or {}).get("_quick_context") or {}
    use_ctx = isinstance(ctx, dict) and str(ctx.get("date") or "") == day.isoformat()
    wake = _clock((ctx.get("wake_time") if use_ctx else None) or config.get("wake_time") or config.get("day_start"), "07:00")
    sleep = _clock((ctx.get("sleep_start") if use_ctx else None) or config.get("sleep_start") or config.get("day_end"), "23:00")
    start = datetime.combine(day, wake, settings.tz)
    end = datetime.combine(day, sleep, settings.tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _task_day(task, start: datetime, horizon_days: int):
    for value in (getattr(task, "start", None), getattr(task, "end", None)):
        dt = _dt(value)
        if dt:
            day = dt.date()
            if start.date() <= day <= (start + timedelta(days=max(1, horizon_days))).date():
                return day
    return start.date()


def _has_explicit_time_bounds(raw: dict) -> bool:
    # User/meta-provided hard timing takes precedence over title inference.
    return any(raw.get(k) not in (None, "") for k in (
        "earliest", "latest_end", "exact_start", "hard_stop", "preferred_window_start", "preferred_window_end"
    ))


def _set_window(raw: dict, *, earliest: datetime | None = None, latest: datetime | None = None,
                pref_start: datetime | None = None, pref_end: datetime | None = None,
                timing: str | None = None, reason: str):
    if earliest is not None:
        raw["earliest"] = earliest.isoformat()
    if latest is not None:
        raw["latest_end"] = latest.isoformat()
    if pref_start is not None:
        raw["preferred_window_start"] = pref_start.strftime("%H:%M")
    if pref_end is not None:
        raw["preferred_window_end"] = pref_end.strftime("%H:%M")
    if timing:
        raw["timing"] = timing
    raw["title_semantic_inference"] = reason


def _apply_title_semantics(task, raw: dict, start: datetime, horizon_days: int, config: dict) -> None:
    if getattr(task, "status", 0) != 0 or not getattr(task, "is_actionable", True):
        return
    tags = _tags(task)
    if "fixed" in tags or "autoscheduler-session" in tags or raw.get("autoschedule") is False:
        return
    if _has_explicit_time_bounds(raw):
        return

    text = _norm(" ".join([
        str(getattr(task, "title", "") or ""),
        str(getattr(task, "desc", "") or ""),
        str(getattr(task, "content", "") or "")[:600],
    ]))
    if not text:
        return

    day = _task_day(task, start, horizon_days)
    wake, sleep = _logical_awake(day, config)
    awake_minutes = max(60, int((sleep - wake).total_seconds() // 60))

    # Strong lifecycle phrases first. These are more specific than the generic
    # words morning/evening/night.
    if re.search(r"\b(?:wake up|wake-up|get up|morning wake)\b", text):
        end = min(sleep, wake + timedelta(minutes=45))
        _set_window(raw, earliest=wake, latest=end, pref_start=wake, pref_end=end,
                    timing="asap", reason="wake-time task")
        return

    if any(x in text for x in (
        "close today", "plan tomorrow", "plan tmr", "night review",
        "end of day review", "end-of-day review", "before bed", "before sleep",
        "before sleeping", "bedtime review", "nightly review",
    )):
        # The task belongs to the closing edge of the logical day, not lunchtime.
        earliest = max(wake, sleep - timedelta(minutes=75))
        pref_start = max(earliest, sleep - timedelta(minutes=35))
        _set_window(raw, earliest=earliest, latest=sleep, pref_start=pref_start,
                    pref_end=sleep, timing="late", reason="end-of-day task")
        return

    if any(x in text for x in ("plan the day", "morning review", "morning plan", "start the day", "daily planning")):
        earliest = min(sleep, wake + timedelta(minutes=20))
        latest = min(sleep, wake + timedelta(hours=3))
        pref_start = min(latest, wake + timedelta(minutes=45))
        pref_end = min(latest, wake + timedelta(hours=2))
        _set_window(raw, earliest=earliest, latest=latest, pref_start=pref_start,
                    pref_end=pref_end, timing="asap", reason="start-of-day task")
        return

    # Generic natural-language time-of-day cues. Keep these wider than the strong
    # lifecycle phrases so ordinary tasks retain optimizer freedom.
    if re.search(r"\bmorning\b", text):
        latest = min(sleep, datetime.combine(day, time(12, 0), settings.tz))
        if latest <= wake:
            latest = min(sleep, wake + timedelta(hours=4))
        _set_window(raw, earliest=wake, latest=latest, pref_start=wake,
                    pref_end=latest, timing="asap", reason="morning task")
        return

    if re.search(r"\bafternoon\b", text):
        earliest = max(wake, datetime.combine(day, time(12, 0), settings.tz))
        latest = min(sleep, datetime.combine(day, time(17, 30), settings.tz))
        if latest > earliest:
            _set_window(raw, earliest=earliest, latest=latest, pref_start=earliest,
                        pref_end=latest, timing="balanced", reason="afternoon task")
        return

    if re.search(r"\bevening\b", text):
        earliest = max(wake, datetime.combine(day, time(17, 0), settings.tz))
        if sleep > earliest:
            _set_window(raw, earliest=earliest, latest=sleep, pref_start=earliest,
                        pref_end=sleep, timing="late", reason="evening task")
        return

    if re.search(r"\b(?:night|nightly)\b", text):
        earliest = max(wake, sleep - timedelta(hours=2))
        _set_window(raw, earliest=earliest, latest=sleep, pref_start=earliest,
                    pref_end=sleep, timing="late", reason="night task")
        return


def _safe_estimate(task, raw=None):
    # decision_patch historically estimated NOTE items before scheduler.py later
    # filtered them. That produced nonsense warnings such as NOTE ≈ 30m.
    if not getattr(task, "is_actionable", True):
        return None
    return _ORIGINAL_ESTIMATE(task, raw or {})


def _ensure_ortools_runtime() -> tuple[bool, str | None]:
    """Repair a mutated/lazy engine flag if CP-SAT is actually importable."""
    if scheduler.ORTOOLS_AVAILABLE and scheduler.cp_model is not None:
        return True, None
    try:
        from ortools.sat.python import cp_model
        scheduler.cp_model = cp_model
        scheduler.ORTOOLS_AVAILABLE = True
        return True, "OR-Tools runtime flag was repaired by a lazy import."
    except Exception as exc:  # pragma: no cover - production diagnostic only
        return False, f"OR-Tools runtime import failed: {type(exc).__name__}."


def install_task_semantics(base_plan):
    # Patch the estimator reference actually used by decision_patch. This is
    # plan-local behavior; no user data is rewritten.
    decision_patch.estimate_task = _safe_estimate

    def semantic_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        start = start.astimezone(settings.tz) if start.tzinfo else start.replace(tzinfo=settings.tz)
        metas = deepcopy(meta_map or {})
        inferred = []
        for task in tasks:
            raw = metas.setdefault(task.id, {})
            before = dict(raw)
            _apply_title_semantics(task, raw, start, horizon_days, config or {})
            if raw.get("title_semantic_inference") and raw != before:
                inferred.append({
                    "task_id": task.id,
                    "title": task.title,
                    "reason": raw.get("title_semantic_inference"),
                    "earliest": raw.get("earliest"),
                    "latest_end": raw.get("latest_end"),
                    "preferred_window_start": raw.get("preferred_window_start"),
                    "preferred_window_end": raw.get("preferred_window_end"),
                })

        engine_ok, engine_note = _ensure_ortools_runtime()
        segments, warnings, diagnostics = base_plan(
            tasks, metas, busy, start, horizon_days, config, mastery_map or {}
        )
        diagnostics = dict(diagnostics or {})
        diagnostics["title_semantic_timing"] = inferred
        diagnostics["runtime_ortools_available"] = bool(scheduler.ORTOOLS_AVAILABLE)
        warnings = list(warnings or [])
        if engine_note:
            warnings.append(engine_note)
        # The hosted image explicitly installs/import-checks OR-Tools. If a lower
        # wrapper still claims otherwise while runtime says CP-SAT exists, surface
        # that inconsistency instead of telling the user to run pip manually.
        if engine_ok:
            warnings = [
                w for w in warnings
                if "or-tools is not installed; using fallback heuristic" not in str(w).lower()
            ]
            if diagnostics.get("engine") == "heuristic-fallback":
                warnings.append("Planner engine invariant: CP-SAT is available but a fallback path was selected; this plan should not be committed until regenerated.")
                diagnostics["engine_invariant_violation"] = True
        return segments, list(dict.fromkeys(warnings)), diagnostics

    return semantic_plan


__all__ = ["install_task_semantics", "_apply_title_semantics"]
