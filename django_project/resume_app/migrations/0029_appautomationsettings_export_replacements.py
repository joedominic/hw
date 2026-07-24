# Generated manually for durable export replacement tokens.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0028_system_optimizer_workflows"),
    ]

    operations = [
        migrations.AddField(
            model_name="appautomationsettings",
            name="export_replacements",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Up to 5 {token, value} pairs applied when exporting optimized resumes to PDF/Word.",
            ),
        ),
    ]
