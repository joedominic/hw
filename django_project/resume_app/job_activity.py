"""
Utilities for checking whether job postings still look active.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests
from django.db import models
import logging

from .models import JobListing, PipelineEntry

# Use Huey's logger so progress lines appear in run_huey output consistently.
logger = logging.getLogger("huey")

_CLOSED_HINTS = (
    "no longer accepting applications",
    "no longer available",
    "job is no longer available",
    "this job has expired",
    "position has been filled",
    "applications are closed",
    "posting has expired",
    "job closed",
    "this posting is no longer active",
    "position no longer available",
)

_CLOSED_URL_HINTS = (
    "job-not-found",
    "jobs-not-found",
    "no-longer-available",
    "position-closed",
)


@dataclass
class JobActivityCheck:
    # True = active, False = inactive, None = unknown (do not delete)
    active: bool | None
    reason: str


def check_job_listing_active(job: JobListing, timeout_seconds: float = 8.0) -> JobActivityCheck:
    """
    Best-effort check for whether a listing still appears active.

    Conservative policy:
    - Unknown checks never delete.
    - Explicit "closed" signals (HTTP status or page text) mark inactive.
    """
    url = (job.url or "").strip()
    if not url:
        return JobActivityCheck(active=None, reason="missing_url")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ResumeEliteBot/1.0; +https://localhost)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(
            url,
            timeout=timeout_seconds,
            allow_redirects=True,
            headers=headers,
        )
    except requests.RequestException as e:
        return JobActivityCheck(active=None, reason=f"request_error:{type(e).__name__}")

    status = int(resp.status_code or 0)
    final_url = str(resp.url or url).lower()
    if status in (404, 410, 451):
        return JobActivityCheck(active=False, reason=f"http_{status}")
    if any(hint in final_url for hint in _CLOSED_URL_HINTS):
        return JobActivityCheck(active=False, reason="closed_url_hint")

    if status >= 500:
        return JobActivityCheck(active=None, reason=f"http_{status}")

    body = (resp.text or "")[:120000].lower()
    if any(hint in body for hint in _CLOSED_HINTS):
        return JobActivityCheck(active=False, reason="closed_text_hint")

    if 200 <= status < 400:
        return JobActivityCheck(active=True, reason=f"http_{status}")
    return JobActivityCheck(active=None, reason=f"http_{status}")


def purge_inactive_pipeline_entries(
    *,
    limit: int = 500,
    per_owner_limit: int | None = None,
) -> dict[str, int]:
    """
    Check applying-stage rows and soft-delete ones whose job listing is inactive.

    Fairly distributes the check budget across owners so one tenant cannot
    monopolize the batch (and so cleanup remains tenant-aware).
    """
    base = (
        PipelineEntry.objects.filter(removed_at__isnull=True)
        .filter(stage=PipelineEntry.Stage.APPLYING)
        .select_related("job_listing")
    )
    owner_ids = list(base.values_list("owner_id", flat=True).distinct().order_by("owner_id"))
    if not owner_ids:
        return {"checked": 0, "removed_inactive": 0, "active": 0, "unknown": 0, "owners": 0}

    total_limit = max(1, int(limit))
    if per_owner_limit is None:
        # Equal share, at least 1 per owner, capped by remaining budget.
        per_owner_limit = max(1, total_limit // max(1, len(owner_ids)))
    else:
        per_owner_limit = max(1, int(per_owner_limit))

    checked = 0
    removed = 0
    unknown = 0
    active = 0
    progress_every = 25

    for owner_id in owner_ids:
        if checked >= total_limit:
            break
        remaining = total_limit - checked
        take = min(per_owner_limit, remaining)
        entries = list(base.filter(owner_id=owner_id).order_by("-added_at")[:take])
        for entry in entries:
            checked += 1
            result = check_job_listing_active(entry.job_listing)
            if result.active is False:
                entry.mark_deleted(save=True)
                removed += 1
            elif result.active is True:
                active += 1
            else:
                unknown += 1
            if checked % progress_every == 0:
                logger.info(
                    "[purge_inactive_pipeline_entries] progress checked=%d/%d "
                    "removed_inactive=%d active=%d unknown=%d owners=%d",
                    checked,
                    total_limit,
                    removed,
                    active,
                    unknown,
                    len(owner_ids),
                )

    return {
        "checked": checked,
        "removed_inactive": removed,
        "active": active,
        "unknown": unknown,
        "owners": len(owner_ids),
    }

