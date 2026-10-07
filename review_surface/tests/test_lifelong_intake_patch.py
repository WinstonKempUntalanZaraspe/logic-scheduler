import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import db
from app import intake_contract
from app import language_intake as intake
from app import lifelong_intake_patch as patch
from app import scheduler
from app.models import BusyBlock
import app.final_entrypoint  # installs the production stack


TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 14, 0, tzinfo=TZ)
PROMPT = (
    "For tomorrow, I'm gonna wake up at 8. I want to go swimming after breakfast, "
    "then i will do math and physics, then go to church, i think that is already "
    "scheduled tomorrow from 5-6pm, then maybe go gym in the night"
)
CFG = {
    "wake_time": "07:00",
    "day_start": "07:00",
    "sleep_start": "23:00",
    "day_end": "23:00",
    "post_meal_buffer_minutes": 30,
}


def row(task_id, title, *, start=None, end=None, tags=None, kind="TASK", meta=None):
    return {
        "id": task_id,
        "title": title,
        "project_id": "p",
        "status": 0,
        "start": start,
        "end": end,
        "tags": list(tags or []),
        "kind": kind,
        "priority": 0,
        "meta": dict(meta or {}),
    }


def rows():
    return [
        row("swim", "Swimming", tags=["flexible"], meta={"duration_minutes": 90}),
        row("math1", "Infinite limits and limits at infinity · 1/2", tags=["deep-work", "flexible"], meta={"duration_minutes": 60}),
        row("math2", "Infinite limits and limits at infinity · 2/2", tags=["deep-work", "flexible"], meta={"duration_minutes": 60}),
        row("physics", "PHYSICS - Simple Harmonic motion", tags=["deep-work", "flexible"], meta={"duration_minutes": 75}),
        row("church", "Church", start="2026-10-04T17:00:00+08:00", end="2026-10-04T18:00:00+08:00", tags=["fixed"]),
        row("gym", "Gym", tags=["flexible"], meta={"duration_minutes": 75}),
        row("note", "Physics notes", kind="NOTE"),
    ]


def test_timeframe_first_future_narrative_is_replan_not_fake_task():
    role, payload = intake.classify(PROMPT, now=NOW)
    assert role == "replan"
    assert payload == PROMPT


def test_exact_prompt_keeps_one_off_wake_when_optional_semantic_interpretation_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    db.init_db()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSCHEDULER_INTAKE_MODEL", "configured-model")

    calls = []
    async def unavailable(*args, **kwargs):
        calls.append(kwargs['json']['model'])
        raise httpx.ConnectError('Simulated unavailable semantic provider')

    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)
    parsed = asyncio.run(intake_contract.review_intake(PROMPT, rows(), CFG, NOW))
    ctx = parsed["context"]
    assert calls == ['configured-model']

    assert parsed["interpreter_mode"] == "deterministic"
    assert not parsed.get("clarifications")
    assert not any(x.get("action") == "create" for x in parsed.get("tasks", []))
    assert not any("HTTP 400" in str(x) or "HTTP 429" in str(x) for x in parsed.get("warnings", []))
    assert ctx["replan_scope"] == "tomorrow"
    assert ctx["tomorrow_plan"]["date"] == "2026-10-04"
    assert ctx["tomorrow_plan"]["wake_time"] == "08:00"
    assert ctx["after_meal_task_ids"]["swim"] == "breakfast"
    assert ctx["optional_date_goal_ids"] == ["gym"]
    assert ctx["tomorrow_plan"]["church_id"] == "church"
    assert "note" not in ctx.get("intent_date_goals", {})
    assert any("saved normal wake time is unchanged" in n for n in parsed.get("notes", []))


@pytest.mark.parametrize(
    "text",
    [
        "Tomorrow I'm waking at 8, then swimming after breakfast, then math, then church, then maybe gym",
        "For tmr: get up at 8; I want to swim after breakfast, then maths and physics, then Mass, then maybe workout at night",
        "Tomorrow I will wake up at 8 and go swimming after breakfast, then do calculus and physics, then go to church, then perhaps gym",
    ],
)
def test_future_day_paraphrases_are_recognized_as_schedule_control(text):
    role, _ = intake.classify(text, now=NOW)
    assert role == "replan"


def test_tomorrow_wake_override_reaches_scheduler_without_mutating_saved_config():
    cfg = dict(CFG)
    cfg["_quick_context"] = {
        "date": "2026-10-03",
        "tomorrow_plan": {"date": "2026-10-04", "wake_time": "08:00"},
    }
    override = scheduler._override_for_day(datetime(2026, 10, 4, tzinfo=TZ).date(), cfg)
    assert override["wake_time"] == "08:00"
    assert cfg["wake_time"] == "07:00"
    assert cfg["_quick_context"]["date"] == "2026-10-03"


def test_spoken_action_vocabulary_generalizes_without_special_case_sentence():
    assert intake.classify("I'm gonna research my report tonight", now=NOW) == ("goal", "research my report tonight")
    assert intake.classify("I'll collect groceries", now=NOW) == ("task", "collect groceries")
    assert intake.classify("I am going to deploy the backend tonight", now=NOW) == ("goal", "deploy the backend tonight")
    assert intake.classify("I'm gonna meditate", now=NOW) == ("task", "meditate")


def test_school_calendar_is_fixed_and_gets_one_commute_around_whole_school_day():
    lecture = BusyBlock(
        datetime(2026, 10, 5, 9, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 10, 0, tzinfo=TZ),
        "Aerospace lecture",
        "google",
    )
    tutorial = BusyBlock(
        datetime(2026, 10, 5, 13, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 15, 0, tzinfo=TZ),
        "Math tutorial",
        "google",
    )
    dentist = BusyBlock(
        datetime(2026, 10, 5, 16, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 17, 0, tzinfo=TZ),
        "Dentist appointment",
        "google",
    )

    original = [lecture, tutorial, dentist]
    result = patch.augment_school_calendar_busy(original)
    commute = [x for x in result if x.source == "google-school-commute"]

    assert all(x in result for x in original), "Calendar commitments must remain untouched hard-busy blocks."
    assert len(commute) == 2, "Multiple lessons on one day are one campus outing, not repeated commutes."
    assert commute[0].start == datetime(2026, 10, 5, 7, 45, tzinfo=TZ)
    assert commute[0].end == datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
    assert commute[1].start == datetime(2026, 10, 5, 15, 0, tzinfo=TZ)
    assert commute[1].end == datetime(2026, 10, 5, 16, 15, tzinfo=TZ)


def test_non_school_classes_do_not_get_school_commute():
    yoga = BusyBlock(
        datetime(2026, 10, 5, 19, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 20, 0, tzinfo=TZ),
        "Yoga class",
        "google",
    )
    result = patch.augment_school_calendar_busy([yoga])
    assert result == [yoga]


def test_semantic_diagnostic_probe_is_local_and_contains_no_http_status():
    result = asyncio.run(patch._no_network_semantic_probe())
    assert result["code"] == "optional-semantic-unavailable"
    assert "HTTP" not in result["summary"]
