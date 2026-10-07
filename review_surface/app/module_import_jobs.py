from __future__ import annotations

"""Resumable-in-the-browser Module Library import jobs.

Large scanned PDFs can spend minutes in local OCR.  The upload request therefore only
spools validated PDFs to a private temporary directory and starts one profile-local
worker.  Client disconnects do not cancel it.  A server restart never replays an import:
the stale job is marked failed and the user can retry safely (document hashes prevent
duplicates).
"""

import asyncio
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import File, Form, HTTPException, UploadFile

from . import db
from .tenant import profile_namespace

_KEY = "module_import_job_v1"
_RUNNING: dict[str, asyncio.Task] = {}
_PAYLOADS: dict[str, dict] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _read() -> dict | None:
    raw = db.get_kv(_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _save(job: dict) -> None:
    db.set_kv(_KEY, json.dumps(job, ensure_ascii=False, default=str))


def _public(job: dict | None) -> dict | None:
    if not job:
        return None
    return {k: v for k, v in job.items() if k not in {"temp_dir"}}


def _process_batch(ns: str, job_id: str, payload: dict) -> None:
    from .module_library import import_pdf_bytes, list_modules

    job = _read() or {}
    if str(job.get("id")) != job_id:
        return
    imported, failures = [], list(payload.get("preflight_failures") or [])
    files = list(payload.get("files") or [])
    total = len(files)
    try:
        job.update(status="running", started_at=time.time(), total_files=total, processed_files=0)
        _save(job)
        for index, item in enumerate(files, start=1):
            name, path = str(item["name"]), Path(item["path"])
            job.update(current_file=name, processed_files=index - 1)
            _save(job)
            try:
                data = path.read_bytes()
                imported.append(import_pdf_bytes(payload["title"], payload.get("code", ""), name, data))
            except HTTPException as exc:
                failures.append({"filename": name, "error": str(exc.detail)})
            except Exception as exc:
                failures.append({"filename": name, "error": f"Could not index PDF ({type(exc).__name__})"})
            job.update(
                processed_files=index,
                imported_count=len(imported),
                failure_count=len(failures),
                current_file=None,
            )
            _save(job)

        modules = list_modules()
        module = next(
            (m for m in modules if any(x.get("module_id") == m.get("module_id") for x in imported)),
            None,
        )
        if not imported:
            job.update(
                status="failed",
                error="No PDFs were indexed.",
                failures=failures,
                documents=[],
                module=None,
            )
        else:
            job.update(
                status="completed",
                ok=True,
                module=module,
                documents=imported,
                failures=failures,
                imported_count=len(imported),
            )
            db.audit("module_library_imported", {
                "module_id": imported[0].get("module_id"),
                "title": payload["title"],
                "imported": len([x for x in imported if not x.get("duplicate")]),
                "duplicates": len([x for x in imported if x.get("duplicate")]),
                "failures": failures,
                "background_job": True,
            })
    finally:
        job["finished_at"] = time.time()
        _save(job)
        try:
            shutil.rmtree(payload.get("temp_dir") or "", ignore_errors=True)
        except Exception:
            pass
        _PAYLOADS.pop(ns, None)


def install_module_import_jobs(app) -> None:
    if getattr(app.state, "module_import_jobs_installed", False):
        return
    app.state.module_import_jobs_installed = True

    @app.post("/api/project-intelligence/modules/import-jobs", status_code=202)
    async def submit_module_import_job(
        title: str = Form(...),
        code: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ):
        from .module_library import MAX_MODULE_FILES, MAX_MODULE_FILE_BYTES, MAX_MODULE_TOTAL_BYTES

        title_clean = " ".join(str(title or "").split()).strip()
        if len(title_clean) < 2:
            raise HTTPException(422, "Enter the module/subject name first.")
        if not files:
            raise HTTPException(422, "Choose at least one module PDF.")
        if len(files) > MAX_MODULE_FILES:
            raise HTTPException(422, f"Import at most {MAX_MODULE_FILES} PDFs in one batch.")

        ns = profile_namespace()
        async with _LOCKS.setdefault(ns, asyncio.Lock()):
            prior = _read()
            worker = _RUNNING.get(ns)
            if prior and prior.get("status") in {"queued", "running"} and worker and not worker.done():
                raise HTTPException(409, "A Module Library import is already running.")

            temp_dir = Path(tempfile.mkdtemp(prefix="autoscheduler-module-"))
            prepared, failures, total = [], [], 0
            queued = False
            try:
                for index, upload in enumerate(files):
                    name = str(upload.filename or f"module-{index + 1}.pdf")
                    if "pdf" not in str(upload.content_type or "").lower() and not name.lower().endswith(".pdf"):
                        failures.append({"filename": name, "error": "PDF files only"})
                        continue
                    target = temp_dir / f"{index:03d}-{uuid.uuid4().hex}.pdf"
                    size = 0
                    with target.open("wb") as handle:
                        while True:
                            chunk = await upload.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_MODULE_FILE_BYTES:
                                break
                            if total > MAX_MODULE_TOTAL_BYTES:
                                raise HTTPException(
                                    413,
                                    "The combined PDF upload is too large. Import the module in smaller batches.",
                                )
                            handle.write(chunk)
                    if size > MAX_MODULE_FILE_BYTES:
                        target.unlink(missing_ok=True)
                        failures.append({
                            "filename": name,
                            "error": f"larger than {MAX_MODULE_FILE_BYTES // 1_000_000} MB",
                        })
                        continue
                    prepared.append({"name": name, "path": str(target), "size": size})

                if not prepared:
                    raise HTTPException(422, {"message": "No valid PDFs were accepted.", "failures": failures})

                job_id = uuid.uuid4().hex
                payload = {
                    "title": title_clean,
                    "code": " ".join(str(code or "").split()).strip(),
                    "files": prepared,
                    "preflight_failures": failures,
                    "temp_dir": str(temp_dir),
                }
                job = {
                    "id": job_id,
                    "status": "queued",
                    "created_at": time.time(),
                    "title": title_clean,
                    "code": payload["code"],
                    "total_files": len(prepared),
                    "processed_files": 0,
                    "imported_count": 0,
                    "failure_count": len(failures),
                    "current_file": None,
                }
                _PAYLOADS[ns] = payload
                _save(job)

                async def runner():
                    try:
                        await asyncio.to_thread(_process_batch, ns, job_id, payload)
                    except Exception as exc:
                        current = _read() or job
                        if str(current.get("id")) == job_id:
                            current.update(
                                status="failed",
                                error=f"Module import failed ({type(exc).__name__}). Retry the PDFs; file hashes prevent duplicates.",
                                finished_at=time.time(),
                            )
                            _save(current)
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        _PAYLOADS.pop(ns, None)
                    finally:
                        current_task = _RUNNING.get(ns)
                        if current_task is asyncio.current_task():
                            _RUNNING.pop(ns, None)

                _RUNNING[ns] = asyncio.create_task(runner())
                queued = True
                return _public(job)
            except Exception:
                if not queued:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                raise

    @app.get("/api/project-intelligence/modules/import-jobs/latest")
    async def latest_module_import_job():
        ns = profile_namespace()
        job = _read()
        if job and job.get("status") in {"queued", "running"}:
            worker = _RUNNING.get(ns)
            if worker is None or worker.done():
                job.update(
                    status="failed",
                    error=(
                        "The server restarted before scanned-PDF indexing finished. "
                        "Nothing is replayed automatically; upload the unfinished PDFs again. "
                        "Already-indexed duplicates will be ignored by hash."
                    ),
                    finished_at=time.time(),
                )
                _save(job)
        return _public(job)


__all__ = ["install_module_import_jobs"]
