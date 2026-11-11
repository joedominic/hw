"""DB-backed active LLM provider + ordered provider/model preference list.

All functions are owner-scoped: they require a ``user`` argument so that
provider configs, preferences, and the active-provider marker are never
shared across tenants.
"""

from __future__ import annotations

from typing import Any

from ..models import LLMProviderConfig, LLMProviderPreference


def _connected_configs_qs(user):
    """Configs that have a non-empty encrypted key, scoped to *user*."""
    return (
        LLMProviderConfig.objects.for_user(user)
        .filter(encrypted_api_key__isnull=False)
        .exclude(encrypted_api_key="")
    )


def get_provider_preferences(user):
    """
    Ordered preference list for runtime selection (user-scoped).

    1) active provider first
    2) lower priority first
    3) stable tie-breaker by id
    """
    return _connected_configs_qs(user).order_by("-is_active", "priority", "id")


def get_provider_preference_rows(user):
    """Return all LLMProviderPreference rows for *user* (via provider_config FK)."""
    return (
        LLMProviderPreference.objects.select_related("provider_config")
        .filter(
            provider_config__owner=user,
            provider_config__encrypted_api_key__isnull=False,
        )
        .exclude(provider_config__encrypted_api_key="")
    )


def get_runtime_provider_candidates(user):
    """
    Ordered runtime candidates as dicts:
      {"provider": str, "model": str|None, "config": LLMProviderConfig}

    - active provider first (config default model)
    - then explicit preference rows by priority (duplicates removed by provider+model)
    - final fallback: first connected provider default model
    """
    candidates = []
    seen = set()

    active_cfg = get_provider_preferences(user).filter(is_active=True).first()
    if active_cfg:
        key = (active_cfg.provider, active_cfg.default_model or "")
        seen.add(key)
        candidates.append(
            {
                "provider": active_cfg.provider,
                "model": active_cfg.default_model or None,
                "config": active_cfg,
            }
        )

    for row in get_provider_preference_rows(user).order_by("priority", "id"):
        cfg = row.provider_config
        model = (row.model or cfg.default_model or "").strip()
        key = (cfg.provider, model)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "provider": cfg.provider,
                "model": model or None,
                "config": cfg,
            }
        )

    if not candidates:
        fallback = get_provider_preferences(user).first()
        if fallback:
            candidates.append(
                {
                    "provider": fallback.provider,
                    "model": fallback.default_model or None,
                    "config": fallback,
                }
            )
    return candidates


def set_active_provider(user, provider: str) -> bool:
    """Persist active provider in DB for *user*. Returns True when changed."""
    provider = (provider or "").strip()
    if not provider:
        return False
    cfg = _connected_configs_qs(user).filter(provider=provider).first()
    if not cfg:
        return False
    LLMProviderConfig.objects.for_user(user).filter(is_active=True).exclude(
        provider=provider
    ).update(is_active=False)
    if not cfg.is_active:
        cfg.is_active = True
        cfg.save(update_fields=["is_active", "updated_at"])
    return True


def get_active_llm_provider(user, request=None):
    """
    Return DB-backed active provider for *user*. If none is active, fall back
    to the first configured provider (by preference order) and persist it.
    """
    active = get_provider_preferences(user).filter(is_active=True).first()
    if active:
        if request is not None:
            request.session["active_llm_provider"] = active.provider
            request.session.modified = True
        return active.provider

    fallback = get_provider_preferences(user).first()
    if not fallback:
        return None
    set_active_provider(user, fallback.provider)
    if request is not None:
        request.session["active_llm_provider"] = fallback.provider
        request.session.modified = True
    return fallback.provider


def resolve_optimizer_provider(
    user,
    request=None,
) -> dict[str, Any]:
    """
    Resolve which LLM provider the Optimizer UI / enqueue path should use.

    Prefers the user's BYOK cloud provider (active provider or from the preference list).
    When none is configured:
    - If the user is on a BYOK plan (e.g. BYOK Rails), platform keys are excluded.
    - Otherwise, falls back to the first cloud platform default for the optimizer writer chain.

    Returns:
        {
            "provider": str | None,
            "model": str | None,
            "using_platform_key": bool,
            "ready": bool,
            "is_byok_plan": bool,
            "blocked_by_byok_plan": bool,
            "error": str | None,
        }
    """
    from .gateway import (
        USAGE_QUERY_OPTIMIZER_WRITER,
        _preference_candidates,
        _system_default_candidates,
        cloud_llm_available,
        is_llm_on_cooldown,
        provider_is_local,
    )
    from .services import DEFAULT_MODELS
    from ..subscriptions import get_user_plan, staff_bypasses_quotas

    plan = get_user_plan(user) if user and getattr(user, "is_authenticated", False) else None
    is_byok_plan = bool(
        plan
        and (
            getattr(plan, "slug", "") == "byok"
            or getattr(plan, "byok_required_for_completion", False)
        )
    )
    is_staff = bool(user and staff_bypasses_quotas(user))

    # 1. Prefer active provider if cloud
    byok = get_active_llm_provider(user, request)
    if byok and not provider_is_local(byok):
        cfg = _connected_configs_qs(user).filter(provider=byok).first()
        model = (cfg.default_model if cfg else None) or DEFAULT_MODELS.get(byok)
        return {
            "provider": byok,
            "model": model,
            "using_platform_key": False,
            "ready": True,
            "is_byok_plan": is_byok_plan,
            "blocked_by_byok_plan": False,
            "error": None,
        }

    # 2. Prefer highest-priority non-local candidate from user's preference list
    for cand in _preference_candidates(user):
        if cand.get("is_local"):
            continue
        prov = (cand.get("provider") or "").strip()
        model = (cand.get("model_get_llm") or cand.get("model") or "").strip() or DEFAULT_MODELS.get(prov)
        if is_llm_on_cooldown(prov, model, user=user):
            continue
        return {
            "provider": prov,
            "model": model,
            "using_platform_key": False,
            "ready": True,
            "is_byok_plan": is_byok_plan,
            "blocked_by_byok_plan": False,
            "error": None,
        }

    # 3. If on a BYOK-only plan without user cloud keys, block platform fallback
    if is_byok_plan and not is_staff:
        plan_name = getattr(plan, "name", "BYOK Rails")
        return {
            "provider": None,
            "model": None,
            "using_platform_key": False,
            "ready": False,
            "is_byok_plan": True,
            "blocked_by_byok_plan": True,
            "error": (
                f"The {plan_name} plan requires configuring your own "
                "cloud API key in Settings -> AI / LLM. Platform-managed API keys are not included on this plan."
            ),
        }

    # 4. Fall back to cloud platform defaults (Free / Pro / Power plans)
    for cand in _system_default_candidates(
        USAGE_QUERY_OPTIMIZER_WRITER,
        user=user,
        only_local=False,
        allow_local=False,
    ):
        prov = (cand.get("provider") or "").strip()
        if not prov or provider_is_local(prov):
            continue
        model = (cand.get("model_get_llm") or cand.get("model") or "").strip() or DEFAULT_MODELS.get(prov)
        if request is not None:
            request.session["active_llm_provider"] = prov
            request.session.modified = True
        return {
            "provider": prov,
            "model": model,
            "using_platform_key": True,
            "ready": True,
            "is_byok_plan": is_byok_plan,
            "blocked_by_byok_plan": False,
            "error": None,
        }

    return {
        "provider": None,
        "model": None,
        "using_platform_key": False,
        "ready": bool(cloud_llm_available(user)),
        "is_byok_plan": is_byok_plan,
        "blocked_by_byok_plan": False,
        "error": None,
    }


set_active_llm_provider = set_active_provider
