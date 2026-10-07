from __future__ import annotations

"""Absolute final invariants for natural-language intake.

Earlier interpreters may be deliberately permissive so unfamiliar positive activities can
be understood.  This final read-only review wrapper enforces two things that must never be
left to fuzzy inference:

1. A NEW durable task needs positive user authority. Reported speech, uncertainty,
   corrections, negative/meta prose and pure availability limits cannot create tasks.
2. Explicit calendar-day words bind the mentioned existing work clause-by-clause.  A
   tomorrow statement can never be pulled into today's intent merely because another
   sentence says "plan today".

The wrapper never creates a task itself. It only removes unauthorized creates and repairs
plan-local date intent for already-existing actionable tasks.
"""

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta

from .config import settings
from . import intake_contract as contract
from . import language_intake as intake
from . import deterministic_intake_patch as deterministic
from . import schedule_instruction_patch as instructions

_INSTALLED = False

_CLOCK = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
_NEGATIVE_META = re.compile(
    r"^(?:i|we)\s+(?:(?:don't|dont|do not|didn't|didnt|did not)\s+want\b|"
    r"(?:changed?|change)\s+(?:my|our)\s+mind\b|"
    r"(?:can't|cannot|won't|wouldn't)\s+promise\b|"
    r"(?:am|'m|are|'re)\s+too\s+.+?\s+to\s+know\b)",
    re.I,
)
_THIRD_PARTY = re.compile(
    r"^(?!i\b|we\b)(?:.{1,80}?)\b(?:told|asked|said|suggested|recommended|wants?|wanted)\b"
    r"(?:.{0,50}\bme\b)?",
    re.I,
)
_AVAILABILITY_ONLY = re.compile(
    rf"^(?:(?:i|we)\s+)?(?:(?:have|need)\s+to\s+|must\s+)?"
    rf"(?:leave|head\s+out|go\s+out)(?:\s+home)?\s+(?:at|by|around)\s+{_CLOCK}\s*$",
    re.I,
)
_HYPOTHETICAL = re.compile(
    r"^(?:if|unless|what\s+if|suppose|for\s+example|example|maybe|perhaps|possibly)\b|"
    r"\b(?:might|may|could|would)\b",
    re.I,
)
_DECLARATIVE_REFERENCE = re.compile(
    r"^(?:actually\s*,?\s*not\b|remember\b)|"
    r"\b(?:is|are)\s+(?:for\s+)?(?:today|tonight|tomorrow|tmr)\b|"
    r"\bcan\s+wait\s+until\s+(?:today|tonight|tomorrow|tmr)\b|"
    r"\b(?:on\s+my\s+mind|not\s+committing|haven't\s+decided|have\s+not\s+decided)\b|"
    r"^(?:i|we)\s+(?:was|were)\s+(?:thinking\s+about|going\s+to)\b|"
    r"^(?:i|we)(?:'m|\s+am|'re|\s+are)\s+considering\b|"
    r"^(?:i|we)\s+need\s+to\s+know\s+whether\b|"
    r"\bprobably\s+won't\b",
    re.I,
)
_BARE_DAY_PLAN = re.compile(
    r"^\s*(?:please\s+)?(?:plan|replan|reschedule|schedule|organize|organise)"
    r"(?:\s+(?:my|the)\s+)?(?:day|schedule|calendar|rest\s+of\s+(?:my|the)\s+day)?"
    r"\s*(?:for\s+|on\s+)?(?:today|tonight|tomorrow|tmr)\s*[.!?]*$",
    re.I,
)
_TOMORROW_PLAN_HEAD = re.compile(
    r"^\s*(?:(?:please\s+)?(?:plan|replan|reschedule|schedule|organize|organise)\b.*\b(?:tomorrow|tmr)\b"
    r"|(?:for\s+)?(?:tomorrow|tmr)\s+(?:only\b|[:,.-]))",
    re.I,
)


def _norm(value) -> str:
    value = str(value or "").replace("’", "'").lower()
    value = re.sub(r"\bmaths\b", "math", value)
    value = re.sub(r"\bswimming\b", "swim", value)
    return re.sub(r"[^a-z0-9:+/\- ]+", " ", value).strip()


def _positive_create_authority(change: dict) -> bool:
    line = str(change.get("line") or change.get("title") or "").strip(" .")
    if not line:
        return False
    low = line.lower().replace("’", "'")
    if _NEGATIVE_META.match(low) or _THIRD_PARTY.match(low) or _AVAILABILITY_ONLY.match(low):
        return False

    # "if I can study coding as well" is a direct optional activity request: the
    # condition controls inclusion/capacity, not whether the sentence is merely a
    # hypothetical. Preserve it. By contrast "If it rains, I might go shopping"
    # remains non-authoritative.
    optional_direct = re.match(r"^if\s+(?:i|we)\s+can\s+(.+)$", low, re.I)
    if optional_direct:
        candidate = optional_direct.group(1).strip(" ,.;:-")
        if intake._ACTION.match(candidate):
            return True

    if low.endswith("?") or _HYPOTHETICAL.search(low):
        return False
    if intake._CREATION.match(line):
        return True

    # Positive first-person spoken action, e.g. "I want to buy toothpaste tomorrow".
    try:
        from .lifelong_intake_patch import _SPOKEN_ACTION_PREFIX
        prefix = _SPOKEN_ACTION_PREFIX.match(line)
    except Exception:
        prefix = None
    if prefix:
        candidate = line[prefix.end():].strip(" ,.;:-")
        if candidate and not _NEGATIVE_META.match(candidate) and not _AVAILABILITY_ONLY.match(candidate):
            from .conversational_activity import is_explicit_activity
            if is_explicit_activity(candidate):
                return True

    # Imperative/direct activity wording, e.g. "Buy toothpaste tomorrow".
    if intake._ACTION.match(line) and not _AVAILABILITY_ONLY.match(line):
        return True

    # The mature parser deliberately supports positive natural fragments inside a day
    # story ("Swimming for two hours", "Coding for 40 minutes"). Do not demand a rigid
    # imperative shape here. The final guard is a deny-list for proven non-authority,
    # not a second task parser.
    if _DECLARATIVE_REFERENCE.search(low):
        return False
    return True


def _source_explicitly_authorizes_create(text: str, change: dict) -> bool:
    """Match a normalized create back to the user's original explicit create clause."""
    title = _norm(change.get("title"))
    line = _norm(change.get("line"))
    for clause, _numbered in intake.clauses(text):
        raw = str(clause or "").strip()
        if not intake._CREATION.match(raw):
            continue
        clause_norm = _norm(raw)
        if line and (line == clause_norm or line in clause_norm or clause_norm in line):
            return True
        if title and len(title) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(title) + r"(?![a-z0-9])", clause_norm):
            return True
    return False


def _create_duplicates_existing(change: dict, rows: list[dict]) -> bool:
    line = _norm(change.get("line"))
    title = _norm(change.get("title"))
    active = _active_rows(rows)
    for row in active:
        rtitle = _norm(row.get("title"))
        if not rtitle:
            continue
        if title == rtitle or (rtitle and re.search(r"(?<![a-z0-9])" + re.escape(rtitle) + r"(?![a-z0-9])", line)):
            return True
        ccat = deterministic._category(str(change.get("title") or change.get("line") or ""))
        rcat = deterministic._category(str(row.get("title") or ""))
        if ccat and rcat and ccat == rcat and len(title.split()) <= 5:
            return True
    return False


def _strip_unauthorized_creates(parsed: dict, original_text: str, rows: list[dict]) -> None:
    kept, blocked = [], []
    for change in parsed.get("tasks") or []:
        if change.get("action") != "create":
            kept.append(change)
            continue
        # Native TickTick enrichment may clean the title/line after the original parser
        # has already established explicit "Add/Create ..." authority. Preserve that
        # authority by tracing the change back to the untouched user source.
        explicit = _source_explicitly_authorizes_create(original_text, change)
        if explicit:
            kept.append(change)
        elif _create_duplicates_existing(change, rows):
            blocked.append(str(change.get("title") or change.get("line") or "unnamed activity"))
        elif _positive_create_authority(change):
            kept.append(change)
        else:
            blocked.append(str(change.get("title") or change.get("line") or "unnamed activity"))
    parsed["tasks"] = kept
    if blocked:
        parsed.setdefault("notes", []).append(
            "Ignored non-authoritative prose that an earlier parser tried to turn into new work: "
            + ", ".join(dict.fromkeys(blocked)) + "."
        )


def _scope_pieces(text: str) -> list[str]:
    pieces = []
    for sentence in re.split(r"[;\r\n]+|(?<=[.!?])\s+(?=[A-Za-z])", str(text or "")):
        sentence = sentence.strip(" ,.;")
        if not sentence:
            continue
        # Split common scope corrections without shredding normal task lists.
        subs = re.split(
            r",\s*(?:and\s+)?(?=(?:remember\b|actually\b|instead\b|(?:today|tomorrow|tmr)\b))",
            sentence,
            flags=re.I,
        )
        pieces.extend(x.strip(" ,.;") for x in subs if x.strip(" ,.;"))
    return pieces


def _strong_date_binding(piece: str) -> bool:
    """Recognize clause-local calendar authority without trusting whole-prompt fuzziness.

    Existing work named in a clause with an explicit day is a local date statement even
    when the user uses shorthand ("Physics tomorrow", "No Gym today"). Questions,
    hypotheticals and uncertainty remain non-authoritative and are left to the normal
    semantic/clarification path.
    """
    low = str(piece or "").strip().lower().replace("’", "'")
    if not re.search(r"\b(?:today|tonight|tomorrow|tmr)\b", low):
        return False
    if low.endswith("?"):
        return False
    if re.search(
        r"^(?:if|unless|what\s+if|suppose|for\s+example|example|maybe|perhaps|possibly)\b"
        r"|\b(?:not\s+sure|haven't\s+decided|have\s+not\s+decided|might|may|could|would)\b"
        r"|\b(?:i|we)\s+(?:think|guess|suspect)\b",
        low,
        re.I,
    ):
        return False
    return bool(
        re.match(r"^(?:for\s+)?(?:today|tonight|tomorrow|tmr)\b", low)
        or re.match(r"^(?:please\s+)?(?:plan|replan|reschedule|schedule|organize|organise)\b", low)
        or re.match(
            r"^(?:actually\s*,?\s*)?(?:(?:not|no)\s+|(?:don'?t|do\s+not)\s+)?"
            r"(?:do|study|swim|go|gym|work\s+on|read|finish|practice|practise|keep|move|skip)\b",
            low,
        )
        or re.match(r"^(?:no\s+|skip\s+|keep\s+|move\s+|remember\b)", low)
        or re.search(r"\b(?:is|are)\s+(?:for\s+)?(?:today|tonight|tomorrow|tmr)\b", low)
        or re.search(r"\bcan\s+wait\s+until\s+(?:today|tonight|tomorrow|tmr)\b", low)
        # Natural shorthand: "Physics tomorrow", "Church tomorrow stays fixed".
        or re.search(
            r"\b(?:today|tonight|tomorrow|tmr)\b"
            r"(?:\s+(?:only|instead|stays?|remains?)\b.*)?\s*$",
            low,
        )
    )


def _piece_day(piece: str, now: datetime):
    low = str(piece or "").lower()
    tomorrow = bool(re.search(r"\b(?:tomorrow|tmr)\b", low))
    today = bool(re.search(r"\b(?:today|tonight)\b", low))
    if tomorrow and not today:
        return now.date() + timedelta(days=1)
    if today and not tomorrow:
        return now.date()
    if tomorrow and today:
        # Prefer the date nearest the actual mentioned activity. This mainly covers
        # constructions such as "not Math today, Math tomorrow instead".
        last_today = max(low.rfind("today"), low.rfind("tonight"))
        last_tomorrow = max(low.rfind("tomorrow"), low.rfind("tmr"))
        return now.date() + timedelta(days=1) if last_tomorrow > last_today else now.date()
    return None


def _active_rows(rows):
    out = []
    for row in rows or []:
        try:
            if int(row.get("status") or 0) != 0:
                continue
        except Exception:
            continue
        if not row.get("id") or str(row.get("kind") or "TASK").upper() == "NOTE":
            continue
        out.append(row)
    return out


def _mentioned_rows(piece: str, rows: list[dict], now: datetime) -> list[dict]:
    value = _norm(piece)
    if not value:
        return []
    found, seen = [], set()
    active = _active_rows(rows)

    # Full title mentions are the strongest evidence.
    padded = " " + value + " "
    for row in active:
        title = _norm(row.get("title"))
        if title and (" " + title + " ") in padded:
            rid = str(row["id"])
            if rid not in seen:
                seen.add(rid); found.append(row)

    # Subject/category mentions may refer to a split task family (Math, Physics, Gym...).
    for category, aliases in deterministic._CATEGORY_WORDS.items():
        alias_hit = any(re.search(r"(?<![a-z0-9])" + re.escape(_norm(alias)) + r"(?![a-z0-9])", value)
                        for alias in aliases if _norm(alias))
        if not alias_hit:
            continue
        for row in active:
            if deterministic._category(str(row.get("title") or "")) != category:
                continue
            rid = str(row["id"])
            if rid not in seen:
                seen.add(rid); found.append(row)

    # Named fixed commitments (e.g. Church) may not belong to a subject category.
    for row in active:
        title = _norm(row.get("title"))
        if title and len(title.split()) <= 4 and re.search(r"(?<![a-z0-9])" + re.escape(title) + r"(?![a-z0-9])", value):
            rid = str(row["id"])
            if rid not in seen:
                seen.add(rid); found.append(row)
    return found


def _negative_for_row(piece: str, row: dict) -> bool:
    low = _norm(piece)
    title = _norm(row.get("title"))
    category = deterministic._category(title)
    names = [title]
    if category:
        names.extend(_norm(x) for x in deterministic._CATEGORY_WORDS.get(category, set()))
    target = "|".join(re.escape(x) for x in names if x)
    if not target:
        return False
    return bool(re.search(
        rf"\b(?:not|no|nothing|don't|dont|don\s+t|do not|skip|without)\b.{{0,35}}(?:{target})"
        rf"|(?:{target}).{{0,35}}\b(?:not|no|nothing|don't|dont|don\s+t|do not|skip|isn't|isn\s+t|is not)\b",
        low,
        re.I,
    ))


def _repair_day_scope(parsed: dict, text: str, rows: list[dict], now: datetime) -> None:
    pieces = _scope_pieces(text)
    if not pieces:
        return
    ctx = deepcopy(parsed.get("context") or {})
    goals = dict(ctx.get("intent_date_goals") or {})
    today_ids = set(str(x) for x in ctx.get("intent_today_ids") or [])
    exclusions = list(ctx.get("intent_exclusions") or [])
    touched = False
    saw_tomorrow = False

    for piece in pieces:
        low_piece = str(piece or "").lower()
        if not _strong_date_binding(piece):
            continue
        # Do not collapse a rich same-sentence narrative that mentions both dates
        # (e.g. "plan today ... I think Church is tomorrow, check again") to whichever
        # date word appears last. The mature day-plan compiler owns that case.
        if re.search(r"\b(?:today|tonight)\b", low_piece) and re.search(r"\b(?:tomorrow|tmr)\b", low_piece):
            continue
        day = _piece_day(piece, now)
        if day is None:
            continue
        if day > now.date():
            saw_tomorrow = True
        mentioned = _mentioned_rows(piece, rows, now)
        for row in mentioned:
            rid = str(row["id"])
            fixed = "fixed" in {str(x).lower().lstrip("#") for x in row.get("tags") or []}
            negative = _negative_for_row(piece, row)
            if day == now.date():
                if negative:
                    today_ids.discard(rid)
                    goals.pop(rid, None)
                    marker = {"task_ids": [rid], "date": day.isoformat()}
                    if marker not in exclusions:
                        exclusions.append(marker)
                elif not fixed:
                    today_ids.add(rid)
                    goals[rid] = day.isoformat()
            else:
                today_ids.discard(rid)
                if not fixed and not negative:
                    goals[rid] = day.isoformat()
                elif negative and goals.get(rid) == day.isoformat():
                    goals.pop(rid, None)
            touched = True

    # A pure tomorrow-plan header needs no named task to establish the horizon.
    if saw_tomorrow or _TOMORROW_PLAN_HEAD.search(str(text or "")):
        ctx["minimum_horizon_days"] = max(2, int(ctx.get("minimum_horizon_days") or 1))
        parsed["minimum_horizon_days"] = max(2, int(parsed.get("minimum_horizon_days") or 1))
    if _TOMORROW_PLAN_HEAD.search(str(text or "")) and not re.search(r"\bplan\b.*\btoday\b", str(text or ""), re.I):
        ctx["explicit_today_scope"] = False
        ctx["replan_requested"] = True
        ctx["replan_scope"] = "tomorrow"
        ctx["replan_from"] = now.isoformat()

    if touched:
        ctx["intent_today_ids"] = sorted(today_ids)
        ctx["intent_date_goals"] = goals
        ctx["intent_exclusions"] = exclusions
    if ctx:
        ctx.setdefault("date", now.date().isoformat())
        ctx.setdefault("source", "quick-dump")
        parsed["context"] = ctx


def _drop_bare_day_phantom_reference(parsed: dict, text: str) -> None:
    if not _BARE_DAY_PLAN.match(str(text or "").strip()):
        return
    bad_fragments = (
        "could not confidently match existing task reference(s): day",
        "could not confidently match existing task reference(s): schedule",
        "name one existing task clearly",
    )
    parsed["clarifications"] = [
        item for item in parsed.get("clarifications") or []
        if not any(fragment in str(item.get("reason") or "").lower() for fragment in bad_fragments)
    ]
    parsed["warnings"] = [
        warning for warning in parsed.get("warnings") or []
        if not any(fragment in str(warning).lower() for fragment in bad_fragments)
    ]


def _persist(text: str, rows: list[dict], config: dict, parsed: dict) -> None:
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


def install_natural_language_safety_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        parsed = await base(text, rows, config, now)
        before = json.dumps(parsed, sort_keys=True, default=str)
        _strip_unauthorized_creates(parsed, text, rows)
        # Final semantic safety: task length alone is allowed, but a create that
        # structurally contains several actions/current-state clauses is not.
        from .task_title_guard import guard_parsed_creates
        guard_parsed_creates(parsed, text)
        _repair_day_scope(parsed, text, rows, now)
        _drop_bare_day_phantom_reference(parsed, text)
        parsed["notes"] = list(dict.fromkeys(parsed.get("notes") or []))
        parsed["warnings"] = list(dict.fromkeys(parsed.get("warnings") or []))
        after = json.dumps(parsed, sort_keys=True, default=str)
        if after != before:
            _persist(text, rows, config, parsed)
        return parsed

    contract.review_intake = review


__all__ = ["install_natural_language_safety_patch"]
