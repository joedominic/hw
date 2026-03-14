from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0004_optimizedresume_token_usage"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobListing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(max_length=64)),
                ("external_id", models.CharField(max_length=256)),
                ("title", models.CharField(max_length=512)),
                ("company_name", models.CharField(max_length=512)),
                ("location", models.CharField(blank=True, max_length=512)),
                ("description", models.TextField(blank=True)),
                ("url", models.URLField(blank=True, max_length=2048)),
                ("fetched_at", models.DateTimeField(auto_now_add=True)),
                ("raw_json", models.JSONField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-fetched_at"],
                "unique_together": {("source", "external_id")},
            },
        ),
        migrations.CreateModel(
            name="JobMatchResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("fit_score", models.IntegerField(blank=True, null=True)),
                ("reasoning", models.TextField(blank=True)),
                ("thoughts", models.TextField(blank=True)),
                ("analyzed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("analyzed", "Analyzed"),
                            ("applied", "Applied"),
                            ("dismissed", "Dismissed"),
                        ],
                        default="analyzed",
                        max_length=32,
                    ),
                ),
                (
                    "job_listing",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="resume_app.joblisting"),
                ),
                (
                    "resume",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="resume_app.userresume"),
                ),
            ],
            options={
                "ordering": ["-analyzed_at"],
                "unique_together": {("job_listing", "resume")},
            },
        ),
    ]
