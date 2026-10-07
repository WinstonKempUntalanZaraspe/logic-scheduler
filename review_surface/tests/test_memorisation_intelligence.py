from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.project_intelligence_models import CampaignDraft, build_campaign, schedulable_work_packages
from app.project_intelligence_quality import lesson_issues
from app.project_intelligence_runtime import scheduler_meta


TZ = ZoneInfo("Asia/Singapore")
REQUEST = "Prepare me for my aircraft anatomy exam. I need to memorise and identify the parts of the plane and their functions."


def _draft():
    return CampaignDraft.model_validate({
        "campaign_type": "exam",
        "scope_options": [],
        "selected_scope": None,
        "scope_selection_basis": "not_applicable",
        "goal": "Recall and identify aircraft parts and functions for the exam",
        "deadline": None,
        "presentation_date": None,
        "knowledge_assumption": "No prior aircraft-anatomy recall is assumed.",
        "knowledge_boundary": None,
        "assumed_mastered_topics": [],
        "study_topics": ["aircraft parts", "control surfaces", "component functions"],
        "summary": "Learn the structures, retrieve their names/functions, and identify them visually.",
        "requirements": [],
        "deliverables": [],
        "rules": [],
        "technical_requirements": [],
        "datasets": [],
        "rubric": [],
        "milestones": [],
        "learning_resources": [{
            "id": "aircraft-notes",
            "title": "Aircraft anatomy notes",
            "url": "user-owned://aircraft-notes",
            "topics": ["aircraft parts", "control surfaces"],
            "reason": "User-owned study material.",
        }],
        "work_packages": [
            {
                "key": "learn-parts",
                "title": "Learn and retrieve major aircraft structures",
                "description": "Study the labelled reference once, then close it and retrieve each structure and function from memory.",
                "estimated_minutes": 35,
                "dependencies": [],
                "phase": "foundation learning",
                "learning_stage": "foundation",
                "work_kind": "learning",
                "learning_mode": "memorisation",
                "retrieval_of": [],
                "review_delay_days": None,
                "priority": "high",
                "risk": "medium",
                "definition_of_done": "Name every major structure and state its function without notes.",
                "rubric_links": [],
                "concepts": ["fuselage", "wing", "empennage", "landing gear", "control surfaces"],
                "worked_example": "Use one labelled aircraft image to connect the wing to lift and the vertical stabilizer to directional stability.",
                "exercise": "Close the notes and write every major structure and its function from memory before revealing the answers.",
                "self_check": "Score the list; record each missed structure/function and require all items correct on the retry.",
                "resource_ids": ["aircraft-notes"],
                "confidence": "RECOMMENDED",
            },
            {
                "key": "blank-diagram-review",
                "title": "Blank-diagram aircraft identification",
                "description": "Use an unlabelled aircraft diagram. Identify and label structures from memory, then correct only the misses.",
                "estimated_minutes": 20,
                "dependencies": ["learn-parts"],
                "phase": "retrieval practice",
                "learning_stage": "guided_practice",
                "work_kind": "technical_practice",
                "learning_mode": "visual_recall",
                "retrieval_of": ["learn-parts"],
                "review_delay_days": 1,
                "priority": "high",
                "risk": "medium",
                "definition_of_done": "Label the blank diagram without notes and correct every missed location.",
                "rubric_links": [],
                "concepts": ["aircraft structure identification"],
                "worked_example": "",
                "exercise": "Label a blank aircraft diagram without notes, then reveal the reference and mark errors.",
                "self_check": "Record number correct out of total and list every missed label.",
                "resource_ids": [],
                "confidence": "RECOMMENDED",
            },
            {
                "key": "spaced-function-recall",
                "title": "Spaced recall of aircraft-part functions",
                "description": "Without notes, retrieve each component name, location and function; focus the retry on prior misses.",
                "estimated_minutes": 20,
                "dependencies": ["blank-diagram-review"],
                "phase": "retrieval practice",
                "learning_stage": "independent_build",
                "work_kind": "technical_practice",
                "learning_mode": "mixed",
                "retrieval_of": ["blank-diagram-review"],
                "review_delay_days": 3,
                "priority": "high",
                "risk": "medium",
                "definition_of_done": "Recall component name, location and function without notes.",
                "rubric_links": [],
                "concepts": ["component functions", "spatial identification"],
                "worked_example": "",
                "exercise": "Self-quiz from memory before checking the answer key.",
                "self_check": "Reach at least 90% correct and list missed items for another retry.",
                "resource_ids": [],
                "confidence": "RECOMMENDED",
            },
            {
                "key": "timed-mixed-mock",
                "title": "Timed mixed aircraft identification mock",
                "description": "Complete a timed closed-book mixed set. Keep an error log for wrong labels/functions, correct the cause, then redo every miss.",
                "estimated_minutes": 30,
                "dependencies": ["spaced-function-recall"],
                "phase": "exam validation",
                "learning_stage": "advanced_validation",
                "work_kind": "testing",
                "learning_mode": "mixed",
                "retrieval_of": [],
                "review_delay_days": None,
                "priority": "high",
                "risk": "medium",
                "definition_of_done": "Finish the timed mixed set, classify errors and redo all missed items correctly without notes.",
                "rubric_links": [],
                "concepts": ["aircraft parts", "component functions"],
                "worked_example": "",
                "exercise": "Run the timed closed-book mock and redo misses.",
                "self_check": "Record score/accuracy and all missed items.",
                "resource_ids": [],
                "confidence": "RECOMMENDED",
            },
        ],
        "major_risks": [],
    })


def _materials():
    return [{
        "id": "aircraft-notes",
        "url": "user-owned://aircraft-notes",
        "status": "user_owned",
        "material_role": "instruction",
    }]


def test_memory_heavy_exam_has_topic_level_retrieval_modes_and_spacing():
    d = _draft()
    issues = lesson_issues(d, _materials(), REQUEST)
    forbidden = (
        "missing_memorisation_mode",
        "missing_active_retrieval",
        "missing_spaced_retrieval",
        "missing_visual_recall",
        "passive_spaced_review",
        "unmeasurable_recall",
        "invalid_retrieval_reference",
    )
    assert not any(issue.startswith(forbidden) for issue in issues), issues


def test_memory_heavy_exam_rejects_generic_conceptual_study_plan():
    d = _draft()
    for package in d.work_packages:
        package.learning_mode = "conceptual"
        package.retrieval_of = []
        package.review_delay_days = None
    issues = lesson_issues(d, _materials(), REQUEST)
    assert any(issue.startswith("missing_memorisation_mode") for issue in issues)
    assert any(issue.startswith("missing_spaced_retrieval") for issue in issues)
    assert any(issue.startswith("missing_visual_recall") for issue in issues)


def test_spaced_review_waits_for_real_completion_and_delay():
    now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
    campaign = build_campaign(_draft(), REQUEST, [])
    by = {p["key"]: p for p in campaign["work_packages"]}

    selected = schedulable_work_packages(campaign, now=now)
    assert [p["key"] for p in selected] == ["learn-parts"]

    by["learn-parts"].update(
        status="done",
        progress_percent=100,
        remaining_minutes=0,
        completed_at=(now - timedelta(days=1)).isoformat(),
    )
    selected = schedulable_work_packages(campaign, now=now)
    assert [p["key"] for p in selected] == ["blank-diagram-review"]
    assert "spaced-function-recall" not in {p["key"] for p in selected}

    meta = scheduler_meta(campaign, by["blank-diagram-review"])
    assert meta["energy"] == "medium"
    assert meta["max_chunk"] == 45
    assert meta["timing"] == "asap"
    assert meta["earliest"].startswith("2026-10-07T00:00:00")


def test_later_spaced_review_waits_for_previous_review_completion():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)
    campaign = build_campaign(_draft(), REQUEST, [])
    by = {p["key"]: p for p in campaign["work_packages"]}
    by["learn-parts"].update(status="done", progress_percent=100, remaining_minutes=0,
                             completed_at="2026-10-01T12:00:00+08:00")
    by["blank-diagram-review"].update(status="done", progress_percent=100, remaining_minutes=0,
                                      completed_at="2026-10-07T12:00:00+08:00")
    selected = schedulable_work_packages(campaign, now=now)
    # The scheduler may look ahead to the dependent mock in the same day, but
    # must retain its prerequisite; selecting work is not completion credit.
    assert [p["key"] for p in selected] == ["spaced-function-recall", "timed-mixed-mock"]
    assert by["timed-mixed-mock"]["dependencies"] == ["spaced-function-recall"]
    assert by["spaced-function-recall"]["status"] == "pending"
    meta = scheduler_meta(campaign, by["spaced-function-recall"])
    assert meta["energy"] == "medium"
    assert meta["max_chunk"] == 45
    assert meta["timing"] == "asap"
