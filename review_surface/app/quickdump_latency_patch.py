from __future__ import annotations

"""Latency-first Quick Dump behavior.

Preview and Apply use the same live task representation. A durable display snapshot
may be older than TickTick, which would make Apply reject an unchanged request with
409. Simple prompts still avoid model calls and unnecessary project/section scans.

Local-first is intentionally scoped here, at the interactive Quick Dump boundary. Direct
semantic callers retain full semantic behavior, while simple or grammar-complete Quick Dumps
make zero model calls. Complex Quick Dumps get one bounded semantic window; if anything in
that stack hangs, the request is cancelled and rerun through deterministic LOCAL_REVIEW.
"""

import asyncio
import re
import time
from datetime import datetime

from fastapi import HTTPException

from .config import settings
from . import main as _main
from . import smart_routing as routing
from .performance_patch import invalidate_ticktick_cache
from .semantic_cost_gate import is_simple_local_prompt, semantic_timeout_seconds
from .general_planning_intent_patch import (
    compile_reviewed_planning_intent,
    looks_like_general_planning_intent,
    persist_compiled_review,
)
from .explicit_dated_activity_patch import (
    compile_explicit_dated_activity,
    looks_like_explicit_dated_activity,
)
from .bare_day_replan_patch import (
    install_bare_day_replan_patch,
    is_bare_today_replan,
)

_INSTALLED = False
_CREATE_PATCHED = False


def _cached_structure() -> list[dict]:
    cache = routing._STRUCTURE_CACHE
    if not cache:
        return []
    cached_at, _ids, rows = cache
    if time.monotonic() - cached_at >= routing._STRUCTURE_TTL:
        return []
    return [dict(x, sections=[dict(y) for y in x.get("sections", [])]) for x in rows]


def _remove_existing_preview_route(app) -> None:
    keep = []
    for route in app.router.routes:
        methods = set(getattr(route, "methods", set()) or set())
        if getattr(route, "path", None) == "/api/quick-dump/smart-preview" and "POST" in methods:
            continue
        keep.append(route)
    app.router.routes[:] = keep


def _patch_routed_creation_cache() -> None:
    global _CREATE_PATCHED
    if _CREATE_PATCHED or getattr(routing._create_task_routed, "_invalidates_ticktick_cache", False):
        return
    _CREATE_PATCHED = True
    base = routing._create_task_routed

    async def create_and_invalidate(*args, **kwargs):
        result = await base(*args, **kwargs)
        invalidate_ticktick_cache()
        return result

    create_and_invalidate._invalidates_ticktick_cache = True
    routing._create_task_routed = create_and_invalidate


async def _forced_local_review(review_intake, text, rows, config, submitted_at):
    from .semantic_plan import LOCAL_REVIEW

    token = LOCAL_REVIEW.set(True)
    try:
        # Local compilation should be very fast. A cap here prevents an unrelated future
        # wrapper from turning a free/local request into another indefinite wait.
        return await asyncio.wait_for(
            review_intake(text, rows, config, submitted_at),
            timeout=3.0,
        )
    finally:
        LOCAL_REVIEW.reset(token)


async def _review_with_hard_local_fallback(review_intake, text, rows, config, submitted_at):
    """Bound complex semantic review, then recover deterministically."""
    try:
        return await asyncio.wait_for(
            review_intake(text, rows, config, submitted_at),
            timeout=semantic_timeout_seconds(config),
        )
    except TimeoutError:
        parsed = await _forced_local_review(
            review_intake, text, rows, config, submitted_at
        )
        parsed.setdefault("warnings", []).append(
            "Semantic interpretation timed out; Quick Dump recovered immediately with local interpretation."
        )
        parsed["preview_timeout_recovered"] = True
        return parsed


def _strip_narrative_container_creates(parsed: dict, text: str, submitted_at: datetime) -> dict:
    """Final Quick Dump boundary: a day narrative is a container, never a task.

    Earlier semantic/native compilers may independently recognize valid atomic activities
    and still leave one create whose title/line is the untouched multi-step sentence.
    Once a prompt is structurally recognized as current reality followed by future stages,
    remove only that container artifact (and any create copied from the current-state
    lead clause). Atomic future activities remain untouched.
    """
    try:
        from .natural_chain_intake_patch import _split
        split = _split(text, submitted_at)
    except Exception:
        split = None
    if not split:
        return parsed

    current_text, _stages = split
    norm = lambda value: " ".join(str(value or "").replace("’", "'").strip(" ,.;:-").casefold().split())
    whole = norm(text)
    current = norm(current_text)

    kept, removed = [], []
    for change in parsed.get("tasks") or []:
        if change.get("action") != "create":
            kept.append(change)
            continue
        title = norm(change.get("title"))
        line = norm(change.get("line"))
        # The entire user narrative and the leading happening-now clause are source
        # instructions/reality, never durable work.
        if (whole and (title == whole or line == whole)) or (
            current and (title == current or line == current)
        ):
            removed.append(str(change.get("title") or change.get("line") or "narrative"))
            continue
        kept.append(change)

    parsed["tasks"] = kept
    if removed:
        parsed.setdefault("notes", []).append(
            "Removed narrative-container create artifact(s); only atomic future activities are reviewable: "
            + ", ".join(dict.fromkeys(removed)) + "."
        )
        parsed["narrative_container_guard"] = True
    return parsed


async def _quickdump_review(review_intake, text, rows, config, submitted_at):
    """Route grammar-complete prompts locally; reserve the model for real ambiguity."""
    from .early_commitment import looks_like_early_future_commitment
    from .temporal_engine import resolve_date_reference
    temporal_plan_control = bool(
        re.search(
            r"\b(?:plan|replan|reschedule|schedule|organize|organise)\b",
            str(text or ""),
            re.I,
        )
        and resolve_date_reference(str(text or ""), submitted_at)
    )
    if (
        is_simple_local_prompt(text)
        or looks_like_general_planning_intent(text)
        or looks_like_explicit_dated_activity(text, submitted_at)
        or looks_like_early_future_commitment(text, submitted_at)
        or temporal_plan_control
        or is_bare_today_replan(text)
    ):
        parsed = await _forced_local_review(
            review_intake, text, rows, config, submitted_at
        )
        parsed["preview_local_first"] = True
        return parsed
    return await _review_with_hard_local_fallback(
        review_intake, text, rows, config, submitted_at
    )


def install_quickdump_latency_patch(app):
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    # Teach the mature date-general day planner that a bare imperative such as
    # "Plan my day" means today from the submission clock. This is deterministic
    # schedule control, not a task and not semantic ambiguity.
    install_bare_day_replan_patch()
    _patch_routed_creation_cache()
    _remove_existing_preview_route(app)

    @app.post("/api/quick-dump/smart-preview")
    async def fast_smart_preview(payload: routing.SmartDumpPayload):
        # Preserve the old connection contract without paying for a network fetch.
        probe = routing.TickTickClient()
        if not probe.connected:
            raise HTTPException(401, "Connect TickTick first")

        config = _main.get_config()
        submitted_at = datetime.now(settings.tz)
        # Review current task state, not the durable display cache: Apply validates
        # this exact representation before writing. Never approve stale task states.
        from .runtime_resilience_patch import REQUIRE_FRESH_TASKS
        invalidate_ticktick_cache(projects=False)
        token = REQUIRE_FRESH_TASKS.set(True)
        try:
            tt, _items, projects, rows = await routing._live_rows()
        finally:
            REQUIRE_FRESH_TASKS.reset(token)
        _main._save_task_snapshot(rows)

        from .intake_contract import review_intake
        parsed = await _quickdump_review(
            review_intake, payload.text, rows, config, submitted_at
        )

        # Final Quick Dump intent boundary. The mature semantic/local review above is
        # untouched. First compile explicit dated personal activities (which have
        # creation authority); then compile named-existing-work planner commands.
        parsed = compile_explicit_dated_activity(
            parsed, payload.text, rows, config, submitted_at
        )
        parsed = compile_reviewed_planning_intent(
            parsed, payload.text, rows, config, submitted_at
        )

        # Absolute HTTP/UI boundary: never let a whole current-state + future-chain
        # sentence render as an additional CREATE card. This runs after every compiler
        # but before the reviewed result is persisted or routed.
        parsed = _strip_narrative_container_creates(parsed, payload.text, submitted_at)
        from .task_title_guard import guard_parsed_creates
        guard_parsed_creates(parsed, payload.text)
        from .intake_contract import validate_task_creation
        validate_task_creation(parsed)
        persist_compiled_review(parsed, payload.text, rows, config)

        creates = [x for x in parsed.get("tasks", []) if x.get("action") == "create"]

        # Most replans/state updates create nothing, so they need no project/section scan.
        if not creates:
            parsed["routing"] = []
            parsed["project_structure"] = _cached_structure()
            parsed["needs_project"] = False
            parsed["preview_fast_path"] = True
            return parsed

        # NEW tasks need current routing destinations. Fetch live account state only now;
        # interpretation itself is not repeated, so this never doubles semantic API cost.
        if tt is None or projects is None:
            tt, _items, projects, live_rows = await routing._live_rows()
            rows_for_routing = live_rows
            _main._save_task_snapshot(live_rows)
        else:
            rows_for_routing = rows

        structure = await routing._project_structure(tt, projects)
        valid_projects = {str(x.get("id")) for x in structure}
        fallback = payload.fallback_project_id if payload.fallback_project_id in valid_projects else None
        suggestions = routing._decorate_routing(parsed, rows_for_routing, structure, fallback)
        unresolved = [x for x in suggestions if not x.get("project_id")]
        parsed["routing"] = suggestions
        parsed["project_structure"] = structure
        parsed["needs_project"] = bool(unresolved)
        parsed["preview_fast_path"] = True
        if unresolved:
            parsed.setdefault("warnings", []).append(
                "Choose a destination beside each unrouted NEW task. Existing tasks stay in their current lists."
            )
        return parsed

    # Final runtime invariants. Fresh-day reset is deliberately installed at the
    # persisted-context reader, not by rewriting the mature low-level carry helper.
    # Every plan still uses a fresh TickTick/Calendar snapshot. Created tasks are seeded
    # into the same Apply pass if TickTick's list endpoint is briefly eventually-consistent.
    from . import production_reliability_patch as reliability
    from . import service as _service
    from .forward_create_plan_patch import install_forward_create_plan_patch
    from .fresh_day_runtime_patch import install_fresh_day_runtime_patch
    from .future_ticktick_crosscheck_patch import install_future_ticktick_crosscheck
    from .timed_event_unicode_compat import install_timed_event_unicode_compat

    reliability._install_fresh_day_slate = lambda: None
    reliability.install_production_reliability_patch()
    install_timed_event_unicode_compat()
    install_fresh_day_runtime_patch()
    _service.plan = install_future_ticktick_crosscheck(_service.plan)
    install_forward_create_plan_patch()


__all__ = [
    "install_quickdump_latency_patch", "_review_with_hard_local_fallback",
    "_quickdump_review", "_forced_local_review",
]
