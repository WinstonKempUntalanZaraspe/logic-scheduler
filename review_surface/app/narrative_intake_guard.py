from __future__ import annotations

"""Route ordinary multi-context life stories into the day planner.

A natural update such as

    I just finished lunch, now I'm on my way to church, then dinner, then swim ...

is a *day narrative*. It must never be swallowed by the single-task lifecycle parser
just because the first words happen to look like "I just finished <something>". Nor
should a whole story be claimed as one temporary reality item because it contains words
such as "takeaway" later in the sentence.

This layer is structural rather than prompt-specific: it detects multi-stage narratives,
routes them through the date-general day planner, preserves explicit lifecycle commands,
separates current facts from future stages, and keeps colloquial capacity choices soft.
No new mutation authority is introduced.
"""

import re

from . import contingency_patch as contingency
from . import day_plan_activities as day_activities
from . import final_human_language_patch as human
from . import general_day_plan_patch as general
from . import personal_intents as intents
from . import plan_revision_patch as revision

_INSTALLED = False

_EXPLICIT_SINGLE_ACTION = re.compile(
    r"^\s*(?:please\s+)?(?:delete|permanently\s+delete|remove\s+permanently|cancel|"
    r"skip|complete(?:\s+the\s+task)?|mark\b|resume|uncancel|restore\s+scheduling\s+for)\b",
    re.I,
)
_EXPLICIT_CREATE = re.compile(
    r"^\s*(?:please\s+)?(?:add|create)\s+(?:(?:a|new)\s+)?(?:task|event)\b|"
    r"^\s*(?:new\s+task|task)\s*:",
    re.I,
)
_SEQUENCE = re.compile(r"\b(?:then|after\s+that|afterwards?|next|followed\s+by|later)\b", re.I)
_CURRENT_OR_HISTORY = re.compile(
    r"\b(?:now|right\s+now|currently|just\s+(?:finished|completed|got|came|arrived|returned)|"
    r"already|on\s+my\s+way|on\s+our\s+way|heading\s+to|coming\s+back|going\s+home|"
    r"back\s+home|at\s+home)\b",
    re.I,
)
_REAL_LIFE = re.compile(
    r"\b(?:breakfast|lunch|dinner|eat|eating|takeaway|takeout|dabao|rest|nap|sleep|bed|"
    r"church|school|class|work|shopping|shop|swim|swimming|pool|gym|training|travel|"
    r"commute|home|friend|family|appointment|meeting|shower|change)\b",
    re.I,
)
_CLOCK_RANGE = re.compile(
    r"\b(?:from\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:-|–|to)\s*"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
    re.I,
)
# The implicit-today extension must never steal wording already handled by the mature
# future/day-specific planners, even when the generic general-day matcher does not
# recognize that exact lead-in (for example, "For tomorrow, ...").
_EXPLICIT_FUTURE_DAY = re.compile(
    r"\b(?:tomorrow|tmr|next\s+(?:day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_CAPACITY_BARE = (
    r"\b(?:"
    r"if\s+have\s+(?:any\s+|enough\s+)?time(?:\s+left)?|"
    r"if\s+(?:i|we)\s+still\s+have\s+(?:any\s+)?time|"
    r"if\s+(?:i|we)\s+have\s+time\s+when\s+(?:i|we)\s+(?:get|come|arrive)\s+back"
    r")\b"
)
_CAPACITY_BARE_RE = re.compile(_CAPACITY_BARE, re.I)

# Short immediate self-care chains already have a richer temporary-reality compiler.
# They are not durable-task day narratives and must stay on that mature path.
_CARE_STAGE = re.compile(
    r"^\s*(?:(?:i|we)(?:'m| am|'re| are)?\s+(?:going\s+to|gonna|will|'ll|want\s+to)\s+)?"
    r"(?:bathe|bath|shower|take\s+(?:a|my)\s+(?:bath|shower)|nap|rest|"
    r"take\s+(?:a\s+)?(?:nap|rest|break)|sleep|go\s+to\s+(?:bed|sleep)|"
    r"(?:eat|have)\s+(?:my\s+)?(?:breakfast|lunch|dinner))\b.*$",
    re.I,
)


def _pure_temporary_care_chain(text: str) -> bool:
    pieces = [p.strip(" ,.;:-") for p in re.split(r"\bthen\b|[;\n]+", str(text or ""), flags=re.I) if p.strip(" ,.;:-")]
    return len(pieces) >= 2 and all(_CARE_STAGE.match(piece) for piece in pieces)


def is_multi_context_narrative(text: str) -> bool:
    """True for a day story containing several stages/contexts, not one command."""
    value = re.sub(r"\s+", " ", str(text or "").replace("’", "'")).strip()
    if not value or value.endswith("?") or _EXPLICIT_CREATE.match(value):
        return False
    if _EXPLICIT_SINGLE_ACTION.match(value) or _pure_temporary_care_chain(value):
        return False

    sequence_count = len(_SEQUENCE.findall(value))
    comma_stage_count = len(re.findall(
        r",\s*(?=(?:now\b|if\s+(?:i\s+|we\s+)?(?:have|got)?\s*time\b|"
        r"i\s+(?:am|'m|will|'ll)|we\s+(?:are|'re|will|'ll)))",
        value,
        re.I,
    ))
    has_current = bool(_CURRENT_OR_HISTORY.search(value))
    has_life = bool(_REAL_LIFE.search(value))
    has_clock = bool(_CLOCK_RANGE.search(value))
    return bool(
        (sequence_count >= 2 and (has_current or has_life or has_clock))
        or sequence_count >= 3
        or (has_current and has_life and sequence_count + comma_stage_count >= 1)
    )


def _extend_regex(regex: re.Pattern, extra: str) -> re.Pattern:
    if "if\\s+have\\s+" in regex.pattern:
        return regex
    return re.compile(f"(?:{regex.pattern})|(?:{extra})", regex.flags)


def _strip_return_choice_filler(value: str, base) -> str:
    text = base(value)
    text = re.sub(
        r"^(?:(?:when|after)\s+(?:i|we)\s+(?:get|come|arrive|am|are)\s+back(?:\s+home)?\s*,?\s*)",
        "", text, flags=re.I,
    )
    text = re.sub(
        r"^(?:(?:i|we)\s+(?:will|'ll|can|want\s+to|would\s+like\s+to)\s+)",
        "", text, flags=re.I,
    )
    return text.strip(" ,.;:-")


def _narrative_fragments(text: str, base) -> list[str]:
    value = str(text or "")
    if not is_multi_context_narrative(value):
        return base(value)
    pieces = re.split(
        r",\s*now\s*,?\s*(?=(?:i|we)\b)|"
        r",\s*(?=if\s+(?:(?:i|we)\s+)?(?:have|got)?\s*(?:any\s+|enough\s+)?time\b)",
        value,
        flags=re.I,
    )
    out: list[str] = []
    for piece in pieces:
        if piece.strip(" ,.;:-"):
            out.extend(base(piece))
    return out


def _capacity_choice_clauses(text: str) -> list[str]:
    """Extract only the optional A/B/C clause from a larger narrative stage."""
    clauses: list[str] = []
    for chunk in re.split(r"\bthen\b|[;\n]+", str(text or ""), flags=re.I):
        match = human._CAPACITY_OPTIONAL_RE.search(chunk)
        if not match or not re.search(r"\bor\b", chunk[match.start():], re.I):
            continue
        clause = chunk[match.start():].strip(" ,.;:-")
        if clause and clause not in clauses:
            clauses.append(clause)
    return clauses


def install_narrative_intake_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    human._CAPACITY_OPTIONAL_RE = _extend_regex(human._CAPACITY_OPTIONAL_RE, _CAPACITY_BARE)
    day_activities._OPTIONAL = _extend_regex(day_activities._OPTIONAL, _CAPACITY_BARE)
    general._OPTIONAL = _extend_regex(general._OPTIONAL, _CAPACITY_BARE)
    revision._OPTIONAL_STUDY_RE = _extend_regex(revision._OPTIONAL_STUDY_RE, _CAPACITY_BARE)
    contingency._OPTIONAL_RE = _extend_regex(contingency._OPTIONAL_RE, _CAPACITY_BARE)

    old_strip_choice = human._strip_choice_filler
    if not getattr(old_strip_choice, "_narrative_guard_wrapped", False):
        def strip_choice(value):
            return _strip_return_choice_filler(value, old_strip_choice)
        strip_choice._narrative_guard_wrapped = True
        human._strip_choice_filler = strip_choice

    old_contingencies = contingency.compile_contingencies
    if not getattr(old_contingencies, "_narrative_guard_wrapped", False):
        choice_only = human._complete_optional_choices(lambda parsed, text, rows, config, now: parsed)

        def compile_contingencies(parsed, text, rows, config, now):
            result = old_contingencies(parsed, text, rows, config, now)
            for clause in _capacity_choice_clauses(text):
                result = choice_only(result, clause, rows, config, now)
            return result

        compile_contingencies._narrative_guard_wrapped = True
        contingency.compile_contingencies = compile_contingencies

    old_task_command = intents.task_command
    if not getattr(old_task_command, "_narrative_guard_wrapped", False):
        def task_command(text):
            if is_multi_context_narrative(text):
                return None
            return old_task_command(text)
        task_command._narrative_guard_wrapped = True
        intents.task_command = task_command

    old_reality_kind = intents.reality_kind
    if not getattr(old_reality_kind, "_narrative_guard_wrapped", False):
        def reality_kind(text):
            if is_multi_context_narrative(text):
                return None
            value = str(text or "")
            if _CLOCK_RANGE.search(value) and re.search(
                r"\b(?:on\s+(?:my|our)\s+way\s+to|heading\s+to|going\s+to)\b",
                value, re.I,
            ):
                return None
            return old_reality_kind(text)
        reality_kind._narrative_guard_wrapped = True
        intents.reality_kind = reality_kind

    old_fragments = day_activities.fragments
    if not getattr(old_fragments, "_narrative_guard_wrapped", False):
        def fragments(text):
            return _narrative_fragments(text, old_fragments)
        fragments._narrative_guard_wrapped = True
        day_activities.fragments = fragments

    old_target_day = general._target_day
    if not getattr(old_target_day, "_narrative_guard_wrapped", False):
        def target_day(text, now):
            explicit = old_target_day(text, now)
            if explicit is not None:
                return explicit
            value = str(text or "")
            # Leave future/date-explicit wording to the mature future-day planners. This
            # guard exists only to infer TODAY when the user gives no day at all.
            if _EXPLICIT_FUTURE_DAY.search(value):
                return None
            if is_multi_context_narrative(value):
                return now.date(), "today", re.match(r"", value)
            return None
        target_day._narrative_guard_wrapped = True
        general._target_day = target_day


__all__ = [
    "install_narrative_intake_guard", "is_multi_context_narrative",
    "_narrative_fragments", "_strip_return_choice_filler", "_capacity_choice_clauses",
    "_pure_temporary_care_chain",
]
