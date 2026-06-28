"""Multi-tenant owner FKs and impersonation audit log."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import resume_app.tenancy


def backfill_owners(apps, schema_editor):
    User = apps.get_model("auth", "User")
    from django.contrib.auth.hashers import make_password

    # Create an inactive, non-login placeholder with an unusable password so that
    # no one can authenticate as this account. Real environments should re-assign
    # these rows to a real admin via: User.objects.filter(username='migration_bootstrap')
    # .update(owner=real_admin) after migration, then delete this placeholder.
    bootstrap, created = User.objects.get_or_create(
        username="migration_bootstrap",
        defaults={
            "email": "migration@local",
            "is_staff": False,
            "is_superuser": False,
            "is_active": False,
            "password": make_password(None),  # unusable — cannot authenticate
        },
    )

    owned_models = [
        "Track",
        "UserResume",
        "OptimizedResume",
        "AtsJudgeProfile",
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


def seed_support_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="resume_app",
        model="impersonationauditlog",
    )
    perm, _ = Permission.objects.get_or_create(
        codename="can_impersonate_users",
        content_type=ct,
        defaults={"name": "Can impersonate users for support"},
    )
    group, _ = Group.objects.get_or_create(name="Support")
    group.permissions.add(perm)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("resume_app", "0013_job_prep_artifacts"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImpersonationAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("reason", models.CharField(blank=True, default="", max_length=500)),
                (
                    "hijacker",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="impersonations_started",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="impersonations_received",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-started_at"],
                "permissions": [("can_impersonate_users", "Can impersonate users for support")],
            },
        ),
        migrations.AddField(
            model_name="track",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tracks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="userresume",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="resumes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="optimized_resumes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="atsjudgeprofile",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ats_judge_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="owner",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="prompt_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="llmproviderconfig",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_provider_configs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="llmappusagetotals",
            name="owner",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_totals",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="llmusagebymodel",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_by_model",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="llmusagebyquery",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="llm_usage_by_query",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="owner",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="automation_settings",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="joblistingaction",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_actions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="joblistingembedding",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_embeddings",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="joblistingtrackmetrics",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_listing_track_metrics",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="userdisqualifier",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="disqualifiers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="optimizerworkflow",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="optimizer_workflows",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="jobmatchresult",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_match_results",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="jobsearchtask",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="job_search_tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pipelineentry",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pipeline_entries",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="applicantprofile",
            name="owner",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="applicant_profile",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="sitecredential",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="site_credentials",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="atsautosubmitstats",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ats_auto_submit_stats",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_owners, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="track",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tracks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userresume",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="resumes",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="userresume",
            name="file",
            field=models.FileField(upload_to=resume_app.tenancy.user_resume_upload_to),
        ),
        migrations.AlterUniqueTogether(
            name="track",
            unique_together={("owner", "slug")},
        ),
        migrations.AlterField(
            model_name="track",
            name="slug",
            field=models.SlugField(
                help_text="Short code used in URLs and tasks, e.g. 'ic', 'mgmt', 'eu_ic'.",
                max_length=32,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="atsjudgeprofile",
            unique_together={("owner", "slug")},
        ),
        migrations.AlterField(
            model_name="atsjudgeprofile",
            name="slug",
            field=models.SlugField(
                help_text="Stable identifier for API and defaults (e.g. 'default').",
                max_length=64,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="llmproviderconfig",
            unique_together={("owner", "provider")},
        ),
        migrations.AlterField(
            model_name="llmproviderconfig",
            name="provider",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterUniqueTogether(
            name="llmusagebymodel",
            unique_together={("owner", "provider", "model")},
        ),
        migrations.AlterUniqueTogether(
            name="llmusagebyquery",
            unique_together={("owner", "query_kind", "provider", "model")},
        ),
        migrations.AlterUniqueTogether(
            name="joblistingaction",
            unique_together={("owner", "job_listing", "action", "track")},
        ),
        migrations.AlterUniqueTogether(
            name="joblistingembedding",
            unique_together={("owner", "job_listing", "embedding_type", "track")},
        ),
        migrations.AlterUniqueTogether(
            name="joblistingtrackmetrics",
            unique_together={("owner", "job_listing", "track")},
        ),
        migrations.AlterUniqueTogether(
            name="userdisqualifier",
            unique_together={("owner", "phrase")},
        ),
        migrations.AlterField(
            model_name="userdisqualifier",
            name="phrase",
            field=models.CharField(max_length=500),
        ),
        migrations.AlterUniqueTogether(
            name="jobmatchresult",
            unique_together={("owner", "job_listing", "resume")},
        ),
        migrations.AlterUniqueTogether(
            name="pipelineentry",
            unique_together={("owner", "job_listing", "track")},
        ),
        migrations.AlterUniqueTogether(
            name="sitecredential",
            unique_together={("owner", "domain")},
        ),
        migrations.AlterField(
            model_name="sitecredential",
            name="domain",
            field=models.CharField(
                help_text="Host the credential applies to, e.g. 'boards.greenhouse.io' or 'acme.com'.",
                max_length=255,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="atsautosubmitstats",
            unique_together={("owner", "ats_type")},
        ),
        migrations.AlterField(
            model_name="atsautosubmitstats",
            name="ats_type",
            field=models.CharField(max_length=32),
        ),
        migrations.RunPython(seed_support_group, migrations.RunPython.noop),
    ]
