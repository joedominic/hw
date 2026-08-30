"""
Application service for Autonomous Apply Agent use cases:
Starting application runs, reviewing dry-runs, approving submissions, and managing credentials.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model

from ..apply_agent import orchestrator
from ..domain.event_bus import event_bus
from ..domain.events import ApplicationAttemptSubmitted
from ..models import ApplicantProfile, ApplicationAttempt, PipelineEntry

logger = logging.getLogger(__name__)


@dataclass
class ApplyStartResult:
    success: bool
    started_attempts: list[ApplicationAttempt]
    message: str = ""


class ApplyApplicationService:
    """
    Application Service orchestrating Autonomous Apply Agent workflows.
    """

    @staticmethod
    def start_attempts_for_jobs(
        user,
        entry_ids: list[int],
        mode: Optional[str] = None,
    ) -> ApplyStartResult:
        """
        Use Case: Verify profile prerequisites and start apply attempts for selected jobs.
        """
        profile = ApplicantProfile.get_for_user(user)
        if not (profile.full_name and profile.email):
            return ApplyStartResult(
                success=False,
                started_attempts=[],
                message="Set your name and email on the Apply Agent profile page before starting.",
            )

        if not entry_ids:
            return ApplyStartResult(
                success=False,
                started_attempts=[],
                message="Select at least one job before starting the apply agent.",
            )

        from ..tasks import run_apply_agent_step

        attempts = orchestrator.start_attempts_for_entries(entry_ids, user_id=user.id, mode=mode)
        for attempt in attempts:
            run_apply_agent_step(user.id, attempt.id)

        if not attempts:
            return ApplyStartResult(
                success=True,
                started_attempts=[],
                message="Selected jobs already have an apply attempt in progress.",
            )

        return ApplyStartResult(
            success=True,
            started_attempts=attempts,
            message=f"Started the apply agent for {len(attempts)} job(s).",
        )

    @staticmethod
    def approve_attempt(user, attempt_id: int, corrected: bool = False) -> tuple[bool, str]:
        """
        Use Case: Approve a dry-run attempt and advance to Submitting.
        """
        attempt = ApplicationAttempt.objects.filter(
            id=attempt_id, pipeline_entry__owner=user
        ).first()
        if not attempt:
            return False, "Apply attempt not found."

        updated = orchestrator.approve_attempt(attempt_id, corrected=corrected)
        if updated and updated.status == ApplicationAttempt.Status.SUBMITTING:
            from ..tasks import run_apply_agent_step
            run_apply_agent_step(user.id, attempt_id)
            return True, "Approved. Submitting the application now."
        if updated and updated.status == ApplicationAttempt.Status.SUCCEEDED:
            event_bus.publish(
                ApplicationAttemptSubmitted(
                    user_id=user.id,
                    attempt_id=attempt.id,
                    pipeline_entry_id=attempt.pipeline_entry_id,
                    ats_type=attempt.ats_type,
                )
            )
            return True, "Marked as applied. Job moved to Done."

        return False, "Attempt is not awaiting approval."

    @staticmethod
    def reject_attempt(user, attempt_id: int, reason: str = "") -> tuple[bool, str]:
        """
        Use Case: Reject a dry-run attempt.
        """
        attempt = ApplicationAttempt.objects.filter(
            id=attempt_id, pipeline_entry__owner=user
        ).first()
        if not attempt:
            return False, "Apply attempt not found."

        orchestrator.reject_attempt(attempt_id)
        return True, "Attempt rejected."
