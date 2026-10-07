from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.config import settings
from app.project_intelligence_models import CampaignDraft
from app.project_intelligence_provenance import sanitize_campaign_draft, validate_campaign_draft


def _draft(**patch):
    raw = {
        "campaign_type": "hackathon",
        "goal": "Nebula X",
        "deadline": None,
        "presentation_date": None,
        "knowledge_assumption": "ZERO_KNOWLEDGE_BASELINE",
        "summary": "Prepare for the challenge",
        "requirements": [{
            "label": "Challenge",
            "value": "Build a tool that automatically detects conflicts between maintenance requests",
            "source": "source_1",
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
            "title": "Learn scheduling basics",
            "description": "Learn variables, constraints and objectives, then model a tiny scheduling problem.",
            "estimated_minutes": 45,
            "dependencies": [],
            "phase": "learn",
            "priority": "high",
            "risk": "medium",
            "definition_of_done": "A tiny model solves correctly.",
            "rubric_links": [],
            "confidence": "RECOMMENDED",
        }],
        "major_risks": [],
    }
    raw.update(patch)
    return CampaignDraft.model_validate(raw)


def _docs():
    return [{
        "source_id": "source_1",
        "source": "https://nebulax.com.sg/",
        "title": "NEBULA X",
        "source_type": "website",
        "text": (
            "Build a tool that automatically detects conflicts between maintenance requests. "
            "Flag conflicts clearly and suggest alternative schedules."
        ),
    }]


def test_fail_soft_omits_one_unsupported_verified_deliverable_but_keeps_blueprint():
    d = _draft(deliverables=[{
        "label": "Hack submission",
        "value": "Submit a mandatory hack package through a portal",
        "source": "source_1",
        "source_type": "website",
        "confidence": "VERIFIED",
    }])
    sanitize_campaign_draft(d, "Prepare me for this hackathon", _docs())
    assert d.deliverables == []
    assert len(d.requirements) == 1
    assert len(d.work_packages) == 1
    assert any("Hack submission" in risk for risk in d.major_risks)


def test_fail_soft_never_keeps_an_unsupported_fact_as_verified():
    d = _draft(rules=[{
        "label": "Secret rule",
        "value": "All teams must use Rust",
        "source": "source_1",
        "source_type": "website",
        "confidence": "VERIFIED",
    }])
    sanitize_campaign_draft(d, "Prepare me", _docs())
    assert d.rules == []
    assert all("Rust" not in x.value for x in d.requirements + d.deliverables + d.rules + d.technical_requirements + d.datasets)


def test_strict_validator_still_rejects_the_same_unsupported_fact():
    d = _draft(deliverables=[{
        "label": "Hack submission",
        "value": "Submit a mandatory hack package through a portal",
        "source": "source_1",
        "source_type": "website",
        "confidence": "VERIFIED",
    }])
    with pytest.raises(HTTPException):
        validate_campaign_draft(d, "Prepare me", _docs())


def test_as_if_upcoming_historical_event_does_not_use_past_date_as_live_deadline():
    past = (datetime.now(settings.tz).date() - timedelta(days=7)).isoformat()
    d = _draft(deadline=past)
    text = f"Prepare me for this hackathon as if it were upcoming. The event deadline was {past}."
    sanitize_campaign_draft(d, text, [])
    assert d.deadline is None
    assert any("Historical deadline" in risk for risk in d.major_risks)


def test_normal_past_date_is_not_silently_rewritten_without_simulation_language():
    past = (datetime.now(settings.tz).date() - timedelta(days=7)).isoformat()
    d = _draft(deadline=past)
    text = f"Prepare me for this hackathon. Deadline: {past}."
    sanitize_campaign_draft(d, text, [])
    assert d.deadline == past
