from __future__ import annotations

"""Difficulty-aware duration estimation.

The planner may use these estimates immediately when a flexible task has no usable
length. They are planning estimates, not fabricated clock commitments. Smart Review
can persist an estimate only after the user approves it.
"""

import math
import re
from typing import Iterable

from .db import duration_multiplier
from .models import Task


ACADEMIC_HARD = (
    "limit", "limits", "integration", "integral", "calculus", "real analysis",
    "linear algebra", "differential", "mechanics", "simple harmonic", "thermo",
    "thermodynamics", "fluid", "physics", "aerospace", "coding", "programming",
)
PROJECT_HARD = ("report", "assignment", "project", "presentation", "slides", "research")
QUICK_ADMIN = (
    "monthly expenses", "expense review", "budget review", "check expenses",
    "email", "reply", "submit", "upload", "print", "pay bill", "book", "confirm",
    "check in", "check-in", "log", "update tracker", "review calendar",
)
CHECKIN_HINTS = (
    "how do i look", "physique", "progress photo", "body progress", "body check",
    "weigh in", "weigh-in", "weight check", "monthly check", "annual check",
)
READING = ("read ", "watch ", "lecture", "notes")
PRACTICE = ("practice", "solve", "questions", "problem", "worksheet", "exercise", "do physics", "do math")
FITNESS = ("swim", "swimming", "gym", "run", "running", "table tennis", "badminton", "football", "workout", "training")
LOGISTICS = (
    "travel to", "commute to", "go home", "travel home", "commute home", "leave house",
    "leave home", "go out of house", "go out of home", "change at", "shower", "transition buffer",
)


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _round5(minutes: float) -> int:
    return max(5, int(5 * round(float(minutes) / 5.0)))


def _tags(task: Task) -> set[str]:
    return {_norm(x) for x in (task.tags or [])}


def has_usable_duration(task: Task, raw: dict) -> bool:
    for value in (
        (raw or {}).get("remaining_minutes"),
        (raw or {}).get("duration_minutes"),
        task.duration_minutes,
    ):
        if value is not None:
            try:
                return float(value) > 0
            except Exception:
                pass
    return False


def estimate_task(task: Task, raw: dict | None = None) -> dict | None:
    raw = raw or {}
    if has_usable_duration(task, raw):
        return None
    if bool(raw.get("unknown_duration")):
        return None
    tags = _tags(task)
    if "fixed" in tags or "autoscheduler-session" in tags:
        return None

    title = _norm(task.title)
    if not title:
        return None

    # Physical support steps are handled by reality_patch with venue-aware defaults.
    if any(x in title for x in LOGISTICS):
        return None

    category = str(raw.get("category") or "").strip().lower()
    base = 30
    confidence = "low"
    reason = "Generic flexible-task estimate"
    difficulty = "medium"

    if any(x in title for x in CHECKIN_HINTS):
        # Progress/check-in reminders are brief observations, not medium-effort work.
        # Keep the planning block small unless the user supplies a real duration.
        base = 10
        confidence = "medium"
        reason = "Brief progress/check-in task"
        difficulty = "low"
    elif any(x in title for x in QUICK_ADMIN):
        # A recurring review/check is tiny unless the title explicitly says otherwise.
        base = 5 if any(x in title for x in ("monthly expenses", "expense review", "budget review", "check in", "check-in", "log")) else 10
        confidence = "high"
        reason = "Short admin/review task"
        difficulty = "low"
    elif any(x in title for x in ACADEMIC_HARD):
        base = 60
        if any(x in title for x in ("limit", "integration", "calculus", "real analysis", "linear algebra", "simple harmonic", "thermo", "mechanics")):
            base = 75
        if any(x in title for x in PRACTICE):
            base += 15
        elif any(x in title for x in READING):
            base = max(35, base - 25)
        confidence = "medium"
        reason = "Concept-heavy academic work"
        difficulty = "high"
    elif any(x in title for x in PROJECT_HARD):
        base = 75
        if any(x in title for x in ("report", "project", "research")):
            base = 90
        confidence = "medium"
        reason = "Multi-step project/deliverable work"
        difficulty = "high"
    elif any(x in title for x in FITNESS):
        base = 60
        if "table tennis" in title or "training" in title:
            base = 90
        confidence = "medium"
        reason = "Typical activity block"
        difficulty = "medium"
    elif title.startswith("review "):
        base = 15
        confidence = "medium"
        reason = "Short review task"
        difficulty = "low"
    elif any(x in title for x in READING):
        base = 30
        confidence = "medium"
        reason = "Reading/learning block"
        difficulty = "medium"

    # Existing user intent and labels refine, but never explode, the estimate.
    if "deep-work" in tags or raw.get("energy") == "high":
        base *= 1.2
        difficulty = "high"
    if "quick-win" in tags or "low-energy" in tags:
        base *= 0.75
    if int(task.priority or 0) >= 5 and difficulty == "high":
        base *= 1.1

    learned_category = category or (
        "math" if any(x in title for x in ("math", "calculus", "limit", "integrat")) else
        "physics" if any(x in title for x in ("physics", "mechanic", "harmonic", "thermo", "fluid")) else
        "fitness" if any(x in title for x in FITNESS) else
        "admin" if any(x in title for x in QUICK_ADMIN) or any(x in title for x in CHECKIN_HINTS) else
        "general"
    )
    try:
        multiplier = float(duration_multiplier(learned_category))
    except Exception:
        multiplier = 1.0
    # History can personalize the estimate, but cap its influence for sparse/noisy data.
    multiplier = max(0.8, min(1.8, multiplier))
    minutes = _round5(max(5, min(180, base * multiplier)))

    return {
        "minutes": minutes,
        "confidence": confidence,
        "difficulty": difficulty,
        "category": learned_category,
        "reason": reason,
        "history_multiplier": round(multiplier, 2),
    }


def duration_suggestions(tasks: Iterable[Task], metas: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for task in tasks:
        if task.status != 0 or not task.is_actionable:
            continue
        estimate = estimate_task(task, metas.get(task.id) or {})
        if not estimate:
            continue
        out.append({
            "kind": "duration",
            "id": f"duration:{task.id}:{estimate['minutes']}",
            "title": f"Estimate duration · {task.title}",
            "reason": f"{estimate['reason']}; estimated from task difficulty/type{'' if estimate['history_multiplier'] == 1.0 else ' and your duration history'}.",
            "confidence": estimate["confidence"],
            "target_task_id": task.id,
            "estimated_minutes": estimate["minutes"],
            "difficulty": estimate["difficulty"],
            "category": estimate["category"],
        })
    return out


__all__ = ["estimate_task", "duration_suggestions", "has_usable_duration"]
