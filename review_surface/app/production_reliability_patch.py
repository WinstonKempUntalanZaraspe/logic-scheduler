from __future__ import annotations

"""Production invariants for real-life timed events and day rollover.

This patch intentionally sits at final runtime boundaries instead of teaching one prompt
at a time.  It provides four guarantees:

* definite timed real-life events are fixed commitments, while accompanying leave/return
  narration is logistics context rather than a second task;
* a task created by Quick Dump is available to the *same* replan even when TickTick's
  list endpoint is briefly eventually-consistent;
* a new calendar day starts from live TickTick/Calendar truth instead of yesterday's
  conversational planning story (only reality literally still active may cross midnight);
* schedule responses are forward-looking: elapsed historical rows are not rendered as
  executable schedule content.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Callable

from .config import settings
from . import db
from . import quickdump as qd
from . import quickdump_latency_patch as latency
from . import smart_routing as routing
from . import personal_intents as intents
from . import service
from .ticktick import task_from_api

_EVENT_HINT = re.compile(
    r"\b(?:hang(?:ing)?\s*out|friends?|party|birthday|wedding|concert|movie|cinema|"
    r"meeting|appointment|doctor|dentist|dinner\s+with|lunch\s+with|breakfast\s+with|"
    r"date\b|church|mass\b|class\b|lecture|tutorial|cca\b|training|practice|match\b|game\b|"
    r"family\s+(?:outing|dinner|lunch|event)|event\b)\b",
    re.I,
)
_OPTIONAL = re.compile(r"\b(?:maybe|perhaps|possibly|might|could|if\s+i\s+can|if\s+possible|not\s+sure)\b", re.I)
_CLOCK = r"(?:midnight|noon|\d{1,2}(?::\d{2})?\s*(?:am|pm))"
_LEAVE = re.compile(rf"\b(?:i\s*(?:'ll|will)?\s*)?(?:leave|head\s+out|go\s+out)(?:\s+home)?\s+(?:around|at|by)\s+(?P<t>{_CLOCK})", re.I)
_RETURN = re.compile(rf"\b(?:i\s*(?:'ll|will)?\s*)?(?:probably\s+)?(?:be|get|come|arrive|return)\s+(?:back\s+)?(?:home\s+)?(?:by|around|at)\s+(?P<t>{_CLOCK})", re.I)

_PENDING_SEEDS_KEY = "quickdump_pending_created_seeds_v1"
_SEED_TTL_SECONDS = 45


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("’", "'").strip().lower()).strip(" ,.;:-")


def _dt(value):
    if not value:
        return None
    try:
        out = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=settings.tz)
        return out.astimezone(settings.tz)
    except (TypeError, ValueError):
        return None


def _event_clause(text: str, now: datetime) -> tuple[str, tuple[datetime, datetime]] | tuple[None, None]:
    pieces = [p.strip(" ,;\n") for p in re.split(r"(?<=[.!?])\s+|[\r\n;]+", str(text or "")) if p.strip(" ,;\n")]
    for piece in pieces or [str(text or "")]:
        timerange = qd._extract_time_range(piece, now)
        if not timerange or _OPTIONAL.search(piece):
            continue
        # Sleep/rest/current-state and ordinary meals are planning context, not
        # permission to create a permanent TickTick task merely because clocks appear.
        if intents.reality_kind(piece) or qd.TIMED_CONTEXT_ONLY.search(piece):
            continue
        # Exact clock ranges are authoritative for any single positive activity,
        # not merely social/event vocabulary. This covers chores, study, coding,
        # errands, etc. while still rejecting state/logistics sentences.
        eventish = bool(_EVENT_HINT.search(piece))
        if not eventish:
            from .conversational_activity import is_explicit_activity
            candidate = qd._strip_task_modifiers(piece)
            candidate = re.sub(r"^(?:please\s+)?i\s*(?:'ll|will|am\s+going\s+to|(?:'m|am)\s+going\s+to)\s+", "", candidate, flags=re.I)
            candidate = re.sub(r"^(?:i\s+)?(?:want|need)\s+to\s+", "", candidate, flags=re.I)
            eventish = is_explicit_activity(candidate)
        if eventish:
            return piece, timerange
    return None, None


def looks_like_definite_timed_event(text: str, now: datetime | None = None) -> bool:
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    clause, timerange = _event_clause(text, now)
    return bool(clause and timerange)


def _event_title(clause: str) -> str:
    value = str(clause or "").replace("’", "'")
    value = qd._strip_task_modifiers(value)
    value = re.sub(r"\b(?:replan|plan)\s+around\s+(?:it|this)\b", "", value, flags=re.I)
    value = re.sub(r"^(?:please\s+)?i\s*(?:'m|am)\s+", "", value, flags=re.I)
    value = re.sub(r"^i\s*(?:'ll|will)\s+", "", value, flags=re.I)
    value = re.sub(r"^(?:i\s+)?(?:want|need)\s+to\s+", "", value, flags=re.I)
    value = re.sub(r"^going\s+to\s+(?:go\s+to\s+)?", "", value, flags=re.I)
    value = re.sub(r"^going\s+to\s+(?:a\s+)?", "", value, flags=re.I)
    value = re.sub(r"^hanging\s+out\b", "Hang out", value, flags=re.I)
    value = re.sub(r"^hang\s+out\b", "Hang out", value, flags=re.I)
    value = re.sub(r"\b(?:today|tomorrow|tonight)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.;:-")
    if value.lower().startswith("a party"):
        value = value[2:].strip()
    return value[:1].upper() + value[1:] if value else "Timed commitment"


def _match_existing(title: str, rows: list[dict]) -> tuple[dict | None, bool]:
    wanted = qd._norm(title)
    active = [r for r in rows if int(r.get("status") or 0) == 0 and str(r.get("kind") or "TEXT").upper() != "NOTE"]
    exact = [r for r in active if qd._norm(r.get("title")) == wanted]
    if len(exact) == 1:
        return exact[0], False
    if len(exact) > 1:
        return None, True
    contained = [r for r in active if wanted and (wanted in qd._norm(r.get("title")) or qd._norm(r.get("title")) in wanted)]
    if len(contained) == 1:
        return contained[0], False
    if len(contained) > 1:
        return None, True
    return None, False


def _clock_on_day(raw: str, day, *, after: datetime | None = None) -> datetime | None:
    low = _norm(raw)
    if low == "midnight":
        minute = 0
    elif low == "noon":
        minute = 12 * 60
    else:
        minute = qd._clock_to_minutes(low)
    if minute is None:
        return None
    out = datetime.combine(day, datetime.min.time(), settings.tz) + timedelta(minutes=minute)
    if after and out <= after:
        out += timedelta(days=1)
    return out


def _logistics_blocks(text: str, start: datetime, end: datetime, owner: str, title: str) -> tuple[list[dict], datetime | None]:
    leave_match = _LEAVE.search(str(text or ""))
    return_match = _RETURN.search(str(text or ""))
    leave = _clock_on_day(leave_match.group("t"), start.date()) if leave_match else None
    back = _clock_on_day(return_match.group("t"), start.date(), after=end) if return_match else None
    blocks: list[dict] = []
    if leave and leave < start:
        blocks.append({
            "label": f"Travel / get to {title}", "start": leave.isoformat(), "end": start.isoformat(),
            "source": "human-reality-explicit-event-logistics", "kind": "logistics",
            "planning_estimate": False, "planning_only": True, "owner_id": owner,
        })
    if back and back > end:
        blocks.append({
            "label": f"Return home after {title}", "start": end.isoformat(), "end": back.isoformat(),
            "source": "human-reality-explicit-event-logistics", "kind": "logistics",
            "planning_estimate": False, "planning_only": True, "owner_id": owner, "location": "home",
        })
    return blocks, back


def _same_source(item: dict, text: str, clause: str) -> bool:
    value = _norm(item.get("text") or item.get("line") or "")
    return bool(value and (value == _norm(text) or value == _norm(clause) or value in _norm(text)))


def compile_definite_timed_event(parsed: dict, text: str, rows: list[dict], config: dict, now: datetime | None = None) -> dict:
    """Compile a definite atomic activity time range as one fixed commitment."""
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    clause, timerange = _event_clause(text, now)
    if not clause or not timerange:
        return parsed
    start, end = timerange
    title = _event_title(clause)
    existing, ambiguous = _match_existing(title, rows)
    result = deepcopy(parsed)
    result.setdefault("tasks", [])
    result.setdefault("clarifications", [])
    result.setdefault("warnings", [])
    result.setdefault("notes", [])
    result.setdefault("intents", [])
    if ambiguous:
        result["clarifications"] = [q for q in result["clarifications"] if not _same_source(q, text, clause)]
        result["clarifications"].append({"text": clause, "reason": "Multiple existing commitments have the same name; choose which one to move/update."})
        return result

    if existing:
        ref = str(existing.get("id") or "")
        change = {
            "action": "update", "task_id": ref, "title": str(existing.get("title") or title), "line": clause,
            "reason": "Definite timed real-life commitment; preserve this exact interval and replan around it.",
            "tags_add": ["fixed"], "fixed_start": start.isoformat(), "fixed_end": end.isoformat(),
            "priority": int(existing.get("priority") or 0),
            "meta_patch": {"autoschedule": False, "splittable": False, "category": "social"},
            "intake_kind": "task",
        }
    else:
        ref = "__event_new__:" + hashlib.sha256((start.isoformat() + "|" + end.isoformat() + "|" + title.lower()).encode()).hexdigest()[:16]
        change = {
            "action": "create", "preview_task_id": ref, "title": title, "line": clause,
            "reason": "Definite timed real-life commitment; create on Apply and replan around it immediately.",
            "tags_add": ["fixed"], "fixed_start": start.isoformat(), "fixed_end": end.isoformat(),
            "priority": 0, "meta_patch": {"autoschedule": False, "splittable": False, "category": "social"},
            "intake_kind": "task",
        }

    # Replace parser guesses sourced from the event or its leave/return narration.  A
    # sentence such as "I'll leave home at 6 and be back by midnight" is geometry,
    # never a second flexible task.
    kept = []
    for old in result.get("tasks") or []:
        old_title = _norm(old.get("title"))
        logistics_guess = bool(re.search(r"\b(?:leave\s+home|back\s+by|back\s+home|return\s+home|be\s+back)\b", old_title))
        if _same_source(old, text, clause) or logistics_guess:
            continue
        kept.append(old)
    kept.append(change)
    result["tasks"] = kept

    ctx = deepcopy(result.get("context") or {})
    ctx.update({
        "date": now.date().isoformat(), "source": "quick-dump", "replan_requested": True,
        "replan_from": now.isoformat(), "preserve_unfinished": True,
        "replan_scope": "today" if start.date() == now.date() else "tomorrow" if start.date() == now.date() + timedelta(days=1) else start.date().isoformat(),
        "minimum_horizon_days": max(int(ctx.get("minimum_horizon_days") or 1), min(14, (start.date() - now.date()).days + 1)),
    })
    ctx.setdefault("intent_date_goals", {})[ref] = start.date().isoformat()
    blocks, back = _logistics_blocks(text, start, end, ref, title)
    if blocks:
        current = [b for b in ctx.get("temporary_blocks") or [] if not (isinstance(b, dict) and b.get("source") == "human-reality-explicit-event-logistics" and str(b.get("owner_id") or "") == ref)]
        ctx["temporary_blocks"] = current + blocks
    if back:
        ctx["return_home_not_after"] = back.isoformat()
    result["context"] = ctx
    result["minimum_horizon_days"] = ctx["minimum_horizon_days"]

    result["clarifications"] = [q for q in result.get("clarifications") or [] if not _same_source(q, text, clause)]
    result["warnings"] = [w for w in result.get("warnings") or [] if "unclear whether this is work or a planning instruction" not in _norm(w)]
    for intent in result.get("intents") or []:
        if _same_source(intent, text, clause) and intent.get("status") == "needs-input":
            intent.update(kind="task", status="compiled")
    result["notes"].append(f"Fixed real-life event: {title} {start.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%H:%M')}; flexible work replans around it.")
    if blocks:
        result["notes"].append("Leave/return wording was kept as protected outing logistics, not created as a separate task.")
    result["notes"] = list(dict.fromkeys(result["notes"]))
    return result


def _install_timed_event_quickdump_hook() -> None:
    if getattr(latency, "_timed_event_reliability_installed", False):
        return
    latency._timed_event_reliability_installed = True
    old_simple = latency.is_simple_local_prompt
    old_compiler = latency.compile_explicit_dated_activity

    def local_prompt(text: str) -> bool:
        return looks_like_definite_timed_event(text) or old_simple(text)

    def composed(parsed, text, rows, config, now=None):
        parsed = compile_definite_timed_event(parsed, text, rows, config, now)
        return old_compiler(parsed, text, rows, config, now)

    latency.is_simple_local_prompt = local_prompt
    latency.compile_explicit_dated_activity = composed


def _install_fresh_day_slate() -> None:
    base = intents.carry_active_reality
    if getattr(base, "_fresh_day_slate", False):
        return

    def fresh(ctx, now):
        ctx = deepcopy(ctx or {})
        now = now.astimezone(settings.tz) if getattr(now, "tzinfo", None) else now.replace(tzinfo=settings.tz)
        source_day = str(ctx.get("date") or "")
        if not source_day or source_day == now.date().isoformat():
            return base(ctx, now)

        # Midnight is a planning reset.  Only reality that is literally active at
        # this instant may cross the boundary; future blocks and yesterday's future
        # plan/ordering/contingency story are deliberately discarded.
        active = []
        for block in ctx.get("temporary_blocks") or []:
            if not isinstance(block, dict) or block.get("completed"):
                continue
            start, end = _dt(block.get("start")), _dt(block.get("end"))
            if start and end and start <= now < end:
                active.append(deepcopy(block))
        if not active:
            return None
        result = {
            "date": now.date().isoformat(), "source": "quick-dump", "temporary_blocks": active,
            "replan_requested": True, "replan_from": now.isoformat(), "personal_scheduler_version": "9.0",
        }
        current = next((b for b in active if b.get("source") == "personal-current-activity"), None)
        if current:
            result["current_activity"] = deepcopy(current)
            if current.get("location"):
                result.update(current_location=current["location"], location_reported_at=current.get("reported_at"))
        sleeping = [b for b in active if b.get("kind") == "sleep"]
        if sleeping:
            result.update(activity_state="sleeping", sleep_until=max(b["end"] for b in sleeping))
        return result

    fresh._fresh_day_slate = True
    intents.carry_active_reality = fresh


def _load_pending_seeds() -> list[dict]:
    raw = db.get_kv(_PENDING_SEEDS_KEY)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save_pending_seed(seed: dict) -> None:
    now = datetime.now(settings.tz)
    rows = [r for r in _load_pending_seeds() if _dt(r.get("expires_at")) and _dt(r.get("expires_at")) > now]
    rows = [r for r in rows if str(r.get("id")) != str(seed.get("id"))]
    rows.append(seed)
    db.set_kv(_PENDING_SEEDS_KEY, json.dumps(rows))


def _install_same_apply_seed() -> None:
    base_create = routing._create_task_routed
    if getattr(base_create, "_same_apply_seed", False):
        return

    async def create_seeded(tt, project_id, title, start=None, end=None, *, tags=None, priority=0, column_id=None):
        created = await base_create(tt, project_id, title, start, end, tags=tags, priority=priority, column_id=column_id)
        task_id = str((created or {}).get("id") or "")
        if task_id:
            raw = dict(created or {})
            raw.update({
                "id": task_id, "projectId": str(raw.get("projectId") or project_id),
                "title": raw.get("title") or title, "tags": list(raw.get("tags") or tags or []),
                "priority": int(raw.get("priority") or priority or 0), "kind": str(raw.get("kind") or "TEXT"),
                "status": int(raw.get("status") or 0), "columnId": raw.get("columnId") or column_id,
            })
            if start is not None and not raw.get("startDate"):
                raw["startDate"] = start.isoformat()
            if end is not None and not raw.get("dueDate"):
                raw["dueDate"] = end.isoformat()
            raw["expires_at"] = (datetime.now(settings.tz) + timedelta(seconds=_SEED_TTL_SECONDS)).isoformat()
            _save_pending_seed(raw)
        return created

    create_seeded._same_apply_seed = True
    routing._create_task_routed = create_seeded

    base_snapshot = service.snapshot
    if getattr(base_snapshot, "_same_apply_seed", False):
        return

    async def snapshot_seeded(horizon_days: int = 8):
        tt, tasks, projects, busy = await base_snapshot(horizon_days)
        now = datetime.now(settings.tz)
        seeds = [r for r in _load_pending_seeds() if _dt(r.get("expires_at")) and _dt(r.get("expires_at")) > now]
        known = {str(t.id) for t in tasks}
        for raw in seeds:
            if str(raw.get("id") or "") in known:
                continue
            try:
                task = task_from_api(raw)
            except Exception:
                continue
            if task.id and task.is_actionable and task.status == 0:
                tasks.append(task)
                known.add(task.id)
        # One scheduling snapshot has now had a chance to consume the seeds.  Live
        # TickTick remains authoritative on every later request.
        db.set_kv(_PENDING_SEEDS_KEY, "")
        return tt, tasks, projects, busy

    snapshot_seeded._same_apply_seed = True
    service.snapshot = snapshot_seeded


def install_forward_preview(base_plan: Callable):
    """Return only remaining timeline rows; history stays in TickTick, not the preview."""
    if getattr(base_plan, "_forward_preview_only", False):
        return base_plan

    def forward(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        segments, warnings, diagnostics = base_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map or {})
        cutoff = _dt(start) or datetime.now(settings.tz)
        segments = [s for s in (segments or []) if s.end > cutoff]
        diagnostics = deepcopy(diagnostics or {})
        hidden = 0
        timeline_keys = ("fixed_timeline", "reality_timeline", "flexible_meals", "human_uncertainty_buffers")
        for key in timeline_keys:
            kept = []
            for row in diagnostics.get(key) or []:
                end = _dt(row.get("end")) if isinstance(row, dict) else None
                if end and end <= cutoff:
                    hidden += 1
                    continue
                kept.append(row)
            if key in diagnostics:
                diagnostics[key] = kept
        if diagnostics.get("planning_gaps"):
            diagnostics["planning_gaps"] = [r for r in diagnostics["planning_gaps"] if not _dt(r.get("end")) or _dt(r.get("end")) > cutoff]

        # Capacity cards should describe days still represented in the forward view.
        visible_dates = {s.start.date().isoformat() for s in segments}
        for key in ("fixed_timeline", "reality_timeline", "flexible_meals", "human_uncertainty_buffers"):
            for row in diagnostics.get(key) or []:
                begin = _dt(row.get("start")) if isinstance(row, dict) else None
                if begin:
                    visible_dates.add(begin.date().isoformat())
        if diagnostics.get("capacity") and visible_dates:
            diagnostics["capacity"] = [r for r in diagnostics["capacity"] if str(r.get("date")) in visible_dates]
        diagnostics["forward_preview"] = {"cutoff": cutoff.isoformat(), "hidden_elapsed_rows": hidden, "history_visible": False}
        return segments, warnings, diagnostics

    forward._forward_preview_only = True
    return forward


def install_production_reliability_patch() -> None:
    _install_timed_event_quickdump_hook()
    _install_fresh_day_slate()
    _install_same_apply_seed()


__all__ = [
    "install_production_reliability_patch", "install_forward_preview",
    "compile_definite_timed_event", "looks_like_definite_timed_event",
]
