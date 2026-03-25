from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0026_userpromptprofile"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppAutomationSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("pipeline_to_vetting_enabled", models.BooleanField(default=False)),
                (
                    "pipeline_preference_margin_min",
                    models.IntegerField(
                        default=0,
                        help_text="Promote Pipeline → Vetting when Pref margin (same as pipeline badge) is >= this value.",
                    ),
                ),
                ("vetting_to_applying_enabled", models.BooleanField(default=False)),
                (
                    "vetting_interview_probability_min",
                    models.PositiveSmallIntegerField(
                        default=70,
                        help_text="Promote Vetting → Applying when interview probability is >= this (0–100).",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "App automation settings",
                "verbose_name_plural": "App automation settings",
            },
        ),
    ]
