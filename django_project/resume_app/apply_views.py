"""
Apply Agent UI views: dashboard, single-attempt review, and applicant profile /
agent settings. Submission actions (approve / reject / override URL) post here and
delegate to the orchestrator, mirroring the JSON API in apply_api.py.
"""
from __future__ import annotations

import json

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse

from .apply_agent.adapters import HANDOFF_ONLY_ATS, get_adapter, supported_ats
from .apply_agent import orchestrator, resolve_and_detect
from .apply_agent.browser import apply_browser_headless
from .models import (
    AppAutomationSettings,
    ApplicantProfile,
    ApplicationAttempt,
    LLMProviderConfig,
    LLMProviderPreference,
    PipelineEntry,
)

# Statuses still in flight (shown in the "in progress" group).
_IN_PROGRESS = ApplicationAttempt.ACTIVE_STATUSES + (ApplicationAttempt.Status.AWAITING_APPROVAL,)


def _apply_agent_llm_form_context(settings_solo: AppAutomationSettings) -> dict:
    """Provider/model dropdown data for the dedicated Apply Agent LLM picker."""
    from .llm_services import DEFAULT_MODELS, LLM_PROVIDERS
    from .pipeline_llm_skill_extract import resolve_provider_api_key

    configured = list(
        LLMProviderConfig.objects.exclude(encrypted_api_key="")
        .exclude(encrypted_api_key__isnull=True)
        .values_list("provider", flat=True)
        .distinct()
    )
    if not configured:
        configured = [p for p in sorted(LLM_PROVIDERS) if resolve_provider_api_key(p)]
    providers = sorted(set(configured))

    models_by_provider: dict[str, list[str]] = {}
    for provider in providers:
        models: list[str] = []
        cfg = LLMProviderConfig.objects.filter(provider=provider).first()
        if cfg and (cfg.default_model or "").strip():
            models.append(cfg.default_model.strip())
        for row in LLMProviderPreference.objects.filter(provider_config__provider=provider).order_by(
            "priority", "id"
        ):
            model = (row.model or "").strip()
            if model and model not in models:
                models.append(model)
        fallback = DEFAULT_MODELS.get(provider)
        if fallback and fallback not in models:
            models.append(fallback)
        models_by_provider[provider] = models

    selected_provider = (settings_solo.apply_agent_llm_provider or "").strip()
    selected_model = (settings_solo.apply_agent_llm_model or "").strip()
    effective_provider = selected_provider
    effective_model = selected_model
    if not effective_provider and providers:
        from .llm_session import get_runtime_provider_candidates

        runtime = get_runtime_provider_candidates()
        if runtime:
            effective_provider = runtime[0].get("provider") or ""
            effective_model = (
                selected_model
                or runtime[0].get("model")
                or DEFAULT_MODELS.get(effective_provider, "")
            )

    return {
        "apply_agent_llm_providers": providers,
        "apply_agent_llm_models_by_provider": models_by_provider,
        "apply_agent_llm_selected_provider": selected_provider,
        "apply_agent_llm_selected_model": selected_model,
        "apply_agent_llm_effective_provider": effective_provider,
        "apply_agent_llm_effective_model": effective_model,
    }


def apply_agent_dashboard_view(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start":
            entry_ids = [e for e in request.POST.getlist("entry_ids") if e]
            if not entry_ids:
                messages.info(request, "Select at least one job to start the apply agent.")
                return redirect(reverse("apply_agent"))
            from .tasks import run_apply_agent_step

            attempts = orchestrator.start_attempts_for_entries([int(e) for e in entry_ids])
            for attempt in attempts:
                run_apply_agent_step(attempt.id)
            if attempts:
                messages.success(request, f"Started the apply agent for {len(attempts)} job(s).")
            else:
                messages.info(request, "Those jobs already have an apply attempt in progress.")
            return redirect(reverse("apply_agent"))
        return redirect(reverse("apply_agent"))

    settings_solo = AppAutomationSettings.get_solo()
    profile = ApplicantProfile.get_solo()

    attempts = list(
        ApplicationAttempt.objects.select_related("pipeline_entry__job_listing").all()[:100]
    )
    in_progress = [a for a in attempts if a.status in _IN_PROGRESS]
    finished = [a for a in attempts if a.is_terminal]

    # Applying-stage entries without an active attempt are candidates to start.
    active_entry_ids = set(
        ApplicationAttempt.objects.filter(status__in=_IN_PROGRESS).values_list(
            "pipeline_entry_id", flat=True
        )
    )
    candidates = (
        PipelineEntry.objects.select_related("job_listing")
        .filter(stage=PipelineEntry.Stage.APPLYING, removed_at__isnull=True)
        .exclude(id__in=active_entry_ids)
        .order_by("-added_at")[:100]
    )

    context = {
        "settings": settings_solo,
        "profile": profile,
        "profile_complete": bool(profile.full_name and profile.email),
        "in_progress": in_progress,
        "finished": finished,
        "candidates": candidates,
        "supported_ats": supported_ats(),
        "handoff_ats": HANDOFF_ONLY_ATS,
        "mock_resolver": resolve_and_detect.use_mock_resolver(),
    }
    return render(request, "resume_app/apply_agent_dashboard.html", context)


def apply_agent_review_view(request, attempt_id: int):
    attempt = (
        ApplicationAttempt.objects.select_related("pipeline_entry__job_listing", "optimized_resume")
        .filter(id=attempt_id)
        .first()
    )
    if not attempt:
        messages.error(request, "Apply attempt not found.")
        return redirect(reverse("apply_agent"))

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            corrected = bool(request.POST.get("corrected"))
            updated = orchestrator.approve_attempt(attempt_id, corrected=corrected)
            if updated and updated.status == ApplicationAttempt.Status.SUBMITTING:
                from .tasks import run_apply_agent_step

                run_apply_agent_step(attempt_id)
                messages.success(request, "Approved. Submitting the application now.")
            elif updated and updated.status == ApplicationAttempt.Status.SUCCEEDED:
                messages.success(request, "Marked as applied. Job moved to Done.")
            else:
                messages.info(request, "Attempt is not awaiting approval.")
        elif action == "reject":
            orchestrator.reject_attempt(attempt_id)
            messages.success(request, "Attempt rejected.")
        elif action == "override_url":
            url = (request.POST.get("apply_url") or "").strip()
            if not url:
                messages.error(request, "Enter a valid apply URL.")
            else:
                from .tasks import run_apply_agent_step

                orchestrator.set_override_url(attempt_id, url)
                run_apply_agent_step(attempt_id)
                messages.success(request, "Apply URL updated; re-running the agent.")
        return redirect(reverse("apply_agent_review", args=[attempt_id]))

    payload = attempt.fill_payload_json or {}
    payload_items = sorted((str(k), v) for k, v in payload.items())
    steps = list(attempt.steps.all())
    from .apply_agent.step_capture import media_url_for_path

    step_rows = []
    for step in steps:
        step_rows.append(
            {
                "step": step,
                "screenshot_url": media_url_for_path(step.screenshot_path),
            }
        )
    network_errors = []
    for step in steps:
        for entry in step.network_log or []:
            try:
                status = int(entry.get("status", 0))
            except (TypeError, ValueError):
                status = 0
            if status >= 400 or status == 0:
                network_errors.append({"step": step.step_name, **entry})

    settings_solo = AppAutomationSettings.get_solo()
    context = {
        "attempt": attempt,
        "job": attempt.pipeline_entry.job_listing,
        "payload_items": payload_items,
        "steps": steps,
        "step_rows": step_rows,
        "network_errors": network_errors,
        "Status": ApplicationAttempt.Status,
        "is_awaiting": attempt.status == ApplicationAttempt.Status.AWAITING_APPROVAL,
        "is_handoff": attempt.ats_type in HANDOFF_ONLY_ATS,
        "requires_manual_submit": get_adapter(attempt.ats_type) is None,
        "is_active": attempt.status in ApplicationAttempt.ACTIVE_STATUSES,
        "browser_headless": apply_browser_headless(),
        "mock_resolver": resolve_and_detect.use_mock_resolver(),
        "settings": settings_solo,
    }
    return render(request, "resume_app/apply_agent_review.html", context)


def apply_agent_profile_view(request):
    profile = ApplicantProfile.get_solo()
    settings_solo = AppAutomationSettings.get_solo()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_profile":
            profile.full_name = (request.POST.get("full_name") or "").strip()
            profile.email = (request.POST.get("email") or "").strip()
            profile.phone = (request.POST.get("phone") or "").strip()
            profile.location = (request.POST.get("location") or "").strip()
            profile.linkedin_url = (request.POST.get("linkedin_url") or "").strip()
            profile.website_url = (request.POST.get("website_url") or "").strip()
            profile.work_authorization = (request.POST.get("work_authorization") or "").strip()
            profile.requires_sponsorship = bool(request.POST.get("requires_sponsorship"))
            profile.salary_expectation = (request.POST.get("salary_expectation") or "").strip()
            profile.cover_letter_template = (request.POST.get("cover_letter_template") or "").strip()
            profile.include_eeo = bool(request.POST.get("include_eeo"))
            raw_qa = (request.POST.get("custom_qa_pairs") or "").strip()
            if raw_qa:
                try:
                    parsed = json.loads(raw_qa)
                    if not isinstance(parsed, dict):
                        raise ValueError
                    profile.custom_qa_pairs = parsed
                except (ValueError, TypeError):
                    messages.error(request, "Custom Q&A must be a valid JSON object.")
                    return redirect(reverse("apply_agent_profile"))
            else:
                profile.custom_qa_pairs = {}
            profile.save()
            messages.success(request, "Applicant profile saved.")
            return redirect(reverse("apply_agent_profile"))

        if action == "save_settings":
            settings_solo.apply_agent_enabled = bool(request.POST.get("apply_agent_enabled"))
            mode = (request.POST.get("apply_automation_mode") or "semi_auto").strip()
            if mode not in ("semi_auto", "full_auto"):
                mode = "semi_auto"
            settings_solo.apply_automation_mode = mode
            fmt = (request.POST.get("apply_resume_upload_format") or "pdf").strip()
            settings_solo.apply_resume_upload_format = "docx" if fmt == "docx" else "pdf"
            settings_solo.apply_generic_fallback_enabled = bool(
                request.POST.get("apply_generic_fallback_enabled")
            )
            try:
                settings_solo.apply_min_optimizer_score = max(
                    0, min(100, int((request.POST.get("apply_min_optimizer_score") or "0").strip()))
                )
            except ValueError:
                messages.error(request, "Minimum optimizer score must be a whole number.")
                return redirect(reverse("apply_agent_profile"))
            allowed_raw = (request.POST.get("apply_allowed_ats") or "").strip()
            allowed = [a.strip().lower() for a in allowed_raw.split(",") if a.strip()]
            settings_solo.apply_allowed_ats = allowed
            llm_provider = (request.POST.get("apply_agent_llm_provider") or "").strip()
            llm_model = (request.POST.get("apply_agent_llm_model") or "").strip()
            if llm_provider:
                from .llm_services import LLM_PROVIDERS
                from .pipeline_llm_skill_extract import resolve_provider_api_key

                if llm_provider not in LLM_PROVIDERS:
                    messages.error(request, "Invalid Apply Agent LLM provider.")
                    return redirect(reverse("apply_agent_profile"))
                has_key = LLMProviderConfig.objects.filter(
                    provider=llm_provider,
                    encrypted_api_key__isnull=False,
                ).exclude(encrypted_api_key="").exists()
                if not has_key and not resolve_provider_api_key(llm_provider):
                    messages.error(
                        request,
                        f"Apply Agent LLM provider {llm_provider} has no API key configured.",
                    )
                    return redirect(reverse("apply_agent_profile"))
            settings_solo.apply_agent_llm_provider = llm_provider
            settings_solo.apply_agent_llm_model = llm_model
            settings_solo.apply_browser_show_window = bool(request.POST.get("apply_browser_show_window"))
            settings_solo.save()
            messages.success(request, "Apply agent settings saved.")
            return redirect(reverse("apply_agent_profile"))

    context = {
        "profile": profile,
        "settings": settings_solo,
        "custom_qa_json": json.dumps(profile.custom_qa_pairs or {}, indent=2),
        "allowed_ats_text": ", ".join(settings_solo.apply_allowed_ats or []),
        "supported_ats": supported_ats(),
        **_apply_agent_llm_form_context(settings_solo),
        "browser_headless": apply_browser_headless(),
    }
    return render(request, "resume_app/apply_agent_profile.html", context)
