"""
Persisted optimizer / job-search prompt templates (UserPromptProfile).

Server-rendered pages and Ninja endpoints should use get_effective_prompts(request)
so DB-backed text wins over code defaults. Legacy session key optimizer_prompts is
migrated once into the profile when the DB row is still empty.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .models import AtsJudgeProfile, OptimizerWorkflow, UserPromptProfile
from .prompts import (
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_ATS_JUDGE_SYSTEM,
    DEFAULT_ATS_JUDGE_USER,
    DEFAULT_INSIGHTS_PROMPT,
    DEFAULT_INSIGHTS_SYSTEM,
    DEFAULT_INSIGHTS_USER,
    DEFAULT_COVER_LETTER_PROMPT,
    DEFAULT_COVER_LETTER_SYSTEM,
    DEFAULT_COVER_LETTER_USER,
    DEFAULT_INTERVIEW_PREP_PROMPT,
    DEFAULT_INTERVIEW_PREP_SYSTEM,
    DEFAULT_INTERVIEW_PREP_USER,
    DEFAULT_JD_CLEANSE_PROMPT,
    DEFAULT_JD_CLEANSE_SYSTEM,
    DEFAULT_JD_CLEANSE_USER,
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
    "cover_letter": DEFAULT_COVER_LETTER_PROMPT,
    "interview_prep": DEFAULT_INTERVIEW_PREP_PROMPT,
    "jd_cleanse": DEFAULT_JD_CLEANSE_PROMPT,
}

_LEGACY_FIELDS = (
    "writer",
    "ats_judge",
    "recruiter_judge",
    "matching",
    "insights",
    "cover_letter",
    "interview_prep",
    "jd_cleanse",
)

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
    "cover_letter": (
        "cover_letter",
        "cover_letter_system",
        "cover_letter_user",
        DEFAULT_COVER_LETTER_SYSTEM,
        DEFAULT_COVER_LETTER_USER,
    ),
    "interview_prep": (
        "interview_prep",
        "interview_prep_system",
        "interview_prep_user",
        DEFAULT_INTERVIEW_PREP_SYSTEM,
        DEFAULT_INTERVIEW_PREP_USER,
    ),
    "jd_cleanse": (
        "jd_cleanse",
        "jd_cleanse_system",
        "jd_cleanse_user",
        DEFAULT_JD_CLEANSE_SYSTEM,
        DEFAULT_JD_CLEANSE_USER,
    ),
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


def resolve_ats_judge_parts(profile: AtsJudgeProfile) -> Tuple[str, str, Optional[str]]:
    """
    Return (system_template, user_template, legacy_combined_or_none) for an ATS judge profile.
    """
    sys_v = (profile.ats_judge_system or "").strip()
    usr_v = (profile.ats_judge_user or "").strip()
    leg = (profile.ats_judge or "").strip()
    if sys_v or usr_v:
        return (sys_v or DEFAULT_ATS_JUDGE_SYSTEM, usr_v or DEFAULT_ATS_JUDGE_USER, None)
    if leg:
        return ("", "", leg)
    return (DEFAULT_ATS_JUDGE_SYSTEM, DEFAULT_ATS_JUDGE_USER, None)


def get_default_ats_judge_profile() -> Optional[AtsJudgeProfile]:
    """Global fallback ATS profile (is_default, slug default, or lowest pk)."""
    return (
        AtsJudgeProfile.objects.filter(is_default=True).first()
        or AtsJudgeProfile.objects.filter(slug="default").first()
        or AtsJudgeProfile.objects.order_by("pk").first()
    )


def list_ats_judge_profiles() -> List[AtsJudgeProfile]:
    return list(AtsJudgeProfile.objects.all().order_by("name"))


def get_ats_judge_profile_by_id(profile_id: Optional[int]) -> Optional[AtsJudgeProfile]:
    if not profile_id:
        return None
    try:
        return AtsJudgeProfile.objects.get(pk=int(profile_id))
    except (AtsJudgeProfile.DoesNotExist, ValueError, TypeError):
        return None


def resolve_effective_ats_judge_profile_id(
    *,
    ats_judge_profile_id: Optional[int] = None,
    workflow: Optional[OptimizerWorkflow] = None,
    workflow_ats_judge_profile_id: Optional[int] = None,
) -> Optional[int]:
    """
    Per-run ATS selection priority:
    1. Explicit ats_judge_profile_id
    2. Workflow's ats_judge_profile
    3. Global default profile
    """
    if ats_judge_profile_id:
        return int(ats_judge_profile_id)
    wf_id = workflow_ats_judge_profile_id
    if workflow is not None and workflow.ats_judge_profile_id:
        wf_id = workflow.ats_judge_profile_id
    if wf_id:
        return int(wf_id)
    default = get_default_ats_judge_profile()
    return default.pk if default else None


def get_ats_judge_profile_display(profile: AtsJudgeProfile) -> Dict[str, str]:
    """Form/display dict for one ATS profile (system, user, combined, legacy_combined)."""
    raw_legacy = (profile.ats_judge or "").strip()
    raw_sys = (profile.ats_judge_system or "").strip()
    raw_usr = (profile.ats_judge_user or "").strip()
    has_split = bool(raw_sys or raw_usr)
    eff_sys, eff_usr, eff_leg = resolve_ats_judge_parts(profile)
    if eff_leg:
        system = ""
        user = ""
    else:
        system = eff_sys
        user = eff_usr
    if has_split:
        combined = (raw_sys or DEFAULT_ATS_JUDGE_SYSTEM) + "\n\n" + (raw_usr or DEFAULT_ATS_JUDGE_USER)
    elif raw_legacy:
        combined = raw_legacy
    else:
        combined = DEFAULT_ATS_JUDGE_PROMPT
    return {
        "ats_judge": combined,
        "ats_judge_system": system,
        "ats_judge_user": user,
        "ats_judge_legacy_combined": "" if has_split else (profile.ats_judge or ""),
    }


def save_ats_judge_profile(
    profile: AtsJudgeProfile,
    *,
    name: Optional[str] = None,
    ats_judge: str = "",
    ats_judge_system: str = "",
    ats_judge_user: str = "",
) -> AtsJudgeProfile:
    """Persist triple using split-wins-over-legacy rules (mirrors prompt library)."""
    sys_v = (ats_judge_system or "").strip()
    usr_v = (ats_judge_user or "").strip()
    leg_v = (ats_judge or "").strip()
    if sys_v or usr_v:
        profile.ats_judge = ""
        profile.ats_judge_system = sys_v
        profile.ats_judge_user = usr_v
    elif leg_v:
        profile.ats_judge = leg_v
        profile.ats_judge_system = ""
        profile.ats_judge_user = ""
    else:
        profile.ats_judge = ""
        profile.ats_judge_system = ""
        profile.ats_judge_user = ""
    if name is not None and name.strip():
        profile.name = name.strip()
    profile.save()
    return profile


def _user_prompt_profile_has_ats(profile: UserPromptProfile) -> bool:
    return any(
        (getattr(profile, f) or "").strip()
        for f in ("ats_judge", "ats_judge_system", "ats_judge_user")
    )


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

    System/user textareas show the effective templates (including code defaults when
    the profile row is still empty), so the Prompt Library matches runtime behavior.
    """
    profile = _get_or_create_profile()
    if request is not None and hasattr(request, "session") and _profile_is_empty(profile):
        _migrate_session_to_profile(request, profile)
        profile.refresh_from_db()

    out: Dict[str, str] = {}
    default_ats = get_default_ats_judge_profile()
    for kind, (leg_a, sys_a, usr_a, def_sys, def_user) in _PROMPT_SPEC.items():
        if kind == "ats_judge" and default_ats is not None:
            out.update(get_ats_judge_profile_display(default_ats))
            continue
        raw_legacy = (getattr(profile, leg_a) or "").strip()
        raw_sys = (getattr(profile, sys_a) or "").strip()
        raw_usr = (getattr(profile, usr_a) or "").strip()

        has_split = bool(raw_sys or raw_usr)
        eff_sys, eff_usr, eff_leg = resolve_prompt_parts(profile, kind)

        # Form fields: show effective system/user (code defaults when DB is empty), not raw blanks.
        if eff_leg:
            out[f"{kind}_system"] = ""
            out[f"{kind}_user"] = ""
        else:
            out[f"{kind}_system"] = eff_sys
            out[f"{kind}_user"] = eff_usr

        if has_split:
            out[kind] = (raw_sys or def_sys) + "\n\n" + (raw_usr or def_user)
        elif raw_legacy:
            out[kind] = raw_legacy
        else:
            out[kind] = DEFAULT_PROMPTS[kind]

        out[f"{kind}_legacy_combined"] = "" if has_split else (getattr(profile, leg_a) or "")

    return out


def save_prompts_to_profile(request: Optional[Any], prompts: Dict[str, str]) -> None:
    """Persist prompt dict to UserPromptProfile (pk=1). ATS judge uses AtsJudgeProfile library."""
    profile = _get_or_create_profile()
    for kind, (leg_a, sys_a, usr_a, _ds, _du) in _PROMPT_SPEC.items():
        if kind == "ats_judge":
            continue
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


def _ats_judge_prompt_state(
    *,
    prompts_override: Optional[Dict[str, str]],
    request: Optional[Any],
    ats_judge_profile_id: Optional[int] = None,
    workflow: Optional[OptimizerWorkflow] = None,
    workflow_ats_judge_profile_id: Optional[int] = None,
) -> Dict[str, str]:
    from .prompts import DEFAULT_ATS_JUDGE_PROMPT as _DEF_ATS

    prefix = "ats_judge_prompt_"
    ov = (prompts_override or {}).get("ats_judge") if prompts_override else None
    if ov and str(ov).strip():
        leg = str(ov).strip()
        return {
            f"{prefix}system": "",
            f"{prefix}user": "",
            f"{prefix}legacy": leg,
            f"{prefix}template": leg or _DEF_ATS,
        }

    effective_id = resolve_effective_ats_judge_profile_id(
        ats_judge_profile_id=ats_judge_profile_id,
        workflow=workflow,
        workflow_ats_judge_profile_id=workflow_ats_judge_profile_id,
    )
    ats_prof = get_ats_judge_profile_by_id(effective_id)
    if ats_prof is not None:
        s, u, leg = resolve_ats_judge_parts(ats_prof)
    else:
        prof = profile_for_llm(request)
        if _user_prompt_profile_has_ats(prof):
            s, u, leg = resolve_prompt_parts(prof, "ats_judge")
        else:
            s, u, leg = DEFAULT_ATS_JUDGE_SYSTEM, DEFAULT_ATS_JUDGE_USER, None

    return {
        f"{prefix}system": s or "",
        f"{prefix}user": u or "",
        f"{prefix}legacy": leg or "",
        f"{prefix}template": (leg if leg else (s + "\n\n" + u)).strip() or _DEF_ATS,
    }


def build_optimizer_graph_prompt_state(
    prompts_override: Optional[Dict[str, str]] = None,
    request: Optional[Any] = None,
    *,
    ats_judge_profile_id: Optional[int] = None,
    workflow: Optional[OptimizerWorkflow] = None,
    workflow_ats_judge_profile_id: Optional[int] = None,
) -> Dict[str, str]:
    """
    LangGraph initial_state keys for writer / ATS / recruiter prompts.
    Optional prompts_override keys: writer, ats_judge, recruiter_judge (each forces legacy single-template mode).
    ATS prompt resolves from ats_judge_profile_id, workflow default, or global default profile.
    """
    from .prompts import (
        DEFAULT_RECRUITER_JUDGE_PROMPT as _DEF_REC,
        DEFAULT_WRITER_PROMPT as _DEF_W,
    )

    prof = profile_for_llm(request)
    out: Dict[str, str] = {}
    out.update(
        _ats_judge_prompt_state(
            prompts_override=prompts_override,
            request=request,
            ats_judge_profile_id=ats_judge_profile_id,
            workflow=workflow,
            workflow_ats_judge_profile_id=workflow_ats_judge_profile_id,
        )
    )
    for kind, prefix in (
        ("writer", "writer_prompt_"),
        ("recruiter_judge", "recruiter_judge_prompt_"),
    ):
        s, u, leg = resolve_prompt_parts(prof, kind)
        override_key = {"writer": "writer", "recruiter_judge": "recruiter_judge"}[kind]
        ov = (prompts_override or {}).get(override_key) if prompts_override else None
        if ov and str(ov).strip():
            s, u, leg = "", "", str(ov).strip()
        out[f"{prefix}system"] = s or ""
        out[f"{prefix}user"] = u or ""
        out[f"{prefix}legacy"] = leg or ""
        _fallback = _DEF_W if kind == "writer" else _DEF_REC
        out[f"{prefix}template"] = (leg if leg else (s + "\n\n" + u)).strip() or _fallback
    return out


def build_jd_cleanse_llm_messages(
    request: Optional[Any],
    *,
    title: str,
    job_description: str,
):
    """
    LangChain messages for Ollama Local JD cleansing (vetting + pipeline optimizer paths).
    Uses Prompt Library kind "jd_cleanse"; placeholders: {title}, {job_description}.
    """
    from .agents import build_llm_messages_for_prompt

    prof = profile_for_llm(request)
    s, u, leg = resolve_prompt_parts(prof, "jd_cleanse")
    return build_llm_messages_for_prompt(
        legacy_combined=leg or None,
        system_template=s or None,
        user_template=u or None,
        format_kwargs={"title": title or "", "job_description": job_description or ""},
    )
