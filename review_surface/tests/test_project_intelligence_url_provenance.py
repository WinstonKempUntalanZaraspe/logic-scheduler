from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.project_intelligence_models import CampaignDraft, extract_urls
from app.project_intelligence_provenance import install, validate_campaign_draft


def _draft(source, value="Build a tool that automates scheduling"):
    return CampaignDraft.model_validate({
        "campaign_type": "hackathon",
        "goal": "Nebula X",
        "deadline": None,
        "presentation_date": None,
        "knowledge_assumption": "ZERO_KNOWLEDGE_BASELINE",
        "summary": "Prepare for railway maintenance scheduling",
        "requirements": [{
            "label": "Challenge",
            "value": value,
            "source": source,
            "source_type": "website",
            "confidence": "VERIFIED",
        }],
        "deliverables": [],
        "rules": [],
        "technical_requirements": [],
        "datasets": [],
        "rubric": [],
        "milestones": [{"title": "Ready", "description": "Ready", "definition_of_done": "Ready"}],
        "work_packages": [{
            "key": "wp1",
            "title": "Learn scheduling model basics",
            "description": "Learn variables, constraints and objectives",
            "estimated_minutes": 45,
            "dependencies": [],
            "phase": "learn",
            "priority": "high",
            "risk": "medium",
            "definition_of_done": "Model a tiny scheduling problem",
            "rubric_links": [],
            "confidence": "RECOMMENDED",
        }],
        "major_risks": [],
    })


def _nebula_doc():
    return [{
        "source": "https://nebulax.com.sg/",
        "title": "NEBULA X · The Living Railway — Future of Mobility",
        "source_type": "website",
        "text": "YOUR MISSION Build the tool that automates scheduling. THE CHALLENGE Build a tool that automatically detects conflicts between maintenance requests.",
    }]


def test_fragment_url_citation_matches_same_fetched_page():
    draft = _draft("https://nebulax.com.sg/#line")
    validate_campaign_draft(draft, "Prepare me from https://nebulax.com.sg/#line", _nebula_doc())
    assert draft.requirements[0].source == "https://nebulax.com.sg/"


def test_trailing_slash_and_default_https_port_are_same_resource():
    draft = _draft("https://NEBULAX.com.sg:443#tracks")
    validate_campaign_draft(draft, "Prepare me", _nebula_doc())
    assert draft.requirements[0].source == "https://nebulax.com.sg/"


def test_wrong_path_label_is_repaired_when_fetched_page_really_supports_claim():
    draft = _draft("https://nebulax.com.sg/not-the-fetched-page#lines")
    validate_campaign_draft(draft, "Prepare me", _nebula_doc())
    assert draft.requirements[0].source == "https://nebulax.com.sg/"


def test_wrong_query_label_is_repaired_when_fetched_page_really_supports_claim():
    draft = _draft("https://nebulax.com.sg/?track=other")
    validate_campaign_draft(draft, "Prepare me", _nebula_doc())
    assert draft.requirements[0].source == "https://nebulax.com.sg/"


def test_bad_source_label_does_not_allow_unsupported_claim():
    draft = _draft("https://nebulax.com.sg/not-real", "Teams must use Rust and CUDA")
    with pytest.raises(HTTPException) as err:
        validate_campaign_draft(draft, "Prepare me", _nebula_doc())
    assert err.value.status_code == 422
    assert "could not ground" in str(err.value.detail).casefold()


def test_install_makes_runtime_url_extraction_accept_backslash_escaped_protocol():
    async def base_reason(request_text, docs):
        return _draft("https://nebulax.com.sg/")

    runtime = SimpleNamespace(reason_campaign=base_reason, extract_urls=extract_urls)
    install(runtime)

    assert runtime.extract_urls(r"Prepare me: https\://nebulax.com.sg/#line") == ["https://nebulax.com.sg/#line"]
    assert runtime.extract_urls(r"Prepare me: https:\/\/nebulax.com.sg/#line") == ["https://nebulax.com.sg/#line"]


def test_normal_url_extraction_is_unchanged_after_install():
    async def base_reason(request_text, docs):
        return _draft("https://nebulax.com.sg/")

    runtime = SimpleNamespace(reason_campaign=base_reason, extract_urls=extract_urls)
    install(runtime)
    assert runtime.extract_urls("Use https://nebulax.com.sg/#line and plan it") == ["https://nebulax.com.sg/#line"]
