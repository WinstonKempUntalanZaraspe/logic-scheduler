from __future__ import annotations

"""Content-grounded provenance validation for Project Intelligence.

The model's ``source`` string is treated as a hint, never as the evidence itself. VERIFIED
claims must be supported by either the user's explicit request or one of the documents that
were actually fetched. Once support is found, provenance is canonicalized to that real
evidence source. Unsupported VERIFIED claims still fail closed.
"""
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from .config import settings

log=logging.getLogger(__name__)

_STOP={"a","an","and","are","as","at","be","by","for","from","in","is","of","on","or","the","to","with","your","you","must","will","this","that"}
_MONTHS={"jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,"apr":4,"april":4,"may":5,"jun":6,"june":6,"jul":7,"july":7,"aug":8,"august":8,"sep":9,"sept":9,"september":9,"oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12}


def _norm(v):
    return " ".join(re.findall(r"[a-z0-9]+",str(v or "").casefold()))


def _supported(claim, corpus):
    c,h=_norm(claim),_norm(corpus)
    if not c or not h:
        return False
    if c in h:
        return True
    toks=[x for x in c.split() if x not in _STOP and len(x)>=2]
    if not toks:
        return False
    hit=sum(bool(re.search(rf"\b{re.escape(x)}\b",h)) for x in toks)
    return hit >= (1 if len(toks)==1 else max(2,(2*len(toks)+2)//3))


def _canonical_source_url(value):
    raw=str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed=urlsplit(raw)
    except Exception:
        return ""
    scheme=parsed.scheme.casefold()
    if scheme not in {"http","https"} or not parsed.hostname:
        return ""
    host=parsed.hostname.casefold().rstrip(".")
    try:
        port=parsed.port
    except ValueError:
        return ""
    default=(scheme=="http" and port==80) or (scheme=="https" and port==443)
    netloc=host if not port or default else f"{host}:{port}"
    path=parsed.path or "/"
    if path != "/":
        path=path.rstrip("/") or "/"
    return urlunsplit((scheme,netloc,path,parsed.query,""))


def _source_matches(doc, source):
    source=str(source or "").strip()
    if source in {doc.get("source"),doc.get("title")}:
        return True
    wanted=_canonical_source_url(source)
    if not wanted:
        return False
    return any(
        wanted==_canonical_source_url(candidate)
        for candidate in (doc.get("source"),doc.get("title"))
        if candidate
    )


def _rubric_supported(claim, weight, corpus):
    """Criterion support alone cannot verify an invented/swapped percentage."""
    chunks=re.split(r'[;\n]|(?<=[%])\s*|\.(?!\d)',str(corpus or ""))
    return any(
        _supported(claim,chunk)
        and any(
            float(number)==float(weight)
            for number in re.findall(
                r'(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|percent\b|points?\b)',chunk,re.I
            )
        )
        for chunk in chunks
    )


def _evidence_supported(section, item, corpus):
    claim=str(item.get("value") or item.get("criterion") or "")
    if not claim:
        return False
    if section=="rubric":
        return _rubric_supported(claim,float(item.get("weight") or 0),corpus)
    return _supported(claim,corpus)


def _document_rows(docs):
    rows=[]
    for index,doc in enumerate(docs, start=1):
        source=str(doc.get("source") or "").strip()
        title=str(doc.get("title") or "").strip()
        source_type=str(doc.get("source_type") or "").strip()
        if source_type not in {"website","pdf"}:
            continue
        rows.append({
            "source_id":str(doc.get("source_id") or f"source_{index}"),
            "source":source or title,
            "title":title,
            "source_type":source_type,
            "text":str(doc.get("text") or ""),
        })
    return rows


def _pick_supporting_document(section,item,doc_rows,source_hint=""):
    matches=[doc for doc in doc_rows if _evidence_supported(section,item,doc["text"])]
    if not matches:
        return None
    hinted=[doc for doc in matches if _source_matches(doc,source_hint) or str(source_hint or "").strip()==doc.get("source_id")]
    return hinted[0] if hinted else matches[0]


def _canonicalize_verified_item(section, raw_item, model_item, request_text, doc_rows):
    """Resolve a VERIFIED claim to evidence that actually supports it.

    Source labels emitted by the model are not trusted. They are used only to prefer one of
    several already-supporting fetched documents. This prevents harmless URL/citation
    formatting mistakes from destroying a valid blueprint while keeping unsupported claims
    fail-closed.
    """
    st=str(raw_item.get("source_type") or "")
    src=str(raw_item.get("source") or "").strip()
    claim=str(raw_item.get("value") or raw_item.get("criterion") or "").strip()

    if not claim:
        raise HTTPException(422,"Project Intelligence produced an empty VERIFIED claim; nothing was saved.")

    if st=="planner_inference":
        raise HTTPException(422,"Project Intelligence rejected an inference labelled VERIFIED; nothing was saved.")

    user_supported=_evidence_supported(section,raw_item,request_text)
    doc=_pick_supporting_document(section,raw_item,doc_rows,src)

    # Preserve an explicit user fact when the request itself really supports it. This also
    # repairs model mistakes where a user-supplied fact was incorrectly labelled website.
    if user_supported and st=="user_input":
        model_item.source_type="user_input"
        model_item.source="user request"
        return

    # Prefer a real fetched document when it supports the claim. The model's source string
    # may be a fragment URL, title, invented anchor, or otherwise malformed; support in the
    # fetched text is what makes the fact verifiable.
    if doc is not None and st in {"website","pdf","user_input"}:
        model_item.source_type=doc["source_type"]
        model_item.source=doc["source"]
        return

    # If no fetched document supports it but the user explicitly supplied the same fact,
    # preserve that provenance instead of falsely attributing it to the website.
    if user_supported and st in {"website","pdf"}:
        model_item.source_type="user_input"
        model_item.source="user request"
        return

    fetched=[doc.get("source") for doc in doc_rows]
    log.warning(
        "Project Intelligence VERIFIED claim could not be grounded: section=%s label=%r model_source=%r source_type=%r fetched_sources=%r",
        section, str(raw_item.get("label") or raw_item.get("criterion") or "")[:120], src[:240], st, fetched[:12],
    )
    if section=="rubric":
        raise HTTPException(422,"Project Intelligence could not verify a rubric criterion and weight in the request or fetched sources; nothing was saved.")
    raise HTTPException(422,"Project Intelligence could not ground a VERIFIED claim in the request or fetched sources; nothing was saved.")


def _date_mentions(text):
    out=set(); value=str(text or "")
    for y,m,d in re.findall(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])\b",value):
        try:
            x=date(int(y),int(m),int(d));out.add((x.year,x.month,x.day))
        except ValueError:
            pass
    mp="|".join(sorted(_MONTHS,key=len,reverse=True))
    for mon,d,y in re.findall(rf"\b({mp})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s*(20\d{{2}})?\b",value,re.I):
        try:
            out.add((int(y) if y else None,_MONTHS[mon.casefold()],int(d)))
        except (ValueError,KeyError):
            pass
    for d,mon,y in re.findall(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({mp})\.?(?:,)?\s*(20\d{{2}})?\b",value,re.I):
        try:
            out.add((int(y) if y else None,_MONTHS[mon.casefold()],int(d)))
        except (ValueError,KeyError):
            pass
    return out


def _date_supported(raw,corpora,request_text=""):
    today=datetime.now(settings.tz).date()
    if not raw:
        return True
    try:
        target=date.fromisoformat(str(raw)[:10])
    except ValueError:
        return False
    relative=set()
    if re.search(r"\btomorrow\b",request_text,re.I):
        relative.add(today+timedelta(days=1))
    if re.search(r"\btoday\b",request_text,re.I):
        relative.add(today)
    for count,unit in re.findall(r"\bin\s+(\d+)\s+(days?|weeks?)\b",request_text,re.I):
        amount=int(count)*(7 if unit.lower().startswith("week") else 1)
        if amount<=3650:
            relative.add(today+timedelta(days=amount))
    if target in relative:
        return True
    return any(
        (month,day)==(target.month,target.day) and (year is None or year==target.year)
        for corpus in corpora for year,month,day in _date_mentions(corpus)
    )


def validate_campaign_draft(draft,request_text,docs):
    raw=draft.model_dump(mode="json")
    doc_rows=_document_rows(docs)

    for section in ("requirements","deliverables","rules","technical_requirements","datasets","rubric"):
        raw_items=raw.get(section) or []
        model_items=list(getattr(draft,section,[]) or [])
        for index,item in enumerate(raw_items):
            if item.get("confidence")!="VERIFIED":
                continue
            if index>=len(model_items):
                raise HTTPException(422,"Project Intelligence evidence validation failed; nothing was saved.")
            _canonicalize_verified_item(section,item,model_items[index],request_text,doc_rows)

    corpora=[request_text,*[doc["text"] for doc in doc_rows]]
    refreshed=draft.model_dump(mode="json")
    for field,label in (("deadline","deadline"),("presentation_date","presentation date")):
        if refreshed.get(field) and not _date_supported(refreshed[field],corpora,request_text):
            raise HTTPException(422,f"Project Intelligence could not verify the {label} in the supplied sources, so it was not saved.")



def _historical_simulation_requested(request_text):
    value=str(request_text or "")
    return bool(re.search(
        r"\b(?:as\s+if\s+(?:it\s+)?(?:(?:were|was)\s+)?upcoming|pretend\s+(?:it\s+)?(?:is|was|were)\s+upcoming|historical\s+(?:simulation|test)|simulate\s+(?:this\s+)?(?:past|historical)\s+(?:event|hackathon|project))\b",
        value,re.I,
    ))


def _append_risk(draft,message):
    risks=list(getattr(draft,"major_risks",[]) or [])
    if message not in risks:
        risks.append(message)
    draft.major_risks=risks


def sanitize_campaign_draft(draft,request_text,docs):
    """Fail soft on individual unsupported model claims without saving them.

    A single unverifiable row must not destroy an otherwise useful blueprint. We remove
    only the unsupported VERIFIED fact, record a visible risk, and keep all grounded
    evidence plus recommended work packages. This is intentionally different from
    validate_campaign_draft(), which remains the strict validator used by regression tests.
    """
    doc_rows=_document_rows(docs)
    omitted=[]

    for section in ("requirements","deliverables","rules","technical_requirements","datasets","rubric"):
        kept=[]
        for model_item in list(getattr(draft,section,[]) or []):
            raw_item=model_item.model_dump(mode="json")
            if raw_item.get("confidence")!="VERIFIED":
                kept.append(model_item)
                continue
            try:
                _canonicalize_verified_item(section,raw_item,model_item,request_text,doc_rows)
                kept.append(model_item)
            except HTTPException:
                label=str(raw_item.get("label") or raw_item.get("criterion") or raw_item.get("value") or "claim").strip()
                omitted.append(f"{section}: {label[:100]}")
        setattr(draft,section,kept)

    if omitted:
        preview=", ".join(omitted[:5])
        suffix=f" (+{len(omitted)-5} more)" if len(omitted)>5 else ""
        _append_risk(
            draft,
            "Source verification omitted unsupported model claim(s): "+preview+suffix+
            ". Grounded facts and the preparation roadmap were kept."
        )

    corpora=[request_text,*[doc["text"] for doc in doc_rows]]
    today=datetime.now(settings.tz).date()
    historical=_historical_simulation_requested(request_text)
    for field,label in (("deadline","deadline"),("presentation_date","presentation date")):
        raw=getattr(draft,field,None)
        if not raw:
            continue
        if not _date_supported(raw,corpora,request_text):
            setattr(draft,field,None)
            _append_risk(
                draft,
                f"An unverified {label} was omitted instead of blocking the project preview."
            )
            continue
        if historical:
            try:
                parsed=date.fromisoformat(str(raw)[:10])
            except ValueError:
                parsed=None
            if parsed and parsed<today:
                setattr(draft,field,None)
                _append_risk(
                    draft,
                    f"Historical {label} {parsed.isoformat()} was retained only as source context; "
                    "it was not used as a live scheduling deadline because this is an 'as if upcoming' simulation."
                )
    return draft

def _tolerant_url_text(text):
    """Normalize common copy/paste escaping only for URL discovery."""
    value=str(text or "")
    value=re.sub(r"(?i)\b(https?)\\+:",r"\1:",value)
    value=value.replace("\\/","/")
    return value


def install(runtime_module):
    base=runtime_module.reason_campaign
    if getattr(base,"_project_provenance_guard",False):
        return

    base_extract=getattr(runtime_module,"extract_urls",None)
    if callable(base_extract) and not getattr(base_extract,"_project_url_tolerant",False):
        def tolerant_extract_urls(text):
            return base_extract(_tolerant_url_text(text))
        tolerant_extract_urls._project_url_tolerant=True
        runtime_module.extract_urls=tolerant_extract_urls

    async def guarded(request_text,docs):
        draft=await base(request_text,docs)
        return sanitize_campaign_draft(draft,request_text,docs)

    guarded._project_provenance_guard=True
    runtime_module.reason_campaign=guarded

