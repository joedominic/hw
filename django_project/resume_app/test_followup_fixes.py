"""Follow-up fixes from the remaining review items."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from resume_app.models import AppAutomationSettings
from resume_app.services import looks_like_pdf_upload
from resume_app.test_utils import create_user
from resume_app.views import _profile_slug_from_request


class OptimizerProfileSlugTests(TestCase):
    def test_profile_query_wins_over_track_and_session(self):
        request = RequestFactory().get("/resume/optimizer/?profile=staff-ml&track=other")
        request.session = {"optimizer_track": "session-slug"}
        self.assertEqual(_profile_slug_from_request(request), "staff-ml")


class WeeklyGoalRouteTests(TestCase):
    def test_tracker_form_saves_weekly_goal(self):
        user = create_user("goal-tenant")
        self.client.force_login(user)
        response = self.client.post(
            reverse("update_dashboard_settings"),
            {"weekly_target_applications": "12"},
        )
        self.assertEqual(response.status_code, 302)
        cfg = AppAutomationSettings.get_for_user(user)
        self.assertEqual(cfg.weekly_target_applications, 12)


class PdfUploadGuardTests(TestCase):
    def test_extension_without_pdf_header_is_rejected(self):
        upload = SimpleUploadedFile("resume.pdf", b"not a pdf", content_type="application/pdf")
        self.assertFalse(looks_like_pdf_upload(upload))
        self.assertEqual(upload.read(3), b"not")


class OpenRedirectTests(TestCase):
    @patch("resume_app.account_views.send_verification_email", return_value=True)
    def test_resend_verification_ignores_external_next(self, _send):
        user = create_user("redirect-tenant")
        self.client.force_login(user)
        response = self.client.post(
            reverse("resend_verification"),
            {"next": "https://evil.example/phish"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example", response["Location"])


class HijackReasonTests(TestCase):
    def test_acquire_without_reason_does_not_impersonate(self):
        staff = create_user("reason-staff")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        perm = Permission.objects.get(codename="can_impersonate_users")
        staff.user_permissions.add(perm)
        target = create_user("reason-target")
        self.client.force_login(staff)
        response = self.client.post(
            reverse("hijack:acquire"),
            {"user_pk": target.pk, "next": reverse("career_cockpit")},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("staff_users"))
        self.assertNotIn("hijack_history", self.client.session)


class BillingHijackTests(TestCase):
    @patch("resume_app.subscriptions.views.create_checkout_session")
    def test_hijacked_session_cannot_start_checkout(self, create_checkout):
        staff = create_user("bill-staff")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        tenant = create_user("bill-tenant")
        self.client.force_login(tenant)
        session = self.client.session
        session["hijack_history"] = [staff.pk]
        session.save()
        response = self.client.post(
            reverse("billing"),
            {"action": "checkout", "plan_slug": "pro"},
        )
        self.assertEqual(response.status_code, 302)
        create_checkout.assert_not_called()


class UnsignedStripeWebhookTests(TestCase):
    @override_settings(DEBUG=True, STRIPE_WEBHOOK_SECRET="", STRIPE_SECRET_KEY="sk_test_x")
    def test_debug_does_not_accept_unsigned_events(self):
        from django.conf import settings

        request = RequestFactory().post(
            "/billing/stripe/webhook/",
            data=json.dumps({"type": "checkout.session.completed"}),
            content_type="application/json",
        )
        with patch.object(settings, "_IN_TEST", False):
            from resume_app.subscriptions.views import stripe_webhook

            response = stripe_webhook(request)
        self.assertEqual(response.status_code, 400)


class HijackedStaffToolTests(TestCase):
    def test_prompt_save_is_forbidden_while_impersonating(self):
        staff = create_user("prompt-hijack-staff")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.client.force_login(staff)
        session = self.client.session
        session["hijack_history"] = [staff.pk]
        session.save()
        response = self.client.post(
            reverse("prompt_studio_save_api"),
            data=json.dumps({"prompt_key": "writer", "system_prompt": "x", "user_prompt": "y"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
