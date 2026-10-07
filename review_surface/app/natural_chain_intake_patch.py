from __future__ import annotations

"""Deterministic current-state + future-chain intake.

Natural speech often starts with what is happening *now* and then elides the subject:

    "I'm eating lunch now, then swim later, then go to ..., then go home"

The ordinary current-state parser must not consume durations from later clauses or discard
the future chain.  This final review wrapper handles only that structural class.  It is
model-free, preview-only, and reuses the mature task inference/logistics metadata.
"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime

from .config import settings
from . import intake_contract as contract
from . import human_context_patch as human
from . import language_intake as intake
from . import personal_intents as personal
from . import quickdump as qd
from . import day_plan_activities as day_activities
from . import reality_patch as reality
from .live_activity import remaining_minutes, strip_activity_duration
from .semantic_plan import LOCAL_REVIEW

_INSTALLED = False
_PREVIEW_PREFIX = "__day_new__:"

_CONNECTOR = re.compile(
    r"\s*,?\s*\b(?:and\s+then|then|after\s+that|afterwards?|following\s+that|"
    r"followed\s+by|subsequently|next\s+up|next|from\s+there|after\s+which|"
    r"later\s+on|once\s+(?:that(?:'s|\s+is)\s+done|this(?:'s|\s+is)\s+done|"
    r"i(?:'m|\s+am)\s+done|we(?:'re|\s+are)\s+done))\b\s*",
    re.I,
)
_FINAL_HOME = re.compile(
    r"^(?:(?:i|we)\s+(?:will|'ll|want\s+to|need\s+to|plan\s+to|intend\s+to|"
    r"am\s+going\s+to|are\s+going\s+to|'m\s+going\s+to|'re\s+going\s+to)\s+)?"
    r"(?:go|head|return|travel|come|get)(?:\s+back)?\s+home\s*$|"
    r"^(?:head|go|come)\s+back\s*$",
    re.I,
)
_OPTIONAL = re.compile(r"^(?:maybe|perhaps|possibly|if\s+(?:i|we)\s+can|if\s+time\s+allows)\b", re.I)
_EXPLICIT_FUTURE_DAY = re.compile(
    r"\b(?:tomorrow|tmr|next\s+(?:day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.I,
)


def _split(text: str, now: datetime):
    value = re.sub(r"\s+", " ", str(text or "").replace("’", "'")).strip()
    # Natural comma sequencing may omit "then": "eating now, after 5 minutes swim".
    # Insert a structural connector while preserving the relative phrase on the next stage.
    relative_number = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|fifty|sixty|ninety)"
    value = re.sub(
        r",\s*(?=(?:in|after)\s+(?:about\s+|around\s+|roughly\s+)?"
        + relative_number
        + r"\s*(?:minutes?|mins?|min|m|hours?|hrs?|hr|h)\b)",
        ", then ",
        value,
        flags=re.I,
    )
    if not value or value.endswith("?") or _EXPLICIT_FUTURE_DAY.search(value):
        return None
    first = _CONNECTOR.search(value)
    if not first:
        return None
    current = value[: first.start()].strip(" ,.;:-")
    future = value[first.end() :].strip(" ,.;:-")
    if not current or not future:
        return None

    # Require the first clause to be actual current reality.  This deliberately does
    # not capture an ordinary future chain such as "eat lunch then study then swim".
    current_block = human._availability_block(current, now)
    current_kind = personal.reality_kind(current)
    current_now = bool(re.search(r"\b(?:now|right\s+now|currently|at\s+the\s+moment|atm|just\s+(?:finished|arrived|returned|got|came|started))\b", current, re.I))
    if not current_block and not current_kind:
        return None
    if not current_now and not (
        current_block and current_block.get("source") in {"human-meal-context", "human-reality-context"}
    ):
        return None

    stages = [part.strip(" ,.;:-") for part in _CONNECTOR.split(future) if part.strip(" ,.;:-")]
    if not stages:
        return None
    # The general day compiler owns meals, recovery, bedtime, fixed appointments
    # and multi-sentence controls. This fast path only understands atomic activity
    # chains; consuming those controls here can create a durable "sleep" task or
    # lose a meal/appointment. Delegate the whole request with its context intact.
    if any(
        day_activities.meal_stage(stage)
        or day_activities._BEDTIME.match(stage)
        or day_activities._REST.match(stage)
        or re.search(r"[.;]|\b(?:from\s+\d|closes?\s+at|opens?\s+at)\b", stage, re.I)
        for stage in stages
    ):
        return None
    return current, stages


def _ref(change: dict, day: str, title: str) -> str:
    if change.get("action") == "create":
        preview = str(change.get("preview_task_id") or "")
        if not preview:
            preview = _PREVIEW_PREFIX + hashlib.sha256((day + "|" + title.casefold()).encode()).hexdigest()[:16]
            change["preview_task_id"] = preview
        return preview
    return str(change.get("task_id") or "")


def _action_title(stage: str):
    raw = re.sub(r"^(?:maybe|perhaps|possibly)\s+", "", str(stage or ""), flags=re.I).strip()
    raw = re.sub(
        r"^(?:(?:i|we)\s+(?:will|'ll|wanna|want\s+to|need\s+to|have\s+to|gotta|"
        r"plan\s+to|intend\s+to|aim\s+to|am\s+going\s+to|are\s+going\s+to)\s+|"
        r"i(?:'m|\s+am)\s+(?:going\s+to|gonna|planning\s+to|trying\s+to)\s+|"
        r"we(?:'re|\s+are)\s+(?:going\s+to|planning\s+to)\s+|"
        r"i(?:'d|\s+would)\s+(?:like|love|prefer)\s+to\s+|"
        r"we(?:'d|\s+would)\s+(?:like|love|prefer)\s+to\s+|"
        r"(?:let\s+me|please)\s+)",
        "",
        raw,
        flags=re.I,
    ).strip()
    raw = re.sub(r"\blater\b", "", raw, flags=re.I).strip(" ,.;:-")
    from .relative_start import strip_start_language
    raw = strip_start_language(raw)
    without_duration = strip_activity_duration(raw).strip(" ,.;:-")

    # A destination plus a purpose is one visit/activity; travel remains scheduler
    # geometry rather than a durable "go to" task.
    venue_match = re.match(
        r"^(?:go|head|travel|drive|walk|ride)\s+(?:over\s+)?to\s+(.+?)"
        r"(?=\s+to\s+(?:get|grab|fetch|buy|pick\s*up|collect|drop\s*off|visit|see|return|deliver)\b|$)"
        r"|^(?:drop|stop|swing)\s+by\s+(.+?)"
        r"(?=\s+to\s+(?:get|grab|fetch|buy|pick\s*up|collect|drop\s*off|visit|see|return|deliver)\b|$)",
        without_duration,
        re.I,
    )
    if venue_match:
        venue = (venue_match.group(1) or venue_match.group(2) or "").strip(" ,.;:-")
        remainder = without_duration[venue_match.end():].strip()
        remainder = re.sub(r"^to\s+", "", remainder, flags=re.I).strip()
        title = f"{remainder} at {venue}" if remainder else f"Visit {venue}"
        return re.sub(r"\s+", " ", title).strip(), venue

    # Reuse the established visit normalizer for ordinary "go to X" wording.
    normalized, venue = day_activities.venue_activity(without_duration)
    return re.sub(r"\s+", " ", normalized).strip(), venue


def _merge_current_and_chain(current: dict, text: str, stages: list[str], rows: list[dict], config: dict, now: datetime) -> dict:
    result = deepcopy(current)
    result.setdefault("tasks", [])
    result.setdefault("notes", [])
    result.setdefault("warnings", [])
    result.setdefault("intents", [])
    result.setdefault("clarifications", [])
    ctx = deepcopy(result.get("context") or {})
    day = now.date().isoformat()
    ctx.update({
        "date": day,
        "source": "quick-dump",
        "replan_requested": True,
        "replan_scope": "today",
        "replan_from": now.isoformat(),
        "preserve_unfinished": True,
        "minimum_horizon_days": 1,
    })
    current_meal = next(
        (
            str(block.get("meal") or "").lower()
            for block in (ctx.get("temporary_blocks") or [])
            if isinstance(block, dict) and block.get("source") == "human-meal-context" and block.get("meal")
        ),
        None,
    )

    working = list(rows or [])
    previous_ref = None
    previous_outing_ref = None
    ordered_titles = []
    terminal_home = False

    for index, stage in enumerate(stages):
        if _FINAL_HOME.match(stage):
            terminal_home = True
            result["intents"].append({"kind": "reality", "text": stage, "status": "compiled"})
            result["notes"].append(
                "Return home is part of the outing/logistics chain; it was not created as a TickTick task."
            )
            continue

        optional = bool(_OPTIONAL.match(stage))

        # An explicit reference to a stored Project Intelligence campaign is not a
        # generic new task. Reuse its truthful next work package, so phrases such as
        # "study physics in preparation for SPHL" inherit the saved competition scope,
        # resources, progress and current daily budget.
        from .project_reference import resolve_project_study_stage
        project_stage = resolve_project_study_stage(stage, working, now)
        if project_stage:
            campaign = project_stage.get("campaign") or {}
            package = project_stage.get("package") or {}
            blocked = project_stage.get("blocked")
            if blocked:
                reason = (
                    "The stored project was found, but its next materialized task is missing; "
                    "refresh Project Intelligence before creating a duplicate."
                    if blocked == "materialized_task_missing"
                    else "The stored project was found, but no work package is ready yet."
                )
                result["clarifications"].append({"text": stage, "reason": reason})
                result["intents"].append({"kind": "goal", "text": stage, "status": "blocked"})
                continue

            ref = str(project_stage.get("task_id") or project_stage.get("virtual_task_id") or "")
            if not ref:
                result["clarifications"].append({
                    "text": stage,
                    "reason": "The project reference was understood but no schedulable work item could be resolved.",
                })
                continue

            campaign_id = str(campaign.get("id") or "")
            package_key = str(package.get("key") or "")
            resolved_title = str(project_stage.get("title") or package.get("title") or campaign.get("goal") or "Project work")
            ctx.setdefault("requested_project_campaign_ids", [])
            if campaign_id and campaign_id not in ctx["requested_project_campaign_ids"]:
                ctx["requested_project_campaign_ids"].append(campaign_id)
            if campaign_id and package_key:
                requested = ctx.setdefault("requested_project_work_packages", {}).setdefault(campaign_id, [])
                if package_key not in requested:
                    requested.append(package_key)
            ctx.setdefault("intent_date_goals", {})[ref] = day
            ctx.setdefault("intent_today_ids", [])
            if ref not in ctx["intent_today_ids"]:
                ctx["intent_today_ids"].append(ref)
            if optional:
                ctx.setdefault("optional_today_ids", [])
                if ref not in ctx["optional_today_ids"]:
                    ctx["optional_today_ids"].append(ref)
            if previous_ref:
                deps = ctx.setdefault("plan_local_dependencies", {}).setdefault(ref, [])
                if previous_ref not in deps:
                    deps.append(previous_ref)

            previous_ref = ref
            ordered_titles.append(resolved_title)
            result["intents"].append({
                "kind": "goal", "text": stage, "status": "compiled",
                "campaign_id": campaign_id, "work_package_key": package_key,
            })
            result["notes"].append(
                f"Stored project reference understood: {campaign.get('goal') or campaign_id} → {resolved_title}. "
                "No generic study task was created. Session length is chosen by the scheduler from the package's "
                "remaining effort, the campaign's current daily budget and today's real legal capacity."
            )
            continue

        title, venue = _action_title(stage)
        if not title:
            result["clarifications"].append({
                "text": stage,
                "reason": "Name the activity in this stage more clearly; no task was invented.",
            })
            continue

        inferred = intake._task_inference(title, working, config, now)
        changes = list(inferred.get("tasks") or [])
        if len(changes) != 1:
            result["clarifications"].append({
                "text": stage,
                "reason": "This stage did not resolve to one activity; no duplicate or guessed task was created.",
            })
            continue

        change = deepcopy(changes[0])
        change["line"] = stage
        change["intake_kind"] = "task"
        patch = dict(change.get("meta_patch") or {})

        from .relative_start import start_delay_minutes, strip_relative_start
        delay_minutes, delay_source = start_delay_minutes(stage)
        duration_stage = strip_relative_start(stage)
        exact = remaining_minutes(duration_stage)
        if exact:
            patch.update(
                duration_minutes=int(exact),
                remaining_minutes=int(exact),
                _explicit_activity_minutes=int(exact),
                confidence="high",
            )
        if delay_minutes is not None:
            patch["earliest"] = (now + __import__("datetime").timedelta(minutes=delay_minutes)).isoformat()
            patch["_relative_start_minutes"] = int(delay_minutes)
            patch["_relative_start_source"] = delay_source

        if optional:
            patch["intent_optional"] = True
        if venue:
            duration = int(patch.get("remaining_minutes") or patch.get("duration_minutes") or 30)
            patch.update(
                location=venue,
                excursion=True,
                splittable=False,
                min_chunk=duration,
                max_chunk=duration,
            )
        change["meta_patch"] = patch

        ref = _ref(change, day, title)
        if not ref:
            continue

        existing_refs = {
            str(item.get("task_id") or item.get("preview_task_id") or "")
            for item in result["tasks"]
        }
        if ref not in existing_refs:
            result["tasks"].append(change)

        ctx.setdefault("intent_date_goals", {})[ref] = day
        ctx.setdefault("intent_today_ids", [])
        if ref not in ctx["intent_today_ids"]:
            ctx["intent_today_ids"].append(ref)
        if optional:
            ctx.setdefault("optional_today_ids", [])
            if ref not in ctx["optional_today_ids"]:
                ctx["optional_today_ids"].append(ref)

        if previous_ref:
            deps = ctx.setdefault("plan_local_dependencies", {}).setdefault(ref, [])
            if previous_ref not in deps:
                deps.append(previous_ref)
        elif current_meal and re.search(r"\bswim(?:ming)?\b", title, re.I):
            ctx.setdefault("after_meal_task_ids", {})[ref] = current_meal

        is_outing = bool(venue or reality._family_from_text(title))
        if is_outing and previous_outing_ref and previous_outing_ref != ref:
            # Consecutive narrated outings are one physical journey. Do not force an
            # intermediate return home: the later outing's outbound leg becomes the
            # disclosed venue-to-venue transition estimate. The final outing still
            # receives its ordinary return-home leg.
            journey = ctx.setdefault("journey_state", {})
            owners = journey.setdefault("replaces_return_owner_ids", [])
            if previous_outing_ref not in owners:
                owners.append(previous_outing_ref)
            journey["source_text"] = text
            journey["multi_stop_chain"] = True
            result["notes"].append(
                "Consecutive outings stay in one journey; no intermediate trip home is inserted. "
                "Unknown venue-to-venue travel uses the disclosed saved/default estimate."
            )
        if is_outing:
            previous_outing_ref = ref

        previous_ref = ref
        ordered_titles.append(title)
        result["intents"].append({"kind": "task", "text": stage, "status": "compiled"})

        if change.get("action") == "create":
            pseudo = {
                "id": ref,
                "project_id": "",
                "title": title,
                "status": 0,
                "kind": "TEXT",
                "tags": change.get("tags_add") or [],
                "meta": deepcopy(patch),
            }
            working.append(pseudo)

    if ordered_titles:
        ctx["intent_exact_order"] = ordered_titles
        if terminal_home:
            journey = ctx.setdefault("journey_state", {})
            journey["ends_at_home"] = True
            journey["source_text"] = text
        result["notes"].append(
            "Natural current-state chain preserved in order: " + " → ".join(ordered_titles)
            + (" → home." if terminal_home else ".")
        )

    # A current meal's own duration must come only from its clause.  Subsequent activity
    # durations are represented on those activities and cannot alter this block.
    result["context"] = ctx
    result["reality_preview"] = deepcopy(ctx.get("temporary_blocks") or [])
    result["minimum_horizon_days"] = 1
    result["line_count"] = len(result.get("intents") or [])
    result["interpreter_mode"] = "deterministic"
    result["parser_version"] = str(result.get("parser_version") or "typed-intake-1") + "+natural-chain-1"
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["notes"] = list(dict.fromkeys(result["notes"]))
    return result


def install_natural_chain_intake_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        split = _split(text, stamp)
        if not split:
            return await base(text, rows, config, stamp)

        current_text, stages = split
        token = LOCAL_REVIEW.set(True)
        try:
            current = await base(current_text, rows, config, stamp)
        finally:
            LOCAL_REVIEW.reset(token)

        # The leading clause is known current reality. It may influence the schedule,
        # but it must never become or update a durable TickTick task merely because an
        # older parser also found task-like words inside it.
        current["tasks"] = []
        current.setdefault("notes", []).append(
            "Current-state lead clause was kept as temporary reality only; no durable task was created from it."
        )

        result = _merge_current_and_chain(current, text, stages, rows, config, stamp)

        # Defensive boundary: a multi-step narrative is an instruction container, not
        # an additional work item. Earlier semantic/local layers may occasionally emit
        # the untouched whole sentence alongside its atomic stages; remove that artifact.
        whole = re.sub(r"\s+", " ", str(text or "")).strip(" ,.;:-").casefold()
        filtered = []
        for change in result.get("tasks") or []:
            if change.get("action") != "create":
                filtered.append(change)
                continue
            title = re.sub(r"\s+", " ", str(change.get("title") or "")).strip(" ,.;:-").casefold()
            line = re.sub(r"\s+", " ", str(change.get("line") or "")).strip(" ,.;:-").casefold()
            if title == whole or line == whole:
                result.setdefault("notes", []).append(
                    "Ignored the full multi-step sentence as a task; only its atomic future activities remain."
                )
                continue
            filtered.append(change)
        result["tasks"] = filtered

        contract.validate_task_creation(result)

        # Synthetic clause reviews write their own fingerprints. Restore the reviewed
        # original request so Apply validates exactly the text the user actually sent.
        try:
            saved = json.loads(contract.get_kv("intake_review") or "{}")
            saved.update(
                text_hash=contract._fingerprint(text),
                snapshot=contract._snapshot(rows, config),
                parsed=result,
            )
            contract.set_kv("intake_review", json.dumps(saved))
        except Exception:
            pass
        return result

    review._natural_chain_intake = True
    contract.review_intake = review


__all__ = ["install_natural_chain_intake_patch", "_split", "_action_title"]
