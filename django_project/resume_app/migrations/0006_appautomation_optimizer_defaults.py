from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0005_backfill_joblisting_fetched_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="appautomationsettings",
            name="default_optimization_notes",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Default Writer notes for the Resume Optimizer (Step 2 / Settings).",
            ),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="default_pipeline_skills_json",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Default pipeline/skills JSON for the Resume Optimizer.",
            ),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="default_job_highlights",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Default supplemental accomplishments for the Resume Optimizer.",
            ),
        ),
    ]
