# Generated manually for Phase 1 account email verification fields.

from django.db import migrations, models
from django.utils import timezone


def mark_existing_users_verified(apps, schema_editor):
    UserExperienceSettings = apps.get_model("resume_app", "UserExperienceSettings")
    now = timezone.now()
    UserExperienceSettings.objects.filter(email_verified_at__isnull=True).update(email_verified_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0021_joblistingaction_hidden"),
    ]

    operations = [
        migrations.AddField(
            model_name="userexperiencesettings",
            name="email_verified_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the account email was verified. Null means unverified.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="userexperiencesettings",
            name="pending_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="Email awaiting verification after an address change.",
                max_length=254,
            ),
        ),
        migrations.RunPython(mark_existing_users_verified, migrations.RunPython.noop),
    ]
