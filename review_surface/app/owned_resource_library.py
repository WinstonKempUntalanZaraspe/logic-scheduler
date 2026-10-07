from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime

import httpx
from fastapi import HTTPException

from . import db
from .config import settings
from .project_intelligence_models import LearningResource
from .semantic_credentials import semantic_api_key, semantic_model

OWNED_RESOURCE_LIBRARY_KEY = "project_intelligence_owned_resource_library_v1"
MAX_OWNED_FILE_BYTES = 8_000_000
MAX_OWNED_FILES = 16
MAX_TOC_ENTRIES = 180
MAX_TOC_ENTRY_CHARS = 220
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _load_raw() -> dict[str, dict]:
    try:
        value = json.loads(db.get_kv(OWNED_RESOURCE_LIBRARY_KEY, "{}") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_raw(value: dict[str, dict]) -> None:
    db.set_kv(
        OWNED_RESOURCE_LIBRARY_KEY,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _norm_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _resource_id(title: str, edition: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _norm_title(title)).strip("-")[:42] or "resource"
    digest = hashlib.sha256(f"{_norm_title(title)}|{str(edition or '').casefold().strip()}".encode()).hexdigest()[:10]
    return f"user_owned_{slug}_{digest}"


def clean_toc_entries(values) -> list[str]:
    """Normalize user-supplied/extracted TOC headings without inventing content."""
    output, seen = [], set()
    for raw in values or []:
        for line in str(raw or "").replace("\r", "\n").split("\n"):
            line = re.sub(r"\s+", " ", line).strip(" \t•·–—|")
            line = re.sub(r"\.{2,}\s*\d+\s*$", "", line).strip()
            if not line or len(line) < 2:
                continue
            if re.fullmatch(r"[ivxlcdm\d\s.\-]+", line, re.I):
                continue
            if line.casefold() in {"contents", "table of contents", "index"}:
                continue
            line = line[:MAX_TOC_ENTRY_CHARS]
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            output.append(line)
            if len(output) >= MAX_TOC_ENTRIES:
                return output
    return output


def _entries_from_pdf_text(text: str) -> list[str]:
    # TOCs commonly use dot leaders followed by a page number. Preserve the heading,
    # but never infer missing chapter/section labels or numbers.
    lines = []
    for raw in str(text or "").splitlines():
        value = re.sub(r"\s+", " ", raw).strip()
        if not value:
            continue
        value = re.sub(r"\s*\.{2,}\s*\d+\s*$", "", value).strip()
        value = re.sub(r"\s+\d+\s*$", "", value).strip() if len(value) > 12 else value
        lines.append(value)
    return clean_toc_entries(lines)


def list_owned_resources() -> list[dict]:
    rows = list(_load_raw().values())
    rows.sort(key=lambda x: (str(x.get("title") or "").casefold(), str(x.get("edition") or "").casefold()))
    return rows


def get_owned_resource(resource_id: str) -> dict | None:
    return _load_raw().get(str(resource_id))


def delete_owned_resource(resource_id: str) -> bool:
    store = _load_raw()
    removed = store.pop(str(resource_id), None)
    if removed is not None:
        _save_raw(store)
        return True
    return False


def save_owned_resource(*, title: str, edition: str = "", toc_entries=None, source_files=None, extraction: str = "user") -> dict:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    edition = re.sub(r"\s+", " ", str(edition or "")).strip()
    if len(title) < 3:
        raise HTTPException(422, "Enter the book/resource title before saving it.")
    entries = clean_toc_entries(toc_entries)
    if not entries:
        raise HTTPException(422, "No readable table-of-contents headings were found. Retake clearer photos or use a text PDF.")
    rid = _resource_id(title, edition)
    now = datetime.now(settings.tz).isoformat()
    store = _load_raw()
    previous = store.get(rid) or {}
    # Re-importing the same title + edition extends the saved TOC rather than
    # replacing it. This lets a long TOC be photographed/imported in batches.
    entries = clean_toc_entries([*(previous.get("toc_entries") or []), *entries])
    merged_files = list(dict.fromkeys([
        *[str(x)[:180] for x in (previous.get("source_files") or [])],
        *[str(x)[:180] for x in (source_files or [])],
    ]))
    row = {
        "id": rid,
        "title": title,
        "edition": edition,
        "toc_entries": entries,
        "source_files": merged_files[:MAX_OWNED_FILES],
        "extraction": extraction,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    store[rid] = row
    _save_raw(store)
    return row


def library_learning_resources() -> list[LearningResource]:
    resources = []
    for row in list_owned_resources():
        entries = clean_toc_entries(row.get("toc_entries") or [])
        edition = str(row.get("edition") or "").strip()
        reason = "Saved in the user's persistent Owned Resource Library."
        if edition:
            reason += f" User-supplied edition: {edition}."
        reason += " Table-of-contents headings were supplied by the user and may be used for exact section/chapter navigation; page numbers are not assumed."
        resources.append(LearningResource(
            id=str(row["id"]),
            title=str(row["title"]),
            url=f"user-owned://library/{row['id']}",
            topics=entries,
            reason=reason,
        ))
    return resources


def merge_with_explicit_resources(explicit_resources) -> list[LearningResource]:
    """Reuse saved books in every campaign; a newly stated copy of a title wins."""
    explicit = list(explicit_resources or [])
    explicit_titles = {_norm_title(x.title) for x in explicit}
    saved = [x for x in library_learning_resources() if _norm_title(x.title) not in explicit_titles]
    return [*explicit, *saved]


def _response_text(body: dict) -> str:
    return "".join(
        str(part.get("text") or "")
        for item in body.get("output", [])
        for part in item.get("content", [])
        if part.get("type") in {"output_text", "text"}
    ).strip()


async def _extract_image_toc(title: str, edition: str, images: list[tuple[str, bytes]]) -> list[str]:
    key, model = semantic_api_key(), semantic_model()
    if not key or not model:
        raise HTTPException(
            503,
            "Photo TOC import needs the configured OpenAI/Luna model once for text extraction. No book was saved.",
        )
    content = [{
        "type": "input_text",
        "text": (
            "These are photographs of table-of-contents pages from a user-owned book. "
            f"User-supplied title: {title}. User-supplied edition: {edition or 'not specified'}. "
            "Transcribe only visible chapter/section headings, in reading order. Preserve visible numbering. "
            "Do not invent missing headings, page numbers, chapters, topics, authors, or edition details. "
            "Omit headers/footers and page-number-only fragments."
        ),
    }]
    for mime, data in images:
        content.append({
            "type": "input_image",
            "image_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
            "detail": "high",
        })
    schema = {
        "type": "object",
        "properties": {
            "toc_entries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_TOC_ENTRIES,
            }
        },
        "required": ["toc_entries"],
        "additionalProperties": False,
    }
    request = {
        "model": model,
        "store": False,
        "instructions": "Perform faithful OCR/transcription of the supplied TOC images. Never infer text that is not visible.",
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_schema", "name": "owned_book_toc", "strict": True, "schema": schema}},
        "max_output_tokens": 8000,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10)) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": "Bearer " + key},
                json=request,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "TOC photo extraction timed out. Nothing was saved; try the same photos again.") from exc
    if response.status_code >= 400:
        raise HTTPException(502, "The TOC photos could not be read by the configured model. Nothing was saved.")
    body = response.json()
    if body.get("status") != "completed":
        raise HTTPException(502, "The TOC photo extraction did not complete. Nothing was saved.")
    try:
        parsed = json.loads(_response_text(body))
        return clean_toc_entries(parsed.get("toc_entries") or [])
    except Exception as exc:
        raise HTTPException(502, "The TOC photo extraction returned unreadable structured data. Nothing was saved.") from exc


async def import_owned_resource_files(*, title: str, edition: str = "", files: list[tuple[str, str, bytes]]) -> dict:
    """Import one book/resource once from TOC photos and/or text PDFs."""
    if not files:
        raise HTTPException(422, "Choose at least one TOC photo or PDF.")
    if len(files) > MAX_OWNED_FILES:
        raise HTTPException(413, f"Use at most {MAX_OWNED_FILES} TOC files for one resource.")

    images: list[tuple[str, bytes]] = []
    entries: list[str] = []
    names: list[str] = []
    for filename, content_type, data in files:
        names.append(str(filename or "upload"))
        if len(data) > MAX_OWNED_FILE_BYTES:
            raise HTTPException(413, f"{filename or 'A file'} is larger than 8 MB.")
        mime = str(content_type or "").split(";", 1)[0].strip().lower()
        lower_name = str(filename or "").lower()
        if mime == "application/pdf" or lower_name.endswith(".pdf"):
            from .project_intelligence_sources import pdf_text
            extracted = pdf_text(data)
            entries.extend(_entries_from_pdf_text(extracted))
        elif mime in _IMAGE_TYPES or lower_name.endswith((".jpg", ".jpeg", ".png", ".webp")):
            if mime not in _IMAGE_TYPES:
                mime = "image/jpeg" if lower_name.endswith((".jpg", ".jpeg")) else (
                    "image/png" if lower_name.endswith(".png") else "image/webp"
                )
            images.append((mime, data))
        else:
            raise HTTPException(415, "Owned Resource Library accepts JPG, PNG, WEBP, or PDF TOC files.")

    if images:
        entries.extend(await _extract_image_toc(title, edition, images))
    entries = clean_toc_entries(entries)
    extraction = "vision+pdf" if images and any(str(n).lower().endswith(".pdf") for n in names) else (
        "vision" if images else "pdf_text"
    )
    return save_owned_resource(
        title=title,
        edition=edition,
        toc_entries=entries,
        source_files=names,
        extraction=extraction,
    )
