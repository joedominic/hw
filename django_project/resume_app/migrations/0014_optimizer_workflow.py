# Generated for OptimizerWorkflow (saved custom workflows for Resume Optimization)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0013_dislikedjoblistingembedding"),
    ]

    operations = [
        migrations.CreateModel(
            name="OptimizerWorkflow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                (
                    "steps",
                    models.JSONField(
                        help_text="Ordered list of step ids, e.g. ['writer', 'ats_judge', 'recruiter_judge']"
                    ),
                ),
                ("loop_to", models.CharField(blank=True, max_length=64)),
                ("max_iterations", models.PositiveSmallIntegerField(default=3)),
                ("score_threshold", models.PositiveSmallIntegerField(default=85)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
    ]
