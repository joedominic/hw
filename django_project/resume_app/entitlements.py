"""SaaS entitlements: plans, quotas, and feature gates."""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

METRIC_LLM_REQUESTS = "llm_requests"
METRIC_LLM_TOKENS = "llm_tokens"
METRIC_JOB_SEARCHES = "job_searches"
METRIC_APPLY_RUNS = "apply_runs"

ACTIVE_SUB_STATUSES = ("trialing", "active")
# Statuses that retain the paid plan row but must not receive paid entitlements.
DEGRADED_SUB_STATUSES = ("past_due", "unpaid", "canceled")


class QuotaExceeded(Exception):
    """User has exhausted a plan quota for the current period."""

    def __init__(self, metric: str, limit: int, message: str | None = None):
        self.metric = metric
        self.limit = limit
        super().__init__(message or f"Quota exceeded for {metric} (limit {limit}).")


class EntitlementDenied(Exception):
    """Plan does not include the requested feature."""


def _default_plan_slug() -> str:
    return getattr(settings, "SAAS_DEFAULT_PLAN_SLUG", "free")


def staff_bypasses_quotas(user: AbstractBaseUser | None) -> bool:
    """Staff/superuser skip quotas only when SAAS_STAFF_BYPASS_QUOTAS is enabled."""
    if not getattr(settings, "SAAS_STAFF_BYPASS_QUOTAS", False):
        return False
    if user is None:
        return False
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def ensure_default_plans() -> None:
    """Idempotently create built-in plans (safe to call from AppConfig / migrate)."""
    from .models import Plan

    defaults = [
        {
            "slug": "free",
            "name": "Free",
            "description": "Starter limits for individual job seekers.",
            "llm_requests_per_day": 50,
            "job_searches_per_day": 20,
            "apply_runs_per_day": 5,
            "storage_mb": 250,
            "api_access": False,
            "is_default": True,
            "sort_order": 10,
        },
        {
            "slug": "pro",
            "name": "Pro",
            "description": "Higher limits plus customer API access.",
            "llm_requests_per_day": 500,
            "job_searches_per_day": 200,
            "apply_runs_per_day": 50,
            "storage_mb": 5000,
            "api_access": True,
            "is_default": False,
            "sort_order": 20,
        },
        {
            "slug": "unlimited",
            "name": "Unlimited",
            "description": "No daily quotas (0 = unlimited).",
            "llm_requests_per_day": 0,
            "job_searches_per_day": 0,
            "apply_runs_per_day": 0,
            "storage_mb": 0,
            "api_access": True,
            "is_default": False,
            "sort_order": 30,
        },
    ]
    for row in defaults:
        Plan.objects.update_or_create(slug=row["slug"], defaults=row)


def get_or_create_subscription(user: AbstractBaseUser):
    from .models import Plan, Subscription

    sub, created = Subscription.objects.select_related("plan").get_or_create(
        owner=user,
        defaults={
            "plan": Plan.objects.filter(slug=_default_plan_slug(), is_active=True).first()
            or Plan.objects.filter(is_default=True, is_active=True).first()
            or Plan.objects.filter(is_active=True).order_by("sort_order").first(),
            "status": Subscription.Status.ACTIVE,
        },
    )
    if created and sub.plan_id is None:
        ensure_default_plans()
        sub.plan = Plan.objects.filter(is_default=True).first()
        sub.save(update_fields=["plan"])
    return sub


def get_user_plan(user: AbstractBaseUser):
    """Return the effective Plan for ``user`` (creates free subscription if needed)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    sub = get_or_create_subscription(user)
    from .models import Plan

    # past_due / unpaid / canceled → free entitlements until payment recovers.
    if sub.status not in ACTIVE_SUB_STATUSES or sub.plan_id is None:
        return Plan.objects.filter(slug=_default_plan_slug()).first() or sub.plan
    return sub.plan


def plan_limit(plan, metric: str) -> int:
    """Return daily limit for metric; 0 means unlimited."""
    if metric == METRIC_LLM_TOKENS:
        by_plan = getattr(settings, "LLM_DAILY_TOKEN_LIMIT_BY_PLAN", None) or {}
        slug = getattr(plan, "slug", None) or "free"
        limit = int(by_plan.get(slug, by_plan.get("free", 0)) or 0)
        env_cap = int(getattr(settings, "LLM_USER_DAILY_TOKEN_LIMIT", 0) or 0)
        if env_cap > 0 and (limit == 0 or env_cap < limit):
            return env_cap if limit == 0 else min(limit, env_cap)
        return limit
    if plan is None:
        return int(getattr(settings, "LLM_USER_DAILY_REQUEST_LIMIT", 0) or 0) if metric == METRIC_LLM_REQUESTS else 0
    mapping = {
        METRIC_LLM_REQUESTS: plan.llm_requests_per_day,
        METRIC_JOB_SEARCHES: plan.job_searches_per_day,
        METRIC_APPLY_RUNS: plan.apply_runs_per_day,
    }
    limit = int(mapping.get(metric, 0) or 0)
    # Legacy env override for LLM when plan says unlimited (0) but env sets a global cap
    if metric == METRIC_LLM_REQUESTS:
        env_cap = int(getattr(settings, "LLM_USER_DAILY_REQUEST_LIMIT", 0) or 0)
        if env_cap > 0 and (limit == 0 or env_cap < limit):
            # When env is set, treat as hard ceiling for all plans
            if limit == 0:
                return env_cap
            return min(limit, env_cap)
    return limit


def usage_today(user: AbstractBaseUser, metric: str) -> int:
    from .models import UsageCounter

    today = timezone.localdate()
    row = UsageCounter.objects.filter(owner=user, metric=metric, period_date=today).first()
    return int(row.count) if row else 0


def check_quota(user: AbstractBaseUser, metric: str) -> None:
    """Raise QuotaExceeded if the user cannot consume one more unit."""
    from django.conf import settings

    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    if user is None or not getattr(user, "is_authenticated", False):
        return
    if staff_bypasses_quotas(user):
        return
    plan = get_user_plan(user)
    limit = plan_limit(plan, metric)
    if limit <= 0:
        return
    if usage_today(user, metric) >= limit:
        raise QuotaExceeded(metric, limit)


@transaction.atomic
def consume_quota(user: AbstractBaseUser, metric: str, amount: int = 1) -> int:
    """
    Atomically increment today's usage and enforce the plan limit.
    Returns the new count. Raises QuotaExceeded when over limit.
    """
    from django.conf import settings
    from .models import UsageCounter

    if amount < 1:
        return usage_today(user, metric)
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return usage_today(user, metric)
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    if staff_bypasses_quotas(user):
        return 0

    plan = get_user_plan(user)
    limit = plan_limit(plan, metric)
    today = timezone.localdate()
    row, _ = UsageCounter.objects.select_for_update().get_or_create(
        owner=user,
        metric=metric,
        period_date=today,
        defaults={"count": 0},
    )
    if limit > 0 and row.count + amount > limit:
        raise QuotaExceeded(metric, limit)
    UsageCounter.objects.filter(pk=row.pk).update(count=F("count") + amount, updated_at=timezone.now())
    row.refresh_from_db(fields=["count"])
    return int(row.count)


def require_api_access(user: AbstractBaseUser) -> None:
    plan = get_user_plan(user)
    if plan is None or not plan.api_access:
        if staff_bypasses_quotas(user):
            return
        raise EntitlementDenied("Your plan does not include API access. Upgrade to Pro.")


def assign_plan(user: AbstractBaseUser, plan_slug: str, *, status: str = "active") -> None:
    from .models import Plan, Subscription

    plan = Plan.objects.get(slug=plan_slug, is_active=True)
    sub = get_or_create_subscription(user)
    sub.plan = plan
    sub.status = status
    sub.save(update_fields=["plan", "status", "updated_at"])


USAGE_METRIC_LABELS: dict[str, str] = {
    METRIC_LLM_REQUESTS: "LLM requests (today)",
    METRIC_LLM_TOKENS: "LLM tokens (today)",
    METRIC_JOB_SEARCHES: "Job searches (today)",
    METRIC_APPLY_RUNS: "Apply runs (today)",
}


def subscription_summary(user: AbstractBaseUser) -> dict:
    sub = get_or_create_subscription(user)
    plan = get_user_plan(user)
    from .storage_quota import storage_summary

    usage = {}
    for metric in (
        METRIC_LLM_REQUESTS,
        METRIC_LLM_TOKENS,
        METRIC_JOB_SEARCHES,
        METRIC_APPLY_RUNS,
    ):
        used = usage_today(user, metric)
        limit = plan_limit(plan, metric)
        remaining = None if limit <= 0 else max(0, limit - used)
        usage[metric] = {
            "used": used,
            "limit": limit,
            "remaining": remaining,
            "label": USAGE_METRIC_LABELS.get(metric, metric),
        }

    return {
        "plan_slug": plan.slug if plan else None,
        "plan_name": plan.name if plan else None,
        "status": sub.status,
        "api_access": bool(plan and plan.api_access),
        "stripe_customer_id": sub.stripe_customer_id or "",
        "trial_ends_at": sub.trial_ends_at,
        "current_period_end": sub.current_period_end,
        "usage": usage,
        "storage": storage_summary(user),
    }
