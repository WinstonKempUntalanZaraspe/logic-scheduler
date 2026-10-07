"""Recognize supported planner policies without turning them into work."""

from __future__ import annotations

import re

_CLOCK = re.compile(
    r"^(?:(?:plan|replan|schedule|start)(?:\s+(?:my|the|this)\s+(?:day|schedule|plan))?\s+"
    r"(?:from|at|using|based\s+on)|use)\s+(?:the\s+|my\s+)?"
    r"(?:(?:current|actual|live|latest|real)\s+(?:local\s+)?time\b|local\s+time\b|"
    r"(?:right\s+)?now\b|(?:time|timestamp)\s+(?:when|at\s+which|that)\s+i\s+"
    r"(?:send|submit|sent)\b)",
    re.I,
)

_REPLAN = re.compile(
    r"^(?:please\s+)?(?:plan|replan|reschedule|rebuild|reorganize|reorganise|optimize|optimise)"
    r"\s+(?:(?:my|the|this)\s+)?(?:day|schedule|tasks?|morning|today|everything)"
    r"(?:\s+(?:today|tomorrow|tonight))?$",
    re.I,
)

# Keep these patterns deliberately simple and independently balanced.
# This is policy recognition only; it must never create work.
_PRODUCTIVE_PATTERNS = (
    r"^(?:use|fill)\s+(?:(?:at\s+least|at\s+most|no\s+more\s+than|only)\s+\d+(?:\.\d+)?\s*(?:minutes?|mins?|m|hours?|hrs?|h)\s+of\s+)?(?:(?:any|the|my|all)\s+)?(?:(?:available|spare|free|open|remaining|useful)\s+)?(?:gaps?|time|slots?|blocks?)\s+(?:for|with|to)\b",
    r"^(?:maximize|maximise)\s+(?:(?:my|the)\s+)?(?:productive\s+time|productivity)\b",
    r"^make\s+the\s+most\s+of\s+(?:(?:my|the|any)\s+)?(?:available|free|spare)\s+time\b",
)
_PRODUCTIVE = tuple(re.compile(pattern, re.I) for pattern in _PRODUCTIVE_PATTERNS)

_SLEEP_OBJECT = r"(?:bedtime|sleep(?:\s+(?:time|schedule|window))?|wind[\s-]+down(?:\s+time)?)"

_SLEEP = re.compile(
    r"^(?:respect|protect|keep|preserve|follow|use)\s+(?:(?:my|the|our)\s+)?"
    r"(?:(?:usual|normal|saved|regular|existing)\s+)?" + _SLEEP_OBJECT +
    r"(?:\s+and\s+(?:(?:my|the)\s+)?" + _SLEEP_OBJECT + r")?(?:\s+protected)?$",
    re.I,
)


def _instruction_value(text):
    value = re.sub(r"\s+", " ", str(text or "").replace("’", "'")).strip(" .")
    return re.sub(
        r"^(?:please\s+|i\s+(?:want|need)\s+you\s+to\s+)",
        "",
        value,
        flags=re.I,
    )


def _instruction_rule(value):
    if value.endswith("?"):
        return None
    if _CLOCK.match(value):
        return "submission_clock"
    if _SLEEP.fullmatch(value):
        return "protect_sleep"
    if any(pattern.match(value) for pattern in _PRODUCTIVE) and re.search(
        r"\b(?:work|tasks?|study|studying|productive|productivity|progress)\b",
        value,
        re.I,
    ):
        return "productive_time"
    return None


def is_replan_command(text):
    """Return True when text is a scheduling/replanning command, not a task."""
    return bool(_REPLAN.fullmatch(_instruction_value(text)))


def is_planning_instruction(text):
    return bool(_instruction_rule(_instruction_value(text)))


def supported_instruction_rules(text):
    value = _instruction_value(text)
    if re.search(r"\b(?:at\s+least|at\s+most|no\s+more\s+than|only)\b|\d", value, re.I):
        return []
    rule = _instruction_rule(value)
    return [rule] if rule else []
