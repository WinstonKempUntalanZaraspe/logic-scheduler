import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import db, intake_contract
import app.final_entrypoint  # production wrapper order

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 6, 18, 10, tzinfo=TZ)
TODAY = "2026-10-06"
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


def creates(parsed):
    return [x for x in parsed.get("tasks", []) if x.get("action") == "create"]


NON_AUTH_TEMPLATES = [
    "I was thinking about {activity} later.",
    "I was going to {activity} tonight but maybe not.",
    "I'm not sure I can {activity} later.",
    "I don't think I can {activity} tonight.",
    "I doubt I'll {activity} tonight.",
    "Maybe I should {activity} later.",
    "Perhaps I could {activity} later.",
    "What if I {activity} tomorrow?",
    "Can I {activity} later?",
    "Would {activity} tonight even fit?",
    "Do I have enough time to {activity} tonight?",
    "I need to know whether I can {activity} later.",
    "My coach told me to {activity} later.",
    "Dad wants me to {activity} tonight.",
    "My friend said I should {activity} later.",
    "For example, I could {activity} tonight.",
    "Suppose I {activity} at 8.",
    "I probably won't {activity} tonight.",
    "I can't promise I'll {activity} later.",
    "I changed my mind about {activity}.",
]
ACTIVITIES = ["study Math", "do Physics", "go swimming", "go gym"]


@pytest.mark.parametrize(
    "text",
    [template.format(activity=activity) for template in NON_AUTH_TEMPLATES for activity in ACTIVITIES],
)
def test_non_authoritative_paraphrase_matrix_never_creates(text):
    parsed = review(text)
    assert not creates(parsed), (text, parsed)


@pytest.mark.parametrize(
    "text",
    [
        "Plan my day tomorrow.",
        "Plan my schedule tomorrow.",
        "Plan the day tomorrow.",
        "Plan the rest of my day tomorrow.",
        "Please plan my day for tomorrow.",
        "Replan tomorrow.",
        "Schedule tomorrow.",
        "Organise my day tomorrow.",
        "Organize my schedule for tmr.",
    ],
)
def test_bare_tomorrow_headers_are_scope_only_not_task_references(text):
    parsed = review(text)
    assert not creates(parsed), parsed
    blob = " ".join(
        [str(x.get("reason") or "") for x in parsed.get("clarifications", [])]
        + [str(x) for x in parsed.get("warnings", [])]
    ).lower()
    assert "task reference(s): day" not in blob, parsed
    assert "task reference(s): schedule" not in blob, parsed
    ctx = parsed.get("context") or {}
    assert int(parsed.get("minimum_horizon_days") or ctx.get("minimum_horizon_days") or 1) >= 2, parsed
    assert ctx.get("replan_scope") not in {"today"}, parsed


@pytest.mark.parametrize(
    "text",
    [
        "Do Math tomorrow.",
        "Math is for tomorrow.",
        "Study Math tomorrow instead.",
        "Tomorrow: Math.",
        "For tomorrow, Math.",
        "Move Math to tomorrow.",
    ],
)
def test_existing_math_tomorrow_never_duplicates_or_leaks_today(text):
    parsed = review(text)
    assert not creates(parsed), parsed
    ctx = parsed.get("context") or {}
    assert "math" not in set(ctx.get("intent_today_ids") or []), parsed
    goals = ctx.get("intent_date_goals") or {}
    if "math" in goals:
        assert goals["math"] == TOMORROW, parsed
    assert int(parsed.get("minimum_horizon_days") or ctx.get("minimum_horizon_days") or 1) >= 2, parsed


@pytest.mark.parametrize(
    "text",
    [
        "Do Math today. Physics tomorrow.",
        "Math today; Physics is for tomorrow.",
        "Today: Math. Tomorrow: Physics.",
        "Plan today with Math. Keep Physics for tomorrow.",
        "Plan the rest of today: Math. Physics can wait until tomorrow.",
    ],
)
def test_two_day_mixed_scope_keeps_each_existing_task_on_its_day(text):
    parsed = review(text)
    assert not creates(parsed), parsed
    ctx = parsed.get("context") or {}
    today_ids = set(ctx.get("intent_today_ids") or [])
    goals = ctx.get("intent_date_goals") or {}
    assert "physics" not in today_ids, parsed
    if "math" in goals:
        assert goals["math"] == TODAY, parsed
    if "physics" in goals:
        assert goals["physics"] == TOMORROW, parsed
    assert int(parsed.get("minimum_horizon_days") or ctx.get("minimum_horizon_days") or 1) >= 2, parsed


@pytest.mark.parametrize(
    "text",
    [
        "Plan today. Tomorrow I have Church at 5.",
        "Plan the rest of today, and remember Church is tomorrow.",
        "Today just Math; Church is tomorrow.",
        "Plan today around Math. Church tomorrow stays fixed.",
    ],
)
def test_tomorrow_fixed_commitment_never_becomes_today_goal(text):
    parsed = review(text)
    assert not creates(parsed), parsed
    ctx = parsed.get("context") or {}
    assert "church" not in set(ctx.get("intent_today_ids") or []), parsed


@pytest.mark.parametrize(
    "text",
    [
        "No Gym today.",
        "Don't do Gym today.",
        "Skip Gym today.",
        "Gym can wait until tomorrow.",
        "Actually, not Gym today.",
    ],
)
def test_negative_today_gym_never_creates_or_forces_today(text):
    parsed = review(text)
    assert not creates(parsed), parsed
    ctx = parsed.get("context") or {}
    assert "gym" not in set(ctx.get("intent_today_ids") or []), parsed


@pytest.mark.parametrize(
    "text",
    [
        "I'm eating dinner now.",
        "I just got home.",
        "I'm heading home now, about 20 minutes.",
        "I'm about to sleep.",
        "I'm taking a shower now.",
        "I need to be home by 8:30.",
        "I have to leave by 7.",
        "I haven't done Math yet.",
        "Physics isn't done yet.",
        "Math is already done.",
    ],
)
def test_state_progress_and_boundaries_never_create(text):
    parsed = review(text)
    assert not creates(parsed), parsed


@pytest.mark.parametrize(
    "text",
    [
        "Add task: Buy toothpaste tomorrow.",
        "Create task: Print passport photo.",
        "Remind me to buy shampoo tomorrow.",
    ],
)
def test_explicit_creation_authority_still_survives_safety_guards(text):
    parsed = review(text)
    assert len(creates(parsed)) == 1, parsed


@pytest.mark.parametrize(
    "text",
    [
        "Buy toothpaste tomorrow.",
        "I want to buy toothpaste tomorrow.",
        "I need to print my passport photo tomorrow.",
    ],
)
def test_positive_novel_work_is_not_overblocked(text):
    parsed = review(text)
    assert creates(parsed), parsed
