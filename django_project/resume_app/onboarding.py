"""Seed per-user defaults when a new account is created."""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import (
    AppAutomationSettings,
    ApplicantProfile,
    AtsJudgeProfile,
    Track,
    UserPromptProfile,
)

User = get_user_model()

DEFAULT_TRACKS = (
    ("ic", "IC (Principal / Staff)", True),
    ("mgmt", "Management (Manager / Director)", False),
)


def seed_user_defaults(user) -> None:
    """Create tracks, profile rows, and builtin ATS judge copies for a user."""
    for slug, label, is_default in DEFAULT_TRACKS:
        Track.objects.get_or_create(
            owner=user,
            slug=slug,
            defaults={"label": label, "is_default": is_default},
        )

    ApplicantProfile.get_for_user(user)
    AppAutomationSettings.get_for_user(user)
    UserPromptProfile.get_for_user(user)

    _seed_ats_judge_profiles(user)


def _seed_ats_judge_profiles(user) -> None:
    """Copy global builtin ATS judge profiles for this user if none exist."""
    if AtsJudgeProfile.objects.filter(owner=user).exists():
        return
    builtins = AtsJudgeProfile.objects.filter(owner__isnull=True, is_builtin=True)
    if not builtins.exists():
        builtins = AtsJudgeProfile.objects.filter(is_builtin=True)
    for src in builtins:
        AtsJudgeProfile.objects.get_or_create(
            owner=user,
            slug=src.slug,
            defaults={
                "name": src.name,
                "ats_judge": src.ats_judge,
                "ats_judge_system": src.ats_judge_system,
                "ats_judge_user": src.ats_judge_user,
                "is_builtin": src.is_builtin,
                "is_default": src.is_default,
            },
        )


@receiver(post_save, sender=User)
def on_user_created(sender, instance, created, **kwargs):
    if created:
        seed_user_defaults(instance)
