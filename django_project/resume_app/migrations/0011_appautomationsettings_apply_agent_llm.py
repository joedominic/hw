from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0010_applicantprofile_atsautosubmitstats_sitecredential_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="appautomationsettings",
            name="apply_agent_llm_provider",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Dedicated LLM provider for browser-use generic form fill. Blank = global active provider.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="appautomationsettings",
            name="apply_agent_llm_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Model for the apply-agent LLM. Blank = provider default from Settings.",
                max_length=128,
            ),
        ),
    ]
