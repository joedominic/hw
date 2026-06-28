"""
Autonomous Apply Agent API (JSON).

Mounted under ``/api/resume/apply/…``. Drives starting attempts, reading status,
and the human approval actions (approve / reject / override URL) used by the
review UI.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from ninja import Router, Schema
from ninja.errors import HttpError

from .models import ApplicationAttempt, PipelineEntry
from .tenancy import api_user

logger = logging.getLogger(__name__)

router = Router()


class StartApplyIn(Schema):
    pipeline_entry_ids: List[int]
    automation_mode: Optional[str] = None


class OverrideUrlIn(Schema):
    url: str


class ApproveIn(Schema):
    corrected: bool = False


def _attempt_payload(attempt: ApplicationAttempt) -> dict:
    return {
        "id": attempt.id,
        "pipeline_entry_id": attempt.pipeline_entry_id,
        "status": attempt.status,
        "status_label": attempt.get_status_display(),
        "automation_mode": attempt.automation_mode,
        "apply_url": attempt.apply_url,
        "ats_type": attempt.ats_type,
        "confidence": attempt.confidence,
        "fill_payload": attempt.fill_payload_json or {},
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "optimized_resume_id": attempt.optimized_resume_id,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "updated_at": attempt.updated_at.isoformat() if attempt.updated_at else None,
    }


def _get_user_attempt(user, attempt_id: int) -> ApplicationAttempt:
    """Return attempt only when its pipeline entry belongs to the tenant user."""
    attempt = (
        ApplicationAttempt.objects.filter(
            id=attempt_id,
            pipeline_entry__owner=user,
        )
        .select_related("pipeline_entry")
        .first()
    )
    if not attempt:
        raise HttpError(404, "Attempt not found.")
    return attempt


@router.post("/start")
def start(request, payload: StartApplyIn):
    """Create attempts for the given Applying-stage entries and kick off processing."""
    from .apply_agent import orchestrator
    from .tasks import run_apply_agent_step

    user = api_user(request)
    if not payload.pipeline_entry_ids:
        raise HttpError(400, "No pipeline entries provided.")
    mode = payload.automation_mode
    if mode and mode not in (ApplicationAttempt.Mode.SEMI_AUTO, ApplicationAttempt.Mode.FULL_AUTO):
        raise HttpError(400, "Invalid automation_mode.")

    valid_entry_ids = list(
        PipelineEntry.objects.for_user(user)
        .filter(
            id__in=payload.pipeline_entry_ids,
            stage=PipelineEntry.Stage.APPLYING,
            removed_at__isnull=True,
        )
        .values_list("id", flat=True)
    )
    if not valid_entry_ids:
        raise HttpError(400, "No valid pipeline entries provided.")

    attempts = orchestrator.start_attempts_for_entries(valid_entry_ids, user_id=user.id, mode=mode)
    # Fast-path: enqueue the first step immediately (heartbeat remains primary).
    for attempt in attempts:
        run_apply_agent_step(user.id, attempt.id)
    return {
        "status": "ok",
        "created": len(attempts),
        "attempt_ids": [a.id for a in attempts],
    }


@router.get("/{attempt_id}")
def get_attempt(request, attempt_id: int):
    user = api_user(request)
    attempt = _get_user_attempt(user, attempt_id)
    steps = [
        {
            "step_name": s.step_name,
            "message": s.message,
            "screenshot_path": s.screenshot_path,
            "network_log": s.network_log or [],
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in attempt.steps.all()
    ]
    data = _attempt_payload(attempt)
    data["steps"] = steps
    return data


@router.post("/{attempt_id}/approve")
def approve(request, attempt_id: int, payload: ApproveIn):
    from .apply_agent import orchestrator
    from .tasks import run_apply_agent_step

    user = api_user(request)
    _get_user_attempt(user, attempt_id)
    attempt = orchestrator.approve_attempt(attempt_id, corrected=payload.corrected)
    if not attempt:
        raise HttpError(404, "Attempt not found.")
    if attempt.status == ApplicationAttempt.Status.SUBMITTING:
        run_apply_agent_step(user.id, attempt.id)
    return _attempt_payload(attempt)


@router.post("/{attempt_id}/reject")
def reject(request, attempt_id: int):
    from .apply_agent import orchestrator

    user = api_user(request)
    _get_user_attempt(user, attempt_id)
    attempt = orchestrator.reject_attempt(attempt_id)
    if not attempt:
        raise HttpError(404, "Attempt not found.")
    return _attempt_payload(attempt)


@router.post("/{attempt_id}/override-url")
def override_url(request, attempt_id: int, payload: OverrideUrlIn):
    from .apply_agent import orchestrator
    from .tasks import run_apply_agent_step

    user = api_user(request)
    _get_user_attempt(user, attempt_id)
    attempt = orchestrator.set_override_url(attempt_id, payload.url)
    if not attempt:
        raise HttpError(404, "Attempt not found.")
    run_apply_agent_step(user.id, attempt.id)
    return _attempt_payload(attempt)
