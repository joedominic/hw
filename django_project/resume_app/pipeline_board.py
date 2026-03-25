"""
Unified Pipeline → Vetting → Applying → Done board (one implementation, four URL routes).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db import models
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ninja.errors import HttpError

from .job_search_core import pipeline_jobs_to_payloads
from .jobs_api import (
    jobs_dislike as api_jobs_dislike,
    jobs_like as api_jobs_like,
    jobs_save as api_jobs_save,
)
from .llm_session import get_active_llm_provider
from .models import JobListingAction, OptimizedResume, PipelineEntry, Track
from .prompt_store import get_effective_prompts
from .utils import format_job_source_label

BOARD_STAGES = ("pipeline", "vetting", "applying", "done")


def _bulk_delete_msg(board_stage: str, count: int) -> str:
    if board_stage == "pipeline":
        return (
            f"Removed {count} job(s) from the pipeline. They will not be re-added by future runs."
        )
    if board_stage == "vetting":
        return f"Removed {count} job(s) from vetting."
    if board_stage == "applying":
        return f"Removed {count} job(s) from Applying."
    return f"Removed {count} job(s) from Done."


def _bulk_dislike_msg(board_stage: str, count: int) -> str:
    if board_stage == "pipeline":
        return f"Removed {count} job(s) from pipeline and excluded them from search."
    if board_stage == "vetting":
        return f"Removed {count} job(s) from vetting and excluded them from search."
    if board_stage == "applying":
        return f"Removed {count} job(s) from Applying and excluded them from search."
    return f"Removed {count} job(s) from Done and excluded them from search."


def _single_delete_msg(board_stage: str) -> str:
    if board_stage == "pipeline":
        return "Job removed from pipeline. It will not be re-added by future runs."
    if board_stage == "vetting":
        return "Job removed from vetting."
    if board_stage == "applying":
        return "Job removed from Applying."
    return "Job removed from Done."


def _single_dislike_msg(board_stage: str) -> str:
    if board_stage == "pipeline":
        return "Job removed from pipeline and excluded from search."
    if board_stage == "vetting":
        return "Job removed from vetting and excluded from search."
    if board_stage == "applying":
        return "Job removed from Applying and excluded from search."
    return "Job removed from Done and excluded from search."


def _apply_save_action(entry: PipelineEntry | None, board_stage: str, request) -> None:
    if not entry:
        return
    if board_stage == "pipeline":
        entry.move_to_vetting(save=True)
        try:
            from .tasks import evaluate_vetting_matching_task

            pe = get_effective_prompts(request)
            matching_prompt = pe.get("matching")
            llm_provider = get_active_llm_provider(request)
            llm_model = (
                request.session.get("job_search_llm_model")
                or request.session.get("optimizer_llm_model")
                or None
            )
            evaluate_vetting_matching_task(
                [entry.id],
                llm_provider=llm_provider,
                llm_model=llm_model,
                matching_prompt=matching_prompt,
            )
        except Exception:
            pass
    elif board_stage == "vetting":
        entry.move_to_applying(save=True)
        try:
            from .tasks import enqueue_applying_resume_optimization_task

            enqueue_applying_resume_optimization_task([entry.id], force_new=False)
        except Exception:
            pass
    elif board_stage == "applying":
        entry.mark_done(save=True)
    # done: favourites only; stage unchanged


def _save_success_message(board_stage: str) -> str:
    if board_stage == "pipeline":
        return "Job saved to favourites."
    if board_stage == "vetting":
        return "Job moved to Applying."
    if board_stage == "applying":
        return "Job moved to Done."
    return "Job saved to favourites."


def pipeline_board_view(request, board_stage: str):
    if board_stage not in BOARD_STAGES:
        raise ValueError("invalid board_stage")

    tracks_qs = Track.ensure_baseline()
    available_slugs = set(tracks_qs.values_list("slug", flat=True))
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "").strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug()
    request.session["job_search_track"] = raw_track
    request.session.modified = True

    if request.method == "POST":
        action = request.POST.get("action")
        job_id = request.POST.get("job_id")
        job_ids = request.POST.getlist("job_ids")
        track_from_form = (request.POST.get("track") or raw_track).strip().lower()
        next_url = request.POST.get("next") or reverse(board_stage) + f"?track={raw_track}"
        selected_ids = [jid for jid in job_ids if jid] or ([job_id] if job_id else [])

        if action == "bulk_optimize" and board_stage == "applying":
            if not selected_ids:
                messages.info(request, "Select at least one job before optimizing.")
                return redirect(next_url)
            track_from_form = (request.POST.get("track") or raw_track).strip().lower()
            entry_ids: list[int] = []
            for jid in selected_ids:
                try:
                    jid_int = int(jid)
                except (ValueError, TypeError):
                    continue
                pe = (
                    PipelineEntry.objects.filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                        stage=PipelineEntry.Stage.APPLYING,
                    )
                    .values_list("id", flat=True)
                    .first()
                )
                if pe is not None:
                    entry_ids.append(pe)
            if entry_ids:
                try:
                    from .tasks import enqueue_applying_resume_optimization_task

                    enqueue_applying_resume_optimization_task(entry_ids, force_new=True)
                    messages.success(
                        request,
                        f"Queued resume optimization for {len(entry_ids)} job(s). Open each job’s Optimization link to track progress.",
                    )
                except Exception as exc:
                    messages.error(request, str(exc))
            else:
                messages.info(request, "No Applying jobs matched the selection.")
            return redirect(next_url)

        if action in {"bulk_delete", "bulk_like", "bulk_dislike"}:
            if not selected_ids:
                messages.info(request, "Select at least one job before running a bulk action.")
                return redirect(next_url)
            success_count = 0
            if action == "bulk_delete":
                for jid in selected_ids:
                    try:
                        jid_int = int(jid)
                    except (ValueError, TypeError):
                        continue
                    entries = PipelineEntry.objects.filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    )
                    for entry in entries:
                        entry.mark_deleted(save=True)
                        success_count += 1
                if success_count:
                    messages.success(request, _bulk_delete_msg(board_stage, success_count))
            elif action in {"bulk_like", "bulk_dislike"}:
                request.session["job_search_track"] = track_from_form
                for jid in selected_ids:
                    try:
                        jid_int = int(jid)
                    except (ValueError, TypeError):
                        continue
                    try:
                        if action == "bulk_like":
                            api_jobs_like(request, job_listing_id=jid_int, track=track_from_form)
                        else:
                            api_jobs_dislike(request, job_listing_id=jid_int, track=track_from_form)
                            entries = PipelineEntry.objects.filter(
                                job_listing_id=jid_int,
                                track=track_from_form,
                                removed_at__isnull=True,
                            )
                            for entry in entries:
                                entry.mark_deleted(save=True)
                        success_count += 1
                    except HttpError:
                        continue
                if success_count:
                    if action == "bulk_like":
                        messages.success(request, f"Liked {success_count} job(s).")
                    else:
                        messages.success(request, _bulk_dislike_msg(board_stage, success_count))
            return redirect(next_url)

        if action == "delete" and job_id:
            try:
                jid_int = int(job_id)
                entries = PipelineEntry.objects.filter(
                    job_listing_id=jid_int,
                    track=track_from_form,
                    removed_at__isnull=True,
                )
                for entry in entries:
                    entry.mark_deleted(save=True)
                messages.success(request, _single_delete_msg(board_stage))
            except (ValueError, TypeError):
                messages.error(request, "Invalid job id.")
        elif action == "optimize_resume" and board_stage == "applying" and job_id:
            try:
                jid_int = int(job_id)
            except (ValueError, TypeError):
                messages.error(request, "Invalid job id.")
            else:
                pe = (
                    PipelineEntry.objects.filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                        stage=PipelineEntry.Stage.APPLYING,
                    )
                    .values_list("id", flat=True)
                    .first()
                )
                if pe is None:
                    messages.error(request, "Job is not in Applying for this track.")
                else:
                    try:
                        from .tasks import enqueue_applying_resume_optimization_task

                        enqueue_applying_resume_optimization_task([pe], force_new=True)
                        messages.success(request, "Resume optimization queued.")
                    except Exception as exc:
                        messages.error(request, str(exc))
        elif action in ("like", "dislike", "save") and job_id:
            request.session["job_search_track"] = track_from_form
            try:
                jid_int = int(job_id)
                if action == "like":
                    api_jobs_like(request, job_listing_id=jid_int, track=track_from_form)
                    messages.success(request, "Job liked.")
                elif action == "dislike":
                    api_jobs_dislike(request, job_listing_id=jid_int, track=track_from_form)
                    entries = PipelineEntry.objects.filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    )
                    for entry in entries:
                        entry.mark_deleted(save=True)
                    messages.success(request, _single_dislike_msg(board_stage))
                elif action == "save":
                    api_jobs_save(request, job_listing_id=jid_int)
                    entry = PipelineEntry.objects.filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    ).first()
                    _apply_save_action(entry, board_stage, request)
                    messages.success(request, _save_success_message(board_stage))
            except (HttpError, ValueError, TypeError) as e:
                messages.error(request, str(e))
        return redirect(next_url)

    saved_ids: set[int] = set()
    if board_stage == "pipeline":
        saved_ids = set(
            JobListingAction.objects.filter(action=JobListingAction.ActionType.SAVED).values_list(
                "job_listing_id", flat=True
            )
        )

    entries_qs = PipelineEntry.objects.filter(track=raw_track, removed_at__isnull=True)
    if board_stage == "pipeline":
        entries_qs = (
            entries_qs.filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE))
            .exclude(job_listing_id__in=saved_ids)
        )
    elif board_stage == "vetting":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.VETTING)
    elif board_stage == "applying":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.APPLYING)
    else:
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.DONE)

    entries = entries_qs.select_related("job_listing").order_by("-added_at")
    job_listings = [e.job_listing for e in entries]
    pipeline_jobs_full = pipeline_jobs_to_payloads(job_listings, track=raw_track)
    pipeline_stage_total = len(pipeline_jobs_full)

    # Distinct sources for filter dropdown (raw `source` matches JobListing.source)
    source_labels: dict[str, str] = {}
    for j in pipeline_jobs_full:
        if j.source and j.source not in source_labels:
            source_labels[j.source] = getattr(j, "source_display", None) or format_job_source_label(j.source)
    pipeline_source_options = sorted(source_labels.items(), key=lambda kv: kv[1].lower())

    source_filter = (request.GET.get("source") or "").strip()
    pref_min_raw = (request.GET.get("pref_min") or "").strip()
    pref_max_raw = (request.GET.get("pref_max") or "").strip()
    pref_min: int | None = None
    pref_max: int | None = None
    try:
        if pref_min_raw != "":
            pref_min = int(pref_min_raw)
    except ValueError:
        pref_min = None
    try:
        if pref_max_raw != "":
            pref_max = int(pref_max_raw)
    except ValueError:
        pref_max = None

    pipeline_jobs = list(pipeline_jobs_full)
    if source_filter:
        pipeline_jobs = [j for j in pipeline_jobs if j.source == source_filter]
    if pref_min is not None or pref_max is not None:

        def _pref_in_range(j) -> bool:
            m = j.preference_margin_percent
            if m is None:
                return False
            if pref_min is not None and m < pref_min:
                return False
            if pref_max is not None and m > pref_max:
                return False
            return True

        pipeline_jobs = [j for j in pipeline_jobs if _pref_in_range(j)]

    pipeline_count_before_text_search = len(pipeline_jobs)

    base_qs = PipelineEntry.objects.filter(track=raw_track, removed_at__isnull=True)
    stage_counts = {
        "pipeline": base_qs.filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)).count(),
        "vetting": base_qs.filter(stage=PipelineEntry.Stage.VETTING).count(),
        "applying": base_qs.filter(stage=PipelineEntry.Stage.APPLYING).count(),
        "done": base_qs.filter(stage=PipelineEntry.Stage.DONE).count(),
    }

    search_q = (request.GET.get("q") or "").strip()
    if search_q:
        q_lower = search_q.lower()
        pipeline_jobs = [
            j
            for j in pipeline_jobs
            if q_lower in (getattr(j, "title", "") or "").lower()
            or q_lower in (getattr(j, "company_name", "") or "").lower()
            or q_lower in (getattr(j, "snippet", "") or "").lower()
        ]

    pipeline_has_active_filters = bool(
        search_q or source_filter or pref_min_raw != "" or pref_max_raw != ""
    )

    if board_stage == "applying" and pipeline_jobs:
        jids = [j.id for j in pipeline_jobs]
        pe_rows = PipelineEntry.objects.filter(
            track=raw_track,
            stage=PipelineEntry.Stage.APPLYING,
            removed_at__isnull=True,
            job_listing_id__in=jids,
        )
        pe_by_job = {e.job_listing_id: e.id for e in pe_rows}
        entry_ids_list = list(pe_by_job.values())
        latest_by_entry: dict[int, int] = {}
        if entry_ids_list:
            for orow in OptimizedResume.objects.filter(
                pipeline_entry_id__in=entry_ids_list
            ).order_by("-created_at"):
                eid = orow.pipeline_entry_id
                if eid is not None and eid not in latest_by_entry:
                    latest_by_entry[eid] = orow.id
        for j in pipeline_jobs:
            peid = pe_by_job.get(j.id)
            if peid is not None and peid in latest_by_entry:
                j.optimized_resume_id = latest_by_entry[peid]

    job_tasks_url = reverse("job_automation")
    board_titles = {
        "pipeline": "Pipeline",
        "vetting": "Vetting",
        "applying": "Applying",
        "done": "Done",
    }
    board_subtitles = {
        "pipeline": "Jobs from scheduled tasks. Focus % is computed when you open this page. Like, Dislike, Save, and Delete behave like Job Search.",
        "vetting": "Saved from Pipeline for detailed review. Save here when you are ready to apply.",
        "applying": "Active applications. Saving here marks a job done after you submit.",
        "done": "Completed applications.",
    }
    empty_messages = {
        "pipeline": f"No jobs in the pipeline for {raw_track} track.",
        "vetting": f"No jobs in Vetting for {raw_track} track.",
        "applying": f"No jobs in Applying for {raw_track} track.",
        "done": f"No jobs in Done for {raw_track} track.",
    }
    empty_help = {
        "pipeline": [
            f"Go to {job_tasks_url} to create an active scheduled search (or use Run now on an existing task). Jobs are added only when a task runs.",
            "Tasks run automatically when next_run_at is due (scheduler runs every minute). Default cron is 9 AM daily—use Run now to test without waiting.",
            "Try the other track (IC vs Management) if your task uses a different track.",
            "Saved jobs are hidden from the pipeline; they appear under Job Search → Favourites.",
        ],
        "vetting": [],
        "applying": [],
        "done": [],
    }

    board_empty_message = empty_messages[board_stage]
    board_empty_help_list = empty_help[board_stage]
    if pipeline_stage_total > 0 and not pipeline_jobs and pipeline_has_active_filters:
        board_empty_message = (
            "No jobs match the current filters. Try clearing the text search, "
            "setting Source to All sources, or widening the Pref score range."
        )
        board_empty_help_list = []

    now = timezone.now()
    week_ago = now - timedelta(days=7)
    done_total = stage_counts["done"]
    done_this_week = base_qs.filter(
        stage=PipelineEntry.Stage.DONE,
        added_at__gte=week_ago,
    ).count()
    total_active = base_qs.count()
    conversion_percent = int(round((done_total / total_active) * 100)) if total_active else 0

    clear_url = reverse(board_stage) + f"?track={raw_track}"

    qd = request.GET.copy()
    qd.pop("track", None)
    pipeline_board_extra_query = qd.urlencode()

    context = {
        "pipeline_jobs": pipeline_jobs,
        "pipeline_track": raw_track,
        "pipeline_search_query": search_q if search_q else None,
        "pipeline_total_count": pipeline_stage_total,
        "pipeline_count_before_text_search": pipeline_count_before_text_search,
        "pipeline_has_active_filters": pipeline_has_active_filters,
        "pipeline_source_options": pipeline_source_options,
        "pipeline_source_selected": source_filter,
        "pipeline_pref_min": pref_min_raw,
        "pipeline_pref_max": pref_max_raw,
        "pipeline_tracks": list(tracks_qs),
        "stage_counts": stage_counts,
        "board_stage": board_stage,
        "board_page_title": board_titles[board_stage],
        "board_page_subtitle": board_subtitles[board_stage],
        "board_empty_message": board_empty_message,
        "board_empty_help_list": board_empty_help_list,
        "board_clear_url": clear_url,
        "show_board_metrics": board_stage == "pipeline",
        "done_total": done_total,
        "done_this_week": done_this_week,
        "conversion_percent": conversion_percent,
        "job_tasks_url": job_tasks_url,
        "pipeline_board_extra_query": pipeline_board_extra_query,
    }
    return render(request, "resume_app/pipeline_board.html", context)


def pipeline_view(request):
    return pipeline_board_view(request, "pipeline")


def vetting_view(request):
    return pipeline_board_view(request, "vetting")


def applying_view(request):
    return pipeline_board_view(request, "applying")


def done_view(request):
    return pipeline_board_view(request, "done")
