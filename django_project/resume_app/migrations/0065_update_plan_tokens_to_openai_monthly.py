from django.db import migrations


def update_plan_daily_tokens(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")

    Plan.objects.filter(slug="free").update(
        llm_tokens_per_day=8300,
        description="250,000 OpenAI tokens a month to try tailoring and Skill Radar.",
    )
    Plan.objects.filter(slug="pro").update(
        llm_tokens_per_day=167000,
    )
    Plan.objects.filter(slug="power").update(
        llm_tokens_per_day=500000,
    )
    Plan.objects.filter(slug="unlimited").update(
        llm_tokens_per_day=500000,
    )


def rollback_plan_daily_tokens(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")

    Plan.objects.filter(slug="free").update(
        llm_tokens_per_day=50000,
        description="Starter limits for exploring the platform. Local models unmetered.",
    )
    Plan.objects.filter(slug="pro").update(
        llm_tokens_per_day=2000000,
    )
    Plan.objects.filter(slug="power").update(
        llm_tokens_per_day=10000000,
    )
    Plan.objects.filter(slug="unlimited").update(
        llm_tokens_per_day=10000000,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0064_plan_byok_and_token_calibration"),
    ]

    operations = [
        migrations.RunPython(update_plan_daily_tokens, rollback_plan_daily_tokens),
    ]
