"""
Prompt Studio & Workbench Views (Staff Only).
Provides wide-screen 3-pane authoring, in-authoring test harness with dynamic variable
injection, audit version history with restore/pruning, and backward-compatible form actions.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from langchain_core.messages import HumanMessage, SystemMessage

from .models import (
    AtsJudgeProfile,
    JobListing,
    PromptVersionHistory,
    SystemPromptProfile,
    UserResume,
)
from .prompt_store import (
    PROMPT_DEFINITIONS,
    PROMPT_TO_QUERY_KIND_MAP,
    _PROMPT_SPEC,
    clear_all_prompts_in_profile,
    clear_prompt_versions,
    delete_prompt_version,
    extract_template_variables,
    get_all_prompt_model_preferences,
    get_ats_judge_profile_by_id,
    get_ats_judge_profile_display,
    get_available_model_options,
    get_effective_prompts,
    get_prompt_model_preference,
    get_prompt_version,
    get_system_prompt_profile,
    inject_variables_into_template,
    list_ats_judge_profiles,
    list_prompt_versions,
    record_prompt_version,
    restore_prompt_version,
    save_ats_judge_profile,
    save_prompts_to_profile,
    set_prompt_model_preference,
)
from .career_profiler import extract_text_from_user_resume

logger = logging.getLogger(__name__)


def _staff_required(view_fn):
    """Decorator: returns 403 for non-staff users (JSON for APIs, forbidden response for HTML)."""
    from functools import wraps

    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not (request.user and request.user.is_authenticated and request.user.is_staff):
            if (
                request.path.startswith("/resume/prompts/api/")
                or request.content_type == "application/json"
                or "application/json" in request.headers.get("Accept", "")
            ):
                return JsonResponse(
                    {"status": "error", "message": "Staff access required. Please log in as an administrator."},
                    status=403,
                )
            return HttpResponseForbidden("Staff access required.")
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _clean_triple(sys_v: str, usr_v: str, leg_v: str) -> tuple[str, str, str]:
    """Split (system/user) takes precedence over combined (legacy) when either has text."""
    sys_v = (sys_v or "").strip()
    usr_v = (usr_v or "").strip()
    leg_v = (leg_v or "").strip()
    if sys_v or usr_v:
        return "", sys_v, usr_v
    if leg_v:
        return leg_v, "", ""
    return "", "", ""


# ---------------------------------------------------------------------------
# Main Workbench View
# ---------------------------------------------------------------------------


@_staff_required
@ensure_csrf_cookie
def prompt_library_view(request):
    """
    Staff-only Prompt Studio & Workbench:
    Wide-screen 3-pane workbench for authoring, variable injection testing, and revision audits.
    Preserves backwards compatibility for traditional POST actions.
    """
    try:
        prompts = get_effective_prompts(request)
    except Exception:
        prompts = get_effective_prompts(None)

    ats_profiles = list_ats_judge_profiles()

    # Determine active prompt or ATS profile
    selected_prompt_key = (request.GET.get("prompt") or "").strip().lower()
    selected_ats_id = request.GET.get("ats_profile")

    if selected_ats_id:
        try:
            selected_ats_id = int(selected_ats_id)
        except (ValueError, TypeError):
            selected_ats_id = None

    global_ats_ids = {p.pk for p in ats_profiles}
    if selected_ats_id not in global_ats_ids:
        selected_ats_id = None

    selected_ats = None
    ats_prompts: Dict[str, str] = {}
    if selected_ats_id:
        selected_ats = get_ats_judge_profile_by_id(selected_ats_id)
        if selected_ats:
            ats_prompts = get_ats_judge_profile_display(selected_ats)
            selected_prompt_key = f"ats_profile_{selected_ats.pk}"

    valid_keys = {p["key"] for p in PROMPT_DEFINITIONS}
    if not selected_prompt_key or (
        not selected_prompt_key.startswith("ats_profile_")
        and selected_prompt_key not in valid_keys
    ):
        selected_prompt_key = "writer"

    # Backward-compatible POST handlers
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_ats_profile":
            profile_id = request.POST.get("ats_profile_id")
            name = (request.POST.get("ats_profile_name") or "").strip()
            a_sys = request.POST.get("prompt_ats_system") or ""
            a_usr = request.POST.get("prompt_ats_user") or ""
            a_combined = request.POST.get("prompt_ats_combined") or ""
            ats_leg, ats_sys, ats_usr = _clean_triple(a_sys, a_usr, a_combined)
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

            # Record revision
            record_prompt_version(
                prompt_key=f"ats_profile_{prof.pk}",
                system_prompt=ats_sys,
                user_prompt=ats_usr,
                legacy_combined=ats_leg,
                author=request.user,
                change_summary=f"Saved ATS profile '{prof.name}'",
            )
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
            prompts_to_save: Dict[str, str] = {}
            for kind in _PROMPT_SPEC.keys():
                if kind == "ats_judge":
                    continue
                s = request.POST.get(f"prompt_{kind}_system") or ""
                u = request.POST.get(f"prompt_{kind}_user") or ""
                c = request.POST.get(f"prompt_{kind}_combined") or ""
                leg_v, sys_v, usr_v = _clean_triple(s, u, c)
                prompts_to_save[f"{kind}"] = leg_v
                prompts_to_save[f"{kind}_system"] = sys_v
                prompts_to_save[f"{kind}_user"] = usr_v
                # Record revision for this prompt
                record_prompt_version(
                    prompt_key=kind,
                    system_prompt=sys_v,
                    user_prompt=usr_v,
                    legacy_combined=leg_v,
                    author=request.user,
                    change_summary="Batch prompt update from Library",
                )

            save_prompts_to_profile(request, prompts_to_save)
            messages.success(
                request,
                "System prompts saved. All users will use these for optimizer, matching, and tools.",
            )
            return redirect(reverse("prompt_library") + f"?prompt={selected_prompt_key}")

        if action == "reset_prompts":
            try:
                clear_all_prompts_in_profile(request)
                messages.success(request, "Prompts reset to code defaults.")
                return redirect(reverse("prompt_library"))
            except Exception as e:
                messages.error(request, f"Could not reset prompts: {e}")

    # Fetch reference test data for variable harness injection
    user_resumes = list(
        UserResume.objects.for_user(request.user)
        .filter(is_library=True)
        .order_by("-uploaded_at")[:10]
    )
    if not user_resumes:
        user_resumes = list(UserResume.objects.order_by("-uploaded_at")[:10])

    available_jobs = list(
        JobListing.objects.order_by("-id")
        .only("id", "title", "company_name", "location", "description")[:10]
    )

    # Revisions history for selected prompt
    version_history = list_prompt_versions(selected_prompt_key, limit=25)

    # Models configured in Settings & per-prompt model routing preferences
    available_model_options = get_available_model_options(request.user, request)
    prompt_model_preferences = get_all_prompt_model_preferences(request.user)

    # Enrich prompt definitions with currently assigned production model
    enriched_definitions = []
    for defn in PROMPT_DEFINITIONS:
        k = defn["key"]
        assigned = prompt_model_preferences.get(k, "")
        d_copy = dict(defn)
        d_copy["assigned_model"] = assigned
        if assigned and "::" in assigned:
            prov, mod = assigned.split("::", 1)
            d_copy["assigned_model_short"] = mod if mod else prov
        else:
            d_copy["assigned_model_short"] = "Default"
        enriched_definitions.append(d_copy)

    # Enrich ATS profiles with currently assigned production model
    enriched_ats_profiles = []
    for prof in ats_profiles:
        prof_key = f"ats_profile_{prof.pk}"
        assigned = prompt_model_preferences.get(prof_key, "")
        short_lbl = "Default"
        if assigned and "::" in assigned:
            prov, mod = assigned.split("::", 1)
            short_lbl = mod if mod else prov
        enriched_ats_profiles.append({
            "pk": prof.pk,
            "name": prof.name,
            "is_default": prof.is_default,
            "assigned_model": assigned,
            "assigned_model_short": short_lbl,
            "ats_judge": getattr(prof, "ats_judge", ""),
            "ats_judge_system": getattr(prof, "ats_judge_system", ""),
            "ats_judge_user": getattr(prof, "ats_judge_user", ""),
        })

    # Group prompt definitions by category for structured sidebar navigation
    grouped_definitions: Dict[str, List[Dict[str, Any]]] = {}
    for d in enriched_definitions:
        cat = d.get("category", "General")
        grouped_definitions.setdefault(cat, []).append(d)

    context = {
        "prompts": prompts,
        "prompt_definitions": enriched_definitions,
        "grouped_definitions": grouped_definitions,
        "selected_prompt_key": selected_prompt_key,
        "ats_profiles": enriched_ats_profiles,
        "selected_ats": selected_ats,
        "ats_prompts": ats_prompts,
        "available_resumes": user_resumes,
        "available_jobs": available_jobs,
        "version_history": version_history,
        "version_count": len(version_history),
        "available_model_options": available_model_options,
        "prompt_model_preferences": prompt_model_preferences,
    }
    return render(request, "resume_app/prompt_library.html", context)


# ---------------------------------------------------------------------------
# Prompt Studio AJAX APIs
# ---------------------------------------------------------------------------


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_save_api(request):
    """
    Saves a specific prompt template and logs an audit version history snapshot.
    """
    try:
        if request.content_type == "application/json" or (request.body and not request.POST):
            payload = json.loads(request.body.decode("utf-8"))
        else:
            payload = request.POST.dict()
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Malformed JSON: {e}"}, status=400)

    prompt_key = (payload.get("prompt_key") or "").strip()
    sys_val = payload.get("system_prompt") or ""
    usr_val = payload.get("user_prompt") or ""
    leg_val = payload.get("legacy_combined") or ""
    change_summary = (payload.get("change_summary") or "").strip()

    if not prompt_key:
        return JsonResponse({"status": "error", "message": "Missing prompt_key."}, status=400)

    leg_clean, sys_clean, usr_clean = _clean_triple(sys_val, usr_val, leg_val)

    if prompt_key.startswith("ats_profile_"):
        try:
            prof_id = int(prompt_key.replace("ats_profile_", ""))
            prof = get_ats_judge_profile_by_id(prof_id)
            if not prof:
                return JsonResponse({"status": "error", "message": "ATS profile not found."}, status=404)
            save_ats_judge_profile(
                prof,
                ats_judge=leg_clean,
                ats_judge_system=sys_clean,
                ats_judge_user=usr_clean,
            )
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"Failed saving ATS profile: {e}"}, status=500)
    elif prompt_key == "ats_judge":
        default_ats = list_ats_judge_profiles()
        prof = default_ats[0] if default_ats else None
        if prof:
            save_ats_judge_profile(
                prof,
                ats_judge=leg_clean,
                ats_judge_system=sys_clean,
                ats_judge_user=usr_clean,
            )
    else:
        spec = _PROMPT_SPEC.get(prompt_key)
        if not spec:
            return JsonResponse({"status": "error", "message": f"Unknown prompt key '{prompt_key}'."}, status=400)
        leg_a, sys_a, usr_a, _ds, _du = spec
        profile = get_system_prompt_profile()
        setattr(profile, leg_a, leg_clean)
        setattr(profile, sys_a, sys_clean)
        setattr(profile, usr_a, usr_clean)
        profile.save()

    # Optional assigned model persistence
    assigned_model = payload.get("assigned_model")
    if assigned_model is not None:
        if "::" in assigned_model:
            p_prov, p_mod = assigned_model.split("::", 1)
            set_prompt_model_preference(request.user, prompt_key, p_prov, p_mod)
        elif not assigned_model.strip():
            set_prompt_model_preference(request.user, prompt_key, "", "")

    # Record snapshot in history
    version_row = record_prompt_version(
        prompt_key=prompt_key,
        system_prompt=sys_clean,
        user_prompt=usr_clean,
        legacy_combined=leg_clean,
        author=request.user,
        change_summary=change_summary or "Studio workbench update",
    )

    return JsonResponse(
        {
            "status": "success",
            "message": f"Saved {prompt_key} (v{version_row.version_number}).",
            "version_number": version_row.version_number,
            "version_id": version_row.id,
            "created_at": version_row.created_at.strftime("%b %d, %Y %H:%M"),
            "author": request.user.get_username() if request.user else "Admin",
            "assigned_model": assigned_model if assigned_model is not None else "",
        }
    )


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_set_model_preference_api(request):
    """
    Sets or clears the assigned production model routing for a prompt key.
    """
    try:
        if request.content_type == "application/json" or (request.body and not request.POST):
            payload = json.loads(request.body.decode("utf-8"))
        else:
            payload = request.POST.dict()
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Malformed payload: {e}"}, status=400)

    prompt_key = (payload.get("prompt_key") or "").strip()
    model_choice = (payload.get("model_choice") or "").strip()

    if not prompt_key:
        return JsonResponse({"status": "error", "message": "Missing prompt_key."}, status=400)

    if model_choice and "::" in model_choice:
        prov, mod = model_choice.split("::", 1)
        set_prompt_model_preference(request.user, prompt_key, prov, mod)
        msg = f"Assigned {prov} — {mod} to {prompt_key}."
    else:
        set_prompt_model_preference(request.user, prompt_key, "", "")
        msg = f"Reset {prompt_key} to platform default model."

    return JsonResponse({
        "status": "success",
        "message": msg,
        "prompt_key": prompt_key,
        "assigned_model": model_choice,
    })


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_test_run_api(request):
    """
    Executes prompt against live LLM gateway with dynamic variable substitution
    using the specified or assigned model (Provider + Model).
    Does not alter stored database templates.
    """
    try:
        if request.content_type == "application/json" or (request.body and not request.POST):
            payload = json.loads(request.body.decode("utf-8"))
        else:
            payload = request.POST.dict()
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Malformed payload: {e}"}, status=400)

    prompt_key = (payload.get("prompt_key") or "test").strip()
    sys_val = payload.get("system_prompt") or ""
    usr_val = payload.get("user_prompt") or ""
    leg_val = payload.get("legacy_combined") or ""
    variables = payload.get("variables") or {}
    model_choice = (payload.get("model_choice") or payload.get("engine") or "assigned").strip()

    # Dynamic in-memory substitution
    resolved_sys = inject_variables_into_template(sys_val, variables)
    resolved_usr = inject_variables_into_template(usr_val, variables)
    resolved_leg = inject_variables_into_template(leg_val, variables)

    messages_list = []
    if resolved_leg.strip():
        messages_list.append(HumanMessage(content=resolved_leg.strip()))
    else:
        if resolved_sys.strip():
            messages_list.append(SystemMessage(content=resolved_sys.strip()))
        if resolved_usr.strip():
            messages_list.append(HumanMessage(content=resolved_usr.strip()))

    if not messages_list:
        return JsonResponse({"status": "error", "message": "Both system and user prompts are empty."}, status=400)

    from .llm import USAGE_QUERY_PROMPT_STUDIO, invoke_llm_messages
    from .llm.factory import get_llm
    from .models import LLMProviderConfig
    from .crypto import decrypt_api_key

    custom_llm = None
    target_qk = PROMPT_TO_QUERY_KIND_MAP.get(prompt_key, prompt_key)
    model_display = ""
    prefer_loc = False
    only_loc = False

    if model_choice == "assigned" or not model_choice:
        # Check if the prompt has an assigned model
        assigned_prov, assigned_mod = get_prompt_model_preference(request.user, prompt_key)
        if assigned_prov:
            cfg = LLMProviderConfig.objects.for_user(request.user).filter(provider=assigned_prov).first()
            api_key = decrypt_api_key(cfg.encrypted_api_key or "") if cfg else None
            try:
                custom_llm = get_llm(assigned_prov, api_key, assigned_mod or None, user=request.user)
                model_display = f"{assigned_prov} / {assigned_mod or 'default'}"
            except Exception as ex:
                logger.warning("Failed building LLM for assigned model %s: %s", assigned_prov, ex)
    elif "::" in model_choice:
        sel_prov, sel_mod = model_choice.split("::", 1)
        sel_prov = sel_prov.strip()
        sel_mod = sel_mod.strip()
        cfg = LLMProviderConfig.objects.for_user(request.user).filter(provider=sel_prov).first()
        api_key = decrypt_api_key(cfg.encrypted_api_key or "") if cfg else None
        try:
            custom_llm = get_llm(sel_prov, api_key, sel_mod or None, user=request.user)
            model_display = f"{sel_prov} / {sel_mod or 'default'}"
        except Exception as ex:
            logger.warning("Failed building LLM for model %s: %s", sel_prov, ex)
            return JsonResponse({
                "status": "error",
                "message": f"Could not initialize provider '{sel_prov}': {ex}. Please configure an API key in Settings -> LLM.",
            }, status=400)
    elif model_choice == "local":
        prefer_loc = True
        only_loc = True
        model_display = "Local (Ollama)"
    elif model_choice == "cloud":
        prefer_loc = False
        only_loc = False
        model_display = "Cloud (Gateway)"

    start_t = time.perf_counter()

    try:
        response = invoke_llm_messages(
            messages_list,
            user=request.user,
            usage_query_kind=target_qk,
            llm_override=custom_llm,
            prefer_local=prefer_loc,
            only_local=only_loc,
            allow_local=True,
        )
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)

        response_text = getattr(response, "content", None) or str(response)
        if isinstance(response_text, list):
            response_text = " ".join(str(c) for c in response_text)

        # Extract tokens from response usage metadata if available
        usage_meta = getattr(response, "usage_metadata", None) or {}
        in_tok = usage_meta.get("input_tokens") or usage_meta.get("prompt_tokens") or 0
        out_tok = usage_meta.get("output_tokens") or usage_meta.get("completion_tokens") or 0
        if not in_tok and not out_tok:
            in_tok = sum(len(m.content) for m in messages_list) // 4
            out_tok = len(response_text) // 4

        used_label = model_display or getattr(response, "_resume_model", None) or "Gateway Default"

        return JsonResponse(
            {
                "status": "success",
                "output": response_text,
                "latency_ms": elapsed_ms,
                "engine_used": used_label,
                "model_used": used_label,
                "tokens": {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "total_tokens": in_tok + out_tok,
                },
                "resolved_prompt": {
                    "system": resolved_sys,
                    "user": resolved_usr,
                    "legacy": resolved_leg,
                },
            }
        )
    except Exception as e:
        logger.exception("[prompt_studio_test_run] Test invocation error: %s", e)
        elapsed_ms = int((time.perf_counter() - start_t) * 1000)
        return JsonResponse(
            {
                "status": "error",
                "message": str(e),
                "latency_ms": elapsed_ms,
                "engine_used": model_display or "Error",
                "model_used": model_display or "Error",
            },
            status=500,
        )


@csrf_exempt
@_staff_required
@require_http_methods(["GET"])
def prompt_studio_history_api(request):
    """
    Returns JSON list of historical revisions for a prompt_key.
    """
    prompt_key = (request.GET.get("prompt_key") or "writer").strip()
    history = list_prompt_versions(prompt_key, limit=50)

    items = []
    for h in history:
        items.append(
            {
                "id": h.id,
                "version_number": h.version_number,
                "author": h.author.get_username() if h.author else "System",
                "change_summary": h.change_summary or f"Revision v{h.version_number}",
                "created_at": h.created_at.strftime("%b %d, %Y %H:%M"),
                "system_len": len(h.system_prompt or ""),
                "user_len": len(h.user_prompt or ""),
                "legacy_len": len(h.legacy_combined or ""),
            }
        )

    return JsonResponse({"status": "success", "prompt_key": prompt_key, "versions": items})


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_restore_api(request):
    """
    Restores a specific revision into the active profile.
    """
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        payload = request.POST.dict()

    version_id = payload.get("version_id")
    if not version_id:
        return JsonResponse({"status": "error", "message": "Missing version_id."}, status=400)

    ok, msg, data = restore_prompt_version(version_id, user=request.user)
    if not ok:
        return JsonResponse({"status": "error", "message": msg}, status=400)

    return JsonResponse(
        {
            "status": "success",
            "message": msg,
            "system_prompt": data.get("system", ""),
            "user_prompt": data.get("user", ""),
            "legacy_combined": data.get("legacy_combined", ""),
        }
    )


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_delete_version_api(request):
    """
    Deletes a single historical revision from PromptVersionHistory.
    """
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        payload = request.POST.dict()

    version_id = payload.get("version_id")
    if not version_id:
        return JsonResponse({"status": "error", "message": "Missing version_id."}, status=400)

    ok = delete_prompt_version(version_id)
    if not ok:
        return JsonResponse({"status": "error", "message": "Revision not found."}, status=404)

    return JsonResponse({"status": "success", "message": "Revision deleted."})


@csrf_exempt
@_staff_required
@require_http_methods(["POST"])
def prompt_studio_clear_history_api(request):
    """
    Clears all historical revisions for the given prompt_key (or all).
    """
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except Exception:
        payload = request.POST.dict()

    prompt_key = (payload.get("prompt_key") or "").strip() or None
    deleted_count = clear_prompt_versions(prompt_key)

    return JsonResponse(
        {
            "status": "success",
            "message": f"Deleted {deleted_count} revision(s).",
            "deleted_count": deleted_count,
        }
    )


@csrf_exempt
@_staff_required
@require_http_methods(["GET"])
def prompt_studio_resume_content_api(request):
    """
    Extracts and returns the text content of a UserResume for dynamic variable testing.
    """
    resume_id = request.GET.get("resume_id")
    if not resume_id:
        return JsonResponse({"status": "error", "message": "Missing resume_id."}, status=400)

    try:
        resume = UserResume.objects.get(pk=int(resume_id))
        text = extract_text_from_user_resume(resume)
        return JsonResponse({"status": "success", "resume_id": resume.id, "text": text})
    except UserResume.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Resume not found."}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Failed reading resume: {e}"}, status=500)


@csrf_exempt
@_staff_required
@require_http_methods(["GET"])
def prompt_studio_job_content_api(request):
    """
    Fetches job title and description from a JobListing for variable injection testing.
    """
    job_id = request.GET.get("job_id")
    if not job_id:
        return JsonResponse({"status": "error", "message": "Missing job_id."}, status=400)

    try:
        job = JobListing.objects.get(pk=int(job_id))
        return JsonResponse(
            {
                "status": "success",
                "job_id": job.id,
                "title": job.title or "",
                "company": job.company_name or "",
                "description": job.description or "",
            }
        )
    except JobListing.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Job listing not found."}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Failed fetching job: {e}"}, status=500)
