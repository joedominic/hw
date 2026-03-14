import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse

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
    jobs_run_keyword_search as api_jobs_run_keyword_search,
    get_focus_breakdown,
    get_focus_sentence_alignment,
    JobSearchRequest,
    MarkAppliedRequest,
    KeywordEntry,
    RunKeywordSearchRequest,
)


def _get_active_llm_provider(request):
    """Return the provider selected in Settings, or the first connected provider."""
    from .models import LLMProviderConfig

    active_provider = (request.session.get("active_llm_provider") or "").strip()
    connected = list(
        LLMProviderConfig.objects.filter(encrypted_api_key__isnull=False)
        .exclude(encrypted_api_key="")
        .order_by("provider")
        .values_list("provider", flat=True)
    )
    if active_provider and active_provider in connected:
        return active_provider
    if connected:
        request.session["active_llm_provider"] = connected[0]
        request.session.modified = True
        return connected[0]
    return None


def optimizer_view(request):
    """
    Resume Optimizer page backed by the existing Ninja API logic.
    - Use the LLM provider configured in Settings
    - Edit prompts
    - Upload resume + job description (or use job_id/resume_id from Match link)
    - Trigger optimization and see status
    """
    selected_provider = _get_active_llm_provider(request)

    # UserResume id (from Match / job search)
    resume_id = request.GET.get("resume_id")
    # OptimizedResume id (for viewing existing runs)
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

    # Prompts: keep last-edited values in session, otherwise load defaults from API
    prompts = request.session.get("optimizer_prompts")
    if not prompts:
        try:
            prompts_obj = api_get_prompts(request)
            prompts = {
                "writer": prompts_obj["writer"],
                "ats_judge": prompts_obj["ats_judge"],
                "recruiter_judge": prompts_obj["recruiter_judge"],
            }
            request.session["optimizer_prompts"] = prompts
        except Exception:
            prompts = {"writer": "", "ats_judge": "", "recruiter_judge": ""}

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "reset_prompts":
            try:
                prompts_obj = api_get_prompts(request)
                prompts = {
                    "writer": prompts_obj["writer"],
                    "ats_judge": prompts_obj["ats_judge"],
                    "recruiter_judge": prompts_obj["recruiter_judge"],
                }
                request.session["optimizer_prompts"] = prompts
                messages.success(request, "Prompts reset to server defaults.")
            except Exception as e:
                messages.error(request, f"Could not reset prompts: {e}")

        elif action == "save_prompts":
            prompts = {
                "writer": request.POST.get("prompt_writer") or prompts.get("writer", ""),
                "ats_judge": request.POST.get("prompt_ats_judge") or prompts.get("ats_judge", ""),
                "recruiter_judge": request.POST.get("prompt_recruiter_judge") or prompts.get("recruiter_judge", ""),
            }
            request.session["optimizer_prompts"] = prompts
            request.session.modified = True
            messages.success(request, "Prompts saved for future runs.")

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

            # Updated prompts from form
            prompts = {
                "writer": request.POST.get("prompt_writer") or prompts.get("writer", ""),
                "ats_judge": request.POST.get("prompt_ats_judge") or prompts.get("ats_judge", ""),
                "recruiter_judge": request.POST.get("prompt_recruiter_judge") or prompts.get("recruiter_judge", ""),
            }
            request.session["optimizer_prompts"] = prompts

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
    context = {
        "selected_provider": selected_provider,
        "llm_models": llm_models,
        "llm_default_model": llm_default_model,
        "selected_llm_model": selected_llm_model,
        "llm_key_stored": llm_key_stored,
        "llm_key_error": llm_key_error,
        "prompts": prompts,
        "resume_id": resume_id,
        "status": status_data,
        "prefill_job_description": prefill_job_description,
        "prefill_resume_id": prefill_resume_id,
        "prefill_resume_name": prefill_resume_name,
        "job_description_value": job_description_value,
        "saved_workflows": saved_workflows,
    }
    return render(request, "resume_app/optimizer.html", context)


def settings_view(request):
    """
    Settings / Integrations: manage LLM provider API keys in one place.
    Used by Resume Optimizer and other tools that need LLM access.
    """
    from .models import LLMProviderConfig

    active_provider = _get_active_llm_provider(request)
    provider_infos = []
    for p in sorted(LLM_PROVIDERS):
        config = LLMProviderConfig.objects.filter(provider=p).first()
        provider_infos.append({
            "name": p,
            "key_stored": bool(config and config.encrypted_api_key),
            "is_active": p == active_provider,
        })

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "connect":
            provider = (request.POST.get("provider") or "").strip()
            api_key = (request.POST.get("api_key") or "").strip()
            if not provider or provider not in LLM_PROVIDERS:
                messages.error(request, "Invalid provider.")
            elif not api_key:
                messages.error(request, "Enter an API key before connecting.")
            else:
                try:
                    had_active_provider = bool(_get_active_llm_provider(request))
                    api_llm_connect(request, ConnectRequest(provider=provider, api_key=api_key))
                    if not had_active_provider:
                        request.session["active_llm_provider"] = provider
                        request.session.modified = True
                    messages.success(request, f"API key for {provider} validated and saved.")
                    return redirect(reverse("settings"))
                except HttpError as e:
                    messages.error(request, str(e))
        elif action == "set_active_provider":
            provider = (request.POST.get("active_provider") or "").strip()
            valid_connected = any(info["name"] == provider and info["key_stored"] for info in provider_infos)
            if not valid_connected:
                messages.error(request, "Choose a connected provider.")
            else:
                request.session["active_llm_provider"] = provider
                request.session.modified = True
                messages.success(request, f"{provider} is now the active provider.")
                return redirect(reverse("settings"))

    context = {
        "provider_infos": provider_infos,
        "active_provider": active_provider,
    }
    return render(request, "resume_app/settings.html", context)


def prompt_library_view(request):
    """
    Prompt Library: edit and manage Writer / ATS Judge / Recruiter / Matching prompt templates in one place.
    Saved prompts are stored in session and used by the Resume Optimizer and Job Search.
    """
    prompts = request.session.get("optimizer_prompts")
    if prompts is not None:
        if "matching" not in prompts:
            prompts = {**prompts, "matching": ""}
        if "insights" not in prompts:
            prompts = {**prompts, "insights": ""}
        request.session["optimizer_prompts"] = prompts
    if not prompts:
        try:
            prompts_obj = api_get_prompts(request)
            prompts = {
                "writer": prompts_obj["writer"],
                "ats_judge": prompts_obj["ats_judge"],
                "recruiter_judge": prompts_obj["recruiter_judge"],
                "matching": prompts_obj.get("matching", ""),
                "insights": prompts_obj.get("insights", ""),
            }
            request.session["optimizer_prompts"] = prompts
        except Exception:
            prompts = {"writer": "", "ats_judge": "", "recruiter_judge": "", "matching": "", "insights": ""}

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_prompts":
            prompts = {
                "writer": request.POST.get("prompt_writer") or prompts.get("writer", ""),
                "ats_judge": request.POST.get("prompt_ats_judge") or prompts.get("ats_judge", ""),
                "recruiter_judge": request.POST.get("prompt_recruiter_judge") or prompts.get("recruiter_judge", ""),
                "matching": request.POST.get("prompt_matching") or prompts.get("matching", ""),
                "insights": request.POST.get("prompt_insights") or prompts.get("insights", ""),
            }
            request.session["optimizer_prompts"] = prompts
            request.session.modified = True
            messages.success(request, "Prompts saved. They will be used by the Resume Optimizer.")
            return redirect(reverse("prompt_library"))
        if action == "reset_prompts":
            try:
                prompts_obj = api_get_prompts(request)
                prompts = {
                    "writer": prompts_obj["writer"],
                    "ats_judge": prompts_obj["ats_judge"],
                    "recruiter_judge": prompts_obj["recruiter_judge"],
                    "matching": prompts_obj.get("matching", ""),
                    "insights": prompts_obj.get("insights", ""),
                }
                request.session["optimizer_prompts"] = prompts
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
            elif action in {"like", "dislike", "save", "unsave", "match", "mark_applied"} and not job_id:
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

    # Track / profile: IC vs Management
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "ic").lower()
    if raw_track not in ("ic", "mgmt"):
        raw_track = "ic"
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
                site_name=None,
                results_wanted=results_wanted_val,
                resume_id=resume_id_val,
                sort=sort_param,
                llm_provider=job_search_llm_provider,
                llm_model=job_search_llm_model or None,
                track=raw_track,
            )
            search_results = api_jobs_search(request, payload=payload)
            # Always pass a dict so template has .jobs and .total even when empty
            jobs_list = getattr(search_results, "jobs", None) or []
            search_results = {
                "jobs": [
                    j.model_dump() if hasattr(j, "model_dump") else vars(j)
                    for j in jobs_list
                ],
                "total": getattr(search_results, "total", 0) or len(jobs_list),
            }
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
    }
    return render(request, "resume_app/jobs_search.html", context)


def focus_breakdown_view(request, job_listing_id: int):
    """Why? page: show title vs role similarity breakdown and resume–job top matches."""
    # Use the same track (IC vs Management) as the job search page so centroids
    # and preference margins line up with what you see in Results.
    raw_track = (request.GET.get("track") or request.session.get("job_search_track") or "ic").lower()
    if raw_track not in ("ic", "mgmt"):
        raw_track = "ic"
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


def keyword_search_view(request):
    """
    Keyword search page.
    - Configure multiple (keyword, resume) pairs
    - Run batched searches with fit checks
    - See applied jobs
    """
    # We keep keyword rows in the session; each row is {"keyword": str, "resume_id": int or None}
    entries = request.session.get("keyword_entries") or []

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add_row":
            entries.append({"keyword": "", "resume_id": None})
            request.session["keyword_entries"] = entries
            return redirect("jobs_keywords")

        if action == "remove_row":
            idx_raw = request.POST.get("index")
            try:
                idx = int(idx_raw)
                if 0 <= idx < len(entries):
                    entries.pop(idx)
                    request.session["keyword_entries"] = entries
            except (TypeError, ValueError):
                messages.error(request, "Invalid row index.")
            return redirect("jobs_keywords")

        if action == "run_keywords":
            # Update entries from form data first
            new_entries = []
            row_indices = request.POST.getlist("row_index")
            for idx_raw in row_indices:
                kw = (request.POST.get(f"keyword_{idx_raw}") or "").strip()
                resume_id_raw = request.POST.get(f"resume_id_{idx_raw}") or ""
                resume_id_val = int(resume_id_raw) if resume_id_raw else None
                if kw and resume_id_val:
                    new_entries.append({"keyword": kw, "resume_id": resume_id_val})
            entries = new_entries
            request.session["keyword_entries"] = entries

            location = (request.POST.get("location") or "").strip()
            results_wanted_raw = (request.POST.get("results_wanted") or "").strip()
            try:
                results_wanted = int(results_wanted_raw) if results_wanted_raw else 20
            except ValueError:
                results_wanted = 20

            if not entries:
                messages.error(request, "Add at least one keyword and select a resume for each.")
            else:
                try:
                    payload = RunKeywordSearchRequest(
                        entries=[KeywordEntry(keyword=e["keyword"], resume_id=e["resume_id"]) for e in entries],
                        location=location or None,
                        site_name=None,
                        results_wanted=results_wanted,
                    )
                    result = api_jobs_run_keyword_search(request, payload=payload)
                    request.session["keyword_results"] = [
                        r.model_dump() if hasattr(r, "model_dump") else r.dict()
                        for r in result.results
                    ]
                    request.session["keyword_errors"] = result.errors
                    messages.success(request, "Keyword search completed.")
                except HttpError as e:
                    messages.error(request, str(e))
                except Exception as e:
                    messages.error(request, f"Error running keyword search: {e}")
            return redirect("jobs_keywords")

    # GET: render page
    try:
        resumes = api_jobs_list_resumes(request)
    except Exception as e:
        messages.error(request, f"Could not load resumes: {e}")
        resumes = []

    if not entries:
        entries = [{"keyword": "", "resume_id": resumes[0].id if resumes else None}]
        request.session["keyword_entries"] = entries

    keyword_results = request.session.get("keyword_results") or []
    keyword_errors = request.session.get("keyword_errors") or []

    # Applied jobs list
    try:
        applied_matches = api_jobs_matches(request, resume_id=None, min_score=None, status="applied")
    except HttpError as e:
        messages.error(request, str(e))
        applied_matches = []
    except Exception as e:
        messages.error(request, f"Error loading applied jobs: {e}")
        applied_matches = []

    context = {
        "entries": entries,
        "resumes": resumes,
        "keyword_results": keyword_results,
        "keyword_errors": keyword_errors,
        "applied_matches": applied_matches,
    }
    return render(request, "resume_app/jobs_keywords.html", context)


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
