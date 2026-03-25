from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0022_remove_legacy_pipeline_columns"),
    ]

    operations = [
        migrations.AddField(
            model_name="pipelineentry",
            name="stage",
            field=models.CharField(
                choices=[
                    ("pipeline", "Pipeline"),
                    ("vetting", "Vetting"),
                    ("applying", "Applying"),
                    ("done", "Done"),
                    ("deleted", "Deleted"),
                ],
                blank=True,
                default="",
                max_length=16,
                help_text="Lightweight stage for this job in the pipeline (e.g. vetting, applying, done). Blank means legacy/default pipeline.",
            ),
        ),
    ]

