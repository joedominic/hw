"""Getting Started onboarding page (legacy URL → Career Profiler)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods

from .experience import get_post_login_url, is_power_user, onboarding_progress


@login_required
@require_http_methods(["GET", "POST"])
def getting_started_view(request):
    """
    Legacy Getting Started checklist.

    First-run setup now lives on the Career Profiler. Keep this URL so bookmarks
    and old emails still land somewhere useful.
    """
    if is_power_user(request.user):
        return redirect("career_cockpit")

    progress = onboarding_progress(request.user)
    if progress["steps_complete"] or progress["completed"] or progress["dismissed"]:
        return redirect(get_post_login_url(request.user))

    return redirect("career_profiler")
