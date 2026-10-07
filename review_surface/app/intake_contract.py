"""Persist reviewed interpretation and validate it before any external writes."""
from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy
from datetime import datetime, timedelta

from fastapi import HTTPException
from .config import settings
from .db import get_kv, set_kv, conn, _execute, _scoped_key
from .language_intake import parse_language, validate_task_creation
from .semantic_intake import semantic_document

TTL_MINUTES = 30


def require_complete_intake(parsed):
    if parsed.get('blocking_conflicts') or parsed.get('clarifications') or any(
        item.get('status') == 'needs-input' for item in parsed.get('intents') or []
    ):
        raise HTTPException(409, 'Resolve the interpretation questions before writing the schedule. Interpret again with the missing details.')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def _snapshot(rows, config):
    fields = ('id', 'project_id', 'title', 'status', 'tags', 'meta', 'start', 'end', 'kind', 'repeat', 'repeat_flag', 'is_all_day', 'priority', 'content', 'desc', 'reminders', 'column_id', 'parent_id')
    # TickTick's fresh read excludes reference items. A cached preview may still
    # contain them; their disappearance or editing cannot stale actionable work.
    # ETags are transport/version metadata and can change without a semantic task
    # change. The snapshot already fingerprints the task fields that matter to
    # interpretation, so ETags must not make a valid preview stale.
    canonical = [{k: row.get(k) for k in fields} for row in rows
                 if str(row.get('kind') or 'TEXT').upper() != 'NOTE'
                 and str(row.get('project_kind') or 'TASK').upper() != 'NOTE']
    for row in canonical:
        row['tags'] = sorted(row.get('tags') or [])
    canonical.sort(key=lambda row: str(row.get('id') or ''))
    return _fingerprint({'tasks': canonical, 'config': config})


async def review_intake(text, rows, config, now=None):
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    document, mode, warning = await semantic_document(text, now)
    parsed = parse_language(text, rows, config, now, document)
    # A one-off early next-day commitment can change tonight's sleep boundary,
    # tomorrow's wake time, morning preparation and outbound travel all at once.
    # Compile this after the general language layer so it can repair accidental
    # command-task guesses without bypassing the normal review-first contract.
    from .early_commitment import enrich_early_future_commitment
    parsed = enrich_early_future_commitment(parsed, text, rows, config, now)
    validate_task_creation(parsed)
    parsed['interpreter_mode'] = mode
    if warning:
        parsed['warnings'].append(warning)
    token = secrets.token_urlsafe(24)
    parsed['preview_id'] = token
    parsed['reviewed_at'] = now.isoformat()
    expires = now + timedelta(minutes=TTL_MINUTES)
    parsed['expires_at'] = expires.isoformat()
    # Only the latest review is valid; bounded storage and no stale old tab apply.
    set_kv('intake_review', json.dumps({'text_hash': _fingerprint(text), 'snapshot': _snapshot(rows, config), 'expires_at': expires.isoformat(), 'parsed': parsed}))
    return parsed


def apply_reviewed_intake(text, rows, config, preview_id, now=None):
    if not preview_id or len(preview_id) > 100:
        raise HTTPException(409, 'Interpret this request first, then apply the reviewed interpretation.')
    raw = get_kv('intake_review')
    if not raw:
        raise HTTPException(409, 'Interpretation expired or already applied. Interpret again.')
    now = (now or datetime.now(settings.tz)).astimezone(settings.tz)
    saved = json.loads(raw)
    if saved['parsed'].get('preview_id') != preview_id:
        raise HTTPException(409, 'A newer interpretation replaced this one. Interpret again.')
    if datetime.fromisoformat(saved['expires_at']) <= now or saved['parsed']['reviewed_at'][:10] != now.date().isoformat():
        raise HTTPException(409, 'Interpretation expired. Interpret again for the current day.')
    if saved['text_hash'] != _fingerprint(text) or saved['snapshot'] != _snapshot(rows, config):
        raise HTTPException(409, 'Your text, tasks or planning rules changed after interpretation. Interpret again before applying.')
    parsed = deepcopy(saved['parsed'])
    validate_task_creation(parsed)
    ctx = parsed.get('context')
    if ctx and ctx.get('replan_requested'):
        ctx['replan_from'] = now.isoformat()
        ctx['planning_now'] = now.isoformat()
    return parsed


def consume_intake(preview_id):
    # Consume before external writes. A retry after a partial failure must refresh
    # live state and receive a fresh reviewed interpretation, preventing duplicates.
    raw = get_kv('intake_review')
    if not raw or json.loads(raw)['parsed'].get('preview_id') != preview_id:
        raise HTTPException(409, 'Interpretation already applied or replaced. Interpret again.')
    with conn() as c:
        claimed = _execute(c, "UPDATE kv SET value = '' WHERE key = ? AND value = ?", (_scoped_key('intake_review'), raw))
        if claimed.rowcount != 1:
            raise HTTPException(409, 'Interpretation already applied. Refresh before retrying.')
