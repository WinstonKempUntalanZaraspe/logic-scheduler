from __future__ import annotations

"""Keep Luna tolerant of discourse filler without accepting omitted real constraints/work."""

import re

from . import final_human_language_patch as human

_INSTALLED = False


def _has_meaningful_gap(data: dict, text: str) -> bool:
    cursor = 0
    for step in data.get("steps") or []:
        source = str(step.get("source") or "")
        pos = text.find(source, cursor)
        if not source or pos < 0:
            return True
        gap = text[cursor:pos]
        if re.search(r"\w", gap) and human._safe_gap_kind(gap)[0] == "question":
            return True
        cursor = pos + len(source)
    tail = text[cursor:]
    return bool(re.search(r"\w", tail) and human._safe_gap_kind(tail)[0] == "question")


def install_semantic_coverage_safety_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    base = human._insert_uncovered_steps

    def safe_insert(data: dict, text: str) -> dict:
        # A model that omitted actual work, a duration, a condition, a time, or another
        # scheduling-significant fragment is not trusted. Leave the gap untouched so the
        # original strict compiler rejects that interpretation. Only harmless filler and
        # discourse connectors may be reconstructed as history.
        if _has_meaningful_gap(data, text):
            return data
        return base(data, text)

    human._insert_uncovered_steps = safe_insert
    human.plan._SYSTEM += """

SOURCE COVERAGE SAFETY: never omit work, durations, choices, conditions, times, locations,
meals, sleep/recovery, negations, or constraints from source steps. Harmless discourse
filler may be history, but a meaningful omitted clause invalidates the interpretation.
"""


__all__ = ["install_semantic_coverage_safety_patch", "_has_meaningful_gap"]
