from __future__ import annotations

import re

from django.conf import settings


_OMITTED_DUPLICATE_NOTE = "(omitted — already shown above; saves tokens)"


def _truncate(text: str | None, max_chars: int) -> str:
    if text is None:
        return ""
    text_str = str(text)
    if max_chars is None or max_chars < 0:
        return text_str
    return text_str[:max_chars]


def _normalize_optional_field(value: str | None, max_chars: int) -> str:
    return _truncate((value or "").strip(), max_chars)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def role_focused_job_description(
    job_description: str,
    *,
    job_title: str = "",
    max_chars: int | None = None,
) -> str:
    """Semantic JD slice for Writer/judges (heuristic cleanse, not prefix truncate)."""
    jd = (job_description or "").strip()
    if not jd:
        return ""
    if not getattr(settings, "OPTIMIZER_USE_ROLE_SLICE_FOR_WRITER_JD", True):
        cap = max_chars or len(jd)
        return _truncate(jd, cap)

    cap = max_chars or getattr(settings, "OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS", 8000)
    from .embeddings import extract_role_description

    return extract_role_description(jd, job_title or "", max_chars=cap)


def should_include_source_resume(source: str, edit_body: str) -> bool:
    """Skip duplicate source anchor when it matches the document being edited."""
    if not (source or "").strip():
        return False
    return _normalize_whitespace(source) != _normalize_whitespace(edit_body)


def should_include_full_job_description(role_slice: str, full_jd: str) -> bool:
    """Skip full JD when the role-focused excerpt already covers the posting."""
    full = (full_jd or "").strip()
    role = (role_slice or "").strip()
    if not full:
        return False
    if not role:
        return True
    norm_full = _normalize_whitespace(full)
    norm_role = _normalize_whitespace(role)
    if norm_full == norm_role:
        return False
    if norm_full.startswith(norm_role) and len(norm_full) - len(norm_role) < 200:
        return False
    return len(norm_full) > len(norm_role) + 200 or norm_role not in norm_full


def omitted_duplicate_field_note() -> str:
    return _OMITTED_DUPLICATE_NOTE


def truncate_judge_resume(text: str) -> str:
    return _truncate(text, getattr(settings, "OPTIMIZER_JUDGE_RESUME_MAX_CHARS", 12000))


def truncate_judge_job_description(text: str) -> str:
    return _truncate(text, getattr(settings, "OPTIMIZER_JUDGE_JD_MAX_CHARS", 8000))


def build_optimizer_context_state_raw(
    resume_text: str,
    job_description: str,
    optimization_notes: str = "",
    pipeline_skills_json: str = "",
    job_highlights: str = "",
    user_resume_id: int | None = None,
    job_title: str = "",
) -> dict:
    """Build a minimal optimizer context state for API / step-by-step runs."""
    resume_text_full = str(resume_text or "")
    job_description_full = str(job_description or "")

    writer_job_description = role_focused_job_description(
        job_description_full,
        job_title=job_title,
    )
    judge_job_description = writer_job_description or job_description_full

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
        "judge_job_description": judge_job_description,
        "resume_text": resume_text_for_writer,
        "source_resume_text": source_resume_text,
        "optimization_notes": notes or "(none)",
        "pipeline_skills_json": skills or "(none)",
        "job_highlights": highlights or "(none)",
        "retrieval_context": retrieval_context,
        "optimizer_context_budget": {
            "writer_jd_chars": len(writer_job_description),
            "judge_jd_chars": len(judge_job_description),
            "full_jd_chars": len(job_description_full),
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
    job_title = ""
    job = getattr(optimized, "job_description", None)
    if job is not None:
        job_title = getattr(job, "title", "") or ""
    return build_optimizer_context_state_raw(
        resume_text_full,
        job_description_full,
        optimization_notes=getattr(optimized, "optimization_notes", "") or "",
        pipeline_skills_json=getattr(optimized, "pipeline_skills_json", "") or "",
        job_highlights=getattr(optimized, "job_highlights", "") or "",
        user_resume_id=getattr(optimized.original_resume, "id", None)
        if getattr(optimized, "original_resume", None)
        else None,
        job_title=job_title,
    )
