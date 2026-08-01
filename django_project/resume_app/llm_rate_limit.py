"""
Redis-backed RPM + TPM limiting for LLM calls (shared across Huey workers).

Fails open when Redis is unavailable if LLM_RATE_LIMIT_FAIL_OPEN is True.

Preference lookups and Redis keys are scoped per user for BYOK credentials;
platform/env keys share a ``platform`` namespace.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_LLM_COOLDOWN_SECONDS = 300
# Minute-bucket counter TTL. Must exceed the acquire window (60s) and the LLM
# wall-clock deadline (180s) so reconcile/refund cannot recreate expired keys.
# Keep in sync with acquire EXPIRE calls.
RATE_LIMIT_BUCKET_TTL_SECONDS = 300


def _safe_key_part(s: str | None) -> str:
    raw = (s or "").strip()[:96]
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", raw)
    return cleaned or "default"


@dataclass
class _RateLimitReservation:
    redis: object
    req_key: str
    tok_key: str
    estimated_tokens: int
    acquired: bool

    def reconcile_tokens(self, actual_prompt_tokens: int) -> None:
        if not self.acquired or actual_prompt_tokens < 0:
            return
        delta = int(actual_prompt_tokens) - int(self.estimated_tokens)
        if delta == 0:
            return
        try:
            # INCRBY recreates an expired key with no TTL; always re-apply expiry.
            pipe = self.redis.pipeline()
            pipe.incrby(self.tok_key, delta)
            pipe.expire(self.tok_key, RATE_LIMIT_BUCKET_TTL_SECONDS)
            pipe.execute()
        except Exception as e:
            logger.warning("llm_rate_limit token reconcile failed: %s", e)

    def release_on_invoke_failure(self) -> None:
        """Undo TPM reservation and request slot when the HTTP call did not run or failed before usage."""
        if not self.acquired:
            return
        try:
            # DECR/INCRBY recreate expired keys with no TTL (LLM wall-clock can
            # exceed RATE_LIMIT_BUCKET_TTL_SECONDS). Re-apply expiry so refunds
            # cannot leave permanent negative counters behind.
            pipe = self.redis.pipeline()
            pipe.decr(self.req_key)
            pipe.expire(self.req_key, RATE_LIMIT_BUCKET_TTL_SECONDS)
            pipe.incrby(self.tok_key, -int(self.estimated_tokens))
            pipe.expire(self.tok_key, RATE_LIMIT_BUCKET_TTL_SECONDS)
            pipe.execute()
        except Exception as e:
            logger.warning("llm_rate_limit release_on_invoke_failure failed: %s", e)


_redis_client: object | None = None
_redis_client_lock = threading.Lock()


def _get_redis():
    """Return a process-wide Redis client (one connection pool per Huey worker)."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_client_lock:
        if _redis_client is not None:
            return _redis_client
        from django.conf import settings

        import redis

        url = getattr(settings, "LLM_RATE_LIMIT_REDIS_URL", None)
        if url:
            _redis_client = redis.from_url(url, decode_responses=True)
        else:
            db = getattr(settings, "LLM_RATE_LIMIT_REDIS_DB", settings.HUEY_REDIS_DB)
            kwargs = {
                "host": settings.HUEY_REDIS_HOST,
                "port": settings.HUEY_REDIS_PORT,
                "db": db,
                "decode_responses": True,
            }
            # Shared Redis AUTH / TLS (see core.settings REDIS_PASSWORD / REDIS_USE_TLS).
            password = getattr(settings, "REDIS_PASSWORD", "")
            if password:
                kwargs["password"] = password
            if getattr(settings, "REDIS_USE_TLS", False):
                kwargs["ssl"] = True
                kwargs["ssl_cert_reqs"] = None
            _redis_client = redis.Redis(**kwargs)
        return _redis_client


def _rate_limit_scope_key(user, provider: str) -> str:
    """BYOK → per-user bucket; platform/env keys → shared platform bucket."""
    if user is None or not getattr(user, "is_authenticated", False):
        return "platform"
    try:
        from .models import LLMProviderConfig

        has_byok = (
            LLMProviderConfig.objects.for_user(user)
            .filter(provider=provider)
            .exclude(encrypted_api_key="")
            .exists()
        )
        if has_byok:
            return f"u{int(user.pk)}"
    except Exception:
        pass
    return "platform"


def _get_limits_from_preferences(
    provider: str, model: str | None, *, user
) -> Optional[tuple[int, int]]:
    """Match LLMProviderPreference row: exact model first, then blank model as provider wildcard."""
    try:
        from .models import LLMProviderPreference
    except Exception:
        return None
    if user is None:
        return None
    prov = (provider or "").strip()
    if not prov:
        return None
    m = (model or "").strip()
    base = (
        LLMProviderPreference.objects.select_related("provider_config")
        .filter(provider_config__owner=user, provider_config__provider=prov)
        .order_by("priority", "id")
    )
    picked = base.filter(model=m).first() if m else None
    if picked is None:
        picked = base.filter(model="").first()
    if picked is None:
        return None
    rpm, tpm = picked.rate_limit_rpm, picked.rate_limit_tpm
    if rpm and tpm and int(rpm) > 0 and int(tpm) > 0:
        return (int(rpm), int(tpm))
    return None


def get_preference_row_for_provider_model(provider: str, model: str | None, *, user):
    """Return matching LLMProviderPreference or None (exact model, then blank model)."""
    try:
        from .models import LLMProviderPreference
    except Exception:
        return None
    if user is None:
        return None
    prov = (provider or "").strip()
    if not prov:
        return None
    m = (model or "").strip()
    base = (
        LLMProviderPreference.objects.select_related("provider_config")
        .filter(provider_config__owner=user, provider_config__provider=prov)
        .order_by("priority", "id")
    )
    picked = base.filter(model=m).first() if m else None
    if picked is None:
        picked = base.filter(model="").first()
    return picked


def get_cooldown_seconds_for_provider_model(provider: str, model: str | None, *, user) -> int:
    row = get_preference_row_for_provider_model(provider, model, user=user)
    if row and row.rate_limit_cooldown_seconds and int(row.rate_limit_cooldown_seconds) > 0:
        return int(row.rate_limit_cooldown_seconds)
    return DEFAULT_LLM_COOLDOWN_SECONDS


def llm_cooldown_redis_key(provider: str, model: str | None, *, user) -> str:
    sk_model = _safe_key_part(model)
    scope = _rate_limit_scope_key(user, provider)
    return f"llm:cooldown:v2:{scope}:{_safe_key_part(provider)}:{sk_model}"


def set_llm_cooldown(provider: str, model: str | None, seconds: int, *, user) -> None:
    """Mark provider+model unavailable for approximately `seconds` (Redis TTL)."""
    if seconds <= 0:
        return
    try:
        r = _get_redis()
        r.setex(llm_cooldown_redis_key(provider, model, user=user), int(seconds), "1")
    except Exception as e:
        logger.warning("llm cooldown set failed: %s", e)


def get_llm_cooldown_ttl(provider: str, model: str | None, *, user) -> Optional[int]:
    """Remaining cooldown seconds, or None if unavailable / key missing / Redis error."""
    try:
        r = _get_redis()
        ttl = r.ttl(llm_cooldown_redis_key(provider, model, user=user))
    except Exception as e:
        logger.debug("llm cooldown ttl failed: %s", e)
        return None
    if ttl is None or ttl < 0:
        return None
    return int(ttl)


def is_llm_on_cooldown(provider: str, model: str | None, *, user) -> bool:
    ttl = get_llm_cooldown_ttl(provider, model, user=user)
    return ttl is not None and ttl > 0


def _get_limits_from_settings(provider: str) -> Optional[tuple[int, int]]:
    from django.conf import settings

    cfg = getattr(settings, "LLM_RATE_LIMIT_BY_PROVIDER", None) or {}
    lim = cfg.get((provider or "").strip())
    if not lim or not isinstance(lim, (tuple, list)) or len(lim) != 2:
        return None
    rpm, tpm = int(lim[0]), int(lim[1])
    if rpm <= 0 or tpm <= 0:
        return None
    return (rpm, tpm)


def _get_limits(provider: str, model: str | None = None, *, user) -> Optional[tuple[int, int]]:
    from django.conf import settings

    if not getattr(settings, "LLM_RATE_LIMIT_ENABLED", False):
        return None
    db_lim = _get_limits_from_preferences(provider, model, user=user)
    if db_lim:
        return db_lim
    return _get_limits_from_settings(provider)


def _try_acquire_once_internal(
    provider: str,
    model: str | None,
    estimated_input_tokens: int,
    *,
    user,
) -> Optional[_RateLimitReservation]:
    lim = _get_limits(provider, model, user=user)
    if not lim:
        return None
    rpm, tpm = lim
    est = max(1, int(estimated_input_tokens))
    sk_model = _safe_key_part(model)
    scope = _rate_limit_scope_key(user, provider)
    try:
        r = _get_redis()
    except Exception as e:
        logger.warning("llm_rate_limit redis connect failed: %s", e)
        return None
    minute = int(time.time() // 60)
    req_key = f"llm:rl:v2:req:{scope}:{_safe_key_part(provider)}:{sk_model}:{minute}"
    tok_key = f"llm:rl:v2:tok:{scope}:{_safe_key_part(provider)}:{sk_model}:{minute}"
    import redis as redis_lib

    try:
        pipe = r.pipeline()
        while True:
            try:
                pipe.watch(req_key, tok_key)
                cur_req = int(pipe.get(req_key) or 0)
                cur_tok = int(pipe.get(tok_key) or 0)
                if cur_req + 1 > rpm or cur_tok + est > tpm:
                    pipe.unwatch()
                    return None
                pipe.multi()
                pipe.incr(req_key)
                pipe.incrby(tok_key, est)
                pipe.expire(req_key, RATE_LIMIT_BUCKET_TTL_SECONDS)
                pipe.expire(tok_key, RATE_LIMIT_BUCKET_TTL_SECONDS)
                pipe.execute()
                return _RateLimitReservation(
                    redis=r, req_key=req_key, tok_key=tok_key, estimated_tokens=est, acquired=True
                )
            except redis_lib.WatchError:
                continue
            finally:
                pipe.reset()
    except Exception as e:
        logger.warning("llm_rate_limit acquire transaction failed: %s", e)
        return None


def try_acquire_llm_slot(
    provider: str,
    model: str | None,
    estimated_input_tokens: int,
    *,
    user,
    prefer_failover: bool = False,
) -> tuple[Optional[Callable[[int], None]], Optional[Callable[[], None]]]:
    """
    Single attempt (current minute window) — no blocking wait.
    Returns (reconcile, release) like acquire_llm_slot if a limit applies and slot taken,
    (noop, noop) if rate limiting disabled / no limits for this pair,
    (None, None) if limits apply but the bucket is full (try another LLM).

    When prefer_failover=True (gateway multi-LLM path), a full bucket always returns
    (None, None) so the caller can try the next candidate — never bypass limits via fail_open.
    """
    from django.conf import settings

    lim = _get_limits(provider, model, user=user)
    if not lim:
        noop = lambda *a, **k: None
        return noop, noop

    fail_open = getattr(settings, "LLM_RATE_LIMIT_FAIL_OPEN", False) and not prefer_failover
    try:
        res = _try_acquire_once_internal(provider, model, estimated_input_tokens, user=user)
    except Exception as e:
        logger.warning("try_acquire_llm_slot failed: %s", e)
        if fail_open:
            noop = lambda *a, **k: None
            return noop, noop
        return None, None
    if res is not None:

        def _reconcile(actual: int, _r=res):
            _r.reconcile_tokens(actual)

        def _release(_r=res):
            _r.release_on_invoke_failure()

        return _reconcile, _release
    if fail_open:
        noop = lambda *a, **k: None
        return noop, noop
    return None, None


def estimate_tokens_from_messages(messages, extra_chars: int = 0) -> int:
    """Rough input token estimate (chars/4) for all message contents."""
    total_chars = int(extra_chars)
    for m in messages or []:
        c = getattr(m, "content", None)
        if c is None:
            continue
        if isinstance(c, list):
            total_chars += sum(
                len(str(x.get("text", x)) if isinstance(x, dict) else str(x)) for x in c
            )
        else:
            total_chars += len(str(c))
    return max(1, total_chars // 4)


def acquire_llm_slot(
    provider: str,
    model: str | None,
    estimated_input_tokens: int,
    *,
    user,
) -> tuple[Callable[[int], None], Callable[[], None]]:
    """
    Reserve one request and estimated_input_tokens against the current minute window.

    Returns:
      (reconcile_fn, release_fn) where:
        - reconcile_fn(actual_prompt_tokens) adjusts TPM for prediction error.
        - release_fn() undoes reservation if invoke never succeeded.
    """
    from django.conf import settings

    lim = _get_limits(provider, model, user=user)
    if not lim:
        noop = lambda *a, **k: None
        return noop, noop

    fail_open = getattr(settings, "LLM_RATE_LIMIT_FAIL_OPEN", False)
    max_wait = float(getattr(settings, "LLM_RATE_LIMIT_MAX_WAIT_SECONDS", 120))

    deadline = time.time() + max_wait
    try:
        _get_redis()
    except Exception as e:
        logger.warning("llm_rate_limit redis connect failed: %s", e)
        if fail_open:
            noop = lambda *a, **k: None
            return noop, noop
        raise

    while time.time() < deadline:
        res = _try_acquire_once_internal(provider, model, estimated_input_tokens, user=user)
        if res is not None:

            def _reconcile(actual: int, _r=res):
                _r.reconcile_tokens(actual)

            def _release(_r=res):
                _r.release_on_invoke_failure()

            return _reconcile, _release
        sleep_for = min(1.5, max(0.05, deadline - time.time()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for + random.uniform(0, 0.08))

    if fail_open:
        logger.warning(
            "llm_rate_limit exceeded wait (%ss) for %s/%s; fail_open allowing request",
            max_wait,
            provider,
            model,
        )
        noop = lambda *a, **k: None
        return noop, noop

    raise RuntimeError(
        f"LLM rate limit: could not acquire slot within {int(max_wait)}s for {provider}/{model}"
    )
