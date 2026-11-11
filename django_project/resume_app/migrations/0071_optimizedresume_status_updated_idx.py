"""Index optimizer runs by status and last update for the stale-run reaper."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0070_raise_free_plan_daily_tokens"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="optimizedresume",
            index=models.Index(fields=["status", "updated_at"], name="opt_res_status_updated_idx"),
        ),
    ]
