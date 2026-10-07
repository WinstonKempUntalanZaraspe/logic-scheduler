from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app import db, intake_contract
import app.final_entrypoint  # installs the production stack
from app import day_plan_activities as day_activities
from app import general_day_plan_patch as general
from app import language_intake as intake
from app import personal_intents as intents
from app import narrative_intake_guard as guard


TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 4, 15, 50, tzinfo=TZ)
PROMPT = (
    "I just finished lunch, now, I am on my way to church, from 5-6pm, "
    "then i will takeaway food home then eat dinner at home, then i will rest then go swimming, "
    "if have time when i come back, i will do math, or phsyics, or coding, then sleep"
)


def row(task_id, title, *, start=None, end=None, tags=None, meta=None):
    return {
        "id": task_id,
        "project_id": "p",
        "title": title,
        "status": 0,
        "start": start,
        "end": end,
        "tags": list(tags or []),
        "kind": "TASK",
        "project_kind": "TASK",
        "priority": 0,
        "meta": dict(meta or {}),
    }


def rows():
    return [
        row("church", "Church", start="2026-10-04T17:00:00+08:00", end="2026-10-04T18:00:00+08:00", tags=["fixed"], meta={"location": "Church"}),
        row("swim", "Swimming", tags=["flexible"], meta={"duration_minutes": 75, "location": "Nearest ActiveSG stadium"}),
        row("math", "Math", tags=["flexible"], meta={"duration_minutes": 45}),
        row("physics", "Physics", tags=["flexible"], meta={"duration_minutes": 45}),
        row("coding", "Coding", tags=["flexible"], meta={"duration_minutes": 45}),
    ]


def cfg():
    return {
        "wake_time": "07:00",
        "day_start": "07:00",
        "sleep_start": "23:00",
        "day_end": "23:00",
        "post_meal_buffer_minutes": 20,
        "post_meal_swim_buffer_minutes": 45,
        "personal_travel_minutes": 20,
        "default_travel_buffer_minutes": 20,
        "bedtime_wind_down_minutes": 20,
    }


def test_multi_context_story_is_not_one_task_action_or_one_reality_item():
    assert guard.is_multi_context_narrative(PROMPT)
    assert intents.task_command(PROMPT) is None
    assert intents.reality_kind(PROMPT) is None
    target = general._target_day(PROMPT, NOW)
    assert target and target[0] == NOW.date()
    role, _ = intake.classify(PROMPT, now=NOW)
    assert role == "replan"


def test_single_explicit_lifecycle_command_stays_a_task_action():
    text = "complete Physics"
    assert not guard.is_multi_context_narrative(text)
    command = intents.task_command(text)
    assert command and command["action"] == "complete" and command["target"] == "Physics"


def test_mature_temporary_care_chain_is_not_rerouted_as_day_narrative():
    text = "I'm going to bathe for 15m then nap for 45m then eat dinner for 30m"
    assert guard._pure_temporary_care_chain(text)
    assert not guard.is_multi_context_narrative(text)


def test_capacity_clause_is_separated_from_required_swimming():
    pieces = day_activities.fragments(PROMPT)
    assert any("go swimming" in p.lower() and "if have time" not in p.lower() for p in pieces)
    assert any("if have time" in p.lower() for p in pieces)


def test_exact_user_style_prompt_never_asks_for_task_id_or_creates_durable_work(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    db.init_db()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AUTOSCHEDULER_INTAKE_MODEL", raising=False)

    parsed = asyncio.run(intake_contract.review_intake(PROMPT, rows(), cfg(), NOW))
    reasons = "\n".join(
        [str(x.get("reason") or "") for x in parsed.get("clarifications", [])]
        + [str(x) for x in parsed.get("warnings", [])]
    ).lower()

    assert "name one existing task or its id" not in reasons
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", []))
    ctx = parsed.get("context") or {}
    assert ctx.get("replan_requested") is True
    assert (ctx.get("day_plan") or {}).get("date") == NOW.date().isoformat()
    assert (ctx.get("day_plan") or {}).get("church_start", "").startswith("2026-10-04T17:00")
    assert "swim" in (ctx.get("intent_date_goals") or {})

    groups = ctx.get("contingency_choice_groups") or ctx.get("optional_choice_groups") or []
    assert groups
    choice = groups[-1]
    assert choice.get("max_selected") == 1
    assert len(choice.get("task_ids") or []) == 3
    assert "swim" not in set(str(x) for x in choice.get("task_ids") or [])


def test_capacity_phrase_without_pronoun_is_optional():
    assert guard._CAPACITY_BARE_RE.search("if have time when i come back")
    assert guard._capacity_choice_clauses(
        "go swimming, if have time when i come back, i will do math or physics or coding, then sleep"
    ) == ["if have time when i come back, i will do math or physics or coding"]


def test_future_explicit_plan_keeps_complete_deterministic_fallback(monkeypatch, tmp_path):
    """The narrative guard must extend implicit-today stories, not degrade mature future plans."""
    now = datetime(2026, 10, 3, 14, 0, tzinfo=TZ)
    text = (
        "For tomorrow, I'm gonna wake up at 8. I want to go swimming after breakfast, "
        "then i will do math and physics, then go to church, i think that is already "
        "scheduled tomorrow from 5-6pm, then maybe go gym in the night"
    )
    source = [
        row("swim", "Swimming", tags=["flexible"], meta={"duration_minutes": 90}),
        row("math1", "Infinite limits and limits at infinity · 1/2", tags=["deep-work", "flexible"], meta={"duration_minutes": 60}),
        row("math2", "Infinite limits and limits at infinity · 2/2", tags=["deep-work", "flexible"], meta={"duration_minutes": 60}),
        row("physics", "PHYSICS - Simple Harmonic motion", tags=["deep-work", "flexible"], meta={"duration_minutes": 75}),
        row("church", "Church", start="2026-10-04T17:00:00+08:00", end="2026-10-04T18:00:00+08:00", tags=["fixed"]),
        row("gym", "Gym", tags=["flexible"], meta={"duration_minutes": 75}),
    ]
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "future.db")
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    db.init_db()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSCHEDULER_INTAKE_MODEL", "configured-model")

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("simulated unavailable semantic provider")

    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)
    parsed = asyncio.run(intake_contract.review_intake(text, source, cfg(), now))
    assert parsed.get("interpreter_mode") == "deterministic", parsed
    assert not parsed.get("clarifications"), parsed
