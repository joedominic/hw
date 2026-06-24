from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0012_appautomationsettings_apply_browser_show_window"),
    ]

    operations = [
        migrations.AddField(
            model_name="optimizedresume",
            name="cover_letter",
            field=models.TextField(
                blank=True,
                default="",
                help_text="On-demand generated cover letter for this job optimization.",
            ),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="cover_letter_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pipelineentry",
            name="interview_prep",
            field=models.TextField(
                blank=True,
                default="",
                help_text="On-demand generated interview prep (JSON or markdown) for Done-stage jobs.",
            ),
        ),
        migrations.AddField(
            model_name="pipelineentry",
            name="interview_prep_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="cover_letter",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="cover_letter_system",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="cover_letter_user",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="interview_prep",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="interview_prep_system",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="interview_prep_user",
            field=models.TextField(blank=True),
        ),
    ]
