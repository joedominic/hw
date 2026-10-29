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

    def _get_state_data(self) -> Dict[str, Any]:
        """Fetch circuit breaker state from shared cache or default."""
        data = cache.get(self._cache_key)
        if not isinstance(data, dict):
            return {
                "state": STATE_CLOSED,
                "failure_count": 0,
                "last_failure_time": 0.0,
                "last_error": "",
            }
        return data

    def _save_state_data(self, data: Dict[str, Any]) -> None:
        """Persist state data to cache with a generous TTL."""
        ttl = self.cooldown_seconds * 3
        cache.set(self._cache_key, data, timeout=ttl)

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic status information for this source."""
        data = self._get_state_data()
        current_state = data.get("state", STATE_CLOSED)
        now = time.time()
        last_fail = data.get("last_failure_time", 0.0)
        cooldown_remaining = 0.0

        if current_state == STATE_OPEN:
            elapsed = now - last_fail
            if elapsed >= self.cooldown_seconds:
                current_state = STATE_HALF_OPEN
            else:
                cooldown_remaining = self.cooldown_seconds - elapsed

        disabled_sources = getattr(settings, "DISABLED_JOB_SOURCES", [])
        is_admin_disabled = self.source in disabled_sources

        return {
            "source": self.source,
            "state": current_state,
            "is_available": current_state != STATE_OPEN and not is_admin_disabled,
            "is_admin_disabled": is_admin_disabled,
            "failure_count": data.get("failure_count", 0),
            "cooldown_remaining_seconds": max(0.0, round(cooldown_remaining, 1)),
            "last_error": data.get("last_error", ""),
            "last_failure_time": last_fail,
        }

    def is_available(self) -> bool:
        """Check if calls to this source are currently permitted."""
        disabled_sources = getattr(settings, "DISABLED_JOB_SOURCES", [])
        if self.source in disabled_sources:
            return False

        data = self._get_state_data()
        current_state = data.get("state", STATE_CLOSED)

        if current_state == STATE_CLOSED:
            return True

        if current_state == STATE_OPEN:
            elapsed = time.time() - data.get("last_failure_time", 0.0)
            if elapsed >= self.cooldown_seconds:
                # Transition to HALF_OPEN to allow trial probe
                data["state"] = STATE_HALF_OPEN
                self._save_state_data(data)
                logger.info(
                    "[CircuitBreaker:%s] Cooldown passed; transitioning to HALF_OPEN probe",
                    self.source,
                )
                return True
            return False

        # HALF_OPEN: permit trial attempt
        return True

    def record_success(self) -> None:
        """Record a successful execution, resetting circuit breaker to CLOSED."""
        data = self._get_state_data()
        if data.get("state") != STATE_CLOSED or data.get("failure_count", 0) > 0:
            logger.info(
                "[CircuitBreaker:%s] Successful call. Resetting circuit breaker to CLOSED.",
                self.source,
            )
        data = {
            "state": STATE_CLOSED,
            "failure_count": 0,
            "last_failure_time": 0.0,
            "last_error": "",
        }
        self._save_state_data(data)

    def record_failure(self, error: Exception | str) -> None:
        """Record an execution failure, potentially tripping the circuit to OPEN."""
        now = time.time()
        err_str = str(error)
        data = self._get_state_data()
        failures = data.get("failure_count", 0) + 1
        data["failure_count"] = failures
        data["last_failure_time"] = now
        data["last_error"] = err_str

        if failures >= self.failure_threshold or data.get("state") == STATE_HALF_OPEN:
            data["state"] = STATE_OPEN
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

        self._save_state_data(data)

    def reset(self) -> None:
        """Manually force reset the circuit breaker to CLOSED."""
        cache.delete(self._cache_key)
        logger.info("[CircuitBreaker:%s] Manually reset to CLOSED.", self.source)

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Execute a function wrapped by this circuit breaker.
        Raises CircuitBreakerOpenException if the circuit is OPEN.
        """
        if not self.is_available():
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
