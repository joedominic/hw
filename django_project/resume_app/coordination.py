"""Locks and periodic-task slots shared across web and worker processes.

Locks live on the Huey Redis database (the noeviction queue instance once Redis
is split). Django's cache is the evictable cache and must not hold them.
Tests and ``HUEY_IMMEDIATE`` use the Django cache so no Redis server is required.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

_redis_client = None


def locks_use_redis() -> bool:
    """True when locks must be shared across processes."""
    if getattr(settings, "APP_ENV", "development") == "test":
        return False
    if getattr(settings, "HUEY_IMMEDIATE", False):
        return False
    return True


def _redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    import redis

    kwargs = {
        "host": settings.HUEY_REDIS_HOST,
        "port": settings.HUEY_REDIS_PORT,
        "db": settings.HUEY_REDIS_DB,
        "decode_responses": True,
        "socket_timeout": 2,
    }
    password = getattr(settings, "REDIS_PASSWORD", "") or ""
    if password:
        kwargs["password"] = password
    if getattr(settings, "REDIS_USE_TLS", False):
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = getattr(settings, "REDIS_SSL_CERT_REQS_VALUE", None)
        ca = getattr(settings, "REDIS_SSL_CA", "") or ""
        if ca:
            kwargs["ssl_ca_certs"] = ca
    _redis_client = redis.Redis(**kwargs)
    return _redis_client


def _key(name: str) -> str:
    return f"lock:{name}"


def lock_add(key: str, value, timeout: int) -> bool:
    """SET NX with a TTL. True when this caller acquired the key."""
    ttl = max(1, int(timeout))
    if not locks_use_redis():
        return bool(cache.add(key, value, timeout=ttl))
    try:
        return bool(_redis().set(_key(key), value, nx=True, ex=ttl))
    except Exception:
        logger.exception("lock_add failed for %s", key)
        raise


def lock_get(key: str):
    if not locks_use_redis():
        return cache.get(key)
    try:
        return _redis().get(_key(key))
    except Exception:
        logger.exception("lock_get failed for %s", key)
        raise


def lock_delete(key: str) -> None:
    if not locks_use_redis():
        cache.delete(key)
        return
    try:
        _redis().delete(_key(key))
    except Exception:
        logger.exception("lock_delete failed for %s", key)
        raise


def scheduled_fire(cron_expr: str, now: datetime | None = None) -> datetime:
    """Floor ``now`` to the latest time ``cron_expr`` would have fired.

    A task that starts late still maps to the fire it belongs to, until the
    next fire, when it coalesces with that later tick.
    """
    from croniter import croniter

    moment = now or timezone.now()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    fire = croniter(cron_expr, moment).get_prev(datetime)
    if timezone.is_naive(fire):
        fire = timezone.make_aware(fire, timezone.get_current_timezone())
    return fire


def seconds_until_next_fire(cron_expr: str, fire: datetime) -> int:
    from croniter import croniter

    nxt = croniter(cron_expr, fire).get_next(datetime)
    if timezone.is_naive(nxt):
        nxt = timezone.make_aware(nxt, timezone.get_current_timezone())
    return max(1, int((nxt - fire).total_seconds()))
