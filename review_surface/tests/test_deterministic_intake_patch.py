from datetime import datetime
from zoneinfo import ZoneInfo

from app import deterministic_intake_patch as patch
from app import deterministic_intake_polish as polish

TZ = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 15, 0, tzinfo=TZ)


def row(tid, title, *, start=None, tags=None, priority=3):
    return {
        "id": tid,
        "title": title,
        "status": 0,
        "project_id": "p",
        "start": start,
        "tags": tags or [],
        "priority": priority,
        "meta": {},
        "kind": "TEXT",
    }


def rows():
    return [
        row("swim", "Swimming", start="2026-10-03T20:40:00+08:00", tags=["fixed"]),
        row("math1", "Infinite limits and limits at infinity · 1/2", start="2026-10-04T17:15:00+08:00", tags=["deep-work", "flexible"]),
        row("math2", "Infinite limits and limits at infinity · 2/2", start="2026-10-04T18:30:00+08:00", tags=["deep-work", "flexible"]),
        row("physics", "PHYSICS -Simple Harmonic motion", start="2026-10-03T21:00:00+08:00", tags=["deep-work", "flexible"]),
        row("church", "Church", start="2026-10-03T16:00:00+08:00", tags=["fixed"], priority=1),
    ]


def test_math_swim_physics_chain_resolves_without_model():
    result = {"tasks": [], "notes": [], "warnings": []}
    patch.deterministic_explicit_relations("Do Math after Swimming, then Physics", result, rows())
    updates = {str(x["task_id"]): x for x in result["tasks"]}
    assert updates["math1"]["meta_patch"]["dependencies"] == ["swim"]
    assert updates["math2"]["meta_patch"]["dependencies"] == ["swim"]
    assert set(updates["physics"]["meta_patch"]["dependencies"]) == {"math1", "math2"}
    assert not result["warnings"]


def test_after_i_get_back_is_context_not_fake_dependency():
    result = {"tasks": [], "notes": [], "warnings": []}
    patch.deterministic_explicit_relations("Swim after I get back if it realistically fits", result, rows())
    assert result["tasks"] == []
    assert result["warnings"] == []


def test_full_prompt_sets_return_gate_and_today_chain():
    text = (
        "Replan today. Church is fixed from 4pm-5pm. "
        "8:30pm is the absolute latest I'll be back home. "
        "Swim after I get back if it realistically fits. "
        "Do Math after Swimming, then Physics."
    )
    parsed = {"context": {"date": "2026-10-03", "source": "quick-dump"}, "notes": [], "warnings": [], "clarifications": []}
    out = patch.deterministic_attach_real_life_bounds(parsed, text, rows(), NOW)
    ctx = out["context"]
    assert ctx["return_home_not_after"].startswith("2026-10-03T20:30")
    assert ctx["after_return_task_ids"] == ["swim"]
    assert {"swim", "math1", "math2", "physics"} <= set(ctx["intent_today_ids"])
    assert all("could not match" not in str(w).lower() for w in out["warnings"])


def test_after_return_gate_reaches_planner(monkeypatch):
    captured = {}

    def fake(tasks, metas, busy, start, horizon_days, config, mastery_map=None):
        captured.update(metas)
        return [], [], {}

    monkeypatch.setattr(patch.contextual, "contextual_intent_aware_plan", fake)
    cfg = {
        "_quick_context": {
            "date": "2026-10-03",
            "after_return_task_ids": ["swim"],
            "return_home_not_after": "2026-10-03T20:30:00+08:00",
        }
    }
    patch.deterministic_contextual_plan([], {"swim": {}}, [], NOW, 1, cfg, {})
    assert captured["swim"]["earliest"].startswith("2026-10-03T20:40")
    assert captured["swim"]["must_finish"] is True


def test_semantic_outage_noise_is_removed_without_hiding_real_ambiguity():
    complete = {
        "warnings": [
            "Semantic interpretation unavailable or failed validation; using conservative local interpretation. Compatibility recovery also failed (HTTP 429).",
            "Semantic fallback diagnostic: OpenAI returned HTTP 429.",
        ],
        "clarifications": [],
        "blocking_conflicts": [],
    }
    assert not polish._has_unresolved(complete)
    polish._clean_semantic_noise(complete)
    assert complete["warnings"] == []

    unresolved = {
        "warnings": [
            "Semantic interpretation unavailable or failed validation (HTTP 400).",
            "I could not match both tasks confidently.",
        ],
        "clarifications": [{"text": "which task?", "reason": "Two existing tasks match."}],
        "blocking_conflicts": [],
    }
    assert polish._has_unresolved(unresolved)
    polish._clean_semantic_noise(unresolved)
    assert unresolved["warnings"] == ["I could not match both tasks confidently."]
    assert unresolved["clarifications"] == [{"text": "which task?", "reason": "Two existing tasks match."}]
