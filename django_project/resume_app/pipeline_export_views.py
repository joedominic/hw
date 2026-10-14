"""
Export views for pipeline resumes and scheduled search runs.
Supports PDF, DOCX, CSV, and JSON snapshots.
"""

from __future__ import annotations

import csv
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.text import slugify

from .models import JobSearchTaskRun, PipelineEntry
from .tenancy import get_active_user, get_owned_or_404
from .utils import sanitize_resume_markdown


@login_required
def pipeline_applied_resume_json_view(request, entry_id: int):
    """
    Return JSON snapshot of the applied/tailored resume markdown for a pipeline entry.
    """
    user = get_active_user(request)
    entry = get_owned_or_404(PipelineEntry, user, id=entry_id)
    job = entry.job_listing

    markdown = entry.applied_resume_markdown or ""
    if not markdown and entry.applied_optimized_resume:
        markdown = entry.applied_optimized_resume.optimized_content or ""
    elif not markdown:
        opt = (
            entry.optimized_resumes.filter(status="completed")
            .exclude(optimized_content="")
            .order_by("-updated_at")
            .first()
        )
        if opt and (opt.optimized_content or "").strip():
            markdown = opt.optimized_content
            entry.applied_resume_markdown = markdown
            entry.applied_optimized_resume = opt
            entry.save(update_fields=["applied_resume_markdown", "applied_optimized_resume"])

    sanitized = sanitize_resume_markdown(markdown)
    if sanitized != markdown:
        markdown = sanitized
        entry.applied_resume_markdown = sanitized
        entry.save(update_fields=["applied_resume_markdown"])

    return JsonResponse({
        "success": True,
        "entry_id": entry.id,
        "job_listing_id": entry.job_listing_id,
        "job_title": job.title if job else "",
        "company_name": job.company_name if job else "",
        "has_resume": bool(markdown.strip()),
        "markdown": markdown,
        "applied_at": entry.applied_at.isoformat() if entry.applied_at else None,
        "optimized_resume_id": entry.applied_optimized_resume_id,
    })


@login_required
def pipeline_export_pdf_view(request, entry_id: int):
    """
    Export snapshot of applied resume markdown as PDF.
    """
    from .document_renderer import (
        _apply_export_replacements,
        _build_export_pdf,
        _export_file_response,
        _format_export_filename,
    )

    user = get_active_user(request)
    entry = get_owned_or_404(PipelineEntry, user, id=entry_id)
    content = entry.applied_resume_markdown or ""
    if not content and entry.applied_optimized_resume:
        content = entry.applied_optimized_resume.optimized_content or ""
    if not content.strip():
        raise Http404("No applied resume markdown available for export.")

    content = sanitize_resume_markdown(content)
    content = _apply_export_replacements(content, request)
    buf = _build_export_pdf(content)
    if buf is None:
        return HttpResponse(
            "PDF export requires reportlab; install with: pip install reportlab",
            status=503,
        )

    job = entry.job_listing
    company = (job.company_name or "").strip() if job else ""
    title = (job.title or "").strip() if job else ""
    filename = _format_export_filename("applied_resume", company, title, "pdf")
    return _export_file_response(buf, filename=filename, content_type="application/pdf")


@login_required
def pipeline_export_docx_view(request, entry_id: int):
    """
    Export snapshot of applied resume markdown as Word (DOCX).
    """
    from .document_renderer import (
        _apply_export_replacements,
        _build_export_docx,
        _export_file_response,
        _format_export_filename,
    )

    user = get_active_user(request)
    entry = get_owned_or_404(PipelineEntry, user, id=entry_id)
    content = entry.applied_resume_markdown or ""
    if not content and entry.applied_optimized_resume:
        content = entry.applied_optimized_resume.optimized_content or ""
    if not content.strip():
        raise Http404("No applied resume markdown available for export.")

    content = sanitize_resume_markdown(content)
    content = _apply_export_replacements(content, request)
    buf = _build_export_docx(content)
    if buf is None:
        return HttpResponse(
            "Word export requires python-docx; install with: pip install python-docx",
            status=503,
        )

    job = entry.job_listing
    company = (job.company_name or "").strip() if job else ""
    title = (job.title or "").strip() if job else ""
    filename = _format_export_filename("applied_resume", company, title, "docx")
    return _export_file_response(
        buf,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@login_required
def download_scheduled_run_csv_view(request, run_id: int):
    """Download CSV audit log of all fetched jobs for a scheduled task run."""
    user = get_active_user(request)
    run = get_object_or_404(
        JobSearchTaskRun.objects.select_related("task", "task__saved_search"),
        id=run_id,
        task__owner=user,
    )
    if not run.details:
        raise Http404("Details for this run are no longer available (only kept for the last 2 runs).")

    profile_name = (
        run.task.saved_search.name
        if run.task.saved_search
        else (run.task.name or run.task.search_term or f"task_{run.task_id}")
    )
    safe_profile = slugify(profile_name) or "scheduled_run"
    date_str = run.started_at.strftime("%Y%m%d_%H%M") if run.started_at else "run"
    filename = f"{safe_profile}_{date_str}_job_audit.csv"

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "Job Title",
        "Company",
        "Location",
        "Source",
        "Status",
        "Reason / Matched Rule",
        "Date Posted",
        "Job URL",
    ])

    status_labels = {
        "saved_to_pipeline": "Saved to Pipeline",
        "reposted_to_pipeline": "Saved to Pipeline (Reposted)",
        "already_in_pipeline": "Already in Pipeline",
        "previously_removed": "Previously Removed",
        "eliminated_disqualifier": "Eliminated: Disqualifier",
        "eliminated_disliked": "Eliminated: Disliked",
        "eliminated_hidden": "Eliminated: Hidden",
        "eliminated_duplicate": "Eliminated: Duplicate",
    }

    for item in run.details:
        disp_raw = item.get("disposition", "")
        status_human = status_labels.get(disp_raw, disp_raw.replace("_", " ").title())
        writer.writerow([
            item.get("title", ""),
            item.get("company", ""),
            item.get("location", ""),
            item.get("source", ""),
            status_human,
            item.get("reason", ""),
            item.get("date_posted", ""),
            item.get("url", ""),
        ])

    return response


@login_required
def scheduled_run_details_json_view(request, run_id: int):
    """Return JSON audit log for in-browser inspector modal."""
    user = get_active_user(request)
    run = get_object_or_404(
        JobSearchTaskRun.objects.select_related("task", "task__saved_search"),
        id=run_id,
        task__owner=user,
    )
    profile_name = (
        run.task.saved_search.name
        if run.task.saved_search
        else (run.task.name or run.task.search_term or f"task_{run.task_id}")
    )
    return JsonResponse({
        "run_id": run.id,
        "profile_name": profile_name,
        "track": run.task.track or "",
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "status": run.get_status_display(),
        "jobs_fetched": run.jobs_fetched,
        "jobs_eliminated": max(0, run.jobs_fetched - run.jobs_after_filter),
        "jobs_saved": run.jobs_added_to_pipeline,
        "details": run.details or [],
        "jobs": run.details or [],
    })
