"""
Domain Event Handlers wiring cross-aggregate side effects asynchronously or synchronously.
"""
from __future__ import annotations

import logging

from ..domain.event_bus import event_bus
from ..domain.events import (
    ApplicationAttemptSubmitted,
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
    """Advance waiting ApplicationAttempt if resume optimization finishes."""
    logger.info(
        "[DomainEvent] ResumeOptimizationCompleted: user_id=%s resume_id=%s entry_id=%s ats=%s recruiter=%s",
        event.user_id,
        event.optimized_resume_id,
        event.pipeline_entry_id,
        event.ats_score,
        event.recruiter_score,
    )
    if event.pipeline_entry_id:
        from ..models import ApplicationAttempt
        from ..tasks import run_apply_agent_step

        waiting_attempts = ApplicationAttempt.objects.filter(
            pipeline_entry_id=event.pipeline_entry_id,
            status=ApplicationAttempt.Status.WAITING_OPTIMIZER,
        )
        for attempt in waiting_attempts:
            run_apply_agent_step(event.user_id, attempt.id)


def on_application_attempt_submitted(event: ApplicationAttemptSubmitted) -> None:
    """Mark the associated PipelineEntry as Done once submitted."""
    logger.info(
        "[DomainEvent] ApplicationAttemptSubmitted: user_id=%s attempt_id=%s entry_id=%s ats=%s",
        event.user_id,
        event.attempt_id,
        event.pipeline_entry_id,
        event.ats_type,
    )
    if event.pipeline_entry_id:
        from ..models import PipelineEntry

        entry = PipelineEntry.objects.filter(id=event.pipeline_entry_id).first()
        if entry and entry.can_mark_done():
            entry.mark_done(save=True)


def register_event_handlers() -> None:
    """Subscribe all handlers to the global domain event bus."""
    event_bus.subscribe(JobPromotedToApplying, on_job_promoted_to_applying)
    event_bus.subscribe(ResumeOptimizationCompleted, on_resume_optimization_completed)
    event_bus.subscribe(ApplicationAttemptSubmitted, on_application_attempt_submitted)
