"""Raise Free plan daily LLM token cap from 8,300 to 50,000.

Preserves staff-customized free-plan token values above the legacy default.
"""

from django.db import migrations


LEGACY_FREE_TOKENS = 8300
NEW_FREE_TOKENS = 50000
NEW_FREE_DESCRIPTION = (
    "1.5M OpenAI tokens a month (~50k/day) to try tailoring and Skill Radar."
)


def raise_free_tokens(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Plan.objects.filter(slug="free", llm_tokens_per_day=LEGACY_FREE_TOKENS).update(
        llm_tokens_per_day=NEW_FREE_TOKENS,
        description=NEW_FREE_DESCRIPTION,
    )


def revert_free_tokens(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Plan.objects.filter(slug="free", llm_tokens_per_day=NEW_FREE_TOKENS).update(
        llm_tokens_per_day=LEGACY_FREE_TOKENS,
        description="250,000 OpenAI tokens a month to try tailoring and Skill Radar.",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0069_backfill_past_due_since"),
    ]

    operations = [
        migrations.RunPython(raise_free_tokens, revert_free_tokens),
    ]
