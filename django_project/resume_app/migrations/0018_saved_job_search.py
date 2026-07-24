"""Seed single default search profile; SavedJobSearch model."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0017_alter_atsjudgeprofile_owner"),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedJobSearch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("search_term", models.CharField(blank=True, default="", max_length=512)),
                ("location", models.CharField(blank=True, default="", max_length=512)),
                (
                    "profile_slug",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Search profile slug (Track.slug) for pipeline and scoring context.",
                        max_length=32,
                    ),
                ),
                ("min_score", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("results_wanted", models.PositiveSmallIntegerField(default=50)),
                ("site_names", models.JSONField(blank=True, default=list)),
                ("llm_model", models.CharField(blank=True, default="", max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="saved_job_searches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resume",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="saved_job_searches",
                        to="resume_app.userresume",
                    ),
                ),
            ],
            options={
                "verbose_name": "Saved job search",
                "verbose_name_plural": "Saved job searches",
                "ordering": ["-updated_at", "name"],
                "unique_together": {("owner", "name")},
            },
        ),
    ]
