# Generated manually for SaaS plans, quotas, API keys, Stripe events.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_plans_and_subscriptions(apps, schema_editor):
    Plan = apps.get_model("resume_app", "Plan")
    Subscription = apps.get_model("resume_app", "Subscription")
    User = apps.get_model(settings.AUTH_USER_MODEL)

    defaults = [
        {
            "slug": "free",
            "name": "Free",
            "description": "Starter limits for individual job seekers.",
            "llm_requests_per_day": 50,
            "job_searches_per_day": 20,
            "apply_runs_per_day": 5,
            "storage_mb": 250,
            "api_access": False,
            "is_default": True,
            "sort_order": 10,
            "is_active": True,
        },
        {
            "slug": "pro",
            "name": "Pro",
            "description": "Higher limits plus customer API access.",
            "llm_requests_per_day": 500,
            "job_searches_per_day": 200,
            "apply_runs_per_day": 50,
            "storage_mb": 5000,
            "api_access": True,
            "is_default": False,
            "sort_order": 20,
            "is_active": True,
        },
        {
            "slug": "unlimited",
            "name": "Unlimited",
            "description": "No daily quotas (0 = unlimited).",
            "llm_requests_per_day": 0,
            "job_searches_per_day": 0,
            "apply_runs_per_day": 0,
            "storage_mb": 0,
            "api_access": True,
            "is_default": False,
            "sort_order": 30,
            "is_active": True,
        },
    ]
    for row in defaults:
        Plan.objects.update_or_create(slug=row["slug"], defaults=row)

    free = Plan.objects.filter(slug="free").first()
    if free:
        for user in User.objects.all().iterator():
            Subscription.objects.get_or_create(
                owner=user,
                defaults={"plan_id": free.id, "status": "active"},
            )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("resume_app", "0022_account_email_verification"),
    ]

    operations = [
        migrations.CreateModel(
            name="Plan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=128)),
                ("description", models.TextField(blank=True, default="")),
                ("llm_requests_per_day", models.PositiveIntegerField(default=50, help_text="Daily LLM invoke cap. 0 = unlimited.")),
                ("job_searches_per_day", models.PositiveIntegerField(default=20, help_text="Daily job-search runs. 0 = unlimited.")),
                ("apply_runs_per_day", models.PositiveIntegerField(default=5, help_text="Daily apply-agent attempt starts. 0 = unlimited.")),
                ("storage_mb", models.PositiveIntegerField(default=250, help_text="Soft storage budget in MB. 0 = unlimited (enforcement optional).")),
                ("api_access", models.BooleanField(default=False)),
                ("stripe_price_id", models.CharField(blank=True, default="", max_length=128)),
                ("is_active", models.BooleanField(default=True)),
                ("is_default", models.BooleanField(default=False)),
                ("sort_order", models.PositiveSmallIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="StripeWebhookEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=128, unique=True)),
                ("event_type", models.CharField(blank=True, default="", max_length=128)),
                ("processed_at", models.DateTimeField(auto_now_add=True)),
                ("payload", models.JSONField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("trialing", "Trialing"), ("active", "Active"), ("past_due", "Past due"), ("canceled", "Canceled"), ("unpaid", "Unpaid")], default="active", max_length=16)),
                ("stripe_customer_id", models.CharField(blank=True, default="", max_length=128)),
                ("stripe_subscription_id", models.CharField(blank=True, default="", max_length=128)),
                ("trial_ends_at", models.DateTimeField(blank=True, null=True)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="subscription", to=settings.AUTH_USER_MODEL)),
                ("plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="subscriptions", to="resume_app.plan")),
            ],
            options={"verbose_name": "Subscription", "verbose_name_plural": "Subscriptions"},
        ),
        migrations.CreateModel(
            name="UsageCounter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("metric", models.CharField(db_index=True, max_length=64)),
                ("period_date", models.DateField(db_index=True)),
                ("count", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_counters", to=settings.AUTH_USER_MODEL)),
            ],
            options={"unique_together": {("owner", "metric", "period_date")}},
        ),
        migrations.CreateModel(
            name="CustomerApiKey",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, default="", max_length=128)),
                ("prefix", models.CharField(db_index=True, max_length=16)),
                ("key_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="api_keys", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="usagecounter",
            index=models.Index(fields=["owner", "period_date"], name="resume_app__owner_i_7c1a0d_idx"),
        ),
        migrations.RunPython(seed_plans_and_subscriptions, migrations.RunPython.noop),
    ]
