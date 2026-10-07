from __future__ import annotations

"""Final production entrypoint.

Loads every existing performance/intelligence layer first, then installs persistent
personal policy, context-aware schedule instructions, human-day logic, schedule quality,
plan integrity, generalized real-life context and a final Human Adjuster. The resulting
planner is designed for actual days rather than ideal calendar geometry: flexible meals,
sleep, duplicate review, cohesive split work, physical travel/context, overruns, human
timing variance, plan revisions, and obvious task-title semantics all survive through
production.
"""

from .performance_entrypoint import app  # noqa: F401
from . import performance_entrypoint as _performance
from .multiuser_patch import install_multiuser_patch
from .multiuser_runtime_fix import install_multiuser_runtime_fix

# Multi-user isolation must be active before later scheduler layers capture DB/cache
# helpers. APP_ACCESS_KEY now unlocks the app; it no longer identifies one shared account.
install_multiuser_patch(app)
install_multiuser_runtime_fix()

from . import main as _main
from . import service as _service
from . import entrypoint as _entrypoint
from . import personal_policy_patch as _policy
from . import human_day_patch as _human_day
from . import plan_quality_patch as _plan_quality
from . import plan_integrity_patch as _plan_integrity
from . import human_reality_patch as _human_reality  # extends Reality Guard families/functions
from . import human_adjuster_patch as _human_adjuster
from . import academic_context_patch as _academic_context
from . import schedule_instruction_patch as _schedule_instruction
from . import schedule_instruction_language_patch as _schedule_instruction_language  # noqa: F401
from . import human_context_patch as _human_context
from . import language_intake as _intake
from .contextual_intake_patch import install_contextual_intake_patch
from .contextual_intake_phrase_extension import extend_contextual_intake_phrases
from .contextual_intake_ui_compat import install_contextual_intake_ui_compat
from .semantic_compat_patch import install_semantic_compatibility
from .semantic_diagnostics_patch import install_semantic_diagnostics
from .deterministic_intake_patch import install_deterministic_intake_patch
from .tomorrow_plan_patch import install_tomorrow_plan_patch
from .plan_revision_patch import install_plan_revision_patch
from . import plan_revision_clause_patch as _plan_revision_clause_patch  # noqa: F401
from . import plan_revision_context_keys_patch as _plan_revision_context_keys_patch  # noqa: F401
from . import plan_revision_recurrence_patch as _plan_revision_recurrence_patch  # noqa: F401
from .plan_revision_apply_patch import install_plan_revision_apply_patch
from .outing_dependency_patch import install_outing_dependency_patch
from .deterministic_intake_polish import install_deterministic_intake_polish
from .final_language_extension import install_final_language_extension
from .general_day_plan_patch import install_general_day_plan_patch
from .general_day_plan_compat import install_general_day_plan_compat
from .provider_noise_guard import install_provider_noise_guard
from .task_semantics_patch import install_task_semantics
from .personal_travel_patch import install_personal_travel_patch
from .contingency_patch import install_contingency_patch
from .final_day_flow_patch import install_final_day_flow_patch
from .planning_gaps import install_gap_explanations
from .academic_routing_patch import install_academic_routing_patch
from .audit_quality_patch import install_audit_quality_patch
from .real_life_audit_patch import install_real_life_audit_patch
from .smart_review_scope_patch import install_smart_review_scope_patch
from .performance_patch import invalidate_ticktick_cache
from .native_ticktick_patch import install_native_ticktick_patch
from .native_ticktick_safety_patch import install_native_ticktick_safety_patch
from .day_plan_activities import install_day_plan_activities
from .semantic_plan import install_semantic_plan
from .semantic_cost_gate import install_semantic_cost_gate
from .quickdump_latency_patch import install_quickdump_latency_patch
from .solver_latency_patch import install_solver_latency_patch
from .final_state_invariant_patch import install_final_state_invariant_patch
from .natural_language_safety_patch import install_natural_language_safety_patch
from .final_overlap_guard import install_final_overlap_guard
from .productive_gap_policy_patch import install_productive_gap_policy
from .final_task_state_guard import install_final_task_state_guard

# Install grounded language interpretation before wiring the final runtime globals.
# The model can use a read-only compact view of live task/context data to resolve
# paraphrases and references, while deterministic code still validates and compiles it.
extend_contextual_intake_phrases()
_contextual_plan = install_contextual_intake_patch()

# If a configured model rejects strict Structured Outputs, recover through a validated
# plain-JSON Responses request before giving up to local grammar.
install_semantic_compatibility()

# The current frontend recognizes `semantic`; keep that badge accurate while retaining
# a separate diagnostic flag that this was the live-context-grounded semantic path.
install_contextual_intake_ui_compat()

# Critical real-life ordering must work even with no semantic API at all. This resolves
# ordinary chains against live tasks and treats "after I get back" as a real-life gate,
# not as a fake task dependency.
_contextual_plan = install_deterministic_intake_patch()

# Conversational future-day planning is also deterministic. "I want to plan for tomorrow"
# describes a schedule, not a task called "plan for". Resolve existing tasks/events only,
# preserve fixed commitments, keep optional work optional, and never touch NOTE items.
_contextual_plan = install_tomorrow_plan_patch(_contextual_plan)

# A later "change of plans" must be able to invalidate the story created by an earlier
# Quick Dump. Replace stale outing/return-home/order state, support explicit one-off
# #fixed commitment moves, and keep bath/nap/dinner narratives temporary rather than
# turning them into permanent tasks. The companion clause/recurrence patches bind the
# date to the movement clause, preserve recurrence rules, and mark explicitly reordered
# non-recurring fixed activities for a one-off release from their obsolete slots.
_contextual_plan = install_plan_revision_patch(_contextual_plan)
# Consume that private release marker atomically at the TickTick/meta write boundary.
# Normal #fixed tasks remain protected; only a fresh revision that explicitly moved the
# activity can clear the old slot and return it to the optimizer.
install_plan_revision_apply_patch()

# Support steps represented inside a contiguous outing are geometry, not independent
# prerequisites. Strip only same-bundle support dependency edges at planning time so a
# stale "Swimming depends on Change at pool" edge cannot deadlock the Swimming bundle.
install_outing_dependency_patch()

# A complete deterministic interpretation is a successful fallback, not an error state.
# Install this before diagnostics so we do not waste another API request merely to explain
# a fallback that the deterministic compiler already handled correctly.
install_deterministic_intake_polish()

# Extend the mature deterministic grammar with ordinary lifelong verbs/nouns and natural
# flow connectors such as "after that", "followed by", and casual spoken action prefixes.
install_final_language_extension()

# Generalize explicit day planning beyond tomorrow. Today, weekdays, relative future days
# and common written dates use the same deterministic narrative path. Recurring #fixed
# TickTick series are projected onto the requested day from their real RRULE/start/end,
# so uncertain remembered times never override the actual stored occurrence.
install_general_day_plan_patch()
# Keep the mature next-day context contract stable for downstream code while exposing the
# new date-general `day_plan` context alongside it.
install_general_day_plan_compat()

# Only unresolved local fallbacks reach diagnostics. Genuine model/quota/schema failures
# may be useful internally, but their raw HTTP/provider details are sanitized below.
install_semantic_diagnostics()
# Last-mile guarantee: raw semantic HTTP 400/429/rate-limit/model compatibility diagnostics
# never become scheduling guidance in the review UI. Real clarifications remain visible.
install_provider_noise_guard()

# Obvious task meaning is also scheduling input. Infer plan-local windows such as
# morning review / wake-up / night review without overwriting explicit user metadata,
# and prevent NOTE items from receiving fake planning durations.
_semantic_plan = install_task_semantics(_contextual_plan)
from .personal_scheduler import install_personal_scheduler
_semantic_plan = install_personal_scheduler(_semantic_plan)

# Persistent real-world commute geometry: school is 75 min each way, church 60 min,
# and swimming/gym default to the nearest ActiveSG stadium at 20 min each way. A named
# alternate gym/pool or an explicit travel duration overrides those defaults plan-locally.
_semantic_plan = install_personal_travel_patch(_semantic_plan)

# Generic contingency semantics sit outside the real-life/location wrappers so virtual
# planning-only items still receive normal travel/recovery logic. Optional work stays
# soft, timed "maybe" events stay tentative, OR groups are enforced, and conditionals
# can switch branches without inventing or permanently mutating TickTick tasks.
_semantic_plan = install_contingency_patch(_semantic_plan)

# Final cohesive-day semantics: explicit A→B→C narratives prefer nearby follow-through,
# "after meal" means reasonably soon after recovery rather than merely later that day,
# freshly stated plans beat stale flexible placement, and optional work cannot crowd out
# required work. Fixed commitments, travel, meals and sleep remain authoritative.
_semantic_plan = install_final_day_flow_patch(_semantic_plan)
_semantic_plan = install_gap_explanations(_semantic_plan)

# Backward-compatible alias: from this point onward "contextual plan" means the full
# contextual planner including deterministic real-life chains, fresh revisions, title
# semantics, personal commute/place defaults, generic contingencies and cohesive flow.
_contextual_plan = _semantic_plan

# Productive replans should actively use legal capacity, and explicit "from now/right
# now" commands must bind to the live submission clock (+5 minutes), never stale context.
# Install before runtime globals capture the parser; scheduler globals are patched in-place.
install_productive_gap_policy()

# main.py/service.py imported function references before these final layers existed.
# Point those runtime globals at the final wrappers so every endpoint sees the same
# interpretation and planner. Typed intake gates task inference and compiles temporary
# reality/date rules. The title-semantic wrapper adds human timing before Human Adjuster.
_main.parse_quick_dump = _intake.parse_language
_main.create_plan = _schedule_instruction.instruction_create_plan
_service.create_plan = _schedule_instruction.instruction_create_plan
_service.plan = _semantic_plan

# Enrich reviewed task/event creation with native TickTick fields while preserving the
# same review-first contract. This is intentionally installed last so NOTE exclusion and
# all contextual/real-life interpretation layers remain authoritative.
install_native_ticktick_patch()
install_native_ticktick_safety_patch()
install_day_plan_activities()
install_semantic_plan()
# Simple/common prompts stay local; complex prompts may use Luna but never block the
# interactive Quick Dump path beyond the bounded semantic time budget.
install_semantic_cost_gate()
# Read-only preview uses the last good task snapshot and avoids live project scans unless
# NEW tasks actually require destination routing. Apply remains fully live/validated.
install_quickdump_latency_patch(app)
# Keep CP-SAT interactive. Existing heuristic fallback still handles rare timeout cases.
install_solver_latency_patch()
# Absolute final intake-state boundary: state facts recognized by the deterministic
# parser must survive every specialized review wrapper. This guard cannot create tasks.
install_final_state_invariant_patch()
# Absolute final natural-language boundary: task creation needs positive authority and
# explicit tomorrow/today wording wins over any earlier fuzzy whole-prompt inference.
install_natural_language_safety_patch()

# Natural speech can begin with current reality and continue as an elliptical future
# chain ("eating lunch now, then swim..., then visit..., then go home"). Install this
# after every other intake safety wrapper so clause-local current state cannot swallow
# later activities. It is deterministic and preserves preview-only mutation semantics.
from .natural_chain_intake_patch import install_natural_chain_intake_patch
install_natural_chain_intake_patch()

# Explicit references to a stored Project Intelligence campaign ("study physics for
# SPHL tonight") reuse its next work package instead of creating a generic duplicate
# study task. This remains preview-only until the final Apply boundary.
from .project_reference_intake_patch import install_project_reference_intake_patch
install_project_reference_intake_patch()

# Final submission-clock semantics for ordinary task creates/updates. This sits outside
# the narrative wrapper so single casual activities get the same relative timing:
# prospective "right now" => +2m; explicit "in/after N minutes" => that exact offset.
from .relative_start_intake_patch import install_relative_start_intake_patch
install_relative_start_intake_patch()

# Normalize every reviewed date/clock/window/deadline through one provenance-preserving
# Temporal IR. This runs outside the mature language stack so every parser path converges
# on the same hard-vs-soft timing semantics before a plan is built.
from .temporal_intake_patch import install_temporal_intake_patch
install_temporal_intake_patch()

# Absolute last planning boundary: the merged human timeline must be executable by one
# person.  Planning estimates may flex around hard commitments/logistics; explicit reality
# and fixed commitments are never silently moved.  If they conflict, the generated block
# is withheld and the preview explains why instead of drawing an impossible overlap.
_service.plan = install_final_overlap_guard(_service.plan)
_contextual_plan = _service.plan

# Absolute final task-state boundary: TickTick abandoned/Won't Do items are never
# schedulable, and a plan distinguishes genuinely empty days from unfinished work and
# active-but-unschedulable tasks. A final legal-window fill also closes avoidable large gaps.
install_final_task_state_guard(_service)
_contextual_plan = _service.plan

_policy.install_policy_routes(app)
install_academic_routing_patch()
_human_day.install_human_day_patch(app, _entrypoint)
install_audit_quality_patch(_performance)
install_real_life_audit_patch(_performance)
# Performance entrypoint installs the hot Smart Review cache first. Scope it only after
# that wrapper exists so today's filter preserves the fast cached account snapshot.
install_smart_review_scope_patch(_entrypoint)


@app.on_event('startup')
async def optional_live_semantic_verification():
    import asyncio
    from .live_semantic_check import launch_once
    asyncio.create_task(launch_once())
    from .live_project_check import launch_once as launch_project_check
    asyncio.create_task(launch_project_check())


@app.post("/api/tasks/refresh")
async def refresh_tasks_from_ticktick():
    """Force the next task read to bypass the short hosted TickTick cache."""
    invalidate_ticktick_cache(projects=True)
    return {"ok": True}


__all__ = ["app"]

# Project Intelligence is production-only and wraps the already-finalized create_plan
# boundary. Generic interactive-job unit fixtures must not install or mutate it.
from .project_intelligence import install_project_intelligence
install_project_intelligence(app, _service, _main)

# Large scanned-PDF OCR/indexing must outlive one browser request. This background
# import lane is profile-local, pollable, and never auto-replays after a restart.
from .module_import_jobs import install_module_import_jobs
install_module_import_jobs(app)

# Absolute external-write contract: every scheduler-generated TickTick mutation is
# preview-only until the server-side final Apply endpoint stamps the plan explicitly.
from .manual_apply_gate import install_manual_apply_gate
install_manual_apply_gate(_service, _main)

# Install after the final interpretation and Project Intelligence routes are selected.
from .interactive_jobs import install_interactive_jobs
install_interactive_jobs(app)


# Apply execution headroom to every production planner, including Minimal Repair.
from .planning_clock import with_execution_headroom
_service.plan = with_execution_headroom(_service.plan)
_contextual_plan = _service.plan
