"""
Server-rendered pages: GET templates and POST handlers that redirect (often with messages).

Form-based flows live here (tracks/resumes, automation, settings, optimizer-adjacent pages).
JSON/HTMX/async actions use Django Ninja in `resume_app.api` (optimizer, LLM, workflows) and
`resume_app.jobs_api` (job search, pipeline, preferences). See `resume_app/docs/ARCHITECTURE_UI.md`.
"""
import json
import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.db import models
from django.db.models import Q

from .tenancy import get_active_user, get_owned_or_404
from ninja.errors import HttpError

from .api import (
    get_prompts as api_get_prompts,
    optimize_resume as api_optimize_resume,
    get_status as api_get_status,
    get_status_data as api_get_status_data,
    llm_connect as api_llm_connect,
    llm_models as api_llm_models,
    llm_set_default_model as api_llm_set_default_model,
    ConnectRequest,
    OptimizeRequest,
    LLM_PROVIDERS,
)
from .jobs_api import (
    jobs_list_resumes as api_jobs_list_resumes,
    jobs_search as api_jobs_search,
    jobs_saved as api_jobs_saved,
    jobs_disliked as api_jobs_disliked,
    jobs_matches as api_jobs_matches,
    jobs_match as api_jobs_match,
    jobs_like as api_jobs_like,
    jobs_dislike as api_jobs_dislike,
    jobs_hide as api_jobs_hide,
    jobs_unhide as api_jobs_unhide,
    jobs_save as api_jobs_save,
    jobs_unsave as api_jobs_unsave,
    jobs_mark_applied as api_jobs_mark_applied,
    get_focus_breakdown,
    JobSearchRequest,
    MarkAppliedRequest,
)
from .pipeline_board import (
    applying_view,
    career_cockpit_view,
    cockpit_skill_radar_api,
    done_view,
    pipeline_view,
    vetting_view,
)
from .models import (
    JobListingAction,
    PipelineEntry,
    JobSearchTask,
    JobSearchTaskRun,
    Track,
    JobListingEmbedding,
    LLMProviderConfig,
    AppAutomationSettings,
)
from .pipeline_llm_skill_extract import resolve_provider_api_key
from .preference import invalidate_preference_cache, invalidate_disliked_embeddings_cache
from .job_sources import ALLOWED_SITE_NAMES, DEFAULT_SITE_NAMES, normalize_site_names
from .job_search_core import rehydrate_job_payloads
from .tasks import run_job_search_task, get_next_run_at, validate_cron, try_vetting_match_debug, VETTING_MATCHING_JD_MIN_CHARS
from .utils import cron_to_short_description
from .huey_dashboard import (
    ADHOC_RUN_NOW_TASKS,
    PERIODIC_TASKS,
    get_periodic_task_info,
    get_periodic_task_wrapper,
    get_run_now_display_name,
    run_now_task_names,
)
from .prompt_store import get_effective_prompts, save_prompts_to_profile, clear_all_prompts_in_profile
from .llm.session import (
    get_active_llm_provider as _get_active_llm_provider,
    get_provider_preferences as _get_provider_preferences,
    get_provider_preference_rows as _get_provider_preference_rows,
    set_active_provider as _set_active_llm_provider,
)
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden

logger = logging.getLogger(__name__)


def _job_search_options_open(
    user,
    *,
    min_score_raw: str,
    resume_id_val: int | None,
    results_wanted_val: int,
    selected_site_names: list[str],
    raw_track: str,
    default_track: str,
    single_search_profile: bool,
    selected_preset_id: int | None,
    llm_model_in_get: bool,
) -> bool:
    """Expand advanced job-search controls when power user or non-default filters are active."""
    from .experience import is_power_user

    if is_power_user(user):
        return True
    if selected_preset_id:
        return True
    if min_score_raw:
        return True
    if resume_id_val is not None:
        return True
    if results_wanted_val != 50:
        return True
    if set(selected_site_names) != set(DEFAULT_SITE_NAMES):
        return True
    if not single_search_profile and raw_track != default_track:
        return True
    if llm_model_in_get:
        return True
    return False


def _staff_required(view_fn):
    """Decorator: returns 403 for non-staff users on operational/monitor endpoints."""
    from functools import wraps

    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not (request.user and request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden("Staff access required.")
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _user_library_resumes(user):
    from .models import UserResume

    return UserResume.objects.for_user(user).filter(is_library=True)


def _parse_task_form(request, default_track: str, valid_track_slugs: set):
    """
    Parse shared job task form fields from POST. Returns (data, errors).
    data: dict with name, search_term, location, track, jobs_to_fetch, frequency, start_time, site_name.
    errors: list of message strings; if non-empty, data may be incomplete.
    """
    name = (request.POST.get("name") or "").strip()
    search_term = (request.POST.get("search_term") or "").strip()
    if not search_term:
        return {}, ["Search term is required."]
    location = (request.POST.get("location") or "").strip()
    raw_track = (request.POST.get("track") or "").strip().lower()
    track = raw_track if raw_track in valid_track_slugs else default_track
    try:
        jobs_to_fetch = max(10, min(200, int(request.POST.get("jobs_to_fetch") or 50)))
    except ValueError:
        jobs_to_fetch = 50
    frequency = (request.POST.get("frequency") or "0 9 * * *").strip()
    try:
        validate_cron(frequency)
    except ValueError as e:
        return {}, [str(e)]
    start_time = None
    start_time_str = (request.POST.get("start_time") or "").strip()
    if start_time_str:
        try:
            from datetime import datetime
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
        except ValueError:
            pass
    site_name = normalize_site_names(request.POST.getlist("site_name") or None)
    return {
        "name": name,
        "search_term": search_term,
        "location": location,
        "track": track,
        "jobs_to_fetch": jobs_to_fetch,
        "frequency": frequency,
        "start_time": start_time,
        "site_name": site_name,
    }, []


def _save_optimizer_supporting_context(
    user,
    optimization_notes: str,
    pipeline_skills_json: str,
    job_highlights: str,
) -> None:
    from .models import AppAutomationSettings

    automation = AppAutomationSettings.get_for_user(user)
    automation.default_optimization_notes = optimization_notes
    automation.default_pipeline_skills_json = pipeline_skills_json
    automation.default_job_highlights = job_highlights
    automation.save(
        update_fields=[
            "default_optimization_notes",
            "default_pipeline_skills_json",
            "default_job_highlights",
            "updated_at",
        ]
    )


def _get_optimizer_supporting_context(request):
    from .models import AppAutomationSettings

    automation = AppAutomationSettings.get_for_user(request.user)
    notes = (automation.default_optimization_notes or "").strip()
    skills = (automation.default_pipeline_skills_json or "").strip()
    highlights = (automation.default_job_highlights or "").strip()

    raw = request.session.get("optimizer_supporting_context")
    if isinstance(raw, dict):
        session_notes = str(raw.get("optimization_notes") or "").strip()
        session_skills = str(raw.get("pipeline_skills_json") or "").strip()
        session_highlights = str(raw.get("job_highlights") or "").strip()
        if not notes and session_notes:
            notes = session_notes
        if not skills and session_skills:
            skills = session_skills
        if not highlights and session_highlights:
            highlights = session_highlights
        if (session_notes or session_skills or session_highlights) and (
            not (automation.default_optimization_notes or "").strip()
            and not (automation.default_pipeline_skills_json or "").strip()
            and not (automation.default_job_highlights or "").strip()
        ):
            _save_optimizer_supporting_context(request.user, notes, skills, highlights)
            request.session.pop("optimizer_supporting_context", None)
            request.session.modified = True

    return {
        "optimization_notes": notes,
        "pipeline_skills_json": skills,
        "job_highlights": highlights,
    }


def optimizer_view(request):
    """
    Resume Optimizer page backed by the existing Ninja API logic.
    - Use the LLM provider configured in Settings
    - Edit prompts
    - Upload resume + job description (or use job_id/resume_id from Match link)
    - Trigger optimization and see status
    """
    selected_provider = _get_active_llm_provider(request.user, request)

    # UserResume id (from Match / job search) — do not use for OptimizedResume PK; use opt_id for that.
    resume_id = request.GET.get("resume_id")
    # OptimizedResume id: loads status, agent logs, Word/PDF download in the status card.
    opt_id = request.GET.get("opt_id")
    job_id = request.GET.get("job_id")
    track_slug = request.GET.get("track") or request.session.get("optimizer_track")
    prefill_job_description = ""
    prefill_resume_id = None
    target_job = None

    opt_record = None
    if opt_id:
        from .models import OptimizedResume
        try:
            opt_record = OptimizedResume.objects.filter(id=int(opt_id), owner=request.user).first()
        except (ValueError, TypeError):
            opt_record = None

    if not job_id and opt_record:
        if opt_record.pipeline_entry:
            target_job = opt_record.pipeline_entry.job_listing
            job_id = str(target_job.id)
            if not track_slug:
                track_slug = opt_record.pipeline_entry.track
        else:
            session_jid = request.session.get("optimizer_job_id")
            if session_jid:
                from .models import JobListing
                try:
                    target_job = JobListing.objects.get(id=int(session_jid))
                    job_id = str(target_job.id)
                    if not track_slug:
                        track_slug = request.session.get("optimizer_track")
                except JobListing.DoesNotExist:
                    pass

            if not target_job and opt_record.job_description and opt_record.job_description.content:
                import re
                from .models import PipelineEntry
                opt_norm = re.sub(r'\s+', ' ', opt_record.job_description.content).strip().lower()
                pref_track = track_slug or request.session.get("job_search_track")
                candidate_entries = list(
                    PipelineEntry.objects.filter(owner=request.user, stage=PipelineEntry.Stage.APPLYING)
                    .select_related("job_listing")
                )
                if pref_track:
                    candidate_entries.sort(key=lambda e: 0 if e.track == pref_track else 1)
                for pe in candidate_entries:
                    jd_norm = re.sub(r'\s+', ' ', pe.job_listing.description or '').strip().lower()
                    if jd_norm and (jd_norm[:100] in opt_norm or opt_norm[:100] in jd_norm):
                        target_job = pe.job_listing
                        job_id = str(target_job.id)
                        if not track_slug:
                            track_slug = pe.track
                        opt_record.pipeline_entry = pe
                        opt_record.save(update_fields=["pipeline_entry"])
                        break

    if job_id and not target_job:
        from .models import JobListing
        try:
            target_job = JobListing.objects.get(id=int(job_id))
        except (ValueError, JobListing.DoesNotExist):
            target_job = None

    if target_job:
        from .sourcing.clients.builtin_client import enrich_builtin_job_listing_description
        from .sourcing.clients.dice_client import enrich_dice_job_listing_description
        from .sourcing.clients.greenhouse_client import enrich_greenhouse_job_listing_description
        from .sourcing.clients.levels_client import enrich_levels_job_listing_description

        job = target_job
        prefill_job_description = enrich_dice_job_listing_description(job)
        if not prefill_job_description:
            prefill_job_description = enrich_levels_job_listing_description(job)
        if not prefill_job_description:
            prefill_job_description = enrich_builtin_job_listing_description(job)
        if not prefill_job_description:
            prefill_job_description = enrich_greenhouse_job_listing_description(job)
        if not prefill_job_description:
            prefill_job_description = (job.description or "").strip()
        request.session["optimizer_prefill_job_description"] = prefill_job_description
        request.session["optimizer_job_id"] = target_job.id
        if track_slug:
            request.session["optimizer_track"] = track_slug
        request.session.modified = True

        if opt_record and not opt_record.pipeline_entry:
            from .models import PipelineEntry
            entry_qs = PipelineEntry.objects.filter(owner=request.user, job_listing=target_job)
            if track_slug:
                pe_match = entry_qs.filter(track=track_slug).first()
            else:
                pe_match = None
            pe_link = pe_match or entry_qs.first()
            if pe_link:
                opt_record.pipeline_entry = pe_link
                opt_record.save(update_fields=["pipeline_entry"])
    else:
        prefill_job_description = request.session.get("optimizer_prefill_job_description", "")
    if resume_id:
        try:
            rid = int(resume_id)
            from .models import UserResume
            if UserResume.objects.for_user(request.user).filter(is_library=True, id=rid).exists():
                prefill_resume_id = rid
                request.session["optimizer_resume_id"] = rid
                request.session.modified = True
        except (ValueError, TypeError):
            pass
    elif track_slug:
        from .models import UserResume, SearchProfile
        ur = UserResume.objects.filter(owner=request.user, track=track_slug, is_library=True).first()
        if not ur:
            sp = SearchProfile.objects.filter(owner=request.user, slug=track_slug).first()
            if sp and sp.resume:
                ur = sp.resume
        if ur:
            prefill_resume_id = ur.id
            request.session["optimizer_resume_id"] = ur.id
            request.session.modified = True
    elif request.session.get("optimizer_resume_id"):
        prefill_resume_id = request.session.get("optimizer_resume_id")

    if not prefill_resume_id:
        from .models import UserResume
        ur = UserResume.objects.filter(owner=request.user, is_library=True).order_by("-id").first()
        if ur:
            prefill_resume_id = ur.id
            request.session["optimizer_resume_id"] = ur.id
            request.session.modified = True

    # LLM models + key status
    llm_models = []
    llm_default_model = None
    llm_key_stored = False
    llm_key_error = None

    if selected_provider:
        try:
            models_data = api_llm_models(request, provider=selected_provider)
            llm_models = models_data.get("models", [])
            llm_default_model = models_data.get("default_model")
            llm_key_stored = True
        except HttpError as e:
            llm_key_error = str(e)
    else:
        llm_key_error = "No LLM provider configured. Choose one in Settings."

    # Prompts: system-wide (Prompt Library / prompts.py); read-only on optimizer.
    try:
        full_prompts = get_effective_prompts(request)
        prompts = {
            "writer": full_prompts["writer"],
            "ats_judge": full_prompts["ats_judge"],
            "recruiter_judge": full_prompts["recruiter_judge"],
        }
    except Exception:
        prompts = {"writer": "", "ats_judge": "", "recruiter_judge": ""}

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "save_draft":
            target_job_id = request.POST.get("job_id") or request.GET.get("job_id") or request.session.get("optimizer_job_id")
            track_slug = request.POST.get("track") or request.GET.get("track") or request.session.get("optimizer_track") or track_slug
            opt_id_val = request.POST.get("opt_id") or request.GET.get("opt_id") or opt_id

            from .models import OptimizedResume, PipelineEntry, SearchProfile
            opt = None
            if opt_id_val:
                try:
                    opt = OptimizedResume.objects.filter(id=int(opt_id_val), owner=request.user).first()
                except (ValueError, TypeError):
                    opt = None

            if not track_slug:
                sp = SearchProfile.objects.filter(owner=request.user, is_default=True).first() or SearchProfile.objects.filter(owner=request.user).first()
                track_slug = sp.slug if sp else "default"

            markdown_text = (request.POST.get("markdown") or "").strip()
            if not markdown_text and opt:
                markdown_text = (opt.optimized_content or "").strip()

            from .utils import sanitize_resume_markdown
            markdown_text = sanitize_resume_markdown(markdown_text)

            if opt and markdown_text:
                opt.optimized_content = markdown_text
                opt.save(update_fields=["optimized_content"])

            if target_job_id:
                try:
                    entry = PipelineEntry.objects.filter(
                        owner=request.user,
                        job_listing_id=int(target_job_id),
                        track=track_slug,
                    ).first()
                    if not entry:
                        entry = PipelineEntry.objects.filter(
                            owner=request.user,
                            job_listing_id=int(target_job_id),
                        ).first()
                    if entry:
                        if markdown_text:
                            entry.applied_resume_markdown = markdown_text
                        if opt:
                            entry.applied_optimized_resume = opt
                            if not opt.pipeline_entry:
                                opt.pipeline_entry = entry
                                opt.save(update_fields=["pipeline_entry"])
                        entry.save(update_fields=["applied_resume_markdown", "applied_optimized_resume"])
                    else:
                        from .models import JobListing
                        jl = JobListing.objects.filter(id=int(target_job_id)).first()
                        if jl:
                            pe = PipelineEntry.objects.create(
                                owner=request.user,
                                job_listing=jl,
                                track=track_slug,
                                stage=PipelineEntry.Stage.APPLYING,
                                applied_resume_markdown=markdown_text or "",
                                applied_optimized_resume=opt,
                            )
                            if opt and not opt.pipeline_entry:
                                opt.pipeline_entry = pe
                                opt.save(update_fields=["pipeline_entry"])
                except Exception as e:
                    logger.warning("Could not persist draft to pipeline entry: %s", e)

            if request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", ""):
                return JsonResponse({"ok": True, "message": "Draft resume saved."})

            messages.success(request, "Draft resume saved in Applying stage.")
            redirect_url = reverse("resume_optimizer") + f"?track={track_slug}"
            if target_job_id:
                redirect_url += f"&job_id={target_job_id}"
            if opt_id_val:
                redirect_url += f"&opt_id={opt_id_val}"
            return redirect(redirect_url)

        elif action in ("approve_and_mark_applied", "mark_applied"):
            target_job_id = request.POST.get("job_id") or request.GET.get("job_id") or request.session.get("optimizer_job_id")
            track_slug = request.POST.get("track") or request.GET.get("track") or request.session.get("optimizer_track") or track_slug
            opt_id_val = request.POST.get("opt_id") or request.GET.get("opt_id") or opt_id

            from .models import OptimizedResume, PipelineEntry, SearchProfile
            from .domain.event_bus import event_bus
            from .domain.events import JobMarkedApplied

            if not track_slug:
                sp = SearchProfile.objects.filter(owner=request.user, is_default=True).first() or SearchProfile.objects.filter(owner=request.user).first()
                track_slug = sp.slug if sp else "default"

            opt = None
            if opt_id_val:
                try:
                    opt = OptimizedResume.objects.filter(id=int(opt_id_val), owner=request.user).first()
                except (ValueError, TypeError):
                    opt = None

            if not target_job_id and opt:
                if opt.pipeline_entry:
                    target_job_id = opt.pipeline_entry.job_listing_id
                    track_slug = opt.pipeline_entry.track
                else:
                    if opt.job_description and opt.job_description.content:
                        import re
                        opt_norm = re.sub(r'\s+', ' ', opt.job_description.content).strip().lower()
                        pref_track = track_slug or request.session.get("job_search_track")
                        candidate_entries = list(
                            PipelineEntry.objects.filter(owner=request.user, stage=PipelineEntry.Stage.APPLYING)
                            .select_related("job_listing")
                        )
                        if pref_track:
                            candidate_entries.sort(key=lambda e: 0 if e.track == pref_track else 1)
                        for pe in candidate_entries:
                            jd_norm = re.sub(r'\s+', ' ', pe.job_listing.description or '').strip().lower()
                            if jd_norm and (jd_norm[:100] in opt_norm or opt_norm[:100] in jd_norm):
                                target_job_id = pe.job_listing_id
                                track_slug = pe.track
                                opt.pipeline_entry = pe
                                opt.save(update_fields=["pipeline_entry"])
                                break

            markdown_text = (request.POST.get("markdown") or "").strip()
            if not markdown_text and opt:
                markdown_text = (opt.optimized_content or "").strip()

            from .utils import sanitize_resume_markdown
            markdown_text = sanitize_resume_markdown(markdown_text)

            if opt and markdown_text and markdown_text != (opt.optimized_content or "").strip():
                opt.optimized_content = markdown_text
                opt.save(update_fields=["optimized_content"])

            if target_job_id:
                try:
                    entry = PipelineEntry.objects.filter(
                        owner=request.user,
                        job_listing_id=int(target_job_id),
                        track=track_slug,
                    ).first()
                    if not entry:
                        entry = PipelineEntry.objects.filter(
                            owner=request.user,
                            job_listing_id=int(target_job_id),
                        ).first()
                    if entry:
                        if markdown_text:
                            entry.applied_resume_markdown = markdown_text
                        if opt:
                            entry.applied_optimized_resume = opt
                            if not opt.pipeline_entry:
                                opt.pipeline_entry = entry
                                opt.save(update_fields=["pipeline_entry"])
                        entry.mark_done(save=True)
                        track_slug = entry.track
                        event_bus.publish(JobMarkedApplied(user_id=request.user.id, entry_id=entry.id, track=entry.track))
                        messages.success(request, "Opportunity marked applied & moved to Applied!")
                    else:
                        from .models import JobListing
                        jl = JobListing.objects.filter(id=int(target_job_id)).first()
                        if jl:
                            from django.utils import timezone
                            from .track_actions import record_job_liked
                            pe = PipelineEntry.objects.create(
                                owner=request.user,
                                job_listing=jl,
                                track=track_slug,
                                stage=PipelineEntry.Stage.DONE,
                                applied_at=timezone.now(),
                                applied_resume_markdown=markdown_text or "",
                                applied_optimized_resume=opt,
                            )
                            if opt and not opt.pipeline_entry:
                                opt.pipeline_entry = pe
                                opt.save(update_fields=["pipeline_entry"])
                            record_job_liked(user=request.user, job=jl, track=track_slug)
                            event_bus.publish(JobMarkedApplied(user_id=request.user.id, entry_id=pe.id, track=track_slug))
                            messages.success(request, "Opportunity marked applied & moved to Applied (tagged as Liked)!")
                except Exception as e:
                    messages.error(request, f"Could not advance pipeline stage: {e}")
            else:
                messages.error(request, "No target opportunity associated with this tailored resume.")
            return redirect(f"/jobs/cockpit/?track={track_slug}&stage=done")

        elif action in ("reset_prompts", "save_prompts"):
            messages.info(
                request,
                "Prompts are managed system-wide by admins in the Prompt library.",
            )

        elif action == "save_engine_settings":
            llm_model = (request.POST.get("llm_model") or "").strip()
            if llm_model:
                request.session["optimizer_llm_model"] = llm_model
            temp_raw = (request.POST.get("llm_temperature") or "").strip()
            if temp_raw != "":
                try:
                    t = float(temp_raw)
                    request.session["optimizer_temperature"] = str(max(0.0, min(2.0, t)))
                except ValueError:
                    pass
            request.session.modified = True
            messages.success(request, "Engine settings saved for the next run.")
            return redirect(reverse("resume_optimizer"))

        elif action == "save_supporting_context":
            notes = (request.POST.get("optimization_notes") or "").strip()
            skills = (request.POST.get("pipeline_skills_json") or "").strip()
            highlights = (request.POST.get("job_highlights") or "").strip()
            _save_optimizer_supporting_context(request.user, notes, skills, highlights)
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                from django.http import JsonResponse

                return JsonResponse({"ok": True})
            messages.success(request, "Supporting context saved.")
            return redirect(reverse("resume_optimizer") + "?wizard_step=2")

        elif action == "run_optimizer":
            from django.core.files.uploadedfile import SimpleUploadedFile
            from .models import UserResume

            resume_file = request.FILES.get("resume_file")
            use_resume_id = request.POST.get("use_resume_id")
            if not resume_file and use_resume_id:
                try:
                    ur = _user_library_resumes(request.user).get(id=int(use_resume_id))
                    with ur.file.open("rb") as fh:
                        content = fh.read()
                    resume_file = SimpleUploadedFile(
                        name=ur.original_filename or ur.file.name or "resume.pdf",
                        content=content,
                        content_type="application/pdf",
                    )
                except (ValueError, UserResume.DoesNotExist, OSError):
                    resume_file = None

            job_description = (request.POST.get("job_description") or "").strip()
            llm_model = request.POST.get("llm_model") or None
            if llm_model:
                request.session["optimizer_llm_model"] = llm_model
                request.session.modified = True
            debug = bool(request.POST.get("debug"))
            rate_limit_delay = (request.POST.get("rate_limit_delay") or "").strip()
            max_iterations = (request.POST.get("max_iterations") or "").strip()

            # Use system Prompt Library templates (no per-user persistence).
            full = get_effective_prompts(request)
            prompts = {
                "writer": None,
                "ats_judge": full.get("ats_judge", ""),
                "recruiter_judge": None,
            }

            raw_ats = (request.POST.get("ats_judge_profile_id") or "").strip()
            ats_profile_id = int(raw_ats) if raw_ats.isdigit() else None
            if ats_profile_id:
                request.session["optimizer_ats_judge_profile_id"] = ats_profile_id
                request.session.modified = True
            raw_wf = (request.POST.get("optimizer_workflow_id") or "").strip()
            optimizer_workflow_id = int(raw_wf) if raw_wf.isdigit() else None

            if not llm_key_stored:
                messages.error(request, "No API key stored for this provider. Connect an API key before running.")
            elif not resume_file:
                messages.error(request, "Please upload a PDF resume or use the resume selected from Match.")
            elif not job_description:
                messages.error(request, "Please provide a job description.")
            else:
                try:
                    score_threshold_raw = request.POST.get("score_threshold", "").strip()
                    score_threshold_val = int(score_threshold_raw) if score_threshold_raw else None
                    saved_supporting_context = _get_optimizer_supporting_context(request)
                    optimization_notes = (request.POST.get("optimization_notes") or "").strip()
                    if not optimization_notes:
                        optimization_notes = saved_supporting_context["optimization_notes"]
                    pipeline_skills_json = (request.POST.get("pipeline_skills_json") or "").strip()
                    if not pipeline_skills_json:
                        pipeline_skills_json = saved_supporting_context["pipeline_skills_json"]
                    job_highlights = (request.POST.get("job_highlights") or "").strip()
                    if not job_highlights:
                        job_highlights = saved_supporting_context["job_highlights"]

                    _save_optimizer_supporting_context(
                        request.user,
                        optimization_notes,
                        pipeline_skills_json,
                        job_highlights,
                    )

                    payload = OptimizeRequest(
                        job_description=job_description,
                        llm_provider=selected_provider,
                        llm_model=llm_model or None,
                        api_key=None,  # use stored key from LLMProviderConfig
                        prompt_writer=prompts["writer"],
                        prompt_recruiter_judge=prompts["recruiter_judge"],
                        ats_judge_profile_id=ats_profile_id,
                        optimizer_workflow_id=optimizer_workflow_id,
                        debug=debug,
                        workflow_steps=request.POST.get("workflow_steps") or None,
                        loop_to=request.POST.get("loop_to") or None,
                        score_threshold=score_threshold_val,
                        optimization_notes=optimization_notes or None,
                        pipeline_skills_json=pipeline_skills_json or None,
                        job_highlights=job_highlights or None,
                    )
                    # optimize_resume also reads rate_limit_delay and max_iterations from request.POST
                    if rate_limit_delay:
                        request.POST._mutable = True  # type: ignore[attr-defined]
                        request.POST["rate_limit_delay"] = rate_limit_delay
                        request.POST._mutable = False  # type: ignore[attr-defined]
                    if max_iterations:
                        request.POST._mutable = True  # type: ignore[attr-defined]
                        request.POST["max_iterations"] = max_iterations
                        request.POST._mutable = False  # type: ignore[attr-defined]

                    result = api_optimize_resume(request, payload=payload, file=resume_file)
                    opt_id = result.get("resume_id")
                    if opt_id:
                        target_jid = request.POST.get("job_id") or request.GET.get("job_id") or request.session.get("optimizer_job_id") or (target_job.id if target_job else None)
                        tr_slug = request.POST.get("track") or request.GET.get("track") or request.session.get("optimizer_track") or track_slug
                        if target_jid:
                            from .models import PipelineEntry, OptimizedResume
                            pe = PipelineEntry.objects.filter(owner=request.user, job_listing_id=int(target_jid))
                            if tr_slug:
                                pe_match = pe.filter(track=tr_slug).first()
                            else:
                                pe_match = None
                            entry_to_link = pe_match or pe.first()
                            if entry_to_link:
                                opt_rec = OptimizedResume.objects.filter(id=int(opt_id), owner=request.user).first()
                                if opt_rec and not opt_rec.pipeline_entry:
                                    opt_rec.pipeline_entry = entry_to_link
                                    opt_rec.save(update_fields=["pipeline_entry"])

                    messages.success(request, f"Optimization started for resume #{opt_id}.")
                    redirect_url = f"{reverse('resume_optimizer')}?opt_id={opt_id}"
                    resolved_jid = job_id or (target_job.id if target_job else None)
                    if resolved_jid:
                        redirect_url += f"&job_id={resolved_jid}"
                    if track_slug:
                        redirect_url += f"&track={track_slug}"
                    return redirect(redirect_url)
                except HttpError as e:
                    messages.error(request, str(e))
                except Exception as e:
                    messages.error(request, f"Error starting optimization: {e}")

        # Other actions fall through to re-render with updated context

    # If we have a running/completed optimization, load its status once; the frontend will poll for updates.
    status_data = None
    if opt_id:
        try:
            status_data = api_get_status_data(
                int(opt_id), get_active_user(request), request=request
            )
        except HttpError as e:
            messages.error(request, str(e))
        except Exception:
            # If there is no OptimizedResume with this id, just show the form without status.
            status_data = None

    selected_llm_model = request.session.get("optimizer_llm_model") or llm_default_model
    optimizer_supporting_context = _get_optimizer_supporting_context(request)
    job_description_value = (
        request.POST.get("job_description", "") if request.method == "POST" else prefill_job_description
    )
    optimization_notes_value = (
        request.POST.get("optimization_notes", "") if request.method == "POST" else optimizer_supporting_context["optimization_notes"]
    )
    pipeline_skills_json_value = (
        request.POST.get("pipeline_skills_json", "") if request.method == "POST" else optimizer_supporting_context["pipeline_skills_json"]
    )
    job_highlights_value = (
        request.POST.get("job_highlights", "") if request.method == "POST" else optimizer_supporting_context["job_highlights"]
    )
    prefill_resume_name = None
    if prefill_resume_id:
        try:
            from .models import UserResume
            ur = _user_library_resumes(request.user).filter(id=prefill_resume_id).first()
            prefill_resume_name = (ur.original_filename or ur.file.name or f"#{prefill_resume_id}") if ur else None
        except Exception:
            pass
    from .prompt_store import (
        get_ats_judge_profile_display,
        list_ats_judge_profiles,
        list_optimizer_workflows,
    )

    saved_workflows = list_optimizer_workflows(request.user)
    for w in saved_workflows:
        w.steps_json = json.dumps(w.steps)
    ats_profiles = list_ats_judge_profiles()
    for p in ats_profiles:
        p.preview_text = get_ats_judge_profile_display(p).get("ats_judge", "")[:200]
    global_ats_ids = {p.pk for p in ats_profiles}
    selected_ats_profile_id = request.session.get("optimizer_ats_judge_profile_id")
    if selected_ats_profile_id is not None:
        try:
            selected_ats_profile_id = int(selected_ats_profile_id)
        except (ValueError, TypeError):
            selected_ats_profile_id = None
    if selected_ats_profile_id not in global_ats_ids:
        selected_ats_profile_id = None
    if not selected_ats_profile_id and ats_profiles:
        default_ats = next((p for p in ats_profiles if p.is_default), None) or ats_profiles[0]
        selected_ats_profile_id = default_ats.pk
    optimized_resume_id = None
    if opt_id:
        try:
            optimized_resume_id = int(opt_id)
        except (ValueError, TypeError):
            pass
    wizard_initial_step = 1
    if optimized_resume_id or (status_data and status_data.get("status")):
        wizard_initial_step = 3
    else:
        wsp = (request.GET.get("wizard_step") or "").strip()
        if wsp:
            try:
                n = int(wsp)
                if 1 <= n <= 3:
                    wizard_initial_step = n
            except ValueError:
                pass
        elif job_id:
            wizard_initial_step = 2
    from .agents import can_view_optimizer_llm_debug
    from .experience import is_power_user

    if not is_power_user(request.user) and wizard_initial_step == 1 and not (opt_id or (status_data and status_data.get("status"))):
        wsp = (request.GET.get("wizard_step") or "").strip()
        if wsp != "1":
            wizard_initial_step = 2
    recent_resumes = list(
        _user_library_resumes(request.user).order_by("-uploaded_at")[:5]
    )
    show_agent_thoughts = can_view_optimizer_llm_debug(request)

    from .models import SearchProfile, PipelineEntry
    default_sp = SearchProfile.objects.filter(owner=request.user, is_default=True).first() or SearchProfile.objects.filter(owner=request.user).first()
    default_profile_slug = default_sp.slug if default_sp else "default"
    resolved_track = track_slug or default_profile_slug

    target_pipeline_stage = "applying"
    if target_job:
        entry_for_stage = PipelineEntry.objects.filter(owner=request.user, job_listing=target_job)
        if track_slug:
            entry_for_stage = entry_for_stage.filter(track=track_slug).first() or entry_for_stage.first()
        else:
            entry_for_stage = entry_for_stage.first()
        if entry_for_stage:
            target_pipeline_stage = entry_for_stage.stage
            if not track_slug and entry_for_stage.track:
                resolved_track = entry_for_stage.track

    context = {
        "is_power_user": is_power_user(request.user),
        "show_agent_thoughts": show_agent_thoughts,
        "selected_provider": selected_provider,
        "llm_models": llm_models,
        "llm_default_model": llm_default_model,
        "selected_llm_model": selected_llm_model,
        "llm_key_stored": llm_key_stored,
        "llm_key_error": llm_key_error,
        "prompts": prompts,
        "resume_id": resume_id,
        "opt_id": opt_id,
        "optimized_resume_id": optimized_resume_id,
        "status": status_data,
        "prefill_job_description": prefill_job_description,
        "prefill_resume_id": prefill_resume_id,
        "prefill_resume_name": prefill_resume_name,
        "recent_resumes": recent_resumes,
        "job_description_value": job_description_value,
        "optimization_notes_value": optimization_notes_value,
        "pipeline_skills_json_value": pipeline_skills_json_value,
        "job_highlights_value": job_highlights_value,
        "saved_workflows": saved_workflows,
        "ats_profiles": ats_profiles,
        "selected_ats_profile_id": selected_ats_profile_id,
        "optimizer_temperature": request.session.get("optimizer_temperature", "0.7"),
        "wizard_initial_step": wizard_initial_step,
        "target_job": target_job,
        "pipeline_track": resolved_track,
        "target_pipeline_stage": target_pipeline_stage,
    }
    return render(request, "resume_app/optimizer.html", context)


from .settings_views import settings_view


def llm_test_view(request):
    """
    Manual test page for LLM connectivity + response rendering.

    Uses stored `LLMProviderConfig` keys where possible (including Ollama Local host/IP).
    """
    from .models import LLMProviderConfig, AppAutomationSettings
    from .crypto import decrypt_api_key
    from .llm import get_llm
    from langchain_core.messages import HumanMessage
    from .llm.services import list_models_for_provider, DEFAULT_MODELS

    stop_llm = AppAutomationSettings.get_for_user(request.user).stop_llm_requests

    provider = (request.GET.get("provider") or "").strip()
    if not provider:
        provider = request.session.get("active_llm_provider") or _get_active_llm_provider(request.user, request) or ""
    if not provider:
        provider = (
            "Ollama Local"
            if LLMProviderConfig.objects.for_user(request.user).filter(provider="Ollama Local").exists()
            else next(iter(LLM_PROVIDERS))
        )

    if provider not in LLM_PROVIDERS:
        provider = next(iter(LLM_PROVIDERS))

    config = LLMProviderConfig.objects.for_user(request.user).filter(provider=provider).first()
    api_key_decrypted = ""
    models = []
    error = None
    selected_model = (request.GET.get("model") or "").strip() if hasattr(request, "GET") else ""
    if config and config.encrypted_api_key:
        api_key_decrypted = decrypt_api_key(config.encrypted_api_key)
        try:
            models = list_models_for_provider(provider, api_key_decrypted)
        except Exception as e:
            error = str(e)
            models = []
    if not selected_model:
        selected_model = (config.default_model or "").strip() if config else ""
    if not selected_model:
        selected_model = DEFAULT_MODELS.get(provider) or (models[0] if models else "")

    if request.method == "POST":
        provider = (request.POST.get("provider") or provider).strip()
        prompt = (request.POST.get("prompt") or "").strip()
        model = (request.POST.get("model") or selected_model).strip()
        if provider not in LLM_PROVIDERS:
            error = "Invalid provider."
        elif not prompt:
            error = "Prompt is required."
        else:
            config = LLMProviderConfig.objects.for_user(request.user).filter(provider=provider).first()
            if not config or not config.encrypted_api_key:
                error = f"No stored connection for {provider}. Go to Settings and connect first."
            else:
                api_key_decrypted = decrypt_api_key(config.encrypted_api_key)
                try:
                    if provider == "Ollama Local":
                        from .llm.factory import _normalize_ollama_local_host

                        try:
                            base = _normalize_ollama_local_host(api_key_decrypted)
                            logger.info(
                                "[llm_test] Ollama Local start base_url=%s model=%r prompt_chars=%s",
                                base,
                                model,
                                len(prompt),
                            )
                        except Exception as ex:
                            logger.warning(
                                "[llm_test] Ollama Local host normalize failed (raw len=%s): %s",
                                len(api_key_decrypted or ""),
                                ex,
                            )
                    llm = get_llm(provider, api_key_decrypted, model=model or None)
                    from .llm import log_llm_invoke

                    log_llm_invoke(
                        provider,
                        model or getattr(llm, "_resume_model", None),
                        query="llm_test",
                        via="direct",
                    )
                    resp = llm.invoke([HumanMessage(content=prompt)])
                    # LangChain chat models typically expose `content`
                    response_text = getattr(resp, "content", None) or str(resp)
                    if provider == "Ollama Local":
                        logger.info(
                            "[llm_test] Ollama Local done model=%r response_chars=%s",
                            model,
                            len(response_text or ""),
                        )
                    return render(
                        request,
                        "resume_app/llm_test.html",
                        {
                            "provider": provider,
                            "providers": sorted(LLM_PROVIDERS),
                            "models": models,
                            "selected_model": model,
                            "prompt": prompt,
                            "response_text": response_text,
                            "error": None,
                            "stop_llm_requests": stop_llm,
                        },
                    )
                except Exception as e:
                    if provider == "Ollama Local":
                        logger.exception(
                            "[llm_test] Ollama Local invoke failed model=%r: %s",
                            model,
                            e,
                        )
                    error = str(e)

    return render(
        request,
        "resume_app/llm_test.html",
        {
            "provider": provider,
            "providers": sorted(LLM_PROVIDERS),
            "models": models,
            "selected_model": selected_model,
            "prompt": "",
            "response_text": None,
            "error": error,
            "stop_llm_requests": stop_llm,
        },
    )


from .prompt_studio_views import (
    prompt_library_view,
    prompt_studio_save_api,
    prompt_studio_set_model_preference_api,
    prompt_studio_test_run_api,
    prompt_studio_history_api,
    prompt_studio_restore_api,
    prompt_studio_delete_version_api,
    prompt_studio_clear_history_api,
    prompt_studio_resume_content_api,
    prompt_studio_job_content_api,
)


# Step ids for workflow builder (must match agents.VALID_STEP_IDS)
WORKFLOW_STEP_IDS = ["writer", "resume_judge", "ats_judge", "recruiter_judge", "jd_cleanse"]
WORKFLOW_STEP_LABELS = {
    "writer": "Writer",
    "resume_judge": "Unified Judge (ATS + Recruiter)",
    "ats_judge": "ATS Judge (Legacy)",
    "recruiter_judge": "Recruiter Judge (Legacy)",
    "jd_cleanse": "JD Cleanse",
}


@_staff_required
def workflow_list_view(request):
    """List system-wide workflows; staff create/edit for all subscribers."""
    from .prompt_store import list_optimizer_workflows

    workflows = list_optimizer_workflows(request.user)
    for w in workflows:
        w.step_labels_display = [WORKFLOW_STEP_LABELS.get(s, s) for s in w.steps]
    return render(
        request,
        "resume_app/workflow_list.html",
        {"workflows": workflows},
    )


def _workflow_form_context(request, workflow, steps_json: str) -> dict:
    from .prompt_store import list_ats_judge_profiles
    from .llm import LLM_PROVIDERS, DEFAULT_MODELS

    return {
        "workflow": workflow,
        "workflow_steps_json": steps_json,
        "ats_profiles": list_ats_judge_profiles(),
        "step_llm_config_json": json.dumps(workflow.step_llm_config) if workflow else "{}",
        "llm_providers": sorted(LLM_PROVIDERS),
        "llm_default_models_json": json.dumps(DEFAULT_MODELS),
    }


@_staff_required
def workflow_create_view(request):
    """Create a new system-wide workflow. POST: validate and save then redirect to list."""
    from .models import AtsJudgeProfile, OptimizerWorkflow
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Name is required.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, None, "[]"))
        steps_raw = request.POST.get("workflow_steps", "")
        try:
            steps = json.loads(steps_raw) if steps_raw else []
        except json.JSONDecodeError:
            messages.error(request, "Invalid steps format.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, None, "[]"))
        invalid = [s for s in steps if s not in WORKFLOW_STEP_IDS]
        if invalid or not steps:
            messages.error(request, "Steps must be a non-empty list of: Writer, ATS Judge, Recruiter Judge, JD Cleanse.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, None, "[]"))
        loop_to = (request.POST.get("loop_to") or "").strip()
        if loop_to and loop_to not in WORKFLOW_STEP_IDS:
            loop_to = ""
        try:
            max_iterations = max(1, min(5, int(request.POST.get("max_iterations") or 3)))
        except (TypeError, ValueError):
            max_iterations = 3
        try:
            score_threshold = max(0, min(100, int(request.POST.get("score_threshold") or 85)))
        except (TypeError, ValueError):
            score_threshold = 85
        step_llm_raw = request.POST.get("step_llm_config", "")
        try:
            step_llm_config = json.loads(step_llm_raw) if step_llm_raw else {}
        except json.JSONDecodeError:
            step_llm_config = {}
        ats_prof = None
        raw_ats = (request.POST.get("ats_judge_profile_id") or "").strip()
        if raw_ats.isdigit():
            ats_prof = AtsJudgeProfile.objects.filter(owner__isnull=True, pk=int(raw_ats)).first()
        OptimizerWorkflow.objects.create(
            owner=None,
            name=name,
            steps=steps,
            step_llm_config=step_llm_config,
            loop_to=loop_to,
            max_iterations=max_iterations,
            score_threshold=score_threshold,
            ats_judge_profile=ats_prof,
        )
        messages.success(request, f"Workflow \"{name}\" created.")
        return redirect(reverse("workflow_list"))
    return render(
        request,
        "resume_app/workflow_form.html",
        _workflow_form_context(request, None, "[]"),
    )


@_staff_required
def workflow_edit_view(request, workflow_id):
    """Edit an existing system-wide workflow. POST: validate and save then redirect to list."""
    from .models import AtsJudgeProfile, OptimizerWorkflow
    from .prompt_store import get_optimizer_workflow_by_id

    workflow = get_optimizer_workflow_by_id(workflow_id, request.user)
    if workflow is None:
        from django.http import Http404

        raise Http404("Workflow not found.")
    steps_json = json.dumps(workflow.steps)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Name is required.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, workflow, steps_json))
        steps_raw = request.POST.get("workflow_steps", "")
        try:
            steps = json.loads(steps_raw) if steps_raw else []
        except json.JSONDecodeError:
            messages.error(request, "Invalid steps format.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, workflow, steps_json))
        invalid = [s for s in steps if s not in WORKFLOW_STEP_IDS]
        if invalid or not steps:
            messages.error(request, "Steps must be a non-empty list of: Writer, ATS Judge, Recruiter Judge, JD Cleanse.")
            return render(request, "resume_app/workflow_form.html", _workflow_form_context(request, workflow, steps_json))
        loop_to = (request.POST.get("loop_to") or "").strip()
        if loop_to and loop_to not in WORKFLOW_STEP_IDS:
            loop_to = ""
        try:
            max_iterations = max(1, min(5, int(request.POST.get("max_iterations") or 3)))
        except (TypeError, ValueError):
            max_iterations = 3
        try:
            score_threshold = max(0, min(100, int(request.POST.get("score_threshold") or 85)))
        except (TypeError, ValueError):
            score_threshold = 85
        step_llm_raw = request.POST.get("step_llm_config", "")
        try:
            step_llm_config = json.loads(step_llm_raw) if step_llm_raw else {}
        except json.JSONDecodeError:
            step_llm_config = {}
        raw_ats = (request.POST.get("ats_judge_profile_id") or "").strip()
        if raw_ats.isdigit():
            ats_prof = AtsJudgeProfile.objects.filter(owner__isnull=True, pk=int(raw_ats)).first()
        else:
            ats_prof = None
        workflow.name = name
        workflow.steps = steps
        workflow.step_llm_config = step_llm_config
        workflow.loop_to = loop_to
        workflow.max_iterations = max_iterations
        workflow.score_threshold = score_threshold
        workflow.ats_judge_profile = ats_prof
        workflow.save()
        messages.success(request, f"Workflow \"{name}\" updated.")
        return redirect(reverse("workflow_list"))
    return render(
        request,
        "resume_app/workflow_form.html",
        _workflow_form_context(request, workflow, steps_json),
    )


@_staff_required
def workflow_delete_view(request, workflow_id):
    """POST: delete system-wide workflow and redirect to list."""
    from .prompt_store import get_optimizer_workflow_by_id

    if request.method == "POST":
        workflow = get_optimizer_workflow_by_id(workflow_id, request.user)
        if workflow is not None:
            name = workflow.name
            workflow.delete()
            messages.success(request, f"Workflow \"{name}\" deleted.")
    return redirect(reverse("workflow_list"))


def job_search_view(request):
    """
    Job search page.
    - Search jobs for a term/location
    - Save/like/dislike jobs and run fit checks
    - View favourites and match history
    """
    if request.method == "POST":
        action = request.POST.get("action")
        job_id = request.POST.get("job_id")
        next_url = request.POST.get("next") or reverse("jobs_search")
        from .saved_searches import (
            build_jobs_search_url,
            create_or_update_saved_search,
            delete_saved_search,
            parse_saved_search_from_request,
        )
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or "")
        )
        if action == "save_saved_search":
            try:
                data = parse_saved_search_from_request(request)
                saved = create_or_update_saved_search(
                    request.user,
                    name=data["name"],
                    search_term=data["search_term"],
                    location=data["location"],
                    profile_slug=data["profile_slug"],
                    resume_id=data["resume_id"],
                    min_score=data["min_score"],
                    results_wanted=data["results_wanted"],
                    site_names=data["site_names"],
                    llm_model=data["llm_model"],
                    preset_id=data["preset_id"],
                )
                was_update = bool(data["preset_id"])
                msg = (
                    f'Updated saved search "{saved.name}".'
                    if was_update
                    else f'Saved search "{saved.name}".'
                )
                from .experience import mark_onboarding_step

                mark_onboarding_step(request.user, "search")
                redirect_url = build_jobs_search_url(
                    query=saved.search_term,
                    location=saved.location,
                    profile_slug=saved.profile_slug,
                    resume_id=saved.resume_id,
                    min_score=saved.min_score,
                    results_wanted=saved.results_wanted,
                    site_names=saved.site_names if isinstance(saved.site_names, list) else None,
                    llm_model=saved.llm_model,
                    preset_id=saved.id,
                    sort=(request.POST.get("sort") or request.GET.get("sort") or ""),
                    from_save=True,
                )
                if wants_json:
                    return JsonResponse(
                        {
                            "success": True,
                            "message": msg,
                            "created": not was_update,
                            "preset": {
                                "id": saved.id,
                                "name": saved.name,
                                "url": redirect_url,
                            },
                        }
                    )
                messages.success(request, msg)
                return redirect(redirect_url)
            except ValueError as e:
                if wants_json:
                    return JsonResponse({"success": False, "detail": str(e)}, status=400)
                messages.error(request, str(e))
                return redirect(next_url)
            except Exception as e:
                if wants_json:
                    return JsonResponse(
                        {"success": False, "detail": f"Error saving search: {e}"},
                        status=500,
                    )
                messages.error(request, f"Error performing action: {e}")
                return redirect(next_url)

        try:
            if action == "delete_saved_search":
                preset_id_raw = (request.POST.get("preset_id") or "").strip()
                try:
                    delete_saved_search(request.user, int(preset_id_raw))
                    messages.success(request, "Saved search deleted.")
                except (TypeError, ValueError) as e:
                    messages.error(request, str(e) if str(e) else "Invalid saved search.")
                return redirect(reverse("jobs_search"))
            elif action == "schedule_saved_search":
                from .saved_search_schedule import (
                    schedule_display_label,
                    set_saved_search_schedule,
                )

                preset_id_raw = (request.POST.get("preset_id") or "").strip()
                interval = (request.POST.get("schedule_interval") or "off").strip().lower()
                time_str = (request.POST.get("schedule_time") or "").strip()
                try:
                    preset_id = int(preset_id_raw)
                except (TypeError, ValueError):
                    messages.error(request, "Invalid saved search.")
                    return redirect(reverse("jobs_search"))
                try:
                    task = set_saved_search_schedule(
                        request.user,
                        preset_id,
                        interval=interval,
                        time_str=time_str,
                    )
                    if interval == "off":
                        messages.success(request, "Automatic runs turned off for this saved search.")
                    elif task:
                        label = schedule_display_label(interval, time_str or "09:00")
                        messages.success(
                            request,
                            f"Scheduled: {label}. Next run: {task.next_run_at:%b %d, %Y at %I:%M %p}.",
                        )
                except ValueError as e:
                    messages.error(request, str(e))
                return redirect(reverse("jobs_search") + f"?preset={preset_id}")
            elif action == "add_disqualifiers":
                from .models import UserDisqualifier

                raw_phrases = request.POST.getlist("phrases")
                custom = (request.POST.get("phrase") or "").strip()
                if custom:
                    raw_phrases.append(custom)
                added = 0
                for p in raw_phrases:
                    p = (p or "").strip()
                    if not p or len(p) < 2:
                        continue
                    norm = " ".join(p.lower().split())
                    if not norm:
                        continue
                    _, created = UserDisqualifier.objects.get_or_create(owner=request.user, phrase=norm)
                    if created:
                        added += 1
                if added:
                    messages.success(request, f"Added {added} disqualifier(s). Future jobs containing these phrases will be hidden.")
                else:
                    messages.info(request, "No new phrases added (empty or already in list).")
            elif action == "remove_disqualifier":
                from .models import UserDisqualifier
                dq_id = request.POST.get("disqualifier_id")
                if dq_id:
                    try:
                        UserDisqualifier.objects.for_user(request.user).filter(id=int(dq_id)).delete()
                        messages.success(request, "Disqualifier removed.")
                    except ValueError:
                        pass
            elif action in {"like", "dislike", "hide", "unhide", "save", "unsave", "mark_applied"} and not job_id:
                messages.error(request, "Missing job id for action.")
            elif action == "like":
                api_jobs_like(request, job_listing_id=int(job_id))
                from .experience import mark_onboarding_step

                mark_onboarding_step(request.user, "search")
                messages.success(request, "Job liked.")
            elif action == "dislike":
                api_jobs_dislike(request, job_listing_id=int(job_id))
                sep = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{sep}disqualifier_job_id={job_id}"
                messages.success(request, "Job disliked. It will stay out of results and refine Match Scoring.")
            elif action == "hide":
                api_jobs_hide(request, job_listing_id=int(job_id))
                messages.success(request, "Job hidden from future searches.")
            elif action == "unhide":
                api_jobs_unhide(request, job_listing_id=int(job_id))
                messages.success(request, "Job restored to search results.")
            elif action == "save":
                api_jobs_save(request, job_listing_id=int(job_id))
                messages.success(request, "Job saved to Review on My jobs.")
            elif action == "unsave":
                api_jobs_unsave(request, job_listing_id=int(job_id))
                messages.success(request, "Job removed from favourites and My jobs.")
            elif action == "mark_applied":
                resume_id_val = request.POST.get("resume_id")
                if not resume_id_val:
                    messages.error(request, "Missing resume id for marking as applied.")
                else:
                    payload = MarkAppliedRequest(resume_id=int(resume_id_val))
                    api_jobs_mark_applied(request, job_listing_id=int(job_id), payload=payload)
                    messages.success(request, "Marked as applied.")
        except ValueError as e:
            messages.error(request, str(e))
        except HttpError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Error performing action: {e}")
        return redirect(next_url)

    # GET: render search page
    from .saved_searches import (
        apply_saved_search_to_get,
        get_saved_search_or_none,
        list_saved_searches,
    )
    from .saved_search_schedule import (
        build_saved_search_schedule_map,
        get_schedule_form_defaults,
    )

    view_mode = (request.GET.get("view") or "results").lower()
    show_favourites = view_mode == "favourites"
    show_excluded = view_mode == "excluded"

    saved_searches_list = list_saved_searches(request.user)
    selected_preset_id = None
    saved_preset = get_saved_search_or_none(request.user, request.GET.get("preset"))
    applied_preset = apply_saved_search_to_get(saved_preset) if saved_preset else {}
    if saved_preset:
        selected_preset_id = saved_preset.id

    if "q" in request.GET:
        query = (request.GET.get("q") or "").strip()
    else:
        query = applied_preset.get("query", "")

    if "location" in request.GET:
        location = (request.GET.get("location") or "").strip()
    else:
        location = applied_preset.get("location", "")

    if "site_name" in request.GET:
        selected_site_names = normalize_site_names(request.GET.getlist("site_name") or None)
    elif applied_preset:
        selected_site_names = applied_preset.get("selected_site_names") or normalize_site_names(None)
    else:
        selected_site_names = normalize_site_names(request.GET.getlist("site_name") or None)

    if "resume_id" in request.GET:
        resume_id_raw = (request.GET.get("resume_id") or "").strip()
    elif applied_preset:
        resume_id_raw = str(applied_preset.get("resume_id_val") or "")
    else:
        resume_id_raw = (request.GET.get("resume_id") or "").strip()
    # Treat blank or explicit "None" as no resume selected
    if not resume_id_raw or resume_id_raw.lower() == "none":
        resume_id_val = None
    else:
        try:
            resume_id_val = int(resume_id_raw)
        except ValueError:
            resume_id_val = None
    if "min_score" in request.GET:
        min_score_raw = (request.GET.get("min_score") or "").strip()
    elif applied_preset:
        min_score_raw = applied_preset.get("min_score_raw", "")
    else:
        min_score_raw = (request.GET.get("min_score") or "").strip()

    if "results_wanted" in request.GET:
        results_wanted_raw = (request.GET.get("results_wanted") or "").strip()
    elif applied_preset:
        results_wanted_raw = str(applied_preset.get("results_wanted_val", 50))
    else:
        results_wanted_raw = (request.GET.get("results_wanted") or "").strip()
    try:
        results_wanted_val = int(results_wanted_raw) if results_wanted_raw else 50
        results_wanted_val = max(10, min(200, results_wanted_val))
    except ValueError:
        results_wanted_val = 50

    resumes = []
    try:
        resumes = api_jobs_list_resumes(request)
    except Exception as e:
        messages.error(request, f"Could not load resumes: {e}")

    # LLM model for Matching step: use active provider from Settings
    job_search_llm_provider = None
    job_search_llm_models = []
    job_search_llm_default_model = None
    job_search_llm_model = None
    try:
        active_provider = _get_active_llm_provider(request.user, request)
        if active_provider:
            job_search_llm_provider = active_provider
            models_data = api_llm_models(request, provider=active_provider)
            job_search_llm_models = models_data.get("models", [])
            job_search_llm_default_model = models_data.get("default_model")
            job_search_llm_model = (
                request.GET.get("llm_model")
                or (applied_preset.get("llm_model") if applied_preset and "llm_model" not in request.GET else None)
                or request.session.get("job_search_llm_model")
                or job_search_llm_default_model
            )
            if job_search_llm_model and job_search_llm_model not in job_search_llm_models:
                job_search_llm_model = job_search_llm_default_model
            if request.GET.get("llm_model"):
                target_model = job_search_llm_model or ""
                if request.session.get("job_search_llm_model") != target_model:
                    request.session["job_search_llm_model"] = target_model
    except Exception:
        pass

    search_results = None
    saved_results = None
    excluded_results = None
    matches = None
    disqualifier_prompt = None
    current_disqualifiers = []

    # Track / profile: dynamic list (e.g. IC vs Management vs custom).
    from .models import SearchProfile

    profiles_qs = SearchProfile.ensure_baseline(request.user)
    tracks_qs = Track.ensure_baseline(request.user)
    profile_labels_map = {p.slug: p.name for p in profiles_qs}
    for t in tracks_qs:
        if t.slug not in profile_labels_map:
            profile_labels_map[t.slug] = t.label
    available_slugs = set(profile_labels_map.keys())
    job_tracks = [
        {"slug": s, "label": l}
        for s, l in sorted(profile_labels_map.items(), key=lambda x: x[1].lower())
    ]
    raw_track_param = (
        (request.GET.get("profile") or request.GET.get("track") or "").strip().lower()
    )
    if raw_track_param:
        if raw_track_param in ("none", "unbiased", "none (unbiased)", "none (unbiased / no profile)"):
            raw_track = ""
        elif raw_track_param in available_slugs:
            raw_track = raw_track_param
        else:
            raw_track = ""
    elif applied_preset.get("profile_slug"):
        preset_slug = applied_preset["profile_slug"].strip().lower()
        raw_track = preset_slug if preset_slug in available_slugs else ""
    else:
        # Default in sandbox is None (unbiased / no profile context)
        raw_track = ""

    if request.session.get("job_search_track") != raw_track:
        request.session["job_search_track"] = raw_track
    if request.session.get("job_search_profile_slug") != raw_track:
        request.session["job_search_profile_slug"] = raw_track

    if saved_preset:
        from .saved_searches import saved_search_matches_params

        preset_is_dirty = not saved_search_matches_params(
            saved_preset,
            search_term=query,
            location=location,
            profile_slug=raw_track,
            resume_id=resume_id_val,
            min_score_raw=min_score_raw,
            results_wanted=results_wanted_val,
            site_names=selected_site_names,
            llm_model=job_search_llm_model or "",
        )
    else:
        preset_is_dirty = False

    raw_sort = (request.GET.get("sort") or request.GET.get("sort_by") or "match").strip().lower()
    if raw_sort in ("latest", "freshness", "date", "newest"):
        sort_param = "latest"
    elif raw_sort == "oldest":
        sort_param = "oldest"
    else:
        sort_param = "match"

    try:
        if show_favourites:
            saved_results = api_jobs_saved(request)
        elif show_excluded:
            excluded_results = api_jobs_disliked(request)
        elif query and request.GET.get("from_save"):
            # After saving a preset, keep current listings — do not re-hit job boards.
            cached_display = request.session.get("job_search_display")
            if isinstance(cached_display, dict) and cached_display.get("jobs") is not None:
                from .job_search_core import sort_job_payloads
                cached_jobs = sort_job_payloads(
                    rehydrate_job_payloads(cached_display.get("jobs") or []),
                    sort_by=sort_param,
                )
                search_results = {
                    "jobs": cached_jobs,
                    "total": cached_display.get("total") or len(cached_jobs),
                }
            else:
                search_results = None
        elif query:
            if request.GET.get("refresh"):
                request.session.pop("job_search_cache", None)
            payload = JobSearchRequest(
                search_term=query,
                location=location or None,
                site_name=selected_site_names,
                results_wanted=results_wanted_val,
                resume_id=resume_id_val,
                sort=sort_param,
                llm_provider=job_search_llm_provider,
                llm_model=job_search_llm_model or None,
                track=raw_track,
            )
            search_results = api_jobs_search(request, payload=payload)
            jobs_list = getattr(search_results, "jobs", None) or []
            search_results = {
                "jobs": jobs_list,
                "total": getattr(search_results, "total", 0) or len(jobs_list),
            }
            # Snapshot for post-save reloads so we can skip board re-fetch.
            try:
                serializable_jobs = []
                for job in jobs_list:
                    if hasattr(job, "model_dump"):
                        raw = job.model_dump(mode="json")
                    elif hasattr(job, "dict"):
                        raw = job.dict()
                    elif isinstance(job, dict):
                        raw = job
                    else:
                        continue
                    serializable_jobs.append(raw)
                request.session["job_search_display"] = {
                    "jobs": serializable_jobs,
                    "total": search_results["total"],
                }
                request.session.modified = True
            except Exception:
                pass
            if jobs_list:
                from .experience import mark_onboarding_step

                mark_onboarding_step(request.user, "search")
        # Empty q: do not replay session-cached results; user must submit a search.
    except HttpError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"Error fetching jobs: {e}")

    try:
        min_score = int(min_score_raw) if min_score_raw else None
    except ValueError:
        min_score = None
    try:
        matches = api_jobs_matches(
            request,
            resume_id=resume_id_val,
            min_score=min_score,
            status=None,
        )
    except HttpError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"Error loading matches: {e}")

    # Disqualifier prompt after a dislike: add phrases to avoid
    from .models import JobListing, UserDisqualifier

    disqualifier_job_id = request.GET.get("disqualifier_job_id")
    if disqualifier_job_id:
        try:
            job = JobListing.objects.get(id=int(disqualifier_job_id))
            disqualifier_prompt = {
                "job_id": job.id,
                "title": job.title,
                "company_name": job.company_name,
                "description": (job.description or "")[:3000],
            }
        except Exception:
            disqualifier_prompt = None
    current_disqualifiers = [{"id": d.id, "phrase": d.phrase} for d in UserDisqualifier.objects.for_user(request.user).order_by("phrase")]
    preserved_site_query = urlencode([("site_name", s) for s in selected_site_names])
    single_search_profile = len(tracks_qs) <= 1
    default_track = Track.get_default_slug(request.user)
    job_search_options_open = _job_search_options_open(
        request.user,
        min_score_raw=min_score_raw,
        resume_id_val=resume_id_val,
        results_wanted_val=results_wanted_val,
        selected_site_names=selected_site_names,
        raw_track=raw_track,
        default_track=default_track,
        single_search_profile=single_search_profile,
        selected_preset_id=selected_preset_id,
        llm_model_in_get="llm_model" in request.GET,
    )
    saved_search_schedule_map = build_saved_search_schedule_map(request.user)
    from .experience import find_jobs_pulse_stats

    context = {
        "resumes": resumes,
        "query": query,
        "location": location,
        "selected_resume_id": resume_id_val,
        "show_favourites": show_favourites,
        "show_excluded": show_excluded,
        "view_mode": view_mode,
        "search_results": search_results,
        "saved_results": saved_results,
        "excluded_results": excluded_results,
        "matches": matches,
        "min_score": min_score_raw or "",
        "results_wanted": results_wanted_val,
        "disqualifier_prompt": disqualifier_prompt,
        "current_disqualifiers": current_disqualifiers,
        "sort_param": sort_param,
        "job_search_llm_provider": job_search_llm_provider,
        "job_search_llm_models": job_search_llm_models,
        "job_search_llm_default_model": job_search_llm_default_model,
        "job_search_llm_model": job_search_llm_model,
        "job_search_track": raw_track,
        "job_search_track_label": (
            profile_labels_map.get(raw_track, raw_track)
            if raw_track
            else "None (Unbiased)"
        ),
        "job_tracks": job_tracks,
        "site_options": list(ALLOWED_SITE_NAMES),
        "selected_site_names": selected_site_names,
        "preserved_site_query": preserved_site_query,
        "saved_searches": saved_searches_list,
        "saved_search_entries": [
            {"search": s, "schedule": saved_search_schedule_map.get(s.id)}
            for s in saved_searches_list
        ],
        "selected_preset_id": selected_preset_id,
        "selected_preset": saved_preset,
        "preset_is_dirty": preset_is_dirty,
        "selected_preset_schedule": get_schedule_form_defaults(request.user, saved_preset),
        "single_search_profile": single_search_profile,
        "job_search_options_open": job_search_options_open,
        "find_jobs_pulse": find_jobs_pulse_stats(request.user),
    }
    return render(request, "resume_app/jobs_search.html", context)


from .huey_views import (
    job_tasks_view,
    huey_dashboard_view,
    huey_periodic_revoke_view,
    huey_periodic_restore_view,
    huey_flush_queue_view,
    huey_run_cleanup_now_view,
    huey_task_run_now_view,
    job_task_create_view,
    job_task_edit_view,
    job_task_run_now_view,
    job_task_status_view,
    job_task_toggle_active_view,
)


MAX_TRACK_RESUME_UPLOAD_BYTES = 10 * 1024 * 1024


def _format_resume_file_size(size_bytes: int | None) -> str:
    """Human-readable size for library resume rows."""
    if size_bytes is None or size_bytes < 0:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _track_list_sort_param(request) -> str:
    sort = (request.GET.get("sort") or "label").strip().lower()
    return sort if sort in ("label", "slug") else "label"


def _count_unique_library_resumes(user) -> int:
    """
    Count distinct library PDFs by normalized filename (one row per file name).

    Duplicate uploads of the same filename count once; rows with no filename
    are keyed by id so they are not collapsed together.
    """
    seen: set[str] = set()
    unique = 0
    for row in _user_library_resumes(user).only("id", "original_filename"):
        name = (row.original_filename or "").strip().lower()
        key = name if name else f"__id_{row.id}"
        if key in seen:
            continue
        seen.add(key)
        unique += 1
    return unique


def _track_list_context(request, user, tracks_qs):
    """Build template context for the Track Management page."""
    from django.db.models import Count
    from .models import UserResume
    from .experience import is_power_user

    power_user = is_power_user(user)
    sort = _track_list_sort_param(request)
    if power_user:
        order_field = "label" if sort == "label" else "slug"
        tracks = list(tracks_qs.order_by(order_field))
        default_track_slug = Track.get_default_slug(user)
    else:
        order_field = "name" if sort == "label" else "slug"
        tracks = []
        for p in tracks_qs.order_by(order_field):
            p.label = p.name
            tracks.append(p)
        default_track_slug = ""
        for t in tracks:
            if t.is_default:
                default_track_slug = t.slug
                break
        if not default_track_slug and tracks:
            default_track_slug = tracks[0].slug

    default_track = next((t for t in tracks if t.is_default), None)
    if default_track is None and tracks:
        default_track = tracks[0]

    slugs = [t.slug for t in tracks]
    counts_qs = (
        UserResume.objects.for_user(user).filter(is_library=True)
        .filter(track__in=slugs)
        .values("track")
        .annotate(c=Count("id"))
    )
    resume_counts_by_slug = {row["track"]: row["c"] for row in counts_qs}
    resume_names_by_slug: dict[str, list[str]] = {slug: [] for slug in slugs}
    for resume in (
        UserResume.objects.for_user(user).filter(is_library=True)
        .filter(track__in=slugs)
        .order_by("-uploaded_at")
        .only("track", "original_filename")
    ):
        slug = resume.track
        if slug in resume_names_by_slug:
            resume_names_by_slug[slug].append(
                (resume.original_filename or "resume.pdf").strip() or "resume.pdf"
            )
    for slug in slugs:
        resume_counts_by_slug.setdefault(slug, 0)

    from .models import JobListingAction
    liked_qs = (
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.LIKED, track__in=slugs)
        .values("track")
        .annotate(c=Count("id"))
    )
    liked_counts = {row["track"]: row["c"] for row in liked_qs}

    disliked_qs = (
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.DISLIKED, track__in=slugs)
        .values("track")
        .annotate(c=Count("id"))
    )
    disliked_counts = {row["track"]: row["c"] for row in disliked_qs}

    from .saved_search_schedule import schedule_from_cron

    # Fetch all user tasks to map scheduling
    tasks_map = {}
    for task in JobSearchTask.objects.for_user(user):
        # Identify the mapping criteria
        if power_user:
            tasks_map[task.track] = task
        elif task.saved_search_id:
            tasks_map[task.saved_search_id] = task

    for track in tracks:
        track.library_resume_count = resume_counts_by_slug.get(track.slug, 0)
        track.library_resume_names = resume_names_by_slug.get(track.slug, [])
        track.liked_count = liked_counts.get(track.slug, 0)
        track.disliked_count = disliked_counts.get(track.slug, 0)

        # Map task schedule options to the track/profile
        task_id_key = track.slug if power_user else track.pk
        task = tasks_map.get(task_id_key)
        if task and task.is_active:
            parsed = schedule_from_cron(task.frequency)
            if parsed:
                track.schedule_interval, track.schedule_time = parsed
            else:
                track.schedule_interval = "custom"
                track.schedule_time = "09:00"
        else:
            track.schedule_interval = "off"
            track.schedule_time = "09:00"

    resume_rows = []
    for resume in _user_library_resumes(user).order_by("-uploaded_at")[:200]:
        try:
            size_bytes = resume.file.size if resume.file else None
        except Exception:
            size_bytes = None
        resume_rows.append(
            {
                "resume": resume,
                "file_size_display": _format_resume_file_size(size_bytes),
            }
        )

    execution_runs = []
    if not power_user:
        from .models import JobSearchTaskRun
        runs_qs = (
            JobSearchTaskRun.objects.filter(task__owner=user)
            .select_related("task", "task__saved_search")
            .order_by("-started_at", "-id")[:5]
        )
        for r in runs_qs:
            eliminated = max(0, r.jobs_fetched - r.jobs_after_filter)
            profile_name = r.task.saved_search.name if r.task.saved_search else (r.task.name or r.task.search_term)
            has_details = bool(r.details and isinstance(r.details, list) and len(r.details) > 0)
            execution_runs.append({
                "run": r,
                "profile_name": profile_name,
                "started_at": r.started_at,
                "status": r.get_status_display(),
                "jobs_fetched": r.jobs_fetched,
                "jobs_eliminated": eliminated,
                "jobs_saved": r.jobs_added_to_pipeline,
                "error_message": r.error_message,
                "has_details": has_details,
                "details_count": len(r.details) if has_details else 0,
            })

    return {
        "tracks": tracks,
        "resume_rows": resume_rows,
        "default_track_slug": default_track_slug,
        "resume_counts_by_slug": resume_counts_by_slug,
        "track_sort": sort,
        "stats": {
            "track_count": len(tracks),
            "resume_count": _count_unique_library_resumes(user),
            "resume_rows_count": len(resume_rows),
            "default_track": default_track,
        },
        "execution_runs": execution_runs,
    }


def _cascade_track_slug_rename(user, old_slug: str, new_slug: str) -> None:
    """Update track slug references across models that store slug as CharField."""
    from .models import (
        JobListingAction,
        JobListingEmbedding,
        JobListingTrackMetrics,
        JobSearchTask,
        PipelineEntry,
        UserResume,
    )

    UserResume.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    JobSearchTask.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    PipelineEntry.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    JobListingAction.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    JobListingEmbedding.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    JobListingTrackMetrics.objects.for_user(user).filter(track=old_slug).update(track=new_slug)
    try:
        invalidate_preference_cache(user)
        invalidate_disliked_embeddings_cache(user)
    except Exception:
        pass


def track_list_view(request):
    """
    Track Management page: search tracks (CRUD for Track) and resume PDFs (upload, assign
    default track, delete). POST `action` distinguishes create_track, edit_track,
    upload_resume, assign_resume_tracks, delete_resume.
    """
    from .experience import is_power_user
    from .models import SearchProfile

    power_user = is_power_user(request.user)
    if power_user:
        tracks_qs = Track.ensure_baseline(request.user)
        tracks = list(tracks_qs)
        default_track_slug = Track.get_default_slug(request.user)
    else:
        tracks_qs = SearchProfile.objects.for_user(request.user)
        tracks = []
        for p in tracks_qs:
            p.label = p.name
            tracks.append(p)
        default_track_slug = ""
        for t in tracks:
            if t.is_default:
                default_track_slug = t.slug
                break
        if not default_track_slug and tracks:
            default_track_slug = tracks[0].slug

    # Keep this bounded: track management should stay snappy even with many resumes.
    from .models import UserResume

    if request.method == "POST":
        action = (request.POST.get("action") or "create_track").strip()

        if action == "upload_resume":
            from .models import UserResume

            resume_file = request.FILES.get("resume_file")
            if not resume_file:
                messages.error(request, "Please select a PDF resume to upload.")
                return redirect("track_list")

            track_slug = (request.POST.get("track_slug") or "").strip().lower()
            track_slugs = {t.slug for t in tracks}
            if track_slug and track_slug not in track_slugs:
                messages.error(request, "Invalid track selection.")
                return redirect("track_list")

            original_name = (getattr(resume_file, "name", "") or "resume.pdf").strip()
            # Windows sometimes includes path-like names in uploads.
            original_name = original_name.split("\\")[-1].split("/")[-1].strip()
            if not original_name.lower().endswith(".pdf"):
                messages.error(request, "Resume file must be a PDF.")
                return redirect("track_list")
            file_size = getattr(resume_file, "size", None)
            if file_size is not None and file_size > MAX_TRACK_RESUME_UPLOAD_BYTES:
                messages.error(request, "Resume file must be 10MB or smaller.")
                return redirect("track_list")
            original_name = (original_name or "resume.pdf")[:255]

            from .subscriptions import QuotaExceeded, assert_upload_allowed

            try:
                assert_upload_allowed(request.user, resume_file)
            except QuotaExceeded as exc:
                messages.error(request, str(exc))
                return redirect("track_list")

            UserResume.objects.create(
                owner=request.user,
                file=resume_file,
                original_filename=original_name,
                track=track_slug or "",
                is_library=True,
            )
            from .experience import mark_onboarding_step

            mark_onboarding_step(request.user, "resume")
            messages.success(request, "Resume uploaded.")
            return redirect("track_list")

        # Delete a resume (and its derived optimization/match rows) from the same Tracks page.
        if action == "delete_resume":
            from .models import UserResume

            delete_resume_id_raw = (request.POST.get("delete_resume_id") or "").strip()
            try:
                delete_resume_id = int(delete_resume_id_raw)
            except (ValueError, TypeError):
                messages.error(request, "Invalid resume id.")
                return redirect("track_list")

            resume = _user_library_resumes(request.user).filter(id=delete_resume_id).first()
            if not resume:
                messages.error(request, "Resume not found.")
                return redirect("track_list")

            # Best-effort file cleanup (if storage supports it).
            try:
                resume.file.delete(save=False)
            except Exception:
                pass
            resume.delete()
            messages.success(request, "Resume deleted.")
            return redirect("track_list")

        if action == "assign_resume_tracks":
            track_slugs = {t.slug for t in tracks}
            updated_count = 0
            for key, value in request.POST.items():
                if not key.startswith("resume_track_"):
                    continue
                rid_raw = key.replace("resume_track_", "", 1)
                try:
                    rid = int(rid_raw)
                except (ValueError, TypeError):
                    continue
                new_slug = (value or "").strip().lower()
                if new_slug and new_slug not in track_slugs:
                    continue  # ignore invalid slugs
                updated_count += _user_library_resumes(request.user).filter(id=rid).update(track=new_slug or "")
            messages.success(
                request,
                f"Updated track assignment for {updated_count} resume(s).",
            )
            return redirect("track_list")

        if action == "edit_track":
            original_slug = (request.POST.get("original_slug") or "").strip().lower()
            new_slug = (request.POST.get("slug") or "").strip().lower()
            label = (request.POST.get("label") or "").strip()
            description = (request.POST.get("description") or "").strip()
            is_default = bool(request.POST.get("is_default"))
            schedule_interval = (request.POST.get("schedule_interval") or "off").strip().lower()
            schedule_time = (request.POST.get("schedule_time") or "09:00").strip()

            if power_user:
                track = Track.objects.for_user(request.user).filter(slug=original_slug).first()
                if not track:
                    messages.error(request, "Track not found.")
                    return redirect("track_list")
                if not new_slug:
                    messages.error(request, "Slug is required.")
                    return redirect("track_list")
                if not label:
                    messages.error(request, "Label is required.")
                    return redirect("track_list")
                if new_slug != original_slug and Track.objects.for_user(request.user).filter(slug=new_slug).exists():
                    messages.error(request, f"Track with slug '{new_slug}' already exists.")
                    return redirect("track_list")

                if is_default:
                    Track.objects.for_user(request.user).update(is_default=False)
                if new_slug != original_slug:
                    _cascade_track_slug_rename(request.user, original_slug, new_slug)
                    track.slug = new_slug
                track.label = label
                track.description = description
                track.is_default = is_default
                track.save(update_fields=["slug", "label", "description", "is_default"])

                # Handle scheduling for Power User Track tasks
                task = JobSearchTask.objects.for_user(request.user).filter(track=track.slug).first()
                if schedule_interval == "off":
                    if task:
                        task.is_active = False
                        task.save(update_fields=["is_active", "updated_at"])
                else:
                    from .saved_search_schedule import cron_from_schedule, parse_schedule_time
                    frequency = cron_from_schedule(schedule_interval, schedule_time)
                    start_time = parse_schedule_time(schedule_time)
                    if not task:
                        task = JobSearchTask(
                            owner=request.user,
                            name=f"{track.label} Search",
                            search_term=track.label,
                            track=track.slug,
                        )
                    task.frequency = frequency
                    task.start_time = start_time
                    task.is_active = True
                    task.next_run_at = get_next_run_at(task.frequency)
                    task.save()

                messages.success(request, f"Track \"{track.label}\" updated.")
            else:
                profile = SearchProfile.objects.for_user(request.user).filter(slug=original_slug).first()
                if not profile:
                    messages.error(request, "Search profile not found.")
                    return redirect("track_list")
                if not new_slug:
                    messages.error(request, "Slug is required.")
                    return redirect("track_list")
                if not label:
                    messages.error(request, "Label/Name is required.")
                    return redirect("track_list")
                if new_slug != original_slug and SearchProfile.objects.for_user(request.user).filter(slug=new_slug).exists():
                    messages.error(request, f"Search profile with slug '{new_slug}' already exists.")
                    return redirect("track_list")

                if is_default:
                    SearchProfile.objects.for_user(request.user).update(is_default=False)
                if new_slug != original_slug:
                    _cascade_track_slug_rename(request.user, original_slug, new_slug)
                    profile.slug = new_slug
                    profile.profile_slug = new_slug
                profile.name = label
                profile.description = description
                profile.is_default = is_default
                profile.save(update_fields=["slug", "profile_slug", "name", "description", "is_default"])

                # Handle scheduling for normal User SearchProfile
                from .saved_search_schedule import set_saved_search_schedule
                try:
                    set_saved_search_schedule(
                        request.user,
                        profile.id,
                        interval=schedule_interval,
                        time_str=schedule_time,
                    )
                except ValueError as e:
                    messages.error(request, f"Error saving schedule: {e}")

                messages.success(request, f"Search profile \"{profile.name}\" updated.")
            return redirect("track_list")

        if action == "create_track":
            slug = (request.POST.get("slug") or "").strip().lower()
            label = (request.POST.get("label") or "").strip()
            description = (request.POST.get("description") or "").strip()
            is_default = bool(request.POST.get("is_default"))
            schedule_interval = (request.POST.get("schedule_interval") or "off").strip().lower()
            schedule_time = (request.POST.get("schedule_time") or "09:00").strip()

            if not slug:
                messages.error(request, "Slug is required.")
            elif not label:
                messages.error(request, "Label/Name is required.")
            else:
                if power_user:
                    if Track.objects.for_user(request.user).filter(slug=slug).exists():
                        messages.error(request, f"Track with slug '{slug}' already exists.")
                    else:
                        if is_default:
                            Track.objects.for_user(request.user).update(is_default=False)
                        track = Track.objects.create(
                            owner=request.user,
                            slug=slug,
                            label=label,
                            description=description,
                            is_default=is_default,
                        )

                        # Handle scheduling for Power User Track tasks
                        if schedule_interval != "off":
                            from .saved_search_schedule import cron_from_schedule, parse_schedule_time
                            frequency = cron_from_schedule(schedule_interval, schedule_time)
                            start_time = parse_schedule_time(schedule_time)
                            JobSearchTask.objects.create(
                                owner=request.user,
                                name=f"{track.label} Search",
                                search_term=track.label,
                                track=track.slug,
                                frequency=frequency,
                                start_time=start_time,
                                is_active=True,
                                next_run_at=get_next_run_at(frequency),
                            )

                        messages.success(request, f"Track \"{track.label}\" created.")
                else:
                    if SearchProfile.objects.for_user(request.user).filter(slug=slug).exists():
                        messages.error(request, f"Search profile with slug '{slug}' already exists.")
                    else:
                        if is_default:
                            SearchProfile.objects.for_user(request.user).update(is_default=False)
                        profile = SearchProfile.objects.create(
                            owner=request.user,
                            slug=slug,
                            profile_slug=slug,
                            name=label,
                            search_term=label,
                            description=description,
                            is_default=is_default,
                        )

                        # Handle scheduling for normal User SearchProfile
                        if schedule_interval != "off":
                            from .saved_search_schedule import set_saved_search_schedule
                            try:
                                set_saved_search_schedule(
                                    request.user,
                                    profile.id,
                                    interval=schedule_interval,
                                    time_str=schedule_time,
                                )
                            except ValueError as e:
                                messages.error(request, f"Error saving schedule: {e}")

                        messages.success(request, f"Search profile \"{profile.name}\" created.")
            return redirect("track_list")

    context = _track_list_context(request, request.user, tracks_qs)
    return render(request, "resume_app/tracks.html", context)


def track_delete_view(request, slug: str):
    """
    Delete a track and cascade-delete associated data:
    - JobSearchTask for that track
    - PipelineEntry rows for that track
    - JobListingAction / JobListingEmbedding rows for that track
    """
    if request.method != "POST":
        return redirect("track_list")

    from .experience import is_power_user
    from .models import SearchProfile

    power_user = is_power_user(request.user)
    if power_user:
        track = Track.objects.for_user(request.user).filter(slug=slug).first()
        if not track:
            messages.error(request, "Track not found.")
            return redirect("track_list")

        if Track.objects.for_user(request.user).count() <= 1:
            messages.error(request, "Cannot delete the only remaining track.")
            return redirect("track_list")

        slug_val = track.slug
        was_default = track.is_default
        label = track.label or track.slug
        track.delete()
    else:
        profile = SearchProfile.objects.for_user(request.user).filter(slug=slug).first()
        if not profile:
            messages.error(request, "Search profile not found.")
            return redirect("track_list")

        if SearchProfile.objects.for_user(request.user).count() <= 1:
            messages.error(request, "Cannot delete the only remaining search profile.")
            return redirect("track_list")

        slug_val = profile.slug
        was_default = profile.is_default
        label = profile.name or profile.slug
        profile.delete()

    # Disassociate any resumes assigned to this track.
    try:
        from .models import UserResume
        UserResume.objects.for_user(request.user).filter(is_library=True, track=slug_val).update(track="")
    except Exception:
        pass

    # Delete scheduled searches for this track
    JobSearchTask.objects.for_user(request.user).filter(track=slug_val).delete()
    # Delete pipeline rows for this track
    PipelineEntry.objects.for_user(request.user).filter(track=slug_val).delete()
    # Delete job actions/embeddings for this track
    JobListingAction.objects.for_user(request.user).filter(Q(track__iexact=slug_val) | Q(track=slug_val)).delete()
    JobListingEmbedding.objects.for_user(request.user).filter(Q(track__iexact=slug_val) | Q(track=slug_val)).delete()
    # Invalidate preference caches so embeddings/centroids are recomputed
    try:
        invalidate_preference_cache(request.user, track=slug_val)
        invalidate_disliked_embeddings_cache(request.user, track=slug_val)
    except Exception:
        # Best-effort; failure here should not block delete.
        pass

    if was_default:
        # Ensure we still have a default track.
        if power_user:
            Track.ensure_baseline(request.user)
        else:
            first_p = SearchProfile.objects.for_user(request.user).first()
            if first_p:
                first_p.is_default = True
                first_p.save(update_fields=["is_default"])

    messages.success(request, f"Track/Profile \"{label}\" and its associated tasks/pipeline/actions were deleted.")
    return redirect("track_list")


from .pipeline_export_views import (
    download_scheduled_run_csv_view,
    scheduled_run_details_json_view,
)



def vetting_match_debug_view(request, job_listing_id: int):

    """
    One-shot vetting LLM match with full raw response for troubleshooting.
    Same prompt path as automation; does not update PipelineEntry fields.
    """
    tracks_qs = Track.ensure_baseline(request.user)
    available_slugs = list(tracks_qs.values_list("slug", flat=True))
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "").strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug(request.user)
    if request.session.get("job_search_track") != raw_track:
        request.session["job_search_track"] = raw_track
    if request.session.get("job_search_profile_slug") != raw_track:
        request.session["job_search_profile_slug"] = raw_track

    entry = (
        PipelineEntry.objects.for_user(request.user).filter(
            job_listing_id=job_listing_id,
            track=raw_track,
            stage=PipelineEntry.Stage.VETTING,
            removed_at__isnull=True,
        )
        .select_related("job_listing")
        .first()
    )
    if not entry:
        messages.error(request, "No vetting row found for this job and track.")
        return redirect(reverse("vetting") + f"?track={raw_track}")

    from .pipeline_llm_skill_extract import resolve_provider_api_key

    available_providers = sorted(
        [p for p in LLM_PROVIDERS if resolve_provider_api_key(p, user=request.user)]
    )
    selected_provider = (request.POST.get("llm_provider") or request.GET.get("provider") or "").strip()
    selected_model = (request.POST.get("llm_model") or request.GET.get("model") or "").strip()
    if selected_provider not in available_providers:
        selected_provider = None
    if not selected_provider:
        selected_provider = (
            "Ollama Local"
            if "Ollama Local" in available_providers
            else (available_providers[0] if available_providers else None)
        )

    llm_models = []
    llm_default_model = None
    llm_key_error = None
    if selected_provider:
        try:
            models_data = api_llm_models(request, provider=selected_provider)
            llm_models = models_data.get("models", [])
            llm_default_model = models_data.get("default_model")
        except HttpError as e:
            llm_key_error = str(e)

    if selected_model not in llm_models:
        selected_model = llm_default_model or (llm_models[0] if llm_models else None)

    debug_result = None
    matching_prompt = None
    if request.method == "POST":
        prompts = get_effective_prompts(request)
        matching_prompt = prompts.get("matching")
        debug_result = try_vetting_match_debug(
            entry,
            matching_prompt=matching_prompt or None,
            llm_provider=selected_provider,
            llm_model=selected_model,
        )
        if debug_result.get("ok"):
            ip = (debug_result.get("result") or {}).get("interview_probability")
            if ip is None:
                messages.warning(
                    request,
                    "LLM returned a response but interview probability was not parsed — the Vetting board will not show an Interview % badge until parsing succeeds. See raw output below.",
                )
            else:
                messages.success(request, "Match debug run finished. See parsed fields and raw response below.")
        else:
            messages.error(
                request,
                "Match debug did not complete: {}.".format(debug_result.get("skip_reason", "unknown")),
            )
    elif selected_provider == "Ollama Local" and not llm_key_error:
        try:
            prompts = get_effective_prompts(request)
            matching_prompt = prompts.get("matching")
            debug_result = try_vetting_match_debug(
                entry,
                matching_prompt=matching_prompt or None,
                llm_provider=selected_provider,
                llm_model=selected_model,
            )
        except Exception:
            debug_result = None

    context = {
        "entry": entry,
        "job": entry.job_listing,
        "track": raw_track,
        "jd_min": VETTING_MATCHING_JD_MIN_CHARS,
        "debug_result": debug_result,
        "vetting_url": reverse("vetting") + f"?track={raw_track}",
        "available_providers": available_providers,
        "selected_provider": selected_provider,
        "llm_models": llm_models,
        "selected_model": selected_model,
        "llm_key_error": llm_key_error,
    }
    return render(request, "resume_app/vetting_match_debug.html", context)


@_staff_required
def focus_breakdown_view(request, job_listing_id: int):
    """Staff-only: title vs role similarity breakdown (scoring internals)."""
    # Use the same track as the job search page so centroids and preference
    # margins line up with what you see in Results.
    tracks_qs = Track.ensure_baseline(request.user)
    available_slugs = list(tracks_qs.values_list("slug", flat=True))
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "").strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug(request.user)
    if request.session.get("job_search_track") != raw_track:
        request.session["job_search_track"] = raw_track
    if request.session.get("job_search_profile_slug") != raw_track:
        request.session["job_search_profile_slug"] = raw_track

    data = get_focus_breakdown(job_listing_id, user=request.user, track=raw_track)
    if data is None:
        messages.error(request, "Job not found or no liked jobs to compare. Like some jobs first.")
        return redirect("jobs_search")
    role_weight = round(1 - data["alpha"], 2)
    resume_id_raw = (request.GET.get("resume_id") or "").strip()
    resume_id = int(resume_id_raw) if resume_id_raw and resume_id_raw.lower() != "none" else None
    if resume_id is None:
        from .models import UserResume
        latest = _user_library_resumes(request.user).order_by("-uploaded_at").first()
        if latest:
            resume_id = latest.id
    # Stored AI Match (LLM) result from job search "AI Match" button
    llm_match = None
    if resume_id:
        stored = request.session.get("job_llm_match") or {}
        key = f"{job_listing_id}_{resume_id}"
        llm_match = stored.get(key)
    return render(
        request,
        "resume_app/focus_breakdown.html",
        {
            "breakdown": data,
            "role_weight": role_weight,
            "llm_match": llm_match,
            "job_search_track": raw_track,
        },
    )


@_staff_required
def focus_alignment_view(request, job_listing_id: int, liked_job_id: int):
    """
    Legacy alignment view: redirect to the V1 Fit Inspector.
    """
    return redirect(f"{reverse('fit_inspector')}?job_id={job_listing_id}")


def optimizer_save_draft_view(request, resume_id: int):
    """POST JSON { optimized_content } — save user edits to the final draft (no API token required)."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    content = body.get("optimized_content")
    if content is None:
        return JsonResponse({"error": "optimized_content is required"}, status=400)
    try:
        from .services import DraftSaveError, save_optimized_draft_content

        optimized = save_optimized_draft_content(int(resume_id), str(content), user=get_active_user(request))
        return JsonResponse({"ok": True, "optimized_content": optimized.optimized_content or ""})
    except DraftSaveError as e:
        return JsonResponse({"error": e.message}, status=e.status_code)
    except Exception as e:
        logger.exception("save draft failed for resume %s", resume_id)
        return JsonResponse({"error": str(e)}, status=500)


def optimizer_status_view(request, resume_id: int):
    """
    JSON endpoint used by the frontend to poll optimization status.
    Wraps the existing Ninja status helper without requiring API auth.
    """
    from django.http import Http404

    try:
        user = get_active_user(request)
        data = api_get_status_data(int(resume_id), user, request=request)
        return JsonResponse(data)
    except Http404:
        return JsonResponse({"error": f"Optimized resume {resume_id} not found"}, status=404)
    except Exception as e:
        logger.exception(f"Error fetching status for resume {resume_id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


def optimizer_context_debug_view(request, resume_id: int):
    """JSON: last saved writer context budget / retrieval debug for an OptimizedResume run."""
    from .models import OptimizedResume

    opt = get_owned_or_404(OptimizedResume, request.user, pk=int(resume_id))
    return JsonResponse(opt.optimizer_context_snapshot or {})


def serve_media_view(request, path: str):
    """Serve uploaded files only to the owning authenticated user (local or S3)."""
    import mimetypes

    from django.http import FileResponse, Http404

    from .media_access import media_exists, media_open, resolve_safe_media_path, user_may_access_media
    from .tenancy import get_active_user

    user = get_active_user(request)
    if not user_may_access_media(user, path):
        raise Http404("File not found")
    safe = resolve_safe_media_path(path)
    if not safe or not media_exists(safe):
        raise Http404("File not found")
    content_type, _ = mimetypes.guess_type(safe)
    return FileResponse(
        media_open(safe),
        content_type=content_type or "application/octet-stream",
    )


@login_required
def fit_inspector_view(request):
    """
    Job Fit Inspector & Simulator:
    - Test any job description against track preference models.
    - Reveal positive & negative driver jobs with similarity breakdown.
    - One-click pruning of training jobs (likes/dislikes).
    """
    from .tenancy import get_active_user
    from .models import JobListing, JobListingAction, JobListingEmbedding, Track, SearchProfile
    from .jobs_api import _resolve_track_from_request

    user = get_active_user(request)

    # 1. Fetch user's actual search profiles
    profiles_qs = SearchProfile.objects.for_user(user).order_by("name")
    search_profiles = []
    for p in profiles_qs:
        search_profiles.append({
            "slug": p.slug,
            "label": p.name or p.slug,
            "is_default": getattr(p, "is_default", False),
        })
    if not search_profiles:
        tracks_qs = Track.ensure_baseline(user)
        for t in tracks_qs:
            search_profiles.append({
                "slug": t.slug,
                "label": t.label or t.slug,
                "is_default": getattr(t, "is_default", False),
            })

    profile_slugs = [p["slug"] for p in search_profiles]
    param_track = (request.POST.get("track") or request.GET.get("track") or "").strip()

    if param_track:
        raw_track = param_track
    else:
        raw_track = _resolve_track_from_request(user, request, track=None)
        if raw_track not in profile_slugs and profile_slugs:
            raw_track = profile_slugs[0]

    if request.session.get("job_search_track") != raw_track:
        request.session["job_search_track"] = raw_track
    if request.session.get("job_search_profile_slug") != raw_track:
        request.session["job_search_profile_slug"] = raw_track

    from .track_actions import (
        normalize_track_slug,
        q_preference_embedding_track,
        q_clear_on_sentiment_change,
    )
    from .preference import (
        get_preference_vectors,
        get_disliked_embeddings,
        invalidate_preference_cache,
        invalidate_disliked_embeddings_cache,
    )
    from . import embeddings as embedding_module

    norm_track = normalize_track_slug(raw_track, user)

    # Handle removal actions (POST)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove_like":
            try:
                job_id = int(request.POST.get("job_id"))
                JobListingAction.objects.for_user(user).filter(
                    job_listing_id=job_id, action=JobListingAction.ActionType.LIKED
                ).filter(Q(track__iexact=norm_track) | Q(track="")).delete()
                JobListingEmbedding.objects.for_user(user).filter(
                    job_listing_id=job_id,
                    embedding_type=JobListingEmbedding.EmbeddingType.LIKED,
                ).filter(Q(track__iexact=norm_track) | Q(track="")).delete()
                invalidate_preference_cache(user, track=norm_track)
                invalidate_disliked_embeddings_cache(user, track=norm_track)
                messages.success(request, f"Removed job #{job_id} from Liked training set.")
            except Exception as e:
                messages.error(request, f"Failed to remove like: {e}")
        elif action == "remove_dislike":
            try:
                job_id = int(request.POST.get("job_id"))
                JobListingAction.objects.for_user(user).filter(
                    job_listing_id=job_id, action=JobListingAction.ActionType.DISLIKED
                ).filter(Q(track__iexact=norm_track) | Q(track="")).delete()
                JobListingEmbedding.objects.for_user(user).filter(
                    job_listing_id=job_id,
                    embedding_type=JobListingEmbedding.EmbeddingType.DISLIKED,
                ).filter(Q(track__iexact=norm_track) | Q(track="")).delete()
                invalidate_preference_cache(user, track=norm_track)
                invalidate_disliked_embeddings_cache(user, track=norm_track)
                messages.success(request, f"Removed job #{job_id} from Disliked training set.")
            except Exception as e:
                messages.error(request, f"Failed to remove dislike: {e}")

    # Form inputs for simulation
    job_id_param = (request.POST.get("job_id") or request.GET.get("job_id") or "").strip()
    sim_title = (request.POST.get("title") or request.GET.get("title") or "").strip()
    sim_company = (request.POST.get("company") or request.GET.get("company") or "").strip()
    sim_description = (request.POST.get("description") or request.GET.get("description") or "").strip()

    loaded_job = None
    if job_id_param:
        try:
            loaded_job = JobListing.objects.filter(id=int(job_id_param)).first()
            if loaded_job:
                if not sim_title:
                    sim_title = loaded_job.title or ""
                if not sim_company:
                    sim_company = loaded_job.company_name or ""
                if not sim_description:
                    sim_description = loaded_job.description or ""
        except (ValueError, TypeError):
            pass

    analysis = None

    if sim_title or sim_description:
        try:
            if sim_description:
                input_vec = embedding_module.embed_full(sim_title, sim_description)
            else:
                input_vec = embedding_module.embed_title_only(sim_title, sim_company)
        except Exception as e:
            input_vec = None
            logger.warning("[fit_inspector] Embedding failed: %s", e)

        if not input_vec:
            messages.warning(
                request, "Unable to compute text embedding for the provided title/description."
            )
        else:
            prefs = get_preference_vectors(user=user, track=norm_track)
            disliked_embeddings = get_disliked_embeddings(user=user, track=norm_track)

            if not prefs or not prefs[0]:
                analysis = {
                    "has_baseline": False,
                    "message": "No liked jobs in this search profile yet. Like at least one job to establish a preference baseline.",
                }
            else:
                liked_centroid, disliked_centroid, liked_jobs = prefs
                liked_titles = [ltitle for _, ltitle, _, _, _ in (liked_jobs or [])]

                from .job_ranking import compute_title_textual_closeness, calibrate_desc_semantic_score

                # Target search terms for the profile
                target_terms = [norm_track]
                sp = SearchProfile.objects.filter(owner=user, slug=norm_track).first()
                if sp:
                    if sp.search_term:
                        target_terms.append(sp.search_term)
                    if sp.name and sp.name not in target_terms:
                        target_terms.append(sp.name)

                # 1. Title Textual Closeness
                t_score, t_details = compute_title_textual_closeness(sim_title, target_terms, liked_titles)
                title_textual_pct = round(t_score * 100, 1)

                # 2. Description Calibrated Semantic Closeness
                sim_liked = embedding_module.cosine_similarity(input_vec, liked_centroid)
                like_pct = int(round(max(0.0, min(1.0, (sim_liked + 1.0) / 2.0)) * 100))
                d_score = calibrate_desc_semantic_score(sim_liked)
                desc_calibrated_pct = round(d_score * 100, 1)

                # 3. Hybrid Base Score (35% Title, 65% Description)
                alpha_title = getattr(settings, "JOB_FOCUS_V1_TITLE_WEIGHT", 0.35)
                alpha_desc = getattr(settings, "JOB_FOCUS_V1_DESC_WEIGHT", 0.65)
                raw_hybrid = alpha_title * t_score + alpha_desc * d_score

                # 4. Title Guardrail
                guardrail_applied = False
                if t_score < 0.15:
                    final_hybrid = min(raw_hybrid, t_score + 0.15)
                    guardrail_applied = (final_hybrid < raw_hybrid)
                elif t_score < 0.30:
                    final_hybrid = min(raw_hybrid, t_score + 0.35)
                    guardrail_applied = (final_hybrid < raw_hybrid)
                else:
                    final_hybrid = raw_hybrid

                v1_score = int(round(final_hybrid * 100))

                dislike_pct = None
                margin_pct = None
                if disliked_centroid:
                    sim_disliked = embedding_module.cosine_similarity(input_vec, disliked_centroid)
                    dislike_pct = int(round(max(0.0, min(1.0, (sim_disliked + 1.0) / 2.0)) * 100))
                    margin_pct = like_pct - dislike_pct

                # Determine verdict based on V1 Match Score and threshold
                cfg = AppAutomationSettings.get_for_user(user)
                threshold = getattr(cfg, "pipeline_match_score_min", 60)

                if margin_pct is not None and margin_pct < -5:
                    verdict_label = "Disliked Traits Alert (Blocked)"
                    verdict_desc = f"V1 Match Score is {v1_score}%, but preference margin is {margin_pct}% (below -5% guardrail). Auto-promotion to Review is blocked."
                    verdict_style = "bg-amber-50 text-amber-800 border-amber-200"
                elif v1_score >= threshold:
                    verdict_label = "Auto-Promote to Review (High Fit)"
                    verdict_desc = f"V1 Match Score is {v1_score}% (meets the {threshold}% threshold). Qualified for automatic intake promotion."
                    verdict_style = "bg-emerald-50 text-emerald-800 border-emerald-200"
                else:
                    verdict_label = "Below Threshold (Intake Only)"
                    if guardrail_applied:
                        verdict_desc = f"V1 Match Score is {v1_score}% (below {threshold}% threshold). Title match is {title_textual_pct}%; Title Guardrail capped description lift from {int(round(raw_hybrid * 100))}% down to {v1_score}%."
                    else:
                        verdict_desc = f"V1 Match Score is {v1_score}% (below {threshold}% threshold). Title match: {title_textual_pct}%, Description semantic: {desc_calibrated_pct}%."
                    verdict_style = "bg-rose-50 text-rose-800 border-rose-200"

                # Rank top 5 liked job drivers using pairwise V1 matching
                liked_drivers = []
                for ljid, ljtitle, ljcomp, ljemb, _ in liked_jobs or []:
                    if ljemb:
                        lt_score, _ = compute_title_textual_closeness(sim_title, [ljtitle or ""], [ljtitle or ""])
                        raw_sim = embedding_module.cosine_similarity(input_vec, ljemb)
                        cal_sim = calibrate_desc_semantic_score(raw_sim)
                        pair_raw = alpha_title * lt_score + alpha_desc * cal_sim
                        if lt_score < 0.20:
                            pair_final = min(pair_raw, lt_score + 0.10)
                        elif lt_score < 0.40:
                            pair_final = min(pair_raw, lt_score + 0.25)
                        else:
                            pair_final = pair_raw
                        p_v1 = int(round(pair_final * 100))
                        liked_drivers.append(
                            {
                                "job_id": ljid,
                                "title": ljtitle or "Untitled",
                                "company": ljcomp or "Unknown",
                                "similarity_percent": p_v1,
                                "title_pct": round(lt_score * 100, 1),
                                "desc_pct": round(cal_sim * 100, 1),
                                "raw_cosine_pct": int(round(max(0.0, min(1.0, (raw_sim + 1.0) / 2.0)) * 100)),
                            }
                        )
                liked_drivers.sort(key=lambda x: -x["similarity_percent"])
                liked_drivers = liked_drivers[:5]

                # Rank top 5 disliked job drivers
                disliked_drivers = []
                if disliked_embeddings:
                    disliked_job_ids = [djid for djid, _ in disliked_embeddings]
                    dj_map = {j.id: j for j in JobListing.objects.filter(id__in=disliked_job_ids)}
                    for djid, demb in disliked_embeddings:
                        if demb:
                            s = embedding_module.cosine_similarity(input_vec, demb)
                            p = int(round(max(0.0, min(1.0, (s + 1.0) / 2.0)) * 100))
                            j_obj = dj_map.get(djid)
                            disliked_drivers.append(
                                {
                                    "job_id": djid,
                                    "title": j_obj.title if j_obj else f"Job #{djid}",
                                    "company": j_obj.company_name if j_obj else "—",
                                    "similarity_percent": p,
                                }
                            )
                    disliked_drivers.sort(key=lambda x: -x["similarity_percent"])
                    disliked_drivers = disliked_drivers[:5]

                analysis = {
                    "has_baseline": True,
                    "v1_score": v1_score,
                    "title_textual_pct": title_textual_pct,
                    "desc_calibrated_pct": desc_calibrated_pct,
                    "guardrail_applied": guardrail_applied,
                    "raw_hybrid_pct": int(round(raw_hybrid * 100)),
                    "threshold": threshold,
                    "like_percent": like_pct,
                    "dislike_percent": dislike_pct,
                    "margin_percent": margin_pct,
                    "verdict_label": verdict_label,
                    "verdict_desc": verdict_desc,
                    "verdict_style": verdict_style,
                    "liked_drivers": liked_drivers,
                    "disliked_drivers": disliked_drivers,
                }

    # Fetch training sets for display
    liked_qs = (
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.LIKED)
        .filter(q_preference_embedding_track(norm_track, user))
    )
    disliked_qs = (
        JobListingAction.objects.for_user(user)
        .filter(action=JobListingAction.ActionType.DISLIKED)
        .filter(q_preference_embedding_track(norm_track, user))
    )

    liked_count = liked_qs.count()
    disliked_count = disliked_qs.count()
    total_count = liked_count + disliked_count

    liked_actions = list(liked_qs.select_related("job_listing").order_by("-created_at")[:200])
    disliked_actions = list(disliked_qs.select_related("job_listing").order_by("-created_at")[:500])

    training_items = []
    for a in liked_actions:
        training_items.append({
            "id": a.id,
            "job_id": a.job_listing_id,
            "title": a.job_listing.title if a.job_listing else "Untitled",
            "company": a.job_listing.company_name if a.job_listing else "—",
            "url": a.job_listing.url if a.job_listing else "",
            "action": "liked",
            "action_label": "Liked",
            "badge_class": "bg-emerald-50 text-emerald-700 border-emerald-200",
            "icon": "👍",
            "created_at": a.created_at,
        })
    for a in disliked_actions:
        training_items.append({
            "id": a.id,
            "job_id": a.job_listing_id,
            "title": a.job_listing.title if a.job_listing else "Untitled",
            "company": a.job_listing.company_name if a.job_listing else "—",
            "url": a.job_listing.url if a.job_listing else "",
            "action": "disliked",
            "action_label": "Disliked",
            "badge_class": "bg-rose-50 text-rose-700 border-rose-200",
            "icon": "👎",
            "created_at": a.created_at,
        })
    training_items.sort(key=lambda x: x["created_at"] or timezone.now(), reverse=True)

    default_filter = "liked" if liked_count > 0 else ("disliked" if disliked_count > 0 else "all")

    context = {
        "search_profiles": search_profiles,
        "selected_track": norm_track,
        "sim_job_id": job_id_param,
        "loaded_job": loaded_job,
        "sim_title": sim_title,
        "sim_company": sim_company,
        "sim_description": sim_description,
        "analysis": analysis,
        "training_items": training_items,
        "liked_count": liked_count,
        "disliked_count": disliked_count,
        "total_count": total_count,
        "default_filter": default_filter,
    }
    return render(request, "resume_app/fit_inspector.html", context)


@login_required
def performance_dashboard_view(request):
    """
    Motivational Career Performance & Momentum Dashboard.
    Renders immediately with cached data or fast skeleton placeholders,
    progressively hydrated by client-side telemetry fetch.
    """
    user = get_active_user(request)
    from django.core.cache import cache
    cache_key = f"perf_stats_{user.id}"
    cached_stats = cache.get(cache_key)
    if not cached_stats:
        from .dashboard_stats import get_performance_dashboard_stats
        cached_stats = get_performance_dashboard_stats(user)
        cache.set(cache_key, cached_stats, timeout=120)

    return render(request, "resume_app/performance_dashboard.html", {
        "stats": cached_stats,
        "is_cached": bool(cached_stats),
        "active_tab": "performance",
    })


@login_required
def dashboard_metrics_api(request):
    """
    Asynchronous JSON telemetry endpoint for progressive dashboard hydration.
    """
    user = get_active_user(request)
    from django.core.cache import cache
    from .dashboard_stats import get_performance_dashboard_stats

    cache_key = f"perf_stats_{user.id}"
    if request.GET.get("refresh") == "1":
        cache.delete(cache_key)

    stats = cache.get(cache_key)
    if not stats:
        stats = get_performance_dashboard_stats(user)
        cache.set(cache_key, stats, timeout=120)  # 2 minute cache

    return JsonResponse({"status": "ok", "stats": stats})


@login_required
def update_dashboard_settings_api(request):
    """
    API endpoint to update user settings directly from the dashboard controls.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    user = get_active_user(request)
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    from .models import AppAutomationSettings, ApplicantProfile, UserDisqualifier
    settings = AppAutomationSettings.get_for_user(user)

    if "weekly_target_applications" in data:
        try:
            val = max(1, min(100, int(data["weekly_target_applications"])))
            settings.weekly_target_applications = val
            settings.save(update_fields=["weekly_target_applications"])
        except (ValueError, TypeError):
            pass

    if "min_comp_floor" in data:
        profile = ApplicantProfile.get_for_user(user)
        profile.salary_expectation = str(data["min_comp_floor"]).strip()
        profile.save(update_fields=["salary_expectation"])

    if "add_disqualifier" in data:
        phrase = str(data["add_disqualifier"]).strip()
        if phrase:
            UserDisqualifier.objects.get_or_create(owner=user, phrase=phrase)

    if "remove_disqualifier" in data:
        phrase = str(data["remove_disqualifier"]).strip()
        if phrase:
            UserDisqualifier.objects.filter(owner=user, phrase__iexact=phrase).delete()

    return JsonResponse({
        "status": "ok",
        "weekly_target_applications": settings.weekly_target_applications,
    })


from .pipeline_export_views import (
    pipeline_applied_resume_json_view,
    pipeline_export_pdf_view,
    pipeline_export_docx_view,
)
from .career_profiler_views import (
    career_profiler_view,
    career_profiler_analyze_api,
    career_profiler_provision_api,
    career_profiler_preview_matches_api,
)




