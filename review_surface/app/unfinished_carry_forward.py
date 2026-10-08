from __future__ import annotations

"""Deterministic carry-forward semantics for unfinished work.

Natural examples:
- "I didn't finish all my tasks tdy, reschedule them tmr."
- "I can't finish some of my tasks today, move the unfinished ones to next week."
- "Carry my remaining work to next month."
- "I didn't complete my tasks today; push them to Tuesday."

This layer never creates work. It only re-dates already-active flexible work that can
be proven to belong to today's plan. Fixed commitments, NOTE items, completed/Won't Do
items, recurring series, generated session rows, and autoschedule-off tasks are excluded.

Exact dates become a one-day eligibility window. Calendar ranges (next week/month)
remain real ranges so the optimizer can distribute work instead of dumping everything
onto the first day.
"""

import json
import re
from copy import deepcopy
from datetime import date, datetime

from .config import settings
from . import intake_contract as contract

_INSTALLED = False

_MOVE = re.compile(
    r"\b(?:reschedul(?:e|ed|ing)|move|shift|push|postpon(?:e|ed|ing)|defer|"
    r"carry(?:\s+(?:over|forward))?|put)\b",
    re.I,
)
_UNFINISHED = re.compile(
    r"\b(?:"
    r"(?:did(?:\s+not|n't)|have(?:\s+not|n't)|haven't|can(?:\s+not|'t)|could(?:\s+not|n't)|"
    r"won't|will\s+not|wasn't\s+able\s+to|am\s+not\s+able\s+to)\s+"
    r"(?:finish|complete|do|get\s+through)"
    r"|unfinished|remaining|leftover|left\s+over"
    r")\b",
    re.I,
)
_WORK_NOUN = re.compile(r"\b(?:tasks?|work|things?|assignments?|study|studying)\b", re.I)
_EXPLICIT_CREATE = re.compile(r"\b(?:add|create|make)\s+(?:a\s+)?task\b|\bremind\s+me\s+to\b", re.I)


def _norm(value: str | None) -> str:
    text = str(value or "").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"\btmr\b", "tomorrow", text, flags=re.I)
    text = re.sub(r"\btdy\b", "today", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.tz)
    return parsed.astimezone(settings.tz)


def _intent_window(text: str, now: datetime):
    """Return (start_date, end_date, label) for a genuine unfinished-work move."""
    source = _norm(text)
    if _EXPLICIT_CREATE.search(source):
        return None
    if not _MOVE.search(source) or not _UNFINISHED.search(source) or not _WORK_NOUN.search(source):
        return None

    # The source-side "today" date must not win over the destination. Resolve only
    # text after the final movement verb.
    moves = list(_MOVE.finditer(source))
    tail = source[moves[-1].end():] if moves else ""
    if not tail.strip():
        return None

    from .temporal_engine import find_date_refs
    refs = find_date_refs(tail, now)
    future = [
        ref for ref in refs
        if (ref.end_date or ref.start_date) >= now.date()
    ]
    if not future:
        return None

    # Prefer an explicit destination role ("to Tuesday", "for next week"). If the
    # source-side word "today" also appears after the verb, a genuinely future date
    # wins over that source date. This handles both:
    #   "reschedule the tasks I didn't finish today to tomorrow"
    #   "reschedule tomorrow because I didn't finish today"
    def destination_rank(ref):
        before = tail[max(0, ref.evidence.start - 18):ref.evidence.start]
        explicit_destination = bool(re.search(r"\b(?:to|until|for|on|into)\s*$", before, re.I))
        strictly_future = (ref.end_date or ref.start_date) > now.date()
        return (
            0 if explicit_destination else 1,
            0 if strictly_future else 1,
            ref.evidence.start,
        )

    ref = min(future, key=destination_rank)
    start = ref.start_date
    end = ref.end_date or ref.start_date
    if end < now.date():
        return None
    return start, end, ref.label


def _active_flexible(row: dict) -> bool:
    try:
        status = int(row.get("status") or 0)
    except (TypeError, ValueError):
        return False
    if status != 0:
        return False
    if str(row.get("kind") or "TEXT").upper() == "NOTE":
        return False
    if str(row.get("project_kind") or "TASK").upper() == "NOTE":
        return False
    tags = {str(x).strip().casefold() for x in (row.get("tags") or [])}
    if "fixed" in tags or "autoscheduler-session" in tags:
        return False
    if bool(row.get("is_all_day")):
        return False
    if row.get("repeat_flag") or row.get("repeat"):
        return False
    meta = row.get("meta") or {}
    if meta.get("autoschedule") is False:
        return False
    remaining = meta.get("remaining_minutes")
    if remaining is not None:
        try:
            if int(remaining) <= 0:
                return False
        except (TypeError, ValueError):
            pass
    return bool(row.get("id"))


def _row_dates(row: dict) -> set[date]:
    values = [row.get("start"), row.get("end")]
    meta = row.get("meta") or {}
    values.extend(meta.get(k) for k in ("earliest", "latest_end", "deadline", "hard_stop"))
    return {stamp.date() for value in values if (stamp := _dt(value)) is not None}


def _session_source_ids_today(rows: list[dict], today: date) -> set[str]:
    out = set()
    for row in rows or []:
        tags = {str(x).strip().casefold() for x in (row.get("tags") or [])}
        if "autoscheduler-session" not in tags:
            continue
        start = _dt(row.get("start"))
        if not start or start.date() != today:
            continue
        for line in str(row.get("content") or "").splitlines():
            if line.startswith("AutoSchedulerSource:"):
                source = line.split(":", 1)[1].strip()
                if source:
                    out.add(source)
                break
    return out


def _last_plan_today_ids(today: date) -> set[str]:
    """Use the last reviewed plan only as additional evidence of today's task scope."""
    try:
        raw = contract.get_kv("last_plan")
        payload = json.loads(raw) if raw else {}
    except Exception:
        return set()
    out = set()
    for segment in payload.get("segments") or []:
        start = _dt(segment.get("start"))
        tid = str(segment.get("task_id") or "")
        if tid and start and start.date() == today and not tid.startswith("pi-virtual:"):
            out.add(tid)
    return out


def _prior_today_ids(today: date) -> set[str]:
    try:
        from . import service
        ctx = service.get_quick_context() or {}
    except Exception:
        return set()
    if str(ctx.get("date") or "") != today.isoformat():
        return set()
    out = {str(x) for x in (ctx.get("intent_today_ids") or []) if str(x)}
    for tid, value in (ctx.get("intent_date_goals") or {}).items():
        try:
            if date.fromisoformat(str(value)[:10]) == today:
                out.add(str(tid))
        except (TypeError, ValueError):
            continue
    return out


def _today_unfinished_rows(rows: list[dict], now: datetime) -> list[dict]:
    today = now.date()
    evidence = (
        _session_source_ids_today(rows, today)
        | _last_plan_today_ids(today)
        | _prior_today_ids(today)
    )
    selected = []
    for row in rows or []:
        if not _active_flexible(row):
            continue
        rid = str(row.get("id"))
        if today in _row_dates(row) or rid in evidence:
            selected.append(row)
    # Stable order: priority first, then title/id. The optimizer still decides slots.
    selected.sort(
        key=lambda row: (
            -int(row.get("priority") or 0),
            str(row.get("title") or "").casefold(),
            str(row.get("id") or ""),
        )
    )
    return selected


def _awake_bounds(day: date, config: dict):
    from . import scheduler
    return scheduler._usable_bounds(day, config or {})


def _remaining_minutes(row: dict) -> int:
    meta = row.get("meta") or {}
    for value in (
        meta.get("remaining_minutes"),
        meta.get("duration_minutes"),
        row.get("duration_minutes"),
    ):
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    start, end = _dt(row.get("start")), _dt(row.get("end"))
    if start and end and end > start:
        return max(1, int((end - start).total_seconds() // 60))
    return 0


def _deadline(row: dict) -> datetime | None:
    return _dt((row.get("meta") or {}).get("deadline"))


def _minimum_deferred_row(candidates: list[dict], target_start: date) -> dict:
    """Pick one safe task that must actually move when the user says "some".

    Prefer leaf work (not a prerequisite for another candidate), no/late deadlines,
    non-must-finish work and lower priority. This makes "some" deterministic while
    leaving the optimizer free to keep as much useful work today as legally fits.
    """
    ids = {str(row.get("id")) for row in candidates}
    dependents = {tid: 0 for tid in ids}
    for row in candidates:
        for dep in (row.get("meta") or {}).get("dependencies") or []:
            dep = str(dep)
            if dep in dependents:
                dependents[dep] += 1

    def rank(row: dict):
        meta = row.get("meta") or {}
        rid = str(row.get("id"))
        deadline = _deadline(row)
        deadline_before_target = bool(deadline and deadline.date() < target_start)
        has_deadline = deadline is not None
        must_finish = bool(meta.get("must_finish"))
        priority = int(row.get("priority") or 0)
        # Larger low-priority work gives the day more useful breathing room.
        remaining = _remaining_minutes(row)
        return (
            1 if deadline_before_target else 0,
            1 if must_finish else 0,
            int(dependents.get(rid, 0)),
            priority,
            1 if has_deadline else 0,
            -remaining,
            str(row.get("title") or "").casefold(),
            rid,
        )

    return min(candidates, key=rank)


def _upsert_update(
    parsed: dict,
    row: dict,
    earliest: datetime,
    latest: datetime,
    *,
    timing: str = "balanced",
    target_start: date | None = None,
    target_end: date | None = None,
    carry_mode: str = "all",
) -> dict:
    rid = str(row.get("id") or "")
    existing = next(
        (
            change for change in parsed.setdefault("tasks", [])
            if change.get("action") == "update" and str(change.get("task_id") or "") == rid
        ),
        None,
    )
    if existing is None:
        existing = {
            "line": "unfinished-work carry-forward",
            "title": str(row.get("title") or "Task"),
            "task_id": rid,
            "project_id": row.get("project_id"),
            "match_score": 1.0,
            "action": "update",
            "priority": int(row.get("priority") or 0),
            "tags_add": [],
            "replace_smart_tags": False,
            "meta_patch": {},
            "fixed_start": None,
            "fixed_end": None,
            "reason": "Carry unfinished work forward",
        }
        parsed["tasks"].append(existing)
    patch = existing.setdefault("meta_patch", {})
    patch["earliest"] = earliest.isoformat()
    patch["latest_end"] = latest.isoformat()
    patch["timing"] = timing
    if target_start is not None and target_end is not None:
        # Persist the real destination independently from the broad preview window.
        # This is essential for "some" overflow: after today's context expires, an
        # old source task may revive only inside the requested future period.
        patch["carry_forward_target_start"] = target_start.isoformat()
        patch["carry_forward_target_end"] = target_end.isoformat()
        patch["carry_forward_mode"] = carry_mode
    existing["reason"] = (
        f"Carry unfinished work into {earliest.date().isoformat()}"
        if earliest.date() == latest.date()
        else f"Carry unfinished work into {earliest.date().isoformat()}–{latest.date().isoformat()}"
    )
    return existing


def _carry_clause(value: str | None) -> bool:
    source = _norm(value)
    return bool(
        source
        and _MOVE.search(source)
        and _UNFINISHED.search(source)
        and _WORK_NOUN.search(source)
    )


def _resolved_carry_piece(value: str | None, full_text: str) -> bool:
    piece = _norm(value).casefold()
    whole = _norm(full_text).casefold()
    if not piece or piece not in whole:
        return False
    # The whole prompt already supplied a destination. A split "I can't finish..."
    # clause is therefore no longer ambiguous, even if the movement verb lives in
    # the following semicolon/new-line clause.
    progress_piece = bool(_UNFINISHED.search(piece) and _WORK_NOUN.search(piece))
    movement_piece = bool(_MOVE.search(piece))
    return progress_piece or movement_piece or _carry_clause(piece)


def _clear_resolved_ambiguity(parsed: dict, full_text: str) -> None:
    parsed["clarifications"] = [
        item for item in (parsed.get("clarifications") or [])
        if not _resolved_carry_piece(
            (item or {}).get("text") if isinstance(item, dict) else str(item),
            full_text,
        )
    ]
    parsed["intents"] = [
        item for item in (parsed.get("intents") or [])
        if not (
            isinstance(item, dict)
            and item.get("status") == "needs-input"
            and _resolved_carry_piece(item.get("text"), full_text)
        )
    ]


def _drop_accidental_control_creates(parsed: dict) -> None:
    kept = []
    for change in parsed.get("tasks") or []:
        if change.get("action") != "create":
            kept.append(change)
            continue
        source = _norm(change.get("line") or change.get("title") or "")
        if _MOVE.search(source) and _UNFINISHED.search(source) and _WORK_NOUN.search(source):
            continue
        kept.append(change)
    parsed["tasks"] = kept


def compile_unfinished_carry_forward(
    parsed: dict,
    text: str,
    rows: list[dict],
    config: dict,
    now: datetime,
) -> dict:
    intent = _intent_window(text, now)
    if not intent:
        return parsed

    target_start, target_end, label = intent
    candidates = _today_unfinished_rows(rows, now)
    out = deepcopy(parsed)
    out.setdefault("notes", [])
    out.setdefault("warnings", [])
    out.setdefault("clarifications", [])
    out.setdefault("intents", [])
    out.setdefault("tasks", [])
    _drop_accidental_control_creates(out)
    _clear_resolved_ambiguity(out, text)

    if not candidates:
        out["warnings"].append(
            "I understood the carry-forward request, but I could not prove which active flexible tasks belonged to today's unfinished plan. "
            "Fixed, completed, Won't Do, NOTE, recurring, all-day and autoschedule-off items were not moved."
        )
        out["warnings"] = list(dict.fromkeys(out["warnings"]))
        return out

    first_start, _ = _awake_bounds(target_start, config)
    _, last_end = _awake_bounds(target_end, config)
    today_start, _ = _awake_bounds(now.date(), config)
    live_today_start = max(now, today_start)

    some_wording = bool(re.search(r"\bsome\b", _norm(text), re.I))
    guaranteed_deferred = _minimum_deferred_row(candidates, target_start) if some_wording else None
    guaranteed_id = str(guaranteed_deferred.get("id")) if guaranteed_deferred else None

    ids = []
    titles = []
    overflow_ids = []
    for row in candidates:
        rid = str(row.get("id"))
        ids.append(rid)
        titles.append(str(row.get("title") or rid))

        if some_wording and rid != guaranteed_id:
            # Keep this work eligible today first. The optimizer may spill it into
            # the requested future period when today's legal capacity is exhausted.
            row_earliest = _dt((row.get("meta") or {}).get("earliest"))
            earliest = max(live_today_start, row_earliest or live_today_start)
            _upsert_update(
                out, row, earliest, last_end,
                timing="asap",
                target_start=target_start,
                target_end=target_end,
                carry_mode="overflow",
            )
            overflow_ids.append(rid)
        else:
            # "all" forces every candidate forward. "some" forces one carefully
            # selected low-risk candidate so the command cannot silently move none.
            _upsert_update(
                out, row, first_start, last_end,
                timing="balanced",
                target_start=target_start,
                target_end=target_end,
                carry_mode="forced",
            )

    ctx = deepcopy(out.get("context") or {})
    ctx.setdefault("date", now.date().isoformat())
    ctx.setdefault("source", "quick-dump")
    ctx.update(
        replan_requested=True,
        replan_from=now.isoformat(),
        replan_scope="today",
        explicit_today_scope=False,
        preserve_unfinished=True,
        catch_up_missed=False,
    )
    windows = ctx.setdefault("intent_date_windows", {})
    for rid in ids:
        overflow = some_wording and rid in overflow_ids
        windows[rid] = {
            "start": (now.date() if overflow else target_start).isoformat(),
            "end": target_end.isoformat(),
            "target_start": target_start.isoformat(),
            "target_end": target_end.isoformat(),
            "mode": "overflow" if overflow else "forced",
            "label": str(label or ""),
            "source": "unfinished-carry-forward",
        }

    # Overflow tasks may remain today or use the requested destination, but never
    # leak into the gap between those periods (e.g. Fri-Sun for "next week").
    if some_wording and overflow_ids and target_start > now.date():
        gap = now.date().fromordinal(now.date().toordinal() + 1)
        while gap < target_start:
            ctx.setdefault("intent_exclusions", []).append({
                "task_ids": list(overflow_ids),
                "date": gap.isoformat(),
            })
            gap = gap.fromordinal(gap.toordinal() + 1)

    plan_earliest = ctx.setdefault("plan_local_earliest", {})
    plan_latest = ctx.setdefault("plan_local_latest_end", {})
    for rid in ids:
        if some_wording and rid in overflow_ids:
            change = next(
                ch for ch in out["tasks"]
                if ch.get("action") == "update" and str(ch.get("task_id")) == rid
            )
            plan_earliest[rid] = change["meta_patch"]["earliest"]
        else:
            plan_earliest[rid] = first_start.isoformat()
        plan_latest[rid] = last_end.isoformat()

    if target_start == target_end:
        goals = ctx.setdefault("intent_date_goals", {})
        for rid in ids:
            if not (some_wording and rid in overflow_ids):
                goals[rid] = target_start.isoformat()

    required_days = max(1, (target_end - now.date()).days + 1)
    interactive_horizon = min(14, required_days)
    ctx["minimum_horizon_days"] = max(
        int(ctx.get("minimum_horizon_days") or 1),
        interactive_horizon,
    )
    out["minimum_horizon_days"] = max(
        int(out.get("minimum_horizon_days") or 1),
        interactive_horizon,
    )
    ctx["unfinished_carry_forward"] = {
        "task_ids": ids,
        "mode": "optimizer-overflow" if some_wording else "all-visible-unfinished-today",
        "minimum_deferred_task_ids": [guaranteed_id] if guaranteed_id else [],
        "overflow_candidate_ids": list(overflow_ids),
        "target_start": target_start.isoformat(),
        "target_end": target_end.isoformat(),
        "range": target_start != target_end,
        "full_window_beyond_interactive_horizon": required_days > 14,
    }
    out["context"] = ctx

    if some_wording:
        forced_title = str(guaranteed_deferred.get("title") or guaranteed_id) if guaranteed_deferred else ""
        out["notes"].append(
            "“Some” means overflow-aware carry-forward: the optimizer keeps useful unfinished work today when it still fits, "
            f"guarantees at least {forced_title or 'one low-risk task'} moves to the requested destination, and may spill more there if needed. "
            "Completed/fixed/reference work was not included."
        )

    if target_start == target_end:
        prefix = "Carry-forward destination" if some_wording else "Carry-forward understood"
        out["notes"].append(
            prefix + ": " + ", ".join(titles)
            + f" → {target_start.isoformat()}. Remaining effort and dependencies are preserved; fixed/recurring/reference items were left untouched."
        )
    else:
        note = (
            "Carry-forward understood: " + ", ".join(titles)
            + f" → {target_start.isoformat()} through {target_end.isoformat()}. "
            "This is a real scheduling window, not a command to stack everything on its first day."
        )
        if required_days > 14:
            note += (
                " The full window is stored now; exact slots beyond the interactive 14-day horizon "
                "will be chosen by the rolling optimizer as that window approaches."
            )
        out["notes"].append(note)

    out["intents"].append({
        "kind": "replan",
        "text": text,
        "status": "compiled",
        "semantic": "unfinished-carry-forward",
        "task_ids": ids,
        "target_start": target_start.isoformat(),
        "target_end": target_end.isoformat(),
    })
    out["notes"] = list(dict.fromkeys(out["notes"]))
    out["warnings"] = list(dict.fromkeys(out["warnings"]))
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


def install_unfinished_carry_forward_intent() -> None:
    global _INSTALLED
    if _INSTALLED or getattr(contract.review_intake, "_unfinished_carry_forward", False):
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        parsed = await base(text, rows, config, stamp)
        enriched = compile_unfinished_carry_forward(parsed, text, rows, config, stamp)
        contract.validate_task_creation(enriched)
        _persist(enriched, text, rows, config)
        return enriched

    review._unfinished_carry_forward = True
    contract.review_intake = review


__all__ = [
    "compile_unfinished_carry_forward",
    "install_unfinished_carry_forward_intent",
]
