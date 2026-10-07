from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import date, datetime

import httpx
from fastapi import File, Form, HTTPException, UploadFile

from . import db
from .config import settings
from .project_intelligence_models import (
    ProgressUpdate, build_campaign, consume_preview, eligible_work_packages, schedulable_work_packages,
    spaced_review_ready_at,
    extract_urls, get_campaign, list_campaigns, load_preview, load_store, new_preview,
    preview_summary, save_campaign, save_store, preflight_source_scope_choice,
)
from .project_intelligence_sources import MAX_SOURCE_BYTES, fetch_source_bundle, pdf_text, reason_campaign
from .project_intelligence_review import refresh_review
from .tenant import profile_namespace
from .ticktick import TickTickClient
from .models import Task

_LOCKS: dict[str, asyncio.Lock] = {}
_PROJECT_PREVIEW_PLAN = ContextVar("project_intelligence_preview_plan", default=False)


def priority_value(value: str) -> int:
    return {"critical": 5, "high": 3, "normal": 1, "low": 0}.get(str(value), 1)


def package_priority_value(package: dict, campaign: dict | None = None) -> int:
    # Administrative/status work must never outrank actual learning or building.
    if str(package.get("work_kind") or "").casefold() == "administration":
        return 0
    value = priority_value(package.get("priority"))
    project_priority = str((campaign or {}).get("priority_class") or "normal").casefold()
    # A user-declared lower-priority competition is still productive work, but it
    # should yield to ordinary school/deadline work. Keep critical/high package
    # ordering within the project while capping its cross-project scheduler weight.
    if project_priority == "low":
        return min(value, 1)
    if project_priority == "high":
        return max(value, 3)
    return value


def task_content(campaign: dict, package: dict) -> str:
    resources = {r["id"]: r for r in campaign.get("learning_resources", [])}
    lesson = []
    for label, key in (("Concepts", "concepts"), ("Worked example", "worked_example"), ("Your exercise", "exercise"), ("Check your answer", "self_check")):
        value = package.get(key)
        if value: lesson.append(f"{label}: " + (", ".join(value) if isinstance(value, list) else value))
    mode = str(package.get("learning_mode") or "not_applicable")
    if mode != "not_applicable":
        lesson.append("Learning mode: " + mode.replace("_", " "))
    if package.get("retrieval_of"):
        lesson.append("Spaced retrieval of: " + ", ".join(map(str, package.get("retrieval_of") or [])))
    if int(package.get("review_delay_days") or 0) > 0:
        lesson.append(f"Minimum review delay: {int(package.get('review_delay_days') or 0)} day(s) after source completion")
    for rid in package.get("resource_ids", []):
        if rid in resources:
            r = resources[rid]
            if str(r.get("url") or "").startswith("user-owned://"):
                lesson.append(f"Learning material: {r['title']} — your owned resource ({r['reason']})")
            else:
                lesson.append(f"Learning material: {r['title']} — {r['url']} ({r['reason']})")

    # For semester-scale indexed modules, retrieve the best passages NOW rather than
    # relying on the initial blueprint call to carry thousands of pages in context.
    try:
        from .module_library import module_search_documents_for_request
        retrieval_query = " ".join([
            str(package.get("title") or ""),
            str(package.get("description") or ""),
            " ".join(map(str, package.get("concepts") or [])),
        ])
        hits = module_search_documents_for_request(str(campaign.get("request") or ""), retrieval_query, top_k=3)
        if hits:
            lesson.append("Relevant indexed module passages (retrieved locally for this task):")
            for hit in hits:
                excerpt = " ".join(str(hit.get("text") or "").split())[:700]
                lesson.append(
                    f"- {hit.get('filename')} · pp. {hit.get('page_start')}–{hit.get('page_end')}"
                    + (f" · {hit.get('heading')}" if hit.get("heading") else "")
                    + f": {excerpt}"
                )
    except Exception:
        pass
    return "\n".join([
        f"ProjectIntelligenceCampaign:{campaign['id']}",
        f"ProjectIntelligenceWorkPackage:{package['key']}",
        f"Goal: {campaign.get('goal','')}",
        str(package.get("description") or ""),
        *lesson,
        "Rubric links: " + ", ".join(package.get("rubric_links") or []),
        f"Definition of done: {package.get('definition_of_done','')}",
        "Generated from the rolling project blueprint. AutoScheduler controls timing.",
    ])


def scheduler_meta(campaign: dict, package: dict) -> dict:
    due = campaign.get("internal_deadline") or campaign.get("deadline") or campaign.get("presentation_date")
    # Intermediate roadmap dates are estimates, not hard eligibility constraints.
    if due and str(due)[:10] < datetime.now(settings.tz).date().isoformat():
        dates = [str(campaign[k]) for k in ('deadline','presentation_date') if campaign.get(k)]
        due = min(dates) if dates else due
    deadline_iso = None
    if due:
        try:
            d = date.fromisoformat(str(due)[:10])
            deadline_iso = datetime.combine(d, datetime.max.time().replace(microsecond=0), tzinfo=settings.tz).isoformat()
        except Exception:
            pass
    minutes = max(15, int(package.get("remaining_minutes") or package.get("estimated_minutes") or 30))
    phase = str(package.get("phase") or "").casefold()
    by_key = {p["key"]: p for p in campaign.get("work_packages", [])}
    dependencies = [str(by_key[k]["ticktick_task_id"]) for k in package.get("dependencies", [])
                    if k in by_key and by_key[k].get("status") != "done" and by_key[k].get("ticktick_task_id")]
    high_energy = any(word in phase for word in ("learn", "build", "implement", "analysis", "research", "test", "integrat"))
    mode = str(package.get("learning_mode") or "not_applicable")
    delayed_retrieval = int(package.get("review_delay_days") or 0) > 0
    recall_mode = mode in {"memorisation", "visual_recall"} or delayed_retrieval
    review_ready = spaced_review_ready_at(campaign, package)
    contiguous = bool(package.get("requires_contiguous_session"))
    return {
        "duration_minutes": int(package.get("estimated_minutes") or minutes), "remaining_minutes": minutes,
        "deadline": deadline_iso,
        "earliest": review_ready.isoformat() if review_ready else None,
        "energy": "medium" if recall_mode else ("high" if high_energy else "auto"),
        "confidence": "high" if contiguous else ("low" if package.get("risk") == "high" else "medium"),
        "splittable": not contiguous,
        "min_chunk": minutes if contiguous else min(15 if recall_mode else 25, minutes, max(5, int(campaign.get("estimated_daily_project_capacity_minutes", 120)))),
        "max_chunk": minutes if contiguous else (45 if recall_mode else 90), "dependencies": dependencies,
        "category": f"project:{campaign['id']}", "autoschedule": True, "context": f"campaign:{campaign['id']}",
        "must_finish": False, "timing": "asap" if delayed_retrieval else "balanced",
        # Explicitly low-priority campaigns are background-fill work: keep them
        # schedulable and dependency-aware, but they must yield to ordinary work
        # when both compete for the same capacity.
        "_background_fill": str(campaign.get("priority_class") or "").casefold() == "low",
    }



def _virtual_task_id(campaign_id: str, package_key: str) -> str:
    return f"pi-virtual:{campaign_id}:{package_key}"


def virtual_project_bundle(campaigns: dict | None = None, requested_packages: dict[str, list[str]] | None = None):
    """Build planner-only project tasks. This performs no TickTick writes.

    requested_packages narrows an ordinary Quick Dump to the explicitly referenced
    campaign work package(s). Project Intelligence's own preview leaves it as None and
    continues to expose the normal bounded rolling prefix.
    """
    from .project_revisions import daily_integration_enabled
    campaigns = campaigns if campaigns is not None else load_store()
    tasks, meta, refs = [], {}, {}
    for campaign in campaigns.values():
        if campaign.get("status") != "active" or not daily_integration_enabled(campaign):
            continue
        if not campaign.get("ticktick_project_id"):
            continue
        selected = schedulable_work_packages(campaign)
        if requested_packages is not None:
            wanted = {str(x) for x in requested_packages.get(str(campaign.get("id")), [])}
            selected = [p for p in selected if str(p.get("key")) in wanted]
        selected_keys = {p["key"] for p in selected}
        by_key = {p["key"]: p for p in campaign.get("work_packages") or []}
        for package in selected:
            vid = _virtual_task_id(campaign["id"], package["key"])
            tasks.append(Task(
                vid,
                str(campaign.get("ticktick_project_id") or "project-intelligence"),
                package["title"],
                priority=package_priority_value(package, campaign),
                tags=["flexible", "project-intelligence", "project-intelligence-virtual"],
                content=task_content(campaign, package),
            ))
            row = scheduler_meta(campaign, package)
            deps = []
            for dep_key in package.get("dependencies") or []:
                dep = by_key.get(dep_key)
                if not dep or dep.get("status") == "done":
                    continue
                if dep.get("ticktick_task_id"):
                    deps.append(str(dep["ticktick_task_id"]))
                elif dep_key in selected_keys:
                    deps.append(_virtual_task_id(campaign["id"], dep_key))
            row["dependencies"] = deps
            row["_project_virtual"] = True
            meta[vid] = row
            refs[vid] = {
                "campaign_id": campaign["id"],
                "work_package_key": package["key"],
                "project_id": str(campaign.get("ticktick_project_id") or ""),
            }
    return tasks, meta, refs


async def _materialize_virtual_sources_for_commit(payload: dict) -> tuple[dict, list[dict]]:
    """Create approved virtual sources only behind the server-side final Apply gate."""
    pending = dict(payload.get("pending_project_materialization") or {})
    if pending and not payload.get("_explicit_ticktick_apply"):
        raise HTTPException(
            403,
            "Project work is preview-only until you click Apply to TickTick. No TickTick task was created.",
        )
    used = {str(s.get("task_id")) for s in payload.get("segments") or []}
    refs = {vid: ref for vid, ref in pending.items() if vid in used}
    if not refs:
        return {}, []

    tt = TickTickClient()
    if not tt.connected:
        raise HTTPException(401, "Connect TickTick first")
    items, projects = await tt.all_active_tasks()
    active_ids = {str(t.id) for t in items if t.is_actionable}
    valid_projects = {
        str(p.get("id")) for p in projects
        if str(p.get("kind") or "TASK").upper() != "NOTE"
    }
    mapping, created = {}, []
    lock = _LOCKS.setdefault(profile_namespace(), asyncio.Lock())
    async with lock:
        store = load_store()
        for campaign_id in sorted({str(r.get("campaign_id")) for r in refs.values()}):
            campaign = store.get(campaign_id)
            if not campaign or campaign.get("status") != "active":
                raise HTTPException(409, "A project changed after preview. Replan before applying.")
            destination = str(campaign.get("ticktick_project_id") or "")
            if destination not in valid_projects:
                raise HTTPException(409, "A Project Intelligence destination list changed. Replan before applying.")
            by_key = {p["key"]: p for p in campaign.get("work_packages") or []}
            keys = {
                str(ref["work_package_key"])
                for ref in refs.values()
                if str(ref.get("campaign_id")) == campaign_id
            }
            from .project_intelligence_models import topological
            for key in topological(list(by_key.values())):
                if key not in keys:
                    continue
                package = by_key[key]
                vid = _virtual_task_id(campaign_id, key)
                existing = str(package.get("ticktick_task_id") or "")
                if existing:
                    mapping[vid] = existing
                    continue
                made = await tt.create_task(
                    destination,
                    package["title"],
                    tags=["flexible", "project-intelligence"],
                    content=task_content(campaign, package),
                    priority=package_priority_value(package, campaign),
                )
                task_id = str((made or {}).get("id") or "")
                if not task_id:
                    raise RuntimeError(f"TickTick did not confirm Project Intelligence task: {package['title']}")
                package.update(
                    ticktick_task_id=task_id,
                    ticktick_project_id=destination,
                    materialized_at=datetime.now(settings.tz).isoformat(),
                    status="actionable",
                )
                mapping[vid] = task_id
                created.append({
                    "type": "project-work",
                    "campaign_id": campaign_id,
                    "work_package_key": key,
                    "task_id": task_id,
                    "title": package["title"],
                })
                store[campaign_id] = campaign
                save_store(store)
                active_ids.add(task_id)

            # All new IDs now exist, so dependency metadata can reference real IDs.
            for key in keys:
                package = by_key[key]
                tid = str(package.get("ticktick_task_id") or "")
                if tid:
                    db.set_meta(tid, scheduler_meta(campaign, package))
            store[campaign_id] = campaign
        save_store(store)

    if created:
        try:
            from .performance_patch import invalidate_ticktick_cache
            invalidate_ticktick_cache(projects=False)
        except Exception:
            pass
        await asyncio.sleep(0.35)
    return mapping, created


async def structure_and_route(campaign: dict, fallback_project_id: str | None):
    from .smart_routing import _live_rows, _project_structure, _suggest_route
    tt, items, projects, rows = await _live_rows()
    structure = await _project_structure(tt, projects)
    valid = {str(x.get("id")) for x in structure}
    fallback = fallback_project_id if fallback_project_id in valid else None
    representative = {
        "title": campaign.get("goal") or "Project preparation",
        "meta_patch": {"category": "coding" if campaign.get("campaign_type") in {"hackathon", "portfolio_project", "research_project"} else "school"},
    }
    suggestion = _suggest_route(representative, rows, structure, fallback)
    route = {"route_key": f"campaign-{campaign['id']}", "title": campaign.get("goal") or "Project preparation", **suggestion}
    return tt, items, projects, structure, route


async def resolve_project_sources(text):
    urls = extract_urls(text)
    if urls:
        docs, warnings = await fetch_source_bundle(urls)
        if not docs:
            raise HTTPException(422, "I could not read the supplied brief or syllabus. Provide its direct link or attach its PDF. No blueprint or tasks were created.")
        return docs, warnings, None
    from .exam_sources import discover_exam_sources
    result = await discover_exam_sources(text)
    return result or ([], [], None)


async def preview_project_request(payload) -> dict:
    from .project_intelligence_models import looks_like_project_blueprint_request
    text = str(payload.text or "")
    if not looks_like_project_blueprint_request(text):
        raise HTTPException(400, "This is not an explicit Project Intelligence request.")
    docs, warnings, selection = await resolve_project_sources(text)
    preflight_source_scope_choice(text, docs)
    draft = await reason_campaign(text, docs)
    campaign = build_campaign(draft, text, docs)
    if selection: campaign["syllabus_selection"] = selection
    _, _, _, structure, route = await structure_and_route(campaign, payload.fallback_project_id)
    stored = new_preview(text, campaign, route)
    return {
        "preview_id": stored["preview_id"],
        "expires_at": datetime.fromtimestamp(stored["expires_at_epoch"], settings.tz).isoformat(),
        "parser_version": "project-intelligence-v1", "interpreter_mode": "project-intelligence",
        "intents": [{"kind": "CREATE_PROJECT_BLUEPRINT", "text": text, "status": "review"}],
        "tasks": [], "cleanup_tasks": [], "context": None,
        "notes": ["Project Intelligence determines WHAT should exist. AutoScheduler still determines WHEN eligible work happens."],
        "warnings": warnings, "routing": [route], "project_structure": structure,
        "needs_project": not bool(route.get("project_id")), "project_intelligence": preview_summary(campaign),
    }


async def reconcile_campaign(campaign: dict, tt: TickTickClient, active_by_id: dict) -> bool:
    changed, checks = False, []
    for p in campaign.get("work_packages") or []:
        task_id = str(p.get("ticktick_task_id") or "")
        if not task_id or p.get("status") in {"done", "cancelled", "paused"}:
            continue
        active = active_by_id.get(task_id)
        if active:
            remain = db.get_meta(task_id).get("remaining_minutes")
            if remain is not None:
                remain = max(0, int(remain))
                if remain != int(p.get("remaining_minutes") or 0):
                    p["remaining_minutes"] = remain
                    estimate = max(1, int(p.get("estimated_minutes") or 1))
                    p["progress_percent"] = min(99, max(int(p.get("progress_percent") or 0), round(100 * (estimate - remain) / estimate)))
                    changed = True
        else:
            checks.append(p)
    sem = asyncio.Semaphore(4)

    async def verify(package):
        nonlocal changed
        project_id, task_id = package.get("ticktick_project_id"), package.get("ticktick_task_id")
        if not project_id or not task_id:
            return
        try:
            async with sem:
                raw = await asyncio.wait_for(tt.get_task(str(project_id), str(task_id)), timeout=7)
            status = int((raw or {}).get("status", 0)) if isinstance(raw, dict) else 0
            if status == 2:
                package.update(status="done", progress_percent=100, remaining_minutes=0,
                               completed_at=datetime.now(settings.tz).isoformat())
                changed = True
            elif status == -1:
                package["status"] = "paused"; changed = True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # A deliberately deleted source must not be resurrected automatically.
                package["status"] = "paused"; changed = True
        except Exception:
            return

    # Bound network work per refresh without permanently starving package 17+.
    if checks:
        offset = int(campaign.get("reconcile_cursor") or 0) % len(checks)
        batch = (checks[offset:] + checks[:offset])[:16]
        if len(checks) > 16:
            campaign["reconcile_cursor"] = (offset + len(batch)) % len(checks)
            changed = True
        await asyncio.gather(*(verify(p) for p in batch))
    if changed:
        refresh_review(campaign)
        campaign["remaining_minutes"] = sum(int(p.get("remaining_minutes") or 0) for p in campaign.get("work_packages") or [] if p.get("status") != "done")
        campaign["updated_at"] = datetime.now(settings.tz).isoformat()
        campaign.setdefault("history", []).append({"at": campaign["updated_at"], "event": "progress_reconciled", "remaining_minutes": campaign["remaining_minutes"]})
    return changed


async def _sync_unlocked(*, tt: TickTickClient | None = None, items=None, projects=None) -> dict:
    campaigns = load_store()
    if not campaigns:
        return {"campaigns": 0, "created": []}
    tt = tt or TickTickClient()
    if not tt.connected:
        return {"campaigns": len(campaigns), "created": [], "warning": "TickTick not connected"}
    if items is None or projects is None:
        items, projects = await tt.all_active_tasks()
    active = {str(t.id): t for t in items if t.is_actionable}
    created = []
    from .project_revisions import daily_integration_enabled
    for campaign_id, campaign in campaigns.items():
        if campaign.get("status") != "active":
            continue
        changed = await reconcile_campaign(campaign, tt, active)
        integrated = daily_integration_enabled(campaign)
        # Historical/test campaigns and user-disabled projects remain visible in
        # Project Intelligence but cannot leak into ordinary school/personal plans.
        for package in campaign.get("work_packages") or []:
            tid = str(package.get("ticktick_task_id") or "")
            if not tid:
                continue
            meta = dict(db.get_meta(tid) or {})
            if not integrated:
                if not meta.get("_project_integration_blocked"):
                    meta["_project_previous_autoschedule"] = meta.get("autoschedule", True)
                meta.update(autoschedule=False, _project_integration_blocked=True)
            elif meta.pop("_project_integration_blocked", False):
                meta["autoschedule"] = meta.pop("_project_previous_autoschedule", True)
            db.set_meta(tid, meta)
        # Existing tasks also need dependency IDs, including after a prerequisite
        # is deliberately deleted/paused. Missing work is not completed work.
        by_key = {p["key"]: p for p in campaign.get("work_packages", [])}
        blocked_keys = set()
        known_present = set(active) | {str(p["task_id"]) for p in created}
        from .project_intelligence_models import topological
        for key in topological(list(by_key.values())):
            p = by_key[key]
            missing_source = p.get("status") != "done" and p.get("ticktick_task_id") and str(p["ticktick_task_id"]) not in known_present
            blocked = missing_source or p.get("status") in {"paused", "cancelled"} or any(
                d in blocked_keys or (by_key[d].get("status") != "done" and not by_key[d].get("ticktick_task_id"))
                for d in p.get("dependencies", []))
            if blocked: blocked_keys.add(key)
            tid = p.get("ticktick_task_id")
            if not tid or p.get("status") == "done": continue
            meta = dict(db.get_meta(tid) or {})
            meta["dependencies"] = scheduler_meta(campaign, p)["dependencies"]
            if blocked:
                if not meta.get("_project_dependency_blocked"):
                    meta["_project_previous_autoschedule"] = meta.get("autoschedule", True)
                meta.update(autoschedule=False, _project_dependency_blocked=True)
            elif meta.pop("_project_dependency_blocked", False):
                meta["autoschedule"] = meta.pop("_project_previous_autoschedule", True)
            db.set_meta(tid, meta)
        if changed:
            campaigns[campaign_id] = campaign
    save_store(campaigns)
    return {"campaigns": len(campaigns), "created": created}


async def sync_campaigns(*, tt: TickTickClient | None = None, items=None, projects=None) -> dict:
    """Refresh project state from current TickTick tasks without creating anything.

    This function is intentionally read-only. Ordinary day planning, project refreshes,
    startup hooks and background jobs may call it safely. New Project Intelligence tasks are materialized only by the final schedule-commit path.
    """
    lock = _LOCKS.setdefault(profile_namespace(), asyncio.Lock())
    async with lock:
        return await _sync_unlocked(tt=tt, items=items, projects=projects)


async def apply_project_request(payload) -> dict:
    preview = load_preview(payload.preview_id, str(payload.text or ""))
    campaign, route = preview["campaign"], preview["route"]
    provided = payload.routes.get(str(route["route_key"]))
    destination = str((provided.project_id if provided else route.get("project_id")) or "")
    if not destination:
        raise HTTPException(400, "Choose a TickTick list for this project campaign before applying.")
    tt = TickTickClient()
    if not tt.connected:
        raise HTTPException(401, "Connect TickTick first")
    projects = await tt.projects()
    if not any(str(p.get("id")) == destination and str(p.get("kind") or "TASK").upper() != "NOTE" for p in projects):
        raise HTTPException(409, "The selected TickTick list is no longer available. Interpret again.")
    lock = _LOCKS.setdefault(profile_namespace(), asyncio.Lock())
    async with lock:
        # Re-read inside the lock: two Apply requests must not consume one preview twice.
        preview = load_preview(payload.preview_id, str(payload.text or ""))
        campaign = preview["campaign"]
        if getattr(payload, "project_daily_minutes", None) is not None:
            campaign["estimated_daily_project_capacity_minutes"] = payload.project_daily_minutes
            campaign["capacity_source"] = "user_input"
            refresh_review(campaign)
        if getattr(payload, "project_daily_integration", None) is not None:
            campaign["daily_integration"] = bool(payload.project_daily_integration)
        campaign["ticktick_project_id"] = destination
        from .project_revisions import apply_revision
        revised = apply_revision(load_store(), campaign, getattr(payload, 'replace_campaign_ids', []),
                                 preview.get('replacement_candidate_ids', []))
        save_store(revised)
        campaign = revised[campaign['id']]
        consume_preview(str(payload.preview_id))
    from . import service
    preview_token = _PROJECT_PREVIEW_PLAN.set(True)
    try:
        plan = await service.create_plan(payload.horizon_days, from_now=datetime.now(settings.tz))
    finally:
        _PROJECT_PREVIEW_PLAN.reset(preview_token)
    changes = [{'type':'project-superseded','campaign_id':cid,'replacement_id':campaign['id']}
               for cid in campaign.get('replaces_campaign_ids', [])] + [{"type": "project-blueprint", "campaign_id": campaign["id"], "title": campaign["goal"]}]
    db.audit("project_intelligence_created", {"campaign_id": campaign["id"], "work_packages": len(campaign.get("work_packages") or []), "materialized": 0, "write_deferred_until_schedule_commit": True})
    return {"ok": True, "interpretation": {"project_intelligence": preview_summary(campaign)}, "changes": changes,
            "plan": plan, "committed": [], "quick_context": service.get_quick_context()}


def campaign_summary(campaign: dict) -> dict:
    refresh_review(campaign)
    from .project_revisions import daily_integration_enabled
    return {"planning_review": campaign.get("planning_review"), "id": campaign.get("id"), "campaign_type": campaign.get("campaign_type"), "goal": campaign.get("goal"),
            "status": campaign.get("status"), "daily_integration": daily_integration_enabled(campaign),
            "historical_simulation": bool(campaign.get("historical_simulation")),
            "deadline": campaign.get("deadline"), "presentation_date": campaign.get("presentation_date"),
            "internal_deadline": campaign.get("internal_deadline"), "deadline_buffer_days": campaign.get("deadline_buffer_days"),
            "remaining_minutes": campaign.get("remaining_minutes"), "total_estimated_minutes": campaign.get("total_estimated_minutes"),
            "work_package_count": len(campaign.get("work_packages") or [])}


def install_project_intelligence(app, service_module, main_module) -> None:
    if getattr(app.state, "project_intelligence_installed", False):
        return
    app.state.project_intelligence_installed = True
    base_create_plan = service_module.create_plan
    base_commit_plan = main_module.commit_plan

    async def project_aware_create_plan(*args, **kwargs):
        warning = None
        try:
            await sync_campaigns()
        except Exception as exc:
            warning = f"Project rolling window could not refresh: {str(exc)[:180]}"
            db.audit("project_intelligence_sync_failed", {"error": str(exc)[:500]})

        # Ordinary day planning does not manufacture Project Intelligence work merely
        # because a campaign exists.  There are two explicit gates:
        #   1) the Project Intelligence preview itself, or
        #   2) a reviewed natural-language reference to a stored campaign/package.
        # Both remain planner-only until the final Apply boundary materializes a task.
        quick = service_module.PLAN_QUICK_CONTEXT.get() or {}
        requested_ids = {
            str(x) for x in (quick.get("requested_project_campaign_ids") or []) if str(x)
        }
        requested_packages = quick.get("requested_project_work_packages") or {}
        project_preview = bool(_PROJECT_PREVIEW_PLAN.get())
        if project_preview or requested_ids:
            campaigns = load_store()
            scoped = campaigns if project_preview else {
                cid: campaign for cid, campaign in campaigns.items() if str(cid) in requested_ids
            }
            extra_tasks, extra_meta, refs = virtual_project_bundle(
                scoped,
                None if project_preview else requested_packages,
            )

            # Quick Dump may already have virtual swim/outing/new-task candidates. Merge
            # rather than replace those ContextVars, otherwise adding SPhL could erase
            # the rest of the same real-life sentence.
            prior_tasks = list(service_module.PLAN_EXTRA_TASKS.get() or [])
            prior_meta = dict(service_module.PLAN_EXTRA_META.get() or {})
            prior_payload = dict(service_module.PLAN_EXTRA_PAYLOAD.get() or {})
            merged_payload = dict(prior_payload)
            if refs:
                merged_refs = dict(prior_payload.get("pending_project_materialization") or {})
                merged_refs.update(refs)
                merged_payload["pending_project_materialization"] = merged_refs

            task_token = service_module.PLAN_EXTRA_TASKS.set([*prior_tasks, *extra_tasks])
            meta_token = service_module.PLAN_EXTRA_META.set({**prior_meta, **extra_meta})
            payload_token = service_module.PLAN_EXTRA_PAYLOAD.set(merged_payload)
            try:
                result = await base_create_plan(*args, **kwargs)
            finally:
                service_module.PLAN_EXTRA_TASKS.reset(task_token)
                service_module.PLAN_EXTRA_META.reset(meta_token)
                service_module.PLAN_EXTRA_PAYLOAD.reset(payload_token)
        else:
            result = await base_create_plan(*args, **kwargs)

        if warning and isinstance(result, dict):
            result.setdefault("warnings", []).append(warning)
        return result

    async def project_aware_commit_plan(payload: dict):
        mapping, created = await _materialize_virtual_sources_for_commit(payload)
        if mapping:
            remapped = dict(payload)
            remapped["segments"] = []
            for segment in payload.get("segments") or []:
                row = dict(segment)
                virtual = str(row.get("task_id") or "")
                if virtual in mapping:
                    row["task_id"] = mapping[virtual]
                    row["source_task"] = {}
                remapped["segments"].append(row)
            payload = remapped
        changes = await base_commit_plan(payload)
        return [*created, *changes]

    project_aware_create_plan._project_intelligence_rolling = True
    service_module.create_plan = main_module.create_plan = project_aware_create_plan
    service_module.commit_plan = main_module.commit_plan = project_aware_commit_plan
    try:
        from . import autopilot as autopilot_module
        autopilot_module.create_plan = project_aware_create_plan
    except Exception:
        pass

    @app.get("/api/project-intelligence")
    async def campaigns_index():
        return [campaign_summary(x) for x in list_campaigns()]

    @app.get("/api/project-intelligence/modules")
    async def module_library_index():
        from .module_library import list_modules
        return {"modules": list_modules()}

    @app.get("/api/project-intelligence/modules/{module_id}")
    async def module_library_detail(module_id: str):
        from .module_library import get_module
        row = get_module(module_id, include_sections=True)
        if not row:
            raise HTTPException(404, "Module Library module not found")
        return {"module": row}

    @app.get("/api/project-intelligence/modules/{module_id}/search")
    async def module_library_search(module_id: str, q: str, top_k: int = 12):
        from .module_library import search_module
        return {"results": search_module(module_id, q, top_k=max(1, min(30, int(top_k))))}

    @app.post("/api/project-intelligence/modules/import")
    async def import_module_pdfs(
        title: str = Form(...),
        code: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ):
        from .module_library import (
            MAX_MODULE_FILES, MAX_MODULE_FILE_BYTES, MAX_MODULE_TOTAL_BYTES,
            import_pdf_bytes, list_modules,
        )
        if not files:
            raise HTTPException(422, "Choose at least one module PDF.")
        if len(files) > MAX_MODULE_FILES:
            raise HTTPException(422, f"Import at most {MAX_MODULE_FILES} PDFs in one batch.")
        total = 0
        imported, failures = [], []
        for upload in files:
            name = str(upload.filename or "module.pdf")
            if "pdf" not in str(upload.content_type or "").lower() and not name.lower().endswith(".pdf"):
                failures.append({"filename": name, "error": "PDF files only"})
                continue
            data = await upload.read(MAX_MODULE_FILE_BYTES + 1)
            total += len(data)
            if total > MAX_MODULE_TOTAL_BYTES:
                raise HTTPException(413, "The combined PDF upload is too large. Import the module in smaller batches.")
            if len(data) > MAX_MODULE_FILE_BYTES:
                failures.append({"filename": name, "error": f"larger than {MAX_MODULE_FILE_BYTES // 1_000_000} MB"})
                continue
            try:
                imported.append(import_pdf_bytes(title, code, name, data))
            except HTTPException as exc:
                failures.append({"filename": name, "error": str(exc.detail)})
            except Exception as exc:
                failures.append({"filename": name, "error": f"Could not index PDF ({type(exc).__name__})"})
        modules = list_modules()
        module = next((m for m in modules if any(x.get("module_id") == m.get("module_id") for x in imported)), None)
        if not imported:
            raise HTTPException(422, {"message": "No PDFs were indexed.", "failures": failures})
        db.audit("module_library_imported", {
            "module_id": imported[0].get("module_id"),
            "title": title,
            "imported": len([x for x in imported if not x.get("duplicate")]),
            "duplicates": len([x for x in imported if x.get("duplicate")]),
            "failures": failures,
        })
        return {"ok": True, "module": module, "documents": imported, "failures": failures}

    @app.post("/api/project-intelligence/modules/{module_id}/sections/{section_id}/progress")
    async def update_module_section_progress(module_id: str, section_id: str, payload: dict):
        from .module_library import update_section_progress
        return {"section": update_section_progress(
            module_id,
            section_id,
            status=payload.get("status"),
            mastery=payload.get("mastery"),
            notes=payload.get("notes"),
        )}

    @app.delete("/api/project-intelligence/modules/{module_id}/documents/{doc_id}")
    async def remove_module_document(module_id: str, doc_id: str):
        from .module_library import delete_document
        if not delete_document(module_id, doc_id):
            raise HTTPException(404, "Module Library PDF not found")
        db.audit("module_library_document_deleted", {"module_id": module_id, "doc_id": doc_id})
        return {"ok": True}

    @app.delete("/api/project-intelligence/modules/{module_id}")
    async def remove_module_library_module(module_id: str):
        from .module_library import delete_module
        if not delete_module(module_id):
            raise HTTPException(404, "Module Library module not found")
        db.audit("module_library_deleted", {"module_id": module_id})
        return {"ok": True}

    @app.get("/api/project-intelligence/owned-resources")
    async def owned_resources_index():
        from .owned_resource_library import list_owned_resources
        return {"resources": list_owned_resources()}

    @app.post("/api/project-intelligence/owned-resources/import")
    async def import_owned_resource(
        title: str = Form(...),
        edition: str = Form(default=""),
        files: list[UploadFile] = File(...),
    ):
        from .owned_resource_library import (
            MAX_OWNED_FILE_BYTES,
            import_owned_resource_files,
        )
        if not files:
            raise HTTPException(422, "Choose at least one TOC photo or PDF.")
        prepared = []
        total = 0
        for upload in files:
            data = await upload.read(MAX_OWNED_FILE_BYTES + 1)
            if len(data) > MAX_OWNED_FILE_BYTES:
                raise HTTPException(413, f"{upload.filename or 'A TOC file'} is larger than 8 MB.")
            total += len(data)
            if total > 64_000_000:
                raise HTTPException(413, "The combined TOC upload is too large. Import the book in smaller batches.")
            prepared.append((
                upload.filename or "toc-upload",
                str(upload.content_type or ""),
                data,
            ))
        row = await import_owned_resource_files(
            title=title,
            edition=edition,
            files=prepared,
        )
        db.audit("owned_resource_saved", {
            "resource_id": row.get("id"),
            "title": row.get("title"),
            "edition": row.get("edition"),
            "toc_entries": len(row.get("toc_entries") or []),
            "source_files": len(row.get("source_files") or []),
        })
        return {"ok": True, "resource": row}

    @app.delete("/api/project-intelligence/owned-resources/{resource_id}")
    async def remove_owned_resource(resource_id: str):
        from .owned_resource_library import delete_owned_resource
        if not delete_owned_resource(resource_id):
            raise HTTPException(404, "Owned resource not found")
        db.audit("owned_resource_deleted", {"resource_id": resource_id})
        return {"ok": True}

    @app.get("/api/project-intelligence/{campaign_id}")
    async def campaign_detail(campaign_id: str):
        campaign = get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "Project campaign not found")
        return campaign

    @app.post("/api/project-intelligence/{campaign_id}/refresh-window")
    async def refresh_campaign_window(campaign_id: str):
        if not get_campaign(campaign_id):
            raise HTTPException(404, "Project campaign not found")
        result = await sync_campaigns()
        return {"ok": True, **result, "campaign": get_campaign(campaign_id)}

    @app.post("/api/project-intelligence/{campaign_id}/progress")
    async def update_campaign_progress(campaign_id: str, update: ProgressUpdate):
        lock = _LOCKS.setdefault(profile_namespace(), asyncio.Lock())
        async with lock:
            campaign = get_campaign(campaign_id)
            if not campaign:
                raise HTTPException(404, "Project campaign not found")
            package = next((p for p in campaign.get("work_packages") or [] if p.get("key") == update.work_package_key), None)
            if not package:
                raise HTTPException(404, "Work package not found")
            if package.get("status") == "done" and update.progress_percent < 100:
                raise HTTPException(409, "Completed project work is historical progress; explicitly reopen it before reducing completion.")
            package["progress_percent"] = update.progress_percent
            package["remaining_minutes"] = update.remaining_minutes if update.remaining_minutes is not None else max(0, round(int(package.get("estimated_minutes") or 0) * (100 - update.progress_percent) / 100))
            if update.progress_percent >= 100:
                package.update(status="done", remaining_minutes=0, completed_at=package.get("completed_at") or datetime.now(settings.tz).isoformat())
            task_id = str(package.get("ticktick_task_id") or "")
            if task_id:
                meta = dict(db.get_meta(task_id) or {})
                meta["remaining_minutes"] = int(package.get("remaining_minutes") or 0)
                if package.get("status") == "done":
                    meta["autoschedule"] = False
                db.set_meta(task_id, meta)
            campaign["remaining_minutes"] = sum(int(p.get("remaining_minutes") or 0) for p in campaign.get("work_packages") or [] if p.get("status") != "done")
            campaign["updated_at"] = datetime.now(settings.tz).isoformat()
            campaign.setdefault("history", []).append({"at": campaign["updated_at"], "event": "progress_update",
                                                       "work_package": package["key"], "progress_percent": update.progress_percent, "note": update.note})
            refresh_review(campaign)
            save_campaign(campaign)

        await sync_campaigns()
        return get_campaign(campaign_id)

    @app.post("/api/project-intelligence/preview-file")
    async def preview_file(prompt: str = Form(...), file: UploadFile = File(...), fallback_project_id: str | None = Form(default=None)):
        from .project_intelligence_models import looks_like_project_blueprint_request
        if not looks_like_project_blueprint_request(prompt):
            raise HTTPException(400, "Use an explicit request such as 'Prepare me for this exam/hackathon and build a complete plan.'")
        data = await file.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise HTTPException(413, "Source file is too large")
        if "pdf" not in str(file.content_type or "").lower() and not str(file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(415, "Project Intelligence file intake currently accepts PDF sources.")
        text = pdf_text(data)
        if not text.strip():
            raise HTTPException(422, "No readable text was extracted from that PDF.")
        docs = [{"source": file.filename or "uploaded.pdf", "source_type": "pdf", "title": file.filename or "Uploaded PDF", "text": text}]
        preflight_source_scope_choice(prompt, docs)
        campaign = build_campaign(await reason_campaign(prompt, docs), prompt, docs)
        _, _, _, structure, route = await structure_and_route(campaign, fallback_project_id)
        stored = new_preview(prompt, campaign, route)
        return {"preview_id": stored["preview_id"], "expires_at": datetime.fromtimestamp(stored["expires_at_epoch"], settings.tz).isoformat(),
                "project_intelligence": preview_summary(campaign), "routing": [route], "project_structure": structure,
                "needs_project": not bool(route.get("project_id"))}
