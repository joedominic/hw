"""Plan storage_mb hard enforcement for user-owned media."""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser

from .entitlements import QuotaExceeded, get_user_plan, staff_bypasses_quotas
from ..media_access import (
    _dir_size_storage,
    _listdir,
    _read_json_media,
    media_size,
)

logger = logging.getLogger(__name__)

METRIC_STORAGE = "storage_bytes"


def resume_storage_bytes(user: AbstractBaseUser) -> int:
    """Sum sizes of UserResume files for ``user``."""
    from ..models import UserResume

    total = 0
    for ur in UserResume.objects.for_user(user).only("file"):
        try:
            f = ur.file
            if not f:
                continue
            try:
                total += int(f.size)
                continue
            except Exception:
                pass
            name = getattr(f, "name", "") or ""
            if name:
                total += media_size(name)
        except Exception:
            continue
    return total


def apply_agent_storage_bytes(user: AbstractBaseUser) -> int:
    """Sum apply-agent screenshots and exported resumes for ``user``."""
    from ..models import ApplicationAttempt

    total = 0
    attempt_ids = ApplicationAttempt.objects.filter(
        pipeline_entry__owner_id=user.pk
    ).values_list("id", flat=True)
    for attempt_id in attempt_ids:
        total += _dir_size_storage(f"apply_agent/attempt_{attempt_id}")
        for ext in ("pdf", "docx"):
            total += media_size(f"apply_agent/attempt_{attempt_id}_resume.{ext}")
    return total


def pipeline_extract_storage_bytes(user: AbstractBaseUser) -> int:
    """Sum pipeline_llm_extract run dirs owned by ``user`` (via run_meta.owner_id)."""
    from ..pipeline_llm_skill_extract import RUN_META_NAME

    total = 0
    tracks, _ = _listdir("pipeline_llm_extract")
    for track in tracks:
        runs, _ = _listdir(f"pipeline_llm_extract/{track}")
        for run_id in runs:
            meta = _read_json_media(f"pipeline_llm_extract/{track}/{run_id}/{RUN_META_NAME}")
            if not meta:
                continue
            try:
                if int(meta.get("owner_id") or 0) != int(user.pk):
                    continue
            except (TypeError, ValueError):
                continue
            total += _dir_size_storage(f"pipeline_llm_extract/{track}/{run_id}")
    return total


def user_storage_bytes(user: AbstractBaseUser) -> int:
    """Total counted bytes for plan storage enforcement."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return (
        resume_storage_bytes(user)
        + apply_agent_storage_bytes(user)
        + pipeline_extract_storage_bytes(user)
    )


def plan_storage_limit_bytes(plan) -> int:
    """Return byte limit; 0 means unlimited."""
    if plan is None:
        return 0
    mb = int(getattr(plan, "storage_mb", 0) or 0)
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


def storage_summary(user: AbstractBaseUser) -> dict:
    plan = get_user_plan(user)
    used = user_storage_bytes(user)
    limit = plan_storage_limit_bytes(plan)
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "used_mb": round(used / (1024 * 1024), 2),
        "limit_mb": int(getattr(plan, "storage_mb", 0) or 0) if plan else 0,
    }


def check_storage_quota(user: AbstractBaseUser, additional_bytes: int = 0) -> None:
    """
    Raise QuotaExceeded if ``user`` cannot store ``additional_bytes`` more data.

    Honors ``SAAS_ENFORCE_QUOTAS``. Staff/superuser bypass only when
    ``SAAS_STAFF_BYPASS_QUOTAS`` is enabled.
    """
    if not getattr(settings, "SAAS_ENFORCE_QUOTAS", True):
        return
    if user is None or not getattr(user, "is_authenticated", False):
        return
    if staff_bypasses_quotas(user):
        return

    plan = get_user_plan(user)
    limit = plan_storage_limit_bytes(plan)
    if limit <= 0:
        return

    additional = max(0, int(additional_bytes or 0))
    used = user_storage_bytes(user)
    if used + additional > limit:
        limit_mb = max(1, limit // (1024 * 1024))
        used_mb = used / (1024 * 1024)
        raise QuotaExceeded(
            METRIC_STORAGE,
            limit_mb,
            message=(
                f"Storage quota exceeded ({used_mb:.1f} MB used of {limit_mb} MB). "
                f"Delete resumes or upgrade your plan."
            ),
        )


def assert_upload_allowed(user: AbstractBaseUser, file_obj) -> None:
    """Convenience: check storage for an uploaded file-like object."""
    size = int(getattr(file_obj, "size", 0) or 0)
    check_storage_quota(user, additional_bytes=size)
