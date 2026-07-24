"""User experience mode, onboarding progress, and post-login routing."""
from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import LLMProviderConfig, PipelineEntry, UserExperienceSettings, UserResume

SERVER_LLM_KEY_ATTRS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "GOOGLE_API_KEY",
)


def default_experience_mode() -> str:
    raw = (getattr(settings, "DEFAULT_EXPERIENCE_MODE", "normal") or "normal").strip().lower()
    if raw == UserExperienceSettings.ExperienceMode.POWER:
        return UserExperienceSettings.ExperienceMode.POWER
    return UserExperienceSettings.ExperienceMode.NORMAL


def server_has_active_llm_keys() -> bool:
    """True when the deployment provides platform-level LLM API keys."""
    for attr in SERVER_LLM_KEY_ATTRS:
        if (getattr(settings, attr, None) or "").strip():
            return True
    return False


def user_has_stored_llm_key(user) -> bool:
    return LLMProviderConfig.objects.for_user(user).exclude(encrypted_api_key="").exists()


def llm_ready_for_user(user) -> bool:
    """User can run LLM features (server keys or personal stored key)."""
    if server_has_active_llm_keys():
        return True
    return user_has_stored_llm_key(user)


def is_power_user(user) -> bool:
    """
    Advanced (power) UI is staff-only.

    Normal users always get the Simple experience. Staff may enable Advanced via
    Settings or the set_experience_mode management command.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_staff", False):
        return False
    exp = UserExperienceSettings.get_for_user(user)
    return exp.experience_mode == UserExperienceSettings.ExperienceMode.POWER


def has_my_jobs_search_profile(user) -> bool:
    """
    Normal-mode users need a named saved search (creates a search profile) before
    My Jobs is usable. Power users and anyone with a non-default profile may enter.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_power_user(user):
        return True
    from .models import SearchProfile, Track

    if SearchProfile.objects.for_user(user).exists():
        return True
    default_slug = Track.get_default_slug(user)
    return Track.objects.for_user(user).exclude(slug=default_slug).exists()


def has_library_resume(user) -> bool:
    return UserResume.objects.for_user(user).filter(is_library=True).exists()


def has_search_activity(user) -> bool:
    return PipelineEntry.objects.for_user(user).exists()


def onboarding_steps_complete(user, exp: UserExperienceSettings | None = None) -> bool:
    """Required onboarding: resume + first search. LLM is platform default or optional BYOK."""
    exp = exp or UserExperienceSettings.get_for_user(user)
    resume_ok = exp.step_resume_uploaded or has_library_resume(user)
    search_ok = exp.step_first_search or has_search_activity(user)
    return resume_ok and search_ok


def sync_onboarding_progress(user) -> UserExperienceSettings:
    """
    Refresh step flags from durable DB state and mark onboarding complete when ready.
    """
    exp = UserExperienceSettings.get_for_user(user)
    if is_power_user(user):
        return exp

    update_fields: list[str] = []
    if llm_ready_for_user(user) and not exp.step_llm_connected:
        exp.step_llm_connected = True
        update_fields.append("step_llm_connected")
    if has_library_resume(user) and not exp.step_resume_uploaded:
        exp.step_resume_uploaded = True
        update_fields.append("step_resume_uploaded")
    if has_search_activity(user) and not exp.step_first_search:
        exp.step_first_search = True
        update_fields.append("step_first_search")

    if onboarding_steps_complete(user, exp) and not exp.onboarding_completed_at:
        exp.onboarding_completed_at = timezone.now()
        update_fields.append("onboarding_completed_at")

    if update_fields:
        exp.save(update_fields=update_fields)
    return exp


def needs_onboarding(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_power_user(user):
        return False
    exp = sync_onboarding_progress(user)
    if exp.onboarding_completed_at or exp.onboarding_dismissed_at:
        return False
    return not onboarding_steps_complete(user, exp)


def show_onboarding_banner(user) -> bool:
    """Soft nudge on pipeline/search after skip or partial progress."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if is_power_user(user):
        return False
    exp = sync_onboarding_progress(user)
    if exp.onboarding_completed_at:
        return False
    return True


def onboarding_progress(user) -> dict:
    """Template-friendly onboarding checklist state."""
    exp = sync_onboarding_progress(user)
    llm_done = llm_ready_for_user(user) or exp.step_llm_connected
    resume_done = exp.step_resume_uploaded or has_library_resume(user)
    search_done = exp.step_first_search or has_search_activity(user)
    platform_llm = server_has_active_llm_keys()
    steps_complete = resume_done and search_done
    return {
        "needs_onboarding": needs_onboarding(user),
        "show_banner": show_onboarding_banner(user),
        "llm_ready": llm_ready_for_user(user),
        "platform_llm": platform_llm,
        "show_byok_hint": not platform_llm and not llm_done,
        "llm_done": llm_done,
        "resume_done": resume_done,
        "search_done": search_done,
        "steps_complete": steps_complete,
        "dismissed": bool(exp.onboarding_dismissed_at),
        "completed": bool(exp.onboarding_completed_at),
    }


def find_jobs_pulse_stats(user) -> dict:
    """
    Lightweight Growth Pulse metrics for the Find Jobs sidebar.

    Profile strength is a simple checklist score (resume / search / LLM ready).
    Pipeline counts exclude soft-deleted rows.
    """
    progress = onboarding_progress(user)
    score = 0
    if progress.get("resume_done"):
        score += 40
    if progress.get("search_done"):
        score += 35
    if progress.get("llm_done"):
        score += 25

    qs = PipelineEntry.objects.for_user(user).filter(removed_at__isnull=True)
    pipeline_count = qs.exclude(stage=PipelineEntry.Stage.DELETED).count()
    interviewing = qs.filter(stage=PipelineEntry.Stage.DONE).count()
    return {
        "profile_strength": score,
        "new_matches": pipeline_count,
        "interviewing": interviewing,
    }


def get_post_login_url(user, next_url: str | None = None) -> str:
    if next_url:
        return next_url
    if needs_onboarding(user):
        return reverse("getting_started")
    return reverse("pipeline")


def mark_onboarding_step(user, step: str) -> None:
    """Record explicit step completion (llm, resume, search)."""
    if not user or not getattr(user, "is_authenticated", False) or is_power_user(user):
        return
    exp = UserExperienceSettings.get_for_user(user)
    field_map = {
        "llm": "step_llm_connected",
        "resume": "step_resume_uploaded",
        "search": "step_first_search",
    }
    field = field_map.get(step)
    if not field or getattr(exp, field):
        sync_onboarding_progress(user)
        return
    setattr(exp, field, True)
    update_fields = [field]
    if onboarding_steps_complete(user, exp) and not exp.onboarding_completed_at:
        exp.onboarding_completed_at = timezone.now()
        update_fields.append("onboarding_completed_at")
    exp.save(update_fields=update_fields)


def dismiss_onboarding(user) -> None:
    exp = UserExperienceSettings.get_for_user(user)
    if not exp.onboarding_dismissed_at:
        exp.onboarding_dismissed_at = timezone.now()
        exp.save(update_fields=["onboarding_dismissed_at"])


def complete_onboarding(user) -> None:
    exp = UserExperienceSettings.get_for_user(user)
    sync_onboarding_progress(user)
    exp.refresh_from_db()
    if not exp.onboarding_completed_at:
        exp.onboarding_completed_at = timezone.now()
        exp.save(update_fields=["onboarding_completed_at"])


def set_experience_mode(user, mode: str) -> UserExperienceSettings:
    """
    Persist experience mode. Non-staff users are forced to normal (Simple).
    """
    if mode not in (
        UserExperienceSettings.ExperienceMode.NORMAL,
        UserExperienceSettings.ExperienceMode.POWER,
    ):
        raise ValueError(f"Invalid experience mode: {mode}")
    if not getattr(user, "is_staff", False):
        mode = UserExperienceSettings.ExperienceMode.NORMAL
    exp = UserExperienceSettings.get_for_user(user)
    exp.experience_mode = mode
    if mode == UserExperienceSettings.ExperienceMode.POWER and not exp.onboarding_completed_at:
        exp.onboarding_completed_at = timezone.now()
        exp.save(update_fields=["experience_mode", "onboarding_completed_at"])
    else:
        exp.save(update_fields=["experience_mode"])
    return exp
