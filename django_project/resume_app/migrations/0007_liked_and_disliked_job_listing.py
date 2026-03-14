from django.db import migrations, models
import django.db.models.deletion


def migrate_deleted_to_disliked(apps, schema_editor):
    DeletedJobListing = apps.get_model("resume_app", "DeletedJobListing")
    DislikedJobListing = apps.get_model("resume_app", "DislikedJobListing")

    for deleted in DeletedJobListing.objects.all():
        DislikedJobListing.objects.get_or_create(
            job_listing_id=deleted.job_listing_id,
            defaults={"created_at": deleted.created_at},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0006_deleted_and_saved_job_listing"),
    ]

    operations = [
        migrations.CreateModel(
            name="DislikedJobListing",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="LikedJobListing",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.RunPython(migrate_deleted_to_disliked, migrations.RunPython.noop),
        migrations.DeleteModel(
            name="DeletedJobListing",
        ),
    ]

