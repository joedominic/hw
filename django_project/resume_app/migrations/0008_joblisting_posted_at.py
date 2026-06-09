from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0007_userresume_is_library_cleanup"),
    ]

    operations = [
        migrations.AddField(
            model_name="joblisting",
            name="posted_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the job was posted on the source board (from JobSpy date_posted).",
                null=True,
            ),
        ),
    ]
