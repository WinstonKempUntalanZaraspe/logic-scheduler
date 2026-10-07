from __future__ import annotations

"""One-off early-morning commitment planning.

This layer is intentionally planning-only. A sentence such as

    "I have a job at 7am tomorrow, 1h15 travel time, 5am need wake up, shower/get ready"

changes the usable sleep/wake/travel geometry without permanently rewriting the user's
normal routine and without inventing a durable TickTick task for an incompletely described
shift. Exact job/event ranges may be protected as temporary reality for that plan; a
start-only commitment keeps later availability conservatively constrained until its real
end time is supplied, rather than inventing a shift length or blocking the whole replan.
"""

from copy import deepcopy
from datetime import datetime, timedelta, time
import re

from .config import settings
from . import quickdump as qd
from . import temporal_engine as temporal

_TOMORROW = re.compile(r"\b(?:tomorrow|tmr)\b", re.I)
_COMMITMENT = re.compile(
    r"\b(?P<label>job|work(?:\s+shift)?|shift|school|class|lecture|tutorial|lab|"
    r"appointment|interview|meeting|exam|flight|train|bus)\b",
    re.I,
)
_START_AFTER = re.compile(
    r"\b(?:job|work(?:\s+shift)?|shift|school|class|lecture|tutorial|lab|"
    r"appointment|interview|meeting|exam|flight|train|bus)\b"
    r".{0,45}?\b(?:starts?\s+)?(?:at|by)\s+"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_START_BEFORE = re.compile(
    r"\b(?:at|by)\s+(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b"
    r".{0,30}?\b(?:job|work(?:\s+shift)?|shift|school|class|lecture|tutorial|lab|"
    r"appointment|interview|meeting|exam|flight|train|bus)\b",
    re.I,
)
_WAKE_AFTER = re.compile(
    r"\b(?:wake(?:\s+up)?|get\s+up|be\s+up|rise)\b"
    r".{0,24}?\b(?:at|by|around|about)?\s*"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_WAKE_BEFORE = re.compile(
    r"\b(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b"
    r".{0,30}?\b(?:need|have|gotta|must|should)?\s*(?:to\s+)?"
    r"(?:wake(?:\s+up)?|get\s+up|be\s+up|rise)\b",
    re.I,
)
_PREP = re.compile(
    r"\b(?:shower|bathe|bath|wash\s+up|get\s+ready|dress|get\s+dressed|"
    r"prepare|morning\s+routine|brush\s+(?:my\s+)?teeth|pack)\b",
    re.I,
)
_EXPLICIT_SLEEP = re.compile(
    r"\b(?:sleep|go\s+to\s+(?:bed|sleep)|bedtime)\b"
    r".{0,20}?\b(?:at|by|around|about)?\s*"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_TRAVEL_WORD = re.compile(r"\b(?:travel|commute|journey|trip)\b", re.I)
_RANGE = re.compile(
    r"(?:\bfrom\s+)?(?P<a>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*"
    r"(?:-|–|—|\bto\b)\s*"
    r"(?P<b>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.I,
)


def _clock_minutes(raw: str | None) -> int | None:
    if not raw:
        return None
    return qd._clock_to_minutes(str(raw))


def _clock_text(minutes: int) -> str:
    return f"{(minutes % 1440) // 60:02d}:{minutes % 60:02d}"


def _clock_dt(day, raw: str | None) -> datetime | None:
    minute = _clock_minutes(raw)
    if minute is None:
        return None
    return datetime.combine(day, time(minute // 60, minute % 60), settings.tz)


def _label(text: str) -> str:
    match = _COMMITMENT.search(str(text or ""))
    raw = (match.group("label") if match else "commitment").strip().lower()
    aliases = {
        "work": "Work",
        "work shift": "Work shift",
        "shift": "Work shift",
        "job": "Job",
        "school": "School",
        "class": "Class",
        "lecture": "Lecture",
        "tutorial": "Tutorial",
        "lab": "Lab",
        "appointment": "Appointment",
        "interview": "Interview",
        "meeting": "Meeting",
        "exam": "Exam",
        "flight": "Flight",
        "train": "Train",
        "bus": "Bus",
    }
    return aliases.get(raw, raw.title() or "Commitment")


def _wake_clock(text: str) -> str | None:
    ref = temporal.clock_near(
        text,
        r"\b(?:wake(?:\s+up)?|get\s+up|be\s+up|rise)\b",
        max_distance=36,
    )
    return temporal.clock_string(ref.minutes) if ref else None

def _future_target_day(text: str, now: datetime):
    ref = temporal.date_near(
        text,
        now,
        r"\b(?:job|work(?:\s+shift)?|shift|school|class|lecture|tutorial|lab|appointment|interview|meeting|exam|flight|train|bus)\b",
        max_distance=120,
    ) or temporal.resolve_date_reference(text, now)
    if ref and ref.start_date > now.date():
        return ref.start_date
    return None


def _commitment_range(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """Bind an exact Temporal-IR interval to the commitment noun."""
    target = _future_target_day(text, now)
    if target is None:
        return None
    source = str(text or "")
    commitments = list(_COMMITMENT.finditer(source))
    doc = temporal.parse_temporal(source, now)
    candidates = [
        c for c in doc.constraints
        if c.kind == "interval" and c.start_at and c.end_at
        and c.start_at.date() == target and not c.optional and not c.negated and not c.hypothetical
    ]
    best = None
    best_distance = None
    for c in candidates:
        for commitment in commitments:
            distance = min(
                abs(c.evidence.start - commitment.end()),
                abs(commitment.start() - c.evidence.end),
            )
            between = source[min(commitment.end(), c.evidence.end):max(commitment.start(), c.evidence.start)]
            if distance > 55 or re.search(r"\b(?:wake|shower|bathe|ready|travel|commute|journey|trip)\b", between, re.I):
                continue
            if best_distance is None or distance < best_distance:
                best = (c.start_at, c.end_at)
                best_distance = distance
    return best


def _commitment_start(text: str, now: datetime) -> datetime | None:
    target = _future_target_day(text, now)
    if target is None:
        return None
    timerange = _commitment_range(text, now)
    if timerange:
        return timerange[0]
    ref = temporal.clock_near(
        text,
        r"\b(?:job|work(?:\s+shift)?|shift|school|class|lecture|tutorial|lab|appointment|interview|meeting|exam|flight|train|bus)\b",
        max_distance=48,
    )
    return _clock_dt(target, temporal.clock_string(ref.minutes)) if ref else None

def _travel_minutes(text: str) -> int | None:
    source = str(text or "")
    hit = _TRAVEL_WORD.search(source)
    if not hit:
        return None
    around = source[max(0, hit.start() - 70):min(len(source), hit.end() + 70)]
    total = temporal.duration_minutes(around)
    return int(total) if total and 0 < total <= 360 else None

def _normal_sleep_minutes(config: dict) -> int:
    sleep = _clock_minutes(str(config.get("sleep_start") or config.get("day_end") or "23:00"))
    wake = _clock_minutes(str(config.get("wake_time") or config.get("day_start") or "07:00"))
    if sleep is None or wake is None:
        return 8 * 60
    span = wake - sleep
    if span <= 0:
        span += 24 * 60
    # Keep absurd/malformed settings from generating extreme inferred bedtimes.
    return span if 180 <= span <= 14 * 60 else 8 * 60


def _explicit_sleep_dt(text: str, day) -> datetime | None:
    ref = temporal.clock_near(
        text,
        r"\b(?:sleep|go\s+to\s+(?:bed|sleep)|bedtime)\b",
        max_distance=32,
    )
    return _clock_dt(day, temporal.clock_string(ref.minutes)) if ref else None


def _logical_day_end(day, wake: datetime, config: dict) -> datetime:
    sleep_minute = _clock_minutes(str((config or {}).get("sleep_start") or (config or {}).get("day_end") or "23:00"))
    sleep_minute = 23 * 60 if sleep_minute is None else sleep_minute
    end = datetime.combine(day, time(sleep_minute // 60, sleep_minute % 60), settings.tz)
    if end <= wake:
        end += timedelta(days=1)
    return end


def _existing_end(rows: list[dict], start: datetime, label: str) -> datetime | None:
    wanted = label.lower()
    best = None
    for row in rows or []:
        title = str(row.get("title") or "").lower()
        if wanted not in title and not (
            wanted in {"job", "work shift"} and re.search(r"\b(?:job|work|shift)\b", title)
        ):
            continue
        raw_start = row.get("start") or row.get("startDate")
        raw_end = row.get("end") or row.get("endDate")
        if not raw_start or not raw_end:
            continue
        try:
            a = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            if a.tzinfo is None:
                a = a.replace(tzinfo=settings.tz)
            if b.tzinfo is None:
                b = b.replace(tzinfo=settings.tz)
            a, b = a.astimezone(settings.tz), b.astimezone(settings.tz)
        except Exception:
            continue
        if a.date() != start.date() or abs((a - start).total_seconds()) > 30 * 60 or b <= a:
            continue
        if best is None or abs((a - start).total_seconds()) < abs((best[0] - start).total_seconds()):
            best = (a, b)
    return best[1] if best else None


def looks_like_early_future_commitment(text: str, now: datetime | None = None) -> bool:
    source = str(text or "")
    stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    if not _COMMITMENT.search(source) or _future_target_day(source, stamp) is None:
        return False
    return bool(_wake_clock(source) and _commitment_start(source, stamp))


def enrich_early_future_commitment(
    parsed: dict,
    text: str,
    rows: list[dict],
    config: dict,
    now: datetime,
) -> dict:
    """Compile a one-off early start into sleep, prep and travel constraints."""
    if not looks_like_early_future_commitment(text, now):
        return parsed

    out = deepcopy(parsed or {})
    out.setdefault("tasks", [])
    out.setdefault("notes", [])
    out.setdefault("warnings", [])
    out.setdefault("clarifications", [])
    out.setdefault("intents", [])

    target = _future_target_day(text, now)
    if target is None:
        return out
    wake_text = _wake_clock(text)
    wake = _clock_dt(target, wake_text)
    start = _commitment_start(text, now)
    travel = _travel_minutes(text)
    label = _label(text)
    exact_range = _commitment_range(text, now)
    end = exact_range[1] if exact_range else (_existing_end(rows, start, label) if start else None)

    if not wake or not start:
        return out

    # This is schedule-control language, not authority to create a durable command-shaped
    # task. Remove only creates sourced from the whole request; unrelated reviewed actions
    # remain untouched.
    norm_source = re.sub(r"\s+", " ", str(text or "").strip().lower())
    kept = []
    for change in out.get("tasks") or []:
        line = re.sub(r"\s+", " ", str(change.get("line") or "").strip().lower())
        title = re.sub(r"\s+", " ", str(change.get("title") or "").strip().lower())
        commandish = (
            change.get("action") == "create"
            and (line == norm_source or (len(title.split()) >= 8 and any(x in title for x in ("wake", "travel", "tomorrow", "tmr"))))
        )
        if not commandish:
            kept.append(change)
    out["tasks"] = kept

    ctx = dict(out.get("context") or {})
    ctx.update(
        date=now.date().isoformat(),
        source="quick-dump",
        replan_requested=True,
        replan_from=now.isoformat(),
        preserve_unfinished=True,
        fresh_plan_revision=True,
        minimum_horizon_days=max(2, int(ctx.get("minimum_horizon_days") or 1)),
    )
    # This update changes tonight AND tomorrow morning, so do not hide today.
    ctx["replan_scope"] = "today"

    plans = dict(ctx.get("future_day_plans") or {})
    plan = dict(plans.get(target.isoformat()) or {})
    plan.update(date=target.isoformat(), wake_time=wake_text)
    plans[target.isoformat()] = plan
    ctx["future_day_plans"] = plans
    if target == now.date() + timedelta(days=1):
        tomorrow_plan = dict(ctx.get("tomorrow_plan") or {})
        tomorrow_plan.update(plan)
        ctx["tomorrow_plan"] = tomorrow_plan
    horizon_days = max(2, (target - now.date()).days + 1)
    ctx["minimum_horizon_days"] = max(horizon_days, int(ctx.get("minimum_horizon_days") or 1))
    out["minimum_horizon_days"] = max(horizon_days, int(out.get("minimum_horizon_days") or 1))

    # Preserve the user's normal sleep quantity where possible, but never pretend we can
    # go to bed in the past. Existing wind-down logic will reserve the interval immediately
    # before this temporary sleep_start.
    normal_sleep = _normal_sleep_minutes(config or {})
    ideal_sleep = wake - timedelta(minutes=normal_sleep)
    explicit_sleep = _explicit_sleep_dt(text, ideal_sleep.date())
    desired_sleep = explicit_sleep or ideal_sleep
    wind_down = max(0, int((config or {}).get("bedtime_wind_down_minutes") or 0))
    if desired_sleep.date() == now.date():
        earliest_realistic = now.replace(second=0, microsecond=0) + timedelta(minutes=wind_down)
        effective_sleep = max(desired_sleep, earliest_realistic)
    else:
        effective_sleep = desired_sleep
    if effective_sleep >= wake:
        effective_sleep = wake - timedelta(minutes=5)
    projected_sleep = max(0, int((wake - effective_sleep).total_seconds() // 60))
    shortfall = max(0, normal_sleep - projected_sleep)

    # Store date-scoped sleep overrides. The root sleep_start remains a compatibility
    # alias only when the affected bedtime is tonight.
    sleep_day = effective_sleep.date()
    sleep_plan = dict(plans.get(sleep_day.isoformat()) or {})
    sleep_plan.update(date=sleep_day.isoformat(), sleep_start=effective_sleep.strftime("%H:%M"))
    plans[sleep_day.isoformat()] = sleep_plan
    ctx["future_day_plans"] = plans
    if sleep_day == now.date():
        ctx["sleep_start"] = effective_sleep.strftime("%H:%M")
    ctx["early_wake_sleep_adjustment"] = {
        "target_date": target.isoformat(),
        "wake_time": wake_text,
        "normal_sleep_minutes": normal_sleep,
        "ideal_sleep_start": ideal_sleep.isoformat(),
        "effective_sleep_start": effective_sleep.isoformat(),
        "projected_sleep_minutes": projected_sleep,
        "sleep_shortfall_minutes": shortfall,
        "explicit_sleep_time": bool(explicit_sleep),
    }

    blocks = [
        b for b in (ctx.get("temporary_blocks") or [])
        if not (isinstance(b, dict) and str(b.get("source") or "").startswith("early-commitment-"))
    ]

    depart = None
    if travel is not None:
        depart = start - timedelta(minutes=travel)
        if wake > depart:
            out["clarifications"].append({
                "text": str(text),
                "reason": (
                    f"{label} starts at {start:%H:%M}, but waking at {wake:%H:%M} leaves less than "
                    f"the stated {travel}-minute travel time. Give an earlier wake time or shorter travel time."
                ),
            })
        else:
            if _PREP.search(text) and depart > wake:
                blocks.append({
                    "label": "Morning preparation (shower / get ready)",
                    "start": wake.isoformat(),
                    "end": depart.isoformat(),
                    "source": "early-commitment-prep",
                    "kind": "care",
                    "planning_only": True,
                    "planning_estimate": False,
                })
            blocks.append({
                "label": f"Travel to {label.lower()}",
                "start": depart.isoformat(),
                "end": start.isoformat(),
                "source": "early-commitment-travel",
                "kind": "logistics",
                "planning_only": True,
                "planning_estimate": False,
            })
    else:
        out["clarifications"].append({
            "text": str(text),
            "reason": f"Give the travel/commute duration to reach {label.lower()} by {start:%H:%M}.",
        })

    if end and end > start:
        blocks.append({
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "source": "early-commitment-fixed",
            "kind": "commitment",
            "planning_only": True,
            "planning_estimate": False,
        })
    else:
        # Do not invent a shift duration and do not fail the whole replan. Hold later
        # availability conservatively until the normal logical day end. A later update
        # with the real end time replaces this planning-only uncertainty.
        unknown_end = _logical_day_end(target, wake, config or {})
        blocks.append({
            "label": f"{label} / availability unknown after start",
            "start": start.isoformat(),
            "end": unknown_end.isoformat(),
            "source": "early-commitment-unknown-tail",
            "kind": "availability",
            "planning_only": True,
            "planning_estimate": False,
            "uncertain_end": True,
        })
        out["warnings"].append(
            f"{label} starts at {start:%H:%M}, but its end time is unknown. "
            "Flexible work after the start is held as constrained rather than scheduled over the commitment. "
            "Give the end time later to release those hours."
        )

    ctx["temporary_blocks"] = blocks
    plan["early_commitment"] = {
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "travel_minutes": travel,
        "depart_home": depart.isoformat() if depart else None,
        "prep_start": wake.isoformat(),
        "prep_end": depart.isoformat() if depart and depart >= wake else None,
    }
    ctx["tomorrow_plan"] = plan
    out["context"] = ctx

    out["notes"].append(
        f"{target.isoformat()} wake time is a one-off {wake_text} override; your saved normal wake time is unchanged."
    )
    if travel is not None and depart:
        prep_minutes = max(0, int((depart - wake).total_seconds() // 60))
        out["notes"].append(
            f"Back-planned morning: wake {wake:%H:%M}, "
            + (f"reserve {prep_minutes} minutes for morning preparation, " if prep_minutes else "")
            + f"leave {depart:%H:%M}, travel {travel} minutes, arrive {start:%H:%M}."
        )
    if effective_sleep > ideal_sleep:
        out["notes"].append(
            f"To preserve your normal {normal_sleep // 60}h{normal_sleep % 60:02d} sleep target, ideal sleep was "
            f"{ideal_sleep:%H:%M}; that is no longer reachable from the current time. "
            f"Protect wind-down now and target sleep at {effective_sleep:%H:%M} "
            f"({projected_sleep // 60}h{projected_sleep % 60:02d} before the {wake:%H:%M} wake)."
        )
    else:
        out["notes"].append(
            f"Protected tonight's sleep at {effective_sleep:%H:%M} so the one-off {wake:%H:%M} wake keeps "
            f"the normal {normal_sleep // 60}h{normal_sleep % 60:02d} sleep quantity."
        )

    # Clear generic whole-prompt ambiguity after the deterministic chain is compiled.
    # Specific impossible-geometry clarifications remain; an unknown shift end is a
    # non-blocking warning plus conservative availability hold.
    generic = "unclear whether this is work or a planning instruction"
    out["clarifications"] = [
        q for q in out["clarifications"]
        if generic not in str(q.get("reason") or "").lower()
    ]
    for intent in out.get("intents") or []:
        if re.sub(r"\s+", " ", str(intent.get("text") or "").strip().lower()) == norm_source:
            if intent.get("status") == "needs-input":
                intent["kind"] = "replan"
                intent["status"] = "compiled"

    out["notes"] = list(dict.fromkeys(out.get("notes") or []))
    out["warnings"] = list(dict.fromkeys(out.get("warnings") or []))
    return out


__all__ = [
    "looks_like_early_future_commitment",
    "enrich_early_future_commitment",
    "_travel_minutes",
    "_wake_clock",
]
