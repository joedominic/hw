from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0008_joblistingembedding"),
    ]

    operations = [
        migrations.AddField(
            model_name="joblisting",
            name="requires_clearance",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="joblisting",
            name="requires_us_citizen",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="joblisting",
            name="no_c2c",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="joblisting",
            name="no_visa_sponsorship",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="joblisting",
            name="high_travel",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="joblisting",
            name="work_model",
            field=models.CharField(
                choices=[
                    ("unknown", "Unknown"),
                    ("remote", "Remote"),
                    ("hybrid", "Hybrid"),
                    ("onsite", "On-site"),
                ],
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="UserDealbreakerPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("avoid_clearance_jobs", models.BooleanField(default=False)),
                ("avoid_no_sponsorship_jobs", models.BooleanField(default=False)),
                ("avoid_high_travel_jobs", models.BooleanField(default=False)),
                ("avoid_onsite_jobs", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]

