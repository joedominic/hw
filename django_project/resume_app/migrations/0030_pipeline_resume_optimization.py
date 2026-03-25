import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0029_llmproviderpreference"),
    ]

    operations = [
        migrations.AddField(
            model_name="optimizedresume",
            name="optimizer_workflow",
            field=models.ForeignKey(
                blank=True,
                help_text="Saved workflow used when this run was enqueued.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="optimized_resumes",
                to="resume_app.optimizerworkflow",
            ),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="pipeline_entry",
            field=models.ForeignKey(
                blank=True,
                help_text="Pipeline row this optimization was started for (Applying stage).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="optimized_resumes",
                to="resume_app.pipelineentry",
            ),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="applying_optimizer_workflow",
            field=models.ForeignKey(
                blank=True,
                help_text="Default Resume Optimizer workflow for Applying-stage runs (auto + bulk).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="resume_app.optimizerworkflow",
            ),
        ),
    ]
