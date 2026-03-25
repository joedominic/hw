"""
Persisted optimizer / job-search prompt templates (UserPromptProfile).

Server-rendered pages and Ninja endpoints should use get_effective_prompts(request)
so DB-backed text wins over code defaults. Legacy session key optimizer_prompts is
migrated once into the profile when the DB row is still empty.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .models import UserPromptProfile
from .prompts import (
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_INSIGHTS_PROMPT,
    DEFAULT_MATCHING_PROMPT,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_WRITER_PROMPT,
)

DEFAULT_PROMPTS: Dict[str, str] = {
    "writer": DEFAULT_WRITER_PROMPT,
    "ats_judge": DEFAULT_ATS_JUDGE_PROMPT,
    "recruiter_judge": DEFAULT_RECRUITER_JUDGE_PROMPT,
    "matching": DEFAULT_MATCHING_PROMPT,
    "insights": DEFAULT_INSIGHTS_PROMPT,
}

_PROMPT_FIELDS = ("writer", "ats_judge", "recruiter_judge", "matching", "insights")


def _get_or_create_profile() -> UserPromptProfile:
    obj, _ = UserPromptProfile.objects.get_or_create(pk=1)
    return obj


def _profile_is_empty(profile: UserPromptProfile) -> bool:
    return not any((getattr(profile, f) or "").strip() for f in _PROMPT_FIELDS)


def _migrate_session_to_profile(request, profile: UserPromptProfile) -> bool:
    sess = request.session.get("optimizer_prompts")
    if not isinstance(sess, dict):
        return False
    changed = False
    for f in _PROMPT_FIELDS:
        val = sess.get(f)
        if val and str(val).strip():
            setattr(profile, f, str(val))
            changed = True
    if changed:
        profile.save()
        request.session.pop("optimizer_prompts", None)
        request.session.modified = True
    return changed


def get_effective_prompts(request: Optional[Any]) -> Dict[str, str]:
    """
    Return merged prompts: non-empty UserPromptProfile fields override code defaults.
    Migrates legacy session data once when the profile row has no text yet.
    """
    profile = _get_or_create_profile()
    if request is not None and hasattr(request, "session") and _profile_is_empty(profile):
        _migrate_session_to_profile(request, profile)
        profile.refresh_from_db()

    out: Dict[str, str] = {}
    for f in _PROMPT_FIELDS:
        raw = (getattr(profile, f) or "").strip()
        out[f] = raw if raw else DEFAULT_PROMPTS[f]
    return out


def save_prompts_to_profile(request: Optional[Any], prompts: Dict[str, str]) -> None:
    """Persist prompt dict to UserPromptProfile (pk=1)."""
    profile = _get_or_create_profile()
    for f in _PROMPT_FIELDS:
        setattr(profile, f, (prompts.get(f) or "") if prompts else "")
    profile.save()
    if request is not None and hasattr(request, "session"):
        request.session.pop("optimizer_prompts", None)
        request.session.modified = True
