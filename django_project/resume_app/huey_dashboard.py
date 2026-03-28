"""
Huey dashboard metadata and helpers.

We intentionally keep this small and data-driven so the UI and views can stay
stable even if the actual periodic task implementations move over time.
"""

from __future__ import annotations

from typing import Any


# NOTE: task_fn_name must match the function name exported from resume_app.tasks.
PERIODIC_TASKS: list[dict[str, str]] = [
    {
        "task_fn_name": "enqueue_due_job_search_tasks",
        "display_name": "Run scheduled job searches",
        "cron_string": "* * * * *",
        "basic": "Checks every minute for scheduled searches that are due, and starts the next one.",
        "advanced": (
            "Finds active JobSearchTask rows where next_run_at <= now (at most 1 per tick), "
            "updates next_run_at to the next cron occurrence, and enqueues run_job_search_task(task.id). "
            "This is the scheduler; it does not fetch jobs itself."
        ),
    },
    {
        "task_fn_name": "enqueue_due_vetting_matching_tasks",
        "display_name": "Vetting Manager",
        "cron_string": "*/20 * * * *",
        "basic": "Every 20 minutes, scores some Vetting jobs with interview probability via your configured LLM (job description must be at least 2000 characters).",
        "advanced": (
            "Looks at recent Vetting-stage PipelineEntry rows and enqueues evaluate_vetting_matching_task "
            "for entries missing vetting_interview_probability or scored with an older resume for that track "
            "(limited per tick). Skips LLM when JobListing.description is under VETTING_MATCHING_JD_MIN_CHARS. "
            "LLM is the active LLMProviderConfig or first entry from get_runtime_provider_candidates() unless "
            "the task is called with explicit provider/model overrides."
        ),
    },
    {
        "task_fn_name": "mark_stale_job_search_runs_failed",
        "display_name": "Mark stuck job searches as failed",
        "cron_string": "*/15 * * * *",
        "basic": "Every 15 minutes, marks job-search runs as failed if they have been running too long.",
        "advanced": (
            "Finds JobSearchTaskRun rows in RUNNING older than JOB_SEARCH_RUN_STALE_MINUTES "
            "and marks them FAILED with a timeout error message. Helps keep the UI accurate if a worker dies."
        ),
    },
    {
        "task_fn_name": "pipeline_manager",
        "display_name": "Pipeline Manager",
        "cron_string": "*/30 * * * *",
        "basic": "Every 30 minutes, maintains Pipeline-stage rows: refresh fit metrics, margin purge, auto-promote to Vetting.",
        "advanced": (
            "For each track, only active Pipeline (or legacy blank) stage entries: recomputes "
            "JobListingTrackMetrics when missing or older than PIPELINE_MANAGER_STATS_MAX_AGE_DAYS; removes entries "
            "whose preference_margin is below PIPELINE_MANAGER_PURGE_MARGIN_MAX (hard-deletes PipelineEntry unless "
            "the job has liked/disliked actions—then mark_deleted). Age-based removal for every stage is handled by "
            "Cleanup Manager (Settings → retention days). Finally apply_pipeline_auto_promotions() may move rows to "
            "Vetting and enqueue vetting matching. Saved-only listings are not scored by this task."
        ),
    },
    {
        "task_fn_name": "cleanup_manager",
        "display_name": "Cleanup Manager",
        "cron_string": "30 1 * * *",
        "basic": "Once daily: dedupe Pipeline/Vetting/Applying rows, purge rows past per-stage age (Settings), then check Applying listings for closed postings.",
        "advanced": (
            "Order: (1) dedupe_pipeline_entries(track_slug=\"*\", stage=\"all\", include_done=False) across active "
            "stages; (2) apply_cleanup_retention_purge() using AppAutomationSettings cleanup_*_retention_days "
            "(0 = skip that stage; uses PipelineEntry.added_at); (3) purge_inactive_pipeline_entries(limit=400) — "
            "URL checks for Applying-stage rows only. Liked/disliked jobs are soft-deleted on purge instead of "
            "hard-deleted."
        ),
    },
]


def get_periodic_task_wrapper(task_fn_name: str) -> Any | None:
    """
    Return Huey TaskWrapper for a periodic task decorated in resume_app.tasks.

    Huey periodic tasks support:
    - revoke() to pause execution
    - restore() to re-enable execution
    - is_revoked() to query paused state
    """

    from . import tasks as resume_tasks

    wrapper = getattr(resume_tasks, task_fn_name, None)
    return wrapper


def get_periodic_task_info(task_fn_name: str) -> dict[str, str] | None:
    for info in PERIODIC_TASKS:
        if info["task_fn_name"] == task_fn_name:
            return info
    return None

