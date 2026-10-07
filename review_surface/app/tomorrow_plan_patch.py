from __future__ import annotations

"""Deterministic support for conversational future-day planning.

A sentence such as "I want to plan for tomorrow, go swimming after breakfast, then
math and physics, then church, then maybe gym" is a schedule request, not a request to
create a task named "plan for".  This layer resolves only existing actionable TickTick
items, records temporary day-plan constraints, checks an existing fixed church event,
and leaves NOTE items completely untouched.
"""

import re
from copy import deepcopy
from datetime import datetime, timedelta, time

from .config import settings
from . import contextual_intake_patch as contextual
from . import deterministic_intake_patch as deterministic
from . import language_intake as intake
from . import quickdump as qd

_INSTALLED = False
_BASE_CLASSIFY = intake.classify
_BASE_ATTACH = None
_BASE_SEMANTIC = None

# Anchor the command at the beginning. A requested output label such as
# "The cleaned schedule for tomorrow" contains the same words but is a noun phrase,
# not a request to replan; matching it used to steal an output item in production.
_PLAN_TOMORROW = re.compile(
    r"^\s*(?:please\s+)?(?:i(?:'d| would)\s+like\s+to\s+|i\s+(?:want|need|have)\s+to\s+)?"
    r"(?:plan|replan|reschedule|schedule|organize|organise)"
    r"(?:\s+(?:my\s+)?(?:day|schedule))?\s+(?:for\s+)?(?:tomorrow|tmr)\b",
    re.I,
)
_OPTIONAL = re.compile(r"\b(?:maybe|perhaps|possibly|if\s+(?:there(?:'s| is)\s+)?time|if\s+possible)\b", re.I)


def _is_tomorrow_plan(text: str) -> bool:
    return bool(_PLAN_TOMORROW.search(str(text or "")))


def classify_future_plan(text, numbered=False, output_section=False, now=None):
    """Keep a whole future-day narrative out of the generic single-goal matcher."""
    if _is_tomorrow_plan(text):
        return "replan", text
    return _BASE_CLASSIFY(text, numbered, output_section, now)


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=settings.tz)
        return result.astimezone(settings.tz)
    except (TypeError, ValueError):
        return None


def _is_fixed(row: dict) -> bool:
    return "fixed" in {str(x).lower().lstrip("#") for x in (row.get("tags") or [])}


def _unique(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        rid = str(row.get("id") or "")
        if rid and rid not in seen:
            seen.add(rid)
            out.append(row)
    return out


def _resolve_named(name: str, rows: list[dict], now: datetime) -> list[dict]:
    """Resolve only live tasks; deterministic._active already excludes NOTE items."""
    aliases = {
        "swim": ("swimming", "swim", "pool"),
        "math": ("math", "maths", "calculus"),
        "physics": ("physics",),
        "church": ("church", "mass"),
        "gym": ("gym", "workout"),
    }
    active = deterministic._active(rows)
    keys = aliases[name]
    exactish = [
        row for row in active
        if any(re.search(rf"\b{re.escape(word)}\b", deterministic._norm(row.get("title"))) for word in keys)
    ]
    if name in {"swim", "church", "gym"}:
        tomorrow = now.date() + timedelta(days=1)
        exactish.sort(key=lambda r: 0 if (_dt(r.get('start') or r.get('startDate')) or now).date() == tomorrow else 1)
    if name in {"swim", "church", "gym"} and exactish:
        exact = [row for row in exactish if deterministic._norm(row.get("title")) in keys]
        if exact:
            return _unique(exact[:2])
    resolved = deterministic._resolve_many(name, active, now)
    if resolved:
        return _unique(resolved)
    return _unique(exactish[:4])


def _stages(text: str, rows: list[dict], now: datetime) -> dict:
    """Resolve the broad stages without treating commentary as task titles."""
    low = str(text or "")
    lead = _PLAN_TOMORROW.search(low)
    body = low[lead.end():] if lead else low
    body = body.lstrip(" ,;:-")
    pieces = [p.strip(" ,.;") for p in re.split(r"\s*,?\s*\bthen\b\s+", body, flags=re.I) if p.strip(" ,.;")]

    result = {"swim": [], "math": [], "physics": [], "church": [], "gym": [], "gym_optional": False,
              "swim_after_breakfast": False, "church_claim": None, "mentioned": set(), "sequence": [], "gym_night": False}
    for piece in pieces:
        normalized = deterministic._norm(piece)
        stage = []
        if re.search(r"\b(?:swim|swimming|pool)\b", normalized):
            result['mentioned'].add('swim')
            stage.append('swim')
            result["swim"] = _resolve_named("swim", rows, now)
            result["swim_after_breakfast"] = bool(re.search(r"\bafter\s+breakfast\b", normalized))
        if re.search(r"\b(?:math|maths|calculus)\b", normalized):
            result['mentioned'].add('math')
            stage.append('math')
            result["math"] = _resolve_named("math", rows, now)
        if re.search(r"\bphysics\b", normalized):
            result['mentioned'].add('physics')
            stage.append('physics')
            result["physics"] = _resolve_named("physics", rows, now)
        if re.search(r"\b(?:church|mass)\b", normalized):
            result['mentioned'].add('church')
            stage.append('church')
            result["church"] = _resolve_named("church", rows, now)
            result["church_claim"] = qd._extract_time_range(piece, now)
        if re.search(r"\b(?:gym|workout)\b", normalized):
            result['mentioned'].add('gym')
            stage.append('gym')
            result["gym"] = _resolve_named("gym", rows, now)
            result["gym_optional"] = bool(_OPTIONAL.search(piece))
            result['gym_night'] = bool(re.search(r'\b(?:night|tonight|evening)\b', piece, re.I))
        if stage:
            result['sequence'].append(stage)

    # The user's exact prompt says "math and physics" in one stage. If punctuation
    # caused a missed split, recover those explicit words directly from the full body.
    full = deterministic._norm(body)
    if not result["swim"] and re.search(r"\b(?:swim|swimming)\b", full):
        result["swim"] = _resolve_named("swim", rows, now)
        result["swim_after_breakfast"] = "after breakfast" in full
    if not result["math"] and re.search(r"\b(?:math|maths|calculus)\b", full):
        result["math"] = _resolve_named("math", rows, now)
    if not result["physics"] and "physics" in full:
        result["physics"] = _resolve_named("physics", rows, now)
    if not result["church"] and re.search(r"\b(?:church|mass)\b", full):
        result["church"] = _resolve_named("church", rows, now)
        result["church_claim"] = qd._extract_time_range(low, now)
    if not result["gym"] and re.search(r"\b(?:gym|workout)\b", full):
        result["gym"] = _resolve_named("gym", rows, now)
        result["gym_optional"] = bool(_OPTIONAL.search(low))
    return result


def _drop_false_goal_question(parsed: dict, text: str) -> None:
    """Remove only the parser artifact caused by interpreting 'plan for' as a task."""
    if not _is_tomorrow_plan(text):
        return
    parsed["clarifications"] = [
        item for item in (parsed.get("clarifications") or [])
        if "name one existing task clearly" not in str(item.get("reason") or "").lower()
    ]
    parsed["warnings"] = [
        warning for warning in (parsed.get("warnings") or [])
        if "name one existing task clearly" not in str(warning).lower()
    ]
    for item in parsed.get("intents") or []:
        if item.get("status") == "needs-input" and deterministic._norm(item.get("text")) == deterministic._norm(text):
            item["kind"] = "replan"
            item["status"] = "compiled"


def attach_tomorrow_plan(parsed: dict, text: str, rows: list[dict], now: datetime) -> dict:
    assert _BASE_ATTACH is not None
    parsed = _BASE_ATTACH(parsed, text, rows, now)
    if not _is_tomorrow_plan(text):
        return parsed

    _drop_false_goal_question(parsed, text)
    info = _stages(text, rows, now)
    tomorrow = now.date() + timedelta(days=1)
    tomorrow_iso = tomorrow.isoformat()
    ctx = parsed.get("context") or {"date": now.date().isoformat(), "source": "quick-dump"}
    ctx["date"] = now.date().isoformat()
    ctx["source"] = "quick-dump"
    ctx["replan_requested"] = True
    # The planner still regenerates from now; the date-goals below constrain work to tomorrow.
    ctx["replan_scope"] = "tomorrow"
    ctx["replan_from"] = now.isoformat()
    ctx["minimum_horizon_days"] = max(2, int(ctx.get("minimum_horizon_days") or 1))
    parsed["minimum_horizon_days"] = max(2, int(parsed.get("minimum_horizon_days") or 1))

    date_goals = dict(ctx.get("intent_date_goals") or {})
    local_deps = {str(k): list(v) for k, v in (ctx.get("plan_local_dependencies") or {}).items()}
    local_earliest = dict(ctx.get("plan_local_earliest") or {})
    local_latest = dict(ctx.get("plan_local_latest_end") or {})
    optional = list(ctx.get("optional_date_goal_ids") or [])

    required_groups = [info["swim"], info["math"], info["physics"]]
    if not info['gym_optional']:
        required_groups.append(info['gym'])
    for group in required_groups:
        for row in group:
            if not _is_fixed(row):
                date_goals[str(row["id"])] = tomorrow_iso

    # Each explicit "then" creates a temporary stage edge. Activities joined by
    # "and" share a stage; their task metadata still controls any internal order.
    previous = []
    stage_index = {}
    for index, stage in enumerate(info['sequence']):
        current = _unique([r for name in stage for r in info[name]])
        for name in stage:
            stage_index[name] = index
        for row in current:
            rid = str(row.get("id") or "")
            if rid and previous and not _is_fixed(row):
                local_deps[rid] = list(dict.fromkeys([*local_deps.get(rid, []), *[str(r['id']) for r in previous if r['id'] != rid]]))
        if current:
            previous = current

    church = info["church"][0] if info["church"] else None
    church_start = _dt(church.get("start")) if church else None
    church_end = _dt(church.get("end")) if church else None
    if church_start and church_start.date() == tomorrow:
        # The narrative says study, then church. Keep the existing fixed appointment
        # authoritative and make the flexible study finish before it.
        for name in ('swim', 'math', 'physics', 'gym'):
            for row in info[name]:
                if not _is_fixed(row):
                    if stage_index.get(name, -1) < stage_index.get('church', -1):
                        local_latest[str(row["id"])] = church_start.isoformat()
                    elif church_end:
                        local_earliest[str(row["id"])] = church_end.isoformat()
        parsed.setdefault("notes", []).append(
            f"Confirmed existing Church: {church_start.strftime('%H:%M')}–{church_end.strftime('%H:%M') if church_end else '?'} tomorrow; it stays fixed."
        )
        claim = info.get("church_claim")
        if claim and (abs((claim[0] - church_start).total_seconds()) > 60 or (church_end and abs((claim[1] - church_end).total_seconds()) > 60)):
            parsed["notes"].append(
                f"Your Church estimate was checked against TickTick; the existing event is {church_start.strftime('%H:%M')}–{church_end.strftime('%H:%M') if church_end else '?'} and was not overwritten."
            )
    elif church:
        parsed.setdefault("notes", []).append("Found Church, but it is not scheduled tomorrow; no duplicate Church event was created.")
    elif 'church' in info['mentioned']:
        parsed.setdefault("clarifications", []).append({
            "text": "church tomorrow",
            "reason": "I could not find an existing Church task/event tomorrow. Nothing was created; add or identify it if Church should be fixed in this plan.",
        })

    # "Maybe gym" is optional. Missing optional work never blocks the rest of the plan.
    for row in info["gym"]:
        rid = str(row.get("id") or "")
        if not rid or _is_fixed(row):
            continue
        date_goals[rid] = tomorrow_iso
        if info['gym_optional'] and rid not in optional:
            optional.append(rid)
        if info['gym_night']:
            evening = datetime.combine(tomorrow, time(19, 0), settings.tz)
            local_earliest[rid] = max(_dt(local_earliest.get(rid)) or evening, evening).isoformat()
    if info["gym_optional"] and not info["gym"]:
        parsed.setdefault("notes", []).append("Optional gym was not matched to an existing task; I skipped it instead of inventing a new task.")

    if info["swim_after_breakfast"] and info["swim"]:
        ctx["after_meal_task_ids"] = dict(ctx.get("after_meal_task_ids") or {})
        for row in info["swim"]:
            ctx["after_meal_task_ids"][str(row["id"])] = "breakfast"
        parsed.setdefault("notes", []).append("Swimming is anchored after breakfast; the normal post-meal recovery rule still applies before the actual swim.")

    missing = []
    for name, label in (("swim", "Swimming"), ("math", "Math"), ("physics", "Physics"), ("gym", "Gym")):
        if name in info['mentioned'] and not info[name] and not (name == 'gym' and info['gym_optional']):
            missing.append(label)
    if missing:
        parsed.setdefault("clarifications", []).append({
            "text": ", ".join(missing),
            "reason": "I could not confidently match these required existing tasks: " + ", ".join(missing) + ". No duplicate tasks were created.",
        })

    ctx["intent_date_goals"] = date_goals
    ctx["plan_local_dependencies"] = local_deps
    ctx["plan_local_earliest"] = local_earliest
    ctx["plan_local_latest_end"] = local_latest
    ctx["optional_date_goal_ids"] = optional
    ctx["tomorrow_plan"] = {
        "date": tomorrow_iso,
        "swim_after_breakfast": bool(info["swim_after_breakfast"]),
        "required_ids": list(dict.fromkeys(str(r["id"]) for g in required_groups for r in g if r.get("id"))),
        "church_id": str(church.get("id")) if church and church.get("id") else None,
        "church_start": church_start.isoformat() if church_start and church_start.date() == tomorrow else None,
        "church_end": church_end.isoformat() if church_end and church_end.date() == tomorrow else None,
        "gym_ids": [str(r["id"]) for r in info["gym"] if r.get("id")],
        "gym_optional": bool(info["gym_optional"]),
    }
    parsed["context"] = ctx
    parsed["notes"] = list(dict.fromkeys(parsed.get("notes") or []))
    parsed["warnings"] = list(dict.fromkeys(parsed.get("warnings") or []))
    return parsed


def tomorrow_plan_planner(base_plan):
    """Apply temporary future-plan edges/windows to copied metadata only."""
    def plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
        cfg = deepcopy(config or {})
        ctx = cfg.get("_quick_context") or {}
        if not ctx.get("tomorrow_plan"):
            return base_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map)
        metas = deepcopy(meta_map or {})
        if ctx.get('after_meal_task_ids'):
            cfg['personal_policy'] = dict(cfg.get('personal_policy') or {}) | {'meal_protection': True}
        for tid, deps in (ctx.get("plan_local_dependencies") or {}).items():
            raw = metas.setdefault(str(tid), {})
            raw["dependencies"] = list(dict.fromkeys([*(raw.get("dependencies") or []), *[str(x) for x in deps]]))
        for tid, value in (ctx.get("plan_local_earliest") or {}).items():
            raw = metas.setdefault(str(tid), {})
            current = _dt(raw.get("earliest"))
            incoming = _dt(value)
            if incoming and (not current or incoming > current):
                raw["earliest"] = incoming.isoformat()
        for tid, value in (ctx.get("plan_local_latest_end") or {}).items():
            raw = metas.setdefault(str(tid), {})
            current = _dt(raw.get("latest_end"))
            incoming = _dt(value)
            if incoming and (not current or incoming < current):
                raw["latest_end"] = incoming.isoformat()
        for tid in ctx.get("optional_date_goal_ids") or []:
            metas.setdefault(str(tid), {})["intent_optional"] = True
        return base_plan(tasks, metas, busy, start, max(int(horizon_days), int(ctx.get("minimum_horizon_days") or 1)), cfg, mastery_map)
    return plan


def install_tomorrow_plan_patch(base_plan):
    global _INSTALLED, _BASE_ATTACH, _BASE_SEMANTIC
    if _INSTALLED:
        return tomorrow_plan_planner(base_plan)
    _INSTALLED = True
    _BASE_ATTACH = contextual._attach_real_life_bounds
    intake.classify = classify_future_plan
    # Grounded semantic validation delegates to this captured classifier.
    contextual._BASE_CLASSIFY = classify_future_plan
    contextual._attach_real_life_bounds = attach_tomorrow_plan
    _BASE_SEMANTIC = contextual.grounded_semantic_document

    async def future_semantic(text, now, rows, config, prior_context):
        lead = _PLAN_TOMORROW.search(str(text or ''))
        if lead:
            body = str(text)[lead.end():].strip(' ,.;:-')
            pieces = [p for p in re.split(r'\bthen\b', body, flags=re.I) if p.strip(' ,.;:-')]
            known = r'\b(?:swim|swimming|pool|math|maths|calculus|physics|church|mass|gym|workout)\b'
            activities = [p for stage in pieces for p in re.split(r'\band\b', stage, flags=re.I) if p.strip(' ,.;:-')]
            if not body or (activities and all(re.search(known, p, re.I) for p in activities)):
                # Source-grounded stage compilation is complete without a provider.
                # A model error, quota error or timeout cannot block this known grammar.
                return intake.extract_intents(text, now), 'local', None, [], []
        return await _BASE_SEMANTIC(text, now, rows, config, prior_context)

    contextual.grounded_semantic_document = future_semantic
    return tomorrow_plan_planner(base_plan)


__all__ = ["attach_tomorrow_plan", "classify_future_plan", "install_tomorrow_plan_patch", "tomorrow_plan_planner"]
