from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, time
from typing import Iterable

from .config import settings
from .db import duration_multiplier
from .models import Task, TaskMeta, BusyBlock, Segment

try:
    from ortools.sat.python import cp_model
    ORTOOLS_AVAILABLE = True
except Exception:  # pragma: no cover - fallback lets UI boot before dependencies finish installing
    cp_model = None
    ORTOOLS_AVAILABLE = False

GRID = 5

_HIGH_ENERGY_TITLE_HINTS = {
    "study", "revise", "revision", "practice", "calculus", "math", "maths", "physics",
    "coding", "code", "programming", "analysis", "proof", "assignment", "project",
    "report", "essay", "research", "homework", "problem set", "exam", "test",
}
_LOW_ENERGY_TITLE_HINTS = {
    "email", "reply", "message", "call", "submit", "print", "upload", "book", "pay",
    "organize", "organise", "clean", "sort", "pack", "get dressed", "bathe", "shower",
    "travel", "go home", "commute",
}
_CATEGORY_TITLE_HINTS = [
    ("math", {"calculus", "math", "maths", "algebra", "integration", "vector", "proof", "limits"}),
    ("physics", {"physics", "mechanics", "thermo", "aero", "fluid", "harmonic"}),
    ("coding", {"code", "coding", "programming", "python", "javascript", "github", "software"}),
    ("school", {"school", "class", "lecture", "tutorial", "lab", "lesson"}),
    ("fitness", {"gym", "run", "running", "swim", "swimming", "table tennis", "badminton", "football", "training"}),
    ("admin", _LOW_ENERGY_TITLE_HINTS),
]


def _aware(dt: datetime, convert: bool = False) -> datetime:
    """Normalize scheduler datetimes around the configured local timezone.

    Naive values are interpreted as local. Planning starts are converted to local
    time before calendar-day logic runs so UTC inputs cannot select the wrong day.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=settings.tz)
    return dt.astimezone(settings.tz) if convert else dt


def ceil_grid(dt: datetime, minutes: int = GRID) -> datetime:
    has_partial_minute = bool(dt.second or dt.microsecond)
    dt = dt.replace(second=0, microsecond=0)
    if has_partial_minute:
        dt += timedelta(minutes=1)
    rem = dt.minute % minutes
    if rem:
        dt += timedelta(minutes=minutes - rem)
    return dt


def overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def dependency_cycle(task_id: str, dependencies: list[str], metas: dict[str, dict]) -> bool:
    """Return True if replacing task_id's dependencies would create a dependency cycle."""
    graph = {k: list((v or {}).get("dependencies") or []) for k, v in metas.items()}
    graph[task_id] = list(dependencies)
    visiting, visited = set(), set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for nxt in graph.get(node, []):
            if visit(nxt):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return visit(task_id)


def merge_busy(blocks: Iterable[BusyBlock]) -> list[BusyBlock]:
    blocks = sorted((b for b in blocks if b.end > b.start), key=lambda b: b.start)
    out: list[BusyBlock] = []
    for b in blocks:
        if not out or b.start > out[-1].end:
            out.append(BusyBlock(b.start, b.end, b.label, b.source))
        else:
            out[-1].end = max(out[-1].end, b.end)
            if b.label not in out[-1].label:
                out[-1].label += f" + {b.label}"
    return out


def free_windows(day_start: datetime, day_end: datetime, busy: list[BusyBlock]):
    cursor = day_start
    for b in merge_busy([x for x in busy if overlap(day_start, day_end, x.start, x.end)]):
        s = max(day_start, b.start)
        e = min(day_end, b.end)
        if cursor < s:
            yield cursor, s
        cursor = max(cursor, e)
    if cursor < day_end:
        yield cursor, day_end


def _quick_context(config: dict) -> dict:
    ctx = config.get("_quick_context") or {}
    return ctx if isinstance(ctx, dict) else {}


def _ctx_dt(value):
    if not value:
        return None
    try:
        x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=settings.tz)
        return x.astimezone(settings.tz)
    except Exception:
        return None


def _override_for_day(day, config: dict) -> dict:
    ctx = _quick_context(config)
    return ctx if str(ctx.get("date") or "") == day.isoformat() else {}


def _day_clocks(day, config: dict) -> tuple[time, time]:
    ctx = _override_for_day(day, config)
    wake_text = str(ctx.get("wake_time") or config.get("wake_time") or config.get("day_start", "07:00"))
    sleep_text = str(ctx.get("sleep_start") or config.get("sleep_start") or config.get("day_end", "23:00"))
    return time.fromisoformat(wake_text), time.fromisoformat(sleep_text)


def _quick_context_active(dt: datetime, config: dict) -> dict:
    ctx = _quick_context(config)
    if not ctx.get("date"):
        return {}
    if dt.date().isoformat() == ctx.get("date"):
        return ctx
    # If today's bedtime runs after midnight, the early-hours tail still belongs
    # to the previous logical day and should keep the same fatigue/energy context.
    try:
        ctx_day = datetime.fromisoformat(ctx["date"]).date()
        sleep = time.fromisoformat(str(ctx.get("sleep_start") or config.get("sleep_start") or "23:00"))
        wake = time.fromisoformat(str(ctx.get("wake_time") or config.get("wake_time") or "07:00"))
        if sleep <= wake and dt.date() == ctx_day + timedelta(days=1) and dt.timetz().replace(tzinfo=None) < sleep:
            return ctx
    except Exception:
        pass
    return {}


def _fatigue_penalty(task: Task, meta: TaskMeta, start: datetime, config: dict) -> float:
    ctx = _quick_context_active(start, config)
    if not ctx:
        return 0.0
    need = tag_energy(task, meta)
    fatigue_from = _ctx_dt(ctx.get("fatigue_from"))
    fatigue_until = _ctx_dt(ctx.get("fatigue_until"))
    inside = bool(fatigue_from and fatigue_until and fatigue_from <= start < fatigue_until)
    penalty = 0.0
    if inside:
        penalty += 190.0 if need == "high" else (55.0 if need == "medium" else 0.0)
    if ctx.get("avoid_deep_today") and need == "high" and (fatigue_from is None or start >= fatigue_from):
        penalty += 260.0
    return penalty


def energy_level(dt: datetime, config: dict) -> float:
    points = config.get("energy_curve") or [
        [7, 0.65], [8, 0.85], [10, 1.0], [12, 0.82], [14, 0.72],
        [16, 0.82], [18, 0.68], [20, 0.58], [22, 0.40], [23, 0.25],
    ]
    h = dt.hour + dt.minute / 60
    wake_t, sleep_t = _day_clocks(dt.date(), config)
    wake_h = wake_t.hour + wake_t.minute / 60
    sleep_h = sleep_t.hour + sleep_t.minute / 60
    if sleep_h <= wake_h and h < wake_h:
        h += 24
    pts = sorted((float(a), float(b)) for a, b in points)
    ctx = _quick_context_active(dt, config)
    if ctx and (ctx.get("wake_time") or ctx.get("sleep_start")) and len(pts) >= 2:
        # The saved curve is fitted to the normal awake window. A one-day late wake
        # or early bedtime remaps the same curve shape instead of permanently editing it.
        try:
            normal_wake = time.fromisoformat(str(config.get("wake_time") or config.get("day_start", "07:00")))
            normal_sleep = time.fromisoformat(str(config.get("sleep_start") or config.get("day_end", "23:00")))
            ow = time.fromisoformat(str(ctx.get("wake_time") or normal_wake.strftime("%H:%M")))
            os = time.fromisoformat(str(ctx.get("sleep_start") or normal_sleep.strftime("%H:%M")))
            new_start = ow.hour + ow.minute / 60
            new_end = os.hour + os.minute / 60
            if new_end <= new_start:
                new_end += 24
            old_start, old_end = pts[0][0], pts[-1][0]
            if old_end > old_start:
                pts = [(new_start + ((ph-old_start)/(old_end-old_start))*(new_end-new_start), pv) for ph,pv in pts]
        except Exception:
            pass
    if h <= pts[0][0]:
        base = pts[0][1]
    elif h >= pts[-1][0]:
        base = pts[-1][1]
    else:
        base = 0.6
        for (h0, v0), (h1, v1) in zip(pts, pts[1:]):
            if h0 <= h <= h1:
                r = (h - h0) / (h1 - h0)
                base = v0 + r * (v1 - v0)
                break
    if ctx:
        fatigue_from = _ctx_dt(ctx.get("fatigue_from"))
        fatigue_until = _ctx_dt(ctx.get("fatigue_until"))
        try:
            requested_scale = max(0.15, min(1.2, float(ctx.get("energy_scale", 1.0))))
        except Exception:
            requested_scale = 1.0
        # "Tired after school" should not rewrite energy earlier than the declared/recovered start.
        scale = requested_scale if (fatigue_from is None or dt >= fatigue_from) else 1.0
        if fatigue_from and fatigue_until and fatigue_from <= dt < fatigue_until:
            scale *= 0.72
        base *= scale
    return max(0.05, min(1.0, base))


def tag_energy(task: Task, meta: TaskMeta) -> str:
    tags = {x.lower() for x in task.tags}
    if meta.energy != "auto":
        return meta.energy
    if "deep-work" in tags:
        return "high"
    if "low-energy" in tags or "quick-win" in tags:
        return "low"
    title = task.title.lower()
    if any(x in title for x in _HIGH_ENERGY_TITLE_HINTS):
        return "high"
    if any(x in title for x in _LOW_ENERGY_TITLE_HINTS):
        return "low"
    return "medium"


def inferred_category(task: Task, meta: TaskMeta) -> str:
    if meta.category:
        return str(meta.category).strip().casefold()
    title = task.title.lower()
    for name, words in _CATEGORY_TITLE_HINTS:
        if any(w in title for w in words):
            return name
    return "general"


def duration_for(task: Task, meta: TaskMeta) -> int | None:
    if meta.explicit_activity_minutes is not None:
        return max(0, int(meta.explicit_activity_minutes))
    # `0` is meaningful: it means no work remains. Do not fall back to the
    # original estimate just because zero is falsy.
    if meta.remaining_minutes is not None:
        base = meta.remaining_minutes
    elif meta.duration_minutes is not None:
        base = meta.duration_minutes
    else:
        base = task.duration_minutes
    if base is None:
        return None
    if base <= 0:
        return 0
    category = meta.category or ("deep-work" if tag_energy(task, meta) == "high" else inferred_category(task, meta))
    learned = duration_multiplier(category)
    if meta.confidence == "high":
        factor = max(1.0, learned * 0.95)
    elif meta.confidence == "medium":
        factor = max(1.10, learned)
    else:
        factor = max(1.35, learned * 1.10)
    return max(GRID, int(math.ceil((base * factor) / GRID) * GRID))


def priority_score(task: Task, meta: TaskMeta, now: datetime, mastery: float | None = None,
                   criticality: float = 0.0, config: dict | None = None,
                   deadline_pressure: float = 0.0) -> float:
    p = {0: 0, 1: 10, 3: 28, 5: 50}.get(task.priority, 0)
    score = float(p)
    config = config or {}
    if meta.deadline:
        hrs = max(0.25, (meta.deadline - now).total_seconds() / 3600)
        score += min(90, 150 / hrs)
        if meta.deadline <= now + timedelta(hours=24):
            score += 20
    if mastery is not None:
        score += max(0, (100 - mastery) * 0.18)
    score += min(70.0, max(0.0, deadline_pressure))
    if meta.must_finish:
        score += 14.0
    if task.created_at:
        try:
            age_days = max(0.0, (now - task.created_at).total_seconds() / 86400.0)
            score += min(10.0, age_days * 0.35)
        except Exception:
            pass
    tags = {x.lower() for x in task.tags}
    if "quick-win" in tags:
        score += 3
    # Explicit recovery replans should not quietly abandon still-open blocks that
    # were scheduled before the user actually woke up / returned to the plan.
    ctx = _quick_context(config)
    cutoff = _ctx_dt(ctx.get("replan_from")) if ctx.get("replan_requested") else None
    if cutoff and ctx.get("catch_up_missed") and task.start:
        if task.start.date().isoformat() == str(ctx.get("date") or "") and task.start < cutoff:
            score += 48.0
    score += min(25.0, criticality)
    return score


def _plan_start(config: dict, fallback: datetime) -> datetime:
    x = _ctx_dt(config.get("_plan_start"))
    return x or fallback


def _context_key(task: Task, meta: TaskMeta) -> str:
    return str(meta.context or inferred_category(task, meta) or tag_energy(task, meta) or "general").strip().casefold()


def _stability_penalty(task: Task, candidate_start: datetime, plan_start: datetime, config: dict) -> float:
    """Penalize needless churn, especially for work that is about to begin.

    A recovery replan is allowed to move already-missed flexible work aggressively,
    while future blocks inside the freeze window are deliberately sticky.
    """
    if not task.start or task.start < plan_start:
        return 0.0
    # During a productivity compaction pass, existing flexible TickTick work is
    # deliberately allowed to move. Hard timing, dependencies, fixed commitments,
    # recovery, meals, sleep and travel are enforced elsewhere in candidate
    # generation; this flag only removes the anti-churn preference.
    if config.get("_compact_flexible_schedule"):
        return 0.0
    diff = abs((candidate_start - task.start).total_seconds() / 60.0)
    if diff < 1:
        return 0.0
    tags = {x.lower() for x in task.tags}
    weight = float(config.get("stability_weight", 0.35)) * (0.35 if "flexible" in tags else 1.0)
    lead = max(0.0, (task.start - plan_start).total_seconds() / 60.0)
    freeze = max(0, int(config.get("stability_freeze_minutes", 35)))
    multiplier = 1.0
    if freeze and lead <= freeze:
        multiplier = 3.2
    elif freeze and lead <= freeze * 2:
        multiplier = 1.8

    ctx = _quick_context(config)
    cutoff = _ctx_dt(ctx.get("replan_from")) if ctx.get("replan_requested") else None
    if cutoff and ctx.get("catch_up_missed") and task.start < cutoff:
        multiplier *= 0.16
    return min(140.0, diff * weight * multiplier)


def _recovery_after_minutes(minutes: int, at: datetime, config: dict) -> int:
    """Adaptive recovery after cognitively heavy work.

    Long blocks and an active fatigue context earn slightly more recovery instead
    of applying one blunt fixed gap to everything.
    """
    base = max(0, int(config.get("high_energy_recovery_minutes", 15)))
    if base <= 0:
        return 0
    extra = 0
    if minutes >= 75:
        extra += 5
    if minutes >= 120:
        extra += 5
    ctx = _quick_context_active(at, config)
    fatigue_from = _ctx_dt(ctx.get("fatigue_from")) if ctx else None
    fatigue_until = _ctx_dt(ctx.get("fatigue_until")) if ctx else None
    if fatigue_from and fatigue_until and fatigue_from <= at < fatigue_until:
        extra += 5
    return min(60, base + extra)


def _capacity_until(cutoff: datetime, start: datetime, hard_busy: list[BusyBlock], config: dict) -> int:
    if cutoff <= start:
        return 0
    total = 0
    day = start.date()
    last = cutoff.date()
    while day <= last:
        ds, de = _usable_bounds(day, config)
        ds = max(ds, start)
        de = min(de, cutoff)
        if ds < de:
            total += sum(int((e - s).total_seconds() // 60) for s, e in free_windows(ds, de, hard_busy))
        day += timedelta(days=1)
    return max(0, total)


def _deadline_pressure(tasks: list[Task], metas: dict[str, TaskMeta], durations: dict[str, int],
                       hard_busy: list[BusyBlock], start: datetime, horizon_end: datetime,
                       config: dict) -> dict[str, float]:
    """Estimate how dangerous it is to postpone each deadline-bearing task.

    Pressure rises when time-to-deadline is short, personal work is large relative
    to available capacity, or several tasks compete for the same pre-deadline time.
    """
    out: dict[str, float] = {}
    deadline_rows = [(t, metas[t.id].deadline) for t in tasks if metas.get(t.id) and metas[t.id].deadline]
    capacity_cache: dict[str, int] = {}
    for t, deadline in deadline_rows:
        assert deadline is not None
        if deadline <= start:
            out[t.id] = 95.0
            continue
        cutoff = min(deadline, horizon_end)
        key = cutoff.isoformat()
        if key not in capacity_cache:
            capacity_cache[key] = _capacity_until(cutoff, start, hard_busy, config)
        cap = max(1, capacity_cache[key])
        cumulative = 0
        for other, other_deadline in deadline_rows:
            if other_deadline and other_deadline <= deadline:
                cumulative += max(0, durations.get(other.id, 0))
        own = max(0, durations.get(t.id, 0))
        demand_ratio = cumulative / cap
        own_ratio = own / cap
        hours = max(0.01, (deadline - start).total_seconds() / 3600.0)
        time_pressure = 32.0 if hours <= 6 else 24.0 if hours <= 12 else 15.0 if hours <= 24 else 8.0 if hours <= 48 else 3.0
        congestion = max(0.0, demand_ratio - 0.35) * 58.0
        personal = min(22.0, own_ratio * 32.0)
        out[t.id] = min(95.0, time_pressure + congestion + personal)
    return out


def slot_utility(task: Task, meta: TaskMeta, start: datetime, task_score: float, config: dict,
                 minutes: int = 0, deadline_pressure: float = 0.0) -> float:
    e = energy_level(start, config)
    need = tag_energy(task, meta)
    if need == "high":
        energy_fit = e * 42
    elif need == "low":
        energy_fit = (1.2 - e) * 16
    else:
        energy_fit = 16 - abs(e - 0.65) * 12
    utility = task_score + energy_fit
    utility -= _fatigue_penalty(task, meta, start, config)

    # Do not waste the user's strongest hours on low-energy admin when high-energy
    # work is waiting, unless other constraints make that the best tradeoff.
    if config.get("_has_high_energy_demand") and e >= 0.82:
        if need == "low":
            utility -= 12.0
        elif need == "medium":
            utility -= 3.0

    # When the user explicitly reports a late/actual wake and asks for a replan,
    # unfinished flexible tasks whose original TickTick block has already been
    # missed should be recovered promptly instead of drifting to the afternoon.
    # Fixed tasks never reach candidate generation, so this only affects flexible work.
    ctx = _quick_context_active(start, config)
    if ctx.get("replan_requested") and ctx.get("catch_up_missed") and task.start:
        cutoff = _ctx_dt(ctx.get("replan_from"))
        if cutoff and task.start.date().isoformat() == str(ctx.get("date") or "") and task.start < cutoff:
            utility += 52.0
            delay_minutes = max(0.0, (start - cutoff).total_seconds() / 60.0)
            utility -= min(88.0, delay_minutes * 0.11)
    if meta.preferred_window_start and meta.preferred_window_end:
        t = start.timetz().replace(tzinfo=None)
        if meta.preferred_window_start <= t <= meta.preferred_window_end:
            utility += 18
        else:
            utility -= 8
    plan_start = _plan_start(config, start)
    elapsed_hours = max(0.0, (start - plan_start).total_seconds() / 3600.0)

    # Deadlines are treated as strong soft targets rather than cliffs. If a task is
    # already overdue or mathematically cannot finish on time, the optimizer still
    # schedules the best recovery plan instead of dropping the task entirely.
    if meta.deadline:
        end = start + timedelta(minutes=max(0, minutes))
        late = max(0.0, (end - meta.deadline).total_seconds() / 60.0)
        baseline_end = plan_start + timedelta(minutes=max(0, minutes))
        unavoidable = max(0.0, (baseline_end - meta.deadline).total_seconds() / 60.0)
        avoidable_late = max(0.0, late - unavoidable)
        if late > 0:
            utility -= 18.0 + avoidable_late * (0.55 + deadline_pressure / 180.0)
        else:
            utility -= elapsed_hours * (deadline_pressure / 100.0) * 0.70

    # Keep late evening available for recovery/sleep wind-down unless urgency dominates.
    utility -= max(0, start.hour + start.minute / 60 - 20) * float(config.get("late_evening_penalty", 2.5))
    # Timing preference now spans the full planning horizon, not just clock time.
    if meta.timing == "asap":
        utility -= elapsed_hours * 1.10
    elif meta.timing == "late":
        utility += min(elapsed_hours, 72.0) * 0.22
    else:
        utility -= elapsed_hours * 0.06
    return utility


def build_meta(task: Task, raw: dict) -> TaskMeta:
    def dt(k):
        v = raw.get(k)
        return datetime.fromisoformat(v) if v else None

    def tm(k):
        v = raw.get(k)
        return time.fromisoformat(v) if v else None

    return TaskMeta(
        task_id=task.id,
        duration_minutes=raw.get("duration_minutes"),
        remaining_minutes=raw.get("remaining_minutes"),
        deadline=dt("deadline"),
        earliest=dt("earliest"),
        latest_end=dt("latest_end"),
        exact_start=dt("exact_start"),
        energy=raw.get("energy", "auto"),
        confidence=raw.get("confidence", "medium"),
        splittable=raw.get("splittable", True),
        min_chunk=int(raw.get("min_chunk", 25)),
        max_chunk=int(raw.get("max_chunk", 90)),
        preferred_window_start=tm("preferred_window_start"),
        preferred_window_end=tm("preferred_window_end"),
        dependencies=list(raw.get("dependencies") or []),
        category=raw.get("category"),
        hard_stop=dt("hard_stop"),
        autoschedule=bool(raw.get("autoschedule", True)),
        location=(raw.get("location") or None),
        context=(raw.get("context") or None),
        weekly_bucket=(raw.get("weekly_bucket") or None),
        transition_minutes=int(raw.get("transition_minutes") or 0),
        must_finish=bool(raw.get("must_finish", False)),
        unknown_duration=bool(raw.get("unknown_duration", False)),
        timing=str(raw.get("timing") or "balanced"),
        allowed_weekdays=[int(x) for x in (raw.get("allowed_weekdays") if raw.get("allowed_weekdays") is not None else list(range(7))) if 0 <= int(x) <= 6],
        explicit_activity_minutes=raw.get('_explicit_activity_minutes'),
    )


def choose_chunks(total: int, meta: TaskMeta) -> list[int]:
    if total <= meta.max_chunk or not meta.splittable:
        return [total]
    chunks: list[int] = []
    left = total
    while left:
        c = min(meta.max_chunk, left)
        if left - c and left - c < meta.min_chunk:
            c = max(meta.min_chunk, left - meta.min_chunk)
        chunks.append(c)
        left -= c
    return chunks


def _mastery_for(task: Task, mastery_map: dict[str, float]) -> float | None:
    title_words = {w.lower() for w in task.title.replace("—", " ").replace("-", " ").split() if len(w) > 3}
    for topic, val in mastery_map.items():
        if topic.lower() in task.title.lower() or title_words.intersection(topic.lower().split()):
            return val
    return None


def _criticality(tasks: list[Task], metas: dict[str, TaskMeta], durations: dict[str, int]) -> dict[str, float]:
    """Approximate DAG critical-path pressure from downstream unfinished work."""
    downstream: dict[str, list[str]] = defaultdict(list)
    for t in tasks:
        for dep in metas.get(t.id, TaskMeta(t.id)).dependencies:
            downstream[dep].append(t.id)

    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def longest(tid: str) -> int:
        if tid in memo:
            return memo[tid]
        if tid in visiting:  # dependency cycle; validation warning is emitted elsewhere
            return 0
        visiting.add(tid)
        val = durations.get(tid, 0) + max([longest(x) for x in downstream.get(tid, [])] or [0])
        visiting.remove(tid)
        memo[tid] = val
        return val

    for t in tasks:
        longest(t.id)
    return {k: min(25.0, v / 30.0) for k, v in memo.items()}



def _usable_bounds(day, config: dict) -> tuple[datetime, datetime]:
    """Return the user's logical awake/work window for a calendar day.

    Wake time is the actual start of the usable day. Sleep start is the end.
    When sleep starts after midnight (e.g. 00:30 with a 07:00 wake), the end
    belongs to the following calendar date. Legacy day_start/day_end values are
    kept only as fallbacks for older configs.
    """
    wake, sleep = _day_clocks(day, config)
    start_dt = datetime.combine(day, wake, settings.tz)
    end_dt = datetime.combine(day, sleep, settings.tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _sleep_bounds(day, config: dict) -> tuple[datetime, datetime]:
    """Return the protected sleep interval that contains the early morning of `day`."""
    wake, sleep = _day_clocks(day, config)
    if sleep > wake:
        return (
            datetime.combine(day - timedelta(days=1), sleep, settings.tz),
            datetime.combine(day, wake, settings.tz),
        )
    return (
        datetime.combine(day, sleep, settings.tz),
        datetime.combine(day, wake, settings.tz),
    )

def _task_schedulable_for_plan(task: Task, start: datetime) -> bool:
    """Final scheduler-side lifecycle/expiry gate.

    This is intentionally duplicated at the planner boundary rather than relying
    only on TickTick ingestion. It protects alternate callers, stale snapshots,
    and any task list assembled by another subsystem.
    """
    if not task.is_actionable:
        return False
    if task.is_expired(start):
        return False
    return True


def _hard_busy(tasks: list[Task], busy: list[BusyBlock], start: datetime, horizon_days: int, config: dict) -> list[BusyBlock]:
    out = list(busy)
    interrupted_source = str(config.get("_interrupted_source_id") or "")

    def generated_source_id(task: Task) -> str | None:
        for line in (task.content or "").splitlines():
            if line.startswith("AutoSchedulerSource:"):
                return line.split(":", 1)[1].strip()
        return None
    fixed = [
        t for t in tasks
        if t.is_actionable and not t.is_all_day and "fixed" in {x.lower() for x in t.tags} and t.start and t.end
    ]
    for t in fixed:
        out.append(BusyBlock(t.start, t.end, t.title, "ticktick-fixed"))

    # Preserve an already-started generated work session while replanning around an interruption.
    # Future generated sessions are intentionally not hard-busy because a new plan may replace them.
    for t in tasks:
        tags = {x.lower() for x in t.tags}
        if "autoscheduler-session" in tags and t.start and t.end and t.start <= start < t.end:
            if interrupted_source and generated_source_id(t) == interrupted_source:
                continue
            ctx = _quick_context(config)
            cutoff = _ctx_dt(ctx.get('replan_from'))
            if ctx.get('preserve_unfinished') and ctx.get('replan_requested') and cutoff and t.start < cutoff:
                from .service import _owned_generated_session
                if _owned_generated_session(t):
                    continue
            out.append(BusyBlock(t.start, t.end, t.title, "active-session"))

    meals = config.get("meals") or []

    # Candidate generation already stays inside the awake window. Explicit sleep
    # blocks are still added so capacity diagnostics and external busy merging are
    # correct around midnight. Include one surrounding day for overnight sleep.
    for d in range(-1, horizon_days + 1):
        day = (start + timedelta(days=d)).date()
        ss, we = _sleep_bounds(day, config)
        if we > ss:
            out.append(BusyBlock(ss, we, "Protected sleep", "rules"))
        if 0 <= d < horizon_days:
            ds, de = _usable_bounds(day, config)
            wind_down = max(0, int(config.get('bedtime_wind_down_minutes', 0)))
            if wind_down:
                out.append(BusyBlock(max(ds, de - timedelta(minutes=wind_down)), de,
                                     'Wind down before sleep', 'bedtime-wind-down'))
            for meal in meals:
                mt = time.fromisoformat(meal["start"])
                ms = datetime.combine(day, mt, settings.tz)
                # A meal after midnight but before wake belongs to the logical day
                # that started the previous morning. Move it forward one date.
                if ms < ds:
                    ms += timedelta(days=1)
                if ds <= ms < de:
                    out.append(BusyBlock(ms, ms + timedelta(minutes=int(meal["minutes"])), meal["name"], "rules"))
    return merge_busy(out)


def _meal_activity_start_allowed(task_id: str, candidate_start: datetime, config: dict, candidate_end: datetime | None = None) -> bool:
    """Check actual sport start within a door-to-door outing, in both engines.

    Guards are transient planner input, never persisted effort estimates. Travel
    and changing may use part of a post-meal comfort gap; desk work remains free.
    """
    for window in (config.get('_task_exclusion_windows') or {}).get(task_id, []):
        lower, upper = datetime.fromisoformat(window['start']), datetime.fromisoformat(window['end'])
        if candidate_start < upper and lower < (candidate_end or candidate_start + timedelta(seconds=1)):
            return False
    for guard in (config.get("_meal_activity_start_guards") or {}).get(task_id, []):
        meal_start = datetime.fromisoformat(guard["meal_start"])
        activity_ready = datetime.fromisoformat(guard["activity_ready"])
        outing_ready = datetime.fromisoformat(guard["outing_ready"])
        activity_start = candidate_start + timedelta(minutes=guard["pre_minutes"])
        if guard.get('require_after') and (activity_start < activity_ready or candidate_start < outing_ready):
            return False
        if meal_start <= activity_start < activity_ready:
            return False
        if meal_start <= candidate_start < outing_ready:
            return False
    return True


def _candidate_starts(window_start: datetime, window_end: datetime, minutes: int, step: int):
    # Exact 5-minute support windows must remain legal even when candidate
    # sampling uses a coarser 10-minute step.
    s = ceil_grid(window_start, GRID)
    while s + timedelta(minutes=minutes) <= window_end:
        yield s
        s += timedelta(minutes=step)


def _capacity_report(start: datetime, horizon_days: int, hard_busy: list[BusyBlock], segments: list[Segment], config: dict) -> list[dict]:
    out = []
    for d in range(horizon_days):
        day = (start + timedelta(days=d)).date()
        ds, de = _usable_bounds(day, config)
        if d == 0:
            ds = max(ds, start)
        free = sum(int((e - s).total_seconds() // 60) for s, e in free_windows(ds, de, hard_busy))
        scheduled = sum(int((seg.end - seg.start).total_seconds() // 60) for seg in segments if ds <= seg.start < de)
        out.append({
            "date": day.isoformat(),
            "free_minutes": free,
            "scheduled_minutes": scheduled,
            "remaining_slack_minutes": max(0, free - scheduled),
            "utilization": round(scheduled / free, 3) if free else 0,
        })
    return out


def _plan_cpsat(tasks: list[Task], meta_map: dict[str, dict], busy: list[BusyBlock], start: datetime,
                horizon_days: int, config: dict, mastery_map: dict[str, float]):
    config = dict(config)
    config["_plan_start"] = start.isoformat()
    warnings: list[str] = []
    diagnostics: dict = {"engine": "cp-sat", "ortools": True, "optimizer": "smart-v8.2"}
    hard_busy = _hard_busy(tasks, busy, start, horizon_days, config)
    last_day = (start + timedelta(days=max(0, horizon_days - 1))).date()
    _, horizon_end = _usable_bounds(last_day, config)

    source_tasks: list[Task] = []
    no_work_left: set[str] = set()
    metas: dict[str, TaskMeta] = {}
    durations: dict[str, int] = {}
    for t in tasks:
        tags = {x.lower() for x in t.tags}
        if (not _task_schedulable_for_plan(t, start)) or "fixed" in tags or "autoscheduler-session" in tags:
            continue
        m = build_meta(t, meta_map.get(t.id, {}))
        if not m.autoschedule:
            continue
        dur = duration_for(t, m)
        if dur is None:
            warnings.append(f"{t.title}: no usable duration; set a duration or use Unknown/checkpoint")
            continue
        if dur <= 0:
            no_work_left.add(t.id)
            continue
        source_tasks.append(t)
        metas[t.id] = m
        durations[t.id] = dur

    critical = _criticality(source_tasks, metas, durations)
    pressure = _deadline_pressure(source_tasks, metas, durations, hard_busy, start, horizon_end, config)
    config["_has_high_energy_demand"] = any(tag_energy(t, metas[t.id]) == "high" for t in source_tasks)
    task_score: dict[str, float] = {
        t.id: priority_score(
            t, metas[t.id], start, _mastery_for(t, mastery_map), critical.get(t.id, 0.0), config,
            pressure.get(t.id, 0.0),
        )
        for t in source_tasks
    }

    # Dependency state includes all actionable active tasks, not only schedulable ones.
    # This prevents an unfinished prerequisite with no duration from being mistaken for "already completed".
    all_active = {t.id: t for t in tasks if t.is_actionable and t.status == 0 and "autoscheduler-session" not in {x.lower() for x in t.tags}}

    # Detect dependency cycles early. Cyclic tasks are blocked instead of making the whole model opaque/infeasible.
    graph = {t.id: [d for d in metas[t.id].dependencies if d in metas] for t in source_tasks}
    temp, perm = set(), set()
    cycle_nodes: set[str] = set()
    def visit(n: str):
        if n in perm:
            return
        if n in temp:
            cycle_nodes.add(n); return
        temp.add(n)
        for x in graph.get(n, []):
            visit(x)
            if x in cycle_nodes:
                cycle_nodes.add(n)
        temp.remove(n); perm.add(n)
    for n in graph:
        visit(n)
    if cycle_nodes:
        warnings.append("Dependency cycle detected: " + ", ".join(next((t.title for t in source_tasks if t.id == x), x) for x in sorted(cycle_nodes)))

    model = cp_model.CpModel()
    step = max(GRID, int(config.get("candidate_step_minutes", 10)))
    max_candidates = max(12, int(config.get("max_candidates_per_chunk", 60)))
    between = int(config.get("between_chunks_buffer", 10))
    stability_weight = float(config.get("stability_weight", 0.35))

    # Encode precedence with linear expressions instead of O(A×B) pairwise
    # candidate conflicts. This keeps dependency-heavy plans tractable.
    def _minute_expr(rows, key):
        return sum(int((r[key] - start).total_seconds() // 60) * r["var"] for r in rows)

    def _add_precedence(first_rows, second_rows, gap_minutes, enforce_literals):
        if first_rows and second_rows:
            model.Add(
                _minute_expr(second_rows, "start")
                >= _minute_expr(first_rows, "end") + gap_minutes
            ).OnlyEnforceIf(enforce_literals)

    chunks: dict[str, list[dict]] = {}
    must_finish_vars: dict[str, object] = {}
    all_candidates: list[dict] = []
    all_intervals = []
    candidate_serial = 0

    for t in source_tasks:
        m = metas[t.id]
        from .plan_duration_requests import requested_chunks
        chunk_sizes = requested_chunks(t, durations[t.id], m, config, choose_chunks)
        task_chunks = []
        for ci, chunk_minutes in enumerate(chunk_sizes):
            raw_candidates = []
            earliest = max(start, m.earliest) if m.earliest else start
            # Deadline is a strong soft target, not a hard cliff. latest_end and
            # hard_stop remain true hard bounds. This lets overdue/impossible tasks
            # recover instead of disappearing from the schedule.
            last_end = min(x for x in [horizon_end, m.latest_end or horizon_end, m.hard_stop or horizon_end])
            initial_end = _ctx_dt(meta_map.get(t.id, {}).get('_initial_latest_end'))
            if ci == 0 and initial_end:
                last_end = min(last_end, initial_end)
            if ci == 0 and m.exact_start is not None:
                # Exact user-stated starts are hard constraints, not soft preferences.
                # Keep the activity duration independent: "study at 19:00" fixes the
                # start but does not invent a user-stated end time.
                s = m.exact_start.astimezone(settings.tz) if m.exact_start.tzinfo else m.exact_start.replace(tzinfo=settings.tz)
                e = s + timedelta(minutes=chunk_minutes)
                within_horizon = start <= s < horizon_end
                weekday_ok = not m.allowed_weekdays or s.weekday() in m.allowed_weekdays
                awake_start, awake_end = _usable_bounds(s.date(), config)
                bounds_ok = (
                    s >= earliest and e <= last_end and
                    s >= awake_start and e <= awake_end
                )
                free_ok = any(fs <= s and e <= fe for fs, fe in free_windows(awake_start, awake_end, hard_busy))
                if within_horizon and weekday_ok and bounds_ok and free_ok and _meal_activity_start_allowed(t.id, s, config, e):
                    util = slot_utility(t, m, s, task_score[t.id], config, chunk_minutes, pressure.get(t.id, 0.0))
                    util -= _stability_penalty(t, s, start, config)
                    raw_candidates.append((util, s, e))
            else:
                for dd in range(horizon_days):
                    day = (start + timedelta(days=dd)).date()
                    if m.allowed_weekdays and day.weekday() not in m.allowed_weekdays:
                        continue
                    ds, de = _usable_bounds(day, config)
                    ds = max(ds, earliest)
                    de = min(de, last_end)
                    if ds >= de:
                        continue
                    for fs, fe in free_windows(ds, de, hard_busy):
                        for s in _candidate_starts(fs, fe, chunk_minutes, step):
                            e = s + timedelta(minutes=chunk_minutes)
                            if not _meal_activity_start_allowed(t.id, s, config, e):
                                continue
                            util = slot_utility(t, m, s, task_score[t.id], config, chunk_minutes, pressure.get(t.id, 0.0))
                            util -= _stability_penalty(t, s, start, config)
                            # Encourage contiguous-ish chunks without forcing marathon sessions.
                            util -= ci * 0.5
                            raw_candidates.append((util, s, e))
            if not raw_candidates:
                warnings.append(f"{t.title}: chunk {ci+1}/{len(chunk_sizes)} has no legal time window")
                task_chunks.append({"minutes": chunk_minutes, "candidates": [], "presence": model.NewBoolVar(f"present_{t.id}_{ci}")})
                model.Add(task_chunks[-1]["presence"] == 0)
                continue

            # Preserve day diversity so a globally high-scoring morning does not erase all fallback days.
            by_day: dict[str, list[tuple]] = defaultdict(list)
            for row in raw_candidates:
                by_day[row[1].date().isoformat()].append(row)
            selected = []
            # Preserve legal-window topology before ranking by utility. Keeping only
            # the top morning candidates can make "A before B" appear impossible
            # when B has plenty of legal capacity in the afternoon/evening.
            coverage_candidates = []
            for day_rows in by_day.values():
                ordered_rows = sorted(day_rows, key=lambda x: x[1])
                windows = []
                for row in ordered_rows:
                    if not windows or row[1] - windows[-1][-1][1] > timedelta(minutes=step * 1.5):
                        windows.append([])
                    windows[-1].append(row)
                for window in windows:
                    coverage_candidates.extend([window[0], window[-1], max(window, key=lambda x: x[0])])
            seen_candidates = set()
            for row in coverage_candidates:
                key = (row[1], row[2])
                if key not in seen_candidates and len(selected) < max_candidates:
                    selected.append(row)
                    seen_candidates.add(key)
            per_day = max(4, max_candidates // max(1, horizon_days))
            for rows in by_day.values():
                rows.sort(key=lambda x: x[0], reverse=True)
                for row in rows[:per_day]:
                    key = (row[1], row[2])
                    if key not in seen_candidates and len(selected) < max_candidates:
                        selected.append(row)
                        seen_candidates.add(key)
            if len(selected) < max_candidates:
                existing = {(s, e) for _, s, e in selected}
                for row in sorted(raw_candidates, key=lambda x: x[0], reverse=True):
                    if (row[1], row[2]) not in existing:
                        selected.append(row); existing.add((row[1], row[2]))
                    if len(selected) >= max_candidates:
                        break
            selected = sorted(selected[:max_candidates], key=lambda x: x[1])

            presence = model.NewBoolVar(f"present_{t.id}_{ci}")
            cand_objs = []
            vars_for_chunk = []
            for util, s, e in selected:
                candidate_serial += 1
                x = model.NewBoolVar(f"x_{candidate_serial}")
                vars_for_chunk.append(x)
                start_slot = int((s - start).total_seconds() // 60 // GRID)
                dur_slots = max(1, int(math.ceil(chunk_minutes / GRID)))
                interval = model.NewOptionalFixedSizeIntervalVar(start_slot, dur_slots, x, f"iv_{candidate_serial}")
                all_intervals.append(interval)
                obj = {
                    "var": x, "task": t, "meta": m, "chunk_index": ci, "chunk_count": len(chunk_sizes),
                    "minutes": chunk_minutes, "start": s, "end": e, "utility": util, "interval": interval,
                }
                cand_objs.append(obj); all_candidates.append(obj)
            model.Add(sum(vars_for_chunk) == presence)
            task_chunks.append({"minutes": chunk_minutes, "candidates": cand_objs, "presence": presence})
        chunks[t.id] = task_chunks

    if cycle_nodes:
        for tid in cycle_nodes:
            for ch in chunks.get(tid, []):
                model.Add(ch["presence"] == 0)

    # No flexible work can overlap another flexible task.
    if all_intervals:
        model.AddNoOverlap(all_intervals)

    # A stated daily session duration limits allocation, not remaining effort.
    # Combined subjects share the budget; genuine completion dependencies still
    # require the full original chunk list.
    from .plan_duration_requests import budget_intervals
    for ids, minutes, budget_start, budget_end in budget_intervals(config):
        terms = [c['minutes'] * c['var'] for c in all_candidates
                 if c['task'].id in ids and budget_start <= c['start'] < budget_end
                 and c['meta'].explicit_activity_minutes is None]
        if terms:
            model.Add(sum(terms) <= minutes)

    # Chunk order + buffer and all-or-prefix semantics.
    for t in source_tasks:
        tcs = chunks.get(t.id, [])
        for i in range(1, len(tcs)):
            model.Add(tcs[i]["presence"] <= tcs[i-1]["presence"])
            _add_precedence(
                tcs[i-1]["candidates"],
                tcs[i]["candidates"],
                between,
                tcs[i]["presence"],
            )
        if metas[t.id].must_finish and tcs:
            # Atomic, but never allowed to make the *entire* model infeasible.
            # The solver chooses either every chunk or none; a very large objective
            # bonus below makes a feasible must-finish task dominate ordinary work.
            activate = model.NewBoolVar(f"must_finish_{t.id}")
            must_finish_vars[t.id] = activate
            if all(c["candidates"] for c in tcs):
                for c in tcs:
                    model.Add(c["presence"] == activate)
            else:
                model.Add(activate == 0)
                for c in tcs:
                    model.Add(c["presence"] == 0)
                warnings.append(f"{t.title}: Must finish is impossible inside the current hard constraints; no partial block was scheduled")

    # Explicit task dependencies. A dependency is satisfied only if it is no longer active,
    # or it has a known future/manual block that the dependent can be placed after.
    # In addition to the hard precedence rule, we softly prefer follow-through soon after
    # the prerequisite so dependency chains feel human instead of drifting days apart.
    schedulable_ids = {t.id for t in source_tasks}
    dependency_penalties = []
    dep_weight = max(0.0, float(config.get("dependency_proximity_weight", 2.0)))
    max_gap_slots = max(1, int(math.ceil((horizon_end - start).total_seconds() / 60 / GRID)) + 12)
    buffer_slots = max(0, int(math.ceil(between / GRID)))
    by_title = {t.id: t.title for t in tasks}
    for t in source_tasks:
        child_chunks = chunks.get(t.id, [])
        if not child_chunks:
            continue
        child_first = child_chunks[0]
        for dep_id in metas[t.id].dependencies:
            dep_gap = int((meta_map.get(t.id, {}).get("_dependency_gap_minutes") or {}).get(dep_id, between))
            dep_gap = max(0, dep_gap)
            dep_buffer_slots = max(0, int(math.ceil(dep_gap / GRID)))
            if dep_id == t.id:
                model.Add(child_first["presence"] == 0)
                warnings.append(f"{t.title}: a task cannot depend on itself")
                continue
            dep_task = all_active.get(dep_id)
            if dep_task is None or dep_id in no_work_left:
                continue  # inactive/deleted, or explicitly no remaining effort => satisfied
            if dep_id in schedulable_ids:
                dep_chunks = chunks.get(dep_id, [])
                if not dep_chunks:
                    model.Add(child_first["presence"] == 0)
                    warnings.append(f"{t.title}: prerequisite {by_title.get(dep_id, dep_id)} is active but cannot be scheduled")
                    continue
                session_dependency = dep_id in (meta_map.get(t.id, {}).get('_session_dependency_ids') or [])
                dep_last = dep_chunks[0] if session_dependency else dep_chunks[-1]
                model.Add(child_first["presence"] <= dep_last["presence"] )
                dep_candidates = [row for ch in dep_chunks for row in ch['candidates']] if session_dependency else dep_last['candidates']
                if session_dependency:
                    # A child follows every dependency chunk that actually runs.
                    for dch in dep_chunks:
                        _add_precedence(
                            dch["candidates"],
                            child_first["candidates"],
                            dep_gap,
                            [child_first["presence"], dch["presence"]],
                        )
                else:
                    _add_precedence(
                        dep_last["candidates"],
                        child_first["candidates"],
                        dep_gap,
                        child_first["presence"],
                    )
                if dep_weight > 0 and dep_candidates and child_first["candidates"]:
                    end_terms = [int((row['end']-start).total_seconds()//60//GRID)*row['var'] for row in dep_candidates]
                    if session_dependency:
                        dep_end_expr = model.NewIntVar(0, max_gap_slots, f'session_end_{dep_id}_{t.id}')
                        model.AddMaxEquality(dep_end_expr, end_terms)
                    else:
                        dep_end_expr = sum(end_terms)
                    child_start_expr = sum(
                        int((row["start"] - start).total_seconds() // 60 // GRID) * row["var"]
                        for row in child_first["candidates"]
                    )
                    gap = model.NewIntVar(0, max_gap_slots, f"dep_gap_{dep_id}_{t.id}")
                    model.Add(gap == child_start_expr - dep_end_expr - dep_buffer_slots).OnlyEnforceIf(child_first["presence"])
                    model.Add(gap == 0).OnlyEnforceIf(child_first["presence"].Not())
                    dependency_penalties.append(max(1, int(round(dep_weight * GRID))) * gap)
                continue

            # Manually scheduled / #fixed / autoschedule-off prerequisite: plan dependent after its future block.
            if dep_task.end and dep_task.end > start and not dep_task.is_all_day:
                cutoff = dep_task.end + timedelta(minutes=dep_gap)
                for brow in child_first["candidates"]:
                    if brow["start"] < cutoff:
                        model.Add(brow["var"] == 0)
                if dep_weight > 0 and child_first["candidates"]:
                    child_start_expr = sum(
                        int((row["start"] - start).total_seconds() // 60 // GRID) * row["var"]
                        for row in child_first["candidates"]
                    )
                    cutoff_slot = max(0, int((cutoff - start).total_seconds() // 60 // GRID))
                    gap = model.NewIntVar(0, max_gap_slots, f"manual_dep_gap_{dep_id}_{t.id}")
                    model.Add(gap == child_start_expr - cutoff_slot).OnlyEnforceIf(child_first["presence"])
                    model.Add(gap == 0).OnlyEnforceIf(child_first["presence"].Not())
                    dependency_penalties.append(max(1, int(round(dep_weight * GRID))) * gap)
            else:
                model.Add(child_first["presence"] == 0)
                warnings.append(f"{t.title}: waiting for unfinished prerequisite {dep_task.title}")

    # Daily utilization/deep-work targets are SOFT. Hard reality (busy time/sleep) remains hard,
    # but urgent/must-finish work may consume buffer instead of making the whole model infeasible.
    productive = bool(config.get('maximize_productive_time'))
    ratio = 1.0 if productive else float(config.get("target_utilization", 0.82))
    min_slack = 0 if productive else int(config.get("min_daily_slack_minutes", 45))
    deep_cap = int(config.get("max_deep_work_minutes", 240))
    daily_caps = {}
    soft_penalties = []
    for dd in range(horizon_days):
        day = (start + timedelta(days=dd)).date()
        ds, de = _usable_bounds(day, config)
        if dd == 0:
            ds = max(ds, start)
        free_min = sum(int((e - s0).total_seconds() // 60) for s0, e in free_windows(ds, de, hard_busy))
        target = max(0, min(free_min, int(free_min * ratio), free_min - min_slack if free_min > min_slack else free_min))
        daily_caps[day.isoformat()] = {"free": free_min, "target": target}
        day_cands = [c for c in all_candidates if ds <= c["start"] < de]
        if day_cands:
            load = sum(c["minutes"] * c["var"] for c in day_cands)
            model.Add(load <= free_min)
            over = model.NewIntVar(0, max(0, free_min), f"over_target_{dd}")
            model.Add(over >= load - target)
            soft_penalties.append(7 * over)
            deep = [c for c in day_cands if tag_energy(c["task"], c["meta"]) == "high"]
            if deep:
                deep_load = sum(c["minutes"] * c["var"] for c in deep)
                deep_over = model.NewIntVar(0, max(0, free_min), f"deep_over_{dd}")
                model.Add(deep_over >= deep_load - deep_cap)
                soft_penalties.append(10 * deep_over)

    # Weekly/category capacity budgets, e.g. {"Math": 600, "Physics": 360}.
    # Match names case-insensitively so `math`, `Math`, and ` MATH ` do not
    # silently become different capacity buckets.
    raw_budgets = config.get("weekly_capacity_minutes") or {}
    budgets = {str(k).strip().casefold(): int(v) for k, v in raw_budgets.items() if str(k).strip()}
    if budgets:
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for c in all_candidates:
            bucket_raw = c["meta"].weekly_bucket or c["meta"].category
            bucket = str(bucket_raw or "").strip().casefold()
            if not bucket or bucket not in budgets:
                continue
            iso = c["start"].isocalendar()
            grouped[(bucket, iso.year, iso.week)].append(c)
        for (bucket, year, week), rows in grouped.items():
            model.Add(sum(c["minutes"] * c["var"] for c in rows) <= budgets[bucket])

    # Location-aware travel transitions. Pairwise incompatibility is only added for close non-overlapping candidates.
    default_travel = int(config.get("default_travel_buffer_minutes", 0))
    travel_constraints = 0
    if default_travel > 0 or any(m.transition_minutes for m in metas.values()):
        ordered = sorted([c for c in all_candidates if c["meta"].location], key=lambda c: c["start"])
        max_transition = max([default_travel] + [m.transition_minutes for m in metas.values()] + [0])
        for i, a in enumerate(ordered):
            for b in ordered[i+1:]:
                if b["start"] - a["end"] > timedelta(minutes=max_transition):
                    break
                if a["task"].id == b["task"].id or a["meta"].location == b["meta"].location:
                    continue
                if overlap(a["start"], a["end"], b["start"], b["end"]):
                    continue  # AddNoOverlap already covers it
                req = max(default_travel, a["meta"].transition_minutes, b["meta"].transition_minutes)
                if 0 <= (b["start"] - a["end"]).total_seconds() / 60 < req:
                    model.Add(a["var"] + b["var"] <= 1)
                    travel_constraints += 1

    # Recovery between cognitively heavy blocks, even when they are different tasks.
    # This is a hard safety gap; users can set it to 0 if they deliberately want
    # back-to-back deep work. Same-task chunks already have their own buffer rule.
    heavy_recovery = max(0, int(config.get("high_energy_recovery_minutes", 15)))
    recovery_constraints = 0
    if heavy_recovery > 0:
        heavy = sorted([c for c in all_candidates if tag_energy(c["task"], c["meta"]) == "high"], key=lambda c: c["start"])
        for i, a in enumerate(heavy):
            for b in heavy[i+1:]:
                max_req = max(
                    _recovery_after_minutes(a["minutes"], a["end"], config),
                    between if a["task"].id == b["task"].id else 0,
                )
                if b["start"] - a["end"] >= timedelta(minutes=max_req):
                    break
                if overlap(a["start"], a["end"], b["start"], b["end"]):
                    continue
                required = max_req
                if 0 <= (b["start"] - a["end"]).total_seconds() / 60 < required:
                    model.Add(a["var"] + b["var"] <= 1)
                    recovery_constraints += 1

    # Soft context-switch cost: preserve flow when two different contexts would
    # otherwise be placed almost back-to-back. It remains soft so deadlines and
    # fixed commitments can override it.
    context_switch_penalties = []
    context_switch_constraints = 0
    switch_window = max(0, int(config.get("context_switch_window_minutes", 30)))
    switch_cost = max(0, int(config.get("context_switch_penalty", 140)))
    max_switch_constraints = max(0, int(config.get("max_context_switch_constraints", 3500)))
    if switch_window > 0 and switch_cost > 0:
        ordered = sorted(all_candidates, key=lambda c: c["start"])
        for i, a in enumerate(ordered):
            if context_switch_constraints >= max_switch_constraints:
                break
            for b in ordered[i+1:]:
                if context_switch_constraints >= max_switch_constraints:
                    break
                if b["start"] < a["end"]:
                    continue
                gap = int((b["start"] - a["end"]).total_seconds() // 60)
                if gap > switch_window:
                    break
                if a["task"].id == b["task"].id or _context_key(a["task"], a["meta"]) == _context_key(b["task"], b["meta"]):
                    continue
                both = model.NewBoolVar(f"switch_{i}_{context_switch_constraints}")
                model.Add(both <= a["var"])
                model.Add(both <= b["var"])
                model.Add(both >= a["var"] + b["var"] - 1)
                scaled = max(1, int(round(switch_cost * (1.0 - 0.45 * gap / max(1, switch_window)))))
                context_switch_penalties.append(scaled * both)
                context_switch_constraints += 1

    objective_terms = []
    started_terms = []
    completion_terms = []
    must_finish_terms = []
    fragmentation_penalties = []
    partial_penalties = []
    coverage_per_minute = max(1, int(config.get("coverage_value_per_minute", 12)))
    if productive:
        coverage_per_minute = max(24, coverage_per_minute)
    placement_weight = max(0.0, float(config.get("placement_utility_weight", 5.0)))
    importance_per_score_minute = max(0.0, float(config.get("importance_value_per_score_minute", 0.055)))
    fragment_cost = max(0, int(config.get("fragmentation_penalty", 120)))
    partial_cost = max(0, int(config.get("partial_task_penalty", 90)))
    for c in all_candidates:
        # Coverage is minute-based rather than chunk-based, removing the old bias
        # where highly fragmented long tasks could win simply by having more chunks.
        task_importance = task_score[c["task"].id]
        placement_only = c["utility"] - task_importance
        coef = max(1, int(round(
            c["minutes"] * (coverage_per_minute + task_importance * importance_per_score_minute)
            + placement_only * placement_weight
        )))
        raw_meta = meta_map.get(c['task'].id, {})
        if raw_meta.get('intent_optional'):
            coef = max(1, int(coef * 0.25))
            if productive:
                # Optional work keeps lower priority, but useful legal progress
                # should beat an empty gap when the user wants productive time.
                # Required work, hard recovery and all bounds still take priority.
                coef = max(coef, int(c['minutes'] * coverage_per_minute * .25))
        elif raw_meta.get('_background_fill'):
            # Background-fill work is not optional: it should use genuinely free
            # capacity. But it must not crowd out ordinary actionable work merely
            # because many project chunks collectively earn more coverage/completion
            # utility. Keep it stronger than optional work but clearly subordinate.
            scale = max(0.10, min(0.80, float(config.get('background_fill_objective_scale', 0.40))))
            coef = max(1, int(coef * scale))
        objective_terms.append(coef * c["var"])
    for t in source_tasks:
        tcs = chunks.get(t.id, [])
        if not tcs:
            continue
        started = model.NewBoolVar(f"started_{t.id}")
        full = model.NewBoolVar(f"full_{t.id}")
        for ch in tcs:
            model.Add(started >= ch["presence"])
        model.Add(started <= sum(ch["presence"] for ch in tcs))
        for ch in tcs:
            model.Add(full <= ch["presence"])
        model.Add(full >= sum(ch["presence"] for ch in tcs) - len(tcs) + 1)
        raw_meta = meta_map.get(t.id, {})
        if raw_meta.get('intent_optional'):
            objective_scale = 0.25
        elif raw_meta.get('_background_fill'):
            objective_scale = max(0.10, min(0.80, float(config.get('background_fill_objective_scale', 0.40))))
        else:
            objective_scale = 1.0
        started_terms.append(int((180 + task_score[t.id] * 4) * objective_scale) * started)
        if meta_map.get(t.id, {}).get('_requested_progress'):
            started_terms.append(max(0, int(config.get('requested_progress_start_bonus', 50000))) * started)
        completion_terms.append(int((500 + task_score[t.id] * 8 + pressure.get(t.id, 0.0) * 10) * objective_scale) * full)
        if fragment_cost:
            for ch in tcs[1:]:
                fragmentation_penalties.append(int(fragment_cost * (objective_scale if productive else 1)) * ch["presence"])
        if partial_cost and len(tcs) > 1:
            partial = model.NewBoolVar(f"partial_{t.id}")
            model.Add(partial <= started)
            model.Add(partial + full <= 1)
            model.Add(partial >= started - full)
            partial_penalties.append(int(partial_cost * (objective_scale if productive else 1)) * partial)
        if t.id in must_finish_vars:
            must_finish_terms.append(int(1_000_000 + task_score[t.id] * 1_000) * must_finish_vars[t.id])
    model.Maximize(
        sum(objective_terms) + sum(started_terms) + sum(completion_terms) + sum(must_finish_terms)
        - sum(soft_penalties) - sum(dependency_penalties) - sum(context_switch_penalties)
        - sum(fragmentation_penalties) - sum(partial_penalties)
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(config.get("solver_time_limit_seconds", 8.0))
    solver.parameters.num_search_workers = int(config.get("solver_workers", 8))
    status = solver.Solve(model)
    diagnostics.update({
        "status": solver.StatusName(status),
        "candidate_count": len(all_candidates),
        "travel_constraints": travel_constraints,
        "recovery_constraints": recovery_constraints,
        "context_switch_constraints": context_switch_constraints,
        "dependency_proximity_penalties": len(dependency_penalties),
        "daily_caps": daily_caps,
        "deadline_pressure": {k: round(v, 2) for k, v in pressure.items()},
        "schedulable_task_count": len(source_tasks),
        "note_count_ignored": sum(1 for t in tasks if t.is_note),
        "objective": solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
    })
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        warnings.append(f"CP-SAT returned {solver.StatusName(status)}; no schedule was committed")
        return [], warnings, diagnostics

    segments: list[Segment] = []
    late_by_task: dict[str, int] = {}
    for t in source_tasks:
        tcs = chunks.get(t.id, [])
        scheduled_count = 0
        for ci, ch in enumerate(tcs):
            chosen = next((c for c in ch["candidates"] if solver.Value(c["var"]) == 1), None)
            if not chosen:
                continue
            scheduled_count += 1
            m = metas[t.id]
            reason = f"P{t.priority} · {tag_energy(t, m)} energy · score {task_score[t.id]:.1f}"
            if m.deadline:
                reason += f" · deadline {m.deadline.strftime('%a %H:%M')}"
                if chosen["end"] > m.deadline:
                    late = int(math.ceil((chosen["end"] - m.deadline).total_seconds() / 60.0))
                    late_by_task[t.id] = max(late_by_task.get(t.id, 0), late)
                    reason += f" · recovery +{late}m"
            if m.location:
                reason += f" · {m.location}"
            segments.append(Segment(
                t.id, t.project_id, t.title, chosen["start"], chosen["end"], task_score[t.id], reason,
                t, ci + 1, len(tcs), m.location, m.category,
            ))
        if scheduled_count < len(tcs):
            missing = sum(ch["minutes"] for ch in tcs[scheduled_count:])
            if metas[t.id].must_finish:
                warnings.append(f"{t.title}: Must finish could not fit atomically; no partial block was scheduled")
            else:
                warnings.append(f"{t.title}: {missing} min could not fit without breaking constraints/capacity")

    for tid, late in late_by_task.items():
        title = next((t.title for t in source_tasks if t.id == tid), tid)
        warnings.append(f"{title}: best recovery plan runs {late} min past its deadline; earlier legal capacity was insufficient or more constrained")

    segments.sort(key=lambda x: x.start)
    diagnostics["capacity"] = _capacity_report(start, horizon_days, hard_busy, segments, config)
    return segments, warnings, diagnostics


def _plan_heuristic(tasks: list[Task], meta_map: dict[str, dict], busy: list[BusyBlock], start: datetime,
                    horizon_days: int, config: dict, mastery_map: dict[str, float]):
    """Dependency-aware compatibility fallback used only when OR-Tools is unavailable."""
    config = dict(config)
    config["_plan_start"] = start.isoformat()
    warnings = ["OR-Tools is not installed; using fallback heuristic. Run: pip install -r requirements.txt"]
    from .plan_duration_requests import budget_intervals
    session_rules = budget_intervals(config)
    hard_busy = _hard_busy(tasks, busy, start, horizon_days, config)
    between = int(config.get("between_chunks_buffer", 10))
    raw_budgets = config.get("weekly_capacity_minutes") or {}
    budgets = {str(k).strip().casefold(): int(v) for k, v in raw_budgets.items() if str(k).strip()}
    budget_used: dict[tuple[str, int, int], int] = defaultdict(int)
    all_active = {
        t.id: t for t in tasks
        if _task_schedulable_for_plan(t, start) and "autoscheduler-session" not in {x.lower() for x in t.tags}
    }

    source_tasks: list[Task] = []
    no_work_left: set[str] = set()
    metas: dict[str, TaskMeta] = {}
    durations: dict[str, int] = {}
    for t in tasks:
        tags = {x.lower() for x in t.tags}
        if (not _task_schedulable_for_plan(t, start)) or "fixed" in tags or "autoscheduler-session" in tags:
            continue
        m = build_meta(t, meta_map.get(t.id, {}))
        if not m.autoschedule:
            continue
        dur = duration_for(t, m)
        if dur is None:
            warnings.append(f"{t.title}: no usable duration")
            continue
        if dur <= 0:
            no_work_left.add(t.id)
            continue
        source_tasks.append(t)
        metas[t.id] = m
        durations[t.id] = dur

    last_day = (start + timedelta(days=max(0, horizon_days - 1))).date()
    _, horizon_end = _usable_bounds(last_day, config)
    critical = _criticality(source_tasks, metas, durations)
    pressure = _deadline_pressure(source_tasks, metas, durations, hard_busy, start, horizon_end, config)
    config["_has_high_energy_demand"] = any(tag_energy(t, metas[t.id]) == "high" for t in source_tasks)
    info: dict[str, tuple[float, Task, TaskMeta, int]] = {}
    for t in source_tasks:
        score = priority_score(
            t, metas[t.id], start, _mastery_for(t, mastery_map), critical.get(t.id, 0.0), config,
            pressure.get(t.id, 0.0),
        )
        raw_meta = meta_map.get(t.id, {})
        if raw_meta.get('intent_optional'):
            score *= 0.25
        elif raw_meta.get('_background_fill'):
            # Keep background projects below ordinary P1+ work even when their
            # roadmap deadline creates artificial urgency.
            scale = max(0.10, min(0.80, float(config.get('background_fill_objective_scale', 0.40))))
            score = min(9.0, score * scale)
        info[t.id] = (score, t, metas[t.id], durations[t.id])

    remaining = dict(info)
    segments: list[Segment] = []
    completed_in_plan: dict[str, datetime] = {}
    progress_in_plan: dict[str, datetime] = {}
    failed: set[str] = set()

    while remaining:
        ready: list[tuple[float, Task, TaskMeta, int, datetime]] = []
        permanently_blocked: list[str] = []
        for tid, (score, t, m, dur) in remaining.items():
            dep_after = max(start, m.earliest) if m.earliest else start
            waiting = False
            blocked = False
            for dep_id in m.dependencies:
                dep_gap = max(0, int((meta_map.get(tid, {}).get("_dependency_gap_minutes") or {}).get(dep_id, between)))
                session_dependency = dep_id in (meta_map.get(tid, {}).get('_session_dependency_ids') or [])
                if dep_id == tid or (dep_id in failed and not (session_dependency and dep_id in progress_in_plan)):
                    blocked = True
                    break
                dep_task = all_active.get(dep_id)
                if dep_task is None or dep_id in no_work_left:
                    continue
                if dep_id in remaining:
                    waiting = True
                    break
                if dep_id in completed_in_plan:
                    dep_after = max(dep_after, completed_in_plan[dep_id] + timedelta(minutes=dep_gap))
                    continue
                if session_dependency and dep_id in progress_in_plan:
                    dep_after = max(dep_after, progress_in_plan[dep_id] + timedelta(minutes=dep_gap))
                    continue
                # Active prerequisite that is fixed/manual/unschedulable.
                if dep_task.end and dep_task.end > start and not dep_task.is_all_day:
                    dep_after = max(dep_after, dep_task.end + timedelta(minutes=dep_gap))
                else:
                    blocked = True
                    break
            if blocked:
                permanently_blocked.append(tid)
            elif not waiting:
                ready.append((score, t, m, dur, dep_after))

        for tid in permanently_blocked:
            _, t, _, _ = remaining.pop(tid)
            failed.add(tid)
            warnings.append(f"{t.title}: waiting for an unfinished/unschedulable prerequisite")

        if not ready:
            if remaining:
                names = ", ".join(row[1].title for row in remaining.values())
                warnings.append(f"Dependency cycle or unresolved prerequisite chain: {names}")
            break

        score, t, m, total, after = max(ready, key=lambda x: x[0])
        remaining.pop(t.id, None)
        from .plan_duration_requests import requested_chunks
        chunks = requested_chunks(t, total, m, config, choose_chunks)
        placed: list[Segment] = []
        local_budget_delta: dict[tuple[str, int, int], int] = defaultdict(int)
        cursor = after
        for ci, minutes in enumerate(chunks):
            best = None
            remaining_after = sum(chunks[ci+1:]) + between * max(0, len(chunks) - ci - 1)
            for dd in range(horizon_days):
                day = (start + timedelta(days=dd)).date()
                if m.allowed_weekdays and day.weekday() not in m.allowed_weekdays:
                    continue
                usable_start, de = _usable_bounds(day, config)
                ds = max(cursor, usable_start)
                de = min([de, m.latest_end or de, m.hard_stop or de])
                initial_end = _ctx_dt(meta_map.get(t.id, {}).get('_initial_latest_end'))
                if ci == 0 and initial_end:
                    de = min(de, initial_end)
                occupied = list(hard_busy)
                current_is_high = tag_energy(t, m) == "high"
                default_travel = max(0, int(config.get("default_travel_buffer_minutes", 0)))
                for x in segments + placed:
                    pad = 0
                    if current_is_high and (x.task_id in info and tag_energy(info[x.task_id][1], info[x.task_id][2]) == "high"):
                        prior_minutes = max(1, int((x.end - x.start).total_seconds() // 60))
                        pad = max(pad, _recovery_after_minutes(prior_minutes, x.end, config))
                    if m.location and x.location and str(m.location).strip().casefold() != str(x.location).strip().casefold():
                        other_transition = info[x.task_id][2].transition_minutes if x.task_id in info else 0
                        pad = max(pad, default_travel, m.transition_minutes, other_transition)
                    occupied.append(BusyBlock(x.start - timedelta(minutes=pad), x.end + timedelta(minutes=pad), x.title, "planned"))
                for fs, fe in free_windows(ds, de, occupied):
                    if ci == 0 and m.exact_start is not None:
                        exact = m.exact_start.astimezone(settings.tz) if m.exact_start.tzinfo else m.exact_start.replace(tzinfo=settings.tz)
                        starts = [exact] if fs <= exact and exact + timedelta(minutes=minutes) <= fe else []
                    else:
                        starts = _candidate_starts(fs, fe, minutes, max(GRID, int(config.get("candidate_step_minutes", 10))))
                    for s0 in starts:
                        e0 = s0 + timedelta(minutes=minutes)
                        over_budget = False
                        for ids, limit, budget_start, budget_end in session_rules:
                            if t.id in ids and budget_start <= s0 < budget_end and m.explicit_activity_minutes is None:
                                used = sum(int((s.end-s.start).total_seconds()//60)
                                           for s in segments + placed
                                           if s.task_id in ids and budget_start <= s.start < budget_end)
                                if used + minutes > limit:
                                    over_budget = True
                                    break
                        if over_budget:
                            continue
                        if not _meal_activity_start_allowed(t.id, s0, config, e0):
                            continue
                        bucket = str(m.weekly_bucket or m.category or "").strip().casefold()
                        if bucket and bucket in budgets:
                            iso = s0.isocalendar(); key = (bucket, iso.year, iso.week)
                            if budget_used[key] + local_budget_delta[key] + minutes > budgets[bucket]:
                                continue
                        # If the task must finish, do not greedily choose a first chunk so late that
                        # the remaining chunks cannot possibly fit in this day's current hard window.
                        if m.must_finish and remaining_after and e0 + timedelta(minutes=remaining_after) > de:
                            continue
                        u = slot_utility(t, m, s0, score, config, minutes, pressure.get(t.id, 0.0))
                        if m.dependencies and after > start:
                            # Prefer follow-through after prerequisites, but keep this soft so
                            # sleep, fixed commitments, deadlines and energy can still win.
                            delay_min = max(0.0, (s0 - after).total_seconds() / 60)
                            u -= delay_min * float(config.get("dependency_proximity_weight", 2.0)) * 0.05
                        if m.must_finish:
                            # Strong early bias for the compatibility engine; CP-SAT handles this globally.
                            u -= (s0 - ds).total_seconds() / 60 * 0.2
                        u -= _stability_penalty(t, s0, start, config)
                        switch_window = max(0, int(config.get("context_switch_window_minutes", 30)))
                        if switch_window:
                            for prev in segments + placed:
                                gap = (s0 - prev.end).total_seconds() / 60.0
                                if 0 <= gap <= switch_window and prev.task_id in info:
                                    pm = info[prev.task_id][2]
                                    pt = info[prev.task_id][1]
                                    if _context_key(pt, pm) != _context_key(t, m):
                                        u -= float(config.get("context_switch_utility_penalty", 10.0)) * (1.0 - 0.5 * gap / max(1, switch_window))
                        if best is None or u > best[0]:
                            best = (u, s0, e0)
            if not best:
                warnings.append(f"{t.title}: could not fit all work")
                break
            _, s0, e0 = best
            placed.append(Segment(t.id, t.project_id, t.title, s0, e0, score, "fallback heuristic", t, ci+1, len(chunks), m.location, m.category))
            bucket = str(m.weekly_bucket or m.category or "").strip().casefold()
            if bucket and bucket in budgets:
                iso = s0.isocalendar(); local_budget_delta[(bucket, iso.year, iso.week)] += minutes
            cursor = e0 + timedelta(minutes=between)

        complete = len(placed) == len(chunks) and bool(placed)
        if m.must_finish and not complete:
            placed = []
            local_budget_delta.clear()
            warnings.append(f"{t.title}: Must finish could not be satisfied by fallback constraints; no partial block was scheduled")
        else:
            for key, used in local_budget_delta.items():
                budget_used[key] += used

        segments.extend(placed)
        if placed:
            progress_in_plan[t.id] = placed[-1].end
        if complete:
            completed_in_plan[t.id] = placed[-1].end
        else:
            failed.add(t.id)

    late_by_task: dict[str, int] = {}
    for seg in segments:
        m = metas.get(seg.task_id)
        if m and m.deadline and seg.end > m.deadline:
            late = int(math.ceil((seg.end - m.deadline).total_seconds() / 60.0))
            late_by_task[seg.task_id] = max(late_by_task.get(seg.task_id, 0), late)
    for tid, late in late_by_task.items():
        title = info[tid][1].title if tid in info else tid
        warnings.append(f"{title}: best recovery plan runs {late} min past its deadline; earlier legal capacity was insufficient or more constrained")

    segments.sort(key=lambda x: x.start)
    return segments, warnings, {
        "engine": "heuristic-fallback",
        "ortools": False,
        "optimizer": "smart-v8.2",
        "schedulable_task_count": len(info),
        "note_count_ignored": sum(1 for t in tasks if t.is_note),
        "deadline_pressure": {k: round(v, 2) for k, v in pressure.items()},
        "capacity": _capacity_report(start, horizon_days, hard_busy, segments, config),
    }


def _remaining_work(tasks, meta_map, segments):
    from .session_titles import task_base_title
    rows = []
    for task in tasks:
        tags = {x.lower() for x in task.tags}
        meta = build_meta(task, meta_map.get(task.id, {}))
        if (not task.is_actionable or task.status != 0 or not meta.autoschedule
                or tags & {'fixed', 'autoscheduler-session'}):
            continue
        total = duration_for(task, meta)
        if total is None or total <= 0:
            continue
        planned = sum(int((s.end-s.start).total_seconds()/60) for s in segments if s.task_id == task.id)
        if planned >= total:
            continue
        rows.append({'task_id': task.id, 'title': task_base_title(task), 'estimated_minutes': total,
                     'planned_minutes': planned, 'remaining_minutes': total-planned,
                     'min_session_minutes': min(total-planned, meta.min_chunk) if meta.splittable else total-planned,
                     'splittable': meta.splittable, 'must_finish': meta.must_finish})
    return rows


def _dependents_cap(task, meta_map, tasks, segments, config):
    """Latest moment more of `task` may still run: before any scheduled dependent starts.

    A prerequisite's remaining effort placed after a dependent has begun breaks the
    dependency order shown to the user. Session-level dependents (which only need one
    session of this task) do not cap the remainder. Returns (cap, dependent_title) or (None, None).
    """
    between = int(config.get('between_chunks_buffer', 10))
    cap, title = None, None
    for other in tasks:
        if other.id == task.id:
            continue
        raw = meta_map.get(other.id, {}) or {}
        if task.id not in list(raw.get('dependencies') or []):
            continue
        if task.id in (raw.get('_session_dependency_ids') or []):
            continue
        theirs = [s for s in segments if s.task_id == other.id]
        if not theirs:
            continue
        gap = max(0, int((raw.get('_dependency_gap_minutes') or {}).get(task.id, between)))
        bound = min(s.start for s in theirs) - timedelta(minutes=gap)
        if cap is None or bound < cap:
            cap, title = bound, other.title
    return cap, title


def _free_task_windows(task, meta_map, tasks, busy, start, horizon_days, config, segments):
    """Remaining-work windows with real bounds, prerequisites and recovery applied."""
    from .interactive_quality_speed_patch import _session_budget_remaining
    meta = build_meta(task, meta_map.get(task.id, {}))
    own = [s for s in segments if s.task_id == task.id]
    earliest = max(start, meta.earliest or start)
    between = int(config.get('between_chunks_buffer', 10))
    raw_self = meta_map.get(task.id, {}) or {}
    # Remaining effort is interchangeable with the sessions already placed, so it may run
    # BEFORE them too (own sessions are padded by the between-session buffer below).
    # Forcing it after the last own session made every earlier idle hour look
    # "constrained" when nothing but session order stood in the way. The old rule is kept
    # only when the FIRST session carries its own constraint (exact start / first-session
    # deadline), because an earlier remainder would become the first session.
    first_session_bound = bool(getattr(meta, 'exact_start', None) is not None or raw_self.get('_initial_latest_end'))
    if own and first_session_bound:
        earliest = max(earliest, max(s.end for s in own) + timedelta(minutes=between))
    dependents_cap, _ = _dependents_cap(task, meta_map, tasks, segments, config)
    active = {t.id: t for t in tasks if t.is_actionable and t.status == 0}
    for dep_id in meta.dependencies:
        dep = active.get(dep_id)
        if not dep:
            continue
        chosen = [s for s in segments if s.task_id == dep_id]
        session_dep = dep_id in (meta_map.get(task.id, {}).get('_session_dependency_ids') or [])
        needed = duration_for(dep, build_meta(dep, meta_map.get(dep_id, {})))
        planned = sum(int((s.end-s.start).total_seconds()/60) for s in chosen)
        if chosen and (session_dep or (needed is not None and planned >= needed)):
            end = max(s.end for s in chosen)
        elif (not chosen and dep.end and dep.end > start and not dep.is_all_day
              and (not build_meta(dep, meta_map.get(dep_id, {})).autoschedule
                   or 'fixed' in {x.lower() for x in dep.tags} or needed is None)):
            end = dep.end
        else:
            return []
        gap = max(0, int((meta_map.get(task.id, {}).get('_dependency_gap_minutes') or {}).get(dep_id, config.get('between_chunks_buffer', 10))))
        earliest = max(earliest, end + timedelta(minutes=gap))
    occupied = list(_hard_busy(tasks, busy, start, horizon_days, config))
    high = tag_energy(task, meta) == 'high'
    for segment in segments:
        other = build_meta(segment.source_task, meta_map.get(segment.task_id, {}))
        pad = 0
        if high and tag_energy(segment.source_task, other) == 'high':
            pad = _recovery_after_minutes(int((segment.end-segment.start).total_seconds()/60), segment.end, config)
        if segment.task_id == task.id:
            pad = max(pad, between)
        if meta.location and segment.location and meta.location.casefold() != segment.location.casefold():
            pad = max(pad, meta.transition_minutes, other.transition_minutes, int(config.get('default_travel_buffer_minutes', 0)))
        occupied.append(BusyBlock(segment.start-timedelta(minutes=pad), segment.end+timedelta(minutes=pad), segment.title, 'planned'))
    for row in (config.get('_task_exclusion_windows') or {}).get(task.id, []):
        occupied.append(BusyBlock(_ctx_dt(row['start']), _ctx_dt(row['end']), 'Excluded occurrence', 'excluded'))
    out = []
    initial_end = _ctx_dt(meta_map.get(task.id, {}).get('_initial_latest_end')) if not own else None
    exact_first = meta.exact_start if meta.exact_start is not None and not own else None
    if exact_first is not None:
        exact_first = exact_first.astimezone(settings.tz) if exact_first.tzinfo else exact_first.replace(tzinfo=settings.tz)
    for dd in range(horizon_days):
        day = (start + timedelta(days=dd)).date()
        if exact_first is not None and day != exact_first.date():
            continue
        if meta.allowed_weekdays and day.weekday() not in meta.allowed_weekdays:
            continue
        lower, upper = _usable_bounds(day, config)
        lower = max(lower, earliest)
        upper = min(upper, meta.latest_end or upper, meta.hard_stop or upper, initial_end or upper, dependents_cap or upper)
        for a, b in free_windows(lower, upper, occupied) if lower < upper else []:
            if exact_first is not None:
                candidates = [exact_first] if a <= exact_first and exact_first + timedelta(minutes=meta.min_chunk) <= b else []
            else:
                candidates = _candidate_starts(a, b, meta.min_chunk, GRID)
            first = next((s for s in candidates if _meal_activity_start_allowed(task.id, s, config, s+timedelta(minutes=meta.min_chunk))), None)
            if first:
                bucket = str(meta.weekly_bucket or meta.category or '').strip().casefold()
                budgets = {str(k).strip().casefold(): int(v) for k, v in (config.get('weekly_capacity_minutes') or {}).items()}
                if bucket in budgets:
                    week = first.isocalendar()[:2]
                    used = sum(int((s.end-s.start).total_seconds()/60) for s in segments
                               if s.start.isocalendar()[:2] == week
                               and str(build_meta(s.source_task, meta_map.get(s.task_id, {})).weekly_bucket
                                       or build_meta(s.source_task, meta_map.get(s.task_id, {})).category or '').strip().casefold() == bucket)
                    b = min(b, first+timedelta(minutes=max(0, budgets[bucket]-used)))
                # Diagnostics and last-mile placement must share the same daily
                # allowance. An open clock interval is not permission to exceed it.
                daily_left = _session_budget_remaining(task.id, day, segments, config)
                if daily_left is not None:
                    b = min(b, first + timedelta(minutes=daily_left))
                if b-first >= timedelta(minutes=meta.min_chunk):
                    out.append((first, b))
    return out


def _adapt_gap_sessions(engine, tasks, meta_map, busy, start, horizon_days, config, mastery_map, result):
    """One bounded re-solve; shorten ceilings, preserve chosen work and hard rules."""
    segments, warnings, diagnostics = result
    adapted = {tid: dict(raw) for tid, raw in meta_map.items()}
    changes = []
    unfinished = {r['task_id']: r for r in _remaining_work(tasks, meta_map, segments)}
    missing_progress = any(raw.get('_requested_progress') and not any(s.task_id == tid for s in segments)
                           for tid, raw in meta_map.items() if tid in unfinished)
    for task in tasks:
        row = unfinished.get(task.id)
        meta = build_meta(task, meta_map.get(task.id, {}))
        share = missing_progress and meta_map.get(task.id, {}).get('_requested_progress')
        if not row and share:
            total = duration_for(task, meta)
            row = {'estimated_minutes': total, 'remaining_minutes': 0} if total else None
        if not row or not meta.splittable or meta.must_finish or task.repeat_flag:
            continue
        chunks = choose_chunks(row['estimated_minutes'], meta)
        count = sum(s.task_id == task.id for s in segments)
        next_minutes = chunks[count] if count < len(chunks) else row['remaining_minutes']
        windows = _free_task_windows(task, meta_map, tasks, busy, start, horizon_days, config, segments)
        cap = max([int((b-a).total_seconds()/60)//GRID*GRID for a, b in windows] or [0])
        if (meta.min_chunk <= cap < next_minutes) or (share and meta.max_chunk > meta.min_chunk):
            ceiling = meta.min_chunk if share else min(meta.max_chunk, cap)
            adapted.setdefault(task.id, {})['max_chunk'] = ceiling
            changes.append({'task_id': task.id, 'max_session_minutes': ceiling})
    if changes:
        retry_config = dict(config) | {'solver_time_limit_seconds': min(2.0, float(config.get('solver_time_limit_seconds', 8)))}
        retry = engine(tasks, adapted, busy, start, horizon_days, retry_config, mastery_map)
        before = {s.task_id: 0 for s in segments}
        after = {s.task_id: 0 for s in retry[0]}
        for segment in segments:
            before[segment.task_id] += int((segment.end-segment.start).total_seconds()/60)
        for segment in retry[0]:
            after[segment.task_id] += int((segment.end-segment.start).total_seconds()/60)
        more_requested = any(raw.get('_requested_progress') and after.get(tid, 0) and not before.get(tid, 0)
                             for tid, raw in meta_map.items())
        preserved = all(after.get(tid, 0) >= minutes for tid, minutes in before.items()
                        if not (more_requested and meta_map.get(tid, {}).get('intent_optional')))
        accepted = preserved and (sum(after.values()) > sum(before.values()) or more_requested)
        if accepted:
            segments, warnings, diagnostics = retry
            meta_map = adapted
        diagnostics['gap_session_adaptation'] = {'changes': changes, 'accepted': accepted,
            'additional_minutes': sum(after.values())-sum(before.values()) if accepted else 0}
    diagnostics['unfinished_work'] = _remaining_work(tasks, meta_map, segments)
    for row in diagnostics['unfinished_work']:
        task = next(t for t in tasks if t.id == row['task_id'])
        row['legal_windows'] = [{'start': a.isoformat(), 'end': b.isoformat()}
            for a, b in _free_task_windows(task, meta_map, tasks, busy, start, horizon_days, config, segments)]
    return segments, warnings, diagnostics


def plan(tasks: list[Task], meta_map: dict[str, dict], busy: list[BusyBlock], start: datetime, horizon_days: int,
         config: dict, mastery_map: dict[str, float] | None = None):
    mastery_map = mastery_map or {}
    start = _aware(start, convert=True)
    busy = [BusyBlock(_aware(b.start, convert=True), _aware(b.end, convert=True), b.label, b.source) for b in busy]

    # IDs key the optimizer's internal tables. Collapse stale duplicate snapshots
    # deterministically, preferring an actionable copy over a non-actionable one.
    unique: dict[str, Task] = {}
    order: list[str] = []
    duplicate_ids: list[str] = []
    for task in tasks:
        if task.id not in unique:
            unique[task.id] = task
            order.append(task.id)
            continue
        duplicate_ids.append(task.id)
        current = unique[task.id]
        current_rank = (int(current.is_actionable), int(not current.is_note), int(bool(current.start and current.end)))
        candidate_rank = (int(task.is_actionable), int(not task.is_note), int(bool(task.start and task.end)))
        if candidate_rank > current_rank:
            unique[task.id] = task
    tasks = [unique[task_id] for task_id in order]

    engine = _plan_cpsat if ORTOOLS_AVAILABLE else _plan_heuristic
    result = engine(tasks, meta_map, busy, start, horizon_days, config, mastery_map)

    # UNKNOWN means the time-limited solver produced no trustworthy solution.
    # Fall back to the dependency-aware heuristic. FEASIBLE/OPTIMAL results are
    # never replaced merely because another engine can fill more minutes.
    if engine is _plan_cpsat and str((result[2] or {}).get("status") or "").upper() == "UNKNOWN":
        cp_warnings = list(result[1] or [])
        fallback = _plan_heuristic(tasks, meta_map, busy, start, horizon_days, config, mastery_map)
        fallback_warnings = [
            w for w in (fallback[1] or [])
            if not w.startswith("OR-Tools is not installed")
        ]
        diagnostics = dict(fallback[2] or {})
        diagnostics["fallback_from"] = "cp-sat:UNKNOWN"
        diagnostics["cp_sat_warnings"] = cp_warnings
        result = (
            fallback[0],
            [*fallback_warnings, "CP-SAT timed out without a usable solution; used the heuristic planner instead"],
            diagnostics,
        )
        engine = _plan_heuristic
    if config.get('maximize_productive_time'):
        # First solve normally to preserve stability. If the resulting plan still
        # leaves a genuinely large awake-time gap, run one short compaction solve
        # that removes only the anti-churn preference. This lets flexible existing
        # TickTick work move into large holes instead of being treated as immovable.
        # We accept the compacted plan only when every task keeps at least as much
        # scheduled work as before and the largest legal gap actually shrinks.
        base_segments = list(result[0] or [])
        def _largest_productive_gap(rows):
            hard = _hard_busy(tasks, busy, start, horizon_days, config)
            occupied = list(hard)
            occupied.extend(BusyBlock(s.start, s.end, s.title, "planned") for s in rows)
            largest = 0
            for dd in range(horizon_days):
                day = (start + timedelta(days=dd)).date()
                ds, de = _usable_bounds(day, config)
                if dd == 0:
                    ds = max(ds, start)
                for a, b in free_windows(ds, de, occupied):
                    largest = max(largest, int((b-a).total_seconds() // 60))
            return largest
        largest_before = _largest_productive_gap(base_segments)
        if largest_before >= 45 and base_segments:
            compact_cfg = dict(config)
            compact_cfg["_compact_flexible_schedule"] = True
            compact_cfg["solver_time_limit_seconds"] = min(
                2.5, float(config.get("solver_time_limit_seconds", 8))
            )
            compact = engine(tasks, meta_map, busy, start, horizon_days, compact_cfg, mastery_map)
            before_by = defaultdict(int)
            after_by = defaultdict(int)
            for s in base_segments:
                before_by[s.task_id] += int((s.end-s.start).total_seconds() // 60)
            for s in compact[0] or []:
                after_by[s.task_id] += int((s.end-s.start).total_seconds() // 60)
            preserved = all(after_by[tid] >= minutes for tid, minutes in before_by.items())
            largest_after = _largest_productive_gap(compact[0] or [])
            if preserved and largest_after + 5 < largest_before:
                compact_diagnostics = dict(compact[2] or {})
                compact_diagnostics["schedule_compaction"] = {
                    "applied": True,
                    "largest_gap_before_minutes": largest_before,
                    "largest_gap_after_minutes": largest_after,
                }
                result = (compact[0], compact[1], compact_diagnostics)
            else:
                diagnostics = dict(result[2] or {})
                diagnostics["schedule_compaction"] = {
                    "applied": False,
                    "largest_gap_before_minutes": largest_before,
                    "largest_gap_after_minutes": largest_after,
                }
                result = (result[0], result[1], diagnostics)
        result = _adapt_gap_sessions(engine, tasks, meta_map, busy, start, horizon_days, config, mastery_map, result)
    else:
        result[2]['unfinished_work'] = _remaining_work(tasks, meta_map, result[0])
        for row in result[2]['unfinished_work']:
            task = next(t for t in tasks if t.id == row['task_id'])
            row['legal_windows'] = [{'start': a.isoformat(), 'end': b.isoformat()}
                for a, b in _free_task_windows(task, meta_map, tasks, busy, start, horizon_days, config, result[0])]
    segments, warnings, diagnostics = result
    untimed_fixed = [
        t.title for t in tasks
        if t.is_actionable and t.status == 0 and "fixed" in {x.lower() for x in t.tags}
        and not t.is_all_day and not (t.start and t.end)
    ]
    if untimed_fixed:
        warnings = [*warnings, *[f"{title}: #fixed is protected but has no complete TickTick time, so it blocks no interval" for title in untimed_fixed]]
    if duplicate_ids:
        warnings = [
            *warnings,
            "Duplicate task id(s) were collapsed before planning: " + ", ".join(sorted(set(duplicate_ids))),
        ]
    return segments, warnings, diagnostics


def schedule_shock(delay_minutes: int) -> str:
    if delay_minutes < 15:
        return "absorb"
    if delay_minutes <= 45:
        return "shift"
    if delay_minutes <= 120:
        return "reoptimize"
    return "rebuild"
