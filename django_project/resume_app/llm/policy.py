"""
Shared LLM call policy used by the gateway and non-LangChain callers (apply-agent, etc.).

Enforces kill switch, daily token budgets, per-user concurrency, and invoke timeouts.
Request-count quotas remain in rate_limits / entitlements and are consumed by the gateway
(and by apply-agent once per generic fill run).

Canonical docs: ``resume_app/docs/LLM_GATEWAY.md``.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)

T = TypeVar("T")

from ..subscriptions import METRIC_LLM_TOKENS

PLATFORM_TOKEN_REDIS_PREFIX = "llm:platform:tok:v1:"
CONCURRENCY_REDIS_PREFIX = "llm:conc:v1:"


class LLMRequestsDisabled(Exception):
    """Raised when AppAutomationSettings.stop_llm_requests is True."""


class LLMEmailVerificationRequired(Exception):
    """Raised when email verification is required before making LLM calls."""


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
    from ..subscriptions import get_user_plan, plan_limit, staff_bypasses_quotas

    if user is None or not getattr(user, "is_authenticated", False):
        return int(getattr(settings, "LLM_USER_DAILY_TOKEN_LIMIT", 0) or 0)
    if staff_bypasses_quotas(user):
        return 0
    return plan_limit(get_user_plan(user), METRIC_LLM_TOKENS)


def estimate_optimizer_full_run_tokens() -> int:
    """
    Rough token estimate for one Writer → ATS Judge → Recruiter Judge pass.

    Uses optimizer context char caps with chars/4 estimation plus per-step
    prompt/output overhead. Intended for preflight UI, not hard metering.
    """
    writer_resume = int(getattr(settings, "OPTIMIZER_WRITER_RESUME_MAX_CHARS", 14000) or 14000)
    writer_jd = int(getattr(settings, "OPTIMIZER_WRITER_JD_ROLE_MAX_CHARS", 8000) or 8000)
    judge_resume = int(getattr(settings, "OPTIMIZER_JUDGE_RESUME_MAX_CHARS", 12000) or 12000)
    judge_jd = int(getattr(settings, "OPTIMIZER_JUDGE_JD_MAX_CHARS", 8000) or 8000)
    step_overhead = 2000
    writer = (writer_resume + writer_jd) // 4 + step_overhead + 3000
    ats = (judge_resume + judge_jd) // 4 + step_overhead + 1500
    recruiter = (judge_resume + judge_jd) // 4 + step_overhead + 1500
    return max(1, writer + ats + recruiter)


def optimizer_token_preflight(user: AbstractBaseUser | None) -> dict[str, Any]:
    """
    Preflight budget snapshot for the Optimizer Run button.

    Returns used/limit/estimate/remaining and whether the run should be blocked.
    Unlimited limits (0) never block.
    """
    from ..subscriptions import usage_today

    estimate = estimate_optimizer_full_run_tokens()
    limit = daily_token_limit_for_user(user)
    used = 0
    if user is not None and getattr(user, "is_authenticated", False):
        used = int(usage_today(user, METRIC_LLM_TOKENS) or 0)
    remaining = None if limit <= 0 else max(0, limit - used)
    blocked = bool(limit > 0 and used + estimate > limit)
    return {
        "estimate": estimate,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "blocked": blocked,
        "unlimited": limit <= 0,
    }


def platform_daily_token_limit() -> int:
    """Global daily token budget for platform/env API keys (0 = unlimited)."""
    return int(getattr(settings, "LLM_PLATFORM_DAILY_TOKEN_LIMIT", 0) or 0)


def _platform_token_redis_key() -> str:
    from django.utils import timezone

    day = timezone.localdate().isoformat()
    return f"{PLATFORM_TOKEN_REDIS_PREFIX}{day}"


def _get_redis():
    from .rate_limit import _get_redis as _rl_redis

    return _rl_redis()


def uses_platform_keys(user: AbstractBaseUser | None, provider: str) -> bool:
    """True when the call would bill the shared platform/env key namespace."""
    from .rate_limit import _rate_limit_scope_key

    return _rate_limit_scope_key(user, provider) == "platform"


def platform_tokens_used_today() -> int:
    try:
        raw = _get_redis().get(_platform_token_redis_key())
        return int(raw or 0)
    except Exception as e:
        logger.debug("platform token read failed: %s", e)
        return 0


def is_local_provider(provider: str | None) -> bool:
    """True for inherently local providers such as Ollama Local."""
    name = (provider or "").strip().lower()
    return name == "ollama local"


def check_token_budget(
    user: AbstractBaseUser | None,
    *,
    estimated_tokens: int = 0,
    provider: str = "",
    is_local: bool = False,
) -> None:
    """Raise LLMTokenBudgetExceeded when the user or platform budget cannot cover ``estimated_tokens``."""
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    if is_local or is_local_provider(provider):
        return
    from ..subscriptions import get_user_plan, staff_bypasses_quotas, usage_today

    est = max(0, int(estimated_tokens))
    if user is not None and getattr(user, "is_authenticated", False) and not staff_bypasses_quotas(user):
        plan = get_user_plan(user)
        if plan and getattr(plan, "slug", "") == "byok" and uses_platform_keys(user, provider):
            raise LLMTokenBudgetExceeded(
                "The BYOK Rails plan requires configuring your own API keys in Settings → LLM "
                "(or using local Ollama models). Platform-managed API keys are not included on this plan.",
                limit=0,
            )
        limit = daily_token_limit_for_user(user)
        if limit > 0:
            used = usage_today(user, METRIC_LLM_TOKENS)
            if used + est > limit:
                plan = get_user_plan(user)
                plan_name = plan.name if plan and hasattr(plan, "name") else (getattr(plan, "slug", "") or "Free").title()
                raise LLMTokenBudgetExceeded(
                    f"Daily LLM token budget exceeded for plan '{plan_name}' "
                    f"({used:,} / {limit:,} tokens used today, +~{est:,} est. required for this call). "
                    f"This is a cumulative daily limit across all cloud LLM operations today (resets at midnight). "
                    f"To continue, you can upgrade your plan in Billing or use unmetered local models.",
                    limit=limit,
                )
    if provider and uses_platform_keys(user, provider):
        plat_lim = platform_daily_token_limit()
        plat_used = platform_tokens_used_today()
        if plat_lim > 0 and plat_used + est > plat_lim:
            raise LLMTokenBudgetExceeded(
                f"Platform shared LLM token budget exceeded "
                f"({plat_used:,} / {plat_lim:,} tokens used today across platform keys). "
                f"Configure your own provider API keys in Settings → LLM or use local Ollama LLMs for free.",
                limit=plat_lim,
            )


def consume_token_budget(
    user: AbstractBaseUser | None,
    tokens: int,
    *,
    provider: str = "",
    is_local: bool = False,
) -> None:
    """Record consumed tokens against user and (when applicable) platform daily budgets."""
    if is_local or is_local_provider(provider):
        return
    amount = max(0, int(tokens))
    if amount < 1:
        return
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    from ..subscriptions import QuotaExceeded, consume_quota, staff_bypasses_quotas

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
    from ..models import AppAutomationSettings

    if user is None or not getattr(user, "is_authenticated", False):
        return
    if AppAutomationSettings.get_for_user(user).stop_llm_requests:
        raise LLMRequestsDisabled(
            "LLM requests are disabled. Turn off 'Stop LLM requests' in Settings → LLM."
        )


def assert_email_verified(user: AbstractBaseUser | None) -> None:
    """
    Ensure user's email address is verified before allowing LLM requests when verification is enforced.
    Staff and superusers are exempt.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return
    require_verification = getattr(
        settings,
        "REQUIRE_EMAIL_VERIFICATION",
        False,
    ) or getattr(settings, "SAAS_REQUIRE_VERIFIED_EMAIL_FOR_LLM", False)
    if not require_verification:
        return
    from ..account import is_email_verified

    if not is_email_verified(user):
        raise LLMEmailVerificationRequired(
            "Email verification is required before using AI features. "
            "Please verify your email address or visit Settings → Account to resend the verification email."
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
    lease_id = uuid.uuid4().hex
    acquired = False
    try:
        try:
            acquired = _acquire_concurrency_lease(key, lease_id, limit)
            if not acquired:
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
                from .rate_limit import _get_redis

                _get_redis().zrem(key, lease_id)
            except Exception as e:
                logger.debug("llm concurrency release failed: %s", e)


def invoke_timeout_seconds() -> float:
    return float(getattr(settings, "LLM_INVOKE_TIMEOUT_SECONDS", 180) or 180)


# Drop expired leases, then add this lease only when under the limit.
_CONCURRENCY_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], 3600)
return 1
"""


def _acquire_concurrency_lease(key: str, lease_id: str, limit: int) -> bool:
    """True when this call holds a lease. Expired leases free themselves."""
    import time

    from .rate_limit import _get_redis

    now = time.time()
    ttl = invoke_timeout_seconds() + 60
    granted = _get_redis().eval(
        _CONCURRENCY_ACQUIRE_LUA,
        1,
        key,
        now,
        now + ttl,
        lease_id,
        int(limit),
    )
    return int(granted) == 1


def _invoke_and_close_db(fn: Callable[[], T]) -> T:
    from django.db import close_old_connections

    try:
        return fn()
    finally:
        close_old_connections()


def run_with_invoke_timeout(fn: Callable[[], T], *, timeout_seconds: float | None = None) -> T:
    """Backstop wall-clock deadline around an LLM call.

    The provider client timeout is what closes the socket. This pool only
    unblocks the caller when a provider ignores that timeout. ``wait=False``
    returns immediately; the abandoned thread exits once the socket times out.
    """
    timeout = invoke_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
    if timeout <= 0:
        return _invoke_and_close_db(fn)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-invoke")
    fut = pool.submit(_invoke_and_close_db, fn)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout as e:
        logger.warning("LLM invoke exceeded %.1fs; abandoning worker thread", timeout)
        raise LLMInvokeTimeout(
            f"LLM invoke exceeded {int(timeout)}s wall-clock deadline."
        ) from e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def estimate_messages_tokens(messages) -> int:
    from .rate_limit import estimate_tokens_from_messages

    return estimate_tokens_from_messages(messages)


