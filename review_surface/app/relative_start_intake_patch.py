from __future__ import annotations

"""Final task-level relative-start semantics.

Applies one consistent submission-clock interpretation after all specialized intake
wrappers have finished:
- prospective "right now" => earliest submission + 2 minutes;
- explicit relative start => earliest submission + requested offset;
- reported activity already underway => untouched.
"""

import json
from datetime import datetime

from .config import settings
from . import intake_contract as contract
from .relative_start import start_at

_INSTALLED = False


def _dt(value):
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out.replace(tzinfo=settings.tz) if out.tzinfo is None else out.astimezone(settings.tz)
    except Exception:
        return None


def apply_relative_starts(parsed: dict, original_text: str, submitted_at: datetime) -> dict:
    changed = False
    for change in parsed.get("tasks") or []:
        if change.get("action") not in {"create", "update"}:
            continue
        source = str(change.get("line") or change.get("title") or "")
        desired, start_source, minutes = start_at(source, submitted_at, 2)
        if desired is None:
            continue
        patch = change.setdefault("meta_patch", {})
        existing = _dt(patch.get("earliest"))
        # User-authored relative timing is a lower bound. Preserve any stricter
        # already-known hard constraint rather than weakening it.
        effective = max(existing, desired) if existing else desired
        patch["earliest"] = effective.isoformat()
        patch["_relative_start_minutes"] = int(minutes or 0)
        patch["_relative_start_source"] = start_source
        patch["_relative_start_requested_at"] = submitted_at.isoformat()
        if start_source == "immediate-lead":
            patch["timing_preference"] = "asap"
        changed = True

    if changed:
        ctx = parsed.get("context") or {
            "date": submitted_at.date().isoformat(),
            "source": "quick-dump",
        }
        ctx.setdefault("planning_now", submitted_at.isoformat())
        ctx.setdefault("timezone", settings.timezone)
        parsed["context"] = ctx
        parsed.setdefault("notes", []).append(
            "Relative starts use the prompt submission clock. Prospective “right now” gets 2 minutes of execution headroom; explicit offsets such as “in 5 minutes” override that default."
        )
        parsed["notes"] = list(dict.fromkeys(parsed["notes"]))
    return parsed


def install_relative_start_intake_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    base = contract.review_intake

    async def review(text, rows, config, now=None):
        stamp = (now or datetime.now(settings.tz)).astimezone(settings.tz)
        parsed = await base(text, rows, config, stamp)
        before = json.dumps(parsed, sort_keys=True, default=str)
        apply_relative_starts(parsed, text, stamp)
        after = json.dumps(parsed, sort_keys=True, default=str)
        if after != before and parsed.get("preview_id") and parsed.get("expires_at"):
            try:
                contract.set_kv(
                    "intake_review",
                    json.dumps({
                        "text_hash": contract._fingerprint(text),
                        "snapshot": contract._snapshot(rows, config),
                        "expires_at": parsed["expires_at"],
                        "parsed": parsed,
                    }),
                )
            except Exception:
                pass
        return parsed

    review._relative_start_intake = True
    contract.review_intake = review


__all__ = ["apply_relative_starts", "install_relative_start_intake_patch"]
