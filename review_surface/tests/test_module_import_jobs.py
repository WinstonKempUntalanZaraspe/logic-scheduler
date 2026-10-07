from pathlib import Path

from app import db
from app import module_import_jobs as jobs
from app import module_library as ml


def _isolated(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(db, "USE_POSTGRES", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    db.init_db()
    ml.ensure_tables()
    jobs._RUNNING.clear()
    jobs._PAYLOADS.clear()


def test_background_import_worker_persists_progress_and_cleans_spool(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    pdf = spool / "scan.pdf"
    pdf.write_bytes(b"synthetic-pdf")

    monkeypatch.setattr(
        ml,
        "import_pdf_bytes",
        lambda title, code, filename, data: {
            "module_id": "mod-scan",
            "doc_id": "doc-scan",
            "filename": filename,
            "duplicate": False,
            "extraction": {
                "extraction_mode": "ocr",
                "ocr_page_count": 200,
                "visual_page_count": 200,
                "low_text_page_count": 0,
                "warnings": [],
            },
        },
    )
    monkeypatch.setattr(
        ml,
        "list_modules",
        lambda: [{"module_id": "mod-scan", "title": "Scanned Systems"}],
    )

    job = {
        "id": "job-1",
        "status": "queued",
        "title": "Scanned Systems",
        "total_files": 1,
        "processed_files": 0,
    }
    jobs._save(job)
    payload = {
        "title": "Scanned Systems",
        "code": "SCAN",
        "files": [{"name": "scan.pdf", "path": str(pdf), "size": len(pdf.read_bytes())}],
        "preflight_failures": [],
        "temp_dir": str(spool),
    }

    jobs._process_batch("test-profile", "job-1", payload)

    final = jobs._read()
    assert final["status"] == "completed"
    assert final["processed_files"] == 1
    assert final["imported_count"] == 1
    assert final["documents"][0]["extraction"]["ocr_page_count"] == 200
    assert final["module"]["module_id"] == "mod-scan"
    assert not spool.exists()


def test_background_import_failure_is_explicit_and_spool_is_cleaned(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    spool = tmp_path / "spool-fail"
    spool.mkdir()
    pdf = spool / "bad.pdf"
    pdf.write_bytes(b"bad")

    def fail(*args, **kwargs):
        raise RuntimeError("ocr failed")

    monkeypatch.setattr(ml, "import_pdf_bytes", fail)
    monkeypatch.setattr(ml, "list_modules", lambda: [])

    jobs._save({
        "id": "job-2",
        "status": "queued",
        "title": "Bad Scan",
        "total_files": 1,
        "processed_files": 0,
    })
    jobs._process_batch(
        "test-profile",
        "job-2",
        {
            "title": "Bad Scan",
            "code": "",
            "files": [{"name": "bad.pdf", "path": str(pdf), "size": 3}],
            "preflight_failures": [],
            "temp_dir": str(spool),
        },
    )

    final = jobs._read()
    assert final["status"] == "failed"
    assert final["error"] == "No PDFs were indexed."
    assert final["processed_files"] == 1
    assert final["failure_count"] == 1
    assert "Could not index PDF" in final["failures"][0]["error"]
    assert not spool.exists()
