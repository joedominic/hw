from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("resume_app", "0028_llmproviderconfig_priority_active"),
    ]

    operations = [
        migrations.CreateModel(
            name="LLMProviderPreference",
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
                ("model", models.CharField(blank=True, max_length=128)),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider_config",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="preference_rows",
                        to="resume_app.llmproviderconfig",
                    ),
                ),
            ],
            options={"ordering": ["priority", "id"]},
        ),
    ]
