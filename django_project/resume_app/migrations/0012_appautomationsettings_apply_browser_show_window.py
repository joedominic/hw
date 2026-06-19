from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0011_appautomationsettings_apply_agent_llm"),
    ]

    operations = [
        migrations.AddField(
            model_name="appautomationsettings",
            name="apply_browser_show_window",
            field=models.BooleanField(
                default=False,
                help_text="When True, show a visible Chromium window during apply-agent browser steps (dev; requires Huey on this machine).",
            ),
        ),
    ]
