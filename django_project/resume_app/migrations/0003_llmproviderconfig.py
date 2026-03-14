# LLM Provider Config for storing API keys and default model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0002_add_optimized_resume_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="LLMProviderConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(max_length=64, unique=True)),
                ("encrypted_api_key", models.TextField(blank=True)),
                ("default_model", models.CharField(blank=True, max_length=128)),
                ("last_validated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "LLM Provider Config",
                "verbose_name_plural": "LLM Provider Configs",
            },
        ),
    ]
