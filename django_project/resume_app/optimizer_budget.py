from django.conf import settings


def _truncate(text: str | None, max_chars: int) -> str:
    if text is None:
        return ""
    text_str = str(text)
    if max_chars is None or max_chars < 0:
        return text_str
    return text_str[:max_chars]


def _normalize_optional_field(value: str | None, max_chars: int) -> str:
    return _truncate((value or "").strip(), max_chars)


def build_optimizer_context_state_raw(
    resume_text: str,
    job_description: str,
    optimization_notes: str = "",
    pipeline_skills_json: str = "",
    job_highlights: str = "",
    user_resume_id: int | None = None,
) -> dict:
    """Build a minimal optimizer context state for API / step-by-step runs."""
    resume_text_full = str(resume_text or "")
    job_description_full = str(job_description or "")

    writer_job_description = job_description_full
    if getattr(settings, "OPTIMIZER_USE_ROLE_SLICE_FOR_WRITER_JD", False):
        writer_job_description = _truncate(
            job_description_full,
            getattr(settings, "OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS", 8000),
        )

    resume_text_for_writer = _truncate(
        resume_text_full,
        getattr(settings, "OPTIMIZER_WRITER_RESUME_MAX_CHARS", 14000),
    )
    source_resume_text = _truncate(
        resume_text_full,
        getattr(settings, "OPTIMIZER_SOURCE_RESUME_MAX_CHARS", 12000),
    )

    notes = _normalize_optional_field(
        optimization_notes,
        getattr(settings, "OPTIMIZER_CONTEXT_NOTES_MAX_CHARS", 4000),
    )
    skills = _normalize_optional_field(
        pipeline_skills_json,
        getattr(settings, "OPTIMIZER_CONTEXT_SKILLS_JSON_MAX_CHARS", 8000),
    )
    highlights = _normalize_optional_field(
        job_highlights,
        getattr(settings, "OPTIMIZER_CONTEXT_JOB_HIGHLIGHTS_MAX_CHARS", 4000),
    )

    retrieval_context = "(none)"

    return {
        "job_description": job_description_full,
        "writer_job_description": writer_job_description,
        "resume_text": resume_text_for_writer,
        "source_resume_text": source_resume_text,
        "optimization_notes": notes or "(none)",
        "pipeline_skills_json": skills or "(none)",
        "job_highlights": highlights or "(none)",
        "retrieval_context": retrieval_context,
        "optimizer_context_budget": {
            "writer_jd_chars": len(writer_job_description),
            "resume_text_chars": len(resume_text_for_writer),
            "source_resume_text_chars": len(source_resume_text),
            "optimization_notes_chars": len(notes),
            "pipeline_skills_json_chars": len(skills),
            "job_highlights_chars": len(highlights),
        },
    }


def build_optimizer_context_state(
    optimized,
    resume_text_full: str,
    job_description_full: str,
) -> dict:
    """Build optimizer context state from an OptimizedResume for async task runs."""
    return build_optimizer_context_state_raw(
        resume_text_full,
        job_description_full,
        optimization_notes=getattr(optimized, "optimization_notes", "") or "",
        pipeline_skills_json=getattr(optimized, "pipeline_skills_json", "") or "",
        job_highlights=getattr(optimized, "job_highlights", "") or "",
        user_resume_id=getattr(optimized.original_resume, "id", None)
        if getattr(optimized, "original_resume", None)
        else None,
    )
