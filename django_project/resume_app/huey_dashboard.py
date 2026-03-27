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
        "display_name": "Backfill Vetting interview scores (LLM)",
        "cron_string": "*/20 * * * *",
        "basic": "Every 20 minutes, scores some Vetting jobs with an interview probability using your configured LLM.",
        "advanced": (
            "Looks at recent Vetting-stage PipelineEntry rows and enqueues evaluate_vetting_matching_task "
            "for entries missing vetting_interview_probability or scored with an older resume for that track "
            "(limited per tick). This is the main periodic LLM consumer for the Vetting stage."
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
        "task_fn_name": "refresh_pipeline_preferences_delta",
        "display_name": "Refresh pipeline fit scores (incremental)",
        "cron_string": "*/30 * * * *",
        "basic": "Every 30 minutes, computes fit/preference scores for new jobs that haven’t been scored yet.",
        "advanced": (
            "For each track, gathers job ids from pipeline + saved jobs and computes JobListingTrackMetrics "
            "for missing rows only (batching). After scoring, it may auto-promote Pipeline → Vetting "
            "depending on your App settings, which can indirectly trigger Vetting matching."
        ),
    },
    {
        "task_fn_name": "refresh_pipeline_preferences_full",
        "display_name": "Refresh pipeline fit scores (full + cleanup)",
        "cron_string": "0 0 * * *",
        "basic": "Once per day, recomputes fit/preference scores for all pipeline/saved jobs and cleans up low-signal jobs.",
        "advanced": (
            "For each track, recomputes JobListingTrackMetrics for pipeline + saved + already-scored jobs "
            "and upserts results. Also purges strongly negative jobs from the Pipeline stage. "
            "May auto-promote Pipeline → Vetting depending on App settings."
        ),
    },
    {
        "task_fn_name": "cleanup_inactive_pipeline_entries_daily",
        "display_name": "Clean inactive jobs (closed + duplicates)",
        "cron_string": "30 1 * * *",
        "basic": "Once daily, checks active pipeline-stage listings and removes jobs that appear closed or no longer accepting applications.",
        "advanced": (
            "Runs a best-effort URL check for active Pipeline/Vetting/Applying rows. "
            "Rows with explicit closed signals (404/410 or clear closed text) are soft-deleted. "
            "Then it runs all-track dedupe to remove location duplicates in active stages."
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

