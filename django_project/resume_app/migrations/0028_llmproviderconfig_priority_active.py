from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("resume_app", "0027_appautomationsettings"),
    ]

    operations = [
        migrations.AddField(
            model_name="llmproviderconfig",
            name="is_active",
            field=models.BooleanField(
                default=False,
                help_text="DB-backed active provider for background jobs (Huey) and web flows.",
            ),
        ),
        migrations.AddField(
            model_name="llmproviderconfig",
            name="priority",
            field=models.PositiveSmallIntegerField(
                default=100,
                help_text="Lower number = higher preference when active provider is unavailable.",
            ),
        ),
    ]
