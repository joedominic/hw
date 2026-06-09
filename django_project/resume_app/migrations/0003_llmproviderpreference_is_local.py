from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0002_userpromptprofile_jd_cleanse"),
    ]

    operations = [
        migrations.AddField(
            model_name="llmproviderpreference",
            name="is_local",
            field=models.BooleanField(
                default=False,
                help_text="If true, this provider+model is treated as local (prioritized or required).",
            ),
        ),
    ]
