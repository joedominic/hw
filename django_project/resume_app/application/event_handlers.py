"""
Domain Event Handlers wiring cross-aggregate side effects asynchronously or synchronously.
"""
from __future__ import annotations

import logging

from ..domain.event_bus import event_bus
from ..domain.events import (
    JobMarkedApplied,
    JobPromotedToApplying,
    JobPromotedToVetting,
    ResumeOptimizationCompleted,
)

logger = logging.getLogger(__name__)


def on_job_promoted_to_applying(event: JobPromotedToApplying) -> None:
    """Handle side-effects when a job reaches the Applying stage."""
    logger.info(
        "[DomainEvent] JobPromotedToApplying: user_id=%s entry_id=%s track=%s",
        event.user_id,
        event.entry_id,
        event.track,
    )


def on_resume_optimization_completed(event: ResumeOptimizationCompleted) -> None:
    """Log when resume optimization completes."""
    logger.info(
        "[DomainEvent] ResumeOptimizationCompleted: user_id=%s resume_id=%s entry_id=%s ats=%s recruiter=%s",
        event.user_id,
        event.optimized_resume_id,
        event.pipeline_entry_id,
        event.ats_score,
        event.recruiter_score,
    )


def on_job_marked_applied(event: JobMarkedApplied) -> None:
    """Handle side-effects when a job reaches the Applied (Done) stage."""
    logger.info(
        "[DomainEvent] JobMarkedApplied: user_id=%s entry_id=%s track=%s",
        event.user_id,
        event.entry_id,
        event.track,
    )
    from ..models import PipelineEntry
    from ..track_actions import record_job_liked

    entry = (
        PipelineEntry.objects.filter(id=event.entry_id)
        .select_related("owner", "job_listing", "search_profile")
        .first()
    )
    if entry and entry.owner and entry.job_listing:
        try:
            record_job_liked(
                user=entry.owner,
                job=entry.job_listing,
                track=entry.track,
                search_profile=entry.search_profile,
            )
        except Exception as e:
            logger.warning(
                "[DomainEvent] Failed to record job liked for entry_id=%s: %s",
                event.entry_id,
                e,
            )


def register_event_handlers() -> None:
    """Subscribe all handlers to the global domain event bus."""
    event_bus.subscribe(JobPromotedToApplying, on_job_promoted_to_applying)
    event_bus.subscribe(JobMarkedApplied, on_job_marked_applied)
    event_bus.subscribe(ResumeOptimizationCompleted, on_resume_optimization_completed)

