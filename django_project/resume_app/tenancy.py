"""
Multi-tenant helpers: owner-scoped querysets and safe object lookup.
"""
from __future__ import annotations

from typing import Any, Optional, Type, TypeVar

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db import models
from django.http import HttpRequest
from django.shortcuts import get_object_or_404

User = get_user_model()
T = TypeVar("T", bound=models.Model)

# Session keys cleared when support staff starts impersonating a user.
IMPERSONATION_SESSION_KEYS = (
    "optimizer_resume_id",
    "optimizer_llm_model",
    "optimizer_llm_provider",
    "job_search_track",
    "job_search_resume_id",
    "optimizer_workflow_id",
    "optimizer_ats_judge_profile_id",
)


class OwnedQuerySet(models.QuerySet):
    """QuerySet mixin for models with an owner FK."""

    def for_user(self, user: AbstractBaseUser):
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(owner=user)


class OwnedManager(models.Manager):
    def get_queryset(self):
        return OwnedQuerySet(self.model, using=self._db)

    def for_user(self, user: AbstractBaseUser):
        return self.get_queryset().for_user(user)


class OwnedModelMixin(models.Model):
    """Abstract base: every row belongs to one Django user."""

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )

    objects = OwnedManager()

    class Meta:
        abstract = True


class UserOwnedSingletonMixin(models.Model):
    """
    One row per user (ApplicantProfile, AppAutomationSettings, etc.).
    Subclasses must define Meta.unique_together or constraints on owner.
    """

    owner = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="%(class)s_profile",
    )

    objects = OwnedManager()

    class Meta:
        abstract = True

    @classmethod
    def get_for_user(cls, user: AbstractBaseUser):
        obj, _created = cls.objects.get_or_create(owner=user)
        return obj


def get_active_user(request: HttpRequest) -> AbstractBaseUser:
    """Tenant user for data scoping (emulated user during hijack)."""
    return getattr(request, "auth", None) or request.user


def api_user(request: HttpRequest) -> AbstractBaseUser:
    """User from Django view or Ninja API request."""
    return get_active_user(request)


def get_real_user(request: HttpRequest) -> Optional[AbstractBaseUser]:
    """Actual signed-in user (support admin during hijack)."""
    try:
        from hijack.helpers import get_hijacker

        hijacker = get_hijacker(request.user)
        if hijacker is not None:
            return hijacker
    except ImportError:
        pass
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


def get_owned_or_404(model: Type[T], user: AbstractBaseUser, **lookup: Any) -> T:
    """Like get_object_or_404 but enforces owner match (IDOR-safe)."""
    if not getattr(user, "is_authenticated", False):
        from django.http import Http404

        raise Http404()
    if hasattr(model, "owner_id"):
        lookup.setdefault("owner", user)
    return get_object_or_404(model, **lookup)


def clear_impersonation_session_keys(request: HttpRequest) -> None:
    """Drop optimizer/search session prefs when starting a hijack."""
    for key in IMPERSONATION_SESSION_KEYS:
        request.session.pop(key, None)


def user_resume_upload_to(instance: "models.Model", filename: str) -> str:
    owner_id = getattr(instance, "owner_id", None) or "unknown"
    return f"resumes/{owner_id}/{filename}"
