"""Production-oriented settings helpers and per-user LLM rate limits / plan quotas."""
from __future__ import annotations

from typing import Optional

from django.contrib.auth.models import AbstractBaseUser

from .subscriptions import METRIC_LLM_REQUESTS, QuotaExceeded, check_quota, consume_quota


class LLMUserRateLimitExceeded(Exception):
    """Per-user LLM usage cap exceeded."""


def check_user_llm_rate_limit(user: Optional[AbstractBaseUser]) -> None:
    """
    Enforce plan daily LLM quota (and optional LLM_USER_DAILY_REQUEST_LIMIT ceiling).
    Raises LLMUserRateLimitExceeded when over quota.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return
    try:
        check_quota(user, METRIC_LLM_REQUESTS)
    except QuotaExceeded as exc:
        raise LLMUserRateLimitExceeded(str(exc)) from exc


def record_llm_request(user: Optional[AbstractBaseUser]) -> None:
    """Increment durable LLM usage when an invoke is about to run."""
    if user is None or not getattr(user, "is_authenticated", False):
        return
    try:
        consume_quota(user, METRIC_LLM_REQUESTS, 1)
    except QuotaExceeded as exc:
        raise LLMUserRateLimitExceeded(str(exc)) from exc
