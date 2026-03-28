"""
Server-rendered pages: GET templates and POST handlers that redirect (often with messages).

Form-based flows live here (tracks/resumes, automation, settings, optimizer-adjacent pages).
JSON/HTMX/async actions use Django Ninja in `resume_app.api` (optimizer, LLM, workflows) and
`resume_app.jobs_api` (job search, pipeline, preferences). See `resume_app/docs/ARCHITECTURE_UI.md`.
"""
import json
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.db import models

from ninja.errors import HttpError

from .api import (
    get_prompts as api_get_prompts,
    optimize_resume as api_optimize_resume,
    get_status as api_get_status,
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
    jobs_save as api_jobs_save,
    jobs_unsave as api_jobs_unsave,
    jobs_mark_applied as api_jobs_mark_applied,
    get_focus_breakdown,
    get_focus_sentence_alignment,
    JobSearchRequest,
    MarkAppliedRequest,
)
from .pipeline_board import applying_view, done_view, pipeline_view, vetting_view
from .models import (
    JobListingAction,
    PipelineEntry,
    JobSearchTask,
    JobSearchTaskRun,
    Track,
    JobListingEmbedding,
)
from .preference import invalidate_preference_cache, invalidate_disliked_embeddings_cache
from .job_sources import DEFAULT_SITE_NAMES
from .tasks import run_job_search_task, get_next_run_at, validate_cron
from .utils import cron_to_short_description
from .huey_dashboard import PERIODIC_TASKS, get_periodic_task_info, get_periodic_task_wrapper
from .prompt_store import get_effective_prompts, save_prompts_to_profile, clear_all_prompts_in_profile
from .llm_session import (
    get_active_llm_provider as _get_active_llm_provider,
    get_provider_preferences as _get_provider_preferences,
    get_provider_preference_rows as _get_provider_preference_rows,
    set_active_provider as _set_active_llm_provider,
)
from django.utils import timezone
from django.core.exceptions import ValidationError


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
    site_name = request.POST.getlist("site_name") or ["indeed"]
    if not site_name:
        site_name = ["indeed"]
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


def optimizer_view(request):
    """
    Resume Optimizer page backed by the existing Ninja API logic.
    - Use the LLM provider configured in Settings
    - Edit prompts
    - Upload resume + job description (or use job_id/resume_id from Match link)
    - Trigger optimization and see status
    """
    selected_provider = _get_active_llm_provider(request)

    # UserResume id (from Match / job search) — do not use for OptimizedResume PK; use opt_id for that.
    resume_id = request.GET.get("resume_id")
    # OptimizedResume id: loads status, agent logs, Word/PDF download in the status card.
    opt_id = request.GET.get("opt_id")
    job_id = request.GET.get("job_id")
    prefill_job_description = ""
    prefill_resume_id = None
    if job_id:
        try:
            from .models import JobListing
            job = JobListing.objects.get(id=int(job_id))
            prefill_job_description = (job.description or "").strip()
            request.session["optimizer_prefill_job_description"] = prefill_job_description
            request.session.modified = True
        except (ValueError, JobListing.DoesNotExist):
            pass
    else:
        prefill_job_description = request.session.get("optimizer_prefill_job_description", "")
    if resume_id:
        try:
            rid = int(resume_id)
            from .models import UserResume
            if UserResume.objects.filter(id=rid).exists():
                prefill_resume_id = rid
                request.session["optimizer_resume_id"] = rid
                request.session.modified = True
        except (ValueError, TypeError):
            pass
    elif request.session.get("optimizer_resume_id"):
        prefill_resume_id = request.session.get("optimizer_resume_id")

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

    # Prompts: persisted in UserPromptProfile (see prompt_store), with code defaults
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

        if action == "reset_prompts":
            try:
                clear_all_prompts_in_profile(request)
                full_prompts = get_effective_prompts(request)
                prompts = {
                    "writer": full_prompts["writer"],
                    "ats_judge": full_prompts["ats_judge"],
                    "recruiter_judge": full_prompts["recruiter_judge"],
                }
                messages.success(request, "Prompts reset to server defaults.")
            except Exception as e:
                messages.error(request, f"Could not reset prompts: {e}")

        elif action == "save_prompts":
            merged = get_effective_prompts(request)
            merged.update(
                {
                    "writer": request.POST.get("prompt_writer") or merged.get("writer", ""),
                    "ats_judge": request.POST.get("prompt_ats_judge") or merged.get("ats_judge", ""),
                    "recruiter_judge": request.POST.get("prompt_recruiter_judge") or merged.get("recruiter_judge", ""),
                    "writer_system": "",
                    "writer_user": "",
                    "ats_judge_system": "",
                    "ats_judge_user": "",
                    "recruiter_judge_system": "",
                    "recruiter_judge_user": "",
                }
            )
            save_prompts_to_profile(request, merged)
            prompts = {
                "writer": merged["writer"],
                "ats_judge": merged["ats_judge"],
                "recruiter_judge": merged["recruiter_judge"],
            }
            messages.success(request, "Prompts saved for future runs.")

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

        elif action == "run_optimizer":
            from django.core.files.uploadedfile import SimpleUploadedFile
            from .models import UserResume

            resume_file = request.FILES.get("resume_file")
            use_resume_id = request.POST.get("use_resume_id")
            if not resume_file and use_resume_id:
                try:
                    ur = UserResume.objects.get(id=int(use_resume_id))
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

            # Updated prompts from form (persist matching/insights from profile)
            merged = get_effective_prompts(request)
            merged.update(
                {
                    "writer": request.POST.get("prompt_writer") or merged.get("writer", ""),
                    "ats_judge": request.POST.get("prompt_ats_judge") or merged.get("ats_judge", ""),
                    "recruiter_judge": request.POST.get("prompt_recruiter_judge") or merged.get("recruiter_judge", ""),
                    "writer_system": "",
                    "writer_user": "",
                    "ats_judge_system": "",
                    "ats_judge_user": "",
                    "recruiter_judge_system": "",
                    "recruiter_judge_user": "",
                }
            )
            save_prompts_to_profile(request, merged)
            prompts = {
                "writer": merged["writer"],
                "ats_judge": merged["ats_judge"],
                "recruiter_judge": merged["recruiter_judge"],
            }

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
                    payload = OptimizeRequest(
                        job_description=job_description,
                        llm_provider=selected_provider,
                        llm_model=llm_model or None,
                        api_key=None,  # use stored key from LLMProviderConfig
                        prompt_writer=prompts["writer"],
                        prompt_ats_judge=prompts["ats_judge"],
                        prompt_recruiter_judge=prompts["recruiter_judge"],
                        debug=debug,
                        workflow_steps=request.POST.get("workflow_steps") or None,
                        loop_to=request.POST.get("loop_to") or None,
                        score_threshold=score_threshold_val,
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
                    messages.success(request, f"Optimization started for resume #{opt_id}.")
                    return redirect(f"{reverse('resume_optimizer')}?opt_id={opt_id}")
                except HttpError as e:
                    messages.error(request, str(e))
                except Exception as e:
                    messages.error(request, f"Error starting optimization: {e}")

        # Other actions fall through to re-render with updated context

    # If we have a running/completed optimization, load its status once; the frontend will poll for updates.
    status_data = None
    if opt_id:
        try:
            status_data = api_get_status(request, int(opt_id))
            # Format agent log thoughts for display (thought is a JSONField dict)
            logs = status_data.get("logs") or []
            for log in logs:
                t = log.get("thought")
                if isinstance(t, dict):
                    parts = []
                    for key in ("feedback", "reasoning", "message"):
                        if t.get(key):
                            parts.append(str(t[key]))
                    if t.get("optimized_resume") and isinstance(t["optimized_resume"], str):
                        s = t["optimized_resume"]
                        parts.append(s[:2000] + ("…" if len(s) > 2000 else ""))
                    skip = {"feedback", "reasoning", "message", "optimized_resume", "input_tokens", "output_tokens", "ats_score", "recruiter_score"}
                    rest = {k: v for k, v in t.items() if k not in skip}
                    if rest:
                        parts.append(json.dumps(rest, indent=2))
                    log["thought_text"] = "\n\n".join(parts) if parts else json.dumps(t, indent=2)
                else:
                    log["thought_text"] = str(t) if t else ""
                log["step_in"] = (t.get("input_tokens") or t.get("input")) if isinstance(t, dict) else None
                log["step_out"] = (t.get("output_tokens") or t.get("output")) if isinstance(t, dict) else None
        except HttpError as e:
            messages.error(request, str(e))
        except Exception:
            # If there is no OptimizedResume with this id, just show the form without status.
            status_data = None

    selected_llm_model = request.session.get("optimizer_llm_model") or llm_default_model
    job_description_value = (
        request.POST.get("job_description", "") if request.method == "POST" else prefill_job_description
    )
    prefill_resume_name = None
    if prefill_resume_id:
        try:
            from .models import UserResume
            ur = UserResume.objects.filter(id=prefill_resume_id).first()
            prefill_resume_name = (ur.original_filename or ur.file.name or f"#{prefill_resume_id}") if ur else None
        except Exception:
            pass
    from .models import OptimizerWorkflow
    saved_workflows = list(OptimizerWorkflow.objects.all())
    for w in saved_workflows:
        w.steps_json = json.dumps(w.steps)
    optimized_resume_id = None
    if opt_id:
        try:
            optimized_resume_id = int(opt_id)
        except (ValueError, TypeError):
            pass
    wizard_initial_step = 1
    if optimized_resume_id or (status_data and status_data.get("status")):
        wizard_initial_step = 3
    context = {
        "selected_provider": selected_provider,
        "llm_models": llm_models,
        "llm_default_model": llm_default_model,
        "selected_llm_model": selected_llm_model,
        "llm_key_stored": llm_key_stored,
        "llm_key_error": llm_key_error,
        "prompts": prompts,
        "resume_id": resume_id,
        "optimized_resume_id": optimized_resume_id,
        "status": status_data,
        "prefill_job_description": prefill_job_description,
        "prefill_resume_id": prefill_resume_id,
        "prefill_resume_name": prefill_resume_name,
        "job_description_value": job_description_value,
        "saved_workflows": saved_workflows,
        "optimizer_temperature": request.session.get("optimizer_temperature", "0.7"),
        "wizard_initial_step": wizard_initial_step,
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
        OptimizerWorkflow,
        Track,
    )
    from .llm_gateway import USAGE_QUERY_LABELS
    from .llm_rate_limit import get_llm_cooldown_ttl
    from .crypto import decrypt_api_key
    from .llm_factory import get_llm
    from .llm_services import list_models_for_provider
    from langchain_core.messages import HumanMessage

    active_provider = _get_active_llm_provider(request)
    provider_infos = []
    for p in sorted(LLM_PROVIDERS):
        config = LLMProviderConfig.objects.filter(provider=p).first()
        provider_infos.append({
            "name": p,
            "key_stored": bool(config and config.encrypted_api_key),
            "is_active": p == active_provider,
            "priority": config.priority if config else 100,
            "default_model": config.default_model if config else "",
        })

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "refresh_provider_models":
            cached: dict[str, list] = {}
            for cfg in _get_provider_preferences():
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
        if action == "reset_llm_usage_stats":
            solo = LLMAppUsageTotals.get_solo()
            LLMAppUsageTotals.objects.filter(pk=solo.pk).update(
                total_input_tokens=0,
                total_output_tokens=0,
                total_requests=0,
                total_estimated_invokes=0,
            )
            LLMUsageByModel.objects.all().delete()
            LLMUsageByQuery.objects.all().delete()
            messages.success(
                request,
                "LLM usage totals and per-model / per-query counters were reset.",
            )
            return redirect(reverse("settings") + "?tab=usage")
        if action == "save_stop_llm_requests":
            automation = AppAutomationSettings.get_solo()
            automation.stop_llm_requests = bool(request.POST.get("stop_llm_requests"))
            automation.save(update_fields=["stop_llm_requests", "updated_at"])
            messages.success(request, "LLM safety settings saved.")
            return redirect(reverse("settings") + "?tab=llm")
        if action == "save_app_automation":
            automation = AppAutomationSettings.get_solo()
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
                try:
                    automation.applying_optimizer_workflow = OptimizerWorkflow.objects.get(pk=int(raw_wf))
                except (ValueError, OptimizerWorkflow.DoesNotExist):
                    messages.error(request, "Invalid optimizer workflow selection.")
                    return redirect(reverse("settings") + "?tab=app")
            else:
                automation.applying_optimizer_workflow = None
            automation.save(
                update_fields=[
                    "pipeline_to_vetting_enabled",
                    "pipeline_preference_margin_min",
                    "vetting_to_applying_enabled",
                    "vetting_interview_probability_min",
                    "applying_optimizer_workflow",
                    "updated_at",
                ]
            )
            messages.success(request, "App automation settings saved.")
            return redirect(reverse("settings") + "?tab=app")
        if action == "dedupe_pipeline":
            from .job_dedupe import dedupe_pipeline_entries

            track = (request.POST.get("dedupe_track") or "*").strip().lower()
            stage = (request.POST.get("dedupe_stage") or "all").strip().lower()
            include_done = bool(request.POST.get("dedupe_include_done"))
            try:
                result = dedupe_pipeline_entries(
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
            row_count = max(
                len(pref_ids),
                len(pref_providers),
                len(pref_models),
                len(pref_priorities),
                len(pref_rpms),
                len(pref_tpms),
                len(pref_cooldowns),
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
                if not provider:
                    continue
                try:
                    priority = max(0, int(raw_priority or "100"))
                except ValueError:
                    messages.error(request, f"Priority for {provider} must be a whole number.")
                    return redirect(reverse("settings") + "?tab=llm")
                cfg = (
                    LLMProviderConfig.objects.filter(provider=provider)
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
                            LLMProviderPreference.objects.filter(id=rid).delete()
                            continue
                        pref_obj = LLMProviderPreference.objects.filter(id=rid).first()
                    except (TypeError, ValueError):
                        pref_obj = None
                if pref_obj is None:
                    pref_obj = LLMProviderPreference()
                pref_obj.provider_config = cfg
                pref_obj.model = model
                pref_obj.priority = priority
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
                LLMProviderPreference.objects.filter(id__in=remove_ids).delete()

            if AppAutomationSettings.get_solo().stop_llm_requests:
                messages.info(request, "Skipped connectivity ping while Stop LLM requests is enabled.")
            for row in saved_rows:
                cfg = row.provider_config
                model = (row.model or cfg.default_model or "").strip()
                if cfg.encrypted_api_key and model and not AppAutomationSettings.get_solo().stop_llm_requests:
                    try:
                        api_key_decrypted = decrypt_api_key(cfg.encrypted_api_key)
                        llm = get_llm(cfg.provider, api_key_decrypted, model=model)
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
                    had_active_provider = bool(_get_active_llm_provider(request))
                    api_llm_connect(request, ConnectRequest(provider=provider, api_key=api_key))
                    if not had_active_provider:
                        _set_active_llm_provider(provider)
                        request.session["active_llm_provider"] = provider
                        request.session.modified = True
                    request.session.pop("settings_provider_models_map", None)
                    messages.success(request, f"API key for {provider} validated and saved.")
                    return redirect(reverse("settings") + "?tab=llm")
                except HttpError as e:
                    messages.error(request, str(e))
        elif action == "set_active_provider":
            provider = (request.POST.get("active_provider") or "").strip()
            valid_connected = any(info["name"] == provider and info["key_stored"] for info in provider_infos)
            if not valid_connected:
                messages.error(request, "Choose a connected provider.")
            else:
                _set_active_llm_provider(provider)
                request.session["active_llm_provider"] = provider
                request.session.modified = True
                messages.success(request, f"{provider} is now the active provider.")
                return redirect(reverse("settings") + "?tab=llm")

    tab = (request.GET.get("tab") or "llm").strip().lower()
    if tab not in ("llm", "app", "usage"):
        tab = "llm"
    provider_preference_list = list(_get_provider_preferences())
    connected_provider_names = [cfg.provider for cfg in provider_preference_list]
    pref_rows = list(_get_provider_preference_rows().order_by("priority", "id"))
    if not pref_rows and connected_provider_names:
        for cfg in provider_preference_list:
            LLMProviderPreference.objects.create(
                provider_config=cfg,
                model=cfg.default_model or "",
                priority=cfg.priority,
            )
        pref_rows = list(_get_provider_preference_rows().order_by("priority", "id"))
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

    tracks_for_dedupe = list(Track.ensure_baseline())
    usage_totals = LLMAppUsageTotals.get_solo()
    stats_map = {(r.provider, r.model): r for r in LLMUsageByModel.objects.all()}
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
            ttl = get_llm_cooldown_ttl(prov, m_gl)
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
    for st in LLMUsageByModel.objects.all().order_by("-last_used_at", "provider"):
        if (st.provider, st.model) in pref_keys_seen:
            continue
        ttl = None
        m_gl = None if st.model == "__default__" else st.model
        try:
            ttl = get_llm_cooldown_ttl(st.provider, m_gl)
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
    for r in LLMUsageByQuery.objects.all().order_by("query_kind", "provider", "model"):
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

    context = {
        "provider_infos": provider_infos,
        "provider_preference_list": provider_preference_list,
        "provider_preference_rows": provider_preference_rows,
        "connected_provider_names": connected_provider_names,
        "provider_models_map": provider_models_map,
        "active_provider": active_provider,
        "settings_tab": tab,
        "app_automation": AppAutomationSettings.get_solo(),
        "optimizer_workflows": list(OptimizerWorkflow.objects.all().order_by("name")),
        "dedupe_tracks": tracks_for_dedupe,
        "usage_totals": usage_totals,
        "usage_rows": usage_rows,
        "usage_estimated_pct": est_pct,
        "usage_cooldown_error": usage_cooldown_error,
        "usage_by_query_rows": usage_by_query_rows,
    }
    return render(request, "resume_app/settings.html", context)


def llm_test_view(request):
    """
    Manual test page for LLM connectivity + response rendering.

    Uses stored `LLMProviderConfig` keys where possible (including Ollama Local host/IP).
    """
    from .models import LLMProviderConfig, AppAutomationSettings
    from .crypto import decrypt_api_key
    from .llm_factory import get_llm
    from langchain_core.messages import HumanMessage
    from .llm_services import list_models_for_provider, DEFAULT_MODELS

    stop_llm = AppAutomationSettings.get_solo().stop_llm_requests

    provider = (request.GET.get("provider") or "").strip()
    if not provider:
        provider = request.session.get("active_llm_provider") or _get_active_llm_provider(request) or ""
    if not provider:
        provider = "Ollama Local" if LLMProviderConfig.objects.filter(provider="Ollama Local").exists() else next(iter(LLM_PROVIDERS))

    if provider not in LLM_PROVIDERS:
        provider = next(iter(LLM_PROVIDERS))

    config = LLMProviderConfig.objects.filter(provider=provider).first()
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
            config = LLMProviderConfig.objects.filter(provider=provider).first()
            if not config or not config.encrypted_api_key:
                error = f"No stored connection for {provider}. Go to Settings and connect first."
            else:
                api_key_decrypted = decrypt_api_key(config.encrypted_api_key)
                try:
                    llm = get_llm(provider, api_key_decrypted, model=model or None)
                    resp = llm.invoke([HumanMessage(content=prompt)])
                    # LangChain chat models typically expose `content`
                    response_text = getattr(resp, "content", None) or str(resp)
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


def prompt_library_view(request):
    """
    Prompt Library: edit Writer / ATS / Recruiter / Matching / Insights templates.
    Values are stored in UserPromptProfile (see prompt_store).
    """
    try:
        prompts = get_effective_prompts(request)
    except Exception:
        prompts = get_effective_prompts(None)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_prompts":
            w_sys = (request.POST.get("prompt_writer_system") or "").strip()
            w_usr = (request.POST.get("prompt_writer_user") or "").strip()
            w_combined = (request.POST.get("prompt_writer_combined") or "").strip()
            if w_combined:
                writer_leg, writer_sys, writer_usr = w_combined, "", ""
            else:
                writer_leg, writer_sys, writer_usr = "", w_sys, w_usr

            a_sys = (request.POST.get("prompt_ats_system") or "").strip()
            a_usr = (request.POST.get("prompt_ats_user") or "").strip()
            a_combined = (request.POST.get("prompt_ats_combined") or "").strip()
            if a_combined:
                ats_leg, ats_sys, ats_usr = a_combined, "", ""
            else:
                ats_leg, ats_sys, ats_usr = "", a_sys, a_usr

            r_sys = (request.POST.get("prompt_recruiter_system") or "").strip()
            r_usr = (request.POST.get("prompt_recruiter_user") or "").strip()
            r_combined = (request.POST.get("prompt_recruiter_combined") or "").strip()
            if r_combined:
                rec_leg, rec_sys, rec_usr = r_combined, "", ""
            else:
                rec_leg, rec_sys, rec_usr = "", r_sys, r_usr

            m_sys = (request.POST.get("prompt_matching_system") or "").strip()
            m_usr = (request.POST.get("prompt_matching_user") or "").strip()
            m_combined = (request.POST.get("prompt_matching_combined") or "").strip()
            if m_combined:
                match_leg, match_sys, match_usr = m_combined, "", ""
            else:
                match_leg, match_sys, match_usr = "", m_sys, m_usr

            i_sys = (request.POST.get("prompt_insights_system") or "").strip()
            i_usr = (request.POST.get("prompt_insights_user") or "").strip()
            i_combined = (request.POST.get("prompt_insights_combined") or "").strip()
            if i_combined:
                ins_leg, ins_sys, ins_usr = i_combined, "", ""
            else:
                ins_leg, ins_sys, ins_usr = "", i_sys, i_usr

            prompts = {
                "writer": writer_leg,
                "writer_system": writer_sys,
                "writer_user": writer_usr,
                "ats_judge": ats_leg,
                "ats_judge_system": ats_sys,
                "ats_judge_user": ats_usr,
                "recruiter_judge": rec_leg,
                "recruiter_judge_system": rec_sys,
                "recruiter_judge_user": rec_usr,
                "matching": match_leg,
                "matching_system": match_sys,
                "matching_user": match_usr,
                "insights": ins_leg,
                "insights_system": ins_sys,
                "insights_user": ins_usr,
            }
            save_prompts_to_profile(request, prompts)
            prompts = get_effective_prompts(request)
            messages.success(request, "Prompts saved. They will be used by the Resume Optimizer and Job Search.")
            return redirect(reverse("prompt_library"))
        if action == "reset_prompts":
            try:
                clear_all_prompts_in_profile(request)
                messages.success(request, "Prompts reset to server defaults.")
                return redirect(reverse("prompt_library"))
            except Exception as e:
                messages.error(request, f"Could not reset prompts: {e}")

    return render(request, "resume_app/prompt_library.html", {"prompts": prompts})


# Step ids for workflow builder (must match agents.VALID_STEP_IDS)
WORKFLOW_STEP_IDS = ["writer", "ats_judge", "recruiter_judge"]
WORKFLOW_STEP_LABELS = {"writer": "Writer", "ats_judge": "ATS Judge", "recruiter_judge": "Recruiter Judge"}


def workflow_list_view(request):
    """List saved workflows; link to create and edit."""
    from .models import OptimizerWorkflow
    workflows = list(OptimizerWorkflow.objects.all())
    for w in workflows:
        w.step_labels_display = [WORKFLOW_STEP_LABELS.get(s, s) for s in w.steps]
    return render(
        request,
        "resume_app/workflow_list.html",
        {"workflows": workflows},
    )


def workflow_create_view(request):
    """Create a new workflow. POST: validate and save then redirect to list."""
    from .models import OptimizerWorkflow
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Name is required.")
            return render(request, "resume_app/workflow_form.html", {"workflow": None, "workflow_steps_json": "[]"})
        steps_raw = request.POST.get("workflow_steps", "")
        try:
            steps = json.loads(steps_raw) if steps_raw else []
        except json.JSONDecodeError:
            messages.error(request, "Invalid steps format.")
            return render(request, "resume_app/workflow_form.html", {"workflow": None, "workflow_steps_json": "[]"})
        invalid = [s for s in steps if s not in WORKFLOW_STEP_IDS]
        if invalid or not steps:
            messages.error(request, "Steps must be a non-empty list of: Writer, ATS Judge, Recruiter Judge.")
            return render(request, "resume_app/workflow_form.html", {"workflow": None, "workflow_steps_json": "[]"})
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
        OptimizerWorkflow.objects.create(
            name=name,
            steps=steps,
            loop_to=loop_to,
            max_iterations=max_iterations,
            score_threshold=score_threshold,
        )
        messages.success(request, f"Workflow \"{name}\" created.")
        return redirect(reverse("workflow_list"))
    return render(
        request,
        "resume_app/workflow_form.html",
        {"workflow": None, "workflow_steps_json": "[]"},
    )


def workflow_edit_view(request, workflow_id):
    """Edit an existing workflow. POST: validate and save then redirect to list."""
    from .models import OptimizerWorkflow
    workflow = get_object_or_404(OptimizerWorkflow, id=workflow_id)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Name is required.")
            return render(request, "resume_app/workflow_form.html", {"workflow": workflow, "workflow_steps_json": json.dumps(workflow.steps)})
        steps_raw = request.POST.get("workflow_steps", "")
        try:
            steps = json.loads(steps_raw) if steps_raw else []
        except json.JSONDecodeError:
            messages.error(request, "Invalid steps format.")
            return render(request, "resume_app/workflow_form.html", {"workflow": workflow, "workflow_steps_json": json.dumps(workflow.steps)})
        invalid = [s for s in steps if s not in WORKFLOW_STEP_IDS]
        if invalid or not steps:
            messages.error(request, "Steps must be a non-empty list of: Writer, ATS Judge, Recruiter Judge.")
            return render(request, "resume_app/workflow_form.html", {"workflow": workflow, "workflow_steps_json": json.dumps(workflow.steps)})
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
        workflow.name = name
        workflow.steps = steps
        workflow.loop_to = loop_to
        workflow.max_iterations = max_iterations
        workflow.score_threshold = score_threshold
        workflow.save()
        messages.success(request, f"Workflow \"{name}\" updated.")
        return redirect(reverse("workflow_list"))
    return render(
        request,
        "resume_app/workflow_form.html",
        {"workflow": workflow, "workflow_steps_json": json.dumps(workflow.steps)},
    )


def workflow_delete_view(request, workflow_id):
    """POST: delete workflow and redirect to list."""
    from .models import OptimizerWorkflow
    if request.method == "POST":
        workflow = get_object_or_404(OptimizerWorkflow, id=workflow_id)
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
        try:
            if action == "add_disqualifiers":
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
                    _, created = UserDisqualifier.objects.get_or_create(phrase=norm)
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
                        UserDisqualifier.objects.filter(id=int(dq_id)).delete()
                        messages.success(request, "Disqualifier removed.")
                    except ValueError:
                        pass
            elif action in {"like", "dislike", "save", "unsave", "mark_applied"} and not job_id:
                messages.error(request, "Missing job id for action.")
            elif action == "like":
                api_jobs_like(request, job_listing_id=int(job_id))
                messages.success(request, "Job liked.")
            elif action == "dislike":
                api_jobs_dislike(request, job_listing_id=int(job_id))
                sep = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{sep}disqualifier_job_id={job_id}"
                messages.success(request, "Job removed. Add phrases from it to avoid similar jobs.")
            elif action == "save":
                api_jobs_save(request, job_listing_id=int(job_id))
                messages.success(request, "Job saved to favourites.")
            elif action == "unsave":
                api_jobs_unsave(request, job_listing_id=int(job_id))
                messages.success(request, "Job removed from favourites.")
            elif action == "mark_applied":
                resume_id_val = request.POST.get("resume_id")
                if not resume_id_val:
                    messages.error(request, "Missing resume id for marking as applied.")
                else:
                    payload = MarkAppliedRequest(resume_id=int(resume_id_val))
                    api_jobs_mark_applied(request, job_listing_id=int(job_id), payload=payload)
                    messages.success(request, "Marked as applied.")
        except HttpError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Error performing action: {e}")
        return redirect(next_url)

    # GET: render search page
    view_mode = (request.GET.get("view") or "results").lower()
    show_favourites = view_mode == "favourites"
    show_excluded = view_mode == "excluded"
    query = (request.GET.get("q") or "").strip()
    location = (request.GET.get("location") or "").strip()
    selected_site_names = [s.strip().lower() for s in request.GET.getlist("site_name") if (s or "").strip()]
    allowed_site_names = ["indeed", "linkedin", "glassdoor", "google", "zip_recruiter"]
    selected_site_names = [s for s in selected_site_names if s in allowed_site_names]
    if not selected_site_names:
        selected_site_names = list(DEFAULT_SITE_NAMES)
    resume_id_raw = (request.GET.get("resume_id") or "").strip()
    # Treat blank or explicit "None" as no resume selected
    if not resume_id_raw or resume_id_raw.lower() == "none":
        resume_id_val = None
    else:
        try:
            resume_id_val = int(resume_id_raw)
        except ValueError:
            resume_id_val = None
    min_score_raw = (request.GET.get("min_score") or "").strip()
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
        active_provider = _get_active_llm_provider(request)
        if active_provider:
            job_search_llm_provider = active_provider
            models_data = api_llm_models(request, provider=active_provider)
            job_search_llm_models = models_data.get("models", [])
            job_search_llm_default_model = models_data.get("default_model")
            job_search_llm_model = (
                request.GET.get("llm_model") or
                request.session.get("job_search_llm_model") or
                job_search_llm_default_model
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
    tracks_qs = Track.ensure_baseline()
    available_slugs = list(tracks_qs.values_list("slug", flat=True))
    raw_track_param = (request.GET.get("track") or "").strip().lower()
    raw_track = raw_track_param or (request.session.get("job_search_track") or "").strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug()

    # Resume -> track association (default only):
    # If a resume has a stored track AND the user did not explicitly pick a track in this request,
    # default the page track to the resume's stored track.
    if resume_id_val is not None and not raw_track_param:
        try:
            from .models import UserResume

            selected_resume = UserResume.objects.filter(id=resume_id_val).first()
            if selected_resume and selected_resume.track and selected_resume.track in available_slugs:
                raw_track = selected_resume.track
        except Exception:
            # Best-effort; never block job search.
            pass
    request.session["job_search_track"] = raw_track
    request.session.modified = True

    try:
        if show_favourites:
            saved_results = api_jobs_saved(request)
        elif show_excluded:
            excluded_results = api_jobs_disliked(request)
        elif query:
            if request.GET.get("refresh"):
                request.session.pop("job_search_cache", None)
            sort_param = (request.GET.get("sort") or "focus").strip().lower()
            if sort_param not in ("focus", "resume"):
                sort_param = "focus"
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
    current_disqualifiers = [{"id": d.id, "phrase": d.phrase} for d in UserDisqualifier.objects.all().order_by("phrase")]

    sort_param = (request.GET.get("sort") or "focus").strip().lower()
    if sort_param not in ("focus", "resume"):
        sort_param = "focus"
    preserved_site_query = urlencode([("site_name", s) for s in selected_site_names])
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
        "job_tracks": list(tracks_qs),
        "site_options": allowed_site_names,
        "selected_site_names": selected_site_names,
        "preserved_site_query": preserved_site_query,
    }
    return render(request, "resume_app/jobs_search.html", context)


def job_tasks_view(request):
    """List job search tasks with next run and run history."""
    tracks_qs = Track.ensure_baseline()
    track_list = list(tracks_qs)
    selected_slug = (request.GET.get("track") or "").strip().lower()
    if not selected_slug and track_list:
        selected_slug = track_list[0].slug
    selected_track = None
    if selected_slug:
        selected_track = next((t for t in track_list if t.slug == selected_slug), None)
    if not selected_track and track_list:
        selected_track = track_list[0]
        selected_slug = selected_track.slug

    tasks_qs = JobSearchTask.objects.all().order_by("name", "id")
    if selected_slug:
        tasks_qs = tasks_qs.filter(track=selected_slug)
    tasks = list(tasks_qs)
    for t in tasks:
        t.recent_runs = list(t.runs.all()[:10])
        t.last_run = t.recent_runs[0] if t.recent_runs else None
        t.schedule_description = cron_to_short_description(t.frequency or "")

    context = {
        "tracks": track_list,
        "selected_track": selected_track,
        "selected_track_slug": selected_slug,
        "tasks": tasks,
    }
    return render(request, "resume_app/job_automation.html", context)


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

    recent_runs = (
        JobSearchTaskRun.objects.select_related("task")
        .order_by("-started_at")[:10]
    )
    from .tasks import CLEANUP_STATUS_CACHE_KEY
    cleanup_status = cache.get(CLEANUP_STATUS_CACHE_KEY)

    context = {
        "immediate": immediate,
        "queue_stats": queue_stats,
        "queue_stats_error": queue_stats_error,
        "periodic_tasks": periodic_rows,
        "recent_runs": recent_runs,
        "cleanup_status": cleanup_status,
    }
    return render(request, "resume_app/huey_dashboard.html", context)


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


def huey_run_cleanup_now_view(request):
    """Enqueue the daily inactive+dedupe cleanup task immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        from .tasks import cleanup_inactive_pipeline_entries_daily

        cleanup_inactive_pipeline_entries_daily()
    except Exception as e:
        messages.error(request, f"Failed to queue cleanup task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, "Cleanup job queued (inactive jobs + dedupe).")
    return redirect("huey_dashboard")


def job_task_create_view(request):
    """Create a new job search task. Sets next_run_at from cron."""
    tracks_qs = Track.ensure_baseline()
    track_list = list(tracks_qs)
    default_track = Track.get_default_slug()
    if request.method != "POST":
        context = {
            "task": None,
            "form_track": default_track,
            "job_tracks": track_list,
            "form_frequency": "0 9 * * *",
            "form_jobs_to_fetch": 50,
            "form_site_name": list(DEFAULT_SITE_NAMES),
            "form_start_time": "",
        }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, default_track, {t.slug for t in track_list})
    if errs:
        for e in errs:
            messages.error(request, e)
        return redirect("job_task_create")

    task = JobSearchTask(
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
    return redirect("job_automation")


def job_task_edit_view(request, task_id):
    """Edit a job search task. Optionally update next_run_at."""
    task = get_object_or_404(JobSearchTask, id=task_id)
    tracks_qs = Track.ensure_baseline()
    track_list = list(tracks_qs)
    if request.method != "POST":
        context = {
            "task": task,
            "form_track": task.track,
            "job_tracks": track_list,
            "form_frequency": task.frequency,
            "form_jobs_to_fetch": task.jobs_to_fetch,
            "form_site_name": task.site_name or list(DEFAULT_SITE_NAMES),
            "form_start_time": task.start_time.strftime("%H:%M") if task.start_time else "",
        }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, Track.get_default_slug(), {t.slug for t in track_list})
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
    return redirect("job_automation")


def job_task_run_now_view(request, task_id):
    """Enqueue run_job_search_task once (does not change next_run_at)."""
    task = get_object_or_404(JobSearchTask, id=task_id)
    run_job_search_task(task_id)
    messages.success(request, f"Task \"{task.name or task.search_term}\" queued to run now.")
    return redirect("job_automation")


def job_task_toggle_active_view(request, task_id):
    """Toggle is_active and redirect to task list."""
    task = get_object_or_404(JobSearchTask, id=task_id)
    task.is_active = not task.is_active
    task.save()
    status = "activated" if task.is_active else "paused"
    messages.success(request, f"Task \"{task.name or task.search_term}\" {status}.")
    return redirect("job_automation")


def track_list_view(request):
    """
    Tracks & resumes page: search tracks (CRUD for Track) and resume PDFs (upload, assign
    default track, delete). POST `action` distinguishes create_track, upload_resume,
    assign_resume_tracks, delete_resume.
    """
    tracks_qs = Track.ensure_baseline()
    tracks = list(tracks_qs)
    default_track_slug = Track.get_default_slug()

    # Keep this bounded: track management should stay snappy even with many resumes.
    from .models import UserResume

    resumes = list(UserResume.objects.order_by("-uploaded_at")[:200])

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
            original_name = (original_name or "resume.pdf")[:255]

            UserResume.objects.create(
                file=resume_file,
                original_filename=original_name,
                track=track_slug or "",
            )
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

            resume = UserResume.objects.filter(id=delete_resume_id).first()
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
                updated_count += UserResume.objects.filter(id=rid).update(track=new_slug or "")
            messages.success(
                request,
                f"Updated track assignment for {updated_count} resume(s).",
            )
            return redirect("track_list")

        # Default branch: create a new track
        slug = (request.POST.get("slug") or "").strip().lower()
        label = (request.POST.get("label") or "").strip()
        description = (request.POST.get("description") or "").strip()
        is_default = bool(request.POST.get("is_default"))
        if not slug:
            messages.error(request, "Slug is required.")
        elif not label:
            messages.error(request, "Label is required.")
        elif Track.objects.filter(slug=slug).exists():
            messages.error(request, f"Track with slug '{slug}' already exists.")
        else:
            if is_default:
                Track.objects.update(is_default=False)
            track = Track.objects.create(
                slug=slug,
                label=label,
                description=description,
                is_default=is_default,
            )
            messages.success(request, f"Track \"{track.label}\" created.")
            return redirect("track_list")

    context = {
        "tracks": tracks,
        "resumes": resumes,
        "default_track_slug": default_track_slug,
    }
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

    track = Track.objects.filter(slug=slug).first()
    if not track:
        messages.error(request, "Track not found.")
        return redirect("track_list")

    if Track.objects.count() <= 1:
        messages.error(request, "Cannot delete the only remaining track.")
        return redirect("track_list")

    slug_val = track.slug

    # Disassociate any resumes assigned to this track.
    try:
        from .models import UserResume
        UserResume.objects.filter(track=slug_val).update(track="")
    except Exception:
        pass

    # Delete scheduled searches for this track
    JobSearchTask.objects.filter(track=slug_val).delete()
    # Delete pipeline rows for this track
    PipelineEntry.objects.filter(track=slug_val).delete()
    # Delete job actions/embeddings for this track
    JobListingAction.objects.filter(track=slug_val).delete()
    JobListingEmbedding.objects.filter(track=slug_val).delete()
    # Invalidate preference caches so embeddings/centroids are recomputed
    try:
        invalidate_preference_cache()
        invalidate_disliked_embeddings_cache()
    except Exception:
        # Best-effort; failure here should not block delete.
        pass

    was_default = track.is_default
    label = track.label or track.slug
    track.delete()

    if was_default:
        # Ensure we still have a default track.
        Track.ensure_baseline()

    messages.success(request, f"Track \"{label}\" and its associated tasks/pipeline/actions were deleted.")
    return redirect("track_list")


def focus_breakdown_view(request, job_listing_id: int):
    """Why? page: show title vs role similarity breakdown and resume–job top matches."""
    # Use the same track as the job search page so centroids and preference
    # margins line up with what you see in Results.
    tracks_qs = Track.ensure_baseline()
    available_slugs = list(tracks_qs.values_list("slug", flat=True))
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "").strip().lower()
    if not raw_track or raw_track not in available_slugs:
        raw_track = Track.get_default_slug()
    request.session["job_search_track"] = raw_track
    request.session.modified = True

    data = get_focus_breakdown(job_listing_id, track=raw_track)
    if data is None:
        messages.error(request, "Job not found or no liked jobs to compare. Like some jobs first.")
        return redirect("jobs_search")
    role_weight = round(1 - data["alpha"], 2)
    resume_id_raw = (request.GET.get("resume_id") or "").strip()
    resume_id = int(resume_id_raw) if resume_id_raw and resume_id_raw.lower() != "none" else None
    if resume_id is None:
        from .models import UserResume
        latest = UserResume.objects.order_by("-uploaded_at").first()
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


def focus_alignment_view(request, job_listing_id: int, liked_job_id: int):
    """
    Sentence-level view: for a given liked job row, show how each job sentence aligned
    with that liked job's sentences to produce the Role %.
    """
    data = get_focus_sentence_alignment(job_listing_id, liked_job_id)
    if data is None:
        messages.error(
            request,
            "Could not compute sentence-level alignment (maybe no role sentences or embeddings yet).",
        )
        return redirect("focus_breakdown", job_listing_id=job_listing_id)
    return render(request, "resume_app/focus_alignment.html", {"alignment": data})


def optimizer_status_view(request, resume_id: int):
    """
    JSON endpoint used by the frontend to poll optimization status.
    Wraps the existing Ninja get_status handler.
    """
    try:
        data = api_get_status(request, int(resume_id))
        return JsonResponse(data)
    except HttpError as e:
        return JsonResponse({"error": str(e)}, status=e.status_code)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
