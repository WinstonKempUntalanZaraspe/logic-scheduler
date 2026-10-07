from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app import db, intake_contract, semantic_plan
from app.semantic_cost_gate import make_cost_aware_interpreter
from app.bare_day_replan_patch import is_bare_today_replan
import app.final_entrypoint  # installs production wrappers, including bare-day support
from app import quickdump_latency_patch as latency


TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 4, 22, 40, tzinfo=TZ)
CFG = {
    "wake_time": "07:00",
    "day_start": "07:00",
    "sleep_start": "23:00",
    "day_end": "23:00",
}
ROWS = [
    {
        "id": "math",
        "project_id": "p",
        "project": "Study",
        "title": "Math revision",
        "status": 0,
        "kind": "TASK",
        "tags": ["flexible"],
        "priority": 1,
        "start": None,
        "end": None,
        "meta": {"duration_minutes": 45, "remaining_minutes": 45},
    },
    {
        "id": "bible",
        "project_id": "p",
        "project": "Personal",
        "title": "Read Bible",
        "status": 0,
        "kind": "TASK",
        "tags": ["flexible"],
        "priority": 1,
        "start": None,
        "end": None,
        "meta": {"duration_minutes": 15, "remaining_minutes": 15},
    },
]


def test_local_review_is_absolute_no_provider_contract():
    calls = []

    async def provider(*_args, **_kwargs):
        calls.append("provider")
        return {"should": "never happen"}

    wrapped = make_cost_aware_interpreter(provider)

    async def scenario():
        token = semantic_plan.LOCAL_REVIEW.set(True)
        try:
            return await wrapped("Plan my day", ROWS, CFG, NOW)
        finally:
            semantic_plan.LOCAL_REVIEW.reset(token)

    result = asyncio.run(scenario())
    assert result is None
    assert calls == []
    assert semantic_plan.LAST_ATTEMPT.get() is None


def test_semantic_timeout_recovers_locally_instead_of_raising_500(monkeypatch):
    monkeypatch.setattr(latency, "semantic_timeout_seconds", lambda _config=None: 0.01)
    calls = []

    async def review(_text, _rows, _config, _now):
        local = bool(semantic_plan.LOCAL_REVIEW.get())
        calls.append(local)
        if not local:
            await asyncio.sleep(0.1)
        return {
            "context": {"replan_requested": True, "replan_scope": "today"},
            "tasks": [], "clarifications": [], "warnings": [], "notes": [],
        }

    parsed = asyncio.run(
        latency._review_with_hard_local_fallback(review, "ambiguous wording", ROWS, CFG, NOW)
    )
    assert calls == [False, True]
    assert parsed["preview_timeout_recovered"] is True
    assert parsed["context"]["replan_requested"] is True


def test_bare_plan_my_day_is_today_from_now_and_stays_local(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    db.init_db()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    semantic_calls = []

    async def should_not_call_semantic(*_args, **_kwargs):
        semantic_calls.append(True)
        raise AssertionError("bare current-day replans must remain deterministic")

    monkeypatch.setattr(semantic_plan, "interpret_plan", should_not_call_semantic)

    parsed = asyncio.run(intake_contract.review_intake("Plan my day", ROWS, CFG, NOW))
    ctx = parsed.get("context") or {}

    assert semantic_calls == []
    assert parsed.get("clarifications") == []
    assert not [x for x in parsed.get("tasks", []) if x.get("action") == "create"]
    assert ctx.get("replan_requested") is True
    assert ctx.get("replan_scope") == "today"
    assert str(ctx.get("replan_from") or "").startswith("2026-10-04T22:40")


def test_bare_day_command_does_not_steal_questions_or_clock_instructions():
    assert is_bare_today_replan("Plan my day") is True
    assert is_bare_today_replan("Please replan the rest of my day.") is True
    # Existing submission-clock grammar owns these richer commands.
    assert is_bare_today_replan("Schedule my day from now") is False
    assert is_bare_today_replan("Plan my day from the current local time") is False
    assert is_bare_today_replan("How do I plan my day?") is False
    assert is_bare_today_replan("What if I plan my day?") is False
