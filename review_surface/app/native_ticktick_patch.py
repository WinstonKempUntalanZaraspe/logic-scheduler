from __future__ import annotations

"""Native TickTick task/event creation for reviewed Quick Dump requests.

This layer deliberately NEVER reads, creates, updates, completes, or deletes NOTE
items. It enriches reviewed task changes with native TickTick fields (content, due
date, reminders, repeat rules, checklist items and true child tasks) while keeping
all scheduling writes behind the existing review/apply boundary.
"""

import contextvars
import json
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime

from fastapi import HTTPException

from .config import settings
from .db import get_kv, set_kv
from .models import ACTIONABLE_KINDS
from . import quickdump as qd
from .ticktick import TickTickClient


_ACTIVE_BATCH: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "autoscheduler_native_ticktick_batch", default=None
)
_INSTALLED = False

_META_PREFIX = re.compile(
    r"^(description|details?|notes?|tags?|subtasks?|steps?|checklist|reminders?|repeat|recurrence|due)\s*:\s*(.*)$",
    re.I,
)
_CREATE_EVENT = re.compile(r"^(?:please\s+)?(?:add|create)\s+(?:an?\s+)?event\s*:?\s*(.+)$", re.I)
_CREATE_NOTE = re.compile(r"^(?:please\s+)?(?:add|create|update|edit)\s+(?:an?\s+)?note\b", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _strip_bullet(value: str) -> str:
    return re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", str(value or "")).strip()


def _duration_title(value: str) -> tuple[str, int | None]:
    duration = qd._extract_duration(value)
    title = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        "",
        value,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" ,;:-")
    return title or value.strip(), duration


def _split_items(value: str) -> list[str]:
    value = str(value or "").strip()
    if not value:
        return []
    if ">" in value:
        parts = re.split(r"\s*>\s*", value)
    elif re.search(r"\bthen\b", value, re.I):
        parts = re.split(r"\s*\bthen\b\s*", value, flags=re.I)
    elif ";" in value:
        parts = re.split(r"\s*;\s*", value)
    else:
        parts = re.split(r"\s*,\s*", value)
    return [re.sub(r"^(?:and\s+)", "", p, flags=re.I).strip(" .") for p in parts if p.strip(" .")]


def _repeat_flag(text: str) -> str | None:
    # New recurrence grammar is normalized in one place. The legacy parser below
    # remains as a compatibility fallback for any syntax not yet migrated.
    try:
        from .temporal_engine import recurrence_rrule
        rule = recurrence_rrule(text)
        if rule:
            return rule
    except Exception:
        pass
    low = str(text or "").lower()
    interval = re.search(r'\b(?:every|each)\s+([1-9]\d*)\s+(days?|weeks?|months?|years?)\b', low)
    if interval:
        freq = {'d': 'DAILY', 'w': 'WEEKLY', 'm': 'MONTHLY', 'y': 'YEARLY'}[interval.group(2)[0]]
        return f'RRULE:FREQ={freq};INTERVAL={int(interval.group(1))}'
    if re.search(r"\b(?:every|each)\s+day\b|\bdaily\b", low):
        return "RRULE:FREQ=DAILY;INTERVAL=1"
    if re.search(r"\b(?:every|each)\s+weekday\b|\bweekdays\b", low):
        return "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR"
    weekdays = {
        "monday": "MO", "mon": "MO", "tuesday": "TU", "tue": "TU",
        "wednesday": "WE", "wed": "WE", "thursday": "TH", "thu": "TH",
        "friday": "FR", "fri": "FR", "saturday": "SA", "sat": "SA",
        "sunday": "SU", "sun": "SU",
    }
    day_names = '|'.join(sorted(weekdays, key=len, reverse=True))
    multi = re.search(rf'\b(?:every|each)\s+((?:{day_names})(?:\s*(?:,|and|&)\s*(?:{day_names}))*)\b', low)
    if multi:
        days = list(dict.fromkeys(weekdays[d] for d in re.findall(rf'\b({day_names})\b', multi.group(1))))
        return 'RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=' + ','.join(days)
    for name, code in weekdays.items():
        if re.search(rf"\b(?:every|each)\s+{name}\b", low):
            return f"RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY={code}"
    if re.search(r"\b(?:every|each)\s+week\b|\bweekly\b", low):
        return "RRULE:FREQ=WEEKLY;INTERVAL=1"
    if re.search(r"\b(?:every|each)\s+month\b|\bmonthly\b", low):
        return "RRULE:FREQ=MONTHLY;INTERVAL=1"
    if re.search(r"\b(?:every|each)\s+year\b|\byearly\b|\bannually\b", low):
        return "RRULE:FREQ=YEARLY;INTERVAL=1"
    return None


def _reminder_triggers(text: str) -> list[str]:
    low = str(text or "").lower()
    if not re.search(r"\bremind(?:er)?s?\b", low):
        return []
    triggers = []
    if re.search(r"\b(?:on time|at time|when due|at due)\b", low):
        triggers.append("TRIGGER:PT0S")
    matches = re.finditer(
        r"\b(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m)\s+before\b",
        low,
    )
    for m in matches:
        amount, unit = float(m.group(1)), m.group(2)
        minutes = max(0, round(amount * (1440 if unit.startswith('d') else 60 if unit.startswith('h') else 1)))
        if not minutes:
            triggers.append('TRIGGER:PT0S')
            continue
        days, rem = divmod(minutes, 1440)
        hours, mins = divmod(rem, 60)
        triggers.append(f"TRIGGER:-P{days}DT{hours}H{mins}M0S")
    return list(dict.fromkeys(triggers))


def _explicit_tags(*texts: str) -> list[str]:
    tags: list[str] = []
    for text in texts:
        for tag in re.findall(r"(?<!\w)#([A-Za-z0-9_-]{1,40})", str(text or "")):
            tags.append(tag.lower())
        m = re.search(r"\btags?\s*:\s*([^\n]+)", str(text or ""), re.I)
        if m:
            for raw in re.split(r"[,;]", m.group(1)):
                tag = re.sub(r"^#", "", raw.strip()).lower().replace(" ", "-")
                if re.fullmatch(r"[a-z0-9_-]{1,40}", tag):
                    tags.append(tag)
    return list(dict.fromkeys(tags))


def _academic_tags(line: str, category: str | None) -> list[str]:
    out: list[str] = []
    if category:
        out.append(re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-"))
    if re.search(r"\b(?:assignment|report|homework|lab report|exam|test|tutorial|lecture|school)\b", line, re.I):
        out.append("school")
    return [x for x in dict.fromkeys(out) if x]


def _priority_upgrade(line: str, current: int) -> int:
    low = str(line or "").lower()
    if re.search(r"\b(?:critical|very important|must do|must finish|urgent|asap)\b", low):
        return 5
    if re.search(r"\b(?:important|medium priority)\b", low):
        return max(current, 3)
    if re.search(r"\blow priority\b", low):
        return 1
    return current


def _collect_detail_blocks(text: str, parsed: dict) -> tuple[dict[str, dict], set[str]]:
    """Bind native metadata lines to the nearest preceding interpreted task."""
    tasks = list(parsed.get("tasks") or [])
    details: dict[str, dict] = defaultdict(lambda: {
        "description": [], "tags": [], "subtasks": [], "checklist": [],
        "reminder": [], "repeat": [], "due": [], "owned_lines": [],
    })
    owned_lines: set[str] = set()
    current_key: str | None = None
    mode: str | None = None

    def task_key_for(line: str) -> str | None:
        n = _norm(line)
        best = None
        best_score = 0
        for index, task in enumerate(tasks):
            tn = _norm(task.get("title") or "")
            ln = _norm(task.get("line") or "")
            score = 0
            if ln and (ln == n or ln in n or n in ln):
                score = 4
            elif tn and tn in n:
                score = 3
            elif tn and len(tn.split()) >= 2 and len(set(tn.split()) & set(n.split())) >= min(2, len(set(tn.split()))):
                score = 2
            if score > best_score:
                best, best_score = str(index), score
        return best

    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            mode = None
            continue
        clean = _strip_bullet(stripped)
        marker = _META_PREFIX.match(clean)
        if marker and current_key is not None:
            kind = marker.group(1).lower()
            value = marker.group(2).strip()
            owned_lines.add(_norm(clean))
            details[current_key]["owned_lines"].append(clean)
            if kind.startswith("description") or kind.startswith("detail") or kind.startswith("note"):
                if value:
                    details[current_key]["description"].append(value)
                mode = "description"
            elif kind.startswith("tag"):
                details[current_key]["tags"].extend(_split_items(value))
                mode = "tags"
            elif kind.startswith("subtask"):
                details[current_key]["subtasks"].extend(_split_items(value))
                mode = "subtasks"
            elif kind.startswith("step"):
                values = _split_items(value)
                if any(qd._extract_duration(v) for v in values) or ">" in value or re.search(r"\bthen\b", value, re.I):
                    details[current_key]["subtasks"].extend(values)
                    mode = "subtasks"
                else:
                    details[current_key]["checklist"].extend(values)
                    mode = "checklist"
            elif kind.startswith("checklist"):
                details[current_key]["checklist"].extend(_split_items(value))
                mode = "checklist"
            elif kind.startswith("reminder"):
                if value:
                    details[current_key]["reminder"].append("reminder " + value)
                mode = "reminder"
            elif kind.startswith("repeat") or kind.startswith("recurrence"):
                if value:
                    details[current_key]["repeat"].append(value)
                mode = "repeat"
            elif kind.startswith("due"):
                if value:
                    details[current_key]["due"].append(value)
                mode = "due"
            continue

        if current_key is not None and mode in {"subtasks", "checklist"} and re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", raw):
            value = _strip_bullet(raw)
            if value:
                details[current_key][mode].append(value)
                owned_lines.add(_norm(value))
                details[current_key]["owned_lines"].append(value)
            continue

        candidate = task_key_for(clean)
        if candidate is not None:
            current_key = candidate
            mode = None
        else:
            mode = None

    return details, owned_lines


def _native_for(change: dict, detail: dict, now: datetime) -> dict:
    line = str(change.get("line") or change.get("title") or "")
    if change.get('title'):
        change['title'] = re.sub(r'\s+', ' ', re.sub(r'(?<!\w)#[A-Za-z0-9_-]{1,40}\b', '', change['title'])).strip()
    meta = change.setdefault("meta_patch", {})
    category = meta.get("category")

    tags = [str(x).strip().lower() for x in change.get("tags_add") or [] if str(x).strip()]
    tags.extend(_explicit_tags(line, *(detail.get("tags") or [])))
    for value in detail.get("tags") or []:
        tag = re.sub(r"^#", "", value.strip()).lower().replace(" ", "-")
        if re.fullmatch(r"[a-z0-9_-]{1,40}", tag):
            tags.append(tag)
    tags.extend(_academic_tags(line, category))
    change["tags_add"] = list(dict.fromkeys(tags))
    change["priority"] = _priority_upgrade(line, int(change.get("priority") or 0))

    description = "\n".join(x.strip() for x in detail.get("description") or [] if x.strip())
    if not description:
        m = re.search(r"\b(?:description|details?|notes?)\s*:\s*(.+)$", line, re.I)
        if m:
            description = m.group(1).strip()

    due_date = meta.get("deadline")
    if detail.get("due"):
        parsed_due = qd._extract_deadline("due " + detail["due"][-1], now)
        if parsed_due:
            due_date = parsed_due.isoformat()
            meta["deadline"] = due_date

    repeat_text = " ".join([line, *(detail.get("repeat") or [])])
    repeat_flag = _repeat_flag(repeat_text)
    reminder_text = " ".join([line, *(detail.get("reminder") or [])])
    reminders = _reminder_triggers(reminder_text)

    checklist_values = list(dict.fromkeys(x for x in detail.get("checklist") or [] if x.strip()))
    checklist = [
        {
            "title": _duration_title(value)[0],
            "status": 0,
            "sortOrder": (index + 1) * 1000,
            "isAllDay": False,
            "timeZone": settings.timezone,
        }
        for index, value in enumerate(checklist_values)
    ]

    subtask_values = list(dict.fromkeys(x for x in detail.get("subtasks") or [] if x.strip()))
    total = int(meta.get("duration_minutes") or meta.get("native_effort_minutes") or 0)
    parsed_subtasks = []
    explicit_sum = 0
    missing = 0
    for value in subtask_values:
        title, duration = _duration_title(value)
        if duration:
            explicit_sum += duration
        else:
            missing += 1
        parsed_subtasks.append({"title": title, "duration_minutes": duration})
    if parsed_subtasks:
        remaining = max(0, total - explicit_sum)
        fallback = max(15, round(remaining / missing)) if missing and remaining else (30 if missing else 0)
        for item in parsed_subtasks:
            if not item["duration_minutes"]:
                item["duration_minutes"] = fallback
        ordered_text = " ".join([*subtask_values, *(detail.get('owned_lines') or [])])
        ordered = bool(
            re.search(r"\b(?:in order|sequentially|one after another|then)\b", line + " " + ordered_text, re.I)
            or ">" in line + " " + ordered_text
        )
        for index, item in enumerate(parsed_subtasks):
            item["order"] = index
            item["after_previous"] = bool(ordered and index)
        # Parent becomes a native project/container; child tasks carry schedulable effort.
        meta["autoschedule"] = False
        meta["splittable"] = False
        meta["native_container"] = True
        meta["native_effort_minutes"] = total or sum(x['duration_minutes'] for x in parsed_subtasks)
        meta["duration_minutes"] = None

    due_without_clock = bool(due_date) and not re.search(
        r"\b(?:due|deadline|by)\b[^\n]*(?:\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm))",
        line + " due " + " ".join(detail.get('due') or []),
        re.I,
    )
    fixed = bool(change.get("fixed_start") and change.get("fixed_end"))
    native_type = "event" if fixed else ("project" if parsed_subtasks else ("checklist" if checklist else "task"))
    native = {
        "type": native_type,
        "content": description or None,
        "content_explicit": bool(description),
        "due_date": due_date if not fixed else None,
        "is_all_day_due": bool(due_without_clock and not fixed),
        "reminders": reminders,
        "repeat_flag": repeat_flag,
        "checklist_items": checklist,
        "subtasks": parsed_subtasks,
        "note_safe": True,
    }
    change["native"] = native
    return native


def _remove_owned_metadata_artifacts(parsed: dict, owned_lines: set[str]) -> None:
    if not owned_lines:
        return
    parent_lines = {_norm(t.get("line") or "") for t in parsed.get("tasks") or []}
    parsed["tasks"] = [
        t for t in parsed.get("tasks") or []
        if _norm(t.get("line") or "") not in owned_lines or _norm(t.get("line") or "") in parent_lines
    ]
    for intent in parsed.get("intents") or []:
        if _norm(intent.get("text") or "") in owned_lines:
            intent["status"] = "compiled"
            intent["native_detail"] = True
    parsed["clarifications"] = [
        q for q in parsed.get("clarifications") or [] if _norm(q.get("text") or "") not in owned_lines
    ]
    parsed["warnings"] = [
        w for w in parsed.get("warnings") or []
        if not any(line and line in _norm(w) for line in owned_lines)
    ]


def _synthesize_explicit_events(text: str, parsed: dict, rows: list[dict], config: dict, now: datetime) -> None:
    known = {_norm(t.get("line") or "") for t in parsed.get("tasks") or []}
    for raw in str(text or "").splitlines():
        line = _strip_bullet(raw)
        match = _CREATE_EVENT.match(line)
        if not match or _norm(line) in known:
            continue
        payload = match.group(1).strip()
        time_range = qd._extract_time_range(payload, now)
        if not time_range:
            parsed.setdefault("clarifications", []).append({
                "text": line,
                "reason": "A TickTick event needs an explicit time range so AutoScheduler can protect it without guessing.",
            })
            parsed.setdefault("warnings", []).append(
                "A TickTick event needs an explicit time range so AutoScheduler can protect it without guessing. “" + line + "”"
            )
            continue
        candidate = qd.parse_task_candidate(payload + " fixed", rows, config, now)
        for change in candidate.get("tasks") or []:
            existing = next((t for t in parsed.get('tasks') or []
                             if _norm(t.get('title')) == _norm(change.get('title'))
                             and t.get('fixed_start') == change.get('fixed_start')
                             and t.get('fixed_end') == change.get('fixed_end')), None)
            if existing is not None:
                existing['reason'] = 'Native TickTick event'
                existing['intake_kind'] = 'task'
                continue
            change["line"] = line
            change["intake_kind"] = "task"
            change["reason"] = "Native TickTick event"
            parsed.setdefault("tasks", []).append(change)
        parsed.setdefault("notes", []).append("Native TickTick event: " + payload)


def enrich_review_result(text: str, parsed: dict, rows: list[dict], config: dict, now: datetime | None = None) -> dict:
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    result = deepcopy(parsed)

    if any(_CREATE_NOTE.match(_strip_bullet(line)) for line in str(text or "").splitlines()):
        result["tasks"] = [
            t for t in result.get("tasks") or []
            if not _CREATE_NOTE.match(str(t.get("line") or ""))
        ]
        message = "AutoScheduler never creates or edits TickTick NOTE items. Notes stay reference-only."
        if not any(q.get("reason") == message for q in result.get("clarifications") or []):
            result.setdefault("clarifications", []).append({"text": "TickTick NOTE request", "reason": message})
        result.setdefault("warnings", []).append(message)

    _synthesize_explicit_events(text, result, rows, config, now)
    details, owned_lines = _collect_detail_blocks(text, result)
    _remove_owned_metadata_artifacts(result, owned_lines)

    for index, change in enumerate(result.get("tasks") or []):
        # Only task creation/update changes receive native task fields. Action intents
        # (delete/complete/skip/cancel) keep their existing explicit safety path.
        if change.get("action") not in {"create", "update"}:
            continue
        _native_for(change, details.get(str(index), {}), now)

    result["native_ticktick"] = {
        "enabled": True,
        "notes": "never touched",
        "features": ["description", "due", "priority", "tags", "reminders", "repeat", "checklist", "subtasks", "events"],
    }
    result["warnings"] = list(dict.fromkeys(result.get("warnings") or []))
    result["notes"] = list(dict.fromkeys(result.get("notes") or []))
    return result


def _persist_enriched_preview(parsed: dict) -> None:
    raw = get_kv("intake_review")
    if not raw:
        return
    try:
        saved = json.loads(raw)
        if saved.get("parsed", {}).get("preview_id") != parsed.get("preview_id"):
            return
        saved["parsed"] = parsed
        set_kv("intake_review", json.dumps(saved))
    except Exception:
        return


def _activate(parsed: dict) -> None:
    creates = []
    updates = {}
    for change in parsed.get("tasks") or []:
        if change.get("action") == "create":
            creates.append(deepcopy(change))
        elif change.get("action") == "update" and change.get("task_id"):
            updates[str(change["task_id"])] = deepcopy(change)
    _ACTIVE_BATCH.set({"creates": creates, "updates": updates})


def _active_update(task_id: str) -> dict | None:
    batch = _ACTIVE_BATCH.get() or {}
    return (batch.get("updates") or {}).get(str(task_id))


def _active_create(title: str) -> dict | None:
    batch = _ACTIVE_BATCH.get() or {}
    rows = batch.get("creates") or []
    target = _norm(title)
    for index, change in enumerate(rows):
        if _norm(change.get("title") or "") == target:
            return rows.pop(index)
    return None


async def _assert_task_project(tt: TickTickClient, project_id: str) -> None:
    projects = await tt.projects()
    project = next((p for p in projects if str(p.get("id") or "") == str(project_id)), None)
    if not project:
        raise HTTPException(409, "The TickTick destination list no longer exists. Refresh and interpret again.")
    kind = str(project.get("kind") or "TASK").upper()
    if kind == "NOTE":
        raise HTTPException(400, "AutoScheduler never writes to TickTick NOTE lists.")
    if kind != "TASK":
        raise HTTPException(400, "AutoScheduler only writes native TickTick TASK lists.")


def _assert_actionable_task(task) -> None:
    kind = str(getattr(task, "kind", "TEXT") or "TEXT").upper()
    if kind == "NOTE" or kind not in ACTIONABLE_KINDS:
        raise HTTPException(400, "AutoScheduler never modifies TickTick NOTE/reference items.")


def _format_dt(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return value
    return value.strftime("%Y-%m-%dT%H:%M:%S%z")


def _merged_items(existing: list[dict], additions: list[dict]) -> list[dict]:
    out = [deepcopy(x) for x in existing or []]
    seen = {_norm(x.get("title") or "") for x in out}
    for item in additions or []:
        if _norm(item.get("title") or "") not in seen:
            out.append(deepcopy(item))
            seen.add(_norm(item.get("title") or ""))
    return out


async def _create_children(tt: TickTickClient, parent: dict, project_id: str, column_id: str | None, change: dict, tags: list[str], priority: int) -> list[str]:
    native = change.get("native") or {}
    subtasks = native.get("subtasks") or []
    parent_id = str((parent or {}).get("id") or "")
    if not subtasks or not parent_id:
        return []

    from . import main as main
    created_ids: list[str] = []
    previous_id: str | None = None
    try:
        for subtask in subtasks:
            payload = {
                "projectId": project_id,
                "title": subtask.get("title") or "Untitled subtask",
                "timeZone": settings.timezone,
                "isAllDay": False,
                "priority": priority,
                "tags": [x for x in tags if str(x).lower() != "fixed"],
                "content": "",
                "parentId": parent_id,
            }
            due = native.get("due_date") or (change.get("meta_patch") or {}).get("deadline")
            if due:
                payload["dueDate"] = _format_dt(due)
                payload["isAllDay"] = bool(native.get("is_all_day_due"))
            if column_id:
                payload["columnId"] = column_id
            child = await tt._req("POST", "/task", json=payload)
            child_id = str((child or {}).get("id") or "")
            if not child_id:
                raise RuntimeError("TickTick did not return a child task ID")
            created_ids.append(child_id)
            # Some API versions accept unknown fields silently. Read back a missing
            # link and roll back if TickTick did not create a real nested subtask.
            if str((child or {}).get("parentId") or "") != parent_id:
                linked = await tt.get_task(project_id, child_id) if callable(getattr(tt, 'get_task', None)) else child
                if str((linked or {}).get("parentId") or "") != parent_id:
                    raise HTTPException(409, 'TickTick did not confirm the subtask parent link; native subtask creation failed.')
            child_meta = main._merge_quick_meta({}, {
                "duration_minutes": int(subtask.get("duration_minutes") or 30),
                "confidence": "medium",
                "energy": (change.get("meta_patch") or {}).get("energy", "auto"),
                "splittable": int(subtask.get("duration_minutes") or 30) > 45,
                "autoschedule": True,
                "min_chunk": min(25, int(subtask.get("duration_minutes") or 30)),
                "max_chunk": min(90, max(30, int(subtask.get("duration_minutes") or 30))),
                "category": (change.get("meta_patch") or {}).get("category"),
                "weekly_bucket": (change.get("meta_patch") or {}).get("weekly_bucket"),
                "deadline": due,
                "dependencies": [previous_id] if subtask.get("after_previous") and previous_id else [],
            }, fixed=False)
            main.set_meta(child_id, child_meta)
            previous_id = child_id
        return created_ids
    except Exception as exc:
        cleanup_failed = []
        for child_id in reversed(created_ids):
            try:
                await tt.delete_task(project_id, child_id)
                main.delete_meta(child_id)
            except Exception:
                cleanup_failed.append(child_id)
        try:
            await tt.delete_task(project_id, parent_id)
        except Exception:
            cleanup_failed.append(parent_id)
        if cleanup_failed:
            raise HTTPException(502, 'Task creation failed and TickTick cleanup could not remove: ' + ', '.join(cleanup_failed) + '. Refresh TickTick before retrying.') from exc
        raise


async def _native_create(
    tt: TickTickClient,
    project_id: str,
    title: str,
    start=None,
    end=None,
    *,
    tags=None,
    content="",
    priority=0,
    reminders=None,
    column_id: str | None = None,
    change: dict | None = None,
):
    await _assert_task_project(tt, project_id)
    change = change or _active_create(title)
    native = (change or {}).get("native") or {}
    payload = {
        "projectId": project_id,
        "title": title,
        "timeZone": settings.timezone,
        "isAllDay": bool(native.get("is_all_day_due") and start is None),
        "priority": int(priority or 0),
        "tags": tags or [],
        "content": native.get("content") if native.get("content_explicit") else (content or ""),
        "kind": "CHECKLIST" if native.get("checklist_items") else "TEXT",
    }
    if column_id:
        payload["columnId"] = column_id
    if start is not None:
        payload["startDate"] = _format_dt(start)
    if end is not None:
        payload["dueDate"] = _format_dt(end)
    elif native.get("due_date"):
        payload["dueDate"] = _format_dt(native["due_date"])
    native_reminders = native.get("reminders") or []
    if native_reminders or reminders:
        payload["reminders"] = native_reminders or reminders
    if native.get("repeat_flag"):
        payload["repeatFlag"] = native["repeat_flag"]
    if native.get("checklist_items"):
        payload["items"] = native["checklist_items"]
        payload["desc"] = payload["content"]
    parent = await tt._req("POST", "/task", json=payload)
    if change:
        children = await _create_children(tt, parent or {}, project_id, column_id, change, list(tags or []), int(priority or 0))
        if isinstance(parent, dict) and children:
            parent["_autoscheduler_children"] = children
    return parent


def install_native_ticktick_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from . import intake_contract as contract
    from . import language_intake as intake
    from . import semantic_intake
    from . import main as main
    from . import smart_routing

    base_review = contract.review_intake
    base_apply = contract.apply_reviewed_intake
    base_classify = intake.classify
    base_all_active = TickTickClient.all_active_tasks

    def native_classify(text, *args, **kwargs):
        line = str(text or "").strip()
        if _CREATE_NOTE.match(line):
            return "ambiguous", line
        event = _CREATE_EVENT.match(line)
        if event:
            payload = event.group(1).strip()
            return "task", payload + (" fixed" if "fixed" not in payload.lower() else "")
        return base_classify(text, *args, **kwargs)

    intake.classify = native_classify
    semantic_intake.classify = native_classify

    async def native_review(text, rows, config, now=None):
        parsed = await base_review(text, rows, config, now)
        enriched = enrich_review_result(text, parsed, rows, config, now)
        _persist_enriched_preview(enriched)
        return enriched

    def native_apply(text, rows, config, preview_id, now=None):
        parsed = base_apply(text, rows, config, preview_id, now)
        # Saved previews are enriched, but recompute defensively for old previews after
        # a rolling deploy; deterministic enrichment never changes task identity.
        parsed = enrich_review_result(text, parsed, rows, config, now)
        _activate(parsed)
        return parsed

    contract.review_intake = native_review
    contract.apply_reviewed_intake = native_apply

    async def safe_all_active(self):
        items, projects = await base_all_active(self)
        task_projects = {
            str(p.get("id")) for p in projects
            if str(p.get("kind") or "TASK").upper() == "TASK"
        }
        return [
            t for t in items
            if str(t.project_id) in task_projects and str(t.kind or "TEXT").upper() in ACTIONABLE_KINDS
        ], projects

    TickTickClient.all_active_tasks = safe_all_active

    async def enhanced_update(self, task, *, start=None, end=None, title=None, tags=None, priority=None, content=None):
        _assert_actionable_task(task)
        await self.assert_task_write(task.project_id, task=task)
        change = _active_update(task.id)
        native = (change or {}).get("native") or {}
        payload = self._preserved_payload(task)
        payload["title"] = title if title is not None else task.title
        payload["priority"] = priority if priority is not None else task.priority
        payload["tags"] = tags if tags is not None else task.tags
        payload["content"] = native.get("content") if native.get("content_explicit") else (content if content is not None else task.content)
        payload["desc"] = task.desc
        payload["isAllDay"] = bool(native.get("is_all_day_due")) if start is None and end is None and native.get("due_date") else (bool(task.is_all_day) if start is None and end is None else False)
        if start is not None:
            payload["startDate"] = _format_dt(start)
        elif task.start:
            payload["startDate"] = _format_dt(task.start)
        if end is not None:
            payload["dueDate"] = _format_dt(end)
        elif native.get("due_date"):
            payload["dueDate"] = _format_dt(native["due_date"])
        elif task.end:
            payload["dueDate"] = _format_dt(task.end)
        if native.get("reminders"):
            payload["reminders"] = native["reminders"]
        if native.get("repeat_flag"):
            payload["repeatFlag"] = native["repeat_flag"]
        if native.get("checklist_items"):
            payload["items"] = _merged_items(task.items or [], native["checklist_items"])
            payload["kind"] = "CHECKLIST"
            payload["desc"] = payload["content"]
        return await self._req("POST", f"/task/{task.id}", json=payload)

    async def enhanced_create(self, project_id, title, start=None, end=None, tags=None, content="", priority=0, reminders=None):
        return await _native_create(
            self, project_id, title, start, end,
            tags=tags, content=content, priority=priority, reminders=reminders,
        )

    TickTickClient.update_task = enhanced_update
    TickTickClient.create_task = enhanced_create

    async def routed_native_create(tt, project_id, title, start=None, end=None, *, tags=None, priority=0, column_id=None):
        return await _native_create(
            tt, project_id, title, start, end,
            tags=tags, priority=priority, column_id=column_id,
        )

    smart_routing._create_task_routed = routed_native_create
    main.parse_quick_dump = intake.parse_language


__all__ = ["install_native_ticktick_patch", "enrich_review_result"]
