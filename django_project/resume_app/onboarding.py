"""Seed per-user defaults when a new account is created."""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .experience import default_experience_mode
from .models import (
    AppAutomationSettings,
    ApplicantProfile,
    Track,
    UserExperienceSettings,
)

User = get_user_model()

DEFAULT_TRACKS = (
    ("general", "General", True),
)


def seed_user_defaults(user, *, experience_mode: str | None = None) -> None:
    """Create tracks and profile rows for a new user."""
    for slug, label, is_default in DEFAULT_TRACKS:
        Track.objects.get_or_create(
            owner=user,
            slug=slug,
            defaults={"label": label, "is_default": is_default},
        )

    ApplicantProfile.get_for_user(user)
    AppAutomationSettings.get_for_user(user)

    mode = experience_mode or default_experience_mode()
    if not getattr(user, "is_staff", False):
        mode = UserExperienceSettings.ExperienceMode.NORMAL
    UserExperienceSettings.objects.get_or_create(
        owner=user,
        defaults={"experience_mode": mode},
    )

    try:
        from .subscriptions import ensure_default_plans, get_or_create_subscription

        ensure_default_plans()
        get_or_create_subscription(user)
    except Exception:
        pass


@receiver(post_save, sender=User)
def on_user_created(sender, instance, created, **kwargs):
    if created:
        seed_user_defaults(instance)
