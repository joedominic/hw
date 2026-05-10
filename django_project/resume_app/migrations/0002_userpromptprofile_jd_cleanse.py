from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpromptprofile",
            name="jd_cleanse",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="jd_cleanse_system",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="userpromptprofile",
            name="jd_cleanse_user",
            field=models.TextField(blank=True),
        ),
    ]
