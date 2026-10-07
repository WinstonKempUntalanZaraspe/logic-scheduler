from __future__ import annotations

import hashlib
import io
import json
import math
import re
from collections import Counter
from datetime import datetime

from fastapi import HTTPException
from pypdf import PdfReader

from . import db
from .config import settings
from .project_intelligence_models import LearningResource

MAX_MODULE_FILES = 20
MAX_MODULE_FILE_BYTES = 40_000_000
MAX_MODULE_TOTAL_BYTES = 300_000_000
MAX_PDF_PAGES = 1000
MAX_PDF_CHARS = 5_000_000
CHUNK_TARGET_CHARS = 6500
CHUNK_MAX_CHARS = 9000
CHUNK_OVERLAP_CHARS = 450
MAX_COURSE_MAP_SECTIONS = 300
OCR_PAGE_MIN_NATIVE_CHARS = 90
OCR_DPI = 150

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+.#_-]{1,}", re.I)
_MODULE_REF = re.compile(r"\bmodule:([a-z0-9][a-z0-9_-]{5,80})\b", re.I)
_STOP = {
    "the","and","for","with","from","that","this","into","onto","your","you","are","was","were",
    "have","has","had","will","would","should","could","can","about","using","use","used","than",
    "then","when","where","what","which","while","module","course","subject","lecture","notes","pdf",
    "page","pages","chapter","section","study","prepare","exam","test","revision","review","learn",
    "learning","topic","topics","part","parts","introduction","overview","contents","content",
}


def _now() -> str:
    return datetime.now(settings.tz).isoformat()


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _slug(value: str, limit: int = 42) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(value)).strip("-")[:limit] or "module"


def _module_id(title: str, code: str = "") -> str:
    digest = hashlib.sha256(f"{_norm(title)}|{_norm(code)}".encode()).hexdigest()[:10]
    return f"mod-{_slug(code or title, 36)}-{digest}"


def _doc_id(module_id: str, sha256: str) -> str:
    digest = hashlib.sha256(f"{module_id}|{sha256}".encode()).hexdigest()[:24]
    return f"doc-{digest}"


def _chunk_id(doc_id: str, index: int) -> str:
    # Truncating the human-readable document prefix can discard its hash, making
    # page chunks from different books collide. Hash the complete document identity.
    digest = hashlib.sha256(doc_id.encode()).hexdigest()[:24]
    return f"chk-{digest}-{index:04d}"


def _section_id(doc_id: str, heading: str, page_start: int) -> str:
    digest = hashlib.sha256(f"{doc_id}|{heading}|{page_start}".encode()).hexdigest()[:12]
    return f"sec-{_slug(doc_id, 26)}-{digest}"


def ensure_tables() -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS module_library (
            module_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            code TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS module_documents (
            doc_id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            page_count INTEGER NOT NULL,
            char_count INTEGER NOT NULL,
            section_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS module_chunks (
            chunk_id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            heading TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            terms TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS module_sections (
            section_id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            heading TEXT NOT NULL,
            page_start INTEGER NOT NULL,
            page_end INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unseen',
            mastery INTEGER,
            last_reviewed TEXT,
            notes TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS module_document_extraction (
            doc_id TEXT PRIMARY KEY,
            module_id TEXT NOT NULL,
            extraction_mode TEXT NOT NULL DEFAULT 'native',
            ocr_page_count INTEGER NOT NULL DEFAULT 0,
            visual_page_count INTEGER NOT NULL DEFAULT 0,
            low_text_page_count INTEGER NOT NULL DEFAULT 0,
            warnings TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_module_documents_module ON module_documents(module_id)",
        "CREATE INDEX IF NOT EXISTS idx_module_chunks_module ON module_chunks(module_id)",
        "CREATE INDEX IF NOT EXISTS idx_module_chunks_doc ON module_chunks(doc_id)",
        "CREATE INDEX IF NOT EXISTS idx_module_sections_module ON module_sections(module_id)",
        "CREATE INDEX IF NOT EXISTS idx_module_sections_doc ON module_sections(doc_id)",
        "CREATE INDEX IF NOT EXISTS idx_module_extraction_module ON module_document_extraction(module_id)",
    ]
    with db.conn() as c:
        for statement in statements:
            c.execute(statement)


def _scope_clause(column: str) -> tuple[str, tuple]:
    prefix = db._prefix()
    if prefix:
        return f"{column} LIKE ?", (prefix + "%",)
    return f"{column} NOT LIKE 'u:%'", ()


def _module_row(row) -> dict:
    out = dict(row)
    out["module_id"] = db._unscope(out.get("module_id"))
    try:
        out["metadata"] = json.loads(out.get("metadata") or "{}")
    except Exception:
        out["metadata"] = {}
    return out


def _extraction_row(row) -> dict:
    out = dict(row)
    out["doc_id"] = db._unscope(out.get("doc_id"))
    out["module_id"] = db._unscope(out.get("module_id"))
    try:
        out["warnings"] = json.loads(out.get("warnings") or "[]")
    except Exception:
        out["warnings"] = []
    return out


def _doc_row(row, extraction: dict | None = None) -> dict:
    out = dict(row)
    out["doc_id"] = db._unscope(out.get("doc_id"))
    out["module_id"] = db._unscope(out.get("module_id"))
    if extraction:
        out["extraction"] = extraction
    else:
        out["extraction"] = {
            "extraction_mode": "native",
            "ocr_page_count": 0,
            "visual_page_count": 0,
            "low_text_page_count": 0,
            "warnings": [],
        }
    return out


def _section_row(row) -> dict:
    out = dict(row)
    out["section_id"] = db._unscope(out.get("section_id"))
    out["doc_id"] = db._unscope(out.get("doc_id"))
    out["module_id"] = db._unscope(out.get("module_id"))
    return out


def list_modules() -> list[dict]:
    ensure_tables()
    clause, params = _scope_clause("module_id")
    with db.conn() as c:
        rows = db._execute(
            c,
            f"""SELECT m.*,
                (SELECT COUNT(*) FROM module_documents d WHERE d.module_id=m.module_id) AS document_count,
                (SELECT COALESCE(SUM(page_count),0) FROM module_documents d WHERE d.module_id=m.module_id) AS page_count,
                (SELECT COUNT(*) FROM module_sections s WHERE s.module_id=m.module_id) AS section_count,
                (SELECT COUNT(*) FROM module_sections s WHERE s.module_id=m.module_id AND s.status='mastered') AS mastered_sections
                FROM module_library m WHERE {clause} ORDER BY updated_at DESC""",
            params,
        ).fetchall()
    output = []
    for row in rows:
        item = _module_row(row)
        total = int(item.get("section_count") or 0)
        mastered = int(item.get("mastered_sections") or 0)
        item["coverage_percent"] = round(100 * mastered / total) if total else 0
        output.append(item)
    return output


def get_module(module_id: str, *, include_sections: bool = True) -> dict | None:
    ensure_tables()
    physical = db._scoped_id(module_id)
    with db.conn() as c:
        row = db._execute(c, "SELECT * FROM module_library WHERE module_id=?", (physical,)).fetchone()
        if not row:
            return None
        docs = db._execute(
            c, "SELECT * FROM module_documents WHERE module_id=? ORDER BY created_at, filename", (physical,)
        ).fetchall()
        extraction_rows = db._execute(
            c, "SELECT * FROM module_document_extraction WHERE module_id=?", (physical,)
        ).fetchall()
        extraction_by_doc = {
            str(x["doc_id"]): _extraction_row(x) for x in extraction_rows
        }
        sections = []
        if include_sections:
            sections = db._execute(
                c,
                """SELECT * FROM module_sections WHERE module_id=?
                   ORDER BY doc_id,page_start,page_end,heading""",
                (physical,),
            ).fetchall()
    result = _module_row(row)
    result["documents"] = [
        _doc_row(x, extraction_by_doc.get(str(x["doc_id"]))) for x in docs
    ]
    result["sections"] = [_section_row(x) for x in sections]
    total = len(result["sections"])
    mastered = sum(1 for x in result["sections"] if x.get("status") == "mastered")
    result["coverage_percent"] = round(100 * mastered / total) if total else 0
    return result


def delete_module(module_id: str) -> bool:
    ensure_tables()
    physical = db._scoped_id(module_id)
    with db.conn() as c:
        exists = db._execute(c, "SELECT 1 FROM module_library WHERE module_id=?", (physical,)).fetchone()
        if not exists:
            return False
        db._execute(c, "DELETE FROM module_chunks WHERE module_id=?", (physical,))
        db._execute(c, "DELETE FROM module_sections WHERE module_id=?", (physical,))
        db._execute(c, "DELETE FROM module_document_extraction WHERE module_id=?", (physical,))
        db._execute(c, "DELETE FROM module_documents WHERE module_id=?", (physical,))
        db._execute(c, "DELETE FROM module_library WHERE module_id=?", (physical,))
    return True


def delete_document(module_id: str, doc_id: str) -> bool:
    ensure_tables()
    pmid, pdid = db._scoped_id(module_id), db._scoped_id(doc_id)
    with db.conn() as c:
        exists = db._execute(
            c, "SELECT 1 FROM module_documents WHERE module_id=? AND doc_id=?", (pmid, pdid)
        ).fetchone()
        if not exists:
            return False
        db._execute(c, "DELETE FROM module_chunks WHERE module_id=? AND doc_id=?", (pmid, pdid))
        db._execute(c, "DELETE FROM module_sections WHERE module_id=? AND doc_id=?", (pmid, pdid))
        db._execute(c, "DELETE FROM module_document_extraction WHERE module_id=? AND doc_id=?", (pmid, pdid))
        db._execute(c, "DELETE FROM module_documents WHERE module_id=? AND doc_id=?", (pmid, pdid))
        db._execute(c, "UPDATE module_library SET updated_at=? WHERE module_id=?", (_now(), pmid))
    return True


def _clean_page_text(text: str) -> str:
    value = re.sub(r"\x00", "", str(text or "")).replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n{4,}", "\n\n\n", value).strip()


def _native_pdf_pages(data: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise HTTPException(422, "This PDF could not be opened.") from exc
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise HTTPException(422, "This PDF is encrypted. Remove the password before importing it.") from exc
    if len(reader.pages) > MAX_PDF_PAGES:
        raise HTTPException(422, f"One PDF has more than {MAX_PDF_PAGES} pages; split it before importing.")
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(_clean_page_text(text))
    return pages


def _extract_pdf_pages_with_diagnostics(data: bytes) -> tuple[list[str], dict]:
    """Extract native PDF text, then OCR only pages that actually need it.

    Selectable text remains authoritative and cheap. Scanned/low-text pages are sent
    through local Tesseract via PyMuPDF when available. Image/diagram presence is
    recorded separately; OCR can recover printed labels but does not pretend to infer
    the geometry or meaning of a complex diagram.
    """
    pages = _native_pdf_pages(data)
    low_native = [
        index for index, text in enumerate(pages)
        if len(re.sub(r"\s+", "", text)) < OCR_PAGE_MIN_NATIVE_CHARS
    ]
    diagnostics = {
        "extraction_mode": "native",
        "ocr_page_count": 0,
        "visual_page_count": 0,
        "low_text_page_count": len(low_native),
        "warnings": [],
    }

    # Fast path for healthy text PDFs. We still inspect visual-page counts only when
    # PyMuPDF is present, without making native-text imports depend on OCR tooling.
    try:
        import fitz  # PyMuPDF
        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        document = None

    visual_indices = set()
    if document is not None:
        try:
            for index, page in enumerate(document):
                images = bool(page.get_images(full=True))
                drawings = bool(getattr(page, "get_drawings", lambda: [])())
                if images or drawings:
                    visual_indices.add(index)
            diagnostics["visual_page_count"] = len(visual_indices)
        except Exception:
            diagnostics["visual_page_count"] = 0
            visual_indices = set()

    # OCR every genuinely low-text page. A single scanned slide or a vector-heavy
    # schematic can otherwise hide inside a mostly-native 200-page lecture pack.
    # Blank/divider pages are harmless: their OCR result will not replace native text.
    ocr_targets = list(low_native)

    ocr_errors = []
    if ocr_targets and document is not None:
        for index in ocr_targets:
            try:
                page = document[index]
                textpage = page.get_textpage_ocr(language="eng", dpi=OCR_DPI, full=True)
                recovered = _clean_page_text(page.get_text("text", textpage=textpage))
                # Never replace usable native text with a poorer OCR result.
                if len(re.sub(r"\s+", "", recovered)) > len(re.sub(r"\s+", "", pages[index])) + 8:
                    pages[index] = recovered
                    diagnostics["ocr_page_count"] += 1
            except Exception as exc:
                ocr_errors.append(f"p{index + 1}: {type(exc).__name__}")

    if diagnostics["ocr_page_count"]:
        diagnostics["extraction_mode"] = (
            "ocr" if diagnostics["ocr_page_count"] >= max(1, len(pages) - 1)
            else "hybrid"
        )

    if diagnostics["visual_page_count"]:
        diagnostics["warnings"].append(
            f"{diagnostics['visual_page_count']} page(s) contain embedded images/diagrams. "
            "Printed labels are searchable when extracted/OCR'd, but complex diagram/equation meaning still needs visual verification."
        )

    total = sum(len(x) for x in pages)
    if total > MAX_PDF_CHARS:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        raise HTTPException(422, "One PDF contains too much extracted/OCR text; split it into smaller PDFs.")

    remaining_low = sum(
        1 for text in pages if len(re.sub(r"\s+", "", text)) < OCR_PAGE_MIN_NATIVE_CHARS
    )
    diagnostics["low_text_page_count"] = remaining_low
    if ocr_errors:
        diagnostics["warnings"].append(
            "OCR could not fully read some low-text pages (" + ", ".join(ocr_errors[:8])
            + ("…" if len(ocr_errors) > 8 else "") + ")."
        )

    if total < 120:
        if document is None:
            raise HTTPException(
                422,
                "This PDF has almost no extractable text and OCR is unavailable on this server. "
                "It appears scanned/image-only; enable the OCR runtime or upload a text PDF.",
            )
        try:
            document.close()
        except Exception:
            pass
        raise HTTPException(
            422,
            "This PDF is scanned/image-heavy and local OCR recovered almost no readable text. "
            "Handwriting, low-resolution scans, equations or complex diagrams may need a clearer scan or visual review; "
            "the Module Library will not pretend it understood them.",
        )

    if remaining_low:
        diagnostics["warnings"].append(
            f"{remaining_low} page(s) still contain little searchable text after extraction/OCR."
        )
    if document is not None:
        try:
            document.close()
        except Exception:
            pass
    return pages, diagnostics


def _extract_pdf_pages(data: bytes) -> list[str]:
    pages, _ = _extract_pdf_pages_with_diagnostics(data)
    return pages


def _looks_like_heading(line: str) -> bool:
    value = re.sub(r"\s+", " ", str(line or "")).strip()
    if not (3 <= len(value) <= 110):
        return False
    if value.endswith((".", "?", "!", ";", ",")) and len(value.split()) > 5:
        return False
    words = value.split()
    if len(words) > 14:
        return False
    numbered = bool(re.match(r"^(?:\d+(?:\.\d+){0,4}|[A-Z]\d+(?:\.\d+)*)\s*[:.)-]?\s+\S", value))
    upperish = sum(1 for ch in value if ch.isupper()) >= max(3, sum(1 for ch in value if ch.isalpha()) * 0.55)
    titleish = sum(1 for w in words if w[:1].isupper()) >= max(2, math.ceil(len(words) * 0.65))
    return numbered or upperish or (titleish and len(words) <= 10)


def _heading_candidates(page_text: str) -> list[str]:
    output, seen = [], set()
    for raw in str(page_text or "").splitlines():
        value = re.sub(r"\s+", " ", raw).strip(" •·–—|\t")
        if not _looks_like_heading(value):
            continue
        key = value.casefold()
        if key in seen or key in {"contents", "table of contents", "index"}:
            continue
        seen.add(key)
        output.append(value[:110])
        if len(output) >= 4:
            break
    return output


def _build_sections(pages: list[str]) -> list[dict]:
    starts = []
    last_heading = None
    for page_no, text in enumerate(pages, start=1):
        candidates = _heading_candidates(text)
        heading = candidates[0] if candidates else None
        if heading and _norm(heading) != _norm(last_heading or ""):
            starts.append((page_no, heading))
            last_heading = heading
    if not starts:
        # Coarse but complete fallback; every page remains reachable.
        return [
            {"heading": f"Pages {start}–{min(len(pages), start + 9)}", "page_start": start,
             "page_end": min(len(pages), start + 9)}
            for start in range(1, len(pages) + 1, 10)
        ][:MAX_COURSE_MAP_SECTIONS]
    if starts[0][0] > 1:
        starts.insert(0, (1, "Opening material"))
    if len(starts) > MAX_COURSE_MAP_SECTIONS:
        # Keep coverage across the whole PDF instead of truncating late chapters/slides.
        indexes = sorted(set(
            round(i * (len(starts) - 1) / (MAX_COURSE_MAP_SECTIONS - 1))
            for i in range(MAX_COURSE_MAP_SECTIONS)
        ))
        starts = [starts[i] for i in indexes]
    sections = []
    for index, (page_start, heading) in enumerate(starts):
        page_end = (starts[index + 1][0] - 1) if index + 1 < len(starts) else len(pages)
        page_end = max(page_start, page_end)
        sections.append({"heading": heading, "page_start": page_start, "page_end": page_end})
    return sections


def _tokens(value: str) -> list[str]:
    return [
        t.casefold() for t in _TOKEN.findall(str(value or ""))
        if len(t) >= 3 and t.casefold() not in _STOP and not t.isdigit()
    ]


def _terms(value: str, limit: int = 90) -> str:
    counts = Counter(_tokens(value))
    top = [term for term, _ in counts.most_common(limit)]
    return "|" + "|".join(top) + "|" if top else ""


def _section_for_page(sections: list[dict], page: int) -> str:
    for section in sections:
        if int(section["page_start"]) <= page <= int(section["page_end"]):
            return str(section["heading"])
    return ""


def _chunk_pages(pages: list[str], sections: list[dict]) -> list[dict]:
    chunks = []
    buffer, page_start, page_end = "", 1, 1

    def flush():
        nonlocal buffer, page_start, page_end
        text = buffer.strip()
        if not text:
            buffer = ""
            return
        chunks.append({
            "chunk_index": len(chunks),
            "page_start": page_start,
            "page_end": page_end,
            "heading": _section_for_page(sections, page_start),
            "text": text,
            "terms": _terms(text + " " + _section_for_page(sections, page_start)),
        })
        overlap = text[-CHUNK_OVERLAP_CHARS:] if len(text) > CHUNK_OVERLAP_CHARS else ""
        buffer = overlap

    for page_no, page_text in enumerate(pages, start=1):
        pieces = [x.strip() for x in re.split(r"\n\s*\n|(?<=\.)\s+(?=[A-Z0-9])", page_text) if x.strip()]
        if not pieces:
            continue
        for piece in pieces:
            if not buffer:
                page_start = page_no
            projected = len(buffer) + len(piece) + 2
            if projected > CHUNK_MAX_CHARS and buffer:
                flush()
                page_start = page_no
            buffer = (buffer + "\n\n" + piece).strip()
            page_end = page_no
            if len(buffer) >= CHUNK_TARGET_CHARS:
                flush()
                page_start = page_no
    if buffer.strip():
        # Avoid emitting an overlap-only tail.
        if not chunks or len(buffer.strip()) > CHUNK_OVERLAP_CHARS + 80:
            flush()
    return chunks


def _upsert_module(title: str, code: str = "") -> str:
    ensure_tables()
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    code = re.sub(r"\s+", " ", str(code or "")).strip()
    if len(title) < 2:
        raise HTTPException(422, "Enter the module/subject name first.")
    module_id = _module_id(title, code)
    physical = db._scoped_id(module_id)
    now = _now()
    with db.conn() as c:
        db._execute(
            c,
            """INSERT INTO module_library(module_id,title,code,metadata,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(module_id) DO UPDATE SET title=excluded.title,code=excluded.code,updated_at=excluded.updated_at""",
            (physical, title[:180], code[:80], "{}", now, now),
        )
    return module_id


def index_document_pages(module_id: str, filename: str, pages: list[str], *, sha256: str | None = None,
                         extraction: dict | None = None) -> dict:
    ensure_tables()
    module = get_module(module_id, include_sections=False)
    if not module:
        raise HTTPException(404, "Module Library module not found.")
    digest = sha256 or hashlib.sha256("\n\f\n".join(pages).encode()).hexdigest()
    doc_id = _doc_id(module_id, digest)
    pmid, pdid = db._scoped_id(module_id), db._scoped_id(doc_id)
    with db.conn() as c:
        existing = db._execute(
            c, "SELECT * FROM module_documents WHERE module_id=? AND sha256=?", (pmid, digest)
        ).fetchone()
        if existing:
            item = _doc_row(existing)
            item["duplicate"] = True
            return item
    sections = _build_sections(pages)
    chunks = _chunk_pages(pages, sections)
    now = _now()
    char_count = sum(len(x) for x in pages)
    with db.conn() as c:
        db._execute(
            c,
            """INSERT INTO module_documents(doc_id,module_id,filename,sha256,page_count,char_count,section_count,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (pdid, pmid, filename[:240], digest, len(pages), char_count, len(sections), now),
        )
        for chunk in chunks:
            cid = db._scoped_id(_chunk_id(doc_id, int(chunk["chunk_index"])))
            db._execute(
                c,
                """INSERT INTO module_chunks(
                   chunk_id,module_id,doc_id,chunk_index,page_start,page_end,heading,text,terms,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (cid, pmid, pdid, int(chunk["chunk_index"]), int(chunk["page_start"]), int(chunk["page_end"]),
                 str(chunk["heading"])[:180], str(chunk["text"]), str(chunk["terms"]), now),
            )
        for section in sections:
            sid = db._scoped_id(_section_id(doc_id, str(section["heading"]), int(section["page_start"])))
            db._execute(
                c,
                """INSERT INTO module_sections(
                   section_id,module_id,doc_id,heading,page_start,page_end,status,mastery,last_reviewed,notes,updated_at
                   ) VALUES(?,?,?,?,?,?,'unseen',NULL,NULL,'',?)""",
                (sid, pmid, pdid, str(section["heading"])[:180], int(section["page_start"]),
                 int(section["page_end"]), now),
            )
        info = dict(extraction or {})
        db._execute(
            c,
            """INSERT INTO module_document_extraction(
               doc_id,module_id,extraction_mode,ocr_page_count,visual_page_count,low_text_page_count,warnings,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                pdid, pmid, str(info.get("extraction_mode") or "native"),
                int(info.get("ocr_page_count") or 0),
                int(info.get("visual_page_count") or 0),
                int(info.get("low_text_page_count") or 0),
                json.dumps(info.get("warnings") or [], ensure_ascii=False),
                now,
            ),
        )
        db._execute(c, "UPDATE module_library SET updated_at=? WHERE module_id=?", (now, pmid))
    return {
        "doc_id": doc_id,
        "module_id": module_id,
        "filename": filename,
        "sha256": digest,
        "page_count": len(pages),
        "char_count": char_count,
        "section_count": len(sections),
        "chunk_count": len(chunks),
        "duplicate": False,
        "extraction": {
            "extraction_mode": str((extraction or {}).get("extraction_mode") or "native"),
            "ocr_page_count": int((extraction or {}).get("ocr_page_count") or 0),
            "visual_page_count": int((extraction or {}).get("visual_page_count") or 0),
            "low_text_page_count": int((extraction or {}).get("low_text_page_count") or 0),
            "warnings": list((extraction or {}).get("warnings") or []),
        },
    }


def import_pdf_bytes(title: str, code: str, filename: str, data: bytes) -> dict:
    if len(data) > MAX_MODULE_FILE_BYTES:
        raise HTTPException(413, f"{filename} is larger than {MAX_MODULE_FILE_BYTES // 1_000_000} MB.")
    ensure_tables()
    module_id = _upsert_module(title, code)
    digest = hashlib.sha256(data).hexdigest()
    pmid = db._scoped_id(module_id)
    with db.conn() as c:
        existing = db._execute(
            c, "SELECT * FROM module_documents WHERE module_id=? AND sha256=?", (pmid, digest)
        ).fetchone()
    if existing:
        item = _doc_row(existing)
        item["duplicate"] = True
        return item
    pages, extraction = _extract_pdf_pages_with_diagnostics(data)
    return index_document_pages(module_id, filename, pages, sha256=digest, extraction=extraction)


def update_section_progress(module_id: str, section_id: str, *, status: str | None = None,
                            mastery: int | None = None, notes: str | None = None) -> dict:
    ensure_tables()
    allowed = {"unseen", "learning", "reviewing", "mastered"}
    if status is not None and status not in allowed:
        raise HTTPException(422, "Invalid section status.")
    if mastery is not None and not 0 <= int(mastery) <= 100:
        raise HTTPException(422, "Mastery must be between 0 and 100.")
    pmid, psid = db._scoped_id(module_id), db._scoped_id(section_id)
    with db.conn() as c:
        row = db._execute(
            c, "SELECT * FROM module_sections WHERE module_id=? AND section_id=?", (pmid, psid)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Module section not found.")
        current = dict(row)
        next_status = status if status is not None else current["status"]
        next_mastery = int(mastery) if mastery is not None else current["mastery"]
        next_notes = str(notes)[:2000] if notes is not None else current["notes"]
        reviewed = _now() if status in {"reviewing", "mastered"} or mastery is not None else current["last_reviewed"]
        db._execute(
            c,
            """UPDATE module_sections SET status=?,mastery=?,last_reviewed=?,notes=?,updated_at=?
               WHERE module_id=? AND section_id=?""",
            (next_status, next_mastery, reviewed, next_notes, _now(), pmid, psid),
        )
        updated = db._execute(c, "SELECT * FROM module_sections WHERE section_id=?", (psid,)).fetchone()
    return _section_row(updated)


def _candidate_chunks(module_id: str, query: str, *, limit: int = 180) -> list[dict]:
    pmid = db._scoped_id(module_id)
    tokens = list(dict.fromkeys(_tokens(query)))[:10]
    with db.conn() as c:
        if tokens:
            clauses, params = [], [pmid]
            for token in tokens:
                clauses.append("(terms LIKE ? OR heading LIKE ?)")
                params.extend((f"%|{token}|%", f"%{token}%"))
            rows = db._execute(
                c,
                f"""SELECT c.*,d.filename FROM module_chunks c
                    JOIN module_documents d ON d.doc_id=c.doc_id
                    WHERE c.module_id=? AND ({' OR '.join(clauses)})
                    ORDER BY c.doc_id,c.chunk_index LIMIT {int(limit)}""",
                tuple(params),
            ).fetchall()
        else:
            rows = []
        if not rows:
            rows = db._execute(
                c,
                """SELECT c.*,d.filename FROM module_chunks c
                   JOIN module_documents d ON d.doc_id=c.doc_id
                   WHERE c.module_id=? ORDER BY c.doc_id,c.chunk_index LIMIT ?""",
                (pmid, int(limit)),
            ).fetchall()
    return [dict(x) for x in rows]


def search_module(module_id: str, query: str, *, top_k: int = 12) -> list[dict]:
    module = get_module(module_id, include_sections=False)
    if not module:
        raise HTTPException(404, "Module Library module not found.")
    qtokens = set(_tokens(query))
    phrase = _norm(query)
    candidates = _candidate_chunks(module_id, query)
    scored = []
    for row in candidates:
        terms = set(str(row.get("terms") or "").strip("|").split("|")) if row.get("terms") else set()
        heading = _norm(row.get("heading") or "")
        text_norm = _norm(str(row.get("text") or "")[:16000])
        overlap = len(qtokens & terms)
        heading_hits = sum(1 for t in qtokens if t in heading)
        exact = 1 if phrase and len(phrase) >= 5 and phrase in text_norm else 0
        score = overlap * 5 + heading_hits * 7 + exact * 12
        if not qtokens:
            score = 1
        scored.append((score, int(row.get("chunk_index") or 0), row))
    scored.sort(key=lambda x: (-x[0], x[1]))
    output = []
    for score, _, row in scored[:max(1, min(30, int(top_k)))]:
        output.append({
            "chunk_id": db._unscope(row["chunk_id"]),
            "doc_id": db._unscope(row["doc_id"]),
            "module_id": module_id,
            "filename": row["filename"],
            "heading": row.get("heading") or "",
            "page_start": int(row["page_start"]),
            "page_end": int(row["page_end"]),
            "text": row["text"],
            "score": score,
        })
    return output


def _select_modules_for_request(request_text: str) -> list[dict]:
    modules = list_modules()
    if not modules:
        return []
    refs = [x.casefold() for x in _MODULE_REF.findall(str(request_text or ""))]
    if refs:
        return [m for m in modules if str(m["module_id"]).casefold() in refs]
    request_norm = _norm(request_text)
    selected = []
    for module in modules:
        title = _norm(module.get("title") or "")
        code = _norm(module.get("code") or "")
        if title and len(title) >= 4 and title in request_norm:
            selected.append(module)
            continue
        if code and len(code) >= 2 and re.search(rf"\b{re.escape(code)}\b", request_norm):
            selected.append(module)
    if selected:
        return selected[:3]
    if len(modules) == 1 and re.search(r"\b(?:my|this|the)\s+(?:module|course|subject)\b|\bmodule\s+library\b", request_text, re.I):
        return modules
    return []


def _course_map(module: dict) -> str:
    detail = get_module(module["module_id"], include_sections=True) or module
    by_doc: dict[str, list[dict]] = {}
    for section in detail.get("sections") or []:
        by_doc.setdefault(section["doc_id"], []).append(section)
    lines = [
        f"MODULE LIBRARY COURSE MAP — {detail.get('title')} ({detail.get('code') or 'no code'})",
        f"Documents: {len(detail.get('documents') or [])}; indexed pages: {sum(int(x.get('page_count') or 0) for x in detail.get('documents') or [])}.",
        "The following page ranges were detected from the user's uploaded PDFs. Use them as navigation evidence; do not invent unseen page ranges.",
    ]
    for doc in detail.get("documents") or []:
        lines.append(f"\nPDF: {doc['filename']} — {doc['page_count']} pages")
        sections = by_doc.get(doc["doc_id"], [])
        for section in sections:
            label = section.get("heading") or "Section"
            lines.append(f"- {label} — pp. {section['page_start']}–{section['page_end']} — status={section.get('status') or 'unseen'}"
                         + (f", mastery={section['mastery']}%" if section.get("mastery") is not None else ""))
    full = "\n".join(lines)
    if len(full) <= 110_000:
        return full
    # Preserve every document's presence even when slide-by-slide headings explode.
    # The persistent index still contains all sections; only the one-call map is compacted.
    compact = [
        f"MODULE LIBRARY COURSE MAP — {detail.get('title')} ({detail.get('code') or 'no code'})",
        f"Documents: {len(detail.get('documents') or [])}; indexed pages: {sum(int(x.get('page_count') or 0) for x in detail.get('documents') or [])}.",
        "The complete section map is stored persistently. This compact view samples headings from every PDF because the full heading list exceeds one reasoning context.",
    ]
    for doc in detail.get("documents") or []:
        sections = by_doc.get(doc["doc_id"], [])
        compact.append(f"\nPDF: {doc['filename']} — {doc['page_count']} pages — {len(sections)} detected sections")
        if not sections:
            continue
        sample_count = min(60, len(sections))
        picks = sorted(set(
            round(i * (len(sections) - 1) / max(1, sample_count - 1))
            for i in range(sample_count)
        ))
        for index in picks:
            section = sections[index]
            compact.append(f"- {section['heading']} — pp. {section['page_start']}–{section['page_end']} — status={section.get('status') or 'unseen'}")
    compact.append("IMPORTANT: omitted headings remain searchable in the persistent Module Library; do not treat this compact sample as the complete syllabus.")
    return "\n".join(compact)[:110_000]


def _representative_chunks(module_id: str, *, max_chunks: int = 14) -> list[dict]:
    detail = get_module(module_id, include_sections=False)
    if not detail:
        return []
    pmid = db._scoped_id(module_id)
    rows = []
    with db.conn() as c:
        for doc in detail.get("documents") or []:
            pdid = db._scoped_id(doc["doc_id"])
            count_row = db._execute(c, "SELECT COUNT(*) AS n FROM module_chunks WHERE doc_id=?", (pdid,)).fetchone()
            count = int(count_row["n"] if count_row else 0)
            if not count:
                continue
            indexes = sorted(set([0, count // 2, max(0, count - 1)]))
            for index in indexes:
                row = db._execute(
                    c,
                    """SELECT c.*,d.filename FROM module_chunks c JOIN module_documents d ON d.doc_id=c.doc_id
                       WHERE c.doc_id=? AND c.chunk_index=?""",
                    (pdid, index),
                ).fetchone()
                if row:
                    rows.append(dict(row))
    # Round-robin-ish document coverage, hard bounded by model context.
    output = []
    for row in rows[:max_chunks]:
        item = dict(row)
        item["chunk_id"] = db._unscope(item.get("chunk_id"))
        item["doc_id"] = db._unscope(item.get("doc_id"))
        item["module_id"] = db._unscope(item.get("module_id"))
        output.append(item)
    return output


def module_context_for_request(request_text: str, *, max_chars: int = 115_000) -> list[dict]:
    selected = _select_modules_for_request(request_text)
    docs, used = [], 0
    for module in selected:
        course_map = _course_map(module)
        if used < max_chars:
            text = course_map[:max_chars - used]
            docs.append({
                "source_id": f"module-map-{module['module_id']}",
                "source": f"module://{module['module_id']}/course-map",
                "source_type": "pdf",
                "source_role": "supporting",
                "title": f"{module['title']} — indexed course map",
                "text": text,
            })
            used += len(text)
        query = request_text
        relevant = search_module(module["module_id"], query, top_k=5)
        representative = _representative_chunks(module["module_id"], max_chunks=7)
        # Always mix breadth with query relevance. This prevents a generic whole-module
        # request from being dominated by the first PDF while still surfacing a named
        # weak topic when the user asks for one.
        retrieved, seen_chunks = [], set()
        for chunk in [*relevant, *representative]:
            key = str(chunk.get("chunk_id") or "")
            if key and key in seen_chunks:
                continue
            if key:
                seen_chunks.add(key)
            retrieved.append(chunk)
        for chunk in retrieved[:10]:
            if used >= max_chars:
                break
            text = str(chunk["text"])[:min(9000, max_chars - used)]
            docs.append({
                "source_id": chunk["chunk_id"],
                "source": f"module://{module['module_id']}/{chunk['doc_id']}#p{chunk['page_start']}-p{chunk['page_end']}",
                "source_type": "pdf",
                "source_role": "supporting",
                "title": f"{module['title']} · {chunk['filename']} · pp. {chunk['page_start']}–{chunk['page_end']}",
                "text": text,
            })
            used += len(text)
    return docs


def module_learning_resources_for_request(request_text: str) -> list[LearningResource]:
    resources = []
    for module in _select_modules_for_request(request_text):
        detail = get_module(module["module_id"], include_sections=True)
        if not detail:
            continue
        by_doc: dict[str, list[dict]] = {}
        for section in detail.get("sections") or []:
            by_doc.setdefault(section["doc_id"], []).append(section)
        for doc in detail.get("documents") or []:
            doc_sections = by_doc.get(doc["doc_id"], [])
            sample_count = min(80, len(doc_sections))
            sample_indexes = sorted(set(
                round(i * (len(doc_sections) - 1) / max(1, sample_count - 1))
                for i in range(sample_count)
            )) if doc_sections else []
            topics = [
                f"{doc_sections[index]['heading']} (pp. {doc_sections[index]['page_start']}–{doc_sections[index]['page_end']})"
                for index in sample_indexes
            ]
            resources.append(LearningResource(
                id=f"module_{doc['doc_id']}",
                title=f"{detail['title']} — {doc['filename']}",
                url=f"user-owned://module/{detail['module_id']}/{doc['doc_id']}",
                topics=topics,
                reason=(
                    f"Indexed from the user's Module Library. Full PDF text is stored persistently; "
                    f"{doc['page_count']} pages are searchable by page range. Use exact listed headings/page ranges only."
                ),
            ))
    return resources


def module_search_documents_for_request(request_text: str, query: str, *, top_k: int = 10) -> list[dict]:
    output = []
    for module in _select_modules_for_request(request_text):
        output.extend(search_module(module["module_id"], query, top_k=top_k))
    output.sort(key=lambda x: -int(x.get("score") or 0))
    return output[:top_k]
