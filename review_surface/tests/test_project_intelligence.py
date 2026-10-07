import asyncio

import pytest

from app import project_intelligence as pi
from app import project_intelligence_runtime as pir


def draft():
    return pi.CampaignDraft.model_validate({
        "campaign_type": "hackathon",
        "goal": "Win the Example Hackathon",
        "deadline": "2026-11-20",
        "presentation_date": "2026-11-20",
        "knowledge_assumption": "NONE",
        "summary": "Build only the knowledge and implementation needed for this challenge.",
        "requirements": [{"label":"Final presentation","value":"20 Nov 2026","source":"user request","source_type":"user_input","confidence":"VERIFIED"}],
        "deliverables": [], "rules": [], "technical_requirements": [], "datasets": [],
        "rubric": [{"criterion":"Technical quality","weight":40,"source":"user request","source_type":"user_input","confidence":"VERIFIED"}],
        "milestones": [{"title":"Baseline","description":"Working baseline","definition_of_done":"Runs end to end"}],
        "work_packages": [
            {"key":"foundation","title":"Learn minimum solver foundations","description":"Only needed concepts","estimated_minutes":120,"dependencies":[],"phase":"learn","priority":"high","risk":"medium","definition_of_done":"Can build a tiny model","rubric_links":["Technical quality"],"confidence":"RECOMMENDED"},
            {"key":"build","title":"Build baseline","description":"First end-to-end version","estimated_minutes":240,"dependencies":["foundation"],"phase":"build","priority":"high","risk":"high","definition_of_done":"Demo works","rubric_links":["Technical quality"],"confidence":"RECOMMENDED"},
        ],
        "major_risks": ["Integration"],
    })


def test_project_intent_is_explicit_only():
    assert pi.looks_like_project_blueprint_request("Prepare me for this hackathon: https://example.com. Assume I know nothing. Build a complete preparation plan.")
    assert pi.looks_like_project_blueprint_request("Build a study plan for my ACMP exam from scratch")
    assert pi.looks_like_project_blueprint_request("I want to learn classical mechanics from my O-Level Physics baseline.")
    assert pi.looks_like_project_blueprint_request("Teach me electromagnetism using the books I own.")
    assert pi.looks_like_project_blueprint_request("I want to learn H2 Physics for 60 minutes per day.")
    assert not pi.looks_like_project_blueprint_request("I want to learn torque for 30 minutes tonight.")
    assert not pi.looks_like_project_blueprint_request("Add a task: I want to learn torque tonight.")
    assert not pi.looks_like_project_blueprint_request("I'm joining a hackathon Nov 20.")
    assert not pi.looks_like_project_blueprint_request("Plan my day.")
    assert not pi.looks_like_project_blueprint_request("Create a task to finish physics tomorrow.")


def test_named_competition_url_routes_without_generic_competition_noun():
    assert pi.looks_like_project_blueprint_request(
        "Prepare me for SPhL next year. https://sgphysicsleague.org"
    )
    assert not pi.looks_like_project_blueprint_request(
        "I visited https://sgphysicsleague.org"
    )


def test_blueprint_backplans_without_clock_times_and_preserves_dependency_gate():
    campaign = pi.build_campaign(draft(), "Prepare me for this hackathon", [])
    by = {p["key"]: p for p in campaign["work_packages"]}
    assert campaign["deadline_buffer_days"] >= 1
    assert by["foundation"]["target_finish"] <= by["build"]["target_finish"]
    assert "T" not in by["foundation"]["target_start"]
    by["build"]["target_start"] = None
    assert [x["key"] for x in pi.eligible_work_packages(campaign)] == ["foundation"]
    by["foundation"]["status"] = "done"
    assert [x["key"] for x in pi.eligible_work_packages(campaign)] == ["build"]


def test_historical_simulation_defaults_daily_integration_off():
    simulated = pi.build_campaign(draft(), "Prepare me for this hackathon as if upcoming", [])
    assert simulated["historical_simulation"] is True
    assert simulated["daily_integration"] is False

    real = pi.build_campaign(draft(), "Prepare me for my upcoming hackathon", [])
    assert real["historical_simulation"] is False
    assert real["daily_integration"] is True


def test_project_reasoning_defaults_to_zero_knowledge_unless_prior_knowledge_is_explicit(monkeypatch):
    async def fake_reason(request_text, docs):
        value = draft()
        value.knowledge_assumption = "MODEL_GUESS"
        return value

    monkeypatch.setattr(pi, "_BASE_REASON_CAMPAIGN", fake_reason)
    zero = asyncio.run(pi._zero_baseline_reason_campaign("Prepare me for this hackathon and build the complete plan", []))
    assert zero.knowledge_assumption == "ZERO_KNOWLEDGE_BASELINE"

    prior = asyncio.run(pi._zero_baseline_reason_campaign("Prepare me for this hackathon. I already know Python and optimization basics.", []))
    assert prior.knowledge_assumption == "USER_STATED_BASELINE: I already know Python and optimization basics"
    assert "I already know Python and optimization basics" in prior.assumed_mastered_topics


def test_provenance_rejects_verified_planner_inference():
    bad = draft().model_dump(mode="json")
    bad["requirements"] = [{"label":"Claim","value":"Invented","source":"planner","source_type":"planner_inference","confidence":"VERIFIED"}]
    with pytest.raises(Exception):
        pi.build_campaign(pi.CampaignDraft.model_validate(bad), "Prepare me", [])


@pytest.mark.parametrize("transition", ["so teach me university physics", "and progress to olympiad physics", "but I want to learn quantum theory"])
def test_learning_target_is_not_promoted_to_prior_knowledge(monkeypatch, transition):
    async def fake_reason(request_text, docs):
        return draft()
    monkeypatch.setattr(pi, "_BASE_REASON_CAMPAIGN", fake_reason)
    result = asyncio.run(pi._zero_baseline_reason_campaign(
        "Prepare me. I already know O level physics " + transition + ".", []))
    assert result.knowledge_assumption == "USER_STATED_BASELINE: I already know O level physics"
    assert result.assumed_mastered_topics == ["I already know O level physics"]


def test_private_url_is_rejected_before_fetch():
    with pytest.raises(ValueError):
        asyncio.run(pi.validate_public_url("http://127.0.0.1/internal"))


def test_final_schedule_commit_materializes_virtual_project_tasks_once(monkeypatch):
    campaign = pi.build_campaign(draft(), "Prepare me for this hackathon", [])
    campaign["ticktick_project_id"] = "p"
    next(p for p in campaign["work_packages"] if p["key"] == "build")["target_start"] = None
    state = {campaign["id"]: campaign}
    saved_meta = {}
    created = []

    monkeypatch.setattr(pir, "load_store", lambda: state)
    monkeypatch.setattr(pir, "save_store", lambda value: state.update(value))
    monkeypatch.setattr(pir.db, "set_meta", lambda task_id, meta: saved_meta.__setitem__(task_id, meta))
    monkeypatch.setattr(pir.db, "get_meta", lambda task_id: saved_meta.get(task_id, {}))
    monkeypatch.setattr(pir, "profile_namespace", lambda: "commit-only")
    pir._LOCKS.clear()

    class FakeTT:
        connected = True
        async def all_active_tasks(self):
            return [], [{"id":"p","name":"Project","kind":"TASK"}]
        async def create_task(self, project_id, title, **kwargs):
            task_id = f"t{len(created)+1}"
            created.append((task_id, title))
            return {"id": task_id, "projectId": project_id}

    monkeypatch.setattr(pir, "TickTickClient", FakeTT)

    tasks, meta, refs = pir.virtual_project_bundle(state)
    assert tasks and refs
    assert created == []
    payload = {
        "segments": [
            {"task_id": task.id, "start":"2026-10-07T08:00:00+08:00", "end":"2026-10-07T08:30:00+08:00"}
            for task in tasks
        ],
        "pending_project_materialization": refs,
        "_explicit_ticktick_apply": True,
    }
    mapping, changes = asyncio.run(pir._materialize_virtual_sources_for_commit(payload))
    assert [x[1] for x in created] == ["Learn minimum solver foundations", "Build baseline"]
    assert len(mapping) == 2 and len(changes) == 2
    assert saved_meta["t2"]["dependencies"] == ["t1"]

    mapping2, changes2 = asyncio.run(pir._materialize_virtual_sources_for_commit(payload))
    assert len(created) == 2
    assert mapping2 == mapping
    assert changes2 == []


def test_project_sync_is_read_only_by_default(monkeypatch):
    campaign = pi.build_campaign(draft(), "Prepare me for this hackathon", [])
    campaign["ticktick_project_id"] = "p"
    state = {campaign["id"]: campaign}
    created = []

    monkeypatch.setattr(pir, "load_store", lambda: state)
    monkeypatch.setattr(pir, "save_store", lambda value: state.update(value))
    monkeypatch.setattr(pir, "profile_namespace", lambda: "readonly-sync")
    monkeypatch.setattr(pir.db, "set_meta", lambda *args, **kwargs: None)
    monkeypatch.setattr(pir.db, "get_meta", lambda task_id: {})
    pir._LOCKS.clear()

    class FakeTT:
        connected = True
        async def all_active_tasks(self):
            return [], [{"id":"p","name":"Project","kind":"TASK"}]
        async def create_task(self, *args, **kwargs):
            created.append((args, kwargs))
            return {"id":"should-not-exist"}

    result = asyncio.run(pi.sync_campaigns(tt=FakeTT()))
    assert result["created"] == []
    assert created == []
    assert not any(p.get("ticktick_task_id") for p in campaign["work_packages"])



def test_repaired_roadmap_rewrites_stale_aggregate_effort_risk():
    value = draft()
    value.major_risks = [
        "About 100 minutes of planned preparation may be difficult to fit around school and sleep."
    ]
    campaign = pi.build_campaign(value, "Prepare me for this hackathon", [])
    total = sum(p["estimated_minutes"] for p in campaign["work_packages"])
    joined = " ".join(campaign["major_risks"])
    assert f"{total} estimated effort minutes" in joined
    assert "100 minutes of planned preparation" not in joined

def test_ordinary_language_classifier_contract_unchanged():
    from app.language_intake import classify
    assert classify("Plan my day.")[0] == "replan"
    assert classify("I just finished lunch.")[0] == "meal-completed"
    assert classify("Create a task to finish physics tomorrow.")[0] == "task"


def test_campaign_persists_bounded_source_content_memory():
    docs = [{
        "source": "https://sgphysicsleague.org/rules",
        "source_type": "website",
        "title": "Singapore Physics League rules",
        "text": (
            "Singapore Physics League. Competition format and rules. "
            "Online round duration is three hours. Scoring, submission requirements, "
            "problem format, syllabus scope and archive solutions are described here. "
        ) * 80,
    }]
    campaign = pi.build_campaign(draft(), "Prepare me for SPhL", docs)
    memory = campaign.get("source_memory") or []
    assert len(memory) == 1
    assert memory[0]["source"] == "https://sgphysicsleague.org/rules"
    assert memory[0]["source_type"] == "website"
    assert memory[0]["source_characters"] > 1000
    assert len(memory[0]["content_sha256"]) == 64
    assert "Competition format and rules" in memory[0]["remembered_excerpt"]
    # Persistent memory is intentionally bounded; the structured blueprint remains
    # authoritative instead of storing an unlimited copy of every website/PDF.
    assert len(memory[0]["remembered_excerpt"]) <= 15000
