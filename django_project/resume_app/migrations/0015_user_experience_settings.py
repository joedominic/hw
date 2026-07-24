"""User experience settings and onboarding progress."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


def backfill_experience_settings(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserExperienceSettings = apps.get_model("resume_app", "UserExperienceSettings")
    UserResume = apps.get_model("resume_app", "UserResume")
    LLMProviderConfig = apps.get_model("resume_app", "LLMProviderConfig")
    PipelineEntry = apps.get_model("resume_app", "PipelineEntry")
    OptimizedResume = apps.get_model("resume_app", "OptimizedResume")

    now = timezone.now()
    for user in User.objects.all().iterator():
        has_resume = UserResume.objects.filter(owner_id=user.pk, is_library=True).exists()
        has_llm = LLMProviderConfig.objects.filter(owner_id=user.pk).exclude(encrypted_api_key="").exists()
        has_activity = (
            PipelineEntry.objects.filter(owner_id=user.pk).exists()
            or OptimizedResume.objects.filter(owner_id=user.pk).exists()
            or has_resume
        )
        mode = "normal"
        if has_llm and has_resume:
            mode = "power"
        completed_at = now if has_activity else None
        UserExperienceSettings.objects.get_or_create(
            owner_id=user.pk,
            defaults={
                "experience_mode": mode,
                "onboarding_completed_at": completed_at,
                "step_llm_connected": has_llm,
                "step_resume_uploaded": has_resume,
                "step_first_search": PipelineEntry.objects.filter(owner_id=user.pk).exists(),
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0014_multi_tenant_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserExperienceSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "experience_mode",
                    models.CharField(
                        choices=[("normal", "Normal"), ("power", "Power")],
                        default="normal",
                        max_length=16,
                    ),
                ),
                ("onboarding_completed_at", models.DateTimeField(blank=True, null=True)),
                ("onboarding_dismissed_at", models.DateTimeField(blank=True, null=True)),
                ("step_llm_connected", models.BooleanField(default=False)),
                ("step_resume_uploaded", models.BooleanField(default=False)),
                ("step_first_search", models.BooleanField(default=False)),
                (
                    "owner",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="experience_settings",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User experience settings",
                "verbose_name_plural": "User experience settings",
            },
        ),
        migrations.RunPython(backfill_experience_settings, migrations.RunPython.noop),
    ]
