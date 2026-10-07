from __future__ import annotations

"""Deterministic last-mile intake for ordinary real-life sequencing.

The semantic model is helpful, but it must never be a single point of failure for phrases
such as "Swim after I get back" or "Do Math after Swimming, then Physics".  This layer
resolves those relations against the live task rows, keeps return-home language as context
rather than a fake task, and carries the resulting hard ordering into the planner.
"""

import re
from copy import deepcopy
from datetime import datetime, timedelta

from .config import settings
from . import intelligence_patch as intelligence
from . import contextual_intake_patch as contextual


_BASE_ATTACH = contextual._attach_real_life_bounds

_CONTEXT_RETURN = re.compile(
    r"\b(?:i|we)\s+(?:get|come|arrive|am|are|'m|'re|will\s+be|'ll\s+be)\s+back\b|"
    r"\b(?:get|come|arrive|be)\s+(?:back\s+)?home\b|\breturn(?:ing)?\s+home\b",
    re.I,
)

_NON_WORK = re.compile(
    r"\b(?:template|optimizer|rules?|note|break|reset|reference|info|more info)\b",
    re.I,
)

_CATEGORY_WORDS = {
    "math": {"math", "maths", "calculus", "limit", "limits", "integration", "integral", "algebra", "trig", "trigonometry", "vector", "derivative", "differential"},
    "physics": {"physics", "mechanic", "mechanics", "harmonic", "thermo", "thermodynamics", "wave", "waves", "electricity", "electrical"},
    "swim": {"swim", "swimming", "pool"},
    "bible": {"bible", "scripture", "gospel"},
}


def _norm(value: str | None) -> str:
    s = str(value or "").lower().replace("’", "'")
    s = re.sub(r"\bmaths\b", "math", s)
    s = re.sub(r"\bswimming\b", "swim", s)
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"[^a-z0-9:+/\- ]+", " ", s).strip()


def _clean_phrase(value: str | None) -> str:
    s = _norm(value)
    s = re.sub(r"^(?:after\s+[^,]+,\s*)", "", s)
    s = re.sub(r"^(?:i\s+)?(?:want|need|have)\s+to\s+", "", s)
    s = re.sub(r"^(?:please\s+)?(?:do|study|work\s+on|read|go\s+for\s+a|go|finish|start)\s+", "", s)
    s = re.sub(r"\s+if\s+.*$", "", s)
    s = re.sub(r"\s+(?:today|tonight|tomorrow)\s*$", "", s)
    return s.strip(" ,-:")


def _active(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        try:
            if int(row.get("status") or 0) != 0:
                continue
        except Exception:
            continue
        if not row.get("id") or not str(row.get("title") or "").strip():
            continue
        if str(row.get("kind") or "").upper() == "NOTE":
            continue
        out.append(row)
    return out


def _row_clock(row: dict) -> datetime | None:
    raw = row.get("start") or row.get("start_date") or row.get("startDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=settings.tz)
        return dt.astimezone(settings.tz)
    except Exception:
        return None


def _category(phrase: str) -> str | None:
    words = set(_clean_phrase(phrase).split())
    for name, aliases in _CATEGORY_WORDS.items():
        if words & aliases:
            return name
    return None


def _row_score(row: dict, phrase: str, now: datetime) -> float:
    p = _clean_phrase(phrase)
    title = _norm(row.get("title"))
    if not p or not title:
        return -999.0
    score = 0.0
    if title == p:
        score += 100.0
    elif p in title or title in p:
        score += 30.0

    pwords = set(p.split())
    twords = set(title.split())
    if pwords and twords:
        score += 18.0 * len(pwords & twords) / max(1, len(pwords | twords))

    cat = _category(p)
    if cat:
        aliases = _CATEGORY_WORDS[cat]
        hits = len(twords & aliases)
        if hits:
            score += 18.0 + min(8.0, 2.0 * hits)
        else:
            score -= 15.0

    tags = {str(x).lower() for x in (row.get("tags") or [])}
    if "deep-work" in tags:
        score += 4.0
    if "flexible" in tags:
        score += 2.0
    if "fixed" in tags:
        score += 1.0

    start = _row_clock(row)
    if start:
        delta = (start.date() - now.date()).days
        if delta == 0:
            score += 8.0
        elif delta < 0:
            score += 5.0
        elif delta == 1:
            score += 3.0
        elif delta > 7:
            score -= 2.0

    try:
        score += min(5.0, max(0.0, float(row.get("priority") or 0)))
    except Exception:
        pass

    if _NON_WORK.search(title) and not _NON_WORK.search(p):
        score -= 30.0
    return score


def _resolve_many(phrase: str, rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Resolve a human task reference conservatively but usefully.

    Exact/contained names win.  Generic subjects such as "Math" may resolve to a small
    group of active study blocks (for example two split limits sessions), which is safer
    than inventing a new task or silently ignoring the user's sequence.
    """
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    p = _clean_phrase(phrase)
    if not p or _CONTEXT_RETURN.search(p):
        return []
    rows = _active(rows)
    exact = [r for r in rows if _norm(r.get("title")) == p]
    if exact:
        return exact[:4]

    contained = [r for r in rows if p in _norm(r.get("title"))]
    if len(contained) == 1:
        return contained

    scored = sorted(((_row_score(r, p, now), r) for r in rows), key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 18.0:
        return []
    top = scored[0][0]
    cat = _category(p)
    # Subject-level phrases may legitimately refer to a pair/set of split study blocks.
    if cat:
        chosen = [r for score, r in scored if score >= max(18.0, top - 5.0)][:4]
        return chosen
    if len(scored) > 1 and top - scored[1][0] < 4.0:
        return []
    return [scored[0][1]]


def _ensure_update(result: dict, row: dict) -> dict:
    for item in result.setdefault("tasks", []):
        if str(item.get("task_id") or "") == str(row.get("id") or ""):
            item.setdefault("meta_patch", {})
            return item
    item = {
        "line": "deterministic relationship",
        "title": row.get("title") or "Task",
        "task_id": row.get("id"),
        "project_id": row.get("project_id"),
        "match_score": 1.0,
        "action": "update",
        "priority": int(row.get("priority") or 0),
        "tags_add": [],
        "replace_smart_tags": False,
        "meta_patch": {},
        "fixed_start": None,
        "fixed_end": None,
        "reason": "Deterministic natural-language ordering",
    }
    result["tasks"].append(item)
    return item


def _add_edges(result: dict, targets: list[dict], prereqs: list[dict]) -> None:
    prereq_ids = [str(r.get("id")) for r in prereqs if r.get("id")]
    if not targets or not prereq_ids:
        return
    for target in targets:
        tid = str(target.get("id") or "")
        deps = [x for x in prereq_ids if x and x != tid]
        if not deps:
            continue
        item = _ensure_update(result, target)
        existing = list((target.get("meta") or {}).get("dependencies") or [])
        patch = list(item.setdefault("meta_patch", {}).get("dependencies") or existing)
        for dep in deps:
            if dep not in patch:
                patch.append(dep)
        item["meta_patch"]["dependencies"] = patch
        names = [str(r.get("title")) for r in prereqs if str(r.get("id")) in deps]
        item["reason"] = "Depends on " + ", ".join(names)
        result.setdefault("notes", []).append(
            "Dependency understood: " + str(target.get("title")) + " after " + ", ".join(names)
        )


def _split_then(text: str) -> list[str]:
    return [x.strip(" ,") for x in re.split(r"\s*,?\s*\bthen\b\s+", text, flags=re.I) if x.strip(" ,")]


def _relation_groups(text: str, rows: list[dict], now: datetime | None = None) -> tuple[list[list[dict]], list[str]]:
    """Return ordered task groups plus task ids explicitly gated by returning home."""
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    ordered: list[list[dict]] = []
    after_return: list[str] = []

    for sentence in re.split(r"[.;\n]+", str(text or "")):
        sentence = sentence.strip()
        if not sentence:
            continue

        # "Swim after I get back ..." is a state gate, not a dependency on a fake task.
        m = re.search(r"(.{1,100}?)\s+after\s+(.+)$", sentence, re.I)
        if m and _CONTEXT_RETURN.search(m.group(2)):
            targets = _resolve_many(m.group(1), rows, now)
            for row in targets:
                rid = str(row.get("id") or "")
                if rid and rid not in after_return:
                    after_return.append(rid)
            if targets:
                ordered.append(targets)
            continue

        pieces = _split_then(sentence)
        if not pieces:
            continue
        first = pieces[0]
        left = right = None
        m = re.match(r"(.{1,100}?)\s+after\s+(.{1,100})$", first, re.I)
        if m:
            left, right = m.group(1), m.group(2)
            prereq = _resolve_many(right, rows, now)
            target = _resolve_many(left, rows, now)
            if prereq and target:
                ordered.extend([prereq, target])
            elif target:
                ordered.append(target)
        else:
            # "After X, do Y".  X may be a task or real-life context.
            m = re.match(r"after\s+([^,]+),\s*(.+)$", first, re.I)
            if m:
                prereq = [] if _CONTEXT_RETURN.search(m.group(1)) else _resolve_many(m.group(1), rows, now)
                target = _resolve_many(m.group(2), rows, now)
                if prereq:
                    ordered.append(prereq)
                if target:
                    ordered.append(target)
            else:
                target = _resolve_many(first, rows, now)
                if target:
                    ordered.append(target)

        for piece in pieces[1:]:
            target = _resolve_many(piece, rows, now)
            if target:
                ordered.append(target)

    # Deduplicate immediately repeated groups.
    compact: list[list[dict]] = []
    for group in ordered:
        ids = tuple(str(r.get("id")) for r in group)
        if not ids:
            continue
        if compact and tuple(str(r.get("id")) for r in compact[-1]) == ids:
            continue
        compact.append(group)
    return compact, after_return


def deterministic_explicit_relations(text: str, result: dict, rows: list[dict]) -> None:
    groups, _ = _relation_groups(text, rows)
    if len(groups) >= 2:
        for prereqs, targets in zip(groups, groups[1:]):
            _add_edges(result, targets, prereqs)
        return

    # If there is no deterministic chain, preserve the legacy resolver for ordinary
    # unambiguous "A depends on B" / "A before B" instructions.  Never feed a
    # return-home state phrase into it because that is what produced the old warning.
    if _CONTEXT_RETURN.search(str(text or "")):
        return
    intelligence._LEGACY_EXPLICIT_RELATIONS(text, result, rows)


def deterministic_attach_real_life_bounds(parsed: dict, text: str, rows: list[dict], now: datetime) -> dict:
    parsed = _BASE_ATTACH(parsed, text, rows, now)
    groups, after_return = _relation_groups(text, rows, now)
    if not groups and not after_return:
        return parsed

    ctx = parsed.get("context") or {"date": now.date().isoformat(), "source": "quick-dump"}
    ctx["date"] = now.date().isoformat()
    ctx["source"] = "quick-dump"

    if after_return:
        ctx["after_return_task_ids"] = list(dict.fromkeys([*ctx.get("after_return_task_ids", []), *after_return]))
        titles = [str(r.get("title")) for r in _active(rows) if str(r.get("id")) in set(after_return)]
        if titles:
            parsed.setdefault("notes", []).append("Return-home gate understood: " + ", ".join(titles) + " only after returning home.")

    # A prompt explicitly replanning today makes its resolved task chain a today goal.
    if re.search(r"\b(?:replan|reschedule|rebuild|plan)\s+(?:my\s+)?(?:day|today)\b|\btoday\b", text, re.I):
        chain_ids = []
        chain_titles = []
        for group in groups:
            for row in group:
                rid = str(row.get("id") or "")
                if rid and rid not in chain_ids:
                    chain_ids.append(rid)
                    chain_titles.append(str(row.get("title") or rid))
        if chain_ids:
            ctx["intent_today_ids"] = list(dict.fromkeys([*ctx.get("intent_today_ids", []), *chain_ids]))
            ctx["replan_requested"] = True
            ctx["replan_scope"] = "today"
            ctx["replan_from"] = now.isoformat()
            ctx["intent_exact_order"] = chain_titles
            parsed.setdefault("notes", []).append("Today sequence understood: " + " → ".join(chain_titles))

    parsed["context"] = ctx
    parsed["warnings"] = list(dict.fromkeys(parsed.get("warnings") or []))
    parsed["notes"] = list(dict.fromkeys(parsed.get("notes") or []))
    return parsed


def deterministic_contextual_plan(tasks, meta_map, busy, start, horizon_days, config, mastery_map=None):
    cfg = deepcopy(config or {})
    metas = deepcopy(meta_map or {})
    ctx = cfg.get("_quick_context") or {}
    if ctx.get("date") == start.date().isoformat():
        ids = {str(x) for x in (ctx.get("after_return_task_ids") or [])}
        gate_raw = ctx.get("return_home_not_before") or ctx.get("return_home_estimate") or ctx.get("return_home_not_after")
        if ids and gate_raw:
            try:
                gate = datetime.fromisoformat(str(gate_raw).replace("Z", "+00:00"))
                if gate.tzinfo is None:
                    gate = gate.replace(tzinfo=settings.tz)
                gate = gate.astimezone(settings.tz)
                # A small arrival/reset buffer prevents impossible instant transitions.
                gate += timedelta(minutes=10)
                for tid in ids:
                    raw = metas.setdefault(tid, {})
                    old = raw.get("earliest")
                    old_dt = None
                    if old:
                        try:
                            old_dt = datetime.fromisoformat(str(old).replace("Z", "+00:00"))
                            if old_dt.tzinfo is None:
                                old_dt = old_dt.replace(tzinfo=settings.tz)
                            old_dt = old_dt.astimezone(settings.tz)
                        except Exception:
                            old_dt = None
                    raw["earliest"] = max(gate, old_dt or gate).isoformat()
                    raw["must_finish"] = True
            except Exception:
                pass
    return contextual.contextual_intent_aware_plan(tasks, metas, busy, start, horizon_days, cfg, mastery_map)


def install_deterministic_intake_patch():
    if not hasattr(intelligence, "_LEGACY_EXPLICIT_RELATIONS"):
        intelligence._LEGACY_EXPLICIT_RELATIONS = intelligence._explicit_relations
    intelligence._explicit_relations = deterministic_explicit_relations
    contextual._attach_real_life_bounds = deterministic_attach_real_life_bounds
    return deterministic_contextual_plan


__all__ = [
    "install_deterministic_intake_patch", "deterministic_explicit_relations",
    "deterministic_attach_real_life_bounds", "deterministic_contextual_plan",
]
