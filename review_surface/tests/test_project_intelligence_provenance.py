import pytest
from fastapi import HTTPException

from app.project_intelligence_models import CampaignDraft
from app.project_intelligence_provenance import validate_campaign_draft


def _draft(**patch):
    raw = {
        "campaign_type":"hackathon","goal":"Example Hackathon","deadline":"2026-11-20","presentation_date":None,
        "knowledge_assumption":"NONE","summary":"Targeted preparation",
        "requirements":[{"label":"Deadline","value":"20 November 2026","source":"https://example.com/rules","source_type":"website","confidence":"VERIFIED"}],
        "deliverables":[],"rules":[],"technical_requirements":[],"datasets":[],"rubric":[],
        "milestones":[{"title":"Ready","description":"Ready","definition_of_done":"Ready"}],
        "work_packages":[{"key":"wp1","title":"Build baseline","description":"Build","estimated_minutes":60,"dependencies":[],"phase":"build","priority":"high","risk":"medium","definition_of_done":"Runs","rubric_links":[],"confidence":"RECOMMENDED"}],
        "major_risks":[],
    }
    raw.update(patch)
    return CampaignDraft.model_validate(raw)


def test_verified_fact_is_canonicalized_to_the_fetched_source_that_supports_it():
    docs=[{"source":"https://example.com/rules","title":"Rules","source_type":"website","text":"Deadline: 20 November 2026."}]
    validate_campaign_draft(_draft(), "Prepare me for this hackathon", docs)
    mislabeled=_draft(requirements=[{"label":"Deadline","value":"20 November 2026","source":"https://fake.example/rules","source_type":"website","confidence":"VERIFIED"}])
    validate_campaign_draft(mislabeled,"Prepare me",docs)
    assert mislabeled.requirements[0].source == "https://example.com/rules"
    assert mislabeled.requirements[0].source_type == "website"


def test_verified_claim_must_be_grounded_in_a_real_evidence_document():
    docs=[{"source":"https://example.com/rules","title":"Rules","source_type":"website","text":"Deadline: 20 November 2026."}]
    bad=_draft(requirements=[{"label":"Prize","value":"Guaranteed ten thousand dollar cash prize","source":"https://example.com/rules","source_type":"website","confidence":"VERIFIED"}])
    with pytest.raises(HTTPException): validate_campaign_draft(bad,"Prepare me",docs)


def test_hard_deadline_must_appear_in_user_or_fetched_sources():
    docs=[{"source":"https://example.com/rules","title":"Rules","source_type":"website","text":"Final submission is 20 November 2026."}]
    validate_campaign_draft(_draft(),"Prepare me",docs)
    bad=_draft(deadline="2026-12-09",requirements=[])
    with pytest.raises(HTTPException): validate_campaign_draft(bad,"Prepare me",docs)


def test_user_supplied_month_day_can_ground_upcoming_iso_date():
    d=_draft(deadline="2026-11-20",requirements=[{"label":"Deadline","value":"Nov 20","source":"user request","source_type":"user_input","confidence":"VERIFIED"}])
    validate_campaign_draft(d,"Prepare me for the hackathon on Nov 20",[])
