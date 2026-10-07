from __future__ import annotations

"""Context-aware Quick Dump scheduling instructions.

This is the final intent layer between natural language and task creation. It separates
commands *about existing schedule objects* ("move all shower related tasks to tomorrow")
from actual new work ("shower tomorrow 20m" or "move boxes tomorrow").

The rule is deliberately conservative: schedule-control grammar may move existing tasks,
but it never invents a new task when the user was clearly giving an instruction. When a
singular reference is ambiguous, it asks for a clearer target instead of guessing.
"""

import re
from datetime import datetime, timedelta, time
from difflib import SequenceMatcher

from .config import settings
from . import academic_context_patch as _academic
from . import quickdump as _qd
from . import service as _service


_BASE_PARSE = _academic.academic_context_parse
_BASE_CREATE_PLAN = _service.create_plan

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_STOP = {
    "a", "an", "the", "all", "any", "my", "our", "existing", "current", "related",
    "task", "tasks", "thing", "things", "work", "stuff", "please", "just", "that", "those",
    "other", "some", "of", "for", "to", "on", "in", "into", "from", "around",
}

# Category/theme aliases improve matching without making category guesses destructive.
# Swimming intentionally includes outing logistics only when the user refers to the
# swimming/pool *group*. "shower related" remains shower-only.
_THEME_ALIASES = {
    "swim": ("swim", "pool", "travel to pool", "go to pool", "change at pool", "shower and change", "go home"),
    "pool": ("swim", "pool", "travel to pool", "go to pool", "change at pool", "shower and change", "go home"),
    "shower": ("shower", "shower and change"),
    "physics": ("physics", "shm", "harmonic motion", "mechanics"),
    "math": ("math", "maths", "calculus", "limits", "integration", "algebra", "vectors"),
    "coding": ("coding", "code", "programming", "python", "javascript", "github"),
    "admin": ("admin", "email", "reply", "submit", "expenses", "budget"),
    "bible": ("bible", "scripture"),
    "fitness": ("fitness", "gym", "run", "swim", "training", "workout"),
    "school": ("school", "poly", "lecture", "tutorial", "lab", "homework", "assignment"),
}


def _norm(value: str | None) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\btmr\b", "tomorrow", text)
    text = re.sub(r"\btdy\b", "today", text)
    text = text.replace("maths", "math").replace("swimming", "swim")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _clauses(text: str) -> list[str]:
    # Keep commas inside a thought (they often describe one schedule instruction),
    # but split true sentences/new lines so mixed Quick Dumps can contain both tasks
    # and instructions without one swallowing the other.
    out = []
    for part in re.split(r"[\r\n;]+|(?<=[.!?])\s+", str(text or "")):
        part = part.strip(" \t,.;")
        if part:
            out.append(part)
    return out


def _clock(raw: str | None, fallback: str) -> time:
    try:
        return time.fromisoformat(str(raw or fallback))
    except Exception:
        return time.fromisoformat(fallback)


def _clock_minutes(raw: str) -> int | None:
    from .temporal_engine import clock_to_minutes
    return clock_to_minutes(raw)

def _day_from_clause(clause: str, now: datetime):
    # One date resolver for the entire product. This supports ordinary relative
    # language ("day after tomorrow", "three Tuesdays from now", "Friday after
    # next", written dates, etc.) while preserving the old (day, label) contract.
    from .temporal_engine import resolve_date_reference
    ref = resolve_date_reference(clause, now)
    if ref and ref.precision == "day":
        return ref.start_date, ref.label
    if ref and ref.precision == "range":
        # Legacy callers need one concrete day. The Temporal IR still preserves the
        # full range for newer consumers; compatibility chooses the first day.
        return ref.start_date, ref.label
    return None, None

def _explicit_creation(clause: str) -> bool:
    low = _norm(clause)
    # These phrases describe work the user wants represented as a task. A later
    # explicit scheduler verb ("I want to move X to tomorrow") still wins.
    return bool(re.search(
        r"^(?:add|create|new task|remind me to|i need to|need to|i have to|have to|gotta|i gotta|must|i must|plan to|i plan to)\b",
        low,
    ))


def _schedule_grammar(clause: str) -> bool:
    low = _norm(clause)
    if not low:
        return False
    if "make sure" in low and not re.search(r"\b(?:task|tasks|schedule)\b", low):
        return False

    # Explicit creation wins unless the sentence contains unmistakable scheduler
    # movement grammar such as "move X to tomorrow".
    movement_to_time = bool(re.search(
        r"\b(?:move|shift|push|reschedule|postpone|defer)\b.+\b(?:to|until|for)\s+"
        r"(?:today|tonight|tomorrow|next\s+\w+|monday|tuesday|wednesday|thursday|friday|saturday|sunday|in\s+\d+\s+days?)\b",
        low,
    ))
    if _explicit_creation(clause) and not movement_to_time:
        return False

    # "Move boxes tomorrow" is a physical task. "Move boxes TO tomorrow" is a
    # scheduling instruction. Group nouns (tasks/everything) make the intent clear
    # even when the user omits the preposition: "all swim tasks move tomorrow".
    if movement_to_time:
        return True
    if re.search(r"\b(?:all|the|my)?\s*.+?\s+(?:related\s+)?tasks?\s+(?:move|shift|go)\s+(?:to\s+|for\s+)?(?:today|tonight|tomorrow)\b", low):
        return True
    if re.search(r"\bmake\s+(?:all\s+)?(?:the\s+)?..+?\s+(?:related\s+)?tasks?\s+(?:go|move|shift)\s+(?:to|for)\s+", low):
        return True
    if re.search(r"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\b", low):
        return True
    if re.search(r"\b(?:keep|leave)\b.+\b(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low):
        return True
    if re.search(r"\b(?:take|remove)\b.+\b(?:off|out of)\s+(?:today|tonight|tomorrow)\b", low):
        return True
    return False


def _instruction_kind(clause: str) -> str:
    low = _norm(clause)
    if re.search(r"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\b", low):
        return "exclude"
    if re.search(r"\b(?:take|remove)\b.+\b(?:off|out of)\b", low):
        return "exclude"
    if re.search(r"\b(?:defer|postpone)\b.+\buntil\b", low):
        return "not-before"
    return "move"


def _extract_target(clause: str) -> str:
    low = _norm(clause)
    temporal = r"(?:today|tonight|tomorrow|next\s+\w+|monday|tuesday|wednesday|thursday|friday|saturday|sunday|in\s+\d+\s+days?)"
    patterns = [
        rf"\bmake\s+(?:all\s+)?(?:the\s+)?(.+?)(?:\s+related)?\s+tasks?\s+(?:go|move|shift)\s+(?:to|for)\s+{temporal}\b",
        rf"\b(?:all\s+)?(?:the\s+)?(.+?)(?:\s+related)?\s+tasks?\s+(?:move|shift|go)\s+(?:to\s+|for\s+)?{temporal}\b",
        rf"\b(?:move|shift|push|reschedule|postpone|defer)\s+(?:all\s+)?(?:the\s+|my\s+)?(.+?)\s+(?:to|until|for)\s+{temporal}\b",
        rf"\b(?:do not|dont|never)\s+(?:schedule|pull|bring|put|move)\s+(?:any\s+)?(.+?)(?:\s+(?:into|on|for|to|from)\s+{temporal}\b|$)",
        rf"\b(?:keep|leave)\s+(?:all\s+)?(?:the\s+|my\s+)?(.+?)\s+{temporal}\b",
        rf"\b(?:take|remove)\s+(?:all\s+)?(?:the\s+|my\s+)?(.+?)\s+(?:off|out of)\s+{temporal}\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, low)
        if m:
            target = m.group(1)
            target = re.sub(r"\b(?:all|the|my|any|existing|current)\b", " ", target)
            target = re.sub(r"\brelated\s+tasks?\b|\btasks?\b", " ", target)
            return re.sub(r"\s+", " ", target).strip()
    return ""


def _bulk_reference(clause: str, target: str) -> bool:
    low = _norm(clause)
    if re.search(r"\b(?:all|everything)\b", low) or "related task" in low or "related tasks" in low:
        return True
    if re.search(r"\btasks\b", low):
        return True
    # Multi-category exclusions such as "Math or study/admin/coding" are naturally bulk.
    return bool(re.search(r"\b(?:or|and)\b", target) or len([x for x in target.split() if x not in _STOP]) >= 3)


def _row_schedulable(row: dict) -> bool:
    if int(row.get("status") or 0) != 0:
        return False
    tags = {str(x).lower() for x in (row.get("tags") or [])}
    if "autoscheduler-session" in tags:
        return False
    if str(row.get("kind") or row.get("item_type") or "").upper() == "NOTE":
        return False
    return True


def _aliases(target: str) -> set[str]:
    n = _norm(target)
    tokens = {x for x in n.split() if x not in _STOP and len(x) >= 2}
    out = set(tokens)
    for key, values in _THEME_ALIASES.items():
        if key in tokens or key in n:
            out.update(_norm(v) for v in values)
    return {x for x in out if x}


def _row_score(row: dict, target: str) -> float:
    title = _norm(row.get("title") or "")
    if not title:
        return 0.0
    target_n = _norm(target)
    if not target_n:
        return 0.0
    if title == target_n:
        return 140.0
    score = 0.0
    if len(target_n) >= 3 and target_n in title:
        score += 90.0
    elif len(title) >= 3 and title in target_n:
        score += 65.0

    title_tokens = set(title.split())
    aliases = _aliases(target_n)
    for alias in aliases:
        a_tokens = set(alias.split())
        if alias == title:
            score += 100.0
        elif len(alias) >= 3 and alias in title:
            score += 58.0
        elif a_tokens and a_tokens <= title_tokens:
            score += 32.0
        else:
            score += 10.0 * len(a_tokens & title_tokens)

    meta = row.get("meta") or {}
    metadata = " ".join(_norm(meta.get(k)) for k in ("category", "context", "weekly_bucket") if meta.get(k))
    if metadata:
        for token in aliases:
            if token in metadata:
                score += 24.0

    # Fuzzy matching is only a tiebreaker; it can never by itself trigger a bulk move.
    score += SequenceMatcher(None, target_n, title).ratio() * 8.0
    return score


def _match_rows(rows: list[dict], target: str, bulk: bool):
    candidates = []
    fixed = []
    for row in rows:
        if not _row_schedulable(row):
            continue
        score = _row_score(row, target)
        if score < 24.0:
            continue
        tags = {str(x).lower() for x in (row.get("tags") or [])}
        if "fixed" in tags:
            fixed.append((score, row))
        else:
            candidates.append((score, row))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    fixed.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates:
        return [], [x[1] for x in fixed], False
    if bulk:
        # A bulk instruction is allowed to match a coherent theme/category. The
        # threshold keeps generic words from dragging unrelated tasks along.
        best = candidates[0][0]
        selected = [row for score, row in candidates if score >= max(28.0, best * 0.26)]
        return selected, [x[1] for x in fixed], False

    best_score, best = candidates[0]
    if best_score < 45.0:
        return [], [x[1] for x in fixed], False
    if len(candidates) > 1:
        second_score = candidates[1][0]
        # If two different tasks match almost equally, do not guess which singular
        # reference the user meant.
        if second_score >= best_score * 0.88 and str(candidates[1][1].get("id")) != str(best.get("id")):
            return [], [x[1] for x in fixed], True
    return [best], [x[1] for x in fixed], False


def _ensure_update(result: dict, row: dict) -> dict:
    rid = str(row.get("id") or "")
    for item in result.setdefault("tasks", []):
        if item.get("action") == "update" and str(item.get("task_id") or "") == rid:
            item.setdefault("meta_patch", {})
            return item
    item = {
        "line": "targeted scheduling instruction",
        "title": row.get("title") or "Task",
        "task_id": rid,
        "project_id": row.get("project_id"),
        "match_score": 1.0,
        "action": "update",
        "priority": int(row.get("priority") or 0),
        "tags_add": [],
        "replace_smart_tags": False,
        "meta_patch": {},
        "fixed_start": None,
        "fixed_end": None,
        "reason": "Targeted scheduling instruction",
    }
    result["tasks"].append(item)
    return item


def _awake_bounds(day, config: dict) -> tuple[datetime, datetime]:
    wake = _clock(config.get("wake_time") or config.get("day_start"), "07:00")
    sleep = _clock(config.get("sleep_start") or config.get("day_end"), "23:00")
    start = datetime.combine(day, wake, settings.tz)
    end = datetime.combine(day, sleep, settings.tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _apply_time_preference(clause: str, start: datetime, end: datetime, patch: dict, now: datetime) -> None:
    low = _norm(clause)
    pref_start, pref_end = start, end
    if "early morning" in low:
        pref_end = min(end, start + timedelta(hours=3))
        patch["timing"] = "asap"
    elif "morning" in low:
        noon = datetime.combine(start.date(), time(12, 0), settings.tz)
        pref_end = min(end, max(start + timedelta(minutes=30), noon))
        patch["timing"] = "asap"
    elif "afternoon" in low:
        pref_start = max(start, datetime.combine(start.date(), time(12, 0), settings.tz))
        pref_end = min(end, datetime.combine(start.date(), time(17, 30), settings.tz))
        patch["timing"] = "balanced"
    elif "evening" in low or "tonight" in low:
        pref_start = max(start, now if start.date() == now.date() else start, datetime.combine(start.date(), time(17, 0), settings.tz))
        patch["timing"] = "late"
    else:
        patch["timing"] = "balanced"

    m = re.search(r"\bafter\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", str(clause).lower())
    if m:
        minutes = _clock_minutes(m.group(1))
        if minutes is not None:
            exact = datetime.combine(start.date(), time(minutes // 60, minutes % 60), settings.tz)
            pref_start = max(pref_start, exact)
            start = max(start, exact)
    m = re.search(r"\bbefore\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", str(clause).lower())
    if m:
        minutes = _clock_minutes(m.group(1))
        if minutes is not None:
            exact = datetime.combine(start.date(), time(minutes // 60, minutes % 60), settings.tz)
            end = min(end, exact)
            pref_end = min(pref_end, exact)
    m = re.search(r"\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", str(clause).lower())
    if m:
        minutes = _clock_minutes(m.group(1))
        if minutes is not None:
            exact = datetime.combine(start.date(), time(minutes // 60, minutes % 60), settings.tz)
            pref_start = max(start, exact - timedelta(minutes=20))
            pref_end = min(end, exact + timedelta(minutes=60))

    if pref_end <= pref_start:
        pref_start, pref_end = start, end
    patch["preferred_window_start"] = pref_start.strftime("%H:%M")
    patch["preferred_window_end"] = pref_end.strftime("%H:%M")
    patch["earliest"] = start.isoformat()


def _instruction_spec(clause: str, rows: list[dict], now: datetime):
    if not _schedule_grammar(clause):
        return None
    target = _extract_target(clause)
    if not target:
        return {"clause": clause, "target": "", "kind": _instruction_kind(clause), "rows": [], "fixed": [], "ambiguous": False, "day": None, "day_label": None, "bulk": False}
    day, day_label = _day_from_clause(clause, now)
    bulk = _bulk_reference(clause, target)
    matched, fixed, ambiguous = _match_rows(rows, target, bulk)
    return {
        "clause": clause,
        "target": target,
        "kind": _instruction_kind(clause),
        "rows": matched,
        "fixed": fixed,
        "ambiguous": ambiguous,
        "day": day,
        "day_label": day_label,
        "bulk": bulk,
    }


def instruction_parse(text: str, rows: list[dict], config: dict, now: datetime | None = None) -> dict:
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    result = dict(_BASE_PARSE(text, rows, config, now))
    result["tasks"] = [dict(x) for x in (result.get("tasks") or [])]
    result["notes"] = list(result.get("notes") or [])
    result["warnings"] = list(result.get("warnings") or [])

    specs = [spec for clause in _clauses(text) if (spec := _instruction_spec(clause, rows, now))]
    if not specs:
        return result

    # Drop only accidental NEW tasks whose source line is itself schedule-control
    # language. Existing updates added by older/specialized intelligence are retained.
    kept = []
    dropped_titles = []
    for change in result["tasks"]:
        if change.get("action") != "create":
            kept.append(change)
            continue
        source = str(change.get("line") or change.get("title") or "")
        if _schedule_grammar(source):
            dropped_titles.append(str(change.get("title") or source))
        else:
            kept.append(change)
    result["tasks"] = kept

    ctx = dict(result.get("context") or {})
    ctx.update({
        "date": now.date().isoformat(),
        "source": "quick-dump",
        "replan_requested": True,
        "replan_from": now.isoformat(),
        "catch_up_missed": False,
    })
    min_horizon = int(ctx.get("minimum_horizon_days") or 1)
    recognized = []

    for spec in specs:
        target = spec["target"]
        if not target:
            result["warnings"].append(
                f"I understood “{spec['clause']}” as a scheduling instruction, but I could not tell which existing task(s) you meant. No new task was created."
            )
            continue
        if spec["ambiguous"]:
            result["warnings"].append(
                f"I understood this as a scheduling instruction for “{target}”, but more than one existing task matches almost equally. Be a little more specific; I will not guess or create a duplicate."
            )
            continue
        if not spec["rows"]:
            fixed_names = [str(x.get("title") or "") for x in spec["fixed"]]
            if fixed_names:
                result["warnings"].append(
                    "I matched only fixed commitment(s) — " + ", ".join(fixed_names) + " — so I left them untouched."
                )
            else:
                result["warnings"].append(
                    f"I understood this as an instruction about existing “{target}” task(s), but none matched confidently. No new task was created."
                )
            continue

        day = spec["day"]
        if day is None:
            result["warnings"].append(
                f"I matched the existing “{target}” task(s), but I still need a day/time to reschedule them. No new task was created."
            )
            continue

        # Negative instructions mean "not in that window", so the earliest legal
        # time becomes the next awake day. Other movement instructions target the
        # requested day itself.
        if spec["kind"] == "exclude":
            target_day = day + timedelta(days=1)
            start, end = _awake_bounds(target_day, config)
            exact_day = False
        else:
            target_day = day
            start, end = _awake_bounds(target_day, config)
            if target_day == now.date():
                start = max(start, now)
            exact_day = spec["kind"] == "move"

        names = []
        for row in spec["rows"]:
            item = _ensure_update(result, row)
            patch = item.setdefault("meta_patch", {})
            _apply_time_preference(spec["clause"], start, end, patch, now)
            if exact_day:
                patch["latest_end"] = end.isoformat()
            # "defer until" and negative exclusions only establish a not-before
            # boundary; they do not force completion on the next day.
            item["reason"] = (
                f"Schedule instruction: not before {target_day.isoformat()}"
                if not exact_day else f"Schedule instruction: move to {target_day.isoformat()}"
            )
            names.append(str(row.get("title") or "Task"))

        delta = max(0, (target_day - now.date()).days)
        min_horizon = max(min_horizon, delta + 1)
        recognized.append({
            "target": target,
            "task_ids": [str(x.get("id") or "") for x in spec["rows"]],
            "titles": names,
            "kind": spec["kind"],
            "target_date": target_day.isoformat(),
        })
        if exact_day:
            result["notes"].append(
                f"Understood as a scheduling instruction, not a new task: {'; '.join(names)} → {target_day.isoformat()}."
            )
        else:
            result["notes"].append(
                f"Understood as a scheduling instruction, not a new task: {'; '.join(names)} stays out until {target_day.isoformat()}."
            )
        if spec["fixed"]:
            result["warnings"].append(
                "Fixed commitment(s) were matched but intentionally not moved: " + ", ".join(str(x.get("title") or "") for x in spec["fixed"])
            )

    if recognized:
        ctx["replan_scope"] = "today"
        ctx["minimum_horizon_days"] = min_horizon
        ctx["targeted_schedule_instructions"] = recognized
        result["context"] = ctx
        result["minimum_horizon_days"] = min_horizon
    elif result.get("context"):
        result["context"] = ctx

    if dropped_titles:
        result["notes"].append("Schedule command kept out of TickTick task creation.")
    result["notes"] = list(dict.fromkeys(result["notes"]))
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


async def instruction_create_plan(
    horizon_days=2,
    from_now=None,
    *,
    save_as_last: bool = True,
    interrupted_source_id: str | None = None,
):
    """Honor a Quick Dump's minimum horizon automatically.

    If the UI still says "Today" but the user just said "move X to tomorrow", the
    planner silently expands to two days. The user should not have to micromanage the
    horizon dropdown just to make natural language work.
    """
    ctx = _service.get_quick_context() or {}
    try:
        minimum = max(1, min(14, int(ctx.get("minimum_horizon_days") or 1)))
    except Exception:
        minimum = 1
    return await _BASE_CREATE_PLAN(
        horizon_days=max(int(horizon_days or 1), minimum),
        from_now=from_now,
        save_as_last=save_as_last,
        interrupted_source_id=interrupted_source_id,
    )


_qd.parse_quick_dump = instruction_parse

__all__ = ["instruction_parse", "instruction_create_plan"]
