from __future__ import annotations

"""Final productivity policy for interactive replans.

This layer deliberately does not relax hard reality. Fixed commitments, sleep,
travel, dependencies, meal recovery, hard stops and legal candidate windows stay
inside the existing planner. It only changes tie-breaking for ordinary flexible
work and makes submission-clock commands start from a fresh live cutoff.
"""

from datetime import datetime, timedelta

from .config import settings

_INSTALLED = False


def install_productive_gap_policy() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from . import scheduler
    from . import language_intake
    from .planning_instructions import supported_instruction_rules
    from .planning_clock import LEAD_MINUTES

    original_stability = scheduler._stability_penalty
    original_slot_utility = scheduler.slot_utility
    original_parse_language = language_intake.parse_language
    original_candidate_starts = scheduler._candidate_starts

    def productive_stability_penalty(task, candidate_start, plan_start, config):
        penalty = float(original_stability(task, candidate_start, plan_start, config))
        if not bool((config or {}).get("maximize_productive_time", True)):
            return penalty
        task_start = getattr(task, "start", None)
        freeze = max(0, int((config or {}).get("stability_freeze_minutes", 35)))
        if task_start and task_start >= plan_start:
            lead = (task_start - plan_start).total_seconds() / 60.0
            if freeze and lead <= freeze:
                return penalty
        tags = {str(x).strip().casefold() for x in (getattr(task, "tags", None) or [])}
        if "fixed" in tags:
            return penalty
        scale = max(0.0, min(1.0, float((config or {}).get("productive_stability_scale", 0.12))))
        cap = max(0.0, float((config or {}).get("productive_stability_cap", 12.0)))
        return min(cap, penalty * scale)

    def productive_slot_utility(task, meta, start, task_score, config, minutes=0, deadline_pressure=0.0):
        utility = float(original_slot_utility(task, meta, start, task_score, config, minutes, deadline_pressure))
        if not bool((config or {}).get("maximize_productive_time", True)):
            return utility
        plan_start = scheduler._plan_start(config or {}, start)
        elapsed_minutes = max(0.0, (start - plan_start).total_seconds() / 60.0)
        rate = max(0.0, float((config or {}).get("productive_frontload_utility_per_minute", 0.25)))
        cap = max(0.0, float((config or {}).get("productive_frontload_utility_cap", 45.0)))
        utility -= min(cap, elapsed_minutes * rate)

        # A live-now command is stronger than an ordinary balanced preference. If two
        # placements are both legal, starting immediately should dominate cosmetic delay.
        ctx = (config or {}).get("_quick_context") or {}
        if ctx.get("live_submission_cutoff") and ctx.get("replan_requested"):
            try:
                cutoff = datetime.fromisoformat(str(ctx.get("replan_from")).replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=settings.tz)
                delay = max(0.0, (start - cutoff).total_seconds() / 60.0)
                utility -= min(180.0, delay * 6.0)
            except Exception:
                pass
        return utility

    def productive_candidate_starts(window_start, window_end, minutes, step):
        """Consider the exact legal window edge once before normal grid sampling.

        This is what lets a live cutoff at 10:04 actually exist as a candidate instead of
        forcing the optimizer to wait for 10:05/10:15. Hard legality is unchanged because
        ``window_start`` is already produced by the mature free-window engine.
        """
        exact = window_start
        emitted_exact = False
        if exact + timedelta(minutes=minutes) <= window_end:
            emitted_exact = True
            yield exact
        for candidate in original_candidate_starts(window_start, window_end, minutes, step):
            if emitted_exact and candidate == exact:
                continue
            yield candidate

    def live_clock_parse_language(text, rows, config, now=None, document=None):
        actual_now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        result = original_parse_language(text, rows, config, actual_now, document=document)
        clock_instruction = False
        try:
            for clause, _numbered in language_intake.clauses(text):
                if "submission_clock" in supported_instruction_rules(clause):
                    clock_instruction = True
                    break
        except Exception:
            clock_instruction = False
        if clock_instruction:
            cutoff = actual_now + timedelta(minutes=LEAD_MINUTES)
            ctx = result.get("context") or {
                "date": actual_now.date().isoformat(),
                "source": "quick-dump",
            }
            zone = str((config or {}).get("timezone") or ctx.get("timezone") or settings.timezone)
            ctx.update(
                replan_requested=True,
                replan_scope="today",
                replan_from=cutoff.isoformat(),
                planning_now=actual_now.isoformat(),
                timezone=zone,
                live_submission_cutoff=True,
            )
            result["context"] = ctx
            result.setdefault("notes", []).append(
                f"Live submission time is {actual_now:%Y-%m-%d %H:%M:%S}; new work may start from {cutoff:%Y-%m-%d %H:%M:%S} ({zone})."
            )
            result["notes"] = list(dict.fromkeys(result["notes"]))
        return result

    scheduler._stability_penalty = productive_stability_penalty
    scheduler.slot_utility = productive_slot_utility
    scheduler._candidate_starts = productive_candidate_starts
    language_intake.parse_language = live_clock_parse_language

    from .interactive_quality_speed_patch import install_interactive_quality_speed_patch
    install_interactive_quality_speed_patch()

    from .interactive_quality_regression_fix import install_interactive_quality_regression_fix
    install_interactive_quality_regression_fix()

    # Absolute final filler contract: short true remainders are schedulable, and the first
    # legal work after a live-now request is pulled to the exact submission+2m boundary.
    from .final_productivity_contract_patch import install_final_productivity_fill_contract
    install_final_productivity_fill_contract()

    from .explicit_semantics_preservation_patch import install_explicit_semantics_preservation
    install_explicit_semantics_preservation()

    from .planner_control_fastpath_patch import install_planner_control_fastpath
    install_planner_control_fastpath()

    _INSTALLED = True

