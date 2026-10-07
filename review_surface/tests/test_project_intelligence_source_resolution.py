import pytest
from fastapi import HTTPException

from app.project_intelligence_models import CampaignDraft
from app.project_intelligence_provenance import validate_campaign_draft


def draft_with(section, item):
    raw = {
        "campaign_type": "hackathon",
        "goal": "NEBULA X",
        "deadline": None,
        "presentation_date": None,
        "knowledge_assumption": "ZERO_KNOWLEDGE_BASELINE",
        "summary": "Preparation",
        "requirements": [],
        "deliverables": [],
        "rules": [],
        "technical_requirements": [],
        "datasets": [],
        "rubric": [],
        "milestones": [{"title": "Ready", "description": "Ready", "definition_of_done": "Ready"}],
        "work_packages": [{
            "key": "wp1", "title": "Learn and build", "description": "Work", "estimated_minutes": 60,
            "dependencies": [], "phase": "learn", "priority": "high", "risk": "medium",
            "definition_of_done": "Done", "rubric_links": [], "confidence": "RECOMMENDED"
        }],
        "major_risks": [],
    }
    raw[section] = [item]
    return CampaignDraft.model_validate(raw)


def site(text, source="https://nebulax.com.sg/", title="NEBULA X"):
    return [{"source": source, "source_type": "website", "title": title, "text": text}]


def test_wrong_model_url_is_repaired_when_one_fetched_document_really_supports_claim():
    d = draft_with("requirements", {
        "label": "Challenge",
        "value": "Build a tool that automatically detects conflicts between maintenance requests",
        "source": "https://nebulax.com.sg/#maintenance-challenge",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    validate_campaign_draft(
        d,
        "Prepare me for this hackathon: https://nebulax.com.sg/#line",
        site("THE CHALLENGE Build a tool that automatically detects conflicts between maintenance requests, flags them clearly, suggests alternatives and automates the scheduling process."),
    )
    assert d.requirements[0].source == "https://nebulax.com.sg/"
    assert d.requirements[0].source_type == "website"


def test_completely_invented_model_source_does_not_kill_a_supported_fact():
    d = draft_with("requirements", {
        "label": "Challenge",
        "value": "Build a tool that automatically detects conflicts between maintenance requests",
        "source": "https://made-up.invalid/fake",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    validate_campaign_draft(
        d,
        "Prepare me for this hackathon",
        site("Build a tool that automatically detects conflicts between maintenance requests."),
    )
    assert d.requirements[0].source == "https://nebulax.com.sg/"


def test_wrong_model_source_still_fails_when_no_fetched_document_supports_claim():
    d = draft_with("requirements", {
        "label": "Requirement",
        "value": "Every team must use Rust",
        "source": "https://made-up.invalid/fake",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(d, "Prepare me", site("Teams may use any appropriate technology."))
    assert err.value.status_code == 422
    assert "could not ground" in str(err.value.detail).casefold()


def test_website_label_is_repaired_to_user_input_when_user_explicitly_supplied_fact():
    d = draft_with("technical_requirements", {
        "label": "Solver",
        "value": "Use Python and OR-Tools CP-SAT",
        "source": "https://nebulax.com.sg/",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    validate_campaign_draft(
        d,
        "Prepare me. The challenge uses Python and OR-Tools CP-SAT.",
        site("Build a tool that automates maintenance scheduling."),
    )
    assert d.technical_requirements[0].source_type == "user_input"
    assert d.technical_requirements[0].source == "user request"


def test_rubric_weight_is_not_repaired_from_criterion_only():
    d = draft_with("rubric", {
        "criterion": "Technical quality",
        "weight": 40,
        "source": "https://whatever.invalid/",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    with pytest.raises(HTTPException):
        validate_campaign_draft(
            d,
            "Prepare me",
            site("Judging includes technical quality and operational feasibility."),
        )


def test_user_supplied_rubric_weight_can_be_repaired_from_bad_website_label():
    d = draft_with("rubric", {
        "criterion": "Technical quality",
        "weight": 40,
        "source": "https://nebulax.com.sg/",
        "source_type": "website",
        "confidence": "VERIFIED",
    })
    validate_campaign_draft(
        d,
        "Judging: technical quality 40%, operational feasibility 40%, presentation 20%.",
        site("Judging dimensions include technical execution, problem fit, ease of use and real-world impact."),
    )
    assert d.rubric[0].source_type == "user_input"
    assert d.rubric[0].source == "user request"
