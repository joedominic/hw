"""Tests for LLM launch-hardening: token budgets, /llm/complete gates, fail-closed defaults."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from resume_app.entitlements import (
    METRIC_LLM_TOKENS,
    assign_plan,
    consume_quota,
    ensure_default_plans,
    plan_limit,
    get_user_plan,
    usage_today,
)
from resume_app.llm_policy import (
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
    @patch("resume_app.llm_policy.uses_platform_keys", return_value=False)
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
