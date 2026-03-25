from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0023_add_pipelineentry_stage"),
    ]

    operations = [
        migrations.AddField(
            model_name="userresume",
            name="track",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Preferred preference track slug for this resume (used to default Job Search track).",
                max_length=32,
            ),
        ),
    ]

