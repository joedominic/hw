# Generated manually for LLM usage breakdown by query kind

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0033_llm_gateway_usage_and_kill_switch"),
    ]

    operations = [
        migrations.CreateModel(
            name="LLMUsageByQuery",
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
                ("query_kind", models.CharField(db_index=True, max_length=64)),
                ("provider", models.CharField(db_index=True, max_length=64)),
                (
                    "model",
                    models.CharField(
                        db_index=True,
                        help_text="Resolved model name, or __default__ when empty.",
                        max_length=128,
                    ),
                ),
                ("request_count", models.PositiveIntegerField(default=0)),
                ("sum_input_tokens", models.BigIntegerField(default=0)),
                ("sum_output_tokens", models.BigIntegerField(default=0)),
                ("sum_cached_tokens", models.BigIntegerField(default=0)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "LLM usage by query",
                "verbose_name_plural": "LLM usage by query",
                "unique_together": {("query_kind", "provider", "model")},
            },
        ),
    ]
