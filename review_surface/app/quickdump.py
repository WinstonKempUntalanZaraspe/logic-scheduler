from __future__ import annotations

import re
from datetime import datetime, timedelta, time
from difflib import SequenceMatcher

from .config import settings

STATE_WORDS = {
    "tired", "exhausted", "drained", "fatigued", "sleepy", "burnt", "burned",
    "woke", "wake", "sleep", "bed", "studying", "study", "rest", "school",
}
EVENT_WORDS = {
    "school", "class", "lecture", "tutorial", "lab", "lesson", "appointment", "doctor",
    "dentist", "meeting", "church", "mass", "table tennis", "badminton", "football",
    "swim", "swimming", "training", "gym", "dinner", "lunch", "flight", "train", "bus",
}
DEEP_WORDS = {
    "study", "revise", "revision", "practice", "calculus", "math", "maths", "physics",
    "coding", "code", "programming", "analysis", "proof", "assignment", "project", "report",
    "essay", "research", "homework", "problem set", "pset", "exam", "test",
}
QUICK_WORDS = {"email", "reply", "message", "call", "submit", "print", "upload", "book", "pay", "send", "check"}
LOW_ENERGY_WORDS = {"admin", "organize", "organise", "clean", "sort", "read email", "email", "reply"}
TIMED_CONTEXT_ONLY = re.compile(
    r"^(?:i\s*(?:'m|am|will|'ll)?\s*)?(?:be\s+)?(?:free|available|unavailable|busy)\b|"
    r"^(?:i\s*(?:'m|am|will|'ll)?\s*)?(?:going\s+to\s+)?(?:sleep|nap|rest)\b|"
    r"^(?:i\s*(?:'m|am|will|'ll)?\s*)?(?:going\s+to\s+)?(?:eat|have)\s+(?:my\s+)?(?:breakfast|lunch|dinner)\b",
    re.I,
)
CATEGORY_RULES = [
    ("Math", {"calculus", "math", "maths", "algebra", "integration", "vector", "proof"}),
    ("Physics", {"physics", "mechanics", "thermo", "aero", "fluid"}),
    ("Coding", {"code", "coding", "programming", "python", "c++", "javascript", "github"}),
    ("School", {"school", "class", "lecture", "tutorial", "lab", "lesson"}),
    ("Fitness", {"gym", "run", "running", "swim", "swimming", "table tennis", "badminton", "football", "training"}),
    ("Admin", QUICK_WORDS | {"admin"}),
]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[\s\-–—*•\d.)]+", "", s or "")).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _contains_any(text: str, words: set[str]) -> bool:
    t = text.lower()
    return any(w in t for w in words)


def _clock_to_minutes(text: str) -> int | None:
    from .temporal_engine import clock_to_minutes
    return clock_to_minutes(text)

def _clock_string(minutes: int) -> str:
    from .temporal_engine import clock_string
    return clock_string(minutes)

def _extract_duration(text: str) -> int | None:
    t = text.lower()
    total = 0
    seen = False
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|hr|h|minutes?|mins?|min|m)\b", re.I)
    for match in pattern.finditer(t):
        # "in/after 5 minutes", "5 minutes from now", and "5 minutes later"
        # describe a START OFFSET, not five minutes of work.
        before = t[max(0, match.start() - 32):match.start()]
        after = t[match.end():match.end() + 24]
        if re.search(r"\b(?:in|after)\s+(?:about\s+|around\s+|roughly\s+|approximately\s+)?$", before, re.I):
            continue
        if re.match(r"\s+(?:from\s+now|later)\b", after, re.I) and not re.search(r"\bfor\s*$", before, re.I):
            continue
        seen = True
        n = float(match.group(1))
        unit = match.group(2).lower()
        total += round(n * 60) if unit.startswith("h") else round(n)
    return max(5, int(total)) if seen else None


def _date_for_word(word: str, now: datetime) -> datetime.date | None:
    from .temporal_engine import date_for_phrase
    return date_for_phrase(word, now)

def _extract_deadline(text: str, now: datetime) -> datetime | None:
    from .temporal_engine import extract_deadline
    return extract_deadline(text, now)

def _extract_day_hint(text: str, now: datetime):
    from .temporal_engine import day_hint
    return day_hint(text, now)

def _extract_time_range(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    from .temporal_engine import extract_time_range
    return extract_time_range(text, now)

def _strip_task_modifiers(text: str) -> str:
    # Concrete temporal modifiers are normalized by the universal Temporal Engine.
    # Relationship language such as "after Physics" is intentionally retained so the
    # dependency compiler can still see it.
    from .temporal_engine import strip_temporal_phrases
    s = strip_temporal_phrases(text, datetime.now(settings.tz))
    s = re.sub(r"\b(?:urgent|high priority|medium priority|low priority|p[123]|fixed|deep work|quick win|quick|flexible)\b", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,:-")

def _category(text: str) -> str | None:
    t = text.lower()
    for name, words in CATEGORY_RULES:
        if any(w in t for w in words):
            return name
    return None


def _match_existing(title: str, rows: list[dict]) -> tuple[dict | None, float]:
    n = _norm(title)
    if not n:
        return None, 0.0
    best, score = None, 0.0
    nt = set(n.split())
    for row in rows:
        rn = _norm(str(row.get("title") or ""))
        if not rn:
            continue
        if n == rn:
            s = 1.0
        elif len(n) >= 4 and (n in rn or rn in n):
            s = 0.93
        else:
            rt = set(rn.split())
            token = len(nt & rt) / max(1, len(nt | rt))
            seq = SequenceMatcher(None, n, rn).ratio()
            s = max(seq * 0.92, token)
        if s > score:
            best, score = row, s
    return (best, score) if score >= 0.72 else (None, score)


def _school_end(rows: list[dict], now: datetime) -> datetime | None:
    ends = []
    for row in rows:
        tags = {str(x).lower() for x in row.get("tags") or []}
        title = str(row.get("title") or "").lower()
        if "fixed" not in tags or not any(k in title for k in ("school", "class", "lecture", "tutorial", "lab")):
            continue
        try:
            end = datetime.fromisoformat(str(row.get("end")).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=settings.tz)
            end = end.astimezone(settings.tz)
        except Exception:
            continue
        if end.date() == now.date():
            ends.append(end)
    return max(ends) if ends else None


def _extract_explicit_clock(text: str) -> str | None:
    from .temporal_engine import extract_clock, clock_string
    ref = extract_clock(text)
    return clock_string(ref.minutes) if ref is not None else None

def _split_lines(text: str) -> list[str]:
    rough = []
    for row in re.split(r"[\r\n;]+", text or ""):
        row = _clean(row)
        if row:
            rough.append(row)
    return rough


def _state_line(line: str) -> bool:
    t = line.lower()
    state_markers = (
        "i'm tired", "im tired", "i am tired", "exhausted", "drained", "fatigued", "sleepy",
        "don't feel like studying", "dont feel like studying", "do not feel like studying",
        "woke up late", "woke late", "wake up late", "slept in", "sleep earlier", "bed earlier",
        "want to sleep", "need rest", "need to rest",
    )
    if any(x in t for x in state_markers):
        return True
    # Natural wake reports such as “woke up at 8am” are state changes, not tasks.
    return bool(re.search(r"\b(?:i\s+)?woke\s+up(?:\s+(?:at|around|about))?\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", t))


def _replan_command(line: str) -> tuple[bool, str | None]:
    """Recognize schedule-control language without turning it into a TickTick task.

    The command must refer to the user's schedule/day/morning/tasks as a whole; a
    sentence like “reschedule dentist” remains eligible to be interpreted as task text.
    """
    t = _norm(line)
    verb = bool(re.search(r"\b(?:plan|replan|reschedule|rebuild|reorganize|reorganise|optimize|optimise|move)\b", t))
    whole = bool(re.search(r"\b(?:my )?(?:morning|day|today|schedule|tasks|everything)\b", t))
    if not (verb and whole):
        return False, None
    if re.search(r'^move\b', t) and not re.search(r'\b(?:schedule|tasks|everything|my day|my morning)\b', t):
        return False, None
    if "morning" in t:
        return True, "morning"
    return True, "today"


def _legacy_command_artifacts(line: str, rows: list[dict]) -> list[dict]:
    """Find only the very narrow accidental task shape created by the old v8 parser.

    We do not broadly delete command-looking tasks. The title must exactly match the
    command text, be untagged/unprioritized, and carry the old parser's 30-minute
    default estimate. The UI exposes the cleanup before it is applied.
    """
    n = _norm(line)
    out = []
    for row in rows:
        if _norm(str(row.get("title") or "")) != n:
            continue
        tags = {str(x).lower() for x in row.get("tags") or []}
        meta = row.get("meta") or {}
        duration = meta.get("duration_minutes")
        if duration is None:
            duration = row.get("duration_minutes")
        if tags or int(row.get("priority") or 0) != 0 or duration != 30:
            continue
        if row.get("id") and row.get("project_id"):
            out.append({
                "task_id": str(row["id"]),
                "project_id": str(row["project_id"]),
                "title": str(row.get("title") or line),
                "reason": "Remove accidental task created by the old Quick Dump parser",
            })
    return out


def parse_quick_dump(text: str, rows: list[dict], config: dict, now: datetime | None = None) -> dict:
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    lines = _split_lines(text)
    context: dict = {"date": now.date().isoformat(), "source": "quick-dump"}
    tasks = []
    notes = []
    warnings = []
    state_seen = False
    cleanup_tasks: list[dict] = []

    for line in lines:
        low = line.lower()
        is_state = _state_line(line)
        is_command, command_scope = _replan_command(line)
        if is_state or is_command:
            state_seen = True
            if any(x in low for x in ("tired", "drained", "fatigued", "sleepy", "don't feel like studying", "dont feel like studying", "do not feel like studying")):
                severe = any(x in low for x in ("exhausted", "completely tired", "very tired", "burnt", "burned"))
                context["energy_scale"] = min(float(context.get("energy_scale", 1.0)), 0.38 if severe else 0.58)
                base = _school_end(rows, now) if "after school" in low else None
                fatigue_from = max(now, base) if base else now
                hours = 3 if severe else 2
                context["fatigue_from"] = fatigue_from.isoformat()
                context["fatigue_until"] = (fatigue_from + timedelta(hours=hours)).isoformat()
                if any(x in low for x in ("don't feel like studying", "dont feel like studying", "do not feel like studying")):
                    context["avoid_deep_today"] = True
                notes.append("Lowered today’s energy and pushed deep work away from your recovery window")

            wake_report = bool(re.search(r"\b(?:i\s+)?woke\s+up\b", low)) or any(x in low for x in ("woke up late", "woke late", "wake up late", "slept in"))
            if wake_report:
                explicit = _extract_explicit_clock(line)
                context["wake_time"] = explicit or _clock_string(now.hour * 60 + now.minute)
                context["actual_wake_reported"] = True
                # A late/actual wake report is inherently a request to rebuild what remains.
                context["replan_requested"] = True
                context["replan_scope"] = command_scope or "today"
                context["replan_from"] = now.isoformat()
                context["catch_up_missed"] = True
                notes.append(f"Actual wake time set to {context['wake_time']} for today only; normal wake time stays unchanged")

            if is_command:
                context["replan_requested"] = True
                context["replan_scope"] = command_scope or context.get("replan_scope") or "today"
                context["replan_from"] = now.isoformat()
                context["catch_up_missed"] = True
                notes.append("Flexible tasks will be rebuilt from now around fixed commitments")

            if any(x in low for x in ("sleep earlier", "bed earlier", "want to sleep", "sleep early")):
                explicit = _extract_explicit_clock(line)
                if explicit:
                    new_sleep = explicit
                else:
                    current = _clock_to_minutes(str(config.get("sleep_start") or "23:00")) or 23 * 60
                    new_sleep = _clock_string(current - 60)
                context["sleep_start"] = new_sleep
                notes.append(f"Protected an earlier bedtime tonight at {new_sleep}")

            # v8.0 could accidentally create the whole status sentence as a 30-minute task.
            # Surface a narrowly-scoped cleanup so applying the corrected interpretation
            # also removes that exact parser artifact rather than scheduling it again.
            if is_command or wake_report:
                for artifact in _legacy_command_artifacts(line, rows):
                    if artifact["task_id"] not in {x["task_id"] for x in cleanup_tasks}:
                        cleanup_tasks.append(artifact)
            continue

        explicit_duration = _extract_duration(line)
        duration = explicit_duration
        deadline = _extract_deadline(line, now)
        time_range = _extract_time_range(line, now)
        explicit_fixed = bool(re.search(r"\bfixed\b", low))
        event_like = _contains_any(low, EVENT_WORDS)
        tentative = bool(re.search(
            r"\b(?:maybe|perhaps|possibly|might|could|if\s+i\s+can|if\s+possible|if\s+time\s+allows?|not\s+sure)\b",
            low,
        ))
        # "Do X from 10:30–10:50" is an exact user-supplied reservation regardless
        # of whether X sounds like a meeting/event or an ordinary chore/study task.
        # Tentative language remains soft and is handled by the contingency layer.
        fixed = explicit_fixed or bool(time_range and not tentative and not TIMED_CONTEXT_ONLY.search(line))
        title = _strip_task_modifiers(line) or line
        title = re.sub(r"\s+", " ", title).strip(" -,:;")
        if not title:
            warnings.append(f"Could not infer a task title from “{line}”")
            continue

        category = _category(line)
        deep = _contains_any(low, DEEP_WORDS) and not event_like
        quick = _contains_any(low, QUICK_WORDS) or "quick win" in low or (duration is not None and duration <= 20)
        low_energy = _contains_any(low, LOW_ENERGY_WORDS)
        urgent = any(x in low for x in ("urgent", "asap", "high priority", "p1"))
        medium = any(x in low for x in ("medium priority", "p2"))
        low_pri = any(x in low for x in ("low priority", "p3"))

        if duration is None:
            if time_range:
                duration = max(5, int((time_range[1] - time_range[0]).total_seconds() // 60))
            elif quick:
                duration = 20
            elif deep:
                duration = 60
            else:
                duration = 30

        if urgent or (deadline and deadline.date() <= (now + timedelta(days=1)).date()):
            priority = 5
        elif medium or deep or (deadline and deadline <= now + timedelta(days=3)):
            priority = 3
        elif low_pri:
            priority = 1
        else:
            priority = 1 if quick else 0

        tags = []
        if fixed:
            tags.append("fixed")
        elif deep:
            tags.append("deep-work")
        if quick and not fixed:
            tags.append("quick-win")
        if low_energy and not fixed:
            tags.append("low-energy")

        meta_patch = {
            "duration_minutes": duration,
            "confidence": "low" if any(x in low for x in ("not sure", "uncertain", "roughly", "maybe")) else "medium",
            "energy": "high" if deep else ("low" if (quick or low_energy) else "auto"),
            "splittable": False if fixed or duration <= 30 else True,
            "autoschedule": not fixed,
            "min_chunk": 25 if duration >= 25 else max(5, duration),
            "max_chunk": min(90, max(30, duration)),
            "category": category,
            "weekly_bucket": category,
            "timing": "asap" if urgent or (deadline and deadline.date() == now.date()) else "balanced",
            "must_finish": bool(deadline and deadline.date() == now.date()),
        }
        if explicit_duration is not None:
            # A number the user explicitly supplied is the activity duration itself,
            # not a soft estimate that later humanization layers may inflate.
            meta_patch["_explicit_activity_minutes"] = int(explicit_duration)
        if deadline:
            meta_patch["deadline"] = deadline.isoformat()
        if fixed and time_range:
            meta_patch["duration_minutes"] = int((time_range[1] - time_range[0]).total_seconds() // 60)

        existing, score = _match_existing(title, rows)
        if existing:
            existing_meta = existing.get("meta") or {}
            # A casual mention of an existing task must not erase carefully tuned manual rules.
            # Only explicit new information or missing fields are filled by Quick Dump.
            if explicit_duration is None and (existing_meta.get("duration_minutes") is not None or existing.get("duration_minutes") is not None):
                meta_patch.pop("duration_minutes", None)
                meta_patch.pop("min_chunk", None)
                meta_patch.pop("max_chunk", None)
            if existing_meta.get("confidence") and not any(x in low for x in ("not sure", "uncertain", "roughly", "maybe")):
                meta_patch.pop("confidence", None)
            if existing_meta.get("category"):
                meta_patch.pop("category", None)
            if existing_meta.get("weekly_bucket"):
                meta_patch.pop("weekly_bucket", None)
            if not fixed:
                if "splittable" in existing_meta:
                    meta_patch.pop("splittable", None)
                if "autoschedule" in existing_meta:
                    meta_patch.pop("autoschedule", None)
            if existing_meta.get("timing") and not urgent and not (deadline and deadline.date() == now.date()):
                meta_patch.pop("timing", None)
            if "must_finish" in existing_meta and not (deadline and deadline.date() == now.date()):
                meta_patch.pop("must_finish", None)
            # Preserve a deliberate TickTick priority unless this line actually carries urgency/deadline information.
            if int(existing.get("priority") or 0) and not (urgent or medium or low_pri or deadline):
                priority = int(existing.get("priority") or 0)

        fixed_start = time_range[0].isoformat() if fixed and time_range else None
        fixed_end = time_range[1].isoformat() if fixed and time_range else None
        if fixed and existing and not time_range:
            if existing.get("start") and existing.get("end"):
                fixed_start, fixed_end = existing.get("start"), existing.get("end")
            elif not existing.get("is_all_day"):
                warnings.append(f"{existing.get('title')}: #fixed needs an existing TickTick time or an explicit range such as 10am-2pm")
        tasks.append({
            "line": line,
            "title": existing.get("title") if existing else title,
            "task_id": existing.get("id") if existing else None,
            "project_id": existing.get("project_id") if existing else None,
            "match_score": round(score, 3) if existing else 0.0,
            "action": "update" if existing else "create",
            "priority": priority,
            "tags_add": tags,
            "replace_smart_tags": bool(tags),
            "meta_patch": meta_patch,
            "fixed_start": fixed_start,
            "fixed_end": fixed_end,
            "reason": "Fixed commitment" if fixed else ("Deep work" if deep else ("Quick win" if quick else "Flexible task")),
        })

    if state_seen and len(context) == 2:
        warnings.append("I noticed a status update but could not infer a concrete scheduling adjustment")
    if not tasks and not state_seen:
        warnings.append("Nothing actionable was recognized. Try one thought or task per line.")
    return {
        "context": context if state_seen else None,
        "tasks": tasks,
        "cleanup_tasks": cleanup_tasks,
        "notes": list(dict.fromkeys(notes)),
        "warnings": warnings,
        "line_count": len(lines),
    }

# Stable task inference primitive. Higher-level intake admits only actual work
# clauses here; subsequent wrappers must not replace this reference.
parse_task_candidate = parse_quick_dump
