"""Exam-specific Project Intelligence regressions."""
import asyncio
from datetime import datetime

from app import project_intelligence_sources as sources
from app.project_intelligence_quality import lesson_issues, exam_knowledge_boundary
from test_learning_readiness import progressive_good, fake_model


def exam_ready():
    d = progressive_good("calculus")
    d.campaign_type = "exam"
    d.goal = "Prepare for a calculus exam"
    advanced = d.work_packages[-1]
    advanced.title = "Timed mixed mock and error-log correction"
    advanced.description = (
        "Complete a timed representative mixed set without notes. Mark it, classify each "
        "mistake in an error log as concept, algebra, reading or time-management, relearn "
        "the cause, then redo every missed question without notes."
    )
    advanced.exercise = "Complete the timed mixed set, mark it, and redo missed questions."
    advanced.self_check = "All missed questions are re-solved correctly without notes."
    return d


def materials(d):
    return [{"id": "python", "url": d.learning_resources[0].url, "status": "retrieved"}]


def boundary_ready():
    d = exam_ready()
    d.knowledge_assumption = "User explicitly knows the ordered syllabus through trigonometry; later topics are not assumed."
    d.knowledge_boundary = "trigonometry"
    d.assumed_mastered_topics = ["number", "algebra", "geometry", "trigonometry"]
    d.study_topics = ["differentiation", "integration", "probability", "statistics"]
    names = [
        ("Learn what a derivative measures", "foundation", "learning", ["differentiation"]),
        ("Guided differentiation questions", "guided_practice", "technical_practice", ["differentiation"]),
        ("Independent calculus problem set", "independent_build", "technical_practice", ["differentiation", "integration"]),
        ("Timed mixed mock and error-log correction", "advanced_validation", "testing", ["calculus", "probability", "statistics"]),
    ]
    for p, (title, stage, kind, concepts) in zip(d.work_packages, names):
        p.title = title
        p.learning_stage = stage
        p.work_kind = kind
        p.concepts = concepts
    d.work_packages[0].dependencies = []
    d.work_packages[1].dependencies = [d.work_packages[0].key]
    d.work_packages[2].dependencies = [d.work_packages[1].key]
    d.work_packages[3].dependencies = [d.work_packages[2].key]
    d.work_packages[3].description = (
        "Complete a timed cumulative mixed mock. Mark it, classify mistakes in an error log, "
        "relearn the cause, and redo every missed question without notes."
    )
    return d


def test_exam_knowledge_boundary_parser_handles_natural_phrasing():
    assert exam_knowledge_boundary("Prepare me for O-Level Math. I know up to trigonometry.") == "trigonometry"
    assert exam_knowledge_boundary("I've covered through trigonometry and I have 60 minutes a day.") == "trigonometry"
    assert exam_knowledge_boundary("I have studied everything until sequences and series.") == "sequences and series"
    assert exam_knowledge_boundary("I know trigonometry.") is None


def test_partial_exam_boundary_still_requires_progression_for_remaining_topics():
    d = boundary_ready()
    issues = lesson_issues(d, materials(d), "Prepare me for this exam. I know up to trigonometry.")
    assert not any("incomplete_progression" in x for x in issues)
    d.work_packages[1].learning_stage = "unspecified"
    issues = lesson_issues(d, materials(d), "Prepare me for this exam. I know up to trigonometry.")
    assert any("unstaged_productive_work" in x or "incomplete_progression" in x for x in issues)


def test_exam_boundary_requires_mastered_prefix_and_remaining_partition():
    d = boundary_ready()
    d.assumed_mastered_topics = ["trigonometry"]
    d.study_topics = []
    issues = lesson_issues(d, materials(d), "Prepare me for this exam. I know up to trigonometry.")
    assert any("missing_remaining_exam_topics" in x for x in issues)


def test_exam_does_not_reteach_topic_inside_known_prefix():
    d = boundary_ready()
    d.work_packages[0].title = "Learn trigonometric identities"
    d.work_packages[0].concepts = ["trigonometry"]
    issues = lesson_issues(d, materials(d), "Prepare me for this exam. I know up to trigonometry.")
    assert any("relearns_mastered_topic" in x for x in issues)


def test_explicit_weak_known_topic_can_override_boundary_skip():
    d = boundary_ready()
    d.work_packages[0].title = "Repair trigonometry identities"
    d.work_packages[0].concepts = ["trigonometry"]
    issues = lesson_issues(
        d, materials(d),
        "Prepare me for this exam. I know up to trigonometry, but I am rusty at trigonometry."
    )
    assert not any("relearns_mastered_topic" in x for x in issues)


def test_exam_zero_knowledge_uses_full_learning_progression():
    d = exam_ready()
    issues = lesson_issues(d, materials(d), "Prepare me for this exam from zero knowledge")
    assert not any("incomplete_progression" in x for x in issues)
    assert not any("unstaged_productive_work" in x for x in issues)


def test_exam_without_cumulative_or_timed_practice_is_rejected():
    d = progressive_good("calculus")
    d.campaign_type = "exam"
    issues = lesson_issues(d, materials(d), "Prepare me for this exam")
    assert any("missing_exam_simulation" in x for x in issues)


def test_exam_without_error_correction_loop_is_rejected():
    d = exam_ready()
    d.work_packages[-1].title = "Timed mixed mock"
    d.work_packages[-1].description = "Complete a timed mixed mock and record the score."
    d.work_packages[-1].exercise = "Complete a timed mixed mock."
    d.work_packages[-1].self_check = "Record the score."
    issues = lesson_issues(d, materials(d), "Prepare me for this exam")
    assert any("missing_exam_error_loop" in x for x in issues)


def test_exam_presentation_work_is_rejected():
    d = exam_ready()
    d.work_packages[-1].work_kind = "presentation"
    issues = lesson_issues(d, materials(d), "Prepare me for this exam")
    assert any("exam_presentation_misclassified" in x for x in issues)


def test_math_exam_has_generic_verified_resource_fallback_candidates():
    fallback = sources._math_exam_fallback_resources(
        "Prepare me for O-Level Mathematics from zero knowledge",
        [{"title": "Mathematics syllabus", "text": "algebra geometry statistics"}],
    )
    assert {r.id for r in fallback} >= {
        "fallback_math_numbers", "fallback_math_algebra",
        "fallback_math_geometry", "fallback_math_data",
    }
    assert all(r.url.startswith("https://www.mathsisfun.com/") for r in fallback)


def test_non_math_exam_does_not_get_math_resource_fallback():
    assert sources._math_exam_fallback_resources(
        "Prepare me for my history exam",
        [{"title": "History syllabus", "text": "source-based case study and essay"}],
    ) == []


def test_physics_competition_has_verified_cross_domain_teaching_fallbacks():
    fallback = sources._physics_fallback_resources(
        "Prepare me for SPhL 2027 Senior Competitive. I already know O-Level Physics.",
        [{"title": "IPhO syllabus", "text": "Mechanics electromagnetic fields oscillations waves relativity quantum thermodynamics"}],
    )
    ids = {r.id for r in fallback}
    assert {
        "fallback_physics_mechanics", "fallback_physics_waves", "fallback_physics_thermo",
        "fallback_physics_electric", "fallback_physics_magnetism", "fallback_physics_optics",
        "fallback_physics_relativity", "fallback_physics_quantum",
    } <= ids
    assert all("openstax.org/books/university-physics" in r.url for r in fallback
               if not r.id.startswith("fallback_physics_math_"))


def test_exam_source_crawler_follows_syllabus_and_past_paper_links(monkeypatch):
    visited = []

    async def download(url):
        visited.append(url)
        if url.endswith("/course"):
            return (
                url,
                "text/html",
                b'<title>Exam</title><nav><a href="/syllabus">Syllabus</a>'
                b'<a href="/past-paper">Past paper</a></nav><p>Prepare for the examination.</p>',
            )
        return url, "text/html", b"<title>Resource</title><p>Assessment topics and representative questions.</p>"

    monkeypatch.setattr(sources, "download", download)
    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://example.com/course"]))
    assert warnings == []
    assert visited == [
        "https://example.com/course",
        "https://example.com/syllabus",
        "https://example.com/past-paper",
    ]
    assert len(docs) == 3





def test_competition_crawler_reaches_bounded_official_past_problem_from_rules_archive(monkeypatch):
    visited = []

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/":
            return (
                url, "text/html",
                b'<title>Competition</title><main><p>Official competition requirements and '
                b'submission rules are published here.</p><a href="/rules">Rules</a></main>',
            )
        if url == "https://contest.example/rules":
            return (
                url, "text/html",
                b'<title>Official Rules</title><main><p>The syllabus and rules apply.</p>'
                b'<a href="/archives/">View past years problems here</a></main>',
            )
        if url == "https://contest.example/archives/":
            return (
                url, "text/html",
                b'<title>Archives</title><main><p>Past competition assessment archive.</p>'
                b'<a href="/archives/2026/">Time travel to Competition 2026</a>'
                b'<a href="/archives/2025/">Time travel to Competition 2025</a></main>',
            )
        if url == "https://contest.example/archives/2026/":
            return (
                url, "text/html",
                b'<title>Competition 2026</title><main><p>2026 assessment archive.</p>'
                b'<a href="/archives/2026/problems">Problems</a>'
                b'<a href="/archives/2026/solutions">Solutions</a>'
                b'<a href="/archives/2026/statistics">Leaderboard statistics</a></main>',
            )
        if url in {
            "https://contest.example/archives/2026/problems",
            "https://contest.example/archives/2026/solutions",
        }:
            return (
                url, "text/html",
                b'<title>Competition material</title><main><p>Representative assessment '
                b'problems, solutions, mechanics, electromagnetism and formula reasoning.</p></main>',
            )
        raise AssertionError(f"Unexpected archive crawl: {url}")

    monkeypatch.setattr(sources, "download", download)
    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://contest.example/"]))

    assert warnings == []
    assert visited == [
        "https://contest.example/",
        "https://contest.example/rules",
        "https://contest.example/archives/",
        "https://contest.example/archives/2026/",
        "https://contest.example/archives/2026/problems",
        "https://contest.example/archives/2026/solutions",
    ]
    assert "https://contest.example/archives/2025/" not in visited
    assert "https://contest.example/archives/2026/statistics" not in visited
    assert [d["source"] for d in docs] == visited
    assert docs[-1]["source_role"] == "supporting"



def test_competition_crawler_follows_archive_linked_directly_from_homepage(monkeypatch):
    visited = []

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/":
            return (
                url, "text/html",
                b'<title>Competition</title><main><p>Official competition requirements and rules.</p>'
                b'<a href="/archives/">Archives</a></main>',
            )
        if url == "https://contest.example/archives/":
            return (
                url, "text/html",
                b'<title>Archives</title><main><p>Past competition archive.</p>'
                b'<a href="/archives/2026/">Competition 2026</a>'
                b'<a href="/archives/2025/">Competition 2025</a></main>',
            )
        if url == "https://contest.example/archives/2026/":
            return (
                url, "text/html",
                b'<title>Competition 2026</title><main><p>Assessment archive for 2026.</p>'
                b'<a href="/archives/2026/problems">Problems</a>'
                b'<a href="/archives/2026/solutions">Solutions</a></main>',
            )
        if url in {
            "https://contest.example/archives/2026/problems",
            "https://contest.example/archives/2026/solutions",
        }:
            return (
                url, "text/html",
                b'<title>Competition material</title><main><p>Representative assessment '
                b'problems and solutions for competition preparation.</p></main>',
            )
        raise AssertionError(f"Unexpected archive crawl: {url}")

    monkeypatch.setattr(sources, "download", download)
    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://contest.example/"]))

    assert warnings == []
    assert visited == [
        "https://contest.example/",
        "https://contest.example/archives/",
        "https://contest.example/archives/2026/",
        "https://contest.example/archives/2026/problems",
        "https://contest.example/archives/2026/solutions",
    ]
    assert "https://contest.example/archives/2025/" not in visited
    # A navigation-only year index is discoverable but not substantive evidence.
    assert [d["source"] for d in docs] == [u for u in visited if u != "https://contest.example/archives/"]



def test_competition_crawler_probes_js_only_archive_shell(monkeypatch):
    visited = []

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 7, tzinfo=tz)

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/":
            return (
                url, "text/html",
                b'<title>Competition</title><main><p>Official competition rules and requirements.</p>'
                b'<a href="/archives/">Past problems</a></main>',
            )
        if url == "https://contest.example/archives/":
            return url, "text/html", b'<title>Archives</title><div id="app"></div>'
        if url == "https://contest.example/archives/2026/":
            return url, "text/html", b'<title>Competition 2026</title><div id="app"></div>'
        if url == "https://contest.example/archives/2026/problems.pdf":
            return url, "application/pdf", b"%PDF-problems"
        if url == "https://contest.example/archives/2026/solutions.pdf":
            return url, "application/pdf", b"%PDF-solutions"
        raise AssertionError(f"Unexpected archive crawl: {url}")

    monkeypatch.setattr(sources, "datetime", FixedDateTime)
    monkeypatch.setattr(sources, "download", download)
    monkeypatch.setattr(
        sources,
        "pdf_text",
        lambda data: (
            "Representative mechanics and electromagnetism competition problems."
            if b"problems" in data
            else "Worked solutions for representative competition problems."
        ),
    )

    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://contest.example/"]))

    assert warnings == []
    assert visited == [
        "https://contest.example/",
        "https://contest.example/archives/",
        "https://contest.example/archives/2026/",
        "https://contest.example/archives/2026/problems.pdf",
        "https://contest.example/archives/2026/solutions.pdf",
    ]
    assert any(d["source"].endswith("problems.pdf") for d in docs)
    assert any(d["source"].endswith("solutions.pdf") for d in docs)



def test_competition_crawler_recovers_client_rendered_archive_controls(monkeypatch):
    visited = []

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/":
            return (
                url, "text/html",
                b'<title>Competition</title><main><p>Official competition requirements and rules.</p>'
                b'<a href="/archives/">Archives</a></main>',
            )
        if url == "https://contest.example/archives/":
            # Client-rendered controls: visible year text but no anchor for the year.
            return (
                url, "text/html",
                b'<title>Archives</title><main><p>Past competition archive.</p>'
                b'<div>SPhL 2026</div><div>Problems Solutions</div><div>SPhL 2025</div></main>',
            )
        if url == "https://contest.example/archives/2026/":
            # Same pattern again: visible document labels but no anchors.
            return (
                url, "text/html",
                b'<title>Competition 2026</title><main><p>2026 competition assessment archive.</p>'
                b'<button>Problems</button><button>Solutions</button></main>',
            )
        if url == "https://contest.example/archives/2026/problems.pdf":
            return (
                url, "application/pdf",
                b"%PDF-1.4 fake problem document",
            )
        if url == "https://contest.example/archives/2026/solutions.pdf":
            return (
                url, "application/pdf",
                b"%PDF-1.4 fake solution document",
            )
        raise AssertionError(f"Unexpected archive crawl: {url}")

    monkeypatch.setattr(sources, "download", download)
    monkeypatch.setattr(sources, "pdf_text", lambda data: (
        "Representative competition problems and solutions with mechanics, waves, "
        "electromagnetism and quantitative reasoning."
    ))

    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://contest.example/"]))

    assert warnings == []
    assert visited == [
        "https://contest.example/",
        "https://contest.example/archives/",
        "https://contest.example/archives/2026/",
        "https://contest.example/archives/2026/problems.pdf",
        "https://contest.example/archives/2026/solutions.pdf",
    ]
    assert "https://contest.example/archives/2025/" not in visited
    assert any(d["source"].endswith("/problems.pdf") for d in docs)
    assert any(d["source"].endswith("/solutions.pdf") for d in docs)



def test_competition_crawler_accepts_archive_as_root_url(monkeypatch):
    visited = []

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 7, tzinfo=tz)

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/archives/":
            return url, "text/html", b'<title>Archives</title><div id="app"></div>'
        if url == "https://contest.example/archives/2026/":
            return (
                url, "text/html",
                b'<title>Competition 2026</title><main><p>2026 competition problems and solutions.</p></main>',
            )
        if url == "https://contest.example/archives/2026/problems.pdf":
            return url, "application/pdf", b"%PDF-problems"
        if url == "https://contest.example/archives/2026/solutions.pdf":
            return url, "application/pdf", b"%PDF-solutions"
        raise AssertionError(f"Unexpected archive crawl: {url}")

    monkeypatch.setattr(sources, "datetime", FixedDateTime)
    monkeypatch.setattr(sources, "download", download)
    monkeypatch.setattr(
        sources,
        "pdf_text",
        lambda data: "Representative competition problem text and worked solution." * 30,
    )

    docs, warnings = asyncio.run(
        sources.fetch_source_bundle(["https://contest.example/archives/"])
    )

    assert warnings == []
    assert visited == [
        "https://contest.example/archives/",
        "https://contest.example/archives/2026/",
        "https://contest.example/archives/2026/problems.pdf",
        "https://contest.example/archives/2026/solutions.pdf",
    ]
    assert any(d["source"].endswith("problems.pdf") for d in docs)
    assert any(d["source"].endswith("solutions.pdf") for d in docs)


def test_project_source_crawler_follows_one_hop_external_official_syllabus_but_not_generic_links(monkeypatch):
    visited = []

    async def download(url):
        visited.append(url)
        if url == "https://contest.example/":
            return (
                url,
                "text/html",
                b'<title>Competition</title>'
                b'<p>The examinable scope follows the official external syllabus.</p>'
                b'<a href="https://syllabus.example/official">Official syllabus</a>'
                b'<a href="https://social.example/community">Community chat</a>',
            )
        if url == "https://syllabus.example/official":
            return (
                url,
                "text/html",
                b'<title>Official syllabus</title><main><p>Mechanics, electromagnetism, '
                b'thermodynamics, waves and modern physics.</p>'
                b'<a href="https://third.example/deeper-syllabus">More syllabus</a></main>',
            )
        raise AssertionError(f"Unexpected external crawl: {url}")

    monkeypatch.setattr(sources, "download", download)
    docs, warnings = asyncio.run(sources.fetch_source_bundle(["https://contest.example/"]))

    assert warnings == []
    assert visited == [
        "https://contest.example/",
        "https://syllabus.example/official",
    ]
    assert [d["source"] for d in docs] == visited
    assert docs[1]["source_role"] == "supporting"

def test_repair_can_add_and_verify_a_new_learning_resource(monkeypatch):
    from app.project_intelligence_models import LearningResource

    first = exam_ready()
    repaired = exam_ready()
    repaired.learning_resources.append(LearningResource(
        id="calc",
        title="Calculus teaching resource",
        url="https://example.com/calculus-teaching",
        topics=["worked calculus questions"],
        reason="Ground a later exam lesson",
    ))
    repaired.work_packages[1].resource_ids = ["calc"]
    repaired.work_packages[1].description += (
        " Use the worked calculus questions section of the Calculus teaching resource."
    )

    calls = fake_model(monkeypatch, [first, repaired])
    result = asyncio.run(sources.reason_campaign(
        "Prepare me for this exam from zero knowledge", []
    ))
    assert len(calls) == 2
    assert result._generation["quality_check"] == "passed"
    verified = {item["id"] for item in result._generation["learning_sources"]}
    assert {"python", "calc"} <= verified


def test_chemistry_exam_has_generic_verified_resource_fallback_candidates():
    fallback = sources._chemistry_exam_fallback_resources(
        "Prepare me for Singapore A-Level H2 Chemistry from zero knowledge",
        [{"title": "Chemistry H2 syllabus", "text": "atomic structure stoichiometry bonding equilibrium organic chemistry"}],
    )
    ids = {r.id for r in fallback}
    assert {
        "fallback_chem_atoms",
        "fallback_chem_stoichiometry",
        "fallback_chem_structure",
        "fallback_chem_equilibrium",
        "fallback_chem_organic",
    } <= ids
    assert all(r.url.startswith("https://openstax.org/books/chemistry-2e/pages/") for r in fallback)
    assert all("never syllabus-rule evidence" in r.reason for r in fallback)


def test_non_chemistry_exam_does_not_get_chemistry_resource_fallback():
    assert sources._chemistry_exam_fallback_resources(
        "Prepare me for my history exam",
        [{"title": "History syllabus", "text": "source-based case study and essay"}],
    ) == []


def test_exam_fallback_pool_selects_subject_without_cross_contamination():
    math = sources._exam_fallback_resources(
        "Prepare me for O-Level Mathematics from zero knowledge",
        [{"title": "Mathematics syllabus", "text": "algebra geometry"}],
    )
    chem = sources._exam_fallback_resources(
        "Prepare me for H2 Chemistry from zero knowledge",
        [{"title": "Chemistry syllabus", "text": "atomic structure chemical bonding"}],
    )
    assert math and all(r.id.startswith("fallback_math_") for r in math)
    assert chem and all(r.id.startswith("fallback_chem_") for r in chem)


def test_missing_lesson_link_triggers_chemistry_fallback_even_with_two_reachable_resources(monkeypatch):
    import json
    from app.project_intelligence_models import LearningResource

    first = exam_ready()
    first.goal = "Prepare for H2 Chemistry"
    # Two model-proposed resources are reachable, but the first teaching lesson is
    # deliberately ungrounded. This must still activate the verified Chemistry pool.
    first.learning_resources.append(LearningResource(
        id="second",
        title="Second reachable teaching resource",
        url="https://example.com/second",
        topics=["chemistry"],
        reason="Second reachable resource",
    ))
    first.work_packages[0].resource_ids = []

    repaired = exam_ready()
    repaired.goal = "Prepare for H2 Chemistry"
    repaired.learning_resources.append(LearningResource(
        id="fallback_chem_atoms",
        title="OpenStax Chemistry 2e — Atoms, Molecules, and Ions",
        url="https://openstax.org/books/chemistry-2e/pages/2-introduction",
        topics=["atomic structure", "atoms", "ions"],
        reason="Verified backup teaching resource for atomic foundations.",
    ))
    repaired.work_packages[0].resource_ids = ["fallback_chem_atoms"]
    repaired.work_packages[0].description += (
        " Use the Atoms, Molecules, and Ions chapter section of OpenStax Chemistry 2e."
    )

    calls = fake_model(monkeypatch, [first, repaired])
    result = asyncio.run(sources.reason_campaign(
        "Prepare me for Singapore A-Level H2 Chemistry from zero knowledge", []
    ))
    assert result._generation["quality_check"] == "passed"
    assert len(calls) == 2
    repair_input = json.loads(calls[1]["input"])
    backups = repair_input.get("verified_backup_learning_documents") or []
    assert any(x["id"] == "fallback_chem_atoms" for x in backups)
    assert any(
        "verified_exam_resource_fallback" in issue
        for issue in repair_input.get("quality_feedback") or []
    )
