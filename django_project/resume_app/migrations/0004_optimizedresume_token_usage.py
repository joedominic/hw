from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0003_llmproviderconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="optimizedresume",
            name="total_input_tokens",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="optimizedresume",
            name="total_output_tokens",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
