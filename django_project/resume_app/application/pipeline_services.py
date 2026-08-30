"""
Application service for Career Pipeline use cases:
Promoting, bulk-moving, and dismissing jobs across pipeline stages.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import models

from ..models import AppAutomationSettings, JobListingTrackMetrics, PipelineEntry

logger = logging.getLogger(__name__)


@dataclass
class StageTransitionResult:
    success: bool
    promoted_count: int
    entry_ids: list[int]
    message: str = ""


class PipelineApplicationService:
    """
    Application Service orchestrating use cases for pipeline stages.
    Enforces business rules and delegates state mutations to PipelineEntry aggregates.
    """

    @staticmethod
    def promote_to_vetting(user, entry_ids: list[int]) -> StageTransitionResult:
        """Use Case: Move pipeline entries into Vetting stage and trigger evaluation."""
        entries = PipelineEntry.objects.for_user(user).filter(id__in=entry_ids, removed_at__isnull=True)
        promoted: list[int] = []
        for entry in entries:
            if entry.can_move_to_vetting():
                entry.move_to_vetting(save=True)
                promoted.append(entry.id)

        if promoted:
            from ..tasks import evaluate_vetting_matching_task
            evaluate_vetting_matching_task(user.id, promoted)

        return StageTransitionResult(
            success=True,
            promoted_count=len(promoted),
            entry_ids=promoted,
            message=f"Promoted {len(promoted)} job(s) to Vetting.",
        )

    @staticmethod
    def promote_to_applying(user, entry_ids: list[int]) -> StageTransitionResult:
        """Use Case: Move vetting entries into Applying stage."""
        entries = PipelineEntry.objects.for_user(user).filter(id__in=entry_ids, removed_at__isnull=True)
        promoted: list[int] = []
        for entry in entries:
            if entry.can_move_to_applying():
                entry.move_to_applying(save=True)
                promoted.append(entry.id)

        return StageTransitionResult(
            success=True,
            promoted_count=len(promoted),
            entry_ids=promoted,
            message=f"Promoted {len(promoted)} job(s) to Applying.",
        )

    @staticmethod
    def mark_applied(user, entry_ids: list[int]) -> StageTransitionResult:
        """Use Case: Mark jobs as applied (Done stage)."""
        entries = PipelineEntry.objects.for_user(user).filter(id__in=entry_ids, removed_at__isnull=True)
        marked: list[int] = []
        for entry in entries:
            if entry.can_mark_done():
                entry.mark_done(save=True)
                marked.append(entry.id)

        return StageTransitionResult(
            success=True,
            promoted_count=len(marked),
            entry_ids=marked,
            message=f"Marked {len(marked)} job(s) as Done.",
        )

    @staticmethod
    def dismiss_entries(user, entry_ids: list[int]) -> StageTransitionResult:
        """Use Case: Soft-delete jobs from pipeline."""
        entries = PipelineEntry.objects.for_user(user).filter(id__in=entry_ids)
        deleted: list[int] = []
        for entry in entries:
            entry.mark_deleted(save=True)
            deleted.append(entry.id)

        return StageTransitionResult(
            success=True,
            promoted_count=len(deleted),
            entry_ids=deleted,
            message=f"Removed {len(deleted)} job(s).",
        )
