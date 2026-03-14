# Generated manually for plan implementation

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="optimizedresume",
            name="status",
            field=models.CharField(default="queued", max_length=255),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="status_display",
            field=models.CharField(blank=True, help_text="Human-readable progress, e.g. 'Drafting iteration 1'", max_length=255),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="error_message",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="ats_score",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="recruiter_score",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
