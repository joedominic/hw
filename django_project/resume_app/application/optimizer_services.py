"""
Application service for Resume Optimization use cases:
Initiating tailored multi-agent runs, tracking progress, and exporting documents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from django.utils import timezone

from ..domain.event_bus import event_bus
from ..domain.events import ResumeOptimizationCompleted
from ..models import (
    AtsJudgeProfile,
    JobDescription,
    OptimizedResume,
    OptimizerWorkflow,
    PipelineEntry,
    UserResume,
)

logger = logging.getLogger(__name__)


@dataclass
class OptimizationStartResult:
    success: bool
    optimized_resume: Optional[OptimizedResume]
    message: str = ""


class OptimizerApplicationService:
    """
    Application Service orchestrating resume optimization workflows.
    """

    @staticmethod
    def start_optimization(
        user,
        resume_id: int,
        job_description_text: str,
        pipeline_entry_id: Optional[int] = None,
        workflow_id: Optional[int] = None,
        ats_profile_id: Optional[int] = None,
        notes: str = "",
        skills_json: str = "",
        highlights: str = "",
        llm_model: Optional[str] = None,
    ) -> OptimizationStartResult:
        """
        Use Case: Enqueue a multi-agent resume optimization run for a user.
        """
        resume = UserResume.objects.for_user(user).filter(id=resume_id).first()
        if not resume:
            return OptimizationStartResult(success=False, optimized_resume=None, message="Resume not found.")

        jd_text = (job_description_text or "").strip()
        if not jd_text:
            return OptimizationStartResult(success=False, optimized_resume=None, message="Job description is required.")

        jd_obj = JobDescription.objects.create(content=jd_text)

        wf = None
        if workflow_id:
            wf = OptimizerWorkflow.objects.filter(id=workflow_id).first()

        ats_prof = None
        if ats_profile_id:
            ats_prof = AtsJudgeProfile.objects.filter(id=ats_profile_id).first()

        pe = None
        if pipeline_entry_id:
            pe = PipelineEntry.objects.for_user(user).filter(id=pipeline_entry_id).first()

        opt = OptimizedResume.objects.create(
            owner=user,
            original_resume=resume,
            job_description=jd_obj,
            pipeline_entry=pe,
            optimizer_workflow=wf,
            ats_judge_profile=ats_prof,
            optimization_notes=notes or "",
            pipeline_skills_json=skills_json or "",
            job_highlights=highlights or "",
            status=OptimizedResume.STATUS_QUEUED,
        )

        from ..tasks import run_optimized_resume_workflow_task

        run_optimized_resume_workflow_task(opt.id, llm_model=llm_model)

        return OptimizationStartResult(
            success=True,
            optimized_resume=opt,
            message="Resume optimization enqueued successfully.",
        )

    @staticmethod
    def record_completed(opt: OptimizedResume) -> None:
        """
        Fired when optimization completes; publishes domain event.
        """
        event = ResumeOptimizationCompleted(
            user_id=opt.owner_id,
            optimized_resume_id=opt.id,
            pipeline_entry_id=opt.pipeline_entry_id,
            ats_score=opt.ats_score,
            recruiter_score=opt.recruiter_score,
        )
        event_bus.publish(event)
