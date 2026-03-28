"""
Persisted optimizer / job-search prompt templates (UserPromptProfile).

Server-rendered pages and Ninja endpoints should use get_effective_prompts(request)
so DB-backed text wins over code defaults. Legacy session key optimizer_prompts is
migrated once into the profile when the DB row is still empty.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .models import UserPromptProfile
from .prompts import (
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_ATS_JUDGE_SYSTEM,
    DEFAULT_ATS_JUDGE_USER,
    DEFAULT_INSIGHTS_PROMPT,
    DEFAULT_INSIGHTS_SYSTEM,
    DEFAULT_INSIGHTS_USER,
    DEFAULT_MATCHING_PROMPT,
    DEFAULT_MATCHING_SYSTEM,
    DEFAULT_MATCHING_USER,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_SYSTEM,
    DEFAULT_RECRUITER_JUDGE_USER,
    DEFAULT_WRITER_PROMPT,
    DEFAULT_WRITER_SYSTEM,
    DEFAULT_WRITER_USER,
)

DEFAULT_PROMPTS: Dict[str, str] = {
    "writer": DEFAULT_WRITER_PROMPT,
    "ats_judge": DEFAULT_ATS_JUDGE_PROMPT,
    "recruiter_judge": DEFAULT_RECRUITER_JUDGE_PROMPT,
    "matching": DEFAULT_MATCHING_PROMPT,
    "insights": DEFAULT_INSIGHTS_PROMPT,
}

_LEGACY_FIELDS = ("writer", "ats_judge", "recruiter_judge", "matching", "insights")

_PROMPT_SPEC = {
    "writer": ("writer", "writer_system", "writer_user", DEFAULT_WRITER_SYSTEM, DEFAULT_WRITER_USER),
    "ats_judge": ("ats_judge", "ats_judge_system", "ats_judge_user", DEFAULT_ATS_JUDGE_SYSTEM, DEFAULT_ATS_JUDGE_USER),
    "recruiter_judge": (
        "recruiter_judge",
        "recruiter_judge_system",
        "recruiter_judge_user",
        DEFAULT_RECRUITER_JUDGE_SYSTEM,
        DEFAULT_RECRUITER_JUDGE_USER,
    ),
    "matching": ("matching", "matching_system", "matching_user", DEFAULT_MATCHING_SYSTEM, DEFAULT_MATCHING_USER),
    "insights": ("insights", "insights_system", "insights_user", DEFAULT_INSIGHTS_SYSTEM, DEFAULT_INSIGHTS_USER),
}


def _get_or_create_profile() -> UserPromptProfile:
    obj, _ = UserPromptProfile.objects.get_or_create(pk=1)
    return obj


def _profile_is_empty(profile: UserPromptProfile) -> bool:
    split_fields = []
    for _leg, sys_a, usr_a, _ds, _du in _PROMPT_SPEC.values():
        split_fields.extend([sys_a, usr_a])
    if any((getattr(profile, f) or "").strip() for f in _LEGACY_FIELDS):
        return False
    if any((getattr(profile, f) or "").strip() for f in split_fields):
        return False
    return True


def _migrate_session_to_profile(request, profile: UserPromptProfile) -> bool:
    sess = request.session.get("optimizer_prompts")
    if not isinstance(sess, dict):
        return False
    changed = False
    for f in _LEGACY_FIELDS:
        val = sess.get(f)
        if val and str(val).strip():
            setattr(profile, f, str(val))
            changed = True
    if changed:
        profile.save()
        request.session.pop("optimizer_prompts", None)
        request.session.modified = True
    return changed


def resolve_prompt_parts(
    profile: UserPromptProfile,
    kind: str,
) -> Tuple[str, str, Optional[str]]:
    """
    Return (system_template, user_template, legacy_combined_or_none).

    If legacy_combined is set, callers should send a single HumanMessage with that
    template (ignore system/user). Otherwise use SystemMessage + HumanMessage with
    the returned strings (after filling empty split parts with defaults).
    """
    leg_a, sys_a, usr_a, def_sys, def_user = _PROMPT_SPEC[kind]
    sys_v = (getattr(profile, sys_a) or "").strip()
    usr_v = (getattr(profile, usr_a) or "").strip()
    leg = (getattr(profile, leg_a) or "").strip()
    if sys_v or usr_v:
        return (sys_v or def_sys, usr_v or def_user, None)
    if leg:
        return ("", "", leg)
    return (def_sys, def_user, None)


def get_effective_prompts(request: Optional[Any]) -> Dict[str, str]:
    """
    Return merged prompts for forms and display: legacy combined strings plus
    system/user fields. Keys: writer, writer_system, writer_user, ...
    """
    profile = _get_or_create_profile()
    if request is not None and hasattr(request, "session") and _profile_is_empty(profile):
        _migrate_session_to_profile(request, profile)
        profile.refresh_from_db()

    out: Dict[str, str] = {}
    for kind, (leg_a, sys_a, usr_a, def_sys, def_user) in _PROMPT_SPEC.items():
        raw_legacy = (getattr(profile, leg_a) or "").strip()
        raw_sys = (getattr(profile, sys_a) or "").strip()
        raw_usr = (getattr(profile, usr_a) or "").strip()

        out[f"{kind}_system"] = getattr(profile, sys_a) or ""
        out[f"{kind}_user"] = getattr(profile, usr_a) or ""

        has_split = bool(raw_sys or raw_usr)
        if has_split:
            out[kind] = (raw_sys or def_sys) + "\n\n" + (raw_usr or def_user)
        elif raw_legacy:
            out[kind] = raw_legacy
        else:
            out[kind] = DEFAULT_PROMPTS[kind]

        out[f"{kind}_legacy_combined"] = "" if has_split else (getattr(profile, leg_a) or "")

    return out


def save_prompts_to_profile(request: Optional[Any], prompts: Dict[str, str]) -> None:
    """Persist prompt dict to UserPromptProfile (pk=1)."""
    profile = _get_or_create_profile()
    for kind, (leg_a, sys_a, usr_a, _ds, _du) in _PROMPT_SPEC.items():
        if leg_a in prompts:
            setattr(profile, leg_a, prompts.get(leg_a) or "")
        if sys_a in prompts:
            setattr(profile, sys_a, prompts.get(sys_a) or "")
        if usr_a in prompts:
            setattr(profile, usr_a, prompts.get(usr_a) or "")
    profile.save()
    if request is not None and hasattr(request, "session"):
        request.session.pop("optimizer_prompts", None)
        request.session.modified = True


def clear_all_prompts_in_profile(request: Optional[Any]) -> None:
    """Clear every prompt field so code defaults (including system/user splits) apply."""
    profile = _get_or_create_profile()
    for _kind, (leg_a, sys_a, usr_a, _ds, _du) in _PROMPT_SPEC.items():
        setattr(profile, leg_a, "")
        setattr(profile, sys_a, "")
        setattr(profile, usr_a, "")
    profile.save()
    if request is not None and hasattr(request, "session"):
        request.session.pop("optimizer_prompts", None)
        request.session.modified = True


def profile_for_llm(request: Optional[Any]) -> UserPromptProfile:
    """Fresh profile row for resolve_prompt_parts (after optional session migrate)."""
    get_effective_prompts(request)
    return _get_or_create_profile()


def build_optimizer_graph_prompt_state(
    prompts_override: Optional[Dict[str, str]],
    request: Optional[Any] = None,
) -> Dict[str, str]:
    """
    LangGraph initial_state keys for writer / ATS / recruiter prompts.
    Optional prompts_override keys: writer, ats_judge, recruiter_judge (each forces legacy single-template mode).
    """
    from .prompts import (
        DEFAULT_ATS_JUDGE_PROMPT as _DEF_ATS,
        DEFAULT_RECRUITER_JUDGE_PROMPT as _DEF_REC,
        DEFAULT_WRITER_PROMPT as _DEF_W,
    )

    prof = profile_for_llm(request)
    out: Dict[str, str] = {}
    for kind, prefix in (
        ("writer", "writer_prompt_"),
        ("ats_judge", "ats_judge_prompt_"),
        ("recruiter_judge", "recruiter_judge_prompt_"),
    ):
        s, u, leg = resolve_prompt_parts(prof, kind)
        override_key = {"writer": "writer", "ats_judge": "ats_judge", "recruiter_judge": "recruiter_judge"}[
            kind
        ]
        ov = (prompts_override or {}).get(override_key) if prompts_override else None
        if ov and str(ov).strip():
            s, u, leg = "", "", str(ov).strip()
        out[f"{prefix}system"] = s or ""
        out[f"{prefix}user"] = u or ""
        out[f"{prefix}legacy"] = leg or ""
        _fallback = _DEF_W if kind == "writer" else _DEF_ATS if kind == "ats_judge" else _DEF_REC
        out[f"{prefix}template"] = (leg if leg else (s + "\n\n" + u)).strip() or _fallback
    return out
