from django.db import migrations, models


def backfill_userresume_is_library(apps, schema_editor):
    UserResume = apps.get_model("resume_app", "UserResume")
    OptimizedResume = apps.get_model("resume_app", "OptimizedResume")
    ephemeral_ids = set(
        OptimizedResume.objects.values_list("original_resume_id", flat=True).distinct()
    )
    if ephemeral_ids:
        UserResume.objects.filter(id__in=ephemeral_ids).update(is_library=False)
        UserResume.objects.exclude(id__in=ephemeral_ids).update(is_library=True)
    else:
        UserResume.objects.update(is_library=True)


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0006_appautomation_optimizer_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="userresume",
            name="is_library",
            field=models.BooleanField(
                default=False,
                help_text="True for PDFs uploaded on Resumes & Tracks; False for ephemeral optimizer run copies.",
            ),
        ),
        migrations.RunPython(backfill_userresume_is_library, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="userresume",
            index=models.Index(fields=["is_library", "-uploaded_at"], name="resume_app__is_lib_8a1f0d_idx"),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="cleanup_generated_resume_retention_days",
            field=models.PositiveSmallIntegerField(
                default=7,
                help_text="Remove optimizer ephemeral resume PDFs older than this many days (0 = off).",
            ),
        ),
    ]
