"""DB-backed active LLM provider + ordered provider/model preference list.

All functions are owner-scoped: they require a ``user`` argument so that
provider configs, preferences, and the active-provider marker are never
shared across tenants.
"""

from .models import LLMProviderConfig, LLMProviderPreference


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
