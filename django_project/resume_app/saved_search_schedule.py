"""Schedule saved job searches via JobSearchTask (human-friendly interval + time)."""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from django.core.exceptions import ValidationError

from .job_search_core import normalize_site_names
from .models import JobSearchTask, SavedJobSearch, Track
from .tasks import get_next_run_at, validate_cron

DEFAULT_SCHEDULE_TIME = "09:00"

SCHEDULE_OFF = "off"
SCHEDULE_DAILY = "daily"
SCHEDULE_WEEKDAYS = "weekdays"
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_CUSTOM = "custom"

VALID_SCHEDULE_INTERVALS = {
    SCHEDULE_OFF,
    SCHEDULE_DAILY,
    SCHEDULE_WEEKDAYS,
    SCHEDULE_WEEKLY,
}


def parse_schedule_time(time_str: str) -> time:
    """Parse HH:MM; fall back to DEFAULT_SCHEDULE_TIME."""
    raw = (time_str or DEFAULT_SCHEDULE_TIME).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return datetime.strptime(DEFAULT_SCHEDULE_TIME, "%H:%M").time()


def cron_from_schedule(interval: str, time_str: str) -> str:
    """Map daily/weekdays/weekly + time to a five-field cron expression."""
    interval = (interval or "").strip().lower()
    if interval not in {SCHEDULE_DAILY, SCHEDULE_WEEKDAYS, SCHEDULE_WEEKLY}:
        raise ValueError("Invalid schedule interval.")
    t = parse_schedule_time(time_str)
    minute, hour = t.minute, t.hour
    if interval == SCHEDULE_DAILY:
        return f"{minute} {hour} * * *"
    if interval == SCHEDULE_WEEKDAYS:
        return f"{minute} {hour} * * 1-5"
    return f"{minute} {hour} * * 1"


def schedule_from_cron(cron_string: str) -> tuple[str, str] | None:
    """
    Reverse-map a simple cron to (interval, HH:MM).
    Returns None when the expression is not a supported preset pattern.
    """
    parts = (cron_string or "").strip().split()
    if len(parts) != 5:
        return None
    minute_s, hour_s, dom, month, dow = parts
    if dom != "*" or month != "*":
        return None
    try:
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        time_str = f"{hour:02d}:{minute:02d}"
    except ValueError:
        return None
    if dow == "*":
        return SCHEDULE_DAILY, time_str
    if dow == "1-5":
        return SCHEDULE_WEEKDAYS, time_str
    if dow == "1":
        return SCHEDULE_WEEKLY, time_str
    return None


def format_schedule_time_display(time_str: str) -> str:
    """Format HH:MM for UI (e.g. 9:00 AM)."""
    t = parse_schedule_time(time_str)
    return t.strftime("%I:%M %p").lstrip("0")


def schedule_display_label(interval: str, time_str: str) -> str:
    if interval == SCHEDULE_OFF:
        return "Not scheduled"
    if interval == SCHEDULE_CUSTOM:
        return "Custom schedule"
    names = {
        SCHEDULE_DAILY: "Daily",
        SCHEDULE_WEEKDAYS: "Weekdays",
        SCHEDULE_WEEKLY: "Weekly",
    }
    return f"{names.get(interval, interval)} · {format_schedule_time_display(time_str)}"


def sync_job_search_task_from_saved(saved: SavedJobSearch, task: JobSearchTask) -> None:
    """Copy search configuration from a saved search onto a scheduled task."""
    default_slug = Track.get_default_slug(saved.owner)
    task.name = saved.name
    task.search_term = ((saved.search_term or "").strip()[:512] or saved.name)[:512]
    task.location = (saved.location or "").strip()[:512]
    task.track = ((saved.slug or saved.profile_slug or default_slug).strip().lower())[:32]
    task.jobs_to_fetch = max(10, min(200, int(saved.results_wanted or 50)))
    task.site_name = normalize_site_names(
        saved.site_names if isinstance(saved.site_names, list) else None
    )


def _task_for_saved_search(user, saved: SavedJobSearch) -> JobSearchTask | None:
    return JobSearchTask.objects.for_user(user).filter(saved_search=saved).first()


def build_saved_search_schedule_map(user) -> dict[int, dict[str, Any]]:
    """Active schedules keyed by saved_search id (for pill badges)."""
    tasks = (
        JobSearchTask.objects.for_user(user)
        .filter(saved_search__isnull=False, is_active=True)
        .select_related("saved_search")
    )
    result: dict[int, dict[str, Any]] = {}
    for task in tasks:
        sid = task.saved_search_id
        if sid is None:
            continue
        parsed = schedule_from_cron(task.frequency)
        if parsed:
            interval, time_str = parsed
            result[sid] = {
                "interval": interval,
                "time": time_str,
                "label": schedule_display_label(interval, time_str),
                "is_active": True,
                "is_custom": False,
                "task_id": task.id,
            }
        else:
            result[sid] = {
                "interval": SCHEDULE_CUSTOM,
                "time": "",
                "label": schedule_display_label(SCHEDULE_CUSTOM, ""),
                "is_active": True,
                "is_custom": True,
                "task_id": task.id,
            }
    return result


def get_schedule_form_defaults(user, saved: SavedJobSearch | None) -> dict[str, Any]:
    """Form defaults for the schedule panel on a selected saved search."""
    base: dict[str, Any] = {
        "interval": SCHEDULE_DAILY,
        "time": DEFAULT_SCHEDULE_TIME,
        "is_active": False,
        "is_custom": False,
        "task_id": None,
        "label": "",
    }
    if not saved:
        return {
            **base,
            "interval": SCHEDULE_OFF,
        }

    task = _task_for_saved_search(user, saved)
    if not task:
        return {
            **base,
            "interval": SCHEDULE_OFF,
        }

    if not task.is_active:
        parsed = schedule_from_cron(task.frequency)
        time_str = parsed[1] if parsed else DEFAULT_SCHEDULE_TIME
        return {
            "interval": SCHEDULE_OFF,
            "time": time_str,
            "is_active": False,
            "is_custom": False,
            "task_id": task.id,
            "label": "",
        }

    parsed = schedule_from_cron(task.frequency)
    if parsed:
        interval, time_str = parsed
        return {
            "interval": interval,
            "time": time_str,
            "is_active": True,
            "is_custom": False,
            "task_id": task.id,
            "label": schedule_display_label(interval, time_str),
        }

    return {
        "interval": SCHEDULE_CUSTOM,
        "time": DEFAULT_SCHEDULE_TIME,
        "is_active": True,
        "is_custom": True,
        "task_id": task.id,
        "label": schedule_display_label(SCHEDULE_CUSTOM, ""),
    }


def set_saved_search_schedule(
    user,
    preset_id: int,
    *,
    interval: str,
    time_str: str = "",
) -> JobSearchTask | None:
    """
    Create, update, or deactivate the JobSearchTask linked to a saved search.
    interval: off | daily | weekdays | weekly
    """
    from .saved_searches import get_saved_search_or_none

    saved = get_saved_search_or_none(user, preset_id)
    if not saved:
        raise ValueError("Saved search not found.")

    interval = (interval or SCHEDULE_OFF).strip().lower()
    if interval not in VALID_SCHEDULE_INTERVALS:
        raise ValueError("Invalid schedule interval.")

    task = _task_for_saved_search(user, saved)

    if interval == SCHEDULE_OFF:
        if task:
            task.is_active = False
            task.save(update_fields=["is_active", "updated_at"])
        return task

    frequency = cron_from_schedule(interval, time_str)
    validate_cron(frequency)
    start_time = parse_schedule_time(time_str)

    if task is None:
        task = JobSearchTask(owner=user, saved_search=saved)

    sync_job_search_task_from_saved(saved, task)
    task.frequency = frequency
    task.start_time = start_time
    task.is_active = True
    try:
        task.full_clean()
    except ValidationError as e:
        if hasattr(e, "message_dict"):
            parts: list[str] = []
            for v in e.message_dict.values():
                parts.extend(v if isinstance(v, list) else [str(v)])
            raise ValueError("; ".join(parts)) from e
        raise ValueError(str(e)) from e
    task.next_run_at = get_next_run_at(task.frequency)
    task.save()
    return task
