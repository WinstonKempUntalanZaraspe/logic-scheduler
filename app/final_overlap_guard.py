from __future__ import annotations

"""Final human-timeline overlap and current-time guard.

The optimizer already prevents ordinary task overlap. Real-life rows such as a current
meal, inferred travel, changing, showers and fixed commitments are assembled by later
layers, though, so they need one last physical-world invariant:

    one person cannot occupy two non-check-in blocks at once.

There is also one absolute temporal invariant:

    newly generated flexible work for today can never start before the planning clock.

Elapsed time is injected as unavailable capacity before the wrapped planner runs, so the
solver and every downstream scheduling layer see the same "now" boundary. If a later
wrapper nevertheless manufactures a segment in the past, the final output boundary holds
that segment rather than exposing or committing an impossible schedule.

This guard never moves a fixed commitment and never fabricates shorter explicit travel.
It may trim/shift *planning estimates* around fixed logistics, and if an explicit reality
block makes required logistics impossible it reports the conflict instead of drawing two
simultaneous blocks. Task segments that somehow collide with the final physical timeline
are conservatively held rather than displayed as executable work.
"""

from copy import deepcopy
from datetime import datetime, timedelta

from .config import settings
from .models import BusyBlock
from . import scheduler

_MINUTE = timedelta(minutes=1)


def _dt(value):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=settings.tz) if parsed.tzinfo is None else parsed.astimezone(settings.tz)
    except (TypeError, ValueError):
        return None


def _interval(row):
    return _dt(row.get("start")), _dt(row.get("end"))


def _overlaps(a, b):
    a0, a1 = _interval(a)
    b0, b1 = _interval(b)
    return bool(a0 and a1 and b0 and b1 and a0 < b1 and b0 < a1)


def _checkin(row):
    kind = str(row.get("kind") or "").lower()
    label = str(row.get("label") or row.get("name") or "").lower()
    return kind in {"checkin", "check-in"} or "check-in" in label or "check in" in label


def _fixed_owner_ids(diagnostics):
    return {str(row.get("owner_id")) for row in diagnostics.get("fixed_timeline") or [] if row.get("owner_id")}


def _fixed_logistics(row, owner_ids):
    owner = str(row.get("owner_id") or "")
    kind = str(row.get("kind") or "").lower()
    source = str(row.get("source") or "").lower()
    return bool(owner and owner in owner_ids and (kind == "logistics" or "logistics" in source))


def _row_label(row):
    return str(row.get("label") or row.get("name") or "Personal time")


def _sorted(rows):
    return sorted(rows, key=lambda row: (_dt(row.get("start")) or datetime.max.replace(tzinfo=settings.tz),
                                         _dt(row.get("end")) or datetime.max.replace(tzinfo=settings.tz),
                                         _row_label(row)))


def _first_overlap(row, rows, *, ignore_owner=None):
    for other in rows:
        if ignore_owner and str(other.get("owner_id") or "") == str(ignore_owner):
            continue
        if not _checkin(other) and _overlaps(row, other):
            return other
    return None


def _find_gap(start, duration, hard_rows, *, day):
    """Find the first same-day slot after start that preserves the estimate duration."""
    cursor = start
    limit = datetime.combine(day + timedelta(days=1), datetime.min.time(), settings.tz)
    for hard in _sorted(hard_rows):
        h0, h1 = _interval(hard)
        if not h0 or not h1 or h1 <= cursor or h0.date() != day:
            continue
        if cursor + duration <= h0:
            return cursor, cursor + duration
        if cursor < h1:
            cursor = h1
        if cursor + duration > limit:
            return None
    return (cursor, cursor + duration) if cursor + duration <= limit else None


def _logical_awake_start(plan_start: datetime, config: dict) -> datetime | None:
    """Return the awake-day boundary containing ``plan_start`` when one exists."""
    plan_start = _dt(plan_start)
    if not plan_start:
        return None
    today_start, today_end = scheduler._usable_bounds(plan_start.date(), config or {})
    if today_start <= plan_start < today_end:
        return today_start
    # Bedtimes after midnight make the first hours of a calendar date part of the
    # previous logical day. Respect that rather than blocking from a future wake time.
    prev_start, prev_end = scheduler._usable_bounds(plan_start.date() - timedelta(days=1), config or {})
    if prev_start <= plan_start < prev_end:
        return prev_start
    return None


def _elapsed_time_block(plan_start: datetime, config: dict) -> BusyBlock | None:
    """Represent elapsed awake time as unavailable capacity for today's planner."""
    plan_start = _dt(plan_start)
    awake_start = _logical_awake_start(plan_start, config)
    if not plan_start or not awake_start or awake_start >= plan_start:
        return None
    return BusyBlock(awake_start, plan_start, "Elapsed time before current planning clock", "current-time-boundary")


def reconcile_timeline(segments, warnings, diagnostics, plan_start):
    diagnostics = deepcopy(diagnostics or {})
    warnings = list(warnings or [])
    plan_start = _dt(plan_start) or datetime.now(settings.tz)
    fixed = _sorted(deepcopy(diagnostics.get("fixed_timeline") or []))
    reality = _sorted(deepcopy(diagnostics.get("reality_timeline") or []))
    owner_ids = _fixed_owner_ids(diagnostics)

    fixed_logistics = [row for row in reality if _fixed_logistics(row, owner_ids)]
    ordinary = [row for row in reality if row not in fixed_logistics]

    # Keep only physically possible fixed-logistics rows. If two fixed commitments make
    # their required travel mutually impossible, expose the conflict instead of drawing
    # overlapping travel/care blocks or silently shortening a saved commute.
    accepted_logistics = []
    for row in fixed_logistics:
        owner = row.get("owner_id")
        collision = _first_overlap(row, fixed, ignore_owner=owner) or _first_overlap(row, accepted_logistics)
        if collision:
            warnings.append(
                f"Cannot fit {_row_label(row)} without overlapping {_row_label(collision)}. "
                "Fixed commitments were left unchanged; review the conflicting commitment or travel requirement."
            )
            continue
        accepted_logistics.append(row)

    hard = _sorted([*fixed, *accepted_logistics])
    accepted_reality = []
    for row in ordinary:
        if _checkin(row):
            accepted_reality.append(row)
            continue
        start, end = _interval(row)
        if not start or not end or end <= start:
            continue
        estimate = bool(row.get("planning_estimate"))
        collisions = [hard_row for hard_row in hard if _overlaps(row, hard_row)]
        if not collisions:
            # Also prevent two ordinary physical blocks from being rendered together.
            prior = _first_overlap(row, accepted_reality)
            if not prior:
                accepted_reality.append(row)
                continue
            collisions = [prior]

        if not estimate:
            # Explicit/current reality wins over inferred logistics. Remove only the
            # conflicting generated logistics row; fixed commitments themselves remain.
            generated_hits = [hit for hit in collisions if hit in accepted_logistics]
            if generated_hits:
                for hit in generated_hits:
                    if hit in accepted_logistics:
                        accepted_logistics.remove(hit)
                    if hit in hard:
                        hard.remove(hit)
                    warnings.append(
                        f"{_row_label(hit)} cannot occur while {_row_label(row)} is active. "
                        "The explicit reality block was preserved and the generated logistics row was withheld."
                    )
                # Re-evaluate after removing impossible generated geometry.
                if not _first_overlap(row, [*fixed, *accepted_reality]):
                    accepted_reality.append(row)
                    continue
            accepted_reality.append(row)
            continue

        duration = end - start
        active_now = start <= plan_start < end
        next_hard = min((hit for hit in collisions if _interval(hit)[0] and _interval(hit)[0] >= start),
                        key=lambda hit: _interval(hit)[0], default=None)

        # Current estimated care/meal may finish earlier when a hard departure is due.
        # We only alter the *estimate*, never an explicit duration.
        if active_now and next_hard:
            cutoff = _interval(next_hard)[0]
            if cutoff and cutoff > start:
                row["end"] = cutoff.isoformat()
                row["planning_adjustment"] = "trimmed-before-hard-block"
                warnings.append(
                    f"Shortened the planning estimate for {_row_label(row)} to end at {cutoff.strftime('%H:%M')} "
                    f"so it does not overlap {_row_label(next_hard)}."
                )
                if _dt(row["end"]) - start >= _MINUTE:
                    accepted_reality.append(row)
                continue

        # Future estimated care/meal can move after a hard block while preserving its
        # full estimated duration. Search the remaining same-day hard timeline.
        anchor = max([_interval(hit)[1] for hit in collisions if _interval(hit)[1]] or [start])
        slot = _find_gap(anchor, duration, hard, day=start.date())
        if slot:
            row["start"], row["end"] = slot[0].isoformat(), slot[1].isoformat()
            row["planning_adjustment"] = "shifted-around-hard-block"
            warnings.append(
                f"Moved the planning estimate for {_row_label(row)} to {slot[0].strftime('%H:%M')}–{slot[1].strftime('%H:%M')} "
                "to keep the day physically non-overlapping."
            )
            if not _first_overlap(row, accepted_reality):
                accepted_reality.append(row)
            continue

        warnings.append(
            f"Could not place the planning estimate for {_row_label(row)} without an overlap; it was withheld from the executable timeline."
        )

    final_reality = _sorted([*accepted_reality, *accepted_logistics])

    # Last boundary for actual task work. A task that collides with fixed/reality
    # geometry is held; no generated schedule may ask the user to do both simultaneously.
    # Past generated work is also impossible: unlike historical fixed/reality rows, it
    # must never be exposed as something the user can still execute.
    blockers = _sorted([*fixed, *final_reality])
    safe_segments = []
    held_past = 0
    for seg in segments or []:
        if seg.start < plan_start:
            held_past += 1
            warnings.append(
                f"Held {seg.title} because it starts before the current planning time "
                f"({plan_start.strftime('%H:%M')})."
            )
            continue
        row = {"start": seg.start.isoformat(), "end": seg.end.isoformat(), "label": seg.title, "kind": "task"}
        hit = _first_overlap(row, blockers)
        if hit:
            warnings.append(f"Held {seg.title} because it overlaps {_row_label(hit)} in the final physical timeline.")
            continue
        previous = safe_segments[-1] if safe_segments else None
        if previous and seg.start < previous.end:
            warnings.append(f"Held {seg.title} because it overlaps {previous.title} in the final task timeline.")
            continue
        safe_segments.append(seg)

    diagnostics["reality_timeline"] = final_reality
    diagnostics["current_time_boundary"] = {
        "active": True,
        "planning_time": plan_start.isoformat(),
        "held_past_task_segments": held_past,
    }
    diagnostics["overlap_guard"] = {
        "active": True,
        "fixed_rows": len(fixed),
        "reality_rows": len(final_reality),
        "held_task_segments": max(0, len(segments or []) - len(safe_segments)),
        "held_past_task_segments": held_past,
    }
    return safe_segments, list(dict.fromkeys(warnings)), diagnostics


def install_final_overlap_guard(base_plan):
    def guarded(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        plan_start = _dt(start) or datetime.now(settings.tz)
        guarded_busy = list(busy or [])
        elapsed = _elapsed_time_block(plan_start, config or {})
        if elapsed:
            guarded_busy.append(elapsed)
        segments, warnings, diagnostics = base_plan(
            tasks, meta_map, guarded_busy, plan_start, horizon_days, config, mastery_map or {}
        )
        return reconcile_timeline(segments, warnings, diagnostics, plan_start)
    return guarded


__all__ = [
    "install_final_overlap_guard", "reconcile_timeline", "_elapsed_time_block",
]
