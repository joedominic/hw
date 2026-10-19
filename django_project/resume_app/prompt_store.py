"""
System-wide optimizer / job-search prompt templates (SystemPromptProfile).

Staff edit prompts via the Prompt Library. All users resolve the same templates
(empty DB fields fall back to code defaults in prompts.py). ATS judge prompts
live in global AtsJudgeProfile rows (owner=null).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

from .models import AtsJudgeProfile, OptimizerWorkflow, PromptVersionHistory, SystemPromptProfile
from .prompts import (
    DEFAULT_ATS_JUDGE_PROMPT,
    DEFAULT_ATS_JUDGE_SYSTEM,
    DEFAULT_ATS_JUDGE_USER,
    DEFAULT_CAREER_PROFILER_PROMPT,
    DEFAULT_CAREER_PROFILER_SYSTEM,
    DEFAULT_CAREER_PROFILER_USER,
    DEFAULT_COVER_LETTER_PROMPT,
    DEFAULT_COVER_LETTER_SYSTEM,
    DEFAULT_COVER_LETTER_USER,
    DEFAULT_FIT_CHECK_PROMPT,
    DEFAULT_FIT_CHECK_SYSTEM,
    DEFAULT_FIT_CHECK_USER,
    DEFAULT_INSIGHTS_PROMPT,
    DEFAULT_INSIGHTS_SYSTEM,
    DEFAULT_INSIGHTS_USER,
    DEFAULT_INTERVIEW_PREP_PROMPT,
    DEFAULT_INTERVIEW_PREP_SYSTEM,
    DEFAULT_INTERVIEW_PREP_USER,
    DEFAULT_JD_CLEANSE_PROMPT,
    DEFAULT_JD_CLEANSE_SYSTEM,
    DEFAULT_JD_CLEANSE_USER,
    DEFAULT_JOB_AUTHENTICITY_PROMPT,
    DEFAULT_JOB_AUTHENTICITY_SYSTEM,
    DEFAULT_JOB_AUTHENTICITY_USER,
    DEFAULT_MATCHING_PROMPT,
    DEFAULT_MATCHING_SYSTEM,
    DEFAULT_MATCHING_USER,
    DEFAULT_PIPELINE_RESUME_REFINE_PROMPT,
    DEFAULT_PIPELINE_RESUME_REFINE_SYSTEM,
    DEFAULT_PIPELINE_RESUME_REFINE_USER,
    DEFAULT_PIPELINE_SKILL_EXTRACT_PROMPT,
    DEFAULT_PIPELINE_SKILL_EXTRACT_SYSTEM,
    DEFAULT_PIPELINE_SKILL_EXTRACT_USER,
    DEFAULT_RECRUITER_JUDGE_PROMPT,
    DEFAULT_RECRUITER_JUDGE_SYSTEM,
    DEFAULT_RECRUITER_JUDGE_USER,
    DEFAULT_RESUME_JUDGE_PROMPT,
    DEFAULT_RESUME_JUDGE_SYSTEM,
    DEFAULT_RESUME_JUDGE_USER,
    DEFAULT_SKILL_RADAR_PROMPT,
    DEFAULT_SKILL_RADAR_SYSTEM,
    DEFAULT_SKILL_RADAR_USER,
    DEFAULT_WRITER_PROMPT,
    DEFAULT_WRITER_SYSTEM,
    DEFAULT_WRITER_USER,
)

PromptProfileLike = Union[SystemPromptProfile, Any]

DEFAULT_PROMPTS: Dict[str, str] = {
    "writer": DEFAULT_WRITER_PROMPT,
    "ats_judge": DEFAULT_ATS_JUDGE_PROMPT,
    "recruiter_judge": DEFAULT_RECRUITER_JUDGE_PROMPT,
    "matching": DEFAULT_MATCHING_PROMPT,
    "insights": DEFAULT_INSIGHTS_PROMPT,
    "cover_letter": DEFAULT_COVER_LETTER_PROMPT,
    "interview_prep": DEFAULT_INTERVIEW_PREP_PROMPT,
    "jd_cleanse": DEFAULT_JD_CLEANSE_PROMPT,
    "skill_radar": DEFAULT_SKILL_RADAR_PROMPT,
    "career_profiler": DEFAULT_CAREER_PROFILER_PROMPT,
    "job_authenticity": DEFAULT_JOB_AUTHENTICITY_PROMPT,
    "pipeline_skill_extract": DEFAULT_PIPELINE_SKILL_EXTRACT_PROMPT,
    "fit_check": DEFAULT_FIT_CHECK_PROMPT,
    "pipeline_resume_refine": DEFAULT_PIPELINE_RESUME_REFINE_PROMPT,
    "resume_judge": DEFAULT_RESUME_JUDGE_PROMPT,
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
    "skill_radar",
    "career_profiler",
    "job_authenticity",
    "pipeline_skill_extract",
    "fit_check",
    "pipeline_resume_refine",
    "resume_judge",
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
    "skill_radar": (
        "skill_radar",
        "skill_radar_system",
        "skill_radar_user",
        DEFAULT_SKILL_RADAR_SYSTEM,
        DEFAULT_SKILL_RADAR_USER,
    ),
    "career_profiler": (
        "career_profiler",
        "career_profiler_system",
        "career_profiler_user",
        DEFAULT_CAREER_PROFILER_SYSTEM,
        DEFAULT_CAREER_PROFILER_USER,
    ),
    "job_authenticity": (
        "job_authenticity",
        "job_authenticity_system",
        "job_authenticity_user",
        DEFAULT_JOB_AUTHENTICITY_SYSTEM,
        DEFAULT_JOB_AUTHENTICITY_USER,
    ),
    "pipeline_skill_extract": (
        "pipeline_skill_extract",
        "pipeline_skill_extract_system",
        "pipeline_skill_extract_user",
        DEFAULT_PIPELINE_SKILL_EXTRACT_SYSTEM,
        DEFAULT_PIPELINE_SKILL_EXTRACT_USER,
    ),
    "fit_check": (
        "fit_check",
        "fit_check_system",
        "fit_check_user",
        DEFAULT_FIT_CHECK_SYSTEM,
        DEFAULT_FIT_CHECK_USER,
    ),
    "pipeline_resume_refine": (
        "pipeline_resume_refine",
        "pipeline_resume_refine_system",
        "pipeline_resume_refine_user",
        DEFAULT_PIPELINE_RESUME_REFINE_SYSTEM,
        DEFAULT_PIPELINE_RESUME_REFINE_USER,
    ),
    "resume_judge": (
        "resume_judge",
        "resume_judge_system",
        "resume_judge_user",
        DEFAULT_RESUME_JUDGE_SYSTEM,
        DEFAULT_RESUME_JUDGE_USER,
    ),
}

PROMPT_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "writer",
        "name": "Resume Writer",
        "category": "Optimization Pipeline",
        "description": "Synthesizes resume revisions incorporating job descriptions, feedback, and skill keywords.",
        "default_engine": "cloud",
        "icon": "edit_note",
        "known_vars": [
            "resume_text",
            "source_resume_text",
            "optimized_resume",
            "job_description",
            "full_job_description",
            "feedback",
            "optimization_notes",
            "pipeline_skills_json",
            "job_highlights",
            "retrieval_context",
        ],
    },
    {
        "key": "resume_judge",
        "name": "Unified Resume Judge (ATS + Recruiter)",
        "category": "Optimization Pipeline",
        "description": "Dual-lens evaluation assessing technical ATS keyword parseability and recruiter executive impact in a single turn.",
        "default_engine": "cloud",
        "icon": "gavel",
        "known_vars": [
            "optimized_resume",
            "job_description",
            "job_title",
            "score_threshold",
            "ats_profile_directives",
            "recruiter_profile_directives",
        ],
    },
    {
        "key": "pipeline_resume_refine",
        "name": "Targeted Keyword Refiner",
        "category": "Optimization Pipeline",
        "description": "Groups and polishes shortlisted pipeline keyword themes into tailored resume bullet stems.",
        "default_engine": "local",
        "icon": "tune",
        "known_vars": ["job_titles", "ranked_keywords", "job_count"],
    },
    {
        "key": "job_authenticity",
        "name": "Labor Compliance & Authenticity Auditor",
        "category": "Evaluation & Vetting",
        "description": "Deterministic 7-point labor compliance auditor detecting DOL ETA-9089 PERM testing, salary locks, and internal placeholders.",
        "default_engine": "local",
        "icon": "verified_user",
        "known_vars": ["title", "company", "location", "job_description"],
    },
    {
        "key": "matching",
        "name": "Job Matching Scorer",
        "category": "Evaluation & Vetting",
        "description": "Evaluates interview probability and semantic alignment between resume and vetted job.",
        "default_engine": "local",
        "icon": "compare_arrows",
        "known_vars": ["resume_text", "job_description", "title", "company"],
    },
    {
        "key": "pipeline_skill_extract",
        "name": "Pipeline Skill Extractor",
        "category": "Evaluation & Vetting",
        "description": "Extracts discrete hard skills, methodologies, soft skills, outcomes, and action verbs from job batches into strict JSON.",
        "default_engine": "local",
        "icon": "playlist_add_check",
        "known_vars": ["job_descriptions"],
    },
    {
        "key": "insights",
        "name": "Batch Job Insights",
        "category": "Evaluation & Vetting",
        "description": "Extracts highlights, key requirements, and quick evaluation metrics from job descriptions.",
        "default_engine": "cloud",
        "icon": "insights",
        "known_vars": ["job_description", "title", "company"],
    },
    {
        "key": "jd_cleanse",
        "name": "JD Cleanser & Parser",
        "category": "Evaluation & Vetting",
        "description": "Strips recruiter boilerplate, EEO statements, and cleans messy job postings into pure requirements.",
        "default_engine": "local",
        "icon": "cleaning_services",
        "known_vars": ["title", "job_description"],
    },
    {
        "key": "career_profiler",
        "name": "Universal Career Profiler",
        "category": "Candidate & Career Tools",
        "description": "Synthesizes universal career archetypes across industries and provisions search profiles.",
        "default_engine": "cloud",
        "icon": "psychology",
        "known_vars": ["resume_text"],
    },
    {
        "key": "skill_radar",
        "name": "Skill Radar Analyzer",
        "category": "Candidate & Career Tools",
        "description": "Audits candidate skill profile against market demand and identifies strategic gaps.",
        "default_engine": "cloud",
        "icon": "radar",
        "known_vars": ["resume_text"],
    },
    {
        "key": "cover_letter",
        "name": "Cover Letter Architect",
        "category": "Candidate & Career Tools",
        "description": "Generates tailored, persuasive cover letters bridging resume experience to target role.",
        "default_engine": "cloud",
        "icon": "mail",
        "known_vars": ["resume_text", "job_description", "company_name", "role_title"],
    },
    {
        "key": "interview_prep",
        "name": "Interview Prep Coach",
        "category": "Candidate & Career Tools",
        "description": "Generates high-yield behavioral and technical interview questions with STAR coaching points.",
        "default_engine": "cloud",
        "icon": "quiz",
        "known_vars": ["resume_text", "job_description", "company_name", "role_title"],
    },
]

PROMPT_TO_QUERY_KIND_MAP: Dict[str, str] = {
    "writer": "optimizer_writer",
    "resume_judge": "optimizer_ats_judge",
    "ats_judge": "optimizer_ats_judge",
    "recruiter_judge": "optimizer_recruiter_judge",
    "matching": "matching",
    "insights": "job_insights",
    "jd_cleanse": "jd_cleanse",
    "career_profiler": "career_profiler",
    "skill_radar": "pipeline_skill_extract",
    "cover_letter": "cover_letter",
    "interview_prep": "interview_prep",
    "job_authenticity": "job_authenticity",
    "pipeline_skill_extract": "pipeline_skill_extract",
    "fit_check": "fit_check",
    "pipeline_resume_refine": "pipeline_resume_refine",
}


def get_available_model_options(user, request=None) -> List[Dict[str, Any]]:
    """
    Returns unified list of available LLM models configured in settings,
    including priority preference rows, connected BYOK provider configs, and platform defaults.
    """
    import logging
    _log = logging.getLogger(__name__)

    from .models import LLMProviderConfig
    from .llm.session import get_provider_preference_rows
    from .llm.gateway import SYSTEM_DEFAULT_MODELS

    available: List[Dict[str, Any]] = []
    seen = set()

    # 1. Models explicitly configured in user provider preference rows
    if user and getattr(user, "is_authenticated", False):
        try:
            pref_rows = list(get_provider_preference_rows(user).order_by("priority", "id"))
            for pref in pref_rows:
                cfg = pref.provider_config
                prov = (cfg.provider or "").strip()
                m = (pref.model or cfg.default_model or "").strip()
                if prov and (prov, m) not in seen:
                    seen.add((prov, m))
                    is_local = bool(pref.is_local) or (prov.lower() == "ollama local")
                    label = f"{prov} — {m}" if m else f"{prov} (default model)"
                    available.append({
                        "value": f"{prov}::{m}",
                        "label": label,
                        "provider": prov,
                        "model": m,
                        "is_local": is_local,
                    })
        except Exception as e:
            _log.debug("Failed querying LLMProviderPreference: %s", e)

    # 2. Configured provider keys (BYOK) with default models or session-cached models
    if user and getattr(user, "is_authenticated", False):
        try:
            cached_models: Dict[str, List[str]] = {}
            if request and hasattr(request, "session"):
                cached_models = request.session.get("settings_provider_models_map") or {}

            configs = LLMProviderConfig.objects.for_user(user).exclude(encrypted_api_key="")
            for cfg in configs:
                prov = (cfg.provider or "").strip()
                if not prov:
                    continue
                # check cached models from provider
                for m in (cached_models.get(prov) or [])[:15]:
                    if (prov, m) not in seen:
                        seen.add((prov, m))
                        available.append({
                            "value": f"{prov}::{m}",
                            "label": f"{prov} — {m}",
                            "provider": prov,
                            "model": m,
                            "is_local": prov.lower() == "ollama local",
                        })
                # default model if any
                dm = (cfg.default_model or "").strip()
                if dm and (prov, dm) not in seen:
                    seen.add((prov, dm))
                    available.append({
                        "value": f"{prov}::{dm}",
                        "label": f"{prov} — {dm}",
                        "provider": prov,
                        "model": dm,
                        "is_local": prov.lower() == "ollama local",
                    })
        except Exception as e:
            _log.debug("Failed querying LLMProviderConfig: %s", e)

    # 3. Platform default fallback candidates
    for _qk, specs in SYSTEM_DEFAULT_MODELS.items():
        for spec in specs:
            prov = spec.get("provider", "").strip()
            m = spec.get("model", "").strip()
            if prov and (prov, m) not in seen:
                seen.add((prov, m))
                available.append({
                    "value": f"{prov}::{m}",
                    "label": f"{prov} — {m}",
                    "provider": prov,
                    "model": m,
                    "is_local": prov.lower() == "ollama local",
                })

    return available


def set_prompt_model_preference(user, prompt_key: str, provider: str, model: str) -> None:
    """
    Persists per-prompt model routing to TenantPromptModelPreference.
    Synchronizes aliases (e.g. matching aliases). If provider is blank, clears override.
    """
    from .models import TenantPromptModelPreference

    provider = (provider or "").strip()
    model = (model or "").strip()
    qk = PROMPT_TO_QUERY_KIND_MAP.get(prompt_key, prompt_key)

    target_keys = {qk, prompt_key}
    if prompt_key == "matching" or qk == "matching":
        target_keys.update({
            "fit_check",
            "pipeline_vetting_matching",
            "jobs_ai_match",
            "keyword_search_fit",
            "jobs_match_api",
        })
    elif prompt_key == "skill_radar":
        target_keys.update({"pipeline_skill_extract", "skill_radar"})
    elif prompt_key == "insights":
        target_keys.update({"job_insights", "insights"})

    if provider:
        for k in target_keys:
            TenantPromptModelPreference.objects.update_or_create(
                owner=user,
                query_kind=k,
                defaults={"provider": provider, "model": model, "is_active": True},
            )
    else:
        for k in target_keys:
            TenantPromptModelPreference.objects.for_user(user).filter(query_kind=k).delete()


def get_prompt_model_preference(user, prompt_key: str) -> Tuple[str, str]:
    """
    Returns (provider, model) configured for a given prompt key, or ('', '') if defaulting.
    """
    from .models import TenantPromptModelPreference

    qk = PROMPT_TO_QUERY_KIND_MAP.get(prompt_key, prompt_key)
    target_pref = (
        TenantPromptModelPreference.objects.for_user(user)
        .filter(query_kind=qk, is_active=True)
        .first()
    )
    if not target_pref and qk != prompt_key:
        target_pref = (
            TenantPromptModelPreference.objects.for_user(user)
            .filter(query_kind=prompt_key, is_active=True)
            .first()
        )
    if not target_pref:
        target_pref = (
            TenantPromptModelPreference.objects.for_user(user)
            .filter(query_kind="__default__", is_active=True)
            .first()
        )
    if target_pref:
        return (target_pref.provider or "").strip(), (target_pref.model or "").strip()
    return "", ""


def get_all_prompt_model_preferences(user) -> Dict[str, str]:
    """
    Returns a mapping of prompt_key -> 'Provider::Model' for all prompt definitions and ATS profiles.
    """
    from .models import TenantPromptModelPreference

    prefs = (
        TenantPromptModelPreference.objects.for_user(user)
        .filter(is_active=True)
        .values("query_kind", "provider", "model")
    )
    pref_map = {p["query_kind"]: f"{p['provider']}::{p['model']}" for p in prefs}

    out: Dict[str, str] = {}
    for p_key, qk in PROMPT_TO_QUERY_KIND_MAP.items():
        val = pref_map.get(qk) or pref_map.get(p_key) or ""
        out[p_key] = val
    for k, v in pref_map.items():
        if k not in out:
            out[k] = v
    return out



def extract_template_variables(*template_strings: Optional[str]) -> List[str]:
    """
    Extract dynamic `{variable}` placeholders from prompt template strings.
    Ignores double curly braces `{{...}}` (e.g. JSON/template escapes) and literal JSON object keys.
    Returns sorted unique variable names preserving first appearance order.
    """
    found: List[str] = []
    seen: set[str] = set()
    for text in template_strings:
        if not text:
            continue
        matches = re.findall(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", str(text))
        for var in matches:
            if var not in seen:
                seen.add(var)
                found.append(var)
    return found


def inject_variables_into_template(template: str, variables: Dict[str, Any]) -> str:
    """
    Safely inject variables into a prompt template without mutating or damaging
    literal braces (such as JSON schemas or escaped double braces).
    """
    if not template:
        return ""

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name in variables:
            val = variables[var_name]
            return str(val if val is not None else "")
        return match.group(0)

    return re.sub(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", replacer, template)


def get_system_prompt_profile() -> SystemPromptProfile:
    """Return the singleton system prompt row."""
    return SystemPromptProfile.get_solo()


def _global_ats_qs():
    """ATS judge profiles shared by all users (admin-managed)."""
    return AtsJudgeProfile.objects.filter(owner__isnull=True)


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


def get_default_ats_judge_profile(user=None) -> Optional[AtsJudgeProfile]:
    """Global fallback ATS profile (is_default, slug default, or lowest pk)."""
    qs = _global_ats_qs()
    return (
        qs.filter(is_default=True).first()
        or qs.filter(slug="default").first()
        or qs.order_by("pk").first()
    )


def list_ats_judge_profiles(user=None) -> List[AtsJudgeProfile]:
    """List admin-managed global ATS judge profiles (user arg ignored)."""
    return list(_global_ats_qs().order_by("name"))


def get_ats_judge_profile_by_id(profile_id: Optional[int], user=None) -> Optional[AtsJudgeProfile]:
    """Fetch a global ATS profile by id (user arg ignored)."""
    if not profile_id:
        return None
    try:
        return _global_ats_qs().get(pk=int(profile_id))
    except (AtsJudgeProfile.DoesNotExist, ValueError, TypeError):
        return None


def resolve_effective_ats_judge_profile_id(
    *,
    ats_judge_profile_id: Optional[int] = None,
    workflow: Optional[OptimizerWorkflow] = None,
    workflow_ats_judge_profile_id: Optional[int] = None,
    user=None,
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
    default = get_default_ats_judge_profile(user)
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


def _system_prompt_profile_has_ats(profile: PromptProfileLike) -> bool:
    return any(
        (getattr(profile, f) or "").strip()
        for f in ("ats_judge", "ats_judge_system", "ats_judge_user")
    )


def resolve_prompt_parts(
    profile: PromptProfileLike,
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
        # Auto-heal: optimizer UI used to persist the concatenated default as
        # legacy and clear system/user — prefer real System+User messages again.
        default_combined = (def_sys + "\n\n" + def_user).strip()
        if leg == default_combined or leg == (DEFAULT_PROMPTS.get(kind) or "").strip():
            return (def_sys, def_user, None)
        return ("", "", leg)
    return (def_sys, def_user, None)


def get_effective_prompts(request: Optional[Any] = None, user=None) -> Dict[str, str]:
    """
    Return merged system prompts for forms and display: legacy combined strings plus
    system/user fields. Keys: writer, writer_system, writer_user, ...

    System/user textareas show the effective templates (including code defaults when
    the profile row is still empty), so the Prompt Library matches runtime behavior.
    """
    del request, user  # API compat; prompts are system-wide
    profile = get_system_prompt_profile()

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
    """Persist prompt dict to the system-wide SystemPromptProfile."""
    del request  # API compat
    profile = get_system_prompt_profile()
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


def clear_all_prompts_in_profile(request: Optional[Any] = None) -> None:
    """Clear every prompt field so code defaults (including system/user splits) apply."""
    del request  # API compat
    profile = get_system_prompt_profile()
    for _kind, (leg_a, sys_a, usr_a, _ds, _du) in _PROMPT_SPEC.items():
        setattr(profile, leg_a, "")
        setattr(profile, sys_a, "")
        setattr(profile, usr_a, "")
    profile.save()


def profile_for_llm(request: Optional[Any] = None) -> SystemPromptProfile:
    """System prompt row for resolve_prompt_parts."""
    del request  # API compat
    return get_system_prompt_profile()


def _ats_judge_prompt_state(
    *,
    prompts_override: Optional[Dict[str, str]],
    request: Optional[Any],
    ats_judge_profile_id: Optional[int] = None,
    workflow: Optional[OptimizerWorkflow] = None,
    workflow_ats_judge_profile_id: Optional[int] = None,
    user=None,
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
        user=user,
    )
    ats_prof = get_ats_judge_profile_by_id(effective_id, user=user)
    if ats_prof is not None:
        s, u, leg = resolve_ats_judge_parts(ats_prof)
    else:
        prof = get_system_prompt_profile()
        if _system_prompt_profile_has_ats(prof):
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
    user=None,
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

    prof = get_system_prompt_profile()
    out: Dict[str, str] = {}
    out.update(
        _ats_judge_prompt_state(
            prompts_override=prompts_override,
            request=request,
            ats_judge_profile_id=ats_judge_profile_id,
            workflow=workflow,
            workflow_ats_judge_profile_id=workflow_ats_judge_profile_id,
            user=user,
        )
    )
    for kind, prefix in (
        ("writer", "writer_prompt_"),
        ("recruiter_judge", "recruiter_judge_prompt_"),
        ("resume_judge", "resume_judge_prompt_"),
    ):
        s, u, leg = resolve_prompt_parts(prof, kind)
        override_key = kind
        ov = (prompts_override or {}).get(override_key) if prompts_override else None
        if ov and str(ov).strip():
            s, u, leg = "", "", str(ov).strip()
        out[f"{prefix}system"] = s or ""
        out[f"{prefix}user"] = u or ""
        out[f"{prefix}legacy"] = leg or ""
        if kind == "writer":
            _fallback = _DEF_W
        elif kind == "resume_judge":
            from .prompts import DEFAULT_RESUME_JUDGE_PROMPT as _DEF_RJ
            _fallback = _DEF_RJ
        else:
            _fallback = _DEF_REC
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


def _global_workflow_qs():
    """Optimizer workflows shared by all users (admin-managed)."""
    return OptimizerWorkflow.objects.filter(owner__isnull=True)


def list_optimizer_workflows(user=None):
    """List admin-managed global optimizer workflows (user arg ignored)."""
    return list(_global_workflow_qs().select_related("ats_judge_profile").order_by("name"))


def get_optimizer_workflow_by_id(workflow_id: Optional[int], user=None) -> Optional[OptimizerWorkflow]:
    """Fetch a global optimizer workflow by id (user arg ignored)."""
    if not workflow_id:
        return None
    try:
        return _global_workflow_qs().select_related("ats_judge_profile").get(pk=int(workflow_id))
    except (OptimizerWorkflow.DoesNotExist, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Prompt Version History & Audit Logging
# ---------------------------------------------------------------------------


def record_prompt_version(
    prompt_key: str,
    system_prompt: str = "",
    user_prompt: str = "",
    legacy_combined: str = "",
    author=None,
    change_summary: str = "",
) -> PromptVersionHistory:
    """
    Record a point-in-time snapshot of a prompt template.
    Auto-increments version_number for the given prompt_key.
    """
    latest = (
        PromptVersionHistory.objects.filter(prompt_key=prompt_key)
        .order_by("-version_number")
        .first()
    )
    next_ver = (latest.version_number + 1) if latest else 1
    return PromptVersionHistory.objects.create(
        prompt_key=prompt_key,
        version_number=next_ver,
        system_prompt=system_prompt or "",
        user_prompt=user_prompt or "",
        legacy_combined=legacy_combined or "",
        author=author if (author and getattr(author, "is_authenticated", False)) else None,
        change_summary=change_summary[:255] if change_summary else f"Revision v{next_ver}",
    )


def list_prompt_versions(prompt_key: str, limit: int = 50) -> List[PromptVersionHistory]:
    """Return historical revisions for a prompt_key, newest first."""
    return list(
        PromptVersionHistory.objects.filter(prompt_key=prompt_key)
        .select_related("author")
        .order_by("-version_number")[:limit]
    )


def get_prompt_version(version_id: int) -> Optional[PromptVersionHistory]:
    """Fetch single prompt version by primary key."""
    try:
        return PromptVersionHistory.objects.select_related("author").get(pk=int(version_id))
    except (PromptVersionHistory.DoesNotExist, ValueError, TypeError):
        return None


def delete_prompt_version(version_id: int) -> bool:
    """Delete a single historical revision."""
    try:
        ver = PromptVersionHistory.objects.get(pk=int(version_id))
        ver.delete()
        return True
    except (PromptVersionHistory.DoesNotExist, ValueError, TypeError):
        return False


def clear_prompt_versions(prompt_key: Optional[str] = None) -> int:
    """Prune/clear historical revisions for a prompt_key or all prompts."""
    qs = PromptVersionHistory.objects.all()
    if prompt_key:
        qs = qs.filter(prompt_key=prompt_key)
    count, _ = qs.delete()
    return count


def restore_prompt_version(version_id: int, user=None) -> Tuple[bool, str, Dict[str, str]]:
    """
    Restore a prompt template from a PromptVersionHistory record back into active database profile.
    Also creates a new revision tracking the restore.
    """
    ver = get_prompt_version(version_id)
    if not ver:
        return False, "Revision not found.", {}

    prompt_key = ver.prompt_key
    if prompt_key.startswith("ats_profile_"):
        try:
            prof_id = int(prompt_key.replace("ats_profile_", ""))
            prof = get_ats_judge_profile_by_id(prof_id)
            if not prof:
                return False, f"ATS Profile #{prof_id} not found.", {}
            save_ats_judge_profile(
                prof,
                ats_judge=ver.legacy_combined,
                ats_judge_system=ver.system_prompt,
                ats_judge_user=ver.user_prompt,
            )
        except Exception as e:
            return False, f"Could not restore ATS profile: {e}", {}
    elif prompt_key == "ats_judge":
        default_ats = get_default_ats_judge_profile()
        if default_ats:
            save_ats_judge_profile(
                default_ats,
                ats_judge=ver.legacy_combined,
                ats_judge_system=ver.system_prompt,
                ats_judge_user=ver.user_prompt,
            )
        else:
            return False, "No default ATS profile to restore.", {}
    else:
        spec = _PROMPT_SPEC.get(prompt_key)
        if not spec:
            return False, f"Unknown prompt key '{prompt_key}'.", {}
        leg_a, sys_a, usr_a, _ds, _du = spec
        profile = get_system_prompt_profile()
        setattr(profile, leg_a, ver.legacy_combined)
        setattr(profile, sys_a, ver.system_prompt)
        setattr(profile, usr_a, ver.user_prompt)
        profile.save()

    # Log new version snapshot documenting the rollback
    record_prompt_version(
        prompt_key=prompt_key,
        system_prompt=ver.system_prompt,
        user_prompt=ver.user_prompt,
        legacy_combined=ver.legacy_combined,
        author=user,
        change_summary=f"Restored from revision v{ver.version_number}",
    )

    return True, f"Successfully restored v{ver.version_number}.", {
        "system": ver.system_prompt,
        "user": ver.user_prompt,
        "legacy_combined": ver.legacy_combined,
    }

