from __future__ import annotations

"""Final Temporal IR enrichment at the reviewed-intake boundary.

Every mature parser can keep its task/state semantics. This layer contributes one
normalized interpretation of dates, clocks, windows, deadlines and relationships, then
compiles only constraints that can be applied without guessing activity identity.

It never writes TickTick. It mutates the reviewed preview only and re-persists that
preview so Apply sees exactly the same interpretation.
"""

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta

from .config import settings
from . import intake_contract as contract
from . import temporal_engine as temporal

_INSTALLED = False


def _dt(value):
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=settings.tz)
        return out.astimezone(settings.tz)
    except Exception:
        return None


def _set_lower(patch: dict, key: str, value: datetime) -> None:
    current = _dt(patch.get(key))
    if current is None or value > current:
        patch[key] = value.isoformat()


def _set_upper(patch: dict, key: str, value: datetime) -> None:
    current = _dt(patch.get(key))
    if current is None or value < current:
        patch[key] = value.isoformat()


def _duration(change: dict) -> int | None:
    raw = (change.get("meta_patch") or {}).get("duration_minutes")
    try:
        return max(1, int(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _has_explicit_date(doc: temporal.TemporalDocument) -> bool:
    return bool(doc.dates)


def _sentence_for_evidence(source: str, item) -> str:
    start = int(item.evidence.start)
    end = int(item.evidence.end)
    left = max(source.rfind(".", 0, start), source.rfind(";", 0, start), source.rfind("\n", 0, start))
    rights = [x for x in (source.find(".", end), source.find(";", end), source.find("\n", end)) if x >= 0]
    right = min(rights) if rights else len(source)
    return source[left + 1:right].strip()


def _constraint_owned_by_other_planner(source: str, item) -> bool:
    """Keep venue/arrival clocks in their dedicated logistics compilers.

    Temporal IR is intentionally broad: it should *record* "pool closes at 21:30" and
    "reach the pool before 20:15". But those clocks are not the task's start/end.
    Existing venue-availability and arrival-deadline layers already translate them
    with travel/changing semantics, so applying them again here would be wrong.
    """
    clause = _sentence_for_evidence(source, item).lower()
    if re.search(
        r"\b(?:pool|gym|library|shop|store|venue|office|school|church|facility|"
        r"stadium|centre|center|market|mall)\b.{0,80}\b"
        r"(?:closes?|opens?|shuts?(?:\s+down)?|locks?\s+up|stays?\s+open|"
        r"is\s+(?:only\s+)?open|is\s+available)\b",
        clause,
        re.I,
    ):
        return True
    if re.search(
        r"\b(?:reach|arrive(?:\s+at)?|get\s+to|be\s+at|get\s+there|be\s+there|"
        r"clock\s+in)\b.{0,80}\b(?:by|before|no\s+later\s+than)\b",
        clause,
        re.I,
    ):
        return True
    return False


def _compile_change(change: dict, now: datetime, clarifications: list[dict]) -> None:
    if change.get("action") not in {"create", "update"}:
        return
    source = str(change.get("line") or "")
    if not source.strip():
        return
    doc = temporal.parse_temporal(source, now)
    patch = change.setdefault("meta_patch", {})
    duration = _duration(change)

    # Existing exact fixed intervals are already stronger than every flexible meta bound.
    fixed_start = _dt(change.get("fixed_start"))
    fixed_end = _dt(change.get("fixed_end"))

    for item in doc.constraints:
        if item.negated or item.hypothetical:
            continue
        if _constraint_owned_by_other_planner(source, item):
            continue

        if item.kind == "deadline" and item.latest_at:
            current = _dt(patch.get("deadline"))
            if current is None or item.latest_at < current:
                patch["deadline"] = item.latest_at.isoformat()
            continue

        if item.kind == "not_after" and item.latest_at:
            _set_upper(patch, "latest_end", item.latest_at)
            continue

        if item.kind == "not_before" and item.earliest_at:
            _set_lower(patch, "earliest", item.earliest_at)
            continue

        if item.kind == "window" and item.start_at and item.end_at and not fixed_start:
            _set_lower(patch, "earliest", item.start_at)
            _set_upper(patch, "latest_end", item.end_at)
            patch.setdefault("preferred_window_start", item.start_at.strftime("%H:%M"))
            patch.setdefault("preferred_window_end", item.end_at.strftime("%H:%M"))
            continue

        if item.kind == "daypart" and item.start_at and item.end_at and not fixed_start:
            _set_lower(patch, "earliest", item.start_at)
            _set_upper(patch, "latest_end", item.end_at)
            patch.setdefault("preferred_window_start", item.start_at.strftime("%H:%M"))
            patch.setdefault("preferred_window_end", item.end_at.strftime("%H:%M"))
            continue

        if item.kind == "point" and item.start_at and not fixed_start:
            clock = item.clock
            approximate = bool(clock and clock.certainty == "approximate")
            if item.optional:
                approximate = True
            if approximate:
                tolerance = max(15, int(clock.tolerance_minutes if clock else 30))
                lower = item.start_at - timedelta(minutes=tolerance)
                upper_start = item.start_at + timedelta(minutes=tolerance)
                _set_lower(patch, "earliest", lower)
                # latest_end is an END bound; only make it hard when duration is known.
                if duration:
                    _set_upper(patch, "latest_end", upper_start + timedelta(minutes=duration))
                patch.setdefault("preferred_window_start", lower.strftime("%H:%M"))
                patch.setdefault("preferred_window_end", upper_start.strftime("%H:%M"))
                continue

            # A bare past clock without a stated date is genuinely ambiguous. Do not
            # silently move it to tomorrow or schedule it in history.
            if item.start_at < now and not _has_explicit_date(doc):
                clarifications.append({
                    "text": source,
                    "reason": (
                        f"{item.evidence.source.strip()} has already passed today. "
                        "Say the intended date (for example, tomorrow) rather than having the scheduler guess."
                    ),
                })
                continue

            existing_exact = _dt(patch.get("exact_start"))
            if existing_exact and abs((existing_exact - item.start_at).total_seconds()) > 60:
                clarifications.append({
                    "text": source,
                    "reason": "Two different exact start times were inferred for the same task; specify one start time.",
                })
                continue
            patch["exact_start"] = item.start_at.isoformat()
            patch["earliest"] = item.start_at.isoformat()
            patch["timing"] = "asap"
            continue

        # The dedicated relative-start layer already owns submission-clock offsets.
        # Named-anchor relationships stay in the IR and existing dependency compilers;
        # they are not converted into arbitrary clocks here.

    if patch.get("exact_start"):
        exact = _dt(patch["exact_start"])
        latest = _dt(patch.get("latest_end"))
        if exact and latest and exact >= latest:
            clarifications.append({
                "text": source,
                "reason": "The exact start is outside the requested latest-finish/window constraint.",
            })


def _temporal_question_like(parsed: dict, unresolved: list[str]) -> bool:
    if not unresolved:
        return False
    questions = " ".join(
        str(x.get("text") or "") + " " + str(x.get("reason") or "")
        for x in (parsed.get("clarifications") or [])
    ).lower()
    if not questions:
        return False
    cues = ("time", "when", "before", "after", "until", "date", "day", "clock", "schedule", "timing")
    return any(cue in questions for cue in cues)


def _merge_semantic_constraints(
    doc: temporal.TemporalDocument,
    additions: list[temporal.TemporalConstraint],
) -> temporal.TemporalDocument:
    if not additions:
        return doc
    rows = [*doc.constraints]
    seen = {
        (
            x.kind, x.evidence.start, x.evidence.end, x.relation,
            x.anchor, x.offset_minutes,
        )
        for x in rows
    }
    for item in additions:
        key = (
            item.kind, item.evidence.start, item.evidence.end, item.relation,
            item.anchor, item.offset_minutes,
        )
        if key not in seen:
            seen.add(key)
            rows.append(item)
    unresolved = []
    for phrase in doc.unresolved:
        low = phrase.casefold()
        if any(
            item.evidence.source.casefold() in low or low in item.evidence.source.casefold()
            for item in additions
        ):
            continue
        unresolved.append(phrase)
    return doc.model_copy(update={
        "constraints": sorted(rows, key=lambda x: (x.evidence.start, x.evidence.end, x.kind)),
        "unresolved": unresolved,
        "confidence": "low" if doc.conflicts else ("medium" if unresolved else "high"),
    })


async def enrich_temporal_review(parsed: dict, text: str, rows: list[dict], config: dict, now: datetime) -> dict:
    out = deepcopy(parsed or {})
    out.setdefault("notes", [])
    out.setdefault("warnings", [])
    out.setdefault("clarifications", [])

    doc = temporal.parse_temporal(text, now)
    semantic_mode = None

    # Use semantic interpretation only for a temporal ambiguity that already needs
    # clarification. Ordinary deterministic prompts never pay for a second model call.
    if _temporal_question_like(out, doc.unresolved):
        from .temporal_semantic import semantic_temporal_fallback
        additions, questions, semantic_mode = await semantic_temporal_fallback(text, doc.unresolved)
        doc = _merge_semantic_constraints(doc, additions)
        for question in questions:
            if not any(question == str(x.get("reason") or "") for x in out["clarifications"]):
                out["clarifications"].append({"text": text, "reason": question})

    for conflict in doc.conflicts:
        if conflict not in out.get("blocking_conflicts", []):
            out.setdefault("blocking_conflicts", []).append(conflict)

    for change in out.get("tasks") or []:
        _compile_change(change, now, out["clarifications"])

    # Exact-source provenance is review/debug information, not a persistent personal
    # rule. Keep it top-level so it expires with the reviewed intake.
    out["temporal_ir"] = doc.model_dump(mode="json")
    out["temporal_review"] = {
        "confidence": doc.confidence,
        "summary": temporal.temporal_summary(doc)[:24],
        "unresolved": doc.unresolved[:8],
        "semantic_relationship_fallback": bool(semantic_mode),
        "timezone": doc.timezone,
    }
    if semantic_mode:
        out["temporal_review"]["semantic_mode"] = semantic_mode

    # Remove duplicate clarifications introduced by stacked legacy wrappers.
    deduped, seen = [], set()
    for item in out.get("clarifications") or []:
        key = (str(item.get("text") or ""), str(item.get("reason") or ""))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    out["clarifications"] = deduped
    return out


def _persist(parsed: dict, text: str, rows: list[dict], config: dict) -> None:
    if not parsed.get("preview_id") or not parsed.get("expires_at"):
        return
    contract.set_kv(
        "intake_review",
        json.dumps({
            "text_hash": contract._fingerprint(text),
            "snapshot": contract._snapshot(rows, config),
            "expires_at": parsed["expires_at"],
            "parsed": parsed,
        }),
    )


def install_temporal_intake_patch() -> None:
    global _INSTALLED
    if _INSTALLED or getattr(contract.review_intake, "_temporal_ir_review", False):
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        parsed = await base(text, rows, config, stamp)
        enriched = await enrich_temporal_review(parsed, text, rows, config, stamp)
        _persist(enriched, text, rows, config)
        return enriched

    review._temporal_ir_review = True
    contract.review_intake = review


__all__ = ["enrich_temporal_review", "install_temporal_intake_patch"]
