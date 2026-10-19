import sys
from typing import Optional, Tuple

# Mapping from usage_query_kind to (trace_name, generation_name) for Langfuse telemetry
TRACE_NAME_MAPPINGS: dict[str, tuple[str, str]] = {
    # Optimizer workflow steps (grouped under trace 'resume_optimizer')
    "optimizer_writer": ("resume_optimizer", "optimizer_writer"),
    "optimizer_ats_judge": ("resume_optimizer", "optimizer_ats_judge"),
    "optimizer_recruiter_judge": ("resume_optimizer", "optimizer_recruiter_judge"),
    "jd_cleanse": ("resume_optimizer", "jd_cleanse"),

    # Standalone features
    "fit_check": ("fit_check", "fit_check"),
    "matching": ("job_matching", "job_matching"),
    "job_insights": ("job_insights", "job_insights"),
    "cover_letter": ("cover_letter", "cover_letter"),
    "interview_prep": ("interview_prep", "interview_prep"),
    "jobs_ai_match": ("jobs_ai_match", "jobs_ai_match"),
    "keyword_search_fit": ("keyword_search_fit", "keyword_search_fit"),
    "jobs_match_api": ("jobs_match_api", "jobs_match_api"),

    # Pipeline stages
    "pipeline_vetting_matching": ("pipeline_vetting", "pipeline_vetting_matching"),
    "pipeline_resume_refine": ("pipeline_refine", "pipeline_resume_refine"),
    "pipeline_skill_extract": ("pipeline_skill_extract", "pipeline_skill_extract"),
    "job_authenticity": ("job_authenticity", "job_authenticity"),
    "skill_radar": ("skill_radar", "skill_radar"),
    "career_profiler": ("career_profiler", "career_profiler"),

    # Tooling / API
    "prompt_studio": ("prompt_studio", "prompt_studio_eval"),
    "api_llm_complete": ("api_completion", "api_llm_complete"),
    "api_resume_fit": ("api_resume_fit", "api_resume_fit"),
}


def _infer_names_from_caller() -> Tuple[Optional[str], Optional[str]]:
    """Inspect call stack to infer a meaningful trace and generation name."""
    try:
        frame = sys._getframe(2)
        while frame:
            filename = frame.f_code.co_filename.replace("\\", "/")
            func_name = frame.f_code.co_name
            if "resume_app" in filename:
                base = filename.rsplit("/", 1)[-1]
                if "pipeline_llm_skill_extract" in base:
                    return "pipeline_skill_extract", "extract_skills"
                if "prompt_studio" in base:
                    return "prompt_studio", f"prompt_studio_{func_name}"
                if "settings_views" in base:
                    return "settings_llm_test", "test_connection"
                if "jobs_api" in base:
                    return "jobs_api", f"jobs_{func_name}"
                if "authenticity" in base:
                    return "job_authenticity", f"auth_{func_name}"
                if "skill_radar" in base:
                    return "skill_radar", f"radar_{func_name}"
                if "career_profiler" in base:
                    return "career_profiler", f"profile_{func_name}"
                if "tasks" in base:
                    return "background_task", f"task_{func_name}"
                if "api" in base:
                    return "api_request", f"api_{func_name}"
            frame = frame.f_back
    except Exception:
        pass
    return None, None


def resolve_trace_names(
    query_kind: Optional[str] = None,
    job_cache_key: Optional[str] = None,
    default_name: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Resolve descriptive trace_name and generation_name for Langfuse telemetry.

    Prevents all calls from sharing generic 'litellm-completion' and enables
    effective grouping, filtering, and analysis by workflow and step.
    """
    qk = (query_kind or "").strip().lower()
    if qk and qk in TRACE_NAME_MAPPINGS:
        trace_name, gen_name = TRACE_NAME_MAPPINGS[qk]
        if qk == "jd_cleanse" and not job_cache_key:
            trace_name = "jd_cleanse"
        return trace_name, gen_name

    if qk and qk not in ("unspecified", "none", "*"):
        return qk, qk

    # Try inspecting caller frame
    inferred_trace, inferred_gen = _infer_names_from_caller()
    if inferred_trace and inferred_gen:
        return inferred_trace, inferred_gen

    if job_cache_key:
        return f"job-{job_cache_key}", "llm_completion"

    clean_model = (default_name or "").split("/")[-1].replace(":", "-").strip()
    trace = f"llm-{clean_model}" if clean_model else "llm_chat"
    gen = f"completion-{clean_model}" if clean_model else "completion"
    return trace, gen
