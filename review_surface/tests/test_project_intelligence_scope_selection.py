import pytest

from app import project_intelligence as pi


def _draft():
    return pi.CampaignDraft.model_validate({
        "campaign_type": "hackathon",
        "goal": "Prepare for an example event",
        "deadline": "2026-11-20",
        "presentation_date": "2026-11-20",
        "knowledge_assumption": "ZERO_KNOWLEDGE_BASELINE",
        "summary": "Prepare for the selected challenge.",
        "requirements": [],
        "deliverables": [],
        "rules": [],
        "technical_requirements": [],
        "datasets": [],
        "rubric": [],
        "milestones": [],
        "work_packages": [{
            "key": "foundation",
            "title": "Learn selected-track foundations",
            "description": "Learn only the foundations required by the selected track.",
            "estimated_minutes": 60,
            "dependencies": [],
            "phase": "learn",
            "priority": "high",
            "risk": "medium",
            "definition_of_done": "Can explain and apply the selected-track basics.",
            "rubric_links": [],
            "confidence": "RECOMMENDED",
        }],
        "major_risks": [],
    })


def _option(key, title):
    return {
        "key": key,
        "title": title,
        "summary": title + " challenge",
        "source": "source_1",
        "source_type": "website",
        "confidence": "VERIFIED",
    }


DOCS = [{"source":"https://example.com","source_type":"website","title":"Example","text":"multiple challenges"}]


def test_multiple_tracks_require_an_explicit_user_choice():
    value = _draft()
    value.scope_options = [
        _option("ps1", "Railway maintenance scheduling"),
        _option("ps2", "Passenger flow optimisation"),
        _option("ps3", "Predictive maintenance using machine learning"),
    ]
    value.selected_scope = "ps1"
    value.scope_selection_basis = "ambiguous"
    with pytest.raises(Exception):
        pi.build_campaign(value, "Prepare me for this hackathon from zero knowledge.", DOCS)


def test_explicit_track_id_is_accepted():
    value = _draft()
    value.scope_options = [
        _option("ps1", "Railway maintenance scheduling"),
        _option("ps3", "Predictive maintenance using machine learning"),
    ]
    value.selected_scope = "ps3"
    value.scope_selection_basis = "user_explicit"
    campaign = pi.build_campaign(value, "Prepare me for PS3 from zero knowledge.", DOCS)
    assert campaign["selected_scope"] == "ps3"
    assert campaign["scope_selection_basis"] == "user_explicit"


def test_model_cannot_invent_a_user_track_choice():
    value = _draft()
    value.scope_options = [
        _option("optimisation", "Railway maintenance scheduling"),
        _option("ml", "Predictive maintenance using machine learning"),
    ]
    value.selected_scope = "ml"
    value.scope_selection_basis = "user_explicit"
    with pytest.raises(Exception):
        pi.build_campaign(value, "Prepare me for this hackathon from zero knowledge.", DOCS)


def test_source_crosscheck_rejects_model_that_omits_numbered_problem_statements():
    value = _draft()
    value.scope_options = [_option("ps1", "Railway maintenance scheduling")]
    value.selected_scope = "ps1"
    value.scope_selection_basis = "single_option"
    docs = [{
        "source": "https://example.com",
        "source_type": "website",
        "title": "Example event",
        "text": "Problem Statement 1 Railway scheduling. Problem Statement 2 Passenger flow. Problem Statement 3 Predictive maintenance.",
    }]
    with pytest.raises(Exception):
        pi.build_campaign(value, "Prepare me for this hackathon from zero knowledge.", docs)


def test_source_crosscheck_deduplicates_repeated_same_problem_number():
    value = _draft()
    value.scope_options = [_option("ps1", "Railway maintenance scheduling")]
    value.selected_scope = "ps1"
    value.scope_selection_basis = "single_option"
    docs = [{
        "source": "https://example.com",
        "source_type": "website",
        "title": "Single challenge event",
        "text": "PS1 Railway scheduling. Problem Statement 1 details. PS1 deliverables.",
    }]
    campaign = pi.build_campaign(value, "Prepare me for this hackathon from zero knowledge.", docs)
    assert campaign["source_scope_markers"] == [1]


def test_named_track_crosscheck_rejects_collapsing_competitive_and_casual():
    value = _draft()
    value.campaign_type = "competition"
    value.scope_options = [_option("competitive", "Competitive Track")]
    value.selected_scope = "competitive"
    value.scope_selection_basis = "single_option"
    docs = [{
        "source": "https://example.com/rules",
        "source_type": "website",
        "title": "Competition rules",
        "text": "The event offers two ways to take part: Competitive Track and Casual Track.",
    }]
    with pytest.raises(Exception):
        pi.build_campaign(value, "Prepare me for this competition.", docs)


def test_named_tracks_are_preserved_when_user_explicitly_selects_competitive():
    value = _draft()
    value.campaign_type = "competition"
    value.scope_options = [
        _option("competitive", "Competitive Track"),
        _option("casual", "Casual Track"),
    ]
    value.selected_scope = "competitive"
    value.scope_selection_basis = "user_explicit"
    docs = [{
        "source": "https://example.com/rules",
        "source_type": "website",
        "title": "Competition rules",
        "text": "Competitive Track has rankings and finals. Casual Track is participation only.",
    }]
    campaign = pi.build_campaign(value, "Prepare me for the Senior Competitive Track.", docs)
    assert campaign["selected_scope"] == "competitive"
    assert set(campaign["source_named_tracks"]) >= {"competitive", "casual"}


def test_scope_preflight_blocks_bare_competitive_vs_casual_before_generation():
    from app.project_intelligence_models import preflight_source_scope_choice
    docs = [{
        "source": "https://sgphysicsleague.example/rules",
        "source_type": "website",
        "title": "Rules",
        "text": "SPhL offers Competitive Track and Casual Track.",
    }]
    with pytest.raises(Exception) as exc:
        preflight_source_scope_choice("Prepare me for SPhL next year.", docs)
    assert getattr(exc.value, "status_code", None) == 409
    preflight_source_scope_choice("Prepare me for the Senior Competitive Track.", docs)


def test_named_track_extractor_ignores_generic_two_tracks_and_rounds_prose():
    from app.project_intelligence_models import _source_named_track_markers
    docs = [{
        "source": "https://sgphysicsleague.org/rules",
        "source_type": "website",
        "title": "SPhL rules",
        "text": (
            "Two Tracks. SPhL offers Competitive Track and Casual Track. "
            "Two rounds of competition. Competitive Track teams can qualify for Grand Finals. "
            "The competition rounds track team progress over time."
        ),
    }]
    assert _source_named_track_markers(docs) == ["casual", "competitive"]


def test_past_physics_tracks_do_not_become_current_competition_choices():
    from app.project_intelligence_models import preflight_source_scope_choice, _source_named_track_markers
    docs = [
        {"source": "https://contest.example/rules", "text": "Competitive Track and Casual Track"},
        {"source": "https://contest.example/archives/2026/solutions.pdf",
         "text": "A wheel moves on a frictionless track and another track. Green Track: Problem Statement 1; Problem Statement 2."},
    ]
    assert _source_named_track_markers(docs) == ["casual", "competitive"]
    with pytest.raises(Exception) as error:
        preflight_source_scope_choice("Prepare me and preserve school and sleep", docs)
    assert getattr(error.value, 'status_code', None) == 409
    preflight_source_scope_choice("Prepare me for the Senior Competitive Track", docs)
