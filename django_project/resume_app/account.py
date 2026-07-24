"""Account lifecycle: email verification, export, and deletion."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .media_access import delete_user_media_files
from .models import (
    ApplicantProfile,
    AppAutomationSettings,
    JobSearchTask,
    OptimizedResume,
    PipelineEntry,
    SearchProfile,
    Track,
    UserExperienceSettings,
    UserResume,
)

logger = logging.getLogger(__name__)
User = get_user_model()


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """Invalidate tokens when email, pending email, or verification state changes."""

    def _make_hash_value(self, user, timestamp):  # type: ignore[override]
        exp = UserExperienceSettings.get_for_user(user)
        email = (user.email or "").lower()
        pending = (exp.pending_email or "").lower()
        verified = str(exp.email_verified_at.isoformat() if exp.email_verified_at else "")
        return f"{user.pk}{user.password}{email}{pending}{verified}{timestamp}"


email_verify_token = EmailVerificationTokenGenerator()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def email_taken(email: str, *, exclude_user=None) -> bool:
    """Case-insensitive uniqueness check for account email."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    qs = User.objects.filter(email__iexact=normalized)
    if exclude_user is not None:
        qs = qs.exclude(pk=exclude_user.pk)
    return qs.exists()


def _user_email_keep_score(user) -> tuple:
    """Higher score wins when resolving duplicate emails."""
    resume_count = UserResume.objects.filter(owner_id=user.pk).count()
    pipeline_count = PipelineEntry.objects.filter(owner_id=user.pk).count()
    return (
        1 if getattr(user, "is_superuser", False) else 0,
        1 if getattr(user, "is_staff", False) else 0,
        resume_count + pipeline_count,
        # Prefer older accounts when usage is equal.
        -int(user.pk or 0),
    )


def dedupe_user_emails(*, dry_run: bool = False) -> list[dict[str, Any]]:
    """
    Normalize emails to lowercase and clear duplicates.

    Keeps the strongest account per email (staff/superuser, then most owned
    resumes/pipeline rows, then oldest pk). Losers get email cleared to ''
    so they can still sign in by username. Blank emails are left alone.
    """
    from collections import defaultdict

    by_email: dict[str, list] = defaultdict(list)
    actions: list[dict[str, Any]] = []

    for user in User.objects.all().iterator():
        normalized = normalize_email(user.email or "")
        if not normalized:
            if user.email:
                actions.append(
                    {"user_id": user.pk, "username": user.username, "action": "clear_blankish", "email": ""}
                )
                if not dry_run:
                    user.email = ""
                    user.save(update_fields=["email"])
            continue
        if user.email != normalized:
            actions.append(
                {
                    "user_id": user.pk,
                    "username": user.username,
                    "action": "normalize",
                    "email": normalized,
                }
            )
            if not dry_run:
                user.email = normalized
                user.save(update_fields=["email"])
            else:
                user.email = normalized
        by_email[normalized].append(user)

    for email, users in by_email.items():
        if len(users) < 2:
            continue
        ranked = sorted(users, key=_user_email_keep_score, reverse=True)
        keeper = ranked[0]
        actions.append(
            {
                "user_id": keeper.pk,
                "username": keeper.username,
                "action": "keep",
                "email": email,
                "duplicates": [u.username for u in ranked[1:]],
            }
        )
        for loser in ranked[1:]:
            actions.append(
                {
                    "user_id": loser.pk,
                    "username": loser.username,
                    "action": "clear_duplicate",
                    "email": "",
                    "kept_username": keeper.username,
                    "previous_email": email,
                }
            )
            if not dry_run:
                loser.email = ""
                loser.save(update_fields=["email"])

    return actions


def install_user_email_signals() -> None:
    """Normalize User.email on every save (idempotent register)."""
    from django.db.models.signals import pre_save

    def _normalize_user_email(sender, instance, **kwargs):
        if getattr(instance, "email", None):
            instance.email = normalize_email(instance.email)

    pre_save.connect(_normalize_user_email, sender=User, dispatch_uid="resume_app.normalize_user_email")


def is_email_verified(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    exp = UserExperienceSettings.get_for_user(user)
    return exp.email_verified_at is not None


def mark_email_verified(user, *, email: str | None = None) -> UserExperienceSettings:
    exp = UserExperienceSettings.get_for_user(user)
    update_fields = ["email_verified_at"]
    if email:
        user.email = normalize_email(email)
        user.save(update_fields=["email"])
        exp.pending_email = ""
        update_fields.append("pending_email")
    elif exp.pending_email:
        user.email = normalize_email(exp.pending_email)
        user.save(update_fields=["email"])
        exp.pending_email = ""
        update_fields.append("pending_email")
    exp.email_verified_at = timezone.now()
    exp.save(update_fields=update_fields)
    return exp


def make_verify_uid_token(user) -> tuple[str, str]:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verify_token.make_token(user)
    return uid, token


def user_from_verify_uid(uidb64: str):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None


def send_verification_email(user, *, request=None) -> bool:
    """Send verification email. Returns False if no email or send failed."""
    exp = UserExperienceSettings.get_for_user(user)
    target = normalize_email(exp.pending_email or user.email or "")
    if not target:
        return False

    uid, token = make_verify_uid_token(user)
    path = f"/accounts/verify-email/{uid}/{token}/"
    if request is not None:
        absolute = request.build_absolute_uri(path)
    else:
        base = getattr(settings, "ACCOUNT_EMAIL_BASE_URL", "").rstrip("/")
        absolute = f"{base}{path}" if base else path

    subject = "Verify your ResumeElite email"
    body = (
        f"Hi {user.get_username()},\n\n"
        f"Confirm your email address for ResumeElite:\n\n{absolute}\n\n"
        "If you did not create this account, you can ignore this message.\n"
    )
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "webmaster@localhost")
    try:
        send_mail(subject, body, from_email, [target], fail_silently=False)
        return True
    except Exception as exc:
        logger.exception("Failed to send verification email to %s: %s", target, exc)
        return False


def request_email_change(user, new_email: str) -> None:
    """Store pending email and clear verification until confirmed."""
    normalized = normalize_email(new_email)
    if not normalized:
        raise ValueError("Email is required.")
    if email_taken(normalized, exclude_user=user):
        raise ValueError("An account with this email already exists.")
    exp = UserExperienceSettings.get_for_user(user)
    if normalize_email(user.email) == normalized and exp.email_verified_at:
        exp.pending_email = ""
        exp.save(update_fields=["pending_email"])
        return
    exp.pending_email = normalized
    exp.email_verified_at = None
    exp.save(update_fields=["pending_email", "email_verified_at"])


def build_account_export(user) -> dict[str, Any]:
    """Serializable snapshot of the user's account data for download."""
    exp = UserExperienceSettings.get_for_user(user)
    profile = ApplicantProfile.get_for_user(user)
    automation = AppAutomationSettings.get_for_user(user)
    resumes = list(
        UserResume.objects.for_user(user).values(
            "id", "original_filename", "is_library", "track", "uploaded_at"
        )
    )
    tracks = list(Track.objects.for_user(user).values("id", "slug", "label", "description"))
    search_profiles = list(
        SearchProfile.objects.for_user(user).values(
            "id", "name", "slug", "search_term", "location", "created_at", "updated_at"
        )
    )
    tasks = list(
        JobSearchTask.objects.for_user(user).values(
            "id", "name", "search_term", "location", "is_active", "frequency", "track"
        )
    )
    pipeline_counts = {
        stage: PipelineEntry.objects.for_user(user).filter(stage=stage).count()
        for stage in ("pipeline", "vetting", "applying", "done")
    }
    opt_counts = {
        "total": OptimizedResume.objects.for_user(user).count(),
        "completed": OptimizedResume.objects.for_user(user)
        .filter(status=OptimizedResume.STATUS_COMPLETED)
        .count(),
    }
    return {
        "exported_at": datetime.now(tz=dt_timezone.utc).isoformat(),
        "user": {
            "id": user.pk,
            "username": user.get_username(),
            "email": user.email,
            "date_joined": user.date_joined.isoformat() if user.date_joined else None,
            "email_verified_at": exp.email_verified_at.isoformat() if exp.email_verified_at else None,
            "experience_mode": exp.experience_mode,
            "pending_email": exp.pending_email or None,
        },
        "applicant_profile": {
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "linkedin_url": profile.linkedin_url,
            "website_url": profile.website_url,
            "work_authorization": profile.work_authorization,
            "requires_sponsorship": profile.requires_sponsorship,
            "salary_expectation": profile.salary_expectation,
        },
        "automation_settings": {
            "stop_llm_requests": automation.stop_llm_requests,
            "apply_agent_enabled": getattr(automation, "apply_agent_enabled", None),
            "export_replacements": getattr(automation, "export_replacements", None) or [],
        },
        "resumes": resumes,
        "tracks": tracks,
        "search_profiles": search_profiles,
        "job_search_tasks": tasks,
        "pipeline_counts": pipeline_counts,
        "optimized_resume_counts": opt_counts,
    }


def export_account_json(user) -> str:
    return json.dumps(build_account_export(user), indent=2, default=str)


def delete_user_account(user) -> None:
    """Delete media files then the user row (CASCADE removes owned models)."""
    delete_user_media_files(user)
    user.delete()
