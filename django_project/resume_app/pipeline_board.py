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

from .experience import has_my_jobs_search_profile, is_power_user
from .job_search_core import pipeline_jobs_to_payloads, VETTING_MATCHING_JD_MIN_CHARS
from .saved_searches import backfill_saved_search_profile_tracks, pipeline_track_tabs_for_board
from .search_profile_scope import resolve_active_profile_slug
from .jobs_api import (
    jobs_dislike as api_jobs_dislike,
    jobs_like as api_jobs_like,
    jobs_save as api_jobs_save,
)
from .llm import get_active_llm_provider
from .models import OptimizedResume, PipelineEntry, Track
from .tasks import _resolve_user_resume_for_track
from .prompt_store import get_effective_prompts
from .utils import format_job_source_label

BOARD_STAGES = ("pipeline", "vetting", "applying", "done")

STAGE_TAB_LABELS_POWER = {
    "pipeline": "Pipeline",
    "vetting": "Vetting",
    "applying": "Applying",
    "done": "Done",
}
STAGE_TAB_LABELS_NORMAL = {
    "pipeline": "New",
    "vetting": "Review",
    "applying": "Applying",
    "done": "Applied",
}


def _attach_optimizer_user_resume_id(user, pipeline_jobs, raw_track: str) -> None:
    """Track default UserResume id for Tailor / Open optimizer links (same resolution as pipeline enqueue)."""
    ur = _resolve_user_resume_for_track(user, raw_track)
    rid = ur.id if ur else None
    for j in pipeline_jobs:
        setattr(j, "optimizer_user_resume_id", rid)


def _attach_optimized_resume_ids_for_stage(user, pipeline_jobs, raw_track: str, stage: str) -> None:
    """Set each payload's optimized_resume_id from the latest OptimizedResume for its pipeline entry."""
    if not pipeline_jobs:
        return
    jids = [j.id for j in pipeline_jobs]
    pe_rows = PipelineEntry.objects.for_user(user).filter(
        track=raw_track,
        stage=stage,
        removed_at__isnull=True,
        job_listing_id__in=jids,
    )
    pe_by_job = {e.job_listing_id: e for e in pe_rows}
    entry_ids_list = [e.id for e in pe_by_job.values()]
    latest_by_entry: dict[int, int] = {}
    if entry_ids_list:
        for orow in OptimizedResume.objects.for_user(user).filter(
            pipeline_entry_id__in=entry_ids_list
        ).order_by("-created_at"):
            eid = orow.pipeline_entry_id
            if eid is not None and eid not in latest_by_entry:
                latest_by_entry[eid] = orow.id
    for j in pipeline_jobs:
        pe = pe_by_job.get(j.id)
        if pe is not None:
            setattr(j, "pipeline_entry_id", pe.id)
            setattr(j, "has_interview_prep", bool((pe.interview_prep or "").strip()))
            if pe.id in latest_by_entry:
                setattr(j, "optimized_resume_id", latest_by_entry[pe.id])


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
        from .domain.event_bus import event_bus
        from .domain.events import JobPromotedToVetting

        event_bus.publish(JobPromotedToVetting(user_id=request.user.id, entry_id=entry.id, track=entry.track))
        try:
            from .tasks import evaluate_vetting_matching_task

            pe = get_effective_prompts(request)
            matching_prompt = pe.get("matching")
            llm_provider = get_active_llm_provider(request.user, request)
            llm_model = (
                request.session.get("job_search_llm_model")
                or request.session.get("optimizer_llm_model")
                or None
            )
            evaluate_vetting_matching_task(
                request.user.id,
                [entry.id],
                llm_provider=llm_provider,
                llm_model=llm_model,
                matching_prompt=matching_prompt,
            )
        except Exception:
            pass
    elif board_stage == "vetting":
        entry.move_to_applying(save=True)
        from .domain.event_bus import event_bus
        from .domain.events import JobPromotedToApplying

        event_bus.publish(JobPromotedToApplying(user_id=request.user.id, entry_id=entry.id, track=entry.track))
    elif board_stage == "applying":
        entry.mark_done(save=True)
        from .domain.event_bus import event_bus
        from .domain.events import JobMarkedApplied

        event_bus.publish(JobMarkedApplied(user_id=request.user.id, entry_id=entry.id, track=entry.track))
    # done: favourites only; stage unchanged


def _save_success_message(board_stage: str, *, power_user: bool) -> str:
    if board_stage == "pipeline":
        if power_user:
            return "Job saved to favourites and moved to Vetting."
        return "Job moved to Review."
    if board_stage == "vetting":
        if power_user:
            return (
                "Job moved to Applying. Use Open optimizer on the Applying board "
                "when you are ready to tailor your resume."
            )
        return "Job moved to Applying."
    if board_stage == "applying":
        return "Marked as applied." if not power_user else "Job moved to Done."
    return "Job saved to favourites."


def pipeline_board_view(request, board_stage: str):
    if board_stage not in BOARD_STAGES:
        raise ValueError("invalid board_stage")

    user = request.user
    power_user = is_power_user(user)
    backfill_saved_search_profile_tracks(user)

    if not has_my_jobs_search_profile(user):
        jobs_search_url = reverse("jobs_search")
        if request.method == "POST":
            messages.info(
                request,
                "Set up a search profile on Find jobs first—run a search and save it with a name.",
            )
            return redirect(reverse("pipeline"))
        return render(
            request,
            "resume_app/pipeline_board.html",
            {
                "my_jobs_profile_gate": True,
                "board_page_title": "My jobs",
                "jobs_search_url": jobs_search_url,
            },
        )

    if not power_user and board_stage in ("vetting", "applying", "done"):
        show_advanced_board_filters = False
        show_pipeline_track_tabs = False
    else:
        show_advanced_board_filters = True
        show_pipeline_track_tabs = True

    tracks_qs = Track.ensure_baseline(user)
    pipeline_track_tabs = pipeline_track_tabs_for_board(user, power_user=power_user)
    board_track_slugs = {t["slug"] for t in pipeline_track_tabs}
    available_slugs = set(tracks_qs.values_list("slug", flat=True))
    raw_track = resolve_active_profile_slug(
        user,
        profile=request.GET.get("profile"),
        track=request.GET.get("track"),
        session_profile=request.session.get("job_search_profile_slug"),
        session_track=request.session.get("job_search_track"),
    )
    if power_user:
        if not raw_track or raw_track not in available_slugs:
            raw_track = Track.get_default_slug(user)
    else:
        if not board_track_slugs:
            raw_track = Track.get_default_slug(user)
        elif not raw_track or raw_track not in board_track_slugs:
            raw_track = pipeline_track_tabs[0]["slug"]
    request.session["job_search_track"] = raw_track
    request.session["job_search_profile_slug"] = raw_track
    request.session.modified = True

    if request.method == "POST":
        action = request.POST.get("action")
        job_id = request.POST.get("job_id")
        job_ids = request.POST.getlist("job_ids")
        track_from_form = (request.POST.get("track") or raw_track).strip().lower()
        next_url = request.POST.get("next") or reverse(board_stage) + f"?track={raw_track}"
        selected_ids = [jid for jid in job_ids if jid] or ([job_id] if job_id else [])

        if action == "start_apply_agent":
            if board_stage != "applying":
                messages.info(request, "The apply agent can only be started from the Applying board.")
                return redirect(next_url)
            entry_ids: list[int] = []
            for jid in selected_ids:
                try:
                    jid_int = int(jid)
                except (ValueError, TypeError):
                    continue
                entry = PipelineEntry.objects.for_user(user).filter(
                    job_listing_id=jid_int,
                    track=track_from_form,
                    stage=PipelineEntry.Stage.APPLYING,
                    removed_at__isnull=True,
                ).first()
                if entry:
                    entry_ids.append(entry.id)

            from .application.apply_services import ApplyApplicationService

            result = ApplyApplicationService.start_attempts_for_jobs(user, entry_ids)
            if result.success:
                if result.started_attempts:
                    messages.success(
                        request,
                        f"Started the apply agent for {len(result.started_attempts)} job(s). Track progress on the Apply Agent page.",
                    )
                else:
                    messages.info(request, result.message)
            else:
                messages.error(request, result.message)
            return redirect(next_url)

        if action in {"bulk_delete", "bulk_like", "bulk_dislike", "bulk_promote"}:
            if not selected_ids:
                messages.info(request, "Select at least one job before running a bulk action.")
                return redirect(next_url)
            success_count = 0
            if action == "bulk_promote":
                for jid in selected_ids:
                    try:
                        jid_int = int(jid)
                    except (ValueError, TypeError):
                        continue
                    entries = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    )
                    for entry in entries:
                        _apply_save_action(entry, board_stage, request)
                        success_count += 1
                if success_count:
                    messages.success(request, f"Promoted {success_count} job(s) to the next stage.")
            elif action == "bulk_delete":
                for jid in selected_ids:
                    try:
                        jid_int = int(jid)
                    except (ValueError, TypeError):
                        continue
                    entries = PipelineEntry.objects.for_user(user).filter(
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
                            entries = PipelineEntry.objects.for_user(user).filter(
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
                entries = PipelineEntry.objects.for_user(user).filter(
                    job_listing_id=jid_int,
                    track=track_from_form,
                    removed_at__isnull=True,
                )
                for entry in entries:
                    entry.mark_deleted(save=True)
                messages.success(request, _single_delete_msg(board_stage))
            except (ValueError, TypeError):
                messages.error(request, "Invalid job id.")
        elif action in ("like", "dislike", "save") and job_id:
            request.session["job_search_track"] = track_from_form
            try:
                jid_int = int(job_id)
                if action == "like":
                    api_jobs_like(request, job_listing_id=jid_int, track=track_from_form)
                    messages.success(request, "Job liked.")
                elif action == "dislike":
                    api_jobs_dislike(request, job_listing_id=jid_int, track=track_from_form)
                    entries = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    )
                    for entry in entries:
                        entry.mark_deleted(save=True)
                    messages.success(request, _single_dislike_msg(board_stage))
                elif action == "save":
                    api_jobs_save(request, job_listing_id=jid_int, track=track_from_form)
                    entry = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    ).first()
                    _apply_save_action(entry, board_stage, request)
                    messages.success(request, _save_success_message(board_stage, power_user=power_user))
            except (HttpError, ValueError, TypeError) as e:
                messages.error(request, str(e))
        return redirect(next_url)

    entries_qs = PipelineEntry.objects.for_user(user).filter(track=raw_track, removed_at__isnull=True)
    if board_stage == "pipeline":
        entries_qs = entries_qs.filter(
            models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)
        )
    elif board_stage == "vetting":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.VETTING)
    elif board_stage == "applying":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.APPLYING)
    else:
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.DONE)

    entries = entries_qs.select_related("job_listing").order_by("-added_at")
    job_listings = [e.job_listing for e in entries]
    pipeline_jobs_full = pipeline_jobs_to_payloads(job_listings, track=raw_track, user=user)
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

    age_days_raw = (request.GET.get("age_days") or "").strip()
    raw_sort = (request.GET.get("sort_by") or request.GET.get("sort") or "latest").strip().lower()
    sort_by = raw_sort if raw_sort in ("latest", "newest", "match", "focus", "interview", "preference", "oldest") else "latest"

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

    if age_days_raw:
        try:
            max_age_days = int(age_days_raw)
            from django.utils import timezone
            now = timezone.now()
            filtered_jobs = []
            for j in pipeline_jobs:
                job_date = j.posted_at or j.fetched_at
                if job_date:
                    diff = now - job_date
                    if diff.days <= max_age_days:
                        filtered_jobs.append(j)
                else:
                    filtered_jobs.append(j)
            pipeline_jobs = filtered_jobs
        except ValueError:
            pass

    # Sort the final pipeline_jobs list according to the selected criterion
    from django.utils import timezone
    from datetime import datetime
    def get_job_date(j):
        return j.posted_at or j.fetched_at or datetime.min.replace(tzinfo=timezone.utc)

    if sort_by in ("latest", "newest"):
        pipeline_jobs.sort(key=get_job_date, reverse=True)
    elif sort_by == "oldest":
        pipeline_jobs.sort(key=get_job_date)
    elif sort_by in ("match", "focus"):
        def get_match_sort_key(j):
            val = j.focus_percent_after_penalty if j.focus_percent_after_penalty is not None else j.focus_percent
            has_val = val is not None
            score = float(val) if has_val else -1.0
            return (has_val, score, get_job_date(j))
        pipeline_jobs.sort(key=get_match_sort_key, reverse=True)
    elif sort_by == "preference":
        def get_pref_sort_key(j):
            val = j.preference_margin_percent
            has_val = val is not None
            score = float(val) if has_val else -9999.0
            return (has_val, score, get_job_date(j))
        pipeline_jobs.sort(key=get_pref_sort_key, reverse=True)
    elif sort_by == "interview":
        def get_interview_sort_key(j):
            val = j.interview_probability
            has_val = val is not None
            score = int(val) if has_val else -1
            return (has_val, score, get_job_date(j))
        pipeline_jobs.sort(key=get_interview_sort_key, reverse=True)

    pipeline_count_before_text_search = len(pipeline_jobs)

    base_qs = PipelineEntry.objects.for_user(user).filter(track=raw_track, removed_at__isnull=True)
    stage_counts = {
        "pipeline": base_qs.filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)).count(),
        "vetting": base_qs.filter(stage=PipelineEntry.Stage.VETTING).count(),
        "applying": base_qs.filter(stage=PipelineEntry.Stage.APPLYING).count(),
        "done": base_qs.filter(stage=PipelineEntry.Stage.DONE).count(),
    }
    pipeline_resume_summary_eligible_count = stage_counts["vetting"] + stage_counts["applying"]

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
        search_q or source_filter or pref_min_raw != "" or pref_max_raw != "" or age_days_raw != "" or (sort_by not in ("latest", "newest"))
    )

    if board_stage in ("vetting", "applying"):
        _attach_optimizer_user_resume_id(user, pipeline_jobs, raw_track)
    if board_stage == "applying":
        _attach_optimized_resume_ids_for_stage(
            user, pipeline_jobs, raw_track, PipelineEntry.Stage.APPLYING
        )
    elif board_stage == "done":
        _attach_optimized_resume_ids_for_stage(
            user, pipeline_jobs, raw_track, PipelineEntry.Stage.DONE
        )

    job_tasks_url = reverse("job_automation")
    stage_tab_labels = STAGE_TAB_LABELS_POWER if power_user else STAGE_TAB_LABELS_NORMAL
    if power_user:
        board_titles = {
            "pipeline": "Dashboard",
            "vetting": "Vetting",
            "applying": "Applying",
            "done": "Done",
        }
        board_subtitles = {
            "pipeline": (
                "Jobs from scheduled tasks. Focus % is computed when you open this page. "
                "Like, Dislike, Save, and Delete behave like Job Search."
            ),
            "vetting": (
                "Interview % shows only when the model reply is parsed to a number. "
                "Use Match debug on a job to run one call and inspect the raw response. "
                "Automation retries missing scores on a cooldown so unparsed replies do not spam the LLM."
            ),
            "applying": (
                "Active applications. Open optimizer opens the resume tool in a new tab with this job "
                "and your track resume prefilled—run when you are ready. Saving marks a job done after you submit."
            ),
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
    else:
        board_titles = {
            "pipeline": "My jobs",
            "vetting": "My jobs",
            "applying": "My jobs",
            "done": "My jobs",
        }
        board_subtitles = {
            "pipeline": "Manage your career pipeline from discovery to offer.",
            "vetting": "Manage your career pipeline from discovery to offer.",
            "applying": "Manage your career pipeline from discovery to offer.",
            "done": "Manage your career pipeline from discovery to offer.",
        }
        empty_messages = {
            "pipeline": "No new jobs yet.",
            "vetting": "No jobs in review.",
            "applying": "No jobs you're applying to right now.",
            "done": "No applied jobs yet.",
        }
        empty_help = {
            "pipeline": [
                "Scheduled searches add matching roles here automatically.",
                "Shortlist a job to move it to Review when you're ready to evaluate it.",
            ],
            "vetting": [
                "Save a job on Find jobs to add it here, or shortlist one from New.",
                "When a job looks promising, choose Ready to apply to move it to Applying.",
            ],
            "applying": [
                "Use Tailor resume to customize your PDF for each role before you submit.",
            ],
            "done": [],
        }

    board_empty_message = empty_messages[board_stage]
    board_empty_help_list = empty_help[board_stage]
    if pipeline_stage_total > 0 and not pipeline_jobs and pipeline_has_active_filters:
        if power_user:
            board_empty_message = (
                "No jobs match the current filters. Try clearing the text search, "
                "setting Source to All sources, or widening the Pref score range."
            )
        else:
            board_empty_message = "No jobs match your search."
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

    show_pipeline_resume_summary = power_user and board_stage in ("vetting", "applying")
    show_advanced_board_filters = power_user
    show_pipeline_track_tabs = (
        len(pipeline_track_tabs) > 1 if not power_user else tracks_qs.count() > 1
    )
    pipeline_resume_llm_providers: list[str] = []
    pipeline_resume_llm_provider: str | None = None
    pipeline_resume_llm_configured = False
    if show_pipeline_resume_summary:
        from .llm import LLM_PROVIDERS
        from .models import LLMProviderConfig
        from .pipeline_llm_skill_extract import resolve_provider_api_key

        configured = list(
            LLMProviderConfig.objects.for_user(user)
            .exclude(encrypted_api_key="")
            .values_list("provider", flat=True)
            .distinct()
        )
        if not configured:
            configured = [p for p in sorted(LLM_PROVIDERS) if resolve_provider_api_key(p, user=user)]
        pipeline_resume_llm_providers = sorted(set(configured))
        pipeline_resume_llm_configured = bool(pipeline_resume_llm_providers)
        chosen = (request.session.get("pipeline_resume_llm_provider") or "").strip()
        if chosen and chosen in pipeline_resume_llm_providers:
            pipeline_resume_llm_provider = chosen
        elif pipeline_resume_llm_providers:
            pipeline_resume_llm_provider = pipeline_resume_llm_providers[0]

    from .models import JobListingAction, UserResume
    from .track_actions import q_preference_embedding_track

    liked_actions = JobListingAction.objects.for_user(user).filter(
        action=JobListingAction.ActionType.LIKED,
    )
    liked_actions = liked_actions.filter(q_preference_embedding_track(raw_track, user))
    missing_liked_jobs = not liked_actions.exists()

    missing_library_resume = not UserResume.objects.for_user(user).filter(is_library=True).exists()

    context = {
        "missing_liked_jobs": missing_liked_jobs,
        "missing_library_resume": missing_library_resume,
        "pipeline_jobs": pipeline_jobs,
        "pipeline_track": raw_track,
        "pipeline_search_query": search_q if search_q else None,
        "pipeline_total_count": pipeline_stage_total,
        "pipeline_count_before_text_search": pipeline_count_before_text_search,
        "pipeline_has_active_filters": pipeline_has_active_filters,
        "pipeline_source_options": pipeline_source_options,
        "pipeline_source_selected": source_filter,
        "pipeline_sort_by": "latest" if sort_by in ("latest", "newest") else ("match" if sort_by in ("match", "focus") else sort_by),
        "pipeline_pref_min": pref_min_raw,
        "pipeline_pref_max": pref_max_raw,
        "pipeline_tracks": pipeline_track_tabs,
        "pipeline_track_tabs": pipeline_track_tabs,
        "stage_counts": stage_counts,
        "board_stage": board_stage,
        "board_stage_display": stage_tab_labels[board_stage],
        "board_page_title": board_titles[board_stage],
        "board_page_subtitle": board_subtitles[board_stage],
        "board_empty_message": board_empty_message,
        "board_empty_help_list": board_empty_help_list,
        "board_clear_url": clear_url,
        "stage_tab_labels": stage_tab_labels,
        "show_advanced_board_filters": show_advanced_board_filters,
        "show_pipeline_track_tabs": show_pipeline_track_tabs,
        "show_board_metrics": power_user and board_stage == "pipeline",
        "done_total": done_total,
        "done_this_week": done_this_week,
        "conversion_percent": conversion_percent,
        "job_tasks_url": job_tasks_url,
        "pipeline_board_extra_query": pipeline_board_extra_query,
        "show_pipeline_resume_summary": show_pipeline_resume_summary,
        "pipeline_resume_summary_eligible_count": pipeline_resume_summary_eligible_count,
        "pipeline_resume_llm_providers": pipeline_resume_llm_providers,
        "pipeline_resume_llm_provider": pipeline_resume_llm_provider,
        "pipeline_resume_llm_configured": pipeline_resume_llm_configured,
        "vetting_jd_min_chars": VETTING_MATCHING_JD_MIN_CHARS,
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
