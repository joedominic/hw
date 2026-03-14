from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0005_add_job_listing_and_match_result"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeletedJobListing",
            fields=[
                ("job_listing", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to="resume_app.joblisting")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="SavedJobListing",
            fields=[
                ("job_listing", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to="resume_app.joblisting")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
