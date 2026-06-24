"""
On-demand cover letter and interview prep generation (external LLM, not Ollama).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from django.utils import timezone
from pydantic import BaseModel, Field

from .agents import build_llm_messages_for_prompt, _llm_invoke_with_retry
from .llm_gateway import USAGE_QUERY_COVER_LETTER, USAGE_QUERY_INTERVIEW_PREP
from .models import (
    ApplicationAttempt,
    OptimizedResume,
    PipelineEntry,
    UserResume,
)
from .prompt_store import profile_for_llm, resolve_prompt_parts
from .services import parse_pdf

logger = logging.getLogger(__name__)

MAX_JD_CHARS = 50_000
MAX_RESUME_CHARS = 40_000


class InterviewPrepAnswer(BaseModel):
    question: str = ""
    talking_points: list[str] = Field(default_factory=list)
    resume_evidence: list[str] = Field(default_factory=list)


class InterviewPrepResult(BaseModel):
    likely_questions: list[str] = Field(default_factory=list)
    themes_to_emphasize: list[str] = Field(default_factory=list)
    suggested_answers: list[InterviewPrepAnswer] = Field(default_factory=list)


@dataclass
class InterviewPrepInputs:
    resume_text: str
    job_description: str
    company_name: str
    job_title: str
    job_url: str


class JobPrepError(Exception):
    """User-facing validation error for job prep generation."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _truncate(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 20] + "\n...[truncated]"


def _job_meta_from_entry(entry: PipelineEntry) -> tuple[str, str, str]:
    job = entry.job_listing
    return (
        (job.company_name or "").strip(),
        (job.title or "").strip(),
        (job.url or "").strip(),
    )


def _job_meta_from_optimized(optimized: OptimizedResume) -> tuple[str, str, str]:
    entry = optimized.pipeline_entry
    if entry is not None:
        return _job_meta_from_entry(entry)
    return ("", "", "")


def _latest_completed_optimization(entry: PipelineEntry) -> OptimizedResume | None:
    return (
        OptimizedResume.objects.filter(
            pipeline_entry=entry,
            status=OptimizedResume.STATUS_COMPLETED,
        )
        .select_related("job_description")
        .exclude(optimized_content="")
        .order_by("-created_at")
        .first()
    )


def _library_resume_text(track_slug: str) -> str:
    track_slug = (track_slug or "").strip().lower()
    ur = (
        UserResume.library().filter(track=track_slug).order_by("-uploaded_at").first()
        if track_slug
        else UserResume.library().order_by("-uploaded_at").first()
    )
    if ur is None or not ur.file:
        return ""
    try:
        return parse_pdf(ur.file.path) or ""
    except Exception as exc:
        logger.warning("Failed to parse library resume for track=%s: %s", track_slug, exc)
        return ""


def resolve_interview_prep_inputs(entry: PipelineEntry) -> InterviewPrepInputs:
    """Resolve resume + JD for interview prep with fallbacks."""
    entry = PipelineEntry.objects.select_related("job_listing").get(pk=entry.pk)
    company, title, url = _job_meta_from_entry(entry)
    job = entry.job_listing
    jd = (job.description or "").strip()

    resume_text = ""
    succeeded = (
        ApplicationAttempt.objects.filter(
            pipeline_entry=entry,
            status=ApplicationAttempt.Status.SUCCEEDED,
        )
        .select_related("optimized_resume")
        .order_by("-submitted_at", "-created_at")
        .first()
    )
    if succeeded and succeeded.optimized_resume and (succeeded.optimized_resume.optimized_content or "").strip():
        resume_text = succeeded.optimized_resume.optimized_content or ""
    if not resume_text:
        opt = _latest_completed_optimization(entry)
        if opt and (opt.optimized_content or "").strip():
            resume_text = opt.optimized_content or ""
        elif opt and opt.job_description_id:
            if not jd:
                jd = (opt.job_description.content or "").strip()
    if not resume_text:
        resume_text = _library_resume_text(entry.track)
    if not jd:
        opt = _latest_completed_optimization(entry)
        if opt and opt.job_description_id:
            jd = (opt.job_description.content or "").strip()

    return InterviewPrepInputs(
        resume_text=_truncate(resume_text, MAX_RESUME_CHARS),
        job_description=_truncate(jd, MAX_JD_CHARS),
        company_name=company,
        job_title=title,
        job_url=url,
    )


def _extract_llm_content(raw: Any) -> str:
    content = raw.content if hasattr(raw, "content") else str(raw)
    if not isinstance(content, str):
        content = str(content)
    return content.strip()


def _parse_interview_prep_json(raw_text: str) -> str:
    """Return normalized JSON string; fall back to wrapping raw markdown."""
    text = (raw_text or "").strip()
    if not text:
        return ""
    # Strip optional markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        parsed = InterviewPrepResult.model_validate(data)
        return parsed.model_dump_json(indent=2)
    except Exception:
        logger.warning("Interview prep response was not valid JSON; storing raw text")
        return text


def interview_prep_to_markdown(stored: str) -> str:
    """Render stored interview prep (JSON or plain text) as markdown for UI."""
    text = (stored or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        result = InterviewPrepResult.model_validate(data)
    except Exception:
        return text

    lines: list[str] = []
    if result.themes_to_emphasize:
        lines.append("## Themes to emphasize")
        lines.append("")
        for theme in result.themes_to_emphasize:
            lines.append(f"- {theme}")
        lines.append("")
    if result.likely_questions:
        if lines:
            lines.append("---")
            lines.append("")
        lines.append("## Likely questions")
        lines.append("")
        for i, q in enumerate(result.likely_questions, 1):
            lines.append(f"{i}. {q}")
        lines.append("")
    if result.suggested_answers:
        if lines:
            lines.append("---")
            lines.append("")
        lines.append("## Suggested answers")
        lines.append("")
        for item in result.suggested_answers:
            lines.append(f"### {item.question}")
            lines.append("")
            if item.talking_points:
                lines.append("**Talking points**")
                lines.append("")
                for pt in item.talking_points:
                    lines.append(f"- {pt}")
                lines.append("")
            if item.resume_evidence:
                lines.append("**Resume evidence**")
                lines.append("")
                for ev in item.resume_evidence:
                    lines.append(f"- {ev}")
                lines.append("")
            lines.append("")
    return "\n".join(lines).strip()


def generate_cover_letter(
    optimized: OptimizedResume,
    *,
    llm,
    prompts_profile=None,
    job_cache_key: Optional[str] = None,
) -> tuple[str, str]:
    """
    Generate and persist a cover letter for a completed optimization.

    Returns (cover_letter_text, prompt_text).
    """
    optimized = OptimizedResume.objects.select_related(
        "job_description", "pipeline_entry__job_listing"
    ).get(pk=optimized.pk)

    if optimized.status != OptimizedResume.STATUS_COMPLETED:
        raise JobPrepError("Optimization must be completed before generating a cover letter.")
    if not (optimized.optimized_content or "").strip():
        raise JobPrepError("Optimized resume content is empty.")

    profile = prompts_profile or profile_for_llm(None)
    sys_t, usr_t, leg = resolve_prompt_parts(profile, "cover_letter")
    company, title, _url = _job_meta_from_optimized(optimized)
    jd = (optimized.job_description.content or "").strip()
    fmt = {
        "optimized_resume": _truncate(optimized.optimized_content or "", MAX_RESUME_CHARS),
        "job_description": _truncate(jd, MAX_JD_CHARS),
        "company_name": company or "the company",
        "job_title": title or "the role",
    }
    messages = build_llm_messages_for_prompt(
        legacy_combined=leg or None,
        system_template=sys_t or None,
        user_template=usr_t or None,
        format_kwargs=fmt,
    )
    prompt_text = "\n\n---\n\n".join(
        f"{type(m).__name__}:\n{getattr(m, 'content', '')}" for m in messages
    )
    cache_key = job_cache_key or f"cover-letter:{optimized.id}"
    raw = _llm_invoke_with_retry(
        llm,
        messages,
        job_cache_key=cache_key,
        usage_query_kind=USAGE_QUERY_COVER_LETTER,
    )
    letter = _extract_llm_content(raw)
    if not letter:
        raise JobPrepError("LLM returned an empty cover letter.", status_code=502)

    optimized.cover_letter = letter
    optimized.cover_letter_generated_at = timezone.now()
    optimized.save(update_fields=["cover_letter", "cover_letter_generated_at", "updated_at"])
    return letter, prompt_text


def generate_interview_prep(
    entry: PipelineEntry,
    *,
    llm,
    prompts_profile=None,
    job_cache_key: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Generate and persist interview prep for a Done-stage pipeline entry.

    Returns (stored_content, markdown_render, prompt_text).
    """
    entry = PipelineEntry.objects.select_related("job_listing").get(pk=entry.pk)
    if entry.stage != PipelineEntry.Stage.DONE:
        raise JobPrepError("Interview prep is available only for Done-stage jobs.")

    inputs = resolve_interview_prep_inputs(entry)
    if not inputs.job_description:
        raise JobPrepError("No job description available for this entry.")
    if not inputs.resume_text:
        raise JobPrepError(
            "No resume text available. Optimize a resume or upload a library resume for this track."
        )

    profile = prompts_profile or profile_for_llm(None)
    sys_t, usr_t, leg = resolve_prompt_parts(profile, "interview_prep")
    fmt = {
        "resume_text": inputs.resume_text,
        "job_description": inputs.job_description,
        "company_name": inputs.company_name or "the company",
        "job_title": inputs.job_title or "the role",
        "job_url": inputs.job_url or "(none)",
    }
    messages = build_llm_messages_for_prompt(
        legacy_combined=leg or None,
        system_template=sys_t or None,
        user_template=usr_t or None,
        format_kwargs=fmt,
    )
    prompt_text = "\n\n---\n\n".join(
        f"{type(m).__name__}:\n{getattr(m, 'content', '')}" for m in messages
    )
    cache_key = job_cache_key or f"interview-prep:{entry.id}"
    raw = _llm_invoke_with_retry(
        llm,
        messages,
        job_cache_key=cache_key,
        usage_query_kind=USAGE_QUERY_INTERVIEW_PREP,
    )
    stored = _parse_interview_prep_json(_extract_llm_content(raw))
    if not stored:
        raise JobPrepError("LLM returned empty interview prep.", status_code=502)

    entry.interview_prep = stored
    entry.interview_prep_generated_at = timezone.now()
    entry.save(update_fields=["interview_prep", "interview_prep_generated_at"])
    return stored, interview_prep_to_markdown(stored), prompt_text
