"""Login / API abuse throttling (cache or Redis-backed)."""
from __future__ import annotations

import logging
import time
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


def _client_ip(request: HttpRequest) -> str:
    """Client address, ignoring untrusted X-Forwarded-For entries.

    TRUSTED_PROXY_COUNT is how many proxies append to the header. Zero uses
    REMOTE_ADDR only, so a client cannot pick a fresh throttle bucket by
    sending its own X-Forwarded-For.
    """
    trusted = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0)
    if trusted <= 0:
        return request.META.get("REMOTE_ADDR") or "unknown"
    forwarded = [
        part.strip()
        for part in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
        if part.strip()
    ]
    if len(forwarded) >= trusted:
        return forwarded[-trusted]
    return request.META.get("REMOTE_ADDR") or "unknown"


def _throttle_key(scope: str, identity: str) -> str:
    return f"abuse:v1:{scope}:{identity}"


def is_rate_limited(scope: str, identity: str, *, limit: int, window_seconds: int) -> bool:
    """
    Return True if identity has exceeded ``limit`` hits in ``window_seconds``.
    Uses Django cache (configure Redis cache in production for multi-process).
    """
    if limit <= 0:
        return False
    now = int(time.time())
    window = max(1, int(window_seconds))
    key = _throttle_key(scope, f"{identity}:{now // window}")
    # add + incr is atomic on Redis. A lost race between two adds only
    # under-counts by one, which still bounds the window.
    cache.add(key, 0, timeout=window)
    try:
        count = int(cache.incr(key))
    except ValueError:
        cache.set(key, 1, timeout=window)
        count = 1
    return count > limit


class AbuseThrottleMiddleware(MiddlewareMixin):
    """
    Throttle sensitive auth and API routes by client IP.

    Config (settings):
      ABUSE_THROTTLE_ENABLED (default True)
      ABUSE_AUTH_LIMIT / ABUSE_AUTH_WINDOW_SECONDS
      ABUSE_API_LIMIT / ABUSE_API_WINDOW_SECONDS
    """

    AUTH_PREFIXES = (
        "/accounts/login/",
        "/accounts/signup/",
        "/accounts/password-reset/",
        "/accounts/resend-verification/",
    )

    def process_request(self, request: HttpRequest) -> Optional[HttpResponse]:
        if not getattr(settings, "ABUSE_THROTTLE_ENABLED", True):
            return None
        path = request.path or ""
        ip = _client_ip(request)

        if any(path.startswith(p) for p in self.AUTH_PREFIXES) and request.method == "POST":
            limit = int(getattr(settings, "ABUSE_AUTH_LIMIT", 20))
            window = int(getattr(settings, "ABUSE_AUTH_WINDOW_SECONDS", 300))
            if is_rate_limited("auth", ip, limit=limit, window_seconds=window):
                logger.warning("abuse throttle: auth blocked ip=%s path=%s", ip, path)
                return HttpResponse("Too many requests. Try again later.", status=429)

        if path.startswith("/api/"):
            limit = int(getattr(settings, "ABUSE_API_LIMIT", 120))
            window = int(getattr(settings, "ABUSE_API_WINDOW_SECONDS", 60))
            identity = ip
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                identity = f"u{user.pk}"
            if is_rate_limited("api", identity, limit=limit, window_seconds=window):
                logger.warning("abuse throttle: api blocked id=%s path=%s", identity, path)
                return JsonResponse({"detail": "Too many requests"}, status=429)

        return None
