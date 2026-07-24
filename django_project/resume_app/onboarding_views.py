"""Getting Started onboarding page."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .experience import (
    complete_onboarding,
    dismiss_onboarding,
    get_post_login_url,
    is_power_user,
    mark_onboarding_step,
    onboarding_progress,
    sync_onboarding_progress,
)
from .models import Track, UserResume

MAX_LIBRARY_RESUME_BYTES = 10 * 1024 * 1024


def _upload_library_resume(user, resume_file, track_slug: str = "") -> tuple[bool, str]:
    if not resume_file:
        return False, "Please select a PDF resume to upload."

    track_slugs = set(Track.ensure_baseline(user).values_list("slug", flat=True))
    track_slug = (track_slug or Track.get_default_slug(user)).strip().lower()
    if track_slug and track_slug not in track_slugs:
        return False, "Invalid track selection."

    original_name = (getattr(resume_file, "name", "") or "resume.pdf").strip()
    original_name = original_name.split("\\")[-1].split("/")[-1].strip()
    if not original_name.lower().endswith(".pdf"):
        return False, "Resume file must be a PDF."
    file_size = getattr(resume_file, "size", None)
    if file_size is not None and file_size > MAX_LIBRARY_RESUME_BYTES:
        return False, "Resume file must be 10MB or smaller."
    original_name = (original_name or "resume.pdf")[:255]

    from .entitlements import QuotaExceeded
    from .storage_quota import assert_upload_allowed

    try:
        assert_upload_allowed(user, resume_file)
    except QuotaExceeded as exc:
        return False, str(exc)

    UserResume.objects.create(
        owner=user,
        file=resume_file,
        original_filename=original_name,
        track=track_slug or "",
        is_library=True,
    )
    return True, "Resume uploaded."


@login_required
@require_http_methods(["GET", "POST"])
def getting_started_view(request):
    if is_power_user(request.user):
        return redirect("pipeline")

    progress = onboarding_progress(request.user)
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "dismiss":
            dismiss_onboarding(request.user)
            messages.info(request, "You can finish setup anytime from the banner on My jobs.")
            return redirect("pipeline")
        if action == "upload_resume":
            ok, msg = _upload_library_resume(
                request.user,
                request.FILES.get("resume_file"),
            )
            if ok:
                mark_onboarding_step(request.user, "resume")
                messages.success(request, msg)
            else:
                messages.error(request, msg)
            sync_onboarding_progress(request.user)
            if onboarding_progress(request.user)["steps_complete"]:
                complete_onboarding(request.user)
                messages.success(request, "Setup complete — welcome to your job pipeline.")
                return redirect("pipeline")
            return redirect("getting_started")
        if action == "finish":
            sync_onboarding_progress(request.user)
            if onboarding_progress(request.user)["steps_complete"]:
                complete_onboarding(request.user)
                return redirect("pipeline")
            messages.info(request, "Complete the remaining steps to finish setup.")
            return redirect("getting_started")

    progress = onboarding_progress(request.user)
    if progress["steps_complete"] and not progress["completed"]:
        complete_onboarding(request.user)
        return redirect(get_post_login_url(request.user))

    if progress["completed"] or progress["dismissed"]:
        return redirect("pipeline")

    default_track = Track.get_default_slug(request.user)
    return render(
        request,
        "resume_app/getting_started.html",
        {
            "progress": progress,
            "default_track": default_track,
            "settings_llm_url": reverse("settings") + "?tab=llm&from=onboarding",
            "jobs_search_url": reverse("jobs_search") + "?from=onboarding",
            "pipeline_url": reverse("pipeline"),
        },
    )
