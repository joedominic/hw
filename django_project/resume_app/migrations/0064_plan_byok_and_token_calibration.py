from django.db import migrations, models


def calibrate_and_backfill_plans(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Subscription = apps.get_model("resume_app", "Subscription")

    # Update Free
    Plan.objects.filter(slug="free").update(
        llm_requests_per_day=50,
        llm_tokens_per_day=50000,
        job_searches_per_day=5,
        apply_runs_per_day=2,
        storage_mb=250,
        api_access=False,
        byok_required_for_completion=False,
        description="Starter limits for exploring the platform. Local models unmetered.",
        is_active=True,
    )

    # Update Pro
    Plan.objects.filter(slug="pro").update(
        llm_requests_per_day=250,
        llm_tokens_per_day=2000000,
        job_searches_per_day=25,
        apply_runs_per_day=10,
        storage_mb=5000,
        api_access=True,
        byok_required_for_completion=True,
        description="High-velocity active search for serious job seekers.",
        is_active=True,
    )

    # Update Power
    power_plan = Plan.objects.filter(slug="power").first()
    if power_plan:
        Plan.objects.filter(slug="power").update(
            llm_requests_per_day=1000,
            llm_tokens_per_day=10000000,
            job_searches_per_day=100,
            apply_runs_per_day=40,
            storage_mb=20000,
            api_access=True,
            byok_required_for_completion=False,
            description="Maximum throughput for power candidates, coaches, and consultants.",
            is_active=True,
        )

    # Deactivate legacy Unlimited
    Plan.objects.filter(slug="unlimited").update(
        is_active=False,
        byok_required_for_completion=False,
    )

    # Reassign any active subscriptions from Unlimited to Power
    if power_plan:
        Subscription.objects.filter(plan__slug="unlimited").update(plan=power_plan)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0063_recalibrate_saas_plans"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="byok_required_for_completion",
            field=models.BooleanField(
                default=False,
                help_text="Require caller to provide BYOK credentials for headless LLM completion.",
            ),
        ),
        migrations.RunPython(calibrate_and_backfill_plans, reverse_code=noop_reverse),
    ]
