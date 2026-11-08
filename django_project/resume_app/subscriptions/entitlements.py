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

ACTIVE_SUB_STATUSES = ("trialing", "active", "past_due")
# Statuses that retain the paid plan row but must not receive paid entitlements.
DEGRADED_SUB_STATUSES = ("unpaid", "canceled")


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


def parse_numeric_quota(val: str | int | float | None, default: int = 0) -> int:
    """
    Parse quota numeric inputs from admin or API forms.
    Gracefully handles commas (e.g. 200,000), whitespace, metric suffixes (e.g. 200k, 1.5M),
    and sentinel strings (0, unlimited, none).
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return max(0, int(val))
    raw = str(val).strip().replace(",", "").replace(" ", "").lower()
    if not raw:
        return default
    if raw in ("unlimited", "none", "inf", "infinity"):
        return 0
    multiplier = 1
    if raw.endswith("k"):
        multiplier = 1_000
        raw = raw[:-1].strip()
    elif raw.endswith("m"):
        multiplier = 1_000_000
        raw = raw[:-1].strip()
    elif raw.endswith("b"):
        multiplier = 1_000_000_000
        raw = raw[:-1].strip()
    try:
        return max(0, int(float(raw) * multiplier))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid numeric quota value: {val!r}")


def ensure_default_plans() -> None:
    """Idempotently create built-in plans if missing (safe to call from AppConfig / migrate)."""
    from ..models import Plan

    defaults = [
        {
            "slug": "free",
            "name": "Free",
            "description": "1.5M OpenAI tokens a month (~50k/day) to try tailoring and Skill Radar.",
            "price_display": "$0 / mo",
            "llm_requests_per_day": 50,
            "llm_tokens_per_day": 50000,
            "job_searches_per_day": 5,
            "storage_mb": 250,
            "api_access": False,
            "byok_required_for_completion": False,
            "is_active": True,
            "is_default": True,
            "sort_order": 10,
        },
        {
            "slug": "byok",
            "name": "BYOK Rails",
            "description": "Orchestration rails with your own API keys. Unlocked API, 2 GB storage, and Pro pipelines.",
            "price_display": "$15 / mo",
            "llm_requests_per_day": 0,
            "llm_tokens_per_day": 0,
            "job_searches_per_day": 15,
            "storage_mb": 2000,
            "api_access": True,
            "byok_required_for_completion": True,
            "is_active": True,
            "is_default": False,
            "sort_order": 15,
        },
        {
            "slug": "pro",
            "name": "Pro",
            "description": "High-velocity active search for serious job seekers.",
            "price_display": "$29 / mo",
            "llm_requests_per_day": 250,
            "llm_tokens_per_day": 167000,
            "job_searches_per_day": 25,
            "storage_mb": 5000,
            "api_access": True,
            "byok_required_for_completion": True,
            "is_active": True,
            "is_default": False,
            "sort_order": 20,
        },
        {
            "slug": "power",
            "name": "Power",
            "description": "Maximum throughput for power candidates, coaches, and consultants.",
            "price_display": "$99 / mo",
            "llm_requests_per_day": 1000,
            "llm_tokens_per_day": 500000,
            "job_searches_per_day": 100,
            "storage_mb": 20000,
            "api_access": True,
            "byok_required_for_completion": False,
            "is_active": True,
            "is_default": False,
            "sort_order": 30,
        },
    ]
    # Create missing built-ins only. Staff edits in the Billing admin console must stick
    # (see test_saas_phase2). Remediations below fix known pre-seed drift only — they
    # never overwrite intentional admin customizations outside those stale values.
    # stripe_price_id is never touched (env-specific Stripe Price IDs).
    for row in defaults:
        p, created = Plan.objects.get_or_create(slug=row["slug"], defaults=row)
        if created:
            continue
        updates = []
        # Fill blank commercial copy from seed (does not clobber custom strings).
        for field in ("description", "price_display"):
            if not (getattr(p, field) or "").strip() and row.get(field):
                setattr(p, field, row[field])
                updates.append(field)
        if updates:
            p.save(update_fields=updates)

    # Ensure deprecated unlimited plan is purged from database and remapped
    if Plan.objects.filter(slug="unlimited").exists():
        power_plan = Plan.objects.filter(slug="power").first() or Plan.objects.filter(is_default=True).first()
        from ..models import Subscription
        if power_plan:
            Subscription.objects.filter(plan__slug="unlimited").update(plan=power_plan)
        Plan.objects.filter(slug="unlimited").delete()


def get_or_create_subscription(user: AbstractBaseUser):
    from ..models import Plan, Subscription

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
    from ..models import Plan, Subscription

    # unpaid / canceled → free entitlements until payment recovers.
    if sub.status not in ACTIVE_SUB_STATUSES or sub.plan_id is None:
        return Plan.objects.filter(slug=_default_plan_slug()).first() or sub.plan

    # past_due retains paid entitlements during a 5-day grace period while Stripe retries.
    if sub.status == Subscription.Status.PAST_DUE:
        from datetime import timedelta
        grace_days = int(getattr(settings, "SAAS_PAST_DUE_GRACE_DAYS", 5) or 5)
        cutoff = timezone.now() - timedelta(days=grace_days)
        ref_time = sub.past_due_since or sub.updated_at
        if ref_time and ref_time < cutoff:
            return Plan.objects.filter(slug=_default_plan_slug()).first() or sub.plan

    return sub.plan


def plan_limit(plan, metric: str) -> int:
    """Return daily limit for metric; 0 means unlimited."""
    if metric == METRIC_LLM_TOKENS:
        limit = 0
        if plan and getattr(plan, "llm_tokens_per_day", 0):
            limit = int(plan.llm_tokens_per_day)
        else:
            by_plan = getattr(settings, "LLM_DAILY_TOKEN_LIMIT_BY_PLAN", None) or {}
            slug = getattr(plan, "slug", None) or "free"
            if slug in by_plan:
                limit = int(by_plan.get(slug, 0) or 0)
            elif plan is None:
                limit = int(by_plan.get("free", 0) or 0)
            else:
                # Custom plan where llm_tokens_per_day was explicitly set to 0 (unlimited)
                limit = 0
        env_cap = int(getattr(settings, "LLM_USER_DAILY_TOKEN_LIMIT", 0) or 0)
        if env_cap > 0:
            return env_cap if limit == 0 else min(limit, env_cap)
        return limit
    if plan is None:
        return int(getattr(settings, "LLM_USER_DAILY_REQUEST_LIMIT", 0) or 0) if metric == METRIC_LLM_REQUESTS else 0
    mapping = {
        METRIC_LLM_REQUESTS: getattr(plan, "llm_requests_per_day", 0),
        METRIC_JOB_SEARCHES: getattr(plan, "job_searches_per_day", 0),
    }
    limit = int(mapping.get(metric, 0) or 0)
    # Legacy env override for LLM when plan says unlimited (0) but env sets a global cap
    if metric == METRIC_LLM_REQUESTS:
        env_cap = int(getattr(settings, "LLM_USER_DAILY_REQUEST_LIMIT", 0) or 0)
        if env_cap > 0:
            if limit == 0:
                return env_cap
            return min(limit, env_cap)
    return limit


def usage_today(user: AbstractBaseUser, metric: str) -> int:
    from ..models import UsageCounter

    today = timezone.localdate()
    row = UsageCounter.objects.filter(owner=user, metric=metric, period_date=today).first()
    return int(row.count) if row else 0


def _format_quota_exceeded_message(user: AbstractBaseUser, metric: str, limit: int, current: int) -> str:
    plan = get_user_plan(user)
    plan_name = plan.name if plan and hasattr(plan, "name") else (getattr(plan, "slug", "") or "Free").title()
    label = USAGE_METRIC_LABELS.get(metric, metric)
    return (
        f"Daily quota exceeded for {label} on plan '{plan_name}' "
        f"({current:,} / {limit:,} used today, limit resets at midnight). "
        f"To continue, upgrade your plan under Plan & usage, or ask an administrator."
    )


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
    used = usage_today(user, metric)
    if used >= limit:
        msg = _format_quota_exceeded_message(user, metric, limit, used)
        raise QuotaExceeded(metric, limit, message=msg)


@transaction.atomic
def consume_quota(user: AbstractBaseUser, metric: str, amount: int = 1) -> int:
    """
    Atomically increment today's usage and enforce the plan limit.
    Returns the new count. Raises QuotaExceeded when over limit.
    """
    from django.conf import settings
    from ..models import UsageCounter

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
        msg = _format_quota_exceeded_message(user, metric, limit, row.count)
        raise QuotaExceeded(metric, limit, message=msg)
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
    from ..models import Plan, Subscription

    plan = Plan.objects.get(slug=plan_slug)
    sub = get_or_create_subscription(user)
    sub.plan = plan
    sub.status = status
    sub.save(update_fields=["plan", "status", "updated_at"])


USAGE_METRIC_LABELS: dict[str, str] = {
    METRIC_LLM_REQUESTS: "LLM requests (today)",
    METRIC_LLM_TOKENS: "LLM tokens (today)",
    METRIC_JOB_SEARCHES: "Job searches (today)",
}


def subscription_summary(user: AbstractBaseUser) -> dict:
    from ..models import Subscription
    sub = get_or_create_subscription(user)
    plan = get_user_plan(user)
    from .storage_quota import storage_summary

    usage = {}
    for metric in (
        METRIC_LLM_REQUESTS,
        METRIC_LLM_TOKENS,
        METRIC_JOB_SEARCHES,
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

    is_in_grace = False
    if sub.status == Subscription.Status.PAST_DUE:
        from datetime import timedelta
        grace_days = int(getattr(settings, "SAAS_PAST_DUE_GRACE_DAYS", 5) or 5)
        cutoff = timezone.now() - timedelta(days=grace_days)
        ref_time = sub.past_due_since or sub.updated_at
        is_in_grace = bool(ref_time and ref_time >= cutoff)

    return {
        "plan_slug": plan.slug if plan else None,
        "plan_name": plan.name if plan else None,
        "plan_price": (plan.price_display if plan else "") or "",
        "status": sub.status,
        "is_in_grace": is_in_grace,
        "past_due_since": sub.past_due_since,
        "api_access": bool(plan and plan.api_access),
        "stripe_customer_id": sub.stripe_customer_id or "",
        "trial_ends_at": sub.trial_ends_at,
        "current_period_end": sub.current_period_end,
        "usage": usage,
        "storage": storage_summary(user),
    }
