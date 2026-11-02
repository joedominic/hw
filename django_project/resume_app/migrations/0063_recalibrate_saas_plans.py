# Generated for commercial plan recalibration & quota guardrails

from django.db import migrations


def recalibrate_plans(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")

    calibrated_plans = [
        {
            "slug": "free",
            "name": "Free",
            "description": "Starter limits for exploring the platform. Local Ollama LLMs unmetered.",
            "price_display": "$0 / mo",
            "llm_requests_per_day": 15,
            "llm_tokens_per_day": 0,  # Uses LLM_DAILY_TOKEN_LIMIT_BY_PLAN["free"] (50,000)
            "job_searches_per_day": 5,
            "apply_runs_per_day": 2,
            "storage_mb": 250,
            "api_access": False,
            "is_default": True,
            "sort_order": 10,
        },
        {
            "slug": "pro",
            "name": "Pro",
            "description": "High-velocity active search for serious job seekers.",
            "price_display": "$29 / mo",
            "llm_requests_per_day": 250,
            "llm_tokens_per_day": 0,  # Uses LLM_DAILY_TOKEN_LIMIT_BY_PLAN["pro"] (2,000,000)
            "job_searches_per_day": 25,
            "apply_runs_per_day": 10,
            "storage_mb": 5000,
            "api_access": True,
            "is_default": False,
            "sort_order": 20,
        },
        {
            "slug": "power",
            "name": "Power",
            "description": "Maximum throughput for power candidates, coaches, and consultants.",
            "price_display": "$99 / mo",
            "llm_requests_per_day": 1000,
            "llm_tokens_per_day": 0,  # Uses LLM_DAILY_TOKEN_LIMIT_BY_PLAN["power"] (10,000,000)
            "job_searches_per_day": 100,
            "apply_runs_per_day": 40,
            "storage_mb": 20000,
            "api_access": True,
            "is_default": False,
            "sort_order": 30,
        },
        {
            "slug": "unlimited",
            "name": "Unlimited",
            "description": "Legacy unlimited tier (fair-use capped).",
            "price_display": "$99 / mo",
            "llm_requests_per_day": 1000,
            "llm_tokens_per_day": 0,  # Uses LLM_DAILY_TOKEN_LIMIT_BY_PLAN["unlimited"] (10,000,000)
            "job_searches_per_day": 100,
            "apply_runs_per_day": 40,
            "storage_mb": 20000,
            "api_access": True,
            "is_default": False,
            "sort_order": 35,
        },
    ]

    for plan_data in calibrated_plans:
        slug = plan_data["slug"]
        Plan.objects.update_or_create(slug=slug, defaults=plan_data)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0062_alter_joblisting_fetched_at"),
    ]

    operations = [
        migrations.RunPython(recalibrate_plans, reverse_code=noop_reverse),
    ]
