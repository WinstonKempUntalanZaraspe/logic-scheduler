from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from . import db
from .config import settings
from .project_intelligence_review import project_capacity, project_capacity_profile, refresh_review

STORE_KEY = "project_intelligence_campaigns_v1"
PREVIEW_PREFIX = "project_intelligence_preview_v1:"
ROLLING_DEFAULT_DAYS = 10
PROJECT_NOUN = re.compile(
    r"\b(?:hackathon|exam|competition|certification|portfolio(?:\s+project)?|research(?:\s+project)?|"
    r"project|contest|challenge|league|olympiad|tournament|deadline|presentation|submission|study\s+plan|preparation)\b", re.I,
)
EXPLICIT_BLUEPRINT = re.compile(
    r"\b(?:prepare\s+me\s+for|build|create|make|design|plan)\b.*\b(?:complete\s+)?"
    r"(?:preparation|study|project|competition|hackathon|exam|certification|roadmap|blueprint|plan)\b",
    re.I | re.S,
)
URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.I)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    value: str
    source: str
    source_type: Literal["website", "pdf", "user_input", "planner_inference"]
    confidence: Literal["VERIFIED", "RECOMMENDED", "OPTIONAL"]


class RubricCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    weight: float = Field(ge=0, le=100)
    source: str
    source_type: Literal["website", "pdf", "user_input", "planner_inference"]
    confidence: Literal["VERIFIED", "RECOMMENDED", "OPTIONAL"]


class MilestoneDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    description: str
    definition_of_done: str


class LearningResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    url: str
    topics: list[str]
    reason: str


class ScopeOption(BaseModel):
    """One mutually-exclusive event track/problem statement/challenge choice."""
    model_config = ConfigDict(extra="forbid")
    key: str
    title: str
    summary: str
    source: str
    source_type: Literal["website", "pdf", "user_input"]
    confidence: Literal["VERIFIED", "RECOMMENDED"]


class WorkPackageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    title: str
    description: str
    estimated_minutes: int = Field(ge=15, le=12000)
    dependencies: list[str]
    phase: str
    learning_stage: Literal["foundation", "guided_practice", "independent_build", "advanced_validation", "delivery", "unspecified"] = "unspecified"
    work_kind: Literal["learning", "technical_practice", "implementation", "testing", "administration", "presentation", "other"] = "other"
    learning_mode: Literal["conceptual", "procedural", "memorisation", "visual_recall", "mixed", "not_applicable"] = "not_applicable"
    retrieval_of: list[str] = Field(default_factory=list)
    review_delay_days: int | None = Field(default=None, ge=0, le=60)
    priority: Literal["critical", "high", "normal", "low"]
    risk: Literal["high", "medium", "low"]
    definition_of_done: str
    rubric_links: list[str]
    concepts: list[str] = Field(default_factory=list)
    worked_example: str = ""
    exercise: str = ""
    self_check: str = ""
    resource_ids: list[str] = Field(default_factory=list)
    requires_contiguous_session: bool = False
    confidence: Literal["VERIFIED", "RECOMMENDED", "OPTIONAL"]


class CampaignDraft(BaseModel):
    _generation: dict = PrivateAttr(default_factory=dict)
    model_config = ConfigDict(extra="forbid")
    campaign_type: Literal[
        "hackathon", "exam", "competition", "certification", "portfolio_project",
        "research_project", "personal_goal", "other",
    ]
    scope_options: list[ScopeOption] = Field(default_factory=list)
    selected_scope: str | None = None
    scope_selection_basis: Literal["user_explicit", "single_option", "not_applicable", "ambiguous"] = "not_applicable"
    goal: str
    deadline: str | None
    presentation_date: str | None
    knowledge_assumption: str
    knowledge_boundary: str | None = None
    assumed_mastered_topics: list[str] = Field(default_factory=list)
    study_topics: list[str] = Field(default_factory=list)
    summary: str
    requirements: list[Evidence]
    deliverables: list[Evidence]
    rules: list[Evidence]
    technical_requirements: list[Evidence]
    datasets: list[Evidence]
    rubric: list[RubricCriterion]
    milestones: list[MilestoneDraft]
    work_packages: list[WorkPackageDraft]
    major_risks: list[str]
    learning_resources: list[LearningResource] = Field(default_factory=list)


class ProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_package_key: str
    progress_percent: int = Field(ge=0, le=100)
    remaining_minutes: int | None = Field(default=None, ge=0, le=12000)
    note: str


def looks_like_project_blueprint_request(text: str) -> bool:
    """Strict opt-in: mentions are not authority, but an explicit sustained learning goal is."""
    value = " ".join(str(text or "").split())
    from .exam_sources import QUALIFICATION
    if not value:
        return False

    explicit_prepare = bool(re.search(r"\bprepare\s+me\s+for\b", value, re.I))
    explicit_learning_goal = bool(re.search(
        r"\b(?:i\s+(?:want|wanna|would\s+like|plan|hope)\s+to\s+(?:learn|master)|teach\s+me)\b",
        value, re.I,
    ))
    explicit_task_command = bool(re.search(
        r"\b(?:add|create|make)\s+(?:a\s+)?task\b|\bremind\s+me\s+to\b",
        value, re.I,
    ))
    recurring_learning = bool(re.search(
        r"\b(?:per\s+day|a\s+day|each\s+day|every\s+day|daily|per\s+week|each\s+week|"
        r"every\s+week|weekly|over\s+the\s+next|for\s+the\s+next|until\s+(?:the\s+)?"
        r"(?:exam|competition|contest|deadline|event)|from\s+(?:zero|scratch)|roadmap|curriculum)\b",
        value, re.I,
    ))
    immediate_only = bool(re.search(
        r"\b(?:today|tonight|this\s+(?:morning|afternoon|evening)|tomorrow)\b",
        value, re.I,
    )) or bool(re.search(
        r"\bfor\s+\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?)\b",
        value, re.I,
    ))

    # "I want to learn X" / "Teach me X" is an explicit request for the WHAT layer
    # unless the user clearly framed it as a one-off task. Repeated/daily study remains
    # a learning campaign even when a per-session duration is supplied.
    if explicit_learning_goal and not explicit_task_command and (recurring_learning or not immediate_only):
        return True

    # A URL plus explicit "prepare me for <named thing>" is enough authority for
    # Project Intelligence even when the target is an acronym (SPhL, IOI, IPhO, etc.)
    # that does not contain a generic noun such as "competition".
    if explicit_prepare and URL_RE.search(value):
        return True
    if not (PROJECT_NOUN.search(value) or QUALIFICATION.search(value)):
        return False
    if explicit_prepare:
        return True
    return bool(EXPLICIT_BLUEPRINT.search(value) and re.search(
        r"\b(?:plan|roadmap|blueprint|prepare|preparation|study|from\s+scratch|assume\s+i\s+know\s+nothing)\b",
        value, re.I,
    ))


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(x.rstrip(".,;:!?") for x in URL_RE.findall(str(text or ""))))[:4]


def load_store() -> dict[str, dict]:
    try:
        value = json.loads(db.get_kv(STORE_KEY, "{}") or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_store(value: dict[str, dict]) -> None:
    db.set_kv(STORE_KEY, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def list_campaigns() -> list[dict]:
    rows = list(load_store().values())
    rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return rows


def get_campaign(campaign_id: str) -> dict | None:
    return load_store().get(str(campaign_id))


def save_campaign(campaign: dict) -> None:
    value = load_store()
    value[str(campaign["id"])] = campaign
    save_store(value)


def _preview_key(preview_id: str) -> str:
    return PREVIEW_PREFIX + str(preview_id)


def save_preview(preview: dict) -> None:
    db.set_kv(_preview_key(preview["preview_id"]), json.dumps(preview, ensure_ascii=False))


def load_preview(preview_id: str | None, text: str) -> dict:
    if not preview_id:
        raise HTTPException(409, "Interpret this project request first.")
    raw = db.get_kv(_preview_key(preview_id))
    if not raw:
        raise HTTPException(409, "That project preview is no longer available. Interpret again.")
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise HTTPException(409, "That project preview is invalid. Interpret again.") from exc
    if float(value.get("expires_at_epoch") or 0) <= time.time():
        raise HTTPException(409, "That project preview expired. Interpret again before applying.")
    if value.get("request_hash") != hashlib.sha256(text.encode()).hexdigest():
        raise HTTPException(409, "The project request changed. Interpret again before applying.")
    return value


def consume_preview(preview_id: str) -> None:
    db.set_kv(_preview_key(preview_id), "")


def date_value(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except Exception:
        return None


def spaced_review_ready_at(campaign: dict, package: dict) -> datetime | None:
    """Return the first local datetime a spaced-retrieval package may run.

    review_delay_days is a calendar-day delay after every referenced retrieval source
    has actually been completed.  A review with missing/incomplete sources is not ready.
    This deliberately uses completion evidence rather than optimistic roadmap dates.
    """
    try:
        delay = int(package.get("review_delay_days") or 0)
    except (TypeError, ValueError):
        delay = 0
    if delay <= 0:
        return None
    refs = [str(x) for x in package.get("retrieval_of") or [] if str(x).strip()]
    if not refs:
        return None
    by_key = {str(p.get("key")): p for p in campaign.get("work_packages") or []}
    completed_days = []
    for key in refs:
        source = by_key.get(key)
        if not source or source.get("status") != "done" or not source.get("completed_at"):
            return None
        try:
            stamp = datetime.fromisoformat(str(source["completed_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=settings.tz)
            completed_days.append(stamp.astimezone(settings.tz).date())
        except (TypeError, ValueError):
            return None
    ready_day = max(completed_days) + timedelta(days=delay)
    return datetime.combine(ready_day, datetime.min.time(), tzinfo=settings.tz)


def topological(packages: list[dict]) -> list[str]:
    by_key = {p["key"]: p for p in packages}
    if len(by_key) != len(packages):
        raise HTTPException(422, "Project blueprint contains duplicate work-package keys.")
    for p in packages:
        bad = [d for d in p.get("dependencies", []) if d not in by_key or d == p["key"]]
        if bad:
            raise HTTPException(422, f"Project blueprint has invalid dependencies for {p['title']}.")
    visiting, visited, order = set(), set(), []

    def visit(key: str):
        if key in visiting:
            raise HTTPException(422, "Project blueprint contains a dependency cycle; nothing was saved.")
        if key in visited:
            return
        visiting.add(key)
        for dep in by_key[key].get("dependencies", []):
            visit(dep)
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    for key in by_key:
        visit(key)
    return order


def _rubric_weight(package: dict, rubric: list[dict]) -> float:
    links = {str(x).strip().casefold() for x in package.get("rubric_links", []) if str(x).strip()}
    total = 0.0
    for row in rubric:
        criterion = str(row.get("criterion") or "").strip().casefold()
        if links and any(link in criterion or criterion in link for link in links):
            total += float(row.get("weight") or 0)
    return min(100.0, total)


def _validate_provenance(raw: dict, docs: list[dict]) -> None:
    available = {str(d.get("source_type") or "") for d in docs}
    for option in raw.get("scope_options") or []:
        source_type = str(option.get("source_type") or "")
        if source_type in {"website", "pdf"} and source_type not in available:
            raise HTTPException(422, "Project Intelligence claimed a source-backed scope option without that source being available.")
    for section in ("requirements", "deliverables", "rules", "technical_requirements", "datasets", "rubric"):
        for item in raw.get(section) or []:
            source_type = str(item.get("source_type") or "")
            confidence = str(item.get("confidence") or "")
            if source_type == "planner_inference" and confidence == "VERIFIED":
                raise HTTPException(422, "Project Intelligence provenance validation failed; an inference was marked VERIFIED.")
            if source_type in {"website", "pdf"} and source_type not in available:
                raise HTTPException(422, "Project Intelligence claimed a source-backed fact without that source being available.")


def _source_scope_markers(docs: list[dict]) -> list[int]:
    """Conservative independent cross-check for numbered mutually-exclusive scopes.

    This is intentionally narrower than model reasoning: it catches common PS1/PS2,
    "Problem Statement 1/2", and "Track 1/2" structures without trying to infer named
    tracks from arbitrary prose. Repeated mentions of the same number count once.
    """
    markers = set()
    patterns = (
        r"\bproblem\s+statements?\s*(?:(?:no\.?|number)\s*)?[#:]?\s*([1-9]\d?)\b",
        r"\bps\s*[-#:]?\s*([1-9]\d?)\b",
        r"\btrack\s*(?:(?:no\.?|number)\s*)?[#:]?\s*([1-9]\d?)\b",
    )
    for doc in docs or []:
        if _historical_problem_source(doc):
            continue
        text = str(doc.get("text") or "")
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                try:
                    markers.add(int(match.group(1)))
                except (TypeError, ValueError):
                    continue
    return sorted(markers)


def _historical_problem_source(doc: dict) -> bool:
    """Past problem packets calibrate difficulty, not current participation choices."""
    from urllib.parse import urlparse
    return bool(re.search(r"/(?:archives?|past[-_/]?(?:papers?|problems?))(?:/|$)",
                          urlparse(str(doc.get("source") or "")).path, re.I))


def _source_named_track_markers(docs: list[dict]) -> list[str]:
    """Conservative cross-check for explicitly named "... Track" alternatives.

    Count the immediate source-visible label before "Track". Using only the final
    token avoids swallowing surrounding prose while still distinguishing common
    alternatives such as Competitive/Casual or Learning/Optimisation tracks.
    """
    labels = set()
    for doc in docs or []:
        if _historical_problem_source(doc):
            continue
        text = str(doc.get("text") or "")
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9&/+.-]{1,32})\s+Tracks?\b", text, re.I):
            label = match.group(1).strip(" -:/").casefold()
            # Reject generic prose/headings that merely happen to precede
            # "track(s)" (e.g. "Two Tracks", "competition rounds track ...").
            # Keep source-visible option names such as Competitive/Casual/Learning.
            if label not in {
                "the", "this", "each", "both", "all", "same",
                "one", "two", "three", "four", "five", "new",
                "round", "rounds", "competition", "different", "multiple",
                "available", "separate", "respective",
                "and", "or", "another", "other", "some", "any",
            }:
                labels.add(label)
    return sorted(labels)


def preflight_source_scope_choice(request_text: str, docs: list[dict]) -> None:
    """Require a source-visible scope choice before expensive blueprint generation."""
    text = " ".join(str(request_text or "").casefold().split())
    numbered = _source_scope_markers(docs)
    named = _source_named_track_markers(docs)

    if len(numbered) > 1:
        hits = [
            number for number in numbered
            if (
                re.search(rf"\bps\s*[-#:]?\s*{number}\b", text, re.I)
                or re.search(rf"\bproblem\s+statement\s*(?:no\.?\s*)?{number}\b", text, re.I)
                or re.search(rf"\btrack\s*(?:no\.?\s*)?{number}\b", text, re.I)
            )
        ]
        if len(set(hits)) != 1:
            choices = ", ".join(f"PS{x}" for x in numbered)
            raise HTTPException(409, "Choose one problem statement/track before preparation is generated: " + choices)

    if len(named) > 1:
        hits = []
        for label in named:
            direct = f"{label} track" in text or f"track {label}" in text
            selected = bool(
                re.search(rf"\b(?:choose|select|pick|compete\s+in|enter)\s+(?:the\s+)?{re.escape(label)}\b", text, re.I)
            )
            if direct or selected:
                hits.append(label)
        if len(set(hits)) != 1:
            choices = ", ".join(x.title() + " Track" for x in named)
            raise HTTPException(409, "Choose one track before track-specific preparation is generated: " + choices)


def _scope_selection_is_explicit(request_text: str, options: list[dict], selected_key: str | None) -> bool:
    """Verify that a multi-track choice came from the user, not the planning model."""
    if not selected_key:
        return False
    selected = next((o for o in options if str(o.get("key") or "").casefold() == str(selected_key).casefold()), None)
    if not selected:
        return False
    text = " ".join(str(request_text or "").casefold().split())
    key = str(selected.get("key") or "").casefold().strip()
    title = " ".join(str(selected.get("title") or "").casefold().split()).strip()
    if key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
        return True
    if title and title in text:
        return True
    # Natural choices such as "I choose the machine-learning challenge" may omit
    # the site's short identifier. Require an explicit selection verb plus at
    # least two distinctive title words so a generic topic mention cannot count.
    if re.search(r"\b(?:choose|chose|select|selected|take|taking|do|doing|work on|working on|pick|picked)\b", text):
        stop = {"the","a","an","and","or","of","for","to","problem","statement","challenge","track","theme","ps"}
        words = [w for w in re.findall(r"[a-z0-9]+", title) if len(w) >= 3 and w not in stop]
        if len(set(words) & set(re.findall(r"[a-z0-9]+", text))) >= min(2, len(set(words))):
            return True
    return False


def _source_memory(docs: list[dict], total_limit: int = 60000, per_doc_limit: int = 15000) -> list[dict]:
    """Persist a bounded source snapshot so later day planning remembers more than URLs.

    The generated blueprint remains the authoritative structured interpretation.  This
    compact snapshot preserves source identity plus representative source text without
    dumping entire large sites/PDFs into the campaign row.
    """
    remaining = max(0, int(total_limit))
    output = []
    keywords = re.compile(
        r"\b(?:deadline|date|schedule|format|duration|rules?|scor|judg|rubric|deliverable|"
        r"syllabus|scope|problem|track|round|eligib|submission|requirement|archive|solution)\w*\b",
        re.I,
    )
    for doc in docs or []:
        if remaining <= 0:
            break
        raw = re.sub(r"\s+", " ", str(doc.get("text") or "")).strip()
        limit = min(per_doc_limit, remaining)
        pieces = []
        if raw:
            head = raw[: min(6000, limit)]
            pieces.append(head)
            if len(raw) > len(head) and limit > len(head):
                # Pull bounded windows around source-visible competition/exam facts so
                # important rules near the middle of a long page survive the snapshot.
                for match in list(keywords.finditer(raw))[:18]:
                    if sum(len(x) for x in pieces) >= limit - 1200:
                        break
                    a = max(0, match.start() - 320)
                    b = min(len(raw), match.end() + 700)
                    window = raw[a:b].strip()
                    if window and not any(window[:120] in existing for existing in pieces):
                        pieces.append(window)
                if sum(len(x) for x in pieces) < limit - 800:
                    pieces.append(raw[-min(2500, limit):])
        excerpt = "\n…\n".join(pieces)
        excerpt = excerpt[:limit]
        remaining -= len(excerpt)
        output.append({
            "source": doc.get("source"),
            "source_type": doc.get("source_type"),
            "title": doc.get("title"),
            "content_sha256": hashlib.sha256(raw.encode()).hexdigest() if raw else None,
            "source_characters": len(raw),
            "remembered_excerpt": excerpt,
        })
    return output


def build_campaign(draft: CampaignDraft, request_text: str, docs: list[dict]) -> dict:
    now = datetime.now(settings.tz)
    raw = draft.model_dump(mode="json")
    _validate_provenance(raw, docs)

    scope_options = list(raw.get("scope_options") or [])
    source_scope_markers = _source_scope_markers(docs)
    source_named_tracks = _source_named_track_markers(docs)
    source_scope_count = max(len(source_scope_markers), len(source_named_tracks))
    if source_scope_count > 1 and len(scope_options) < source_scope_count:
        visible = (
            ", ".join(source_named_tracks)
            if len(source_named_tracks) >= len(source_scope_markers)
            else ", ".join(str(x) for x in source_scope_markers)
        )
        raise HTTPException(
            422,
            "The source material contains multiple problem statements/tracks "
            f"({visible}), but the generated blueprint did not enumerate all of them. "
            "I will not collapse the event to one branch. Interpret again or choose a track/problem statement explicitly. "
            "No blueprint or TickTick tasks were created.",
        )
    selected_scope = raw.get("selected_scope")
    selection_basis = str(raw.get("scope_selection_basis") or "not_applicable")
    if len(scope_options) > 1:
        keys = {str(o.get("key") or "").casefold() for o in scope_options}
        if not selected_scope or str(selected_scope).casefold() not in keys:
            choices = "; ".join(f"{o.get('key')}: {o.get('title')}" for o in scope_options[:8])
            raise HTTPException(
                409,
                "This event contains multiple mutually exclusive problem statements/tracks. "
                "Choose one before I generate track-specific study/build tasks. Options: " + choices
                + ". No blueprint or TickTick tasks were created.",
            )
        if selection_basis != "user_explicit" or not _scope_selection_is_explicit(request_text, scope_options, selected_scope):
            choices = "; ".join(f"{o.get('key')}: {o.get('title')}" for o in scope_options[:8])
            raise HTTPException(
                409,
                "Project Intelligence detected multiple problem statements/tracks but the request did not explicitly select one. "
                "I will not guess a track. Choose one by its id/name (for example, 'prepare me for PS3'). Options: "
                + choices + ". No blueprint or TickTick tasks were created.",
            )
    elif len(scope_options) == 1:
        only_key = str(scope_options[0].get("key") or "")
        if not selected_scope:
            selected_scope = only_key
            raw["selected_scope"] = only_key
        if selection_basis == "ambiguous":
            raw["scope_selection_basis"] = "single_option"

    if not 1 <= len(raw['work_packages']) <= 200:
        raise HTTPException(422, "A project blueprint must contain between 1 and 200 concrete work packages.")
    originals = [str(p.get('key') or '').strip() for p in raw['work_packages']]
    if len(set(originals)) != len(originals) or not all(originals):
        raise HTTPException(422, "Project blueprint contains blank or duplicate work-package keys.")
    if any(not str(p.get(field) or '').strip() for p in raw['work_packages'] for field in ('title','description','definition_of_done')):
        raise HTTPException(422, "Every work package needs a title, instructions and a definition of done.")
    packages, used, original_map = [], set(), {}
    for index, source in enumerate(raw["work_packages"], 1):
        item = dict(source)
        original = str(item.get("key") or f"wp{index}")
        key = re.sub(r"[^a-zA-Z0-9_-]+", "-", original).strip("-").lower() or f"wp{index}"
        base, suffix = key, 2
        while key in used:
            key = f"{base}-{suffix}"; suffix += 1
        used.add(key); original_map[original] = key; item["key"] = key
        item.update(status="pending", progress_percent=0, remaining_minutes=int(item["estimated_minutes"]),
                    actual_minutes=0, ticktick_task_id=None, ticktick_project_id=None,
                    materialized_at=None, completed_at=None, target_start=None, target_finish=None, rubric_weight=0.0)
        packages.append(item)
    for index, package in enumerate(packages):
        package["dependencies"] = [
            original_map.get(str(dep), re.sub(r"[^a-zA-Z0-9_-]+", "-", str(dep)).strip("-").lower())
            for dep in raw["work_packages"][index].get("dependencies", [])
        ]
        package["retrieval_of"] = [
            original_map.get(str(ref), re.sub(r"[^a-zA-Z0-9_-]+", "-", str(ref)).strip("-").lower())
            for ref in raw["work_packages"][index].get("retrieval_of", [])
        ]
    order = topological(packages)

    deadline, presentation = date_value(raw.get("deadline")), date_value(raw.get("presentation_date"))
    final_day = min(x for x in (deadline, presentation) if x is not None) if (deadline or presentation) else None
    if any(raw.get(k) and date_value(raw[k]) is None for k in ('deadline','presentation_date')):
        raise HTTPException(422, "Project date is invalid; no work was created.")
    if final_day and final_day < now.date():
        raise HTTPException(422, "This project deadline has already passed. Provide the current event date before creating preparation work.")
    total = sum(int(p["estimated_minutes"]) for p in packages)
    major_risks = list(raw.get("major_risks") or [])
    aggregate_effort = re.compile(
        r"\b(?:roughly|approximately|about)?\s*[\d,]+\s+minutes?\s+of\s+(?:planned\s+)?(?:preparation|work|effort)\b",
        re.I,
    )
    if any(aggregate_effort.search(str(risk)) for risk in major_risks):
        major_risks = [risk for risk in major_risks if not aggregate_effort.search(str(risk))]
        major_risks.append(
            f"The current roadmap totals {total} estimated effort minutes. This is active effort, "
            "not scheduled wall-clock time; reduce scope rather than borrowing protected commitments if it does not fit."
        )
    high_risk = sum(1 for p in packages if p.get("risk") == "high") + len(major_risks)
    edges = sum(len(p.get("dependencies", [])) for p in packages)
    internal_deadline, buffer_days = None, 0
    if final_day:
        days_left = max(1, (final_day - now.date()).days)
        desired = 1 + int(total >= 1800) + int(edges >= 8) + int(high_risk >= 3) + int(bool(presentation))
        buffer_days = min(max(0, (final_day - now.date()).days), desired, max(1, min(7, days_left // 5 if days_left >= 5 else 1)))
        internal_deadline = max(now.date(), final_day - timedelta(days=buffer_days))

    by_key = {p["key"]: p for p in packages}
    for p in packages:
        unknown = [key for key in p.get("retrieval_of", []) if key not in by_key or key == p["key"]]
        if unknown:
            raise HTTPException(422, f"Project blueprint has invalid spaced-review references for {p['title']}.")
        if int(p.get("review_delay_days") or 0) > 0 and not p.get("retrieval_of"):
            raise HTTPException(422, f"Spaced review {p['title']} must name the work it retrieves.")
    # Without a verified date, offer an effort-based practice roadmap. The
    # scheduler never treats this estimate as the event date or a hard deadline.
    capacity_profile = project_capacity_profile(request_text, now)
    capacity = int(capacity_profile["planning_capacity_minutes"])
    capacity_source = str(capacity_profile["capacity_source"])
    # Recommendations can expand; a verified event deadline cannot be moved.
    effort_days = math.ceil(total * 1.25 / capacity) if capacity else 28
    if capacity_profile.get("capacity_phases"):
        # Today's intensive capacity must not be extrapolated into the school term.
        # Integrate actual per-date caps; an uncapped phase still uses a finite
        # planning estimate and never assumes unlimited study hours.
        from .project_intelligence_review import preparation_days_for_effort
        estimate_profile = dict(capacity_profile, estimated_daily_project_capacity_minutes=capacity)
        effort_days = preparation_days_for_effort(estimate_profile, now.date(), math.ceil(total * 1.25))
        if effort_days >= 3660:
            major_risks.append("The stated study capacity does not fit the estimated effort within ten years; revise the scope or availability before trusting roadmap dates.")
    recommended_days = max(28, effort_days)
    roadmap_end = internal_deadline or (now.date() + timedelta(days=recommended_days - 1))
    if total > 0:
        from .project_intelligence_review import assign_roadmap_targets
        assign_roadmap_targets([by_key[key] for key in order], capacity_profile, now.date(), roadmap_end)

    rubric = raw.get("rubric") or []
    for p in packages:
        p["rubric_weight"] = _rubric_weight(p, rubric)
        if p["rubric_weight"] >= 30 and p["priority"] in {"normal", "low"}:
            p["priority"] = "high"
    milestones = raw.get("milestones") or []
    if internal_deadline and milestones:
        span = max(1, (internal_deadline - now.date()).days + 1)
        for index, milestone in enumerate(milestones, 1):
            offset = min(span - 1, max(0, math.ceil(span * index / len(milestones)) - 1))
            milestone["target_date"] = (now.date() + timedelta(days=offset)).isoformat()
    else:
        for milestone in milestones:
            milestone["target_date"] = None
    prep_days = max(1, (internal_deadline - now.date()).days + 1) if internal_deadline else None
    required_daily = math.ceil(total / prep_days) if prep_days else None
    campaign_id = uuid.uuid4().hex[:12]
    # Reuse the same deterministic capacity profile so review and daily planning
    # cannot disagree about a phased user budget.
    from .project_intelligence_provenance import _historical_simulation_requested
    historical_simulation = _historical_simulation_requested(request_text)
    campaign = {
        "id": campaign_id, "version": 1, "campaign_type": raw["campaign_type"], "goal": raw["goal"],
        "scope_options": scope_options, "selected_scope": raw.get("selected_scope"),
        "scope_selection_basis": raw.get("scope_selection_basis"),
        "source_scope_markers": source_scope_markers,
        "source_named_tracks": source_named_tracks,
        "request": request_text, "created_at": now.isoformat(), "updated_at": now.isoformat(), "status": "active",
        "historical_simulation": historical_simulation, "daily_integration": not historical_simulation,
        "deadline": deadline.isoformat() if deadline else None,
        "presentation_date": presentation.isoformat() if presentation else None,
        "internal_deadline": internal_deadline.isoformat() if internal_deadline else None,
        "deadline_buffer_days": buffer_days, "knowledge_assumption": raw["knowledge_assumption"],
        "knowledge_boundary": raw.get("knowledge_boundary"),
        "assumed_mastered_topics": raw.get("assumed_mastered_topics", []),
        "study_topics": raw.get("study_topics", []),
        "summary": raw["summary"], "requirements": raw["requirements"], "deliverables": raw["deliverables"],
        "rules": raw["rules"], "technical_requirements": raw["technical_requirements"], "datasets": raw["datasets"],
        "rubric": rubric, "milestones": milestones, "major_risks": major_risks, "work_packages": packages,
        "source_documents": [{"source": d.get("source"), "source_type": d.get("source_type"), "title": d.get("title")} for d in docs],
        "source_memory": _source_memory(docs),
        "rolling_window_days": ROLLING_DEFAULT_DAYS, "estimated_daily_project_capacity_minutes": capacity, "capacity_source": capacity_source,
        "capacity_phases": list(capacity_profile.get("capacity_phases") or []),
        "priority_class": str(capacity_profile.get("priority_class") or "normal"),
        "required_average_minutes_per_day": required_daily,
        "planning_capacity_ratio": round(required_daily / 120, 3) if required_daily is not None else None,
        "total_estimated_minutes": total, "remaining_minutes": total, "ticktick_project_id": None,
        "history": [{"at": now.isoformat(), "event": "blueprint_created", "remaining_minutes": total}],
    }

    campaign["learning_resources"] = raw.get("learning_resources", [])
    campaign["preparation_start"] = now.date().isoformat()
    campaign["roadmap_end"] = roadmap_end.isoformat()
    campaign["recommended_preparation_days"] = recommended_days
    campaign["readiness_effort_minutes_with_buffer"] = math.ceil(total * 1.25)
    campaign["roadmap_date_basis"] = "confirmed_deadline" if final_day else "recommended_four_week_practice"
    campaign["generation"] = dict(draft._generation)
    refresh_review(campaign)
    return campaign


def eligible_work_packages(campaign: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(settings.tz)
    if campaign.get("status") != "active":
        return []
    packages = list(campaign.get("work_packages") or [])
    by_key = {p.get("key"): p for p in packages}
    days = max(7, min(14, int(campaign.get("rolling_window_days") or ROLLING_DEFAULT_DAYS)))
    horizon = now.date() + timedelta(days=days)
    eligible = []
    for p in packages:
        if p.get("status") in {"done", "paused", "cancelled"} or p.get("ticktick_task_id"):
            continue
        if not all(by_key.get(dep, {}).get("status") == "done" for dep in p.get("dependencies", [])):
            continue
        if int(p.get("review_delay_days") or 0) > 0:
            ready_at = spaced_review_ready_at(campaign, p)
            if ready_at is None or ready_at > now:
                continue
        target = date_value(p.get("target_start"))
        from .project_intelligence_quality import productive
        # Back-planned dates are estimates. An independent learning starter must
        # not be withheld for weeks simply because the deadline is distant.
        if productive(p):
            target = None
        # Once prerequisites are completed, the next learning/build step can start early.
        if target and target > horizon and not p.get("dependencies"):
            continue
        eligible.append(p)
    rank = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    eligible.sort(key=lambda p: (rank.get(p.get("priority"), 2), p.get("target_finish") or "9999-12-31", -float(p.get("rubric_weight") or 0)))
    return eligible[:12]


def preview_summary(campaign: dict) -> dict:
    refresh_review(campaign)
    from .project_revisions import replacement_candidates
    eligible = eligible_work_packages(campaign)
    return {
        "intent": "CREATE_PROJECT_BLUEPRINT", "blueprint": campaign,
        "replacement_candidates": replacement_candidates(campaign, load_store()),
        "metrics": {"total_estimated_minutes": campaign.get("total_estimated_minutes", 0),
                    "remaining_minutes": campaign.get("remaining_minutes", 0),
                    "work_package_count": len(campaign.get("work_packages") or []), "eligible_now_count": len(eligible),
                    "rolling_window_days": campaign.get("rolling_window_days"), "deadline_buffer_days": campaign.get("deadline_buffer_days")},
        "eligible_preview": [{"key": p["key"], "title": p["title"], "remaining_minutes": p["remaining_minutes"],
                              "priority": p["priority"], "target_finish": p.get("target_finish")} for p in eligible],
    }


def new_preview(text: str, campaign: dict, route: dict) -> dict:
    preview_id, expires = uuid.uuid4().hex, time.time() + 30 * 60
    value = {"preview_id": preview_id, "request_hash": hashlib.sha256(text.encode()).hexdigest(),
             "expires_at_epoch": expires, "campaign": campaign, "route": route}
    from .project_revisions import replacement_candidates
    value["replacement_candidate_ids"] = [x["id"] for x in replacement_candidates(campaign, load_store())]
    save_preview(value)
    return value




def schedulable_work_packages(campaign, active_task_ids=None, now: datetime | None = None):
    """Release a bounded topological prefix; planning is not completion credit.

    Spaced-retrieval packages are stricter than ordinary dependencies: they are not
    released optimistically in the same planning window. The referenced material must
    have real completion evidence and its calendar-day delay must have elapsed first.
    """
    if campaign.get("status") != "active":
        return []
    now = now or datetime.now(settings.tz)
    by = {p["key"]: p for p in campaign.get("work_packages", [])}
    available, selected = set(), []
    budget = max(0, int(campaign.get("estimated_daily_project_capacity_minutes", 120))) * min(14, max(7, int(campaign.get("rolling_window_days", 10))))
    effort = 0
    for key in topological(list(by.values())):
        p = by[key]
        if p.get("status") in {"paused", "cancelled"}:
            continue
        if p.get("status") == "done":
            available.add(key); continue
        if not all(dep in available for dep in p.get("dependencies", [])):
            continue
        if int(p.get("review_delay_days") or 0) > 0:
            ready_at = spaced_review_ready_at(campaign, p)
            if ready_at is None or ready_at > now:
                continue
        if p.get("ticktick_task_id"):
            if active_task_ids is not None and str(p["ticktick_task_id"]) not in active_task_ids:
                continue
            available.add(key)
            effort += int(p.get("remaining_minutes") or 0)
            continue
        if len(selected) >= 12 or effort >= budget:
            continue
        selected.append(p); available.add(key)
        effort += int(p.get("remaining_minutes") or p.get("estimated_minutes") or 0)
    return selected
