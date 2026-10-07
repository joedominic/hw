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

        if action == "approve_and_mark_applied":
            target_job_id = request.POST.get("job_id") or request.GET.get("job_id") or request.session.get("optimizer_job_id")
            track_slug = request.POST.get("track") or request.GET.get("track") or request.session.get("optimizer_track") or track_slug or "dir-plano"
            opt_id_val = request.POST.get("opt_id") or request.GET.get("opt_id") or opt_id

            from .models import OptimizedResume, PipelineEntry
            from .domain.event_bus import event_bus
            from .domain.events import JobMarkedApplied

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
                        messages.success(request, "Opportunity approved & moved to Applied!")
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
                            messages.success(request, "Opportunity approved & moved to Applied (tagged as Liked)!")
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
        "pipeline_track": track_slug or (target_job.track if target_job and hasattr(target_job, "track") and target_job.track else "dir-plano"),
    }
    return render(request, "resume_app/optimizer.html", context)


def settings_view(request):
    """
    Settings: LLM integrations (tab) and app automation thresholds (tab).
    """
    from .models import (
        AppAutomationSettings,
        LLMProviderConfig,
        LLMProviderPreference,
        LLMAppUsageTotals,
        LLMUsageByModel,
        LLMUsageByQuery,
        Track,
    )
    from .llm import USAGE_QUERY_LABELS, get_llm, list_models_for_provider
    from .llm.rate_limit import get_llm_cooldown_ttl
    from .crypto import decrypt_api_key
    from langchain_core.messages import HumanMessage

    user = get_active_user(request)
    active_provider = _get_active_llm_provider(user, request)
    provider_infos = []
    for p in sorted(LLM_PROVIDERS):
        config = LLMProviderConfig.objects.for_user(user).filter(provider=p).first()
        provider_infos.append({
            "name": p,
            "key_stored": bool(config and config.encrypted_api_key),
            "is_active": p == active_provider,
            "priority": config.priority if config else 100,
            "default_model": config.default_model if config else "",
        })

    if request.method == "POST":
        from .account_views import handle_account_settings_post

        account_response = handle_account_settings_post(request)
        if account_response is not None:
            return account_response

        action = request.POST.get("action")
        if action == "save_experience_mode":
            from .experience import set_experience_mode
            from .models import UserExperienceSettings
            from .tenancy import get_real_user

            real_user = get_real_user(request)
            if not getattr(real_user, "is_staff", False):
                messages.error(request, "Experience mode is managed by admins.")
                return redirect(reverse("settings") + "?tab=account")
            mode = (request.POST.get("experience_mode") or "").strip().lower()
            if mode not in (
                UserExperienceSettings.ExperienceMode.NORMAL,
                UserExperienceSettings.ExperienceMode.POWER,
            ):
                messages.error(request, "Invalid experience mode.")
            else:
                set_experience_mode(user, mode)
                label = "Advanced" if mode == UserExperienceSettings.ExperienceMode.POWER else "Simple"
                messages.success(request, f"Experience mode set to {label}.")
            return redirect(reverse("settings") + "?tab=account")
        if action == "refresh_provider_models":
            cached: dict[str, list] = {}
            for cfg in _get_provider_preferences(user):
                if not cfg.encrypted_api_key:
                    continue
                try:
                    key = decrypt_api_key(cfg.encrypted_api_key)
                    if key:
                        cached[cfg.provider] = list(list_models_for_provider(cfg.provider, key))
                    else:
                        cached[cfg.provider] = []
                except Exception:
                    cached[cfg.provider] = []
            request.session["settings_provider_models_map"] = cached
            request.session.modified = True
            messages.success(
                request,
                "Loaded model lists from providers. If a provider timed out, try again.",
            )
            return redirect(reverse("settings") + "?tab=llm")
        if action == "reset_today_quotas":
            from django.utils import timezone
            from .models import UsageCounter, LLMDailyUsageBreakdown
            today = timezone.localdate()
            UsageCounter.objects.for_user(user).filter(period_date=today).delete()
            LLMDailyUsageBreakdown.objects.for_user(user).filter(period_date=today).delete()
            messages.success(
                request,
                f"Today's daily quota counters ({today.strftime('%b %d, %Y')}) were reset. You can now make new LLM calls.",
            )
            return redirect(reverse("settings") + "?tab=usage")
        if action == "reset_llm_usage_stats":
            if not request.user.is_staff:
                messages.error(request, "Permission denied.")
                return redirect(reverse("settings") + "?tab=usage")
            from .models import UsageCounter, LLMDailyUsageBreakdown
            solo = LLMAppUsageTotals.get_for_user(user)
            LLMAppUsageTotals.objects.filter(pk=solo.pk).update(
                total_input_tokens=0,
                total_output_tokens=0,
                total_requests=0,
                total_estimated_invokes=0,
            )
            LLMUsageByModel.objects.for_user(user).delete()
            LLMUsageByQuery.objects.for_user(user).delete()
            UsageCounter.objects.for_user(user).delete()
            LLMDailyUsageBreakdown.objects.for_user(user).delete()
            messages.success(
                request,
                "LLM usage totals, per-model / per-query counters, and daily quota ledgers were reset.",
            )
            return redirect(reverse("settings") + "?tab=usage")
        if action == "save_stop_llm_requests":
            automation = AppAutomationSettings.get_for_user(user)
            automation.stop_llm_requests = bool(request.POST.get("stop_llm_requests"))
            automation.save(update_fields=["stop_llm_requests", "updated_at"])
            messages.success(request, "LLM safety settings saved.")
            return redirect(reverse("settings") + "?tab=llm")
        if action == "save_app_automation":
            automation = AppAutomationSettings.get_for_user(user)
            automation.pipeline_to_vetting_enabled = bool(
                request.POST.get("pipeline_to_vetting_enabled")
            )
            automation.vetting_to_applying_enabled = bool(
                request.POST.get("vetting_to_applying_enabled")
            )
            try:
                automation.pipeline_preference_margin_min = int(
                    (request.POST.get("pipeline_preference_margin_min") or "0").strip()
                )
            except ValueError:
                messages.error(request, "Pref margin threshold must be a whole number.")
                return redirect(reverse("settings") + "?tab=app")
            raw_match_min = request.POST.get("pipeline_match_score_min")
            if raw_match_min is not None and raw_match_min.strip() != "":
                try:
                    pmm = int(raw_match_min.strip())
                    if pmm < 0 or pmm > 100:
                        messages.error(request, "Match score threshold must be between 0 and 100.")
                        return redirect(reverse("settings") + "?tab=app")
                    automation.pipeline_match_score_min = pmm
                except ValueError:
                    messages.error(request, "Match score threshold must be a whole number.")
                    return redirect(reverse("settings") + "?tab=app")
            raw_purge_max = request.POST.get("pipeline_purge_match_score_max")
            if raw_purge_max is not None and raw_purge_max.strip() != "":
                try:
                    ppm = int(raw_purge_max.strip())
                    if ppm < 0 or ppm > 100:
                        messages.error(request, "Auto-dismiss match score threshold must be between 0 and 100.")
                        return redirect(reverse("settings") + "?tab=app")
                    automation.pipeline_purge_match_score_max = ppm
                except ValueError:
                    messages.error(request, "Auto-dismiss match score threshold must be a whole number.")
                    return redirect(reverse("settings") + "?tab=app")
            try:
                vip = int(
                    (request.POST.get("vetting_interview_probability_min") or "70").strip()
                )
            except ValueError:
                messages.error(request, "Interview probability threshold must be a whole number.")
                return redirect(reverse("settings") + "?tab=app")
            if vip < 0 or vip > 100:
                messages.error(request, "Interview probability must be between 0 and 100.")
                return redirect(reverse("settings") + "?tab=app")
            automation.vetting_interview_probability_min = vip
            raw_wf = (request.POST.get("applying_optimizer_workflow") or "").strip()
            if raw_wf:
                from .prompt_store import get_optimizer_workflow_by_id

                wf = get_optimizer_workflow_by_id(int(raw_wf), user) if raw_wf.isdigit() else None
                if wf is None:
                    messages.error(request, "Invalid optimizer workflow selection.")
                    return redirect(reverse("settings") + "?tab=app")
                automation.applying_optimizer_workflow = wf
            else:
                automation.applying_optimizer_workflow = None

            _save_optimizer_supporting_context(
                user,
                (request.POST.get("optimization_notes") or "").strip(),
                (request.POST.get("pipeline_skills_json") or "").strip(),
                (request.POST.get("job_highlights") or "").strip(),
            )

            def _cleanup_days(field: str):
                raw = (request.POST.get(field) or "").strip()
                try:
                    v = int(raw)
                except ValueError:
                    return None
                if v < 0 or v > 365:
                    return None
                return v

            cjob = _cleanup_days("cleanup_job_retention_days")
            if cjob is None:
                cjob = 7
            cjob = max(1, min(30, cjob))
            cg = _cleanup_days("cleanup_generated_resume_retention_days")
            if cg is None:
                cg = 7
            automation.cleanup_job_retention_days = cjob
            automation.cleanup_pipeline_retention_days = cjob
            automation.cleanup_vetting_retention_days = cjob
            automation.cleanup_applying_retention_days = cjob
            automation.cleanup_done_retention_days = 0  # Applied jobs are strictly exempt from purge
            automation.cleanup_generated_resume_retention_days = cg

            automation.save(
                update_fields=[
                    "pipeline_to_vetting_enabled",
                    "pipeline_match_score_min",
                    "pipeline_purge_match_score_max",
                    "pipeline_preference_margin_min",
                    "vetting_to_applying_enabled",
                    "vetting_interview_probability_min",
                    "applying_optimizer_workflow",
                    "cleanup_job_retention_days",
                    "cleanup_pipeline_retention_days",
                    "cleanup_vetting_retention_days",
                    "cleanup_applying_retention_days",
                    "cleanup_done_retention_days",
                    "cleanup_generated_resume_retention_days",
                    "updated_at",
                ]
            )
            messages.success(request, "App automation settings saved.")
            return redirect(reverse("settings") + "?tab=app")
        if action == "save_export_replacements":
            replacements = []
            for i in range(5):
                token = (request.POST.get(f"replacement_token_{i}") or "").strip()
                value = (request.POST.get(f"replacement_value_{i}") or "").strip()
                replacements.append({"token": token, "value": value})
            automation = AppAutomationSettings.get_for_user(user)
            stored = automation.set_export_replacements(replacements)
            # Keep session mirror for in-flight tabs; DB is the source of truth.
            request.session["export_replacements"] = stored
            request.session.modified = True
            messages.success(request, "Export replacement tokens saved.")
            return redirect(reverse("settings") + "?tab=replacements")
        if action == "dedupe_pipeline":
            from .job_dedupe import dedupe_pipeline_entries

            track = (request.POST.get("dedupe_track") or "*").strip().lower()
            stage = (request.POST.get("dedupe_stage") or "all").strip().lower()
            include_done = bool(request.POST.get("dedupe_include_done"))
            try:
                result = dedupe_pipeline_entries(
                    user=user,
                    track_slug=track,
                    stage=stage,
                    include_done=include_done,
                )
            except ValueError as e:
                messages.error(request, str(e))
                return redirect(reverse("settings") + "?tab=app")

            n = int(result.get("entries_removed", 0))
            g = int(result.get("duplicate_groups", 0))
            if n == 0:
                messages.info(request, "No duplicate groups found for the selected scope.")
            else:
                messages.success(
                    request,
                    f"Removed {n} duplicate pipeline row(s) across {g} group(s).",
                )
            return redirect(reverse("settings") + "?tab=app")
        if action == "save_provider_preferences":
            ping_prompt = "Respond with exactly: OK"
            ping_results = []
            remove_ids = set()
            for rid in request.POST.getlist("remove_pref_id"):
                try:
                    remove_ids.add(int(rid))
                except (TypeError, ValueError):
                    pass

            pref_ids = request.POST.getlist("pref_id")
            pref_providers = request.POST.getlist("pref_provider")
            pref_models = request.POST.getlist("pref_model")
            pref_priorities = request.POST.getlist("pref_priority")
            pref_rpms = request.POST.getlist("pref_rate_limit_rpm")
            pref_tpms = request.POST.getlist("pref_rate_limit_tpm")
            pref_cooldowns = request.POST.getlist("pref_rate_limit_cooldown")
            pref_is_locals = request.POST.getlist("pref_is_local")
            row_count = max(
                len(pref_ids),
                len(pref_providers),
                len(pref_models),
                len(pref_priorities),
                len(pref_rpms),
                len(pref_tpms),
                len(pref_cooldowns),
                len(pref_is_locals),
            )

            def _parse_rate_limit_field(raw: str):
                v = (raw or "").strip()
                if not v:
                    return None
                try:
                    n = int(v)
                    return n if n > 0 else None
                except ValueError:
                    return None

            def _parse_cooldown_field(raw: str):
                v = (raw or "").strip()
                if not v:
                    return None
                try:
                    n = int(v)
                    return n if n > 0 else None
                except ValueError:
                    return None

            saved_rows = []
            rate_limit_partial_rows: list[str] = []
            for i in range(row_count):
                raw_id = pref_ids[i].strip() if i < len(pref_ids) else ""
                provider = pref_providers[i].strip() if i < len(pref_providers) else ""
                model = pref_models[i].strip() if i < len(pref_models) else ""
                raw_priority = pref_priorities[i].strip() if i < len(pref_priorities) else "100"
                rl_rpm = _parse_rate_limit_field(pref_rpms[i] if i < len(pref_rpms) else "")
                rl_tpm = _parse_rate_limit_field(pref_tpms[i] if i < len(pref_tpms) else "")
                rl_cd = _parse_cooldown_field(pref_cooldowns[i] if i < len(pref_cooldowns) else "")
                raw_local = (pref_is_locals[i] if i < len(pref_is_locals) else "0").strip()
                is_local = raw_local in ("1", "true", "on", "yes")
                if not provider:
                    continue
                try:
                    priority = max(0, int(raw_priority or "100"))
                except ValueError:
                    messages.error(request, f"Priority for {provider} must be a whole number.")
                    return redirect(reverse("settings") + "?tab=llm")
                cfg = (
                    LLMProviderConfig.objects.for_user(user)
                    .filter(provider=provider)
                    .exclude(encrypted_api_key="")
                    .first()
                )
                if not cfg:
                    continue
                pref_obj = None
                if raw_id:
                    try:
                        rid = int(raw_id)
                        if rid in remove_ids:
                            LLMProviderPreference.objects.filter(
                                id=rid,
                                provider_config__owner=user,
                            ).delete()
                            continue
                        pref_obj = LLMProviderPreference.objects.filter(
                            id=rid,
                            provider_config__owner=user,
                        ).first()
                    except (TypeError, ValueError):
                        pref_obj = None
                if pref_obj is None:
                    pref_obj = LLMProviderPreference()
                pref_obj.provider_config = cfg
                pref_obj.model = model
                pref_obj.priority = priority
                pref_obj.is_local = is_local
                if rl_rpm is not None and rl_tpm is not None:
                    pref_obj.rate_limit_rpm = rl_rpm
                    pref_obj.rate_limit_tpm = rl_tpm
                elif rl_rpm is not None or rl_tpm is not None:
                    rate_limit_partial_rows.append(provider)
                    pref_obj.rate_limit_rpm = None
                    pref_obj.rate_limit_tpm = None
                else:
                    pref_obj.rate_limit_rpm = None
                    pref_obj.rate_limit_tpm = None
                pref_obj.rate_limit_cooldown_seconds = rl_cd
                pref_obj.save()
                saved_rows.append(pref_obj)

            if remove_ids:
                LLMProviderPreference.objects.filter(
                    id__in=remove_ids,
                    provider_config__owner=user,
                ).delete()

            if AppAutomationSettings.get_for_user(user).stop_llm_requests:
                messages.info(request, "Skipped connectivity ping while Stop LLM requests is enabled.")
            for row in saved_rows:
                cfg = row.provider_config
                model = (row.model or cfg.default_model or "").strip()
                if cfg.encrypted_api_key and model and not AppAutomationSettings.get_for_user(user).stop_llm_requests:
                    try:
                        api_key_decrypted = decrypt_api_key(cfg.encrypted_api_key)
                        llm = get_llm(cfg.provider, api_key_decrypted, model=model)
                        from .llm import log_llm_invoke

                        log_llm_invoke(
                            cfg.provider,
                            model,
                            query="settings_connectivity_ping",
                            via="direct",
                        )
                        resp = llm.invoke([HumanMessage(content=ping_prompt)])
                        text = (getattr(resp, "content", None) or str(resp)).strip()
                        if text.upper().startswith("OK"):
                            ping_results.append(f"{cfg.provider}/{model}: OK")
                        else:
                            ping_results.append(f"{cfg.provider}/{model}: unexpected response")
                    except Exception as e:
                        ping_results.append(f"{cfg.provider}/{model}: failed ({e})")
            messages.success(request, "LLM provider preference list saved.")
            if rate_limit_partial_rows:
                messages.warning(
                    request,
                    "Rate limits require both RPM and TPM, or both blank. Cleared limits for: "
                    + ", ".join(sorted(set(rate_limit_partial_rows))),
                )
            for line in ping_results:
                if line.endswith(": OK"):
                    messages.success(request, f"Connectivity check passed — {line}")
                else:
                    messages.warning(request, f"Connectivity check — {line}")
            return redirect(reverse("settings") + "?tab=llm")
        if action == "connect":
            provider = (request.POST.get("provider") or "").strip()
            api_key = (request.POST.get("api_key") or "").strip()
            if not provider or provider not in LLM_PROVIDERS:
                messages.error(request, "Invalid provider.")
            elif not api_key:
                if provider == "Ollama Local":
                    messages.error(request, "Enter the Ollama host/IP before connecting.")
                else:
                    messages.error(request, "Enter an API key before connecting.")
            else:
                try:
                    had_active_provider = bool(_get_active_llm_provider(user, request))
                    api_llm_connect(request, ConnectRequest(provider=provider, api_key=api_key))
                    if not had_active_provider:
                        _set_active_llm_provider(user, provider)
                        request.session["active_llm_provider"] = provider
                        request.session.modified = True
                    request.session.pop("settings_provider_models_map", None)
                    messages.success(request, f"API key for {provider} validated and saved.")
                    from .experience import mark_onboarding_step

                    mark_onboarding_step(user, "llm")
                    return redirect(reverse("settings") + "?tab=llm")
                except HttpError as e:
                    messages.error(request, str(e))
        elif action == "set_active_provider":
            provider = (request.POST.get("active_provider") or "").strip()
            valid_connected = any(info["name"] == provider and info["key_stored"] for info in provider_infos)
            if not valid_connected:
                messages.error(request, "Choose a connected provider.")
            else:
                _set_active_llm_provider(user, provider)
                request.session["active_llm_provider"] = provider
                request.session.modified = True
                messages.success(request, f"{provider} is now the active provider.")
                return redirect(reverse("settings") + "?tab=llm")
        elif action == "save_prompt_model_preferences":
            from .models import TenantPromptModelPreference

            # 1. Global Tenant Default (not settable by tenants in the UI)
            if request.user.is_superuser and "global_default_provider_model" in request.POST:
                global_choice = (request.POST.get("global_default_provider_model") or "").strip()
                if global_choice and "::" in global_choice:
                    g_prov, g_mod = global_choice.split("::", 1)
                    TenantPromptModelPreference.objects.update_or_create(
                        owner=user,
                        query_kind="__default__",
                        defaults={"provider": g_prov, "model": g_mod, "is_active": True},
                    )
                else:
                    TenantPromptModelPreference.objects.for_user(user).filter(query_kind="__default__").delete()

            # 2. Per-Prompt Overrides
            for key, val in request.POST.items():
                if key.startswith("prompt_pref_"):
                    qk = key[len("prompt_pref_") :].strip()
                    val = (val or "").strip()
                    if val and "::" in val:
                        p_prov, p_mod = val.split("::", 1)
                        TenantPromptModelPreference.objects.update_or_create(
                            owner=user,
                            query_kind=qk,
                            defaults={"provider": p_prov, "model": p_mod, "is_active": True},
                        )
                    else:
                        TenantPromptModelPreference.objects.for_user(user).filter(query_kind=qk).delete()

                    if qk == "matching":
                        for alias_qk in (
                            "fit_check",
                            "pipeline_vetting_matching",
                            "jobs_ai_match",
                            "keyword_search_fit",
                            "jobs_match_api",
                        ):
                            if val and "::" in val:
                                p_prov, p_mod = val.split("::", 1)
                                TenantPromptModelPreference.objects.update_or_create(
                                    owner=user,
                                    query_kind=alias_qk,
                                    defaults={"provider": p_prov, "model": p_mod, "is_active": True},
                                )
                            else:
                                TenantPromptModelPreference.objects.for_user(user).filter(query_kind=alias_qk).delete()

            messages.success(request, "Prompt & Workflow model preferences saved successfully.")
            return redirect(reverse("settings") + "?tab=llm")

    tab = (request.GET.get("tab") or "account").strip().lower()
    if tab not in ("account", "llm", "usage", "replacements", "app"):
        tab = "account"
    provider_preference_list = list(_get_provider_preferences(user))
    connected_provider_names = [cfg.provider for cfg in provider_preference_list]
    pref_rows = list(_get_provider_preference_rows(user).order_by("priority", "id"))
    if not pref_rows and connected_provider_names:
        for cfg in provider_preference_list:
            LLMProviderPreference.objects.create(
                provider_config=cfg,
                model=cfg.default_model or "",
                priority=cfg.priority,
            )
        pref_rows = list(_get_provider_preference_rows(user).order_by("priority", "id"))
    raw_session_models = request.session.get("settings_provider_models_map")
    if not isinstance(raw_session_models, dict):
        models_cache: dict[str, list] = {}
    else:
        models_cache = {k: list(v or []) for k, v in raw_session_models.items()}

    def _models_for_settings_preferences(provider: str, cfg: LLMProviderConfig) -> list:
        """
        Model dropdowns use only the session cache populated by
        "Refresh model lists from providers" (no live provider calls on page load).
        """
        return list(models_cache.get(provider) or [])

    provider_preference_rows = []
    provider_models_map: dict[str, list] = {}
    for pref in pref_rows:
        cfg = pref.provider_config
        models = _models_for_settings_preferences(cfg.provider, cfg)
        provider_models_map[cfg.provider] = models
        provider_preference_rows.append({"pref": pref, "cfg": cfg, "models": models})
    for cfg in provider_preference_list:
        if cfg.provider not in provider_models_map:
            provider_models_map[cfg.provider] = _models_for_settings_preferences(cfg.provider, cfg)

    tracks_for_dedupe = list(Track.ensure_baseline(user))
    usage_totals = LLMAppUsageTotals.get_for_user(user)
    stats_map = {(r.provider, r.model): r for r in LLMUsageByModel.objects.for_user(user)}
    usage_rows = []
    usage_cooldown_error = False
    pref_keys_seen = set()
    for item in provider_preference_rows:
        pref = item["pref"]
        cfg = item["cfg"]
        prov = cfg.provider
        mkey = (pref.model or cfg.default_model or "").strip() or "__default__"
        m_gl = (pref.model or cfg.default_model or "").strip() or None
        pref_keys_seen.add((prov, mkey))
        ttl = None
        try:
            ttl = get_llm_cooldown_ttl(prov, m_gl, user=user)
        except Exception:
            usage_cooldown_error = True
        st = stats_map.get((prov, mkey))
        sin = int(st.sum_input_tokens) if st else 0
        sout = int(st.sum_output_tokens) if st else 0
        rc = int(st.request_count) if st else 0
        avg = (sin + sout) // rc if rc else 0
        usage_rows.append(
            {
                "provider": prov,
                "model_display": mkey if mkey != "__default__" else "(default)",
                "priority": pref.priority,
                "on_ice": ttl is not None and ttl > 0,
                "cooldown_seconds": ttl,
                "connected": bool(cfg.encrypted_api_key),
                "request_count": rc,
                "sum_in": sin,
                "sum_out": sout,
                "sum_cached": int(st.sum_cached_tokens) if st else 0,
                "last_used": st.last_used_at if st else None,
                "avg_tokens": avg,
            }
        )
    for st in LLMUsageByModel.objects.for_user(user).order_by("-last_used_at", "provider"):
        if (st.provider, st.model) in pref_keys_seen:
            continue
        ttl = None
        m_gl = None if st.model == "__default__" else st.model
        try:
            ttl = get_llm_cooldown_ttl(st.provider, m_gl, user=user)
        except Exception:
            usage_cooldown_error = True
        rc = int(st.request_count)
        sin, sout = int(st.sum_input_tokens), int(st.sum_output_tokens)
        usage_rows.append(
            {
                "provider": st.provider,
                "model_display": st.model if st.model != "__default__" else "(default)",
                "priority": None,
                "on_ice": ttl is not None and ttl > 0,
                "cooldown_seconds": ttl,
                "connected": True,
                "request_count": rc,
                "sum_in": sin,
                "sum_out": sout,
                "sum_cached": int(st.sum_cached_tokens),
                "last_used": st.last_used_at,
                "avg_tokens": (sin + sout) // rc if rc else 0,
            }
        )
    est_pct = 0.0
    if usage_totals.total_requests:
        est_pct = 100.0 * float(usage_totals.total_estimated_invokes) / float(
            usage_totals.total_requests
        )

    usage_by_query_rows = []
    for r in LLMUsageByQuery.objects.for_user(user).order_by("query_kind", "provider", "model"):
        qk = r.query_kind or ""
        usage_by_query_rows.append(
            {
                "query_kind": qk,
                "query_label": USAGE_QUERY_LABELS.get(
                    qk, qk.replace("_", " ").title() if qk else "—"
                ),
                "provider": r.provider,
                "model_display": r.model if r.model != "__default__" else "(default)",
                "request_count": int(r.request_count),
                "sum_in": int(r.sum_input_tokens),
                "sum_out": int(r.sum_output_tokens),
                "sum_cached": int(r.sum_cached_tokens),
                "last_used": r.last_used_at,
            }
        )

    from .subscriptions import METRIC_LLM_REQUESTS, METRIC_LLM_TOKENS, subscription_summary
    from datetime import timedelta
    from django.utils import timezone
    from .models import UsageCounter

    plan_usage = subscription_summary(user)
    daily_llm_quota = {
        "requests": plan_usage["usage"].get(METRIC_LLM_REQUESTS) or {},
        "tokens": plan_usage["usage"].get(METRIC_LLM_TOKENS) or {},
        "plan_name": plan_usage.get("plan_name") or "—",
    }

    today_date = timezone.localdate()
    start_date = today_date - timedelta(days=30)
    counters = UsageCounter.objects.for_user(user).filter(period_date__gte=start_date).order_by("-period_date", "metric")
    daily_map = {}
    for c in counters:
        d = c.period_date
        if d not in daily_map:
            daily_map[d] = {
                "date": d,
                "is_today": d == today_date,
                "is_yesterday": d == (today_date - timedelta(days=1)),
                "date_display": "Today" if d == today_date else ("Yesterday" if d == (today_date - timedelta(days=1)) else d.strftime("%b %d, %Y")),
                "llm_tokens": 0,
                "llm_requests": 0,
                "job_searches": 0,
                "apply_runs": 0,
                "updated_at": c.updated_at,
            }
        if c.metric == METRIC_LLM_TOKENS:
            daily_map[d]["llm_tokens"] = c.count
        elif c.metric == METRIC_LLM_REQUESTS:
            daily_map[d]["llm_requests"] = c.count
        elif c.metric == "job_searches":
            daily_map[d]["job_searches"] = c.count
        elif c.metric == "apply_runs":
            daily_map[d]["apply_runs"] = c.count
        if c.updated_at and (not daily_map[d]["updated_at"] or c.updated_at > daily_map[d]["updated_at"]):
            daily_map[d]["updated_at"] = c.updated_at

    if today_date not in daily_map:
        daily_map[today_date] = {
            "date": today_date,
            "is_today": True,
            "is_yesterday": False,
            "date_display": "Today",
            "llm_tokens": 0,
            "llm_requests": 0,
            "job_searches": 0,
            "apply_runs": 0,
            "updated_at": None,
        }

    token_limit = daily_llm_quota["tokens"].get("limit") or 0
    request_limit = daily_llm_quota["requests"].get("limit") or 0

    from .models import LLMDailyUsageBreakdown
    from collections import defaultdict

    breakdowns = LLMDailyUsageBreakdown.objects.for_user(user).filter(
        period_date__gte=start_date
    ).order_by("-period_date", "query_kind", "provider", "model")

    daily_breakdowns_map = defaultdict(list)
    for b in breakdowns:
        qk = b.query_kind or ""
        daily_breakdowns_map[b.period_date].append({
            "query_kind": qk,
            "query_label": USAGE_QUERY_LABELS.get(qk, qk.replace("_", " ").title() if qk else "—"),
            "provider": b.provider,
            "model_display": b.model if b.model != "__default__" else "(default)",
            "request_count": int(b.request_count),
            "sum_in": int(b.sum_input_tokens),
            "sum_out": int(b.sum_output_tokens),
            "sum_cached": int(b.sum_cached_tokens),
            "last_used": b.last_used_at,
        })

    daily_usage_rows = []
    for d, item in sorted(daily_map.items(), key=lambda x: x[0], reverse=True):
        t_used = item["llm_tokens"]
        r_used = item["llm_requests"]
        t_pct = round((t_used / token_limit * 100), 1) if token_limit > 0 else 0
        r_pct = round((r_used / request_limit * 100), 1) if request_limit > 0 else 0

        is_tok_exceeded = token_limit > 0 and t_used >= token_limit
        is_req_exceeded = request_limit > 0 and r_used >= request_limit
        is_tok_near = token_limit > 0 and (t_used >= token_limit * 0.8) and not is_tok_exceeded
        is_req_near = request_limit > 0 and (r_used >= request_limit * 0.8) and not is_req_exceeded

        item["token_limit"] = token_limit
        item["token_pct"] = t_pct
        item["request_limit"] = request_limit
        item["request_pct"] = r_pct
        item["is_exceeded"] = is_tok_exceeded or is_req_exceeded
        item["is_near_limit"] = (is_tok_near or is_req_near) and not item["is_exceeded"]

        if not token_limit and not request_limit:
            status_label = "Unlimited"
            status_badge = "bg-slate-100 text-slate-600"
        elif is_tok_exceeded and is_req_exceeded:
            status_label = f"Exceeded (Req {r_pct}%, Tok {t_pct}%)"
            status_badge = "bg-red-100 text-red-800 font-semibold"
        elif is_tok_exceeded:
            status_label = f"Tokens Exceeded ({t_pct}%)"
            status_badge = "bg-red-100 text-red-800 font-semibold"
        elif is_req_exceeded:
            status_label = f"Requests Exceeded ({r_pct}%)"
            status_badge = "bg-red-100 text-red-800 font-semibold"
        elif is_tok_near or is_req_near:
            max_p = max(t_pct, r_pct)
            status_label = f"Near Limit ({max_p}%)"
            status_badge = "bg-amber-100 text-amber-800 font-medium"
        else:
            max_p = max(t_pct, r_pct)
            status_label = f"OK ({max_p}%)"
            status_badge = "bg-emerald-50 text-emerald-700 font-medium"

        item["status_label"] = status_label
        item["status_badge"] = status_badge
        item["breakdown_rows"] = daily_breakdowns_map.get(d, [])
        item["has_breakdown"] = len(item["breakdown_rows"]) > 0
        daily_usage_rows.append(item)

    tenant_usage_rows = []
    if request.user.is_staff:
        from collections import defaultdict
        tenant_usage = defaultdict(lambda: {
            "username": "",
            "email": "",
            "local_requests": 0,
            "local_input_tokens": 0,
            "local_output_tokens": 0,
            "remote_requests": 0,
            "remote_input_tokens": 0,
            "remote_output_tokens": 0,
        })
        # Preload local preferences to find is_local status efficiently
        local_prefs = set(
            LLMProviderPreference.objects.filter(is_local=True).values_list(
                "provider_config__owner_id", "provider_config__provider", "model"
            )
        )
        all_stats = LLMUsageByModel.objects.select_related("owner").all()
        for st in all_stats:
            o_id = st.owner_id
            t = tenant_usage[o_id]
            if not t["username"]:
                t["username"] = st.owner.username
                t["email"] = st.owner.email or ""

            # check if local
            is_local = False
            if str(st.provider).strip().lower() == "ollama local":
                is_local = True
            else:
                key = (o_id, st.provider, st.model)
                if key in local_prefs:
                    is_local = True
                elif (o_id, st.provider, "__default__") in local_prefs:
                    is_local = True

            if is_local:
                t["local_requests"] += st.request_count
                t["local_input_tokens"] += st.sum_input_tokens
                t["local_output_tokens"] += st.sum_output_tokens
            else:
                t["remote_requests"] += st.request_count
                t["remote_input_tokens"] += st.sum_input_tokens
                t["remote_output_tokens"] += st.sum_output_tokens

        tenant_usage_rows = sorted(tenant_usage.values(), key=lambda x: x["username"])

    optimizer_supporting_context = _get_optimizer_supporting_context(request)
    automation_for_replacements = AppAutomationSettings.get_for_user(user)
    raw_replacements = automation_for_replacements.export_replacements or []
    # One-time migrate legacy session-only tokens into durable settings.
    if not any(
        isinstance(e, dict) and (e.get("token") or "").strip() and (e.get("value") or "").strip()
        for e in (raw_replacements if isinstance(raw_replacements, list) else [])
    ):
        session_raw = request.session.get("export_replacements") or []
        if isinstance(session_raw, list) and any(
            isinstance(e, dict) and (e.get("token") or "").strip() and (e.get("value") or "").strip()
            for e in session_raw
        ):
            raw_replacements = automation_for_replacements.set_export_replacements(session_raw)
    replacement_entries = AppAutomationSettings.normalize_export_replacements(raw_replacements)

    from .models import UserExperienceSettings
    from .experience import is_power_user
    from .account_views import account_settings_context
    from .prompt_store import list_optimizer_workflows

    from .models import TenantPromptModelPreference
    from .llm.gateway import (
        USAGE_QUERY_OPTIMIZER_WRITER,
        USAGE_QUERY_OPTIMIZER_ATS_JUDGE,
        USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE,
        USAGE_QUERY_JD_CLEANSE,
        USAGE_QUERY_MATCHING,
        USAGE_QUERY_FIT_CHECK,
        USAGE_QUERY_PIPELINE_VETTING,
        USAGE_QUERY_PIPELINE_SKILL_EXTRACT,
        USAGE_QUERY_COVER_LETTER,
        USAGE_QUERY_INTERVIEW_PREP,
    )

    available_model_options = []
    seen_opt_keys = set()
    for item in provider_preference_rows:
        cfg = item["cfg"]
        pref = item["pref"]
        prov = cfg.provider
        m = (pref.model or cfg.default_model or "").strip()
        k = (prov, m)
        if k not in seen_opt_keys:
            seen_opt_keys.add(k)
            label = f"{prov} — {m}" if m else f"{prov} (default model)"
            available_model_options.append({
                "value": f"{prov}::{m}",
                "label": label,
                "provider": prov,
                "model": m,
                "is_local": pref.is_local or (prov.lower() == "ollama local"),
            })

    for prov, mlist in provider_models_map.items():
        for m in (mlist or [])[:20]:
            k = (prov, m)
            if k not in seen_opt_keys:
                seen_opt_keys.add(k)
                available_model_options.append({
                    "value": f"{prov}::{m}",
                    "label": f"{prov} — {m}",
                    "provider": prov,
                    "model": m,
                    "is_local": prov.lower() == "ollama local",
                })

    stored_prompt_prefs = {
        p.query_kind: f"{p.provider}::{p.model}"
        for p in TenantPromptModelPreference.objects.for_user(user).filter(is_active=True)
    }
    global_default_val = stored_prompt_prefs.get("__default__", "")

    prompt_routing_sections = [
        {
            "category": "Resume Optimizer",
            "description": "Generation, refinement, and scoring models used during resume optimization workflows.",
            "items": [
                {
                    "query_kind": USAGE_QUERY_OPTIMIZER_WRITER,
                    "label": "Resume Writer",
                    "hint": "Rewrites experience bullets, summary, and achievements against targeted job description.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_OPTIMIZER_WRITER, ""),
                },
                {
                    "query_kind": USAGE_QUERY_OPTIMIZER_ATS_JUDGE,
                    "label": "ATS Rubric Judge",
                    "hint": "Scoring judge for keyword overlap, formatting heuristics, and parseability.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_OPTIMIZER_ATS_JUDGE, ""),
                },
                {
                    "query_kind": USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE,
                    "label": "Recruiter Rubric Judge",
                    "hint": "Scoring judge for impact, relevance, and executive clarity.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE, ""),
                },
                {
                    "query_kind": USAGE_QUERY_JD_CLEANSE,
                    "label": "Job Description Cleanser",
                    "hint": "Cleans raw job postings into concise requirements, removing company boilerplate.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_JD_CLEANSE, ""),
                },
            ],
        },
        {
            "category": "Job Matching & Sourcing",
            "description": "High-volume evaluation prompts for search results, candidate pipelines, and skill intelligence.",
            "items": [
                {
                    "query_kind": USAGE_QUERY_MATCHING,
                    "label": "Job Fit & Alignment Matcher",
                    "hint": "Unified engine for job match scoring, fit diagnostics, and pipeline candidate vetting.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_MATCHING, ""),
                },
                {
                    "query_kind": USAGE_QUERY_PIPELINE_SKILL_EXTRACT,
                    "label": "Pipeline Skill & Market Extraction",
                    "hint": "Extracts market-wide skill taxonomy, tools, and methodologies from batches of job descriptions.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_PIPELINE_SKILL_EXTRACT, ""),
                },
            ],
        },
        {
            "category": "Career Assets & Prep",
            "description": "Tailored career asset generation and interview preparation prompts.",
            "items": [
                {
                    "query_kind": USAGE_QUERY_COVER_LETTER,
                    "label": "Cover Letter Generator",
                    "hint": "Drafts customized, professional cover letters tailored to optimized resumes.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_COVER_LETTER, ""),
                },
                {
                    "query_kind": USAGE_QUERY_INTERVIEW_PREP,
                    "label": "Interview Question Prep",
                    "hint": "Generates likely behavioral and technical interview questions for targeted roles.",
                    "selected": stored_prompt_prefs.get(USAGE_QUERY_INTERVIEW_PREP, ""),
                },
            ],
        },
    ]

    context = {
        "provider_infos": provider_infos,
        "provider_preference_list": provider_preference_list,
        "provider_preference_rows": provider_preference_rows,
        "connected_provider_names": connected_provider_names,
        "provider_models_map": provider_models_map,
        "active_provider": active_provider,
        "settings_tab": tab,
        "app_automation": AppAutomationSettings.get_for_user(user),
        "experience_settings": UserExperienceSettings.get_for_user(user),
        "is_power_user": is_power_user(user),
        "optimizer_workflows": list_optimizer_workflows(user),
        "optimizer_supporting_context": optimizer_supporting_context,
        "dedupe_tracks": tracks_for_dedupe,
        "usage_totals": usage_totals,
        "usage_rows": usage_rows,
        "usage_estimated_pct": est_pct,
        "usage_cooldown_error": usage_cooldown_error,
        "usage_by_query_rows": usage_by_query_rows,
        "daily_llm_quota": daily_llm_quota,
        "daily_usage_rows": daily_usage_rows,
        "replacement_entries": replacement_entries,
        "tenant_usage_rows": tenant_usage_rows,
        "available_model_options": available_model_options,
        "global_default_val": global_default_val,
        "prompt_routing_sections": prompt_routing_sections,
    }
    context.update(account_settings_context(user))
    return render(request, "resume_app/settings.html", context)


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


@_staff_required
def prompt_library_view(request):
    """
    Staff-only Prompt Library: edit system-wide Writer / Recruiter / Matching /
    Insights / Cover letter / Interview prep / JD cleanse templates.
    ATS judge prompts are managed as global AtsJudgeProfile rows (owner=null).
    """
    from .models import AtsJudgeProfile
    from .prompt_store import (
        get_ats_judge_profile_by_id,
        get_ats_judge_profile_display,
        list_ats_judge_profiles,
        save_ats_judge_profile,
    )

    try:
        prompts = get_effective_prompts(request)
    except Exception:
        prompts = get_effective_prompts(None)

    ats_profiles = list_ats_judge_profiles()
    selected_ats_id = request.GET.get("ats_profile")
    if selected_ats_id:
        try:
            selected_ats_id = int(selected_ats_id)
        except (ValueError, TypeError):
            selected_ats_id = None
    global_ats_ids = {p.pk for p in ats_profiles}
    if selected_ats_id not in global_ats_ids:
        selected_ats_id = None
    if not selected_ats_id and ats_profiles:
        default_ats = next((p for p in ats_profiles if p.is_default), None) or ats_profiles[0]
        selected_ats_id = default_ats.pk
    selected_ats = None
    ats_prompts = {}
    if selected_ats_id:
        selected_ats = get_ats_judge_profile_by_id(selected_ats_id)
        if selected_ats:
            ats_prompts = get_ats_judge_profile_display(selected_ats)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_ats_profile":
            profile_id = request.POST.get("ats_profile_id")
            name = (request.POST.get("ats_profile_name") or "").strip()
            a_sys = request.POST.get("prompt_ats_system") or ""
            a_usr = request.POST.get("prompt_ats_user") or ""
            a_combined = request.POST.get("prompt_ats_combined") or ""

            def _ats_triple(sys_v: str, usr_v: str, leg_v: str) -> tuple[str, str, str]:
                sys_v = (sys_v or "").strip()
                usr_v = (usr_v or "").strip()
                leg_v = (leg_v or "").strip()
                if sys_v or usr_v:
                    return "", sys_v, usr_v
                if leg_v:
                    return leg_v, "", ""
                return "", "", ""

            ats_leg, ats_sys, ats_usr = _ats_triple(a_sys, a_usr, a_combined)
            is_default = request.POST.get("ats_profile_is_default") == "on"
            if profile_id and str(profile_id).strip().isdigit():
                prof = get_ats_judge_profile_by_id(int(profile_id))
                if prof is None:
                    messages.error(request, "ATS profile not found.")
                    return redirect(reverse("prompt_library"))
            else:
                prof = AtsJudgeProfile(owner=None, name=name or "New ATS profile")
            save_ats_judge_profile(
                prof,
                name=name or prof.name,
                ats_judge=ats_leg,
                ats_judge_system=ats_sys,
                ats_judge_user=ats_usr,
            )
            if is_default:
                prof.is_default = True
                prof.save()
            messages.success(request, f'ATS profile "{prof.name}" saved.')
            return redirect(reverse("prompt_library") + f"?ats_profile={prof.pk}")

        if action == "new_ats_profile":
            prof = AtsJudgeProfile.objects.create(owner=None, name="New ATS profile", slug="")
            return redirect(reverse("prompt_library") + f"?ats_profile={prof.pk}")

        if action == "delete_ats_profile":
            profile_id = request.POST.get("ats_profile_id")
            if profile_id and str(profile_id).strip().isdigit():
                prof = get_ats_judge_profile_by_id(int(profile_id))
                if prof is None:
                    messages.error(request, "ATS profile not found.")
                elif prof.is_builtin and AtsJudgeProfile.objects.filter(owner__isnull=True).count() <= 1:
                    messages.error(request, "Cannot delete the only built-in ATS profile.")
                else:
                    was_default = prof.is_default
                    name = prof.name
                    prof.delete()
                    if was_default:
                        fallback = AtsJudgeProfile.objects.filter(owner__isnull=True).order_by("pk").first()
                        if fallback:
                            fallback.is_default = True
                            fallback.save()
                    messages.success(request, f'ATS profile "{name}" deleted.')
            return redirect(reverse("prompt_library"))

        if action == "save_prompts":
            # Split (system/user) wins over Combined (legacy) when either side has text.
            def _prompt_triple(sys_v: str, usr_v: str, leg_v: str) -> tuple[str, str, str]:
                sys_v = (sys_v or "").strip()
                usr_v = (usr_v or "").strip()
                leg_v = (leg_v or "").strip()
                if sys_v or usr_v:
                    return "", sys_v, usr_v
                if leg_v:
                    return leg_v, "", ""
                return "", "", ""

            w_sys = request.POST.get("prompt_writer_system") or ""
            w_usr = request.POST.get("prompt_writer_user") or ""
            w_combined = request.POST.get("prompt_writer_combined") or ""
            writer_leg, writer_sys, writer_usr = _prompt_triple(w_sys, w_usr, w_combined)

            r_sys = request.POST.get("prompt_recruiter_system") or ""
            r_usr = request.POST.get("prompt_recruiter_user") or ""
            r_combined = request.POST.get("prompt_recruiter_combined") or ""
            rec_leg, rec_sys, rec_usr = _prompt_triple(r_sys, r_usr, r_combined)

            m_sys = request.POST.get("prompt_matching_system") or ""
            m_usr = request.POST.get("prompt_matching_user") or ""
            m_combined = request.POST.get("prompt_matching_combined") or ""
            match_leg, match_sys, match_usr = _prompt_triple(m_sys, m_usr, m_combined)

            i_sys = request.POST.get("prompt_insights_system") or ""
            i_usr = request.POST.get("prompt_insights_user") or ""
            i_combined = request.POST.get("prompt_insights_combined") or ""
            ins_leg, ins_sys, ins_usr = _prompt_triple(i_sys, i_usr, i_combined)

            jd_sys = request.POST.get("prompt_jd_cleanse_system") or ""
            jd_usr = request.POST.get("prompt_jd_cleanse_user") or ""
            jd_combined = request.POST.get("prompt_jd_cleanse_combined") or ""
            jd_leg, jd_sys, jd_usr = _prompt_triple(jd_sys, jd_usr, jd_combined)

            cl_sys = request.POST.get("prompt_cover_letter_system") or ""
            cl_usr = request.POST.get("prompt_cover_letter_user") or ""
            cl_combined = request.POST.get("prompt_cover_letter_combined") or ""
            cl_leg, cl_sys, cl_usr = _prompt_triple(cl_sys, cl_usr, cl_combined)

            ip_sys = request.POST.get("prompt_interview_prep_system") or ""
            ip_usr = request.POST.get("prompt_interview_prep_user") or ""
            ip_combined = request.POST.get("prompt_interview_prep_combined") or ""
            ip_leg, ip_sys, ip_usr = _prompt_triple(ip_sys, ip_usr, ip_combined)

            sr_sys = request.POST.get("prompt_skill_radar_system") or ""
            sr_usr = request.POST.get("prompt_skill_radar_user") or ""
            sr_combined = request.POST.get("prompt_skill_radar_combined") or ""
            sr_leg, sr_sys, sr_usr = _prompt_triple(sr_sys, sr_usr, sr_combined)

            prompts = {
                "writer": writer_leg,
                "writer_system": writer_sys,
                "writer_user": writer_usr,
                "recruiter_judge": rec_leg,
                "recruiter_judge_system": rec_sys,
                "recruiter_judge_user": rec_usr,
                "matching": match_leg,
                "matching_system": match_sys,
                "matching_user": match_usr,
                "insights": ins_leg,
                "insights_system": ins_sys,
                "insights_user": ins_usr,
                "jd_cleanse": jd_leg,
                "jd_cleanse_system": jd_sys,
                "jd_cleanse_user": jd_usr,
                "cover_letter": cl_leg,
                "cover_letter_system": cl_sys,
                "cover_letter_user": cl_usr,
                "interview_prep": ip_leg,
                "interview_prep_system": ip_sys,
                "interview_prep_user": ip_usr,
                "skill_radar": sr_leg,
                "skill_radar_system": sr_sys,
                "skill_radar_user": sr_usr,
            }
            save_prompts_to_profile(request, prompts)
            prompts = get_effective_prompts(request)
            messages.success(
                request,
                "System prompts saved. All users will use these for the Resume Optimizer, "
                "Job Search, vetting, cover letter, interview prep, and JD cleansing.",
            )
            return redirect(reverse("prompt_library"))
        if action == "reset_prompts":
            try:
                clear_all_prompts_in_profile(request)
                messages.success(request, "Prompts reset to code defaults.")
                return redirect(reverse("prompt_library"))
            except Exception as e:
                messages.error(request, f"Could not reset prompts: {e}")

    return render(
        request,
        "resume_app/prompt_library.html",
        {
            "prompts": prompts,
            "ats_profiles": ats_profiles,
            "selected_ats": selected_ats,
            "ats_prompts": ats_prompts,
        },
    )


# Step ids for workflow builder (must match agents.VALID_STEP_IDS)
WORKFLOW_STEP_IDS = ["writer", "ats_judge", "recruiter_judge", "jd_cleanse"]
WORKFLOW_STEP_LABELS = {
    "writer": "Writer",
    "ats_judge": "ATS Judge",
    "recruiter_judge": "Recruiter Judge",
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
                request.session["job_search_llm_model"] = job_search_llm_model or ""
                request.session.modified = True
    except Exception:
        pass

    search_results = None
    saved_results = None
    excluded_results = None
    matches = None
    disqualifier_prompt = None
    current_disqualifiers = []

    # Track / profile: dynamic list (e.g. IC vs Management vs custom).
    tracks_qs = Track.ensure_baseline(request.user)
    from .models import SearchProfile

    available_slugs = set(tracks_qs.values_list("slug", flat=True))
    available_slugs.update(
        SearchProfile.objects.for_user(request.user).values_list("slug", flat=True)
    )
    raw_track_param = (
        (request.GET.get("profile") or request.GET.get("track") or "").strip().lower()
    )
    if raw_track_param:
        raw_track = raw_track_param
    elif applied_preset.get("profile_slug"):
        raw_track = applied_preset["profile_slug"].strip().lower()
    else:
        raw_track = (
            request.session.get("job_search_profile_slug")
            or request.session.get("job_search_track")
            or ""
        ).strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug(request.user)

    # Resume -> track association (default only):
    # If a resume has a stored track AND the user did not explicitly pick a track in this request,
    # default the page track to the resume's stored track.
    if resume_id_val is not None and not raw_track_param:
        try:
            from .models import UserResume

            selected_resume = _user_library_resumes(request.user).filter(id=resume_id_val).first()
            if selected_resume and selected_resume.track and selected_resume.track in available_slugs:
                raw_track = selected_resume.track
        except Exception:
            # Best-effort; never block job search.
            pass
    request.session["job_search_track"] = raw_track
    request.session["job_search_profile_slug"] = raw_track
    request.session.modified = True

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
        "job_search_track_label": next(
            (t.label for t in tracks_qs if t.slug == raw_track),
            raw_track,
        ),
        "job_tracks": list(tracks_qs),
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


def job_tasks_view(request):
    """Decommissioned in favor of Career Cockpit Search Schedule. Redirects to career_cockpit."""
    track = (request.GET.get("track") or "").strip()
    cockpit_url = reverse("career_cockpit")
    if track:
        cockpit_url += f"?track={track}"
    return redirect(cockpit_url)



@_staff_required
def huey_dashboard_view(request):
    """UI for monitoring Huey queue depth and controlling periodic tasks (pause/restore)."""

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    queue_stats = None
    queue_stats_error = None
    if not immediate:
        try:
            storage = HUEY.storage
            queue_stats = {
                "pending": storage.queue_size(),
                "scheduled": storage.schedule_size(),
                "results": storage.result_store_size(),
            }
        except Exception as e:
            queue_stats_error = str(e)

    periodic_rows: list[dict[str, object]] = []
    now = timezone.now()
    for info in PERIODIC_TASKS:
        task_fn_name = info["task_fn_name"]
        wrapper = get_periodic_task_wrapper(task_fn_name)
        if wrapper is None:
            # Shouldn't happen unless tasks.py was modified.
            continue

        is_revoked = False
        try:
            is_revoked = bool(wrapper.is_revoked())
        except Exception:
            is_revoked = False

        next_run_at = None
        try:
            next_run_at = get_next_run_at(info["cron_string"], from_time=now)
        except Exception:
            next_run_at = None

        periodic_rows.append(
            {
                "task_fn_name": task_fn_name,
                "display_name": info["display_name"],
                "cron_string": info["cron_string"],
                "schedule_description": cron_to_short_description(info["cron_string"]),
                "basic": info.get("basic") or info.get("description") or "",
                "advanced": info.get("advanced") or "",
                "is_revoked": is_revoked,
                "next_run_at": next_run_at,
            }
        )

    from datetime import timedelta
    twenty_four_hours_ago = now - timedelta(hours=24)
    searches_24h = JobSearchTaskRun.objects.filter(started_at__gte=twenty_four_hours_ago).count()
    failed_24h = JobSearchTaskRun.objects.filter(started_at__gte=twenty_four_hours_ago, status="failed").count()
    active_running = JobSearchTaskRun.objects.filter(status="running").count()

    from .tasks import CLEANUP_STATUS_CACHE_KEY
    cleanup_status = cache.get(CLEANUP_STATUS_CACHE_KEY)

    adhoc_rows: list[dict[str, str]] = []
    for info in ADHOC_RUN_NOW_TASKS:
        adhoc_rows.append(
            {
                "task_fn_name": info["task_fn_name"],
                "display_name": info["display_name"],
                "basic": info.get("basic") or "",
                "advanced": info.get("advanced") or "",
            }
        )

    # Build recent activity items from search scrapes
    recent_searches = list(
        JobSearchTaskRun.objects.select_related("task", "task__owner")
        .order_by("-started_at")[:20]
    )

    unified_activity = []
    for s in recent_searches:
        user_name = s.task.owner.get_username() if s.task and s.task.owner else "System"
        title = s.task.name if s.task else "Job Search"
        subtitle = s.task.search_term if s.task else ""
        unified_activity.append({
            "type": "Search Scrape",
            "type_badge": "bg-sky-50 text-sky-700 border-sky-200",
            "title": title,
            "subtitle": subtitle,
            "user": user_name,
            "status": s.status,  # completed, failed, running
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "items_count": getattr(s, "jobs_fetched", 0),
            "error_message": s.error_message,
        })

    unified_activity.sort(key=lambda x: x["started_at"] or now, reverse=True)
    unified_activity = unified_activity[:25]

    context = {
        "immediate": immediate,
        "queue_stats": queue_stats,
        "queue_stats_error": queue_stats_error,
        "periodic_tasks": periodic_rows,
        "adhoc_run_tasks": adhoc_rows,
        "recent_runs": recent_searches[:10],
        "unified_activity": unified_activity,
        "searches_24h": searches_24h,
        "applies_24h": 0,
        "failed_24h": failed_24h,
        "active_running": active_running,
        "cleanup_status": cleanup_status,
    }
    return render(request, "resume_app/huey_dashboard.html", context)


@_staff_required
def huey_periodic_revoke_view(request, task_name: str):
    """Pause a specific Huey periodic task via revoke()."""

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; periodic pause controls are disabled.")
        return redirect("huey_dashboard")

    info = get_periodic_task_info(task_name)
    if not info:
        messages.error(request, f"Unknown periodic task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Periodic task not found: {task_name}")
        return redirect("huey_dashboard")

    try:
        wrapper.revoke()
    except Exception as e:
        messages.error(request, f"Failed to pause task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, f'Paused: {info["display_name"]}')
    return redirect("huey_dashboard")


@_staff_required
def huey_periodic_restore_view(request, task_name: str):
    """Restore a specific Huey periodic task via restore()."""

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; periodic restore controls are disabled.")
        return redirect("huey_dashboard")

    info = get_periodic_task_info(task_name)
    if not info:
        messages.error(request, f"Unknown periodic task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Periodic task not found: {task_name}")
        return redirect("huey_dashboard")

    try:
        wrapper.restore()
    except Exception as e:
        messages.error(request, f"Failed to restore task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, f'Restored: {info["display_name"]}')
    return redirect("huey_dashboard")


@_staff_required
def huey_flush_queue_view(request):
    """Flush pending Huey queue (destructive)."""

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; there is no Redis queue to flush.")
        return redirect("huey_dashboard")

    confirm = (request.POST.get("confirm") or "").strip().lower()
    if confirm != "yes":
        messages.error(request, "Queue flush cancelled (missing confirmation).")
        return redirect("huey_dashboard")

    try:
        storage = HUEY.storage
        storage.flush_queue()
    except Exception as e:
        messages.error(request, f"Failed to flush queue: {e}")
        return redirect("huey_dashboard")

    messages.success(request, "Huey queue flushed (pending tasks removed).")
    return redirect("huey_dashboard")


@_staff_required
def huey_run_cleanup_now_view(request):
    """Enqueue Cleanup Manager (dedupe + retention + inactive check) immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        from .tasks import cleanup_manager

        cleanup_manager()
    except Exception as e:
        messages.error(request, f"Failed to queue cleanup task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, "Cleanup Manager queued (dedupe, retention purge, inactive Applying check).")
    return redirect("huey_dashboard")


@_staff_required
def huey_task_run_now_view(request, task_name: str):
    """Enqueue a periodic Huey task or an ad-hoc @db_task that needs no arguments."""

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if task_name not in run_now_task_names():
        messages.error(request, f"Unknown or unsupported on-demand task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Task not found in resume_app.tasks: {task_name}")
        return redirect("huey_dashboard")

    label = get_run_now_display_name(task_name)
    try:
        wrapper()
    except Exception as e:
        messages.error(request, f'Failed to queue "{label}": {e}')
        return redirect("huey_dashboard")

    messages.success(request, f"Queued: {label}")
    return redirect("huey_dashboard")


def job_task_create_view(request):
    """Create a new job search task. Sets next_run_at from cron."""
    from .saved_searches import get_saved_search_or_none

    tracks_qs = Track.ensure_baseline(request.user)
    track_list = list(tracks_qs)
    default_track = Track.get_default_slug(request.user)
    preset = get_saved_search_or_none(request.user, request.GET.get("preset"))
    if request.method != "POST":
        if preset:
            context = {
                "task": None,
                "form_track": preset.profile_slug or default_track,
                "job_tracks": track_list,
                "form_frequency": "0 9 * * *",
                "form_jobs_to_fetch": preset.results_wanted or 50,
                "form_site_name": normalize_site_names(
                    preset.site_names if isinstance(preset.site_names, list) else None
                ),
                "form_start_time": "",
                "form_name": preset.name,
                "form_search_term": preset.search_term,
                "form_location": preset.location,
            }
        else:
            context = {
                "task": None,
                "form_track": default_track,
                "job_tracks": track_list,
                "form_frequency": "0 9 * * *",
                "form_jobs_to_fetch": 50,
                "form_site_name": list(DEFAULT_SITE_NAMES),
                "form_start_time": "",
                "form_name": "",
                "form_search_term": "",
                "form_location": "",
            }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, default_track, {t.slug for t in track_list})
    if errs:
        for e in errs:
            messages.error(request, e)
        return redirect("job_task_create")

    task = JobSearchTask(
        owner=request.user,
        name=data["name"],
        search_term=data["search_term"],
        location=data["location"],
        track=data["track"],
        jobs_to_fetch=data["jobs_to_fetch"],
        frequency=data["frequency"],
        start_time=data["start_time"],
        site_name=data["site_name"],
        is_active=True,
    )
    try:
        task.full_clean()
    except ValidationError as e:
        for _k, v in e.message_dict.items():
            for msg in (v if isinstance(v, list) else [v]):
                messages.error(request, msg)
        return redirect("job_task_create")
    task.next_run_at = get_next_run_at(task.frequency)
    task.save()
    messages.success(request, f"Task \"{task.name or task.search_term}\" created. Next run: {task.next_run_at}")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)


def job_task_edit_view(request, task_id):
    """Edit a job search task. Optionally update next_run_at."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    tracks_qs = Track.ensure_baseline(request.user)
    track_list = list(tracks_qs)
    if request.method != "POST":
        context = {
            "task": task,
            "form_track": task.track,
            "job_tracks": track_list,
            "form_frequency": task.frequency,
            "form_jobs_to_fetch": task.jobs_to_fetch,
            "form_site_name": normalize_site_names(
                task.site_name if isinstance(task.site_name, list) else None
            ),
            "form_start_time": task.start_time.strftime("%H:%M") if task.start_time else "",
        }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, Track.get_default_slug(request.user), {t.slug for t in track_list})
    if errs:
        for e in errs:
            messages.error(request, e)
        return redirect("job_task_edit", task_id=task_id)
    task.name = data["name"]
    task.search_term = data["search_term"]
    task.location = data["location"]
    task.track = data["track"]
    task.jobs_to_fetch = data["jobs_to_fetch"]
    task.frequency = data["frequency"]
    task.start_time = data["start_time"]
    task.site_name = data["site_name"]
    try:
        task.full_clean()
    except ValidationError as e:
        for _k, v in e.message_dict.items():
            for msg in (v if isinstance(v, list) else [v]):
                messages.error(request, msg)
        return redirect("job_task_edit", task_id=task_id)
    task.save()
    messages.success(request, "Task updated.")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)


def job_task_run_now_view(request, task_id):
    """Enqueue run_job_search_task once with 60-min cooldown and concurrency check."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or request.content_type == "application/json"
    )

    track_slug = task.track or (task.saved_search.slug if task.saved_search else "")
    cockpit_url = reverse("career_cockpit")
    if track_slug:
        cockpit_url += f"?track={track_slug}"
    redirect_url = request.POST.get("next") or request.GET.get("next") or cockpit_url

    from django.core.cache import cache
    import time
    from .tasks import get_job_search_task_lock_key
    from .models import JobSearchTaskRun

    # 1. Check if a search task is already actively running for this user
    lock_key = get_job_search_task_lock_key(request.user.id)
    is_running = bool(cache.get(lock_key)) or task.runs.filter(status=JobSearchTaskRun.STATUS_RUNNING).exists()
    if is_running:
        msg = "A search task is already actively running for your account. Please wait for it to complete."
        if is_ajax:
            return JsonResponse({"status": "running", "message": msg}, status=409)
        messages.warning(request, msg)
        return redirect(redirect_url)

    # 2. Check 60-minute manual cooldown (cleared on failed run so user can retry immediately)
    COOLDOWN_SECONDS = 3600
    cooldown_key = f"job_task_manual_cooldown:{task.id}"
    last_run = task.runs.first()
    if last_run and last_run.status == JobSearchTaskRun.STATUS_FAILED and not is_running:
        cache.delete(cooldown_key)
        expires_at = None
    else:
        expires_at = cache.get(cooldown_key)

    now = time.time()
    if expires_at and expires_at > now:
        remaining = int(expires_at - now)
        mins = max(1, (remaining + 59) // 60)
        msg = f"Task is on cooldown. Please wait {mins} minute{'s' if mins != 1 else ''} before running again."
        if is_ajax:
            return JsonResponse({
                "status": "cooldown",
                "message": msg,
                "cooldown_remaining_seconds": remaining,
            }, status=429)
        messages.warning(request, msg)
        return redirect(redirect_url)

    # 3. Set cooldown, mark pending, and enqueue task
    cache.set(cooldown_key, now + COOLDOWN_SECONDS, timeout=COOLDOWN_SECONDS)
    cache.set(f"job_task_pending:{task.id}", now, timeout=180)
    run_job_search_task(task.owner_id, task.id)

    task_label = task.name or (task.saved_search.name if task.saved_search else None) or task.search_term
    msg = f'Search "{task_label}" queued to run now.'
    if is_ajax:
        return JsonResponse({
            "status": "queued",
            "message": msg,
            "task_id": task.id,
            "cooldown_remaining_seconds": COOLDOWN_SECONDS,
        })

    messages.success(request, msg)
    return redirect(redirect_url)


def job_task_status_view(request, task_id):
    """Return JSON status of a JobSearchTask (running state, last run, cooldown)."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    from django.core.cache import cache
    from django.utils.timesince import timesince
    import time
    from .tasks import get_job_search_task_lock_key
    from .models import JobSearchTaskRun

    is_pending = bool(cache.get(f"job_task_pending:{task.id}"))
    lock_key = get_job_search_task_lock_key(request.user.id)
    is_running = is_pending or bool(cache.get(lock_key)) or task.runs.filter(status=JobSearchTaskRun.STATUS_RUNNING).exists()

    cooldown_key = f"job_task_manual_cooldown:{task.id}"
    last_run = task.runs.first()
    if last_run and last_run.status == JobSearchTaskRun.STATUS_FAILED and not is_running:
        cache.delete(cooldown_key)
        cooldown_remaining = 0
    else:
        expires_at = cache.get(cooldown_key)
        now = time.time()
        cooldown_remaining = max(0, int(expires_at - now)) if (expires_at and expires_at > now) else 0

    last_run = task.runs.first()
    last_run_data = None
    if last_run:
        last_run_data = {
            "id": last_run.id,
            "status": last_run.status,
            "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
            "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
            "jobs_fetched": last_run.jobs_fetched,
            "jobs_added": last_run.jobs_added_to_pipeline,
            "jobs_eliminated": last_run.jobs_eliminated,
            "has_details": bool(last_run.details),
            "date_display": last_run.started_at.strftime("%b %d, %Y · %I:%M %p") if last_run.started_at else "",
            "timesince": timesince(last_run.started_at) if last_run.started_at else "",
            "json_url": reverse("scheduled_run_details_json", args=[last_run.id]),
            "csv_url": reverse("download_scheduled_run_csv", args=[last_run.id]),
        }

    return JsonResponse({
        "status": "ok",
        "task_id": task.id,
        "is_running": is_running,
        "cooldown_remaining_seconds": cooldown_remaining,
        "last_run": last_run_data,
    })


def job_task_toggle_active_view(request, task_id):
    """Toggle is_active and redirect to Cockpit."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    task.is_active = not task.is_active
    task.save()
    status = "activated" if task.is_active else "paused"
    messages.success(request, f"Task \"{task.name or task.search_term}\" {status}.")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)



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


@login_required
def download_scheduled_run_csv_view(request, run_id: int):
    """Download CSV audit log of all fetched jobs for a scheduled task run."""
    import csv
    from django.http import HttpResponse, Http404
    from django.shortcuts import get_object_or_404
    from django.utils.text import slugify
    from .models import JobSearchTaskRun

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
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from .models import JobSearchTaskRun

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
    request.session["job_search_track"] = raw_track
    request.session["job_search_profile_slug"] = raw_track
    request.session.modified = True

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
    request.session["job_search_track"] = raw_track
    request.session["job_search_profile_slug"] = raw_track
    request.session.modified = True

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

    request.session["job_search_track"] = raw_track
    request.session["job_search_profile_slug"] = raw_track
    request.session.modified = True

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
        opt = entry.optimized_resumes.filter(status="completed").exclude(optimized_content="").order_by("-updated_at").first()
        if opt and (opt.optimized_content or "").strip():
            markdown = opt.optimized_content
            entry.applied_resume_markdown = markdown
            entry.applied_optimized_resume = opt
            entry.save(update_fields=["applied_resume_markdown", "applied_optimized_resume"])

    from .utils import sanitize_resume_markdown
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
    from django.http import Http404, HttpResponse
    from .api import _apply_export_replacements, _build_export_pdf, _export_file_response, _format_export_filename
    from .utils import sanitize_resume_markdown

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
        return HttpResponse("PDF export requires reportlab; install with: pip install reportlab", status=503)

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
    from django.http import Http404, HttpResponse
    from .api import _apply_export_replacements, _build_export_docx, _export_file_response, _format_export_filename
    from .utils import sanitize_resume_markdown

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
        return HttpResponse("Word export requires python-docx; install with: pip install python-docx", status=503)

    job = entry.job_listing
    company = (job.company_name or "").strip() if job else ""
    title = (job.title or "").strip() if job else ""
    filename = _format_export_filename("applied_resume", company, title, "docx")
    return _export_file_response(
        buf,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )



