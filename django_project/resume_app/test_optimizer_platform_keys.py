"""Optimizer platform-key fallback and Free token preflight."""
from django.test import TestCase, override_settings
from django.urls import reverse

from resume_app.llm import (
    estimate_optimizer_full_run_tokens,
    optimizer_token_preflight,
    resolve_optimizer_provider,
)
from resume_app.onboarding import seed_user_defaults
from resume_app.subscriptions import ensure_default_plans, get_user_plan
from resume_app.test_utils import TEST_PASSWORD, TENANT_TEST_MIDDLEWARE, create_user, login_client


@override_settings(MIDDLEWARE=TENANT_TEST_MIDDLEWARE, OPENAI_API_KEY="sk-platform-test")
class OptimizerPlatformKeyTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("opt_platform")
        seed_user_defaults(self.user)

    def test_resolve_optimizer_provider_uses_platform_when_no_byok(self):
        resolved = resolve_optimizer_provider(self.user, None)
        self.assertTrue(resolved["ready"])
        self.assertTrue(resolved["using_platform_key"])
        self.assertEqual(resolved["provider"], "OpenAI")

    def test_optimizer_page_ready_without_user_key(self):
        login_client(self.client, self.user)
        response = self.client.get(reverse("resume_optimizer"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["llm_key_stored"])
        self.assertTrue(response.context["using_platform_key"])
        self.assertContains(response, "Using HireEdge platform AI")

    def test_free_plan_daily_tokens_calibrated(self):
        plan = get_user_plan(self.user)
        self.assertEqual(plan.slug, "free")
        self.assertEqual(plan.llm_tokens_per_day, 50000)

    def test_token_preflight_estimate_positive(self):
        estimate = estimate_optimizer_full_run_tokens()
        self.assertGreater(estimate, 1000)
        preflight = optimizer_token_preflight(self.user)
        self.assertEqual(preflight["estimate"], estimate)
        self.assertEqual(preflight["limit"], 50000)
        self.assertFalse(preflight["blocked"])
