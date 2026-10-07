from __future__ import annotations

"""Bounded semantic fallback for unresolved Temporal IR relationships.

The model may classify an unfamiliar relationship ("once I'm done there", "not right
after class but before dinner") but it is deliberately forbidden from producing concrete
dates or clock times. Deterministic Temporal Engine parsing remains the only authority for
absolute time. This keeps language intelligence without calendar hallucination.
"""

import asyncio
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .semantic_credentials import semantic_api_key, semantic_model
from .semantic_plan import LOCAL_REVIEW
from .temporal_engine import TemporalConstraint, Evidence, duration_minutes


class SemanticRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    relation: Literal["after", "before", "until", "by", "between"]
    anchor: str
    offset_minutes: int | None = Field(default=None, ge=-10080, le=10080)
    optional: bool
    negated: bool
    hypothetical: bool
    certainty: Literal["approximate", "ambiguous"]
    question: str | None


class SemanticTemporalMeaning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relations: list[SemanticRelation]


_SYSTEM = """Translate only unresolved temporal RELATIONSHIP language into a safe Temporal IR supplement.
You MUST NOT output or infer any absolute date, weekday occurrence, clock time, duration,
calendar event duration, timezone, or recurrence. Deterministic code owns all concrete
calendar arithmetic.

Each relation.source MUST be an exact contiguous excerpt of the user's request. anchor MUST
also be an exact contiguous excerpt of relation.source (or be exactly "submission_time").
Use:
- after: activity begins after anchor
- before: activity ends/is constrained before anchor
- until: activity/state continues until anchor
- by: latest completion/arrival is anchor
- between: source expresses a soft interval between two named anchors; keep both names in anchor

offset_minutes may be non-null ONLY when the exact source explicitly states that duration.
Positive means after the anchor; negative means before. Never convert vague words such as
"soon", "later", "a bit", "not immediately", "sometime" into minutes.

Preserve optionality, negation and hypothetical wording. If the relationship cannot be
represented without guessing, set question to a short focused clarification and keep anchor
as the exact unresolved phrase. Do not create tasks, do not schedule, and do not interpret
content unrelated to time."""


def _exact_span(haystack: str, needle: str) -> tuple[int, int] | None:
    if not needle:
        return None
    start = haystack.find(needle)
    if start >= 0:
        return start, start + len(needle)
    # Unicode apostrophe/case tolerance, but never fuzzy paraphrase.
    low_h = haystack.replace("’", "'").casefold()
    low_n = needle.replace("’", "'").casefold()
    start = low_h.find(low_n)
    return (start, start + len(needle)) if start >= 0 else None


def _validated_constraints(text: str, meaning: SemanticTemporalMeaning) -> tuple[list[TemporalConstraint], list[str]]:
    constraints: list[TemporalConstraint] = []
    questions: list[str] = []
    used_spans: list[tuple[int, int]] = []
    for item in meaning.relations[:20]:
        span = _exact_span(text, item.source)
        if span is None or any(span[0] < b and a < span[1] for a, b in used_spans):
            continue
        anchor_span = _exact_span(item.source, item.anchor) if item.anchor != "submission_time" else (0, 0)
        if anchor_span is None:
            continue
        if item.offset_minutes is not None:
            stated = duration_minutes(item.source)
            if stated is None or stated != abs(int(item.offset_minutes)):
                continue
            if item.relation == "after" and item.offset_minutes < 0:
                continue
            if item.relation == "before" and item.offset_minutes > 0:
                continue
        if item.question:
            questions.append(item.question)
            used_spans.append(span)
            continue
        constraints.append(TemporalConstraint(
            kind="relative",
            relation=item.relation,
            anchor=item.anchor,
            offset_minutes=item.offset_minutes if item.offset_minutes is not None else 0,
            optional=item.optional,
            negated=item.negated,
            hypothetical=item.hypothetical,
            evidence=Evidence(
                source=text[span[0]:span[1]],
                start=span[0],
                end=span[1],
                certainty=item.certainty,
            ),
        ))
        used_spans.append(span)
    return constraints, list(dict.fromkeys(questions))


async def semantic_temporal_fallback(text: str, unresolved: list[str], timeout_seconds: float = 4.0):
    """Return validated relationship-only constraints, or nothing on any provider failure."""
    if LOCAL_REVIEW.get() or not unresolved:
        return [], [], None
    key, model = semantic_api_key(), semantic_model()
    if not key or not model:
        return [], [], None
    payload = {
        "request": text,
        "unresolved_temporal_phrases": unresolved[:12],
    }

    async def call():
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": model,
                    "store": False,
                    "instructions": _SYSTEM,
                    "input": json.dumps(payload),
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "temporal_relationships",
                            "strict": True,
                            "schema": SemanticTemporalMeaning.model_json_schema(),
                        }
                    },
                    "max_output_tokens": 2500,
                },
            )
            response.raise_for_status()
            body = response.json()
        if body.get("status") != "completed":
            raise ValueError("incomplete")
        chunks = [
            part["text"]
            for item in body.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        return SemanticTemporalMeaning.model_validate_json("".join(chunks))

    try:
        meaning = await asyncio.wait_for(call(), timeout=timeout_seconds + 0.5)
        constraints, questions = _validated_constraints(text, meaning)
        return constraints, questions, "semantic-relationship"
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError):
        return [], [], None


__all__ = ["SemanticRelation", "SemanticTemporalMeaning", "semantic_temporal_fallback"]
