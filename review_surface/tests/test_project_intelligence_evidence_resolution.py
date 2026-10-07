import pytest
from fastapi import HTTPException

from app.project_intelligence_models import CampaignDraft
from app.project_intelligence_provenance import validate_campaign_draft


def _draft(**patch):
    raw = {
        "campaign_type": "hackathon",
        "goal": "Example Hackathon",
        "deadline": None,
        "presentation_date": None,
        "knowledge_assumption": "ZERO_KNOWLEDGE_BASELINE",
        "summary": "Targeted preparation",
        "requirements": [],
        "deliverables": [],
        "rules": [],
        "technical_requirements": [],
        "datasets": [],
        "rubric": [],
        "milestones": [{"title": "Ready", "description": "Ready", "definition_of_done": "Ready"}],
        "work_packages": [{
            "key": "wp1",
            "title": "Build baseline",
            "description": "Build",
            "estimated_minutes": 60,
            "dependencies": [],
            "phase": "build",
            "priority": "high",
            "risk": "medium",
            "definition_of_done": "Runs",
            "rubric_links": [],
            "confidence": "RECOMMENDED",
        }],
        "major_risks": [],
    }
    raw.update(patch)
    return CampaignDraft.model_validate(raw)


def _website(text, source="https://example.com/rules", title="Rules"):
    return [{"source": source, "title": title, "source_type": "website", "text": text}]


def test_verified_fact_mislabeled_user_input_is_repaired_to_fetched_website():
    draft = _draft(requirements=[{
        "label": "Submission format",
        "value": "Teams must submit a working prototype",
        "source": "user request",
        "source_type": "user_input",
        "confidence": "VERIFIED",
    }])
    request = "Prepare me for this hackathon: https://example.com/rules"
    validate_campaign_draft(draft, request, _website("Rules: Teams must submit a working prototype."))
    claim = draft.requirements[0]
    assert claim.source_type == "website"
    assert claim.source == "https://example.com/rules"


def test_url_in_request_is_not_itself_evidence_for_website_fact():
    draft = _draft(requirements=[{
        "label": "Dataset",
        "value": "Use the official transit dataset",
        "source": "user request",
        "source_type": "user_input",
        "confidence": "VERIFIED",
    }])
    request = "Prepare me from https://example.com/rules"
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(draft, request, _website("Welcome to the event."))
    assert err.value.status_code == 422
    assert "request or fetched sources" in str(err.value.detail)


def test_wrong_model_website_citation_is_repaired_to_document_that_supports_claim():
    draft = _draft(rules=[{
        "label": "Team size",
        "value": "Teams may have up to four members",
        "source": "https://example.com/home",
        "source_type": "website",
        "confidence": "VERIFIED",
    }])
    docs = [
        {"source": "https://example.com/home", "title": "Home", "source_type": "website", "text": "Hackathon home page."},
        {"source": "https://example.com/rules", "title": "Rules", "source_type": "website", "text": "Teams may have up to four members."},
    ]
    validate_campaign_draft(draft, "Prepare me for this hackathon", docs)
    assert draft.rules[0].source == "https://example.com/rules"
    assert draft.rules[0].source_type == "website"


def test_verified_rubric_weight_mislabeled_user_input_is_repaired_only_with_numeric_support():
    draft = _draft(rubric=[{
        "criterion": "Technical quality",
        "weight": 40,
        "source": "user request",
        "source_type": "user_input",
        "confidence": "VERIFIED",
    }])
    validate_campaign_draft(
        draft,
        "Prepare me for this hackathon: https://example.com/judging",
        _website("Judging criteria\nTechnical quality: 40%\nPresentation: 60%", "https://example.com/judging", "Judging"),
    )
    assert draft.rubric[0].source_type == "website"
    assert draft.rubric[0].source == "https://example.com/judging"


def test_rubric_same_criterion_wrong_percentage_still_fails_closed():
    draft = _draft(rubric=[{
        "criterion": "Technical quality",
        "weight": 80,
        "source": "user request",
        "source_type": "user_input",
        "confidence": "VERIFIED",
    }])
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(
            draft,
            "Prepare me for this hackathon: https://example.com/judging",
            _website("Technical quality: 40%. Presentation: 60%.", "https://example.com/judging", "Judging"),
        )
    assert err.value.status_code == 422
    assert "rubric criterion and weight" in str(err.value.detail)


def test_planner_inference_marked_verified_still_fails_even_if_source_has_same_words():
    draft = _draft(technical_requirements=[{
        "label": "Language",
        "value": "Python is required",
        "source": "planner",
        "source_type": "planner_inference",
        "confidence": "VERIFIED",
    }])
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(
            draft,
            "Prepare me for this hackathon",
            _website("Technical requirement: Python is required."),
        )
    assert err.value.status_code == 422
    assert "inference labelled VERIFIED" in str(err.value.detail)


def test_truly_unsupported_verified_inference_still_fails_closed():
    draft = _draft(technical_requirements=[{
        "label": "Language",
        "value": "Rust is mandatory",
        "source": "planner",
        "source_type": "planner_inference",
        "confidence": "VERIFIED",
    }])
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(draft, "Prepare me for this hackathon", _website("Any programming language is allowed."))
    assert err.value.status_code == 422
    assert "inference labelled VERIFIED" in str(err.value.detail)
