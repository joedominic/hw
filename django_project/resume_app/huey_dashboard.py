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
        "display_name": "Job search scheduler",
        "cron_string": "* * * * *",
        "description": "Enqueue due job-search tasks every minute.",
    },
    {
        "task_fn_name": "enqueue_due_vetting_matching_tasks",
        "display_name": "Vetting matching enqueue",
        "cron_string": "*/20 * * * *",
        "description": "Enqueue vetting matching evaluations every 20 minutes.",
    },
    {
        "task_fn_name": "mark_stale_job_search_runs_failed",
        "display_name": "Mark stale runs failed",
        "cron_string": "*/15 * * * *",
        "description": "Mark job search runs as failed if they've been RUNNING too long.",
    },
    {
        "task_fn_name": "refresh_pipeline_preferences_delta",
        "display_name": "Pipeline preferences delta",
        "cron_string": "*/30 * * * *",
        "description": "Refresh focus metrics for missing rows (delta) every 30 minutes.",
    },
    {
        "task_fn_name": "refresh_pipeline_preferences_full",
        "display_name": "Pipeline preferences full",
        "cron_string": "0 0 * * *",
        "description": "Full recompute of focus metrics once per day (00:00 UTC).",
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

