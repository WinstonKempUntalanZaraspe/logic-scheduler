import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import db, intake_contract
import app.final_entrypoint  # installs the production intake stack

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 6, 16, 20, tzinfo=TZ)
TOMORROW = "2026-10-07"
CFG = {
    "wake_time": "07:00",
    "day_start": "07:00",
    "sleep_start": "23:00",
    "day_end": "23:00",
    "post_meal_buffer_minutes": 45,
}


def row(task_id, title, *, start=None, end=None, tags=None, meta=None):
    return {
        "id": task_id,
        "title": title,
        "project_id": "p",
        "status": 0,
        "start": start,
        "end": end,
        "tags": list(tags or []),
        "kind": "TASK",
        "priority": 0,
        "meta": dict(meta or {}),
    }


ROWS = [
    row("math", "Math", tags=["deep-work", "flexible"], meta={"duration_minutes": 60}),
    row("physics", "Physics", tags=["deep-work", "flexible"], meta={"duration_minutes": 75}),
    row("swim", "Swimming", tags=["flexible"], meta={"duration_minutes": 120}),
    row("gym", "Gym", tags=["flexible"], meta={"duration_minutes": 75}),
    row(
        "church",
        "Church",
        start="2026-10-07T17:00:00+08:00",
        end="2026-10-07T18:00:00+08:00",
        tags=["fixed"],
    ),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    db.init_db()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AUTOSCHEDULER_INTAKE_MODEL", raising=False)
    monkeypatch.delenv("AUTOSCHEDULER_INTAKE_FALLBACK_MODEL", raising=False)


def review(text):
    return asyncio.run(intake_contract.review_intake(text, ROWS, CFG, NOW))


@pytest.mark.parametrize(
    "text",
    [
        "I was thinking about swimming later, but I haven't decided.",
        "I was going to swim tonight but maybe not anymore.",
        "Maybe I should study later.",
        "Perhaps gym tonight, not sure yet.",
        "I might do physics tomorrow if I have energy.",
        "If I finish early I could swim, otherwise forget it.",
        "Can I fit swimming later before the pool closes?",
        "Would swimming tonight even fit?",
        "Do I have enough time to study math tonight?",
        "I need to know whether I can go gym later.",
        "My friend said we should swim later.",
        "Dad wants me to study physics tonight.",
        "The coach told me to practice later.",
        "For example, study math tonight then swim.",
        "Suppose I swim at 7 and study afterward.",
        "What if I swim after dinner?",
        "I'm considering doing math after church tomorrow.",
        "I don't want the scheduler to create a swimming task.",
        "Don't add a task for swimming; just use the existing one if it fits.",
        "Swimming is something I may do later.",
        "Math is on my mind but I'm not committing to it yet.",
        "I probably won't go gym tonight.",
        "I can't promise I'll study physics later.",
        "I'm too tired to know if I'll do math.",
        "I just got back and I'm deciding what to do.",
        "I'm heading home now, probably 20 minutes.",
        "I need to be home by 8:30.",
        "I have to leave by 6.",
        "I need at least 45 minutes after dinner before swimming.",
        "After church we might eat outside.",
        "I'm about to sleep.",
        "I'm taking a shower now.",
        "I just finished shopping.",
        "I haven't done math yet.",
        "Physics isn't done yet.",
        "Math is already done.",
        "I finished Physics earlier.",
        "No gym today.",
        "Don't do math today.",
        "I don't need Physics anymore.",
        "I changed my mind about swimming.",
    ],
)
def test_non_authoritative_natural_language_never_creates_tasks(text):
    parsed = review(text)
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", [])), parsed


@pytest.mark.parametrize(
    "text",
    [
        "Plan my day tomorrow.",
        "Plan my schedule tomorrow.",
        "Plan tomorrow: Math, then Physics after church.",
        "Tomorrow I want Math before church and Physics after church.",
        "For tomorrow, do Math and maybe Gym at night.",
        "Plan my day tomorrow. Swim after breakfast, then Math.",
        "Tomorrow only: Math and Physics. Nothing from today.",
    ],
)
def test_tomorrow_requests_do_not_leak_into_today(text):
    parsed = review(text)
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", [])), parsed
    ctx = parsed.get("context") or {}
    assert not ctx.get("explicit_today_scope", False), parsed
    assert int(parsed.get("minimum_horizon_days") or 1) >= 2, parsed
    for task_id, day in (ctx.get("intent_date_goals") or {}).items():
        if task_id in {"math", "physics", "swim", "gym"}:
            assert day == TOMORROW, parsed
    assert "church" not in set(ctx.get("intent_today_ids") or []), parsed


@pytest.mark.parametrize(
    "text",
    [
        "Plan today. Tomorrow I have church at 5.",
        "Do Math today; Physics is for tomorrow.",
        "Today just Math. Gym can wait until tomorrow.",
        "Plan the rest of today, and remember church is tomorrow.",
    ],
)
def test_mixed_day_language_keeps_tomorrow_items_out_of_today(text):
    parsed = review(text)
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", [])), parsed
    ctx = parsed.get("context") or {}
    today_ids = set(ctx.get("intent_today_ids") or [])
    assert "church" not in today_ids, parsed
    goals = ctx.get("intent_date_goals") or {}
    if "physics" in goals:
        assert goals["physics"] == TOMORROW, parsed
    if "gym" in goals:
        assert goals["gym"] == TOMORROW, parsed


def test_correction_moves_existing_math_to_tomorrow_without_duplicate_creation():
    parsed = review("Actually, not Math today. Do Math tomorrow instead.")
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", [])), parsed
    ctx = parsed.get("context") or {}
    assert (ctx.get("intent_date_goals") or {}).get("math") == TOMORROW, parsed
    assert "math" not in set(ctx.get("intent_today_ids") or []), parsed


def test_optional_existing_activity_is_optional_not_new_work():
    parsed = review("Plan today. Maybe Gym tonight if time allows.")
    assert not any(change.get("action") == "create" for change in parsed.get("tasks", [])), parsed
    ctx = parsed.get("context") or {}
    assert "gym" in set(ctx.get("optional_date_goal_ids") or ctx.get("optional_today_ids") or []), parsed


def test_explicit_creation_still_works_when_user_really_asks_for_it():
    parsed = review("Add task: Buy toothpaste tomorrow.")
    creates = [x for x in parsed.get("tasks", []) if x.get("action") == "create"]
    assert len(creates) == 1, parsed
    assert "toothpaste" in str(creates[0].get("title") or "").lower(), parsed
