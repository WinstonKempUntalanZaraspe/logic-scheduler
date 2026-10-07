from pathlib import Path
import sys
import types

from app import db
from app import module_library as ml
from app.project_intelligence_runtime import task_content


def _isolated_db(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "module-library.db")
    db.init_db()
    ml.ensure_tables()


def _page(doc_index: int, page_no: int) -> str:
    special = " flutterresonance flutterresonance flutterresonance" if (doc_index, page_no) == (8, 150) else ""
    return (
        f"Lecture {doc_index:02d}.{page_no:03d} Aircraft Topic {page_no}\n"
        f"This is indexed teaching content for document {doc_index}, page {page_no}. "
        f"It explains concept_{doc_index}_{page_no}, definitions, examples and exam applications.{special}\n\n"
        f"Worked example {page_no}: apply the principle to a representative engineering situation."
    )


def _build_huge_module(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    module_id = ml._upsert_module("Massive Aircraft Systems", "MAS")
    for doc_index in range(1, 13):
        pages = [_page(doc_index, page) for page in range(1, 201)]
        row = ml.index_document_pages(
            module_id,
            f"Lecture-Pack-{doc_index:02d}.pdf",
            pages,
            sha256=(f"{doc_index:064x}")[-64:],
        )
        assert row["page_count"] == 200
        assert row["duplicate"] is False
    return module_id


def test_module_library_indexes_12_by_200_pages_without_context_dump(monkeypatch, tmp_path):
    module_id = _build_huge_module(monkeypatch, tmp_path)
    rows = ml.list_modules()
    assert len(rows) == 1
    module = rows[0]
    assert module["module_id"] == module_id
    assert module["document_count"] == 12
    assert module["page_count"] == 2400
    assert module["section_count"] >= 240

    detail = ml.get_module(module_id, include_sections=True)
    assert len(detail["documents"]) == 12
    assert len(detail["sections"]) >= 240

    request = f"Prepare me for Massive Aircraft Systems using Module Library module:{module_id}"
    context = ml.module_context_for_request(request, max_chars=100_000)
    assert context
    assert context[0]["source"].endswith("/course-map")
    assert sum(len(x["text"]) for x in context) <= 100_000
    # The reasoning call receives a bounded retrieval view, not 2,400 pages of raw text.
    assert len(context) < 40


def test_full_text_search_reaches_late_pages_and_task_time_retrieval(monkeypatch, tmp_path):
    module_id = _build_huge_module(monkeypatch, tmp_path)
    hits = ml.search_module(module_id, "flutterresonance", top_k=5)
    assert hits
    assert hits[0]["filename"] == "Lecture-Pack-08.pdf"
    assert hits[0]["page_start"] <= 150 <= hits[0]["page_end"]
    assert "flutterresonance" in hits[0]["text"].lower()

    request = f"Prepare me for Massive Aircraft Systems using my Module Library module:{module_id}."
    campaign = {
        "id": "huge-module",
        "request": request,
        "goal": "Master Massive Aircraft Systems",
        "learning_resources": [],
        "work_packages": [],
    }
    package = {
        "key": "flutter",
        "title": "Understand flutterresonance",
        "description": "Learn the flutterresonance mechanism and recognise it in exam questions.",
        "concepts": ["flutterresonance"],
        "worked_example": "",
        "exercise": "Explain it from memory.",
        "self_check": "Correct explanation without notes.",
        "definition_of_done": "Explain accurately without notes.",
        "rubric_links": [],
        "resource_ids": [],
        "learning_mode": "mixed",
        "retrieval_of": [],
        "review_delay_days": None,
    }
    text = task_content(campaign, package)
    assert "Relevant indexed module passages" in text
    assert "Lecture-Pack-08.pdf" in text
    assert "flutterresonance" in text.lower()


def test_module_resources_preserve_exact_pdf_navigation_and_mastery(monkeypatch, tmp_path):
    module_id = _build_huge_module(monkeypatch, tmp_path)
    request = f"Prepare me using module:{module_id}"
    resources = ml.module_learning_resources_for_request(request)
    assert len(resources) == 12
    assert all(r.url.startswith("user-owned://module/") for r in resources)
    assert all(any("pp." in topic for topic in r.topics) for r in resources)

    detail = ml.get_module(module_id, include_sections=True)
    target = detail["sections"][0]
    updated = ml.update_section_progress(
        module_id,
        target["section_id"],
        status="mastered",
        mastery=95,
        notes="Closed-book recall passed.",
    )
    assert updated["status"] == "mastered"
    assert updated["mastery"] == 95
    for section in detail["sections"][1:4]:
        ml.update_section_progress(module_id, section["section_id"], status="mastered", mastery=90)

    refreshed = ml.get_module(module_id, include_sections=True)
    assert refreshed["coverage_percent"] > 0
    course_map = ml._course_map(refreshed)
    assert "status=mastered" in course_map
    assert "mastery=95%" in course_map


def test_duplicate_pdf_hash_is_not_indexed_twice(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    module_id = ml._upsert_module("Systems", "SYS")
    pages = ["Introduction\nAlpha beta gamma"] * 20
    first = ml.index_document_pages(module_id, "one.pdf", pages, sha256="a" * 64)
    second = ml.index_document_pages(module_id, "same-again.pdf", pages, sha256="a" * 64)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    detail = ml.get_module(module_id, include_sections=False)
    assert len(detail["documents"]) == 1


def test_module_selection_does_not_leak_into_unrelated_projects(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    module_id = ml._upsert_module("Aircraft Systems", "ASYS")
    ml.index_document_pages(module_id, "systems.pdf", ["Hydraulics\nPressure and pumps"] * 30, sha256="b" * 64)

    assert ml.module_context_for_request("Prepare me for a railway optimization hackathon") == []
    selected = ml.module_context_for_request(
        f"Prepare me for Aircraft Systems using module:{module_id}", max_chars=30_000
    )
    assert selected


def test_scanned_pdf_pages_use_ocr_and_flag_visual_content(monkeypatch):
    monkeypatch.setattr(ml, "_native_pdf_pages", lambda data: ["", "", ""])

    class FakePage:
        def __init__(self, number):
            self.number = number
        def get_images(self, full=True):
            return [(self.number,)]  # every page is image/diagram-heavy
        def get_textpage_ocr(self, language="eng", dpi=150, full=True):
            assert language == "eng"
            assert dpi == ml.OCR_DPI
            assert full is True
            return {"page": self.number}
        def get_text(self, mode, textpage=None):
            assert mode == "text"
            return (
                f"Scanned lecture page {self.number + 1}. "
                "Lift equals one half rho V squared S C L. "
                "Diagram labels alpha velocity pressure force."
            )

    class FakeDoc:
        def __init__(self):
            self.pages = [FakePage(i) for i in range(3)]
        def __iter__(self):
            return iter(self.pages)
        def __getitem__(self, index):
            return self.pages[index]

    monkeypatch.setitem(sys.modules, "fitz", types.SimpleNamespace(open=lambda **kwargs: FakeDoc()))
    pages, diagnostics = ml._extract_pdf_pages_with_diagnostics(b"fake-scanned-pdf")

    assert all("Scanned lecture page" in page for page in pages)
    assert diagnostics["extraction_mode"] == "ocr"
    assert diagnostics["ocr_page_count"] == 3
    assert diagnostics["visual_page_count"] == 3
    assert diagnostics["low_text_page_count"] == 0
    assert any("diagram/equation meaning" in warning for warning in diagnostics["warnings"])


def test_document_extraction_diagnostics_persist_with_module(monkeypatch, tmp_path):
    _isolated_db(monkeypatch, tmp_path)
    module_id = ml._upsert_module("Scanned Aircraft Systems", "SAS")
    row = ml.index_document_pages(
        module_id,
        "scan.pdf",
        ["Hydraulic system\nPump pressure accumulator valves"] * 8,
        sha256="c" * 64,
        extraction={
            "extraction_mode": "hybrid",
            "ocr_page_count": 4,
            "visual_page_count": 6,
            "low_text_page_count": 1,
            "warnings": ["1 page still contains little searchable text after OCR."],
        },
    )
    assert row["extraction"]["ocr_page_count"] == 4

    detail = ml.get_module(module_id, include_sections=False)
    doc = detail["documents"][0]
    assert doc["extraction"]["extraction_mode"] == "hybrid"
    assert doc["extraction"]["ocr_page_count"] == 4
    assert doc["extraction"]["visual_page_count"] == 6
    assert doc["extraction"]["low_text_page_count"] == 1
    assert "little searchable text" in doc["extraction"]["warnings"][0]


def test_single_scanned_visual_page_inside_text_pdf_is_still_ocrd(monkeypatch):
    native = [
        "Dense selectable lecture text about aerodynamics, equations, worked examples and definitions. " * 8,
        "",
        "More selectable lecture text about stability, control derivatives and flight dynamics. " * 8,
        "Further native text with enough content that this PDF is not globally scan-heavy. " * 8,
        "Final native lecture page with searchable definitions and examples. " * 8,
    ]
    monkeypatch.setattr(ml, "_native_pdf_pages", lambda data: list(native))

    class FakePage:
        def __init__(self, number):
            self.number = number
        def get_images(self, full=True):
            return [(1,)] if self.number == 1 else []
        def get_textpage_ocr(self, language="eng", dpi=150, full=True):
            assert self.number == 1
            return {"page": self.number}
        def get_text(self, mode, textpage=None):
            assert self.number == 1
            return "Scanned free-body diagram labels lift drag thrust weight angle alpha velocity."
    class FakeDoc:
        def __init__(self):
            self.pages = [FakePage(i) for i in range(len(native))]
        def __iter__(self):
            return iter(self.pages)
        def __getitem__(self, index):
            return self.pages[index]
        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "fitz", types.SimpleNamespace(open=lambda **kwargs: FakeDoc()))
    pages, diagnostics = ml._extract_pdf_pages_with_diagnostics(b"mixed-pdf")

    assert "Scanned free-body diagram" in pages[1]
    assert diagnostics["extraction_mode"] == "hybrid"
    assert diagnostics["ocr_page_count"] == 1
    assert diagnostics["visual_page_count"] == 1
    # OCR succeeded but this diagram-only page still has little searchable prose.
    assert diagnostics["low_text_page_count"] == 1
