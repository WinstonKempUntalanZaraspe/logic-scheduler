from __future__ import annotations

"""Resolve semantic-model credentials without leaking the server owner's API key.

Hosted friend profiles are BYOK: every signed invited browser profile must store its own
OpenAI API key. A separately claimed owner profile may use the deployment-level
OPENAI_API_KEY without the raw key ever being returned to the browser. Legacy/server
execution with no active profile also keeps deployment-key compatibility.
"""

import os

from . import db
from .owner_profile import is_owner_profile
from .tenant import current_profile_id

PROFILE_KEY = "openai_api_key"
PROFILE_MODEL = "openai_intake_model"
DEFAULT_MODEL = "gpt-6-luna"


def _raw_env(name: str, default: str = "") -> str:
    try:
        return str(os.environ[str(name)] or "")
    except KeyError:
        return default


def semantic_api_key() -> str:
    if current_profile_id():
        if is_owner_profile():
            return _raw_env("OPENAI_API_KEY", "").strip()
        return str(db.get_kv(PROFILE_KEY, "") or "").strip()
    return _raw_env("OPENAI_API_KEY", "").strip()


def semantic_model() -> str:
    if current_profile_id() and not is_owner_profile():
        saved = str(db.get_kv(PROFILE_MODEL, "") or "").strip()
        if saved:
            return saved
    return _raw_env("AUTOSCHEDULER_INTAKE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def semantic_configured() -> bool:
    return bool(semantic_api_key() and semantic_model())


def semantic_key_status() -> dict:
    key = semantic_api_key()
    owner = bool(current_profile_id() and is_owner_profile())
    if owner:
        source = "owner-server" if key else "owner-server-missing"
    elif current_profile_id():
        source = "profile"
    else:
        source = "server" if key else "none"
    return {
        "configured": bool(key),
        "model": semantic_model(),
        "source": source,
        "managed": owner,
        "last4": key[-4:] if len(key) >= 4 else "",
    }


def save_profile_semantic_key(key: str, model: str | None = None) -> None:
    if not current_profile_id():
        raise RuntimeError("A signed user profile is required")
    if is_owner_profile():
        raise ValueError("Owner profile already uses the deployment OpenAI key")
    clean = str(key or "").strip()
    if len(clean) < 20:
        raise ValueError("Enter a valid OpenAI API key")
    db.set_kv(PROFILE_KEY, clean, secret=True)
    if model is not None:
        chosen = str(model or "").strip() or DEFAULT_MODEL
        if len(chosen) > 120:
            raise ValueError("Model name is too long")
        db.set_kv(PROFILE_MODEL, chosen)


def clear_profile_semantic_key() -> None:
    if not current_profile_id():
        raise RuntimeError("A signed user profile is required")
    if is_owner_profile():
        raise ValueError("Owner profile uses the deployment OpenAI key; there is no personal key to remove")
    # Empty secret is safer than falling through to the deployment environment because
    # friend profiles never read OPENAI_API_KEY while a profile is active.
    db.set_kv(PROFILE_KEY, "", secret=True)


__all__ = [
    "DEFAULT_MODEL", "semantic_api_key", "semantic_model", "semantic_configured",
    "semantic_key_status", "save_profile_semantic_key", "clear_profile_semantic_key",
]
