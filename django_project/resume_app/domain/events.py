"""
Domain Event definitions representing meaningful business occurrences in the domain.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from django.utils import timezone


@dataclass(frozen=True)
class DomainEvent:
    """Base class for all Domain Events."""
    user_id: int
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=timezone.now)


@dataclass(frozen=True)
class JobPromotedToVetting(DomainEvent):
    """Fired when a pipeline entry is promoted to the Vetting stage."""
    entry_id: int = 0
    track: str = ""


@dataclass(frozen=True)
class JobPromotedToApplying(DomainEvent):
    """Fired when a pipeline entry reaches the Applying stage."""
    entry_id: int = 0
    track: str = ""


@dataclass(frozen=True)
class JobMarkedApplied(DomainEvent):
    """Fired when a job is marked as submitted/done."""
    entry_id: int = 0
    track: str = ""


@dataclass(frozen=True)
class ResumeOptimizationCompleted(DomainEvent):
    """Fired when a resume optimization multi-agent run completes successfully."""
    optimized_resume_id: int = 0
    pipeline_entry_id: Optional[int] = None
    ats_score: Optional[int] = None
    recruiter_score: Optional[int] = None


@dataclass(frozen=True)
class ApplicationAttemptSubmitted(DomainEvent):
    """Fired when the Autonomous Apply Agent submits an application."""
    attempt_id: int = 0
    pipeline_entry_id: int = 0
    ats_type: str = ""
