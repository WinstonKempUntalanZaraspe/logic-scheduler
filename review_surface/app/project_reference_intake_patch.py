from __future__ import annotations

"""Final intake guard for explicit references to stored Project Intelligence campaigns.

A user may say "study physics for SPHL tonight" without a current-state narrative.  That
must reuse the SPhL campaign's next work package rather than create a generic duplicate
"Study Physics" task.  Explicit "create task:" commands remain authoritative and bypass
this guard.
"""

import json
import re
from copy import deepcopy
from datetime import datetime

from .config import settings
from . import intake_contract as contract
from .project_reference import resolve_project_study_stage

_INSTALLED = False

_DAY = re.compile(
    r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening)|later|right\s+now|now)\b",
    re.I,
)
_EXPLICIT_CREATE = re.compile(r"\b(?:create|add|make)\s+(?:a\s+)?task\b|\bremind\s+me\s+to\b", re.I)


def _ref(work: dict) -> str:
    return str(work.get("task_id") or work.get("virtual_task_id") or "")


def _looks_like_generic_project_create(change: dict, text: str) -> bool:
    if change.get("action") != "create":
        return False
    line = str(change.get("line") or "")
    title = str(change.get("title") or "")
    low = (line + " " + title).casefold()
    # Only remove the generic study create that came from this explicit project-study
    # request. Other creates in the same prompt (swimming, errands, etc.) are untouched.
    subjectish = bool(re.search(r"\b(?:study|revise|review|practice|practise|learn|physics|math|chemistry)\b", low))
    return subjectish and (line.strip() == text.strip() or line and line.casefold() in text.casefold())


def _bind(parsed: dict, text: str, rows: list[dict], now: datetime) -> dict:
    if _EXPLICIT_CREATE.search(text):
        return parsed
    ctx = deepcopy(parsed.get("context") or {})
    immediate = bool(_DAY.search(text) or ctx.get("replan_scope") == "today" or ctx.get("replan_requested"))
    if not immediate:
        return parsed

    work = resolve_project_study_stage(text, rows, now)
    if not work:
        return parsed
    campaign = work.get("campaign") or {}
    campaign_id = str(campaign.get("id") or "")
    # Natural-chain intake already performed the richer clause-local binding.
    if campaign_id and campaign_id in {str(x) for x in ctx.get("requested_project_campaign_ids") or []}:
        return parsed

    # Once the phrase has resolved to a persistent campaign, a generic inferred study
    # create is no longer truthful. Remove only that artifact.
    parsed["tasks"] = [
        change for change in parsed.get("tasks") or []
        if not _looks_like_generic_project_create(change, text)
    ]

    blocked = work.get("blocked")
    if blocked:
        parsed.setdefault("clarifications", []).append({
            "text": text,
            "reason": (
                "The stored project was found, but its next materialized task is missing. "
                "Refresh Project Intelligence; no duplicate study task was created."
                if blocked == "materialized_task_missing"
                else "The stored project was found, but no project work package is ready yet."
            ),
        })
        parsed["context"] = ctx
        return parsed

    package = work.get("package") or {}
    package_key = str(package.get("key") or "")
    ref = _ref(work)
    if not ref:
        return parsed

    day = now.date().isoformat()
    ctx.setdefault("date", day)
    ctx.setdefault("source", "quick-dump")
    ctx["replan_requested"] = True
    ctx["replan_scope"] = "today"
    ctx["replan_from"] = now.isoformat()
    ctx.setdefault("requested_project_campaign_ids", [])
    if campaign_id and campaign_id not in ctx["requested_project_campaign_ids"]:
        ctx["requested_project_campaign_ids"].append(campaign_id)
    if campaign_id and package_key:
        wanted = ctx.setdefault("requested_project_work_packages", {}).setdefault(campaign_id, [])
        if package_key not in wanted:
            wanted.append(package_key)
    ctx.setdefault("intent_date_goals", {})[ref] = day
    ctx.setdefault("intent_today_ids", [])
    if ref not in ctx["intent_today_ids"]:
        ctx["intent_today_ids"].append(ref)

    title = str(work.get("title") or package.get("title") or campaign.get("goal") or "Project work")
    parsed.setdefault("notes", []).append(
        f"Stored project reference understood: {campaign.get('goal') or campaign_id} → {title}. "
        "No generic study task was created; the scheduler chooses a legal session length from "
        "remaining project effort, campaign capacity and today's available time."
    )
    parsed.setdefault("intents", []).append({
        "kind": "goal", "text": text, "status": "compiled",
        "campaign_id": campaign_id, "work_package_key": package_key,
    })
    parsed["context"] = ctx
    parsed["warnings"] = list(dict.fromkeys(parsed.get("warnings") or []))
    parsed["notes"] = list(dict.fromkeys(parsed.get("notes") or []))
    return parsed


def install_project_reference_intake_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        parsed = await base(text, rows, config, stamp)
        parsed = _bind(parsed, text, rows, stamp)
        contract.validate_task_creation(parsed)

        # The inner review saved its pre-guard interpretation. Persist the post-guard
        # campaign binding under the same preview token so "Use this & replan" consumes
        # exactly what the user saw instead of resurrecting a generic study create.
        try:
            saved = json.loads(contract.get_kv("intake_review") or "{}")
            if saved and saved.get("parsed", {}).get("preview_id") == parsed.get("preview_id"):
                saved.update(
                    text_hash=contract._fingerprint(text),
                    snapshot=contract._snapshot(rows, config),
                    parsed=parsed,
                )
                contract.set_kv("intake_review", json.dumps(saved))
        except Exception:
            pass
        return parsed

    review._project_reference_intake = True
    contract.review_intake = review


__all__ = ["install_project_reference_intake_patch", "_bind"]
