"""SaaS phase 2: plans, quotas, API keys, abuse, ownership, crypto rotation."""
from __future__ import annotations

from django.test import Client, TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from resume_app.api_keys import authenticate_api_key, generate_api_key, revoke_api_key
from resume_app.api_keys import authenticate_api_key, generate_api_key, revoke_api_key
from resume_app.models import Plan, Subscription, UsageCounter
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
    get_or_create_subscription,
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

    def test_power_plan_allows_api(self):
        assign_plan(self.user, "power")
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

    def test_past_due_grace_and_unpaid_degradation(self):
        from resume_app.subscriptions import get_or_create_subscription
        from resume_app.models import Subscription

        assign_plan(self.user, "pro")
        sub = get_or_create_subscription(self.user)
        # In past_due status, user enters grace period while Stripe retries and retains paid plan
        sub.status = Subscription.Status.PAST_DUE
        sub.save(update_fields=["status"])
        self.assertEqual(get_user_plan(self.user).slug, "pro")

        # When payment retries expire and status becomes unpaid or canceled, degraded to free
        sub.status = Subscription.Status.UNPAID
        sub.save(update_fields=["status"])
        self.assertEqual(get_user_plan(self.user).slug, "free")

        sub.status = Subscription.Status.CANCELED
        sub.save(update_fields=["status"])
        self.assertEqual(get_user_plan(self.user).slug, "free")

    @override_settings(
        SAAS_ENFORCE_QUOTAS=True,
        LLM_DAILY_TOKEN_LIMIT_BY_PLAN={"free": 1000, "pro": 5000, "power": 0},
    )
    def test_subscription_summary_includes_token_quota(self):
        free_p = get_user_plan(self.user)
        free_p.llm_tokens_per_day = 0
        free_p.save()
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
        self.assertIsNotNone(sub.past_due_since)
        # Retains paid entitlements in grace period while Stripe retries
        self.assertEqual(get_user_plan(self.user).slug, "pro")

    def test_past_due_grace_expires_after_five_days(self):
        from datetime import timedelta
        from django.utils import timezone
        from resume_app.subscriptions import get_or_create_subscription, get_user_plan
        from resume_app.models import Subscription

        assign_plan(self.user, "pro")
        sub = get_or_create_subscription(self.user)
        sub.status = Subscription.Status.PAST_DUE
        sub.save()
        # Fast-forward 6 days into the past (both past_due_since and updated_at)
        Subscription.objects.filter(pk=sub.pk).update(
            past_due_since=timezone.now() - timedelta(days=6),
            updated_at=timezone.now() - timedelta(days=6),
        )
        sub.refresh_from_db()
        self.assertEqual(get_user_plan(self.user).slug, "free")

    def test_subscription_deleted_retains_plan_and_stripe_id(self):
        from resume_app.subscriptions import get_or_create_subscription, get_user_plan
        from resume_app.models import Subscription

        assign_plan(self.user, "pro")
        sub = get_or_create_subscription(self.user)
        sub.stripe_customer_id = "cus_del"
        sub.stripe_subscription_id = "sub_del_123"
        sub.save()

        apply_stripe_event(
            {
                "id": "evt_del_1",
                "type": "customer.subscription.deleted",
                "data": {"object": {"customer": "cus_del"}},
            }
        )
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.CANCELED)
        # Plan FK and Stripe ID are preserved for portal recovery
        self.assertEqual(sub.plan.slug, "pro")
        self.assertEqual(sub.stripe_subscription_id, "sub_del_123")
        # Entitlements resolve to Free
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

    def test_power_plan_allows_large_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from resume_app.subscriptions import assert_upload_allowed

        assign_plan(self.user, "power")
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

    def test_admin_can_preview_candidate_billing_view(self):
        ensure_default_plans()
        admin = create_user("bill_admin_preview")
        admin.is_staff = True
        admin.save()

        client = Client()
        login_client(client, admin)
        resp = client.get(reverse("billing") + "?view=candidate")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Viewing Candidate Billing Experience")
        self.assertContains(resp, "Return to Admin Control Center")

    def test_admin_can_update_user_subscription_and_reset_quotas(self):
        ensure_default_plans()
        admin = create_user("bill_admin_sub_mgr")
        admin.is_staff = True
        admin.save()

        candidate = create_user("sub_candidate_test")
        sub = get_or_create_subscription(candidate)
        self.assertEqual(sub.plan.slug, "free")
        self.assertEqual(sub.status, "active")

        # Create usage counter today
        UsageCounter.objects.create(
            owner=candidate,
            metric=METRIC_LLM_REQUESTS,
            period_date=timezone.localdate(),
            count=45,
        )

        client = Client()
        login_client(client, admin)

        # 1. Update subscription to Pro and trialing with 14 trial days
        resp = client.post(
            reverse("billing"),
            {
                "action": "update_user_subscription",
                "subscription_id": sub.pk,
                "plan_slug": "pro",
                "status": "trialing",
                "trial_days": "14",
            },
        )
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.plan.slug, "pro")
        self.assertEqual(sub.status, "trialing")
        self.assertIsNotNone(sub.trial_ends_at)

        # 2. Reset quotas for candidate
        resp2 = client.post(
            reverse("billing"),
            {
                "action": "reset_user_quotas",
                "user_id": candidate.pk,
            },
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertFalse(
            UsageCounter.objects.filter(
                owner=candidate,
                metric=METRIC_LLM_REQUESTS,
                period_date=timezone.localdate(),
            ).exists()
        )

    def test_staff_user_action_set_sub_status_and_reset_quotas(self):
        ensure_default_plans()
        staff = create_user("staff_support_agent")
        staff.is_staff = True
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename="can_impersonate_users")
        staff.user_permissions.add(perm)
        staff.save()

        candidate = create_user("target_for_staff")
        sub = get_or_create_subscription(candidate)

        UsageCounter.objects.create(
            owner=candidate,
            metric=METRIC_JOB_SEARCHES,
            period_date=timezone.localdate(),
            count=10,
        )

        client = Client()
        login_client(client, staff)

        # Update status to past_due
        resp = client.post(
            reverse("staff_user_action", kwargs={"user_id": candidate.pk}),
            {"action": "set_sub_status", "sub_status": "past_due"},
        )
        self.assertEqual(resp.status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "past_due")

        # Reset quotas
        resp2 = client.post(
            reverse("staff_user_action", kwargs={"user_id": candidate.pk}),
            {"action": "reset_quotas"},
        )
        self.assertEqual(resp2.status_code, 302)
        self.assertFalse(
            UsageCounter.objects.filter(
                owner=candidate,
                metric=METRIC_JOB_SEARCHES,
                period_date=timezone.localdate(),
            ).exists()
        )

    def test_subscription_and_plan_admin_actions(self):
        ensure_default_plans()
        from resume_app.admin import SubscriptionAdmin, PlanAdmin
        from django.contrib.admin.sites import AdminSite

        site = AdminSite()
        sub_admin = SubscriptionAdmin(Subscription, site)
        plan_admin = PlanAdmin(Plan, site)

        u1 = create_user("admin_sub_u1")
        sub1 = get_or_create_subscription(u1)
        sub1.status = "canceled"
        sub1.save()

        UsageCounter.objects.create(
            owner=u1,
            metric=METRIC_LLM_REQUESTS,
            period_date=timezone.localdate(),
            count=20,
        )

        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        rf = RequestFactory()
        req = rf.get("/admin/")
        req.user = u1
        setattr(req, "session", {})
        messages_storage = FallbackStorage(req)
        setattr(req, "_messages", messages_storage)

        # Admin action assign pro
        sub_admin.assign_pro_plan(req, Subscription.objects.filter(pk=sub1.pk))
        sub1.refresh_from_db()
        self.assertEqual(sub1.plan.slug, "pro")

        # Admin action mark active
        sub_admin.mark_active(req, Subscription.objects.filter(pk=sub1.pk))
        sub1.refresh_from_db()
        self.assertEqual(sub1.status, "active")

        # Admin action reset quotas
        sub_admin.reset_quotas_today(req, Subscription.objects.filter(pk=sub1.pk))
        self.assertFalse(
            UsageCounter.objects.filter(
                owner=u1,
                period_date=timezone.localdate(),
            ).exists()
        )

        # Plan admin action deactivate and activate
        free_plan = Plan.objects.filter(slug="free")
        plan_admin.deactivate_plans(req, free_plan)
        self.assertFalse(Plan.objects.get(slug="free").is_active)

        plan_admin.activate_plans(req, free_plan)
        self.assertTrue(Plan.objects.get(slug="free").is_active)

    def test_past_due_since_tracks_grace_clock_even_if_updated_at_resets(self):
        from datetime import timedelta
        from django.utils import timezone
        from resume_app.subscriptions import get_or_create_subscription, get_user_plan
        from resume_app.models import Subscription

        ensure_default_plans()
        user = create_user("grace_user")
        assign_plan(user, "pro")
        sub = get_or_create_subscription(user)
        sub.status = Subscription.Status.PAST_DUE
        sub.save()
        self.assertIsNotNone(sub.past_due_since)

        # Fast forward past_due_since 6 days ago, but updated_at is touched now
        Subscription.objects.filter(pk=sub.pk).update(
            past_due_since=timezone.now() - timedelta(days=6),
            updated_at=timezone.now(),
        )
        sub.refresh_from_db()
        # Must degrade to free because past_due_since > 5 days, even though updated_at is brand new
        self.assertEqual(get_user_plan(user).slug, "free")

    @override_settings(SAAS_ENFORCE_QUOTAS=True)
    def test_pipeline_board_resume_upload_enforces_storage(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import Client
        from resume_app.subscriptions import assign_plan
        from resume_app.models import Plan

        ensure_default_plans()
        from resume_app.models import SearchProfile
        user = create_user("board_storage_user")
        SearchProfile.objects.create(owner=user, name="Initial Profile")
        assign_plan(user, "free")
        free_plan = Plan.objects.get(slug="free")
        free_plan.storage_mb = 1
        free_plan.save()

        client = Client()
        login_client(client, user)
        # Upload 2MB file
        large_pdf = SimpleUploadedFile("resume.pdf", b"%PDF-1.4" + b"X" * (2 * 1024 * 1024), content_type="application/pdf")
        resp = client.post(
            reverse("pipeline"),
            {
                "action": "save_search_profile",
                "name": "New Target Track",
                "search_term": "Python",
                "track": "SWE",
                "resume_file": large_pdf,
            },
            follow=True,
        )
        self.assertContains(resp, "Storage quota exceeded")


    def test_past_due_since_backfill_shape_and_entitlements(self):
        """Verify pre-migration shape with past_due_since=NULL respects grace and backfills."""
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import F
        from resume_app.subscriptions import get_or_create_subscription, get_user_plan
        from resume_app.models import Subscription

        ensure_default_plans()
        user = create_user("backfill_test_user")
        assign_plan(user, "pro")
        sub = get_or_create_subscription(user)
        # Simulate legacy pre-migration row where past_due_since is NULL
        Subscription.objects.filter(pk=sub.pk).update(
            status=Subscription.Status.PAST_DUE,
            past_due_since=None,
            updated_at=timezone.now() - timedelta(days=2),
        )
        sub.refresh_from_db()
        self.assertIsNone(sub.past_due_since)
        # Within 5 days of updated_at fallback -> still in grace, retains Pro
        self.assertEqual(get_user_plan(user).slug, "pro")

        # Now run backfill logic
        Subscription.objects.filter(
            status="past_due",
            past_due_since__isnull=True,
        ).update(past_due_since=F("updated_at"))
        sub.refresh_from_db()
        self.assertIsNotNone(sub.past_due_since)
        self.assertEqual(sub.past_due_since, sub.updated_at)

    def test_subscription_admin_actions(self):
        """Verify SubscriptionAdmin actions mark_active, mark_canceled, and assign_byok."""
        from resume_app.admin import SubscriptionAdmin
        from resume_app.models import Subscription
        from resume_app.subscriptions import get_or_create_subscription
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from django.utils import timezone

        ensure_default_plans()
        user = create_user("admin_sub_user")
        sub = get_or_create_subscription(user)
        sub.status = Subscription.Status.PAST_DUE
        sub.past_due_since = timezone.now()
        sub.save()
        self.assertIsNotNone(sub.past_due_since)

        admin = SubscriptionAdmin(Subscription, AdminSite())
        from django.contrib.messages.storage.fallback import FallbackStorage
        rf = RequestFactory()
        req = rf.get("/")
        admin_user = create_user("admin_runner")
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.save()
        req.user = admin_user
        setattr(req, "session", "session")
        setattr(req, "_messages", FallbackStorage(req))

        # Test mark_active clears past_due_since
        admin.mark_active(req, Subscription.objects.filter(pk=sub.pk))
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertIsNone(sub.past_due_since)

        # Test assign_byok_plan
        admin.assign_byok_plan(req, Subscription.objects.filter(pk=sub.pk))
        sub.refresh_from_db()
        self.assertEqual(sub.plan.slug, "byok")

        # Test mark_canceled
        admin.mark_canceled(req, Subscription.objects.filter(pk=sub.pk))
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.CANCELED)
        self.assertIsNone(sub.past_due_since)
