from __future__ import annotations

"""Reality guard for physical-world scheduling.

The optimizer is good at time/capacity, but raw task rows do not always encode the
physical fact that an outing is one continuous excursion.  This layer keeps support
steps (travel/change/shower/go-home/buffers) attached to the activity they belong to
instead of letting unrelated work appear between them.

It is intentionally conservative:
- explicit user metadata always wins;
- #fixed tasks remain authoritative;
- orphan logistics are held rather than floated around the day;
- flexible outings are scheduled as one contiguous bundle, then expanded back into
  the original TickTick tasks before commit;
- fixed outings anchor their support tasks immediately around the fixed event.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Iterable

from . import scheduler as _sch
from .models import Segment, Task


_BASE_PLAN = _sch.plan


_FAMILY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pool", ("swim", "swimming", "pool")),
    ("table-tennis", ("table tennis", "ping pong", "ping-pong")),
    ("gym", ("gym", "workout", "weights", "strength training")),
    ("badminton", ("badminton",)),
    ("football", ("football", "soccer")),
    ("run", ("run", "running", "jog", "jogging")),
    ("training", ("training", "practice")),
)

_SUPPORT_HINTS = (
    "travel to ", "commute to ", "go home", "travel home", "commute home",
    "change at ", "get changed at ", "shower and change", "shower & change",
    "shower", "warm up", "warm-up", "cool down", "cool-down",
    "transition buffer", "overrun buffer", "overrun / transition buffer",
)

_PRE_HINTS = (
    "travel to ", "commute to ", "change at ", "get changed at ",
    "warm up", "warm-up",
)
_POST_HINTS = (
    "go home", "travel home", "commute home", "shower", "cool down", "cool-down",
)

_DEFAULT_SUPPORT_MINUTES = {
    "travel": 25,
    "change": 15,
    "shower": 20,
    "home": 25,
    "buffer": 10,
    "warmup": 10,
    "cooldown": 10,
}


@dataclass
class BundlePart:
    task: Task
    minutes: int
    role: str
    location: str | None


@dataclass
class OutingBundle:
    primary: Task
    family: str
    parts: list[BundlePart]
    original_main_minutes: int

    @property
    def total_minutes(self) -> int:
        return sum(max(0, int(x.minutes)) for x in self.parts)


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _tags(task: Task) -> set[str]:
    return {_norm(x) for x in (task.tags or [])}


def _is_fixed(task: Task) -> bool:
    return "fixed" in _tags(task)


def _is_support(task: Task) -> bool:
    title = _norm(task.title)
    return any(x in title for x in _SUPPORT_HINTS)


def _explicit_location(raw: dict) -> str | None:
    value = str((raw or {}).get("location") or "").strip()
    return value or None


def _family_from_text(text: str | None) -> str | None:
    from .activity_semantics import activity_family
    return activity_family(_norm(text), _FAMILY_HINTS)


def _family(task: Task, raw: dict) -> str | None:
    # Explicit metadata wins, but common venue words still normalize to a stable key.
    loc = _explicit_location(raw)
    if loc:
        mapped = _family_from_text(loc)
        return mapped or f"location:{_norm(loc)}"
    if str(raw.get("category") or "").startswith("project:"):
        return None
    return _family_from_text(task.title)


def _location_for_family(family: str | None) -> str | None:
    if not family:
        return None
    if family.startswith("location:"):
        return family.split(":", 1)[1]
    return {
        "pool": "Pool",
        "table-tennis": "Table tennis venue",
        "gym": "Gym",
        "badminton": "Badminton venue",
        "football": "Football venue",
        "run": "Running route",
        "training": "Training venue",
    }.get(family, family.replace("-", " ").title())


def _is_primary_outing(task: Task, raw: dict) -> bool:
    if _is_support(task):
        return False
    fam = _family(task, raw)
    if _explicit_location(raw) and any(raw.get(k) is not None for k in ("travel_minutes", "return_travel_minutes")):
        return True
    if not fam:
        return False
    # A manually supplied location on an ordinary desk task should not magically make
    # it an excursion; title/category must still look like an activity.
    title = _norm(task.title)
    category = _norm((raw or {}).get("category"))
    return (
        _family_from_text(title) is not None
        or category in {"fitness", "sport", "sports", "exercise"}
    )


def _duration(task: Task, raw: dict, *, support: bool = False) -> int | None:
    raw = raw or {}
    for key in ("remaining_minutes", "duration_minutes"):
        value = raw.get(key)
        if value is not None:
            try:
                return max(0, int(round(float(value))))
            except Exception:
                pass
    if task.duration_minutes is not None:
        return int(task.duration_minutes)
    if not support:
        return None

    title = _norm(task.title)
    if "travel" in title or "commute" in title:
        return _DEFAULT_SUPPORT_MINUTES["travel"]
    if "change" in title and "shower" not in title:
        return _DEFAULT_SUPPORT_MINUTES["change"]
    if "shower" in title:
        return _DEFAULT_SUPPORT_MINUTES["shower"]
    if "go home" in title or "home" in title:
        return _DEFAULT_SUPPORT_MINUTES["home"]
    if "warm" in title:
        return _DEFAULT_SUPPORT_MINUTES["warmup"]
    if "cool" in title:
        return _DEFAULT_SUPPORT_MINUTES["cooldown"]
    return _DEFAULT_SUPPORT_MINUTES["buffer"]


def _support_role(task: Task, primary: Task | None = None) -> str:
    title = _norm(task.title)
    if any(x in title for x in _PRE_HINTS):
        return "pre"
    if any(x in title for x in _POST_HINTS):
        return "post"
    # Buffers inherit their side from the original timeline when possible.
    if primary and task.start and primary.start:
        return "pre" if task.start <= primary.start else "post"
    return "pre"


def _support_sort_key(part: BundlePart) -> tuple[int, datetime, str]:
    title = _norm(part.task.title)
    if part.role == "pre":
        order = 0 if ("travel" in title or "commute" in title) else 1 if "change" in title else 2
    else:
        order = 0 if ("buffer" in title or "cool" in title) else 1 if "shower" in title else 2
    stamp = part.task.start or datetime.max.replace(tzinfo=None)
    # Compare aware/naive safely by using a timestamp only when possible.
    try:
        stamp_key = datetime.fromtimestamp(stamp.timestamp())
    except Exception:
        stamp_key = datetime.max
    return order, stamp_key, title


def _same_original_day(a: Task, b: Task) -> bool:
    if not a.start or not b.start:
        return False
    return a.start.date() == b.start.date()


def _temporal_distance_minutes(a: Task, b: Task) -> float:
    if not a.start or not b.start:
        return 1e9
    return abs((a.start - b.start).total_seconds()) / 60.0


def _choose_primary(support: Task, primaries: list[Task], metas: dict[str, dict]) -> Task | None:
    sfam = _family(support, metas.get(support.id, {}))
    candidates = [p for p in primaries if not _is_fixed(p)] + [p for p in primaries if _is_fixed(p)]

    if sfam:
        exact = [p for p in candidates if _family(p, metas.get(p.id, {})) == sfam]
        if exact:
            exact.sort(key=lambda p: (_temporal_distance_minutes(support, p), p.title.lower()))
            return exact[0]
        # Venue-specific support without a matching venue activity is an orphan.  Do
        # not attach "Travel to pool" to table tennis just because both are exercise.
        return None

    same_day = [p for p in candidates if _same_original_day(support, p)]
    if same_day:
        same_day.sort(key=lambda p: (_temporal_distance_minutes(support, p), p.title.lower()))
        # Generic shower/go-home/buffer belongs to the nearest outing on that day,
        # but only when it is reasonably nearby in the original plan.
        if _temporal_distance_minutes(support, same_day[0]) <= 8 * 60:
            return same_day[0]

    if len(candidates) == 1:
        return candidates[0]
    return None


def _infer_context_metadata(tasks: Iterable[Task], metas: dict[str, dict]) -> None:
    for task in tasks:
        raw = metas.setdefault(task.id, {})
        fam = _family(task, raw)
        if fam and not raw.get("location"):
            raw["location"] = _location_for_family(fam)
        if _is_support(task):
            raw.setdefault("energy", "low")
            raw.setdefault("category", "logistics")
            raw.setdefault("context", f"outing:{fam}" if fam else "outing:logistics")
        elif _is_primary_outing(task, raw):
            raw.setdefault("category", "fitness")
            raw.setdefault("context", f"outing:{fam}" if fam else "outing")


def _anchor_support_around_fixed(primary: Task, supports: list[Task], metas: dict[str, dict]) -> list[str]:
    """Give support tasks exact contiguous windows around a timed fixed outing."""
    if not primary.start or not primary.end or primary.is_all_day:
        held = []
        for task in supports:
            if _is_fixed(task):
                continue
            metas.setdefault(task.id, {})["autoschedule"] = False
            held.append(task.title)
        return held

    parts: list[BundlePart] = []
    for task in supports:
        if _is_fixed(task):
            continue
        raw = metas.setdefault(task.id, {})
        mins = _duration(task, raw, support=True) or 5
        parts.append(BundlePart(task, mins, _support_role(task, primary), raw.get("location")))

    pre = sorted((x for x in parts if x.role == "pre"), key=_support_sort_key)
    post = sorted((x for x in parts if x.role == "post"), key=_support_sort_key)

    cursor = primary.start - timedelta(minutes=sum(x.minutes for x in pre))
    previous_id: str | None = None
    for part in pre:
        raw = metas.setdefault(part.task.id, {})
        raw.update({
            "earliest": cursor.isoformat(),
            "latest_end": (cursor + timedelta(minutes=part.minutes)).isoformat(),
            "duration_minutes": part.minutes,
            "remaining_minutes": None,
            "splittable": False,
            "confidence": "high",
            "timing": "asap",
        })
        deps = list(raw.get("dependencies") or [])
        if previous_id and previous_id not in deps:
            deps.append(previous_id)
        raw["dependencies"] = deps
        if previous_id:
            raw.setdefault("_dependency_gap_minutes", {})[previous_id] = 0
        previous_id = part.task.id
        cursor += timedelta(minutes=part.minutes)

    cursor = primary.end
    previous_id = primary.id
    for part in post:
        raw = metas.setdefault(part.task.id, {})
        raw.update({
            "earliest": cursor.isoformat(),
            "latest_end": (cursor + timedelta(minutes=part.minutes)).isoformat(),
            "duration_minutes": part.minutes,
            "remaining_minutes": None,
            "splittable": False,
            "confidence": "high",
            "timing": "asap",
        })
        deps = [x for x in (raw.get("dependencies") or []) if x != primary.id]
        if previous_id and previous_id not in deps:
            deps.append(previous_id)
        raw["dependencies"] = deps
        if previous_id:
            raw.setdefault("_dependency_gap_minutes", {})[previous_id] = 0
        previous_id = part.task.id
        cursor += timedelta(minutes=part.minutes)
    return []


def _build_flexible_bundle(primary: Task, supports: list[Task], metas: dict[str, dict]) -> OutingBundle | None:
    if _is_fixed(primary):
        return None
    raw = metas.setdefault(primary.id, {})
    main_minutes = _duration(primary, raw, support=False)
    if main_minutes is None or main_minutes <= 0:
        return None

    family = _family(primary, raw) or "outing"
    pre: list[BundlePart] = []
    post: list[BundlePart] = []
    for task in supports:
        if _is_fixed(task):
            continue
        sraw = metas.setdefault(task.id, {})
        mins = _duration(task, sraw, support=True) or 5
        role = _support_role(task, primary)
        part = BundlePart(task, mins, role, sraw.get("location") or _location_for_family(family))
        (pre if role == "pre" else post).append(part)
        # The support task is represented inside the primary's contiguous bundle;
        # it must not be scheduled a second time on its own.
        sraw["autoschedule"] = False

    pre.sort(key=_support_sort_key)
    post.sort(key=_support_sort_key)
    parts = pre + [BundlePart(primary, main_minutes, "main", raw.get("location") or _location_for_family(family))] + post
    if len(parts) == 1:
        return None

    total = sum(x.minutes for x in parts)
    if raw.get('_explicit_activity_minutes') is not None:
        # The solver schedules the whole bundle. Its exact geometry includes
        # logistics; the main BundlePart retains the requested activity time.
        raw['_explicit_activity_minutes'] = total
    raw.update({
        "duration_minutes": total,
        "remaining_minutes": None,
        "splittable": False,
        "min_chunk": total,
        "max_chunk": total,
        "confidence": "high",
        "location": raw.get("location") or _location_for_family(family),
        "context": raw.get("context") or f"outing:{family}",
    })
    return OutingBundle(primary, family, parts, main_minutes)


def _expand_bundle(seg: Segment, bundle: OutingBundle) -> list[Segment]:
    available = max(1, int(round((seg.end - seg.start).total_seconds() / 60.0)))
    required = bundle.total_minutes
    if available < required:
        # This should not happen because the representative duration was expanded,
        # but safety beats corrupting/overlapping real tasks.
        return [seg]

    extra = available - required
    cursor = seg.start
    out: list[Segment] = []
    for part in bundle.parts:
        minutes = part.minutes + (extra if part.role == "main" else 0)
        end = cursor + timedelta(minutes=minutes)
        title = part.task.title
        reason = (
            f"Reality bundle · {part.role} step for {bundle.primary.title}"
            if part.role != "main"
            else f"Reality bundle · activity kept with its travel/prep/recovery"
        )
        out.append(Segment(
            task_id=part.task.id,
            project_id=part.task.project_id,
            title=title,
            start=cursor,
            end=end,
            score=seg.score,
            reason=reason,
            source_task=part.task,
            segment_index=1,
            segment_count=1,
            location=part.location or seg.location,
            category="logistics" if part.role != "main" else seg.category,
        ))
        cursor = end
    return out


def reality_plan(tasks: list[Task], meta_map: dict[str, dict], busy, start: datetime, horizon_days: int,
                 config: dict, mastery_map: dict[str, float] | None = None):
    metas = deepcopy(meta_map or {})
    _infer_context_metadata(tasks, metas)

    primaries = [t for t in tasks if _is_primary_outing(t, metas.get(t.id, {}))]
    supports = [t for t in tasks if _is_support(t) and not _is_fixed(t)]
    grouped: dict[str, list[Task]] = {p.id: [] for p in primaries}
    orphan_support: list[Task] = []

    for support in supports:
        primary = _choose_primary(support, primaries, metas)
        if primary is None:
            metas.setdefault(support.id, {})["autoschedule"] = False
            orphan_support.append(support)
        else:
            grouped.setdefault(primary.id, []).append(support)

    bundles: dict[str, OutingBundle] = {}
    held_due_to_fixed: list[str] = []
    for primary in primaries:
        attached = grouped.get(primary.id, [])
        if not attached:
            continue
        if _is_fixed(primary):
            held_due_to_fixed.extend(_anchor_support_around_fixed(primary, attached, metas))
            continue
        bundle = _build_flexible_bundle(primary, attached, metas)
        if bundle is None:
            # If the main activity has no usable duration, floating the logistics is
            # worse than holding them.  The user can estimate/checkpoint the activity.
            for task in attached:
                metas.setdefault(task.id, {})["autoschedule"] = False
            orphan_support.extend(attached)
        else:
            bundles[primary.id] = bundle

    segments, warnings, diagnostics = _BASE_PLAN(
        tasks, metas, busy, start, horizon_days, config, mastery_map or {}
    )

    expanded: list[Segment] = []
    bundled_support_ids = {part.task.id for b in bundles.values() for part in b.parts if part.role != "main"}
    for seg in segments:
        if seg.task_id in bundles:
            expanded.extend(_expand_bundle(seg, bundles[seg.task_id]))
        elif seg.task_id in bundled_support_ids:
            # Defensive: support was marked autoschedule=False, but never let a stale
            # fallback path duplicate it outside the bundle.
            continue
        else:
            expanded.append(seg)

    expanded.sort(key=lambda x: x.start)

    # Final invariant: never return overlapping blocks, even if a future planner
    # regression slips through.  Preserve the higher-scoring/earlier block and warn.
    safe: list[Segment] = []
    dropped_overlap: list[str] = []
    for seg in expanded:
        if safe and seg.start < safe[-1].end:
            dropped_overlap.append(seg.title)
            continue
        safe.append(seg)

    diagnostics = dict(diagnostics or {})
    diagnostics.update({
        "reality_guard": True,
        "reality_guard_version": "1.0",
        "outing_bundle_count": len(bundles),
        "orphan_logistics_suppressed": len({x.id for x in orphan_support}),
        "overlap_guard_dropped": len(dropped_overlap),
    })

    warnings = list(warnings or [])
    if orphan_support:
        names = list(dict.fromkeys(x.title for x in orphan_support))
        warnings.append(
            "Held outing logistics instead of floating them around the day because no schedulable matching activity was found: "
            + ", ".join(names[:8])
            + ("…" if len(names) > 8 else "")
        )
    if held_due_to_fixed:
        warnings.append(
            "Held support steps for a fixed outing whose exact time is incomplete: "
            + ", ".join(dict.fromkeys(held_due_to_fixed))
        )
    if dropped_overlap:
        warnings.append(
            "Reality guard removed overlapping output rather than committing an impossible plan: "
            + ", ".join(dict.fromkeys(dropped_overlap))
        )

    return safe, warnings, diagnostics


# entrypoint imports adaptive_patch first, so this wraps the already-adaptive planner.
_sch.plan = reality_plan


__all__ = ["reality_plan", "_BASE_PLAN"]

