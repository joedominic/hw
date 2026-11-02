from django.db import migrations


def add_byok_rails_plan(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Plan.objects.update_or_create(
        slug="byok",
        defaults={
            "name": "BYOK Rails",
            "description": "Orchestration rails with your own API keys. Unlocked API, 2 GB storage, and Pro pipelines.",
            "price_display": "$15 / mo",
            "llm_requests_per_day": 0,
            "llm_tokens_per_day": 0,
            "job_searches_per_day": 15,
            "apply_runs_per_day": 5,
            "storage_mb": 2000,
            "api_access": True,
            "byok_required_for_completion": True,
            "is_active": True,
            "is_default": False,
            "sort_order": 15,
        },
    )


def rollback_add_byok_rails_plan(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Subscription = apps.get_model("resume_app", "Subscription")
    free_plan = Plan.objects.filter(slug="free").first() or Plan.objects.filter(is_default=True).first()
    if free_plan:
        Subscription.objects.filter(plan__slug="byok").update(plan=free_plan)
    Plan.objects.filter(slug="byok").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0066_remove_unlimited_plan"),
    ]

    operations = [
        migrations.RunPython(add_byok_rails_plan, rollback_add_byok_rails_plan),
    ]
