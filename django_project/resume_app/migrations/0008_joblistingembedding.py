from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0007_liked_and_disliked_job_listing"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobListingEmbedding",
            fields=[
                (
                    "job_listing",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to="resume_app.joblisting",
                    ),
                ),
                (
                    "embedding",
                    models.JSONField(
                        help_text="List of floats, e.g. 384-dim from sentence-transformers"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
