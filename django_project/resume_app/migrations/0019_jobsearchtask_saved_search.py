from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0018_saved_job_search"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobsearchtask",
            name="saved_search",
            field=models.OneToOneField(
                blank=True,
                help_text="When set, this task runs the linked saved search configuration.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="scheduled_task",
                to="resume_app.savedjobsearch",
            ),
        ),
    ]
