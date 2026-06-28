"""Purge ephemeral UserResume rows created by the resume optimizer."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def purge_generated_user_resumes(retention_days: int, *, user=None) -> int:
    """
    Delete optimizer copies (is_library=False) older than retention_days.

    Skips resumes tied to a queued or running OptimizedResume. Returns count removed.
    """
    if retention_days <= 0:
        return 0

    from .models import OptimizedResume, UserResume

    cutoff = timezone.now() - timedelta(days=retention_days)
    active_resume_ids = OptimizedResume.objects.filter(
        status__in=(OptimizedResume.STATUS_QUEUED, OptimizedResume.STATUS_RUNNING),
    )
    if user is not None:
        active_resume_ids = active_resume_ids.for_user(user)
    active_resume_ids = active_resume_ids.values_list("original_resume_id", flat=True)

    qs = UserResume.objects.filter(is_library=False, uploaded_at__lt=cutoff).exclude(
        id__in=active_resume_ids
    )
    if user is not None:
        qs = qs.filter(owner=user)
    qs = qs.order_by("uploaded_at")

    removed = 0
    for resume in qs.iterator(chunk_size=100):
        try:
            resume.file.delete(save=False)
        except Exception:
            pass
        resume.delete()
        removed += 1

    if removed:
        logger.info(
            "[purge_generated_user_resumes] removed=%d retention_days=%d",
            removed,
            retention_days,
        )
    return removed
