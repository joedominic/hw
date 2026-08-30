"""SaaS phase 2: plans, quotas, API keys, abuse, ownership, crypto rotation."""
from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from resume_app.api_keys import authenticate_api_key, generate_api_key, revoke_api_key
from resume_app.api_keys import authenticate_api_key, generate_api_key, revoke_api_key
from resume_app.models import Plan
from resume_app.crypto import decrypt_api_key, encrypt_api_key
from resume_app.subscriptions import (
    METRIC_JOB_SEARCHES,
    METRIC_LLM_REQUESTS,
    METRIC_LLM_TOKENS,
    QuotaExceeded,
    apply_stripe_event,
    assign_plan,
    check_quota,
    consume_quota,
    ensure_default_plans,
    get_user_plan,
    require_api_access,
    subscription_summary,
    EntitlementDenied,
)
from resume_app.ownership import OwnershipError, assert_same_owner
from resume_app.test_utils import create_user, login_client


class EntitlementQuotaTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("quota_user")
        assign_plan(self.user, "free")

    @override_settings(SAAS_ENFORCE_QUOTAS=True, LLM_USER_DAILY_REQUEST_LIMIT=0)
    def test_llm_quota_enforced(self):
        plan = get_user_plan(self.user)
        limit = plan.llm_requests_per_day
        for _ in range(limit):
            consume_quota(self.user, METRIC_LLM_REQUESTS, 1)
        with self.assertRaises(QuotaExceeded):
            consume_quota(self.user, METRIC_LLM_REQUESTS, 1)

    @override_settings(SAAS_ENFORCE_QUOTAS=True)
    def test_search_quota_enforced(self):
        assign_plan(self.user, "free")
        plan = get_user_plan(self.user)
        for _ in range(plan.job_searches_per_day):
            consume_quota(self.user, METRIC_JOB_SEARCHES, 1)
        with self.assertRaises(QuotaExceeded):
            check_quota(self.user, METRIC_JOB_SEARCHES)

    def test_unlimited_plan_allows_api(self):
        assign_plan(self.user, "unlimited")
        require_api_access(self.user)  # no raise

    def test_free_plan_denies_api(self):
        assign_plan(self.user, "free")
        with self.assertRaises(EntitlementDenied):
            require_api_access(self.user)

    @override_settings(SAAS_ENFORCE_QUOTAS=True, SAAS_STAFF_BYPASS_QUOTAS=False)
    def test_staff_does_not_bypass_quotas_by_default(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        assign_plan(self.user, "free")
        plan = get_user_plan(self.user)
        for _ in range(plan.job_searches_per_day):
            consume_quota(self.user, METRIC_JOB_SEARCHES, 1)
        with self.assertRaises(QuotaExceeded):
            consume_quota(self.user, METRIC_JOB_SEARCHES, 1)

    @override_settings(SAAS_ENFORCE_QUOTAS=True, SAAS_STAFF_BYPASS_QUOTAS=True)
    def test_staff_bypass_when_flag_enabled(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        assign_plan(self.user, "free")
        # Should not raise even past free limit
        for _ in range(5):
            consume_quota(self.user, METRIC_JOB_SEARCHES, 1)

    def test_past_due_falls_back_to_free_entitlements(self):
        from resume_app.subscriptions import get_or_create_subscription
        from resume_app.models import Subscription

        assign_plan(self.user, "pro")
        sub = get_or_create_subscription(self.user)
        sub.status = Subscription.Status.PAST_DUE
        sub.save(update_fields=["status"])
        self.assertEqual(get_user_plan(self.user).slug, "free")

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 1000, "pro": 5000, "unlimited": 0},
    )
    def test_subscription_summary_includes_token_quota(self):
        consume_quota(self.user, METRIC_LLM_TOKENS, 250)
        summary = subscription_summary(self.user)
        tok = summary["usage"][METRIC_LLM_TOKENS]
        self.assertEqual(tok["used"], 250)
        self.assertEqual(tok["limit"], 1000)
        self.assertEqual(tok["remaining"], 750)
        self.assertIn("token", tok["label"].lower())


class ApiKeyTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("api_user")
        assign_plan(self.user, "pro")

    def test_generate_authenticate_revoke(self):
        row, raw = generate_api_key(self.user, name="ci")
        self.assertTrue(raw.startswith("re_"))
        self.assertEqual(authenticate_api_key(raw), self.user)
        self.assertTrue(revoke_api_key(self.user, row.pk))
        self.assertIsNone(authenticate_api_key(raw))


class StripeWebhookTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("stripe_user")
        assign_plan(self.user, "free")

    def test_checkout_completed_assigns_plan(self):
        from resume_app.subscriptions import get_or_create_subscription

        sub = get_or_create_subscription(self.user)
        sub.stripe_customer_id = "cus_test"
        sub.save(update_fields=["stripe_customer_id"])
        event = {
            "id": "evt_test_1",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "customer": "cus_test",
                    "client_reference_id": str(self.user.pk),
                    "metadata": {"user_id": str(self.user.pk), "plan_slug": "pro"},
                    "subscription": "sub_test",
                }
            },
        }
        self.assertTrue(apply_stripe_event(event))
        self.assertFalse(apply_stripe_event(event))  # idempotent
        self.assertEqual(get_user_plan(self.user).slug, "pro")

    def test_payment_failed_sets_past_due(self):
        from resume_app.subscriptions import get_or_create_subscription
        from resume_app.models import Subscription

        assign_plan(self.user, "pro")
        sub = get_or_create_subscription(self.user)
        sub.stripe_customer_id = "cus_fail"
        sub.save(update_fields=["stripe_customer_id"])
        apply_stripe_event(
            {
                "id": "evt_pay_fail",
                "type": "invoice.payment_failed",
                "data": {"object": {"customer": "cus_fail"}},
            }
        )
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.PAST_DUE)
        self.assertEqual(get_user_plan(self.user).slug, "free")


class OwnershipTests(TestCase):
    def test_assert_same_owner(self):
        a = create_user("own_a")
        b = create_user("own_b")
        from resume_app.models import UserResume

        ra = UserResume.objects.create(owner=a, original_filename="a.pdf")
        rb = UserResume.objects.create(owner=b, original_filename="b.pdf")
        self.assertEqual(assert_same_owner(ra, ra), a.pk)
        with self.assertRaises(OwnershipError):
            assert_same_owner(ra, rb)


class CryptoRotationTests(TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        token = encrypt_api_key("sk-test-123")
        self.assertTrue(token)
        self.assertEqual(decrypt_api_key(token), "sk-test-123")


@override_settings(SAAS_ENFORCE_QUOTAS=True)
class StorageQuotaTests(TestCase):
    def setUp(self):
        ensure_default_plans()
        self.user = create_user("storage_user")
        from resume_app.models import Plan

        plan = Plan.objects.get(slug="free")
        plan.storage_mb = 1  # 1 MB hard cap for test
        plan.save(update_fields=["storage_mb"])
        assign_plan(self.user, "free")

    def test_upload_blocked_when_over_quota(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from resume_app.subscriptions import QuotaExceeded, assert_upload_allowed, check_storage_quota

        # Fill almost 1MB with a resume
        big = SimpleUploadedFile("big.pdf", b"x" * (900 * 1024), content_type="application/pdf")
        from resume_app.models import UserResume

        UserResume.objects.create(
            owner=self.user,
            file=big,
            original_filename="big.pdf",
            is_library=True,
        )
        check_storage_quota(self.user, additional_bytes=0)  # still under if exactly counted
        oversized = SimpleUploadedFile("more.pdf", b"y" * (200 * 1024), content_type="application/pdf")
        with self.assertRaises(QuotaExceeded):
            assert_upload_allowed(self.user, oversized)

    def test_unlimited_plan_allows_large_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from resume_app.subscriptions import assert_upload_allowed

        assign_plan(self.user, "unlimited")
        huge = SimpleUploadedFile("huge.pdf", b"z" * (2 * 1024 * 1024), content_type="application/pdf")
        assert_upload_allowed(self.user, huge)  # no raise


@override_settings(ABUSE_THROTTLE_ENABLED=True, ABUSE_AUTH_LIMIT=3, ABUSE_AUTH_WINDOW_SECONDS=60)
class AbuseThrottleTests(TestCase):
    def test_login_posts_throttled(self):
        client = Client()
        for _ in range(3):
            client.post(reverse("login"), {"username": "x", "password": "y"})
        resp = client.post(reverse("login"), {"username": "x", "password": "y"})
        self.assertEqual(resp.status_code, 429)


class BillingPageTests(TestCase):
    def test_billing_page_loads(self):
        ensure_default_plans()
        user = create_user("bill_user")
        client = Client()
        login_client(client, user)
        resp = client.get(reverse("billing"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Current plan")

    def test_billing_page_loads_admin_console_for_staff(self):
        ensure_default_plans()
        admin = create_user("bill_admin")
        admin.is_staff = True
        admin.save()

        client = Client()
        login_client(client, admin)
        resp = client.get(reverse("billing"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin Control Center")
        self.assertContains(resp, "Billing Plans &amp; Quotas")
        self.assertContains(resp, "Configured Commercial Plans")

    def test_admin_can_update_plan_limits_and_pricing(self):
        ensure_default_plans()
        admin = create_user("bill_admin2")
        admin.is_staff = True
        admin.save()

        free_plan = Plan.objects.get(slug="free")

        client = Client()
        login_client(client, admin)
        resp = client.post(
            reverse("billing"),
            {
                "action": "save_plan",
                "plan_id": free_plan.pk,
                "name": "Free Starter Plus",
                "price_display": "$0 forever",
                "description": "Updated starter plan",
                "stripe_price_id": "price_free_test",
                "llm_requests_per_day": "75",
                "llm_tokens_per_day": "150000",
                "job_searches_per_day": "35",
                "apply_runs_per_day": "10",
                "storage_mb": "300",
                "sort_order": "5",
                "is_active": "1",
                "is_default": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        free_plan.refresh_from_db()
        self.assertEqual(free_plan.name, "Free Starter Plus")
        self.assertEqual(free_plan.price_display, "$0 forever")
        self.assertEqual(free_plan.llm_requests_per_day, 75)
        self.assertEqual(free_plan.llm_tokens_per_day, 150000)
        self.assertEqual(free_plan.job_searches_per_day, 35)
        self.assertEqual(free_plan.apply_runs_per_day, 10)
        self.assertEqual(free_plan.storage_mb, 300)

        # Calling ensure_default_plans must NOT overwrite the admin's changes
        ensure_default_plans()
        free_plan.refresh_from_db()
        self.assertEqual(free_plan.name, "Free Starter Plus")
        self.assertEqual(free_plan.llm_requests_per_day, 75)

    def test_admin_can_create_new_plan(self):
        ensure_default_plans()
        admin = create_user("bill_admin3")
        admin.is_staff = True
        admin.save()

        client = Client()
        login_client(client, admin)
        resp = client.post(
            reverse("billing"),
            {
                "action": "create_plan",
                "name": "Enterprise Scale",
                "slug": "enterprise",
                "price_display": "$299 / month",
                "description": "High scale corporate plan",
                "stripe_price_id": "price_enterprise_live",
                "llm_requests_per_day": "2000",
                "llm_tokens_per_day": "5000000",
                "job_searches_per_day": "1000",
                "apply_runs_per_day": "200",
                "storage_mb": "20000",
                "sort_order": "40",
                "api_access": "1",
                "is_active": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        enterprise = Plan.objects.get(slug="enterprise")
        self.assertEqual(enterprise.name, "Enterprise Scale")
        self.assertEqual(enterprise.price_display, "$299 / month")
        self.assertEqual(enterprise.llm_requests_per_day, 2000)
        self.assertEqual(enterprise.api_access, True)
