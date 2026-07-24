"""Enforce non-null owner on tenant-scoped models (0014 backfill follow-up)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_null_owners(apps, schema_editor):
    """Assign bootstrap user to any rows still missing owner before NOT NULL."""
    User = apps.get_model("auth", "User")
    bootstrap = User.objects.filter(username="migration_bootstrap").first()
    if bootstrap is None:
        return

    owned_models = [
        "OptimizedResume",
        "UserPromptProfile",
        "LLMProviderConfig",
        "LLMAppUsageTotals",
        "LLMUsageByModel",
        "LLMUsageByQuery",
        "AppAutomationSettings",
        "JobListingAction",
        "JobListingEmbedding",
        "JobListingTrackMetrics",
        "UserDisqualifier",
        "OptimizerWorkflow",
        "JobMatchResult",
        "JobSearchTask",
        "PipelineEntry",
        "ApplicantProfile",
        "SiteCredential",
        "AtsAutoSubmitStats",
    ]
    for name in owned_models:
        Model = apps.get_model("resume_app", name)
        Model.objects.filter(owner__isnull=True).update(owner=bootstrap)


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0015_user_experience_settings"),
    ]

    operations = [
        migrations.RunPython(backfill_null_owners, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="optimizedresume",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="optimized_resumes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userpromptprofile",
            name="owner",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="prompt_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="llmproviderconfig",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_provider_configs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="llmappusagetotals",
            name="owner",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_totals",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="llmusagebymodel",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_by_model",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="llmusagebyquery",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_by_query",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="appautomationsettings",
            name="owner",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="automation_settings",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="joblistingaction",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_actions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="joblistingembedding",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_embeddings",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="joblistingtrackmetrics",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_track_metrics",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userdisqualifier",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="disqualifiers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="optimizerworkflow",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="optimizer_workflows",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="jobmatchresult",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_match_results",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="jobsearchtask",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_search_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="pipelineentry",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pipeline_entries",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="applicantprofile",
            name="owner",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="applicant_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="sitecredential",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="site_credentials",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="atsautosubmitstats",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ats_auto_submit_stats",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveIndex(
            model_name="joblistingtrackmetrics",
            name="resume_app__track_90ff7c_idx",
        ),
        migrations.RemoveIndex(
            model_name="userresume",
            name="resume_app__is_libr_d504ff_idx",
        ),
        migrations.AddIndex(
            model_name="joblistingtrackmetrics",
            index=models.Index(fields=["owner", "track", "job_listing"], name="resume_app__owner_i_83a761_idx"),
        ),
        migrations.AddIndex(
            model_name="userresume",
            index=models.Index(fields=["owner", "is_library", "-uploaded_at"], name="resume_app__owner_i_972af6_idx"),
        ),
    ]
