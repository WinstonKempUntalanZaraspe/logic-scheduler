from __future__ import annotations

"""Fresh-plan revision semantics for real life.

A later Quick Dump is allowed to contradict an earlier one.  "Change of plans" must
replace stale *planning* context (outing/return-home/order assumptions) while preserving
facts that are still true (completed meals, an explicitly reported wake time, etc.).

This layer also handles an important explicit override: a protected commitment may be
moved when the user clearly says that *this occurrence* changed, e.g. "I'm not going to
church today only; it is moved tomorrow 5-6pm".  Ordinary replans still cannot move a
#fixed commitment.

Short real-life sequences such as "now I'll bathe, nap, eat dinner, swim, then maybe math
or physics if time allows" are represented as temporary planning context, not permanent
TickTick chores.  Durations that the user did not specify are planning-only estimates and
are surfaced as such.
"""

import re
from copy import deepcopy
from datetime import datetime, timedelta, time
from typing import Callable

from .config import settings
from .models import BusyBlock
from . import language_intake as intake
from . import contextual_intake_patch as contextual
from . import deterministic_intake_patch as deterministic
from . import quickdump as qd
from . import service


_REVISION_RE = re.compile(
    r"\b(?:change\s+of\s+plans?|plans?\s+changed|changed\s+plans?|instead\b|"
    r"not\s+going\s+to|won't\s+(?:be\s+)?going\s+to|will\s+not\s+(?:be\s+)?going\s+to|"
    r"cancel(?:led|ing)?\b|skip(?:ping|ped)?\b|reschedul(?:e|ed|ing)\b|postpon(?:e|ed|ing)\b)\b",
    re.I,
)

_CANCEL_RE = re.compile(
    r"\b(?:i\s+(?:am|'m)\s+)?(?:not\s+going\s+to|won't\s+(?:be\s+)?going\s+to|"
    r"will\s+not\s+(?:be\s+)?going\s+to|not\s+attending|skip(?:ping)?|cancel(?:ling|ing)?)\s+"
    r"(?:the\s+)?(?P<target>[a-z0-9][a-z0-9 &'/_+\-]{0,80}?)"
    r"(?=\s+(?:for\s+)?today\b|\s+today\b|[,.;]|$)",
    re.I,
)

_MOVE_RE = re.compile(r"\b(?:move(?:d)?|reschedul(?:e|ed)|shift(?:ed)?|postpon(?:e|ed))\b", re.I)
_BATH_RE = re.compile(r"\b(?:bathe|bath|shower)\b", re.I)
_NAP_RE = re.compile(r"\b(?:nap|sleep)\b", re.I)
_DINNER_RE = re.compile(r"\b(?:eat|have|having)?\s*dinner\b|\bdinner\b", re.I)
_SWIM_RE = re.compile(r"\b(?:swim|swimming|pool)\b", re.I)
_OPTIONAL_STUDY_RE = re.compile(
    r"\b(?:maybe|perhaps|if\s+(?:there(?:'s| is)\s+)?time(?:\s+allows?)?|if\s+time\s+allows?|if\s+possible)\b",
    re.I,
)

# These are day-plan instructions, not durable user facts.  They are deliberately
# discarded when a fresh revision supersedes the previous story.
_TRANSIENT_KEYS = {
    "temporary_blocks", "return_home_not_after", "return_home_not_before",
    "return_home_estimate", "after_return_task_ids", "requested_constraints", "maximize_productive_time",
    "intent_only_tonight_ids", "intent_only_tonight_titles", "intent_swim_tomorrow_ids",
    "intent_swim_tomorrow_start", "intent_today_ids", "before_main_study_ids",
    "intent_exact_order", "intent_dinner_start", "intent_dinner_end", "intent_sleep_end",
    "intent_date_goals", "intent_date_windows", "intent_exclusions", "targeted_schedule_instructions",
    "defer_discretionary", "human_reality_active", "human_reality_checkpoint_at",
    "fixed_overrides", "suppressed_fixed_task_ids_today", "suppressed_fixed_titles_today",
    "plan_local_dependencies", "plan_local_earliest", "optional_today_ids",
    "optional_choice_groups", "dinner_location", "meal_locations", "home_base_active", "journey_state",
    "superseded_dependency_edges", "fresh_plan_revision",
    "tomorrow_plan", "future_day_plans", "plan_local_latest_end", "optional_date_goal_ids", "after_meal_task_ids", "personal_venue_overrides",
    "meal_after_task_ids", "meal_not_before",
    "current_activity", "after_meal_rest_minutes", "plan_local_duration_requests",
    "requested_project_campaign_ids", "requested_project_work_packages", "requested_project_ephemeral_packages",
}

# Facts that normally survive a change of plan because the later prompt did not make
# them false.  Everything else is rebuilt from the new intake.
_RETAIN_FACT_KEYS = {
    "date", "source", "completed_meals", "wake_time", "actual_wake_reported",
    "reported_clock", "energy_scale", "avoid_deep_today", "fatigue_until",
}


def _norm(value: str | None) -> str:
    text = str(value or "").replace("’", "'").lower()
    text = re.sub(r"\btmr\b", "tomorrow", text)
    text = re.sub(r"\btdy\b", "today", text)
    text = text.replace("maths", "math").replace("swimming", "swim")
    return re.sub(r"\s+", " ", text).strip()


def _is_fresh_revision(text: str) -> bool:
    low = _norm(text)
    if not _REVISION_RE.search(low):
        return False
    # A bare word like "instead" is not enough.  Require an actual day/schedule
    # contradiction or a new immediate sequence.
    return bool(
        re.search(r"\b(?:today|tomorrow|now|tonight)\b", low)
        or _CANCEL_RE.search(low)
        or (_MOVE_RE.search(low) and qd._extract_time_range(low, datetime.now(settings.tz)))
    )


def revision_merge_context(current: dict | None, incoming: dict | None, base_merge: Callable | None = None) -> dict:
    """Merge same-day context, replacing stale planning state on a fresh revision."""
    incoming = deepcopy(incoming or {})
    current = deepcopy(current or {})
    merge = base_merge or intake.merge_context
    if not incoming.get("fresh_plan_revision"):
        return merge(current, incoming)
    if not current or current.get("date") != incoming.get("date"):
        return deepcopy(incoming)

    retained = {k: deepcopy(v) for k, v in current.items() if k in _RETAIN_FACT_KEYS}
    # Completed meals are facts; merge rather than replace them if the new prompt adds one.
    old_meals = dict(current.get("completed_meals") or {})
    new_meals = dict(incoming.get("completed_meals") or {})
    if old_meals or new_meals:
        retained["completed_meals"] = old_meals | new_meals

    # Do not let a future change accidentally reintroduce a transient key through the
    # retained dictionary if the retention set grows later.
    for key in _TRANSIENT_KEYS:
        retained.pop(key, None)

    merged = merge(retained, incoming)
    merged["fresh_plan_revision"] = True
    return merged


def _resolve_one(target: str, rows: list[dict], now: datetime) -> dict | None:
    target_n = deterministic._norm(target)
    active = deterministic._active(rows)
    exact = [r for r in active if deterministic._norm(r.get("title")) == target_n]
    if len(exact) == 1:
        return exact[0]
    contained = [r for r in active if target_n and target_n in deterministic._norm(r.get("title"))]
    if len(contained) == 1:
        return contained[0]
    matches = deterministic._resolve_many(target, rows, now)
    return matches[0] if len(matches) == 1 else None


def _cancelled_rows(text: str, rows: list[dict], now: datetime) -> list[dict]:
    found: list[dict] = []
    for match in _CANCEL_RE.finditer(_norm(text)):
        target = match.group("target").strip(" ,-:")
        row = _resolve_one(target, rows, now)
        if row and all(str(x.get("id")) != str(row.get("id")) for x in found):
            found.append(row)
    return found


def _move_range(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    low = _norm(text)
    if not _MOVE_RE.search(low):
        return None
    if not re.search(r"\b(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low):
        return None
    return qd._extract_time_range(low, now)


def _duration_near(text: str, keyword_re: str, default: int, *, low: int, high: int) -> int:
    match = re.search(
        rf"{keyword_re}.{{0,40}}?(?:for\s+)?(\d+(?:\.\d+)?)\s*(hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        _norm(text), re.I,
    )
    if not match:
        return default
    value = float(match.group(1))
    unit = match.group(2).lower()
    minutes = int(round(value * 60 if unit.startswith("h") else value))
    return max(low, min(high, minutes))


def _at(day, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), settings.tz)


def _sequence_blocks(text: str, now: datetime) -> tuple[list[dict], dict, list[str]]:
    """Estimate an immediate bath -> nap -> dinner sequence when the order is explicit."""
    low = _norm(text)
    positions = [
        (_BATH_RE.search(low), "bath"),
        (_NAP_RE.search(low), "nap"),
        (_DINNER_RE.search(low), "dinner"),
        (_SWIM_RE.search(low), "swim"),
    ]
    if not all(m for m, _ in positions) or "now" not in low:
        return [], {}, []
    indexes = [m.start() for m, _ in positions]
    if indexes != sorted(indexes):
        return [], {}, []

    bath_minutes = _duration_near(low, r"(?:bathe|bath|shower)", 25, low=10, high=90)
    nap_minutes = _duration_near(low, r"(?:nap|sleep)", 90, low=20, high=240)
    dinner_minutes = _duration_near(low, r"(?:dinner)", 45, low=20, high=120)

    cursor = now.replace(second=0, microsecond=0)
    bath_start = cursor
    bath_end = bath_start + timedelta(minutes=bath_minutes)
    nap_start = bath_end + timedelta(minutes=5)
    nap_end = nap_start + timedelta(minutes=nap_minutes)
    # Do not invent a 16:00 "dinner" merely because a 90-minute nap ended early.
    # If the user supplied no clock, use a realistic earliest dinner but keep the
    # estimate flexible/visible rather than pretending it is a hard appointment.
    dinner_start = max(nap_end + timedelta(minutes=15), _at(now.date(), 17, 30))
    dinner_end = dinner_start + timedelta(minutes=dinner_minutes)

    blocks = [
        {
            "label": "Bathe / shower",
            "start": bath_start.isoformat(),
            "end": bath_end.isoformat(),
            "source": "human-reality-plan-revision",
            "certainty": "medium",
            "planning_estimate": True,
        },
        {
            "label": "Nap / rest",
            "start": nap_start.isoformat(),
            "end": nap_end.isoformat(),
            "source": "human-reality-plan-revision",
            "certainty": "medium",
            "planning_estimate": True,
        },
        {
            "label": "Dinner at home",
            "meal": "dinner",
            "start": dinner_start.isoformat(),
            "end": dinner_end.isoformat(),
            "nominal_end": dinner_end.isoformat(),
            "uncertainty_minutes": 0,
            "source": "human-meal-context",
            "certainty": "medium",
            "planning_estimate": True,
            "location": "home",
        },
    ]
    ctx = {
        "home_base_active": True,
        "dinner_location": "home",
        "intent_dinner_start": dinner_start.isoformat(),
        "intent_dinner_end": dinner_end.isoformat(),
    }
    notes = [
        f"Planning-only real-life estimates: bathe {bath_minutes}m → nap {nap_minutes}m → dinner at home {dinner_minutes}m. These are temporary blocks, not new TickTick tasks."
    ]
    return blocks, ctx, notes


def _prior_intent_edges(prior: dict, rows: list[dict], now: datetime) -> list[tuple[str, str]]:
    """Recover only adjacency edges from the immediately previous intake sequence."""
    order = list(prior.get("intent_exact_order") or [])
    if len(order) < 2:
        return []
    resolved: list[str | None] = []
    for title in order:
        row = _resolve_one(str(title), rows, now)
        resolved.append(str(row.get("id")) if row else None)
    edges: list[tuple[str, str]] = []
    by_id = {str(r.get("id")): r for r in rows if r.get("id")}
    for prereq, target in zip(resolved, resolved[1:]):
        if not prereq or not target or prereq == target:
            continue
        deps = {str(x) for x in ((by_id.get(target) or {}).get("meta") or {}).get("dependencies", [])}
        if prereq in deps:
            edges.append((target, prereq))
    return edges


def _remove_superseded_edges(parsed: dict, edges: list[tuple[str, str]], rows: list[dict]) -> None:
    by_id = {str(r.get("id")): r for r in rows if r.get("id")}
    for target_id, prereq_id in edges:
        row = by_id.get(target_id)
        if not row:
            continue
        deps = [str(x) for x in ((row.get("meta") or {}).get("dependencies") or []) if str(x) != prereq_id]
        item = deterministic._ensure_update(parsed, row)
        item.setdefault("meta_patch", {})["dependencies"] = deps
        item["reason"] = "Plan revision removed superseded temporary ordering"


def _strip_revision_noise(parsed: dict, text: str) -> None:
    """Remove parser noise only for the clause that this deterministic layer handled."""
    normalized = _norm(text)
    parsed["clarifications"] = [
        item for item in (parsed.get("clarifications") or [])
        if _norm(item.get("text")) not in {normalized, "change of plans"}
    ]
    warnings = []
    for warning in parsed.get("warnings") or []:
        low = _norm(warning)
        if any(x in low for x in (
            "unclear whether this is work or a planning instruction",
            "i heard a dependency",
            "could not match both tasks confidently",
        )) and ("church" in low or "swim" in low or "math" in low or "physics" in low or normalized[:40] in low):
            continue
        warnings.append(warning)
    parsed["warnings"] = list(dict.fromkeys(warnings))


def apply_revision_semantics(parsed: dict, text: str, rows: list[dict], now: datetime,
                             prior_context: dict | None = None) -> dict:
    if not _is_fresh_revision(text):
        return parsed

    prior = deepcopy(prior_context or {})
    parsed = dict(parsed)
    parsed["tasks"] = [deepcopy(x) for x in (parsed.get("tasks") or [])]
    parsed["notes"] = list(parsed.get("notes") or [])
    parsed["warnings"] = list(parsed.get("warnings") or [])
    parsed["clarifications"] = list(parsed.get("clarifications") or [])

    # A fresh human plan should not persist relation updates inferred from the same
    # prose.  The new sequence becomes plan-local below unless the user explicitly
    # said "depends on" / "prerequisite".
    if not re.search(r"\b(?:depends?\s+on|prerequisite|requires?)\b", _norm(text)):
        parsed["tasks"] = [
            x for x in parsed["tasks"]
            if not (
                str(x.get("line") or "") == "deterministic relationship"
                or str(x.get("reason") or "").startswith("Depends on ")
            )
        ]

    ctx = dict(parsed.get("context") or {})
    ctx.update({
        "date": now.date().isoformat(),
        "source": "quick-dump",
        "fresh_plan_revision": True,
        "replan_requested": True,
        "replan_scope": "today",
        "replan_from": now.isoformat(),
        "catch_up_missed": False,
        "preserve_unfinished": True,
        "defer_discretionary": False,
    })

    cancelled = _cancelled_rows(text, rows, now)
    if cancelled:
        ids = [str(r.get("id")) for r in cancelled]
        titles = [str(r.get("title") or "Commitment") for r in cancelled]
        ctx["suppressed_fixed_task_ids_today"] = ids
        ctx["suppressed_fixed_titles_today"] = titles
        parsed["notes"].append(
            "Today's cancelled commitment(s) are removed from today's planning truth: " + ", ".join(titles) + "."
        )

    move = _move_range(text, now)
    if move and cancelled:
        start, end = move
        overrides = []
        for row in cancelled:
            item = deterministic._ensure_update(parsed, row)
            item["fixed_start"] = start.isoformat()
            item["fixed_end"] = end.isoformat()
            item["reason"] = "Explicit one-off fixed commitment reschedule"
            overrides.append({
                "task_id": str(row.get("id")),
                "title": str(row.get("title") or "Commitment"),
                "from_date": now.date().isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
        ctx["fixed_overrides"] = overrides
        ctx["minimum_horizon_days"] = max(2, int(ctx.get("minimum_horizon_days") or 1))
        parsed["minimum_horizon_days"] = ctx["minimum_horizon_days"]
        parsed["notes"].append(
            "Explicit fixed-event override understood: " + ", ".join(x["title"] for x in overrides)
            + f" → {start.strftime('%a %H:%M')}–{end.strftime('%H:%M')}."
        )

    # The prior Quick Dump may have persisted temporary dependency edges.  A clearly
    # stated change of plan is authority to remove only the edges that came from the
    # immediately previous exact intake sequence.
    old_edges = _prior_intent_edges(prior, rows, now)
    if old_edges:
        _remove_superseded_edges(parsed, old_edges, rows)
        ctx["superseded_dependency_edges"] = [
            {"target_id": target, "prerequisite_id": prereq} for target, prereq in old_edges
        ]
        parsed["notes"].append("Superseded ordering from the previous Quick Dump was cleared before building the new plan.")

    blocks, sequence_ctx, sequence_notes = _sequence_blocks(text, now)
    if blocks:
        # New reality replaces, rather than appends to, the previous outing story.
        ctx["temporary_blocks"] = blocks
        ctx.update(sequence_ctx)
        parsed["notes"].extend(sequence_notes)

    low = _norm(text)
    swim_rows = deterministic._resolve_many("swim", rows, now) if _SWIM_RE.search(low) else []
    if swim_rows:
        swim_ids = [str(r.get("id")) for r in swim_rows if r.get("id")]
        ctx["intent_today_ids"] = swim_ids
        if blocks:
            dinner = next((b for b in blocks if b.get("meal") == "dinner"), None)
            if dinner:
                ctx["plan_local_earliest"] = {
                    tid: dinner["end"] for tid in swim_ids
                }
        parsed["notes"].append("Swimming remains a today goal after dinner; meal recovery/travel logic still applies.")

    # "Maybe Math or Physics if time allows" is optional capacity-fill, not a must-do.
    optional_ids: list[str] = []
    if _OPTIONAL_STUDY_RE.search(low) and re.search(r"\bmath\b", low) and re.search(r"\bphysics\b", low):
        math_rows = deterministic._resolve_many("math", rows, now)
        physics_rows = deterministic._resolve_many("physics", rows, now)
        for row in [*math_rows, *physics_rows]:
            rid = str(row.get("id") or "")
            if rid and rid not in optional_ids:
                optional_ids.append(rid)
        if optional_ids:
            ctx["optional_today_ids"] = optional_ids
            if swim_rows:
                swim_ids = [str(r.get("id")) for r in swim_rows if r.get("id")]
                ctx["plan_local_dependencies"] = {tid: swim_ids for tid in optional_ids}
            ctx["optional_choice_groups"] = [{
                "label": "Math or Physics if time allows",
                "task_ids": optional_ids,
                "max_required": 0,
            }]
            parsed["notes"].append("Math/Physics are optional after swimming: schedule them only if real capacity remains; neither is forced today.")

    if blocks and swim_rows:
        swim_title = str(swim_rows[0].get("title") or "Swimming")
        ctx["intent_exact_order"] = ["Bathe / shower", "Nap / rest", "Dinner at home", swim_title, "Optional study if time allows"]

    # An explicitly cancelled outing invalidates the old return-home/outside story.
    if cancelled:
        ctx["home_base_active"] = True
        if _DINNER_RE.search(low):
            ctx["dinner_location"] = "home"
        parsed["notes"].append("Previous outside/return-home assumptions are superseded by this change of plans; dinner is treated as at home unless a new outing is stated.")

    # Never create permanent chores merely because the user narrated an immediate
    # bath/nap/dinner sequence. Explicit "add/create task" language still survives.
    if blocks and not re.search(r"\b(?:add|create)\s+(?:a\s+)?(?:new\s+)?task\b|\bnew\s+task\b", low):
        activity_words = ("bath", "bathe", "shower", "sleep", "nap", "dinner", "swim")
        parsed["tasks"] = [
            change for change in parsed["tasks"]
            if not (
                change.get("action") == "create"
                and any(word in _norm(change.get("line") or change.get("title")) for word in activity_words)
            )
        ]

    parsed["context"] = ctx
    _strip_revision_noise(parsed, text)
    parsed["notes"] = list(dict.fromkeys(parsed["notes"]))
    parsed["warnings"] = list(dict.fromkeys(parsed["warnings"]))
    return parsed


def _same_day(dt: datetime | None, day) -> bool:
    return bool(dt and dt.astimezone(settings.tz).date() == day)


def _busy_matches_title(block: BusyBlock, titles: list[str], day) -> bool:
    if not _same_day(block.start, day):
        return False
    label = deterministic._norm(block.label)
    for title in titles:
        t = deterministic._norm(title)
        if t and (label == t or t in label or label in t):
            return True
    return False


def revision_aware_plan(base_plan: Callable):
    def wrapped(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        cfg = deepcopy(config or {})
        ctx = cfg.get("_quick_context") or {}
        task_list = list(tasks or [])
        metas = deepcopy(meta_map or {})
        busy_blocks = list(busy or [])

        if ctx.get("date") == start.date().isoformat() and ctx.get("fresh_plan_revision"):
            suppressed = {str(x) for x in (ctx.get("suppressed_fixed_task_ids_today") or [])}
            titles = [str(x) for x in (ctx.get("suppressed_fixed_titles_today") or [])]
            if suppressed:
                task_list = [
                    t for t in task_list
                    if not (str(t.id) in suppressed and _same_day(t.start, start.date()))
                ]
                # A cancelled occurrence may still exist in Google Calendar for a few
                # minutes after TickTick moves. Explicit user cancellation wins over the
                # stale duplicate only when its label matches the cancelled commitment.
                busy_blocks = [
                    b for b in busy_blocks
                    if not _busy_matches_title(b, titles, start.date())
                ]

            for tid, raw_time in (ctx.get("plan_local_earliest") or {}).items():
                try:
                    dt = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=settings.tz)
                    dt = dt.astimezone(settings.tz)
                except Exception:
                    continue
                raw = metas.setdefault(str(tid), {})
                old = raw.get("earliest")
                old_dt = None
                if old:
                    try:
                        old_dt = datetime.fromisoformat(str(old).replace("Z", "+00:00"))
                        if old_dt.tzinfo is None:
                            old_dt = old_dt.replace(tzinfo=settings.tz)
                        old_dt = old_dt.astimezone(settings.tz)
                    except Exception:
                        old_dt = None
                raw["earliest"] = max(dt, old_dt or dt).isoformat()

            for tid, deps in (ctx.get("plan_local_dependencies") or {}).items():
                raw = metas.setdefault(str(tid), {})
                existing = [str(x) for x in (raw.get("dependencies") or [])]
                raw["dependencies"] = list(dict.fromkeys([*existing, *[str(x) for x in deps]]))
                # Optional study is allowed to disappear when the evening is full.
                if str(tid) in {str(x) for x in (ctx.get("optional_today_ids") or [])}:
                    raw["must_finish"] = False

        result = base_plan(task_list, metas, busy_blocks, start, horizon_days, cfg, mastery_map)
        try:
            segments, warnings, diagnostics = result
            diagnostics = dict(diagnostics or {})
            if ctx.get("fresh_plan_revision"):
                diagnostics["fresh_plan_revision"] = True
                diagnostics["suppressed_fixed_today"] = list(ctx.get("suppressed_fixed_task_ids_today") or [])
                diagnostics["optional_today_ids"] = list(ctx.get("optional_today_ids") or [])
            return segments, warnings, diagnostics
        except Exception:
            return result

    return wrapped


def install_plan_revision_patch(base_plan: Callable):
    """Install context lifecycle + attach semantics and return the wrapped planner."""
    base_merge = intake.merge_context

    def merged(current, incoming):
        return revision_merge_context(current, incoming, base_merge)

    intake.merge_context = merged

    base_attach = contextual._attach_real_life_bounds

    def attach(parsed, text, rows, now):
        parsed = base_attach(parsed, text, rows, now)
        try:
            prior = service.get_quick_context() or {}
        except Exception:
            prior = {}
        return apply_revision_semantics(parsed, text, rows, now, prior)

    contextual._attach_real_life_bounds = attach
    return revision_aware_plan(base_plan)


__all__ = [
    "install_plan_revision_patch", "apply_revision_semantics", "revision_merge_context",
    "revision_aware_plan",
]
