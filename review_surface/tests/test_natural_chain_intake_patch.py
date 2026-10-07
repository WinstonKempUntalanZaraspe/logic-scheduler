import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, intake_contract
import app.final_entrypoint  # installs production intake stack
from app.semantic_plan import LOCAL_REVIEW

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 7, 12, 15, tzinfo=TZ)
CFG = {
    "wake_time": "07:00",
    "sleep_start": "23:00",
    "day_start": "07:00",
    "day_end": "23:00",
    "post_meal_swim_buffer_minutes": 45,
    "personal_travel_minutes": 25,
    "default_travel_buffer_minutes": 25,
}
PROMPT = "I will eat lunch now, then swim later for 2 hours then go to my aunties house to get bread for 1 hour then go home"


def review(text=PROMPT):
    db.init_db()
    token = LOCAL_REVIEW.set(True)
    try:
        return asyncio.run(intake_contract.review_intake(text, [], CFG, NOW))
    finally:
        LOCAL_REVIEW.reset(token)


def ref(change):
    return str(change.get("task_id") or change.get("preview_task_id") or "")


def test_exact_current_meal_future_chain_is_clause_local_and_complete():
    parsed = review()
    assert not parsed.get("clarifications"), parsed
    blocks = parsed["context"]["temporary_blocks"]
    lunch = next(x for x in blocks if x.get("meal") == "lunch")
    assert lunch["start"].startswith("2026-10-07T12:17")
    assert lunch["end"].startswith("2026-10-07T12:57"), parsed

    tasks = parsed["tasks"]
    assert len(tasks) == 2, parsed
    swim = next(x for x in tasks if "swim" in x["title"].lower())
    bread = next(x for x in tasks if "bread" in x["title"].lower())

    assert swim["meta_patch"]["duration_minutes"] == 120
    assert swim["meta_patch"]["_explicit_activity_minutes"] == 120
    assert bread["meta_patch"]["duration_minutes"] == 60
    assert bread["meta_patch"]["_explicit_activity_minutes"] == 60
    assert bread["meta_patch"]["location"].lower() == "my aunties house"
    assert bread["meta_patch"]["excursion"] is True
    assert not any("home" == x["title"].strip().lower() or "go home" in x["title"].lower() for x in tasks)

    swim_ref, bread_ref = ref(swim), ref(bread)
    ctx = parsed["context"]
    assert ctx["after_meal_task_ids"][swim_ref] == "lunch"
    assert ctx["plan_local_dependencies"][bread_ref] == [swim_ref]
    assert swim_ref in ctx["journey_state"]["replaces_return_owner_ids"]
    assert ctx["journey_state"]["ends_at_home"] is True
    assert ctx["intent_exact_order"] == [swim["title"], bread["title"]]


def test_current_meal_does_not_steal_later_activity_duration():
    parsed = review("I'm eating breakfast now, then study calculus for 45 minutes then go home")
    breakfast = next(x for x in parsed["context"]["temporary_blocks"] if x.get("meal") == "breakfast")
    assert breakfast["end"].startswith("2026-10-07T12:45")
    task = next(x for x in parsed["tasks"] if "calculus" in x["title"].lower())
    assert task["meta_patch"]["duration_minutes"] == 45
    assert not any("home" in x["title"].lower() for x in parsed["tasks"])


def test_chain_is_preview_only_and_does_not_grant_current_state_task_authority():
    parsed = review()
    assert not any("lunch" in str(x.get("title") or "").lower() for x in parsed["tasks"])
    assert all(x.get("action") in {"create", "update"} for x in parsed["tasks"])
    assert all(x.get("intake_kind") == "task" for x in parsed["tasks"])
    assert parsed.get("interpreter_mode") == "deterministic"


def test_bare_eat_lunch_now_chain_does_not_create_the_whole_sentence():
    text = "eat lunch now, then I want to swim then I will go to my auntie's house to get bread"
    parsed = review(text)
    assert not parsed.get("clarifications"), parsed
    lunch = next(x for x in parsed["context"]["temporary_blocks"] if x.get("meal") == "lunch")
    assert lunch["start"].startswith("2026-10-07T12:17")
    assert lunch["end"].startswith("2026-10-07T12:57")

    tasks = parsed["tasks"]
    assert len(tasks) == 2, parsed
    assert any("swim" in x["title"].lower() for x in tasks)
    assert any("bread" in x["title"].lower() for x in tasks)
    whole = " ".join(text.lower().split())
    assert all(" ".join(str(x.get("title") or "").lower().split()) != whole for x in tasks)
    assert all(" ".join(str(x.get("line") or "").lower().split()) != whole for x in tasks)
    assert not any("lunch" in str(x.get("title") or "").lower() for x in tasks)


def test_real_life_chain_binds_sphl_to_stored_campaign_without_generic_physics_task(monkeypatch):
    from app import project_reference

    campaign = {
        "id": "sphl",
        "status": "active",
        "goal": "Win SPhL 2027 Senior Competitive Track",
        "request": "Prepare me to win SPhL 2027 Senior Competitive Track. I already know O-Level Physics.",
        "source_documents": [{"source": "https://sgphysicsleague.org", "title": "Singapore Physics League"}],
        "requirements": [{"value": "Singapore Physics League Senior Competitive Track"}],
        "rules": [],
        "technical_requirements": [],
        "ticktick_project_id": "physics-project",
        "work_packages": [
            {
                "key": "mechanics-foundation",
                "title": "Olympiad mechanics — force models and constrained motion",
                "status": "pending",
                "dependencies": [],
                "review_delay_days": None,
                "ticktick_task_id": None,
                "estimated_minutes": 240,
                "remaining_minutes": 240,
            },
            {
                "key": "harder-mechanics",
                "title": "Advanced mechanics problem solving",
                "status": "pending",
                "dependencies": ["mechanics-foundation"],
                "review_delay_days": None,
                "ticktick_task_id": None,
                "estimated_minutes": 300,
                "remaining_minutes": 300,
            },
        ],
    }
    monkeypatch.setattr(project_reference, "load_store", lambda: {"sphl": campaign})

    text = (
        "I will eat lunch now, then I want to swim later for 2 hours "
        "then I will go to my auntie's house to get bread, "
        "then i will study physics in preparation of SPHL"
    )
    parsed = review(text)

    titles = [str(x.get("title") or "").lower() for x in parsed["tasks"]]
    assert len(titles) == 2, parsed
    assert any("swim" in x for x in titles)
    assert any("bread" in x for x in titles)
    assert not any("physics" in x or "sphl" in x for x in titles), parsed

    ctx = parsed["context"]
    assert ctx["requested_project_campaign_ids"] == ["sphl"]
    assert ctx["requested_project_work_packages"]["sphl"] == ["mechanics-foundation"]
    project_ref = "pi-virtual:sphl:mechanics-foundation"
    assert project_ref in ctx["intent_today_ids"]

    bread = next(x for x in parsed["tasks"] if "bread" in str(x.get("title") or "").lower())
    bread_ref = ref(bread)
    assert ctx["plan_local_dependencies"][project_ref] == [bread_ref]
    assert ctx["intent_exact_order"][-1] == "Olympiad mechanics — force models and constrained motion"
    assert any("No generic study task was created" in note for note in parsed["notes"])


def test_sphl_reference_reuses_existing_materialized_project_task(monkeypatch):
    from app import project_reference

    campaign = {
        "id": "sphl",
        "status": "active",
        "goal": "Win SPhL 2027",
        "request": "Prepare me for SPhL 2027.",
        "source_documents": [{"source": "https://sgphysicsleague.org", "title": "SPhL"}],
        "requirements": [],
        "rules": [],
        "technical_requirements": [],
        "ticktick_project_id": "physics-project",
        "work_packages": [{
            "key": "mechanics",
            "title": "Mechanics diagnostic and olympiad bridge",
            "status": "pending",
            "dependencies": [],
            "review_delay_days": None,
            "ticktick_task_id": "sphl-mechanics-task",
            "estimated_minutes": 180,
            "remaining_minutes": 180,
        }],
    }
    monkeypatch.setattr(project_reference, "load_store", lambda: {"sphl": campaign})
    rows = [{
        "id": "sphl-mechanics-task",
        "project_id": "physics-project",
        "title": "Mechanics diagnostic and olympiad bridge",
        "status": 0,
        "kind": "TEXT",
        "tags": ["project-intelligence", "flexible"],
        "priority": 1,
        "meta": {"duration_minutes": 180},
    }]

    db.init_db()
    token = LOCAL_REVIEW.set(True)
    try:
        parsed = asyncio.run(intake_contract.review_intake(
            "I'm eating lunch now, then swim for 30 minutes, then study physics for SPHL",
            rows, CFG, NOW,
        ))
    finally:
        LOCAL_REVIEW.reset(token)

    assert not any(x.get("action") == "create" and "physics" in str(x.get("title") or "").lower()
                   for x in parsed.get("tasks") or [])
    ctx = parsed["context"]
    assert "sphl-mechanics-task" in ctx["intent_today_ids"]
    assert ctx["requested_project_work_packages"]["sphl"] == ["mechanics"]


def test_standalone_tonight_sphl_reference_does_not_create_generic_study_task(monkeypatch):
    from app import project_reference

    campaign = {
        "id": "sphl",
        "status": "active",
        "goal": "Win SPhL 2027",
        "request": "Prepare me for SPhL 2027.",
        "source_documents": [{"source": "https://sgphysicsleague.org", "title": "Singapore Physics League"}],
        "requirements": [],
        "rules": [],
        "technical_requirements": [],
        "ticktick_project_id": "physics-project",
        "work_packages": [{
            "key": "bridge",
            "title": "Mechanics olympiad bridge",
            "status": "pending",
            "dependencies": [],
            "review_delay_days": None,
            "ticktick_task_id": None,
            "estimated_minutes": 240,
            "remaining_minutes": 240,
        }],
    }
    monkeypatch.setattr(project_reference, "load_store", lambda: {"sphl": campaign})
    parsed = review("I want to study physics for SPHL tonight")

    assert not any(x.get("action") == "create" for x in parsed.get("tasks") or []), parsed
    ctx = parsed["context"]
    assert ctx["requested_project_campaign_ids"] == ["sphl"]
    assert ctx["requested_project_work_packages"]["sphl"] == ["bridge"]
    assert "pi-virtual:sphl:bridge" in ctx["intent_today_ids"]


def test_explicit_create_task_for_sphl_stays_user_authoritative(monkeypatch):
    from app import project_reference

    campaign = {
        "id": "sphl", "status": "active", "goal": "Win SPhL 2027",
        "request": "Prepare me for SPhL 2027.",
        "source_documents": [{"source": "https://sgphysicsleague.org", "title": "SPhL"}],
        "requirements": [], "rules": [], "technical_requirements": [],
        "ticktick_project_id": "physics-project", "work_packages": [],
    }
    monkeypatch.setattr(project_reference, "load_store", lambda: {"sphl": campaign})
    parsed = review("Create a task: Study physics for SPHL tonight")
    assert any(x.get("action") == "create" for x in parsed.get("tasks") or []), parsed
    assert "requested_project_campaign_ids" not in (parsed.get("context") or {})


def test_fresh_same_day_replan_clears_stale_project_reference_intent():
    from app.language_intake import merge_context

    current = {
        "date": "2026-10-07",
        "source": "quick-dump",
        "replan_requested": True,
        "requested_project_campaign_ids": ["sphl"],
        "requested_project_work_packages": {"sphl": ["mechanics"]},
        "intent_today_ids": ["pi-virtual:sphl:mechanics"],
        "intent_date_goals": {"pi-virtual:sphl:mechanics": "2026-10-07"},
    }
    incoming = {
        "date": "2026-10-07",
        "source": "quick-dump",
        "intake_version": 1,
        "replan_requested": True,
        "intent_today_ids": [],
        "intent_date_goals": {},
    }
    merged = merge_context(current, incoming)
    assert merged["requested_project_campaign_ids"] == []
    assert merged["requested_project_work_packages"] == {}
    assert merged["intent_today_ids"] == []
    assert merged["intent_date_goals"] == {}
