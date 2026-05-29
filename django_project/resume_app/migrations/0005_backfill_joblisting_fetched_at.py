"""Backfill JobListing rows with empty/invalid fetched_at from legacy DB recovery."""

from django.db import migrations
from django.utils import timezone


def backfill_fetched_at(apps, schema_editor):
    JobListing = apps.get_model("resume_app", "JobListing")
    now = timezone.now()
    JobListing.objects.filter(fetched_at__isnull=True).update(fetched_at=now)
    # SQLite recovery sometimes stored '' instead of NULL (ORM cannot filter DateTimeField = '').
    if schema_editor.connection.vendor == "sqlite":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE resume_app_joblisting SET fetched_at = %s WHERE length(fetched_at) = 0",
                [now],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0004_atsjudgeprofile"),
    ]

    operations = [
        migrations.RunPython(backfill_fetched_at, migrations.RunPython.noop),
    ]
