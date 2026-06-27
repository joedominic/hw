"""Production-oriented settings helpers and per-user LLM rate limits."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

from .models import LLMAppUsageTotals


def check_user_llm_rate_limit(user: Optional[AbstractBaseUser]) -> None:
    """
    Raise LLMUserRateLimitExceeded when the user exceeds daily request cap.
    No-op when LLM_USER_DAILY_REQUEST_LIMIT is 0 (unlimited).
    """
    limit = getattr(settings, "LLM_USER_DAILY_REQUEST_LIMIT", 0)
    if not limit or user is None or not getattr(user, "is_authenticated", False):
        return
    totals = LLMAppUsageTotals.get_for_user(user)
    if totals.updated_at and totals.updated_at < timezone.now() - timedelta(days=1):
        totals.total_requests = 0
        totals.save(update_fields=["total_requests", "updated_at"])
    if totals.total_requests >= limit:
        raise LLMUserRateLimitExceeded(
            f"Daily LLM request limit ({limit}) reached for this account."
        )


class LLMUserRateLimitExceeded(Exception):
    """Per-user LLM usage cap exceeded."""
