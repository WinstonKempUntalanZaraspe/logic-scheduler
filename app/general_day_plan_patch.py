from __future__ import annotations

"""General deterministic day-plan grammar + recurring fixed occurrence awareness.

This layer closes the gap between "plan tomorrow" and ordinary lifelong phrasing such as
"plan today", "plan Monday", or "plan in 3 days".  It is intentionally conservative:

* day-plan language only resolves existing actionable TickTick items;
* NOTE items never participate;
* fixed/recurring events are read from TickTick rather than recreated or moved;
* a weekly/monthly/daily/yearly RRULE is projected onto the requested date using the
  series' real start/end clock and recurrence rule;
* uncertain remembered times ("I think it is 5-6, check again") never override the
  stored fixed/recurring occurrence; and
* known day-plan grammar bypasses optional semantic inference entirely.

The existing tomorrow compiler remains in the chain for backward compatibility.  This
wrapper runs after it and normalizes the final context into one date-general day plan.
"""

import calendar
import re
from copy import deepcopy
from datetime import date, datetime, timedelta, time

from .config import settings
from .models import BusyBlock
from . import contextual_intake_patch as contextual
from . import deterministic_intake_patch as deterministic
from . import final_language_extension as language
from . import language_intake as intake
from . import quickdump as qd
from . import schedule_instruction_patch as instructions
from . import scheduler


_INSTALLED = False
_BASE_CLASSIFY = None
_BASE_ATTACH = None
_BASE_SEMANTIC = None
_BASE_HARD_BUSY = None

_WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DAY_CODE = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

_WHEN = (
    r"today|tonight|tomorrow|tmr|in\s+\d{1,2}\s+days?|"
    r"(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"\d{4}-\d{2}-\d{2}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?|"
    r"\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?"
)
_PLAN_DAY = re.compile(
    rf"^\s*(?:please\s+)?(?:i(?:'d|\s+would)\s+like\s+to\s+|i\s+(?:want|need|have)\s+to\s+)?"
    rf"(?:plan|replan|reschedule|schedule|organize|organise)"
    rf"(?:\s+(?:my\s+)?(?:day|schedule)|\s+the\s+rest\s+of)?\s+(?:for\s+|on\s+)?(?P<when>{_WHEN})\b",
    re.I,
)
_OPTIONAL = re.compile(r"\b(?:maybe|perhaps|possibly|optional|if\s+(?:there(?:'s|\s+is)\s+)?time|if\s+possible)\b", re.I)

_CATEGORY_PATTERNS = {
    "swim": re.compile(r"\b(?:swim|swimming|pool|laps?)\b", re.I),
    "math": re.compile(r"\b(?:math|maths|calculus|integration|integrals?|limits?|algebra|trig(?:onometry)?)\b", re.I),
    "physics": re.compile(r"\b(?:physics|mechanics|thermo(?:dynamics)?|waves?|optics|electromagnetism|shm|harmonic\s+motion)\b", re.I),
    "church": re.compile(r"\b(?:church|mass)\b", re.I),
    "gym": re.compile(r"\b(?:gym|workout|work\s*out|weights?|lifting|strength\s+training)\b", re.I),
    "bible": re.compile(r"\b(?:bible|scripture|gospel|devotional|devotion)\b", re.I),
}
_CATEGORY_QUERY = {
    "swim": "swim", "math": "math", "physics": "physics",
    "church": "church", "gym": "gym", "bible": "bible",
}


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dt(value) -> datetime | None:
    if not value:
        return None
    try:
        out = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=settings.tz)
        return out.astimezone(settings.tz)
    except (TypeError, ValueError):
        return None


def _is_fixed(row) -> bool:
    return "fixed" in {str(x).lower().lstrip("#") for x in (_get(row, "tags", []) or [])}


def _repeat_text(row) -> str:
    return str(
        _get(row, "repeat_flag") or _get(row, "repeatFlag") or _get(row, "repeat") or ""
    ).strip()


def _rrule_parts(row) -> dict[str, str]:
    raw = _repeat_text(row)
    if not raw:
        return {}
    upper = raw.upper().strip()
    if upper.startswith("RRULE:"):
        upper = upper[6:]
    return {
        key.strip(): value.strip()
        for token in upper.split(";") if "=" in token
        for key, value in [token.split("=", 1)]
    }


def _until_date(value: str | None) -> date | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    for fmt in ("%Y%m%d", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def _month_delta(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _rule_matches_without_count(row, target: date) -> bool:
    start = _dt(_get(row, "start") or _get(row, "startDate"))
    parts = _rrule_parts(row)
    if not start or not parts:
        return False
    anchor = start.date()
    if target < anchor:
        return False
    until = _until_date(parts.get("UNTIL"))
    if until and target > until:
        return False
    try:
        interval = max(1, int(parts.get("INTERVAL") or 1))
    except ValueError:
        interval = 1
    freq = parts.get("FREQ", "").upper()
    diff = (target - anchor).days

    if freq == "DAILY":
        return diff % interval == 0

    if freq == "WEEKLY":
        byday = [re.sub(r"^[+-]?\d+", "", x) for x in str(parts.get("BYDAY") or "").split(",") if x]
        allowed = {_DAY_CODE[x] for x in byday if x in _DAY_CODE} or {anchor.weekday()}
        if target.weekday() not in allowed:
            return False
        anchor_week = anchor - timedelta(days=anchor.weekday())
        target_week = target - timedelta(days=target.weekday())
        weeks = (target_week - anchor_week).days // 7
        return weeks >= 0 and weeks % interval == 0

    if freq == "MONTHLY":
        months = _month_delta(anchor, target)
        if months < 0 or months % interval:
            return False
        raw_days = [x for x in str(parts.get("BYMONTHDAY") or "").split(",") if x]
        if not raw_days:
            return target.day == anchor.day
        month_last = calendar.monthrange(target.year, target.month)[1]
        allowed = set()
        for raw in raw_days:
            try:
                n = int(raw)
            except ValueError:
                continue
            allowed.add(n if n > 0 else month_last + n + 1)
        return target.day in allowed

    if freq == "YEARLY":
        years = target.year - anchor.year
        if years < 0 or years % interval:
            return False
        months = {int(x) for x in str(parts.get("BYMONTH") or anchor.month).split(",") if x.isdigit()}
        raw_days = [x for x in str(parts.get("BYMONTHDAY") or anchor.day).split(",") if x]
        days = {int(x) for x in raw_days if re.fullmatch(r"\d{1,2}", x)}
        return target.month in months and target.day in days

    return False


def _recurs_on(row, target: date) -> bool:
    if not _rule_matches_without_count(row, target):
        return False
    parts = _rrule_parts(row)
    try:
        count = int(parts.get("COUNT") or 0)
    except ValueError:
        count = 0
    if count <= 0:
        return True
    start = _dt(_get(row, "start") or _get(row, "startDate"))
    if not start:
        return False
    seen = 0
    cursor = start.date()
    # COUNT is uncommon for lifelong fixed events; bounded scanning keeps malformed
    # decades-old series from becoming an intake performance problem.
    max_days = min(3660, max(0, (target - cursor).days) + 1)
    for offset in range(max_days):
        day = cursor + timedelta(days=offset)
        if _rule_matches_without_count(row, day):
            seen += 1
            if day == target:
                return seen <= count
            if seen >= count:
                return False
    return False


def _occurrence(row, target: date) -> tuple[datetime, datetime, str] | None:
    start = _dt(_get(row, "start") or _get(row, "startDate"))
    end = _dt(_get(row, "end") or _get(row, "endDate"))
    if not start or not end or end <= start:
        return None
    if start.date() == target:
        return start, end, "stored-occurrence"
    if not _recurs_on(row, target):
        return None
    duration = end - start
    projected = datetime.combine(target, start.timetz().replace(tzinfo=None), settings.tz)
    return projected, projected + duration, "recurrence-projection"


def _next_occurrence(row, after: date, days: int = 370) -> tuple[datetime, datetime, str] | None:
    for offset in range(max(1, days + 1)):
        found = _occurrence(row, after + timedelta(days=offset))
        if found:
            return found
    return None


def _materialize(row: dict, target: date) -> dict | None:
    if not _is_fixed(row):
        return dict(row)
    occ = _occurrence(row, target)
    if not occ:
        return None
    start, end, source = occ
    copy = dict(row)
    copy["start"] = start.isoformat()
    copy["end"] = end.isoformat()
    copy["_occurrence_source"] = source
    return copy


def _unique(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for row in rows:
        rid = str(row.get("id") or "")
        if rid and rid not in seen:
            seen.add(rid)
            out.append(row)
    return out


def _matching_series(category: str, rows: list[dict], now: datetime) -> list[dict]:
    active = deterministic._active(rows)
    pattern = _CATEGORY_PATTERNS[category]
    direct = [r for r in active if pattern.search(str(r.get("title") or ""))]
    resolved = deterministic._resolve_many(_CATEGORY_QUERY[category], active, now)
    return _unique([*direct, *resolved])


def _resolve_category(category: str, rows: list[dict], now: datetime, target: date) -> list[dict]:
    candidates = _matching_series(category, rows, now)
    materialized = []
    for row in candidates:
        item = _materialize(row, target)
        if item is not None:
            materialized.append(item)
    # A real fixed occurrence on the requested date is stronger evidence than a flexible
    # task with a similar title.  Flexible siblings remain available for study categories.
    materialized.sort(key=lambda r: (0 if _is_fixed(r) else 1, deterministic._norm(r.get("title"))))
    if category in {"church", "swim", "gym", "bible"}:
        fixed = [r for r in materialized if _is_fixed(r)]
        if fixed:
            return fixed[:1]
    return _unique(materialized[:6])


def _parse_explicit_date(raw: str, now: datetime) -> date | None:
    from .temporal_engine import resolve_date_reference
    ref = resolve_date_reference(raw, now)
    return ref.start_date if ref else None


def _target_day(text: str, now: datetime) -> tuple[date, str, re.Match] | None:
    source = str(text or "")
    match = _PLAN_DAY.search(source)
    if match:
        raw = match.group("when")
        day, label = instructions._day_from_clause(raw, now)
        if day:
            return day, label or raw, match
        exact = _parse_explicit_date(raw, now)
        if exact:
            return exact, exact.isoformat(), match

    # Compatibility fallback for date language that the legacy _PLAN_DAY regex never
    # knew about: "day after tomorrow", "three Tuesdays from now", "Friday after next",
    # DD/MM dates, ordinal weekdays, etc. Return a real regex Match spanning through the
    # temporal phrase because downstream code uses match.end() to find the narrative body.
    head = re.match(
        r"^\s*(?:please\s+)?"
        r"(?:(?:i(?:'d|\s+would)\s+like\s+to|i\s+(?:want|need|have)\s+to)\s+)?"
        r"(?:plan|replan|reschedule|schedule|organize|organise)"
        r"(?:\s+(?:my\s+)?(?:day|schedule)|\s+the\s+rest\s+of)?"
        r"(?:\s+(?:for|on))?\s+",
        source,
        re.I,
    )
    if not head:
        return None
    from .temporal_engine import resolve_date_reference
    tail = source[head.end():]
    ref = resolve_date_reference(tail, now)
    if not ref:
        return None
    absolute_end = head.end() + ref.evidence.end
    span = re.match(rf"(?s)^.{{{absolute_end}}}", source)
    if not span:
        return None
    return ref.start_date, ref.label, span


def classify_day_plan(text, numbered=False, output_section=False, now=None):
    assert _BASE_CLASSIFY is not None
    when = _target_day(str(text or ""), now or datetime.now(settings.tz))
    if when:
        return "replan", text
    bare = _PLAN_DAY.match(str(text or ''))
    if bare and not str(text)[bare.end():].strip(' .,:;-'):
        return 'replan', text
    return _BASE_CLASSIFY(text, numbered, output_section, now)


def _clean_generic_fragment(piece: str) -> str:
    from .day_plan_activities import clean_activity, venue_activity
    from .live_activity import strip_activity_duration
    text = qd._strip_task_modifiers(strip_activity_duration(venue_activity(clean_activity(piece))[0]))
    text = re.split(r"\b(?:i\s+think|check\s+again|already\s+scheduled)\b", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"^\s*(?:maybe|perhaps|possibly)\s+", "", text, flags=re.I)
    text = re.sub(
        r"^\s*(?:(?:i\s*(?:will|'ll|want\s+to|wanna|need\s+to|have\s+to|am\s+going\s+to|'m\s+gonna))|please)\s+",
        "", text, flags=re.I,
    )
    text = re.sub(r"\bafter\s+(?:breakfast|lunch|dinner)\b", "", text, flags=re.I)
    text = re.sub(r"\b(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|evening|night)|at\s+night)\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,.;:-")


def _generic_rows(piece: str, rows: list[dict], now: datetime, target: date) -> list[dict]:
    clean = _clean_generic_fragment(piece)
    if not clean or len(clean) < 2:
        return []
    active = deterministic._active(rows)
    attempts = [clean]
    if re.search(r"\band\b", clean, re.I):
        attempts.extend(x.strip() for x in re.split(r"\band\b", clean, flags=re.I) if x.strip())
    out = []
    for query in attempts:
        for row in deterministic._resolve_many(query, active, now):
            item = _materialize(row, target)
            if item is not None:
                out.append(item)
    return _unique(out[:6])


def _stages(text: str, rows: list[dict], now: datetime, target: date, lead: re.Match) -> dict:
    normalized_text = language._normalize_relative_language(str(text or ""))
    normalized_lead = _PLAN_DAY.search(normalized_text) or lead
    body = normalized_text[normalized_lead.end():].lstrip(" ,;:-")
    from .day_plan_activities import fragments, positive_stage, meal_stage, _FACT, _REST, _BEDTIME, _RELATIVE, _OPTIONAL as conversational_optional
    pieces = fragments(body)
    result = {
        "stages": [], "categories": {k: [] for k in _CATEGORY_PATTERNS}, "mentioned": set(),
        "optional_ids": set(), "night_ids": set(), "after_meal": {}, "church_claim": None,
        "church_series": [], "unresolved": [], 'meal_after': {}, 'unordered_ids': set(), 'not_before': {}, 'meal_rest': {}, 'durations': [],
    }
    pending_meal = None
    activity_pieces = []
    for piece in pieces:
        if not positive_stage(piece) or _FACT.match(piece):
            continue
        meal_info = meal_stage(piece)
        if meal_info:
            meal, anchor = meal_info
            pending_meal = meal[1].lower()
            if anchor:
                anchor_rows = []
                for category, pattern in _CATEGORY_PATTERNS.items():
                    if pattern.search(anchor):
                        found = _resolve_category(category, rows, now, target)
                        result['mentioned'].add(category)
                        result['categories'][category] = _unique([*result['categories'][category], *found])
                        anchor_rows.extend(found)
                        if category == 'church':
                            result['church_series'] = _matching_series(category, rows, now)
                anchor_rows = _unique(anchor_rows or _generic_rows(anchor, rows, now, target))
                if not anchor_rows:
                    result['unresolved'].append(f'{pending_meal} after {anchor}')
                elif not result['stages'] or {r['id'] for r in result['stages'][-1]} != {r['id'] for r in anchor_rows}:
                    result['stages'].append(anchor_rows)
                result['meal_after'][pending_meal] = [str(r['id']) for r in anchor_rows if r.get('id')]
            elif result['stages']:
                result['meal_after'][pending_meal] = [str(r['id']) for r in result['stages'][-1] if r.get('id')]
            continue
        if _REST.match(piece) and pending_meal:
            from .live_activity import remaining_minutes
            result['meal_rest'][pending_meal] = remaining_minutes(piece)
            continue
        from . import language_intake as intake
        if _BEDTIME.match(piece) or intake.classify(piece, now=now)[0] in {'constraint', 'output', 'history', 'state', 'reality', 'task-action', 'clock'}:
            continue
        activity_pieces.append(piece)
        stage_rows = []
        matched_category = False
        optional = bool(_OPTIONAL.search(piece) or conversational_optional.search(piece))
        for category, pattern in _CATEGORY_PATTERNS.items():
            if not pattern.search(piece):
                continue
            matched_category = True
            result["mentioned"].add(category)
            found = _resolve_category(category, rows, now, target)
            result["categories"][category] = _unique([*result["categories"][category], *found])
            stage_rows.extend(found)
            if category == "swim" and re.search(r"\bafter\s+(breakfast|lunch|dinner)\b", piece, re.I):
                meal = re.search(r"\bafter\s+(breakfast|lunch|dinner)\b", piece, re.I).group(1).lower()
                for row in found:
                    result["after_meal"][str(row["id"])] = meal
            if category == "church":
                result["church_claim"] = qd._extract_time_range(piece, now)
                result["church_series"] = _matching_series("church", rows, now)
        if not matched_category:
            stage_rows = _generic_rows(piece, rows, now, target)
            if not stage_rows and _clean_generic_fragment(piece):
                result["unresolved"].append(_clean_generic_fragment(piece))
        stage_rows = _unique(stage_rows)
        from .live_activity import remaining_minutes
        duration = remaining_minutes(_RELATIVE.sub('', piece))
        flexible_ids = [str(r['id']) for r in stage_rows if r.get('id') and not _is_fixed(r)]
        if duration and flexible_ids:
            result['durations'].append({'task_ids': flexible_ids, 'minutes': duration,
                                        'date': target.isoformat(), 'source': piece})
        from .relative_start import relative_start_minutes
        minutes, _ = relative_start_minutes(piece)
        if minutes is not None and target == now.date():
            for row in stage_rows:
                if not _is_fixed(row):
                    result['not_before'][str(row['id'])] = (now + timedelta(minutes=minutes)).isoformat()
        if pending_meal:
            for row in stage_rows:
                if not _is_fixed(row):
                    result['after_meal'][str(row['id'])] = pending_meal
            pending_meal = None if stage_rows else pending_meal
        if re.search(r'\b(?:as\s+well|fit\b.*\bin\b|squeeze\b.*\bin\b)\b', piece, re.I):
            result['unordered_ids'].update(str(r['id']) for r in stage_rows)
        if optional:
            result["optional_ids"].update(str(r["id"]) for r in stage_rows if r.get("id") and not _is_fixed(r))
        if re.search(r"\b(?:night|tonight|evening)\b", piece, re.I):
            result["night_ids"].update(str(r["id"]) for r in stage_rows if r.get("id") and not _is_fixed(r))
        if stage_rows:
            result["stages"].append(stage_rows)

    # Recover explicit category words if punctuation made the narrative one large piece.
    full = deterministic._norm(' '.join(activity_pieces))
    for category, pattern in _CATEGORY_PATTERNS.items():
        if category in result["mentioned"] or not pattern.search(full):
            continue
        result["mentioned"].add(category)
        found = _resolve_category(category, rows, now, target)
        result["categories"][category] = found
        if found:
            result["stages"].append(found)
        if category == "church":
            result["church_series"] = _matching_series("church", rows, now)
            result["church_claim"] = qd._extract_time_range(body, now)
    return result


def _drop_false_plan_question(parsed: dict, text: str) -> None:
    parsed["clarifications"] = [
        item for item in (parsed.get("clarifications") or [])
        if "name one existing task clearly" not in str(item.get("reason") or "").lower()
    ]
    parsed["warnings"] = [
        warning for warning in (parsed.get("warnings") or [])
        if "name one existing task clearly" not in str(warning).lower()
    ]
    for item in parsed.get("intents") or []:
        if item.get("status") == "needs-input" and deterministic._norm(item.get("text")) == deterministic._norm(text):
            item["kind"] = "replan"
            item["status"] = "compiled"


def _claim_time_differs(claim, actual_start: datetime, actual_end: datetime) -> bool:
    if not claim:
        return False
    c0, c1 = claim
    return (
        (c0.hour, c0.minute) != (actual_start.hour, actual_start.minute)
        or (c1.hour, c1.minute) != (actual_end.hour, actual_end.minute)
    )


def _describe_next(series: dict, target: date) -> str | None:
    nxt = _next_occurrence(series, target)
    if not nxt:
        return None
    start, end, _ = nxt
    return f"{start.strftime('%A %d %b, %H:%M')}–{end.strftime('%H:%M')}"


def attach_general_day_plan(parsed: dict, text: str, rows: list[dict], now: datetime) -> dict:
    assert _BASE_ATTACH is not None
    result = _BASE_ATTACH(parsed, text, rows, now)
    target_info = _target_day(text, now)
    if not target_info:
        return result
    target, label, lead = target_info
    _drop_false_plan_question(result, text)
    info = _stages(text, rows, now, target, lead)
    target_iso = target.isoformat()
    ctx = result.get("context") or {"date": now.date().isoformat(), "source": "quick-dump"}
    ctx.update(date=now.date().isoformat(), source="quick-dump", replan_requested=True, replan_from=now.isoformat())
    ctx["replan_scope"] = "today" if target == now.date() else target_iso
    horizon = max(1, min(14, (target - now.date()).days + 1))
    ctx["minimum_horizon_days"] = max(horizon, int(ctx.get("minimum_horizon_days") or 1))
    result["minimum_horizon_days"] = max(horizon, int(result.get("minimum_horizon_days") or 1))

    date_goals = dict(ctx.get("intent_date_goals") or {})
    local_deps = {str(k): list(v) for k, v in (ctx.get("plan_local_dependencies") or {}).items()}
    local_earliest = dict(ctx.get("plan_local_earliest") or {})
    for tid, value in info['not_before'].items():
        local_earliest[tid] = max(_dt(local_earliest.get(tid)) or _dt(value), _dt(value)).isoformat()
    local_latest = dict(ctx.get("plan_local_latest_end") or {})
    optional = set(str(x) for x in (ctx.get("optional_date_goal_ids") or []))
    optional.update(info["optional_ids"])

    all_stage_rows = _unique([row for stage in info["stages"] for row in stage])
    for row in all_stage_rows:
        rid = str(row.get("id") or "")
        if rid and not _is_fixed(row):
            date_goals[rid] = target_iso

    # Sequence edges only connect flexible work. Fixed events become explicit time windows
    # so a projected recurrence never depends on the series' old anchor date.
    previous_flexible = []
    for stage in info["stages"]:
        if any(_is_fixed(r) for r in stage):
            # The fixed anchor already separates the earlier and later windows.
            # An infeasible morning activity must not strand all evening work.
            previous_flexible = []
        current_flexible = [r for r in stage if not _is_fixed(r) and str(r.get('id')) not in info['unordered_ids']]
        for row in current_flexible:
            rid = str(row.get("id") or "")
            if rid and previous_flexible:
                local_deps[rid] = list(dict.fromkeys([
                    *local_deps.get(rid, []),
                    *[str(r["id"]) for r in previous_flexible if str(r.get("id")) != rid],
                ]))
        # A discretionary stage is not a completion prerequisite for later work.
        # Keep the preceding required stage when "maybe B" or "B or C" is skipped.
        required_flexible = [r for r in current_flexible if str(r.get('id')) not in optional]
        if required_flexible:
            previous_flexible = required_flexible

    for index, stage in enumerate(info["stages"]):
        fixed_rows = [r for r in stage if _is_fixed(r) and _dt(r.get("start")) and _dt(r.get("end"))]
        for fixed in fixed_rows:
            fs, fe = _dt(fixed["start"]), _dt(fixed["end"])
            if not fs or not fe:
                continue
            for earlier in info["stages"][:index]:
                for row in earlier:
                    if not _is_fixed(row) and row.get("id") and str(row['id']) not in info['unordered_ids']:
                        rid = str(row["id"])
                        existing = _dt(local_latest.get(rid))
                        local_latest[rid] = min(existing, fs).isoformat() if existing else fs.isoformat()
            for later in info["stages"][index + 1:]:
                for row in later:
                    if not _is_fixed(row) and row.get("id") and str(row['id']) not in info['unordered_ids']:
                        rid = str(row["id"])
                        existing = _dt(local_earliest.get(rid))
                        local_earliest[rid] = max(existing, fe).isoformat() if existing else fe.isoformat()

    for tid in info["night_ids"]:
        evening = datetime.combine(target, time(19, 0), settings.tz)
        existing = _dt(local_earliest.get(tid))
        local_earliest[tid] = max(existing, evening).isoformat() if existing else evening.isoformat()

    if info["after_meal"]:
        meal_map = dict(ctx.get("after_meal_task_ids") or {})
        meal_map.update(info["after_meal"])
        ctx["after_meal_task_ids"] = meal_map
        result.setdefault("notes", []).append(
            "Relative meal ordering is active; normal post-meal recovery and travel still apply before the activity."
        )
    if info['meal_after']:
        ctx.setdefault('meal_after_task_ids', {}).update(info['meal_after'])
    if info['meal_rest']:
        ctx.setdefault('after_meal_rest_minutes', {}).update(info['meal_rest'])
        for meal, minutes in info['meal_rest'].items():
            minutes = minutes if minutes is not None else 30
            result.setdefault('notes', []).append(f'Requested rest after {meal}: {minutes} minutes' + (' (planning estimate; specify a duration to change it).' if info['meal_rest'][meal] is None else '.'))
    if info['durations']:
        ctx['plan_local_duration_requests'] = info['durations']
        for request in info['durations']:
            names = [str(r['title']) for r in all_stage_rows if str(r['id']) in request['task_ids']]
            result.setdefault('notes', []).append(f"Requested activity time for {target:%a %d %b}: {', '.join(names)} — {request['minutes']} minutes" + (' combined' if len(names) > 1 else '') + '. Travel and preparation are separate; scheduled study does not mark unfinished work complete.')

    # Reconcile Church against the real fixed/recurring series.  This is evidence-driven:
    # the title is not assigned a default weekday/time in code.
    church = (info["categories"].get("church") or [None])[0]
    church_start = _dt(church.get("start")) if church else None
    church_end = _dt(church.get("end")) if church else None
    result["notes"] = [
        n for n in (result.get("notes") or [])
        if "found church, but it is not scheduled tomorrow" not in str(n).lower()
    ]
    result["clarifications"] = [
        item for item in (result.get("clarifications") or [])
        if not ("church" in str(item.get("text") or "").lower() and "could not find" in str(item.get("reason") or "").lower())
    ]
    if church_start and church_end:
        if target == now.date() and church_end <= now:
            result.setdefault("clarifications", []).append({
                "text": "Church",
                "reason": f"The stored Church occurrence today was {church_start.strftime('%H:%M')}–{church_end.strftime('%H:%M')} and has already passed. I did not move the recurring series automatically.",
            })
        else:
            source = "recurring TickTick occurrence" if church.get("_occurrence_source") == "recurrence-projection" else "existing TickTick occurrence"
            result.setdefault("notes", []).append(
                f"Confirmed {source}: Church is {church_start.strftime('%H:%M')}–{church_end.strftime('%H:%M')} on {target.strftime('%A %d %b')}; it remains fixed."
            )
            if _claim_time_differs(info.get("church_claim"), church_start, church_end):
                result["notes"].append(
                    f"Your remembered Church time was checked against TickTick; the stored occurrence is {church_start.strftime('%H:%M')}–{church_end.strftime('%H:%M')} and was not overwritten."
                )
    elif "church" in info["mentioned"]:
        recurring = next((r for r in info["church_series"] if _repeat_text(r)), None)
        if recurring:
            next_text = _describe_next(recurring, target)
            rule = _repeat_text(recurring)
            reason = f"Church is a recurring fixed TickTick event, but its recurrence has no occurrence on {target.strftime('%A %d %b')}."
            if next_text:
                reason += f" The next stored occurrence is {next_text}."
            reason += " I did not invent or move a Church occurrence; say explicitly if you intend a one-off change."
            result.setdefault("clarifications", []).append({"text": "Church", "reason": reason})
            result.setdefault("notes", []).append(f"Recurring Church rule preserved: {rule}")
        else:
            result.setdefault("clarifications", []).append({
                "text": "Church",
                "reason": f"I could not verify an existing Church occurrence on {target.strftime('%A %d %b')}. Nothing was created or moved.",
            })

    if info["unresolved"]:
        result.setdefault("clarifications", []).append({
            "text": "; ".join(info["unresolved"][:4]),
            "reason": "I could not confidently match these plan stages to existing actionable tasks. No duplicate tasks were created.",
        })

    # Explicitly-mentioned known categories that have no matching actionable work remain
    # visible rather than disappearing from the plan. Optional missing work does not block.
    for category in sorted(info["mentioned"] - {"church"}):
        if info["categories"].get(category):
            continue
        if category == "gym" and re.search(r"\b(?:maybe|perhaps|possibly)\b[^.]*\b(?:gym|workout)\b", text, re.I):
            result.setdefault("notes", []).append("Optional Gym was not matched to an existing task; it was skipped instead of invented.")
            continue
        result.setdefault("clarifications", []).append({
            "text": category.title(),
            "reason": f"I could not confidently match the requested {category} stage to an existing actionable task. No duplicate task was created.",
        })

    ctx["intent_date_goals"] = date_goals
    ctx["plan_local_dependencies"] = local_deps
    ctx["plan_local_earliest"] = local_earliest
    ctx["plan_local_latest_end"] = local_latest
    ctx["optional_date_goal_ids"] = sorted(optional)
    if target == now.date():
        today_ids = list(ctx.get("intent_today_ids") or [])
        today_ids.extend(tid for tid, day in date_goals.items() if day == target_iso)
        ctx["intent_today_ids"] = list(dict.fromkeys(today_ids))

    required_ids = [
        str(r["id"]) for r in all_stage_rows
        if r.get("id") and not _is_fixed(r) and str(r["id"]) not in optional
    ]
    plan_payload = {
        "date": target_iso,
        "scope_label": label,
        "required_ids": list(dict.fromkeys(required_ids)),
        "optional_ids": sorted(optional),
        "church_id": str(church.get("id")) if church and church.get("id") else None,
        "church_start": church_start.isoformat() if church_start else None,
        "church_end": church_end.isoformat() if church_end else None,
        "recurrence_verified": bool(church and church.get("_occurrence_source") == "recurrence-projection"),
    }
    ctx["day_plan"] = plan_payload
    # Compatibility alias for mature planner wrappers that were written before day-plan
    # semantics were generalized beyond tomorrow.
    ctx["tomorrow_plan"] = plan_payload
    result["context"] = ctx
    result["notes"] = list(dict.fromkeys(result.get("notes") or []))
    result["warnings"] = list(dict.fromkeys(result.get("warnings") or []))
    return result


def recurring_fixed_hard_busy(tasks, busy, start: datetime, horizon_days: int, config: dict):
    """Expand fixed TickTick recurrence rules when the API row is a series anchor.

    If TickTick already supplies the concrete occurrence for a day, the base hard-busy
    logic owns it.  We only synthesize dates different from the row's stored start date.
    """
    assert _BASE_HARD_BUSY is not None
    out = list(_BASE_HARD_BUSY(tasks, busy, start, horizon_days, config))
    existing = {(b.start, b.end, str(b.label)) for b in out}
    for task in tasks or []:
        if not _is_fixed(task) or not _repeat_text(task) or not _get(task, "is_actionable", True):
            continue
        stored = _dt(_get(task, "start"))
        for offset in range(max(1, int(horizon_days))):
            day = (start + timedelta(days=offset)).date()
            if stored and stored.date() == day:
                continue
            occ = _occurrence(task, day)
            if not occ:
                continue
            os, oe, _ = occ
            key = (os, oe, str(_get(task, "title") or "Recurring fixed event"))
            if key in existing:
                continue
            out.append(BusyBlock(os, oe, key[2], "ticktick-fixed-recurring"))
            existing.add(key)
    return scheduler.merge_busy(out)


def install_general_day_plan_patch() -> None:
    global _INSTALLED, _BASE_CLASSIFY, _BASE_ATTACH, _BASE_SEMANTIC, _BASE_HARD_BUSY
    if _INSTALLED:
        return
    _INSTALLED = True

    _BASE_CLASSIFY = intake.classify
    intake.classify = classify_day_plan
    contextual._BASE_CLASSIFY = classify_day_plan

    _BASE_ATTACH = contextual._attach_real_life_bounds
    contextual._attach_real_life_bounds = attach_general_day_plan

    _BASE_SEMANTIC = contextual.grounded_semantic_document

    async def day_plan_semantic(text, now, rows, config, prior_context):
        if _target_day(text, now):
            # The source-grounded day-plan compiler has live task + recurrence data and
            # does not need a provider for this grammar class.
            return intake.extract_intents(text, now), "local", None, [], []
        return await _BASE_SEMANTIC(text, now, rows, config, prior_context)

    contextual.grounded_semantic_document = day_plan_semantic

    _BASE_HARD_BUSY = scheduler._hard_busy
    scheduler._hard_busy = recurring_fixed_hard_busy


__all__ = [
    "install_general_day_plan_patch", "attach_general_day_plan", "classify_day_plan",
    "recurring_fixed_hard_busy", "_target_day", "_occurrence", "_recurs_on",
]
