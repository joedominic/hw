"""
Apply Agent orchestrator: a DB-backed, stateless state machine.

Each call to :func:`advance_attempt` performs exactly one step for one
:class:`ApplicationAttempt` and persists the result, so a fresh Huey worker can
resume after a restart. The browser is never kept alive between steps; the
dry-run captures a semantic answer key and the submit step re-validates against
a freshly loaded form.

Browser-touching steps are bounded by a hard deadline and gated by a small
concurrency semaphore so they cannot exhaust the browser worker pool.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import (
    AppAutomationSettings,
    ApplicantProfile,
    ApplicationAttempt,
    ApplicationAttemptStep,
    AtsAutoSubmitStats,
    OptimizedResume,
    PipelineEntry,
)
from . import resolve_and_detect as resolver
from .adapters import HANDOFF_ONLY_ATS, get_adapter
from .ats_detect import ATS_UNKNOWN, detect_ats_from_url
from .base import ApplyContext

logger = logging.getLogger("huey")

# Minimum adapter confidence required before an attempt may auto-submit.
AUTO_SUBMIT_MIN_CONFIDENCE = 0.8

_BROWSER_SEMAPHORE_KEY = "apply_agent_browser_slots"

# Indirection so tests can patch the browser session factory with a fake page.
def _default_browser_session(**kwargs):
    from .browser import browser_session

    return browser_session(**kwargs)


browser_session_factory = _default_browser_session


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def start_attempts_for_entries(entry_ids: list[int], *, mode: str | None = None) -> list[ApplicationAttempt]:
    """Create a queued ApplicationAttempt for each Applying-stage entry.

    Skips entries that already have a non-terminal attempt (no duplicate runs).
    """
    settings_solo = AppAutomationSettings.get_solo()
    effective_mode = mode or settings_solo.apply_automation_mode or ApplicationAttempt.Mode.SEMI_AUTO
    created: list[ApplicationAttempt] = []
    for eid in entry_ids:
        try:
            entry = PipelineEntry.objects.select_related("job_listing").get(
                id=int(eid), removed_at__isnull=True
            )
        except (PipelineEntry.DoesNotExist, TypeError, ValueError):
            continue
        if entry.stage != PipelineEntry.Stage.APPLYING:
            continue
        has_active = ApplicationAttempt.objects.filter(
            pipeline_entry=entry,
            status__in=ApplicationAttempt.ACTIVE_STATUSES + (ApplicationAttempt.Status.AWAITING_APPROVAL,),
        ).exists()
        if has_active:
            continue
        attempt = ApplicationAttempt.objects.create(
            pipeline_entry=entry,
            automation_mode=effective_mode,
            status=ApplicationAttempt.Status.QUEUED,
        )
        created.append(attempt)
    return created


def approve_attempt(attempt_id: int, *, corrected: bool = False) -> ApplicationAttempt | None:
    """Approve a dry-run.

    Known ATS adapters proceed to SUBMITTING for automated submit. Unknown/generic
    ATS (no deterministic adapter) completes as a manual handoff — the user
    submits on the company site themselves.
    """
    attempt = _get(attempt_id)
    if not attempt or attempt.status != ApplicationAttempt.Status.AWAITING_APPROVAL:
        return attempt
    if corrected and attempt.ats_type:
        _record_correction(attempt.ats_type)

    if get_adapter(attempt.ats_type) is None:
        _log_step(
            attempt,
            "manual_complete",
            message="User confirmed manual apply (no auto-submit adapter for this ATS).",
        )
        attempt.status = ApplicationAttempt.Status.SUCCEEDED
        attempt.submitted_at = timezone.now()
        attempt.error_code = ""
        attempt.error_message = ""
        attempt.save(
            update_fields=["status", "submitted_at", "error_code", "error_message", "updated_at"]
        )
        attempt.pipeline_entry.mark_done()
        return attempt

    attempt.status = ApplicationAttempt.Status.SUBMITTING
    attempt.save(update_fields=["status", "updated_at"])
    return attempt


def reject_attempt(attempt_id: int) -> ApplicationAttempt | None:
    attempt = _get(attempt_id)
    if not attempt or attempt.is_terminal:
        return attempt
    attempt.mark_failed(ApplicationAttempt.ERROR_REJECTED, "Rejected by user.")
    return attempt


def set_override_url(attempt_id: int, url: str) -> ApplicationAttempt | None:
    """Manually set the apply URL (e.g. when resolution failed) and re-detect ATS."""
    attempt = _get(attempt_id)
    if not attempt:
        return attempt
    url = (url or "").strip()
    if not url:
        return attempt
    attempt.apply_url = url
    attempt.ats_type = detect_ats_from_url(url)
    attempt.error_code = ""
    attempt.error_message = ""
    attempt.status = ApplicationAttempt.Status.DRY_RUN_FILL
    attempt.save(update_fields=["apply_url", "ats_type", "error_code", "error_message", "status", "updated_at"])
    return attempt


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def advance_attempt(attempt_id: int) -> dict:
    """Perform one state-machine step for the given attempt."""
    attempt = _get(attempt_id)
    if not attempt:
        return {"status": "error", "message": "attempt not found"}
    if attempt.is_terminal or attempt.status == ApplicationAttempt.Status.AWAITING_APPROVAL:
        return {"status": "noop", "state": attempt.status}

    attempt.last_heartbeat_at = timezone.now()
    if attempt.started_at is None:
        attempt.started_at = timezone.now()
    attempt.save(update_fields=["last_heartbeat_at", "started_at", "updated_at"])

    handler = _HANDLERS.get(attempt.status)
    if handler is None:
        return {"status": "noop", "state": attempt.status}
    try:
        handler(attempt)
    except Exception as e:  # noqa: BLE001 - never let a step crash the worker
        logger.exception("[apply_agent] step failed attempt=%s state=%s", attempt_id, attempt.status)
        attempt.mark_failed(ApplicationAttempt.ERROR_FILL_FAILED, str(e))
    return {"status": "ok", "state": attempt.status}


def _handle_queued(attempt: ApplicationAttempt) -> None:
    _set_status(attempt, ApplicationAttempt.Status.OPTIMIZING)


def _handle_optimizing(attempt: ApplicationAttempt) -> None:
    completed = _latest_completed_optimization(attempt)
    if completed:
        attempt.optimized_resume = completed
        attempt.save(update_fields=["optimized_resume", "updated_at"])
        _enter_resolution(attempt)
        return
    if _has_active_optimization(attempt):
        _set_status(attempt, ApplicationAttempt.Status.WAITING_OPTIMIZER)
        return
    _enqueue_optimization(attempt)
    _set_status(attempt, ApplicationAttempt.Status.WAITING_OPTIMIZER)


def _handle_waiting_optimizer(attempt: ApplicationAttempt) -> None:
    completed = _latest_completed_optimization(attempt)
    if completed:
        attempt.optimized_resume = completed
        attempt.save(update_fields=["optimized_resume", "updated_at"])
        _enter_resolution(attempt)
        return
    if not _has_active_optimization(attempt):
        # Nothing running and nothing completed: the optimizer run failed.
        attempt.mark_failed(
            ApplicationAttempt.ERROR_OPTIMIZER_FAILED,
            "Resume optimization did not complete.",
        )


def _handle_resolve_and_detect(attempt: ApplicationAttempt) -> None:
    job_url = (attempt.pipeline_entry.job_listing.url or "").strip()
    if attempt.apply_url:
        # Override URL already set; ats_type detected in set_override_url.
        ats = attempt.ats_type or detect_ats_from_url(attempt.apply_url)
        _log_step(attempt, "resolve", message=f"Using override apply URL ({attempt.ats_type or ats})")
    else:
        mode = "mock map" if resolver.use_mock_resolver() else "live browser"
        _log_step(attempt, "resolve", message=f"Resolving apply URL from job listing ({mode}): {job_url}")
        needs_browser = not resolver.use_mock_resolver()
        if needs_browser and not _acquire_browser_slot():
            _log_step(attempt, "resolve", message="Waiting for a free browser slot…")
            return  # heartbeat will retry when a slot frees up
        try:
            result = _run_browser_step(
                lambda: resolver.resolve_and_detect(job_url),
                attempt,
                default_error=ApplicationAttempt.ERROR_UNRESOLVED_URL,
            )
        finally:
            if needs_browser:
                _release_browser_slot()
        if result is None:
            return  # timeout/crash; already marked failed
        if not result.ok:
            _log_step(attempt, "resolve", message=result.message or "Could not resolve apply URL.")
            attempt.mark_failed(result.error_code or ApplicationAttempt.ERROR_UNRESOLVED_URL, result.message)
            return
        attempt.apply_url = result.apply_url
        ats = result.ats_type
        _log_step(
            attempt,
            "resolve",
            message=f"Resolved to {result.apply_url} (ATS: {result.ats_type or ATS_UNKNOWN})",
        )
    attempt.ats_type = ats or ATS_UNKNOWN
    attempt.save(update_fields=["apply_url", "ats_type", "updated_at"])

    if attempt.ats_type in HANDOFF_ONLY_ATS:
        _log_step(attempt, "handoff", message=f"{attempt.ats_type} requires manual apply (assistive handoff).")
        _set_status(attempt, ApplicationAttempt.Status.AWAITING_APPROVAL)
        return
    _set_status(attempt, ApplicationAttempt.Status.DRY_RUN_FILL)


def _handle_dry_run_fill(attempt: ApplicationAttempt) -> None:
    adapter = get_adapter(attempt.ats_type)
    use_generic = adapter is None

    def _do_fill():
        if use_generic:
            from . import generic_agent

            # browser-use manages its own Chromium; skip Playwright to avoid double browsers.
            ctx = _build_context(attempt, page=None)
            return generic_agent.run_generic_fill(ctx), True
        kwargs = _browser_kwargs(attempt)
        with browser_session_factory(**kwargs) as page:
            ctx = _build_context(attempt, page)
            return adapter.fill_application(ctx, stop_before_submit=True), False

    if not _acquire_browser_slot():
        return  # heartbeat will retry when a slot frees up

    try:
        outcome = _run_browser_step(_do_fill, attempt, default_error=ApplicationAttempt.ERROR_FILL_FAILED)
    finally:
        _release_browser_slot()

    if outcome is None:
        return  # failed (timeout/crash); already marked
    fill_result, _is_generic = outcome

    attempt.fill_payload_json = fill_result.payload or {}
    attempt.confidence = fill_result.confidence
    attempt.save(update_fields=["fill_payload_json", "confidence", "updated_at"])

    if not fill_result.ok and fill_result.error_code == "no_adapter":
        attempt.mark_failed(ApplicationAttempt.ERROR_NO_ADAPTER, fill_result.message)
        return

    # Decide auto-submit vs human review.
    if _should_auto_submit(attempt, is_generic=use_generic, fill_ok=fill_result.ok):
        _set_status(attempt, ApplicationAttempt.Status.SUBMITTING)
    else:
        _set_status(attempt, ApplicationAttempt.Status.AWAITING_APPROVAL)


def _handle_submitting(attempt: ApplicationAttempt) -> None:
    adapter = get_adapter(attempt.ats_type)
    if adapter is None:
        # Generic path never auto-submits; submission requires a deterministic adapter.
        attempt.mark_failed(
            ApplicationAttempt.ERROR_NO_ADAPTER,
            "No deterministic adapter available to submit this application.",
        )
        return

    def _do_submit():
        kwargs = _browser_kwargs(attempt)
        with browser_session_factory(**kwargs) as page:
            ctx = _build_context(attempt, page)
            # Re-validation pass on a fresh form, then submit + verify atomically.
            fill = adapter.fill_from_payload(ctx, attempt.fill_payload_json or {})
            if not fill.ok:
                return ("fill_failed", fill)
            submit = adapter.submit_and_verify(ctx)
            return ("submit", submit)

    if not _acquire_browser_slot():
        return

    try:
        # A crash/timeout mid-submit is ambiguous (the POST may have fired): never auto-retry.
        outcome = _run_browser_step(_do_submit, attempt, default_error=ApplicationAttempt.ERROR_SUBMIT_AMBIGUOUS)
    finally:
        _release_browser_slot()

    if outcome is None:
        return  # mid-submit crash/timeout -> already marked submit_ambiguous

    kind, result = outcome
    if kind == "fill_failed":
        attempt.mark_failed(ApplicationAttempt.ERROR_FILL_FAILED, result.message)
        return

    _log_step(attempt, "submit", message=result.message, network_log=result.network_log)
    if result.ok and result.confirmed:
        attempt.status = ApplicationAttempt.Status.SUCCEEDED
        attempt.submitted_at = timezone.now()
        attempt.save(update_fields=["status", "submitted_at", "updated_at"])
        attempt.pipeline_entry.mark_done()
        _record_clean_submit(attempt.ats_type)
    else:
        # Ambiguous: never auto-retry (double-apply risk). Human verifies manually.
        attempt.mark_failed(
            result.error_code or ApplicationAttempt.ERROR_SUBMIT_AMBIGUOUS,
            result.message or "Submission could not be confirmed.",
        )


_HANDLERS = {
    ApplicationAttempt.Status.QUEUED: _handle_queued,
    ApplicationAttempt.Status.OPTIMIZING: _handle_optimizing,
    ApplicationAttempt.Status.WAITING_OPTIMIZER: _handle_waiting_optimizer,
    ApplicationAttempt.Status.RESOLVE_AND_DETECT: _handle_resolve_and_detect,
    ApplicationAttempt.Status.DRY_RUN_FILL: _handle_dry_run_fill,
    ApplicationAttempt.Status.SUBMITTING: _handle_submitting,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get(attempt_id: int) -> ApplicationAttempt | None:
    return (
        ApplicationAttempt.objects.select_related("pipeline_entry", "pipeline_entry__job_listing", "optimized_resume")
        .filter(id=attempt_id)
        .first()
    )


def _set_status(attempt: ApplicationAttempt, status: str) -> None:
    attempt.status = status
    attempt.save(update_fields=["status", "updated_at"])


def _enter_resolution(attempt: ApplicationAttempt) -> None:
    """Apply the optional min-score gate, export the resume, then resolve the URL."""
    optimized = attempt.optimized_resume
    solo = AppAutomationSettings.get_solo()
    gate = int(solo.apply_min_optimizer_score or 0)
    if gate > 0 and optimized is not None:
        scores = [s for s in (optimized.ats_score, optimized.recruiter_score) if s is not None]
        avg = sum(scores) / len(scores) if scores else 0
        if avg < gate:
            attempt.mark_failed(
                ApplicationAttempt.ERROR_OPTIMIZER_FAILED,
                f"Optimized resume score {avg:.0f} is below the minimum {gate}.",
            )
            return
    path = _export_resume_file(attempt, solo.apply_resume_upload_format)
    if not path:
        attempt.mark_failed(ApplicationAttempt.ERROR_NO_RESUME, "Could not export the optimized resume for upload.")
        return
    attempt.resume_file_path = path
    attempt.save(update_fields=["resume_file_path", "updated_at"])
    _set_status(attempt, ApplicationAttempt.Status.RESOLVE_AND_DETECT)


def _export_resume_file(attempt: ApplicationAttempt, fmt: str) -> str:
    optimized = attempt.optimized_resume
    if optimized is None or not (optimized.optimized_content or "").strip():
        return ""
    from ..api import _build_export_docx, _build_export_pdf

    fmt = (fmt or "pdf").lower()
    if fmt == "docx":
        buf = _build_export_docx(optimized.optimized_content)
        ext = "docx"
    else:
        buf = _build_export_pdf(optimized.optimized_content)
        ext = "pdf"
    if buf is None:
        return ""

    media_root = getattr(settings, "MEDIA_ROOT", "") or os.path.join(os.getcwd(), "media")
    out_dir = os.path.join(media_root, "apply_agent")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"attempt_{attempt.id}_resume.{ext}")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


def _latest_completed_optimization(attempt: ApplicationAttempt) -> OptimizedResume | None:
    return (
        OptimizedResume.objects.filter(
            pipeline_entry=attempt.pipeline_entry,
            status=OptimizedResume.STATUS_COMPLETED,
        )
        .exclude(optimized_content="")
        .order_by("-created_at")
        .first()
    )


def _has_active_optimization(attempt: ApplicationAttempt) -> bool:
    return OptimizedResume.objects.filter(
        pipeline_entry=attempt.pipeline_entry,
        status__in=(OptimizedResume.STATUS_QUEUED, OptimizedResume.STATUS_RUNNING),
    ).exists()


def _enqueue_optimization(attempt: ApplicationAttempt) -> None:
    from ..tasks import _enqueue_single_pipeline_resume_optimization

    result = _enqueue_single_pipeline_resume_optimization(attempt.pipeline_entry_id, force_new=False)
    _log_step(attempt, "optimize", message=str(result.get("message") or result.get("status")))
    if result.get("status") == "error":
        attempt.mark_failed(ApplicationAttempt.ERROR_OPTIMIZER_FAILED, str(result.get("message")))


def _resolve_cover_letter(attempt: ApplicationAttempt, profile: ApplicantProfile) -> str:
    """Prefer job-specific cover letter from optimization over global template."""
    opt = attempt.optimized_resume or _latest_completed_optimization(attempt)
    if opt and (opt.cover_letter or "").strip():
        return opt.cover_letter
    return profile.cover_letter_template


def _build_context(attempt: ApplicationAttempt, page) -> ApplyContext:
    profile = ApplicantProfile.get_solo()
    job = attempt.pipeline_entry.job_listing
    credential = _resolve_credential(attempt.apply_url)
    cookies = credential.get_session_cookies() if credential else None
    if cookies and page is not None:
        try:
            page.context.add_cookies(cookies)
        except Exception:
            pass
    ctx = ApplyContext(
        full_name=profile.full_name,
        email=profile.email,
        phone=profile.phone,
        location=profile.location,
        linkedin_url=profile.linkedin_url,
        website_url=profile.website_url,
        work_authorization=profile.work_authorization,
        requires_sponsorship=profile.requires_sponsorship,
        salary_expectation=profile.salary_expectation,
        cover_letter=_resolve_cover_letter(attempt, profile),
        custom_qa=profile.custom_qa_pairs or {},
        include_eeo=profile.include_eeo,
        apply_url=attempt.apply_url,
        resume_file_path=attempt.resume_file_path,
        company_name=job.company_name,
        job_title=job.title,
        page=page,
        credential=credential,
        attempt_id=attempt.id,
    )
    ctx._log_step = lambda step_name, **kw: _log_step(attempt, step_name, **kw)
    return ctx


def _resolve_credential(url: str):
    from urllib.parse import urlparse

    from ..models import SiteCredential

    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    return SiteCredential.objects.filter(domain=host).first()


def _should_auto_submit(attempt: ApplicationAttempt, *, is_generic: bool, fill_ok: bool) -> bool:
    if attempt.automation_mode != ApplicationAttempt.Mode.FULL_AUTO:
        return False
    if is_generic or not fill_ok:
        return False  # generic path never auto-submits; incomplete fills need review
    if (attempt.confidence or 0) < AUTO_SUBMIT_MIN_CONFIDENCE:
        return False
    stats = AtsAutoSubmitStats.objects.filter(ats_type=attempt.ats_type).first()
    return bool(stats and stats.full_auto_enabled)


def _log_step(attempt: ApplicationAttempt, step_name: str, *, message: str = "", action_snapshot=None, network_log=None, screenshot_path: str = "") -> None:
    ApplicationAttemptStep.objects.create(
        attempt=attempt,
        step_name=step_name[:64],
        message=message or "",
        action_snapshot=action_snapshot,
        network_log=network_log or [],
        screenshot_path=screenshot_path or "",
    )


# ----- full-auto graduation -------------------------------------------------
def _record_clean_submit(ats_type: str) -> None:
    if not ats_type:
        return
    solo = AppAutomationSettings.get_solo()
    threshold = int(solo.apply_full_auto_min_clean_submits or 10)
    stats, _ = AtsAutoSubmitStats.objects.get_or_create(ats_type=ats_type)
    stats.clean_submit_streak += 1
    stats.total_submits += 1
    if stats.clean_submit_streak >= threshold:
        stats.full_auto_enabled = True
    stats.save()


def _record_correction(ats_type: str) -> None:
    stats, _ = AtsAutoSubmitStats.objects.get_or_create(ats_type=ats_type)
    stats.clean_submit_streak = 0
    stats.total_corrections += 1
    stats.full_auto_enabled = False
    stats.save()


# ----- concurrency + timeout ------------------------------------------------
def _browser_concurrency() -> int:
    return max(1, int(getattr(settings, "APPLY_BROWSER_CONCURRENCY", 2)))


def _browser_kwargs(attempt: ApplicationAttempt) -> dict:
    from .browser import apply_browser_headless

    return {"headless": apply_browser_headless()}


def _browser_step_timeout_seconds() -> int:
    return max(30, int(getattr(settings, "APPLY_BROWSER_STEP_TIMEOUT_SECONDS", 360)))


def _acquire_browser_slot() -> bool:
    limit = _browser_concurrency()
    try:
        current = cache.incr(_BROWSER_SEMAPHORE_KEY)
    except ValueError:
        cache.add(_BROWSER_SEMAPHORE_KEY, 0)
        current = cache.incr(_BROWSER_SEMAPHORE_KEY)
    if current > limit:
        _release_browser_slot()
        return False
    return True


def _release_browser_slot() -> None:
    try:
        if cache.decr(_BROWSER_SEMAPHORE_KEY) < 0:
            cache.set(_BROWSER_SEMAPHORE_KEY, 0)
    except ValueError:
        cache.set(_BROWSER_SEMAPHORE_KEY, 0)


def _run_browser_step(fn, attempt: ApplicationAttempt, *, default_error: str):
    """Run a browser step with a wall-clock deadline, mapping failures to terminal state.

    Huey thread workers cannot be force-killed, so we run ``fn`` in a helper thread
    and enforce ``APPLY_BROWSER_STEP_TIMEOUT_SECONDS``. On timeout or exception we
    sweep orphaned Chromium processes so hung browsers do not leak memory.
    """
    from .browser import kill_orphan_chromium

    timeout = _browser_step_timeout_seconds()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        kill_orphan_chromium()
        message = f"Browser step exceeded {timeout}s wall-clock deadline."
        logger.warning("[apply_agent] browser step timed out attempt=%s: %s", attempt.id, message)
        attempt.mark_failed(ApplicationAttempt.ERROR_AUTOMATION_TIMEOUT, message)
        return None
    except Exception as e:  # noqa: BLE001
        kill_orphan_chromium()
        message = str(e)
        code = ApplicationAttempt.ERROR_AUTOMATION_TIMEOUT if "timeout" in message.lower() else default_error
        logger.warning("[apply_agent] browser step failed attempt=%s code=%s: %s", attempt.id, code, message)
        attempt.mark_failed(code, message)
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
