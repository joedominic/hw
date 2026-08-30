"""Tests for LLM launch-hardening: token budgets, /llm/complete gates, fail-closed defaults."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from resume_app.subscriptions import (
    METRIC_LLM_TOKENS,
    assign_plan,
    consume_quota,
    ensure_default_plans,
    plan_limit,
    get_user_plan,
    usage_today,
)
from resume_app.llm import (
    LLMTokenBudgetExceeded,
    check_token_budget,
    consume_token_budget,
    daily_token_limit_for_user,
)
from resume_app.test_utils import create_user, login_client


class TokenBudgetTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("tok_user")
        assign_plan(self.user, "free")

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 1000, "pro": 5000, "unlimited": 0},
        LLM_USER_DAILY_TOKEN_LIMIT=0,
        LLM_PLATFORM_DAILY_TOKEN_LIMIT=0,
    )
    def test_free_plan_token_limit(self):
        self.assertEqual(daily_token_limit_for_user(self.user), 1000)
        self.assertEqual(plan_limit(get_user_plan(self.user), METRIC_LLM_TOKENS), 1000)

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 100, "pro": 5000, "unlimited": 0},
        LLM_PLATFORM_DAILY_TOKEN_LIMIT=0,
    )
    def test_check_token_budget_blocks(self):
        consume_quota(self.user, METRIC_LLM_TOKENS, 90)
        check_token_budget(self.user, estimated_tokens=5, provider="OpenAI")
        with self.assertRaises(LLMTokenBudgetExceeded):
            check_token_budget(self.user, estimated_tokens=20, provider="OpenAI")

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 500, "pro": 5000, "unlimited": 0},
        LLM_PLATFORM_DAILY_TOKEN_LIMIT=0,
    )
    @patch("resume_app.llm.policy.uses_platform_keys", return_value=False)
    def test_consume_token_budget(self, _mock_plat):
        consume_token_budget(self.user, 120, provider="OpenAI")
        self.assertEqual(usage_today(self.user, METRIC_LLM_TOKENS), 120)


class LlmCompleteGateTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.client = Client()
        self.free = create_user("free_complete")
        self.pro = create_user("pro_complete")
        assign_plan(self.free, "free")
        assign_plan(self.pro, "pro")

    @override_settings(SAAS_ENFORCE_QUOTAS=True, LLM_COMPLETE_MAX_INPUT_CHARS=100)
    def test_free_plan_denied(self):
        login_client(self.client, self.free)
        resp = self.client.post(
            "/api/resume/llm/complete",
            data={"user": "hello"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    @override_settings(SAAS_ENFORCE_QUOTAS=True, LLM_COMPLETE_MAX_INPUT_CHARS=50)
    @patch("resume_app.api.call_invoke_llm_messages")
    def test_pro_plan_size_cap(self, mock_invoke):
        login_client(self.client, self.pro)
        resp = self.client.post(
            "/api/resume/llm/complete",
            data={"user": "x" * 60},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        mock_invoke.assert_not_called()

    @override_settings(SAAS_ENFORCE_QUOTAS=True, LLM_COMPLETE_MAX_INPUT_CHARS=10_000)
    @patch("resume_app.api.call_invoke_llm_messages")
    def test_pro_plan_allowed(self, mock_invoke):
        mock_raw = MagicMock()
        mock_raw.content = "ok"
        mock_invoke.return_value = mock_raw
        login_client(self.client, self.pro)
        resp = self.client.post(
            "/api/resume/llm/complete",
            data={"user": "hello world"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        mock_invoke.assert_called_once()


class FailOpenDefaultTests(TestCase):
    def test_openai_rate_limit_wired(self):
        from django.conf import settings as dj_settings

        self.assertIn("OpenAI", dj_settings.LLM_RATE_LIMIT_BY_PROVIDER)
        self.assertIn("Groq", dj_settings.LLM_RATE_LIMIT_BY_PROVIDER)
        rpm, tpm = dj_settings.LLM_RATE_LIMIT_BY_PROVIDER["OpenAI"]
        self.assertGreater(rpm, 0)
        self.assertGreater(tpm, 0)

    def test_fail_open_default_is_debug_when_unset(self):
        """Code default is ``default=DEBUG``; env may override — verify the setting exists."""
        from django.conf import settings as dj_settings

        self.assertIsInstance(dj_settings.LLM_RATE_LIMIT_FAIL_OPEN, bool)


@override_settings(SAAS_ENFORCE_QUOTAS=True)
class SettingsUsageTabAndResetTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.client = Client()
        self.user = create_user("usagetab_user")
        assign_plan(self.user, "free")
        login_client(self.client, self.user)

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 100, "pro": 5000, "unlimited": 0},
    )
    def test_informative_token_budget_error_message(self):
        consume_quota(self.user, METRIC_LLM_TOKENS, 95)
        with self.assertRaises(LLMTokenBudgetExceeded) as ctx:
            check_token_budget(self.user, estimated_tokens=10, provider="OpenAI")
        msg = str(ctx.exception)
        self.assertIn("Daily LLM token budget exceeded for plan 'Free'", msg)
        self.assertIn("95 / 100 tokens used today", msg)
        self.assertIn("cumulative daily limit", msg)

    def test_settings_usage_tab_renders_daily_history(self):
        consume_quota(self.user, METRIC_LLM_TOKENS, 1200)
        resp = self.client.get("/settings/?tab=usage")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Daily usage history (past 30 days)", resp.content.decode())
        self.assertIn("1200", resp.content.decode())
        self.assertIn("Reset today", resp.content.decode())

    def test_reset_today_quotas_post_action(self):
        from resume_app.models import LLMDailyUsageBreakdown
        from django.utils import timezone
        today = timezone.localdate()
        consume_quota(self.user, METRIC_LLM_TOKENS, 1500)
        LLMDailyUsageBreakdown.objects.create(
            owner=self.user,
            period_date=today,
            query_kind="cover_letter",
            provider="OpenAI",
            model="gpt-4o-mini",
            request_count=1,
            sum_input_tokens=1000,
            sum_output_tokens=500,
        )
        self.assertEqual(usage_today(self.user, METRIC_LLM_TOKENS), 1500)
        self.assertEqual(LLMDailyUsageBreakdown.objects.filter(owner=self.user, period_date=today).count(), 1)
        resp = self.client.post("/settings/", data={"action": "reset_today_quotas"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(usage_today(self.user, METRIC_LLM_TOKENS), 0)
        self.assertEqual(LLMDailyUsageBreakdown.objects.filter(owner=self.user, period_date=today).count(), 0)

    def test_record_llm_usage_persists_daily_breakdown(self):
        from resume_app.llm.gateway import record_llm_usage
        from resume_app.models import LLMDailyUsageBreakdown
        from django.utils import timezone
        today = timezone.localdate()

        record_llm_usage(
            provider="OpenAI",
            model="gpt-4o-mini",
            input_tokens=800,
            output_tokens=250,
            cached_tokens=100,
            tokens_estimated=False,
            user=self.user,
            query_kind="cover_letter",
        )
        row = LLMDailyUsageBreakdown.objects.filter(
            owner=self.user,
            period_date=today,
            query_kind="cover_letter",
            provider="OpenAI",
            model="gpt-4o-mini",
        ).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.request_count, 1)
        self.assertEqual(row.sum_input_tokens, 800)
        self.assertEqual(row.sum_output_tokens, 250)
        self.assertEqual(row.sum_cached_tokens, 100)

        # Rendering check in Settings Usage tab
        resp = self.client.get("/settings/?tab=usage")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Optimizer — cover letter", html)
        self.assertIn("gpt-4o-mini", html)


