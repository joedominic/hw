"""
Unified Pipeline → Vetting → Applying → Done board (one implementation, four URL routes).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from ninja.errors import HttpError

from .experience import has_my_jobs_search_profile, is_power_user
from .job_search_core import pipeline_jobs_to_payloads, VETTING_MATCHING_JD_MIN_CHARS
from .saved_searches import (
    backfill_saved_search_profile_tracks,
    pipeline_track_tabs_for_board,
    saved_search_profile_tabs,
)
from .search_profile_scope import resolve_active_profile_slug
from .jobs_api import (
    jobs_dislike as api_jobs_dislike,
    jobs_like as api_jobs_like,
    jobs_unlike as api_jobs_unlike,
    jobs_save as api_jobs_save,
)
from .llm import get_active_llm_provider
from .tasks import _resolve_user_resume_for_track
from .models import (
    AppAutomationSettings,
    JobListingAction,
    JobListingEmbedding,
    JobListingTrackMetrics,
    JobSearchTask,
    OptimizedResume,
    PipelineEntry,
    SearchProfile,
    Track,
    UserDisqualifier,
    UserResume,
)
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
    latest_events: dict[int, Any] = {}
    if entry_ids_list:
        from .models import EmployerInterviewEvent
        for orow in OptimizedResume.objects.for_user(user).filter(
            pipeline_entry_id__in=entry_ids_list
        ).order_by("-created_at"):
            eid = orow.pipeline_entry_id
            if eid is not None and eid not in latest_by_entry:
                latest_by_entry[eid] = orow.id
        for ev in EmployerInterviewEvent.objects.filter(
            pipeline_entry_id__in=entry_ids_list
        ).order_by("-created_at"):
            if ev.pipeline_entry_id not in latest_events:
                latest_events[ev.pipeline_entry_id] = ev
    for j in pipeline_jobs:
        pe = pe_by_job.get(j.id)
        if pe is not None:
            setattr(j, "pipeline_entry_id", pe.id)
            setattr(j, "has_interview_prep", bool((pe.interview_prep or "").strip()))
            setattr(j, "has_applied_resume", bool((pe.applied_resume_markdown or "").strip()))
            setattr(j, "applied_resume_markdown", pe.applied_resume_markdown or "")
            setattr(j, "post_apply_substatus", pe.post_apply_substatus or "")
            setattr(
                j,
                "post_apply_substatus_display",
                pe.get_post_apply_substatus_display()
                if hasattr(pe, "get_post_apply_substatus_display")
                else (pe.post_apply_substatus or ""),
            )
            setattr(j, "next_interview_at", pe.next_interview_at)
            latest_ev = latest_events.get(pe.id)
            if latest_ev:
                setattr(j, "latest_interview_event", latest_ev)
                setattr(j, "interview_notes", latest_ev.notes or "")
                setattr(j, "interview_names", latest_ev.interviewer_names or "")
                setattr(j, "interview_location", latest_ev.location_or_link or "")
                setattr(j, "interview_duration", latest_ev.duration_minutes or 45)
                setattr(j, "interview_status", latest_ev.status or "scheduled")
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
        return "Marked as applied (tagged as Liked)." if not power_user else "Job moved to Done (tagged as Liked)."
    return "Job saved to favourites."



def pipeline_board_view(request, board_stage: str, template_name: str = "resume_app/pipeline_board.html"):
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

    is_cockpit = (
        template_name == "resume_app/career_cockpit.html"
        or (request.resolver_match and request.resolver_match.url_name == "career_cockpit")
        or request.path.startswith("/jobs/cockpit/")
    )

    tracks_qs = Track.ensure_baseline(user)
    if is_cockpit:
        pipeline_track_tabs = saved_search_profile_tabs(user)
        try:
            counts_by_track = dict(
                PipelineEntry.objects.filter(owner=user, removed_at__isnull=True)
                .values("track")
                .annotate(cnt=models.Count("id"))
                .values_list("track", "cnt")
            )
            for t in pipeline_track_tabs:
                t["count"] = counts_by_track.get(t["slug"], 0)
        except Exception:
            pass
        board_track_slugs = {t["slug"] for t in pipeline_track_tabs}
        available_slugs = set(board_track_slugs)
    else:
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
    if is_cockpit:
        if not raw_track or raw_track not in board_track_slugs:
            raw_track = pipeline_track_tabs[0]["slug"] if pipeline_track_tabs else Track.get_default_slug(user)
    elif power_user:
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
        if is_cockpit:
            default_next = reverse("career_cockpit") + f"?track={raw_track}&stage={board_stage}"
        else:
            default_next = reverse(board_stage) + f"?track={raw_track}"
        next_url = request.POST.get("next") or default_next
        selected_ids = [jid for jid in job_ids if jid] or ([job_id] if job_id else [])

        if action == "save_search_profile":
            from .saved_searches import create_or_update_saved_search
            from .saved_search_schedule import set_saved_search_schedule

            preset_id_raw = (request.POST.get("preset_id") or "").strip()
            name = (request.POST.get("name") or "").strip()
            search_term = (request.POST.get("search_term") or "").strip()
            location = (request.POST.get("location") or "").strip()
            resume_id_raw = (request.POST.get("resume_id") or "").strip()
            site_names = request.POST.getlist("site_names")
            schedule_interval = (request.POST.get("schedule_interval") or "off").strip().lower()
            schedule_time = (request.POST.get("schedule_time") or "09:00").strip()
            is_default = bool(request.POST.get("is_default"))

            resume_file = request.FILES.get("resume_file")
            uploaded_resume = None
            if resume_file:
                original_name = (getattr(resume_file, "name", "") or "resume.pdf").strip()
                original_name = original_name.split("\\")[-1].split("/")[-1].strip()
                if original_name.lower().endswith(".pdf"):
                    uploaded_resume = UserResume.objects.create(
                        owner=user,
                        file=resume_file,
                        original_filename=original_name[:255],
                        is_library=True,
                    )

            resume_id = uploaded_resume.id if uploaded_resume else (int(resume_id_raw) if resume_id_raw.isdigit() else None)
            pid = int(preset_id_raw) if preset_id_raw.isdigit() else None

            if not name:
                messages.error(request, "Profile Name is required.")
                return redirect(next_url)
            if not search_term:
                messages.error(request, "Search Query is required.")
                return redirect(next_url)

            try:
                profile = create_or_update_saved_search(
                    user,
                    name=name,
                    search_term=search_term,
                    location=location,
                    resume_id=resume_id,
                    site_names=site_names if site_names else ["indeed", "linkedin"],
                    preset_id=pid,
                )
                if is_default:
                    SearchProfile.objects.filter(owner=user).update(is_default=False)
                    profile.is_default = True
                    profile.save(update_fields=["is_default"])

                if uploaded_resume:
                    uploaded_resume.track = profile.slug
                    uploaded_resume.save(update_fields=["track"])

                try:
                    set_saved_search_schedule(
                        user,
                        profile.id,
                        interval=schedule_interval,
                        time_str=schedule_time,
                    )
                except Exception as sched_err:
                    messages.warning(request, f"Profile saved, but scheduling warning: {sched_err}")

                messages.success(request, f'Search profile "{profile.name}" saved.')
                return redirect(reverse("career_cockpit") + f"?track={profile.slug}&stage={board_stage}")
            except Exception as e:
                messages.error(request, f"Could not save profile: {e}")
                return redirect(next_url)

        elif action == "delete_search_profile":
            from .views import invalidate_preference_cache, invalidate_disliked_embeddings_cache

            preset_id_raw = (request.POST.get("preset_id") or request.POST.get("delete_profile_id") or "").strip()
            slug_raw = (request.POST.get("delete_profile_slug") or request.POST.get("slug") or "").strip().lower()

            profile = None
            if preset_id_raw.isdigit():
                profile = SearchProfile.objects.filter(owner=user, id=int(preset_id_raw)).first()
            if not profile and slug_raw:
                profile = SearchProfile.objects.filter(owner=user, slug=slug_raw).first()

            if not profile:
                messages.error(request, "Search profile not found.")
                return redirect(next_url)

            slug_val = profile.slug
            was_default = profile.is_default
            p_name = profile.name

            # 1. Delete SearchProfile
            profile.delete()

            # 2. Delete all associated configuration, metrics, tasks, pipeline jobs, and ML training data
            UserResume.objects.filter(owner=user, is_library=True).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).update(track="")
            JobSearchTask.objects.filter(owner=user).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).delete()
            PipelineEntry.objects.filter(owner=user).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).delete()
            JobListingAction.objects.filter(owner=user).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).delete()
            JobListingEmbedding.objects.filter(owner=user).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).delete()
            JobListingTrackMetrics.objects.filter(owner=user).filter(
                models.Q(track__iexact=slug_val) | models.Q(track=slug_val)
            ).delete()
            Track.objects.filter(owner=user).filter(
                models.Q(slug__iexact=slug_val) | models.Q(slug=slug_val)
            ).delete()

            # 3. Cache & Session Invalidation
            try:
                invalidate_preference_cache(user, track=slug_val)
                invalidate_disliked_embeddings_cache(user, track=slug_val)
            except Exception:
                pass

            if request.session.get("job_search_track") == slug_val:
                request.session.pop("job_search_track", None)
            if request.session.get("job_search_profile_slug") == slug_val:
                request.session.pop("job_search_profile_slug", None)
            request.session.pop("job_search_cache", None)
            request.session.modified = True

            # 4. Ensure remaining profiles have a default, or baseline if none remain
            remaining_profiles = SearchProfile.objects.filter(owner=user)
            if not remaining_profiles.exists():
                SearchProfile.ensure_baseline(user)
                remaining_profiles = SearchProfile.objects.filter(owner=user)
                messages.success(
                    request,
                    f'Search profile "{p_name}" and all associated data deleted. A default profile has been created.',
                )
            else:
                if was_default or not remaining_profiles.filter(is_default=True).exists():
                    first_p = remaining_profiles.first()
                    if first_p:
                        first_p.is_default = True
                        first_p.save(update_fields=["is_default"])
                messages.success(request, f'Search profile "{p_name}" and all associated data deleted.')

            next_profile = remaining_profiles.filter(is_default=True).first() or remaining_profiles.first()
            next_track = next_profile.slug if next_profile else ""
            request.session["job_search_track"] = next_track
            request.session["job_search_profile_slug"] = next_track
            request.session.modified = True
            return redirect(reverse("career_cockpit") + f"?track={next_track}&stage={board_stage}")

        elif action == "add_disqualifier":
            phrase = (request.POST.get("phrase") or "").strip().lower()
            if phrase:
                UserDisqualifier.objects.get_or_create(owner=user, phrase=phrase)
            return redirect(reverse("career_cockpit") + f"?track={raw_track}&stage={board_stage}")

        elif action == "remove_disqualifier":
            phrase = (request.POST.get("phrase") or "").strip().lower()
            if phrase:
                UserDisqualifier.objects.filter(owner=user, phrase=phrase).delete()
            return redirect(reverse("career_cockpit") + f"?track={raw_track}&stage={board_stage}")

        elif action in ("update_pipeline_retention", "update_pipeline_policies"):
            cfg = AppAutomationSettings.get_for_user(user)
            update_fields = ["updated_at"]

            raw_days = (request.POST.get("cleanup_pipeline_retention_days") or "").strip()
            if raw_days:
                try:
                    days_val = int(raw_days)
                    if days_val in (3, 5, 7, 14, 30):
                        cfg.cleanup_pipeline_retention_days = days_val
                        cfg.cleanup_job_retention_days = days_val
                        cfg.cleanup_vetting_retention_days = days_val
                        cfg.cleanup_applying_retention_days = days_val
                        cfg.cleanup_done_retention_days = 0
                        update_fields.extend([
                            "cleanup_pipeline_retention_days",
                            "cleanup_job_retention_days",
                            "cleanup_vetting_retention_days",
                            "cleanup_applying_retention_days",
                            "cleanup_done_retention_days",
                        ])
                except (ValueError, TypeError):
                    pass

            raw_purge_max = (request.POST.get("pipeline_purge_match_score_max") or "").strip()
            if raw_purge_max:
                try:
                    purge_val = int(raw_purge_max)
                    if 0 <= purge_val <= 100:
                        cfg.pipeline_purge_match_score_max = purge_val
                        update_fields.append("pipeline_purge_match_score_max")
                except (ValueError, TypeError):
                    pass

            raw_promote_min = (request.POST.get("pipeline_match_score_min") or "").strip()
            if raw_promote_min:
                try:
                    promote_val = int(raw_promote_min)
                    if 0 <= promote_val <= 100:
                        cfg.pipeline_match_score_min = promote_val
                        update_fields.append("pipeline_match_score_min")
                except (ValueError, TypeError):
                    pass

            raw_vetting_promote = (request.POST.get("vetting_interview_probability_min") or "").strip()
            if raw_vetting_promote:
                if raw_vetting_promote in ("disabled", "off", "0", "manual"):
                    cfg.vetting_to_applying_enabled = False
                    update_fields.append("vetting_to_applying_enabled")
                else:
                    try:
                        vip_val = int(raw_vetting_promote)
                        if 0 <= vip_val <= 100:
                            cfg.vetting_to_applying_enabled = True
                            cfg.vetting_interview_probability_min = vip_val
                            update_fields.extend(["vetting_to_applying_enabled", "vetting_interview_probability_min"])
                    except (ValueError, TypeError):
                        pass

            if len(update_fields) > 1:
                cfg.save(update_fields=list(set(update_fields)))
                messages.success(request, "Pipeline thresholds & policies updated.")
            return redirect(next_url)

        elif action in ("pipeline_cleanup_policies", "purge_pipeline_retention_now"):
            from .tasks import apply_pipeline_cleanup_policies
            cfg = AppAutomationSettings.get_for_user(user)
            res = apply_pipeline_cleanup_policies(user)
            purged = res.get("purged", 0)
            promoted = res.get("promoted", 0)
            if purged > 0 and promoted > 0:
                messages.success(request, f"Cleanup complete: {purged} low-scoring/expired role(s) retired, {promoted} qualified role(s) promoted. Applied roles preserved.")
            elif purged > 0:
                messages.success(request, f"Cleanup complete: {purged} role(s) retired (older than {cfg.cleanup_pipeline_retention_days} days or scored below {cfg.pipeline_purge_match_score_max}%). Applied roles preserved.")
            elif promoted > 0:
                messages.success(request, f"Cleanup complete: {promoted} qualified role(s) promoted across the pipeline.")
            else:
                messages.info(request, "Pipeline is already clean and compliant with your thresholds. Applied roles preserved.")
            return redirect(next_url)

        if action in {"bulk_delete", "bulk_like", "bulk_dislike", "bulk_promote"}:
            if not selected_ids:
                if not is_cockpit:
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
                        removed_at__isnull=True,
                    )
                    if track_from_form and track_from_form != "*":
                        entries = entries.filter(
                            models.Q(track=track_from_form) | models.Q(track__iexact=track_from_form)
                        )
                    for entry in entries:
                        try:
                            api_jobs_save(request, job_listing_id=jid_int, track=entry.track)
                        except Exception:
                            pass
                        _apply_save_action(entry, board_stage, request)
                        success_count += 1
                if success_count and not is_cockpit:
                    if board_stage == "pipeline":
                        messages.success(request, f"Shortlisted {success_count} job(s) and advanced to Review.")
                    elif board_stage == "vetting":
                        messages.success(request, f"Advanced {success_count} job(s) to Applying.")
                    elif board_stage == "applying":
                        messages.success(request, f"Marked {success_count} job(s) as Applied (tagged as Liked).")
                    else:
                        messages.success(request, f"Advanced {success_count} job(s) to the next stage.")
            elif action == "bulk_delete":
                for jid in selected_ids:
                    try:
                        jid_int = int(jid)
                    except (ValueError, TypeError):
                        continue
                    entries = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        removed_at__isnull=True,
                    )
                    if track_from_form and track_from_form != "*":
                        entries = entries.filter(
                            models.Q(track=track_from_form) | models.Q(track__iexact=track_from_form)
                        )
                    for entry in entries:
                        entry.mark_deleted(save=True)
                        success_count += 1
                if success_count and not is_cockpit:
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
                if success_count and not is_cockpit:
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
            except (ValueError, TypeError):
                messages.error(request, "Invalid job id.")
        elif action in ("like", "unlike", "dislike", "save") and job_id:
            request.session["job_search_track"] = track_from_form
            try:
                jid_int = int(job_id)
                if action == "like":
                    api_jobs_like(request, job_listing_id=jid_int, track=track_from_form)
                elif action == "unlike":
                    api_jobs_unlike(request, job_listing_id=jid_int, track=track_from_form)
                elif action == "dislike":
                    api_jobs_dislike(request, job_listing_id=jid_int, track=track_from_form)
                    entries = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    )
                    for entry in entries:
                        entry.mark_deleted(save=True)
                elif action == "save":
                    api_jobs_save(request, job_listing_id=jid_int, track=track_from_form)
                    entry = PipelineEntry.objects.for_user(user).filter(
                        job_listing_id=jid_int,
                        track=track_from_form,
                        removed_at__isnull=True,
                    ).first()
                    _apply_save_action(entry, board_stage, request)
            except (HttpError, ValueError, TypeError) as e:
                messages.error(request, str(e))
        return redirect(next_url)

    automation_settings = AppAutomationSettings.get_for_user(user) if user.is_authenticated else None
    pipeline_retention_days = int(automation_settings.cleanup_pipeline_retention_days or 7) if automation_settings else 7
    pipeline_cutoff = timezone.now() - timedelta(days=pipeline_retention_days) if pipeline_retention_days > 0 else None

    entries_qs = PipelineEntry.objects.for_user(user).filter(track=raw_track, removed_at__isnull=True)
    if board_stage == "pipeline":
        entries_qs = entries_qs.filter(
            models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE)
        )
        if pipeline_cutoff:
            entries_qs = entries_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
    elif board_stage == "vetting":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.VETTING)
        if pipeline_cutoff:
            entries_qs = entries_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
    elif board_stage == "applying":
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.APPLYING)
        if pipeline_cutoff:
            entries_qs = entries_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
    else:
        # Applied (Done) stage is strictly exempt from retention cutoff
        entries_qs = entries_qs.filter(stage=PipelineEntry.Stage.DONE)

    entries = entries_qs.select_related("job_listing").order_by("-added_at")
    job_listings = [e.job_listing for e in entries]
    pipeline_jobs_full = pipeline_jobs_to_payloads(job_listings, track=raw_track, user=user)
    pipeline_stage_total = len(pipeline_jobs_full)

    from django.utils.timesince import timesince
    for j in pipeline_jobs_full:
        f_dt = getattr(j, "fetched_at", None)
        p_dt = getattr(j, "posted_at", None)
        diff_days = abs((f_dt.date() - p_dt.date()).days) if (f_dt and p_dt) else 0
        j.has_date_diff = diff_days >= 1
        j.sourced_age = f"Sourced {timesince(f_dt)} ago" if f_dt else ""
        j.posted_age = f"Posted {timesince(p_dt)} ago" if p_dt else ""
        if j.has_date_diff and j.sourced_age and j.posted_age:
            j.combined_age = f"{j.sourced_age} ({j.posted_age})"
        elif j.sourced_age:
            j.combined_age = j.sourced_age
        elif j.posted_age:
            j.combined_age = j.posted_age
        else:
            j.combined_age = "Recently"

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
    sort_by = raw_sort if raw_sort in ("latest", "newest", "score", "match", "focus", "interview", "preference", "oldest") else "latest"

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
            now = timezone.now()
            filtered_jobs = []
            for j in pipeline_jobs:
                job_date = j.fetched_at or j.posted_at
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
    from datetime import datetime
    def get_job_date(j):
        return j.fetched_at or j.posted_at or datetime.min.replace(tzinfo=timezone.utc)

    if sort_by in ("latest", "newest"):
        pipeline_jobs.sort(key=get_job_date, reverse=True)
    elif sort_by == "oldest":
        pipeline_jobs.sort(key=get_job_date)
    elif sort_by in ("score", "match", "focus"):
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
    pipeline_count_qs = base_qs.filter(models.Q(stage="") | models.Q(stage=PipelineEntry.Stage.PIPELINE))
    vetting_count_qs = base_qs.filter(stage=PipelineEntry.Stage.VETTING)
    applying_count_qs = base_qs.filter(stage=PipelineEntry.Stage.APPLYING)
    if pipeline_cutoff:
        pipeline_count_qs = pipeline_count_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
        vetting_count_qs = vetting_count_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
        applying_count_qs = applying_count_qs.filter(job_listing__fetched_at__gte=pipeline_cutoff)
    stage_counts = {
        "pipeline": pipeline_count_qs.count(),
        "vetting": vetting_count_qs.count(),
        "applying": applying_count_qs.count(),
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

    from .track_actions import q_preference_embedding_track

    liked_actions = JobListingAction.objects.for_user(user).filter(
        action=JobListingAction.ActionType.LIKED,
    )
    liked_actions = liked_actions.filter(q_preference_embedding_track(raw_track, user))
    missing_liked_jobs = not liked_actions.exists()

    missing_library_resume = not UserResume.objects.for_user(user).filter(is_library=True).exists()

    disqualifiers = []
    bound_resume = None
    active_search_profile = None
    scheduled_task = None
    last_task_run = None
    task_cooldown_remaining_seconds = 0
    is_task_running = False
    task_frequency_display = ""
    if user.is_authenticated:
        disqualifiers = list(UserDisqualifier.objects.filter(owner=user).values_list("phrase", flat=True))
        bound_resume = UserResume.objects.filter(owner=user, is_library=True, track=raw_track).first() or UserResume.objects.filter(owner=user, is_library=True).first()

        active_search_profile = SearchProfile.objects.filter(owner=user, slug=raw_track).first()
        if not active_search_profile:
            active_search_profile = SearchProfile.objects.filter(owner=user, is_default=True).first() or SearchProfile.objects.filter(owner=user).first()

        if active_search_profile:
            try:
                scheduled_task = getattr(active_search_profile, "scheduled_task", None)
            except Exception:
                scheduled_task = None
            if not scheduled_task:
                scheduled_task = JobSearchTask.objects.filter(owner=user, track=active_search_profile.slug).first()
            if not scheduled_task and active_search_profile:
                try:
                    from .saved_search_schedule import sync_job_search_task_from_saved
                    scheduled_task = JobSearchTask(
                        owner=user,
                        saved_search=active_search_profile,
                        is_active=True,
                        frequency="0 9 * * *",
                    )
                    sync_job_search_task_from_saved(active_search_profile, scheduled_task)
                    scheduled_task.save()
                except Exception as e:
                    logger.warning("Could not auto-create scheduled_task for SearchProfile %s: %s", getattr(active_search_profile, "id", None), e)
                    scheduled_task = None

            if scheduled_task:
                last_task_run = scheduled_task.runs.first()

                from django.core.cache import cache
                import time
                from .tasks import get_job_search_task_lock_key
                from .models import JobSearchTaskRun

                is_pending = bool(cache.get(f"job_task_pending:{scheduled_task.id}"))
                lock_key = get_job_search_task_lock_key(user.id)
                if is_pending or cache.get(lock_key) or scheduled_task.runs.filter(status=JobSearchTaskRun.STATUS_RUNNING).exists():
                    is_task_running = True

                cooldown_expires = cache.get(f"job_task_manual_cooldown:{scheduled_task.id}")
                if last_task_run and last_task_run.status == JobSearchTaskRun.STATUS_FAILED and not is_task_running:
                    cache.delete(f"job_task_manual_cooldown:{scheduled_task.id}")
                    task_cooldown_remaining_seconds = 0
                elif cooldown_expires and cooldown_expires > time.time():
                    task_cooldown_remaining_seconds = int(cooldown_expires - time.time())

        if scheduled_task and getattr(scheduled_task, "frequency", None):
            from .utils import format_cron_human_friendly
            task_frequency_display = format_cron_human_friendly(scheduled_task.frequency)
        elif active_search_profile and getattr(active_search_profile, "schedule_interval", None) and active_search_profile.schedule_interval != 'off':
            from .saved_search_schedule import schedule_display_label
            task_frequency_display = schedule_display_label(
                active_search_profile.schedule_interval,
                str(getattr(active_search_profile, "schedule_time", None) or "09:00")[:5]
            )

    if template_name == "resume_app/career_cockpit.html":
        import json
        import re
        from django.core.cache import cache
        from django.db.models import Q

        resume_text = ""
        if bound_resume and bound_resume.file:
            cache_key = f"cockpit_resume_text_{bound_resume.id}"
            resume_text = cache.get(cache_key)
            if resume_text is None:
                try:
                    from .services import parse_pdf
                    resume_text = (parse_pdf(bound_resume.file.path) or "").lower()
                    cache.set(cache_key, resume_text, 3600)
                except Exception:
                    resume_text = ""

        # Fetch user's liked and disliked training signal for this profile
        track_liked_actions = list(
            JobListingAction.objects.filter(owner=user, action=JobListingAction.ActionType.LIKED)
            .filter(Q(track__iexact=raw_track) | Q(track=""))
            .select_related("job_listing")[:30]
        )
        track_disliked_actions = list(
            JobListingAction.objects.filter(owner=user, action=JobListingAction.ActionType.DISLIKED)
            .filter(Q(track__iexact=raw_track) | Q(track=""))
            .select_related("job_listing")[:30]
        )

        def calc_similarity_pct(cand_title: str, cand_desc: str, ref_title: str, ref_desc: str) -> int:
            """Ultra-fast, deterministic semantic token similarity for explainable ranking influences."""
            ct_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', (cand_title or "").lower()))
            rt_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', (ref_title or "").lower()))
            t_overlap = len(ct_words & rt_words) / max(1, len(ct_words | rt_words)) if (ct_words or rt_words) else 0.5

            cd_words = set(re.findall(r'\b[a-zA-Z]{4,}\b', (cand_desc or "")[:800].lower()))
            rd_words = set(re.findall(r'\b[a-zA-Z]{4,}\b', (ref_desc or "")[:800].lower()))
            d_overlap = len(cd_words & rd_words) / max(1, len(cd_words | rd_words)) if (cd_words or rd_words) else 0.4

            comb = (t_overlap * 0.65) + (d_overlap * 0.35)
            return int(round(55 + (comb * 42)))

        SALARY_PATTERN = re.compile(
            r'(?:\$|\bUSD\s*)\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}k)'
            r'(?:\s*(?:-|–|—|to)\s*(?:\$|\bUSD\s*)?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}k))?'
            r'(?:\s*(?:/|per|a)?\s*(year|yr|annum|annual|annually|hour|hr|hourly|month|mo))?',
            re.IGNORECASE,
        )
        HOURLY_PATTERN = re.compile(
            r'\$\s*(\d{2,3}(?:\.\d{2})?)\s*(?:-|–|—|to)\s*\$?\s*(\d{2,3}(?:\.\d{2})?)\s*(?:/|per)?\s*(?:hr|hour|hourly)',
            re.IGNORECASE,
        )

        def extract_job_salary(job) -> str | None:
            raw = getattr(job, "raw_json", None)
            if isinstance(raw, dict):
                min_amt = raw.get("min_amount")
                max_amt = raw.get("max_amount")
                currency = raw.get("currency") or "USD"
                interval = (raw.get("interval") or "").lower()
                if min_amt or max_amt:
                    curr_sym = "$" if str(currency).upper() in ("USD", "CAD", "$") else f"{currency} "
                    inter_str = f" / {interval}" if interval in ("year", "yr", "hour", "hr", "month") else ""
                    if min_amt and max_amt and min_amt != max_amt:
                        return f"{curr_sym}{int(min_amt):,} – {curr_sym}{int(max_amt):,}{inter_str}"
                    elif min_amt:
                        return f"{curr_sym}{int(min_amt):,}{inter_str}"
                    elif max_amt:
                        return f"Up to {curr_sym}{int(max_amt):,}{inter_str}"

            desc = getattr(job, "description", "") or getattr(job, "snippet", "") or ""
            if not desc:
                return None

            for match in SALARY_PATTERN.finditer(desc):
                start_idx = max(0, match.start() - 20)
                end_idx = min(len(desc), match.end() + 20)
                context_around = desc[start_idx:end_idx].lower()

                if "401" in context_around or "referral" in context_around:
                    continue
                if "bonus" in context_around and not any(k in context_around for k in ("base", "salary", "pay", "comp")):
                    continue

                amt1 = match.group(1)
                amt2 = match.group(2)
                interval = match.group(3)

                def parse_num(s):
                    if not s:
                        return 0.0
                    s = s.lower().replace(",", "").replace("$", "").replace("usd", "").strip()
                    if s.endswith("k"):
                        return float(s[:-1]) * 1000
                    try:
                        return float(s)
                    except ValueError:
                        return 0.0

                n1 = parse_num(amt1)
                n2 = parse_num(amt2)

                if not amt2 and n1 < 30000 and not interval:
                    continue

                clean1 = f"${int(n1):,}" if n1 >= 1000 else f"${n1:.2f}".rstrip("0").rstrip(".")
                if amt2 and n2:
                    clean2 = f"${int(n2):,}" if n2 >= 1000 else f"${n2:.2f}".rstrip("0").rstrip(".")
                    res = f"{clean1} – {clean2}"
                else:
                    res = clean1

                if interval:
                    clean_int = interval.lower()
                    if clean_int in ("year", "yr", "annum", "annual", "annually"):
                        res += " / yr"
                    elif clean_int in ("hour", "hr", "hourly"):
                        res += " / hr"
                    elif clean_int in ("month", "mo"):
                        res += " / mo"

                return res

            h_match = HOURLY_PATTERN.search(desc)
            if h_match:
                return re.sub(r"\s+", " ", h_match.group(0)).replace("-", "–").strip()

            return None

        from .skill_radar import SkillRadarService
        resume_id_tag = bound_resume.id if bound_resume else "none"
        for job in pipeline_jobs:
            desc = getattr(job, "description", None) or getattr(job, "snippet", "") or ""
            ollama_cache_key = SkillRadarService.get_cache_key(getattr(user, "id", None), job.id, getattr(bound_resume, "id", None))
            cached_radar = cache.get(ollama_cache_key)
            job_core = getattr(job, "zero_llm_core_matches", None) or []
            job_stretch = getattr(job, "zero_llm_stretch_skills", None) or []
            if (
                cached_radar
                and isinstance(cached_radar, dict)
                and cached_radar.get("source") != "error"
                and (cached_radar.get("core_competencies") or cached_radar.get("match_score") is not None)
            ):
                core = cached_radar.get("core_competencies") or job_core
                stretch = cached_radar.get("stretch_skills") or job_stretch
                calc_pct = (
                    cached_radar.get("match_score")
                    or getattr(job, "vetting_match_score", None)
                    or getattr(job, "focus_percent_after_penalty", None)
                    or getattr(job, "focus_percent", None)
                    or 78
                )
                fit_summary = cached_radar.get("fit_summary") or getattr(job, "interview_reasoning", "") or ""
                source_radar = "ollama_local"
            elif job_core or getattr(job, "vetting_match_score", None) is not None or getattr(job, "interview_probability", None) is not None:
                core = job_core
                stretch = job_stretch
                if getattr(job, "vetting_match_score", None) is not None:
                    calc_pct = job.vetting_match_score
                elif core or stretch:
                    tot = len(core) + len(stretch)
                    calc_pct = round((len(core) / tot) * 100) if tot > 0 else (getattr(job, "focus_percent", None) or 75)
                else:
                    calc_pct = getattr(job, "focus_percent_after_penalty", None) or getattr(job, "focus_percent", None) or 75
                fit_summary = getattr(job, "interview_reasoning", "") or ""
                source_radar = "ollama_local"
            else:
                core = []
                stretch = []
                calc_pct = getattr(job, "focus_percent_after_penalty", None) or getattr(job, "focus_percent", None) or 0
                fit_summary = ""
                source_radar = "unanalyzed"

            # Compute Positive Influences (roles the user liked that boosted ranking)
            pos_influences = []
            for la in track_liked_actions:
                ref_j = getattr(la, "job_listing", None)
                if not ref_j or ref_j.id == job.id:
                    continue
                r_desc = getattr(ref_j, "description", None) or getattr(ref_j, "snippet", "") or ""
                sim = calc_similarity_pct(job.title, desc, ref_j.title, r_desc)
                pos_influences.append({
                    "job_id": ref_j.id,
                    "title": ref_j.title or "Untitled Role",
                    "company": ref_j.company_name or "Unknown Company",
                    "location": ref_j.location or "",
                    "match_pct": sim,
                    "type": "liked",
                    "url": ref_j.url or "",
                })
            pos_influences.sort(key=lambda x: -x["match_pct"])
            pos_influences = pos_influences[:5]

            # Compute Negative Influences (roles the user disliked that exerted negative pull/penalty)
            neg_influences = []
            for da in track_disliked_actions:
                ref_j = getattr(da, "job_listing", None)
                if not ref_j or ref_j.id == job.id:
                    continue
                r_desc = getattr(ref_j, "description", None) or getattr(ref_j, "snippet", "") or ""
                sim = calc_similarity_pct(job.title, desc, ref_j.title, r_desc)
                neg_influences.append({
                    "job_id": ref_j.id,
                    "title": ref_j.title or "Untitled Role",
                    "company": ref_j.company_name or "Unknown Company",
                    "location": ref_j.location or "",
                    "match_pct": sim,
                    "type": "disliked",
                    "url": ref_j.url or "",
                })
            neg_influences.sort(key=lambda x: -x["match_pct"])
            neg_influences = neg_influences[:5]

            setattr(job, "zero_llm_core_matches", core[:8])
            setattr(job, "zero_llm_stretch_skills", stretch[:8])
            setattr(job, "zero_llm_match_pct", calc_pct)
            setattr(job, "zero_llm_core_json", json.dumps(core[:8]))
            setattr(job, "zero_llm_stretch_json", json.dumps(stretch[:8]))
            setattr(job, "zero_llm_fit_summary", fit_summary)
            setattr(job, "skill_radar_source", source_radar)
            setattr(job, "positive_influences_json", json.dumps(pos_influences))
            setattr(job, "negative_influences_json", json.dumps(neg_influences))
            setattr(job, "similar_jobs_json", json.dumps(pos_influences))
            setattr(job, "extracted_salary", extract_job_salary(job))

        if sort_by in ("score", "match", "focus"):
            pipeline_jobs.sort(
                key=lambda j: (
                    getattr(j, "zero_llm_match_pct", None) is not None,
                    getattr(j, "zero_llm_match_pct", None) or getattr(j, "focus_percent_after_penalty", None) or getattr(j, "focus_percent", None) or 0,
                    get_job_date(j)
                ),
                reverse=True
            )

    user_resumes = []
    all_search_profiles = []
    search_profiles_json = "[]"
    if template_name == "resume_app/career_cockpit.html" and user.is_authenticated:
        from .saved_search_schedule import _task_for_saved_search, schedule_from_cron
        user_resumes = list(UserResume.objects.filter(owner=user, is_library=True).order_by("-uploaded_at"))
        all_search_profiles = list(SearchProfile.objects.filter(owner=user).order_by("-is_default", "name"))
        sp_data = []
        for p in all_search_profiles:
            p_interval = "off"
            p_time = "09:00"
            task = _task_for_saved_search(user, p)
            if task and task.is_active:
                parsed = schedule_from_cron(task.frequency)
                if parsed:
                    p_interval, p_time = parsed
            elif p.schedule_interval and p.schedule_interval != "off":
                p_interval = p.schedule_interval
                if p.schedule_time:
                    p_time = p.schedule_time.strftime("%H:%M")
            sp_data.append({
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "search_term": p.search_term or "",
                "location": p.location or "",
                "resume_id": p.resume_id or (bound_resume.id if bound_resume else None),
                "site_names": p.site_names or ["indeed", "linkedin"],
                "schedule_interval": p_interval,
                "schedule_time": p_time,
                "is_default": bool(p.is_default),
            })
        search_profiles_json = json.dumps(sp_data)

    local_model_name = "Local AI Model"
    if user.is_authenticated:
        try:
            from .models import LLMProviderPreference
            user_pref = LLMProviderPreference.objects.filter(
                provider_config__owner=user,
                provider_config__provider__in=["Ollama Local", "Ollama (Local)"],
                provider_config__is_active=True,
            ).first()
            if user_pref and user_pref.model:
                owner_username = str(getattr(user_pref.provider_config.owner, "username", ""))
                if "NVIDIA-Nemotron" in user_pref.model or "migration_bootstrap" in owner_username:
                    local_model_name = "Local AI Model"
                else:
                    local_model_name = user_pref.model
        except Exception:
            local_model_name = "Local AI Model"

    context = {
        "automation_settings": automation_settings,
        "pipeline_retention_days": pipeline_retention_days,
        "local_model_name": local_model_name,
        "user_resumes": user_resumes,
        "all_search_profiles": all_search_profiles,
        "search_profiles_json": search_profiles_json,
        "active_search_profile": active_search_profile,
        "scheduled_task": scheduled_task,
        "last_task_run": last_task_run,
        "task_cooldown_remaining_seconds": task_cooldown_remaining_seconds,
        "is_task_running": is_task_running,
        "task_frequency_display": task_frequency_display if template_name == "resume_app/career_cockpit.html" else "",
        "missing_liked_jobs": missing_liked_jobs,
        "missing_library_resume": missing_library_resume,
        "disqualifiers": disqualifiers,
        "bound_resume": bound_resume,
        "pipeline_jobs": pipeline_jobs,
        "pipeline_track": raw_track,
        "pipeline_search_query": search_q if search_q else None,
        "pipeline_total_count": pipeline_stage_total,
        "pipeline_count_before_text_search": pipeline_count_before_text_search,
        "pipeline_has_active_filters": pipeline_has_active_filters,
        "pipeline_source_options": pipeline_source_options,
        "pipeline_source_selected": source_filter,
        "pipeline_sort_by": "latest" if sort_by in ("latest", "newest") else ("score" if sort_by in ("score", "match", "focus") else sort_by),
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
    if is_cockpit:
        from django.contrib.messages import get_messages
        list(get_messages(request))
    return render(request, template_name, context)


def career_cockpit_view(request):
    from django.contrib.messages import get_messages
    # Drain any queued messages in session so they do not build up or spill into other pages
    list(get_messages(request))
    stage = (request.GET.get("stage") or "pipeline").strip().lower()
    if stage not in BOARD_STAGES:
        stage = "pipeline"
    return pipeline_board_view(request, stage, template_name="resume_app/career_cockpit.html")


def pipeline_view(request):
    return pipeline_board_view(request, "pipeline")


def vetting_view(request):
    return pipeline_board_view(request, "vetting")


def applying_view(request):
    return pipeline_board_view(request, "applying")


def done_view(request):
    return pipeline_board_view(request, "done")


@login_required
def cockpit_skill_radar_api(request):
    """
    API endpoint to run or fetch deep Ollama Local Skill Radar diagnostics on-demand.
    """
    job_id = (request.GET.get("job_id") or request.POST.get("job_id") or "").strip()
    if not job_id:
        return JsonResponse({"error": "job_id is required"}, status=400)

    from .models import JobListing, SearchProfile, UserResume
    from .skill_radar import SkillRadarService
    from .services import parse_pdf

    job = JobListing.objects.filter(id=job_id).first()
    if not job:
        return JsonResponse({"error": "Job listing not found"}, status=404)

    raw_track = (request.GET.get("track") or "").strip()
    bound_resume = _resolve_user_resume_for_track(request.user, raw_track)
    if not bound_resume:
        active_prof = SearchProfile.objects.filter(owner=request.user, is_default=True).first()
        if active_prof and active_prof.resume:
            bound_resume = active_prof.resume
        else:
            bound_resume = UserResume.objects.filter(owner=request.user, is_library=True).order_by("-uploaded_at").first()

    resume_text = ""
    resume_id = getattr(bound_resume, "id", None)
    if bound_resume and bound_resume.file:
        try:
            resume_text = parse_pdf(bound_resume.file.path)
        except Exception as e:
            logger.warning("Could not parse resume PDF: %s", e)

    force_refresh = request.GET.get("force") in ("1", "true") or request.POST.get("force") in ("1", "true")
    res = SkillRadarService.analyze(
        job,
        resume_text,
        user=request.user,
        resume_id=resume_id,
        force_refresh=force_refresh,
    )
    if res.get("source") != "error" and res.get("match_score") is not None:
        entry = PipelineEntry.objects.filter(owner=request.user, job_listing=job).first()
        if entry:
            entry.record_vetting_interview_result(
                probability=res.get("match_score"),
                reasoning=res.get("fit_summary"),
                resume_id=resume_id,
                core_competencies=res.get("core_competencies"),
                stretch_skills=res.get("stretch_skills"),
                save=True,
            )
    return JsonResponse({
        "status": "ok",
        "job_id": job.id,
        "data": res,
    })
