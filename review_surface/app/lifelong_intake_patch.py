from __future__ import annotations

"""Long-lived natural-language and calendar safety layer.

This patch deliberately generalises *classes* of real-life input instead of teaching the
scheduler one exact sentence. It adds:

* timeframe-first tomorrow narratives ("For tomorrow, ...", "Tomorrow I'll ...");
* one-off future-day wake overrides without changing the saved normal routine;
* broader conversational action vocabulary and ordinary task/event nouns;
* broader subject aliases used when resolving existing tasks;
* Google Calendar school timetable protection plus one school commute around the day's
  first/last school block; and
* semantic-provider resilience: known deterministic grammar bypasses the provider, generic
  semantic requests use one normal attempt, and the review UI never performs a second
  diagnostic network probe merely to display an HTTP error.

All additions are planning-only. They never create a TickTick NOTE and never move an
arbitrary Google Calendar event.
"""

import re
from datetime import datetime, timedelta, time

from .config import settings
from .models import BusyBlock
from . import contextual_intake_patch as contextual
from . import deterministic_intake_patch as deterministic
from . import language_intake as intake
from . import quickdump as qd
from . import scheduler
from . import task_semantics_patch as task_semantics
from . import tomorrow_plan_patch as tomorrow
from . import semantic_compat_patch as semantic_compat
from .google_calendar import GoogleCalendarClient


_INSTALLED = False
_BASE_ATTACH = None
_BASE_CLASSIFY = None
_BASE_SCHEDULER_OVERRIDE = None
_BASE_LOGICAL_AWAKE = None
_BASE_GOOGLE_BUSY = None

# Keep the original plan-command grammar and add a timeframe-first conversational form.
# The lookahead prevents a bare factual sentence such as "Tomorrow is a holiday" from
# being treated as a replan merely because it begins with the word tomorrow.
_TIMEFRAME_FIRST = (
    r"^\s*(?:for\s+)?(?:tomorrow|tmr)\b\s*[,;:.-]?\s*"
    r"(?=(?:i\b|we\b|my\b|our\b|wake\b|get\b|start\b|breakfast\b|lunch\b|dinner\b|"
    r"school\b|class\b|lecture\b|tutorial\b|lab\b|work\b|study\b|revise\b|read\b|"
    r"write\b|finish\b|do\b|go\b|head\b|leave\b|attend\b|meet\b|visit\b|swim\b|"
    r"gym\b|workout\b|run\b|church\b|mass\b|pray\b|then\b|after\b|before\b))"
)

_WAKE_RE = re.compile(
    r"\b(?:wake(?:\s+up)?|get\s+up|be\s+up|rise|start(?:ing)?\s+(?:my|the)\s+day)"
    r"\s*(?:at|around|about|by)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.I,
)

# Common spoken intent prefixes. Explicit task-creation commands are intentionally left
# to the original parser so phrases such as "please create a task" keep native semantics.
_SPOKEN_ACTION_PREFIX = re.compile(
    r"^\s*(?:(?:i\s*(?:am|'m|m)\s+(?:gonna|going\s+to|planning\s+to|about\s+to))|"
    r"(?:i\s*(?:will|'ll|wanna|want\s+to|need\s+to|have\s+to|gotta|plan\s+to))|"
    r"(?:we\s*(?:will|'ll|are\s+going\s+to|'re\s+going\s+to))|"
    r"(?:let\s+me)|please)\s+",
    re.I,
)

_ACTION_WORDS = (
    "read", "do", "study", "revise", "review", "practice", "practise", "train", "learn",
    "finish", "complete", "continue", "resume", "start", "write", "draft", "edit", "research",
    "design", "draw", "model", "calculate", "solve", "work on", "build", "code", "program",
    "debug", "test", "compile", "deploy", "render", "upload", "submit", "print", "prepare",
    "make", "create", "record", "film", "photograph", "watch", "listen", "call", "email",
    "message", "reply", "send", "meet", "attend", "visit", "book", "buy", "purchase", "pay",
    "collect", "pick up", "pickup", "drop off", "deliver", "shop", "clean", "tidy", "organize",
    "organise", "wash", "laundry", "vacuum", "mop", "pack", "repair", "fix", "cook", "eat",
    "swim", "run", "jog", "walk", "cycle", "bike", "exercise", "work out", "workout", "lift",
    "stretch", "play", "pray", "meditate", "journal", "shower", "change", "travel", "commute",
    "drive", "ride", "go", "head", "leave", "return", "arrive", "take", "use", "keep",
)

_EVENT_NOUNS = {
    "seminar", "workshop", "conference", "consultation", "rehearsal", "shift", "campus",
    "polytechnic", "university", "college", "module", "practical", "commute", "errand",
    "groceries", "grocery", "shopping", "haircut", "barber", "ceremony", "wedding", "party",
    "brunch", "workout", "weights", "lifting", "running", "jogging", "cycling", "hike",
    "presentation", "interview", "appointment", "meeting", "lecture", "tutorial", "lesson",
}

_DEEP_NOUNS = {
    "assignment", "homework", "coursework", "report", "essay", "research", "reading",
    "revision", "project", "presentation", "problem set", "problem-set", "proof", "coding",
    "programming", "design", "analysis", "calculus", "algebra", "statistics", "probability",
}

# School recognition is intentionally based on event title evidence. Direct Google events
# are already fixed; this classifier only decides whether the persistent school commute
# should surround the first/last event of that day.
_SCHOOL_STRONG = re.compile(
    r"\b(?:school|campus|polytechnic|poly|university|college|lecture|tutorial|seminar|"
    r"practical|module|classroom|cca)\b",
    re.I,
)
_SCHOOL_WEAK = re.compile(r"\b(?:class|lab|lesson)\b", re.I)
_NON_SCHOOL_CLASS = re.compile(
    r"\b(?:gym|fitness|yoga|pilates|spin|dance|piano|guitar|music|driving|swim|swimming|"
    r"tennis|badminton|football|church|doctor|medical|dental|blood)\b",
    re.I,
)
_MODULE_CODE = re.compile(r"\b[A-Za-z]{2,6}\d{2,4}[A-Za-z]?\b")


def _clock_text(hour_text: str, minute_text: str | None, ampm: str | None) -> str | None:
    try:
        hour = int(hour_text)
        minute = int(minute_text or 0)
    except (TypeError, ValueError):
        return None
    marker = str(ampm or "").lower()
    if marker:
        if hour < 1 or hour > 12:
            return None
        if marker == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif hour > 23:
        return None
    if minute < 0 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _future_wake(text: str) -> str | None:
    from .temporal_engine import clock_near, clock_string
    ref = clock_near(
        text,
        r"\b(?:wake(?:\s+up)?|get\s+up|be\s+up|rise|start(?:ing)?\s+(?:my|the)\s+day)\b",
        max_distance=34,
    )
    return clock_string(ref.minutes) if ref else None


def _future_plan_for_day(ctx: dict, day) -> dict:
    # New date-general store. Keep tomorrow_plan as a compatibility alias for older
    # downstream/UI code until all callers have migrated.
    plans = dict((ctx or {}).get("future_day_plans") or {})
    direct = plans.get(day.isoformat())
    if isinstance(direct, dict):
        return dict(direct)
    plan = dict((ctx or {}).get("tomorrow_plan") or {})
    return plan if str(plan.get("date") or "") == day.isoformat() else {}


def _install_vocabulary() -> None:
    escaped = sorted((re.escape(x) for x in _ACTION_WORDS), key=len, reverse=True)
    intake._ACTION = re.compile(r"^(?:" + "|".join(escaped) + r")\b", re.I)
    qd.EVENT_WORDS.update(_EVENT_NOUNS)
    qd.DEEP_WORDS.update(_DEEP_NOUNS)

    deterministic._CATEGORY_WORDS.setdefault("math", set()).update({
        "math", "maths", "calculus", "algebra", "geometry", "trig", "trigonometry",
        "statistics", "probability", "analysis", "integration", "integral", "limits",
        "derivative", "differential", "vector", "vectors",
    })
    deterministic._CATEGORY_WORDS.setdefault("physics", set()).update({
        "physics", "mechanics", "dynamics", "statics", "thermo", "thermodynamics",
        "electromagnetism", "electricity", "waves", "wave", "optics", "quantum",
    })
    deterministic._CATEGORY_WORDS.setdefault("swim", set()).update({
        "swim", "swimming", "pool", "laps", "aquatic",
    })
    deterministic._CATEGORY_WORDS.setdefault("bible", set()).update({
        "bible", "scripture", "gospel", "devotional", "devotion",
    })
    deterministic._CATEGORY_WORDS.setdefault("gym", set()).update({
        "gym", "workout", "weights", "lifting", "strength", "fitness",
    })
    deterministic._CATEGORY_WORDS.setdefault("school", set()).update({
        "school", "class", "lecture", "tutorial", "lab", "practical", "seminar",
        "campus", "module", "lesson", "polytechnic", "university", "college",
    })


def classify_lifelong(text, numbered=False, output_section=False, now=None):
    assert _BASE_CLASSIFY is not None
    source = str(text or "").strip()
    from .conversational_activity import day_unavailability, is_explicit_activity, has_day_span
    if day_unavailability(source):
        return 'reality', source
    if re.match(r'^(?:i|we)\s+(?:was|were|used\s+to|had\s+been)\b', source, re.I):
        return 'history', source
    role, payload = _BASE_CLASSIFY(text, numbered, output_section, now)

    # Preserve all non-work state/control meanings from the mature parser. Only normalize
    # ordinary spoken action phrases that it would otherwise call a task/ambiguity with the
    # conversational prefix still attached.
    if intake._CREATION.match(source):
        return role, payload
    match = _SPOKEN_ACTION_PREFIX.match(source)
    if role not in {"task", "ambiguous"} or not match:
        return role, payload

    candidate = source[match.end():].strip(" ,.;:-")
    if not candidate or not is_explicit_activity(candidate):
        return ('ambiguous', source) if role == 'task' else (role, payload)

    low = source.lower()
    # Spoken future/timing language is a scheduling goal, not permission to invent a
    # duplicate durable task. The normal goal compiler resolves it against live work.
    if intake._ACTION.match(candidate) and not has_day_span(source) and (
        re.search(r"\b(?:today|tonight|tomorrow|tmr|morning|afternoon|evening|night|later|after|before|then)\b", low)
        or re.search(r"\b(?:find\s+(?:me\s+)?(?:a\s+)?(?:good|best|suitable)\s+time|fit\s+(?:it|this)\s+in|whenever\s+(?:it|this)\s+fits)\b", low)
    ):
        return "goal", candidate
    return "task", candidate


def attach_lifelong_future_context(parsed: dict, text: str, rows: list[dict], now: datetime) -> dict:
    assert _BASE_ATTACH is not None
    parsed = _BASE_ATTACH(parsed, text, rows, now)

    wake = _future_wake(text)
    if not wake:
        return parsed

    from .temporal_engine import date_near, resolve_date_reference
    ref = date_near(
        text,
        now,
        r"\b(?:wake(?:\s+up)?|get\s+up|be\s+up|rise|start(?:ing)?\s+(?:my|the)\s+day)\b",
        max_distance=100,
    ) or resolve_date_reference(text, now)
    target_day = ref.start_date if ref and ref.start_date > now.date() else None
    if target_day is None and tomorrow._is_tomorrow_plan(text):
        target_day = now.date() + timedelta(days=1)
    if target_day is None:
        return parsed

    ctx = parsed.get("context") or {"date": now.date().isoformat(), "source": "quick-dump"}
    plans = dict(ctx.get("future_day_plans") or {})
    plan = dict(plans.get(target_day.isoformat()) or {})
    plan.update(date=target_day.isoformat(), wake_time=wake)
    plans[target_day.isoformat()] = plan
    ctx["future_day_plans"] = plans

    # Compatibility alias for mature tomorrow-specific UI/logic.
    if target_day == now.date() + timedelta(days=1):
        tomorrow_plan = dict(ctx.get("tomorrow_plan") or {})
        tomorrow_plan.update(plan)
        ctx["tomorrow_plan"] = tomorrow_plan

    days_ahead = max(1, (target_day - now.date()).days)
    ctx["minimum_horizon_days"] = max(days_ahead + 1, int(ctx.get("minimum_horizon_days") or 1))
    parsed["minimum_horizon_days"] = max(days_ahead + 1, int(parsed.get("minimum_horizon_days") or 1))
    parsed["context"] = ctx
    note = (
        f"{target_day.isoformat()} wake time is a one-off {wake} override; "
        "your saved normal wake time is unchanged."
    )
    notes = list(parsed.get("notes") or [])
    if note not in notes:
        notes.append(note)
    parsed["notes"] = notes
    return parsed


def scheduler_override_for_future_day(day, config: dict) -> dict:
    assert _BASE_SCHEDULER_OVERRIDE is not None
    base = dict(_BASE_SCHEDULER_OVERRIDE(day, config) or {})
    ctx = scheduler._quick_context(config)
    plan = _future_plan_for_day(ctx, day)
    for key in ("wake_time", "sleep_start"):
        if plan.get(key):
            base[key] = plan[key]
    return base


def logical_awake_with_future_day(day, config: dict):
    assert _BASE_LOGICAL_AWAKE is not None
    start, end = _BASE_LOGICAL_AWAKE(day, config)
    ctx = (config or {}).get("_quick_context") or {}
    plan = _future_plan_for_day(ctx, day)
    wake_text = plan.get("wake_time")
    sleep_text = plan.get("sleep_start")
    if wake_text:
        try:
            start = datetime.combine(day, time.fromisoformat(str(wake_text)), settings.tz)
        except ValueError:
            pass
    if sleep_text:
        try:
            end = datetime.combine(day, time.fromisoformat(str(sleep_text)), settings.tz)
        except ValueError:
            pass
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _is_school_calendar_label(label: str) -> bool:
    raw = str(label or "").strip()
    if not raw:
        return False
    if _SCHOOL_STRONG.search(raw) or _MODULE_CODE.search(raw):
        return True
    return bool(_SCHOOL_WEAK.search(raw) and not _NON_SCHOOL_CLASS.search(raw))


def augment_school_calendar_busy(blocks: list[BusyBlock], commute_minutes: int = 75) -> list[BusyBlock]:
    """Add exactly one outbound/return commute around each Google school day.

    Several lessons on one date are treated as one campus outing, so the scheduler does
    not invent a 75-minute trip between every lecture/tutorial.
    """
    out = list(blocks or [])
    by_day: dict[object, list[BusyBlock]] = {}
    for block in blocks or []:
        if str(getattr(block, "source", "")) != "google":
            continue
        if not _is_school_calendar_label(getattr(block, "label", "")):
            continue
        duration = block.end - block.start
        # All-day date markers are commitments, not evidence of a physical campus stay.
        if duration >= timedelta(hours=23):
            continue
        by_day.setdefault(block.start.astimezone(settings.tz).date(), []).append(block)

    travel = timedelta(minutes=max(0, int(commute_minutes)))
    if travel <= timedelta(0):
        return out
    for day_blocks in by_day.values():
        first = min(day_blocks, key=lambda b: b.start).start
        last = max(day_blocks, key=lambda b: b.end).end
        out.append(BusyBlock(first - travel, first, "Travel to school", "google-school-commute"))
        out.append(BusyBlock(last, last + travel, "Travel home from school", "google-school-commute"))
    return out


async def google_busy_with_school_commute(self, start: datetime, end: datetime) -> list[BusyBlock]:
    assert _BASE_GOOGLE_BUSY is not None
    blocks = await _BASE_GOOGLE_BUSY(self, start, end)
    return augment_school_calendar_busy(blocks, 75)


async def _no_network_semantic_probe() -> dict:
    """Local diagnostic value used by regression coverage; normal review does not probe."""
    return {
        "code": "optional-semantic-unavailable",
        "summary": "Optional semantic enrichment is unavailable; deterministic/local interpretation remains active.",
    }


def install_lifelong_intake_patch() -> None:
    global _INSTALLED, _BASE_ATTACH, _BASE_CLASSIFY
    global _BASE_SCHEDULER_OVERRIDE, _BASE_LOGICAL_AWAKE, _BASE_GOOGLE_BUSY
    if _INSTALLED:
        return
    _INSTALLED = True

    _install_vocabulary()

    # Extend tomorrow grammar without deleting the already-tested planning-verb form.
    old_pattern = tomorrow._PLAN_TOMORROW.pattern
    tomorrow._PLAN_TOMORROW = re.compile(r"(?:" + old_pattern + r")|(?:" + _TIMEFRAME_FIRST + r")", re.I)

    _BASE_CLASSIFY = intake.classify
    intake.classify = classify_lifelong
    contextual._BASE_CLASSIFY = classify_lifelong

    _BASE_ATTACH = contextual._attach_real_life_bounds
    contextual._attach_real_life_bounds = attach_lifelong_future_context

    _BASE_SCHEDULER_OVERRIDE = scheduler._override_for_day
    scheduler._override_for_day = scheduler_override_for_future_day

    _BASE_LOGICAL_AWAKE = task_semantics._logical_awake
    task_semantics._logical_awake = logical_awake_with_future_day

    _BASE_GOOGLE_BUSY = GoogleCalendarClient.busy_blocks
    GoogleCalendarClient.busy_blocks = google_busy_with_school_commute

    # Do not automatically fan out to a second model after a provider failure. An
    # explicitly configured fallback still remains available to deployments that want it.
    semantic_compat._DEFAULT_FALLBACK_MODEL = ""

    # The tomorrow wrapper captured the compatibility function earlier. Keep semantic
    # inference available for genuinely ambiguous language, but make it a single normal
    # grounded attempt. Known tomorrow grammar still exits locally before this path.
    if getattr(tomorrow, "_BASE_SEMANTIC", None) is not None:
        tomorrow._BASE_SEMANTIC = semantic_compat._BASE_GROUNDED


__all__ = [
    "install_lifelong_intake_patch",
    "attach_lifelong_future_context",
    "augment_school_calendar_busy",
    "classify_lifelong",
    "scheduler_override_for_future_day",
]
