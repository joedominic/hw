"""
Settings view and modular tab handlers:
- Account tab: handled via account_views.
- LLM tab: provider configs, active provider selection, preferences, rate limits, model lists.
- App Automation tab: stage promotion thresholds, auto-purges, retention days, pipeline deduplication.
- Replacements tab: export replacement token mappings.
- Usage tab: daily quota counters, LLM token and request usage ledgers.
- Prompt Routing tab: prompt and workflow model overrides.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from langchain_core.messages import HumanMessage
from ninja.errors import HttpError

from .account_views import account_settings_context, handle_account_settings_post
from .api import ConnectRequest, llm_connect as api_llm_connect
from .crypto import decrypt_api_key
from .experience import is_power_user, mark_onboarding_step, set_experience_mode
from .job_dedupe import dedupe_pipeline_entries
from .llm import (
    LLM_PROVIDERS,
    USAGE_QUERY_LABELS,
    get_active_llm_provider,
    get_llm,
    list_models_for_provider,
    log_llm_invoke,
)
from .llm.gateway import (
    USAGE_QUERY_COVER_LETTER,
    USAGE_QUERY_INTERVIEW_PREP,
    USAGE_QUERY_JD_CLEANSE,
    USAGE_QUERY_MATCHING,
    USAGE_QUERY_OPTIMIZER_ATS_JUDGE,
    USAGE_QUERY_OPTIMIZER_RECRUITER_JUDGE,
    USAGE_QUERY_OPTIMIZER_WRITER,
    USAGE_QUERY_PIPELINE_SKILL_EXTRACT,
)
from .llm.rate_limit import get_llm_cooldown_ttl
from .llm.session import (
    get_provider_preference_rows,
    get_provider_preferences,
    set_active_provider,
)
from .models import (
    AppAutomationSettings,
    LLMAppUsageTotals,
    LLMDailyUsageBreakdown,
    LLMProviderConfig,
    LLMProviderPreference,
    LLMUsageByModel,
    LLMUsageByQuery,
    TenantPromptModelPreference,
    Track,
    UsageCounter,
    UserExperienceSettings,
)
from .prompt_store import get_optimizer_workflow_by_id, list_optimizer_workflows
from .subscriptions import (
    METRIC_LLM_REQUESTS,
    METRIC_LLM_TOKENS,
    subscription_summary,
)
from .tenancy import get_active_user, get_real_user

logger = logging.getLogger(__name__)


def _save_optimizer_context(
    user,
    optimization_notes: str,
    pipeline_skills_json: str,
    job_highlights: str,
) -> None:
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


def _get_optimizer_context(request) -> dict[str, str]:
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
    return {
        "optimization_notes": notes,
        "pipeline_skills_json": skills,
        "job_highlights": highlights,
    }


def _handle_experience_mode(request, user):
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


def _handle_refresh_provider_models(request, user):
    cached: dict[str, list] = {}
    for cfg in get_provider_preferences(user):
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


def _handle_reset_today_quotas(request, user):
    today = timezone.localdate()
    UsageCounter.objects.for_user(user).filter(period_date=today).delete()
    LLMDailyUsageBreakdown.objects.for_user(user).filter(period_date=today).delete()
    messages.success(
        request,
        f"Today's daily quota counters ({today.strftime('%b %d, %Y')}) were reset. You can now make new LLM calls.",
    )
    return redirect(reverse("settings") + "?tab=usage")


def _handle_reset_llm_usage_stats(request, user):
    if not request.user.is_staff:
        messages.error(request, "Permission denied.")
        return redirect(reverse("settings") + "?tab=usage")
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


def _handle_save_stop_llm_requests(request, user):
    automation = AppAutomationSettings.get_for_user(user)
    automation.stop_llm_requests = bool(request.POST.get("stop_llm_requests"))
    automation.save(update_fields=["stop_llm_requests", "updated_at"])
    messages.success(request, "LLM safety settings saved.")
    return redirect(reverse("settings") + "?tab=llm")


def _handle_save_app_automation(request, user):
    automation = AppAutomationSettings.get_for_user(user)
    automation.pipeline_to_vetting_enabled = bool(request.POST.get("pipeline_to_vetting_enabled"))
    automation.vetting_to_applying_enabled = bool(request.POST.get("vetting_to_applying_enabled"))
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
        vip = int((request.POST.get("vetting_interview_probability_min") or "70").strip())
    except ValueError:
        messages.error(request, "Interview probability threshold must be a whole number.")
        return redirect(reverse("settings") + "?tab=app")
    if vip < 0 or vip > 100:
        messages.error(request, "Interview probability must be between 0 and 100.")
        return redirect(reverse("settings") + "?tab=app")
    automation.vetting_interview_probability_min = vip
    raw_wf = (request.POST.get("applying_optimizer_workflow") or "").strip()
    if raw_wf:
        wf = get_optimizer_workflow_by_id(int(raw_wf), user) if raw_wf.isdigit() else None
        if wf is None:
            messages.error(request, "Invalid optimizer workflow selection.")
            return redirect(reverse("settings") + "?tab=app")
        automation.applying_optimizer_workflow = wf
    else:
        automation.applying_optimizer_workflow = None

    _save_optimizer_context(
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
    automation.cleanup_done_retention_days = 0
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


def _handle_save_export_replacements(request, user):
    replacements = []
    for i in range(5):
        token = (request.POST.get(f"replacement_token_{i}") or "").strip()
        value = (request.POST.get(f"replacement_value_{i}") or "").strip()
        replacements.append({"token": token, "value": value})
    automation = AppAutomationSettings.get_for_user(user)
    stored = automation.set_export_replacements(replacements)
    request.session["export_replacements"] = stored
    request.session.modified = True
    messages.success(request, "Export replacement tokens saved.")
    return redirect(reverse("settings") + "?tab=replacements")


def _handle_dedupe_pipeline(request, user):
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


def _handle_save_provider_preferences(request, user):
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
    pref_model_tiers = request.POST.getlist("pref_model_tier")
    row_count = max(
        len(pref_ids),
        len(pref_providers),
        len(pref_models),
        len(pref_priorities),
        len(pref_rpms),
        len(pref_tpms),
        len(pref_cooldowns),
        len(pref_is_locals),
        len(pref_model_tiers),
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
        raw_tier = (pref_model_tiers[i] if i < len(pref_model_tiers) else "").strip().lower()
        if raw_tier not in ("slm", "frontier"):
            from .llm.gateway import classify_model_tier
            raw_tier = classify_model_tier(provider, model)
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
        pref_obj.model_tier = raw_tier
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


def _handle_connect(request, user, provider_infos):
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
            had_active_provider = bool(get_active_llm_provider(user, request))
            api_llm_connect(request, ConnectRequest(provider=provider, api_key=api_key))
            if not had_active_provider:
                set_active_provider(user, provider)
                request.session["active_llm_provider"] = provider
                request.session.modified = True
            request.session.pop("settings_provider_models_map", None)
            messages.success(request, f"API key for {provider} validated and saved.")
            mark_onboarding_step(user, "llm")
            return redirect(reverse("settings") + "?tab=llm")
        except HttpError as e:
            messages.error(request, str(e))
    return None


def _handle_set_active_provider(request, user, provider_infos):
    provider = (request.POST.get("active_provider") or "").strip()
    valid_connected = any(info["name"] == provider and info["key_stored"] for info in provider_infos)
    if not valid_connected:
        messages.error(request, "Choose a connected provider.")
    else:
        set_active_provider(user, provider)
        request.session["active_llm_provider"] = provider
        request.session.modified = True
        messages.success(request, f"{provider} is now the active provider.")
        return redirect(reverse("settings") + "?tab=llm")
    return None


def _handle_save_prompt_model_preferences(request, user):
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


def settings_view(request):
    """
    Settings controller: dispatches POST actions and renders the modular tab context.
    """
    user = get_active_user(request)
    active_provider = get_active_llm_provider(user, request)
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
        account_response = handle_account_settings_post(request)
        if account_response is not None:
            return account_response

        action = request.POST.get("action")
        if action == "save_experience_mode":
            return _handle_experience_mode(request, user)
        if action == "refresh_provider_models":
            return _handle_refresh_provider_models(request, user)
        if action == "reset_today_quotas":
            return _handle_reset_today_quotas(request, user)
        if action == "reset_llm_usage_stats":
            return _handle_reset_llm_usage_stats(request, user)
        if action == "save_stop_llm_requests":
            return _handle_save_stop_llm_requests(request, user)
        if action == "save_app_automation":
            return _handle_save_app_automation(request, user)
        if action == "save_export_replacements":
            return _handle_save_export_replacements(request, user)
        if action == "dedupe_pipeline":
            return _handle_dedupe_pipeline(request, user)
        if action == "save_provider_preferences":
            return _handle_save_provider_preferences(request, user)
        if action == "connect":
            resp = _handle_connect(request, user, provider_infos)
            if resp:
                return resp
        elif action == "set_active_provider":
            resp = _handle_set_active_provider(request, user, provider_infos)
            if resp:
                return resp
        elif action == "save_prompt_model_preferences":
            return _handle_save_prompt_model_preferences(request, user)

    tab = (request.GET.get("tab") or "account").strip().lower()
    allowed_tabs = ["account", "llm", "usage", "replacements"]
    if request.user.is_staff:
        allowed_tabs.append("app")
    if tab not in allowed_tabs:
        tab = "account"
    provider_preference_list = list(get_provider_preferences(user))
    connected_provider_names = [cfg.provider for cfg in provider_preference_list]
    pref_rows = list(get_provider_preference_rows(user).order_by("priority", "id"))
    if not pref_rows and connected_provider_names:
        for cfg in provider_preference_list:
            LLMProviderPreference.objects.create(
                provider_config=cfg,
                model=cfg.default_model or "",
                priority=cfg.priority,
            )
        pref_rows = list(get_provider_preference_rows(user).order_by("priority", "id"))
    raw_session_models = request.session.get("settings_provider_models_map")
    if not isinstance(raw_session_models, dict):
        models_cache: dict[str, list] = {}
    else:
        models_cache = {k: list(v or []) for k, v in raw_session_models.items()}

    def _models_for_settings_preferences(provider: str, cfg: LLMProviderConfig) -> list:
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

    optimizer_supporting_context = _get_optimizer_context(request)
    automation_for_replacements = AppAutomationSettings.get_for_user(user)
    raw_replacements = automation_for_replacements.export_replacements or []
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
