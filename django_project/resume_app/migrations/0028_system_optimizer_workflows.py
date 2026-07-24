"""Make OptimizerWorkflow system-wide (owner nullable) and promote staff-owned rows."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def promote_staff_workflows(apps, schema_editor):
    """Promote staff-owned workflows to global (owner=null) so subscribers can see them."""
    User = apps.get_model("auth", "User")
    OptimizerWorkflow = apps.get_model("resume_app", "OptimizerWorkflow")

    staff_ids = list(User.objects.filter(is_staff=True).values_list("pk", flat=True))
    if not staff_ids:
        return

    OptimizerWorkflow.objects.filter(owner_id__in=staff_ids).update(owner_id=None)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("resume_app", "0027_promote_global_ats_profiles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="optimizerworkflow",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Null for system-wide admin-managed workflows. "
                    "Legacy per-user rows are ignored at runtime."
                ),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="optimizer_workflows",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(promote_staff_workflows, noop_reverse),
    ]
