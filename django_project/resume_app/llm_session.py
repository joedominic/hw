"""DB-backed active LLM provider + ordered provider/model preference list."""

from .models import LLMProviderConfig, LLMProviderPreference


def _connected_configs_qs():
    return (
        LLMProviderConfig.objects.filter(encrypted_api_key__isnull=False)
        .exclude(encrypted_api_key="")
    )


def get_provider_preferences():
    """
    Ordered preference list for runtime selection.
    1) active provider first
    2) lower priority first
    3) stable tie-breaker by id
    """
    return _connected_configs_qs().order_by("-is_active", "priority", "id")


def get_provider_preference_rows():
    return LLMProviderPreference.objects.select_related("provider_config").filter(
        provider_config__encrypted_api_key__isnull=False
    ).exclude(provider_config__encrypted_api_key="")


def get_runtime_provider_candidates():
    """
    Ordered runtime candidates as dicts:
    {"provider": str, "model": str|None, "config": LLMProviderConfig}
    - active provider first (config default model)
    - then explicit preference rows by priority (duplicates removed by provider+model)
    - final fallback: first connected provider default model
    """
    candidates = []
    seen = set()

    active_cfg = get_provider_preferences().filter(is_active=True).first()
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

    for row in get_provider_preference_rows().order_by("priority", "id"):
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
        fallback = get_provider_preferences().first()
        if fallback:
            candidates.append(
                {
                    "provider": fallback.provider,
                    "model": fallback.default_model or None,
                    "config": fallback,
                }
            )
    return candidates


def set_active_provider(provider: str) -> bool:
    """Persist active provider in DB. Returns True when changed."""
    provider = (provider or "").strip()
    if not provider:
        return False
    cfg = _connected_configs_qs().filter(provider=provider).first()
    if not cfg:
        return False
    LLMProviderConfig.objects.filter(is_active=True).exclude(provider=provider).update(
        is_active=False
    )
    if not cfg.is_active:
        cfg.is_active = True
        cfg.save(update_fields=["is_active", "updated_at"])
    return True


def get_active_llm_provider(request=None):
    """
    Return DB-backed active provider. If none is active, return first configured by
    preference order and persist it as active.
    """
    active = get_provider_preferences().filter(is_active=True).first()
    if active:
        if request is not None:
            request.session["active_llm_provider"] = active.provider
            request.session.modified = True
        return active.provider

    fallback = get_provider_preferences().first()
    if not fallback:
        return None
    set_active_provider(fallback.provider)
    if request is not None:
        request.session["active_llm_provider"] = fallback.provider
        request.session.modified = True
    return fallback.provider
