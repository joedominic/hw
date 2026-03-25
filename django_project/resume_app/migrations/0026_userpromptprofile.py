# Generated manually for UserPromptProfile

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0025_pipelineentry_vetting_interview_probability_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPromptProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("writer", models.TextField(blank=True)),
                ("ats_judge", models.TextField(blank=True)),
                ("recruiter_judge", models.TextField(blank=True)),
                ("matching", models.TextField(blank=True)),
                ("insights", models.TextField(blank=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "User prompt profile",
                "verbose_name_plural": "User prompt profiles",
            },
        ),
    ]
