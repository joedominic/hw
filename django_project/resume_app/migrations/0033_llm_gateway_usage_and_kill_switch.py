# Generated manually for LLM gateway

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0032_llmproviderpreference_rate_limits"),
    ]

    operations = [
        migrations.AddField(
            model_name="appautomationsettings",
            name="stop_llm_requests",
            field=models.BooleanField(
                default=False,
                help_text="When set, the app will not send any LLM API requests (kill switch).",
            ),
        ),
        migrations.AddField(
            model_name="llmproviderpreference",
            name="rate_limit_cooldown_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="After rate limit or 429, skip this provider+model for this many seconds (empty = 300).",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="LLMAppUsageTotals",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("total_input_tokens", models.BigIntegerField(default=0)),
                ("total_output_tokens", models.BigIntegerField(default=0)),
                ("total_requests", models.PositiveIntegerField(default=0)),
                (
                    "total_estimated_invokes",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Calls where tokens were heuristic (provider did not report usage).",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "LLM app usage totals",
                "verbose_name_plural": "LLM app usage totals",
            },
        ),
        migrations.CreateModel(
            name="LLMUsageByModel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
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
                "verbose_name": "LLM usage by model",
                "verbose_name_plural": "LLM usage by model",
                "unique_together": {("provider", "model")},
            },
        ),
    ]
