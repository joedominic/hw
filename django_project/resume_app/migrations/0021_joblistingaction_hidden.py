from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0020_search_profile_phase1"),
    ]

    operations = [
        migrations.AlterField(
            model_name="joblistingaction",
            name="action",
            field=models.CharField(
                choices=[
                    ("liked", "Liked"),
                    ("disliked", "Disliked"),
                    ("hidden", "Hidden"),
                    ("saved", "Saved"),
                ],
                max_length=16,
            ),
        ),
    ]
