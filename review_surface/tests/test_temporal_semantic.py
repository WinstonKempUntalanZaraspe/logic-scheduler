import pytest
from pydantic import ValidationError

from app.temporal_semantic import (
    SemanticRelation,
    SemanticTemporalMeaning,
    _validated_constraints,
)


def meaning(**kwargs):
    return SemanticTemporalMeaning(relations=[SemanticRelation(**kwargs)])


def test_semantic_fallback_can_only_add_exact_source_relationships():
    text = "Do physics 20 minutes after lunch."
    constraints, questions = _validated_constraints(text, meaning(
        source="20 minutes after lunch",
        relation="after",
        anchor="lunch",
        offset_minutes=20,
        optional=False,
        negated=False,
        hypothetical=False,
        certainty="approximate",
        question=None,
    ))
    assert not questions
    assert len(constraints) == 1
    row = constraints[0]
    assert row.kind == "relative"
    assert row.anchor == "lunch"
    assert row.offset_minutes == 20
    assert row.evidence.source == "20 minutes after lunch"


def test_semantic_fallback_rejects_invented_offset():
    text = "Do physics sometime after lunch."
    constraints, _ = _validated_constraints(text, meaning(
        source="sometime after lunch",
        relation="after",
        anchor="lunch",
        offset_minutes=30,
        optional=False,
        negated=False,
        hypothetical=False,
        certainty="approximate",
        question=None,
    ))
    assert constraints == []


def test_semantic_fallback_rejects_paraphrased_non_source_anchor():
    text = "Continue once I'm back home."
    constraints, _ = _validated_constraints(text, meaning(
        source="once I'm back home",
        relation="after",
        anchor="arrive home",
        offset_minutes=None,
        optional=False,
        negated=False,
        hypothetical=False,
        certainty="ambiguous",
        question=None,
    ))
    assert constraints == []


def test_semantic_schema_has_no_absolute_clock_or_date_escape_hatch():
    payload = {
        "relations": [{
            "source": "after class",
            "relation": "after",
            "anchor": "class",
            "offset_minutes": None,
            "optional": False,
            "negated": False,
            "hypothetical": False,
            "certainty": "approximate",
            "question": None,
            "start_at": "2026-10-08T19:00:00+08:00",
        }]
    }
    with pytest.raises(ValidationError):
        SemanticTemporalMeaning.model_validate(payload)


def test_focused_question_is_returned_without_applying_a_constraint():
    text = "Do it sometime after that."
    constraints, questions = _validated_constraints(text, meaning(
        source="sometime after that",
        relation="after",
        anchor="that",
        offset_minutes=None,
        optional=False,
        negated=False,
        hypothetical=False,
        certainty="ambiguous",
        question="What does “that” refer to?",
    ))
    assert constraints == []
    assert questions == ["What does “that” refer to?"]
