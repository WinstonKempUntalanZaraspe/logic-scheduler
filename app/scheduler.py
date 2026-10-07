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
    """Return `dt` as an aware datetime in the planner's timezone.

    Naive values are assumed to be local (settings.tz). With convert=True an aware
    value is also converted, which matters for the planning start: every calendar
    day, sleep window and meal is built in settings.tz, so taking `.date()` of a
    UTC start can land on the wrong local day.
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
        hrs_left = (meta.deadline - now).total_seconds() / 3600
        hrs = max(0.25, hrs_left)
        score += min(90, 150 / hrs)
        if meta.deadline <= now + timedelta(hours=24):
            score += 20
        if hrs_left < 0:
            # The 150/hrs term saturates at ~1.7h, so without this an item that is
            # days overdue ties with one due in 30 minutes.
            score += min(30.0, 10.0 + (-hrs_left) / 12.0)
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
        return _aware(datetime.fromisoformat(v)) if v else None

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
    # The look-ahead above cannot always keep the tail >= min_chunk (e.g. 50 min with
    # min 30 / max 45 gave [30, 20]). Borrow from earlier chunks; if that is impossible,
    # merge the tail back, accepting a small overshoot of max_chunk instead of a fragment.
    deficit = meta.min_chunk - chunks[-1]
    if len(chunks) > 1 and deficit > 0:
        for i in range(len(chunks) - 1):
            give = min(deficit, chunks[i] - meta.min_chunk)
            if give > 0:
                chunks[i] -= give
                chunks[-1] += give
                deficit -= give
            if deficit <= 0:
                break
        if deficit > 0:
            tail = chunks.pop()
            chunks[-1] += tail
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
    no_work_left: set[str] = set()  # remaining effort == 0 => prerequisite is effectively done
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

    # Precedence ("B starts >= A ends + gap") is encoded as ONE linear constraint over the
    # chosen candidate instead of one conflict clause per (A, B) candidate pair. The two
    # are equivalent, but the pairwise form is O(|A|*|B|) per link (up to 80x80) and made
    # CP-SAT return UNKNOWN on ~25-task plans.
    def _minute_expr(rows, key):
        return sum(int((r[key] - start).total_seconds() // 60) * r["var"] for r in rows)

    def _add_precedence(first_rows, second_rows, gap_minutes, enforce_literals):
        if first_rows and second_rows:
            model.Add(_minute_expr(second_rows, "start") >= _minute_expr(first_rows, "end") + gap_minutes
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
            _add_precedence(tcs[i-1]["candidates"], tcs[i]["candidates"], between, tcs[i]["presence"])
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