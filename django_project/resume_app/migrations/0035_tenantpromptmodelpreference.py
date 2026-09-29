import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('resume_app', '0034_llmdailyusagebreakdown'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantPromptModelPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('query_kind', models.CharField(help_text="Query kind e.g. 'optimizer_writer', 'matching', or '__default__' for tenant global default.", max_length=64)),
                ('provider', models.CharField(max_length=64)),
                ('model', models.CharField(blank=True, max_length=128)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='prompt_model_preferences', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Tenant Prompt Model Preference',
                'verbose_name_plural': 'Tenant Prompt Model Preferences',
                'unique_together': {('owner', 'query_kind')},
            },
        ),
    ]
