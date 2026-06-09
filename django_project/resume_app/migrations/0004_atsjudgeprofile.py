from django.db import migrations, models
import django.db.models.deletion


def seed_ats_judge_profiles(apps, schema_editor):
    AtsJudgeProfile = apps.get_model("resume_app", "AtsJudgeProfile")
    UserPromptProfile = apps.get_model("resume_app", "UserPromptProfile")

    if AtsJudgeProfile.objects.exists():
        return

    profile = UserPromptProfile.objects.filter(pk=1).first()
    has_custom = False
    if profile:
        has_custom = any(
            (getattr(profile, f) or "").strip()
            for f in ("ats_judge", "ats_judge_system", "ats_judge_user")
        )

    if has_custom:
        AtsJudgeProfile.objects.create(
            name="Default (migrated)",
            slug="default",
            ats_judge=profile.ats_judge or "",
            ats_judge_system=profile.ats_judge_system or "",
            ats_judge_user=profile.ats_judge_user or "",
            is_builtin=True,
            is_default=True,
        )
    else:
        # Empty profile: store empty fields; runtime uses code defaults via resolve_ats_judge_parts.
        AtsJudgeProfile.objects.create(
            name="Default",
            slug="default",
            ats_judge="",
            ats_judge_system="",
            ats_judge_user="",
            is_builtin=True,
            is_default=True,
        )


def unseed_ats_judge_profiles(apps, schema_editor):
    AtsJudgeProfile = apps.get_model("resume_app", "AtsJudgeProfile")
    AtsJudgeProfile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0003_llmproviderpreference_is_local"),
    ]

    operations = [
        migrations.CreateModel(
            name="AtsJudgeProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                (
                    "slug",
                    models.SlugField(
                        help_text="Stable identifier for API and defaults (e.g. 'default').",
                        max_length=64,
                        unique=True,
                    ),
                ),
                ("ats_judge", models.TextField(blank=True)),
                ("ats_judge_system", models.TextField(blank=True)),
                ("ats_judge_user", models.TextField(blank=True)),
                (
                    "is_builtin",
                    models.BooleanField(
                        default=False,
                        help_text="Seeded built-in profile; deletion may be restricted in UI.",
                    ),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        default=False,
                        help_text="Global fallback when no per-run or workflow ATS is selected.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "ATS judge profile",
                "verbose_name_plural": "ATS judge profiles",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="ats_judge_profile",
            field=models.ForeignKey(
                blank=True,
                help_text="ATS judge prompt profile used for this optimization run.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="optimized_resumes",
                to="resume_app.atsjudgeprofile",
            ),
        ),
        migrations.AddField(
            model_name="optimizerworkflow",
            name="ats_judge_profile",
            field=models.ForeignKey(
                blank=True,
                help_text="Default ATS judge prompt when this workflow is used.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="optimizer_workflows",
                to="resume_app.atsjudgeprofile",
            ),
        ),
        migrations.RunPython(seed_ats_judge_profiles, unseed_ats_judge_profiles),
    ]
