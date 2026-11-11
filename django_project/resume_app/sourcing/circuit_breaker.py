"""
Circuit breaker pattern for third-party job scrapers and external sourcing APIs.

Prevents cascading latency spikes and repeated connection timeouts by fast-failing
boards that are rate-limited, blocked by Cloudflare/anti-bot systems, or down.
Allows healthy sources to proceed, ensuring graceful partial search results.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

T = TypeVar("T")

STATE_CLOSED = "CLOSED"      # Healthy: calls pass through
STATE_OPEN = "OPEN"          # Tripped: fast-fail immediately without network calls
STATE_HALF_OPEN = "HALF_OPEN"# Cooldown passed: allow single trial probe call


class CircuitBreakerOpenException(Exception):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""

    def __init__(self, source: str, cooldown_remaining: float, last_error: str = ""):
        self.source = source
        self.cooldown_remaining = max(0.0, round(cooldown_remaining, 1))
        self.last_error = last_error
        msg = f"Job source '{source}' circuit breaker is OPEN ({self.cooldown_remaining}s cooldown remaining)."
        if last_error:
            msg += f" Last error: {last_error}"
        super().__init__(msg)


class SourceCircuitBreaker:
    """
    Distributed, cache-backed circuit breaker for a named external job board.
    """

    CACHE_PREFIX = "circuit_breaker:job_source:"

    def __init__(
        self,
        source: str,
        failure_threshold: Optional[int] = None,
        cooldown_seconds: Optional[int] = None,
    ):
        self.source = source.strip().lower()
        self.failure_threshold = failure_threshold or getattr(
            settings, "JOB_SOURCE_CB_FAILURE_THRESHOLD", 3
        )
        self.cooldown_seconds = cooldown_seconds or getattr(
            settings, "JOB_SOURCE_CB_COOLDOWN_SECONDS", 180
        )
        self._cache_key = f"{self.CACHE_PREFIX}{self.source}"

    def _ttl(self) -> int:
        return max(1, int(self.cooldown_seconds) * 3)

    def _fail_key(self) -> str:
        return f"{self._cache_key}:failures"

    def _open_key(self) -> str:
        return f"{self._cache_key}:open_until"

    def _err_key(self) -> str:
        return f"{self._cache_key}:last_error"

    def _probe_key(self) -> str:
        return f"{self._cache_key}:probe"

    def _failure_count(self) -> int:
        raw = cache.get(self._fail_key())
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def _open_until(self) -> float:
        raw = cache.get(self._open_key())
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _state(self) -> str:
        open_until = self._open_until()
        if open_until <= 0:
            return STATE_CLOSED
        if time.time() < open_until:
            return STATE_OPEN
        return STATE_HALF_OPEN

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic status information for this source."""
        current_state = self._state()
        open_until = self._open_until()
        now = time.time()
        cooldown_remaining = max(0.0, open_until - now) if current_state == STATE_OPEN else 0.0
        disabled_sources = getattr(settings, "DISABLED_JOB_SOURCES", [])
        is_admin_disabled = self.source in disabled_sources
        return {
            "source": self.source,
            "state": current_state,
            "is_available": current_state != STATE_OPEN and not is_admin_disabled,
            "is_admin_disabled": is_admin_disabled,
            "failure_count": self._failure_count(),
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "last_error": str(cache.get(self._err_key()) or ""),
            "last_failure_time": open_until - self.cooldown_seconds if open_until else 0.0,
        }

    def is_available(self) -> bool:
        """Check if calls to this source are currently permitted.

        HALF_OPEN does not consume the single probe token. ``call`` does.
        """
        disabled_sources = getattr(settings, "DISABLED_JOB_SOURCES", [])
        if self.source in disabled_sources:
            return False
        return self._state() != STATE_OPEN

    def record_success(self) -> None:
        """Record a successful execution, resetting circuit breaker to CLOSED."""
        if self._state() != STATE_CLOSED or self._failure_count() > 0:
            logger.info(
                "[CircuitBreaker:%s] Successful call. Resetting circuit breaker to CLOSED.",
                self.source,
            )
        self._clear()

    def record_failure(self, error: Exception | str) -> None:
        """Record an execution failure, potentially tripping the circuit to OPEN."""
        err_str = str(error)
        ttl = self._ttl()
        cache.add(self._fail_key(), 0, timeout=ttl)
        try:
            failures = int(cache.incr(self._fail_key()))
        except ValueError:
            cache.set(self._fail_key(), 1, timeout=ttl)
            failures = 1
        cache.set(self._err_key(), err_str, timeout=ttl)
        was_half_open = self._state() == STATE_HALF_OPEN
        if failures >= self.failure_threshold or was_half_open:
            cache.set(self._open_key(), time.time() + self.cooldown_seconds, timeout=ttl)
            cache.delete(self._probe_key())
            logger.warning(
                "[CircuitBreaker:%s] Tripped OPEN! %d consecutive failures (threshold=%d). Error: %s",
                self.source,
                failures,
                self.failure_threshold,
                err_str,
            )
        else:
            logger.warning(
                "[CircuitBreaker:%s] Failure recorded (%d/%d): %s",
                self.source,
                failures,
                self.failure_threshold,
                err_str,
            )

    def _clear(self) -> None:
        cache.delete_many([
            self._cache_key,
            self._fail_key(),
            self._open_key(),
            self._err_key(),
            self._probe_key(),
        ])

    def reset(self) -> None:
        """Manually force reset the circuit breaker to CLOSED."""
        self._clear()
        logger.info("[CircuitBreaker:%s] Manually reset to CLOSED.", self.source)

    def claim_probe(self) -> bool:
        """True for the single caller allowed through while HALF_OPEN."""
        if self._state() != STATE_HALF_OPEN:
            return True
        return bool(cache.add(self._probe_key(), 1, timeout=self.cooldown_seconds))

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Execute a function wrapped by this circuit breaker.
        Raises CircuitBreakerOpenException if the circuit is OPEN.
        """
        if not self.is_available() or not self.claim_probe():
            status = self.get_status()
            raise CircuitBreakerOpenException(
                self.source,
                status.get("cooldown_remaining_seconds", 0.0),
                status.get("last_error", ""),
            )

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure(exc)
            raise


# Global registry of circuit breakers per source
_circuit_breakers: Dict[str, SourceCircuitBreaker] = {}


def get_circuit_breaker(source: str) -> SourceCircuitBreaker:
    """Retrieve or create the singleton circuit breaker for a given source."""
    s = source.strip().lower()
    if s not in _circuit_breakers:
        _circuit_breakers[s] = SourceCircuitBreaker(s)
    return _circuit_breakers[s]


def is_source_enabled(source: str) -> bool:
    """Check if a source is enabled administratively and not tripped OPEN."""
    cb = get_circuit_breaker(source)
    return cb.is_available()


def get_all_sources_status() -> list[Dict[str, Any]]:
    """Return status summary for all known job sources."""
    from ..job_sources import ALLOWED_SITE_NAMES

    return [get_circuit_breaker(s).get_status() for s in ALLOWED_SITE_NAMES]
