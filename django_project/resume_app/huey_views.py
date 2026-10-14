"""
Huey operational dashboard, periodic task control, and JobSearchTask views.
"""

from __future__ import annotations

import time
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from .job_sources import DEFAULT_SITE_NAMES, normalize_site_names
from .models import JobSearchTask, JobSearchTaskRun, Track
from .tasks import (
    CLEANUP_STATUS_CACHE_KEY,
    get_job_search_task_lock_key,
    get_next_run_at,
    run_job_search_task,
    validate_cron,
)
from .huey_dashboard import (
    ADHOC_RUN_NOW_TASKS,
    PERIODIC_TASKS,
    get_periodic_task_info,
    get_periodic_task_wrapper,
    get_run_now_display_name,
    run_now_task_names,
)
from .tenancy import get_owned_or_404
from .utils import cron_to_short_description


def _parse_task_form(request, default_track: str, valid_track_slugs: set):
    """
    Parse shared job task form fields from POST. Returns (data, errors).
    data: dict with name, search_term, location, track, jobs_to_fetch, frequency, start_time, site_name.
    errors: list of message strings; if non-empty, data may be incomplete.
    """
    name = (request.POST.get("name") or "").strip()
    search_term = (request.POST.get("search_term") or "").strip()
    if not search_term:
        return {}, ["Search term is required."]
    location = (request.POST.get("location") or "").strip()
    raw_track = (request.POST.get("track") or "").strip().lower()
    track = raw_track if raw_track in valid_track_slugs else default_track
    try:
        jobs_to_fetch = max(10, min(200, int(request.POST.get("jobs_to_fetch") or 50)))
    except ValueError:
        jobs_to_fetch = 50
    frequency = (request.POST.get("frequency") or "0 9 * * *").strip()
    try:
        validate_cron(frequency)
    except ValueError as e:
        return {}, [str(e)]
    start_time = None
    start_time_str = (request.POST.get("start_time") or "").strip()
    if start_time_str:
        try:
            from datetime import datetime
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
        except ValueError:
            pass
    site_name = normalize_site_names(request.POST.getlist("site_name") or None)
    return {
        "name": name,
        "search_term": search_term,
        "location": location,
        "track": track,
        "jobs_to_fetch": jobs_to_fetch,
        "frequency": frequency,
        "start_time": start_time,
        "site_name": site_name,
    }


def staff_required(view_fn):
    """Decorator: returns 403 for non-staff users on operational/monitor endpoints."""
    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not (request.user and request.user.is_authenticated and request.user.is_staff):
            return HttpResponseForbidden("Staff access required.")
        return view_fn(request, *args, **kwargs)

    return _wrapped


def job_tasks_view(request):
    """Decommissioned in favor of Career Cockpit Search Schedule. Redirects to career_cockpit."""
    track = (request.GET.get("track") or "").strip()
    cockpit_url = reverse("career_cockpit")
    if track:
        cockpit_url += f"?track={track}"
    return redirect(cockpit_url)


@staff_required
def huey_dashboard_view(request):
    """UI for monitoring Huey queue depth and controlling periodic tasks (pause/restore)."""
    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    queue_stats = None
    queue_stats_error = None
    if not immediate:
        try:
            storage = HUEY.storage
            queue_stats = {
                "pending": storage.queue_size(),
                "scheduled": storage.schedule_size(),
                "results": storage.result_store_size(),
            }
        except Exception as e:
            queue_stats_error = str(e)

    periodic_rows: list[dict[str, object]] = []
    now = timezone.now()
    for info in PERIODIC_TASKS:
        task_fn_name = info["task_fn_name"]
        wrapper = get_periodic_task_wrapper(task_fn_name)
        if wrapper is None:
            continue

        is_revoked = False
        try:
            is_revoked = bool(wrapper.is_revoked())
        except Exception:
            is_revoked = False

        next_run_at = None
        try:
            next_run_at = get_next_run_at(info["cron_string"], from_time=now)
        except Exception:
            next_run_at = None

        periodic_rows.append(
            {
                "task_fn_name": task_fn_name,
                "display_name": info["display_name"],
                "cron_string": info["cron_string"],
                "schedule_description": cron_to_short_description(info["cron_string"]),
                "basic": info.get("basic") or info.get("description") or "",
                "advanced": info.get("advanced") or "",
                "is_revoked": is_revoked,
                "next_run_at": next_run_at,
            }
        )

    twenty_four_hours_ago = now - timedelta(hours=24)
    searches_24h = JobSearchTaskRun.objects.filter(started_at__gte=twenty_four_hours_ago).count()
    failed_24h = JobSearchTaskRun.objects.filter(started_at__gte=twenty_four_hours_ago, status="failed").count()
    active_running = JobSearchTaskRun.objects.filter(status="running").count()

    cleanup_status = cache.get(CLEANUP_STATUS_CACHE_KEY)

    adhoc_rows: list[dict[str, str]] = []
    for info in ADHOC_RUN_NOW_TASKS:
        adhoc_rows.append(
            {
                "task_fn_name": info["task_fn_name"],
                "display_name": info["display_name"],
                "basic": info.get("basic") or "",
                "advanced": info.get("advanced") or "",
            }
        )

    # Build recent activity items from search scrapes
    recent_searches = list(
        JobSearchTaskRun.objects.select_related("task", "task__owner")
        .order_by("-started_at")[:20]
    )

    unified_activity = []
    for s in recent_searches:
        user_name = s.task.owner.get_username() if s.task and s.task.owner else "System"
        title = s.task.name if s.task else "Job Search"
        subtitle = s.task.search_term if s.task else ""
        unified_activity.append({
            "type": "Search Scrape",
            "type_badge": "bg-sky-50 text-sky-700 border-sky-200",
            "title": title,
            "subtitle": subtitle,
            "user": user_name,
            "status": s.status,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "items_count": getattr(s, "jobs_fetched", 0),
            "error_message": s.error_message,
        })

    unified_activity.sort(key=lambda x: x["started_at"] or now, reverse=True)
    unified_activity = unified_activity[:25]

    context = {
        "immediate": immediate,
        "queue_stats": queue_stats,
        "queue_stats_error": queue_stats_error,
        "periodic_tasks": periodic_rows,
        "adhoc_run_tasks": adhoc_rows,
        "recent_runs": recent_searches[:10],
        "unified_activity": unified_activity,
        "searches_24h": searches_24h,
        "applies_24h": 0,
        "failed_24h": failed_24h,
        "active_running": active_running,
        "cleanup_status": cleanup_status,
    }
    return render(request, "resume_app/huey_dashboard.html", context)


@staff_required
def huey_periodic_revoke_view(request, task_name: str):
    """Pause a specific Huey periodic task via revoke()."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; periodic pause controls are disabled.")
        return redirect("huey_dashboard")

    info = get_periodic_task_info(task_name)
    if not info:
        messages.error(request, f"Unknown periodic task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Periodic task not found: {task_name}")
        return redirect("huey_dashboard")

    try:
        wrapper.revoke()
    except Exception as e:
        messages.error(request, f"Failed to pause task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, f'Paused: {info["display_name"]}')
    return redirect("huey_dashboard")


@staff_required
def huey_periodic_restore_view(request, task_name: str):
    """Restore a specific Huey periodic task via restore()."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; periodic restore controls are disabled.")
        return redirect("huey_dashboard")

    info = get_periodic_task_info(task_name)
    if not info:
        messages.error(request, f"Unknown periodic task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Periodic task not found: {task_name}")
        return redirect("huey_dashboard")

    try:
        wrapper.restore()
    except Exception as e:
        messages.error(request, f"Failed to restore task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, f'Restored: {info["display_name"]}')
    return redirect("huey_dashboard")


@staff_required
def huey_flush_queue_view(request):
    """Flush pending Huey queue (destructive)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from huey.contrib.djhuey import HUEY

    immediate = bool(getattr(HUEY, "immediate", False))
    if immediate:
        messages.error(request, "Huey is in immediate mode; there is no Redis queue to flush.")
        return redirect("huey_dashboard")

    confirm = (request.POST.get("confirm") or "").strip().lower()
    if confirm != "yes":
        messages.error(request, "Queue flush cancelled (missing confirmation).")
        return redirect("huey_dashboard")

    try:
        storage = HUEY.storage
        storage.flush_queue()
    except Exception as e:
        messages.error(request, f"Failed to flush queue: {e}")
        return redirect("huey_dashboard")

    messages.success(request, "Huey queue flushed (pending tasks removed).")
    return redirect("huey_dashboard")


@staff_required
def huey_run_cleanup_now_view(request):
    """Enqueue Cleanup Manager (dedupe + retention + inactive check) immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        from .tasks import cleanup_manager

        cleanup_manager()
    except Exception as e:
        messages.error(request, f"Failed to queue cleanup task: {e}")
        return redirect("huey_dashboard")

    messages.success(request, "Cleanup Manager queued (dedupe, retention purge, inactive Applying check).")
    return redirect("huey_dashboard")


@staff_required
def huey_task_run_now_view(request, task_name: str):
    """Enqueue a periodic Huey task or an ad-hoc @db_task that needs no arguments."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    if task_name not in run_now_task_names():
        messages.error(request, f"Unknown or unsupported on-demand task: {task_name}")
        return redirect("huey_dashboard")

    wrapper = get_periodic_task_wrapper(task_name)
    if wrapper is None:
        messages.error(request, f"Task not found in resume_app.tasks: {task_name}")
        return redirect("huey_dashboard")

    label = get_run_now_display_name(task_name)
    try:
        wrapper()
    except Exception as e:
        messages.error(request, f'Failed to queue "{label}": {e}')
        return redirect("huey_dashboard")

    messages.success(request, f"Queued: {label}")
    return redirect("huey_dashboard")


def job_task_create_view(request):
    """Create a new job search task. Sets next_run_at from cron."""
    from .saved_searches import get_saved_search_or_none

    tracks_qs = Track.ensure_baseline(request.user)
    track_list = list(tracks_qs)
    default_track = Track.get_default_slug(request.user)
    preset = get_saved_search_or_none(request.user, request.GET.get("preset"))
    if request.method != "POST":
        if preset:
            context = {
                "task": None,
                "form_track": preset.profile_slug or default_track,
                "job_tracks": track_list,
                "form_frequency": "0 9 * * *",
                "form_jobs_to_fetch": preset.results_wanted or 50,
                "form_site_name": normalize_site_names(
                    preset.site_names if isinstance(preset.site_names, list) else None
                ),
                "form_start_time": "",
                "form_name": preset.name,
                "form_search_term": preset.search_term,
                "form_location": preset.location,
            }
        else:
            context = {
                "task": None,
                "form_track": default_track,
                "job_tracks": track_list,
                "form_frequency": "0 9 * * *",
                "form_jobs_to_fetch": 50,
                "form_site_name": list(DEFAULT_SITE_NAMES),
                "form_start_time": "",
                "form_name": "",
                "form_search_term": "",
                "form_location": "",
            }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, default_track, {t.slug for t in track_list})
    if errs:
        for e in errs:
            messages.error(request, e)
        return redirect("job_task_create")

    task = JobSearchTask(
        owner=request.user,
        name=data["name"],
        search_term=data["search_term"],
        location=data["location"],
        track=data["track"],
        jobs_to_fetch=data["jobs_to_fetch"],
        frequency=data["frequency"],
        start_time=data["start_time"],
        site_name=data["site_name"],
        is_active=True,
    )
    try:
        task.full_clean()
    except ValidationError as e:
        for _k, v in e.message_dict.items():
            for msg in (v if isinstance(v, list) else [v]):
                messages.error(request, msg)
        return redirect("job_task_create")
    task.next_run_at = get_next_run_at(task.frequency)
    task.save()
    messages.success(request, f"Task \"{task.name or task.search_term}\" created. Next run: {task.next_run_at}")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)


def job_task_edit_view(request, task_id):
    """Edit a job search task. Optionally update next_run_at."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    tracks_qs = Track.ensure_baseline(request.user)
    track_list = list(tracks_qs)
    if request.method != "POST":
        context = {
            "task": task,
            "form_track": task.track,
            "job_tracks": track_list,
            "form_frequency": task.frequency,
            "form_jobs_to_fetch": task.jobs_to_fetch,
            "form_site_name": normalize_site_names(
                task.site_name if isinstance(task.site_name, list) else None
            ),
            "form_start_time": task.start_time.strftime("%H:%M") if task.start_time else "",
        }
        return render(request, "resume_app/job_task_form.html", context)

    data, errs = _parse_task_form(request, Track.get_default_slug(request.user), {t.slug for t in track_list})
    if errs:
        for e in errs:
            messages.error(request, e)
        return redirect("job_task_edit", task_id=task_id)
    task.name = data["name"]
    task.search_term = data["search_term"]
    task.location = data["location"]
    task.track = data["track"]
    task.jobs_to_fetch = data["jobs_to_fetch"]
    task.frequency = data["frequency"]
    task.start_time = data["start_time"]
    task.site_name = data["site_name"]
    try:
        task.full_clean()
    except ValidationError as e:
        for _k, v in e.message_dict.items():
            for msg in (v if isinstance(v, list) else [v]):
                messages.error(request, msg)
        return redirect("job_task_edit", task_id=task_id)
    task.save()
    messages.success(request, "Task updated.")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)


def job_task_run_now_view(request, task_id):
    """Enqueue run_job_search_task once with 60-min cooldown and concurrency check."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    is_ajax = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or request.content_type == "application/json"
    )

    track_slug = task.track or (task.saved_search.slug if task.saved_search else "")
    cockpit_url = reverse("career_cockpit")
    if track_slug:
        cockpit_url += f"?track={track_slug}"
    redirect_url = request.POST.get("next") or request.GET.get("next") or cockpit_url

    # 1. Check if a search task is already actively running for this user
    lock_key = get_job_search_task_lock_key(request.user.id)
    is_running = bool(cache.get(lock_key)) or task.runs.filter(status=JobSearchTaskRun.STATUS_RUNNING).exists()
    if is_running:
        msg = "A search task is already actively running for your account. Please wait for it to complete."
        if is_ajax:
            return JsonResponse({"status": "running", "message": msg}, status=409)
        messages.warning(request, msg)
        return redirect(redirect_url)

    # 2. Check 3-minute manual cooldown (cleared on failed run so user can retry immediately)
    COOLDOWN_SECONDS = 180
    cooldown_key = f"job_task_manual_cooldown:{task.id}"
    last_run = task.runs.first()
    if last_run and last_run.status == JobSearchTaskRun.STATUS_FAILED and not is_running:
        cache.delete(cooldown_key)
        expires_at = None
    else:
        expires_at = cache.get(cooldown_key)

    now = time.time()
    if expires_at and expires_at > now:
        remaining = int(expires_at - now)
        mins = max(1, (remaining + 59) // 60)
        msg = f"Task is on cooldown. Please wait {mins} minute{'s' if mins != 1 else ''} before running again."
        if is_ajax:
            return JsonResponse({
                "status": "cooldown",
                "message": msg,
                "cooldown_remaining_seconds": remaining,
            }, status=429)
        messages.warning(request, msg)
        return redirect(redirect_url)

    # 3. Set cooldown, mark pending, and enqueue task
    cache.set(cooldown_key, now + COOLDOWN_SECONDS, timeout=COOLDOWN_SECONDS)
    cache.set(f"job_task_pending:{task.id}", now, timeout=180)
    from . import views
    views.run_job_search_task(task.owner_id, task.id)

    task_label = task.name or (task.saved_search.name if task.saved_search else None) or task.search_term
    msg = f'Search "{task_label}" queued to run now.'
    if is_ajax:
        return JsonResponse({
            "status": "queued",
            "message": msg,
            "task_id": task.id,
            "cooldown_remaining_seconds": COOLDOWN_SECONDS,
        })

    messages.success(request, msg)
    return redirect(redirect_url)


def job_task_status_view(request, task_id):
    """Return JSON status of a JobSearchTask (running state, last run, cooldown)."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)

    is_pending = bool(cache.get(f"job_task_pending:{task.id}"))
    lock_key = get_job_search_task_lock_key(request.user.id)
    is_running = is_pending or bool(cache.get(lock_key)) or task.runs.filter(status=JobSearchTaskRun.STATUS_RUNNING).exists()

    cooldown_key = f"job_task_manual_cooldown:{task.id}"
    last_run = task.runs.first()
    if last_run and last_run.status == JobSearchTaskRun.STATUS_FAILED and not is_running:
        cache.delete(cooldown_key)
        cooldown_remaining = 0
    else:
        expires_at = cache.get(cooldown_key)
        now = time.time()
        cooldown_remaining = max(0, int(expires_at - now)) if (expires_at and expires_at > now) else 0

    last_run = task.runs.first()
    last_run_data = None
    if last_run:
        last_run_data = {
            "id": last_run.id,
            "status": last_run.status,
            "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
            "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
            "jobs_fetched": last_run.jobs_fetched,
            "jobs_added": last_run.jobs_added_to_pipeline,
            "jobs_eliminated": last_run.jobs_eliminated,
            "has_details": bool(last_run.details),
            "date_display": last_run.started_at.strftime("%b %d, %Y · %I:%M %p") if last_run.started_at else "",
            "timesince": timesince(last_run.started_at) if last_run.started_at else "",
            "json_url": reverse("scheduled_run_details_json", args=[last_run.id]),
            "csv_url": reverse("download_scheduled_run_csv", args=[last_run.id]),
        }

    return JsonResponse({
        "status": "ok",
        "task_id": task.id,
        "is_running": is_running,
        "cooldown_remaining_seconds": cooldown_remaining,
        "last_run": last_run_data,
    })


def job_task_toggle_active_view(request, task_id):
    """Toggle is_active and redirect to Cockpit."""
    task = get_owned_or_404(JobSearchTask, request.user, id=task_id)
    task.is_active = not task.is_active
    task.save()
    status = "activated" if task.is_active else "paused"
    messages.success(request, f"Task \"{task.name or task.search_term}\" {status}.")
    cockpit_url = reverse("career_cockpit")
    if task.track:
        cockpit_url += f"?track={task.track}"
    return redirect(cockpit_url)
