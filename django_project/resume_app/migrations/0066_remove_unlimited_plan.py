from django.db import migrations


def remove_unlimited_plan(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Subscription = apps.get_model("resume_app", "Subscription")

    power_plan = Plan.objects.filter(slug="power").first() or Plan.objects.filter(is_default=True).first()
    if power_plan:
        Subscription.objects.filter(plan__slug="unlimited").update(plan=power_plan)
    Plan.objects.filter(slug="unlimited").delete()


def rollback_remove_unlimited_plan(apps, schema_editor):
    # No-op since unlimited plan was deprecated and replaced by power plan
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0065_update_plan_tokens_to_openai_monthly"),
    ]

    operations = [
        migrations.RunPython(remove_unlimited_plan, rollback_remove_unlimited_plan),
    ]
