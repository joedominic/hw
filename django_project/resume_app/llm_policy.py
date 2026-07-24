"""
Shared LLM call policy used by the gateway and non-LangChain callers (apply-agent, etc.).

Enforces kill switch, daily token budgets, per-user concurrency, and invoke timeouts.
Request-count quotas remain in rate_limits / entitlements and are consumed by the gateway
(and by apply-agent once per generic fill run).

Canonical docs: ``resume_app/docs/LLM_GATEWAY.md``.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)

T = TypeVar("T")

from .entitlements import METRIC_LLM_TOKENS

PLATFORM_TOKEN_REDIS_PREFIX = "llm:platform:tok:v1:"
CONCURRENCY_REDIS_PREFIX = "llm:conc:v1:"


class LLMRequestsDisabled(Exception):
    """Raised when AppAutomationSettings.stop_llm_requests is True."""


class LLMTokenBudgetExceeded(Exception):
    """User or platform daily token budget exhausted."""

    def __init__(self, message: str, *, limit: int = 0):
        self.limit = limit
        super().__init__(message)


class LLMConcurrencyLimitExceeded(Exception):
    """Too many in-flight LLM calls for this user."""


class LLMInvokeTimeout(Exception):
    """LLM invoke exceeded the configured wall-clock timeout."""


def daily_token_limit_for_user(user: AbstractBaseUser | None) -> int:
    """
    Effective daily token limit for ``user`` (0 = unlimited).

    Uses ``LLM_DAILY_TOKEN_LIMIT_BY_PLAN[plan_slug]``, then optional
    ``LLM_USER_DAILY_TOKEN_LIMIT`` as a hard ceiling (same pattern as request quotas).
    """
    from .entitlements import get_user_plan, plan_limit, staff_bypasses_quotas

    if user is None or not getattr(user, "is_authenticated", False):
        return int(getattr(settings, "LLM_USER_DAILY_TOKEN_LIMIT", 0) or 0)
    if staff_bypasses_quotas(user):
        return 0
    return plan_limit(get_user_plan(user), METRIC_LLM_TOKENS)


def platform_daily_token_limit() -> int:
    """Global daily token budget for platform/env API keys (0 = unlimited)."""
    return int(getattr(settings, "LLM_PLATFORM_DAILY_TOKEN_LIMIT", 0) or 0)


def _platform_token_redis_key() -> str:
    from django.utils import timezone

    day = timezone.localdate().isoformat()
    return f"{PLATFORM_TOKEN_REDIS_PREFIX}{day}"


def _get_redis():
    from .llm_rate_limit import _get_redis as _rl_redis

    return _rl_redis()


def uses_platform_keys(user: AbstractBaseUser | None, provider: str) -> bool:
    """True when the call would bill the shared platform/env key namespace."""
    from .llm_rate_limit import _rate_limit_scope_key

    return _rate_limit_scope_key(user, provider) == "platform"


def platform_tokens_used_today() -> int:
    try:
        raw = _get_redis().get(_platform_token_redis_key())
        return int(raw or 0)
    except Exception as e:
        logger.debug("platform token read failed: %s", e)
        return 0


def check_token_budget(
    user: AbstractBaseUser | None,
    *,
    estimated_tokens: int = 0,
    provider: str = "",
) -> None:
    """Raise LLMTokenBudgetExceeded when the user or platform budget cannot cover ``estimated_tokens``."""
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    from .entitlements import staff_bypasses_quotas, usage_today

    est = max(0, int(estimated_tokens))
    if user is not None and getattr(user, "is_authenticated", False) and not staff_bypasses_quotas(user):
        limit = daily_token_limit_for_user(user)
        if limit > 0:
            used = usage_today(user, METRIC_LLM_TOKENS)
            if used + est > limit:
                raise LLMTokenBudgetExceeded(
                    f"Daily LLM token budget exceeded (limit {limit}).",
                    limit=limit,
                )
    if provider and uses_platform_keys(user, provider):
        plat_lim = platform_daily_token_limit()
        if plat_lim > 0 and platform_tokens_used_today() + est > plat_lim:
            raise LLMTokenBudgetExceeded(
                f"Platform LLM token budget exceeded (limit {plat_lim}).",
                limit=plat_lim,
            )


def consume_token_budget(
    user: AbstractBaseUser | None,
    tokens: int,
    *,
    provider: str = "",
) -> None:
    """Record consumed tokens against user and (when applicable) platform daily budgets."""
    amount = max(0, int(tokens))
    if amount < 1:
        return
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    from .entitlements import QuotaExceeded, consume_quota, staff_bypasses_quotas

    if user is not None and getattr(user, "is_authenticated", False) and not staff_bypasses_quotas(user):
        try:
            consume_quota(user, METRIC_LLM_TOKENS, amount)
        except QuotaExceeded as e:
            # Overshoot after a successful invoke should not discard the response.
            logger.warning("llm token budget overshoot user=%s: %s", getattr(user, "pk", None), e)

    if provider and uses_platform_keys(user, provider):
        plat_lim = platform_daily_token_limit()
        try:
            r = _get_redis()
            key = _platform_token_redis_key()
            pipe = r.pipeline()
            pipe.incrby(key, amount)
            pipe.expire(key, 86400 * 2)
            pipe.execute()
            if plat_lim > 0 and platform_tokens_used_today() > plat_lim:
                logger.warning(
                    "platform token budget exceeded after consume limit=%s used~=%s",
                    plat_lim,
                    platform_tokens_used_today(),
                )
        except Exception as e:
            logger.warning("platform token consume failed: %s", e)


def assert_llm_kill_switch(user: AbstractBaseUser | None) -> None:
    from .models import AppAutomationSettings

    if user is None or not getattr(user, "is_authenticated", False):
        return
    if AppAutomationSettings.get_for_user(user).stop_llm_requests:
        raise LLMRequestsDisabled(
            "LLM requests are disabled. Turn off 'Stop LLM requests' in Settings → LLM."
        )


@contextmanager
def user_llm_concurrency(user: AbstractBaseUser | None) -> Iterator[None]:
    """
    Bound in-flight LLM calls per user (Redis). Fail-closed when Redis is unavailable
    in production-style configs (same flag as provider rate limits).
    """
    limit = int(getattr(settings, "LLM_USER_MAX_CONCURRENT", 0) or 0)
    if limit <= 0 or user is None or not getattr(user, "is_authenticated", False):
        yield
        return

    fail_open = bool(getattr(settings, "LLM_RATE_LIMIT_FAIL_OPEN", False))
    key = f"{CONCURRENCY_REDIS_PREFIX}u{int(user.pk)}"
    acquired = False
    try:
        try:
            r = _get_redis()
            current = int(r.incr(key))
            r.expire(key, 600)
            acquired = True
            if current > limit:
                raise LLMConcurrencyLimitExceeded(
                    f"Too many concurrent LLM calls (limit {limit}). Try again shortly."
                )
        except LLMConcurrencyLimitExceeded:
            raise
        except Exception as e:
            logger.warning("llm concurrency acquire failed: %s", e)
            if fail_open:
                yield
                return
            raise LLMConcurrencyLimitExceeded(
                "LLM concurrency control unavailable; try again shortly."
            ) from e
        yield
    finally:
        if acquired:
            try:
                r = _get_redis()
                n = int(r.decr(key))
                if n <= 0:
                    r.delete(key)
            except Exception as e:
                logger.debug("llm concurrency release failed: %s", e)


def invoke_timeout_seconds() -> float:
    return float(getattr(settings, "LLM_INVOKE_TIMEOUT_SECONDS", 180) or 180)


def run_with_invoke_timeout(fn: Callable[[], T], *, timeout_seconds: float | None = None) -> T:
    """Run ``fn`` in a worker thread with a wall-clock timeout."""
    timeout = invoke_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
    if timeout <= 0:
        return fn()
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except FuturesTimeout as e:
            raise LLMInvokeTimeout(
                f"LLM invoke exceeded {int(timeout)}s wall-clock deadline."
            ) from e


def estimate_messages_tokens(messages) -> int:
    from .llm_rate_limit import estimate_tokens_from_messages

    return estimate_tokens_from_messages(messages)


def wrap_browser_use_llm(inner: Any, *, user, provider: str, model: str) -> Any:
    """
    Proxy a browser-use chat model so each completion honors kill switch, token budget,
    concurrency, timeout, usage recording, and token consumption.
    """
    from .llm_gateway import USAGE_QUERY_APPLY_AGENT, log_llm_invoke, record_llm_usage
    from .llm_rate_limit import estimate_tokens_from_messages
    from .rate_limits import check_user_llm_rate_limit

    class _PolicyBrowserLLM:
        def __init__(self) -> None:
            self._inner = inner
            self._user = user
            self._provider = provider
            self._model = model

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def _preflight(self, messages) -> int:
            assert_llm_kill_switch(self._user)
            check_user_llm_rate_limit(self._user)
            est = estimate_tokens_from_messages(messages or [])
            check_token_budget(self._user, estimated_tokens=est, provider=self._provider)
            return est

        def _finalize(self, response, est: int) -> None:
            in_tok, out_tok = est, 0
            try:
                from .agents import _normalize_token_usage

                u = _normalize_token_usage(response, None, None)
                in_tok = int(u.get("input_tokens") or 0) or est
                out_tok = int(u.get("output_tokens") or 0)
            except Exception:
                pass
            try:
                record_llm_usage(
                    self._provider,
                    self._model,
                    in_tok,
                    out_tok,
                    0,
                    True,
                    user=self._user,
                    query_kind=USAGE_QUERY_APPLY_AGENT,
                )
            except Exception as e:
                logger.debug("apply-agent usage record skipped: %s", e)
            consume_token_budget(
                self._user, in_tok + out_tok, provider=self._provider
            )

        def invoke(self, messages, *args, **kwargs):
            est = self._preflight(messages)
            log_llm_invoke(
                self._provider,
                self._model,
                query=USAGE_QUERY_APPLY_AGENT,
                via="browser-use",
            )

            def _call():
                return self._inner.invoke(messages, *args, **kwargs)

            with user_llm_concurrency(self._user):
                raw = run_with_invoke_timeout(_call)
                self._finalize(raw, est)
                return raw

        async def ainvoke(self, messages, *args, **kwargs):
            import asyncio

            est = self._preflight(messages)
            log_llm_invoke(
                self._provider,
                self._model,
                query=USAGE_QUERY_APPLY_AGENT,
                via="browser-use-async",
            )
            with user_llm_concurrency(self._user):
                try:
                    timeout = invoke_timeout_seconds()
                    if timeout > 0:
                        raw = await asyncio.wait_for(
                            self._inner.ainvoke(messages, *args, **kwargs),
                            timeout=timeout,
                        )
                    else:
                        raw = await self._inner.ainvoke(messages, *args, **kwargs)
                except asyncio.TimeoutError as e:
                    raise LLMInvokeTimeout(
                        f"LLM invoke exceeded {int(invoke_timeout_seconds())}s wall-clock deadline."
                    ) from e
                self._finalize(raw, est)
                return raw

    return _PolicyBrowserLLM()
