from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0009_dealbreakers"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserDisqualifier",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phrase", models.CharField(max_length=500, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["phrase"],
            },
        ),
    ]
